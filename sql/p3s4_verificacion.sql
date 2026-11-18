-- =====================================================================
--  p3s4_verificacion.sql — P3-S4: verificación del conductor DURANTE el viaje.
--
--  Se ejecuta UNA sola vez con el USUARIO MAESTRO de la RDS, ANTES de
--  desplegar el código. Si se vuelve a ejecutar no pasa nada. No borra
--  ni cambia datos que ya existen: solo agrega columnas y amplía una regla.
--
--  Para la seguridad en la vía (que no cambien de conductor en el camino):
--    · mientras el envío está en ruta, la app vuelve a reconocer al conductor
--      cada 2 minutos (en la presentación) con el ESP32-CAM de la cabina;
--    · un operador o un administrador puede pedir una verificación desde la
--      página cuando quiera (la app la hace en menos de 15 s);
--    · si reconoce a OTRA persona, o no logra reconocer al conductor 2 veces
--      seguidas, se abre una alerta 'conductor' en el envío.
--
--  Qué agrega:
--    envios            verificacion_pedida_en / _por: pedido pendiente de un operador
--                      ultima_verificacion_en / _ok:  último intento y si fue el mismo conductor
--    identificaciones  id_envio, origen (inicio | periodica | operador) y coincide
--    alertas           el tipo 'conductor'
--
--  Permisos: app_rastreo ya puede leer, crear y actualizar estas tablas;
--  los permisos de cada tabla cubren sus columnas nuevas.
-- =====================================================================

BEGIN;

ALTER TABLE envios ADD COLUMN IF NOT EXISTS verificacion_pedida_en  TIMESTAMPTZ;
ALTER TABLE envios ADD COLUMN IF NOT EXISTS verificacion_pedida_por VARCHAR(30);
ALTER TABLE envios ADD COLUMN IF NOT EXISTS ultima_verificacion_en  TIMESTAMPTZ;
ALTER TABLE envios ADD COLUMN IF NOT EXISTS ultima_verificacion_ok  BOOLEAN;

ALTER TABLE identificaciones ADD COLUMN IF NOT EXISTS id_envio INTEGER REFERENCES envios (id);
ALTER TABLE identificaciones ADD COLUMN IF NOT EXISTS origen   VARCHAR(10);
ALTER TABLE identificaciones ADD COLUMN IF NOT EXISTS coincide BOOLEAN;

ALTER TABLE identificaciones DROP CONSTRAINT IF EXISTS identificaciones_origen;
ALTER TABLE identificaciones ADD CONSTRAINT identificaciones_origen
    CHECK (origen IS NULL OR origen IN ('inicio', 'periodica', 'operador'));

CREATE INDEX IF NOT EXISTS identificaciones_por_envio ON identificaciones (id_envio, momento);

ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo
    CHECK (tipo IN ('retraso', 'desvio', 'carga_abierta', 'conductor'));

COMMIT;


-- ---------------------------------------------------------------------
--  Comprobación rápida: deben salir 7 filas (4 de envios y 3 de identificaciones).
-- ---------------------------------------------------------------------
SELECT table_name AS tabla, column_name AS columna, data_type AS tipo
FROM information_schema.columns
WHERE (table_name = 'envios' AND column_name LIKE '%verificacion%')
   OR (table_name = 'identificaciones' AND column_name IN ('id_envio', 'origen', 'coincide'))
ORDER BY 1, 2;

-- ---------------------------------------------------------------------
--  Para deshacer (NO hace falta; borra el historial de verificaciones):
--    DELETE FROM alertas WHERE tipo = 'conductor';
--    ALTER TABLE alertas DROP CONSTRAINT alertas_tipo;
--    ALTER TABLE alertas ADD CONSTRAINT alertas_tipo CHECK (tipo IN ('retraso', 'desvio', 'carga_abierta'));
--    ALTER TABLE identificaciones DROP COLUMN coincide, DROP COLUMN origen, DROP COLUMN id_envio;
--    ALTER TABLE envios DROP COLUMN ultima_verificacion_ok, DROP COLUMN ultima_verificacion_en,
--                       DROP COLUMN verificacion_pedida_por, DROP COLUMN verificacion_pedida_en;
-- ---------------------------------------------------------------------
