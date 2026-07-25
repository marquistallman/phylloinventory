SELECT confirmar_movimiento(2, true) AS result;
SELECT id, status, decision, movimiento_id FROM pending_evaluations WHERE id=2;
SELECT producto, stock_actual, bodega FROM stock_actual WHERE producto='papa';
