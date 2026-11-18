"""
crear_usuario.py — P3-S4: crea un usuario de la página (o le cambia la contraseña)
desde la terminal de la EC2. Sirve para el PRIMER administrador; los demás se
crean desde la página (menú → Usuarios).

Uso (en la carpeta de inicio de la EC2, donde está basedatos.py):
    python3 crear_usuario.py                     pregunta todo
    python3 crear_usuario.py --restablecer jesus le pone una contraseña nueva (y lo reactiva)

La contraseña se escribe dos veces y no se ve en pantalla. Con --clave-archivo
se lee de un archivo (lo usan las pruebas automáticas; el archivo debe tener
chmod 600 y nunca va a GitHub).
"""

import argparse
import getpass
import sys

import acceso
import api_envios


def _preguntar(texto, valido, error):
    while True:
        valor = input(texto).strip()
        if valido(valor):
            return valor
        print("  " + error)


def _pedir_clave():
    while True:
        clave = getpass.getpass(f"Contraseña (mínimo {acceso.CLAVE_MIN} caracteres, no se ve al escribir): ")
        if len(clave) < acceso.CLAVE_MIN:
            print(f"  Debe tener al menos {acceso.CLAVE_MIN} caracteres.")
        elif getpass.getpass("Escríbala otra vez: ") != clave:
            print("  Las dos contraseñas no coinciden.")
        else:
            return clave


def _leer_clave(ruta):
    with open(ruta, encoding="utf-8") as f:
        clave = f.read().strip()
    if len(clave) < acceso.CLAVE_MIN:
        sys.exit(f"La contraseña de {ruta} debe tener al menos {acceso.CLAVE_MIN} caracteres.")
    return clave


def main():
    parser = argparse.ArgumentParser(description="Crea un usuario de la página de Orbita.")
    parser.add_argument("--usuario")
    parser.add_argument("--nombre")
    parser.add_argument("--rol", choices=list(acceso.ROLES))
    parser.add_argument("--restablecer", metavar="USUARIO", help="cambiar la contraseña de un usuario que ya existe")
    parser.add_argument("--clave-archivo", help="leer la contraseña de este archivo en vez de preguntarla")
    a = parser.parse_args()

    if a.restablecer:
        usuario = a.restablecer.strip().lower()
        clave = _leer_clave(a.clave_archivo) if a.clave_archivo else _pedir_clave()
        with api_envios._bd() as cur:
            cur.execute("UPDATE usuarios SET clave_hash = %s, activo = true WHERE usuario = %s RETURNING rol",
                        (api_envios._hash_clave(clave), usuario))
            fila = cur.fetchone()
            if fila:
                cur.execute("UPDATE sesiones_web SET vence = now() WHERE usuario = %s AND vence > now()", (usuario,))
        if not fila:
            sys.exit(f"No existe el usuario {usuario}.")
        print(f"Listo: {usuario} ({acceso.ROLES[fila['rol']]}) tiene contraseña nueva. Sus sesiones abiertas se cerraron.")
        return

    print("Nuevo usuario de la página de Orbita.")
    usuario = (a.usuario or _preguntar(
        "Usuario (minúsculas, sin espacios, ej. jcotes): ",
        lambda v: acceso.PATRON_USUARIO.match(v.lower()) is not None,
        "De 3 a 30 letras minúsculas, números, punto, guion o guion bajo (sin tildes ni ñ).")).lower()
    if not acceso.PATRON_USUARIO.match(usuario):
        sys.exit("Usuario no válido: de 3 a 30 letras minúsculas, números, punto, guion o guion bajo.")
    nombre = a.nombre or _preguntar("Nombre completo: ", lambda v: 2 <= len(v) <= 80, "Entre 2 y 80 caracteres.")
    rol = a.rol or _preguntar("Rol (admin, operador o consulta): ", lambda v: v in acceso.ROLES,
                              "Escriba admin, operador o consulta.")
    clave = _leer_clave(a.clave_archivo) if a.clave_archivo else _pedir_clave()

    with api_envios._bd() as cur:
        cur.execute("""INSERT INTO usuarios (usuario, nombre, clave_hash, rol) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (usuario) DO NOTHING RETURNING usuario""",
                    (usuario, " ".join(nombre.split()), api_envios._hash_clave(clave), rol))
        creado = cur.fetchone()
    if not creado:
        sys.exit(f"Ya existe el usuario {usuario}. Para cambiarle la contraseña: "
                 f"python3 crear_usuario.py --restablecer {usuario}")
    print(f"Listo: {usuario} ({acceso.ROLES[rol]}) ya puede entrar a la página.")


if __name__ == "__main__":
    main()
