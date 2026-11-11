"""
prueba_pagina.py — prueba la página web (web/index.html) sin abrir el navegador.

Qué revisa:
  1. Que el JavaScript de la página no tenga errores de sintaxis (con Node.js).
  2. P3-S3: las funciones de los sensores de la carga (bloque P3S3-INICIO ... P3S3-FIN),
     ejecutadas en Node con un Leaflet de mentira:
       - con eventos: dibuja una marca por evento con ubicación y arma el resumen;
       - sin la clave "sensores" (servidor viejo) o con sensores = null: no dibuja nada
         y no rompe la página.
  3. Con --url, además mira la página publicada y la API de ese servidor.

Uso (en el PC o en la EC2; necesita Node.js instalado para los puntos 1 y 2):
    py pruebas/prueba_pagina.py
    py pruebas/prueba_pagina.py --url https://jesuscotes.sytes.net
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
PAGINA = os.path.join(AQUI, "..", "web", "index.html")
resultados = {"ok": 0, "fallo": 0}


def revisar(nombre, condicion, detalle=""):
    resultados["ok" if condicion else "fallo"] += 1
    print(f"  {'OK   ' if condicion else 'FALLO'}  {nombre}" + ("" if condicion or not detalle else f"  ->  {detalle}"))
    return condicion


def scripts_de(html):
    """El JavaScript propio de la página (los <script> sin src)."""
    return [m.group(1) for m in re.finditer(r"<script>(.*?)</script>", html, re.S)]


# Arnés para Node: un Leaflet de mentira que cuenta las marcas, y las ayudas que el bloque usa.
ARNES = r"""
var marcas = [];
var L = {
  divIcon: function (o) { return o; },
  marker: function (pos, o) {
    var m = { pos: pos, o: o, popup: '', bindPopup: function (t) { this.popup = t; return this; },
              addTo: function (capa) { marcas.push(this); return this; } };
    return m;
  }
};
var modoActual = 'envios', mapa = null, mapaEnvios = null, mapaHistorico = null;
function horaDe(iso) { return iso ? iso.substring(11, 16) : '—'; }
/*BLOQUE*/
var salida = {};
var conEventos = {
  estado: 'en_ruta',
  sensores: {
    eventos: [
      { id: 1, tipo: 'luz_abierta', valor: 812, lat: 11.01, lon: -74.85, avance_m: 0, momento: '2026-11-10T08:00:00-05:00' },
      { id: 2, tipo: 'frenada', valor: 0.62, lat: 11.012, lon: -74.84, avance_m: 1520, momento: '2026-11-10T08:10:00-05:00' },
      { id: 3, tipo: 'hueco', valor: 1.1, lat: null, lon: null, avance_m: null, momento: '2026-11-10T08:12:00-05:00' },
      { id: 4, tipo: 'tipo_futuro', valor: 1, lat: 11.0, lon: -74.8, avance_m: 10, momento: '2026-11-10T08:13:00-05:00' }
    ],
    conteo: { frenada: 1, hueco: 1, curva: 0, luz_abierta: 1, luz_cerrada: 0 },
    carga: { estado: 'abierta', nivel: 812, ultimo_contacto: '2026-11-10T08:12:30-05:00', hace_s: 5, en_linea: true }
  }
};
marcas = []; dibujarEventos({}, conEventos); salida.marcas = marcas.length;
salida.popupFrenada = (marcas.filter(function (m) { return m.o.title === 'Frenada brusca'; })[0] || {}).popup || '';
salida.detalle = htmlSensores(conEventos, 8);
salida.corto = htmlSensoresCorto(conEventos);
marcas = []; dibujarEventos({}, { estado: 'en_ruta' }); salida.marcasSinClave = marcas.length;
marcas = []; dibujarEventos({}, { estado: 'en_ruta', sensores: null }); salida.marcasNull = marcas.length;
salida.detalleSinClave = htmlSensores({ estado: 'en_ruta' });
salida.detalleNull = htmlSensores({ estado: 'en_ruta', sensores: null });
salida.cortoSinClave = htmlSensoresCorto({ estado: 'en_ruta' });
var vacio = { estado: 'asignado', sensores: { eventos: [], conteo: {}, carga: { estado: null, ultimo_contacto: null } } };
salida.detalleVacio = htmlSensores(vacio);
salida.cortoVacio = htmlSensoresCorto(vacio);
console.log(JSON.stringify(salida));
"""


def probar_con_node(html):
    node = shutil.which("node")
    if not node:
        print("  (Node.js no está instalado: se saltan las pruebas de JavaScript)")
        return
    js = "\n;\n".join(scripts_de(html))
    with tempfile.TemporaryDirectory() as carpeta:
        archivo = os.path.join(carpeta, "pagina.js")
        with open(archivo, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run([node, "--check", archivo], capture_output=True, text=True)
        revisar("El JavaScript de la página no tiene errores de sintaxis", r.returncode == 0, r.stderr[:400])

        m = re.search(r"// P3S3-INICIO.*?// P3S3-FIN", html, re.S)
        if not revisar("La página tiene el bloque de sensores (P3S3-INICIO ... P3S3-FIN)", m is not None):
            return
        archivo = os.path.join(carpeta, "arnes.js")
        with open(archivo, "w", encoding="utf-8") as f:
            f.write(ARNES.replace("/*BLOQUE*/", m.group(0)))
        r = subprocess.run([node, archivo], capture_output=True, text=True, encoding="utf-8")
        if not revisar("Las funciones de sensores corren en Node sin errores", r.returncode == 0, r.stderr[:400]):
            return
        s = json.loads(r.stdout.strip().splitlines()[-1])

    print("\n--- Con eventos ---")
    revisar("Dibuja una marca por evento con ubicación (2 de 4: sin GPS y tipo desconocido se saltan)",
            s["marcas"] == 2, s["marcas"])
    revisar("La ventana de la frenada dice tipo, intensidad, hora y km",
            all(t in s["popupFrenada"] for t in ("Frenada brusca", "0,62 g", "08:10", "km 1,5")), s["popupFrenada"])
    revisar("El detalle trae el título, el estado 'Carga abierta' y el último contacto",
            all(t in s["detalle"] for t in ("Sensores de la carga", "Carga abierta", "Sensores en línea")), s["detalle"][:300])
    revisar("El detalle cuenta 1 frenada y 1 hueco", "1 frenadas" in s["detalle"] and "1 huecos" in s["detalle"])
    revisar("La lista corta muestra primero el evento más reciente (hueco antes que frenada)",
            s["detalle"].find("Hueco") < s["detalle"].find("Frenada brusca") and s["detalle"].find("Hueco") > 0)
    revisar("Un evento con ubicación se puede tocar para ir al mapa", "irAEvento(11.012,-74.84)" in s["detalle"])
    revisar("La tarjeta de Tiempo real resume la carga y los contadores",
            "Carga abierta" in s["corto"] and "en línea" in s["corto"], s["corto"])

    print("\n--- Servidor viejo o sin datos ---")
    revisar("Sin la clave 'sensores' no dibuja nada", s["marcasSinClave"] == 0)
    revisar("Con sensores = null no dibuja nada", s["marcasNull"] == 0)
    revisar("Sin sensores, el detalle y la tarjeta quedan vacíos (la página sigue igual)",
            s["detalleSinClave"] == "" and s["detalleNull"] == "" and s["cortoSinClave"] == "")
    revisar("Con sensores pero sin eventos dice 'Sin eventos de la carga todavía'",
            "Sin eventos de la carga todavía" in s["detalleVacio"] and "Sin datos de la carga" in s["detalleVacio"])
    revisar("...y la tarjeta de Tiempo real no muestra nada", s["cortoVacio"] == "")


def probar_servidor(url):
    print(f"\n--- Servidor {url} ---")
    try:
        with urllib.request.urlopen(url + "/", timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
        revisar("La página publicada ya tiene P3-S3", "P3S3-INICIO" in html)
        revisar("La página publicada conoce la alerta carga_abierta", "carga_abierta" in html)
    except Exception as e:
        revisar("La página responde", False, e)
    try:
        with urllib.request.urlopen(url + "/api/envios?estado=todos&limite=1", timeout=30) as r:
            lista = json.loads(r.read().decode("utf-8"))["envios"]
        if lista:
            with urllib.request.urlopen(url + f"/api/envios/{lista[0]['id']}?ruta=0", timeout=30) as r:
                envio = json.loads(r.read().decode("utf-8"))["envio"]
            revisar("El detalle de un envío trae la clave 'sensores'", "sensores" in envio)
            revisar("...con eventos, conteo y carga (p3s3.sql ejecutado)",
                    isinstance(envio.get("sensores"), dict) and {"eventos", "conteo", "carga"} <= set(envio["sensores"]),
                    envio.get("sensores"))
        else:
            print("  (no hay envíos para revisar la API)")
    except Exception as e:
        revisar("La API de envíos responde", False, e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="también revisar este servidor, por ejemplo https://jesuscotes.sytes.net")
    args = parser.parse_args()
    with open(PAGINA, encoding="utf-8") as f:
        html = f.read()
    print(f"Probando {os.path.normpath(PAGINA)}")
    print("\n--- JavaScript ---")
    probar_con_node(html)
    revisar("textoAlerta conoce la alerta 'carga_abierta'", "a.tipo === 'carga_abierta'" in html)
    revisar("El detalle del envío dibuja los eventos en el mapa", "dibujarEventos(capaEnvio, e)" in html)
    revisar("Tiempo real dibuja los eventos del envío en vivo", "dibujarEventos(capaEventosVivo, e)" in html)
    revisar("Históricos dibuja los eventos del envío", "dibujarEventos(capaPlanHist, e)" in html)
    if args.url:
        probar_servidor(args.url.rstrip("/"))


if __name__ == "__main__":
    try:
        main()
    finally:
        print("\n" + "-" * 50)
        if resultados["fallo"] == 0 and resultados["ok"]:
            print(f"TODO BIEN: {resultados['ok']} pruebas pasaron.")
        else:
            print(f"{resultados['ok']} pasaron y {resultados['fallo']} fallaron.")
