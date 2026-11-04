"""
reconocer.py â€” Prueba en vivo del reconocimiento facial (P3-S1, paso 5).

    py reconocer.py
    py reconocer.py --camara 1
    py reconocer.py --umbral 0.60

Abre la cÃ¡mara y marca cada rostro:
  Â· verde  = conductor reconocido, con su nombre y la similitud
  Â· rojo   = "Desconocido" (nadie supera su umbral: similitud < umbral exigido)
             o "Dudoso" (supera el umbral pero no le saca ventaja al segundo mÃ¡s parecido)
  Â· gris   = rostro muy lejos para evaluarlo
Al cerrar (tecla Q) imprime un resumen para la tabla de pruebas del informe.
"""

import argparse

from comun import (    # comun va antes que cv2: configura OpenCV para que no llene la pantalla de avisos
    ANCHO_MIN_RECONOCER, CIAN, GRIS, ROJO, UMBRAL, VERDE,
    BaseRostros, MotorFacial, abrir_camara, dibujar_rostro, escribir,
    salir_con_error, ventana_cerrada,
)

import cv2

VENTANA = "Orbita - Prueba de reconocimiento"
DESCONOCIDO = "Desconocido"      # nadie supera su umbral
DUDOSO = "Dudoso"                # supera el umbral, pero se parece casi igual a otro conductor


def imprimir_resumen(conteo, cuadros, umbral):
    print(f"\nResumen de la prueba ({cuadros} cuadros de video, umbral {umbral:.2f}):")
    if not conteo:
        print("  No se evaluÃ³ ningÃºn rostro.")
        return
    print(f"  {'Resultado':<32} {'Cuadros':>8}   Similitud promedio")
    for etiqueta, (veces, suma) in sorted(conteo.items(), key=lambda par: -par[1][0]):
        print(f"  {etiqueta:<32} {veces:>8}   {suma / veces:.2f}")
    print("  (Para 'Desconocido', la similitud es la del conductor mÃ¡s parecido.)\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prueba en vivo del reconocimiento facial de Orbita.")
    parser.add_argument("--camara", type=int, default=0, help="NÃºmero de la cÃ¡mara (0 = la del portÃ¡til)")
    parser.add_argument("--umbral", type=float, default=UMBRAL, help=f"Similitud mÃ­nima para aceptar (por defecto {UMBRAL})")
    args = parser.parse_args(argv)
    if not 0 < args.umbral < 1:
        salir_con_error("El umbral debe estar entre 0 y 1 (por ejemplo 0.55).")

    base = BaseRostros()
    if not base.conductores:
        salir_con_error('No hay conductores registrados. Primero:  py registrar.py --cedula ... --nombre "..."')
    print("\nConductores registrados: " + ", ".join(c["nombre"] for c in base.conductores.values()))

    print("Cargando modelos...")
    motor = MotorFacial()
    camara = abrir_camara(args.camara)
    cv2.namedWindow(VENTANA, cv2.WINDOW_AUTOSIZE)
    print("Se abriÃ³ la ventana de la cÃ¡mara. Presiona Q para terminar.")

    conteo = {}          # etiqueta -> [cuadros, suma de similitudes]
    cuadros = 0
    try:
        while True:
            ok, cuadro = camara.read()
            if not ok:
                print("La cÃ¡mara dejÃ³ de enviar imÃ¡genes.")
                break
            cuadros += 1
            vista = cuadro.copy()

            for rostro in motor.detectar(cuadro):
                if rostro[2] < ANCHO_MIN_RECONOCER:
                    dibujar_rostro(vista, rostro, "Acercate", GRIS)
                    continue
                d = base.evaluar(motor.huella(cuadro, rostro), args.umbral)
                similitud = d["similitud"]
                if d["cedula"]:
                    etiqueta, color, texto = d["nombre"], VERDE, f"{d['nombre']}  {similitud:.2f}"
                elif d["motivo"] == "poca_ventaja":
                    etiqueta, color = DUDOSO, ROJO
                    texto = f"{DUDOSO}  {similitud:.2f} vs {d['segunda']:.2f}"
                else:
                    etiqueta, color = DESCONOCIDO, ROJO
                    texto = f"{DESCONOCIDO}  {similitud:.2f} < {d['umbral']:.2f}"
                dibujar_rostro(vista, rostro, texto, color)
                registro = conteo.setdefault(etiqueta, [0, 0.0])
                registro[0] += 1
                registro[1] += similitud

            escribir(vista, f"Umbral {args.umbral:.2f}   |   Q = salir", 10, vista.shape[0] - 15, CIAN)
            cv2.imshow(VENTANA, vista)

            tecla = cv2.waitKey(1) & 0xFF
            if tecla in (ord("q"), ord("Q"), 27) or ventana_cerrada(VENTANA):
                break
    finally:
        camara.release()
        cv2.destroyAllWindows()

    imprimir_resumen(conteo, cuadros, args.umbral)


if __name__ == "__main__":
    main()
