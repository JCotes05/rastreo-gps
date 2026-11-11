"""
prueba_rostros.py — P3-S3 (esquema híbrido): prueba la API que reconoce al conductor.

    POST /api/camiones/conductor/identificar   {"rostros": ["<jpg base64>", ...]}

Uso (en la EC2 o en el PC; solo usa la librería estándar):
    python3 prueba_rostros.py                                  contra http://localhost:8080
    python3 prueba_rostros.py --url https://jesuscotes.sytes.net
    python3 prueba_rostros.py --url ... --fotos CARPETA --cedula 1001000001
        además prueba que reconoce al conductor con 2-3 recortes reales del ESP32-CAM
        (los de capturar_esp.py). Esas fotos son datos biométricos: no se suben a GitHub.

Usa el camión de prueba ZZZ905 (lo registra si no existe). Los intentos quedan en la tabla
identificaciones; se borran con limpiar_pruebas_p3s2.sql (usuario maestro).
Tarda ~35 s: espera a que pase el bloqueo de 30 s tras 2 intentos fallidos.
"""

import argparse
import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

PLACA, CLAVE = "ZZZ905", "pruebaOrbita1"
UN_ANO = (date.today() + timedelta(days=365)).isoformat()
CAMION = {"placa": PLACA, "clave": CLAVE, "configuracion": "C2", "carroceria": "furgon", "capacidad_kg": 4500,
          "marca": "Chevrolet", "linea": "NPR", "modelo": 2019, "color": "Blanco",
          "propietario_nombre": "Dueño de prueba", "propietario_documento": "1001000009",
          "propietario_celular": "3001000009", "soat_vence": UN_ANO, "tecnomecanica_vence": UN_ANO}
# JPEG de 64x64 sin ningún rostro (un cuadro de color sobre gris).
SIN_ROSTRO = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5a"
    "YVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAAR"
    "CABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEG"
    "E1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEB"
    "AQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0"
    "tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwAooqxFEjRgkc/WonNQV2XCDm7Ir0Vb8mP+"
    "7+tHkx/3f1rL6xE0+ryKlFW/Jj/u/rR5Mf8Ad/Wj6xEPq8ipRVp4owjELyB61VrWFRT2M503DcKtwf6laqVbg/1K1liPhNMP8RJRRRXGdgUU"
    "UUANk/1bfQ1Sq7J/q2+hqlXXhtmcuI3QVbg/1K1Uq3B/qVp4j4ScP8RJRRRXGdgUUUUANk/1bfQ1Sq7J/q2+hqlXXhtmcuI3QVahdREAWAP1"
    "qrRW1SHOrGNOfI7l3en99fzo3p/fX86pUVj9WXc2+sPsXd6f31/Ojen99fzqlRR9WXcPrD7Ft3Uo2GXoe9VKKK1p01BGVSo5s//Z"
)
RUTA = "/api/camiones/conductor/identificar"
URL = "http://localhost:8080"
fallos = 0


def pedir(metodo, ruta, datos=None, token=None):
    cuerpo = None if datos is None else json.dumps(datos).encode()
    pedido = urllib.request.Request(URL + ruta, data=cuerpo, method=metodo)
    pedido.add_header("Content-Type", "application/json")
    if token:
        pedido.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(pedido, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def revisar(nombre, condicion, detalle=""):
    global fallos
    if condicion:
        print(f"  OK     {nombre}")
    else:
        fallos += 1
        print(f"  FALLO  {nombre}   -> {detalle}")
    return condicion


def main():
    global URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL)
    parser.add_argument("--fotos", help="Carpeta con recortes reales del ESP32-CAM de un conductor registrado")
    parser.add_argument("--cedula", help="Cédula que se espera reconocer con --fotos")
    args = parser.parse_args()
    URL = args.url.rstrip("/")

    print(f"\nProbando {URL}{RUTA}\n")
    estado, r = pedir("POST", "/api/camiones/login", {"placa": PLACA, "clave": CLAVE})
    if estado != 200:
        estado, r = pedir("POST", "/api/camiones/registro", CAMION)
    token = r.get("token")
    if not revisar(f"Sesión del camión de prueba {PLACA}", bool(token), f"{estado} {r}"):
        return 1

    print("\n--- Validaciones ---")
    estado, r = pedir("POST", RUTA, {"rostros": [SIN_ROSTRO]})
    revisar("Sin sesión responde 401", estado == 401, f"{estado} {r}")
    estado, r = pedir("POST", RUTA, {}, token)
    revisar("Sin rostros responde 400 (campo rostros)", estado == 400 and r.get("campo") == "rostros", f"{estado} {r}")
    estado, r = pedir("POST", RUTA, {"rostros": ["esto no es base64!!"]}, token)
    revisar("Base64 inválido responde 400", estado == 400, f"{estado} {r}")
    estado, r = pedir("POST", RUTA, {"rostros": [base64.b64encode(b"no soy un jpeg").decode()]}, token)
    revisar("Algo que no es JPEG responde 400", estado == 400, f"{estado} {r}")
    estado, r = pedir("POST", RUTA, {"rostros": [SIN_ROSTRO] * 6}, token)
    revisar("Más de 5 rostros responde 400", estado == 400, f"{estado} {r}")

    print("\n--- Sin rostro y bloqueo por intentos fallidos ---")
    estado, r = pedir("POST", RUTA, {"rostros": [SIN_ROSTRO] * 3}, token)
    if estado == 503:
        revisar("El servidor tiene el reconocimiento instalado (OpenCV, modelos y tablas)", False, r.get("error"))
        return 1
    if estado == 429:
        print("  (el camión estaba en espera por una prueba anterior; se esperan 31 s)")
        time.sleep(31)
        estado, r = pedir("POST", RUTA, {"rostros": [SIN_ROSTRO] * 3}, token)
    revisar("Imágenes sin rostro: 200 no_reconocido con 0 rostros", estado == 200 and r.get("estado") == "no_reconocido"
            and r.get("con_rostro") == 0, f"{estado} {r}")
    revisar("La respuesta no trae datos internos (similitudes)", "similitudes" not in r, r)
    estado, r = pedir("POST", RUTA, {"rostros": [SIN_ROSTRO] * 2}, token)
    revisar("Segundo intento fallido: no_reconocido", estado == 200 and r.get("estado") == "no_reconocido", f"{estado} {r}")
    estado, r = pedir("POST", RUTA, {"rostros": [SIN_ROSTRO] * 2}, token)
    revisar("Tercer intento: 429, debe esperar", estado == 429 and "Espere" in r.get("error", ""), f"{estado} {r}")

    if args.fotos:
        print("\n--- Conductor real (recortes del ESP32-CAM) ---")
        fotos = sorted(glob.glob(os.path.join(args.fotos, "*.jpg")))[:3]
        rostros = [base64.b64encode(open(f, "rb").read()).decode() for f in fotos]
        print("  (esperando 31 s a que termine el bloqueo...)")
        time.sleep(31)
        t0 = time.time()
        estado, r = pedir("POST", RUTA, {"rostros": rostros}, token)
        segundos = time.time() - t0
        revisar(f"Reconoce al conductor con {len(rostros)} recortes ({segundos:.1f} s)",
                estado == 200 and r.get("estado") == "reconocido"
                and (not args.cedula or r.get("cedula") == args.cedula), f"{estado} {r}")
        if r.get("estado") == "reconocido":
            print(f"         similitud {r.get('similitud')}")
        estado, r = pedir("POST", RUTA, {"rostros": rostros[:1]}, token)
        revisar("Con un solo recorte no acepta (se piden al menos 2)", estado == 200 and r.get("estado") == "no_reconocido",
                f"{estado} {r}")

    print("\n" + ("TODO BIEN: todas las pruebas pasaron." if fallos == 0 else f"ATENCIÓN: {fallos} prueba(s) fallaron."))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
