INSERT INTO pending_evaluations (session_id, tool_name, producto_id, bodega_id, tipo, cantidad, payload)
VALUES ('test-cli', 'agregar_inventario', 1, 1, 'entrada', 4,
        '{"producto":"papa","cantidad":4}'::jsonb)
RETURNING id;
