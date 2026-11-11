-- =====================================================================
--  p3s3_rostros.sql — P3-S3: reconocimiento facial en el servidor
--  (esquema híbrido: el ESP32-CAM detecta el rostro y el servidor lo reconoce).
--
--  Se ejecuta UNA sola vez, conectado con el USUARIO MAESTRO de la RDS,
--  igual que p3s3.sql. Si se vuelve a ejecutar no pasa nada.
--  Todo va dentro de una transacción: si algo falla, no queda nada a medias.
--
--  Qué hace:
--    1. Crea la tabla rostros_conductores: las huellas SFace de cada conductor
--       (128 números por muestra; NO son fotos). Son datos biométricos.
--    2. Crea la tabla identificaciones: cada intento de reconocer al conductor
--       desde la app (resultado y similitud; sin imágenes). Reemplaza el
--       registro_identificaciones.csv del computador de P3-S1.
--    3. Da permisos a app_rastreo (leer, crear y actualizar; NUNCA borrar).
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
--  1. HUELLAS DE LOS CONDUCTORES — una fila por conductor, con varias
--     muestras. Las sube "python3 reconocimiento_facial.py --cargar" a
--     partir del conductores_rostros.json que crea registrar.py.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rostros_conductores (
    cedula          TEXT PRIMARY KEY REFERENCES conductores (cedula),
    muestras        JSONB NOT NULL,                 -- [[128 números], [128 números], ...]
    modelo          TEXT NOT NULL DEFAULT 'SFace 2021dec (OpenCV Zoo)',
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT rostros_muestras_lista CHECK (jsonb_typeof(muestras) = 'array'
                                             AND jsonb_array_length(muestras) BETWEEN 1 AND 50)
);


-- ---------------------------------------------------------------------
--  2. INTENTOS DE IDENTIFICACIÓN — evidencia para el informe.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identificaciones (
    id          SERIAL PRIMARY KEY,
    placa       TEXT NOT NULL REFERENCES camiones (placa) ON DELETE CASCADE,
    resultado   TEXT NOT NULL,                      -- reconocido | no_reconocido
    cedula      TEXT REFERENCES conductores (cedula),   -- solo si fue reconocido
    similitud   DOUBLE PRECISION NOT NULL,
    rostros     INTEGER NOT NULL,                   -- recortes que mandó el ESP32-CAM
    con_rostro  INTEGER NOT NULL,                   -- en cuántos encontró rostro el servidor
    motivo      TEXT,
    momento     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT identificaciones_resultado CHECK (resultado IN ('reconocido', 'no_reconocido')),
    CONSTRAINT identificaciones_cedula    CHECK ((resultado = 'reconocido') = (cedula IS NOT NULL)),
    CONSTRAINT identificaciones_similitud CHECK (similitud BETWEEN -1 AND 1),
    CONSTRAINT identificaciones_rostros   CHECK (rostros BETWEEN 1 AND 10 AND con_rostro BETWEEN 0 AND rostros)
);

CREATE INDEX IF NOT EXISTS identificaciones_por_placa ON identificaciones (placa, momento);


-- ---------------------------------------------------------------------
--  3. PERMISOS para el usuario de la app. Leer, crear y actualizar;
--     NUNCA borrar.
-- ---------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON rostros_conductores TO app_rastreo;
GRANT SELECT, INSERT, UPDATE ON identificaciones TO app_rastreo;
GRANT USAGE, SELECT ON SEQUENCE identificaciones_id_seq TO app_rastreo;

COMMIT;


-- ---------------------------------------------------------------------
--  Comprobación rápida (se puede ejecutar cuantas veces quieras):
-- ---------------------------------------------------------------------
SELECT t AS tabla,
       has_table_privilege('app_rastreo', t, 'SELECT') AS leer,
       has_table_privilege('app_rastreo', t, 'INSERT') AS crear,
       has_table_privilege('app_rastreo', t, 'UPDATE') AS actualizar,
       has_table_privilege('app_rastreo', t, 'DELETE') AS borrar
FROM unnest(ARRAY['rostros_conductores', 'identificaciones']) AS t;

-- ---------------------------------------------------------------------
--  Para deshacer (NO hace falta; solo si algo quedó mal):
--      DROP TABLE identificaciones;
--      DROP TABLE rostros_conductores;
-- ---------------------------------------------------------------------
