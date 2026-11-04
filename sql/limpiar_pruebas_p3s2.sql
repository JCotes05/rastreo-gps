-- =====================================================================
--  limpiar_pruebas_p3s2.sql — borra lo que dejan las pruebas del paso 3
--  (los camiones ZZZ901, ZZZ902, ZZZ903 y sus envíos).
--
--  Se ejecuta con el USUARIO MAESTRO (app_rastreo no puede borrar).
--  Si las pruebas se vuelven a correr después, los camiones se crean de nuevo.
-- =====================================================================

BEGIN;
DELETE FROM envios   WHERE placa LIKE 'ZZZ%';     -- también borra sus puntos de control y alertas
DELETE FROM camiones WHERE placa LIKE 'ZZZ%';
COMMIT;

SELECT count(*) AS camiones_de_prueba_que_quedan FROM camiones WHERE placa LIKE 'ZZZ%';
