"""
listener.py — El "sniffer propio": escucha el puerto UDP de forma
PERMANENTE, recibe las coordenadas que manda la app Android, y las
guarda en la base de datos PostgreSQL compartida (RDS).
"""

import os  # P3-S1
import re  # P3-S1
import socket
import psycopg2
from datetime import datetime, timezone, timedelta
from basedatos import obtener_conexion, crear_tabla

# P3-S1: el puerto se puede cambiar con la variable de entorno PUERTO_UDP
# (solo para pruebas). Si no existe, sigue siendo 5000 como siempre.
PUERTO = int(os.environ.get("PUERTO_UDP", "5000"))

# P3-S1: una cédula válida son solo dígitos del 0 al 9, entre 5 y 15.
PATRON_CEDULA = re.compile(r"^[0-9]{5,15}$")

# P3-S1: id_recorrido cuya planilla ya quedó "resuelta" (creada, o con
# una cédula que no existe en la tabla conductores). La app manda un
# paquete cada 5 s con la misma cédula; con esto el INSERT se intenta
# una sola vez por recorrido en vez de en cada paquete.
planillas_resueltas = set()
MAX_PLANILLAS_EN_MEMORIA = 10000

# Los servidores de AWS corren en UTC por defecto, 5 horas adelante de
# Colombia. Sin esto, datetime.now() guardaba la hora del servidor
# (UTC) en vez de la hora real — lo que hacía que "hora_recepcion"
# quedara 5 horas adelantada, y hasta la FECHA se corriera al día
# siguiente para cualquier hora después de las 7:00 p.m. Colombia no
# tiene horario de verano, así que un desfase fijo de -5 siempre es
# correcto, sin necesitar tablas de husos horarios.
COLOMBIA = timezone(timedelta(hours=-5))


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


# P3-S1
def extraer_cedula(texto):
    """
    Busca el campo opcional CONDUCTOR:<cedula> por su NOMBRE (no por su
    posición) y devuelve la cédula como texto, o None si el campo no
    viene o la cédula no es válida (solo dígitos, entre 5 y 15).
    """
    for parte in texto.strip().split(","):
        parte = parte.strip()
        if parte.startswith("CONDUCTOR:"):
            cedula = parte.split(":", 1)[1].strip()
            if PATRON_CEDULA.match(cedula):
                return cedula
            print(f"[AVISO] Cédula inválida en el mensaje, se ignora: {cedula!r}")
            return None
    return None


def guardar_ubicacion(id_dispositivo, latitud, longitud, hora_gps, id_recorrido):
    """Inserta una nueva fila en la tabla 'ubicaciones'."""
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            INSERT INTO ubicaciones (id_dispositivo, latitud, longitud, hora_gps, hora_recepcion, id_recorrido)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (id_dispositivo, latitud, longitud, hora_gps, datetime.now(COLOMBIA).isoformat(), id_recorrido))
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
        """, (id_dispositivo, latitud, longitud, hora_gps, datetime.now(COLOMBIA).isoformat()))
        conexion.commit()
        print("[AVISO] La columna id_recorrido no existe todavía — se guardó sin ella.")
    finally:
        cursor.close()
        conexion.close()


# P3-S1
def registrar_planilla(id_recorrido, cedula):
    """
    Registra en 'planillas' qué conductor hizo el recorrido, en su PROPIA
    transacción (separada de la de la posición). Usa ON CONFLICT DO
    NOTHING porque el usuario de la app no tiene permiso de UPDATE: si
    el recorrido ya tenía planilla, se queda con la primera cédula.

    Devuelve True si el caso quedó "resuelto" (planilla creada, ya
    existía, o la cédula no está registrada) y False si hubo un error
    inesperado (para reintentar con el siguiente paquete).
    Nunca lanza excepciones: el listener no se puede caer por esto.
    """
    conexion = None
    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor()
        try:
            cursor.execute(
                "INSERT INTO planillas (id_recorrido, cedula) VALUES (%s, %s) "
                "ON CONFLICT (id_recorrido) DO NOTHING",
                (id_recorrido, cedula),
            )
            creada = cursor.rowcount == 1
            conexion.commit()
            if creada:
                print(f"[PLANILLA] Recorrido {id_recorrido} → conductor {cedula}")
            else:
                print(f"[PLANILLA] Recorrido {id_recorrido} ya tenía planilla, se conserva la primera.")
            return True
        except psycopg2.errors.ForeignKeyViolation:
            conexion.rollback()
            print(f"[AVISO] Cédula no registrada: {cedula} (recorrido {id_recorrido}). La posición sí se guardó.")
            return True
        except Exception as error:
            conexion.rollback()
            print(f"[ERROR] No se pudo registrar la planilla del recorrido {id_recorrido}: {error}")
            return False
        finally:
            cursor.close()
    except Exception as error:
        # Por ejemplo, no se pudo ni abrir la conexión.
        print(f"[ERROR] Falló la conexión al registrar la planilla: {error}")
        return False
    finally:
        if conexion is not None:
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
        cedula = extraer_cedula(texto)  # P3-S1
        guardar_ubicacion(ip_origen, latitud, longitud, hora_gps, id_recorrido)
        fecha_hora = datetime.now(COLOMBIA).strftime("%d/%m/%Y %H:%M:%S")
        print(f"[GUARDADO] {fecha_hora} — {ip_origen} → lat={latitud}, lon={longitud}, recorrido={id_recorrido}")

        # P3-S1: DESPUÉS de guardar la posición (ya con commit), se intenta
        # la planilla. Si algo falla aquí, la posición ya está a salvo.
        if cedula and id_recorrido and id_recorrido not in planillas_resueltas:
            try:
                if registrar_planilla(id_recorrido, cedula):
                    if len(planillas_resueltas) > MAX_PLANILLAS_EN_MEMORIA:
                        planillas_resueltas.clear()
                    planillas_resueltas.add(id_recorrido)
            except Exception as error:
                print(f"[ERROR] Error inesperado con la planilla: {error}")


if __name__ == "__main__":
    iniciar_listener()
