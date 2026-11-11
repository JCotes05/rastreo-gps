"""
simulador_esp_sensores.py — P3-S3: el PC hace de ESP32 de sensores por Bluetooth.

Se comporta EXACTAMENTE como el firmware firmware/esp32_sensores/esp32_sensores.ino,
pero por el puerto COM Bluetooth entrante de Windows (igual que
servicio_reconocimiento.py --puerto COMx). Así se prueba la app de punta a punta
con el celular real antes de tener el hardware.

Uso (en PowerShell, dentro de esta carpeta):
    py simulador_esp_sensores.py --listar-puertos
    py simulador_esp_sensores.py --puerto COM7
    py simulador_esp_sensores.py --consola            (prueba sin celular: muestra lo que mandaría)

Teclas (mientras corre):
    f  frenada brusca         h  hueco          c  curva brusca
    a  abrir la carga (luz)   z  cerrar la carga (oscuridad)
    s  ver el estado          q  salir

Protocolo (una línea por mensaje, terminada en \\n; campos CLAVE:valor separados por ';'):
  Del celular al ESP:
    PING                  -> PONG
    INICIO;ENVIO:<id>     empieza el viaje: desde aquí se mandan eventos
    FIN                   termina el viaje
  Del ESP al celular:
    HOLA;DISP:SENSORES;VER:1                        al conectar
    EVENTO;TIPO:FRENADA;G:0.62;SEQ:<n>              (también HUECO y CURVA)
    LUZ;ESTADO:ABIERTA;NIVEL:812;SEQ:<n>            solo cuando cambia (y al recibir INICIO)
    LATIDO;LUZ:<nivel>;ESTADO:<ABIERTA|CERRADA>     cada 10 s

Nota: Windows abre el puerto COM aunque el celular todavía no se haya conectado; lo
que se escriba antes de que el celular se conecte se pierde (como en el ESP real
antes de emparejar). Por eso la app NO depende del HOLA: usa PING/PONG e INICIO.
"""

import argparse
import random
import sys
import threading
import time
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:          # el modo consola funciona sin pyserial
    serial = None

LATIDO_S = 10
LUZ_ABIERTA = (700, 900)     # nivel del ADC (0-4095) con luz
LUZ_CERRADA = (15, 60)       # en la oscuridad
VALORES = {"f": ("FRENADA", 0.45, 0.85), "h": ("HUECO", 0.85, 1.6), "c": ("CURVA", 0.42, 0.65)}
ANTIRREBOTE_S = 2.0          # igual que el firmware: 2 s sin repetir el mismo tipo


def ahora():
    return datetime.now().strftime("%H:%M:%S")


class EspSimulado:
    def __init__(self, enviar):
        self._enviar_crudo = enviar
        self.candado = threading.Lock()
        self.seq = 0
        self.envio = None                     # id del envío tras INICIO; None = sin viaje
        self.luz_abierta = False
        self.nivel = random.randint(*LUZ_CERRADA)
        self.ultimo_por_tipo = {}
        self.activo = True

    def enviar(self, texto):
        with self.candado:
            try:
                self._enviar_crudo(texto)
                print(f"[{ahora()}] -> {texto}")
            except Exception as e:               # celular no conectado: el mensaje se pierde
                print(f"[{ahora()}] (no se pudo enviar '{texto}': {e})")

    def _sec(self):
        self.seq += 1
        return self.seq

    def estado_luz(self):
        return "ABIERTA" if self.luz_abierta else "CERRADA"

    # --- mensajes que llegan del celular ---
    def atender(self, linea):
        print(f"[{ahora()}] <- {linea}")
        partes = linea.strip().split(";")
        tipo = partes[0].strip().upper()
        campos = {}
        for p in partes[1:]:
            if ":" in p:
                k, v = p.split(":", 1)
                campos[k.strip().upper()] = v.strip()
        if tipo == "PING":
            self.enviar("PONG")
        elif tipo == "INICIO":
            self.envio = campos.get("ENVIO", "?")
            print(f"           Viaje ENV-{self.envio} iniciado: desde ahora se mandan eventos.")
            self.enviar("HOLA;DISP:SENSORES;VER:1")
            # Estado actual de la luz (por si la carga ya estaba abierta al empezar).
            self.enviar(f"LUZ;ESTADO:{self.estado_luz()};NIVEL:{self.nivel};SEQ:{self._sec()}")
        elif tipo == "FIN":
            print(f"           Viaje ENV-{self.envio} terminado.")
            self.envio = None
        elif tipo == "HOLA":
            self.enviar("HOLA;DISP:SENSORES;VER:1")
        # Otros mensajes se ignoran (como en el firmware).

    # --- teclas ---
    def tecla(self, t):
        t = t.lower()
        if t in VALORES:
            nombre, minimo, maximo = VALORES[t]
            if self.envio is None:
                print(f"           (sin viaje: el ESP no manda {nombre}; espere el INICIO del celular)")
                return
            ultimo = self.ultimo_por_tipo.get(nombre, 0)
            if time.monotonic() - ultimo < ANTIRREBOTE_S:
                print(f"           (antirrebote: otra {nombre} en menos de {ANTIRREBOTE_S:g} s se ignora)")
                return
            self.ultimo_por_tipo[nombre] = time.monotonic()
            self.enviar(f"EVENTO;TIPO:{nombre};G:{random.uniform(minimo, maximo):.2f};SEQ:{self._sec()}")
        elif t in ("a", "z"):
            abrir = t == "a"
            if abrir == self.luz_abierta:
                print(f"           (la carga ya está {self.estado_luz().lower()})")
                return
            self.luz_abierta = abrir
            self.nivel = random.randint(*(LUZ_ABIERTA if abrir else LUZ_CERRADA))
            if self.envio is None:
                print(f"           Carga {self.estado_luz().lower()} (sin viaje: solo se informa en el latido).")
            else:
                self.enviar(f"LUZ;ESTADO:{self.estado_luz()};NIVEL:{self.nivel};SEQ:{self._sec()}")
        elif t == "s":
            print(f"           Viaje: {('ENV-' + self.envio) if self.envio else 'ninguno'} · carga {self.estado_luz().lower()}"
                  f" (nivel {self.nivel}) · SEQ {self.seq}")
        elif t == "q":
            self.activo = False

    def latir(self):
        while self.activo:
            time.sleep(LATIDO_S)
            if not self.activo:
                break
            # El nivel varía un poco, como el sensor real.
            rango = LUZ_ABIERTA if self.luz_abierta else LUZ_CERRADA
            self.nivel = max(rango[0], min(rango[1], self.nivel + random.randint(-8, 8)))
            self.enviar(f"LATIDO;LUZ:{self.nivel};ESTADO:{self.estado_luz()}")


def leer_teclas(esp):
    print("\nTeclas: f frenada · h hueco · c curva · a abrir carga · z cerrar carga · s estado · q salir\n")
    try:
        import msvcrt                       # Windows: una tecla sin Enter
        while esp.activo:
            if msvcrt.kbhit():
                esp.tecla(msvcrt.getwch())
            else:
                time.sleep(0.05)
    except ImportError:                     # otros sistemas: letra + Enter
        while esp.activo:
            try:
                linea = input()
            except EOFError:
                break
            for t in linea.strip():
                esp.tecla(t)


def leer_ordenes(esp, ruta):
    """--ordenes: cada letra que se agregue al archivo se toma como una tecla (para manejarlo desde otro programa)."""
    open(ruta, "w").close()
    leido = 0
    while esp.activo:
        try:
            with open(ruta, encoding="utf-8") as f:
                f.seek(leido)
                nuevo = f.read()
                leido = f.tell()
        except OSError:
            nuevo = ""
        for t in nuevo:
            if t.strip():
                esp.tecla(t)
        time.sleep(0.3)


def modo_consola():
    print("MODO CONSOLA: no hay celular; se muestra lo que el ESP mandaría.")
    print("Escriba mensajes del celular con el prefijo ':' (por ejemplo  :INICIO;ENVIO:7  o  :PING).")
    esp = EspSimulado(lambda texto: None)
    threading.Thread(target=esp.latir, daemon=True).start()
    while esp.activo:
        try:
            linea = input()
        except (EOFError, KeyboardInterrupt):
            break
        if linea.startswith(":"):
            esp.atender(linea[1:])
        else:
            for t in linea.strip():
                esp.tecla(t)


def modo_puerto(puerto, ordenes=None):
    while True:
        try:
            with serial.serial_for_url(puerto, baudrate=115200, timeout=1, write_timeout=3) as conexion:
                print(f"[{ahora()}] Puerto {puerto} abierto. Conecte el celular (la app se conecta al iniciar un viaje).")

                def escribir(texto):
                    conexion.write((texto + "\n").encode("utf-8"))
                    conexion.flush()

                esp = EspSimulado(escribir)
                esp.enviar("HOLA;DISP:SENSORES;VER:1")
                threading.Thread(target=esp.latir, daemon=True).start()
                if ordenes:
                    threading.Thread(target=leer_ordenes, args=(esp, ordenes), daemon=True).start()
                else:
                    threading.Thread(target=leer_teclas, args=(esp,), daemon=True).start()
                pendiente = b""
                while esp.activo:
                    pendiente += conexion.read(conexion.in_waiting or 1)
                    while b"\n" in pendiente:
                        crudo, pendiente = pendiente.split(b"\n", 1)
                        linea = crudo.decode("utf-8", "replace").strip()
                        if linea:
                            esp.atender(linea)
                return
        except serial.SerialException as e:
            print(f"[{ahora()}] El puerto {puerto} no está disponible o se desconectó ({e}).")
            print("           Reintentando en 3 s... (Ctrl+C para salir)")
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="Simula el ESP32 de sensores de la carga por Bluetooth.")
    parser.add_argument("--puerto", help="Puerto COM entrante de Bluetooth, por ejemplo COM7")
    parser.add_argument("--consola", action="store_true", help="Probar sin celular")
    parser.add_argument("--listar-puertos", action="store_true")
    parser.add_argument("--ordenes", metavar="ARCHIVO",
                        help="leer las teclas (f h c a z s q) de este archivo en vez del teclado")
    args = parser.parse_args()
    if (args.puerto or args.listar_puertos) and serial is None:
        sys.exit("Falta la librería pyserial. Instálala con:  py -m pip install pyserial")
    if args.listar_puertos:
        for p in sorted(list_ports.comports(), key=lambda x: x.device):
            print(f"  {p.device:<8} {p.description}")
        print("\nUse el puerto ENTRANTE de Bluetooth (Más opciones de Bluetooth > Puertos COM).")
        print("Si ya usa uno para el reconocimiento, cree OTRO puerto entrante para este simulador.")
        return
    try:
        if args.consola:
            modo_consola()
        elif args.puerto:
            modo_puerto(args.puerto, args.ordenes)
        else:
            parser.print_usage()
            sys.exit("Indique --puerto COMx o --consola.")
    except KeyboardInterrupt:
        print("\nDetenido.")


if __name__ == "__main__":
    main()
