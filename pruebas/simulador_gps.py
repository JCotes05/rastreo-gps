"""
simulador_gps.py — P3-S2: hace de celular de un camión que recorre la ruta de un envío.

Lee la ruta del envío desde la API (/api/envios/<id>) y manda al listener
los mismos paquetes UDP que la app, cada 5 s:
    LAT:..,LON:..,HORA:..,RECORRIDO:..[,CONDUCTOR:..],ENVIO:..,VEL:..

Sirve para probar (y mostrar en la presentación) los puntos de control, el
retraso y el desvío sin tener que manejar. Solo usa la librería estándar.

Uso (en el PC con  py  o en la EC2 con  python3):
    py simulador_gps.py --servidor https://jesuscotes.sytes.net --envio 12
    py simulador_gps.py --servidor https://jesuscotes.sytes.net --envio 12 --ritmo 0.6
    py simulador_gps.py --servidor ... --envio 12 --desvio 1.5:300:40 --parar 3:120

Opciones:
    --ritmo R          1 = va justo a tiempo; 0.6 = va al 60 % (se atrasa); 1.3 = se adelanta
    --parar KM:SEG     se detiene SEG segundos al llegar al km KM (se puede repetir)
    --desvio KM:M:SEG  en el km KM se sale M metros de la ruta durante SEG segundos (se puede repetir)
    --cedula CEDULA    agrega el campo CONDUCTOR, como si el PC lo hubiera verificado
    --udp HOST:PUERTO  a dónde mandar los paquetes (por defecto, el host de --servidor y el puerto 5000)
    --cada S           segundos entre paquetes (por defecto 5, como la app)
"""

import argparse
import bisect
import json
import math
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

R = 6371000.0
COLOMBIA = timezone(timedelta(hours=-5))


def distancia(lat1, lon1, lat2, lon2):
    f1, f2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((f2 - f1) / 2) ** 2
         + math.cos(f1) * math.cos(f2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def punto_en(ruta, d):
    """(lat, lon) a d metros del origen, sobre la ruta."""
    dist, pts = ruta["dist_m"], ruta["puntos"]
    if d <= 0:
        return tuple(pts[0])
    if d >= dist[-1]:
        return tuple(pts[-1])
    i = bisect.bisect_right(dist, d) - 1
    f = (d - dist[i]) / ((dist[i + 1] - dist[i]) or 1)
    return pts[i][0] + f * (pts[i + 1][0] - pts[i][0]), pts[i][1] + f * (pts[i + 1][1] - pts[i][1])


def distancia_en_tiempo(ruta, t):
    """Cuántos metros se recorren en t segundos de viaje según el plan de la ruta."""
    tiempo, dist = ruta["tiempo_s"], ruta["dist_m"]
    if t <= 0:
        return 0.0
    if t >= tiempo[-1]:
        return dist[-1]
    i = bisect.bisect_right(tiempo, t) - 1
    f = (t - tiempo[i]) / ((tiempo[i + 1] - tiempo[i]) or 1)
    return dist[i] + f * (dist[i + 1] - dist[i])


def tiempo_en_distancia(ruta, d):
    dist, tiempo = ruta["dist_m"], ruta["tiempo_s"]
    if d >= dist[-1]:
        return tiempo[-1]
    i = max(0, bisect.bisect_right(dist, d) - 1)
    f = (d - dist[i]) / ((dist[i + 1] - dist[i]) or 1)
    return tiempo[i] + f * (tiempo[i + 1] - tiempo[i])


def desplazar(ruta, d, metros):
    """Punto a 'metros' a la izquierda de la ruta (perpendicular a la marcha) en el km d."""
    lat, lon = punto_en(ruta, d)
    lat2, lon2 = punto_en(ruta, min(ruta["dist_m"][-1], d + 10))
    if (lat2, lon2) == (lat, lon):
        lat2, lon2 = lat, lon
        lat, lon = punto_en(ruta, max(0, d - 10))
        lat, lon, lat2, lon2 = lat2, lon2, lat, lon
    dy, dx = lat2 - lat, (lon2 - lon) * math.cos(math.radians(lat))
    norma = math.hypot(dx, dy) or 1
    return (lat + math.degrees(metros / R) * (dx / norma),
            lon - math.degrees(metros / (R * math.cos(math.radians(lat)))) * (dy / norma))


def leer_envio(servidor, id_envio):
    url = f"{servidor.rstrip('/')}/api/envios/{id_envio}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                    timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))["envio"]
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: {url} respondió {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"ERROR: no pude leer {url} ({e}). ¿Está bien la dirección y el paso 3 desplegado?")


def lista_de(textos, partes):
    salida = []
    for texto in textos or []:
        try:
            valores = [float(x) for x in texto.split(":")]
            if len(valores) != partes:
                raise ValueError
        except ValueError:
            sys.exit(f"ERROR: '{texto}' debe tener {partes} números separados por ':'")
        salida.append(valores)
    return salida


def main():
    parser = argparse.ArgumentParser(description="Simula el celular de un camión que recorre la ruta de un envío.")
    parser.add_argument("--servidor", required=True, help="por ejemplo https://jesuscotes.sytes.net")
    parser.add_argument("--envio", required=True, type=int, help="número del envío (ENV-0012 -> 12)")
    parser.add_argument("--ritmo", type=float, default=1.0)
    parser.add_argument("--parar", action="append", metavar="KM:SEG")
    parser.add_argument("--desvio", action="append", metavar="KM:METROS:SEG")
    parser.add_argument("--cedula")
    parser.add_argument("--udp", metavar="HOST:PUERTO")
    parser.add_argument("--cada", type=float, default=5.0)
    args = parser.parse_args()
    if not 0.1 <= args.ritmo <= 10:
        sys.exit("ERROR: --ritmo debe estar entre 0.1 y 10")

    envio = leer_envio(args.servidor, args.envio)
    if envio["estado"] not in ("asignado", "en_ruta"):
        sys.exit(f"ERROR: el envío {envio['codigo']} está '{envio['estado']}'. Debe estar asignado o en ruta.")
    ruta = envio["ruta"]
    total = ruta["dist_m"][-1]
    recorrido = envio["progreso"]["id_recorrido"] or str(int(time.time() * 1000))
    host, _, puerto = (args.udp or urlparse(args.servidor).hostname + ":5000").partition(":")
    destino_udp = (host, int(puerto or 5000))
    paradas = sorted([km * 1000, seg] for km, seg in lista_de(args.parar, 2))
    desvios = sorted([km * 1000, m, seg] for km, m, seg in lista_de(args.desvio, 3))

    print(f"{envio['codigo']}: {envio['origen']['direccion']} -> {envio['destino']['direccion']}")
    print(f"Ruta de {total / 1000:.2f} km planeada en {ruta['tiempo_s'][-1] / 60:.1f} min; ritmo {args.ritmo}"
          f" (llegaría en ~{ruta['tiempo_s'][-1] / args.ritmo / 60:.1f} min)")
    print(f"Recorrido {recorrido}; paquetes UDP a {destino_udp[0]}:{destino_udp[1]} cada {args.cada:g} s\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t_plan, d = 0.0, 0.0                 # tiempo "del plan" recorrido y distancia
    parado_hasta = None
    desvio_activo = None                 # [inicio_real, metros, segundos]
    inicio = time.monotonic()
    llegadas_extra = 3                   # al llegar, manda unos paquetes más quieto en el destino
    try:
        while True:
            ahora = time.monotonic() - inicio
            estado = ""
            # ¿empieza una parada o un desvío en este punto?
            if paradas and d >= paradas[0][0] and parado_hasta is None:
                parado_hasta = ahora + paradas.pop(0)[1]
            if desvios and d >= desvios[0][0] and desvio_activo is None:
                _, metros, segundos = desvios.pop(0)
                desvio_activo = [ahora, metros, segundos]

            if parado_hasta is not None and ahora < parado_hasta:
                velocidad = 0.0
                estado = f"parado ({parado_hasta - ahora:.0f} s más)"
            else:
                parado_hasta = None
                d_antes = d
                t_plan += args.ritmo * args.cada
                d = distancia_en_tiempo(ruta, t_plan)
                velocidad = (d - d_antes) / args.cada * 3.6

            lat, lon = punto_en(ruta, d)
            if desvio_activo:
                transcurrido = ahora - desvio_activo[0]
                if transcurrido >= desvio_activo[2]:
                    desvio_activo = None
                else:
                    # sale y vuelve poco a poco (en 15 s, o en la cuarta parte del desvío si es corto)
                    subida = min(15.0, desvio_activo[2] / 4)
                    rampa = min(1.0, transcurrido / subida, (desvio_activo[2] - transcurrido) / subida)
                    rampa = max(rampa, 0.34)
                    lat, lon = desplazar(ruta, d, desvio_activo[1] * rampa)
                    estado = f"DESVIADO {desvio_activo[1] * rampa:.0f} m"

            hora = datetime.now(COLOMBIA).strftime("%H:%M:%S")
            mensaje = f"LAT:{lat:.5f},LON:{lon:.5f},HORA:{hora},RECORRIDO:{recorrido}"
            if args.cedula:
                mensaje += f",CONDUCTOR:{args.cedula}"
            mensaje += f",ENVIO:{args.envio},VEL:{velocidad:.1f}"
            sock.sendto(mensaje.encode("utf-8"), destino_udp)

            plan = tiempo_en_distancia(ruta, d)
            print(f"t={ahora:5.0f} s  km {d / 1000:6.2f}/{total / 1000:.2f}  {velocidad:5.1f} km/h  "
                  f"retraso aprox. {ahora - plan:+5.0f} s  {estado}")
            if d >= total:
                llegadas_extra -= 1
                if llegadas_extra <= 0:
                    break
            time.sleep(max(0.0, args.cada - ((time.monotonic() - inicio) - ahora)))
    except KeyboardInterrupt:
        print("\nDetenido.")
    print(f"\nListo. Revisen el envío en {args.servidor.rstrip('/')}/api/envios/{args.envio}?ruta=0 o en la página.")


if __name__ == "__main__":
    main()
