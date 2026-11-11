"""
simulador_sensores.py — P3-S3: hace de celular con el ESP32 de sensores conectado.

Manda a POST /api/camiones/viaje/eventos los mismos lotes que manda la app:
frenadas, huecos y curvas en ciertos km de la ruta, abrir y cerrar la carga,
y el latido del ESP cada 10 s. Sirve para la demostración en la página,
junto con simulador_gps.py (que mueve el camión).

Cómo sabe dónde va el camión: lee el avance del envío en la API cada 3 s
(lo calcula el servidor con los paquetes de simulador_gps.py o de la app).
Cuando el camión pasa el km pedido, manda el evento en ese punto de la ruta.
Con --ya no espera: manda todo de inmediato (ubicado en el km pedido).

Uso (en el PC con  py  o en la EC2 con  python3):
    py simulador_sensores.py --servidor https://jesuscotes.sytes.net --envio 12 --placa ZZZ123 --clave ******** \\
        --frenada 1.2 --hueco 2:1.3 --curva 2.5 --abrir 3:40

Opciones:
    --frenada KM[:G]     frenada brusca en el km KM (G = desaceleración en g; por defecto 0.6)
    --hueco KM[:G]       hueco en el km KM (G = golpe vertical; por defecto 1.1)
    --curva KM[:G]       curva brusca en el km KM (G = aceleración lateral; por defecto 0.5)
    --abrir KM:SEG       la carga se abre en el km KM durante SEG segundos (alerta si está en ruta)
    --abrir-ahora SEG    abre la carga YA durante SEG segundos (sirve en cualquier estado)
    --sin-latido         no manda el latido del ESP cada 10 s
    --ya                 no espera a que el camión llegue a cada km
    --token TOKEN        sesión del camión (si no, inicia sesión con --placa y --clave)

OJO: iniciar sesión con --placa/--clave cierra la sesión del celular de ese camión
(cada camión tiene una sola sesión). Para la demostración use un camión de prueba.
"""

import argparse
import bisect
import getpass
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

COLOMBIA = timezone(timedelta(hours=-5))
G_POR_DEFECTO = {"frenada": 0.6, "hueco": 1.1, "curva": 0.5}
LUZ_ABIERTA, LUZ_CERRADA = 812, 35


def punto_en(ruta, d):
    dist, pts = ruta["dist_m"], ruta["puntos"]
    if d <= 0:
        return tuple(pts[0])
    if d >= dist[-1]:
        return tuple(pts[-1])
    i = bisect.bisect_right(dist, d) - 1
    f = (d - dist[i]) / ((dist[i + 1] - dist[i]) or 1)
    return pts[i][0] + f * (pts[i + 1][0] - pts[i][0]), pts[i][1] + f * (pts[i + 1][1] - pts[i][1])


class Api:
    def __init__(self, servidor, token=None):
        self.servidor, self.token = servidor.rstrip("/"), token

    def pedir(self, metodo, ruta, datos=None, con_token=True):
        cuerpo = json.dumps(datos).encode("utf-8") if datos is not None else None
        pedido = urllib.request.Request(self.servidor + ruta, data=cuerpo, method=metodo)
        pedido.add_header("Content-Type", "application/json")
        pedido.add_header("Accept", "application/json")
        if con_token and self.token:
            pedido.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(pedido, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": str(e)}


def leer_km(textos, tipo):
    salida = []
    for texto in textos or []:
        partes = texto.split(":")
        try:
            km = float(partes[0])
            g = float(partes[1]) if len(partes) > 1 else G_POR_DEFECTO[tipo]
            if len(partes) > 2 or km < 0 or not 0 < g <= 16:
                raise ValueError
        except ValueError:
            sys.exit(f"ERROR: --{tipo} '{texto}' debe ser KM o KM:G (por ejemplo 1.5 o 1.5:0.7)")
        salida.append({"m": km * 1000, "tipo": tipo, "valor": g})
    return salida


def main():
    parser = argparse.ArgumentParser(description="Simula el ESP32 de sensores (a través del celular) para un envío.")
    parser.add_argument("--servidor", required=True)
    parser.add_argument("--envio", required=True, type=int)
    parser.add_argument("--placa")
    parser.add_argument("--clave")
    parser.add_argument("--token")
    parser.add_argument("--frenada", action="append", metavar="KM[:G]")
    parser.add_argument("--hueco", action="append", metavar="KM[:G]")
    parser.add_argument("--curva", action="append", metavar="KM[:G]")
    parser.add_argument("--abrir", action="append", metavar="KM:SEG")
    parser.add_argument("--abrir-ahora", type=float, metavar="SEG")
    parser.add_argument("--sin-latido", action="store_true")
    parser.add_argument("--ya", action="store_true")
    args = parser.parse_args()

    api = Api(args.servidor, args.token)
    if not api.token:
        if not args.placa:
            sys.exit("ERROR: falta --token o --placa (y --clave) del camión.")
        clave = args.clave or getpass.getpass(f"Contraseña del camión {args.placa}: ")
        estado, r = api.pedir("POST", "/api/camiones/login", {"placa": args.placa, "clave": clave}, con_token=False)
        if estado != 200:
            sys.exit(f"ERROR: no pude iniciar sesión ({estado}): {r.get('error', r)}")
        api.token = r["token"]
        print(f"Sesión abierta como {r['camion']['placa']} (la sesión del celular de ese camión queda cerrada).")

    estado, r = api.pedir("GET", f"/api/envios/{args.envio}")
    if estado != 200:
        sys.exit(f"ERROR: no pude leer el envío {args.envio} ({estado}): {r.get('error', r)}")
    envio = r["envio"]
    ruta = envio.get("ruta")
    if not ruta:
        sys.exit("ERROR: el envío no trae la ruta.")
    total = ruta["dist_m"][-1]
    print(f"{envio['codigo']} ({envio['estado']}): {envio['origen']['direccion']} -> {envio['destino']['direccion']}, "
          f"{total / 1000:.2f} km")

    pendientes = leer_km(args.frenada, "frenada") + leer_km(args.hueco, "hueco") + leer_km(args.curva, "curva")
    for texto in args.abrir or []:
        try:
            km, seg = (float(x) for x in texto.split(":"))
        except ValueError:
            sys.exit(f"ERROR: --abrir '{texto}' debe ser KM:SEG (por ejemplo 3:40)")
        pendientes.append({"m": km * 1000, "tipo": "abrir", "seg": seg})
    pendientes.sort(key=lambda x: x["m"])
    for p in pendientes:
        if p["m"] > total:
            print(f"Aviso: el km {p['m'] / 1000:g} está después del final de la ruta; se manda al final.")
    cerrar_en = []                           # [momento_monotonic, lat, lon]
    if args.abrir_ahora:
        pendientes.insert(0, {"m": -1, "tipo": "abrir", "seg": args.abrir_ahora})

    contador = [0]
    sufijo = str(int(time.time()))
    luz = {"estado": "cerrada", "nivel": LUZ_CERRADA}

    def mandar(lista, latido=False, posicion=None):
        cuerpo = {"id_envio": args.envio, "eventos": lista}
        if latido:
            cuerpo["latido"] = {"luz": luz["estado"], "nivel": luz["nivel"]}
        if posicion:
            cuerpo["posicion"] = {"lat": posicion[0], "lon": posicion[1]}
        estado, r = api.pedir("POST", "/api/camiones/viaje/eventos", cuerpo)
        if estado != 200:
            print(f"  ERROR {estado}: {r.get('error', r)}")
            return None
        return r

    def evento(tipo, valor, lat, lon):
        contador[0] += 1
        return {"uid": f"sim-{sufijo}-{contador[0]}", "tipo": tipo, "valor": valor, "lat": lat, "lon": lon,
                "momento": datetime.now(COLOMBIA).isoformat(timespec="seconds")}

    ultimo_latido = 0.0
    avance, estado_envio = 0.0, envio["estado"]
    if not args.ya:
        print("Esperando que el camión pase por cada km (Ctrl+C para salir)...\n")
    try:
        while True:
            ahora = time.monotonic()
            if not args.ya:
                estado, r = api.pedir("GET", f"/api/envios/{args.envio}?ruta=0")
                if estado == 200:
                    avance = r["envio"]["progreso"]["avance_m"]
                    estado_envio = r["envio"]["estado"]
                    if estado_envio == "en_destino":
                        avance = total
            pos = punto_en(ruta, avance)
            lote = []
            while pendientes and (args.ya or pendientes[0]["m"] <= avance):
                p = pendientes.pop(0)
                lat, lon = punto_en(ruta, p["m"] if args.ya else avance)
                if p["tipo"] == "abrir":
                    luz.update(estado="abierta", nivel=LUZ_ABIERTA)
                    lote.append(evento("luz_abierta", LUZ_ABIERTA, lat, lon))
                    cerrar_en.append([ahora + p["seg"], lat, lon])
                    print(f"  km {avance / 1000:5.2f}  se ABRE la carga por {p['seg']:g} s")
                else:
                    lote.append(evento(p["tipo"], p["valor"], lat, lon))
                    print(f"  km {(p['m'] if args.ya else avance) / 1000:5.2f}  {p['tipo']} de {p['valor']:g} g")
            for c in list(cerrar_en):
                if ahora >= c[0]:
                    cerrar_en.remove(c)
                    luz.update(estado="cerrada", nivel=LUZ_CERRADA)
                    lote.append(evento("luz_cerrada", LUZ_CERRADA, *punto_en(ruta, avance)))
                    print(f"  km {avance / 1000:5.2f}  se CIERRA la carga")
            latir = not args.sin_latido and ahora - ultimo_latido >= 10
            if lote or latir:
                r = mandar(lote, latido=latir, posicion=pos)
                if latir:
                    ultimo_latido = ahora
                if r is not None and lote:
                    print(f"           guardados: {len(r['guardados'])}  ·  alerta de carga abierta: "
                          f"{'SÍ' if r['alerta_carga_abierta'] else 'no'}")
            if not pendientes and not cerrar_en and (args.ya or estado_envio not in ("asignado", "en_ruta")):
                break
            if estado_envio in ("finalizado", "cancelado"):
                print("El envío terminó.")
                break
            time.sleep(1 if cerrar_en and args.ya else 3)
    except KeyboardInterrupt:
        print("\nDetenido.")
    print(f"\nListo. Mire el envío en {args.servidor.rstrip('/')} (Envíos -> {envio['codigo']}).")


if __name__ == "__main__":
    main()
