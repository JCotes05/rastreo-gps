"""
demo_flota.py — P3-S4: pone VARIOS camiones de prueba en ruta al mismo tiempo, para ver
la página en Tiempo real con varios vehículos (cada uno con su color).

Usa los camiones de prueba ZZZ901, ZZZ902 y ZZZ903 (los crea si no existen), les crea un
envío a cada uno por Barranquilla y lanza un simulador_gps.py por camión, en paralelo.
Al terminar (o con Ctrl+C) finaliza los viajes para que los camiones queden libres.

Uso (en la EC2, carpeta pruebas/; necesita el usuario "pruebas" de sesion_web.py):
    python3 demo_flota.py --api http://localhost:8080 --udp 127.0.0.1:5000
    python3 demo_flota.py --api http://localhost:8081 --udp 127.0.0.1:5001 --camiones 2 --ritmo 3

Opciones: --camiones N (1 a 3), --ritmo R (más alto = más rápido; 1 = a la velocidad planeada),
          --sin-finalizar (deja los viajes en destino al terminar).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import sesion_web

AQUI = os.path.dirname(os.path.abspath(__file__))
COLOMBIA = timezone(timedelta(hours=-5))
CLAVE = "pruebaOrbita1"
API = "http://localhost:8080"

# placa, carrocería, configuración, capacidad, tipo de carga, origen, destino (Barranquilla)
FLOTA = [
    ("ZZZ901", "furgon", "C2", 4500, "general",
     ("Universidad del Norte", 11.0190, -74.8505), ("Buenavista", 11.0130, -74.8270)),
    ("ZZZ902", "refrigerado", "3S3", 34000, "refrigerada",
     ("Puerto de Barranquilla", 10.9690, -74.7630), ("Centro Comercial Viva", 10.9900, -74.7890)),
    ("ZZZ903", "estacas", "C3", 17000, "general",
     ("Estadio Metropolitano", 10.9269, -74.8000), ("Parque Washington", 11.0010, -74.8080)),
]


def pedir(metodo, ruta, datos=None, token=None):
    cuerpo = json.dumps(datos).encode("utf-8") if datos is not None else (b"" if metodo == "POST" else None)
    pedido = urllib.request.Request(API + ruta, data=cuerpo, method=metodo)
    pedido.add_header("Content-Type", "application/json")
    if token:
        pedido.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(pedido, timeout=90) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {}


def preparar(placa, carroceria, configuracion, capacidad, origen):
    un_ano = (date.today() + timedelta(days=365)).isoformat()
    datos = {"placa": placa, "clave": CLAVE, "configuracion": configuracion, "carroceria": carroceria,
             "capacidad_kg": capacidad, "marca": "Prueba", "linea": "Demo", "modelo": 2020, "color": "Blanco",
             "propietario_nombre": "Dueño de prueba", "propietario_documento": "1001000009",
             "propietario_celular": "3001000009", "soat_vence": un_ano, "tecnomecanica_vence": un_ano}
    estado, r = pedir("POST", "/api/camiones/registro", datos)
    if estado == 409:
        estado, r = pedir("POST", "/api/camiones/login", {"placa": placa, "clave": CLAVE})
    if estado not in (200, 201):
        sys.exit(f"No pude preparar {placa}: {estado} {r}")
    token = r["token"]
    pedir("POST", "/api/camiones/documentos", {"soat_vence": un_ano, "tecnomecanica_vence": un_ano}, token)
    _, r = pedir("GET", "/api/camiones/viaje", token=token)
    viaje = r.get("viaje")
    if viaje and viaje["estado"] == "asignado":
        pedir("POST", f"/api/envios/{viaje['id']}/cancelar", {})
    elif viaje:
        pedir("POST", "/api/camiones/viaje/finalizar", {}, token)
    estado, r = pedir("POST", "/api/camiones/disponibilidad", {"disponible": True, "lat": origen[1], "lon": origen[2]}, token)
    if estado != 200:
        sys.exit(f"No pude poner disponible a {placa}: {estado} {r}")
    return token


def crear_envio(placa, tipo, origen, destino, k):
    envio = {"cliente_nombre": "Cliente de demostración", "cliente_nit": "900123456-7",
             "origen_direccion": origen[0], "origen": {"lat": origen[1], "lon": origen[2]},
             "cargue_contacto": "Contacto de prueba", "cargue_celular": "3001000010",
             "destino_direccion": destino[0], "destino": {"lat": destino[1], "lon": destino[2]},
             "destinatario_nombre": "Destinatario de prueba", "destinatario_celular": "3001000011",
             "tipo_carga": tipo, "descripcion": "Carga de demostración", "empaque": "cajas", "cantidad": 5 + k,
             "peso_kg": 800 + 100 * k, "placa": placa,
             "cargue_programado": datetime.now(COLOMBIA).strftime("%Y-%m-%dT%H:%M")}
    estado, r = pedir("POST", "/api/envios", envio)
    if estado != 201:
        sys.exit(f"No pude crear el envío de {placa}: {estado} {r}")
    return r["envio"]


def main():
    global API
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API)
    ap.add_argument("--udp", default="127.0.0.1:5000", help="listener: HOST:PUERTO")
    ap.add_argument("--camiones", type=int, default=3, choices=[1, 2, 3])
    ap.add_argument("--ritmo", type=float, default=2.0)
    ap.add_argument("--sin-finalizar", action="store_true")
    a = ap.parse_args()
    API = a.api.rstrip("/")
    sesion_web.iniciar(API)

    procesos, tokens = [], {}
    for k, (placa, carroceria, conf, capacidad, tipo, origen, destino) in enumerate(FLOTA[:a.camiones]):
        tokens[placa] = preparar(placa, carroceria, conf, capacidad, origen)
        e = crear_envio(placa, tipo, origen, destino, k)
        print(f"{placa}: {e['codigo']} {origen[0]} -> {destino[0]}")
        procesos.append(subprocess.Popen(
            [sys.executable, os.path.join(AQUI, "simulador_gps.py"), "--servidor", API, "--envio", str(e["id"]),
             "--udp", a.udp, "--ritmo", str(a.ritmo * (1 - 0.15 * k))],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
        time.sleep(3)

    def terminar(*_):
        for p in procesos:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        time.sleep(2)
        if not a.sin_finalizar:
            for placa, token in tokens.items():
                pedir("POST", "/api/camiones/viaje/finalizar", {}, token)
                pedir("POST", "/api/camiones/disponibilidad", {"disponible": False}, token)
            print("Viajes finalizados; camiones de prueba no disponibles.")
        sys.exit(0)

    signal.signal(signal.SIGINT, terminar)
    signal.signal(signal.SIGTERM, terminar)
    print("En ruta. Mire la página en Tiempo real (Ctrl+C para terminar).")
    while any(p.poll() is None for p in procesos):
        time.sleep(2)
    print("Los camiones llegaron.")
    time.sleep(20)                  # unos segundos en destino, visibles en la página
    terminar()


if __name__ == "__main__":
    main()
