"""
monitoreo.py — P3-S2, paso 4: vigila cada viaje con los paquetes del celular.

listener.py lo llama con cada paquete que trae el campo ENVIO:
    monitoreo.procesar(id_envio, id_recorrido, latitud, longitud, velocidad)

Con cada paquete:
  1. El primero del viaje lo enlaza: el envío pasa a "en_ruta", se guarda la
     hora real de salida y el recorrido del celular.
  2. Suma los km reales recorridos (sin el temblor del GPS cuando está quieto
     ni los saltos imposibles).
  3. Lleva la posición a la ruta planeada: cuánto ha avanzado y a cuántos
     metros está de ella (desvío).
  4. Marca los puntos de control que pasó, con la hora a la que los pasó
     (interpolada entre dos paquetes) y su retraso contra lo planeado.
  5. Calcula el retraso en vivo: tiempo transcurrido menos el tiempo planeado
     para el avance que lleva.
  6. Abre y cierra alertas:
       desvío : más de 120 m de la ruta en 3 paquetes seguidos (15 s);
                se cierra al volver a menos de 60 m en 2 paquetes seguidos.
       retraso: nivel "alerta" sostenido 1 min (15 s en modo demostración);
                se cierra cuando baja de ese nivel por el mismo tiempo.
  7. Detecta la llegada (al entrar a 100 m del destino): el envío pasa a "en_destino".

procesar() nunca lanza excepciones: si algo falla, lo imprime y el listener
sigue. Va en su propia transacción, después de guardar la posición.
"""

import re
import sys
import traceback
from datetime import datetime, timedelta, timezone

import psycopg2.extras

import rutas
from api_envios import modo_demo, nivel_retraso
from basedatos import obtener_conexion

# ---------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------
CORREDOR_M = 120            # más lejos que esto de la ruta = fuera de la ruta
VUELTA_M = 60               # más cerca que esto = de vuelta en la ruta
PAQUETES_DESVIO = 3         # paquetes seguidos fuera para abrir la alerta (15 s)
PAQUETES_VUELTA = 2         # paquetes seguidos dentro para cerrarla
SOSTENER_RETRASO_S = 60     # el retraso debe estar en "alerta" este tiempo para avisar
SOSTENER_RETRASO_DEMO_S = 15
TEMBLOR_M = 10              # movimientos menores no suman km (GPS quieto)
VEL_MAXIMA_MS = 150 / 3.6   # más rápido que esto es un salto del GPS
ATRAS_M, ADELANTE_M = 500, 3000     # parte de la ruta donde se busca primero
TOLERANCIA_PUNTO_M = 25     # un punto de control se da por pasado unos metros antes
LLEGADA_M = 100             # a menos de esto del destino (o de terminar la ruta) = llegó
VEL_MAXIMA_KMH = 250
MAX_VIAJES_EN_MEMORIA = 500

PATRON_ENVIO = re.compile(r"^[0-9]{1,9}$")


# ---------------------------------------------------------------------
# Campos opcionales del mensaje UDP (se buscan por su nombre, como CONDUCTOR)
# ---------------------------------------------------------------------
def _campo(texto, nombre):
    for parte in texto.strip().split(","):
        parte = parte.strip()
        if parte.startswith(nombre + ":"):
            return parte.split(":", 1)[1].strip()
    return None


def extraer_envio(texto):
    """'...,ENVIO:12' -> 12. None si no viene o no es un número."""
    valor = _campo(texto, "ENVIO")
    return int(valor) if valor and PATRON_ENVIO.match(valor) else None


def extraer_velocidad(texto):
    """'...,VEL:43.2' -> 43.2 (km/h, la que mide el GPS). None si no viene o no tiene sentido."""
    try:
        valor = float(_campo(texto, "VEL"))
    except (TypeError, ValueError):
        return None
    return valor if 0 <= valor <= VEL_MAXIMA_KMH else None


# ---------------------------------------------------------------------
# Lo que se recuerda de cada viaje entre paquete y paquete
# ---------------------------------------------------------------------
class _Viaje:
    def __init__(self, envio, ruta, puntos, activas):
        self.id = envio["id"]
        self.codigo = f"ENV-{envio['id']:04d}"
        self.ruta = ruta
        self.distancia_m = envio["distancia_m"]
        self.duracion_s = envio["duracion_s"]
        self.puntos = puntos
        self.avance_m = envio["avance_m"] or 0.0
        self.recorrido_m = envio["recorrido_m"] or 0.0
        self.ultimo = None              # (lat, lon, momento) del último punto que sumó km
        self.previo = None              # (avance, momento) del paquete anterior
        self.fuera = self.dentro = 0
        self.fuera_desde = None
        self.alerta_desvio = "desvio" in activas
        self.alerta_retraso = "retraso" in activas
        self.alto_desde = self.normal_desde = None
        self.aviso_recorrido = False


_viajes = {}
_ignorados = set()          # envíos cerrados o inexistentes ya avisados (para no repetir el aviso en cada paquete)


def _cargar(cur, envio):
    cur.execute("SELECT ruta FROM envios WHERE id = %s", (envio["id"],))
    ruta = cur.fetchone()["ruta"]
    cur.execute("""SELECT orden, tipo, nombre, lat, lon, distancia_m, tiempo_plan_s, hora_real
                   FROM puntos_control WHERE id_envio = %s ORDER BY orden""", (envio["id"],))
    puntos = [dict(p) for p in cur.fetchall()]
    cur.execute("SELECT tipo FROM alertas WHERE id_envio = %s AND fin IS NULL", (envio["id"],))
    activas = {a["tipo"] for a in cur.fetchall()}
    if len(_viajes) > MAX_VIAJES_EN_MEMORIA:
        _viajes.clear()
    viaje = _viajes[envio["id"]] = _Viaje(envio, ruta, puntos, activas)
    return viaje


# ---------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------
def procesar(id_envio, id_recorrido, lat, lon, velocidad_kmh=None, momento=None):
    """Procesa un paquete. Devuelve la lista de eventos (textos) o None si hubo un error."""
    momento = momento or datetime.now(timezone.utc)
    try:
        conexion = obtener_conexion()
        try:
            cur = conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            eventos = _procesar(cur, id_envio, id_recorrido, lat, lon, velocidad_kmh, momento)
            conexion.commit()
        except BaseException:
            conexion.rollback()
            raise
        finally:
            conexion.close()
    except Exception as error:
        print(f"[ERROR] Monitoreo del envío {id_envio}: {error}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _viajes.pop(id_envio, None)         # en el siguiente paquete se recarga desde la base
        return None
    for evento in eventos:
        print(f"[ENV-{id_envio:04d}] {evento}")
    return eventos


def _procesar(cur, id_envio, id_recorrido, lat, lon, velocidad_kmh, momento):
    eventos = []
    cur.execute("""SELECT id, estado, placa, id_recorrido, salida_real, distancia_m, duracion_s, avance_m, recorrido_m
                   FROM envios WHERE id = %s FOR UPDATE""", (id_envio,))
    envio = cur.fetchone()
    if envio is None or envio["estado"] not in ("asignado", "en_ruta", "en_destino"):
        _viajes.pop(id_envio, None)
        if id_envio not in _ignorados:
            if len(_ignorados) > MAX_VIAJES_EN_MEMORIA:
                _ignorados.clear()
            _ignorados.add(id_envio)
            eventos.append("El envío no existe o ya terminó: el paquete solo se guarda como posición.")
        return eventos

    viaje = _viajes.get(id_envio) or _cargar(cur, envio)

    # 1. Primer paquete del viaje: salida -------------------------------------------
    if envio["estado"] == "asignado":
        recorrido = id_recorrido
        if recorrido:
            cur.execute("SELECT 1 FROM envios WHERE id_recorrido = %s AND id <> %s", (recorrido, id_envio))
            if cur.fetchone():
                eventos.append(f"El recorrido {recorrido} ya pertenece a otro envío: no se enlaza.")
                recorrido = None
        cur.execute("""UPDATE envios SET estado = 'en_ruta', salida_real = %s,
                       id_recorrido = COALESCE(id_recorrido, %s) WHERE id = %s""", (momento, recorrido, id_envio))
        cur.execute("UPDATE camiones SET estado = 'en_ruta' WHERE placa = %s", (envio["placa"],))
        cur.execute("UPDATE puntos_control SET hora_real = %s, retraso_s = 0 WHERE id_envio = %s AND orden = 0",
                    (momento, id_envio))
        viaje.puntos[0]["hora_real"] = momento
        envio["estado"], envio["salida_real"] = "en_ruta", momento
        eventos.append(f"Salió (recorrido {recorrido}).")
    elif id_recorrido and envio["id_recorrido"] and id_recorrido != envio["id_recorrido"] and not viaje.aviso_recorrido:
        viaje.aviso_recorrido = True
        eventos.append(f"Llegan paquetes con otro recorrido ({id_recorrido}); se sigue vigilando el envío.")
    salida = envio["salida_real"]
    # La flota de la página muestra al camión donde va ahora
    cur.execute("UPDATE camiones SET ultima_lat = %s, ultima_lon = %s, ubicacion_en = %s WHERE placa = %s",
                (lat, lon, momento, envio["placa"]))

    # 2. Km reales recorridos -------------------------------------------------------
    velocidad = velocidad_kmh
    if viaje.ultimo is None:
        viaje.ultimo = (lat, lon, momento)
    else:
        d = rutas.distancia(viaje.ultimo[0], viaje.ultimo[1], lat, lon)
        dt = (momento - viaje.ultimo[2]).total_seconds()
        if dt > 0 and d >= TEMBLOR_M:
            if d / dt <= VEL_MAXIMA_MS:
                viaje.recorrido_m += d
                if velocidad is None:
                    velocidad = d / dt * 3.6
            else:
                eventos.append(f"Salto del GPS de {d:.0f} m en {dt:.0f} s: no se suma.")
            viaje.ultimo = (lat, lon, momento)
        elif velocidad is None and dt > 0:
            velocidad = 0.0

    if envio["estado"] == "en_destino":
        cur.execute("""UPDATE envios SET recorrido_m = %s, velocidad_kmh = %s, actualizado_en = %s
                       WHERE id = %s""", (viaje.recorrido_m, velocidad, momento, id_envio))
        return eventos

    # 3. Avance sobre la ruta y desvío ----------------------------------------------
    avance, desvio = rutas.proyectar(viaje.ruta, lat, lon, viaje.avance_m - ATRAS_M, viaje.avance_m + ADELANTE_M)
    if desvio > CORREDOR_M:
        avance_total, desvio_total = rutas.proyectar(viaje.ruta, lat, lon)     # por si se adelantó mucho
        if desvio_total <= CORREDOR_M:
            avance, desvio = avance_total, desvio_total
    en_ruta = desvio <= CORREDOR_M
    avance_antes = viaje.avance_m
    if en_ruta:
        viaje.avance_m = max(viaje.avance_m, avance)      # no retrocede por el ruido del GPS

    # 4. Puntos de control ------------------------------------------------------------
    for punto in viaje.puntos[1:-1]:
        if punto["hora_real"] is not None:
            continue
        if not (en_ruta and viaje.avance_m >= punto["distancia_m"] - TOLERANCIA_PUNTO_M):
            break
        eventos.append(_marcar_punto(cur, viaje, punto, salida, avance_antes, momento))
    llegada = viaje.puntos[-1]
    cerca_del_destino = (viaje.avance_m >= viaje.distancia_m / 2          # (por si el destino queda cerca del origen)
                         and rutas.distancia(lat, lon, llegada["lat"], llegada["lon"]) <= LLEGADA_M)
    if llegada["hora_real"] is None and (viaje.avance_m >= viaje.distancia_m - LLEGADA_M or cerca_del_destino):
        eventos.append(_marcar_punto(cur, viaje, llegada, salida, avance_antes, momento))
        cur.execute("UPDATE envios SET estado = 'en_destino', llegada_real = %s, retraso_s = %s, desvio_m = %s, "
                    "avance_m = %s, recorrido_m = %s, velocidad_kmh = %s, actualizado_en = %s WHERE id = %s",
                    (llegada["hora_real"], llegada["retraso_s"], desvio, max(viaje.avance_m, avance),
                     viaje.recorrido_m, velocidad, momento, id_envio))
        cur.execute("UPDATE alertas SET fin = %s WHERE id_envio = %s AND fin IS NULL", (momento, id_envio))
        viaje.alerta_desvio = viaje.alerta_retraso = False
        eventos.append(f"Llegó al destino. Recorrió {viaje.recorrido_m / 1000:.2f} km.")
        viaje.previo = (viaje.avance_m, momento)
        return eventos

    # 5. Retraso en vivo -------------------------------------------------------------
    tiempo_plan = rutas.interpolar(viaje.ruta, viaje.avance_m)[2]
    retraso = round((momento - salida).total_seconds() - tiempo_plan)

    # 6. Alertas ------------------------------------------------------------------------
    eventos += _alerta_desvio(cur, viaje, desvio, lat, lon, momento)
    eventos += _alerta_retraso(cur, viaje, retraso, lat, lon, momento)

    cur.execute("""UPDATE envios SET avance_m = %s, recorrido_m = %s, retraso_s = %s, desvio_m = %s,
                   velocidad_kmh = %s, actualizado_en = %s WHERE id = %s""",
                (viaje.avance_m, viaje.recorrido_m, retraso, desvio, velocidad, momento, id_envio))
    viaje.previo = (viaje.avance_m, momento)
    return eventos


def _marcar_punto(cur, viaje, punto, salida, avance_antes, momento):
    """Hora a la que pasó el punto: se interpola entre el paquete anterior y este según el avance."""
    hora = momento
    if viaje.previo and viaje.avance_m > avance_antes:
        fraccion = (punto["distancia_m"] - avance_antes) / (viaje.avance_m - avance_antes)
        fraccion = max(0.0, min(1.0, fraccion))
        hora = viaje.previo[1] + (momento - viaje.previo[1]) * fraccion
    retraso = round((hora - (salida + timedelta(seconds=punto["tiempo_plan_s"]))).total_seconds())
    anterior = max((p for p in viaje.puntos if p["hora_real"] is not None and p["orden"] < punto["orden"]),
                   key=lambda p: p["orden"], default=None)
    velocidad = None
    if anterior is not None:
        segundos = (hora - anterior["hora_real"]).total_seconds()
        if segundos > 0:
            velocidad = (punto["distancia_m"] - anterior["distancia_m"]) / segundos * 3.6
    cur.execute("""UPDATE puntos_control SET hora_real = %s, retraso_s = %s, velocidad_kmh = %s
                   WHERE id_envio = %s AND orden = %s""", (hora, retraso, velocidad, viaje.id, punto["orden"]))
    punto["hora_real"], punto["retraso_s"] = hora, retraso
    signo = "+" if retraso >= 0 else "-"
    return (f"Pasó el punto {punto['orden']} ({punto['nombre']}) con {signo}{abs(retraso) // 60}:{abs(retraso) % 60:02d}"
            f" ({nivel_retraso(retraso, viaje.duracion_s)}).")


def _alerta_desvio(cur, viaje, desvio, lat, lon, momento):
    eventos = []
    if desvio > CORREDOR_M:
        if viaje.fuera == 0:
            viaje.fuera_desde = (momento, lat, lon)
        viaje.fuera, viaje.dentro = viaje.fuera + 1, 0
    elif desvio < VUELTA_M:
        viaje.dentro, viaje.fuera = viaje.dentro + 1, 0
    else:
        viaje.fuera = viaje.dentro = 0      # en la franja de 60 a 120 m no se decide nada

    if viaje.fuera >= PAQUETES_DESVIO and not viaje.alerta_desvio:
        inicio, lat0, lon0 = viaje.fuera_desde
        cur.execute("""INSERT INTO alertas (id_envio, tipo, inicio, valor_max, lat, lon)
                       VALUES (%s, 'desvio', %s, %s, %s, %s)""", (viaje.id, inicio, round(desvio), lat0, lon0))
        viaje.alerta_desvio = True
        eventos.append(f"ALERTA: desvío de {desvio:.0f} m de la ruta.")
    elif viaje.alerta_desvio and desvio > CORREDOR_M:
        cur.execute("""UPDATE alertas SET valor_max = GREATEST(valor_max, %s)
                       WHERE id_envio = %s AND tipo = 'desvio' AND fin IS NULL""", (round(desvio), viaje.id))
    if viaje.alerta_desvio and viaje.dentro >= PAQUETES_VUELTA:
        cur.execute("UPDATE alertas SET fin = %s WHERE id_envio = %s AND tipo = 'desvio' AND fin IS NULL",
                    (momento, viaje.id))
        viaje.alerta_desvio = False
        eventos.append("Volvió a la ruta: se cierra la alerta de desvío.")
    return eventos


def _alerta_retraso(cur, viaje, retraso, lat, lon, momento):
    eventos = []
    sostener = timedelta(seconds=SOSTENER_RETRASO_DEMO_S if modo_demo() else SOSTENER_RETRASO_S)
    if nivel_retraso(retraso, viaje.duracion_s) == "alerta":
        viaje.normal_desde = None
        viaje.alto_desde = viaje.alto_desde or momento
        if not viaje.alerta_retraso and momento - viaje.alto_desde >= sostener:
            cur.execute("""INSERT INTO alertas (id_envio, tipo, inicio, valor_max, lat, lon)
                           VALUES (%s, 'retraso', %s, %s, %s, %s)""", (viaje.id, viaje.alto_desde, retraso, lat, lon))
            viaje.alerta_retraso = True
            eventos.append(f"ALERTA: retraso de {retraso // 60} min {retraso % 60} s.")
        elif viaje.alerta_retraso:
            cur.execute("""UPDATE alertas SET valor_max = GREATEST(valor_max, %s)
                           WHERE id_envio = %s AND tipo = 'retraso' AND fin IS NULL""", (retraso, viaje.id))
    else:
        viaje.alto_desde = None
        if viaje.alerta_retraso:
            viaje.normal_desde = viaje.normal_desde or momento
            if momento - viaje.normal_desde >= sostener:
                cur.execute("UPDATE alertas SET fin = %s WHERE id_envio = %s AND tipo = 'retraso' AND fin IS NULL",
                            (momento, viaje.id))
                viaje.alerta_retraso = False
                eventos.append("El retraso bajó: se cierra la alerta de retraso.")
    return eventos
