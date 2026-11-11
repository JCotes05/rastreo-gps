"""
prueba_p3s3.py — P3-S3: prueba de punta a punta los eventos de la carga (ESP32 de sensores).

Necesita P3-S2 funcionando (API + listener), sql/p3s3.sql ejecutado y el servidor
con eventos_carga.py. Debe estar en la misma carpeta que simulador_gps.py.

Uso, en la EC2:
    python3 prueba_p3s3.py                                   API en localhost:8080, listener UDP 5000
    python3 prueba_p3s3.py --api http://localhost:8081 --udp 5001     (servicios de prueba)

Qué hace (tarda menos de 1 minuto):
  1. Usa el camión de prueba ZZZ901 (lo crea si no existe) y le crea un envío corto
     Uninorte -> Buenavista.
  2. Prueba POST /api/camiones/viaje/eventos sin sesión y con datos malos.
  3. Con el envío ASIGNADO: la luz solo se registra (cargue), sin alerta.
  4. Reenvía el mismo uid: no se duplica (idempotencia).
  5. Manda un paquete UDP como la app: el envío pasa a EN RUTA.
  6. Frenada y hueco: quedan ubicados sobre la ruta (avance_m) en el km correcto.
  7. Luz en plena ruta: se abre la alerta "carga_abierta" (una sola); al cerrarse, se cierra.
     El latido del ESP también la abre y la cierra, y actualiza "en línea".
  8. Llega al destino (UDP): la luz en EN DESTINO solo se registra (entrega).
  9. El JSON del envío (página y app) trae los eventos, el conteo y el estado de la carga.
 10. Finaliza el viaje: los eventos que la app tenía en cola todavía se aceptan con id_envio,
     pero sin id_envio da 409 (ya no hay viaje).
"""

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from simulador_gps import punto_en

CLAVE = "pruebaOrbita1"
UNINORTE = {"lat": 11.0190, "lon": -74.8505}
BUENAVISTA = {"lat": 11.0130, "lon": -74.8270}
COLOMBIA = timezone(timedelta(hours=-5))
API = "http://localhost:8080"
resultados = {"ok": 0, "fallo": 0}
SUFIJO = str(int(time.time()))          # uids distintos en cada corrida


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


def seccion(titulo):
    print(f"\n--- {titulo} ---")


def uid(nombre):
    return f"prueba-{SUFIJO}-{nombre}"


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
        sys.exit(f"No pude preparar el camión ZZZ901: {estado} {r}")
    token = r["token"]
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
    print(f"API {API}  ·  listener en 127.0.0.1:{args.udp}")

    token = preparar_camion()
    envio = {
        "cliente_nombre": "Cliente de prueba S.A.S.", "cliente_nit": "900123456-7",
        "origen_direccion": "Universidad del Norte", "origen": UNINORTE,
        "cargue_contacto": "Contacto de prueba", "cargue_celular": "3001000010",
        "destino_direccion": "Buenavista", "destino": BUENAVISTA,
        "destinatario_nombre": "Destinatario de prueba", "destinatario_celular": "3001000011",
        "tipo_carga": "general", "descripcion": "Cajas de prueba (sensores)", "empaque": "cajas", "cantidad": 5,
        "peso_kg": 800, "placa": "ZZZ901", "cargue_programado": datetime.now(COLOMBIA).strftime("%Y-%m-%dT%H:%M"),
    }
    seccion("Preparación")
    estado, r = pedir("POST", "/api/envios", envio)
    if not revisar("Crea un envío Uninorte -> Buenavista para ZZZ901", estado == 201, f"{estado} {r}"):
        sys.exit(1)
    id_envio = r["envio"]["id"]
    revisar("El JSON del envío trae la clave \"sensores\" (p3s3.sql ejecutado)",
            isinstance(r["envio"].get("sensores"), dict), r["envio"].get("sensores"))
    _, r = pedir("GET", f"/api/envios/{id_envio}")
    ruta = r["envio"]["ruta"]
    total = ruta["dist_m"][-1]
    print(f"         ({r['envio']['codigo']}: {total / 1000:.2f} km)")

    def eventos(lista, latido=None, con_token=True, **extra):
        cuerpo = {"eventos": lista, **extra}
        if latido is not None:
            cuerpo["latido"] = latido
        return pedir("POST", "/api/camiones/viaje/eventos", cuerpo, token if con_token else None)

    def detalle():
        return pedir("GET", f"/api/envios/{id_envio}?ruta=0")[1]["envio"]

    def alertas_carga(e):
        return [a for a in e["alertas"] if a["tipo"] == "carga_abierta"]

    seccion("Sin sesión y datos malos")
    estado, r = eventos([{"uid": uid("x"), "tipo": "hueco", "valor": 1.0}], con_token=False)
    revisar("Sin token responde 401", estado == 401, f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/viaje/eventos", {"eventos": "no es lista"}, token)
    revisar("eventos que no es lista responde 400", estado == 400, f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/viaje/eventos", {"eventos": []}, token)
    revisar("Sin eventos ni latido responde 400", estado == 400, f"{estado} {r}")
    estado, r = eventos([{"uid": uid("malo1"), "tipo": "vuelco", "valor": 2},
                         {"uid": "con espacios", "tipo": "hueco", "valor": 1},
                         {"uid": uid("malo3"), "tipo": "frenada", "valor": 99}])
    revisar("Rechaza tipo inexistente, uid inválido y valor fuera de rango (sin guardar nada)",
            estado == 200 and len(r.get("rechazados", [])) == 3 and not r.get("guardados"), f"{estado} {r}")
    estado, r = eventos([{"uid": uid("ajeno"), "tipo": "hueco", "valor": 1}], id_envio=999999999)
    revisar("Un envío que no es de este camión responde 404", estado == 404, f"{estado} {r}")

    seccion("Asignado (cargue): la luz solo se registra")
    estado, r = eventos([{"uid": uid("luz-cargue"), "tipo": "luz_abierta", "valor": 800, **UNINORTE}])
    revisar("Guarda luz_abierta en el cargue", estado == 200 and r.get("guardados") == [uid("luz-cargue")], f"{estado} {r}")
    revisar("En el cargue NO abre alerta", r.get("alerta_carga_abierta") is False, r)
    estado, r = eventos([{"uid": uid("luz-cargue"), "tipo": "luz_abierta", "valor": 800, **UNINORTE}])
    revisar("Idempotencia: reenviar el mismo uid no lo duplica (queda en 'repetidos')",
            estado == 200 and r.get("guardados") == [] and r.get("repetidos") == [uid("luz-cargue")], f"{estado} {r}")
    estado, r = eventos([{"uid": uid("luz-cargue-fin"), "tipo": "luz_cerrada", "valor": 30, **UNINORTE}])
    revisar("Guarda luz_cerrada al terminar el cargue", estado == 200 and r.get("guardados"), f"{estado} {r}")
    revisar("Solo hay 2 eventos de luz (no se duplicó)",
            r["sensores"]["conteo"]["luz_abierta"] == 1 and r["sensores"]["conteo"]["luz_cerrada"] == 1, r["sensores"]["conteo"])

    seccion("Sale: en ruta")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recorrido = str(int(time.time() * 1000))

    def paquete(lat, lon):
        hora = datetime.now(COLOMBIA).strftime("%H:%M:%S")
        sock.sendto(f"LAT:{lat:.5f},LON:{lon:.5f},HORA:{hora},RECORRIDO:{recorrido},ENVIO:{id_envio},VEL:30.0"
                    .encode("utf-8"), ("127.0.0.1", args.udp))

    paquete(*punto_en(ruta, 0))
    time.sleep(2)
    e = detalle()
    if not revisar("Con el primer paquete UDP el envío pasa a 'en_ruta'", e["estado"] == "en_ruta", e["estado"]):
        print("         (¿está corriendo el listener en ese puerto UDP?)")
        sys.exit(1)
    time.sleep(1)       # los eventos pasan DESPUÉS de la salida

    seccion("Frenada y hueco ubicados sobre la ruta")
    mitad = total / 2
    lat, lon = punto_en(ruta, mitad)
    estado, r = eventos([{"uid": uid("frenada"), "tipo": "frenada", "valor": 0.62, "lat": lat, "lon": lon,
                          "momento": datetime.now(COLOMBIA).isoformat(timespec="seconds")}])
    revisar("Guarda la frenada", estado == 200 and r.get("guardados") == [uid("frenada")], f"{estado} {r}")
    ev = next((x for x in r["sensores"]["eventos"] if x["tipo"] == "frenada"), None)
    revisar(f"La frenada queda en el km {mitad / 1000:.2f} de la ruta (±150 m)",
            ev and ev["avance_m"] is not None and abs(ev["avance_m"] - mitad) <= 150, ev)
    revisar("Guarda la intensidad en g", ev and abs(ev["valor"] - 0.62) < 0.01, ev)
    lat3, lon3 = punto_en(ruta, total * 0.75)
    estado, r = eventos([{"uid": uid("hueco"), "tipo": "hueco", "valor": 1.1}], posicion={"lat": lat3, "lon": lon3})
    ev = next((x for x in r.get("sensores", {}).get("eventos", []) if x["tipo"] == "hueco"), None)
    revisar("Un hueco sin GPS propio toma la 'posicion' del lote y queda en el km 3/4",
            estado == 200 and ev and ev["lat"] is not None and abs(ev["avance_m"] - total * 0.75) <= 150, f"{estado} {ev}")
    estado, r = eventos([{"uid": uid("curva"), "tipo": "curva", "valor": 0.48, "lat": lat3, "lon": lon3}])
    revisar("Guarda una curva brusca", estado == 200 and r.get("guardados") == [uid("curva")], f"{estado} {r}")

    seccion("Carga abierta en plena ruta")
    estado, r = eventos([{"uid": uid("luz-ruta"), "tipo": "luz_abierta", "valor": 812, "lat": lat, "lon": lon}])
    revisar("Luz en ruta: abre la alerta carga_abierta", estado == 200 and r.get("alerta_carga_abierta") is True, f"{estado} {r}")
    estado, r = eventos([{"uid": uid("luz-ruta-2"), "tipo": "luz_abierta", "valor": 950, "lat": lat, "lon": lon}])
    e = detalle()
    activas = [a for a in alertas_carga(e) if a["activa"]]
    revisar("Otra luz con la alerta abierta no crea una segunda alerta", len(activas) == 1, alertas_carga(e))
    revisar("La alerta guarda el nivel de luz más alto (950)", activas and activas[0]["valor_max"] == 950, activas)
    revisar("La alerta queda ubicada donde se abrió la carga", activas and activas[0]["lat"] is not None, activas)
    revisar("El estado de la carga en el JSON es 'abierta'", e["sensores"]["carga"]["estado"] == "abierta", e["sensores"]["carga"])
    estado, r = pedir("GET", "/api/envios?estado=activos&limite=200")
    fila = next((x for x in r.get("envios", []) if x["id"] == id_envio), {})
    revisar("La lista de envíos cuenta la alerta (insignia de la página)", fila.get("alertas_activas", 0) >= 1, fila)
    estado, r = eventos([{"uid": uid("luz-ruta-fin"), "tipo": "luz_cerrada", "valor": 20, "lat": lat, "lon": lon}])
    revisar("Luz cerrada: se cierra la alerta", estado == 200 and r.get("alerta_carga_abierta") is False, f"{estado} {r}")
    e = detalle()
    revisar("La alerta cerrada queda con hora de fin", all(not a["activa"] and a["fin"] for a in alertas_carga(e)), alertas_carga(e))

    seccion("Latido del ESP")
    estado, r = eventos([], latido={"luz": "cerrada", "nivel": 25})
    carga = r.get("sensores", {}).get("carga", {})
    revisar("El latido actualiza el último contacto: sensores 'en línea'", estado == 200 and carga.get("en_linea") is True, f"{estado} {carga}")
    estado, r = eventos([], latido={"luz": "abierta", "nivel": 700}, posicion={"lat": lat, "lon": lon})
    revisar("Un latido con la carga abierta en ruta también abre la alerta", r.get("alerta_carga_abierta") is True, r)
    estado, r = eventos([], latido={"luz": "cerrada", "nivel": 22})
    revisar("Y un latido con la carga cerrada la cierra", r.get("alerta_carga_abierta") is False, r)
    revisar("El estado de la carga vuelve a 'cerrada' (nivel 22)",
            r["sensores"]["carga"]["estado"] == "cerrada" and r["sensores"]["carga"]["nivel"] == 22, r["sensores"]["carga"])

    seccion("Llega al destino: la luz de la entrega solo se registra")
    paquete(*punto_en(ruta, total / 2))
    time.sleep(1)
    paquete(*punto_en(ruta, total))
    time.sleep(2)
    e = detalle()
    revisar("El envío llega: 'en_destino'", e["estado"] == "en_destino", e["estado"])
    time.sleep(1)
    estado, r = eventos([{"uid": uid("luz-entrega"), "tipo": "luz_abierta", "valor": 820, **BUENAVISTA}])
    revisar("Luz en la entrega: se guarda sin alerta", estado == 200 and r.get("guardados") and r.get("alerta_carga_abierta") is False,
            f"{estado} {r}")

    seccion("JSON del envío (página y app)")
    e = detalle()
    s = e.get("sensores") or {}
    tipos = [x["tipo"] for x in s.get("eventos", [])]
    revisar("Trae la lista de eventos en orden de tiempo",
            tipos[:3] == ["luz_abierta", "luz_cerrada", "frenada"], tipos)
    c = s.get("conteo", {})
    revisar("Conteo: 1 frenada, 1 hueco, 1 curva, 4 luz abierta, 2 luz cerrada",
            (c.get("frenada"), c.get("hueco"), c.get("curva"), c.get("luz_abierta"), c.get("luz_cerrada")) == (1, 1, 1, 4, 2), c)
    revisar("Cada evento trae tipo, valor, lat, lon, avance_m y momento",
            all(set(x) >= {"tipo", "valor", "lat", "lon", "avance_m", "momento"} for x in s.get("eventos", [])))
    revisar("Hubo 2 alertas de carga abierta, ninguna activa",
            len(alertas_carga(e)) == 2 and not any(a["activa"] for a in alertas_carga(e)), alertas_carga(e))
    _, r = pedir("GET", "/api/camiones/viaje", token=token)
    revisar("La app también recibe los sensores en /api/camiones/viaje",
            (r.get("viaje") or {}).get("sensores", {}).get("conteo", {}).get("frenada") == 1, r.get("viaje", {}).get("sensores"))
    _, datos = pedir("GET", "/datos")
    if (datos.get("envio") or {}).get("id") == id_envio:
        revisar("/datos (Tiempo real) trae los sensores del envío", "sensores" in datos["envio"])

    seccion("Finalizar y cola atrasada")
    estado, r = pedir("POST", "/api/camiones/viaje/finalizar", {}, token)
    revisar("El conductor finaliza el viaje", estado == 200, f"{estado} {r}")
    estado, r = eventos([{"uid": uid("tarde"), "tipo": "hueco", "valor": 0.9, "lat": lat3, "lon": lon3}])
    revisar("Sin id_envio y sin viaje abierto responde 409", estado == 409, f"{estado} {r}")
    estado, r = eventos([{"uid": uid("tarde"), "tipo": "hueco", "valor": 0.9, "lat": lat3, "lon": lon3}], id_envio=id_envio)
    revisar("Con id_envio se acepta un evento que estaba en cola (viaje ya finalizado)",
            estado == 200 and r.get("guardados") == [uid("tarde")], f"{estado} {r}")
    estado, r = eventos([{"uid": uid("tarde-luz"), "tipo": "luz_abierta", "valor": 900}], id_envio=id_envio)
    revisar("...y una luz atrasada de un viaje finalizado no abre alerta", r.get("alerta_carga_abierta") is False, r)
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
