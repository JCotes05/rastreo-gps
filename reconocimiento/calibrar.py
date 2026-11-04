"""
calibrar.py — Mide qué tan bien separa el reconocimiento a los conductores de los
desconocidos, para elegir el umbral con datos (P3-S1, mejora de falsos positivos).

1) GRABAR sesiones (unos 15 s cada una) con la cámara:

    py calibrar.py --grabar --tipo genuino  --cedula 1001000001 --etiqueta "jesus con gafas"
    py calibrar.py --grabar --tipo genuino  --cedula 1001000001 --etiqueta "jesus sin gafas"
    py calibrar.py --grabar --tipo impostor --etiqueta "companero 1 con gafas"

   · genuino  = un conductor REGISTRADO (se indica su cédula)
   · impostor = alguien que NO está registrado (pídele permiso; se graba solo un número
                por cuadro, NUNCA su imagen)
   Pide que se mueva un poco, gire la cara y cambie de expresión: así se parece a la realidad.

2) Ver el REPORTE:

    py calibrar.py --reporte

   Muestra, para cada umbral, qué porcentaje de cuadros de impostores se habría aceptado
   (falsos aceptados) y qué porcentaje de cuadros de conductores se habría rechazado
   (falsos rechazados), y cuántas sesiones habrían terminado con un conductor aceptado
   (con la regla real del servicio: 8 cuadros seguidos). También guarda calibracion.png.

Los datos quedan en calibracion.csv (NO va a GitHub: dice quién participó).
"""

import argparse
import csv
import os
import time
from datetime import datetime

from comun import (    # comun va antes que cv2: configura OpenCV
    ANCHO_MIN_RECONOCER, CARPETA, CIAN, GRIS, MARGEN, ROJO, UMBRAL, VERDE,
    BaseRostros, MotorFacial, abrir_camara, cedula_valida, dibujar_rostro, escribir,
    salir_con_error, ventana_cerrada,
)

import cv2
import numpy as np

from servicio_reconocimiento import CUADROS_SEGUIDOS

ARCHIVO = os.path.join(CARPETA, "calibracion.csv")
GRAFICA = os.path.join(CARPETA, "calibracion.png")
VENTANA = "Orbita - Calibracion"
COLUMNAS = ["sesion", "fecha_hora", "tipo", "etiqueta", "cedula_esperada", "cuadro",
            "mejor_cedula", "mejor_sim", "segunda_sim", "sim_esperada", "umbral_vecinos"]
UMBRALES = [round(u, 2) for u in np.arange(0.30, 0.801, 0.05)]


# ---------------------------------------------------------------
#  Grabar una sesión
# ---------------------------------------------------------------
def grabar(args):
    base = BaseRostros()
    if not base.conductores:
        salir_con_error("No hay conductores registrados.")
    if args.tipo == "genuino":
        if not cedula_valida(args.cedula) or args.cedula not in base.conductores:
            salir_con_error("Para una sesión 'genuino' indica --cedula de un conductor registrado.\n"
                            "Registrados: " + ", ".join(f"{c} ({d['nombre']})" for c, d in base.conductores.items()))
    elif args.cedula:
        salir_con_error("Una sesión 'impostor' no lleva --cedula (es alguien NO registrado).")

    motor = MotorFacial()
    camara = abrir_camara(args.camara)
    sesion = datetime.now().strftime("%Y%m%d-%H%M%S")
    filas = []
    print(f"\nGrabando {args.segundos:.0f} s ({args.tipo}: {args.etiqueta}). Q para terminar antes.")
    inicio = time.monotonic()
    try:
        while time.monotonic() - inicio < args.segundos:
            ok, cuadro = camara.read()
            if not ok:
                break
            vista = cuadro.copy()
            rostros = [r for r in motor.detectar(cuadro) if r[2] >= ANCHO_MIN_RECONOCER]
            if rostros:
                rostro = rostros[0]                       # el más cercano, igual que el servicio
                huella = motor.huella(cuadro, rostro)
                lista = base.similitudes(huella)
                mejor_sim, mejor_ced, _ = lista[0]
                segunda = lista[1][0] if len(lista) > 1 else 0.0
                esperada = next((s for s, c, _ in lista if c == args.cedula), "") if args.cedula else ""
                filas.append([sesion, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), args.tipo, args.etiqueta,
                              args.cedula or "", len(filas) + 1, mejor_ced, f"{mejor_sim:.4f}", f"{segunda:.4f}",
                              f"{esperada:.4f}" if esperada != "" else "", f"{base.umbral_de(mejor_ced, 0):.4f}"])
                dibujar_rostro(vista, rostro, f"{mejor_sim:.2f}", VERDE if args.tipo == "genuino" else ROJO)
            restante = max(0, args.segundos - (time.monotonic() - inicio))
            escribir(vista, f"Calibrando ({args.tipo}) - muevete un poco", 10, 30, CIAN, 0.7, 2)
            escribir(vista, f"Cuadros: {len(filas)}   Restan {restante:.0f} s   Q = terminar", 10, vista.shape[0] - 15, GRIS)
            cv2.imshow(VENTANA, vista)
            if (cv2.waitKey(1) & 0xFF) in (ord("q"), ord("Q"), 27) or ventana_cerrada(VENTANA):
                break
    finally:
        camara.release()
        cv2.destroyAllWindows()

    if not filas:
        salir_con_error("No se detectó ningún rostro: no se guardó nada.")
    nuevo = not os.path.isfile(ARCHIVO)
    with open(ARCHIVO, "a", newline="", encoding="utf-8-sig") as f:
        escritor = csv.writer(f)
        if nuevo:
            escritor.writerow(COLUMNAS)
        escritor.writerows(filas)
    sims = [float(f[7]) for f in filas]
    print(f"Guardados {len(filas)} cuadros en calibracion.csv  (similitud máx {max(sims):.2f}, "
          f"promedio {np.mean(sims):.2f})")


# ---------------------------------------------------------------
#  Reporte
# ---------------------------------------------------------------
def acepta(fila, umbral, margen):
    """¿La regla de comun.py habría aceptado a ALGÚN conductor en este cuadro? Devuelve su cédula o None."""
    exigido = max(umbral, fila["umbral_vecinos"])
    if fila["mejor_sim"] >= exigido and fila["mejor_sim"] - fila["segunda_sim"] >= margen:
        return fila["mejor_cedula"]
    return None


def sesion_acepta(cuadros, umbral, margen):
    """Simula el servicio: ¿hubo CUADROS_SEGUIDOS cuadros seguidos aceptando a la misma persona?"""
    racha_ced, racha = None, 0
    for fila in cuadros:
        ced = acepta(fila, umbral, margen)
        if ced and ced == racha_ced:
            racha += 1
        elif ced:
            racha_ced, racha = ced, 1
        else:
            racha_ced, racha = None, 0
        if racha >= CUADROS_SEGUIDOS:
            return racha_ced
    return None


def leer():
    if not os.path.isfile(ARCHIVO):
        salir_con_error("Todavía no hay datos. Primero graba sesiones con --grabar.")
    sesiones = {}
    with open(ARCHIVO, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            for k in ("mejor_sim", "segunda_sim", "umbral_vecinos"):
                r[k] = float(r[k])
            sesiones.setdefault(r["sesion"], []).append(r)
    return sesiones


def reporte(args):
    sesiones = leer()
    genuinas = {s: c for s, c in sesiones.items() if c[0]["tipo"] == "genuino"}
    impostoras = {s: c for s, c in sesiones.items() if c[0]["tipo"] == "impostor"}
    cg = [f for c in genuinas.values() for f in c]
    ci = [f for c in impostoras.values() for f in c]
    print(f"\nDatos: {len(genuinas)} sesiones de conductores ({len(cg)} cuadros), "
          f"{len(impostoras)} sesiones de impostores ({len(ci)} cuadros). Margen {args.margen:.2f}, "
          f"racha {CUADROS_SEGUIDOS} cuadros.\n")
    if ci:
        peor = max(ci, key=lambda f: f["mejor_sim"])
        print(f"Impostor más alto: {peor['mejor_sim']:.2f} ('{peor['etiqueta']}', se parecía a {peor['mejor_cedula']})")
    if cg:
        print(f"Conductor más bajo: {min(f['mejor_sim'] for f in cg):.2f}")
    print()
    print(f"{'Umbral':>6} | {'Cuadros impostor':>16} {'Cuadros conductor':>18} | {'Sesiones impostor':>17} {'Sesiones conductor':>18}")
    print(f"{'':>6} | {'aceptados':>16} {'rechazados':>18} | {'aceptadas':>17} {'rechazadas':>18}")
    print("-" * 84)
    filas_grafica = []
    for u in UMBRALES:
        far = sum(1 for f in ci if acepta(f, u, args.margen)) / len(ci) if ci else float("nan")
        # Un conductor es "bien aceptado" solo si lo acepta COMO ÉL MISMO.
        frr = sum(1 for f in cg if acepta(f, u, args.margen) != f["cedula_esperada"]) / len(cg) if cg else float("nan")
        si = sum(1 for c in impostoras.values() if sesion_acepta(c, u, args.margen))
        sg = sum(1 for c in genuinas.values() if sesion_acepta(c, u, args.margen) != c[0]["cedula_esperada"])
        marca = "  <- actual" if abs(u - UMBRAL) < 1e-6 else ""
        print(f"{u:>6.2f} | {far:>15.1%} {frr:>18.1%} | {si:>8}/{len(impostoras):<8} {sg:>9}/{len(genuinas):<8}{marca}")
        filas_grafica.append((u, far, frr))
    print("\nLo ideal: 0 sesiones de impostor aceptadas y el menor número de conductores rechazados.\n")
    graficar(filas_grafica)
    print(f"Gráfica guardada en {GRAFICA}")


def graficar(filas):
    """Gráfica simple con OpenCV (sin instalar nada más): falsos aceptados y falsos rechazados vs umbral."""
    an, al, m = 760, 460, 60
    img = np.full((al, an, 3), 255, np.uint8)
    x = lambda u: int(m + (u - UMBRALES[0]) / (UMBRALES[-1] - UMBRALES[0]) * (an - 2 * m))
    y = lambda p: int(al - m - p * (al - 2 * m))
    for p in (0, 0.25, 0.5, 0.75, 1.0):
        cv2.line(img, (m, y(p)), (an - m, y(p)), (225, 225, 225), 1)
        cv2.putText(img, f"{p:.0%}", (8, y(p) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (90, 90, 90), 1, cv2.LINE_AA)
    for u in UMBRALES[::2]:
        cv2.putText(img, f"{u:.2f}", (x(u) - 14, al - m + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (90, 90, 90), 1, cv2.LINE_AA)
    cv2.line(img, (x(UMBRAL), m), (x(UMBRAL), al - m), (150, 150, 150), 1)
    cv2.putText(img, f"actual {UMBRAL:.2f}", (x(UMBRAL) + 4, m + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (90, 90, 90), 1, cv2.LINE_AA)
    for indice, color, nombre in ((1, (40, 40, 200), "Impostores aceptados"), (2, (160, 110, 20), "Conductores rechazados")):
        puntos = [(x(f[0]), y(f[indice])) for f in filas if f[indice] == f[indice]]   # descarta NaN
        for a, b in zip(puntos, puntos[1:]):
            cv2.line(img, a, b, color, 2, cv2.LINE_AA)
        for p in puntos:
            cv2.circle(img, p, 4, color, -1, cv2.LINE_AA)
        cv2.putText(img, nombre, (an - m - 230, m + 18 + 22 * (indice - 1)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    cv2.putText(img, "Orbita - calibracion del umbral (por cuadro)", (m, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (30, 30, 30), 1, cv2.LINE_AA)
    cv2.imwrite(GRAFICA, img)


def main(argv=None):
    p = argparse.ArgumentParser(description="Calibración del umbral del reconocimiento facial de Orbita.")
    p.add_argument("--grabar", action="store_true", help="Grabar una sesión con la cámara")
    p.add_argument("--reporte", action="store_true", help="Mostrar la tabla y guardar la gráfica")
    p.add_argument("--tipo", choices=["genuino", "impostor"])
    p.add_argument("--cedula", help="Cédula del conductor (solo para --tipo genuino)")
    p.add_argument("--etiqueta", help='Descripción corta, por ejemplo "jesus con gafas" (sin datos personales)')
    p.add_argument("--segundos", type=float, default=15)
    p.add_argument("--camara", type=int, default=0)
    p.add_argument("--margen", type=float, default=MARGEN, help=f"Ventaja mínima sobre el segundo (por defecto {MARGEN})")
    args = p.parse_args(argv)
    if args.grabar:
        if not args.tipo or not args.etiqueta:
            salir_con_error('Indica --tipo genuino|impostor y --etiqueta "...".')
        grabar(args)
    elif args.reporte:
        reporte(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
