"""
entrenar.py — P3-S4: entrena y evalúa el reconocimiento del conductor con los datos de
recolectar.py (cámara OV3660 del ESP32-CAM) y genera las métricas y gráficas del informe.

    py entrenar.py                (usa Rastreo_GPS/datos_entrenamiento/)
    py entrenar.py --sin-aumento  (más rápido: no genera las imágenes aumentadas)

Qué se entrena (aprendizaje por transferencia): SFace (red ya entrenada con millones de
rostros) convierte cada rostro en una huella de 128 números. Encima de eso se entrenan y
comparan:
  · la GALERÍA de cada conductor (cuántas muestras, cuáles, con o sin aumento de datos)
  · el MODELO de puntaje: top-3 (el del servidor), 1-vecino (máx), centroide; y si hay
    2 o más conductores, un SVM lineal para decir quién es
  · el UMBRAL, elegido con el conjunto de validación (no a ojo)
  · la REGLA de decisión con 3 recortes: estricta (la del servidor) o por mayoría

Particiones:
  · "sesiones" (cuando cada conductor tiene 3 sesiones): S1 entrenamiento, S2 validación,
    S3 prueba. Así no se prueba con fotos casi idénticas a las de entrenamiento.
  · "provisional" (mientras tanto): dentro de cada toma, recortes 1-10 entrenamiento,
    11-15 validación, 16-20 prueba. OJO: da métricas optimistas (fuga entre recortes
    seguidos); por eso también se reporta la validación cruzada por condición.
  · Validación cruzada "deja una condición afuera": la galería se arma SIN la condición
    que se evalúa (¿funciona con una luz o postura que no vio en el entrenamiento?).

Resultados (sin imágenes de rostros; las cifras usan alias P1, X1...):
    datos_entrenamiento/resultados/metricas.json   todo, para el documento
    datos_entrenamiento/resultados/resumen.md      tablas legibles
    datos_entrenamiento/resultados/*.png           gráficas
    datos_entrenamiento/galeria_recomendada.json   galería final (DATO BIOMÉTRICO: no se comparte)
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import defaultdict

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AQUI))
import comun                                          # noqa: E402  (configura OpenCV)
import cv2                                            # noqa: E402
import numpy as np                                    # noqa: E402
import matplotlib                                     # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
from sklearn.metrics import (auc, confusion_matrix,   # noqa: E402
                             precision_recall_fscore_support, roc_curve)

DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(AQUI))), "datos_entrenamiento")
RES = os.path.join(DATOS, "resultados")
UMBRAL_ACTUAL = comun.UMBRAL
MARGEN = comun.MARGEN
DISTANCIA_A_VECINOS = comun.DISTANCIA_A_VECINOS
MAX_GALERIA = 50                  # el servidor guarda hasta 50 muestras por conductor
SEMILLA = 7
MODELOS = ["top3", "max", "centroide"]
AZUL, NARANJA, GRIS, VERDE, ROJO = "#1565D8", "#E8710A", "#8A94A3", "#0F9D58", "#C62828"


# ---------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------
def leer_csv(nombre):
    ruta = os.path.join(DATOS, nombre)
    if not os.path.exists(ruta):
        return []
    with open(ruta, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cargar():
    muestras = leer_csv("muestras.csv")
    por_id = {m["id"]: m for m in muestras}
    huellas = []
    with open(os.path.join(DATOS, "huellas.jsonl"), encoding="utf-8") as f:
        for linea in f:
            h = json.loads(linea)
            v = np.array(h["huella"], dtype=np.float32)
            m = por_id[h["id"]]
            huellas.append({"id": h["id"], "persona": h["persona"], "tipo": h["tipo"], "sesion": int(h["sesion"]),
                            "condicion": h["condicion"], "n": int(m["n"]), "archivo": m["archivo"],
                            "toma": h["id"].rsplit("_", 1)[0], "v": v / (np.linalg.norm(v) + 1e-9)})
    return muestras, huellas, leer_csv("deteccion.csv")


def particionar(huellas):
    gen = [h for h in huellas if h["tipo"] == "genuino"]
    imp = [h for h in huellas if h["tipo"] == "impostor"]
    personas = sorted({h["persona"] for h in gen})
    sesiones = {p: sorted({h["sesion"] for h in gen if h["persona"] == p}) for p in personas}
    if personas and all(len(s) >= 3 for s in sesiones.values()):
        protocolo = "sesiones"
        for h in gen:
            s = sesiones[h["persona"]]
            h["parte"] = {s[0]: "entrenamiento", s[1]: "validacion"}.get(h["sesion"], "prueba")
    else:
        protocolo = "provisional"
        for h in gen:
            h["parte"] = "entrenamiento" if h["n"] <= 10 else ("validacion" if h["n"] <= 15 else "prueba")
    impostores = sorted({h["persona"] for h in imp})
    if len(impostores) >= 2:              # personas distintas en validación y en prueba
        mitad = {p: ("validacion" if i % 2 == 0 else "prueba") for i, p in enumerate(impostores)}
        for h in imp:
            h["parte"] = mitad[h["persona"]]
    else:                                 # un solo impostor: recortes alternados
        for h in imp:
            h["parte"] = "validacion" if h["n"] % 2 else "prueba"
    return protocolo, personas, impostores, sesiones


# ---------------------------------------------------------------------
# Modelos de puntaje
# ---------------------------------------------------------------------
class Galeria:
    def __init__(self, por_persona):
        self.m = {p: np.array(v, dtype=np.float32) for p, v in por_persona.items() if len(v)}
        self.c = {p: (lambda x: x / (np.linalg.norm(x) + 1e-9))(m.mean(axis=0)) for p, m in self.m.items()}
        self._vecinos = {}

    def puntaje(self, p, v, modelo="top3"):
        if modelo == "centroide":
            return float(self.c[p] @ v)
        s = self.m[p] @ v
        return float(s.max()) if modelo == "max" else float(np.mean(np.sort(s)[::-1][:3]))

    def puntajes(self, v, modelo="top3"):
        return sorted(((self.puntaje(p, v, modelo), p) for p in self.m), reverse=True)

    def umbral_de(self, p, umbral):
        if p not in self._vecinos:
            otros = [q for q in self.m if q != p]
            self._vecinos[p] = max((self.puntaje(q, m) for q in otros for m in self.m[p]), default=0.0)
        return max(umbral, self._vecinos[p] + DISTANCIA_A_VECINOS)

    def evaluar(self, v, umbral):
        """La misma regla del servidor: umbral (o más si se parece a otro conductor) y margen."""
        lista = self.puntajes(v)
        sim, p = lista[0]
        segunda = lista[1][0] if len(lista) > 1 else 0.0
        if sim < self.umbral_de(p, umbral):
            return None, sim, "bajo_umbral"
        if sim - segunda < MARGEN:
            return None, sim, "poca_ventaja"
        return p, sim, "aceptado"


def galeria_de(huellas, partes=("entrenamiento",), extra=None):
    por = defaultdict(list)
    for h in huellas:
        if h["tipo"] == "genuino" and h["parte"] in partes:
            por[h["persona"]].append(h["v"])
    for p, vs in (extra or {}).items():
        por[p].extend(vs)
    return Galeria(por)


def puntajes_verificacion(gal, huellas, parte, modelo="top3"):
    """Genuinos: contra su propia galería. Impostores: el MEJOR puntaje contra cualquier conductor
    (también cuenta como impostor un conductor comparado con la galería de OTRO conductor)."""
    g, i, cond = [], [], []
    for h in huellas:
        if h["parte"] != parte:
            continue
        if h["tipo"] == "genuino" and h["persona"] in gal.m:
            g.append(gal.puntaje(h["persona"], h["v"], modelo))
            cond.append(h["condicion"])
            otros = [gal.puntaje(q, h["v"], modelo) for q in gal.m if q != h["persona"]]
            if otros:
                i.append(max(otros))
        elif h["tipo"] == "impostor" and gal.m:
            i.append(gal.puntajes(h["v"], modelo)[0][0])
    return np.array(g), np.array(i), cond


# ---------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------
def metricas(g, i, umbral):
    r = {"n_genuinos": int(len(g)), "n_impostores": int(len(i)), "umbral": round(float(umbral), 3)}
    if len(g) == 0:
        return r
    tp, fn = int((g >= umbral).sum()), int((g < umbral).sum())
    fp, tn = int((i >= umbral).sum()), int((i < umbral).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    r.update({"VP": tp, "FN": fn, "FP": fp, "VN": tn,
              "FAR": round(fp / len(i), 4) if len(i) else None, "FRR": round(fn / len(g), 4),
              "exactitud": round((tp + tn) / (len(g) + len(i)), 4),
              "precision": round(precision, 4), "recall_TAR": round(recall, 4),
              "F1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
              "media_genuinos": round(float(g.mean()), 3), "min_genuinos": round(float(g.min()), 3)})
    if len(i):
        y = np.r_[np.ones(len(g)), np.zeros(len(i))]
        s = np.r_[g, i]
        fpr, tpr, th = roc_curve(y, s)
        k = int(np.argmin(np.abs(fpr - (1 - tpr))))
        r.update({"AUC": round(float(auc(fpr, tpr)), 4),
                  "EER": round(float((fpr[k] + 1 - tpr[k]) / 2), 4), "umbral_EER": round(float(min(th[k], 1.0)), 3),
                  "TAR_a_FAR_1pct": round(float(tpr[fpr <= 0.01].max()), 4) if (fpr <= 0.01).any() else 0.0,
                  "media_impostores": round(float(i.mean()), 3), "max_impostores": round(float(i.max()), 3),
                  "d_prime": round(float(abs(g.mean() - i.mean()) / np.sqrt((g.var() + i.var()) / 2 + 1e-12)), 2)})
    return r


def elegir_umbrales(g_val, i_val):
    """Umbrales candidatos elegidos SOLO con validación."""
    m = metricas(g_val, i_val, UMBRAL_ACTUAL)
    candidatos = {"actual_0.55": UMBRAL_ACTUAL}
    if len(i_val):
        candidatos["EER_validacion"] = m["umbral_EER"]
        candidatos["FAR0_mas_0.05"] = round(float(i_val.max()) + 0.05, 3)
    return candidatos


# ---------------------------------------------------------------------
# Regla con 3 recortes (como en un viaje real)
# ---------------------------------------------------------------------
def intentos(muestras, huellas, filtro):
    """Agrupa los recortes de cada toma de 3 en 3, en orden (los que SFace no vio quedan en None)."""
    por_id = {h["id"]: h for h in huellas}
    tomas = defaultdict(list)
    for m in muestras:
        if filtro(m):
            tomas[m["id"].rsplit("_", 1)[0]].append(m)
    grupos = []
    for lista in tomas.values():
        lista.sort(key=lambda m: int(m["n"]))
        vs = [por_id[m["id"]]["v"] if m["id"] in por_id else None for m in lista]
        grupos += [(lista[0], vs[k:k + 3]) for k in range(0, len(vs) - 2, 3)]
    return grupos


def decidir(gal, trio, umbral, regla):
    evals = [gal.evaluar(v, umbral) if v is not None else (None, 0.0, "sin_rostro") for v in trio]
    con = [e for e in evals if e[2] != "sin_rostro"]
    if len(con) < 2:
        return None
    if regla == "estricta":                       # la del servidor: TODOS los recortes con rostro iguales
        ps = {e[0] for e in con}
        return ps.pop() if len(ps) == 1 and None not in ps else None
    votos = defaultdict(int)                      # mayoría: 2 de 3 la misma persona y nadie más aceptado
    for e in con:
        if e[0]:
            votos[e[0]] += 1
    if len(votos) == 1:
        p, n = next(iter(votos.items()))
        return p if n >= 2 else None
    return None


# ---------------------------------------------------------------------
# Aumento de datos
# ---------------------------------------------------------------------
def variantes(img):
    """Imágenes 'difíciles' generadas a partir de una buena: oscura, movida, desenfocada, con ruido, girada."""
    salida = {}
    oscura = np.clip(255 * ((img / 255.0) ** 1.8) * 0.55, 0, 255).astype(np.uint8)
    salida["oscura"] = oscura
    k = np.zeros((9, 9), np.float32)
    k[4, :] = 1 / 9
    salida["movida_h"] = cv2.filter2D(img, -1, k)
    salida["movida_v"] = cv2.filter2D(img, -1, k.T)
    salida["desenfocada"] = cv2.GaussianBlur(img, (0, 0), 1.6)
    ruido = np.random.default_rng(SEMILLA).normal(0, 12, img.shape)
    salida["ruido"] = np.clip(img + ruido, 0, 255).astype(np.uint8)
    h, w = img.shape[:2]
    for ang in (-12, 12):
        rot = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
        salida[f"girada{ang:+d}"] = cv2.warpAffine(img, rot, (w, h), borderMode=cv2.BORDER_REPLICATE)
    return salida


def aumentar(motor, huellas, partes=("entrenamiento",)):
    extra, conteo = defaultdict(list), defaultdict(int)
    for h in huellas:
        if h["tipo"] != "genuino" or h["parte"] not in partes or not h["archivo"]:
            continue
        img = cv2.imread(os.path.join(DATOS, h["archivo"]))
        if img is None:
            continue
        for nombre, var in variantes(img).items():
            rostros = motor.detectar(var)
            conteo[nombre + ("_ok" if len(rostros) else "_sin_rostro")] += 1
            if len(rostros):
                extra[h["persona"]].append(motor.huella(var, rostros[0]))
    return extra, dict(conteo)


def seleccion_diversa(vectores, cuantos):
    """Elige 'cuantos' muestras lo más distintas entre sí (punto más lejano): cubre más condiciones."""
    m = np.array(vectores)
    if len(m) <= cuantos:
        return m
    elegidos = [int(np.argmax(m @ m.mean(axis=0)))]
    dist = 1 - m @ m[elegidos[0]]
    while len(elegidos) < cuantos:
        k = int(np.argmax(dist))
        elegidos.append(k)
        dist = np.minimum(dist, 1 - m @ m[k])
    return m[elegidos]


# ---------------------------------------------------------------------
# Gráficas
# ---------------------------------------------------------------------
def guardar(fig, nombre):
    fig.tight_layout()
    fig.savefig(os.path.join(RES, nombre), dpi=150)
    plt.close(fig)


def grafica_histograma(g, i, umbrales, titulo):
    fig, ax = plt.subplots(figsize=(7, 4))
    bins = np.linspace(-0.1, 1.0, 45)
    ax.hist(g, bins=bins, alpha=0.75, color=AZUL, label=f"Genuinos (n={len(g)})")
    if len(i):
        ax.hist(i, bins=bins, alpha=0.75, color=NARANJA, label=f"Impostores (n={len(i)})")
    estilos = ["--", ":", "-."]
    for k, (nombre, u) in enumerate(umbrales.items()):
        ax.axvline(u, ls=estilos[k % 3], lw=1.4, color=ROJO if nombre.startswith("actual") else GRIS,
                   label=f"Umbral {nombre.replace('_', ' ')}: {u:.2f}")
    ax.set_xlabel("Similitud (coseno, modelo top-3)")
    ax.set_ylabel("Recortes")
    ax.set_title(titulo)
    ax.legend()
    guardar(fig, "histograma_similitud.png")


def grafica_roc(curvas):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4))
    for nombre, (g, i) in curvas.items():
        if not len(i):
            continue
        fpr, tpr, _ = roc_curve(np.r_[np.ones(len(g)), np.zeros(len(i))], np.r_[g, i])
        a1.plot(fpr, tpr, label=f"{nombre} (AUC {auc(fpr, tpr):.3f})")
        fnr = 1 - tpr
        a2.plot(np.clip(fpr, 1e-3, 1), np.clip(fnr, 1e-3, 1), label=nombre)
    a1.plot([0, 1], [0, 1], ":", color=GRIS)
    a1.set_xlabel("FAR (impostores aceptados)")
    a1.set_ylabel("TAR (genuinos aceptados)")
    a1.set_title("Curva ROC (prueba)")
    a1.legend(fontsize=8)
    a2.set_xscale("log")
    a2.set_yscale("log")
    a2.set_xlabel("FAR")
    a2.set_ylabel("FRR")
    a2.set_title("Curva DET (prueba)")
    a2.legend(fontsize=8)
    guardar(fig, "roc_det.png")


def grafica_far_frr(g, i):
    if not len(i):
        return
    u = np.linspace(0, 1, 201)
    far = [(i >= x).mean() for x in u]
    frr = [(g < x).mean() for x in u]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(u, far, color=NARANJA, label="FAR")
    ax.plot(u, frr, color=AZUL, label="FRR")
    ax.axvline(UMBRAL_ACTUAL, ls="--", color=ROJO, lw=1)
    ax.set_xlabel("Umbral")
    ax.set_ylabel("Tasa")
    ax.set_title("FAR y FRR según el umbral (prueba)")
    ax.legend()
    guardar(fig, "far_frr_umbral.png")


def grafica_por_condicion(tabla, nombre, titulo):
    conds = [c for c in tabla if tabla[c].get("n_genuinos")]
    if not conds:
        return
    x = np.arange(len(conds))
    fig, ax = plt.subplots(figsize=(max(7, len(conds) * 0.9), 4))
    ax.bar(x - 0.2, [tabla[c]["media_genuinos"] for c in conds], 0.4, color=AZUL, label="Similitud media")
    ax.bar(x + 0.2, [tabla[c]["FRR"] for c in conds], 0.4, color=ROJO, label="FRR con 0,55")
    ax.axhline(UMBRAL_ACTUAL, ls="--", color=GRIS, lw=1)
    ax.set_xticks(x, conds, rotation=30, ha="right")
    ax.set_ylim(0, 1)
    ax.set_title(titulo)
    ax.legend()
    guardar(fig, nombre)


def grafica_deteccion(det):
    if not det:
        return
    conds = list(det)
    fig, ax = plt.subplots(figsize=(max(7, len(conds) * 0.9), 4))
    ax.bar(conds, [det[c]["tasa_placa"] for c in conds], color=AZUL, label="ESP32-CAM (cuadros con rostro)")
    ax.plot(conds, [det[c]["tasa_sface"] if det[c]["tasa_sface"] is not None else np.nan for c in conds],
            "o", color=NARANJA, label="SFace ve el rostro en el recorte")
    ax.set_ylim(0, 1.05)
    ax.set_title("Tasa de detección por condición")
    ax.tick_params(axis="x", rotation=30)
    ax.legend(fontsize=8)
    guardar(fig, "deteccion_por_condicion.png")


def grafica_aprendizaje(curva):
    k = [c["k"] for c in curva]
    fig, ax = plt.subplots(figsize=(7, 4))
    for clave, color, nombre in (("frr_val", AZUL, "FRR validación"), ("frr_train", VERDE, "FRR entrenamiento (fuera de la galería)"),
                                 ("eer_val", NARANJA, "EER validación")):
        m = np.array([c[clave][0] for c in curva], dtype=float)
        s = np.array([c[clave][1] for c in curva], dtype=float)
        if np.all(np.isnan(m)):
            continue
        ax.plot(k, m, "o-", color=color, label=nombre)
        ax.fill_between(k, m - s, m + s, color=color, alpha=0.15)
    ax.set_xlabel("Muestras en la galería por conductor")
    ax.set_ylabel("Tasa de error")
    ax.set_title("Curva de aprendizaje (media ± desviación, 20 repeticiones)")
    ax.legend(fontsize=8)
    guardar(fig, "curva_aprendizaje.png")


def grafica_aumento(loco, loco_aum):
    conds = [c for c in loco if loco[c].get("n_genuinos")]
    if not conds:
        return
    x = np.arange(len(conds))
    fig, ax = plt.subplots(figsize=(max(7, len(conds) * 0.9), 4))
    ax.bar(x - 0.2, [loco[c]["media_genuinos"] for c in conds], 0.4, color=GRIS, label="Sin aumento")
    ax.bar(x + 0.2, [loco_aum[c]["media_genuinos"] for c in conds], 0.4, color=VERDE, label="Con aumento de datos")
    ax.axhline(UMBRAL_ACTUAL, ls="--", color=ROJO, lw=1)
    ax.set_xticks(x, conds, rotation=30, ha="right")
    ax.set_ylim(0, 1)
    ax.set_ylabel("Similitud media")
    ax.set_title("Condición NO vista en el entrenamiento: sin vs. con aumento de datos")
    ax.legend(loc="lower left")
    guardar(fig, "aumento_de_datos.png")


def grafica_confusion(mc, etiquetas):
    fig, ax = plt.subplots(figsize=(1.2 * len(etiquetas) + 2, 1.2 * len(etiquetas) + 1.5))
    ax.imshow(mc, cmap="Blues")
    for (r, c), v in np.ndenumerate(mc):
        ax.text(c, r, str(v), ha="center", va="center")
    ax.set_xticks(range(len(etiquetas)), etiquetas)
    ax.set_yticks(range(len(etiquetas)), etiquetas)
    ax.set_xlabel("Predicho")
    ax.set_ylabel("Real")
    ax.set_title("Matriz de confusión (prueba)")
    guardar(fig, "matriz_confusion.png")


# ---------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-aumento", action="store_true")
    a = ap.parse_args()
    random.seed(SEMILLA)
    np.random.seed(SEMILLA)
    os.makedirs(RES, exist_ok=True)
    muestras, huellas, deteccion = cargar()
    protocolo, personas, impostores, sesiones = particionar(huellas)
    R = {"fecha": time.strftime("%Y-%m-%d %H:%M"), "protocolo": protocolo, "conductores": personas,
         "impostores": impostores, "sesiones": sesiones, "umbral_actual": UMBRAL_ACTUAL}
    print(f"Protocolo: {protocolo} | conductores {personas} | impostores {impostores}")

    # ---- 1. Conjunto de datos ----
    conteo = defaultdict(lambda: defaultdict(int))
    for h in huellas:
        conteo[h["persona"]][h["parte"]] += 1
    R["datos"] = {"recortes_totales": len(muestras), "con_huella": len(huellas),
                  "por_persona_y_parte": {p: dict(v) for p, v in conteo.items()},
                  "condiciones": sorted({m["condicion"] for m in muestras})}

    # ---- 2. Detección y calidad por condición ----
    det = {}
    for c in sorted({d["condicion"] for d in deteccion}):
        filas = [d for d in deteccion if d["condicion"] == c]
        ms = [m for m in muestras if m["condicion"] == c]
        cuadros = sum(int(d["cuadros"]) for d in filas)
        det[c] = {"tomas": len(filas), "cuadros": cuadros,
                  "tasa_placa": round(sum(int(d["con_rostro"]) for d in filas) / cuadros, 3) if cuadros else 0.0,
                  "ms_por_cuadro": round(float(np.mean([int(d["ms_promedio"]) for d in filas])), 0),
                  "recortes_pedidos": sum(int(d["pedidos"]) for d in filas),
                  "recortes_recibidos": sum(int(d["recibidos"]) for d in filas),
                  "segundos_por_recorte": (round(sum(float(d["segundos_captura"]) for d in filas) /
                                                 sum(int(d["recibidos"]) for d in filas), 2)
                                           if sum(int(d["recibidos"]) for d in filas) else None),
                  "tasa_sface": round(np.mean([int(m["yunet_ok"]) for m in ms]), 3) if ms else None,
                  "nitidez": round(float(np.mean([float(m["nitidez"]) for m in ms])), 1) if ms else None,
                  "brillo": round(float(np.mean([float(m["brillo"]) for m in ms])), 1) if ms else None,
                  "contraste": round(float(np.mean([float(m["contraste"]) for m in ms])), 1) if ms else None}
    R["deteccion_y_calidad"] = det
    grafica_deteccion(det)

    # ---- 3. Galería de entrenamiento y comparación de modelos ----
    gal = galeria_de(huellas)
    R["modelos"] = {}
    curvas = {}
    for modelo in MODELOS:
        gv, iv, _ = puntajes_verificacion(gal, huellas, "validacion", modelo)
        gt, it, _ = puntajes_verificacion(gal, huellas, "prueba", modelo)
        umbrales = elegir_umbrales(gv, iv)
        R["modelos"][modelo] = {"validacion": metricas(gv, iv, UMBRAL_ACTUAL),
                                "prueba": {n: metricas(gt, it, u) for n, u in umbrales.items()},
                                "umbrales_elegidos_en_validacion": umbrales}
        curvas[modelo] = (gt, it)
    gt, it, cond_t = puntajes_verificacion(gal, huellas, "prueba")
    grafica_histograma(gt, it, R["modelos"]["top3"]["umbrales_elegidos_en_validacion"],
                       "Similitud de genuinos e impostores (prueba)")
    grafica_far_frr(gt, it)

    # ---- 4. Aumento de datos ----
    motor = comun.MotorFacial()
    t0 = time.time()
    n_t = 0
    for h in huellas[:40]:
        if h["archivo"]:
            img = cv2.imread(os.path.join(DATOS, h["archivo"]))
            r = motor.detectar(img)
            if len(r):
                motor.huella(img, r[0])
                n_t += 1
    R["latencia_huella_ms_en_PC"] = round(1000 * (time.time() - t0) / max(1, n_t), 1)
    extra, conteo_aum = ({}, {}) if a.sin_aumento else aumentar(motor, huellas)
    R["aumento"] = {"variantes": conteo_aum, "huellas_agregadas": {p: len(v) for p, v in extra.items()}}
    if extra:
        gal_aum = galeria_de(huellas, extra=extra)
        gv, iv, _ = puntajes_verificacion(gal_aum, huellas, "validacion")
        ga, ia, _ = puntajes_verificacion(gal_aum, huellas, "prueba")
        umbrales = elegir_umbrales(gv, iv)
        R["modelos"]["top3+aumento"] = {"validacion": metricas(gv, iv, UMBRAL_ACTUAL),
                                        "prueba": {n: metricas(ga, ia, u) for n, u in umbrales.items()},
                                        "umbrales_elegidos_en_validacion": umbrales}
        curvas["top3+aumento"] = (ga, ia)
    grafica_roc(curvas)

    # ---- 5. Robustez por condición (prueba) ----
    por_cond = {}
    for c in sorted(set(cond_t)):
        g = np.array([s for s, cc in zip(gt, cond_t) if cc == c])
        por_cond[c] = metricas(g, it, UMBRAL_ACTUAL)
    R["robustez_prueba_por_condicion"] = por_cond
    grafica_por_condicion(por_cond, "robustez_por_condicion.png", "Prueba por condición (galería de entrenamiento)")

    # ---- 6. Validación cruzada: deja una condición afuera ----
    loco, loco_aum = {}, {}
    gen = [h for h in huellas if h["tipo"] == "genuino"]
    imps = [h for h in huellas if h["tipo"] == "impostor"]
    extra_por_cond = defaultdict(lambda: defaultdict(list))
    if extra:                                       # aumento sin la condición evaluada
        for h in gen:
            if h["parte"] == "entrenamiento" and h["archivo"]:
                img = cv2.imread(os.path.join(DATOS, h["archivo"]))
                for var in variantes(img).values():
                    r = motor.detectar(var)
                    if len(r):
                        extra_por_cond[h["condicion"]][h["persona"]].append(motor.huella(var, r[0]))
    for c in sorted({h["condicion"] for h in gen}):
        por = defaultdict(list)
        for h in gen:
            if h["condicion"] != c and h["parte"] == "entrenamiento":
                por[h["persona"]].append(h["v"])
        g_c = Galeria(por)
        g = np.array([g_c.puntaje(h["persona"], h["v"]) for h in gen if h["condicion"] == c and h["persona"] in g_c.m])
        i = np.array([g_c.puntajes(h["v"])[0][0] for h in imps]) if g_c.m else np.array([])
        loco[c] = metricas(g, i, UMBRAL_ACTUAL)
        if extra:
            for otra, porp in extra_por_cond.items():
                if otra != c:
                    for p, vs in porp.items():
                        por[p].extend(vs)
            g_a = Galeria(por)
            g2 = np.array([g_a.puntaje(h["persona"], h["v"]) for h in gen if h["condicion"] == c and h["persona"] in g_a.m])
            i2 = np.array([g_a.puntajes(h["v"])[0][0] for h in imps]) if g_a.m else np.array([])
            loco_aum[c] = metricas(g2, i2, UMBRAL_ACTUAL)
    R["validacion_cruzada_deja_condicion"] = {"sin_aumento": loco, "con_aumento": loco_aum}
    vals = [m["FRR"] for m in loco.values() if "FRR" in m]
    R["validacion_cruzada_resumen"] = {"FRR_media": round(float(np.mean(vals)), 4), "FRR_desv": round(float(np.std(vals)), 4)} if vals else {}
    if loco_aum:
        vals2 = [m["FRR"] for m in loco_aum.values() if "FRR" in m]
        R["validacion_cruzada_resumen"].update({"FRR_media_con_aumento": round(float(np.mean(vals2)), 4),
                                                "FRR_desv_con_aumento": round(float(np.std(vals2)), 4)})
        grafica_aumento(loco, loco_aum)

    # ---- 7. Curva de aprendizaje ----
    curva = []
    entren = defaultdict(list)
    for h in gen:
        if h["parte"] == "entrenamiento":
            entren[h["persona"]].append(h)
    n_max = min((len(v) for v in entren.values()), default=0)
    for k in [x for x in (1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 80) if x < n_max] + ([n_max] if n_max else []):
        frr_v, frr_t, eer_v = [], [], []
        for _ in range(20):
            por, fuera = {}, []
            for p, lista in entren.items():
                el = random.sample(lista, k)
                por[p] = [h["v"] for h in el]
                fuera += [h for h in lista if h not in el]
            g_k = Galeria(por)
            gv, iv, _ = puntajes_verificacion(g_k, huellas, "validacion")
            frr_v.append((gv < UMBRAL_ACTUAL).mean() if len(gv) else np.nan)
            if len(iv):
                eer_v.append(metricas(gv, iv, UMBRAL_ACTUAL).get("EER", np.nan))
            gtr = np.array([g_k.puntaje(h["persona"], h["v"]) for h in fuera])
            frr_t.append((gtr < UMBRAL_ACTUAL).mean() if len(gtr) else np.nan)
        curva.append({"k": k, "frr_val": [round(float(np.nanmean(frr_v)), 4), round(float(np.nanstd(frr_v)), 4)],
                      "frr_train": [round(float(np.nanmean(frr_t)), 4) if not np.all(np.isnan(frr_t)) else None,
                                    round(float(np.nanstd(frr_t)), 4) if not np.all(np.isnan(frr_t)) else None],
                      "eer_val": [round(float(np.nanmean(eer_v)), 4) if eer_v else None,
                                  round(float(np.nanstd(eer_v)), 4) if eer_v else None]})
    R["curva_aprendizaje"] = curva
    if curva:
        grafica_aprendizaje([{**c, "frr_train": [x if x is not None else np.nan for x in c["frr_train"]],
                              "eer_val": [x if x is not None else np.nan for x in c["eer_val"]]} for c in curva])

    # ---- 8. Identificación (2 o más conductores) ----
    if len(personas) >= 2:
        from sklearn.svm import LinearSVC
        Xtr = np.array([h["v"] for h in gen if h["parte"] == "entrenamiento"])
        ytr = [h["persona"] for h in gen if h["parte"] == "entrenamiento"]
        prueba = [h for h in huellas if h["parte"] == "prueba"]
        reales = [h["persona"] if h["tipo"] == "genuino" else "desconocido" for h in prueba]
        svm = LinearSVC(C=1.0).fit(Xtr, ytr)
        pred_svm, pred_top3 = [], []
        for h in prueba:
            s, p = gal.puntajes(h["v"])[0]
            aceptado = s >= UMBRAL_ACTUAL
            pred_top3.append(p if aceptado else "desconocido")
            pred_svm.append(svm.predict([h["v"]])[0] if aceptado else "desconocido")
        etiquetas = personas + ["desconocido"]
        R["identificacion"] = {}
        for nombre, pred in (("top3", pred_top3), ("svm_lineal", pred_svm)):
            pr, rc, f1, sop = precision_recall_fscore_support(reales, pred, labels=etiquetas, zero_division=0)
            R["identificacion"][nombre] = {
                "exactitud": round(float(np.mean([a == b for a, b in zip(reales, pred)])), 4),
                "por_clase": {e: {"precision": round(float(pr[k]), 3), "recall": round(float(rc[k]), 3),
                                  "F1": round(float(f1[k]), 3), "soporte": int(sop[k])} for k, e in enumerate(etiquetas)},
                "F1_macro": round(float(np.mean(f1)), 4),
                "matriz_confusion": confusion_matrix(reales, pred, labels=etiquetas).tolist(), "etiquetas": etiquetas}
        grafica_confusion(np.array(R["identificacion"]["top3"]["matriz_confusion"]), etiquetas)

    # ---- 9. Regla de 3 recortes (como en un viaje), galería sin la condición evaluada ----
    R["intentos_3_recortes"] = {}
    for regla in ("estricta", "mayoria"):
        res = {"genuino": defaultdict(lambda: [0, 0]), "impostor": [0, 0]}
        for c in sorted({h["condicion"] for h in gen}):
            por = defaultdict(list)
            for h in gen:
                if h["condicion"] != c and h["parte"] == "entrenamiento":
                    por[h["persona"]].append(h["v"])
            g_c = Galeria(por)
            for m0, trio in intentos(muestras, huellas, lambda m, c=c: m["tipo"] == "genuino" and m["condicion"] == c):
                if m0["persona"] in g_c.m:
                    res["genuino"][c][1] += 1
                    res["genuino"][c][0] += decidir(g_c, trio, UMBRAL_ACTUAL, regla) == m0["persona"]
        gal_t = galeria_de(huellas)
        for m0, trio in intentos(muestras, huellas, lambda m: m["tipo"] == "impostor"):
            res["impostor"][1] += 1
            res["impostor"][0] += decidir(gal_t, trio, UMBRAL_ACTUAL, regla) is not None
        tot = [sum(v[0] for v in res["genuino"].values()), sum(v[1] for v in res["genuino"].values())]
        R["intentos_3_recortes"][regla] = {
            "exito_genuinos": round(tot[0] / tot[1], 4) if tot[1] else None, "intentos_genuinos": tot[1],
            "por_condicion": {c: round(v[0] / v[1], 3) for c, v in res["genuino"].items() if v[1]},
            "impostores_aceptados": res["impostor"][0], "intentos_impostores": res["impostor"][1]}

    # ---- 10. Galería recomendada (para el servidor) ----
    final = galeria_de(huellas, partes=("entrenamiento", "validacion", "prueba"), extra=extra)
    gal_json = {p: seleccion_diversa(m, MAX_GALERIA).round(6).tolist() for p, m in final.m.items()}
    with open(os.path.join(DATOS, "galeria_recomendada.json"), "w", encoding="utf-8") as f:
        json.dump({"modelo": "SFace 2021dec (OpenCV Zoo)", "conductores": gal_json}, f)
    R["galeria_recomendada"] = {p: len(v) for p, v in gal_json.items()}

    with open(os.path.join(RES, "metricas.json"), "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=1)
    escribir_resumen(R)
    print(f"Listo. Resultados en {RES}")


def fila(*celdas):
    return "| " + " | ".join(str(c) for c in celdas) + " |"


def pct(x):
    return "—" if x is None else f"{100 * x:.1f} %"


def escribir_resumen(R):
    L = [f"# Entrenamiento y evaluación del reconocimiento (ESP32-CAM OV3660)", "",
         f"Fecha: {R['fecha']} · Protocolo: **{R['protocolo']}** · Conductores: {', '.join(R['conductores'])} · "
         f"Impostores: {', '.join(R['impostores']) or 'ninguno'}", ""]
    if R["protocolo"] == "provisional":
        L += ["> **Provisional:** todavía no hay 3 sesiones por conductor; validación y prueba salen de la misma "
              "sesión que el entrenamiento, así que las cifras son optimistas. La validación cruzada por condición "
              "es la medida más honesta por ahora.", ""]
    L += ["## Datos", "", fila("Persona", "Entrenamiento", "Validación", "Prueba"), fila("---", "---", "---", "---")]
    for p, v in R["datos"]["por_persona_y_parte"].items():
        L.append(fila(p, v.get("entrenamiento", 0), v.get("validacion", 0), v.get("prueba", 0)))
    L += ["", "## Detección y calidad por condición", "",
          fila("Condición", "Detección placa", "SFace ve rostro", "ms/cuadro", "s/recorte", "Nitidez", "Brillo"),
          fila(*["---"] * 7)]
    for c, d in R["deteccion_y_calidad"].items():
        L.append(fila(c, pct(d["tasa_placa"]), pct(d["tasa_sface"]), d["ms_por_cuadro"], d["segundos_por_recorte"] if d["segundos_por_recorte"] is not None else "—",
                      d["nitidez"], d["brillo"]))
    L += ["", "## Modelos (conjunto de prueba)", "",
          fila("Modelo", "Umbral", "Valor", "AUC", "EER", "FAR", "FRR", "Exactitud", "Precisión", "Recall", "F1", "TAR@FAR1%", "d′"),
          fila(*["---"] * 13)]
    for mod, d in R["modelos"].items():
        for n, m in d["prueba"].items():
            L.append(fila(mod, n, m["umbral"], m.get("AUC", "—"), pct(m.get("EER")), pct(m.get("FAR")), pct(m.get("FRR")),
                          pct(m.get("exactitud")), pct(m.get("precision")), pct(m.get("recall_TAR")), m.get("F1", "—"),
                          pct(m.get("TAR_a_FAR_1pct")), m.get("d_prime", "—")))
    L += ["", "## Validación cruzada: condición no vista en el entrenamiento (umbral 0,55)", "",
          fila("Condición", "Similitud media", "Mínima", "FRR", "Con aumento: media", "Con aumento: FRR"), fila(*["---"] * 6)]
    sa, ca = R["validacion_cruzada_deja_condicion"]["sin_aumento"], R["validacion_cruzada_deja_condicion"]["con_aumento"]
    for c, m in sa.items():
        if m.get("n_genuinos"):
            m2 = ca.get(c, {})
            L.append(fila(c, m["media_genuinos"], m["min_genuinos"], pct(m["FRR"]), m2.get("media_genuinos", "—"), pct(m2.get("FRR"))))
    if R.get("validacion_cruzada_resumen"):
        r = R["validacion_cruzada_resumen"]
        L += ["", f"FRR media ± desviación: {pct(r['FRR_media'])} ± {pct(r['FRR_desv'])}" +
              (f" · con aumento: {pct(r['FRR_media_con_aumento'])} ± {pct(r['FRR_desv_con_aumento'])}" if "FRR_media_con_aumento" in r else "")]
    L += ["", "## Intentos con 3 recortes (como en un viaje)", "", fila("Regla", "Éxito genuinos", "Intentos", "Impostores aceptados"),
          fila(*["---"] * 4)]
    for regla, d in R["intentos_3_recortes"].items():
        L.append(fila(regla, pct(d["exito_genuinos"]), d["intentos_genuinos"], f"{d['impostores_aceptados']}/{d['intentos_impostores']}"))
    L += ["", "## Curva de aprendizaje (FRR validación, media ± desv.)", "", fila("Muestras", "FRR validación", "FRR entrenamiento", "EER validación"),
          fila(*["---"] * 4)]
    for c in R["curva_aprendizaje"]:
        L.append(fila(c["k"], f"{pct(c['frr_val'][0])} ± {pct(c['frr_val'][1])}",
                      pct(c["frr_train"][0]) if c["frr_train"][0] is not None else "—",
                      pct(c["eer_val"][0]) if c["eer_val"][0] is not None else "—"))
    L += ["", f"Latencia de la huella SFace en el PC: {R['latencia_huella_ms_en_PC']} ms por recorte.",
          f"Galería recomendada: {R['galeria_recomendada']} muestras por conductor (máximo {MAX_GALERIA})."]
    with open(os.path.join(RES, "resumen.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
