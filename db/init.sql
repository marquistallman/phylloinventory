-- =====================================================================
--  Cactus Inventory — DB init (microservicios)
--  Sin triggers. Toda la logica Kalman la ejecuta el kalman-worker (Go).
--  La DB expone solo funciones puras y una tabla-cola.
-- =====================================================================

CREATE TABLE productos (
    id              SERIAL PRIMARY KEY,
    nombre          VARCHAR(100) UNIQUE NOT NULL,
    stock_actual    INTEGER NOT NULL DEFAULT 0,
    media_kalman    FLOAT NOT NULL DEFAULT 0,
    varianza_kalman FLOAT NOT NULL DEFAULT 100.0,
    q_proceso       FLOAT NOT NULL DEFAULT 5.0,
    r_medicion      FLOAT NOT NULL DEFAULT 1.0,
    umbral_sigma    FLOAT NOT NULL DEFAULT 2.0,
    creado_en       TIMESTAMP DEFAULT NOW(),
    actualizado_en  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE inventario_movimientos (
    id                   SERIAL PRIMARY KEY,
    producto_id          INTEGER NOT NULL REFERENCES productos(id),
    tipo                 VARCHAR(10) NOT NULL CHECK (tipo IN ('entrada', 'salida')),
    cantidad_reportada   INTEGER NOT NULL CHECK (cantidad_reportada > 0),
    residual_kalman      FLOAT,
    decision_kalman      VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE'
        CHECK (decision_kalman IN ('ACEPTADA', 'SOSPECHOSA', 'RECHAZADA', 'CONFIRMADA_MANUAL')),
    umbral_usado         FLOAT,
    stock_resultante     INTEGER,
    creado_en            TIMESTAMP DEFAULT NOW()
);

CREATE TABLE auditoria_log (
    id              SERIAL PRIMARY KEY,
    movimiento_id   INTEGER REFERENCES inventario_movimientos(id),
    puntaje_riesgo  FLOAT NOT NULL,
    motivo          TEXT,
    creado_en       TIMESTAMP DEFAULT NOW()
);

-- =====================================================================
--  Cola: pending_evaluations
--  La llenan needle-service / openrouter-service.
--  La consume kalman-worker (Go) con SELECT ... FOR UPDATE SKIP LOCKED.
-- =====================================================================
CREATE TABLE pending_evaluations (
    id             BIGSERIAL PRIMARY KEY,
    session_id     TEXT,
    tool_name      VARCHAR(50) NOT NULL,
    producto_id    INTEGER REFERENCES productos(id),
    tipo           VARCHAR(10),
    cantidad       INTEGER,
    payload        JSONB,
    status         VARCHAR(20) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'ACEPTADA', 'SOSPECHOSA', 'CONFIRMADA_MANUAL', 'RECHAZADA')),
    decision       TEXT,
    residual       FLOAT,
    umbral         FLOAT,
    movimiento_id  INTEGER REFERENCES inventario_movimientos(id),
    locked_by      TEXT,
    locked_at      TIMESTAMP,
    created_at     TIMESTAMP DEFAULT NOW(),
    resolved_at    TIMESTAMP
);

CREATE INDEX idx_pending_status ON pending_evaluations (status) WHERE status = 'PENDING';
CREATE INDEX idx_pending_session ON pending_evaluations (session_id);

-- =====================================================================
--  Seed
-- =====================================================================
INSERT INTO productos (nombre, stock_actual, media_kalman, varianza_kalman) VALUES
    ('papa', 50, 50, 100.0),
    ('cebolla', 30, 30, 100.0),
    ('tomate', 25, 25, 100.0),
    ('zanahoria', 40, 40, 100.0),
    ('ajo', 15, 15, 100.0);

-- =====================================================================
--  kalman_evaluar() — FUNCION PURA
--  NO escribe nada. Solo computa. El worker decide que hacer.
--  Retorna: decision (PASA|FALLA), residual, umbral, media, varianza, stock_proyectado
-- =====================================================================
CREATE OR REPLACE FUNCTION kalman_evaluar(
    p_producto_id INTEGER,
    p_tipo        VARCHAR,
    p_cantidad    INTEGER
) RETURNS TABLE (
    decision          TEXT,
    residual          FLOAT,
    umbral            FLOAT,
    media_actual      FLOAT,
    varianza_actual   FLOAT,
    stock_proyectado  INTEGER,
    puntaje_riesgo    FLOAT
) AS $$
DECLARE
    prod          RECORD;
    p_pred        FLOAT;
    s_innov       FLOAT;
    sigma_umbral  FLOAT;
    residual_v    FLOAT;
    nuevo_stock   INTEGER;
BEGIN
    SELECT * INTO prod FROM productos WHERE id = p_producto_id;
    IF NOT FOUND THEN
        decision := 'ERROR';
        residual := NULL;
        umbral   := NULL;
        media_actual := NULL;
        varianza_actual := NULL;
        stock_proyectado := NULL;
        puntaje_riesgo := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    p_pred := prod.varianza_kalman + prod.q_proceso;

    IF p_tipo = 'entrada' THEN
        nuevo_stock := prod.stock_actual + p_cantidad;
    ELSE
        nuevo_stock := prod.stock_actual - p_cantidad;
    END IF;

    residual_v   := nuevo_stock - prod.media_kalman;
    s_innov      := p_pred + prod.r_medicion;
    sigma_umbral := prod.umbral_sigma * sqrt(s_innov);

    IF abs(residual_v) <= sigma_umbral THEN
        decision := 'PASA';
    ELSE
        decision := 'FALLA';
    END IF;

    residual         := residual_v;
    umbral           := sigma_umbral;
    media_actual     := prod.media_kalman;
    varianza_actual  := prod.varianza_kalman;
    stock_proyectado := nuevo_stock;
    puntaje_riesgo   := abs(residual_v) / NULLIF(sigma_umbral, 0);

    RETURN NEXT;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  aplicar_movimiento_aceptado()
--  La llama el worker despues de kalman_evaluar() == 'PASA'.
--  Inserta el movimiento, actualiza Kalman state del producto.
--  Retorna el id del movimiento creado.
-- =====================================================================
CREATE OR REPLACE FUNCTION aplicar_movimiento_aceptado(
    p_producto_id INTEGER,
    p_tipo        VARCHAR,
    p_cantidad    INTEGER,
    p_residual    FLOAT,
    p_umbral      FLOAT
) RETURNS INTEGER AS $$
DECLARE
    prod        RECORD;
    p_pred      FLOAT;
    s_innov     FLOAT;
    k_ganancia  FLOAT;
    nuevo_stock INTEGER;
    new_id      INTEGER;
BEGIN
    SELECT * INTO prod FROM productos WHERE id = p_producto_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Producto % no existe', p_producto_id;
    END IF;

    IF p_tipo = 'entrada' THEN
        nuevo_stock := prod.stock_actual + p_cantidad;
    ELSE
        nuevo_stock := prod.stock_actual - p_cantidad;
    END IF;

    p_pred     := prod.varianza_kalman + prod.q_proceso;
    s_innov    := p_pred + prod.r_medicion;
    k_ganancia := p_pred / s_innov;

    UPDATE productos SET
        media_kalman    = prod.media_kalman + k_ganancia * p_residual,
        varianza_kalman = (1.0 - k_ganancia) * p_pred,
        stock_actual    = nuevo_stock,
        actualizado_en  = NOW()
    WHERE id = p_producto_id;

    INSERT INTO inventario_movimientos (
        producto_id, tipo, cantidad_reportada,
        residual_kalman, decision_kalman, umbral_usado, stock_resultante
    ) VALUES (
        p_producto_id, p_tipo, p_cantidad,
        p_residual, 'ACEPTADA', p_umbral, nuevo_stock
    )
    RETURNING id INTO new_id;

    RETURN new_id;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  investigar_sospechosos() — sin cambios
-- =====================================================================
CREATE OR REPLACE FUNCTION investigar_sospechosos(p_producto_nombre VARCHAR DEFAULT NULL)
RETURNS TABLE(
    movimiento_id        INTEGER,
    producto_nombre      VARCHAR,
    cantidad_reportada   INTEGER,
    tipo                 VARCHAR,
    residual             FLOAT,
    puntaje_riesgo       FLOAT,
    decision             VARCHAR,
    fecha                TIMESTAMP
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        im.id,
        p.nombre,
        im.cantidad_reportada,
        im.tipo,
        im.residual_kalman,
        al.puntaje_riesgo,
        im.decision_kalman,
        im.creado_en
    FROM inventario_movimientos im
    JOIN productos p ON p.id = im.producto_id
    JOIN auditoria_log al ON al.movimiento_id = im.id
    WHERE (p_producto_nombre IS NULL OR p.nombre = p_producto_nombre)
    ORDER BY al.puntaje_riesgo DESC
    LIMIT 10;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  confirmar_movimiento() — usado por el worker al resolver una sospecha
--  Busca la fila PENDIENTE mas reciente del session_id o por movimiento_id
--  y la resuelve.
-- =====================================================================
CREATE OR REPLACE FUNCTION confirmar_movimiento(
    p_pending_id   BIGINT,
    p_confirmar    BOOLEAN
) RETURNS TEXT AS $$
DECLARE
    pend    RECORD;
    prod    RECORD;
    p_pred  FLOAT;
    s_innov FLOAT;
    k_gan  FLOAT;
    ns      INTEGER;
BEGIN
    SELECT * INTO pend FROM pending_evaluations WHERE id = p_pending_id FOR UPDATE;
    IF pend IS NULL THEN
        RETURN 'Pending no encontrado';
    END IF;
    IF pend.status NOT IN ('SOSPECHOSA', 'PENDING') THEN
        RETURN 'Pending no esta en estado resoluble (' || pend.status || ')';
    END IF;

    SELECT * INTO prod FROM productos WHERE id = pend.producto_id FOR UPDATE;

    IF p_confirmar THEN
        IF pend.tipo = 'entrada' THEN
            ns := prod.stock_actual + pend.cantidad;
        ELSE
            ns := prod.stock_actual - pend.cantidad;
        END IF;

        p_pred := prod.varianza_kalman + prod.q_proceso;
        s_innov := p_pred + prod.r_medicion;
        k_gan := p_pred / s_innov;

        UPDATE productos SET
            media_kalman    = prod.media_kalman + k_gan * pend.residual,
            varianza_kalman = (1.0 - k_gan) * p_pred,
            stock_actual    = ns,
            actualizado_en  = NOW()
        WHERE id = prod.id;

        INSERT INTO inventario_movimientos (
            producto_id, tipo, cantidad_reportada,
            residual_kalman, decision_kalman, umbral_usado, stock_resultante
        ) VALUES (
            pend.producto_id, pend.tipo, pend.cantidad,
            pend.residual, 'CONFIRMADA_MANUAL', pend.umbral, ns
        ) RETURNING id INTO pend.movimiento_id;

        UPDATE pending_evaluations
        SET status = 'CONFIRMADA_MANUAL', resolved_at = NOW()
        WHERE id = p_pending_id;

        RETURN 'Movimiento confirmado. Stock actualizado a ' || ns;
    ELSE
        UPDATE pending_evaluations
        SET status = 'RECHAZADA', resolved_at = NOW()
        WHERE id = p_pending_id;

        RETURN 'Movimiento rechazado. Stock no modificado.';
    END IF;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  Notificacion opcional: el worker puede usar LISTEN/NOTIFY ademas del poll
-- =====================================================================
CREATE OR REPLACE FUNCTION notify_pending_insert()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('pending_eval_channel', NEW.id::TEXT);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

--  Este trigger SI se permite: solo hace NOTIFY, no afecta data.
CREATE TRIGGER trigger_notify_pending
    AFTER INSERT ON pending_evaluations
    FOR EACH ROW
    WHEN (NEW.status = 'PENDING')
    EXECUTE FUNCTION notify_pending_insert();
