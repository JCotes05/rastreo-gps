-- =====================================================================
--  p3s3.sql — P3-S3: eventos de la carga (ESP32 de sensores)
--
--  Se ejecuta UNA sola vez, conectado con el USUARIO MAESTRO de la RDS,
--  igual que p3s2.sql. Si se vuelve a ejecutar no pasa nada.
--  Todo va dentro de una transacción: si algo falla, no queda nada a medias.
--
--  Qué hace:
--    1. Crea la tabla eventos_carga (frenadas, huecos, curvas y luz).
--    2. Permite el tipo de alerta 'carga_abierta'.
--    3. Agrega a envios el último estado de la luz y el último contacto del ESP.
--    4. Da permisos a app_rastreo (leer, crear y actualizar; NUNCA borrar).
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
--  1. EVENTOS DE LA CARGA — los manda el celular (que los recibe del
--     ESP32 por Bluetooth). Cada evento queda ubicado sobre la ruta.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eventos_carga (
    id                    SERIAL PRIMARY KEY,
    id_envio              INTEGER NOT NULL REFERENCES envios (id) ON DELETE CASCADE,
    uid                   TEXT NOT NULL,          -- identificador que pone el celular (evita duplicados al reintentar)
    tipo                  TEXT NOT NULL,          -- frenada | hueco | curva | luz_abierta | luz_cerrada
    valor                 DOUBLE PRECISION,       -- g (frenada, hueco, curva) o nivel de luz (0-4095)
    lat                   DOUBLE PRECISION,       -- dónde pasó (último GPS del celular)
    lon                   DOUBLE PRECISION,
    avance_m              DOUBLE PRECISION,       -- en qué metro de la ruta planeada (proyectado)
    momento               TIMESTAMPTZ NOT NULL,   -- cuándo pasó (hora del celular)
    recibido_en           TIMESTAMPTZ NOT NULL DEFAULT now(),   -- cuándo llegó al servidor

    CONSTRAINT eventos_uid_por_envio  UNIQUE (id_envio, uid),
    CONSTRAINT eventos_tipo           CHECK (tipo IN ('frenada', 'hueco', 'curva', 'luz_abierta', 'luz_cerrada')),
    CONSTRAINT eventos_uid_formato    CHECK (uid ~ '^[A-Za-z0-9_.:-]{1,64}$'),
    CONSTRAINT eventos_coordenadas    CHECK ((lat IS NULL AND lon IS NULL)
                                             OR (lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180)),
    CONSTRAINT eventos_valor          CHECK (valor IS NULL OR valor >= 0)
);

CREATE INDEX IF NOT EXISTS eventos_por_envio ON eventos_carga (id_envio, momento);


-- ---------------------------------------------------------------------
--  2. ALERTAS: se suma el tipo 'carga_abierta' (luz dentro de la carga
--     mientras el envío está en ruta). Se reemplaza la regla del tipo.
-- ---------------------------------------------------------------------
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo CHECK (tipo IN ('retraso', 'desvio', 'carga_abierta'));


-- ---------------------------------------------------------------------
--  3. ENVÍOS: último estado de la luz y último contacto con el ESP32
--     (lo actualiza cada latido, cada 10 s).
-- ---------------------------------------------------------------------
ALTER TABLE envios ADD COLUMN IF NOT EXISTS luz_estado     TEXT;              -- 'abierta' | 'cerrada' | NULL (sin sensores)
ALTER TABLE envios ADD COLUMN IF NOT EXISTS luz_nivel      INTEGER;
ALTER TABLE envios ADD COLUMN IF NOT EXISTS sensores_en    TIMESTAMPTZ;       -- último contacto con el ESP32

ALTER TABLE envios DROP CONSTRAINT IF EXISTS envios_luz_estado;
ALTER TABLE envios ADD CONSTRAINT envios_luz_estado CHECK (luz_estado IS NULL OR luz_estado IN ('abierta', 'cerrada'));


-- ---------------------------------------------------------------------
--  4. PERMISOS para el usuario de la app. Leer, crear y actualizar;
--     NUNCA borrar.
-- ---------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON eventos_carga TO app_rastreo;
GRANT USAGE, SELECT ON SEQUENCE eventos_carga_id_seq TO app_rastreo;

COMMIT;


-- ---------------------------------------------------------------------
--  Comprobación rápida (se puede ejecutar cuantas veces quieras):
-- ---------------------------------------------------------------------
SELECT 'eventos_carga' AS tabla,
       has_table_privilege('app_rastreo', 'eventos_carga', 'SELECT') AS leer,
       has_table_privilege('app_rastreo', 'eventos_carga', 'INSERT') AS crear,
       has_table_privilege('app_rastreo', 'eventos_carga', 'UPDATE') AS actualizar,
       has_table_privilege('app_rastreo', 'eventos_carga', 'DELETE') AS borrar;

SELECT column_name AS columna_nueva_en_envios FROM information_schema.columns
WHERE table_name = 'envios' AND column_name IN ('luz_estado', 'luz_nivel', 'sensores_en');

-- ---------------------------------------------------------------------
--  Para deshacer P3-S3 (NO hace falta; solo si algo quedó mal):
--      DROP TABLE eventos_carga;
--      ALTER TABLE envios DROP COLUMN luz_estado, DROP COLUMN luz_nivel, DROP COLUMN sensores_en;
--      DELETE FROM alertas WHERE tipo = 'carga_abierta';
--      ALTER TABLE alertas DROP CONSTRAINT alertas_tipo;
--      ALTER TABLE alertas ADD CONSTRAINT alertas_tipo CHECK (tipo IN ('retraso', 'desvio'));
-- ---------------------------------------------------------------------
