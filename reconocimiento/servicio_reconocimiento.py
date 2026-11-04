"""
servicio_reconocimiento.py — Atiende al celular por Bluetooth (P3-S1, paso 6).

El celular (que hace de módem satelital del camión) le pide a este computador
que identifique al conductor. El programa prende la cámara, lo reconoce y le
responde con su cédula y su nombre. El celular es quien luego lo manda al servidor.

    py servicio_reconocimiento.py --listar-puertos      (para encontrar el puerto Bluetooth)
    py servicio_reconocimiento.py --puerto COM6         (el puerto COM "entrante" de Windows)
    py servicio_reconocimiento.py --consola             (prueba sin celular: escribes tú los mensajes)

Opciones: --camara N, --segundos 10, --umbral 0.55, --sin-ventana

Para evitar falsos positivos (P3-S1: un compañero con gafas pasó como Adrián), un
conductor se acepta solo si en 8 cuadros seguidos supera su umbral Y le saca ventaja
al segundo más parecido (ver comun.py). Tras 2 NO_RECONOCIDO seguidos se responde
ERROR durante 30 s, para que insistir hasta que salga una racha con suerte no sirva.

PROTOCOLO — una línea de texto por mensaje (UTF-8, termina en salto de línea):

    Celular -> PC        PC -> Celular
    -------------        -------------------------------------------------------
    PING                 PONG
    IDENTIFICAR          IDENTIFICANDO                                  (de inmediato)
                         CONDUCTOR;CED:1001000001;NOM:Jesús Cotes;SIM:0.82   (reconocido)
                         NO_RECONOCIDO;SIM:0.21      (nadie superó el umbral en el tiempo límite)
                         ERROR;MOTIVO:...            (por ejemplo, la cámara no abre)
    (otra cosa)          ERROR;MOTIVO:comando desconocido

El celular debe leer los campos por su NOMBRE (CED, NOM, SIM), no por posición,
para que agregar campos más adelante no lo rompa.
"""

import argparse
import csv
import os
import time
from datetime import datetime

from comun import (    # comun va antes que cv2: configura OpenCV para que no llene la pantalla de avisos
    ANCHO_MIN_RECONOCER, CARPETA, CIAN, GRIS, ROJO, UMBRAL, VERDE,
    BaseRostros, MotorFacial, abrir_camara, dibujar_rostro, escribir, salir_con_error,
)

import cv2

try:
    import serial
    from serial.tools import list_ports
except ImportError:          # el modo consola funciona sin pyserial
    serial = None

SEGUNDOS_POR_DEFECTO = 10      # tiempo máximo para reconocer antes de responder NO_RECONOCIDO
CUADROS_SEGUIDOS = 8           # cuadros seguidos aceptando a la MISMA persona (cada uno supera umbral y margen)
FALLOS_ANTES_DE_ESPERA = 2     # tras este número de NO_RECONOCIDO seguidos se hace esperar...
SEGUNDOS_DE_ESPERA = 30        # ...este tiempo antes de aceptar otro intento
VENTANA = "Orbita - Identificando conductor"
ARCHIVO_REGISTRO = os.path.join(CARPETA, "registro_identificaciones.csv")   # evidencia para el informe


def ahora():
    return datetime.now().strftime("%H:%M:%S")


def registrar_en_csv(resultado, cedula, nombre, similitud, segundos):
    nuevo = not os.path.isfile(ARCHIVO_REGISTRO)
    # utf-8-sig: así Excel abre bien las tildes
    with open(ARCHIVO_REGISTRO, "a", newline="", encoding="utf-8-sig") as f:
        escritor = csv.writer(f)
        if nuevo:
            escritor.writerow(["fecha_hora", "resultado", "cedula", "nombre", "similitud", "segundos"])
        escritor.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), resultado, cedula or "",
                           nombre or "", f"{similitud:.2f}", f"{segundos:.1f}"])


def limpiar_campo(texto):
    """Los campos del mensaje se separan con ';' y ':' — no pueden ir dentro del nombre."""
    return " ".join(str(texto).replace(";", ",").replace(":", " ").split())


# ---------------------------------------------------------------
#  Identificación con la cámara
# ---------------------------------------------------------------
class Identificador:
    def __init__(self, indice_camara, segundos, umbral, mostrar_ventana):
        print("Cargando modelos...")
        self.motor = MotorFacial()
        self.indice_camara = indice_camara
        self.segundos = segundos
        self.umbral = umbral
        self.mostrar = mostrar_ventana
        self.fallos_seguidos = 0
        self.bloqueado_hasta = 0.0      # time.monotonic() hasta el que no se aceptan intentos

    def _mostrar(self, vista, espera_ms=1):
        if not self.mostrar:
            return
        try:
            cv2.imshow(VENTANA, vista)
            cv2.waitKey(espera_ms)
        except cv2.error:
            print("  (No se pudo mostrar la ventana de la cámara; se sigue sin ella.)")
            self.mostrar = False

    def identificar(self):
        """Devuelve un diccionario con 'estado' ('reconocido' | 'no_reconocido' | 'error') y los datos."""
        inicio = time.monotonic()
        base = BaseRostros()            # se relee siempre: así toma en cuenta registros recientes
        if not base.conductores:
            return {"estado": "error", "motivo": "no hay conductores registrados", "segundos": 0.0}

        camara = abrir_camara(self.indice_camara, salir_si_falla=False)
        if camara is None:
            return {"estado": "error", "motivo": "no se pudo abrir la camara", "segundos": time.monotonic() - inicio}

        racha_cedula, racha_sims = None, []
        mejor_similitud = 0.0
        resultado = None
        try:
            while time.monotonic() - inicio < self.segundos:
                ok, cuadro = camara.read()
                if not ok:
                    resultado = {"estado": "error", "motivo": "la camara dejo de enviar imagenes"}
                    break
                vista = cuadro.copy() if self.mostrar else None

                # El conductor es el rostro más grande (el más cercano a la cámara).
                rostros = [r for r in self.motor.detectar(cuadro) if r[2] >= ANCHO_MIN_RECONOCER]
                if rostros:
                    rostro = rostros[0]
                    d = base.evaluar(self.motor.huella(cuadro, rostro), self.umbral)
                    cedula, nombre, similitud = d["cedula"], d["nombre"], d["similitud"]
                    mejor_similitud = max(mejor_similitud, similitud)
                    if cedula and cedula == racha_cedula:
                        racha_sims.append(similitud)
                    elif cedula:
                        racha_cedula, racha_sims = cedula, [similitud]
                    else:
                        racha_cedula, racha_sims = None, []
                    if self.mostrar:
                        if cedula:
                            texto = f"{nombre}  {similitud:.2f}  ({len(racha_sims)}/{CUADROS_SEGUIDOS})"
                        elif d["motivo"] == "poca_ventaja":
                            texto = f"Dudoso  {similitud:.2f} vs {d['segunda']:.2f}"
                        else:
                            texto = f"Desconocido  {similitud:.2f} < {d['umbral']:.2f}"
                        dibujar_rostro(vista, rostro, texto, VERDE if cedula else ROJO)
                    if len(racha_sims) >= CUADROS_SEGUIDOS:
                        resultado = {
                            "estado": "reconocido", "cedula": racha_cedula,
                            "nombre": base.conductores[racha_cedula]["nombre"],
                            "similitud": sum(racha_sims) / len(racha_sims),
                        }
                        if self.mostrar:
                            escribir(vista, f"Conductor verificado: {resultado['nombre']}", 10, 30, VERDE, 0.7, 2)
                            self._mostrar(vista, 1200)
                        break
                # (un cuadro sin rostro —un parpadeo, un movimiento— no corta la racha)

                if self.mostrar:
                    restante = max(0, self.segundos - (time.monotonic() - inicio))
                    escribir(vista, "Mira a la camara", 10, 30, CIAN, 0.7, 2)
                    escribir(vista, f"Tiempo restante: {restante:.0f} s", 10, vista.shape[0] - 15, GRIS)
                    self._mostrar(vista)
        finally:
            camara.release()
            if self.mostrar:
                try:
                    cv2.destroyWindow(VENTANA)
                    cv2.waitKey(1)
                except cv2.error:
                    pass

        if resultado is None:
            resultado = {"estado": "no_reconocido", "similitud": mejor_similitud}
        resultado["segundos"] = time.monotonic() - inicio
        return resultado


# ---------------------------------------------------------------
#  Atender un mensaje del celular
# ---------------------------------------------------------------
def atender(linea, identificador, enviar):
    """Responde un mensaje. Devuelve True si hizo una identificación (para vaciar lo que llegó mientras tanto)."""
    comando = linea.split(";")[0].strip().upper()
    print(f"[{ahora()}] <- {linea}")

    if comando == "PING":
        enviar("PONG")
        return False

    if comando == "IDENTIFICAR":
        espera = identificador.bloqueado_hasta - time.monotonic()
        if espera > 0:
            enviar(f"ERROR;MOTIVO:demasiados intentos fallidos, espera {espera:.0f} s")
            registrar_en_csv("error: en espera tras intentos fallidos", None, None, 0.0, 0.0)
            return True
        enviar("IDENTIFICANDO")
        r = identificador.identificar()
        if r["estado"] == "no_reconocido":
            identificador.fallos_seguidos += 1
            if identificador.fallos_seguidos >= FALLOS_ANTES_DE_ESPERA:
                identificador.fallos_seguidos = 0
                identificador.bloqueado_hasta = time.monotonic() + SEGUNDOS_DE_ESPERA
                print(f"           ({FALLOS_ANTES_DE_ESPERA} intentos fallidos seguidos: "
                      f"espera de {SEGUNDOS_DE_ESPERA} s)")
        elif r["estado"] == "reconocido":
            identificador.fallos_seguidos = 0
        if r["estado"] == "reconocido":
            enviar(f"CONDUCTOR;CED:{r['cedula']};NOM:{limpiar_campo(r['nombre'])};SIM:{r['similitud']:.2f}")
            registrar_en_csv("reconocido", r["cedula"], r["nombre"], r["similitud"], r["segundos"])
        elif r["estado"] == "no_reconocido":
            enviar(f"NO_RECONOCIDO;SIM:{r['similitud']:.2f}")
            registrar_en_csv("no_reconocido", None, None, r["similitud"], r["segundos"])
        else:
            enviar(f"ERROR;MOTIVO:{limpiar_campo(r['motivo'])}")
            registrar_en_csv("error: " + r["motivo"], None, None, 0.0, r["segundos"])
        print(f"           ({r['segundos']:.1f} s)")
        return True

    enviar("ERROR;MOTIVO:comando desconocido")
    return False


def atender_consola(identificador):
    print("\nMODO CONSOLA: escribe PING o IDENTIFICAR y presiona Enter (Ctrl+C para salir).\n")

    def enviar(texto):
        print(f"[{ahora()}] -> {texto}")

    while True:
        linea = input("> ").strip()
        if linea:
            atender(linea, identificador, enviar)


def atender_puerto(puerto, identificador):
    """Abre el puerto Bluetooth y atiende mensajes para siempre. Si el puerto falla, reintenta."""
    while True:
        try:
            with serial.serial_for_url(puerto, baudrate=115200, timeout=1, write_timeout=5) as conexion:
                print(f"\n[{ahora()}] Puerto {puerto} abierto. Esperando mensajes del celular... (Ctrl+C para salir)")

                def enviar(texto):
                    conexion.write((texto + "\n").encode("utf-8"))
                    conexion.flush()
                    print(f"[{ahora()}] -> {texto}")

                pendiente = b""
                while True:
                    pendiente += conexion.read(conexion.in_waiting or 1)
                    while b"\n" in pendiente:
                        crudo, pendiente = pendiente.split(b"\n", 1)
                        linea = crudo.decode("utf-8", "replace").strip()     # quita también el \r
                        if not linea:
                            continue
                        if atender(linea, identificador, enviar):
                            # Lo que el celular haya mandado mientras se identificaba (por ejemplo,
                            # un IDENTIFICAR repetido) se descarta: ya recibió su respuesta.
                            conexion.reset_input_buffer()
                            pendiente = b""
        except serial.SerialException as e:
            print(f"[{ahora()}] El puerto {puerto} no está disponible o se desconectó ({e}).")
            print("           Reintentando en 3 s... (Ctrl+C para salir)")
            time.sleep(3)


def listar_puertos():
    puertos = list(list_ports.comports())
    if not puertos:
        print("\nNo se encontró ningún puerto COM.")
    else:
        print("\nPuertos disponibles:")
        for p in sorted(puertos, key=lambda x: x.device):
            print(f"  {p.device:<8} {p.description}")
    print("\nEl que sirve es el puerto ENTRANTE que creaste en la configuración de Bluetooth de Windows")
    print("(Más opciones de Bluetooth > Puertos COM). Ahí aparece su número.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Servicio de reconocimiento facial que atiende al celular por Bluetooth.")
    parser.add_argument("--puerto", help="Puerto COM entrante de Bluetooth, por ejemplo COM6")
    parser.add_argument("--consola", action="store_true", help="Probar escribiendo los mensajes en el teclado")
    parser.add_argument("--listar-puertos", action="store_true", help="Muestra los puertos COM del computador")
    parser.add_argument("--camara", type=int, default=0, help="Número de la cámara (0 = la del portátil)")
    parser.add_argument("--segundos", type=float, default=SEGUNDOS_POR_DEFECTO, help="Tiempo máximo para reconocer")
    parser.add_argument("--umbral", type=float, default=UMBRAL, help=f"Similitud mínima (por defecto {UMBRAL})")
    parser.add_argument("--sin-ventana", action="store_true", help="No mostrar la cámara mientras identifica")
    args = parser.parse_args(argv)

    if (args.puerto or args.listar_puertos) and serial is None:
        salir_con_error("Falta la librería pyserial. Instálala con:  py -m pip install pyserial")
    if args.listar_puertos:
        listar_puertos()
        return
    if not args.puerto and not args.consola:
        parser.print_usage()
        salir_con_error("Indica el puerto (--puerto COM6) o usa --consola para probar sin celular.\n"
                        "Para ver los puertos:  py servicio_reconocimiento.py --listar-puertos")
    if not 0 < args.umbral < 1:
        salir_con_error("El umbral debe estar entre 0 y 1 (por ejemplo 0.55).")
    if args.segundos < 2:
        salir_con_error("Usa al menos 2 segundos (--segundos).")

    base = BaseRostros()
    if not base.conductores:
        salir_con_error('No hay conductores registrados. Primero:  py registrar.py --cedula ... --nombre "..."')
    print("\nConductores registrados (umbral que se le exige a cada uno):")
    for cedula, c in base.conductores.items():
        print(f"  {c['nombre']:<34} {base.umbral_de(cedula, args.umbral):.2f}")

    identificador = Identificador(args.camara, args.segundos, args.umbral, not args.sin_ventana)
    try:
        if args.consola:
            atender_consola(identificador)
        else:
            atender_puerto(args.puerto, identificador)
    except (KeyboardInterrupt, EOFError):
        print("\nServicio detenido.")


if __name__ == "__main__":
    main()
