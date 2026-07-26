-- =====================================================================
--  Migration 003: conteo absoluto ("hay N" en vez de "agregue/saque N")
--  Aplica a DBs existentes (init.sql ya lo trae para fresh installs).
--  Idempotente: se puede correr multiples veces sin error.
-- =====================================================================
--
--  Idea central (ver charla del proyecto): un conteo fisico ("hay 3
--  papas") NO es un delta a sumar/restar, es una medicion absoluta del
--  estado. Pero matematicamente, si el delta se calcula UNA SOLA VEZ
--  contra el stock vigente en el momento del conteo (delta0 = N -
--  stock_en_ese_momento) y se guarda como cualquier movimiento
--  entrada/salida normal, se puede reusar el 100% de kalman_evaluar() /
--  aplicar_movimiento_aceptado() / confirmar_movimiento() sin tocarlos:
--  nuevo_stock = stock + delta0 = N (identidad algebraica).
--
--  Esto tambien preserva el comportamiento correcto de
--  "movimientos legitimos en el medio": si mientras el conteo esperaba
--  confirmacion humana entro una compra real, confirmar_movimiento ya
--  encadena esa entrada porque usa el stock_actual FRESCO al confirmar
--  + el delta0 guardado.
--
--  Lo unico que agrega esta migracion es la contraparte de ESE caso: si
--  DOS conteos del mismo producto quedan pendientes (dos personas
--  contando lo mismo), el segundo que se resuelve invalida al resto —
--  porque dos conteos no son deltas que se compongan sumando, son dos
--  mediciones competidoras de la misma realidad.
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

        --  Conteo absoluto: cualquier OTRO registrar_conteo del mismo
        --  producto+bodega que siga esperando confirmacion quedo con un
        --  baseline de stock viejo (se calculo contra el stock de ANTES
        --  de que este se confirmara). Aplicarlo despues seria sumar/
        --  restar un delta que ya no tiene sentido -> se invalida.
        IF pend.tool_name = 'registrar_conteo' THEN
            UPDATE pending_evaluations
            SET status = 'RECHAZADA', decision = 'invalidado_por_conteo_mas_reciente', resolved_at = NOW()
            WHERE tool_name = 'registrar_conteo'
              AND producto_id = pend.producto_id AND bodega_id = pend.bodega_id
              AND status IN ('PENDING', 'SOSPECHOSA')
              AND id <> pend.id;

            UPDATE registros_conteo
            SET decision_kalman = 'RECHAZADA'
            WHERE pending_id IN (
                SELECT id FROM pending_evaluations
                WHERE tool_name = 'registrar_conteo'
                  AND producto_id = pend.producto_id AND bodega_id = pend.bodega_id
                  AND status = 'RECHAZADA' AND decision = 'invalidado_por_conteo_mas_reciente'
            );
        END IF;

        RETURN 'Movimiento confirmado. Stock actualizado a ' || ns;
    ELSE
        UPDATE pending_evaluations
        SET status = 'RECHAZADA', resolved_at = NOW()
        WHERE id = p_pending_id;

        RETURN 'Movimiento rechazado. Stock no modificado.';
    END IF;
END;
$$ LANGUAGE plpgsql;
