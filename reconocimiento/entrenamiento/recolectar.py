"""
recolectar.py — P3-S4: recolecta el conjunto de datos para entrenar y evaluar el
reconocimiento con la cámara de la cabina (ESP32-CAM con OV3660).

Cada corrida es UNA condición de UNA sesión de UNA persona:

    py recolectar.py --persona P1 --tipo genuino  --sesion 1 --condicion base
    py recolectar.py --persona X3 --tipo impostor --sesion 1 --condicion base

  1. Detección en la placa (10 s, comando PROBAR): cuántos cuadros tienen rostro y cuánto tarda.
  2. Captura (comando CAPTURAR): los mismos recortes que la placa manda en un viaje real.
  3. Para cada recorte: calidad (nitidez, brillo, contraste, tamaño), detección con YuNet
     y la huella de SFace (128 números).

Dónde queda (FUERA del repositorio: son datos biométricos, nunca van a GitHub):
    Rastreo_GPS/datos_entrenamiento/
        imagenes/<persona>/s<sesion>/<condicion>/NN.jpg   solo de los GENUINOS
        muestras.csv     una fila por recorte (calidad, detección; sin la imagen)
        huellas.jsonl    la huella de cada recorte (también la de los impostores)
        deteccion.csv    una fila por corrida (tasa de detección de la placa, tiempos)

Personas: se usan ALIAS (P1, P2, P3 para los conductores; X1, X2... para los impostores).
La equivalencia alias -> nombre NO se guarda aquí.
"""

import argparse
import base64
import csv
import json
import os
import sys
import time
from datetime import datetime

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AQUI))           # reconocimiento/
import capturar_esp as esp                            # noqa: E402
import comun                                          # noqa: E402  (configura OpenCV)
import cv2                                            # noqa: E402
import numpy as np                                    # noqa: E402

DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(AQUI))), "datos_entrenamiento")

CONDICIONES = {
    "base":        "De frente, luz normal de frente, a unos 50 cm, quieto",
    "luz_baja":    "Luz tenue: apaga la luz principal (deja solo una lámpara lejos o la pantalla)",
    "contraluz":   "Con una ventana o lámpara DETRÁS de ti",
    "lateral":     "Luz fuerte de un solo lado (lámpara o ventana a tu izquierda o derecha)",
    "cerca":       "A unos 30 cm de la cámara",
    "lejos":       "A unos 80-90 cm de la cámara",
    "giro":        "Gira la cabeza despacio a izquierda y derecha (hasta unos 30 grados)",
    "inclinacion": "Sube y baja la cara despacio (mira un poco arriba y un poco abajo)",
    "expresiones": "Sonríe, habla, abre la boca, cambia la expresión",
    "gafas":       "Con gafas (de sol o formuladas)",
    "gorra":       "Con gorra o sombrero",
    "movimiento":  "Muévete como si fueras en el camión: balanceo suave y continuo",
}

CAMPOS_MUESTRA = ["id", "fecha", "persona", "tipo", "sesion", "condicion", "n", "archivo", "ancho_esp",
                  "nitidez", "brillo", "contraste", "yunet_ok", "yunet_conf", "ancho_yunet", "segundos"]
CAMPOS_DETECCION = ["fecha", "persona", "tipo", "sesion", "condicion", "cuadros", "con_rostro", "sirve",
                    "ms_promedio", "pedidos", "recibidos", "segundos_captura"]


def agregar_fila(archivo, campos, fila):
    nuevo = not os.path.exists(archivo)
    with open(archivo, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        if nuevo:
            w.writeheader()
        w.writerow(fila)


def calidad(img):
    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return {"nitidez": round(float(cv2.Laplacian(gris, cv2.CV_64F).var()), 1),
            "brillo": round(float(gris.mean()), 1),
            "contraste": round(float(gris.std()), 1)}


def deteccion_en_placa(con):
    con.write(b"PROBAR\n")
    cuadros = con_rostro = sirve = 0
    tiempos = []
    for linea in esp.lineas(con, 16):
        tipo, d = esp.campos(linea)
        if tipo == "PRUEBA":
            cuadros += 1
            if "ANCHO" in d:
                con_rostro += 1
                sirve += d.get("SIRVE") == "SI"
            if d.get("MS", "").isdigit():
                tiempos.append(int(d["MS"]))
        elif tipo == "FIN_PRUEBA":
            break
    return cuadros, con_rostro, sirve, (round(sum(tiempos) / len(tiempos)) if tiempos else 0)


def main():
    p = argparse.ArgumentParser(description="Recolecta datos para entrenar el reconocimiento (ESP32-CAM).")
    p.add_argument("--puerto", default="COM4")
    p.add_argument("--persona", required=True, help="Alias: P1, P2, P3 (conductores) o X1, X2... (impostores)")
    p.add_argument("--tipo", required=True, choices=["genuino", "impostor"])
    p.add_argument("--sesion", required=True, type=int, choices=[1, 2, 3, 4])
    p.add_argument("--condicion", required=True, choices=list(CONDICIONES))
    p.add_argument("--cantidad", type=int, default=20)
    p.add_argument("--espera", type=int, default=5, help="Segundos para acomodarse antes de empezar")
    p.add_argument("--guardar-vacio", action="store_true",
                   help="Guardar la corrida aunque la placa no vea ningún rostro (es un dato: falla en esa condición)")
    a = p.parse_args()
    if not a.persona.isalnum() or len(a.persona) > 6:
        sys.exit("El alias debe ser corto y sin espacios (P1, X2...). No uses nombres.")

    os.makedirs(DATOS, exist_ok=True)
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sello = datetime.now().strftime("%Y%m%d%H%M%S")
    print(f"\n=== {a.persona} ({a.tipo}) · sesión {a.sesion} · {a.condicion} ===")
    print(f"    {CONDICIONES[a.condicion]}")
    con = esp.abrir(a.puerto)
    for k in range(a.espera, 0, -1):
        print(f"    empieza en {k}...", end="\r")
        time.sleep(1)

    print("\n1) Detección en la placa (10 s)...")
    cuadros, con_rostro, sirve, ms = deteccion_en_placa(con)
    print(f"   {con_rostro}/{cuadros} cuadros con rostro, {sirve} de tamaño suficiente, {ms} ms por cuadro")
    if con_rostro == 0 and not a.guardar_vacio:
        con.close()
        sys.exit("\nLa placa no vio ningún rostro: NO se guardó nada. Revisa que la cámara te apunte a la cara\n"
                 "y repite. (Si en esta condición de verdad no se ve, por ejemplo con muy poca luz, repite\n"
                 "con --guardar-vacio para registrar el fallo como dato.)")

    print(f"2) Captura de {a.cantidad} recortes (hasta 60 s)...")
    carpeta = os.path.join(DATOS, "imagenes", a.persona, f"s{a.sesion}", a.condicion)
    if a.tipo == "genuino":
        os.makedirs(carpeta, exist_ok=True)
    motor = comun.MotorFacial()
    con.write(f"CAPTURAR;N:{a.cantidad}\n".encode())
    inicio = time.time()
    recibidos = 0
    for linea in esp.lineas(con, 75):
        tipo, d = esp.campos(linea)
        if tipo == "ROSTRO" and d.get("JPG"):
            try:
                jpg = base64.b64decode(d["JPG"], validate=True)
            except ValueError:
                continue
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            recibidos += 1
            ident = f"{a.persona}_s{a.sesion}_{a.condicion}_{sello}_{recibidos:02d}"
            archivo = ""
            if a.tipo == "genuino":
                archivo = os.path.relpath(os.path.join(carpeta, f"{sello}_{recibidos:02d}.jpg"), DATOS)
                with open(os.path.join(DATOS, archivo), "wb") as f:
                    f.write(jpg)
            q = calidad(img)
            rostros = motor.detectar(img)
            fila = {"id": ident, "fecha": fecha, "persona": a.persona, "tipo": a.tipo, "sesion": a.sesion,
                    "condicion": a.condicion, "n": recibidos, "archivo": archivo, "ancho_esp": d.get("ANCHO", ""),
                    **q, "yunet_ok": int(len(rostros) > 0),
                    "yunet_conf": round(float(rostros[0][-1]), 3) if len(rostros) else "",
                    "ancho_yunet": int(rostros[0][2]) if len(rostros) else "",
                    "segundos": round(time.time() - inicio, 2)}
            agregar_fila(os.path.join(DATOS, "muestras.csv"), CAMPOS_MUESTRA, fila)
            if len(rostros):
                huella = motor.huella(img, rostros[0])
                with open(os.path.join(DATOS, "huellas.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"id": ident, "persona": a.persona, "tipo": a.tipo, "sesion": a.sesion,
                                        "condicion": a.condicion,
                                        "huella": [round(float(v), 6) for v in huella]}) + "\n")
            print(f"   {recibidos:2d}  nitidez {q['nitidez']:6.0f}  brillo {q['brillo']:5.0f}  "
                  f"{'rostro OK' if len(rostros) else 'SFace NO ve rostro'}")
        elif tipo in ("FIN_ROSTROS", "ERROR"):
            if tipo == "ERROR":
                print("   La placa respondió:", d.get("MOTIVO", linea))
            break
    segundos = round(time.time() - inicio, 1)
    con.close()
    agregar_fila(os.path.join(DATOS, "deteccion.csv"), CAMPOS_DETECCION,
                 {"fecha": fecha, "persona": a.persona, "tipo": a.tipo, "sesion": a.sesion, "condicion": a.condicion,
                  "cuadros": cuadros, "con_rostro": con_rostro, "sirve": sirve, "ms_promedio": ms,
                  "pedidos": a.cantidad, "recibidos": recibidos, "segundos_captura": segundos})
    print(f"\nListo: {recibidos}/{a.cantidad} recortes en {segundos} s.")


if __name__ == "__main__":
    main()
