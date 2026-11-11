"""
api_envios.py — P3-S2, paso 3: API de camiones y envíos de Orbita.

servidor_web.py le pasa aquí todo lo que llegue a /api/... con:
    estado, datos = api_envios.atender(metodo, ruta, parametros, cuerpo, autorizacion)
y responde "datos" como JSON con ese código de estado. Esta función
nunca lanza excepciones: cualquier error se vuelve una respuesta JSON
{"error": "..."} (y "campo" cuando el problema es un dato del formulario).

Para la APP del camión (con "Authorization: Bearer <token>", salvo registro y login):
  POST /api/camiones/registro         crea el camión y abre sesión
  POST /api/camiones/login            abre sesión con placa y contraseña
  POST /api/camiones/logout           cierra la sesión
  GET  /api/camiones/yo               datos del camión
  POST /api/camiones/disponibilidad   {"disponible": true|false, "lat", "lon"}
  POST /api/camiones/ubicacion        {"lat", "lon"}  (cada minuto, mientras la app está abierta)
  GET  /api/camiones/viaje            envío asignado o en curso (?ruta=1 incluye la línea)
  POST /api/camiones/viaje/finalizar  termina el viaje en curso
  POST /api/camiones/viaje/eventos    P3-S3: lote de eventos del ESP32 de sensores y su latido
                                      (ver eventos_carga.py)
  POST /api/camiones/conductor/identificar   P3-S3: {"rostros": [jpg base64...]} del ESP32-CAM;
                                      responde el conductor reconocido (ver reconocimiento_facial.py)

Para la PÁGINA:
  GET  /api/catalogo                  configuraciones, carrocerías, tipos de carga y empaques
  GET  /api/camiones                  la flota (sin datos del dueño)
  POST /api/envios/cotizar            ruta, puntos de control y camiones candidatos
  POST /api/envios                    crea el envío y le asigna el camión
  GET  /api/envios                    lista (?estado=activos|finalizados|todos&limite=50)
  GET  /api/envios/<id>               detalle (?ruta=0 para no traer la línea)
  POST /api/envios/<id>/cancelar      solo si el camión todavía no ha salido
  POST /api/alertas/<id>/vista        marca una alerta como vista
  GET  /api/planillas/<id_recorrido>/foto   P3-S3: foto con la que se reconoció al conductor (7 días)

Para el resto de servidor_web.py:
  resumen_envio_de_recorrido(id_recorrido)  -> clave "envio" de /datos
  agregar_envios(lista_recorridos)          -> clave "envio" de cada recorrido en Históricos
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from base64 import b64decode, b64encode
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.errors
import psycopg2.extras

import eventos_carga  # P3-S3
import reconocimiento_facial  # P3-S3 (esquema híbrido)
import rutas
from basedatos import obtener_conexion

# ---------------------------------------------------------------------
# Reglas del negocio (las mismas que valida la base de datos en p3s2.sql)
# ---------------------------------------------------------------------
# Peso bruto vehicular máximo por configuración (Res. 4100 de 2004, art. 8).
# La capacidad de carga del camión tiene que ser menor que este valor.
CONFIGURACIONES = {
    "C2":  ("C2 · Turbo o sencillo (2 ejes)", 16000),
    "C3":  ("C3 · Doble troque (3 ejes)", 28000),
    "C4":  ("C4 · Cuatro ejes", 36000),
    "2S2": ("2S2 · Tractocamión (4 ejes)", 32000),
    "2S3": ("2S3 · Tractocamión (5 ejes)", 40500),
    "3S2": ("3S2 · Tractomula (5 ejes)", 48000),
    "3S3": ("3S3 · Tractomula (6 ejes)", 52000),
}
CARROCERIAS = {
    "furgon": "Furgón", "refrigerado": "Furgón refrigerado", "estacas": "Estacas",
    "planchon": "Planchón", "volco": "Volco", "tanque": "Tanque", "portacontenedor": "Portacontenedor",
}
# Qué carrocerías sirven para cada tipo de carga.
TIPOS_CARGA = {
    "general":     ("Carga general", ("furgon", "estacas", "planchon")),
    "refrigerada": ("Refrigerada", ("refrigerado",)),
    "granel":      ("Granel sólido", ("volco",)),
    "liquidos":    ("Líquidos", ("tanque",)),
    "contenedor":  ("Contenedor", ("portacontenedor",)),
}
EMPAQUES = {"cajas": "Cajas", "estibas": "Estibas", "bultos": "Bultos", "granel": "A granel",
            "contenedor": "Contenedor", "otro": "Otro"}

ESTADOS_ABIERTOS = ("asignado", "en_ruta", "en_destino")
UBICACION_RECIENTE_MIN = 30     # un camión sin reportar ubicación en este tiempo no se ofrece
EMPATE_M = 2000                 # a menos de 2 km de diferencia, gana el camión más pequeño que sirva
MAX_CANDIDATOS = 3

# Retraso: aviso (amarillo) y alerta (rojo). El umbral es el mayor entre
# un valor fijo y un porcentaje del tiempo total planeado del viaje.
AVISO_RETRASO_S, AVISO_RETRASO_PCT = 3 * 60, 0.10
ALERTA_RETRASO_S, ALERTA_RETRASO_PCT = 10 * 60, 0.25

# Modo demostración: si existe el archivo modo_demo.txt junto a este, los umbrales fijos
# bajan a segundos para poder mostrar un aviso y una alerta de retraso en pocos minutos
# (en la presentación). Se activa y desactiva sin reiniciar:  touch ~/modo_demo.txt  /  rm ~/modo_demo.txt
RUTA_MODO_DEMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modo_demo.txt")
AVISO_DEMO_S, ALERTA_DEMO_S = 20, 60

ITERACIONES_CLAVE = 600_000     # PBKDF2-SHA256 (recomendación de OWASP)
DIAS_SESION = 30
MAX_CUERPO = 1_000_000          # bytes
INTENTOS_LOGIN, BLOQUEO_LOGIN_S = 5, 600

BOGOTA = timezone(timedelta(hours=-5))      # Colombia no tiene horario de verano

PATRON_PLACA = re.compile(r"^[A-Z]{3}[0-9]{3}$")
PATRON_DOCUMENTO = re.compile(r"^[0-9]{5,15}(-[0-9])?$")
PATRON_CELULAR = re.compile(r"^3[0-9]{9}$")


class ErrorAPI(Exception):
    def __init__(self, estado, mensaje, campo=None):
        super().__init__(mensaje)
        self.estado, self.mensaje, self.campo = estado, mensaje, campo


# ---------------------------------------------------------------------
# Entrada: servidor_web.py llama a atender()
# ---------------------------------------------------------------------
class _Pedido:
    def __init__(self, parametros, datos, autorizacion, args):
        self.parametros = {k: v[0] if isinstance(v, list) else v for k, v in (parametros or {}).items()}
        self.datos = datos
        self.autorizacion = autorizacion
        self.args = args


def atender(metodo, ruta, parametros, cuerpo, autorizacion):
    """Devuelve (código_http, diccionario). Nunca lanza excepciones."""
    try:
        manejador, args = _buscar(metodo, ruta)
        datos = _leer_json(cuerpo) if metodo == "POST" else {}
        return manejador(_Pedido(parametros, datos, autorizacion, args))
    except ErrorAPI as e:
        respuesta = {"error": e.mensaje}
        if e.campo:
            respuesta["campo"] = e.campo
        return e.estado, respuesta
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 500, {"error": "Error interno del servidor."}


def _leer_json(cuerpo):
    if not cuerpo:
        return {}
    if len(cuerpo) > MAX_CUERPO:
        raise ErrorAPI(413, "El contenido es demasiado grande.")
    try:
        datos = json.loads(cuerpo.decode("utf-8") if isinstance(cuerpo, bytes) else cuerpo)
    except (ValueError, UnicodeDecodeError):
        raise ErrorAPI(400, "El contenido no es un JSON válido.") from None
    if not isinstance(datos, dict):
        raise ErrorAPI(400, "Se esperaba un objeto JSON.")
    return datos


# ---------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------
@contextmanager
def _bd():
    """Cursor con filas tipo diccionario. Hace commit si todo sale bien y rollback si no."""
    conexion = obtener_conexion()
    try:
        cursor = conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cursor
        conexion.commit()
    except BaseException:
        conexion.rollback()
        raise
    finally:
        conexion.close()


COLUMNAS_CAMION = """placa, configuracion, carroceria, capacidad_kg, marca, linea, modelo, color,
    propietario_nombre, propietario_documento, propietario_celular, soat_vence, tecnomecanica_vence,
    estado, ultima_lat, ultima_lon, ubicacion_en, creado_en"""

COLUMNAS_ENVIO = """id, creado_en, cliente_nombre, cliente_nit, origen_direccion, origen_lat, origen_lon,
    cargue_contacto, cargue_celular, destino_direccion, destino_lat, destino_lon, destinatario_nombre,
    destinatario_celular, tipo_carga, descripcion, empaque, cantidad, peso_kg, valor_declarado,
    cargue_programado, placa, estado, ruta_fuente, distancia_m, duracion_s, id_recorrido, salida_real,
    llegada_real, finalizado_en, avance_m, recorrido_m, retraso_s, desvio_m, velocidad_kmh, actualizado_en"""


# ---------------------------------------------------------------------
# Validación de datos
# ---------------------------------------------------------------------
def _m(texto):
    """Primera letra en mayúscula sin tocar el resto ("el SOAT" -> "El SOAT")."""
    return texto[:1].upper() + texto[1:]


def _texto(d, campo, maximo=80, nombre=None):
    valor = d.get(campo)
    if not isinstance(valor, str) or not valor.strip():
        raise ErrorAPI(400, f"Falta {nombre or campo}.", campo)
    valor = " ".join(valor.split())
    if len(valor) > maximo:
        raise ErrorAPI(400, f"{_m(nombre or campo)} es muy largo (máximo {maximo} caracteres).", campo)
    return valor


def _entero(d, campo, minimo=None, maximo=None, nombre=None):
    valor = d.get(campo)
    if isinstance(valor, str) and re.fullmatch(r"\s*-?\d+\s*", valor):
        valor = int(valor)
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor != int(valor):
        raise ErrorAPI(400, f"{_m(nombre or campo)} debe ser un número entero.", campo)
    valor = int(valor)
    if (minimo is not None and valor < minimo) or (maximo is not None and valor > maximo):
        if minimo is not None and maximo is not None:
            mensaje = f"{_m(nombre or campo)} debe estar entre {minimo:,} y {maximo:,}.".replace(",", ".")
        else:
            mensaje = f"{_m(nombre or campo)} está fuera de rango."
        raise ErrorAPI(400, mensaje, campo)
    return valor


def _opcion(d, campo, opciones, nombre=None):
    valor = d.get(campo)
    if valor not in opciones:
        raise ErrorAPI(400, f"{_m(nombre or campo)} no es válido.", campo)
    return valor


def _patron(d, campo, patron, mensaje):
    valor = d.get(campo)
    valor = re.sub(r"[\s.]", "", valor) if isinstance(valor, str) else ""
    if not patron.match(valor):
        raise ErrorAPI(400, mensaje, campo)
    return valor


def _fecha(d, campo, nombre):
    try:
        return date.fromisoformat(str(d.get(campo, "")))
    except ValueError:
        raise ErrorAPI(400, f"{_m(nombre)} debe ser una fecha AAAA-MM-DD.", campo) from None


def _fecha_hora(d, campo, nombre):
    try:
        valor = datetime.fromisoformat(str(d.get(campo, "")))
    except ValueError:
        raise ErrorAPI(400, f"{_m(nombre)} debe ser una fecha y hora AAAA-MM-DDTHH:MM.", campo) from None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=BOGOTA)           # lo que escribe la página es hora de Colombia
    return valor


def _numero_en(valor, minimo, maximo):
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not (minimo <= valor <= maximo):
        return None
    return float(valor)


def _lat_lon(d, campo="ubicación"):
    lat, lon = _numero_en(d.get("lat"), -90, 90), _numero_en(d.get("lon"), -180, 180)
    if lat is None or lon is None:
        raise ErrorAPI(400, f"Falta la {campo} (lat y lon).", "lat")
    return lat, lon


def _punto(d, campo, nombre):
    valor = d.get(campo)
    if not isinstance(valor, dict):
        raise ErrorAPI(400, f"Falta el punto de {nombre} en el mapa.", campo)
    lat, lon = _numero_en(valor.get("lat"), -90, 90), _numero_en(valor.get("lon"), -180, 180)
    if lat is None or lon is None:
        raise ErrorAPI(400, f"El punto de {nombre} no es válido.", campo)
    return lat, lon


def _placa(d):
    valor = d.get("placa")
    valor = re.sub(r"[\s-]", "", valor).upper() if isinstance(valor, str) else ""
    if not PATRON_PLACA.match(valor):
        raise ErrorAPI(400, "La placa debe tener 3 letras y 3 números (por ejemplo TKL482).", "placa")
    return valor


def _booleano(d, campo):
    valor = d.get(campo)
    if not isinstance(valor, bool):
        raise ErrorAPI(400, f"{campo} debe ser true o false.", campo)
    return valor


# ---------------------------------------------------------------------
# Contraseñas, sesiones y bloqueo por intentos fallidos
# ---------------------------------------------------------------------
def _hash_clave(clave, sal=None, iteraciones=ITERACIONES_CLAVE):
    """Formato guardado: pbkdf2_sha256$<iteraciones>$<sal base64>$<hash base64>."""
    sal = sal or secrets.token_bytes(16)
    resumen = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, iteraciones)
    return f"pbkdf2_sha256${iteraciones}${b64encode(sal).decode()}${b64encode(resumen).decode()}"


def _verificar_clave(clave, guardado):
    try:
        algoritmo, iteraciones, sal, resumen = guardado.split("$")
        if algoritmo != "pbkdf2_sha256":
            return False
        calculado = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), b64decode(sal), int(iteraciones))
        return hmac.compare_digest(calculado, b64decode(resumen))
    except (ValueError, AttributeError):
        return False


_HASH_FALSO = _hash_clave("no-existe", b"0" * 16)     # para tardar lo mismo si la placa no existe


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _nuevo_token():
    return secrets.token_urlsafe(32)


_fallos = {}                   # placa -> [intentos fallidos, momento del primero]
_candado_fallos = threading.Lock()


def _revisar_bloqueo(placa):
    with _candado_fallos:
        intentos, desde = _fallos.get(placa, (0, 0))
        if time.monotonic() - desde > BLOQUEO_LOGIN_S:
            _fallos.pop(placa, None)
        elif intentos >= INTENTOS_LOGIN:
            minutos = max(1, round((BLOQUEO_LOGIN_S - (time.monotonic() - desde)) / 60))
            raise ErrorAPI(429, f"Demasiados intentos fallidos. Intente de nuevo en {minutos} min.")


def _contar_fallo(placa):
    with _candado_fallos:
        intentos, desde = _fallos.get(placa, (0, time.monotonic()))
        _fallos[placa] = [intentos + 1, desde]


def _limpiar_fallos(placa):
    with _candado_fallos:
        _fallos.pop(placa, None)


def _camion_autenticado(cur, autorizacion, bloquear=False):
    if not autorizacion or not autorizacion.startswith("Bearer "):
        raise ErrorAPI(401, "Inicie sesión.")
    cur.execute(f"SELECT {COLUMNAS_CAMION} FROM camiones WHERE token_hash = %s AND token_vence > now()"
                + (" FOR UPDATE" if bloquear else ""), (_hash_token(autorizacion[7:].strip()),))
    camion = cur.fetchone()
    if camion is None:
        raise ErrorAPI(401, "La sesión venció o no es válida. Inicie sesión de nuevo.")
    return camion


# ---------------------------------------------------------------------
# Conversión a JSON
# ---------------------------------------------------------------------
def _iso(valor):
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.astimezone(BOGOTA).isoformat(timespec="seconds")
    return valor.isoformat()


def _hoy():
    return datetime.now(BOGOTA).date()


def _documentos_vencidos(camion):
    hoy, vencidos = _hoy(), []
    if camion["soat_vence"] < hoy:
        vencidos.append("el SOAT")
    if camion["tecnomecanica_vence"] < hoy:
        vencidos.append("la revisión técnico-mecánica")
    return vencidos


def _minutos_desde(momento):
    if momento is None:
        return None
    return max(0, round((datetime.now(timezone.utc) - momento).total_seconds() / 60))


def _camion_json(c, privado=False):
    datos = {
        "placa": c["placa"],
        "configuracion": c["configuracion"],
        "configuracion_nombre": CONFIGURACIONES[c["configuracion"]][0],
        "carroceria": c["carroceria"],
        "carroceria_nombre": CARROCERIAS[c["carroceria"]],
        "capacidad_kg": c["capacidad_kg"],
        "marca": c["marca"], "linea": c["linea"], "modelo": c["modelo"], "color": c["color"],
        "estado": c["estado"],
        "ubicacion": None if c["ultima_lat"] is None else {
            "lat": c["ultima_lat"], "lon": c["ultima_lon"],
            "hora": _iso(c["ubicacion_en"]), "hace_min": _minutos_desde(c["ubicacion_en"])},
        "documentos": {"soat_vence": _iso(c["soat_vence"]), "tecnomecanica_vence": _iso(c["tecnomecanica_vence"]),
                       "vigentes": not _documentos_vencidos(c)},
    }
    if privado:
        datos["propietario"] = {"nombre": c["propietario_nombre"], "documento": c["propietario_documento"],
                                "celular": c["propietario_celular"]}
    return datos


def modo_demo():
    return os.path.exists(RUTA_MODO_DEMO)


def umbrales_retraso(duracion_total_s):
    """(aviso_s, alerta_s) para un viaje: el mayor entre el valor fijo y el porcentaje del tiempo planeado."""
    aviso_fijo, alerta_fijo = (AVISO_DEMO_S, ALERTA_DEMO_S) if modo_demo() else (AVISO_RETRASO_S, ALERTA_RETRASO_S)
    return (max(aviso_fijo, AVISO_RETRASO_PCT * duracion_total_s),
            max(alerta_fijo, ALERTA_RETRASO_PCT * duracion_total_s))


def nivel_retraso(retraso_s, duracion_total_s):
    """'a_tiempo' | 'aviso' | 'alerta' según los umbrales de S2 (None si no hay dato)."""
    if retraso_s is None:
        return None
    aviso, alerta = umbrales_retraso(duracion_total_s)
    if retraso_s > alerta:
        return "alerta"
    if retraso_s > aviso:
        return "aviso"
    return "a_tiempo"


def _envio_json(cur, e, con_ruta=False, para_conductor=False):
    salida = e["salida_real"]
    cur.execute("""SELECT orden, tipo, nombre, lat, lon, distancia_m, tiempo_plan_s, hora_real, retraso_s, velocidad_kmh
                   FROM puntos_control WHERE id_envio = %s ORDER BY orden""", (e["id"],))
    puntos = []
    for p in cur.fetchall():
        puntos.append({
            "orden": p["orden"], "tipo": p["tipo"], "nombre": p["nombre"], "lat": p["lat"], "lon": p["lon"],
            "distancia_m": p["distancia_m"], "tiempo_plan_s": p["tiempo_plan_s"],
            "hora_plan": _iso(salida + timedelta(seconds=p["tiempo_plan_s"])) if salida else None,
            "hora_real": _iso(p["hora_real"]), "retraso_s": p["retraso_s"],
            "nivel": nivel_retraso(p["retraso_s"], e["duracion_s"]) if p["hora_real"] else "pendiente",
            "velocidad_kmh": p["velocidad_kmh"],
        })
    cur.execute("""SELECT id, tipo, inicio, fin, valor_max, lat, lon, vista_en FROM alertas
                   WHERE id_envio = %s ORDER BY inicio DESC""", (e["id"],))
    alertas = [{"id": a["id"], "tipo": a["tipo"], "inicio": _iso(a["inicio"]), "fin": _iso(a["fin"]),
                "activa": a["fin"] is None, "valor_max": a["valor_max"], "lat": a["lat"], "lon": a["lon"],
                "vista": a["vista_en"] is not None} for a in cur.fetchall()]
    conductor = None
    if e["id_recorrido"]:
        cur.execute("""SELECT c.cedula, c.nombre_completo FROM planillas p JOIN conductores c ON c.cedula = p.cedula
                       WHERE p.id_recorrido = %s""", (e["id_recorrido"],))
        fila = cur.fetchone()
        if fila:
            conductor = {"cedula": fila["cedula"], "nombre": fila["nombre_completo"]}

    llegada_estimada = None
    if salida and e["estado"] in ("en_ruta", "en_destino"):
        llegada_estimada = _iso(salida + timedelta(seconds=e["duracion_s"] + (e["retraso_s"] or 0)))
    datos = {
        "id": e["id"], "codigo": f"ENV-{e['id']:04d}", "estado": e["estado"], "creado_en": _iso(e["creado_en"]),
        "cliente": {"nombre": e["cliente_nombre"], "nit": e["cliente_nit"]},
        "origen": {"direccion": e["origen_direccion"], "lat": e["origen_lat"], "lon": e["origen_lon"],
                   "contacto": e["cargue_contacto"], "celular": e["cargue_celular"]},
        "destino": {"direccion": e["destino_direccion"], "lat": e["destino_lat"], "lon": e["destino_lon"],
                    "destinatario": e["destinatario_nombre"], "celular": e["destinatario_celular"]},
        "carga": {"tipo": e["tipo_carga"], "tipo_nombre": TIPOS_CARGA[e["tipo_carga"]][0],
                  "descripcion": e["descripcion"], "empaque": e["empaque"], "empaque_nombre": EMPAQUES[e["empaque"]],
                  "cantidad": e["cantidad"], "peso_kg": e["peso_kg"]},
        "cargue_programado": _iso(e["cargue_programado"]),
        "placa": e["placa"],
        "plan": {"fuente": e["ruta_fuente"], "distancia_m": e["distancia_m"], "duracion_s": e["duracion_s"]},
        "progreso": {
            "id_recorrido": e["id_recorrido"], "salida_real": _iso(salida), "llegada_real": _iso(e["llegada_real"]),
            "finalizado_en": _iso(e["finalizado_en"]), "avance_m": round(e["avance_m"]),
            "porcentaje": min(100, round(100 * e["avance_m"] / e["distancia_m"])) if e["distancia_m"] else 0,
            "recorrido_m": round(e["recorrido_m"]), "retraso_s": e["retraso_s"],
            "nivel_retraso": nivel_retraso(e["retraso_s"], e["duracion_s"]),
            "desvio_m": None if e["desvio_m"] is None else round(e["desvio_m"]),
            "velocidad_kmh": None if e["velocidad_kmh"] is None else round(e["velocidad_kmh"]),
            "llegada_estimada": llegada_estimada, "actualizado_en": _iso(e["actualizado_en"]),
        },
        "puntos_control": puntos,
        "alertas": alertas,
        "conductor": conductor,
        # P3-S3: eventos de la carga (frenadas, huecos, curvas, luz), conteo y estado de la carga.
        # None si la base todavía no tiene p3s3.sql.
        "sensores": eventos_carga.datos_de_carga(cur, e["id"]),
    }
    if not para_conductor:
        cur.execute(f"SELECT {COLUMNAS_CAMION} FROM camiones WHERE placa = %s", (e["placa"],))
        camion = cur.fetchone()
        datos["camion"] = _camion_json(camion) if camion else None
    if con_ruta:
        cur.execute("SELECT ruta FROM envios WHERE id = %s", (e["id"],))
        datos["ruta"] = cur.fetchone()["ruta"]
    return datos


# ---------------------------------------------------------------------
# APP: camiones
# ---------------------------------------------------------------------
def registrar_camion(p):
    d = p.datos
    placa = _placa(d)
    clave = d.get("clave")
    if not isinstance(clave, str) or len(clave) < 8:
        raise ErrorAPI(400, "La contraseña debe tener al menos 8 caracteres.", "clave")
    configuracion = _opcion(d, "configuracion", CONFIGURACIONES, "la configuración")
    carroceria = _opcion(d, "carroceria", CARROCERIAS, "la carrocería")
    capacidad = _entero(d, "capacidad_kg", 1, 60000, "la capacidad")
    pbv = CONFIGURACIONES[configuracion][1]
    if capacidad >= pbv:
        raise ErrorAPI(400, f"La capacidad debe ser menor que {pbv:,} kg, el peso bruto máximo de un "
                            f"{configuracion} (Res. 4100 de 2004).".replace(",", "."), "capacidad_kg")
    valores = {
        "placa": placa, "clave_hash": _hash_clave(clave), "configuracion": configuracion, "carroceria": carroceria,
        "capacidad_kg": capacidad,
        "marca": _texto(d, "marca", 40, "la marca"), "linea": _texto(d, "linea", 40, "la línea"),
        "modelo": _entero(d, "modelo", 1970, _hoy().year + 1, "el modelo"),
        "color": _texto(d, "color", 40, "el color"),
        "propietario_nombre": _texto(d, "propietario_nombre", 80, "el nombre del dueño"),
        "propietario_documento": _patron(d, "propietario_documento", PATRON_DOCUMENTO,
                                         "El documento del dueño debe tener solo números (cédula o NIT)."),
        "propietario_celular": _patron(d, "propietario_celular", PATRON_CELULAR,
                                       "El celular debe tener 10 dígitos y empezar por 3."),
        "soat_vence": _fecha(d, "soat_vence", "el vencimiento del SOAT"),
        "tecnomecanica_vence": _fecha(d, "tecnomecanica_vence", "el vencimiento de la técnico-mecánica"),
    }
    token = _nuevo_token()
    valores["token_hash"] = _hash_token(token)
    columnas = ", ".join(valores)
    marcas = ", ".join(f"%({k})s" for k in valores)
    with _bd() as cur:
        try:
            cur.execute(f"""INSERT INTO camiones ({columnas}, token_vence)
                            VALUES ({marcas}, now() + interval '{DIAS_SESION} days')
                            RETURNING {COLUMNAS_CAMION}""", valores)
        except psycopg2.errors.UniqueViolation:
            raise ErrorAPI(409, "Esa placa ya está registrada. Inicie sesión.", "placa") from None
        camion = cur.fetchone()
    return 201, {"token": token, "camion": _camion_json(camion, privado=True)}


def iniciar_sesion(p):
    placa = _placa(p.datos)
    clave = p.datos.get("clave") if isinstance(p.datos.get("clave"), str) else ""
    _revisar_bloqueo(placa)
    with _bd() as cur:
        cur.execute("SELECT clave_hash FROM camiones WHERE placa = %s", (placa,))
        fila = cur.fetchone()
        valida = _verificar_clave(clave, fila["clave_hash"] if fila else _HASH_FALSO) and fila is not None
        if not valida:
            _contar_fallo(placa)
            raise ErrorAPI(401, "Placa o contraseña incorrecta.")
        token = _nuevo_token()
        cur.execute(f"""UPDATE camiones SET token_hash = %s, token_vence = now() + interval '{DIAS_SESION} days'
                        WHERE placa = %s RETURNING {COLUMNAS_CAMION}""", (_hash_token(token), placa))
        camion = cur.fetchone()
    _limpiar_fallos(placa)
    return 200, {"token": token, "camion": _camion_json(camion, privado=True)}


def cerrar_sesion(p):
    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion)
        cur.execute("UPDATE camiones SET token_hash = NULL, token_vence = NULL WHERE placa = %s", (camion["placa"],))
    return 200, {"ok": True}


def datos_del_camion(p):
    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion)
    return 200, {"camion": _camion_json(camion, privado=True)}


def cambiar_disponibilidad(p):
    disponible = _booleano(p.datos, "disponible")
    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion, bloquear=True)
        if camion["estado"] in ("asignado", "en_ruta"):
            raise ErrorAPI(409, "Tiene un viaje asignado: no puede cambiar su disponibilidad.")
        if disponible:
            lat, lon = _lat_lon(p.datos)
            vencidos = _documentos_vencidos(camion)
            if vencidos:
                raise ErrorAPI(409, "No puede quedar disponible: " + " y ".join(vencidos)
                               + (" está vencido." if len(vencidos) == 1 else " están vencidos."))
            cur.execute(f"""UPDATE camiones SET estado = 'disponible', ultima_lat = %s, ultima_lon = %s,
                            ubicacion_en = now() WHERE placa = %s RETURNING {COLUMNAS_CAMION}""",
                        (lat, lon, camion["placa"]))
        else:
            cur.execute(f"""UPDATE camiones SET estado = 'no_disponible' WHERE placa = %s
                            RETURNING {COLUMNAS_CAMION}""", (camion["placa"],))
        camion = cur.fetchone()
    return 200, {"camion": _camion_json(camion, privado=True)}


def actualizar_ubicacion(p):
    lat, lon = _lat_lon(p.datos)
    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion)
        cur.execute("UPDATE camiones SET ultima_lat = %s, ultima_lon = %s, ubicacion_en = now() WHERE placa = %s",
                    (lat, lon, camion["placa"]))
    return 200, {"ok": True, "estado": camion["estado"]}


def _envio_abierto_de(cur, placa, estados=ESTADOS_ABIERTOS, bloquear=False):
    cur.execute(f"""SELECT {COLUMNAS_ENVIO} FROM envios WHERE placa = %s AND estado = ANY(%s)
                    ORDER BY id DESC LIMIT 1""" + (" FOR UPDATE" if bloquear else ""), (placa, list(estados)))
    return cur.fetchone()


def viaje_actual(p):
    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion)
        envio = _envio_abierto_de(cur, camion["placa"])
        viaje = _envio_json(cur, envio, con_ruta=p.parametros.get("ruta") == "1", para_conductor=True) if envio else None
    return 200, {"viaje": viaje, "camion": _camion_json(camion, privado=True)}


def finalizar_viaje(p):
    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion, bloquear=True)
        envio = _envio_abierto_de(cur, camion["placa"], ("en_ruta", "en_destino"), bloquear=True)
        if envio is None:
            raise ErrorAPI(409, "No tiene un viaje en curso para finalizar.")
        cur.execute("UPDATE envios SET estado = 'finalizado', finalizado_en = now() WHERE id = %s", (envio["id"],))
        # Se cierran TODAS las alertas activas del envío: retraso, desvío y (P3-S3) carga abierta.
        cur.execute("UPDATE alertas SET fin = now() WHERE id_envio = %s AND fin IS NULL", (envio["id"],))
        nuevo = "no_disponible" if _documentos_vencidos(camion) else "disponible"
        cur.execute(f"UPDATE camiones SET estado = %s WHERE placa = %s RETURNING {COLUMNAS_CAMION}",
                    (nuevo, camion["placa"]))
        camion = cur.fetchone()
    return 200, {"viaje": None, "camion": _camion_json(camion, privado=True)}


def eventos_del_viaje(p):
    """
    P3-S3. Cuerpo:
      {"id_envio": 7,                                  (opcional; si no viene, el viaje abierto)
       "eventos": [{"uid", "tipo", "valor", "lat", "lon", "momento"}, ...],
       "latido": {"luz": "abierta"|"cerrada", "nivel": 35, "momento": ISO},   (opcional)
       "posicion": {"lat", "lon"}}                     (opcional: último GPS, para eventos sin ubicación)
    Responde qué uid se guardaron, cuáles ya estaban (reintento) y cuáles se rechazaron.
    """
    d = p.datos
    lista = d.get("eventos", [])
    if not isinstance(lista, list):
        raise ErrorAPI(400, "eventos debe ser una lista.", "eventos")
    if len(lista) > eventos_carga.MAX_EVENTOS_POR_LOTE:
        raise ErrorAPI(413, f"Máximo {eventos_carga.MAX_EVENTOS_POR_LOTE} eventos por envío.", "eventos")
    ahora = datetime.now(timezone.utc)
    validos, rechazados = [], []
    for item in lista:
        try:
            validos.append(eventos_carga.leer_evento(item, ahora))
        except eventos_carga.EventoInvalido as error:
            uid = item.get("uid") if isinstance(item, dict) and isinstance(item.get("uid"), str) else None
            rechazados.append({"uid": uid, "error": str(error)})
    latido = eventos_carga.leer_latido(d.get("latido"), ahora) if d.get("latido") is not None else None
    if not lista and latido is None:
        raise ErrorAPI(400, "No llegó ningún evento ni latido.", "eventos")
    posicion = None
    if isinstance(d.get("posicion"), dict):
        lat, lon = _numero_en(d["posicion"].get("lat"), -90, 90), _numero_en(d["posicion"].get("lon"), -180, 180)
        if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0):
            posicion = (lat, lon)
    id_envio = d.get("id_envio")
    if id_envio is not None and (isinstance(id_envio, bool) or not isinstance(id_envio, int) or id_envio <= 0):
        raise ErrorAPI(400, "id_envio debe ser un número entero.", "id_envio")

    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion)
        if id_envio is not None:
            # Puede ser un viaje ya terminado: la app manda lo que tenía en cola cuando vuelve la señal.
            cur.execute(f"SELECT {COLUMNAS_ENVIO} FROM envios WHERE id = %s AND placa = %s FOR UPDATE",
                        (id_envio, camion["placa"]))
            envio = cur.fetchone()
            if envio is None:
                raise ErrorAPI(404, "Ese envío no existe o no es de este camión.", "id_envio")
        else:
            envio = _envio_abierto_de(cur, camion["placa"], bloquear=True)
            if envio is None:
                raise ErrorAPI(409, "No tiene un viaje asignado ni en curso.")
        ruta = None
        if any(ev["lat"] is not None for ev in validos) or (posicion and validos):
            cur.execute("SELECT ruta FROM envios WHERE id = %s", (envio["id"],))
            ruta = cur.fetchone()["ruta"]
        try:
            guardados, repetidos, avisos = eventos_carga.registrar(cur, envio, validos, latido, ruta, posicion)
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn):
            raise ErrorAPI(503, "La base de datos todavía no tiene P3-S3 (falta ejecutar sql/p3s3.sql).") from None
        sensores = eventos_carga.datos_de_carga(cur, envio["id"])
        cur.execute("SELECT 1 FROM alertas WHERE id_envio = %s AND tipo = 'carga_abierta' AND fin IS NULL", (envio["id"],))
        carga_abierta = cur.fetchone() is not None
    for aviso in avisos:
        print(f"[ENV-{envio['id']:04d}] {aviso}", flush=True)
    return 200, {"id_envio": envio["id"], "estado": envio["estado"], "guardados": guardados, "repetidos": repetidos,
                 "rechazados": rechazados, "alerta_carga_abierta": carga_abierta, "sensores": sensores}


def identificar_conductor(p):
    """
    P3-S3 (esquema híbrido). Cuerpo: {"rostros": ["<jpg base64>", ...]} — los recortes que el
    ESP32-CAM de la cabina mandó al celular. Responde reconocido (cédula y nombre) o
    no_reconocido (ver reconocimiento_facial.py). Error 429 si el camión debe esperar.
    """
    try:
        jpgs = reconocimiento_facial.leer_rostros(p.datos)
    except reconocimiento_facial.DatoInvalido as error:
        raise ErrorAPI(400, str(error), "rostros") from None
    with _bd() as cur:
        camion = _camion_autenticado(cur, p.autorizacion)
        try:
            resultado = reconocimiento_facial.identificar(cur, camion["placa"], jpgs)
        except reconocimiento_facial.EnEspera as error:
            raise ErrorAPI(429, str(error)) from None
        except reconocimiento_facial.NoDisponible as error:
            raise ErrorAPI(503, str(error)) from None
        except psycopg2.errors.UndefinedTable:
            raise ErrorAPI(503, "La base de datos todavía no tiene las huellas (falta ejecutar sql/p3s3_rostros.sql).") from None
    resultado.pop("similitudes", None)
    return 200, resultado


def foto_de_planilla(p):
    """
    P3-S3. Foto con la que el ESP32-CAM verificó al conductor de un recorrido (la que reconoció
    el servidor). La muestra la planilla de la página. 404 si no hay o ya pasaron 7 días.
    """
    with _bd() as cur:
        try:
            foto = reconocimiento_facial.foto_de_planilla(cur, p.args[0])
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn):
            foto = None
    if foto is None:
        raise ErrorAPI(404, "Este recorrido no tiene foto del conductor (o ya pasaron 7 días).")
    return 200, foto


# ---------------------------------------------------------------------
# PÁGINA: catálogo, flota y envíos
# ---------------------------------------------------------------------
def catalogo(p):
    return 200, {
        "configuraciones": [{"codigo": k, "nombre": n, "pbv_kg": pbv} for k, (n, pbv) in CONFIGURACIONES.items()],
        "carrocerias": [{"codigo": k, "nombre": n} for k, n in CARROCERIAS.items()],
        "tipos_carga": [{"codigo": k, "nombre": n, "carrocerias": list(c)} for k, (n, c) in TIPOS_CARGA.items()],
        "empaques": [{"codigo": k, "nombre": n} for k, n in EMPAQUES.items()],
        "umbrales": {"modo_demo": modo_demo(),
                     "aviso_retraso_s": AVISO_DEMO_S if modo_demo() else AVISO_RETRASO_S,
                     "aviso_retraso_pct": AVISO_RETRASO_PCT,
                     "alerta_retraso_s": ALERTA_DEMO_S if modo_demo() else ALERTA_RETRASO_S,
                     "alerta_retraso_pct": ALERTA_RETRASO_PCT},
    }


def flota(p):
    with _bd() as cur:
        cur.execute(f"""SELECT {COLUMNAS_CAMION} FROM camiones
                        ORDER BY CASE estado WHEN 'en_ruta' THEN 0 WHEN 'asignado' THEN 1
                                             WHEN 'disponible' THEN 2 ELSE 3 END, placa""")
        camiones = [_camion_json(c) for c in cur.fetchall()]
    return 200, {"camiones": camiones}


def _motivo_no_apto(c, peso, tipo):
    """Por qué un camión no sirve para este envío (None si sí sirve)."""
    if c["estado"] != "disponible":
        return "ya no está disponible"
    if c["ubicacion_en"] is None or _minutos_desde(c["ubicacion_en"]) > UBICACION_RECIENTE_MIN:
        return f"no ha reportado su ubicación en los últimos {UBICACION_RECIENTE_MIN} min"
    if c["capacidad_kg"] < peso:
        return "no tiene capacidad para ese peso"
    if c["carroceria"] not in TIPOS_CARGA[tipo][1]:
        return "su carrocería no sirve para ese tipo de carga"
    if _documentos_vencidos(c):
        return "tiene documentos vencidos"
    return None


def _candidatos(cur, origen, peso, tipo):
    cur.execute(f"""SELECT {COLUMNAS_CAMION} FROM camiones WHERE estado = 'disponible'
                    AND ubicacion_en > now() - interval '{UBICACION_RECIENTE_MIN} minutes'""")
    disponibles = cur.fetchall()
    con_capacidad = [c for c in disponibles if c["capacidad_kg"] >= peso]
    con_carroceria = [c for c in con_capacidad if c["carroceria"] in TIPOS_CARGA[tipo][1]]
    aptos = [c for c in con_carroceria if not _documentos_vencidos(c)]
    for c in aptos:
        c["_dist"] = rutas.distancia(origen[0], origen[1], c["ultima_lat"], c["ultima_lon"])
    aptos.sort(key=lambda c: c["_dist"])
    if aptos:
        # Entre los que están casi igual de cerca (2 km), primero el más pequeño que sirva.
        cercanos = [c for c in aptos if c["_dist"] <= aptos[0]["_dist"] + EMPATE_M]
        cercanos.sort(key=lambda c: (c["capacidad_kg"], c["_dist"]))
        aptos = cercanos + [c for c in aptos if c not in cercanos]
    candidatos = []
    for i, c in enumerate(aptos[:MAX_CANDIDATOS]):
        datos = _camion_json(c)
        datos["distancia_origen_m"] = round(c["_dist"])
        datos["recomendado"] = i == 0
        candidatos.append(datos)
    resumen = {"disponibles": len(disponibles), "con_capacidad": len(con_capacidad),
               "con_carroceria": len(con_carroceria), "aptos": len(aptos)}
    return candidatos, resumen


def _calcular_ruta(origen, destino):
    try:
        return rutas.calcular_ruta(origen, destino)
    except rutas.ErrorRuta as e:
        raise ErrorAPI(502, str(e)) from None


def cotizar_envio(p):
    d = p.datos
    origen, destino = _punto(d, "origen", "origen"), _punto(d, "destino", "destino")
    peso = _entero(d, "peso_kg", 1, 60000, "el peso")
    tipo = _opcion(d, "tipo_carga", TIPOS_CARGA, "el tipo de carga")
    ruta = _calcular_ruta(origen, destino)
    puntos = rutas.puntos_de_control(ruta, "Origen", "Destino")
    with _bd() as cur:
        candidatos, resumen = _candidatos(cur, origen, peso, tipo)
    return 200, {
        "ruta": {**rutas.ruta_para_bd(ruta), "fuente": ruta["fuente"], "distancia_m": ruta["distancia_m"],
                 "duracion_s": ruta["duracion_s"], "aviso": ruta.get("aviso")},
        "puntos_control": puntos,
        "candidatos": candidatos,
        "flota": resumen,
    }


def crear_envio(p):
    d = p.datos
    tipo = _opcion(d, "tipo_carga", TIPOS_CARGA, "el tipo de carga")
    peso = _entero(d, "peso_kg", 1, 60000, "el peso")
    origen, destino = _punto(d, "origen", "origen"), _punto(d, "destino", "destino")
    cargue = _fecha_hora(d, "cargue_programado", "la fecha de cargue")
    ahora = datetime.now(BOGOTA)
    if not (ahora - timedelta(days=1) <= cargue <= ahora + timedelta(days=120)):
        raise ErrorAPI(400, "La fecha de cargue debe estar entre ayer y los próximos 4 meses.", "cargue_programado")
    valores = {
        "cliente_nombre": _texto(d, "cliente_nombre", 80, "el nombre del cliente"),
        "cliente_nit": _patron(d, "cliente_nit", PATRON_DOCUMENTO, "El NIT debe tener solo números (puede llevar -dígito)."),
        "origen_direccion": _texto(d, "origen_direccion", 120, "la dirección de origen"),
        "origen_lat": origen[0], "origen_lon": origen[1],
        "cargue_contacto": _texto(d, "cargue_contacto", 80, "el contacto de cargue"),
        "cargue_celular": _patron(d, "cargue_celular", PATRON_CELULAR, "El celular de cargue debe tener 10 dígitos y empezar por 3."),
        "destino_direccion": _texto(d, "destino_direccion", 120, "la dirección de destino"),
        "destino_lat": destino[0], "destino_lon": destino[1],
        "destinatario_nombre": _texto(d, "destinatario_nombre", 80, "el nombre del destinatario"),
        "destinatario_celular": _patron(d, "destinatario_celular", PATRON_CELULAR,
                                        "El celular del destinatario debe tener 10 dígitos y empezar por 3."),
        "tipo_carga": tipo,
        "descripcion": _texto(d, "descripcion", 200, "la descripción de la carga"),
        "empaque": _opcion(d, "empaque", EMPAQUES, "el empaque"),
        "cantidad": _entero(d, "cantidad", 1, 1_000_000, "la cantidad"),
        "peso_kg": peso,
        "valor_declarado": 0,          # ya no se pide (la columna sigue en la tabla y queda en 0)
        "cargue_programado": cargue,
        "placa": _placa(d),
    }
    # La ruta se pide ANTES de abrir la transacción (puede tardar unos segundos).
    ruta = _calcular_ruta(origen, destino)
    puntos = rutas.puntos_de_control(ruta, valores["origen_direccion"], valores["destino_direccion"])
    valores.update({"ruta_fuente": ruta["fuente"], "ruta": json.dumps(rutas.ruta_para_bd(ruta)),
                    "distancia_m": ruta["distancia_m"], "duracion_s": ruta["duracion_s"]})

    with _bd() as cur:
        # FOR UPDATE: si dos personas asignan el mismo camión a la vez, la segunda espera y luego ve que ya no está libre.
        cur.execute(f"SELECT {COLUMNAS_CAMION} FROM camiones WHERE placa = %s FOR UPDATE", (valores["placa"],))
        camion = cur.fetchone()
        if camion is None:
            raise ErrorAPI(409, "Ese camión no existe.", "placa")
        motivo = _motivo_no_apto(camion, peso, tipo)
        if motivo:
            raise ErrorAPI(409, f"No se puede asignar {camion['placa']}: {motivo}. Calcule de nuevo.", "placa")
        columnas = ", ".join(valores)
        marcas = ", ".join(f"%({k})s" for k in valores)
        cur.execute(f"INSERT INTO envios ({columnas}) VALUES ({marcas}) RETURNING id", valores)
        id_envio = cur.fetchone()["id"]
        psycopg2.extras.execute_values(cur, """INSERT INTO puntos_control
            (id_envio, orden, tipo, nombre, lat, lon, distancia_m, tiempo_plan_s) VALUES %s""",
            [(id_envio, x["orden"], x["tipo"], x["nombre"], x["lat"], x["lon"], x["distancia_m"],
              x["tiempo_plan_s"]) for x in puntos])
        cur.execute("UPDATE camiones SET estado = 'asignado' WHERE placa = %s", (camion["placa"],))
        cur.execute(f"SELECT {COLUMNAS_ENVIO} FROM envios WHERE id = %s", (id_envio,))
        envio = _envio_json(cur, cur.fetchone())
    if ruta.get("aviso"):
        envio["aviso_ruta"] = ruta["aviso"]
    return 201, {"envio": envio}


def listar_envios(p):
    filtro = p.parametros.get("estado", "activos")
    condiciones = {"activos": "estado IN ('asignado', 'en_ruta', 'en_destino')",
                   "finalizados": "estado IN ('finalizado', 'cancelado')", "todos": "TRUE"}
    if filtro not in condiciones:
        raise ErrorAPI(400, "estado debe ser activos, finalizados o todos.", "estado")
    try:
        limite = max(1, min(200, int(p.parametros.get("limite", 50))))
    except ValueError:
        limite = 50
    with _bd() as cur:
        cur.execute(f"""SELECT {COLUMNAS_ENVIO},
                               (SELECT count(*) FROM alertas a WHERE a.id_envio = e.id AND a.fin IS NULL) AS alertas_activas
                        FROM envios e WHERE {condiciones[filtro]} ORDER BY id DESC LIMIT %s""", (limite,))
        envios = [{
            "id": e["id"], "codigo": f"ENV-{e['id']:04d}", "estado": e["estado"], "creado_en": _iso(e["creado_en"]),
            "cliente": e["cliente_nombre"], "origen": e["origen_direccion"], "destino": e["destino_direccion"],
            "tipo_carga": e["tipo_carga"], "peso_kg": e["peso_kg"], "placa": e["placa"],
            "cargue_programado": _iso(e["cargue_programado"]),
            "distancia_m": e["distancia_m"], "duracion_s": e["duracion_s"],
            "porcentaje": min(100, round(100 * e["avance_m"] / e["distancia_m"])) if e["distancia_m"] else 0,
            "retraso_s": e["retraso_s"], "nivel_retraso": nivel_retraso(e["retraso_s"], e["duracion_s"]),
            "alertas_activas": e["alertas_activas"],
            "salida_real": _iso(e["salida_real"]), "llegada_real": _iso(e["llegada_real"]),
        } for e in cur.fetchall()]
    return 200, {"envios": envios}


def detalle_envio(p):
    with _bd() as cur:
        cur.execute(f"SELECT {COLUMNAS_ENVIO} FROM envios WHERE id = %s", (int(p.args[0]),))
        envio = cur.fetchone()
        if envio is None:
            raise ErrorAPI(404, "Ese envío no existe.")
        return 200, {"envio": _envio_json(cur, envio, con_ruta=p.parametros.get("ruta", "1") != "0")}


def cancelar_envio(p):
    with _bd() as cur:
        cur.execute(f"SELECT {COLUMNAS_ENVIO} FROM envios WHERE id = %s FOR UPDATE", (int(p.args[0]),))
        envio = cur.fetchone()
        if envio is None:
            raise ErrorAPI(404, "Ese envío no existe.")
        if envio["estado"] != "asignado":
            raise ErrorAPI(409, "Solo se puede cancelar un envío que todavía no ha salido.")
        cur.execute("UPDATE envios SET estado = 'cancelado', finalizado_en = now() WHERE id = %s", (envio["id"],))
        cur.execute("""UPDATE camiones SET estado = 'disponible'
                       WHERE placa = %s AND estado = 'asignado'""", (envio["placa"],))
        envio["estado"] = "cancelado"
        envio["finalizado_en"] = datetime.now(timezone.utc)
        return 200, {"envio": _envio_json(cur, envio)}


def marcar_alerta_vista(p):
    with _bd() as cur:
        cur.execute("UPDATE alertas SET vista_en = COALESCE(vista_en, now()) WHERE id = %s RETURNING id",
                    (int(p.args[0]),))
        if cur.fetchone() is None:
            raise ErrorAPI(404, "Esa alerta no existe.")
    return 200, {"ok": True}


# ---------------------------------------------------------------------
# Para /datos (Tiempo Real) e Históricos
# ---------------------------------------------------------------------
def resumen_envio_de_recorrido(id_recorrido):
    """Envío ligado a ese recorrido del celular (sin la línea de la ruta), o None."""
    if not id_recorrido:
        return None
    try:
        with _bd() as cur:
            cur.execute(f"SELECT {COLUMNAS_ENVIO} FROM envios WHERE id_recorrido = %s", (str(id_recorrido),))
            envio = cur.fetchone()
            return _envio_json(cur, envio) if envio else None
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return None


def agregar_envios(lista_recorridos):
    """Le agrega "envio" (resumen o None) a cada recorrido de la lista, con una sola consulta."""
    ids = [str(r["id_recorrido"]) for r in lista_recorridos if r.get("id_recorrido")]
    envios = {}
    if ids:
        try:
            with _bd() as cur:
                cur.execute("""SELECT id, id_recorrido, placa, estado, origen_direccion, destino_direccion
                               FROM envios WHERE id_recorrido = ANY(%s)""", (ids,))
                for e in cur.fetchall():
                    envios[e["id_recorrido"]] = {"id": e["id"], "codigo": f"ENV-{e['id']:04d}", "placa": e["placa"],
                                                 "estado": e["estado"], "origen": e["origen_direccion"],
                                                 "destino": e["destino_direccion"]}
        except Exception:
            traceback.print_exc(file=sys.stderr)
    for r in lista_recorridos:
        r["envio"] = envios.get(str(r.get("id_recorrido")))


# ---------------------------------------------------------------------
# Tabla de rutas
# ---------------------------------------------------------------------
_RUTAS = [
    ("POST", r"/api/camiones/registro", registrar_camion),
    ("POST", r"/api/camiones/login", iniciar_sesion),
    ("POST", r"/api/camiones/logout", cerrar_sesion),
    ("GET",  r"/api/camiones/yo", datos_del_camion),
    ("POST", r"/api/camiones/disponibilidad", cambiar_disponibilidad),
    ("POST", r"/api/camiones/ubicacion", actualizar_ubicacion),
    ("GET",  r"/api/camiones/viaje", viaje_actual),
    ("POST", r"/api/camiones/viaje/finalizar", finalizar_viaje),
    ("POST", r"/api/camiones/viaje/eventos", eventos_del_viaje),      # P3-S3
    ("POST", r"/api/camiones/conductor/identificar", identificar_conductor),   # P3-S3
    ("GET",  r"/api/camiones", flota),
    ("GET",  r"/api/catalogo", catalogo),
    ("POST", r"/api/envios/cotizar", cotizar_envio),
    ("POST", r"/api/envios", crear_envio),
    ("GET",  r"/api/envios", listar_envios),
    ("GET",  r"/api/envios/(\d{1,9})", detalle_envio),
    ("POST", r"/api/envios/(\d{1,9})/cancelar", cancelar_envio),
    ("POST", r"/api/alertas/(\d{1,9})/vista", marcar_alerta_vista),
    ("GET",  r"/api/planillas/(\d{1,20})/foto", foto_de_planilla),          # P3-S3
]


def _buscar(metodo, ruta):
    ruta = ruta.rstrip("/") or "/"
    otro_metodo = False
    for m, patron, funcion in _RUTAS:
        coincide = re.fullmatch(patron, ruta)
        if coincide:
            if m == metodo:
                return funcion, coincide.groups()
            otro_metodo = True
    if otro_metodo:
        raise ErrorAPI(405, f"Esa ruta no acepta {metodo}.")
    raise ErrorAPI(404, "Ruta no encontrada.")
