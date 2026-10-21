"""
Mide el nivel relativo de cada liga con los partidos europeos entre equipos de ligas distintas.

Para cada cruce, el modelo calcula lo esperado con los datos de liga de cada equipo hasta ese día
(sin ajuste de nivel). Luego se busca el nivel de cada liga que mejor explica lo que pasó en goles,
tiros, remates y córners. Se ajusta con 2024/25 y 2025/26 y se valida con 2026/27.

Resultado: datos/fuerza_ligas.json (lo usa modelo.py si existe).
"""

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

import champions as UCL
import modelo as M

SALIDA = Path(__file__).resolve().parent / "datos" / "fuerza_ligas.json"
METS = ["goles", "tiros", "remates", "corners"]
CORTE_VALIDACION = pd.Timestamp("2026-08-01")


def cruces(con):
    filas = []
    hist = Path(__file__).resolve().parent / "datos" / "europa_historico.json"
    if hist.exists():
        filas += json.loads(hist.read_text(encoding="utf-8"))
    try:
        filas += pd.read_sql("SELECT * FROM ucl_partidos WHERE completado = 1", con).to_dict("records")
    except Exception:
        pass
    vistos, out = set(), []
    for f in filas:
        if not f.get("completado") or f.get("goles_local") is None:
            continue
        clave = (f["fecha_utc"][:10], f["local"], f["visitante"])
        if clave in vistos:
            continue
        vistos.add(clave)
        ll, nl = UCL.emparejar(con, f["local"], todas=True)
        lv, nv = UCL.emparejar(con, f["visitante"], todas=True)
        if not ll or not lv or ll == lv:
            continue
        out.append(dict(fecha=pd.Timestamp(f["fecha_utc"]).tz_convert("Europe/Madrid").normalize().tz_localize(None),
                        liga_l=ll, liga_v=lv, local=nl, visitante=nv,
                        reales={"goles": (f["goles_local"], f["goles_visitante"]),
                                "tiros": (f.get("tiros_local"), f.get("tiros_visitante")),
                                "remates": (f.get("remates_local"), f.get("remates_visitante")),
                                "corners": (f.get("corners_local"), f.get("corners_visitante"))}))
    return sorted(out, key=lambda x: x["fecha"])


def esperados(df, eu):
    """Recorre la liga día a día y, antes de cada día, calcula lo esperado (sin ajuste) de los cruces europeos."""
    guardado = dict(M.FUERZA)
    M.FUERZA.clear()             # sin ajuste de nivel
    mod = M.Modelo()
    filas, i = [], 0
    for fecha, jornada in df.groupby("fecha", sort=True):
        while i < len(eu) and eu[i]["fecha"] <= fecha:
            c = eu[i]
            i += 1
            for met in METS:
                yl, yv = c["reales"][met]
                if yl is None or yv is None or (isinstance(yl, float) and math.isnan(yl)):
                    continue
                if mod.partidos[(c["liga_l"], c["local"])] < 5 or mod.partidos[(c["liga_v"], c["visitante"])] < 5:
                    continue
                e = mod.esperado(c["liga_l"], c["local"], c["visitante"], met, c["liga_v"])
                if e:
                    filas.append(dict(fecha=c["fecha"], met=met, ll=c["liga_l"], lv=c["liga_v"],
                                      el=e[0], ev=e[1], yl=float(yl), yv=float(yv)))
        for _, p in jornada.iterrows():
            mod.actualizar(p.to_dict())
    M.FUERZA.update(guardado)
    return pd.DataFrame(filas)


def desviacion(d, fuerza):
    """Desviación de Poisson (menos es mejor), normalizada por métrica."""
    total = 0.0
    for met, g in d.groupby("met"):
        f = np.array([fuerza.get(a, 1) / fuerza.get(b, 1) for a, b in zip(g.ll, g.lv)])
        mu_l, mu_v = g.el.values * f, g.ev.values / f
        dev = 0.0
        for y, mu in ((g.yl.values, mu_l), (g.yv.values, mu_v)):
            mu = np.maximum(mu, 1e-6)
            dev += np.sum(np.where(y > 0, y * np.log(np.maximum(y, 1e-9) / mu), 0) - (y - mu))
        total += 2 * dev / max(g.yl.mean() + g.yv.mean(), 1e-6)
    return total


def ajustar(d, ligas):
    ref = ligas[0]

    def obj(x):
        fz = {ref: 1.0, **{l: math.exp(v) for l, v in zip(ligas[1:], x)}}
        return desviacion(d, fz) + 4.0 * float(np.sum(np.square(x)))   # prudencia hacia "todas iguales"
    r = minimize(obj, np.zeros(len(ligas) - 1), method="L-BFGS-B")
    fz = {ref: 1.0, **{l: math.exp(v) for l, v in zip(ligas[1:], r.x)}}
    media = math.exp(np.mean([math.log(v) for v in fz.values()]))
    return {l: round(v / media, 4) for l, v in fz.items()}


if __name__ == "__main__":
    con = sqlite3.connect(M.BD)
    df, _ = M.unir_fuentes(con)
    eu = cruces(con)
    d = esperados(df, eu)
    print(f"cruces entre ligas: {d[d.met == 'goles'].shape[0]} partidos")
    ent, val = d[d.fecha < CORTE_VALIDACION], d[d.fecha >= CORTE_VALIDACION]
    ligas = sorted(set(d.ll) | set(d.lv))
    fz = ajustar(ent, ligas)
    iguales = {l: 1.0 for l in ligas}
    resultado = {"fuerza": fz, "partidos_ajuste": int(ent[ent.met == "goles"].shape[0]),
                 "partidos_validacion": int(val[val.met == "goles"].shape[0])}
    if len(val):
        resultado["validacion_desviacion"] = {
            "sin_ajuste": round(desviacion(val, iguales), 2),
            "a_ojo": round(desviacion(val, dict(M.FUERZA)), 2),
            "medido": round(desviacion(val, fz), 2)}
    # validación cruzada por partidos (5 grupos): ajustar con 4/5 y medir en el 1/5 restante
    ids = d.groupby(["fecha", "ll", "lv"]).ngroup()
    grupo = ids % 5
    cv = {"sin_ajuste": 0.0, "a_ojo": 0.0, "medido": 0.0}
    for k in range(5):
        tr, te = d[grupo != k], d[grupo == k]
        cv["sin_ajuste"] += desviacion(te, iguales)
        cv["a_ojo"] += desviacion(te, dict(M.FUERZA))
        cv["medido"] += desviacion(te, ajustar(tr, ligas))
    resultado["validacion_cruzada"] = {k: round(v, 2) for k, v in cv.items()}
    # el definitivo se ajusta con todo
    resultado["fuerza_final"] = ajustar(d, ligas)
    print(json.dumps(resultado, indent=1, ensure_ascii=False))
    SALIDA.write_text(json.dumps(resultado, indent=1, ensure_ascii=False), encoding="utf-8")
