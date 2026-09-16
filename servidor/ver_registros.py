"""
ver_registros.py — Muestra TODOS los registros guardados en la base
de datos compartida (PostgreSQL / RDS), en una tabla legible en la
terminal. Útil para revisar el historial completo, o para diagnosticar
si los datos están llegando de verdad (sin depender de las páginas web).
"""

from basedatos import obtener_conexion


def mostrar_todos_los_registros():
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    cursor.execute("""
        SELECT id, id_dispositivo, latitud, longitud, hora_gps, hora_recepcion, id_recorrido
        FROM ubicaciones
        ORDER BY id DESC
        LIMIT 15
    """)
    filas = cursor.fetchall()
    cursor.close()
    conexion.close()

    if not filas:
        print("Todavía no hay ningún registro guardado.")
        return

    print(f"\nÚltimos {len(filas)} registros:\n")
    print(f"{'ID':<5}{'Dispositivo':<17}{'Latitud':<12}{'Longitud':<12}{'Hora GPS':<11}{'Hora Recepción':<28}{'Recorrido'}")
    print("-" * 115)
    for id_, dispositivo, lat, lon, hora_gps, hora_recepcion, id_recorrido in filas:
        print(f"{id_:<5}{dispositivo:<17}{lat:<12}{lon:<12}{(hora_gps or '—'):<11}{hora_recepcion:<28}{id_recorrido or '— (NULL)'}")


if __name__ == "__main__":
    mostrar_todos_los_registros()
