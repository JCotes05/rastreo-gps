-- =====================================================================
--  p3s2_prueba.sql — comprueba que las reglas de las tablas nuevas
--  funcionan. Se ejecuta DESPUÉS de p3s2.sql, con el usuario maestro.
--
--  Todo pasa dentro de una transacción que al final se deshace
--  (ROLLBACK): no queda ningún camión, envío ni alerta guardado.
--  Se puede ejecutar cuantas veces quieras.
-- =====================================================================

BEGIN;

-- Ayudas temporales (desaparecen con el ROLLBACK) -----------------------

-- Ejecuta una sentencia que DEBE funcionar.
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

-- Ejecuta una sentencia que la base DEBE rechazar con el error esperado.
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

-- Registra un camión de prueba variando solo lo que interesa.
CREATE FUNCTION pg_temp.camion(p TEXT, conf TEXT, carr TEXT, cap INT) RETURNS VOID
LANGUAGE sql AS $$
    INSERT INTO camiones (placa, clave_hash, configuracion, carroceria, capacidad_kg, marca, linea, modelo,
                          color, propietario_nombre, propietario_documento, propietario_celular,
                          soat_vence, tecnomecanica_vence)
    VALUES (p, 'prueba', conf, carr, cap, 'Chevrolet', 'NPR', 2019, 'Blanco',
            'Dueño de prueba', '1001000001', '3001000001', '2027-06-30', '2027-06-30');
$$;

-- Crea un envío de prueba (Uninorte → Centro de Barranquilla).
CREATE FUNCTION pg_temp.envio(n INT, p TEXT, tipo TEXT, peso INT) RETURNS VOID
LANGUAGE sql AS $$
    INSERT INTO envios (id, cliente_nombre, cliente_nit, origen_direccion, origen_lat, origen_lon,
                        cargue_contacto, cargue_celular, destino_direccion, destino_lat, destino_lon,
                        destinatario_nombre, destinatario_celular, tipo_carga, descripcion, empaque,
                        cantidad, peso_kg, valor_declarado, cargue_programado, placa,
                        ruta_fuente, ruta, distancia_m, duracion_s)
    VALUES (n, 'Cliente de prueba S.A.S.', '900123456-7', 'Universidad del Norte', 11.0190, -74.8505,
            'Contacto cargue', '3001000002', 'Centro de Barranquilla', 10.9878, -74.7889,
            'Destinatario', '3001000003', tipo, 'Cajas de prueba', 'cajas',
            20, peso, 5000000, now() + interval '1 hour', p,
            'ors-hgv', '{"puntos": [[11.0190, -74.8505], [10.9878, -74.7889]],
                         "dist_m": [0, 8200], "tiempo_s": [0, 1260]}', 8200, 1260);
$$;

-- Las pruebas ------------------------------------------------------------
DO $$
DECLARE
    f INT := 0;     -- cuántas fallaron
BEGIN
    RAISE NOTICE '--- Camiones ---';
    f := f + pg_temp.debe_pasar ('1. Registra un camión válido (C2, furgón, 4 500 kg)',
              $q$SELECT pg_temp.camion('ZZZ001', 'C2', 'furgon', 4500)$q$);
    f := f + pg_temp.debe_fallar('2. Rechaza una placa mal escrita (zz-001)',
              $q$SELECT pg_temp.camion('zz-001', 'C2', 'furgon', 4500)$q$, '23514');
    f := f + pg_temp.debe_fallar('3. Rechaza un C2 con 20 000 kg (su PBV máximo es 16 t)',
              $q$SELECT pg_temp.camion('ZZZ002', 'C2', 'furgon', 20000)$q$, '23514');
    f := f + pg_temp.debe_fallar('4. Rechaza una carrocería que no existe (bus)',
              $q$SELECT pg_temp.camion('ZZZ003', 'C2', 'bus', 4500)$q$, '23514');
    f := f + pg_temp.debe_fallar('5. Rechaza una placa repetida',
              $q$SELECT pg_temp.camion('ZZZ001', 'C3', 'estacas', 17000)$q$, '23505');
    f := f + pg_temp.debe_pasar ('6. Acepta una tractomula 3S3 de 34 000 kg',
              $q$SELECT pg_temp.camion('ZZZ004', '3S3', 'refrigerado', 34000)$q$);

    RAISE NOTICE '--- Envíos ---';
    f := f + pg_temp.debe_pasar ('7. Crea un envío asignado al camión ZZZ001',
              $q$SELECT pg_temp.envio(900001, 'ZZZ001', 'general', 3000)$q$);
    f := f + pg_temp.debe_fallar('8. No deja asignarle un segundo envío abierto al mismo camión',
              $q$SELECT pg_temp.envio(900002, 'ZZZ001', 'general', 1000)$q$, '23505');
    f := f + pg_temp.debe_pasar ('9. Al finalizar el primero, sí se le puede asignar otro',
              $q$UPDATE envios SET estado = 'finalizado', finalizado_en = now() WHERE id = 900001;
                 SELECT pg_temp.envio(900002, 'ZZZ001', 'general', 1000)$q$);
    f := f + pg_temp.debe_fallar('10. Rechaza un envío para un camión que no existe',
              $q$SELECT pg_temp.envio(900003, 'ZZZ999', 'general', 1000)$q$, '23503');
    f := f + pg_temp.debe_fallar('11. Rechaza un tipo de carga que no existe (explosivos)',
              $q$SELECT pg_temp.envio(900004, 'ZZZ004', 'explosivos', 1000)$q$, '23514');
    f := f + pg_temp.debe_fallar('12. No deja usar el mismo recorrido del celular en dos envíos',
              $q$UPDATE envios SET id_recorrido = '1730000000000' WHERE id IN (900001, 900002)$q$, '23505');

    RAISE NOTICE '--- Puntos de control ---';
    f := f + pg_temp.debe_pasar ('13. Guarda salida, un punto de control y llegada',
              $q$INSERT INTO puntos_control (id_envio, orden, tipo, nombre, lat, lon, distancia_m, tiempo_plan_s)
                 VALUES (900002, 0, 'salida',  'Universidad del Norte',  11.0190, -74.8505,    0,    0),
                        (900002, 1, 'control', 'Calle 72 con Carrera 46', 10.9990, -74.8080, 4100,  630),
                        (900002, 2, 'llegada', 'Centro de Barranquilla', 10.9878, -74.7889, 8200, 1260)$q$);
    f := f + pg_temp.debe_fallar('14. No deja repetir el número de un punto en el mismo envío',
              $q$INSERT INTO puntos_control (id_envio, orden, tipo, nombre, lat, lon, distancia_m, tiempo_plan_s)
                 VALUES (900002, 1, 'control', 'Repetido', 10.99, -74.80, 4000, 600)$q$, '23505');

    RAISE NOTICE '--- Alertas ---';
    f := f + pg_temp.debe_pasar ('15. Abre una alerta de desvío',
              $q$INSERT INTO alertas (id, id_envio, tipo, valor_max, lat, lon)
                 VALUES (900001, 900002, 'desvio', 180, 10.995, -74.81)$q$);
    f := f + pg_temp.debe_fallar('16. No deja dos alertas de desvío activas en el mismo envío',
              $q$INSERT INTO alertas (id, id_envio, tipo, valor_max) VALUES (900002, 900002, 'desvio', 200)$q$, '23505');
    f := f + pg_temp.debe_pasar ('17. Cerrada la primera, sí se puede abrir otra',
              $q$UPDATE alertas SET fin = now() WHERE id = 900001;
                 INSERT INTO alertas (id, id_envio, tipo, valor_max) VALUES (900002, 900002, 'desvio', 200)$q$);

    RAISE NOTICE '--- Permisos de app_rastreo ---';
    IF has_table_privilege('app_rastreo', 'envios', 'INSERT')
       AND has_table_privilege('app_rastreo', 'envios', 'UPDATE')
       AND has_table_privilege('app_rastreo', 'alertas', 'INSERT')
       AND has_sequence_privilege('app_rastreo', 'envios_id_seq', 'USAGE') THEN
        RAISE NOTICE 'OK     18. La app puede leer, crear y actualizar';
    ELSE
        RAISE NOTICE 'FALLO  18. A la app le faltan permisos de lectura, creación o actualización';
        f := f + 1;
    END IF;
    IF NOT has_table_privilege('app_rastreo', 'envios', 'DELETE')
       AND NOT has_table_privilege('app_rastreo', 'camiones', 'DELETE') THEN
        RAISE NOTICE 'OK     19. La app NO puede borrar camiones ni envíos';
    ELSE
        RAISE NOTICE 'FALLO  19. La app tiene permiso de borrar (no debería)';
        f := f + 1;
    END IF;

    RAISE NOTICE '-----------------------------------------------';
    IF f = 0 THEN
        RAISE NOTICE 'TODO BIEN: las 19 pruebas pasaron.';
    ELSE
        RAISE NOTICE 'ATENCIÓN: % prueba(s) fallaron.', f;
    END IF;
    RAISE NOTICE 'No quedó ningún dato guardado (ROLLBACK).';
END $$;

ROLLBACK;
