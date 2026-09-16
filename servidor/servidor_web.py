"""
servidor_web.py — Ahora SOLO expone el endpoint /datos (JSON puro).

El HTML/CSS/JavaScript ya NO vive aquí — es un archivo estático
(index.html) que Nginx sirve directamente desde el disco, sin pasar
por Python. Este programa se volvió más simple: su único trabajo es
consultar la base de datos y devolver los 4 valores como JSON.
"""

import json
import os
import psycopg2
from http.server import BaseHTTPRequestHandler, HTTPServer
from basedatos import obtener_conexion, crear_tabla

PUERTO = 8080

# El nombre de cada integrante NO vive en este archivo — este archivo
# se despliega IDÉNTICO a las 3 instancias por GitHub Actions. En vez
# de eso, cada instancia tiene su propio "nombre.txt" local (NUNCA
# subido a GitHub, por eso está en .gitignore) al lado de este script.
# Así, el despliegue automático nunca le pisa el nombre a nadie.
RUTA_NOMBRE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nombre.txt")


def obtener_nombre_integrante():
    try:
        with open(RUTA_NOMBRE, "r", encoding="utf-8") as f:
            nombre = f.read().strip()
            return nombre if nombre else "Sin nombre"
    except FileNotFoundError:
        return "Sin nombre"


def obtener_ultima_ubicacion():
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    cursor.execute("""
        SELECT latitud, longitud, hora_gps, hora_recepcion
        FROM ubicaciones
        ORDER BY id DESC
        LIMIT 1
    """)
    fila = cursor.fetchone()
    cursor.close()
    conexion.close()
    return fila


def obtener_datos_actuales():
    fila = obtener_ultima_ubicacion()
    nombre = obtener_nombre_integrante()
    if fila is None:
        return {"lat": "—", "lon": "—", "fecha": "—", "hora": "—", "nombre": nombre}

    latitud, longitud, hora_gps, hora_recepcion = fila
    fecha = hora_recepcion.split("T")[0]
    hora = hora_gps if hora_gps else "—"

    return {"lat": str(latitud), "lon": str(longitud), "fecha": fecha, "hora": hora, "nombre": nombre}


def obtener_puntos_del_recorrido_actual():
    """
    Devuelve TODOS los puntos (lat, lon) del recorrido más reciente, en
    orden cronológico. Si la columna id_recorrido todavía no existe
    (falta el ALTER TABLE del usuario maestro), devuelve una lista
    vacía en vez de caerse — el mapa simplemente no dibuja la línea
    todavía, pero el resto de la página sigue funcionando normal.
    """
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            SELECT latitud, longitud
            FROM ubicaciones
            WHERE id_recorrido = (
                SELECT id_recorrido FROM ubicaciones
                WHERE id_recorrido IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
            )
            ORDER BY id ASC
        """)
        filas = cursor.fetchall()
    except psycopg2.errors.UndefinedColumn:
        conexion.rollback()
        filas = []
    finally:
        cursor.close()
        conexion.close()
    return [{"lat": lat, "lon": lon} for lat, lon in filas]


class ManejadorDeSolicitudes(BaseHTTPRequestHandler):
    def do_GET(self):
        # Nginx sirve "/" directamente desde el disco (index.html) y
        # solo reenvía aquí las peticiones a /datos y /recorrido.
        if self.path == "/recorrido":
            datos = obtener_puntos_del_recorrido_actual()
        else:
            datos = obtener_datos_actuales()

        cuerpo = json.dumps(datos).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, format, *args):
        pass


def iniciar_servidor():
    crear_tabla()
    servidor = HTTPServer(("0.0.0.0", PUERTO), ManejadorDeSolicitudes)
    print(f"Servidor de datos activo en el puerto {PUERTO}... (Ctrl+C para detener)")
    servidor.serve_forever()


if __name__ == "__main__":
    iniciar_servidor()
