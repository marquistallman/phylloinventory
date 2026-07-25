INSERT INTO pending_evaluations (session_id, tool_name, producto_id, bodega_id, tipo, cantidad, payload)
VALUES ('test-cli', 'agregar_inventario', 1, 1, 'entrada', 200,
        '{"producto":"papa","cantidad":200}'::jsonb)
RETURNING id;
