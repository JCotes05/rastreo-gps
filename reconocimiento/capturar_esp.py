"""
capturar_esp.py — Toma fotos del rostro del conductor CON EL ESP32-CAM (P3-S3).

El ESP32-CAM detecta el rostro, lo recorta y lo manda por el cable USB. Este programa
guarda esos recortes en reconocimiento/fotos_esp/<cedula>/ para registrarlo después con
registrar.py --fotos. Así el conductor queda registrado con la MISMA cámara con la que
se le va a reconocer en la cabina (sale mejor que registrarlo con la cámara del portátil).

    py capturar_esp.py --puerto COM6 --cedula 1001000001
    py registrar.py --cedula 1001000001 --nombre "Jesus Cotes" --fotos fotos_esp\\1001000001

Opciones: --cantidad 10 (fotos a tomar), --probar (solo muestra si el ESP ve el rostro).

Las fotos son datos biométricos: la carpeta fotos_esp/ NO se sube a GitHub (.gitignore).
Después de registrar se pueden borrar.
"""

import argparse
import base64
import os
import sys
import time

try:
    import serial
except ImportError:
    print("\n[ERROR] Falta la librería pyserial. Instálala con:  py -m pip install pyserial\n")
    sys.exit(1)

CARPETA = os.path.dirname(os.path.abspath(__file__))


def abrir(puerto):
    """Abre el puerto SIN reiniciar la placa (DTR y RTS quedan quietos)."""
    conexion = serial.Serial()
    conexion.port, conexion.baudrate, conexion.timeout = puerto, 115200, 0.2
    conexion.dtr = False
    conexion.rts = False
    try:
        conexion.open()
    except serial.SerialException as e:
        print(f"\n[ERROR] No se pudo abrir {puerto}: {e}\n"
              "Revisa el número del puerto (Administrador de dispositivos > Puertos) y que el\n"
              "monitor serie de Arduino esté cerrado.\n")
        sys.exit(1)
    time.sleep(0.3)
    conexion.reset_input_buffer()
    return conexion


def lineas(conexion, segundos):
    """Entrega las líneas que manda la placa durante 'segundos' como máximo."""
    pendiente = b""
    limite = time.time() + segundos
    while time.time() < limite:
        pendiente += conexion.read(conexion.in_waiting or 1)
        while b"\n" in pendiente:
            crudo, pendiente = pendiente.split(b"\n", 1)
            yield crudo.decode("utf-8", "replace").strip()


def campos(linea):
    """'ROSTRO;N:1;JPG:abc' -> ('ROSTRO', {'N': '1', 'JPG': 'abc'})"""
    trozos = linea.split(";")
    datos = {}
    for t in trozos[1:]:
        if ":" in t:
            clave, valor = t.split(":", 1)
            datos[clave.strip().upper()] = valor.strip()
    return trozos[0].strip().upper(), datos


def probar(conexion):
    conexion.write(b"PROBAR\n")
    print("Mirando 10 s: ponte frente a la cámara...\n")
    for linea in lineas(conexion, 15):
        tipo, d = campos(linea)
        if tipo == "PRUEBA":
            if "ANCHO" in d:
                print(f"  rostro de {d['ANCHO']} px  {'(sirve)' if d.get('SIRVE') == 'SI' else '(muy lejos)'}")
            else:
                print("  sin rostro")
        elif tipo == "FIN_PRUEBA":
            print(f"\nCuadros con rostro: {d.get('CON_ROSTRO')} de {d.get('CUADROS')}.")
            return


def capturar(conexion, cedula, cantidad):
    destino = os.path.join(CARPETA, "fotos_esp", cedula)
    os.makedirs(destino, exist_ok=True)
    conexion.write(f"CAPTURAR;N:{cantidad}\n".encode())
    print(f"\nMira a la cámara del ESP32-CAM y mueve un poco la cabeza (hasta 60 s)...\n")
    guardadas = 0
    sello = time.strftime("%Y%m%d_%H%M%S")
    for linea in lineas(conexion, 75):
        tipo, d = campos(linea)
        if tipo == "ROSTRO" and d.get("JPG"):
            try:
                jpg = base64.b64decode(d["JPG"], validate=True)
            except ValueError:
                print("  (llegó una foto dañada; se descarta)")
                continue
            guardadas += 1
            ruta = os.path.join(destino, f"esp_{sello}_{guardadas:02d}.jpg")
            with open(ruta, "wb") as f:
                f.write(jpg)
            print(f"  foto {guardadas}/{cantidad}  (rostro de {d.get('ANCHO', '?')} px)")
        elif tipo == "ERROR":
            print(f"\n[ERROR] El ESP32-CAM respondió: {d.get('MOTIVO', linea)}")
            break
        elif tipo == "FIN_ROSTROS":
            break
    print(f"\nSe guardaron {guardadas} fotos en:\n    {destino}")
    if guardadas >= 3:
        print("\nAhora regístralo con:")
        print(f'    py registrar.py --cedula {cedula} --nombre "Nombre Apellido" --fotos "{destino}"')
    else:
        print("\nMuy pocas fotos. Acércate (unos 50 cm), ponte de frente y con luz, y repite.")


def main():
    parser = argparse.ArgumentParser(description="Toma fotos del rostro con el ESP32-CAM para registrar al conductor.")
    parser.add_argument("--puerto", required=True, help="Puerto COM del ESP32-CAM, por ejemplo COM6")
    parser.add_argument("--cedula", help="Cédula del conductor (la misma de la tabla conductores)")
    parser.add_argument("--cantidad", type=int, default=10, help="Fotos a tomar (3 a 20)")
    parser.add_argument("--probar", action="store_true", help="Solo revisar si el ESP ve el rostro")
    args = parser.parse_args()

    conexion = abrir(args.puerto)
    try:
        if args.probar:
            probar(conexion)
            return
        if not args.cedula or not args.cedula.isdigit() or not 5 <= len(args.cedula) <= 15:
            print("\n[ERROR] Indica la cédula (solo números, 5 a 15 dígitos): --cedula 1001000001\n")
            sys.exit(1)
        if not 3 <= args.cantidad <= 20:
            print("\n[ERROR] --cantidad va de 3 a 20.\n")
            sys.exit(1)
        capturar(conexion, args.cedula, args.cantidad)
    finally:
        conexion.close()


if __name__ == "__main__":
    main()
