-- =====================================================================
--  Migration 004: sincronizar registros_conteo.decision_kalman al
--  resolver una alerta (confirmar_movimiento).
--  Aplica a DBs existentes (init.sql ya lo trae para fresh installs).
--  Idempotente: se puede correr multiples veces sin error.
-- =====================================================================
--
--  Bug que arregla: confirmar_movimiento() actualizaba
--  pending_evaluations.status a CONFIRMADA_MANUAL/RECHAZADA, pero nunca
--  tocaba registros_conteo.decision_kalman de ESA MISMA fila (solo lo
--  hacia para conteos COMPETIDORES invalidados). Resultado: una alerta ya
--  resuelta seguia apareciendo como 'SOSPECHOSA' para siempre en
--  /api/reporte/sospechosos/{sesion_id} y en el chip "Alertas" de Buscar,
--  aunque el pending ya estuviera resuelto.
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

        --  Sincronizar la fila de registros_conteo de ESTE pending (si vino
        --  de una sesion de conteo). Sin esto, el registro queda marcado
        --  para siempre como 'SOSPECHOSA' en los reportes/filtros aunque ya
        --  se haya confirmado manualmente.
        UPDATE registros_conteo
        SET decision_kalman = 'CONFIRMADA_MANUAL'
        WHERE pending_id = p_pending_id;

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

        --  Idem rama de confirmacion: sincronizar registros_conteo de ESTE
        --  pending para que deje de aparecer como alerta pendiente.
        UPDATE registros_conteo
        SET decision_kalman = 'RECHAZADA'
        WHERE pending_id = p_pending_id;

        RETURN 'Movimiento rechazado. Stock no modificado.';
    END IF;
END;
$$ LANGUAGE plpgsql;
