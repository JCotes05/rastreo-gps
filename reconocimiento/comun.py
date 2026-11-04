"""
comun.py — Motor de reconocimiento facial de Orbita (P3-S1).

Lo usan registrar.py y reconocer.py (y, en el paso 6, el servicio que
atiende al celular por Bluetooth). No se ejecuta solo.

Cómo funciona, en dos pasos (modelos de OpenCV Zoo, carpeta modelos/):
  1. YuNet encuentra los rostros en la imagen (y 5 puntos: ojos, nariz, boca).
  2. SFace convierte cada rostro en una "huella" de 128 números.
Dos huellas de la misma persona se parecen mucho (similitud cercana a 1);
de personas distintas, poco (cercana a 0). Para reconocer a alguien se
compara su huella con las guardadas al registrar a cada conductor.
"""

import os

# Oculta los avisos internos de OpenCV (deben configurarse ANTES de importar cv2).
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import json
import re
import sys
import unicodedata
from datetime import datetime

import cv2
import numpy as np

try:    # por si cv2 ya se había importado antes que este archivo
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:
    pass

# ---------------------------------------------------------------
#  Rutas y parámetros
# ---------------------------------------------------------------
CARPETA = os.path.dirname(os.path.abspath(__file__))
MODELO_DETECTOR = os.path.join(CARPETA, "modelos", "face_detection_yunet_2023mar.onnx")
MODELO_RECONOCEDOR = os.path.join(CARPETA, "modelos", "face_recognition_sface_2021dec.onnx")
ARCHIVO_BASE = os.path.join(CARPETA, "conductores_rostros.json")   # NO se sube a GitHub (datos biométricos)
CARPETA_FOTOS = os.path.join(CARPETA, "fotos")                    # tampoco

# Similitud mínima para aceptar que el rostro es de un conductor registrado.
# OpenCV recomienda 0.363 para SFace; usamos algo más estricto porque poner
# a otra persona en la planilla es peor que pedirle que lo intente de nuevo.
# Se calibra con las pruebas (reconocer.py muestra la similitud en pantalla).
UMBRAL = 0.40

CONFIANZA_DETECCION = 0.85     # qué tan seguro debe estar YuNet de que es un rostro
ANCHO_MIN_RECONOCER = 70       # px: rostros más pequeños (lejos de la cámara) no se evalúan

PATRON_CEDULA = re.compile(r"^[0-9]{5,15}$")   # igual que en el listener del servidor

VERDE = (80, 200, 60)
ROJO = (60, 60, 230)
GRIS = (170, 170, 170)
CIAN = (252, 219, 45)          # el cian de la marca Orbita (en BGR)


# ---------------------------------------------------------------
#  Utilidades
# ---------------------------------------------------------------
def salir_con_error(mensaje):
    print("\n[ERROR] " + mensaje + "\n")
    sys.exit(1)


def verificar_instalacion():
    """Revisa lo que suele fallar en Windows antes de cargar nada."""
    for ruta in (MODELO_DETECTOR, MODELO_RECONOCEDOR):
        if not os.path.isfile(ruta):
            salir_con_error(
                f"No encuentro el modelo:\n    {ruta}\n"
                "Revisa que la carpeta 'modelos' esté junto a este archivo y tenga los dos .onnx."
            )
    try:
        CARPETA.encode("ascii")
    except UnicodeEncodeError:
        salir_con_error(
            f"La carpeta del proyecto tiene tildes o ñ:\n    {CARPETA}\n"
            "OpenCV no puede abrir los modelos desde rutas así en Windows.\n"
            "Mueve la carpeta 'reconocimiento' a una ruta simple, por ejemplo C:\\orbita\\reconocimiento"
        )


def sin_tildes(texto):
    """OpenCV no sabe dibujar tildes ni ñ en la imagen: 'Jesús Muñoz' -> 'Jesus Munoz'."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")


def cedula_valida(cedula):
    return bool(PATRON_CEDULA.match(cedula or ""))


def leer_imagen(ruta):
    """Como cv2.imread, pero funciona aunque la ruta tenga tildes o ñ."""
    try:
        datos = np.fromfile(ruta, dtype=np.uint8)
    except OSError:
        return None
    if datos.size == 0:
        return None
    return cv2.imdecode(datos, cv2.IMREAD_COLOR)


def guardar_imagen(ruta, imagen):
    """Como cv2.imwrite, pero funciona aunque la ruta tenga tildes o ñ."""
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    ok, codificada = cv2.imencode(".jpg", imagen)
    if ok:
        codificada.tofile(ruta)
    return ok


def abrir_camara(indice, salir_si_falla=True):
    """Abre la cámara web. En Windows se usa DirectShow, que abre mucho más rápido.
    Si no abre: termina el programa con un mensaje, o devuelve None si salir_si_falla=False
    (así lo usa el servicio, que debe seguir funcionando y avisarle al celular)."""
    if os.name == "nt":
        camara = cv2.VideoCapture(indice, cv2.CAP_DSHOW)
    else:
        camara = cv2.VideoCapture(indice)
    if not camara.isOpened():
        if not salir_si_falla:
            camara.release()
            return None
        salir_con_error(
            f"No se pudo abrir la cámara {indice}.\n"
            "  · Cierra otras apps que la estén usando (Zoom, Teams, Meet, la app Cámara).\n"
            "  · Si tienes una cámara USB además de la del portátil, prueba con --camara 1.\n"
            "  · En Windows: Configuración > Privacidad > Cámara > permitir a las apps de escritorio."
        )
    camara.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camara.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return camara


def ventana_cerrada(nombre):
    """True si el usuario cerró la ventana con la X."""
    try:
        return cv2.getWindowProperty(nombre, cv2.WND_PROP_VISIBLE) < 1
    except cv2.error:
        return True


def escribir(imagen, texto, x, y, color=(255, 255, 255), escala=0.6, grosor=1):
    """Texto con fondo oscuro para que se lea sobre cualquier imagen."""
    texto = sin_tildes(texto)
    fuente = cv2.FONT_HERSHEY_SIMPLEX
    (ancho, alto), base = cv2.getTextSize(texto, fuente, escala, grosor)
    cv2.rectangle(imagen, (x - 4, y - alto - 6), (x + ancho + 4, y + base + 2), (20, 20, 20), -1)
    cv2.putText(imagen, texto, (x, y), fuente, escala, color, grosor, cv2.LINE_AA)


def dibujar_rostro(imagen, rostro, texto, color):
    x, y, w, h = [int(v) for v in rostro[:4]]
    cv2.rectangle(imagen, (x, y), (x + w, y + h), color, 2)
    if texto:
        escribir(imagen, texto, x, max(y - 10, 20), color, 0.6, 2)


# ---------------------------------------------------------------
#  Motor: detectar rostros y sacar su huella
# ---------------------------------------------------------------
class MotorFacial:
    def __init__(self):
        verificar_instalacion()
        self.detector = cv2.FaceDetectorYN.create(
            MODELO_DETECTOR, "", (320, 320), CONFIANZA_DETECCION, 0.3, 5000
        )
        self.reconocedor = cv2.FaceRecognizerSF.create(MODELO_RECONOCEDOR, "")
        self._tamano = None

    def detectar(self, imagen):
        """Lista de rostros, del más grande al más pequeño.
        Cada rostro es un arreglo: x, y, ancho, alto, 10 valores de puntos clave, confianza."""
        alto, ancho = imagen.shape[:2]
        if self._tamano != (ancho, alto):
            self.detector.setInputSize((ancho, alto))
            self._tamano = (ancho, alto)
        _, rostros = self.detector.detect(imagen)
        if rostros is None:
            return []
        return sorted(rostros, key=lambda r: r[2] * r[3], reverse=True)

    def recortar(self, imagen, rostro):
        """Rostro alineado (ojos horizontales) de 112x112: lo que ve SFace."""
        return self.reconocedor.alignCrop(imagen, rostro)

    def huella(self, imagen, rostro):
        """Huella de 128 números, normalizada (largo 1) para comparar con producto punto."""
        vector = self.reconocedor.feature(self.recortar(imagen, rostro)).flatten().astype(np.float32)
        return vector / (np.linalg.norm(vector) + 1e-9)


# ---------------------------------------------------------------
#  Base de rostros registrados (archivo conductores_rostros.json)
# ---------------------------------------------------------------
class BaseRostros:
    """
    Guarda, por cédula, el nombre y varias huellas del conductor.
    La cédula DEBE ser la misma que está en la tabla 'conductores' del
    servidor: es lo único que el celular le manda al servidor.
    """

    def __init__(self, ruta=ARCHIVO_BASE):
        self.ruta = ruta
        self.conductores = {}       # cedula -> {"nombre", "registrado", "muestras": np.array (n, 128)}
        if os.path.isfile(ruta):
            with open(ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
            for cedula, c in datos.get("conductores", {}).items():
                self.conductores[cedula] = {
                    "nombre": c["nombre"],
                    "registrado": c.get("registrado", ""),
                    "muestras": np.array(c["muestras"], dtype=np.float32),
                }

    def guardar(self):
        datos = {
            "modelo": "SFace 2021dec (OpenCV Zoo)",
            "conductores": {
                cedula: {
                    "nombre": c["nombre"],
                    "registrado": c["registrado"],
                    "muestras": [[round(float(v), 6) for v in m] for m in c["muestras"]],
                }
                for cedula, c in self.conductores.items()
            },
        }
        temporal = self.ruta + ".tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False)
        os.replace(temporal, self.ruta)     # así nunca queda un archivo a medio escribir

    def agregar(self, cedula, nombre, muestras):
        self.conductores[cedula] = {
            "nombre": nombre,
            "registrado": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "muestras": np.array(muestras, dtype=np.float32),
        }

    def eliminar(self, cedula):
        return self.conductores.pop(cedula, None) is not None

    def similitudes(self, huella):
        """Similitud de una huella con cada conductor: promedio de sus 3 muestras más parecidas.
        Devuelve una lista [(similitud, cedula, nombre)] de mayor a menor."""
        resultado = []
        for cedula, c in self.conductores.items():
            parecidos = np.sort(c["muestras"] @ huella)[::-1]
            resultado.append((float(np.mean(parecidos[:3])), cedula, c["nombre"]))
        resultado.sort(reverse=True)
        return resultado

    def identificar(self, huella, umbral=UMBRAL):
        """(cedula, nombre, similitud) del conductor reconocido, o (None, None, similitud) si nadie
        supera el umbral. La similitud devuelta es siempre la del más parecido."""
        lista = self.similitudes(huella)
        if not lista:
            return None, None, 0.0
        similitud, cedula, nombre = lista[0]
        if similitud >= umbral:
            return cedula, nombre, similitud
        return None, None, similitud
