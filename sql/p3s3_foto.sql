-- =====================================================================
--  p3s3_foto.sql — P3-S3: foto del conductor reconocido en la planilla.
--
--  Se ejecuta UNA sola vez, DESPUÉS de p3s3_rostros.sql, con el USUARIO
--  MAESTRO de la RDS. Si se vuelve a ejecutar no pasa nada.
--
--  Qué hace: agrega a identificaciones la columna foto (el recorte JPEG
--  con el que el servidor reconoció al conductor, ~9 KB). La página la
--  muestra en la planilla del recorrido como prueba de que se tomó en vivo.
--  El servidor la vacía (foto = NULL) a los 7 días: son datos biométricos
--  y la página todavía no tiene inicio de sesión (llega en P3-S4).
--
--  Permisos: app_rastreo ya puede leer, crear y actualizar identificaciones;
--  los permisos de la tabla cubren la columna nueva.
-- =====================================================================

BEGIN;

ALTER TABLE identificaciones ADD COLUMN IF NOT EXISTS foto BYTEA;

ALTER TABLE identificaciones DROP CONSTRAINT IF EXISTS identificaciones_foto;
ALTER TABLE identificaciones ADD CONSTRAINT identificaciones_foto
    CHECK (foto IS NULL OR (resultado = 'reconocido' AND octet_length(foto) BETWEEN 100 AND 300000));

COMMIT;


-- ---------------------------------------------------------------------
--  Comprobación rápida: debe mostrar la columna foto y "true" en leer,
--  crear y actualizar.
-- ---------------------------------------------------------------------
SELECT column_name AS columna, data_type AS tipo,
       has_column_privilege('app_rastreo', 'identificaciones', 'foto', 'SELECT') AS leer,
       has_column_privilege('app_rastreo', 'identificaciones', 'foto', 'INSERT') AS crear,
       has_column_privilege('app_rastreo', 'identificaciones', 'foto', 'UPDATE') AS actualizar
FROM information_schema.columns
WHERE table_name = 'identificaciones' AND column_name = 'foto';

-- ---------------------------------------------------------------------
--  Para deshacer (NO hace falta):
--      ALTER TABLE identificaciones DROP COLUMN foto;
-- ---------------------------------------------------------------------
