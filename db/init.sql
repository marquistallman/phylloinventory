
CREATE TABLE productos (
    id SERIAL PRIMARY KEY,
    nombre VARCHAR(100) UNIQUE NOT NULL,
    stock_actual INTEGER NOT NULL DEFAULT 0,
    media_kalman FLOAT NOT NULL DEFAULT 0,
    varianza_kalman FLOAT NOT NULL DEFAULT 100.0,
    q_proceso FLOAT NOT NULL DEFAULT 5.0,
    r_medicion FLOAT NOT NULL DEFAULT 1.0,
    umbral_sigma FLOAT NOT NULL DEFAULT 2.0,
    creado_en TIMESTAMP DEFAULT NOW(),
    actualizado_en TIMESTAMP DEFAULT NOW()
);

CREATE TABLE inventario_movimientos (
    id SERIAL PRIMARY KEY,
    producto_id INTEGER NOT NULL REFERENCES productos(id),
    tipo VARCHAR(10) NOT NULL CHECK (tipo IN ('entrada', 'salida')),
    cantidad_reportada INTEGER NOT NULL CHECK (cantidad_reportada > 0),
    residual_kalman FLOAT,
    decision_kalman VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE'
        CHECK (decision_kalman IN ('ACEPTADA', 'SOSPECHOSA', 'RECHAZADA', 'CONFIRMADA_MANUAL')),
    umbral_usado FLOAT,
    stock_resultante INTEGER,
    creado_en TIMESTAMP DEFAULT NOW()
);

CREATE TABLE auditoria_log (
    id SERIAL PRIMARY KEY,
    movimiento_id INTEGER REFERENCES inventario_movimientos(id),
    puntaje_riesgo FLOAT NOT NULL,
    motivo TEXT,
    creado_en TIMESTAMP DEFAULT NOW()
);

INSERT INTO productos (nombre, stock_actual, media_kalman, varianza_kalman) VALUES
    ('papa', 50, 50, 100.0),
    ('cebolla', 30, 30, 100.0),
    ('tomate', 25, 25, 100.0),
    ('zanahoria', 40, 40, 100.0),
    ('ajo', 15, 15, 100.0);

CREATE OR REPLACE FUNCTION kalman_evaluar_movimiento()
RETURNS TRIGGER AS $$
DECLARE
    prod RECORD;
    p_pred FLOAT;
    s_innov FLOAT;
    residual FLOAT;
    k_ganancia FLOAT;
    sigma_umbral FLOAT;
    nuevo_stock INTEGER;
BEGIN
    SELECT * INTO prod FROM productos WHERE id = NEW.producto_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Producto con id % no existe', NEW.producto_id;
    END IF;

    p_pred := prod.varianza_kalman + prod.q_proceso;

    IF NEW.tipo = 'entrada' THEN
        nuevo_stock := prod.stock_actual + NEW.cantidad_reportada;
    ELSE
        nuevo_stock := prod.stock_actual - NEW.cantidad_reportada;
    END IF;

    residual := nuevo_stock - prod.media_kalman;
    s_innov := p_pred + prod.r_medicion;
    sigma_umbral := prod.umbral_sigma * sqrt(s_innov);

    NEW.residual_kalman := residual;
    NEW.umbral_usado := sigma_umbral;

    IF abs(residual) <= sigma_umbral THEN
        k_ganancia := p_pred / s_innov;

        UPDATE productos SET
            media_kalman = prod.media_kalman + k_ganancia * residual,
            varianza_kalman = (1.0 - k_ganancia) * p_pred,
            stock_actual = nuevo_stock,
            actualizado_en = NOW()
        WHERE id = prod.id;

        NEW.decision_kalman := 'ACEPTADA';
        NEW.stock_resultante := nuevo_stock;
    ELSE
        NEW.decision_kalman := 'SOSPECHOSA';
        NEW.stock_resultante := prod.stock_actual;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_kalman_inventario
    BEFORE INSERT ON inventario_movimientos
    FOR EACH ROW
    EXECUTE FUNCTION kalman_evaluar_movimiento();

CREATE OR REPLACE FUNCTION auditoria_post_insert()
RETURNS TRIGGER AS $$
DECLARE
    puntaje FLOAT;
    motivo_text TEXT;
    prod RECORD;
BEGIN
    IF NEW.decision_kalman = 'SOSPECHOSA' THEN
        SELECT varianza_kalman, q_proceso, r_medicion
        INTO prod FROM productos WHERE id = NEW.producto_id;

        puntaje := abs(NEW.residual_kalman) / sqrt(prod.varianza_kalman + prod.q_proceso + prod.r_medicion);

        motivo_text := 'Residual ' || round(NEW.residual_kalman::numeric, 2)
                    || ' excede umbral ' || round(NEW.umbral_usado::numeric, 2)
                    || ' (' || round(puntaje::numeric, 2) || 'σ)';

        INSERT INTO auditoria_log (movimiento_id, puntaje_riesgo, motivo)
        VALUES (NEW.id, puntaje, motivo_text);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_auditoria_post_insert
    AFTER INSERT ON inventario_movimientos
    FOR EACH ROW
    EXECUTE FUNCTION auditoria_post_insert();

CREATE OR REPLACE FUNCTION investigar_sospechosos(p_producto_nombre VARCHAR DEFAULT NULL)
RETURNS TABLE(
    movimiento_id INTEGER,
    producto_nombre VARCHAR,
    cantidad_reportada INTEGER,
    tipo VARCHAR,
    residual FLOAT,
    puntaje_riesgo FLOAT,
    decision VARCHAR,
    fecha TIMESTAMP
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

CREATE OR REPLACE FUNCTION confirmar_movimiento(p_movimiento_id INTEGER, p_confirmar BOOLEAN)
RETURNS TEXT AS $$
DECLARE
    mov RECORD;
    prod RECORD;
    p_pred FLOAT;
    s_innov FLOAT;
    k_ganancia FLOAT;
    nuevo_stock INTEGER;
BEGIN
    SELECT * INTO mov FROM inventario_movimientos WHERE id = p_movimiento_id;
    IF mov IS NULL THEN
        RETURN 'Movimiento no encontrado';
    END IF;

    IF mov.decision_kalman != 'SOSPECHOSA' THEN
        RETURN 'El movimiento no esta marcado como sospechoso';
    END IF;

    SELECT * INTO prod FROM productos WHERE id = mov.producto_id FOR UPDATE;

    p_pred := prod.varianza_kalman + prod.q_proceso;
    s_innov := p_pred + prod.r_medicion;

    IF p_confirmar THEN
        IF mov.tipo = 'entrada' THEN
            nuevo_stock := prod.stock_actual + mov.cantidad_reportada;
        ELSE
            nuevo_stock := prod.stock_actual - mov.cantidad_reportada;
        END IF;

        k_ganancia := p_pred / s_innov;

        UPDATE productos SET
            media_kalman = prod.media_kalman + k_ganancia * mov.residual_kalman,
            varianza_kalman = (1.0 - k_ganancia) * p_pred,
            stock_actual = nuevo_stock,
            actualizado_en = NOW()
        WHERE id = prod.id;

        UPDATE inventario_movimientos SET
            decision_kalman = 'CONFIRMADA_MANUAL',
            stock_resultante = nuevo_stock
        WHERE id = p_movimiento_id;

        RETURN 'Movimiento confirmado. Stock actualizado a ' || nuevo_stock;
    ELSE
        UPDATE inventario_movimientos SET
            decision_kalman = 'RECHAZADA'
        WHERE id = p_movimiento_id;

        RETURN 'Movimiento rechazado. Stock no modificado.';
    END IF;
END;
$$ LANGUAGE plpgsql;
