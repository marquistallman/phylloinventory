-- =====================================================================
--  Migration 001: Normalize a 3FN
--  Separa productos (catalogo) de stock (por bodega), crea unidades.
--  Idempotente: se puede correr varias veces sin efecto.
--  Si init.sql ya corrio con el esquema nuevo, este script es un no-op.
-- =====================================================================

BEGIN;

-- 0. unidades
CREATE TABLE IF NOT EXISTS unidades (
    id     SERIAL PRIMARY KEY,
    nombre VARCHAR(20) UNIQUE NOT NULL
);
INSERT INTO unidades (nombre) VALUES ('Unidad'), ('Kilogram'), ('Liter')
ON CONFLICT (nombre) DO NOTHING;

-- 1. Detectar si existe la tabla productos VIEJA (sin normalizar):
--    tiene columna bodega_id.
DO $$
DECLARE
    v_old_has_bodega BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'productos'
          AND column_name  = 'bodega_id'
    ) INTO v_old_has_bodega;

    IF v_old_has_bodega THEN
        -- ------------------------------------------------------------
        --  Migracion desde esquema viejo
        -- ------------------------------------------------------------
        -- 1a. Crear catalogo nuevo
        CREATE TABLE productos_new (
            id              SERIAL PRIMARY KEY,
            nombre          VARCHAR(150) UNIQUE NOT NULL,
            codigo_articulo VARCHAR(20),
            unidad_id       INTEGER NOT NULL REFERENCES unidades(id) DEFAULT 1,
            q_proceso       FLOAT NOT NULL DEFAULT 5.0,
            r_medicion      FLOAT NOT NULL DEFAULT 1.0,
            umbral_sigma    FLOAT NOT NULL DEFAULT 2.0,
            creado_en       TIMESTAMP DEFAULT NOW()
        );

        -- 1b. Volcar productos distintos al catalogo nuevo
        INSERT INTO productos_new (nombre, codigo_articulo, unidad_id,
                                   q_proceso, r_medicion, umbral_sigma)
        SELECT DISTINCT p.nombre, p.codigo_articulo, u.id,
                        p.q_proceso, p.r_medicion, p.umbral_sigma
        FROM productos p
        JOIN unidades u ON u.nombre = p.unidad
        ON CONFLICT (nombre) DO NOTHING;

        -- 1c. Crear tabla stock (idempotente)
        CREATE TABLE IF NOT EXISTS stock (
            producto_id     INTEGER NOT NULL REFERENCES productos_new(id) ON DELETE CASCADE,
            bodega_id       INTEGER NOT NULL REFERENCES bodegas(id)      ON DELETE CASCADE,
            stock_actual    FLOAT NOT NULL DEFAULT 0,
            media_kalman    FLOAT NOT NULL DEFAULT 0,
            varianza_kalman FLOAT NOT NULL DEFAULT 100.0,
            creado_en       TIMESTAMP DEFAULT NOW(),
            actualizado_en  TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (producto_id, bodega_id)
        );

        -- 1d. Volcar stock desde la tabla vieja
        INSERT INTO stock (producto_id, bodega_id,
                           stock_actual, media_kalman, varianza_kalman,
                           creado_en, actualizado_en)
        SELECT pn.id, p.bodega_id,
               p.stock_actual, p.media_kalman, p.varianza_kalman,
               p.creado_en, p.actualizado_en
        FROM productos p
        JOIN productos_new pn ON pn.nombre = p.nombre
        ON CONFLICT (producto_id, bodega_id) DO NOTHING;

        -- 1e. Drop viejo, rename nuevo
        DROP TABLE productos CASCADE;
        ALTER TABLE productos_new RENAME TO productos;
        ALTER TABLE productos_new_id_seq RENAME TO productos_id_seq;

        -- 1f. Anadir bodega_id a pending_evaluations si falta
        ALTER TABLE pending_evaluations
            ADD COLUMN IF NOT EXISTS bodega_id INTEGER REFERENCES bodegas(id);

        -- 1g. Anadir bodega_id a registros_conteo si falta
        ALTER TABLE registros_conteo
            ADD COLUMN IF NOT EXISTS bodega_id INTEGER REFERENCES bodegas(id);

        -- 1h. Anadir bodega_id a inventario_movimientos si falta
        ALTER TABLE inventario_movimientos
            ADD COLUMN IF NOT EXISTS bodega_id INTEGER REFERENCES bodegas(id);

        RAISE NOTICE 'Migracion 001: esquema viejo migrado a 3FN.';
    ELSE
        -- Esquema ya normalizado, no-op
        RAISE NOTICE 'Migracion 001: esquema ya en 3FN, nada que hacer.';
    END IF;
END $$;

-- 2. Garantizar tabla stock (si init.sql no se ha corrido, o si se
--    partio de cero)
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

-- 3. Crear/refresh views
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

COMMIT;
