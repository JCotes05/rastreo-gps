"""
acceso.py — P3-S4: acceso a la página de Orbita con usuario y contraseña.

La página es un archivo estático (Nginx lo sirve sin preguntar nada), pero todos
los DATOS que pide pasan por servidor_web.py, que antes de responder llama a:

    bloqueo = acceso.revisar(metodo, ruta, cabecera_cookie)
    if bloqueo: responder bloqueo (401 sin sesión, 403 sin permiso)

La sesión viaja en la cookie "orbita_sesion" (HttpOnly, Secure, SameSite=Strict):
el JavaScript de la página no la puede leer y otro sitio no la puede usar. Dura
12 horas. En la base solo se guarda el SHA-256 del token.

Roles (cada uno puede todo lo del anterior):
    consulta   ver Tiempo real, Históricos, Envíos y alertas (solo GET)
    operador   además crear, cotizar y cancelar envíos y marcar alertas como vistas
    admin      además crear usuarios, cambiar su rol, desactivarlos y cambiar su contraseña

Lo que NO pide sesión de la página:
    /api/camiones/...   la app del camión (usa su propio token "Bearer")
    /api/catalogo       listas fijas (configuraciones, carrocerías...)
    /api/sesion, /api/sesion/ingresar

Rutas de este módulo (servidor_web.py se las pasa con atender()):
    GET  /api/sesion                 quién soy (401 si no hay sesión)
    POST /api/sesion/ingresar        {"usuario", "clave"}  -> pone la cookie
    POST /api/sesion/salir           cierra la sesión y borra la cookie
    POST /api/sesion/clave           {"actual", "nueva"}   cambia la contraseña propia
    GET  /api/usuarios               (admin) lista de usuarios
    POST /api/usuarios               (admin) {"usuario", "nombre", "rol", "clave"}
    POST /api/usuarios/<usuario>     (admin) {"nombre"?, "rol"?, "activo"?, "clave"?}

El primer administrador se crea en la EC2 con:  python3 crear_usuario.py
"""

import re
import sys
import threading
import time
import traceback
from http.cookies import CookieError, SimpleCookie

import api_envios
from api_envios import ErrorAPI, _bd

COOKIE = "orbita_sesion"
HORAS_SESION = 12
CACHE_S = 30                    # cuánto se recuerda una sesión ya revisada (evita ir a la base cada 5 s)
CLAVE_MIN, CLAVE_MAX = 8, 128

ROLES = {"consulta": "Consulta", "operador": "Operador", "admin": "Administrador"}
NIVEL = {"consulta": 1, "operador": 2, "admin": 3}
PATRON_USUARIO = re.compile(r"^[a-z0-9._-]{3,30}$")


# ---------------------------------------------------------------------
# Qué necesita cada ruta
# ---------------------------------------------------------------------
def _rol_necesario(metodo, ruta):
    """None si la ruta es pública; si no, el rol mínimo que la puede usar."""
    ruta = ruta.rstrip("/") or "/"
    if ruta.startswith("/api/camiones/") or ruta == "/api/catalogo":
        return None                                 # la app del camión y listas fijas
    if ruta in ("/api/sesion", "/api/sesion/ingresar"):
        return None                                 # este módulo responde 401 si hace falta
    if ruta == "/api/usuarios" or ruta.startswith("/api/usuarios/"):
        return "admin"
    if ruta.startswith("/api/sesion/"):
        return "consulta"                           # salir y cambiar la contraseña propia
    if metodo == "GET":
        return "consulta"
    return "operador"                               # crear, cotizar, cancelar, marcar alertas


def revisar(metodo, ruta, cabecera_cookie):
    """None si puede seguir; si no, (código, respuesta) para devolver tal cual."""
    rol = _rol_necesario(metodo, ruta)
    if rol is None:
        return None
    try:
        sesion = sesion_de(cabecera_cookie)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 500, {"error": "Error interno del servidor."}
    if sesion is None:
        return 401, {"error": "Inicie sesión.", "sin_sesion": True}
    if NIVEL[sesion["rol"]] < NIVEL[rol]:
        return 403, {"error": f"Su usuario ({ROLES[sesion['rol']].lower()}) no tiene permiso para esto."}
    return None


# ---------------------------------------------------------------------
# Sesiones
# ---------------------------------------------------------------------
_cache = {}                     # token_hash -> (sesion, momento en que deja de valer)
_candado = threading.Lock()


def _token_de(cabecera_cookie):
    if not cabecera_cookie:
        return None
    try:
        galleta = SimpleCookie()
        galleta.load(cabecera_cookie)
    except CookieError:
        return None
    m = galleta.get(COOKIE)
    return m.value if m and 20 <= len(m.value) <= 100 else None


def sesion_de(cabecera_cookie):
    """{"usuario", "nombre", "rol", "token_hash", "vence"} o None."""
    token = _token_de(cabecera_cookie)
    if token is None:
        return None
    clave = api_envios._hash_token(token)
    ahora = time.monotonic()
    with _candado:
        guardada = _cache.get(clave)
        if guardada and guardada[1] > ahora:
            return guardada[0]
    with _bd() as cur:
        cur.execute("""SELECT u.usuario, u.nombre, u.rol, s.vence FROM sesiones_web s
                       JOIN usuarios u ON u.usuario = s.usuario
                       WHERE s.token_hash = %s AND s.vence > now() AND u.activo""", (clave,))
        fila = cur.fetchone()
    sesion = None if fila is None else {**fila, "token_hash": clave}
    with _candado:
        if sesion is None:
            _cache.pop(clave, None)
        else:
            _cache[clave] = (sesion, ahora + CACHE_S)
    return sesion


def _olvidar(usuario=None, token_hash=None):
    """Saca de la memoria las sesiones de un usuario (o una sola) para que el cambio se note ya."""
    with _candado:
        for clave in [k for k, (s, _) in _cache.items() if k == token_hash or s["usuario"] == usuario]:
            _cache.pop(clave, None)


def _cookie(token, segundos):
    return ("Set-Cookie", f"{COOKIE}={token}; Path=/; Max-Age={segundos}; HttpOnly; Secure; SameSite=Strict")


def _usuario_json(u):
    return {"usuario": u["usuario"], "nombre": u["nombre"], "rol": u["rol"], "rol_nombre": ROLES[u["rol"]]}


def _exigir_sesion(p):
    sesion = sesion_de(p.cookie)
    if sesion is None:
        raise ErrorAPI(401, "Inicie sesión.")
    return sesion


def quien_soy(p):
    sesion = sesion_de(p.cookie)
    if sesion is None:
        return 401, {"error": "Inicie sesión.", "sin_sesion": True}
    return 200, {"usuario": _usuario_json(sesion), "vence": api_envios._iso(sesion["vence"])}


def ingresar(p):
    usuario = p.datos.get("usuario") if isinstance(p.datos.get("usuario"), str) else ""
    usuario = usuario.strip().lower()
    clave = p.datos.get("clave") if isinstance(p.datos.get("clave"), str) else ""
    llave_bloqueo = "web:" + usuario
    api_envios._revisar_bloqueo(llave_bloqueo)
    with _bd() as cur:
        cur.execute("SELECT usuario, nombre, rol, activo, clave_hash FROM usuarios WHERE usuario = %s", (usuario,))
        u = cur.fetchone()
        valida = api_envios._verificar_clave(clave, u["clave_hash"] if u else api_envios._HASH_FALSO) and u is not None
        if not valida:
            api_envios._contar_fallo(llave_bloqueo)
            raise ErrorAPI(401, "Usuario o contraseña incorrecta.")
        if not u["activo"]:
            raise ErrorAPI(403, "Este usuario está desactivado. Hable con un administrador.")
        token = api_envios._nuevo_token()
        cur.execute(f"""INSERT INTO sesiones_web (token_hash, usuario, vence, ip)
                        VALUES (%s, %s, now() + interval '{HORAS_SESION} hours', %s)""",
                    (api_envios._hash_token(token), u["usuario"], (p.ip or "")[:45] or None))
        cur.execute("UPDATE usuarios SET ultimo_ingreso = now() WHERE usuario = %s", (u["usuario"],))
    api_envios._limpiar_fallos(llave_bloqueo)
    return 200, {"usuario": _usuario_json(u)}, [_cookie(token, HORAS_SESION * 3600)]


def salir(p):
    sesion = sesion_de(p.cookie)
    if sesion is not None:
        with _bd() as cur:
            cur.execute("UPDATE sesiones_web SET vence = now() WHERE token_hash = %s", (sesion["token_hash"],))
        _olvidar(token_hash=sesion["token_hash"])
    return 200, {"ok": True}, [_cookie("", 0)]


def _clave_nueva(d, campo):
    clave = d.get(campo)
    if not isinstance(clave, str) or not (CLAVE_MIN <= len(clave) <= CLAVE_MAX):
        raise ErrorAPI(400, f"La contraseña debe tener entre {CLAVE_MIN} y {CLAVE_MAX} caracteres.", campo)
    return clave


def cambiar_mi_clave(p):
    sesion = _exigir_sesion(p)
    actual = p.datos.get("actual") if isinstance(p.datos.get("actual"), str) else ""
    nueva = _clave_nueva(p.datos, "nueva")
    if nueva == actual:
        raise ErrorAPI(400, "La contraseña nueva debe ser distinta de la actual.", "nueva")
    with _bd() as cur:
        cur.execute("SELECT clave_hash FROM usuarios WHERE usuario = %s FOR UPDATE", (sesion["usuario"],))
        if not api_envios._verificar_clave(actual, cur.fetchone()["clave_hash"]):
            raise ErrorAPI(400, "La contraseña actual no es correcta.", "actual")
        cur.execute("UPDATE usuarios SET clave_hash = %s WHERE usuario = %s",
                    (api_envios._hash_clave(nueva), sesion["usuario"]))
        # Las demás sesiones abiertas (otro computador, por ejemplo) se cierran; esta sigue.
        cur.execute("""UPDATE sesiones_web SET vence = now()
                       WHERE usuario = %s AND token_hash <> %s AND vence > now()""",
                    (sesion["usuario"], sesion["token_hash"]))
    _olvidar(usuario=sesion["usuario"])
    return 200, {"ok": True}


# ---------------------------------------------------------------------
# Administración de usuarios (solo admin; revisar() ya lo comprobó)
# ---------------------------------------------------------------------
def _texto_nombre(d):
    nombre = d.get("nombre")
    if not isinstance(nombre, str) or not (2 <= len(nombre.strip()) <= 80):
        raise ErrorAPI(400, "Escriba el nombre de la persona (2 a 80 caracteres).", "nombre")
    return " ".join(nombre.split())


def _rol(d):
    rol = d.get("rol")
    if rol not in ROLES:
        raise ErrorAPI(400, "Elija el rol: admin, operador o consulta.", "rol")
    return rol


def lista_usuarios(p):
    with _bd() as cur:
        cur.execute("""SELECT usuario, nombre, rol, activo, creado_en, creado_por, ultimo_ingreso FROM usuarios
                       ORDER BY activo DESC, CASE rol WHEN 'admin' THEN 0 WHEN 'operador' THEN 1 ELSE 2 END, usuario""")
        filas = cur.fetchall()
    return 200, {"usuarios": [{**_usuario_json(u), "activo": u["activo"], "creado_por": u["creado_por"],
                               "creado_en": api_envios._iso(u["creado_en"]),
                               "ultimo_ingreso": api_envios._iso(u["ultimo_ingreso"])} for u in filas],
                 "roles": [{"codigo": k, "nombre": v} for k, v in ROLES.items()]}


def crear_usuario(p):
    sesion = _exigir_sesion(p)
    d = p.datos
    usuario = d.get("usuario").strip().lower() if isinstance(d.get("usuario"), str) else ""
    if not PATRON_USUARIO.match(usuario):
        raise ErrorAPI(400, "El usuario debe tener de 3 a 30 letras minúsculas, números, punto, guion o guion bajo "
                            "(sin espacios ni tildes).", "usuario")
    valores = (usuario, _texto_nombre(d), api_envios._hash_clave(_clave_nueva(d, "clave")), _rol(d), sesion["usuario"])
    with _bd() as cur:
        cur.execute("""INSERT INTO usuarios (usuario, nombre, clave_hash, rol, creado_por)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (usuario) DO NOTHING
                       RETURNING usuario, nombre, rol""", valores)
        u = cur.fetchone()
    if u is None:
        raise ErrorAPI(409, "Ya existe un usuario con ese nombre.", "usuario")
    return 201, {"usuario": _usuario_json(u)}


def modificar_usuario(p):
    sesion = _exigir_sesion(p)
    usuario, d = p.args[0], p.datos
    cambios, cerrar_sesiones = {}, False
    if "nombre" in d:
        cambios["nombre"] = _texto_nombre(d)
    if "rol" in d:
        cambios["rol"] = _rol(d)
    if "activo" in d:
        if not isinstance(d["activo"], bool):
            raise ErrorAPI(400, "activo debe ser true o false.", "activo")
        cambios["activo"] = d["activo"]
        cerrar_sesiones = not d["activo"]
    if "clave" in d:
        cambios["clave_hash"] = api_envios._hash_clave(_clave_nueva(d, "clave"))
        cerrar_sesiones = True
    if not cambios:
        raise ErrorAPI(400, "No hay nada que cambiar.")
    if usuario == sesion["usuario"] and (cambios.get("rol", "admin") != "admin" or cambios.get("activo") is False):
        raise ErrorAPI(409, "No puede quitarse a sí mismo el rol de administrador ni desactivarse.")
    columnas = ", ".join(f"{k} = %({k})s" for k in cambios)
    with _bd() as cur:
        cur.execute(f"UPDATE usuarios SET {columnas} WHERE usuario = %(u)s RETURNING usuario, nombre, rol, activo",
                    {**cambios, "u": usuario})
        u = cur.fetchone()
        if u is None:
            raise ErrorAPI(404, "Ese usuario no existe.")
        if cerrar_sesiones and usuario != sesion["usuario"]:
            cur.execute("UPDATE sesiones_web SET vence = now() WHERE usuario = %s AND vence > now()", (usuario,))
    _olvidar(usuario=usuario)
    return 200, {"usuario": {**_usuario_json(u), "activo": u["activo"]}}


# ---------------------------------------------------------------------
# Entrada: servidor_web.py
# ---------------------------------------------------------------------
_RUTAS = [
    ("GET",  r"/api/sesion", quien_soy),
    ("POST", r"/api/sesion/ingresar", ingresar),
    ("POST", r"/api/sesion/salir", salir),
    ("POST", r"/api/sesion/clave", cambiar_mi_clave),
    ("GET",  r"/api/usuarios", lista_usuarios),
    ("POST", r"/api/usuarios", crear_usuario),
    ("POST", r"/api/usuarios/([a-z0-9._-]{3,30})", modificar_usuario),
]


def es_ruta_propia(ruta):
    ruta = ruta.rstrip("/") or "/"
    return ruta == "/api/sesion" or ruta.startswith("/api/sesion/") or ruta == "/api/usuarios" \
        or ruta.startswith("/api/usuarios/")


class _Pedido:
    def __init__(self, datos, cookie, ip, args):
        self.datos, self.cookie, self.ip, self.args = datos, cookie, ip, args


def atender(metodo, ruta, cuerpo, cookie, ip):
    """Devuelve (código, diccionario, cabeceras extra). Nunca lanza excepciones."""
    ruta = ruta.rstrip("/") or "/"
    try:
        manejador, args, otro_metodo = None, (), False
        for m, patron, funcion in _RUTAS:
            coincide = re.fullmatch(patron, ruta)
            if coincide:
                if m == metodo:
                    manejador, args = funcion, coincide.groups()
                    break
                otro_metodo = True
        if manejador is None:
            raise ErrorAPI(405 if otro_metodo else 404,
                           f"Esa ruta no acepta {metodo}." if otro_metodo else "Ruta no encontrada.")
        datos = api_envios._leer_json(cuerpo) if metodo == "POST" else {}
        resultado = manejador(_Pedido(datos, cookie, ip, args))
        return resultado if len(resultado) == 3 else (*resultado, [])
    except ErrorAPI as e:
        respuesta = {"error": e.mensaje}
        if e.campo:
            respuesta["campo"] = e.campo
        return e.estado, respuesta, []
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 500, {"error": "Error interno del servidor."}, []
