"""
reconocimiento_facial.py — P3-S3: reconocimiento del conductor en el SERVIDOR (esquema híbrido).

El ESP32-CAM de la cabina detecta el rostro, lo recorta y lo manda al celular por Bluetooth;
el celular manda 1 a 3 recortes (JPEG en base64) a:

    POST /api/camiones/conductor/identificar      (Authorization: Bearer <token del camión>)
    {"rostros": ["<jpg base64>", ...]}

Aquí se vuelve a encontrar el rostro en cada recorte (YuNet), se saca su huella (SFace, el mismo
modelo de reconocimiento/comun.py del P3-S1) y se compara con las huellas de los conductores
guardadas en la tabla rostros_conductores. Responde:

    {"estado": "reconocido", "cedula": "...", "nombre": "...", "similitud": 0.82, "rostros": 3}
    {"estado": "no_reconocido", "similitud": 0.31, "motivo": "...", "rostros": 3}

Reglas contra falsos positivos (las de comun.py, P3-S1):
  - similitud de un conductor = promedio de sus 3 muestras más parecidas;
  - el más parecido debe superar UMBRAL (0.55, o más si se parece a otro conductor registrado)
    y sacarle MARGEN (0.10) al segundo;
  - TODOS los recortes con rostro (mínimo 2) deben aceptar a la MISMA persona;
  - tras 2 no reconocidos seguidos, el camión espera 30 s (insistir no sirve).
Cada intento queda en la tabla identificaciones (evidencia para el informe; sin imágenes).

Necesita numpy y OpenCV (cv2) y los modelos .onnx en ~/modelos (o en MODELOS_ROSTROS).
Si faltan, la API responde 503 y el resto del servidor sigue funcionando igual: solo la
instancia a la que llama la app (la de Jesús) necesita tenerlos.

Herramienta de línea de comandos (en la instancia, carpeta ~):
    python3 reconocimiento_facial.py --cargar conductores_rostros.json   sube las huellas de registrar.py
    python3 reconocimiento_facial.py --listar                            conductores con huellas
    python3 reconocimiento_facial.py --probar foto1.jpg foto2.jpg        identifica unas fotos
"""

import base64
import binascii
import json
import os
import re
import sys
import threading
import time

CARPETA = os.path.dirname(os.path.abspath(__file__))
CARPETA_MODELOS = os.environ.get("MODELOS_ROSTROS", os.path.join(CARPETA, "modelos"))
MODELO_DETECTOR = os.path.join(CARPETA_MODELOS, "face_detection_yunet_2023mar.onnx")
MODELO_RECONOCEDOR = os.path.join(CARPETA_MODELOS, "face_recognition_sface_2021dec.onnx")
NOMBRE_MODELO = "SFace 2021dec (OpenCV Zoo)"

# Las mismas reglas de reconocimiento/comun.py (ver allí por qué cada valor).
UMBRAL = 0.55
MARGEN = 0.10
DISTANCIA_A_VECINOS = 0.15
CONFIANZA_DETECCION = 0.80      # un poco menos que en el PC: el recorte del ESP es pequeño y algo borroso
ANCHO_MIN_ROSTRO = 60           # px dentro del recorte

MAX_ROSTROS = 5
MIN_ROSTROS_COINCIDENTES = 2
MAX_BYTES_JPG = 200_000
FALLOS_ANTES_DE_ESPERA = 2
SEGUNDOS_DE_ESPERA = 30
SEGUNDOS_CACHE_BASE = 60        # las huellas se releen de la base cada minuto
MAX_MUESTRAS = 50

PATRON_CEDULA = re.compile(r"^[0-9]{5,15}$")


class NoDisponible(Exception):
    """Falta numpy, OpenCV o los modelos en esta instancia."""


class DatoInvalido(Exception):
    """El pedido trae algo mal (base64, tamaño...)."""


class EnEspera(Exception):
    def __init__(self, segundos):
        super().__init__(f"Demasiados intentos fallidos. Espere {segundos} s y vuelva a intentar.")
        self.segundos = segundos


# ---------------------------------------------------------------------
# Motor (se carga la primera vez que se usa: así el servidor arranca igual sin OpenCV)
# ---------------------------------------------------------------------
_candado_motor = threading.Lock()      # los objetos de OpenCV no se usan desde dos hilos a la vez
_motor = None


def _cargar_motor():
    global _motor
    if _motor is not None:
        return _motor
    try:
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
        import cv2
        import numpy as np
    except ImportError:
        raise NoDisponible("El reconocimiento facial no está instalado en este servidor (falta OpenCV).") from None
    for ruta in (MODELO_DETECTOR, MODELO_RECONOCEDOR):
        if not os.path.isfile(ruta):
            raise NoDisponible("El reconocimiento facial no está instalado en este servidor (faltan los modelos).")
    detector = cv2.FaceDetectorYN.create(MODELO_DETECTOR, "", (320, 320), CONFIANZA_DETECCION, 0.3, 5000)
    reconocedor = cv2.FaceRecognizerSF.create(MODELO_RECONOCEDOR, "")
    _motor = (cv2, np, detector, reconocedor)
    return _motor


def disponible():
    try:
        with _candado_motor:
            _cargar_motor()
        return True
    except NoDisponible:
        return False


def huella_de_jpg(jpg):
    """Huella normalizada del rostro más grande del JPEG, o (None, motivo). Devuelve (huella, ancho_px)."""
    with _candado_motor:
        cv2, np, detector, reconocedor = _cargar_motor()
        imagen = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if imagen is None:
            return None, "imagen_invalida"
        alto, ancho = imagen.shape[:2]
        detector.setInputSize((ancho, alto))
        _, rostros = detector.detect(imagen)
        if rostros is None or len(rostros) == 0:
            return None, "sin_rostro"
        rostro = max(rostros, key=lambda r: r[2] * r[3])
        if rostro[2] < ANCHO_MIN_ROSTRO:
            return None, "rostro_pequeno"
        vector = reconocedor.feature(reconocedor.alignCrop(imagen, rostro)).flatten().astype(np.float32)
        return vector / (np.linalg.norm(vector) + 1e-9), int(rostro[2])


# ---------------------------------------------------------------------
# Huellas de los conductores (tabla rostros_conductores) — misma lógica que BaseRostros
# ---------------------------------------------------------------------
class BaseHuellas:
    def __init__(self, filas):
        _, np, _, _ = _cargar_motor()
        self.np = np
        self.conductores = {}      # cedula -> (nombre, matriz n x 128)
        for cedula, nombre, muestras in filas:
            m = np.array(muestras, dtype=np.float32)
            if m.ndim == 2 and m.shape[0] >= 1 and m.shape[1] == 128:
                m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
                self.conductores[cedula] = (nombre, m)
        self._vecinos = {}

    def _parecido(self, muestras, huella):
        return float(self.np.mean(self.np.sort(muestras @ huella)[::-1][:3]))

    def umbral_de(self, cedula):
        if cedula not in self._vecinos:
            propias = self.conductores[cedula][1]
            mayor = 0.0
            for otra, (_, muestras) in self.conductores.items():
                if otra != cedula:
                    mayor = max(mayor, max(self._parecido(muestras, m) for m in propias))
            self._vecinos[cedula] = mayor
        return max(UMBRAL, self._vecinos[cedula] + DISTANCIA_A_VECINOS)

    def evaluar(self, huella):
        lista = sorted(((self._parecido(m, huella), c, n) for c, (n, m) in self.conductores.items()), reverse=True)
        if not lista:
            return {"cedula": None, "similitud": 0.0, "mejor": None, "segunda": 0.0, "motivo": "sin_conductores"}
        similitud, cedula, nombre = lista[0]
        segunda = lista[1][0] if len(lista) > 1 else 0.0
        r = {"cedula": None, "nombre": None, "similitud": similitud, "mejor": cedula, "segunda": segunda,
             "umbral": self.umbral_de(cedula)}
        if similitud < r["umbral"]:
            r["motivo"] = "bajo_umbral"
        elif similitud - segunda < MARGEN:
            r["motivo"] = "poca_ventaja"
        else:
            r.update(cedula=cedula, nombre=nombre, motivo="aceptado")
        return r


_cache = {"base": None, "leida": 0.0}
_candado_cache = threading.Lock()


def _base(cur):
    with _candado_cache:
        if _cache["base"] is None or time.monotonic() - _cache["leida"] > SEGUNDOS_CACHE_BASE:
            cur.execute("""SELECT r.cedula, c.nombre_completo, r.muestras
                           FROM rostros_conductores r JOIN conductores c ON c.cedula = r.cedula""")
            filas = [(f["cedula"], f["nombre_completo"], f["muestras"]) if isinstance(f, dict) else f
                     for f in cur.fetchall()]
            _cache["base"] = BaseHuellas(filas)
            _cache["leida"] = time.monotonic()
        return _cache["base"]


def olvidar_base():
    with _candado_cache:
        _cache["base"] = None


# ---------------------------------------------------------------------
# Intentos fallidos por camión (en memoria: se reinicia con el servicio)
# ---------------------------------------------------------------------
_fallos = {}            # placa -> (fallos_seguidos, en_espera_hasta)
_candado_fallos = threading.Lock()


def _revisar_espera(placa):
    with _candado_fallos:
        _, hasta = _fallos.get(placa, (0, 0.0))
        falta = hasta - time.monotonic()
        if falta > 0:
            raise EnEspera(int(falta) + 1)


def _anotar(placa, reconocido):
    with _candado_fallos:
        if reconocido:
            _fallos.pop(placa, None)
            return
        seguidos, _ = _fallos.get(placa, (0, 0.0))
        seguidos += 1
        if seguidos >= FALLOS_ANTES_DE_ESPERA:
            _fallos[placa] = (0, time.monotonic() + SEGUNDOS_DE_ESPERA)
        else:
            _fallos[placa] = (seguidos, 0.0)


# ---------------------------------------------------------------------
# Lo que llama la API
# ---------------------------------------------------------------------
def leer_rostros(datos):
    """Valida el cuerpo {"rostros": [base64, ...]} y devuelve la lista de JPEG en bytes."""
    lista = datos.get("rostros")
    if not isinstance(lista, list) or not lista:
        raise DatoInvalido("Falta la lista de rostros.")
    if len(lista) > MAX_ROSTROS:
        raise DatoInvalido(f"Máximo {MAX_ROSTROS} rostros por intento.")
    jpgs = []
    for texto in lista:
        if not isinstance(texto, str):
            raise DatoInvalido("Cada rostro debe ser un texto en base64.")
        try:
            jpg = base64.b64decode(texto, validate=True)
        except (binascii.Error, ValueError):
            raise DatoInvalido("Un rostro no es base64 válido.") from None
        if len(jpg) > MAX_BYTES_JPG:
            raise DatoInvalido("Un rostro es demasiado grande.")
        if not jpg.startswith(b"\xff\xd8"):
            raise DatoInvalido("Cada rostro debe ser una imagen JPEG.")
        jpgs.append(jpg)
    return jpgs


def decidir(base, jpgs):
    """Evalúa cada recorte y toma la decisión. No toca la base de datos."""
    evaluaciones = []
    for jpg in jpgs:
        huella, ancho_o_motivo = huella_de_jpg(jpg)
        if huella is None:
            evaluaciones.append({"cedula": None, "similitud": 0.0, "motivo": ancho_o_motivo})
        else:
            evaluaciones.append(base.evaluar(huella))
    con_rostro = [e for e in evaluaciones if e["motivo"] not in ("sin_rostro", "rostro_pequeno", "imagen_invalida")]
    mejor = max((e["similitud"] for e in con_rostro), default=0.0)
    resultado = {"rostros": len(jpgs), "con_rostro": len(con_rostro),
                 "similitudes": [round(e["similitud"], 3) for e in evaluaciones]}
    if not base.conductores:
        return dict(resultado, estado="no_reconocido", similitud=0.0, motivo="No hay conductores registrados.")
    if len(con_rostro) < MIN_ROSTROS_COINCIDENTES:
        return dict(resultado, estado="no_reconocido", similitud=round(mejor, 2),
                    motivo="No se vio bien el rostro. Mire de frente a la cámara, con buena luz.")
    cedulas = {e["cedula"] for e in con_rostro}
    if len(cedulas) == 1 and None not in cedulas:
        cedula = cedulas.pop()
        sim = sum(e["similitud"] for e in con_rostro) / len(con_rostro)
        return dict(resultado, estado="reconocido", cedula=cedula, nombre=base.conductores[cedula][0],
                    similitud=round(sim, 2))
    motivos = {e["motivo"] for e in con_rostro}
    if "aceptado" in motivos:
        motivo = "Los rostros no coincidieron con una misma persona."
    elif "poca_ventaja" in motivos:
        motivo = "El rostro se parece a más de un conductor."
    else:
        motivo = "Ningún conductor registrado coincidió."
    return dict(resultado, estado="no_reconocido", similitud=round(mejor, 2), motivo=motivo)


def identificar(cur, placa, jpgs):
    """Decide, cuenta los fallos del camión y deja el intento en la tabla identificaciones."""
    _revisar_espera(placa)
    base = _base(cur)
    r = decidir(base, jpgs)
    _anotar(placa, r["estado"] == "reconocido")
    cur.execute("""INSERT INTO identificaciones (placa, resultado, cedula, similitud, rostros, con_rostro, motivo)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (placa, r["estado"], r.get("cedula"), r["similitud"], r["rostros"], r["con_rostro"], r.get("motivo")))
    print(f"[ROSTRO] {placa}: {r['estado']} {r.get('cedula') or ''} sim={r['similitud']} "
          f"({r['con_rostro']}/{r['rostros']} con rostro, {r['similitudes']})", flush=True)
    return r


# ---------------------------------------------------------------------
# Línea de comandos: cargar huellas, listar y probar
# ---------------------------------------------------------------------
def _cargar_archivo(ruta):
    """Sube a rostros_conductores las huellas de un conductores_rostros.json de registrar.py."""
    from basedatos import obtener_conexion
    with open(ruta, encoding="utf-8") as f:
        datos = json.load(f)
    if "SFace" not in str(datos.get("modelo", "")):
        print("[ERROR] El archivo no es de SFace (registrar.py del P3-S1).")
        return 1
    conexion = obtener_conexion()
    try:
        cur = conexion.cursor()
        subidos = 0
        for cedula, c in datos.get("conductores", {}).items():
            muestras = c.get("muestras", [])
            if not PATRON_CEDULA.match(cedula):
                print(f"  {cedula}: cédula inválida, se omite")
                continue
            if not muestras or any(len(m) != 128 for m in muestras):
                print(f"  {cedula}: huellas inválidas, se omite")
                continue
            cur.execute("SELECT nombre_completo FROM conductores WHERE cedula = %s", (cedula,))
            fila = cur.fetchone()
            if fila is None:
                print(f"  {cedula} ({c.get('nombre')}): no está en la tabla conductores, se omite")
                continue
            cur.execute("""INSERT INTO rostros_conductores (cedula, muestras, modelo) VALUES (%s, %s, %s)
                           ON CONFLICT (cedula) DO UPDATE SET muestras = EXCLUDED.muestras,
                               modelo = EXCLUDED.modelo, actualizado_en = now()""",
                        (cedula, json.dumps(muestras[:MAX_MUESTRAS]), NOMBRE_MODELO))
            subidos += 1
            print(f"  {cedula} ({fila[0]}): {min(len(muestras), MAX_MUESTRAS)} muestras")
        conexion.commit()
        print(f"\nListo: {subidos} conductores con huellas en la base.")
        print("Borra el archivo de esta instancia cuando termines (son datos biométricos):  rm " + ruta)
        return 0
    finally:
        conexion.close()


def _listar():
    from basedatos import obtener_conexion
    conexion = obtener_conexion()
    try:
        cur = conexion.cursor()
        cur.execute("""SELECT r.cedula, c.nombre_completo, jsonb_array_length(r.muestras),
                              to_char(r.actualizado_en AT TIME ZONE 'America/Bogota', 'YYYY-MM-DD HH24:MI')
                       FROM rostros_conductores r JOIN conductores c ON c.cedula = r.cedula ORDER BY 2""")
        filas = cur.fetchall()
        for cedula, nombre, n, cuando in filas:
            print(f"  {cedula}  {nombre:<30} {n:>3} muestras  ({cuando})")
        print(f"{len(filas)} conductores con huellas.")
        return 0
    finally:
        conexion.close()


def _probar(rutas):
    from basedatos import obtener_conexion
    conexion = obtener_conexion()
    try:
        cur = conexion.cursor()
        jpgs = []
        for ruta in rutas:
            with open(ruta, "rb") as f:
                jpgs.append(f.read())
        print(json.dumps(decidir(_base(cur), jpgs), ensure_ascii=False, indent=2))
        return 0
    finally:
        conexion.close()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--cargar":
        sys.exit(_cargar_archivo(sys.argv[2]))
    if len(sys.argv) == 2 and sys.argv[1] == "--listar":
        sys.exit(_listar())
    if len(sys.argv) >= 3 and sys.argv[1] == "--probar":
        sys.exit(_probar(sys.argv[2:]))
    print(__doc__)
    sys.exit(2)
