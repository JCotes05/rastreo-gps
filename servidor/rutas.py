"""
rutas.py — P3-S2, paso 2: motor de rutas de Orbita.

Qué hace:
  1. calcular_ruta(): pide la ruta entre el origen y el destino a
     openrouteservice con el perfil de CAMIÓN (driving-hgv). Si no hay
     clave o el servicio falla, usa OSRM (perfil de carro, con los
     tiempos ajustados para camión).
  2. puntos_de_control(): reparte los puntos de control sobre la ruta y
     "imanta" cada uno al giro más cercano.
  3. Geometría que también usa el listener (paso 4): distancia(),
     interpolar() y proyectar().

Solo usa la librería estándar de Python: no hay que instalar nada.

Prueba en el PC (genera un mapa y lo abre en el navegador):
    py rutas.py                   Uninorte -> Centro de Barranquilla
    py rutas.py --larga           Barranquilla -> Cartagena
    py rutas.py --origen 11.0190,-74.8505 --destino 10.9632,-74.7964
    py rutas.py --solo-osrm       usa OSRM aunque haya clave de openrouteservice
"""

import argparse
import bisect
import json
import math
import os
import sys
import urllib.error
import urllib.request
import webbrowser

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------
CARPETA = os.path.dirname(os.path.abspath(__file__))
RUTA_CLAVE_ORS = os.path.join(CARPETA, "clave_ors.txt")     # NUNCA se sube a GitHub

URL_ORS = "https://api.openrouteservice.org/v2/directions/driving-hgv/geojson"
URL_OSRM = "https://router.project-osrm.org/route/v1/driving/"
AGENTE = "Orbita/1.0 (proyecto Diseno Electronico, Universidad del Norte)"
TIEMPO_ESPERA_S = 20
RADIO_BUSQUEDA_VIA_M = 1000     # ORS: qué tan lejos de una vía puede estar el punto marcado
FACTOR_CAMION_OSRM = 1.25       # OSRM calcula para carro: un camión tarda ~25 % más

ESPACIADO_PUNTOS_M = 5000       # aproximadamente un punto de control cada 5 km...
MIN_PUNTOS, MAX_PUNTOS = 3, 10  # ...pero entre 3 y 10 (sin contar salida y llegada)
VENTANA_GIRO = 0.25             # se busca un giro a ±25 % del espaciado

RADIO_TIERRA_M = 6371000.0

# Giros que sirven como punto de control (cambios de vía).
# openrouteservice: 0-5 izquierda/derecha, 7 glorieta, 9 vuelta en U, 12-13 mantenerse a un lado
GIROS_ORS = {0, 1, 2, 3, 4, 5, 7, 9, 12, 13}
# OSRM: tipos de maniobra
GIROS_OSRM = {"turn", "end of road", "fork", "merge", "on ramp", "off ramp",
              "roundabout", "rotary", "roundabout turn"}


class ErrorRuta(Exception):
    """Error con un mensaje listo para mostrar en la página."""


class _ErrorHTTP(ErrorRuta):
    def __init__(self, codigo, detalle):
        super().__init__(f"HTTP {codigo}")
        self.codigo = codigo
        self.detalle = detalle


# ---------------------------------------------------------------------
# Geometría
# ---------------------------------------------------------------------
def distancia(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos (fórmula del haversine)."""
    f1, f2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((f2 - f1) / 2) ** 2
         + math.cos(f1) * math.cos(f2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * RADIO_TIERRA_M * math.asin(math.sqrt(a))


def interpolar(ruta, d):
    """(lat, lon, tiempo_s) del punto que está a d metros del origen, sobre la ruta."""
    dist, pts, tiempo = ruta["dist_m"], ruta["puntos"], ruta["tiempo_s"]
    if d <= 0:
        return pts[0][0], pts[0][1], tiempo[0]
    if d >= dist[-1]:
        return pts[-1][0], pts[-1][1], tiempo[-1]
    i = bisect.bisect_right(dist, d) - 1            # tramo i: entre el punto i y el i+1
    largo = dist[i + 1] - dist[i]
    f = (d - dist[i]) / largo if largo > 0 else 0.0
    return (pts[i][0] + f * (pts[i + 1][0] - pts[i][0]),
            pts[i][1] + f * (pts[i + 1][1] - pts[i][1]),
            tiempo[i] + f * (tiempo[i + 1] - tiempo[i]))


def proyectar(ruta, lat, lon, desde_m=0.0, hasta_m=None):
    """
    Lleva una posición del GPS a la ruta. Devuelve (avance_m, desvio_m):
      avance_m: cuántos metros de la ruta lleva recorridos ese punto.
      desvio_m: a cuántos metros está el camión de la línea de la ruta.
    desde_m / hasta_m limitan la búsqueda a una parte de la ruta, para no
    confundir un tramo de ida con uno de vuelta que pase cerca.
    """
    dist, pts = ruta["dist_m"], ruta["puntos"]
    i0 = max(0, bisect.bisect_right(dist, desde_m) - 1)
    i1 = len(pts) - 1 if hasta_m is None else min(len(pts) - 1, bisect.bisect_left(dist, hasta_m) + 1)
    # Plano local en metros con el camión en el origen (sirve para distancias cortas).
    ky = math.pi * RADIO_TIERRA_M / 180
    kx = ky * math.cos(math.radians(lat))
    mejor_d2, mejor_avance = math.inf, 0.0
    for i in range(i0, max(i0, i1)):
        ax, ay = (pts[i][1] - lon) * kx, (pts[i][0] - lat) * ky
        bx, by = (pts[i + 1][1] - lon) * kx, (pts[i + 1][0] - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
        qx, qy = ax + t * dx, ay + t * dy                # punto de la ruta más cercano
        d2 = qx * qx + qy * qy
        if d2 < mejor_d2:
            mejor_d2, mejor_avance = d2, dist[i] + t * (dist[i + 1] - dist[i])
    if mejor_d2 == math.inf:                             # ruta de un solo punto
        return 0.0, distancia(lat, lon, pts[0][0], pts[0][1])
    return mejor_avance, math.sqrt(mejor_d2)


# ---------------------------------------------------------------------
# Pedir la ruta a los servicios
# ---------------------------------------------------------------------
def _pedir_json(url, cuerpo=None, cabeceras=None):
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    pedido = urllib.request.Request(url, data=datos, headers={
        "User-Agent": AGENTE,
        "Accept": "application/json, application/geo+json",
        **(cabeceras or {}),
    })
    if datos is not None:
        pedido.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(pedido, timeout=TIEMPO_ESPERA_S) as respuesta:
            return json.loads(respuesta.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detalle = json.loads(e.read().decode("utf-8"))
        except Exception:
            detalle = {}
        raise _ErrorHTTP(e.code, detalle) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErrorRuta(f"no respondió ({e})") from None
    except ValueError:
        raise ErrorRuta("respondió algo que no es JSON") from None


def leer_clave_ors():
    """La clave sale de la variable ORS_API_KEY o del archivo clave_ors.txt. None si no hay."""
    clave = os.environ.get("ORS_API_KEY", "").strip()
    if clave:
        return clave
    try:
        with open(RUTA_CLAVE_ORS, encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _nombre_via(nombre):
    nombre = (nombre or "").strip()
    return "" if nombre in ("-", "") else nombre


def _mensaje_ors(error):
    if error.codigo in (401, 403):
        return "la clave no es válida o todavía no está activa"
    if error.codigo == 429:
        return "se agotó el cupo de consultas (2.000 al día, 40 por minuto)"
    info = error.detalle.get("error", {}) if isinstance(error.detalle, dict) else {}
    if not isinstance(info, dict):
        info = {"message": str(info)}
    codigo = info.get("code")
    if codigo == 2010:
        return "el origen o el destino está muy lejos de una vía"
    if codigo == 2009:
        return "no hay una ruta para camión entre esos puntos"
    if codigo == 2004:
        return "la ruta es demasiado larga"
    return info.get("message") or f"error HTTP {error.codigo}"


def _ruta_ors(origen, destino, clave, peso_t=None):
    cuerpo = {
        "coordinates": [[origen[1], origen[0]], [destino[1], destino[0]]],   # ORS usa [lon, lat]
        "instructions": True,
        "language": "es",
        "units": "m",
        "radiuses": [RADIO_BUSQUEDA_VIA_M, RADIO_BUSQUEDA_VIA_M],
        "options": {"vehicle_type": "hgv"},
    }
    if peso_t:
        cuerpo["options"]["profile_params"] = {"restrictions": {"weight": round(peso_t, 1)}}
    try:
        r = _pedir_json(URL_ORS, cuerpo, {"Authorization": clave})
    except _ErrorHTTP as e:
        raise ErrorRuta(_mensaje_ors(e)) from None
    try:
        rasgo = r["features"][0]
        coords = rasgo["geometry"]["coordinates"]
        pasos = [p for s in rasgo["properties"]["segments"] for p in s.get("steps", [])]
    except (KeyError, IndexError, TypeError):
        raise ErrorRuta("respondió en un formato inesperado") from None

    puntos = [[c[1], c[0]] for c in coords]
    largos = _largos(puntos)
    dur_tramo = [0.0] * len(largos)
    vias, giros = [], []
    for k, paso in enumerate(pasos):
        a, b = paso["way_points"]
        nombre = _nombre_via(paso.get("name"))
        largo = sum(largos[a:b])
        for i in range(a, b):        # la duración del paso se reparte según el largo de cada tramo
            dur_tramo[i] = paso.get("duration", 0) * (largos[i] / largo if largo > 0 else 1 / (b - a))
        vias.append((a, b, nombre))
        if paso.get("type") in GIROS_ORS and 0 < a < len(puntos) - 1:
            antes = vias[-2][2] if len(vias) >= 2 else ""
            giros.append({"indice": a, "antes": antes, "despues": nombre})
    return _armar("ors-hgv", puntos, dur_tramo, vias, giros)


def _mensaje_osrm(codigo):
    return {
        "NoRoute": "no hay una ruta por carretera entre esos puntos",
        "NoSegment": "el origen o el destino está muy lejos de una vía",
        "TooBig": "la ruta es demasiado larga",
    }.get(codigo, f"respondió {codigo}")


def _ruta_osrm(origen, destino):
    url = (f"{URL_OSRM}{origen[1]:.6f},{origen[0]:.6f};{destino[1]:.6f},{destino[0]:.6f}"
           "?overview=full&geometries=geojson&steps=true&annotations=duration")
    try:
        r = _pedir_json(url)
    except _ErrorHTTP as e:
        raise ErrorRuta(_mensaje_osrm(e.detalle.get("code") if isinstance(e.detalle, dict) else None)) from None
    if r.get("code") != "Ok":
        raise ErrorRuta(_mensaje_osrm(r.get("code")))
    try:
        ruta = r["routes"][0]
        coords = ruta["geometry"]["coordinates"]
        tramo = ruta["legs"][0]
    except (KeyError, IndexError, TypeError):
        raise ErrorRuta("respondió en un formato inesperado") from None

    puntos = [[c[1], c[0]] for c in coords]
    largos = _largos(puntos)
    dur_tramo = (tramo.get("annotation") or {}).get("duration")
    if not dur_tramo or len(dur_tramo) != len(largos):
        total, suma = ruta.get("duration", 0), sum(largos) or 1
        dur_tramo = [total * l / suma for l in largos]
    dur_tramo = [d * FACTOR_CAMION_OSRM for d in dur_tramo]

    vias, giros, a = [], [], 0
    for paso in tramo.get("steps", []):
        a = _ajustar_indice(puntos, paso.get("maneuver", {}).get("location"), a)
        n_coords = len(paso.get("geometry", {}).get("coordinates", []))
        b = min(a + max(n_coords - 1, 0), len(puntos) - 1)
        nombre = _nombre_via(paso.get("name") or paso.get("ref"))
        vias.append((a, b, nombre))
        if paso.get("maneuver", {}).get("type") in GIROS_OSRM and 0 < a < len(puntos) - 1:
            antes = vias[-2][2] if len(vias) >= 2 else ""
            giros.append({"indice": a, "antes": antes, "despues": nombre})
        a = b
    return _armar("osrm-car", puntos, dur_tramo, vias, giros)


def _ajustar_indice(puntos, ubicacion, indice):
    """La geometría de OSRM es la unión de la de cada paso; por si acaso, se confirma que el
    giro esté en ese punto y, si no, se busca el más cercano alrededor."""
    if not ubicacion:
        return indice
    lon, lat = ubicacion
    if distancia(lat, lon, *puntos[indice]) < 5:
        return indice
    cerca = range(max(0, indice - 50), min(len(puntos), indice + 50))
    return min(cerca, key=lambda i: distancia(lat, lon, *puntos[i]))


def _largos(puntos):
    return [distancia(*puntos[i], *puntos[i + 1]) for i in range(len(puntos) - 1)]


def _armar(fuente, puntos, dur_tramo, vias, giros):
    if len(puntos) < 2:
        raise ErrorRuta("la ruta llegó vacía")
    dist, tiempo = [0.0], [0.0]
    for i, largo in enumerate(_largos(puntos)):
        dist.append(dist[-1] + largo)
        tiempo.append(tiempo[-1] + max(0.0, dur_tramo[i]))
    return {
        "fuente": fuente,
        "puntos": puntos,
        "dist_m": dist,
        "tiempo_s": tiempo,
        "distancia_m": round(dist[-1]),
        "duracion_s": round(tiempo[-1]),
        "vias": vias,        # (desde, hasta, nombre): qué vía va entre esos índices
        "giros": giros,      # candidatos a punto de control
    }


def calcular_ruta(origen, destino, peso_t=None, solo_osrm=False):
    """
    origen y destino: (lat, lon). Devuelve un diccionario con la ruta
    (puntos, dist_m y tiempo_s acumulados, distancia_m, duracion_s, fuente...).
    Si openrouteservice falla y OSRM no, la ruta trae "aviso" con el motivo.
    Lanza ErrorRuta si no se pudo calcular.
    """
    for lat, lon in (origen, destino):
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ErrorRuta("Las coordenadas no son válidas.")
    if distancia(*origen, *destino) < 50:
        raise ErrorRuta("El origen y el destino están en el mismo lugar.")

    clave = None if solo_osrm else leer_clave_ors()
    errores = []
    if clave:
        try:
            return _ruta_ors(origen, destino, clave, peso_t)
        except ErrorRuta as e:
            errores.append(f"openrouteservice: {e}")
    try:
        ruta = _ruta_osrm(origen, destino)
    except ErrorRuta as e:
        errores.append(f"OSRM: {e}")
        raise ErrorRuta("No se pudo calcular la ruta (" + "; ".join(errores) + ").") from None
    if errores:
        ruta["aviso"] = errores[0] + ". Se usó OSRM."
    return ruta


# ---------------------------------------------------------------------
# Puntos de control
# ---------------------------------------------------------------------
def _via_en(ruta, d):
    i = max(0, bisect.bisect_right(ruta["dist_m"], d) - 1)
    for a, b, nombre in ruta["vias"]:
        if a <= i < b and nombre:
            return nombre
    return ""


_PREFIJOS_RAMPA = ("salida a ", "entrada a ", "acceso a ", "retorno a ")


def _sin_rampa(nombre):
    """'Salida a Avenida Carrera 46' -> 'Avenida Carrera 46' (las rampas llevan el nombre de la vía a la que van)."""
    for prefijo in _PREFIJOS_RAMPA:
        if nombre.lower().startswith(prefijo):
            return nombre[len(prefijo):]
    return nombre


def _nombre_cruce(giro):
    antes, despues = _sin_rampa(giro["antes"]), _sin_rampa(giro["despues"])
    if antes and despues and antes != despues:
        return f"{antes} con {despues}"[:80]
    return (despues or antes)[:80]


def puntos_de_control(ruta, nombre_origen="Salida", nombre_destino="Llegada"):
    """
    Lista de puntos (salida, controles y llegada) lista para guardar en la
    tabla puntos_control. Cada control se pone a distancias iguales sobre la
    ruta y se mueve al giro más cercano, si hay uno a menos del 25 % del
    espaciado: ahí es donde un camión se puede desviar y donde el punto se
    reconoce fácil ("Carrera 46 con Calle 72").
    """
    dist, tiempo, pts = ruta["dist_m"], ruta["tiempo_s"], ruta["puntos"]
    total = dist[-1]
    n = max(MIN_PUNTOS, min(MAX_PUNTOS, round(total / ESPACIADO_PUNTOS_M)))
    espaciado = total / (n + 1)
    ventana = VENTANA_GIRO * espaciado

    lista = [{"orden": 0, "tipo": "salida", "nombre": nombre_origen[:80], "lat": pts[0][0], "lon": pts[0][1],
              "distancia_m": 0, "tiempo_plan_s": 0}]
    for k in range(1, n + 1):
        objetivo = k * espaciado
        cerca = [g for g in ruta["giros"] if abs(dist[g["indice"]] - objetivo) <= ventana]
        if cerca:
            giro = min(cerca, key=lambda g: abs(dist[g["indice"]] - objetivo))
            i = giro["indice"]
            d, t, (lat, lon) = dist[i], tiempo[i], pts[i]
            nombre = _nombre_cruce(giro) or f"Km {d / 1000:.1f}"
            if " con " not in nombre and not nombre.startswith("Km "):
                nombre = f"{nombre} (km {d / 1000:.1f})"     # giro sin cruce con nombre: se ubica por el km
        else:
            lat, lon, t = interpolar(ruta, objetivo)
            d = objetivo
            via = _via_en(ruta, d)
            nombre = f"{via} (km {d / 1000:.1f})" if via else f"Km {d / 1000:.1f}"
        if nombre in {p["nombre"] for p in lista}:
            nombre = f"{nombre} (km {d / 1000:.1f})"          # dos puntos no pueden llamarse igual
        lista.append({"orden": k, "tipo": "control", "nombre": nombre, "lat": lat, "lon": lon,
                      "distancia_m": round(d), "tiempo_plan_s": round(t)})
    lista.append({"orden": n + 1, "tipo": "llegada", "nombre": nombre_destino[:80],
                  "lat": pts[-1][0], "lon": pts[-1][1],
                  "distancia_m": round(total), "tiempo_plan_s": round(tiempo[-1])})
    return lista


def ruta_para_bd(ruta):
    """Lo que se guarda en la columna envios.ruta (JSONB): {"puntos", "dist_m", "tiempo_s"}."""
    return {
        "puntos": [[round(lat, 6), round(lon, 6)] for lat, lon in ruta["puntos"]],
        "dist_m": [round(d, 1) for d in ruta["dist_m"]],
        "tiempo_s": [round(t, 1) for t in ruta["tiempo_s"]],
    }


# ---------------------------------------------------------------------
# Prueba desde la consola: imprime el resumen y dibuja un mapa
# ---------------------------------------------------------------------
_PLANTILLA_MAPA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orbita · prueba de ruta</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body, #mapa { height: 100%; margin: 0; font-family: Calibri, Arial, sans-serif; }
  .panel { position: absolute; top: 12px; right: 12px; z-index: 1000; background: #fff; padding: 12px 16px;
           border-radius: 10px; box-shadow: 0 2px 10px rgba(11,31,58,.25); max-width: 340px; color: #0B1F3A; }
  .panel h1 { font-size: 17px; margin: 0 0 6px; }
  .panel p { margin: 2px 0; font-size: 14px; }
  .panel .aviso { color: #E4572E; }
  .num { background: #0096FE; color: #fff; border: 2px solid #fff; border-radius: 50%; width: 22px; height: 22px;
         line-height: 22px; text-align: center; font-weight: bold; font-size: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.4); }
  .num.salida { background: #0B1F3A; } .num.llegada { background: #E4572E; }
</style></head><body>
<div id="mapa"></div>
<div class="panel" id="panel"></div>
<script>
var D = __DATOS__;
var mapa = L.map('mapa');
// Abierto como archivo (file://), los servidores de OpenStreetMap bloquean las imágenes del mapa;
// por eso se usa CARTO y, si también falla, Esri.
var FONDOS = [
  ['https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
   '&copy; OpenStreetMap &copy; CARTO'],
  ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
   '&copy; Esri &copy; OpenStreetMap']
];
var fondo = null, errores = 0;
function ponerFondo(i) {
  if (fondo) { mapa.removeLayer(fondo); }
  errores = 0;
  fondo = L.tileLayer(FONDOS[i][0], { maxZoom: 19, attribution: FONDOS[i][1] }).addTo(mapa);
  fondo.on('tileerror', function () {
    if (++errores === 4 && i + 1 < FONDOS.length) { ponerFondo(i + 1); }
  });
}
ponerFondo(0);
var linea = L.polyline(D.puntos, { color: '#0096FE', weight: 5, opacity: .85 }).addTo(mapa);
D.giros.forEach(function (g) {
  L.circleMarker(g.pos, { radius: 3, color: '#A7B1BE', weight: 1, fillOpacity: 1 })
   .bindTooltip('Giro: ' + g.nombre).addTo(mapa);
});
D.control.forEach(function (p) {
  var icono = L.divIcon({ className: '', html: '<div class="num ' + p.tipo + '">' + p.orden + '</div>',
                          iconSize: [26, 26], iconAnchor: [13, 13] });
  L.marker([p.lat, p.lon], { icon: icono })
   .bindPopup('<b>' + p.orden + '. ' + p.nombre + '</b><br>Km ' + (p.distancia_m / 1000).toFixed(1) +
              ' · minuto ' + Math.round(p.tiempo_plan_s / 60)).addTo(mapa);
});
mapa.fitBounds(linea.getBounds(), { padding: [30, 30] });
document.getElementById('panel').innerHTML =
  '<h1>Prueba de ruta</h1><p>' + D.fuente + '</p>' +
  '<p>' + (D.distancia_m / 1000).toFixed(1) + ' km · ' + Math.round(D.duracion_s / 60) + ' min</p>' +
  '<p>' + (D.control.length - 2) + ' puntos de control (números) · giros en gris</p>' +
  (D.aviso ? '<p class="aviso">' + D.aviso + '</p>' : '');
</script></body></html>
"""

_FUENTES = {"ors-hgv": "openrouteservice, perfil de camión", "osrm-car": "OSRM, perfil de carro (tiempos x1.25)"}


def guardar_mapa(ruta, control, archivo):
    datos = {
        "fuente": _FUENTES.get(ruta["fuente"], ruta["fuente"]),
        "aviso": ruta.get("aviso", ""),
        "puntos": ruta_para_bd(ruta)["puntos"],
        "distancia_m": ruta["distancia_m"],
        "duracion_s": ruta["duracion_s"],
        "giros": [{"pos": ruta["puntos"][g["indice"]], "nombre": _nombre_cruce(g) or "sin nombre"}
                  for g in ruta["giros"]],
        "control": control,
    }
    with open(archivo, "w", encoding="utf-8") as f:
        f.write(_PLANTILLA_MAPA.replace("__DATOS__", json.dumps(datos, ensure_ascii=False)))


def _coordenada(texto):
    try:
        lat, lon = (float(x) for x in texto.split(","))
        return lat, lon
    except ValueError:
        raise argparse.ArgumentTypeError("escríbela como lat,lon (por ejemplo 11.0190,-74.8505)")


def main():
    parser = argparse.ArgumentParser(description="Prueba del motor de rutas de Orbita.")
    parser.add_argument("--origen", type=_coordenada, help="lat,lon del origen")
    parser.add_argument("--destino", type=_coordenada, help="lat,lon del destino")
    parser.add_argument("--larga", action="store_true", help="prueba Barranquilla -> Cartagena")
    parser.add_argument("--solo-osrm", action="store_true", help="no usar openrouteservice")
    parser.add_argument("--mapa", default="ruta_prueba.html", help="archivo del mapa (por defecto ruta_prueba.html)")
    parser.add_argument("--sin-abrir", action="store_true", help="no abrir el mapa en el navegador")
    args = parser.parse_args()

    if args.larga:
        origen, destino = (10.9639, -74.7964), (10.3910, -75.4794)      # Barranquilla -> Cartagena
    else:
        origen, destino = (11.0190, -74.8505), (10.9878, -74.7889)      # Uninorte -> Centro
    origen, destino = args.origen or origen, args.destino or destino

    print("Clave de openrouteservice:", "encontrada" if (leer_clave_ors() and not args.solo_osrm) else "no (se usa OSRM)")
    try:
        ruta = calcular_ruta(origen, destino, solo_osrm=args.solo_osrm)
    except ErrorRuta as e:
        print("ERROR:", e)
        sys.exit(1)
    control = puntos_de_control(ruta)

    print(f"\nRuta calculada con {_FUENTES[ruta['fuente']]}")
    if ruta.get("aviso"):
        print("  Aviso:", ruta["aviso"])
    print(f"  Distancia: {ruta['distancia_m'] / 1000:.1f} km   Tiempo estimado: {ruta['duracion_s'] / 60:.0f} min")
    print(f"  Puntos de la línea: {len(ruta['puntos'])}   Giros encontrados: {len(ruta['giros'])}")
    print(f"  Puntos de control: {len(control) - 2} (uno cada ~{ruta['distancia_m'] / (len(control) - 1) / 1000:.1f} km,"
          f" movidos al giro más cercano)\n")
    print(f"  {'#':>2}  {'Tipo':<8} {'Km':>6} {'Min':>5}  Nombre")
    for p in control:
        print(f"  {p['orden']:>2}  {p['tipo']:<8} {p['distancia_m'] / 1000:>6.1f} {p['tiempo_plan_s'] / 60:>5.0f}  {p['nombre']}")
    tamano = len(json.dumps(ruta_para_bd(ruta))) / 1024
    print(f"\n  Tamaño de la ruta guardada en la base: {tamano:.0f} KB")

    guardar_mapa(ruta, control, args.mapa)
    ruta_mapa = os.path.abspath(args.mapa)
    print(f"\nMapa guardado en {ruta_mapa}")
    if not args.sin_abrir:
        webbrowser.open("file:///" + ruta_mapa.replace("\\", "/").lstrip("/"))


if __name__ == "__main__":
    main()
