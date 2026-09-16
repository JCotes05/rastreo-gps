"""
listener.py — El "sniffer propio": escucha el puerto UDP de forma
PERMANENTE, recibe las coordenadas que manda la app Android, y las
guarda en la base de datos PostgreSQL compartida (RDS).
"""

import socket
import psycopg2
from datetime import datetime
from basedatos import obtener_conexion, crear_tabla

PUERTO = 5000


def parsear_mensaje(texto):
    """
    Intenta extraer latitud, longitud, hora del GPS e ID de recorrido de
    un mensaje con el formato acordado:
    "LAT:10.96854,LON:-74.78142,HORA:14:32:10,RECORRIDO:1699999999999"
    """
    try:
        partes = texto.strip().split(",")
        lat_texto = partes[0].split(":")[1]
        lon_texto = partes[1].split(":")[1]
        hora_gps = partes[2].split(":", 1)[1]
        id_recorrido = partes[3].split(":", 1)[1]
        return float(lat_texto), float(lon_texto), hora_gps, id_recorrido
    except (IndexError, ValueError):
        return None


def guardar_ubicacion(id_dispositivo, latitud, longitud, hora_gps, id_recorrido):
    """Inserta una nueva fila en la tabla 'ubicaciones'."""
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            INSERT INTO ubicaciones (id_dispositivo, latitud, longitud, hora_gps, hora_recepcion, id_recorrido)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (id_dispositivo, latitud, longitud, hora_gps, datetime.now().isoformat(), id_recorrido))
        conexion.commit()
    except psycopg2.errors.UndefinedColumn:
        # La columna id_recorrido todavía no existe en la tabla real
        # (falta que alguien con el usuario MAESTRO corra el ALTER
        # TABLE). Mientras tanto, guardamos igual sin ese dato — así
        # el sistema sigue funcionando para todo el equipo, sin
        # bloquearse por una sola columna pendiente.
        conexion.rollback()
        cursor.execute("""
            INSERT INTO ubicaciones (id_dispositivo, latitud, longitud, hora_gps, hora_recepcion)
            VALUES (%s, %s, %s, %s, %s)
        """, (id_dispositivo, latitud, longitud, hora_gps, datetime.now().isoformat()))
        conexion.commit()
        print("[AVISO] La columna id_recorrido no existe todavía — se guardó sin ella.")
    finally:
        cursor.close()
        conexion.close()


def iniciar_listener():
    crear_tabla()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PUERTO))
    print(f"Escuchando en el puerto {PUERTO}... (Ctrl+C para detener)")

    while True:
        datos, direccion = sock.recvfrom(1024)
        ip_origen = direccion[0]
        texto = datos.decode("utf-8", errors="replace")

        resultado = parsear_mensaje(texto)
        if resultado is None:
            print(f"[IGNORADO] Mensaje con formato inválido desde {ip_origen}: {texto!r}")
            continue

        latitud, longitud, hora_gps, id_recorrido = resultado
        guardar_ubicacion(ip_origen, latitud, longitud, hora_gps, id_recorrido)
        fecha_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        print(f"[GUARDADO] {fecha_hora} — {ip_origen} → lat={latitud}, lon={longitud}, recorrido={id_recorrido}")


if __name__ == "__main__":
    iniciar_listener()
