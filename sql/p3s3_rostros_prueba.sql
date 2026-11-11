-- =====================================================================
--  p3s3_rostros_prueba.sql — comprueba las reglas de p3s3_rostros.sql.
--  Se ejecuta DESPUÉS de p3s3_rostros.sql, con el usuario maestro.
--
--  Todo pasa dentro de una transacción que al final se deshace
--  (ROLLBACK): no queda ningún dato guardado. Se puede ejecutar cuantas
--  veces quieras. Usa un conductor y un camión inventados.
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

-- Un conductor y un camión de prueba (desaparecen con el ROLLBACK).
INSERT INTO conductores (cedula, nombre_completo, celular) VALUES ('9990000001', 'Conductor de prueba', '3009990001');
INSERT INTO camiones (placa, clave_hash, configuracion, carroceria, capacidad_kg, marca, linea, modelo,
                      color, propietario_nombre, propietario_documento, propietario_celular,
                      soat_vence, tecnomecanica_vence)
VALUES ('ZZZ802', 'prueba', 'C2', 'furgon', 4500, 'Chevrolet', 'NPR', 2019, 'Blanco',
        'Dueño de prueba', '9990000001', '3009990002', '2027-06-30', '2027-06-30');

DO $$
DECLARE
    f INT := 0;
BEGIN
    RAISE NOTICE '--- Huellas de los conductores ---';
    f := f + pg_temp.debe_pasar('1. Guardar 2 muestras de un conductor',
        $q$INSERT INTO rostros_conductores (cedula, muestras) VALUES ('9990000001', '[[0.1, 0.2], [0.3, 0.4]]')$q$);
    f := f + pg_temp.debe_pasar('2. Actualizar sus muestras',
        $q$UPDATE rostros_conductores SET muestras = '[[0.5]]', actualizado_en = now() WHERE cedula = '9990000001'$q$);
    f := f + pg_temp.debe_fallar('3. Rechaza una cédula que no está en conductores',
        $q$INSERT INTO rostros_conductores (cedula, muestras) VALUES ('9990000099', '[[0.1]]')$q$, '23503');
    f := f + pg_temp.debe_fallar('4. Rechaza muestras que no son una lista',
        $q$UPDATE rostros_conductores SET muestras = '{"a": 1}' WHERE cedula = '9990000001'$q$, '23514');
    f := f + pg_temp.debe_fallar('5. Rechaza una lista vacía de muestras',
        $q$UPDATE rostros_conductores SET muestras = '[]' WHERE cedula = '9990000001'$q$, '23514');

    RAISE NOTICE '--- Intentos de identificación ---';
    f := f + pg_temp.debe_pasar('6. Guardar un intento reconocido',
        $q$INSERT INTO identificaciones (placa, resultado, cedula, similitud, rostros, con_rostro)
           VALUES ('ZZZ802', 'reconocido', '9990000001', 0.81, 3, 3)$q$);
    f := f + pg_temp.debe_pasar('7. Guardar un intento no reconocido',
        $q$INSERT INTO identificaciones (placa, resultado, similitud, rostros, con_rostro, motivo)
           VALUES ('ZZZ802', 'no_reconocido', 0.21, 3, 1, 'No se vio bien el rostro.')$q$);
    f := f + pg_temp.debe_fallar('8. Reconocido exige cédula',
        $q$INSERT INTO identificaciones (placa, resultado, similitud, rostros, con_rostro)
           VALUES ('ZZZ802', 'reconocido', 0.81, 3, 3)$q$, '23514');
    f := f + pg_temp.debe_fallar('9. No reconocido no lleva cédula',
        $q$INSERT INTO identificaciones (placa, resultado, cedula, similitud, rostros, con_rostro)
           VALUES ('ZZZ802', 'no_reconocido', '9990000001', 0.3, 3, 3)$q$, '23514');
    f := f + pg_temp.debe_fallar('10. Resultado desconocido',
        $q$INSERT INTO identificaciones (placa, resultado, similitud, rostros, con_rostro)
           VALUES ('ZZZ802', 'quizas', 0.5, 3, 3)$q$, '23514');
    f := f + pg_temp.debe_fallar('11. Más rostros con cara que recortes',
        $q$INSERT INTO identificaciones (placa, resultado, similitud, rostros, con_rostro)
           VALUES ('ZZZ802', 'no_reconocido', 0.5, 2, 3)$q$, '23514');

    RAISE NOTICE '--- Permisos de app_rastreo ---';
    IF has_table_privilege('app_rastreo', 'rostros_conductores', 'SELECT')
       AND has_table_privilege('app_rastreo', 'rostros_conductores', 'INSERT')
       AND has_table_privilege('app_rastreo', 'identificaciones', 'INSERT')
       AND has_sequence_privilege('app_rastreo', 'identificaciones_id_seq', 'USAGE') THEN
        RAISE NOTICE 'OK     12. La app puede leer huellas y guardar intentos';
    ELSE
        RAISE NOTICE 'FALLO  12. A la app le faltan permisos';
        f := f + 1;
    END IF;
    IF NOT has_table_privilege('app_rastreo', 'rostros_conductores', 'DELETE')
       AND NOT has_table_privilege('app_rastreo', 'identificaciones', 'DELETE') THEN
        RAISE NOTICE 'OK     13. La app NO puede borrar huellas ni intentos';
    ELSE
        RAISE NOTICE 'FALLO  13. La app tiene permiso de borrar (no debería)';
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
