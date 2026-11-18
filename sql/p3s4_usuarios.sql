-- =====================================================================
--  p3s4_usuarios.sql — P3-S4: acceso a la página con usuario y contraseña.
--
--  Se ejecuta UNA sola vez con el USUARIO MAESTRO de la RDS, ANTES de
--  desplegar el código de P3-S4. Si se vuelve a ejecutar no pasa nada.
--
--  Qué crea:
--    usuarios      las personas que pueden entrar a la página, con su rol:
--                    admin     todo, incluido crear y desactivar usuarios
--                    operador  ver, crear y cancelar envíos, marcar alertas
--                    consulta  solo mirar
--    sesiones_web  una fila por cada inicio de sesión (dura 12 horas). Se
--                  guarda el SHA-256 del token, nunca el token.
--
--  No se borra nada: un usuario se desactiva (activo = false) y una sesión
--  se cierra poniendo vence = now(). Por eso app_rastreo no necesita DELETE.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS usuarios (
    usuario         VARCHAR(30)  PRIMARY KEY,
    nombre          VARCHAR(80)  NOT NULL,
    clave_hash      TEXT         NOT NULL,             -- pbkdf2_sha256$600000$sal$hash
    rol             VARCHAR(10)  NOT NULL,
    activo          BOOLEAN      NOT NULL DEFAULT true,
    creado_en       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    creado_por      VARCHAR(30),                        -- NULL = creado con crear_usuario.py
    ultimo_ingreso  TIMESTAMPTZ,
    CONSTRAINT usuarios_usuario CHECK (usuario ~ '^[a-z0-9._-]{3,30}$'),
    CONSTRAINT usuarios_rol CHECK (rol IN ('admin', 'operador', 'consulta'))
);

CREATE TABLE IF NOT EXISTS sesiones_web (
    token_hash  CHAR(64)     PRIMARY KEY,               -- SHA-256 en hexadecimal
    usuario     VARCHAR(30)  NOT NULL REFERENCES usuarios(usuario),
    creada_en   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    vence       TIMESTAMPTZ  NOT NULL,
    ip          VARCHAR(45)
);
CREATE INDEX IF NOT EXISTS sesiones_web_usuario ON sesiones_web (usuario, vence);

GRANT SELECT, INSERT, UPDATE ON usuarios, sesiones_web TO app_rastreo;

COMMIT;


-- ---------------------------------------------------------------------
--  Comprobación rápida: deben salir las 2 tablas con "true" en las 3 columnas.
-- ---------------------------------------------------------------------
SELECT t AS tabla,
       has_table_privilege('app_rastreo', t, 'SELECT') AS leer,
       has_table_privilege('app_rastreo', t, 'INSERT') AS crear,
       has_table_privilege('app_rastreo', t, 'UPDATE') AS actualizar
FROM unnest(ARRAY['usuarios', 'sesiones_web']) AS t;

-- ---------------------------------------------------------------------
--  Para deshacer (NO hace falta; borra los usuarios creados):
--    DROP TABLE sesiones_web; DROP TABLE usuarios;
-- ---------------------------------------------------------------------
