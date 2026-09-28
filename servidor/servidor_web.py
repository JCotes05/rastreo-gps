"""
servidor_web.py — Expone los datos como JSON (no sirve HTML).

El HTML/CSS/JavaScript ya NO vive aquí — es un archivo estático
(index.html) que Nginx sirve directamente desde el disco, sin pasar
por Python. Este programa es el ÚNICO que le habla a la base de datos
para LEER, y responde estas rutas:

  /datos                  último punto GPS (Tiempo Real)
  /recorrido              puntos del recorrido en curso (Tiempo Real)
  /recorridos             resumen de recorridos, con filtro opcional por
                          rango de fecha/hora (?desde=...&hasta=...&limite=N)
  /recorridos_zona        recorridos que pasaron por un círculo
                          (?lat=...&lon=...&radio=metros)
  /recorridos_rango       primera y última fecha con datos (límites del calendario)
  /puntos_recorrido       todos los puntos de UN recorrido (?id=...)
  /puntos_recorridos      la geometría de VARIOS recorridos de una vez
                          (?ids=a,b,c&paso=N) para dibujarlos juntos en el mapa
"""

import json
import os
import re
import sys
import traceback
import psycopg2
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
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


# ---------------------------------------------------------------
#  HISTÓRICOS — consultas sobre recorridos ya guardados
# ---------------------------------------------------------------

RADIO_TIERRA_M = 6371000.0

# Colombia no tiene horario de verano: el mismo desfase fijo (-5 h) que
# usa listener.py al guardar hora_recepcion.
ZONA_HORARIA = "-05:00"

_PATRON_INSTANTE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")


def _normalizar_instante(texto, es_fin):
    """
    Convierte lo que manda el navegador ("2026-09-27T20:43" o solo
    "2026-09-27") en un instante completo con la zona horaria de
    Colombia, listo para compararse contra hora_recepcion.

    Si es el FIN del rango y viene solo hasta el minuto, se completa al
    final de ese minuto (:59.999999) para que sea inclusivo.
    """
    if not texto:
        return None
    texto = texto.strip()
    if not _PATRON_INSTANTE.match(texto):
        return None
    if len(texto) == 10:                       # solo la fecha
        texto += "T23:59:59.999999" if es_fin else "T00:00:00"
    elif len(texto) == 16:                     # fecha + hh:mm
        texto += ":59.999999" if es_fin else ":00"
    return texto + ZONA_HORARIA


def _limitar(valor, por_defecto=30, maximo=100):
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return por_defecto
    return max(1, min(n, maximo))


def _resumir_grupos(cursor, grupos):
    """
    Recibe filas (id_recorrido, primer_id, ultimo_id, num_puntos) y arma
    el resumen de cada recorrido: cuándo/dónde empezó y cuándo/dónde
    terminó. Lo comparten /recorridos y /recorridos_zona.
    """
    resultado = []
    for id_recorrido, primer_id, ultimo_id, num_puntos in grupos:
        cursor.execute(
            "SELECT latitud, longitud, hora_gps, hora_recepcion FROM ubicaciones WHERE id = %s",
            (primer_id,),
        )
        inicio = cursor.fetchone()
        cursor.execute(
            "SELECT latitud, longitud, hora_gps, hora_recepcion FROM ubicaciones WHERE id = %s",
            (ultimo_id,),
        )
        fin = cursor.fetchone()

        resultado.append({
            "id_recorrido": id_recorrido,
            "num_puntos": num_puntos,
            "lat_inicio": inicio[0], "lon_inicio": inicio[1],
            "hora_inicio": inicio[2] or "—", "hora_recepcion_inicio": inicio[3],
            "lat_fin": fin[0], "lon_fin": fin[1],
            "hora_fin": fin[2] or "—", "hora_recepcion_fin": fin[3],
        })
    return resultado


def obtener_lista_recorridos(limite=30, desde=None, hasta=None):
    """
    Resumen de los recorridos guardados, del más reciente al más antiguo.

    Sin filtros devuelve los últimos `limite` (así se piden "las últimas
    4 rutas"). Con desde/hasta devuelve los recorridos que se SOLAPAN
    con ese rango: empezaron antes del fin del rango Y terminaron
    después de su inicio — un viaje de varios días aparece si cualquier
    parte suya cae dentro de la ventana.
    """
    limite = _limitar(limite)
    desde = _normalizar_instante(desde, es_fin=False)
    hasta = _normalizar_instante(hasta, es_fin=True)

    condiciones = []
    parametros = []
    if desde:
        condiciones.append("MAX(hora_recepcion::timestamptz) >= %s::timestamptz")
        parametros.append(desde)
    if hasta:
        condiciones.append("MIN(hora_recepcion::timestamptz) <= %s::timestamptz")
        parametros.append(hasta)
    having = ("HAVING " + " AND ".join(condiciones)) if condiciones else ""

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    resultado = []
    try:
        cursor.execute(f"""
            SELECT id_recorrido, MIN(id) AS primer_id, MAX(id) AS ultimo_id, COUNT(*) AS num_puntos
            FROM ubicaciones
            WHERE id_recorrido IS NOT NULL
            GROUP BY id_recorrido
            {having}
            ORDER BY MIN(id) DESC
            LIMIT %s
        """, parametros + [limite])
        resultado = _resumir_grupos(cursor, cursor.fetchall())
    except psycopg2.errors.UndefinedColumn:
        conexion.rollback()
    finally:
        cursor.close()
        conexion.close()
    return resultado


def obtener_recorridos_por_zona(lat, lon, radio, limite=30):
    """
    Recorridos que pasaron por un círculo (centro lat/lon, radio en metros).

    La distancia se calcula con la fórmula de Haversine directamente en
    SQL (el proyecto no usa PostGIS). BOOL_OR(...) en el HAVING deja
    pasar a un recorrido si AL MENOS UNO de sus puntos cae dentro del
    círculo; num_puntos sigue contando TODOS los puntos del recorrido.

    Limitación: solo se evalúan los puntos GPS guardados, no los tramos
    entre ellos — con radios muy pequeños y un vehículo rápido, un
    cruce entre dos lecturas podría no detectarse.
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 10 <= radio <= 200000):
        return []
    limite = _limitar(limite)

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    resultado = []
    try:
        cursor.execute("""
            SELECT id_recorrido, MIN(id) AS primer_id, MAX(id) AS ultimo_id, COUNT(*) AS num_puntos
            FROM ubicaciones
            WHERE id_recorrido IS NOT NULL
            GROUP BY id_recorrido
            HAVING BOOL_OR(
                2 * %(R)s * ASIN(SQRT(LEAST(1.0,
                    POWER(SIN(RADIANS((latitud::float8 - %(lat)s::float8) / 2)), 2) +
                    COS(RADIANS(%(lat)s::float8)) * COS(RADIANS(latitud::float8)) *
                    POWER(SIN(RADIANS((longitud::float8 - %(lon)s::float8) / 2)), 2)
                ))) <= %(radio)s::float8
            )
            ORDER BY MIN(id) DESC
            LIMIT %(limite)s
        """, {"R": RADIO_TIERRA_M, "lat": lat, "lon": lon, "radio": radio, "limite": limite})
        resultado = _resumir_grupos(cursor, cursor.fetchall())
    except psycopg2.errors.UndefinedColumn:
        conexion.rollback()
    finally:
        cursor.close()
        conexion.close()
    return resultado


def obtener_rango_fechas():
    """
    Primer y último instante con datos de recorridos. El calendario de
    la página usa esto para no dejar escoger fechas sin información.
    """
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    primero = ultimo = None
    try:
        cursor.execute("""
            SELECT hora_recepcion FROM ubicaciones
            WHERE id_recorrido IS NOT NULL
            ORDER BY hora_recepcion::timestamptz ASC LIMIT 1
        """)
        fila = cursor.fetchone()
        primero = fila[0] if fila else None
        cursor.execute("""
            SELECT hora_recepcion FROM ubicaciones
            WHERE id_recorrido IS NOT NULL
            ORDER BY hora_recepcion::timestamptz DESC LIMIT 1
        """)
        fila = cursor.fetchone()
        ultimo = fila[0] if fila else None
    except psycopg2.errors.UndefinedColumn:
        conexion.rollback()
    finally:
        cursor.close()
        conexion.close()
    return {"min": primero, "max": ultimo}


def obtener_geometria_de_recorridos(ids, paso=1):
    """
    Devuelve {id_recorrido: [[lat, lon], ...]} para VARIOS recorridos en
    una sola consulta — lo que necesita el mapa para dibujar de golpe
    todas las rutas de un filtro, cada una de un color.

    `paso` reduce la cantidad de puntos (1 de cada N, siempre conservando
    el primero y el último): para DIBUJAR una vista general no hace
    falta la resolución completa, y así la respuesta pesa mucho menos.
    """
    ids = [i for i in ids if i][:50]
    if not ids:
        return {}
    paso = _limitar(paso, por_defecto=1, maximo=1000)

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    filas = []
    try:
        cursor.execute("""
            SELECT id_recorrido, latitud, longitud
            FROM ubicaciones
            WHERE id_recorrido = ANY(%s)
            ORDER BY id_recorrido, id ASC
        """, (ids,))
        filas = cursor.fetchall()
    except psycopg2.errors.UndefinedColumn:
        conexion.rollback()
    finally:
        cursor.close()
        conexion.close()

    agrupado = {}
    for id_recorrido, lat, lon in filas:
        agrupado.setdefault(id_recorrido, []).append([lat, lon])

    if paso > 1:
        for id_recorrido, puntos in agrupado.items():
            if len(puntos) > 2:
                reducidos = puntos[::paso]
                if reducidos[-1] is not puntos[-1]:
                    reducidos.append(puntos[-1])
                agrupado[id_recorrido] = reducidos
    return agrupado


def obtener_puntos_de_recorrido(id_recorrido):
    """
    Igual que obtener_puntos_del_recorrido_actual(), pero para UN
    recorrido específico (por su id_recorrido), no necesariamente el
    más reciente — usado por la sección Históricos al seleccionar uno
    de la lista.
    """
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    filas = []
    try:
        cursor.execute("""
            SELECT latitud, longitud, hora_gps, hora_recepcion
            FROM ubicaciones
            WHERE id_recorrido = %s
            ORDER BY id ASC
        """, (id_recorrido,))
        filas = cursor.fetchall()
    except psycopg2.errors.UndefinedColumn:
        conexion.rollback()
    finally:
        cursor.close()
        conexion.close()
    return [
        {
            "lat": lat, "lon": lon,
            "hora": hora_gps or "—",
            "fecha": hora_recepcion.split("T")[0],
            "hora_recepcion": hora_recepcion,
        }
        for lat, lon, hora_gps, hora_recepcion in filas
    ]


def _numero(parametros, nombre):
    """Lee un parámetro numérico de la URL; devuelve None si falta o no es un número."""
    try:
        return float(parametros.get(nombre, [""])[0])
    except ValueError:
        return None


class ManejadorDeSolicitudes(BaseHTTPRequestHandler):
    def do_GET(self):
        # Nginx sirve "/" directamente desde el disco (index.html) y
        # solo reenvía aquí las peticiones a estas rutas de datos.
        ruta = urlparse(self.path)
        parametros = parse_qs(ruta.query)

        try:
            if ruta.path == "/recorrido":
                datos = obtener_puntos_del_recorrido_actual()
            elif ruta.path == "/recorridos":
                datos = obtener_lista_recorridos(
                    limite=parametros.get("limite", [None])[0],
                    desde=parametros.get("desde", [None])[0],
                    hasta=parametros.get("hasta", [None])[0],
                )
            elif ruta.path == "/recorridos_zona":
                lat, lon, radio = _numero(parametros, "lat"), _numero(parametros, "lon"), _numero(parametros, "radio")
                if None in (lat, lon, radio):
                    datos = []
                else:
                    datos = obtener_recorridos_por_zona(lat, lon, radio, parametros.get("limite", [None])[0])
            elif ruta.path == "/recorridos_rango":
                datos = obtener_rango_fechas()
            elif ruta.path == "/puntos_recorrido":
                id_recorrido = parametros.get("id", [None])[0]
                datos = obtener_puntos_de_recorrido(id_recorrido) if id_recorrido else []
            elif ruta.path == "/puntos_recorridos":
                ids = parametros.get("ids", [""])[0].split(",")
                datos = obtener_geometria_de_recorridos(ids, parametros.get("paso", [None])[0])
            else:
                datos = obtener_datos_actuales()
            estado = 200
        except Exception:
            # Un error de base de datos no debe dejar al navegador
            # colgado sin respuesta: se registra y se responde 500 en
            # JSON, y la página muestra un mensaje en vez de quedarse
            # "Cargando…" para siempre.
            traceback.print_exc(file=sys.stderr)
            datos = {"error": "interno"}
            estado = 500

        cuerpo = json.dumps(datos).encode("utf-8")
        self.send_response(estado)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, format, *args):
        pass


def iniciar_servidor():
    crear_tabla()
    # ThreadingHTTPServer atiende cada petición en su propio hilo: la
    # página hace varias a la vez (lista, geometrías, rango de fechas, y el
    # sondeo de Tiempo Real cada 5 s), y así una consulta lenta no deja
    # esperando a las demás. Cada petición abre su propia conexión a la
    # base de datos, por eso es seguro hacerlo en paralelo.
    servidor = ThreadingHTTPServer(("0.0.0.0", PUERTO), ManejadorDeSolicitudes)
    print(f"Servidor de datos activo en el puerto {PUERTO}... (Ctrl+C para detener)")
    servidor.serve_forever()


if __name__ == "__main__":
    iniciar_servidor()
