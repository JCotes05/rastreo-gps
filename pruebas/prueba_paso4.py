"""
prueba_paso4.py — P3-S2, paso 4: prueba de punta a punta la vigilancia del viaje en el listener.

Necesita el paso 3 funcionando (la API) y el listener con los cambios del paso 4.
Debe estar en la misma carpeta que simulador_gps.py.

Uso, en la EC2:
    python3 prueba_paso4.py                         API en localhost:8080, listener en el puerto UDP 5000
    python3 prueba_paso4.py --api http://localhost:8081 --udp 5001     (servicios de prueba)

Qué hace (tarda unos 3 minutos):
  1. Usa el camión de prueba ZZZ901 (lo crea si no existe) y lo pone disponible en Uninorte.
  2. Crea un envío corto Uninorte -> Buenavista con la ruta real.
  3. Manda paquetes UDP como la app (uno por segundo, al doble de la velocidad planeada),
     con un desvío de 300 m (30 s) a la mitad del camino.
  4. Revisa con la API: salida, puntos de control, alerta de desvío abierta y cerrada,
     km recorridos y llegada.
  5. Finaliza el viaje y deja el camión no disponible.
"""

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from simulador_gps import desplazar, distancia_en_tiempo, punto_en

CLAVE = "pruebaOrbita1"
UNINORTE = {"lat": 11.0190, "lon": -74.8505}
BUENAVISTA = {"lat": 11.0130, "lon": -74.8270}
COLOMBIA = timezone(timedelta(hours=-5))
API = "http://localhost:8080"
resultados = {"ok": 0, "fallo": 0}


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


def revisar(nombre, condicion, detalle=""):
    resultados["ok" if condicion else "fallo"] += 1
    print(f"  {'OK   ' if condicion else 'FALLO'}  {nombre}" + ("" if condicion or not detalle else f"  ->  {detalle}"))
    return condicion


def preparar_camion():
    un_ano = (date.today() + timedelta(days=365)).isoformat()
    datos = {"placa": "ZZZ901", "clave": CLAVE, "configuracion": "C2", "carroceria": "furgon", "capacidad_kg": 4500,
             "marca": "Chevrolet", "linea": "NPR", "modelo": 2019, "color": "Blanco",
             "propietario_nombre": "Dueño de prueba", "propietario_documento": "1001000009",
             "propietario_celular": "3001000009", "soat_vence": un_ano, "tecnomecanica_vence": un_ano}
    estado, r = pedir("POST", "/api/camiones/registro", datos)
    if estado == 409:
        estado, r = pedir("POST", "/api/camiones/login", {"placa": "ZZZ901", "clave": CLAVE})
    if estado not in (200, 201):
        sys.exit(f"No pude preparar el camión ZZZ901: {estado} {r}. ¿Está desplegado el paso 3?")
    token = r["token"]
    # Si quedó con un viaje de una prueba anterior, se cierra
    _, r = pedir("GET", "/api/camiones/viaje", token=token)
    viaje = r.get("viaje")
    if viaje and viaje["estado"] == "asignado":
        pedir("POST", f"/api/envios/{viaje['id']}/cancelar", {})
    elif viaje:
        pedir("POST", "/api/camiones/viaje/finalizar", {}, token)
    estado, r = pedir("POST", "/api/camiones/disponibilidad", {"disponible": True, **UNINORTE}, token)
    if estado != 200:
        sys.exit(f"No pude poner disponible a ZZZ901: {estado} {r}")
    return token


def main():
    global API
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=API)
    parser.add_argument("--udp", type=int, default=5000, help="puerto UDP del listener (en esta misma máquina)")
    args = parser.parse_args()
    API = args.api.rstrip("/")
    print(f"API {API}  ·  listener en 127.0.0.1:{args.udp}\n")

    token = preparar_camion()
    envio = {
        "cliente_nombre": "Cliente de prueba S.A.S.", "cliente_nit": "900123456-7",
        "origen_direccion": "Universidad del Norte", "origen": UNINORTE,
        "cargue_contacto": "Contacto de prueba", "cargue_celular": "3001000010",
        "destino_direccion": "Buenavista", "destino": BUENAVISTA,
        "destinatario_nombre": "Destinatario de prueba", "destinatario_celular": "3001000011",
        "tipo_carga": "general", "descripcion": "Cajas de prueba", "empaque": "cajas", "cantidad": 5,
        "peso_kg": 800, "placa": "ZZZ901",
        "cargue_programado": datetime.now(COLOMBIA).strftime("%Y-%m-%dT%H:%M"),
    }
    estado, r = pedir("POST", "/api/envios", envio)
    if not revisar("Crea un envío Uninorte -> Buenavista para ZZZ901", estado == 201, f"{estado} {r}"):
        sys.exit(1)
    id_envio = r["envio"]["id"]
    _, r = pedir("GET", f"/api/envios/{id_envio}")
    ruta = r["envio"]["ruta"]
    total, plan = ruta["dist_m"][-1], ruta["tiempo_s"][-1]
    print(f"         ({r['envio']['codigo']}: {total / 1000:.2f} km, {plan / 60:.1f} min planeados,"
          f" {len(r['envio']['puntos_control']) - 2} puntos de control)")

    # --- Manejar: 1 paquete por segundo, al doble del ritmo planeado, desvío de 300 m a mitad de camino
    print("\nManejando (unos minutos)...")
    ritmo, cada = 2.0, 1.0
    recorrido = str(int(time.time() * 1000))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t_plan, d, enviados, desvio_t = 0.0, 0.0, 0, None
    llegada_extra = 5
    while True:
        t_plan += ritmo * cada
        d = distancia_en_tiempo(ruta, t_plan)
        lat, lon = punto_en(ruta, d)
        if d >= total * 0.5 and desvio_t is None:
            desvio_t = 0
        if desvio_t is not None and desvio_t < 30:          # 30 s fuera: sale y vuelve en 10 s cada vez (como un carro real)
            lateral = 300 * min(1.0, (desvio_t + 1) / 10, (30 - desvio_t) / 10)
            lat, lon = desplazar(ruta, d, lateral)
            desvio_t += 1
        hora = datetime.now(COLOMBIA).strftime("%H:%M:%S")
        mensaje = f"LAT:{lat:.5f},LON:{lon:.5f},HORA:{hora},RECORRIDO:{recorrido},ENVIO:{id_envio},VEL:40.0"
        sock.sendto(mensaje.encode("utf-8"), ("127.0.0.1", args.udp))
        enviados += 1
        if enviados == 3:
            time.sleep(1.5)
            _, r = pedir("GET", f"/api/envios/{id_envio}?ruta=0")
            revisar("Con el primer paquete el envío pasa a 'en_ruta' y queda enlazado al recorrido",
                    r["envio"]["estado"] == "en_ruta" and r["envio"]["progreso"]["id_recorrido"] == recorrido,
                    f"{r['envio']['estado']} {r['envio']['progreso']['id_recorrido']}")
        if enviados % 20 == 0:
            print(f"  km {d / 1000:.2f} de {total / 1000:.2f}" + ("  (desviado)" if desvio_t is not None and desvio_t < 30 else ""))
        if d >= total:
            llegada_extra -= 1
            if llegada_extra == 0:
                break
        time.sleep(cada)
    time.sleep(2)

    print("\nResultados:")
    _, r = pedir("GET", f"/api/envios/{id_envio}?ruta=0")
    e = r["envio"]
    p = e["progreso"]
    revisar("El envío llegó: estado 'en_destino'", e["estado"] == "en_destino", e["estado"])
    revisar("Tiene hora real de salida y de llegada", p["salida_real"] and p["llegada_real"], p)
    pasados = [x for x in e["puntos_control"] if x["hora_real"]]
    revisar("Todos los puntos de control quedaron con hora real",
            len(pasados) == len(e["puntos_control"]), [(x["orden"], x["hora_real"]) for x in e["puntos_control"]])
    revisar("Iba adelantado (doble de rápido): sin retraso", all(x["retraso_s"] <= 0 for x in pasados),
            [x["retraso_s"] for x in pasados])
    desvios = [a for a in e["alertas"] if a["tipo"] == "desvio"]
    revisar("Se abrió una alerta de desvío", len(desvios) == 1, e["alertas"])
    if desvios:
        revisar("La alerta de desvío llegó a unos 300 m", 220 <= desvios[0]["valor_max"] <= 330, desvios[0]["valor_max"])
        revisar("La alerta de desvío se cerró al volver a la ruta", not desvios[0]["activa"], desvios[0])
    revisar("No hubo alerta de retraso", not any(a["tipo"] == "retraso" for a in e["alertas"]), e["alertas"])
    revisar("Contó los km recorridos (entre 60 % y 130 % de la ruta)",
            0.6 * total <= p["recorrido_m"] <= 1.3 * total, f"{p['recorrido_m']} m de {total:.0f} m")
    _, datos = pedir("GET", "/datos")
    revisar("/datos muestra este envío en Tiempo Real", (datos.get("envio") or {}).get("id") == id_envio,
            (datos.get("envio") or {}).get("codigo"))

    estado, r = pedir("POST", "/api/camiones/viaje/finalizar", {}, token)
    revisar("El conductor finaliza el viaje", estado == 200, f"{estado} {r}")
    pedir("POST", "/api/camiones/disponibilidad", {"disponible": False}, token)


if __name__ == "__main__":
    try:
        main()
    finally:
        print("\n" + "-" * 50)
        if resultados["fallo"] == 0 and resultados["ok"]:
            print(f"TODO BIEN: {resultados['ok']} pruebas pasaron.")
        else:
            print(f"{resultados['ok']} pasaron y {resultados['fallo']} fallaron.")
