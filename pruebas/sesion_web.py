"""
sesion_web.py — P3-S4: las pruebas inician sesión en la página antes de usar la API.

Desde P3-S4, /datos, Históricos y la API de envíos piden la cookie de sesión.
Cada prueba llama una vez a:

    import sesion_web
    sesion_web.iniciar("https://jesuscotes.sytes.net")

y a partir de ahí TODOS los urllib.request.urlopen() de ese programa llevan la
cookie (también los que tienen "Authorization: Bearer" de la app: no estorba).

¿Con qué usuario? En este orden:
  1. variables de entorno ORBITA_USUARIO y ORBITA_CLAVE
  2. el usuario "pruebas" con la contraseña del archivo ~/clave_pruebas_web.txt
     (chmod 600; NUNCA va a GitHub). Se crea en la EC2 con:
        python3 crear_usuario.py --usuario pruebas --nombre "Pruebas automáticas" \
            --rol operador --clave-archivo ~/clave_pruebas_web.txt
"""

import json
import os
import sys
import urllib.error
import urllib.request

ARCHIVO_CLAVE = os.path.expanduser("~/clave_pruebas_web.txt")


class _ConCookie(urllib.request.BaseHandler):
    def __init__(self, cookie):
        self.cookie = cookie

    def http_request(self, pedido):
        pedido.add_unredirected_header("Cookie", self.cookie)
        return pedido

    https_request = http_request


def _credenciales():
    usuario, clave = os.environ.get("ORBITA_USUARIO"), os.environ.get("ORBITA_CLAVE")
    if usuario and clave:
        return usuario, clave
    if os.path.exists(ARCHIVO_CLAVE):
        with open(ARCHIVO_CLAVE, encoding="utf-8") as f:
            return "pruebas", f.read().strip()
    sys.exit("Falta el usuario de pruebas de la página. Defina ORBITA_USUARIO y ORBITA_CLAVE, "
             f"o cree {ARCHIVO_CLAVE} (ver pruebas/sesion_web.py).")


def iniciar(url):
    usuario, clave = _credenciales()
    pedido = urllib.request.Request(url.rstrip("/") + "/api/sesion/ingresar", method="POST",
                                    data=json.dumps({"usuario": usuario, "clave": clave}).encode("utf-8"),
                                    headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=30) as r:
            cabecera = r.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        sys.exit(f"No se pudo iniciar sesión en la página como {usuario}: {e.code} {e.read().decode('utf-8', 'replace')}")
    cookie = cabecera.split(";", 1)[0]
    if not cookie.startswith("orbita_sesion="):
        sys.exit("El servidor no devolvió la cookie de sesión (¿tiene el código de P3-S4?).")
    urllib.request.install_opener(urllib.request.build_opener(_ConCookie(cookie)))
    print(f"Sesión de la página iniciada como {usuario}.")
    return cookie
