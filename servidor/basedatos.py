"""
basedatos.py — Crea y prepara la tabla en la base de datos PostgreSQL
(alojada en AWS RDS) que va a usar tanto el listener UDP como el
servidor HTTP.

A diferencia de la versión con SQLite, aquí NO se crea ningún archivo
en el disco — nos conectamos por red a un servidor de base de datos
que ya existe (tu instancia RDS), usando estos datos de conexión.
"""

import os
import psycopg2

# --- Datos de conexión a tu instancia RDS ---
# HOST, USUARIO y NOMBRE_BD son iguales para los 3 integrantes (misma
# RDS compartida) — no hace falta tocarlos.
HOST = "basedatosdiseno.cw3qo244673z.us-east-1.rds.amazonaws.com"
USUARIO = "app_rastreo"
PUERTO = 5432
NOMBRE_BD = "basedatosdiseno"

# La CONTRASEÑA sí es sensible, y este archivo se despliega
# automáticamente desde GitHub a las 3 instancias — así que NO puede
# vivir escrita aquí (o el despliegue la pisaría con un valor de
# ejemplo en cada push). En su lugar, se lee de un archivo local que
# nunca se sube al repositorio (ver .gitignore).
RUTA_CONTRASENA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contrasena_bd.txt")


def obtener_contrasena():
    try:
        with open(RUTA_CONTRASENA, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"Falta el archivo {RUTA_CONTRASENA} con la contraseña de la base de datos. "
            f"Créalo una vez con: echo \"tu_contraseña_real\" | sudo tee {RUTA_CONTRASENA}"
        )


CONTRASENA = obtener_contrasena()


def obtener_conexion():
    """
    Abre una conexión a la base de datos PostgreSQL en RDS.
    A diferencia de sqlite3.connect(), esto no "crea" nada — la base
    de datos ya existe en el servidor remoto, solo nos conectamos.
    """
    return psycopg2.connect(
        host=HOST,
        port=PUERTO,
        dbname=NOMBRE_BD,
        user=USUARIO,
        password=CONTRASENA,
    )


def crear_tabla():
    """
    Crea la tabla 'ubicaciones' si todavía no existe. Es seguro llamar
    esta función varias veces.

    Si el usuario conectado NO tiene permiso para crear tablas (como
    "app_rastreo", el usuario restringido de la app — a propósito, por
    seguridad), simplemente asumimos que la tabla ya existe (creada
    antes por el usuario maestro) y seguimos sin caernos.
    """
    conexion = obtener_conexion()
    cursor = conexion.cursor()

    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ubicaciones (
                id SERIAL PRIMARY KEY,
                id_dispositivo TEXT NOT NULL,
                latitud REAL NOT NULL,
                longitud REAL NOT NULL,
                hora_gps TEXT,
                hora_recepcion TEXT NOT NULL,
                id_recorrido TEXT
            )
        """)
        conexion.commit()
        print(f"Base de datos lista en {HOST}")
    except psycopg2.errors.InsufficientPrivilege:
        # "rollback" es obligatorio aquí: en PostgreSQL, apenas un
        # comando falla dentro de una transacción, TODA la transacción
        # queda "abortada" hasta que se cancele explícitamente.
        conexion.rollback()
        print(f"Conectado a {HOST} (usuario de aplicación — sin permiso para crear tablas, se asume que ya existe).")
    finally:
        cursor.close()
        conexion.close()


# ----------------------------------------------------------------
# Este bloque SOLO se ejecuta si corres este archivo directamente
# (python3 basedatos.py) — sirve para probar que la conexión y la
# tabla funcionan, antes de conectar el listener y el servidor web.
# ----------------------------------------------------------------
if __name__ == "__main__":
    from datetime import datetime

    crear_tabla()

    conexion = obtener_conexion()
    cursor = conexion.cursor()

    # OJO: los espacios reservados en psycopg2 son "%s", no "?" como
    # en sqlite3 — es la única diferencia real de sintaxis entre los dos.
    cursor.execute("""
        INSERT INTO ubicaciones (id_dispositivo, latitud, longitud, hora_gps, hora_recepcion)
        VALUES (%s, %s, %s, %s, %s)
    """, ("camion_prueba", 10.96854, -74.78142, "23:59:59", datetime.now().isoformat()))
    conexion.commit()

    print("\nRegistro de prueba insertado. Contenido actual de la tabla:\n")
    cursor.execute("SELECT * FROM ubicaciones")
    for fila in cursor.fetchall():
        print(fila)

    cursor.close()
    conexion.close()
