"""
eventos_carga.py — P3-S3: eventos de la carga que manda el ESP32 de sensores.

El ESP32 (en la zona de la carga) NO tiene Wi-Fi: habla por Bluetooth con el
celular del conductor, y el celular le pasa todo al servidor por
POST /api/camiones/viaje/eventos (la ruta la atiende api_envios.py, que es
quien revisa el token del camión y luego llama a registrar()).

Eventos:
  frenada, hueco, curva   valor = g (desaceleración, golpe vertical o aceleración lateral)
  luz_abierta, luz_cerrada valor = nivel de luz (0-4095 del ADC del ESP32)

Reglas:
  - Cada evento trae un "uid" que pone el celular. Si el celular reintenta y
    manda otra vez el mismo uid, no se guarda dos veces (idempotente).
  - Cada evento se ubica sobre la ruta planeada (avance_m) con rutas.proyectar,
    para mostrarlo en el km donde ocurrió.
  - Luz dentro de la carga mientras el envío está EN RUTA = alerta
    "carga_abierta" (una sola activa). Al detectar oscuridad otra vez, se cierra.
    En el cargue (asignado) y en la entrega (en_destino) la luz es normal: solo
    queda registrada.
  - El latido del ESP (cada 10 s) actualiza el último estado de la luz y el
    último contacto con los sensores.

Si p3s3.sql todavía no se ha ejecutado (no existe la tabla), datos_de_carga()
devuelve None y el resto de la API sigue funcionando igual que en S2.
"""

import re
from datetime import datetime, timedelta, timezone

import psycopg2.errors

import rutas

TIPOS = ("frenada", "hueco", "curva", "luz_abierta", "luz_cerrada")
TIPOS_MOVIMIENTO = ("frenada", "hueco", "curva")
PATRON_UID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_EVENTOS_POR_LOTE = 200
MAX_EVENTOS_EN_JSON = 300
VALOR_MAXIMO = {"frenada": 16.0, "hueco": 16.0, "curva": 16.0, "luz_abierta": 4095, "luz_cerrada": 4095}
ANTIGUEDAD_MAXIMA = timedelta(days=7)        # eventos más viejos se rechazan
ADELANTO_MAXIMO = timedelta(minutes=10)      # reloj del celular adelantado: se usa la hora del servidor
SENSORES_EN_LINEA_S = 35                     # el ESP late cada 10 s: sin latido en 35 s = desconectado
VENTANA_PROYECCION_M = 3000                  # primero se busca cerca del avance actual
DESVIO_PROYECCION_M = 300                    # si queda más lejos, se busca en toda la ruta


class EventoInvalido(Exception):
    pass


# ---------------------------------------------------------------------
# Lectura y validación de un evento
# ---------------------------------------------------------------------
def _numero(valor, minimo, maximo):
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    valor = float(valor)
    return valor if minimo <= valor <= maximo else None


def _momento(texto, ahora):
    if texto is None:
        return ahora
    try:
        valor = datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        raise EventoInvalido("momento debe ser una fecha y hora ISO.") from None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone(timedelta(hours=-5)))    # hora de Colombia
    if valor < ahora - ANTIGUEDAD_MAXIMA:
        raise EventoInvalido("el evento tiene más de 7 días.")
    if valor > ahora + ADELANTO_MAXIMO:
        return ahora                          # el reloj del celular está muy adelantado
    return min(valor, ahora)


def leer_evento(d, ahora):
    """Valida un evento del lote. Devuelve un diccionario limpio o lanza EventoInvalido."""
    if not isinstance(d, dict):
        raise EventoInvalido("cada evento debe ser un objeto.")
    uid = d.get("uid")
    if not isinstance(uid, str) or not PATRON_UID.match(uid):
        raise EventoInvalido("uid falta o tiene caracteres no permitidos.")
    tipo = d.get("tipo")
    if tipo not in TIPOS:
        raise EventoInvalido(f"tipo debe ser uno de: {', '.join(TIPOS)}.")
    valor = None
    if d.get("valor") is not None:
        valor = _numero(d.get("valor"), 0, VALOR_MAXIMO[tipo])
        if valor is None:
            raise EventoInvalido(f"valor fuera de rango para {tipo}.")
    lat, lon = _numero(d.get("lat"), -90, 90), _numero(d.get("lon"), -180, 180)
    if lat is None or lon is None or (lat == 0 and lon == 0):
        lat = lon = None                      # sin GPS todavía: se guarda sin ubicación
    return {"uid": uid, "tipo": tipo, "valor": valor, "lat": lat, "lon": lon,
            "momento": _momento(d.get("momento"), ahora)}


def leer_latido(d, ahora):
    """{"luz": "abierta"|"cerrada", "nivel": 0-4095, "momento": ISO} -> diccionario limpio o None."""
    if not isinstance(d, dict):
        return None
    luz = d.get("luz")
    luz = luz.lower() if isinstance(luz, str) else None
    if luz not in ("abierta", "cerrada"):
        luz = None
    nivel = _numero(d.get("nivel"), 0, 4095)
    try:
        momento = _momento(d.get("momento"), ahora)
    except EventoInvalido:
        momento = ahora
    return {"luz": luz, "nivel": None if nivel is None else int(nivel), "momento": momento}


# ---------------------------------------------------------------------
# Guardar
# ---------------------------------------------------------------------
def _en_ruta_en(envio, momento):
    """¿El camión estaba en ruta (ya salió y no había llegado) en ese momento?"""
    salida, llegada = envio["salida_real"], envio["llegada_real"]
    return salida is not None and momento >= salida and (llegada is None or momento < llegada)


def _proyectar(ruta, envio, lat, lon):
    if not ruta or lat is None:
        return None
    try:
        actual = envio["avance_m"] or 0.0
        avance, desvio = rutas.proyectar(ruta, lat, lon, max(0.0, actual - VENTANA_PROYECCION_M),
                                         actual + VENTANA_PROYECCION_M)
        if desvio > DESVIO_PROYECCION_M:
            avance, _ = rutas.proyectar(ruta, lat, lon)
        return round(avance, 1)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _abrir_alerta_carga(cur, envio, momento, nivel, lat, lon):
    """Abre la alerta carga_abierta si no hay una activa. Devuelve True si la abrió."""
    cur.execute("""INSERT INTO alertas (id_envio, tipo, inicio, valor_max, lat, lon)
                   SELECT %s, 'carga_abierta', %s, %s, %s, %s
                   WHERE NOT EXISTS (SELECT 1 FROM alertas WHERE id_envio = %s AND tipo = 'carga_abierta' AND fin IS NULL)
                   RETURNING id""",
                (envio["id"], momento, int(nivel or 0), lat, lon, envio["id"]))
    return cur.fetchone() is not None


def _cerrar_alerta_carga(cur, id_envio, momento):
    cur.execute("""UPDATE alertas SET fin = GREATEST(inicio, %s)
                   WHERE id_envio = %s AND tipo = 'carga_abierta' AND fin IS NULL RETURNING id""",
                (momento, id_envio))
    return cur.fetchone() is not None


def _subir_nivel_alerta(cur, id_envio, nivel):
    if nivel is not None:
        cur.execute("""UPDATE alertas SET valor_max = GREATEST(valor_max, %s)
                       WHERE id_envio = %s AND tipo = 'carga_abierta' AND fin IS NULL""", (int(nivel), id_envio))


def registrar(cur, envio, eventos, latido, ruta, ultima_posicion=None):
    """
    Guarda un lote de eventos ya validados (ordenados por momento) y el latido.
    envio: fila de la tabla envios (con salida_real, llegada_real, avance_m, estado).
    Devuelve (guardados, repetidos, avisos) — listas de uid y de textos para el log.
    """
    guardados, repetidos, avisos = [], [], []
    ultima_luz = None                 # (estado, nivel, momento) del último evento de luz del lote
    for ev in sorted(eventos, key=lambda e: e["momento"]):
        if ev["lat"] is None and ultima_posicion:
            ev["lat"], ev["lon"] = ultima_posicion
        avance = _proyectar(ruta, envio, ev["lat"], ev["lon"])
        cur.execute("""INSERT INTO eventos_carga (id_envio, uid, tipo, valor, lat, lon, avance_m, momento)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id_envio, uid) DO NOTHING RETURNING id""",
                    (envio["id"], ev["uid"], ev["tipo"], ev["valor"], ev["lat"], ev["lon"], avance, ev["momento"]))
        if cur.fetchone() is None:
            repetidos.append(ev["uid"])
            continue
        guardados.append(ev["uid"])
        if ev["tipo"] in ("luz_abierta", "luz_cerrada"):
            abierta = ev["tipo"] == "luz_abierta"
            ultima_luz = ("abierta" if abierta else "cerrada", ev["valor"], ev["momento"])
            if abierta and envio["estado"] == "en_ruta" and _en_ruta_en(envio, ev["momento"]):
                if _abrir_alerta_carga(cur, envio, ev["momento"], ev["valor"], ev["lat"], ev["lon"]):
                    avisos.append(f"ALERTA: carga abierta en plena ruta (luz {ev['valor']}).")
                else:
                    _subir_nivel_alerta(cur, envio["id"], ev["valor"])
            elif not abierta and _cerrar_alerta_carga(cur, envio["id"], ev["momento"]):
                avisos.append("La carga se cerró: se cierra la alerta de carga abierta.")
        else:
            avisos.append(f"{ev['tipo']} de {ev['valor'] if ev['valor'] is not None else '?'} g"
                          + (f" en el km {avance / 1000:.2f}" if avance is not None else ""))

    # Último estado conocido de la luz y último contacto con el ESP.
    if ultima_luz:
        cur.execute("""UPDATE envios SET luz_estado = %s, luz_nivel = COALESCE(%s, luz_nivel)
                       WHERE id = %s AND (sensores_en IS NULL OR sensores_en <= %s)""",
                    (ultima_luz[0], None if ultima_luz[1] is None else int(ultima_luz[1]), envio["id"], ultima_luz[2]))
    if guardados:
        # Un evento nuevo también demuestra que el ESP está vivo.
        ultimo = max(ev["momento"] for ev in eventos if ev["uid"] in set(guardados))
        cur.execute("UPDATE envios SET sensores_en = GREATEST(COALESCE(sensores_en, %s), %s) WHERE id = %s",
                    (ultimo, ultimo, envio["id"]))
    if latido:
        cur.execute("""UPDATE envios SET sensores_en = GREATEST(COALESCE(sensores_en, %s), %s),
                                         luz_estado = COALESCE(%s, luz_estado), luz_nivel = COALESCE(%s, luz_nivel)
                       WHERE id = %s""",
                    (latido["momento"], latido["momento"], latido["luz"], latido["nivel"], envio["id"]))
        # El latido también abre o cierra la alerta (por si se perdió el evento de cambio,
        # o si la carga ya estaba abierta cuando se conectó el ESP).
        if latido["luz"] == "abierta" and envio["estado"] == "en_ruta" and _en_ruta_en(envio, latido["momento"]):
            lat, lon = ultima_posicion or (None, None)
            if _abrir_alerta_carga(cur, envio, latido["momento"], latido["nivel"], lat, lon):
                avisos.append("ALERTA: carga abierta en plena ruta (según el latido del ESP).")
            else:
                _subir_nivel_alerta(cur, envio["id"], latido["nivel"])
        elif latido["luz"] == "cerrada" and _cerrar_alerta_carga(cur, envio["id"], latido["momento"]):
            avisos.append("La carga se cerró (latido): se cierra la alerta de carga abierta.")
    return guardados, repetidos, avisos


# ---------------------------------------------------------------------
# Para el JSON del envío (página y app)
# ---------------------------------------------------------------------
def _iso(valor):
    return valor.astimezone(timezone(timedelta(hours=-5))).isoformat(timespec="seconds") if valor else None


def datos_de_carga(cur, id_envio):
    """
    {"eventos": [...], "conteo": {...}, "carga": {...}} del envío, o None si la
    base todavía no tiene P3-S3 (p3s3.sql sin ejecutar).
    """
    cur.execute("SAVEPOINT sensores")
    try:
        cur.execute("""SELECT id, tipo, valor, lat, lon, avance_m, momento FROM eventos_carga
                       WHERE id_envio = %s ORDER BY momento DESC, id DESC LIMIT %s""", (id_envio, MAX_EVENTOS_EN_JSON))
        filas = cur.fetchall()
        cur.execute("SELECT tipo, count(*) AS n FROM eventos_carga WHERE id_envio = %s GROUP BY tipo", (id_envio,))
        conteo = {t: 0 for t in TIPOS}
        for fila in cur.fetchall():
            conteo[fila["tipo"]] = fila["n"]
        cur.execute("SELECT luz_estado, luz_nivel, sensores_en FROM envios WHERE id = %s", (id_envio,))
        estado = cur.fetchone() or {}
        cur.execute("RELEASE SAVEPOINT sensores")
    except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn):
        cur.execute("ROLLBACK TO SAVEPOINT sensores")
        return None
    ultimo = estado.get("sensores_en")
    hace_s = None if ultimo is None else max(0, round((datetime.now(timezone.utc) - ultimo).total_seconds()))
    eventos = [{
        "id": e["id"], "tipo": e["tipo"],
        "valor": None if e["valor"] is None else round(e["valor"], 2 if e["tipo"] in TIPOS_MOVIMIENTO else 0),
        "lat": e["lat"], "lon": e["lon"],
        "avance_m": None if e["avance_m"] is None else round(e["avance_m"]),
        "momento": _iso(e["momento"]),
    } for e in reversed(filas)]                           # del más viejo al más nuevo
    return {
        "eventos": eventos,
        "conteo": conteo,
        "carga": {
            "estado": estado.get("luz_estado"),           # "abierta" | "cerrada" | None (sin datos)
            "nivel": estado.get("luz_nivel"),
            "ultimo_contacto": _iso(ultimo),
            "hace_s": hace_s,
            "en_linea": hace_s is not None and hace_s <= SENSORES_EN_LINEA_S,
        },
    }
