-- =====================================================================
--  Cactus Inventory — DB init (3FN)
--
--  Modelo:
--    unidades               (lookup, evita VARCHAR libre)
--    bodegas                (ubicaciones fisicas)
--    productos              (catalogo abstracto, 1 fila por producto)
--    stock                  (estado por (producto, bodega))
--
--  Views:
--    productos_en_bodega    (forma antigua, 1 fila por (producto, bodega))
--    productos_catalogo     (1 fila por producto abstracto, sin bodega)
--    stock_actual           (join legible para queries de inventario)
--
--  Las funciones kalman_* toman (producto_id, bodega_id, ...).
--  pending_evaluations y registros_conteo almacenan bodega_id explicitamente.
-- =====================================================================

-- =====================================================================
--  Unidades
-- =====================================================================
CREATE TABLE IF NOT EXISTS unidades (
    id     SERIAL PRIMARY KEY,
    nombre VARCHAR(20) UNIQUE NOT NULL
);
INSERT INTO unidades (nombre) VALUES ('Unidad'), ('Kilogram'), ('Liter')
ON CONFLICT (nombre) DO NOTHING;

-- =====================================================================
--  Bodegas
-- =====================================================================
CREATE TABLE IF NOT EXISTS bodegas (
    id          SERIAL PRIMARY KEY,
    nombre      VARCHAR(150) UNIQUE NOT NULL,
    creado_en   TIMESTAMP DEFAULT NOW()
);
INSERT INTO bodegas (nombre) VALUES ('bodega_default')
ON CONFLICT (nombre) DO NOTHING;

-- =====================================================================
--  Productos (catalogo abstracto, 1 fila por producto)
-- =====================================================================
CREATE TABLE IF NOT EXISTS productos (
    id              SERIAL PRIMARY KEY,
    nombre          VARCHAR(150) UNIQUE NOT NULL,
    codigo_articulo VARCHAR(20),
    unidad_id       INTEGER NOT NULL REFERENCES unidades(id) DEFAULT 1,
    q_proceso       FLOAT NOT NULL DEFAULT 5.0,
    r_medicion      FLOAT NOT NULL DEFAULT 1.0,
    umbral_sigma    FLOAT NOT NULL DEFAULT 2.0,
    creado_en       TIMESTAMP DEFAULT NOW()
);

-- =====================================================================
--  Stock (estado por bodega)
-- =====================================================================
CREATE TABLE IF NOT EXISTS stock (
    producto_id     INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    bodega_id       INTEGER NOT NULL REFERENCES bodegas(id)  ON DELETE CASCADE,
    stock_actual    FLOAT NOT NULL DEFAULT 0,
    media_kalman    FLOAT NOT NULL DEFAULT 0,
    varianza_kalman FLOAT NOT NULL DEFAULT 100.0,
    creado_en       TIMESTAMP DEFAULT NOW(),
    actualizado_en  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (producto_id, bodega_id)
);
CREATE INDEX IF NOT EXISTS idx_stock_bodega ON stock (bodega_id);

-- =====================================================================
--  Views (compatibilidad + consultas limpias)
-- =====================================================================

-- productos_en_bodega: replica la forma de la antigua tabla productos.
-- Usar solo donde realmente se necesita el (producto, bodega) explicito.
CREATE OR REPLACE VIEW productos_en_bodega AS
SELECT
    s.producto_id                                   AS id,
    p.nombre,
    p.codigo_articulo,
    u.nombre                                        AS unidad,
    s.bodega_id,
    b.nombre                                        AS bodega,
    s.stock_actual,
    s.media_kalman,
    s.varianza_kalman,
    p.q_proceso,
    p.r_medicion,
    p.umbral_sigma,
    s.creado_en,
    s.actualizado_en
FROM stock s
JOIN productos p ON p.id = s.producto_id
JOIN bodegas   b ON b.id = s.bodega_id
JOIN unidades  u ON u.id = p.unidad_id;

-- productos_catalogo: 1 fila por producto abstracto. Lo que consume el CLI
-- en el inventario global (no repite por bodega).
CREATE OR REPLACE VIEW productos_catalogo AS
SELECT
    p.id,
    p.nombre,
    p.codigo_articulo,
    u.nombre        AS unidad,
    p.q_proceso,
    p.r_medicion,
    p.umbral_sigma
FROM productos p
JOIN unidades  u ON u.id = p.unidad_id;

-- stock_actual: vista legible join de todo.
CREATE OR REPLACE VIEW stock_actual AS
SELECT
    s.producto_id,
    p.nombre        AS producto,
    p.codigo_articulo,
    u.nombre        AS unidad,
    s.bodega_id,
    b.nombre        AS bodega,
    s.stock_actual,
    s.media_kalman,
    s.varianza_kalman,
    s.actualizado_en
FROM stock s
JOIN productos p ON p.id = s.producto_id
JOIN bodegas   b ON b.id = s.bodega_id
JOIN unidades  u ON u.id = p.unidad_id;

-- =====================================================================
--  Inventario movimientos
-- =====================================================================
CREATE TABLE IF NOT EXISTS inventario_movimientos (
    id                   SERIAL PRIMARY KEY,
    producto_id          INTEGER NOT NULL REFERENCES productos(id),
    bodega_id            INTEGER NOT NULL REFERENCES bodegas(id),
    tipo                 VARCHAR(10) NOT NULL CHECK (tipo IN ('entrada', 'salida')),
    cantidad_reportada   FLOAT NOT NULL CHECK (cantidad_reportada > 0),
    residual_kalman      FLOAT,
    decision_kalman      VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE'
        CHECK (decision_kalman IN ('ACEPTADA', 'SOSPECHOSA', 'RECHAZADA', 'CONFIRMADA_MANUAL')),
    umbral_usado         FLOAT,
    stock_resultante     FLOAT,
    creado_en            TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mov_producto ON inventario_movimientos (producto_id, bodega_id);
CREATE INDEX IF NOT EXISTS idx_mov_fecha    ON inventario_movimientos (creado_en);

-- =====================================================================
--  Auditoria
-- =====================================================================
CREATE TABLE IF NOT EXISTS auditoria_log (
    id              SERIAL PRIMARY KEY,
    movimiento_id   INTEGER REFERENCES inventario_movimientos(id),
    puntaje_riesgo  FLOAT NOT NULL,
    motivo          TEXT,
    creado_en       TIMESTAMP DEFAULT NOW()
);

-- =====================================================================
--  Cola pending_evaluations
--  bodega_id es obligatorio para movimientos (no para lecturas).
-- =====================================================================
CREATE TABLE IF NOT EXISTS pending_evaluations (
    id             BIGSERIAL PRIMARY KEY,
    session_id     TEXT,
    tool_name      VARCHAR(50) NOT NULL,
    producto_id    INTEGER REFERENCES productos(id),
    bodega_id      INTEGER REFERENCES bodegas(id),
    tipo           VARCHAR(10),
    cantidad       FLOAT,
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
CREATE INDEX IF NOT EXISTS idx_pending_status  ON pending_evaluations (status) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_evaluations (session_id);

-- =====================================================================
--  Sesiones y registros de conteo
-- =====================================================================
CREATE TABLE IF NOT EXISTS sesiones_conteo (
    id              SERIAL PRIMARY KEY,
    bodega_id       INTEGER NOT NULL REFERENCES bodegas(id),
    estado          VARCHAR(20) NOT NULL DEFAULT 'activa'
        CHECK (estado IN ('activa', 'finalizada', 'cancelada')),
    iniciada_por    VARCHAR(100) DEFAULT 'anonimo',
    creado_en       TIMESTAMP DEFAULT NOW(),
    finalizado_en   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS registros_conteo (
    id                    SERIAL PRIMARY KEY,
    sesion_id             INTEGER NOT NULL REFERENCES sesiones_conteo(id),
    producto_id           INTEGER NOT NULL REFERENCES productos(id),
    bodega_id             INTEGER NOT NULL REFERENCES bodegas(id),
    cantidad_contada      FLOAT NOT NULL,
    unidad_usada          VARCHAR(20) NOT NULL,
    cantidad_normalizada  FLOAT NOT NULL,
    stock_sistema         FLOAT NOT NULL,
    decision_kalman       VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE',
    movimiento_id         INTEGER REFERENCES inventario_movimientos(id),
    pending_id            BIGINT REFERENCES pending_evaluations(id),
    creado_en             TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_registros_sesion ON registros_conteo (sesion_id);

-- =====================================================================
--  Seed: catalogo base + stock en bodega_default
-- =====================================================================
INSERT INTO productos (nombre, codigo_articulo, unidad_id) VALUES
    ('papa',      NULL, (SELECT id FROM unidades WHERE nombre = 'Kilogram')),
    ('cebolla',   NULL, (SELECT id FROM unidades WHERE nombre = 'Kilogram')),
    ('tomate',    NULL, (SELECT id FROM unidades WHERE nombre = 'Kilogram')),
    ('zanahoria', NULL, (SELECT id FROM unidades WHERE nombre = 'Kilogram')),
    ('ajo',       NULL, (SELECT id FROM unidades WHERE nombre = 'Kilogram'))
ON CONFLICT (nombre) DO NOTHING;

INSERT INTO stock (producto_id, bodega_id, stock_actual, media_kalman, varianza_kalman)
SELECT p.id, b.id, s.stock, s.stock, 100.0
FROM (VALUES
    ('papa',      50),
    ('cebolla',   30),
    ('tomate',    25),
    ('zanahoria', 40),
    ('ajo',       15)
) AS s(nombre, stock)
JOIN productos p ON p.nombre = s.nombre
JOIN bodegas   b ON b.nombre = 'bodega_default'
ON CONFLICT (producto_id, bodega_id) DO NOTHING;

-- =====================================================================
--  kalman_evaluar() — FUNCION PURA
--  Lee (producto, bodega) desde stock+productos.
-- =====================================================================
CREATE OR REPLACE FUNCTION kalman_evaluar(
    p_producto_id INTEGER,
    p_bodega_id   INTEGER,
    p_tipo        VARCHAR,
    p_cantidad    FLOAT
) RETURNS TABLE (
    decision          TEXT,
    residual          FLOAT,
    umbral            FLOAT,
    media_actual      FLOAT,
    varianza_actual   FLOAT,
    stock_proyectado  FLOAT,
    puntaje_riesgo    FLOAT
) AS $$
DECLARE
    prod          RECORD;
    p_pred        FLOAT;
    s_innov       FLOAT;
    sigma_umbral  FLOAT;
    residual_v    FLOAT;
    nuevo_stock   FLOAT;
BEGIN
    SELECT
        s.stock_actual, s.media_kalman, s.varianza_kalman,
        p.q_proceso, p.r_medicion, p.umbral_sigma
    INTO prod
    FROM stock s
    JOIN productos p ON p.id = s.producto_id
    WHERE s.producto_id = p_producto_id AND s.bodega_id = p_bodega_id;

    IF NOT FOUND THEN
        decision := 'ERROR';
        residual := NULL;  umbral := NULL;
        media_actual := NULL;  varianza_actual := NULL;
        stock_proyectado := NULL;  puntaje_riesgo := NULL;
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
--  Actualiza stock + inserta movimiento (con bodega_id explicito).
-- =====================================================================
CREATE OR REPLACE FUNCTION aplicar_movimiento_aceptado(
    p_producto_id INTEGER,
    p_bodega_id   INTEGER,
    p_tipo        VARCHAR,
    p_cantidad    FLOAT,
    p_residual    FLOAT,
    p_umbral      FLOAT
) RETURNS INTEGER AS $$
DECLARE
    prod        RECORD;
    p_pred      FLOAT;
    s_innov     FLOAT;
    k_ganancia  FLOAT;
    nuevo_stock FLOAT;
    new_id      INTEGER;
BEGIN
    SELECT s.stock_actual, s.media_kalman, s.varianza_kalman,
           p.q_proceso, p.r_medicion
    INTO prod
    FROM stock s
    JOIN productos p ON p.id = s.producto_id
    WHERE s.producto_id = p_producto_id AND s.bodega_id = p_bodega_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Stock no existe para producto=% bodega=%', p_producto_id, p_bodega_id;
    END IF;

    IF p_tipo = 'entrada' THEN
        nuevo_stock := prod.stock_actual + p_cantidad;
    ELSE
        nuevo_stock := prod.stock_actual - p_cantidad;
    END IF;

    p_pred     := prod.varianza_kalman + prod.q_proceso;
    s_innov    := p_pred + prod.r_medicion;
    k_ganancia := p_pred / s_innov;

    UPDATE stock SET
        media_kalman    = prod.media_kalman + k_ganancia * p_residual,
        varianza_kalman = (1.0 - k_ganancia) * p_pred,
        stock_actual    = nuevo_stock,
        actualizado_en  = NOW()
    WHERE producto_id = p_producto_id AND bodega_id = p_bodega_id;

    INSERT INTO inventario_movimientos (
        producto_id, bodega_id, tipo, cantidad_reportada,
        residual_kalman, decision_kalman, umbral_usado, stock_resultante
    ) VALUES (
        p_producto_id, p_bodega_id, p_tipo, p_cantidad,
        p_residual, 'ACEPTADA', p_umbral, nuevo_stock
    )
    RETURNING id INTO new_id;

    RETURN new_id;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  investigar_sospechosos()
-- =====================================================================
CREATE OR REPLACE FUNCTION investigar_sospechosos(p_producto_nombre VARCHAR DEFAULT NULL)
RETURNS TABLE(
    movimiento_id        INTEGER,
    producto_nombre      VARCHAR,
    cantidad_reportada   FLOAT,
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
    JOIN productos     p ON p.id = im.producto_id
    JOIN auditoria_log al ON al.movimiento_id = im.id
    WHERE (p_producto_nombre IS NULL OR p.nombre = p_producto_nombre)
    ORDER BY al.puntaje_riesgo DESC
    LIMIT 10;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  confirmar_movimiento() — usa bodega_id de la propia pending
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
    k_gan   FLOAT;
    ns      FLOAT;
BEGIN
    SELECT * INTO pend FROM pending_evaluations WHERE id = p_pending_id FOR UPDATE;
    IF pend IS NULL THEN
        RETURN 'Pending no encontrado';
    END IF;
    IF pend.status NOT IN ('SOSPECHOSA', 'PENDING') THEN
        RETURN 'Pending no esta en estado resoluble (' || pend.status || ')';
    END IF;

    IF p_confirmar THEN
        IF pend.bodega_id IS NULL OR pend.producto_id IS NULL THEN
            RETURN 'Pending sin bodega/producto';
        END IF;

        SELECT s.stock_actual, s.media_kalman, s.varianza_kalman,
               p.q_proceso, p.r_medicion
        INTO prod
        FROM stock s
        JOIN productos p ON p.id = s.producto_id
        WHERE s.producto_id = pend.producto_id AND s.bodega_id = pend.bodega_id
        FOR UPDATE;

        IF NOT FOUND THEN
            RETURN 'Stock no existe para producto=' || pend.producto_id || ' bodega=' || pend.bodega_id;
        END IF;

        IF pend.tipo = 'entrada' THEN
            ns := prod.stock_actual + pend.cantidad;
        ELSE
            ns := prod.stock_actual - pend.cantidad;
        END IF;

        p_pred := prod.varianza_kalman + prod.q_proceso;
        s_innov := p_pred + prod.r_medicion;
        k_gan := p_pred / s_innov;

        UPDATE stock SET
            media_kalman    = prod.media_kalman + k_gan * pend.residual,
            varianza_kalman = (1.0 - k_gan) * p_pred,
            stock_actual    = ns,
            actualizado_en  = NOW()
        WHERE producto_id = pend.producto_id AND bodega_id = pend.bodega_id;

        INSERT INTO inventario_movimientos (
            producto_id, bodega_id, tipo, cantidad_reportada,
            residual_kalman, decision_kalman, umbral_usado, stock_resultante
        ) VALUES (
            pend.producto_id, pend.bodega_id, pend.tipo, pend.cantidad,
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
--  Notificacion opcional
-- =====================================================================
CREATE OR REPLACE FUNCTION notify_pending_insert()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('pending_eval_channel', NEW.id::TEXT);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_notify_pending ON pending_evaluations;
CREATE TRIGGER trigger_notify_pending
    AFTER INSERT ON pending_evaluations
    FOR EACH ROW
    WHEN (NEW.status = 'PENDING')
    EXECUTE FUNCTION notify_pending_insert();

-- =====================================================================
--  Constraint: productos con unidad "Unidad" no admiten decimales
--  en ningun campo de cantidad. Para Kilogram / Liter (y futuros)
--  los decimales si estan permitidos.
--  Aplicado a: inventario_movimientos, registros_conteo,
--  pending_evaluations, stock.
-- =====================================================================

CREATE OR REPLACE FUNCTION check_cantidad_unidad()
RETURNS TRIGGER AS $$
DECLARE
    v_unidad_nombre VARCHAR(20);
    v_producto_id  INTEGER;
    v_campo        TEXT;
    v_valor        FLOAT;
BEGIN
    -- 1) Resolver el producto_id segun la tabla que dispara
    IF TG_TABLE_NAME = 'inventario_movimientos' THEN
        v_producto_id := NEW.producto_id;
    ELSIF TG_TABLE_NAME = 'registros_conteo' THEN
        v_producto_id := NEW.producto_id;
    ELSIF TG_TABLE_NAME = 'pending_evaluations' THEN
        v_producto_id := NEW.producto_id;
    ELSIF TG_TABLE_NAME = 'stock' THEN
        v_producto_id := NEW.producto_id;
    ELSE
        RETURN NEW;
    END IF;

    -- 2) Sin producto, no hay nada que validar (movimientos sin producto son raros pero validos)
    IF v_producto_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- 3) Buscar la unidad del producto
    SELECT u.nombre INTO v_unidad_nombre
    FROM productos p
    JOIN unidades  u ON u.id = p.unidad_id
    WHERE p.id = v_producto_id;

    IF v_unidad_nombre IS NULL THEN
        RETURN NEW;
    END IF;

    -- 4) Si la unidad NO es "Unidad", todo permitido (decimales OK)
    IF v_unidad_nombre <> 'Unidad' THEN
        RETURN NEW;
    END IF;

    -- 5) Validar los campos de cantidad segun la tabla
    --    Usamos un loop con un ARRAY de (campo, valor) para no repetir el IF.
    IF TG_TABLE_NAME = 'inventario_movimientos' THEN
        v_campo := 'cantidad_reportada';
        v_valor := NEW.cantidad_reportada;
        IF v_valor IS NOT NULL AND v_valor <> ROUND(v_valor) THEN
            RAISE EXCEPTION 'inventario_movimientos.% debe ser entero para productos con unidad "Unidad" (producto_id=%, recibido: %)',
                v_campo, v_producto_id, v_valor
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

    ELSIF TG_TABLE_NAME = 'registros_conteo' THEN
        FOREACH v_campo IN ARRAY ARRAY['cantidad_contada', 'cantidad_normalizada'] LOOP
            IF v_campo = 'cantidad_contada' THEN
                v_valor := NEW.cantidad_contada;
            ELSE
                v_valor := NEW.cantidad_normalizada;
            END IF;
            IF v_valor IS NOT NULL AND v_valor <> ROUND(v_valor) THEN
                RAISE EXCEPTION 'registros_conteo.% debe ser entero para productos con unidad "Unidad" (producto_id=%, recibido: %)',
                    v_campo, v_producto_id, v_valor
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END LOOP;

    ELSIF TG_TABLE_NAME = 'pending_evaluations' THEN
        v_campo := 'cantidad';
        v_valor := NEW.cantidad;
        IF v_valor IS NOT NULL AND v_valor <> ROUND(v_valor) THEN
            RAISE EXCEPTION 'pending_evaluations.% debe ser entero para productos con unidad "Unidad" (producto_id=%, recibido: %)',
                v_campo, v_producto_id, v_valor
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

    ELSIF TG_TABLE_NAME = 'stock' THEN
        v_campo := 'stock_actual';
        v_valor := NEW.stock_actual;
        IF v_valor IS NOT NULL AND v_valor <> ROUND(v_valor) THEN
            RAISE EXCEPTION 'stock.% debe ser entero para productos con unidad "Unidad" (producto_id=%, recibido: %)',
                v_campo, v_producto_id, v_valor
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

--  Triggers (idempotentes: drop + create)
DROP TRIGGER IF EXISTS trg_cantidad_unidad_movimientos ON inventario_movimientos;
CREATE TRIGGER trg_cantidad_unidad_movimientos
    BEFORE INSERT OR UPDATE ON inventario_movimientos
    FOR EACH ROW
    EXECUTE FUNCTION check_cantidad_unidad();

DROP TRIGGER IF EXISTS trg_cantidad_unidad_registros ON registros_conteo;
CREATE TRIGGER trg_cantidad_unidad_registros
    BEFORE INSERT OR UPDATE ON registros_conteo
    FOR EACH ROW
    EXECUTE FUNCTION check_cantidad_unidad();

DROP TRIGGER IF EXISTS trg_cantidad_unidad_pending ON pending_evaluations;
CREATE TRIGGER trg_cantidad_unidad_pending
    BEFORE INSERT OR UPDATE ON pending_evaluations
    FOR EACH ROW
    EXECUTE FUNCTION check_cantidad_unidad();

DROP TRIGGER IF EXISTS trg_cantidad_unidad_stock ON stock;
CREATE TRIGGER trg_cantidad_unidad_stock
    BEFORE INSERT OR UPDATE ON stock
    FOR EACH ROW
    EXECUTE FUNCTION check_cantidad_unidad();
