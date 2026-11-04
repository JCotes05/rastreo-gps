-- =====================================================================
--  p3s2.sql — P3-S2, paso 1: camiones, envíos, puntos de control y alertas
--
--  Se ejecuta UNA sola vez, conectado con el USUARIO MAESTRO de la RDS,
--  igual que planillas.sql en S1 (app_rastreo no puede crear tablas, a
--  propósito). Si se vuelve a ejecutar no pasa nada: lo que ya existe
--  no se toca.
--
--  Si algo falla en la mitad, no queda nada a medias: todo va dentro de
--  una sola transacción (BEGIN ... COMMIT).
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
--  1. CAMIONES — los registra su dueño desde la app.
--     La placa es el usuario para iniciar sesión.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS camiones (
    placa                 TEXT PRIMARY KEY,
    clave_hash            TEXT NOT NULL,          -- PBKDF2: nunca se guarda la contraseña
    configuracion         TEXT NOT NULL,          -- Res. 4100 de 2004 (C2, C3, 3S3...)
    carroceria            TEXT NOT NULL,
    capacidad_kg          INTEGER NOT NULL,       -- la de la licencia de tránsito
    marca                 TEXT NOT NULL,
    linea                 TEXT NOT NULL,
    modelo                SMALLINT NOT NULL,      -- año
    color                 TEXT NOT NULL,
    propietario_nombre    TEXT NOT NULL,
    propietario_documento TEXT NOT NULL,          -- cédula o NIT, solo números
    propietario_celular   TEXT NOT NULL,
    soat_vence            DATE NOT NULL,
    tecnomecanica_vence   DATE NOT NULL,
    estado                TEXT NOT NULL DEFAULT 'no_disponible',
    ultima_lat            DOUBLE PRECISION,       -- última ubicación que reportó la app
    ultima_lon            DOUBLE PRECISION,
    ubicacion_en          TIMESTAMPTZ,
    token_hash            TEXT,                   -- sesión abierta: SHA-256 del token
    token_vence           TIMESTAMPTZ,
    creado_en             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT camiones_placa_formato CHECK (placa ~ '^[A-Z]{3}[0-9]{3}$'),
    CONSTRAINT camiones_configuracion CHECK (configuracion IN ('C2', 'C3', 'C4', '2S2', '2S3', '3S2', '3S3')),
    CONSTRAINT camiones_carroceria    CHECK (carroceria IN ('furgon', 'refrigerado', 'estacas', 'planchon',
                                                            'volco', 'tanque', 'portacontenedor')),
    -- La carga útil debe ser menor que el peso bruto vehicular máximo (PBV)
    -- de su configuración, según la Res. 4100 de 2004, art. 8.
    -- (C4 tiene tres variantes: se toma la mayor, 36 t.)
    CONSTRAINT camiones_capacidad_pbv CHECK (capacidad_kg > 0 AND capacidad_kg < CASE configuracion
                                                 WHEN 'C2'  THEN 16000
                                                 WHEN 'C3'  THEN 28000
                                                 WHEN 'C4'  THEN 36000
                                                 WHEN '2S2' THEN 32000
                                                 WHEN '2S3' THEN 40500
                                                 WHEN '3S2' THEN 48000
                                                 WHEN '3S3' THEN 52000
                                             END),
    CONSTRAINT camiones_modelo        CHECK (modelo BETWEEN 1970 AND 2100),
    CONSTRAINT camiones_documento     CHECK (propietario_documento ~ '^[0-9]{5,15}(-[0-9])?$'),
    CONSTRAINT camiones_celular       CHECK (propietario_celular ~ '^3[0-9]{9}$'),
    CONSTRAINT camiones_estado        CHECK (estado IN ('no_disponible', 'disponible', 'asignado', 'en_ruta')),
    CONSTRAINT camiones_ubicacion     CHECK ((ultima_lat IS NULL AND ultima_lon IS NULL)
                                             OR (ultima_lat BETWEEN -90 AND 90 AND ultima_lon BETWEEN -180 AND 180))
);


-- ---------------------------------------------------------------------
--  2. ENVÍOS — los crea la empresa en la página ("Nuevo envío").
--     La ruta se calcula al crearlo; la parte de "ejecución" la va
--     llenando el listener con cada paquete del celular.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS envios (
    id                    SERIAL PRIMARY KEY,     -- en pantalla: ENV-0001
    creado_en             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Cliente (quien contrata el envío)
    cliente_nombre        TEXT NOT NULL,
    cliente_nit           TEXT NOT NULL,

    -- Origen
    origen_direccion      TEXT NOT NULL,
    origen_lat            DOUBLE PRECISION NOT NULL,
    origen_lon            DOUBLE PRECISION NOT NULL,
    cargue_contacto       TEXT NOT NULL,
    cargue_celular        TEXT NOT NULL,

    -- Destino
    destino_direccion     TEXT NOT NULL,
    destino_lat           DOUBLE PRECISION NOT NULL,
    destino_lon           DOUBLE PRECISION NOT NULL,
    destinatario_nombre   TEXT NOT NULL,
    destinatario_celular  TEXT NOT NULL,

    -- Carga
    tipo_carga            TEXT NOT NULL,
    descripcion           TEXT NOT NULL,
    empaque               TEXT NOT NULL,
    cantidad              INTEGER NOT NULL,
    peso_kg               INTEGER NOT NULL,
    valor_declarado       BIGINT NOT NULL,        -- pesos colombianos
    cargue_programado     TIMESTAMPTZ NOT NULL,

    -- Asignación
    placa                 TEXT NOT NULL REFERENCES camiones (placa),
    estado                TEXT NOT NULL DEFAULT 'asignado',

    -- Ruta planeada
    ruta_fuente           TEXT NOT NULL,          -- 'ors-hgv' (camión) u 'osrm-car' (respaldo)
    ruta                  JSONB NOT NULL,         -- {"puntos": [[lat, lon], ...],
                                                  --  "dist_m": [...], "tiempo_s": [...]}  (acumulados)
    distancia_m           INTEGER NOT NULL,
    duracion_s            INTEGER NOT NULL,

    -- Ejecución (la llena el listener)
    id_recorrido          TEXT UNIQUE,            -- el RECORRIDO del celular al empezar a transmitir
    salida_real           TIMESTAMPTZ,
    llegada_real          TIMESTAMPTZ,
    finalizado_en         TIMESTAMPTZ,
    avance_m              DOUBLE PRECISION NOT NULL DEFAULT 0,   -- cuánto lleva sobre la ruta
    recorrido_m           DOUBLE PRECISION NOT NULL DEFAULT 0,   -- distancia real recorrida
    retraso_s             INTEGER,                -- retraso en vivo (negativo = va adelantado)
    desvio_m              DOUBLE PRECISION,       -- distancia actual a la ruta
    velocidad_kmh         DOUBLE PRECISION,
    actualizado_en        TIMESTAMPTZ,

    CONSTRAINT envios_cliente_nit     CHECK (cliente_nit ~ '^[0-9]{5,15}(-[0-9])?$'),
    CONSTRAINT envios_celulares       CHECK (cargue_celular ~ '^3[0-9]{9}$' AND destinatario_celular ~ '^3[0-9]{9}$'),
    CONSTRAINT envios_coordenadas     CHECK (origen_lat  BETWEEN -90 AND 90 AND origen_lon  BETWEEN -180 AND 180
                                             AND destino_lat BETWEEN -90 AND 90 AND destino_lon BETWEEN -180 AND 180),
    CONSTRAINT envios_tipo_carga      CHECK (tipo_carga IN ('general', 'refrigerada', 'granel', 'liquidos', 'contenedor')),
    CONSTRAINT envios_empaque         CHECK (empaque IN ('cajas', 'estibas', 'bultos', 'granel', 'contenedor', 'otro')),
    CONSTRAINT envios_cantidades      CHECK (cantidad > 0 AND peso_kg > 0 AND valor_declarado >= 0),
    CONSTRAINT envios_estado          CHECK (estado IN ('asignado', 'en_ruta', 'en_destino', 'finalizado', 'cancelado')),
    CONSTRAINT envios_ruta_fuente     CHECK (ruta_fuente IN ('ors-hgv', 'osrm-car')),
    CONSTRAINT envios_ruta_valores    CHECK (distancia_m > 0 AND duracion_s > 0)
);

-- Un camión no puede tener dos envíos abiertos al mismo tiempo.
CREATE UNIQUE INDEX IF NOT EXISTS envios_un_viaje_por_camion
    ON envios (placa) WHERE estado IN ('asignado', 'en_ruta', 'en_destino');

CREATE INDEX IF NOT EXISTS envios_por_estado ON envios (estado);


-- ---------------------------------------------------------------------
--  3. PUNTOS DE CONTROL — se calculan al crear el envío.
--     orden 0 = salida, el último = llegada, los de la mitad = control.
--     hora planeada = salida_real del envío + tiempo_plan_s
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS puntos_control (
    id_envio              INTEGER NOT NULL REFERENCES envios (id) ON DELETE CASCADE,
    orden                 SMALLINT NOT NULL,
    tipo                  TEXT NOT NULL,          -- 'salida' | 'control' | 'llegada'
    nombre                TEXT NOT NULL,          -- p. ej. "Calle 72 con Carrera 46"
    lat                   DOUBLE PRECISION NOT NULL,
    lon                   DOUBLE PRECISION NOT NULL,
    distancia_m           INTEGER NOT NULL,       -- desde el origen, medida sobre la ruta
    tiempo_plan_s         INTEGER NOT NULL,       -- desde la salida, según la ruta
    hora_real             TIMESTAMPTZ,            -- cuándo lo pasó (NULL = todavía no)
    retraso_s             INTEGER,                -- hora_real − hora planeada
    velocidad_kmh         DOUBLE PRECISION,       -- promedio desde el punto anterior

    PRIMARY KEY (id_envio, orden),
    CONSTRAINT puntos_tipo            CHECK (tipo IN ('salida', 'control', 'llegada')),
    CONSTRAINT puntos_valores         CHECK (orden >= 0 AND distancia_m >= 0 AND tiempo_plan_s >= 0)
);


-- ---------------------------------------------------------------------
--  4. ALERTAS — las crea el listener. Una alerta queda "activa" mientras
--     fin sea NULL; se cierra cuando el problema se corrige.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alertas (
    id                    SERIAL PRIMARY KEY,
    id_envio              INTEGER NOT NULL REFERENCES envios (id) ON DELETE CASCADE,
    tipo                  TEXT NOT NULL,          -- 'retraso' | 'desvio' (en S3 se suman las de la carga)
    inicio                TIMESTAMPTZ NOT NULL DEFAULT now(),
    fin                   TIMESTAMPTZ,            -- NULL = sigue activa
    valor_max             INTEGER NOT NULL,       -- el peor valor: segundos (retraso) o metros (desvío)
    lat                   DOUBLE PRECISION,       -- dónde empezó
    lon                   DOUBLE PRECISION,
    vista_en              TIMESTAMPTZ,            -- cuándo la marcaron como vista en la página

    CONSTRAINT alertas_tipo           CHECK (tipo IN ('retraso', 'desvio')),
    CONSTRAINT alertas_fin            CHECK (fin IS NULL OR fin >= inicio)
);

-- Solo puede haber una alerta activa de cada tipo por envío.
CREATE UNIQUE INDEX IF NOT EXISTS alertas_una_activa
    ON alertas (id_envio, tipo) WHERE fin IS NULL;


-- ---------------------------------------------------------------------
--  5. PERMISOS para el usuario de la app (listener y servidor web).
--     Leer, crear y actualizar; NUNCA borrar (un envío se cancela,
--     no se borra).
-- ---------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON camiones, envios, puntos_control, alertas TO app_rastreo;
GRANT USAGE, SELECT ON SEQUENCE envios_id_seq, alertas_id_seq TO app_rastreo;

COMMIT;


-- ---------------------------------------------------------------------
--  Comprobación rápida (se puede ejecutar cuantas veces quieras):
-- ---------------------------------------------------------------------
SELECT t AS tabla,
       has_table_privilege('app_rastreo', t, 'SELECT') AS leer,
       has_table_privilege('app_rastreo', t, 'INSERT') AS crear,
       has_table_privilege('app_rastreo', t, 'UPDATE') AS actualizar,
       has_table_privilege('app_rastreo', t, 'DELETE') AS borrar
FROM unnest(ARRAY['camiones', 'envios', 'puntos_control', 'alertas']) AS t;

-- ---------------------------------------------------------------------
--  Si algo quedó mal y quieres empezar de cero (borra las 4 tablas):
--      DROP TABLE alertas, puntos_control, envios, camiones;
-- ---------------------------------------------------------------------
