"""
prueba_paso3.py — P3-S2, paso 3: prueba de punta a punta la API de camiones y envíos.

Uso, en la EC2 (no necesita estar en ninguna carpeta en especial):
    python3 prueba_paso3.py                                      contra http://localhost:8080 (sin Nginx)
    python3 prueba_paso3.py --url https://SU-DOMINIO.sytes.net   pasando por Nginx (prueba /api/)

Qué hace: registra 3 camiones de prueba (ZZZ901, ZZZ902 y ZZZ903), los pone
disponibles cerca de Uninorte, calcula una ruta Uninorte -> Centro, crea un
envío, revisa lo que ve la app del camión y al final cancela el envío y deja
los camiones "no disponibles". Se puede correr varias veces.

Los datos de prueba quedan en la base; para borrarlos, el usuario maestro
ejecuta limpiar_pruebas_p3s2.sql.
"""

import argparse
import json
import random
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

CLAVE = "pruebaOrbita1"
UNINORTE = {"lat": 11.0190, "lon": -74.8505}
CENTRO = {"lat": 10.9878, "lon": -74.7889}
UN_ANO = (date.today() + timedelta(days=365)).isoformat()
DUENO = {"propietario_nombre": "Dueño de prueba", "propietario_documento": "1001000009",
         "propietario_celular": "3001000009", "soat_vence": UN_ANO, "tecnomecanica_vence": UN_ANO, "clave": CLAVE}
CAMIONES = {
    # placa: (datos, dónde queda disponible)
    "ZZZ901": ({"configuracion": "C2", "carroceria": "furgon", "capacidad_kg": 4500, "marca": "Chevrolet",
                "linea": "NPR", "modelo": 2019, "color": "Blanco"}, {"lat": 11.0190, "lon": -74.8395}),   # a 1.2 km
    "ZZZ902": ({"configuracion": "3S3", "carroceria": "refrigerado", "capacidad_kg": 34000, "marca": "Kenworth",
                "linea": "T800", "modelo": 2020, "color": "Rojo"}, CENTRO),
    "ZZZ903": ({"configuracion": "C3", "carroceria": "estacas", "capacidad_kg": 17000, "marca": "International",
                "linea": "4400", "modelo": 2017, "color": "Azul"}, UNINORTE),                           # en Uninorte
}

URL = "http://localhost:8080"
resultados = {"ok": 0, "fallo": 0}


def pedir(metodo, ruta, datos=None, token=None):
    cuerpo = json.dumps(datos).encode("utf-8") if datos is not None else (b"" if metodo == "POST" else None)
    pedido = urllib.request.Request(URL + ruta, data=cuerpo, method=metodo)
    pedido.add_header("Content-Type", "application/json")
    if token:
        pedido.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(pedido, timeout=90) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        texto = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(texto)
        except ValueError:
            return e.code, {"error": texto[:200]}


def revisar(nombre, condicion, detalle=""):
    if condicion:
        resultados["ok"] += 1
        print(f"  OK     {nombre}")
    else:
        resultados["fallo"] += 1
        print(f"  FALLO  {nombre}" + (f"  ->  {detalle}" if detalle else ""))
    return condicion


def seccion(titulo):
    print(f"\n--- {titulo} ---")


def main():
    global URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL, help="dirección del servidor (por defecto http://localhost:8080)")
    URL = parser.parse_args().url.rstrip("/")
    print(f"Probando {URL}")

    seccion("La API responde")
    estado, r = pedir("GET", "/api/catalogo")
    if not revisar("GET /api/catalogo responde 200", estado == 200, f"{estado} {r}"):
        print("\nLa API no responde. Revise que servidor_web.py tenga los cambios, que api_envios.py y rutas.py"
              " estén junto a él, y (si usa --url) que Nginx tenga el bloque location /api/.")
        sys.exit(1)
    revisar("El catálogo trae las 7 configuraciones", len(r.get("configuraciones", [])) == 7, r)

    seccion("Registro e inicio de sesión de los camiones")
    tokens = {}
    for placa, (datos, _) in CAMIONES.items():
        estado, r = pedir("POST", "/api/camiones/registro", {"placa": placa, **datos, **DUENO})
        if estado == 409:
            estado, r = pedir("POST", "/api/camiones/login", {"placa": placa, "clave": CLAVE})
            revisar(f"{placa} ya existía: inicia sesión", estado == 200 and "token" in r, f"{estado} {r}")
        else:
            revisar(f"Registra {placa}", estado == 201 and "token" in r, f"{estado} {r}")
        tokens[placa] = r.get("token")

    base = {**CAMIONES["ZZZ901"][0], **DUENO}
    estado, r = pedir("POST", "/api/camiones/registro", {**base, "placa": "ZZ-9"})
    revisar("Rechaza una placa mal escrita (400, campo placa)", estado == 400 and r.get("campo") == "placa", f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/registro", {**base, "placa": "ZZZ904", "capacidad_kg": 20000})
    revisar("Rechaza un C2 con 20 000 kg (400, campo capacidad_kg)",
            estado == 400 and r.get("campo") == "capacidad_kg", f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/registro", {**base, "placa": "ZZZ904", "clave": "corta"})
    revisar("Rechaza una contraseña de menos de 8 caracteres", estado == 400 and r.get("campo") == "clave", f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/registro", {**base, "placa": "ZZZ901"})
    revisar("Rechaza una placa repetida (409)", estado == 409, f"{estado} {r}")

    estado, r = pedir("POST", "/api/camiones/login", {"placa": "ZZZ901", "clave": "equivocada"})
    revisar("Contraseña equivocada (401)", estado == 401, f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/login", {"placa": "zzz 901", "clave": CLAVE})
    revisar("Inicia sesión aunque escriba la placa en minúscula y con espacio", estado == 200 and r.get("token"), f"{estado} {r}")
    tokens["ZZZ901"] = r.get("token") or tokens["ZZZ901"]

    estado, r = pedir("GET", "/api/camiones/yo")
    revisar("Sin token no deja ver los datos (401)", estado == 401, f"{estado} {r}")
    estado, r = pedir("GET", "/api/camiones/yo", token=tokens["ZZZ901"])
    revisar("Con token devuelve el camión y su dueño", estado == 200 and r.get("camion", {}).get("placa") == "ZZZ901"
            and "propietario" in r.get("camion", {}), f"{estado} {r}")

    seccion("Disponibilidad y ubicación")
    estado, r = pedir("POST", "/api/camiones/disponibilidad", {"disponible": True}, tokens["ZZZ901"])
    revisar("Para quedar disponible pide la ubicación (400)", estado == 400, f"{estado} {r}")
    for placa, (_, punto) in CAMIONES.items():
        estado, r = pedir("POST", "/api/camiones/disponibilidad", {"disponible": True, **punto}, tokens[placa])
        revisar(f"{placa} queda disponible", estado == 200 and r.get("camion", {}).get("estado") == "disponible",
                f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/ubicacion", CAMIONES["ZZZ901"][1], tokens["ZZZ901"])
    revisar("Actualiza la ubicación", estado == 200 and r.get("ok"), f"{estado} {r}")
    estado, r = pedir("GET", "/api/camiones")
    placas = [c["placa"] for c in r.get("camiones", [])]
    revisar("La flota muestra los 3 camiones de prueba", all(p in placas for p in CAMIONES), placas)
    revisar("La flota NO muestra datos del dueño", all("propietario" not in c for c in r.get("camiones", [])))

    seccion("Cotizar: ruta, puntos de control y camión sugerido")
    estado, r = pedir("POST", "/api/envios/cotizar",
                      {"origen": UNINORTE, "destino": CENTRO, "peso_kg": 3000, "tipo_carga": "general"})
    if revisar("Calcula la ruta Uninorte -> Centro", estado == 200 and len(r.get("ruta", {}).get("puntos", [])) > 1,
               f"{estado} {r.get('error', r)}"):
        ruta = r["ruta"]
        print(f"         ({ruta['fuente']}: {ruta['distancia_m'] / 1000:.1f} km, {ruta['duracion_s'] / 60:.0f} min"
              + (f"; aviso: {ruta['aviso']}" if ruta.get("aviso") else "") + ")")
        pc = r.get("puntos_control", [])
        revisar("Trae salida, al menos 3 puntos de control y llegada",
                len(pc) >= 5 and pc[0]["tipo"] == "salida" and pc[-1]["tipo"] == "llegada", pc)
        placas = [c["placa"] for c in r.get("candidatos", [])]
        print(f"         (candidatos: {', '.join(placas) or 'ninguno'})")
        revisar("El refrigerado ZZZ902 no aparece para carga general", "ZZZ902" not in placas, placas)
        revisar("ZZZ901 y ZZZ903 aparecen como candidatos", "ZZZ901" in placas and "ZZZ903" in placas, placas)
        if "ZZZ901" in placas and "ZZZ903" in placas:
            revisar("Gana ZZZ901 (4,5 t) sobre ZZZ903 (17 t): están a menos de 2 km y sirve el más pequeño",
                    placas.index("ZZZ901") < placas.index("ZZZ903"), placas)
    estado, r = pedir("POST", "/api/envios/cotizar",
                      {"origen": UNINORTE, "destino": CENTRO, "peso_kg": 20000, "tipo_carga": "refrigerada"})
    placas = [c["placa"] for c in r.get("candidatos", [])]
    revisar("Para 20 t refrigeradas solo sirve ZZZ902", estado == 200 and "ZZZ902" in placas
            and "ZZZ901" not in placas and "ZZZ903" not in placas, f"{estado} {placas or r}")
    estado, r = pedir("POST", "/api/envios/cotizar",
                      {"origen": UNINORTE, "destino": CENTRO, "peso_kg": 3000, "tipo_carga": "explosivos"})
    revisar("Rechaza un tipo de carga que no existe (400)", estado == 400 and r.get("campo") == "tipo_carga", f"{estado} {r}")

    seccion("Crear el envío y asignar el camión")
    manana = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT08:00")
    envio = {
        "cliente_nombre": "Cliente de prueba S.A.S.", "cliente_nit": "900123456-7",
        "origen_direccion": "Universidad del Norte, Km 5 vía Puerto Colombia", "origen": UNINORTE,
        "cargue_contacto": "Contacto de prueba", "cargue_celular": "3001000010",
        "destino_direccion": "Centro de Barranquilla", "destino": CENTRO,
        "destinatario_nombre": "Destinatario de prueba", "destinatario_celular": "3001000011",
        "tipo_carga": "general", "descripcion": "Cajas de prueba", "empaque": "cajas", "cantidad": 20,
        "peso_kg": 3000, "cargue_programado": manana, "placa": "ZZZ901",
    }
    estado, r = pedir("POST", "/api/envios", envio)
    id_envio = r.get("envio", {}).get("id")
    if not revisar("Crea el envío asignado a ZZZ901 (201)", estado == 201 and id_envio, f"{estado} {r}"):
        return
    e = r["envio"]
    print(f"         ({e['codigo']}: {e['plan']['distancia_m'] / 1000:.1f} km, {len(e['puntos_control'])} puntos)")
    revisar("El envío queda 'asignado'", e["estado"] == "asignado", e["estado"])
    revisar("La salida lleva la dirección de origen", e["puntos_control"][0]["nombre"].startswith("Universidad del Norte"),
            e["puntos_control"][0])
    estado, r = pedir("POST", "/api/envios", envio)
    revisar("No deja asignarle otro envío al mismo camión (409)", estado == 409, f"{estado} {r}")

    seccion("Lo que ve la app del camión")
    estado, r = pedir("GET", "/api/camiones/viaje", token=tokens["ZZZ901"])
    viaje = r.get("viaje") or {}
    revisar("ZZZ901 recibe su viaje", estado == 200 and viaje.get("id") == id_envio, f"{estado} {r}")
    revisar("Sin ?ruta=1 no trae la línea (ahorra datos)", "ruta" not in viaje)
    estado, r = pedir("GET", "/api/camiones/viaje?ruta=1", token=tokens["ZZZ901"])
    revisar("Con ?ruta=1 trae la línea de la ruta", len((r.get("viaje") or {}).get("ruta", {}).get("puntos", [])) > 1)
    estado, r = pedir("GET", "/api/camiones/viaje", token=tokens["ZZZ903"])
    revisar("ZZZ903 no tiene viaje", estado == 200 and r.get("viaje") is None, f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/disponibilidad", {"disponible": False}, tokens["ZZZ901"])
    revisar("Con viaje asignado no puede cambiar su disponibilidad (409)", estado == 409, f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/viaje/finalizar", {}, tokens["ZZZ901"])
    revisar("No puede finalizar un viaje que no ha empezado (409)", estado == 409, f"{estado} {r}")

    seccion("Lo que ve la página")
    estado, r = pedir("GET", "/api/envios")
    revisar("La lista de envíos activos incluye el nuevo", estado == 200
            and any(x["id"] == id_envio for x in r.get("envios", [])), f"{estado}")
    estado, r = pedir("GET", f"/api/envios/{id_envio}")
    d = r.get("envio", {})
    revisar("El detalle trae camión, puntos de control y la ruta",
            estado == 200 and d.get("camion", {}).get("placa") == "ZZZ901" and d.get("puntos_control")
            and len(d.get("ruta", {}).get("puntos", [])) > 1, f"{estado}")
    estado, r = pedir("GET", f"/api/envios/{id_envio}?ruta=0")
    revisar("Con ?ruta=0 el detalle no trae la línea", estado == 200 and "ruta" not in r.get("envio", {}))
    estado, r = pedir("GET", "/api/envios/999999999")
    revisar("Un envío que no existe da 404", estado == 404, f"{estado} {r}")
    estado, r = pedir("POST", "/api/alertas/999999999/vista", {})
    revisar("Una alerta que no existe da 404", estado == 404, f"{estado} {r}")
    estado, r = pedir("GET", "/datos")
    revisar("/datos sigue funcionando y trae la clave \"envio\"", estado == 200 and "envio" in r, f"{estado} {r}")
    estado, r = pedir("GET", "/recorridos")
    revisar("Históricos sigue funcionando y cada recorrido trae \"envio\"",
            estado == 200 and all("envio" in x for x in r), f"{estado}")

    seccion("Cancelar")
    estado, r = pedir("POST", f"/api/envios/{id_envio}/cancelar", {})
    revisar("Cancela el envío", estado == 200 and r.get("envio", {}).get("estado") == "cancelado", f"{estado} {r}")
    estado, r = pedir("GET", "/api/camiones/yo", token=tokens["ZZZ901"])
    revisar("ZZZ901 vuelve a quedar disponible", r.get("camion", {}).get("estado") == "disponible", r)
    estado, r = pedir("POST", f"/api/envios/{id_envio}/cancelar", {})
    revisar("No deja cancelar dos veces (409)", estado == 409, f"{estado} {r}")

    seccion("Seguridad")
    inventada = f"ZZY{random.randint(100, 999)}"          # una placa que no existe, distinta en cada corrida
    codigos = [pedir("POST", "/api/camiones/login", {"placa": inventada, "clave": "nada12345"})[0] for _ in range(6)]
    revisar("Tras 5 intentos fallidos bloquea la placa por 10 min (429)", codigos[:5] == [401] * 5 and codigos[5] == 429,
            codigos)
    estado, r = pedir("GET", "/api/nada")
    revisar("Una ruta que no existe da 404", estado == 404, f"{estado}")

    seccion("Dejar todo como estaba")
    for placa in CAMIONES:
        estado, r = pedir("POST", "/api/camiones/disponibilidad", {"disponible": False}, tokens[placa])
        revisar(f"{placa} queda no disponible", estado == 200, f"{estado} {r}")
    estado, r = pedir("POST", "/api/camiones/logout", {}, tokens["ZZZ903"])
    estado, r = pedir("GET", "/api/camiones/yo", token=tokens["ZZZ903"])
    revisar("Después de cerrar sesión el token ya no sirve (401)", estado == 401, f"{estado} {r}")


if __name__ == "__main__":
    try:
        main()
    finally:
        print("\n" + "-" * 50)
        if resultados["fallo"] == 0 and resultados["ok"]:
            print(f"TODO BIEN: {resultados['ok']} pruebas pasaron.")
        else:
            print(f"{resultados['ok']} pasaron y {resultados['fallo']} fallaron.")
