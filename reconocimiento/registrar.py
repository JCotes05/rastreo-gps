"""
registrar.py — Registra el rostro de un conductor (P3-S1, paso 5).

Con la cámara (la persona se sienta frente al computador):
    py registrar.py --cedula 1001000001 --nombre "Jesús Cotes"

Desde una carpeta de fotos (por ejemplo, selfies que mandó un compañero):
    py registrar.py --cedula 1001000002 --nombre "Valentina Pérez" --fotos C:\\orbita\\selfies_valentina

Ver quiénes están registrados:
    py registrar.py --listar

Borrar a alguien (también borra sus fotos):
    py registrar.py --borrar 1001000002

IMPORTANTE: la cédula debe ser EXACTAMENTE la misma que está en la tabla
'conductores' del servidor, porque es lo único que viaja hasta allá.
"""

import argparse
import os
import shutil
import time

from comun import (    # comun va antes que cv2: configura OpenCV para que no llene la pantalla de avisos
    CARPETA_FOTOS, CIAN, ROJO, UMBRAL, VERDE,
    BaseRostros, MotorFacial, abrir_camara, cedula_valida, dibujar_rostro,
    escribir, guardar_imagen, leer_imagen, salir_con_error, ventana_cerrada,
)

import cv2
import numpy as np

MUESTRAS_POR_DEFECTO = 10
MUESTRAS_MINIMAS = 3
INTERVALO_CAPTURA_S = 0.6      # tiempo entre una muestra y la siguiente (para que cambie la pose)
ANCHO_MIN_REGISTRO = 110       # px: el rostro debe verse grande para registrarlo bien
ANCHO_MIN_FOTO = 60            # px: en fotos, rostros más pequeños (personas lejos) no sirven
LADO_MAXIMO_FOTO = 1280        # las fotos de celular se reducen a este tamaño antes de analizarlas
VENTANA = "Orbita - Registro de conductor"

# Se van mostrando en orden mientras se capturan las muestras, para tener poses variadas.
INDICACIONES = [
    "Mira de frente a la camara",
    "Gira la cara un poco a tu izquierda",
    "Ahora un poco a tu derecha",
    "Sube un poco la barbilla",
    "Baja un poco la barbilla",
]
EXTENSIONES = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


# ---------------------------------------------------------------
#  Captura con la cámara
# ---------------------------------------------------------------
def capturar_con_camara(motor, indice_camara, cantidad, nombre, cedula):
    camara = abrir_camara(indice_camara)
    cv2.namedWindow(VENTANA, cv2.WINDOW_AUTOSIZE)
    muestras, recortes = [], []
    ultima_captura = 0.0
    print("\nSe abrió la ventana de la cámara. Sigue las indicaciones en pantalla (Q para cancelar).")
    try:
        while len(muestras) < cantidad:
            ok, cuadro = camara.read()
            if not ok:
                salir_con_error("La cámara dejó de enviar imágenes.")
            vista = cuadro.copy()
            rostros = motor.detectar(cuadro)

            if not rostros:
                estado, color = "No veo ningun rostro: mira a la camara", ROJO
            elif len(rostros) > 1:
                estado, color = "Solo debe haber UNA persona frente a la camara", ROJO
                for r in rostros:
                    dibujar_rostro(vista, r, "", ROJO)
            elif rostros[0][2] < ANCHO_MIN_REGISTRO:
                estado, color = "Acercate un poco mas a la camara", ROJO
                dibujar_rostro(vista, rostros[0], "", ROJO)
            else:
                rostro = rostros[0]
                ahora = time.monotonic()
                if ahora - ultima_captura >= INTERVALO_CAPTURA_S:
                    muestras.append(motor.huella(cuadro, rostro))
                    recortes.append(motor.recortar(cuadro, rostro))
                    ultima_captura = ahora
                paso = min(len(muestras) * len(INDICACIONES) // cantidad, len(INDICACIONES) - 1)
                estado, color = INDICACIONES[paso], VERDE
                dibujar_rostro(vista, rostro, f"{len(muestras)}/{cantidad}", VERDE)

            escribir(vista, f"Registrando: {nombre} ({cedula})", 10, 28, CIAN)
            escribir(vista, estado, 10, 58, color)
            escribir(vista, f"Muestras {len(muestras)}/{cantidad}   |   Q = cancelar", 10, vista.shape[0] - 15, CIAN)
            cv2.imshow(VENTANA, vista)

            tecla = cv2.waitKey(1) & 0xFF
            if tecla in (ord("q"), ord("Q"), 27) or ventana_cerrada(VENTANA):
                print("\nRegistro cancelado. No se guardó nada.")
                return None, None
    finally:
        camara.release()
        cv2.destroyAllWindows()
    return muestras, recortes


# ---------------------------------------------------------------
#  Registro desde una carpeta de fotos
# ---------------------------------------------------------------
def reducir(imagen, lado_maximo):
    alto, ancho = imagen.shape[:2]
    escala = lado_maximo / max(alto, ancho)
    if escala >= 1:
        return imagen
    return cv2.resize(imagen, (int(ancho * escala), int(alto * escala)), interpolation=cv2.INTER_AREA)


def capturar_desde_fotos(motor, carpeta):
    if not os.path.isdir(carpeta):
        salir_con_error(f"No existe la carpeta:\n    {carpeta}")
    archivos = sorted(f for f in os.listdir(carpeta) if f.lower().endswith(EXTENSIONES))
    if not archivos:
        salir_con_error(f"No hay fotos (.jpg, .png) en:\n    {carpeta}")

    print(f"\nRevisando {len(archivos)} foto(s) de {carpeta}:")
    muestras, recortes = [], []
    for archivo in archivos:
        imagen = leer_imagen(os.path.join(carpeta, archivo))
        if imagen is None:
            print(f"  --  {archivo}: no se pudo abrir, se omite")
            continue
        imagen = reducir(imagen, LADO_MAXIMO_FOTO)
        # Algunas fotos de celular llegan "acostadas": si no hay rostro, se prueba girándola.
        rostros = []
        for giro in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
            candidata = imagen if giro is None else cv2.rotate(imagen, giro)
            rostros = motor.detectar(candidata)
            if rostros:
                imagen = candidata
                break
        if not rostros:
            print(f"  --  {archivo}: no se encontró un rostro, se omite")
            continue
        if rostros[0][2] < ANCHO_MIN_FOTO:
            print(f"  --  {archivo}: el rostro se ve muy pequeño (persona lejos), se omite")
            continue
        if len(rostros) > 1:
            print(f"  OK  {archivo} (hay {len(rostros)} rostros: se usa el más grande)")
        else:
            print(f"  OK  {archivo}")
        muestras.append(motor.huella(imagen, rostros[0]))
        recortes.append(motor.recortar(imagen, rostros[0]))
    return muestras, recortes


# ---------------------------------------------------------------
#  Revisión de calidad antes de guardar
# ---------------------------------------------------------------
def revisar_calidad(muestras, base, cedula):
    """Avisa si las muestras se parecen poco entre sí, o si se parecen a otro conductor."""
    matriz = np.array(muestras)
    n = len(matriz)
    consistencia = float((matriz @ matriz.T).sum() - n) / (n * (n - 1))
    if consistencia >= 0.6:
        juicio = "muy buena"
    elif consistencia >= 0.45:
        juicio = "aceptable"
    else:
        juicio = "BAJA: repite el registro con mejor luz y de frente a la cámara"
    print(f"\nParecido entre las {n} muestras: {consistencia:.2f} ({juicio})")

    for otra_cedula, otro in base.conductores.items():
        if otra_cedula == cedula:
            continue
        parecidos = []
        for huella in matriz:
            sims = np.sort(otro["muestras"] @ huella)[::-1]
            parecidos.append(float(np.mean(sims[:3])))
        promedio = float(np.mean(parecidos))
        if promedio >= UMBRAL:
            print(f"[AVISO] Estas muestras se parecen a {otro['nombre']} ({otra_cedula}): {promedio:.2f}. "
                  "Revisen que no se haya registrado la misma persona con dos cédulas.")


def guardar_recortes(cedula, recortes):
    """Guarda los rostros recortados (sirven como evidencia y para el informe)."""
    carpeta = os.path.join(CARPETA_FOTOS, cedula)
    shutil.rmtree(carpeta, ignore_errors=True)          # si se registra de nuevo, se reemplazan
    for i, recorte in enumerate(recortes, start=1):
        guardar_imagen(os.path.join(carpeta, f"muestra_{i:02d}.jpg"), recorte)
    return carpeta


# ---------------------------------------------------------------
#  Programa principal
# ---------------------------------------------------------------
def listar(base):
    if not base.conductores:
        print("\nNo hay conductores registrados todavía.")
        return
    print(f"\n{'Cédula':<13} {'Nombre':<32} {'Muestras':>8}   Registrado")
    for cedula, c in sorted(base.conductores.items(), key=lambda par: par[1]["nombre"]):
        print(f"{cedula:<13} {c['nombre']:<32} {len(c['muestras']):>8}   {c['registrado']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Registra el rostro de un conductor de Orbita.")
    parser.add_argument("--cedula", help="Cédula, solo números (la misma de la base de datos del servidor)")
    parser.add_argument("--nombre", help='Nombre completo entre comillas, ej. "Jesús Cotes"')
    parser.add_argument("--fotos", help="Carpeta con fotos de la persona (en vez de usar la cámara)")
    parser.add_argument("--camara", type=int, default=0, help="Número de la cámara (0 = la del portátil)")
    parser.add_argument("--muestras", type=int, default=MUESTRAS_POR_DEFECTO, help="Muestras a tomar con la cámara")
    parser.add_argument("--listar", action="store_true", help="Muestra los conductores registrados")
    parser.add_argument("--borrar", metavar="CEDULA", help="Borra el registro (y las fotos) de esa cédula")
    args = parser.parse_args(argv)

    base = BaseRostros()

    if args.listar:
        listar(base)
        return
    if args.borrar:
        if base.eliminar(args.borrar):
            base.guardar()
            shutil.rmtree(os.path.join(CARPETA_FOTOS, args.borrar), ignore_errors=True)
            print(f"\nSe borró el registro de la cédula {args.borrar}.")
        else:
            print(f"\nNo hay ningún conductor registrado con la cédula {args.borrar}.")
        return

    # --- Validaciones ---
    if not args.cedula or not args.nombre:
        parser.print_usage()
        salir_con_error('Faltan datos. Ejemplo:  py registrar.py --cedula 1001000001 --nombre "Jesús Cotes"')
    cedula = args.cedula.strip()
    nombre = " ".join(args.nombre.split())
    if not cedula_valida(cedula):
        salir_con_error(f"La cédula '{cedula}' no es válida: solo números, sin puntos ni espacios (5 a 15 dígitos).")
    if args.muestras < MUESTRAS_MINIMAS:
        salir_con_error(f"Se necesitan al menos {MUESTRAS_MINIMAS} muestras.")
    if cedula in base.conductores:
        anterior = base.conductores[cedula]["nombre"]
        respuesta = input(f"\nLa cédula {cedula} ya está registrada como '{anterior}'. ¿Reemplazar su registro? (s/n): ")
        if not respuesta.strip().lower().startswith("s"):
            print("No se cambió nada.")
            return

    print("\nCargando modelos...")
    motor = MotorFacial()

    if args.fotos:
        muestras, recortes = capturar_desde_fotos(motor, args.fotos)
    else:
        muestras, recortes = capturar_con_camara(motor, args.camara, args.muestras, nombre, cedula)
    if muestras is None:
        return
    if len(muestras) < MUESTRAS_MINIMAS:
        salir_con_error(f"Solo se obtuvieron {len(muestras)} muestras con rostro; se necesitan al menos "
                        f"{MUESTRAS_MINIMAS}. Usa más fotos, de frente y con buena luz.")

    revisar_calidad(muestras, base, cedula)
    base.agregar(cedula, nombre, muestras)
    base.guardar()
    carpeta = guardar_recortes(cedula, recortes)

    print(f"\nListo: {nombre} ({cedula}) quedó registrado con {len(muestras)} muestras.")
    print(f"Sus rostros recortados quedaron en: {carpeta}")
    print("Recuerda que esa cédula debe existir en la tabla 'conductores' del servidor.")
    print("Pruébalo con:  py reconocer.py\n")


if __name__ == "__main__":
    main()
