-- =====================================================================
--  Migration 002: trigger de enteros para unidad "Unidad"
--  Aplica a DBs existentes (init.sql ya lo trae para fresh installs).
--  Es idempotente: se puede correr multiples veces sin error.
-- =====================================================================
--
--  Cubre las tablas:
--    - inventario_movimientos.cantidad_reportada
--    - registros_conteo.cantidad_contada
--    - registros_conteo.cantidad_normalizada
--    - pending_evaluations.cantidad
--    - stock.stock_actual
--
--  Regla: si el producto tiene unidad_id apuntando a "Unidad" (id=1), la
--  cantidad debe ser un entero. Para Kilogram / Liter los decimales
--  estan permitidos.
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

    -- 2) Sin producto, no hay nada que validar
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

--  Triggers idempotentes
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
