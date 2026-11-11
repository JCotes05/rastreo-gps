-- =====================================================================
--  p3s3_prueba.sql — comprueba las reglas de P3-S3. Se ejecuta DESPUÉS
--  de p3s3.sql, con el usuario maestro.
--
--  Todo pasa dentro de una transacción que al final se deshace
--  (ROLLBACK): no queda ningún dato guardado. Se puede ejecutar cuantas
--  veces quieras.
-- =====================================================================

BEGIN;

CREATE FUNCTION pg_temp.debe_pasar(texto TEXT, sentencia TEXT) RETURNS INT
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE sentencia;
    RAISE NOTICE 'OK     %', texto;
    RETURN 0;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'FALLO  %  (error: %)', texto, SQLERRM;
    RETURN 1;
END $$;

CREATE FUNCTION pg_temp.debe_fallar(texto TEXT, sentencia TEXT, codigo TEXT) RETURNS INT
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE sentencia;
    RAISE NOTICE 'FALLO  %  (la base lo dejó pasar)', texto;
    RETURN 1;
EXCEPTION WHEN OTHERS THEN
    IF SQLSTATE = codigo THEN
        RAISE NOTICE 'OK     %', texto;
        RETURN 0;
    END IF;
    RAISE NOTICE 'FALLO  %  (error inesperado: %)', texto, SQLERRM;
    RETURN 1;
END $$;

-- Un camión y un envío de prueba (desaparecen con el ROLLBACK).
INSERT INTO camiones (placa, clave_hash, configuracion, carroceria, capacidad_kg, marca, linea, modelo,
                      color, propietario_nombre, propietario_documento, propietario_celular,
                      soat_vence, tecnomecanica_vence)
VALUES ('ZZZ801', 'prueba', 'C2', 'furgon', 4500, 'Chevrolet', 'NPR', 2019, 'Blanco',
        'Dueño de prueba', '1001000001', '3001000001', '2027-06-30', '2027-06-30');
INSERT INTO envios (id, cliente_nombre, cliente_nit, origen_direccion, origen_lat, origen_lon,
                    cargue_contacto, cargue_celular, destino_direccion, destino_lat, destino_lon,
                    destinatario_nombre, destinatario_celular, tipo_carga, descripcion, empaque,
                    cantidad, peso_kg, valor_declarado, cargue_programado, placa,
                    ruta_fuente, ruta, distancia_m, duracion_s)
VALUES (900101, 'Cliente de prueba S.A.S.', '900123456-7', 'Universidad del Norte', 11.0190, -74.8505,
        'Contacto cargue', '3001000002', 'Centro de Barranquilla', 10.9878, -74.7889,
        'Destinatario', '3001000003', 'general', 'Cajas de prueba', 'cajas',
        20, 3000, 0, now() + interval '1 hour', 'ZZZ801',
        'osrm-car', '{"puntos": [[11.0190, -74.8505], [10.9878, -74.7889]], "dist_m": [0, 8200], "tiempo_s": [0, 1260]}',
        8200, 1260);

DO $$
DECLARE
    f INT := 0;
BEGIN
    RAISE NOTICE '--- Eventos de la carga ---';
    f := f + pg_temp.debe_pasar ('1. Guarda una frenada de 0,62 g con su ubicación',
              $q$INSERT INTO eventos_carga (id_envio, uid, tipo, valor, lat, lon, avance_m, momento)
                 VALUES (900101, 'cel-1', 'frenada', 0.62, 11.01, -74.84, 1200, now())$q$);
    f := f + pg_temp.debe_fallar('2. No deja guardar dos veces el mismo uid en el mismo envío',
              $q$INSERT INTO eventos_carga (id_envio, uid, tipo, valor, momento)
                 VALUES (900101, 'cel-1', 'hueco', 1.1, now())$q$, '23505');
    f := f + pg_temp.debe_pasar ('3. Acepta un evento de luz sin ubicación (aún sin GPS)',
              $q$INSERT INTO eventos_carga (id_envio, uid, tipo, valor, momento)
                 VALUES (900101, 'cel-2', 'luz_abierta', 812, now())$q$);
    f := f + pg_temp.debe_fallar('4. Rechaza un tipo de evento que no existe (vuelco)',
              $q$INSERT INTO eventos_carga (id_envio, uid, tipo, valor, momento)
                 VALUES (900101, 'cel-3', 'vuelco', 2, now())$q$, '23514');
    f := f + pg_temp.debe_fallar('5. Rechaza un uid con caracteres raros',
              $q$INSERT INTO eventos_carga (id_envio, uid, tipo, valor, momento)
                 VALUES (900101, 'uno; DROP', 'hueco', 1, now())$q$, '23514');
    f := f + pg_temp.debe_fallar('6. Rechaza un evento de un envío que no existe',
              $q$INSERT INTO eventos_carga (id_envio, uid, tipo, valor, momento)
                 VALUES (999999999, 'cel-4', 'hueco', 1, now())$q$, '23503');

    RAISE NOTICE '--- Alerta de carga abierta ---';
    f := f + pg_temp.debe_pasar ('7. Abre una alerta de carga abierta',
              $q$INSERT INTO alertas (id, id_envio, tipo, valor_max, lat, lon)
                 VALUES (900101, 900101, 'carga_abierta', 812, 11.01, -74.84)$q$);
    f := f + pg_temp.debe_fallar('8. No deja dos alertas de carga abierta activas a la vez',
              $q$INSERT INTO alertas (id, id_envio, tipo, valor_max) VALUES (900102, 900101, 'carga_abierta', 900)$q$, '23505');
    f := f + pg_temp.debe_fallar('9. Sigue rechazando un tipo de alerta que no existe',
              $q$INSERT INTO alertas (id, id_envio, tipo, valor_max) VALUES (900103, 900101, 'incendio', 1)$q$, '23514');

    RAISE NOTICE '--- Estado de la luz en el envío ---';
    f := f + pg_temp.debe_pasar ('10. Guarda el último estado de la luz y el último contacto',
              $q$UPDATE envios SET luz_estado = 'cerrada', luz_nivel = 35, sensores_en = now() WHERE id = 900101$q$);
    f := f + pg_temp.debe_fallar('11. Rechaza un estado de luz que no existe',
              $q$UPDATE envios SET luz_estado = 'entreabierta' WHERE id = 900101$q$, '23514');

    RAISE NOTICE '--- Permisos de app_rastreo ---';
    IF has_table_privilege('app_rastreo', 'eventos_carga', 'INSERT')
       AND has_table_privilege('app_rastreo', 'eventos_carga', 'SELECT')
       AND has_sequence_privilege('app_rastreo', 'eventos_carga_id_seq', 'USAGE') THEN
        RAISE NOTICE 'OK     12. La app puede leer y guardar eventos';
    ELSE
        RAISE NOTICE 'FALLO  12. A la app le faltan permisos sobre eventos_carga';
        f := f + 1;
    END IF;
    IF NOT has_table_privilege('app_rastreo', 'eventos_carga', 'DELETE') THEN
        RAISE NOTICE 'OK     13. La app NO puede borrar eventos';
    ELSE
        RAISE NOTICE 'FALLO  13. La app tiene permiso de borrar eventos (no debería)';
        f := f + 1;
    END IF;

    RAISE NOTICE '-----------------------------------------------';
    IF f = 0 THEN
        RAISE NOTICE 'TODO BIEN: las 13 pruebas pasaron.';
    ELSE
        RAISE NOTICE 'ATENCIÓN: % prueba(s) fallaron.', f;
    END IF;
    RAISE NOTICE 'No quedó ningún dato guardado (ROLLBACK).';
END $$;

ROLLBACK;
