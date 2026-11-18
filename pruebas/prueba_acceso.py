"""
prueba_acceso.py — P3-S4: prueba el inicio de sesión de la página y los permisos por rol.

Uso, en la EC2 (necesita un ADMIN y el usuario "pruebas" de sesion_web.py):
    ORBITA_ADMIN_USUARIO=jcotes ORBITA_ADMIN_CLAVE='...' python3 prueba_acceso.py --url http://localhost:8081

Crea (la primera vez) el usuario "pruebas.consulta" con rol consulta y lo deja DESACTIVADO
al terminar. No crea envíos ni toca camiones.
"""
import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

URL = "http://localhost:8080"
resultados = {"ok": 0, "fallo": 0}


def pedir(metodo, ruta, datos=None, cookie=None):
    cuerpo = json.dumps(datos).encode("utf-8") if datos is not None else (b"" if metodo == "POST" else None)
    pedido = urllib.request.Request(URL + ruta, data=cuerpo, method=metodo)
    pedido.add_header("Content-Type", "application/json")
    if cookie:
        pedido.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(pedido, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}"), r.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}"), e.headers.get("Set-Cookie", "")
        except ValueError:
            return e.code, {}, ""


def revisar(nombre, condicion, detalle=""):
    resultados["ok" if condicion else "fallo"] += 1
    print(("  OK     " if condicion else "  FALLO  ") + nombre + ("" if condicion else f"   ({detalle})"))
    return condicion


def ingresar(usuario, clave):
    estado, r, galleta = pedir("POST", "/api/sesion/ingresar", {"usuario": usuario, "clave": clave})
    return estado, r, galleta.split(";", 1)[0] if galleta else None


def main():
    global URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL)
    URL = parser.parse_args().url.rstrip("/")
    admin_u, admin_c = os.environ.get("ORBITA_ADMIN_USUARIO"), os.environ.get("ORBITA_ADMIN_CLAVE")
    if not (admin_u and admin_c):
        sys.exit("Defina ORBITA_ADMIN_USUARIO y ORBITA_ADMIN_CLAVE (un usuario admin).")
    with open(os.path.expanduser("~/clave_pruebas_web.txt"), encoding="utf-8") as f:
        clave_operador = f.read().strip()
    print(f"Probando {URL}\n\n--- Sin sesión ---")
    for metodo, ruta in [("GET", "/datos"), ("GET", "/recorridos"), ("GET", "/api/envios"), ("GET", "/api/camiones"),
                         ("POST", "/api/envios/cotizar"), ("GET", "/api/usuarios"), ("GET", "/api/sesion")]:
        estado, r, _ = pedir(metodo, ruta, {} if metodo == "POST" else None)
        revisar(f"{metodo} {ruta} pide sesión (401)", estado == 401, estado)
    estado, _, _ = pedir("GET", "/api/catalogo")
    revisar("/api/catalogo sigue público", estado == 200, estado)
    estado, r, _ = pedir("GET", "/api/camiones/yo")
    revisar("La app sigue con su propio token (401 sin Bearer)", estado == 401 and "sin_sesion" not in r, r)
    estado, r, _ = pedir("GET", "/datos", cookie="orbita_sesion=" + "x" * 43)
    revisar("Una cookie inventada no sirve", estado == 401, estado)

    print("\n--- Ingreso ---")
    estado, r, _ = ingresar("pruebas", "equivocada")
    revisar("Contraseña equivocada: 401", estado == 401, r)
    estado, r, _ = ingresar("no.existe", "loquesea123")
    revisar("Usuario inexistente: el mismo mensaje", estado == 401 and r.get("error") == "Usuario o contraseña incorrecta.", r)
    estado, r, operador = ingresar(" PRUEBAS ", clave_operador)
    revisar("Ingresa el operador (sin importar mayúsculas ni espacios)", estado == 200 and operador, r)
    _, _, galleta = pedir("POST", "/api/sesion/ingresar", {"usuario": "pruebas", "clave": clave_operador})
    revisar("La cookie es HttpOnly, Secure y SameSite=Strict, por 12 h",
            all(x in galleta for x in ("HttpOnly", "Secure", "SameSite=Strict", "Max-Age=43200")), galleta)
    estado, r, _ = pedir("GET", "/api/sesion", cookie=operador)
    revisar("GET /api/sesion dice quién es", estado == 200 and r["usuario"]["rol"] == "operador", r)
    estado, admin_r, admin = ingresar(admin_u, admin_c)
    if not revisar("Ingresa el admin", estado == 200 and admin_r["usuario"]["rol"] == "admin", admin_r):
        sys.exit(1)

    print("\n--- Operador ---")
    for metodo, ruta in [("GET", "/datos"), ("GET", "/api/envios"), ("GET", "/api/camiones")]:
        estado, _, _ = pedir(metodo, ruta, cookie=operador)
        revisar(f"Operador: {metodo} {ruta} (200)", estado == 200, estado)
    estado, r, _ = pedir("POST", "/api/envios/cotizar", {}, cookie=operador)
    revisar("Operador puede cotizar (400 por datos, no 403)", estado == 400, (estado, r))
    estado, _, _ = pedir("GET", "/api/usuarios", cookie=operador)
    revisar("Operador NO ve los usuarios (403)", estado == 403, estado)

    print("\n--- Consulta ---")
    clave_consulta = secrets.token_urlsafe(12)
    estado, r, _ = pedir("POST", "/api/usuarios", {"usuario": "pruebas.consulta", "nombre": "Prueba de consulta",
                                                   "rol": "consulta", "clave": clave_consulta}, cookie=admin)
    if estado == 409:
        estado, r, _ = pedir("POST", "/api/usuarios/pruebas.consulta",
                             {"clave": clave_consulta, "activo": True, "rol": "consulta"}, cookie=admin)
    revisar("El admin crea (o reactiva) un usuario de consulta", estado in (200, 201), r)
    estado, r, _ = pedir("POST", "/api/usuarios", {"usuario": "Malo Con Espacios", "nombre": "X", "rol": "consulta",
                                                   "clave": "corta"}, cookie=admin)
    revisar("Rechaza un usuario mal escrito", estado == 400 and r.get("campo") == "usuario", r)
    estado, r, consulta = ingresar("pruebas.consulta", clave_consulta)
    revisar("Ingresa el de consulta", estado == 200, r)
    estado, _, _ = pedir("GET", "/api/envios", cookie=consulta)
    revisar("Consulta ve los envíos (200)", estado == 200, estado)
    for ruta in ("/api/envios/cotizar", "/api/envios", "/api/envios/1/cancelar", "/api/alertas/1/vista"):
        estado, _, _ = pedir("POST", ruta, {}, cookie=consulta)
        revisar(f"Consulta NO puede POST {ruta} (403)", estado == 403, estado)
    estado, r, _ = pedir("POST", "/api/sesion/clave", {"actual": "mala", "nueva": "otraClave123"}, cookie=consulta)
    revisar("Consulta sí puede intentar cambiar su contraseña (400 por la actual)", estado == 400 and r.get("campo") == "actual", r)
    nueva = secrets.token_urlsafe(12)
    estado, r, _ = pedir("POST", "/api/sesion/clave", {"actual": clave_consulta, "nueva": nueva}, cookie=consulta)
    revisar("...y la cambia", estado == 200, r)
    estado, _, consulta = ingresar("pruebas.consulta", nueva)
    revisar("Ingresa con la contraseña nueva", estado == 200, estado)

    print("\n--- Admin ---")
    estado, r, _ = pedir("GET", "/api/usuarios", cookie=admin)
    revisar("El admin ve la lista de usuarios", estado == 200 and any(u["usuario"] == "pruebas" for u in r["usuarios"]), r)
    estado, r, _ = pedir("POST", f"/api/usuarios/{admin_u}", {"rol": "consulta"}, cookie=admin)
    revisar("El admin no puede quitarse su propio rol", estado == 409, r)
    estado, r, _ = pedir("POST", "/api/usuarios/pruebas.consulta", {"activo": False}, cookie=admin)
    revisar("El admin desactiva al de consulta", estado == 200 and r["usuario"]["activo"] is False, r)
    estado, _, _ = pedir("GET", "/api/envios", cookie=consulta)
    revisar("...y su sesión abierta deja de servir al instante", estado == 401, estado)
    estado, r, _ = ingresar("pruebas.consulta", nueva)
    revisar("...y ya no puede ingresar", estado == 403, r)

    print("\n--- Salir ---")
    estado, _, galleta = pedir("POST", "/api/sesion/salir", cookie=operador)
    revisar("Salir responde 200 y borra la cookie", estado == 200 and "Max-Age=0" in galleta, galleta)
    estado, _, _ = pedir("GET", "/api/envios", cookie=operador)
    revisar("Después de salir, esa cookie ya no sirve", estado == 401, estado)
    pedir("POST", "/api/sesion/salir", cookie=admin)

    print("\n" + "-" * 50)
    if resultados["fallo"]:
        print(f"HAY FALLOS: {resultados['fallo']} de {resultados['ok'] + resultados['fallo']}")
        sys.exit(1)
    print(f"TODO BIEN: {resultados['ok']} pruebas pasaron.")


if __name__ == "__main__":
    main()
