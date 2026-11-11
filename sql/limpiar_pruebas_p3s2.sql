-- =====================================================================
--  limpiar_pruebas_p3s2.sql — borra lo que dejan las pruebas de P3-S2 y
--  P3-S3 (los camiones de prueba ZZZ... y sus envíos).
--
--  Se ejecuta con el USUARIO MAESTRO (app_rastreo no puede borrar).
--  Si las pruebas se vuelven a correr después, los camiones se crean de nuevo.
-- =====================================================================

BEGIN;
-- Al borrar un envío también se borran sus puntos de control, alertas y
-- eventos de la carga (P3-S3), porque esas tablas tienen ON DELETE CASCADE.
-- Se borran primero los eventos explícitamente por si p3s3.sql aún no se corrió
-- en otra base (la sentencia se salta si la tabla no existe).
DO $$
BEGIN
    IF to_regclass('eventos_carga') IS NOT NULL THEN
        DELETE FROM eventos_carga WHERE id_envio IN (SELECT id FROM envios WHERE placa LIKE 'ZZZ%');
    END IF;
    -- P3-S3 (rostros): intentos de identificación de los camiones de prueba.
    IF to_regclass('identificaciones') IS NOT NULL THEN
        DELETE FROM identificaciones WHERE placa LIKE 'ZZZ%';
    END IF;
END $$;
DELETE FROM envios   WHERE placa LIKE 'ZZZ%';
DELETE FROM camiones WHERE placa LIKE 'ZZZ%';
COMMIT;

SELECT count(*) AS camiones_de_prueba_que_quedan FROM camiones WHERE placa LIKE 'ZZZ%';
