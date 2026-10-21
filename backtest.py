"""
Backtest: recorre los partidos en orden de fecha. Para cada jornada pronostica
usando SOLO los partidos anteriores, y luego compara con lo que pasó.

- Temporada 2024/25: calentamiento (el modelo aprende y se calibra la dispersión).
- Desde agosto 2025: evaluación (lo que cuenta para la precisión).

Mide, para cada métrica del partido completo y por partes:
  A) Acierto eligiendo más/menos en la línea típica de la liga (siempre se pronostica).
  B) Acierto solo en pronósticos "seguros" (probabilidad del modelo >= 70/75/80 %).
  C) Goles más/menos 2,5 frente a la casa (Bet365).
"""

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

import modelo as M

INICIO_EVAL = pd.Timestamp("2025-08-01")
UMBRALES = [0.70, 0.75, 0.80]


def ejecutar(df):
    mod = M.Modelo()
    calib = []
    filas = []
    for fecha, jornada in df.groupby("fecha", sort=True):
        # 1) pronosticar
        for _, p in jornada.iterrows():
            for met, (cl, cv, _) in M.METRICAS.items():
                if pd.isna(p.get(cl)) or pd.isna(p.get(cv)):
                    continue
                if mod.partidos[(p.liga, p.local)] < 5 or mod.partidos[(p.liga, p.visitante)] < 5:
                    continue
                e = mod.esperado(p.liga, p.local, p.visitante, met)
                if e is None:
                    continue
                real = float(p[cl]) + float(p[cv])
                if fecha < INICIO_EVAL:
                    if fecha >= pd.Timestamp("2024-11-01"):
                        calib.append((met, e[0] + e[1], real))
                else:
                    filas.append(dict(fecha=fecha, liga=p.liga, local=p.local, visitante=p.visitante,
                                      met=met, esp=e[0] + e[1], real=real,
                                      b365_mas25=p.get("b365_mas25"), b365_menos25=p.get("b365_menos25")))
        if fecha >= INICIO_EVAL and not mod.dispersion:
            M.calibrar_dispersion(mod, calib)
            cdf = pd.DataFrame(calib, columns=["met", "esp", "real"])
            mod.lineas = {met: M.elegir_linea(g.real) for met, g in cdf.groupby("met")}
            M.calibrar_platt(mod, calib, mod.lineas)
        # 2) aprender del resultado
        for _, p in jornada.iterrows():
            mod.actualizar(p.to_dict())
    return pd.DataFrame(filas), mod, pd.DataFrame(calib, columns=["met", "esp", "real"])


def evaluar(res, mod, calib):
    salida = {}
    lineas = mod.lineas

    res = res.copy()
    res["linea"] = res.met.map(lineas)
    res["p_mas"] = [mod.prob(m, e, l) for m, e, l in zip(res.met, res.esp, res.linea)]
    res["elige_mas"] = res.p_mas >= 0.5
    res["p_elegida"] = np.where(res.elige_mas, res.p_mas, 1 - res.p_mas)
    res["acierto"] = np.where(res.elige_mas, res.real > res.linea, res.real < res.linea)
    # referencia "tonta": elegir siempre el lado que más salió en el calentamiento
    base = {met: (g.real > lineas[met]).mean() for met, g in calib.groupby("met")}
    res["acierto_base"] = [(r > l) if base[m] >= 0.5 else (r < l)
                           for m, r, l in zip(res.met, res.real, res.linea)]

    for ambito, sub in [("Todas las ligas", res), ("LaLiga", res[res.liga == "laliga"])]:
        tabla = []
        for met in M.METRICAS:
            g = sub[sub.met == met]
            if g.empty:
                continue
            fila = dict(metrica=M.NOMBRES[met], linea=lineas[met], n=len(g),
                        acierto=g.acierto.mean(), base=g.acierto_base.mean(),
                        error_medio=(g.esp - g.real).abs().mean())
            for u in UMBRALES:
                s = g[g.p_elegida >= u]
                fila[f"seg{int(u*100)}_n"] = len(s)
                fila[f"seg{int(u*100)}_acierto"] = s.acierto.mean() if len(s) else None
            tabla.append(fila)
        salida[ambito] = pd.DataFrame(tabla)

    # Calibración: cuando el modelo dice X%, ¿acierta X%?
    res["tramo"] = pd.cut(res.p_elegida, [0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    salida["calibracion"] = (res.groupby("tramo", observed=True)
                             .agg(n=("acierto", "size"), prob_media=("p_elegida", "mean"),
                                  acierto=("acierto", "mean")).reset_index())

    # Goles +/- 2.5 frente a la casa
    g = res[(res.met == "goles") & res.b365_mas25.notna()].copy()
    g["p_mas25"] = [mod.prob("goles", e, 2.5) for e in g.esp]
    imp_mas = (1 / g.b365_mas25) / (1 / g.b365_mas25 + 1 / g.b365_menos25)
    g["casa_mas"] = imp_mas >= 0.5
    g["modelo_mas"] = g.p_mas25 >= 0.5
    g["hubo_mas"] = g.real > 2.5
    g["cuota_elegida"] = np.where(g.modelo_mas, g.b365_mas25, g.b365_menos25)
    g["valor"] = np.where(g.modelo_mas, g.p_mas25, 1 - g.p_mas25) * g.cuota_elegida - 1
    con_valor = g[g.valor > 0.05]
    ganancia = (np.where(con_valor.modelo_mas == con_valor.hubo_mas, con_valor.cuota_elegida - 1, -1)).sum()
    salida["casa"] = dict(
        n=len(g),
        acierto_modelo=float((g.modelo_mas == g.hubo_mas).mean()),
        acierto_casa=float((g.casa_mas == g.hubo_mas).mean()),
        n_valor=len(con_valor),
        rentabilidad_valor=float(ganancia / len(con_valor)) if len(con_valor) else None,
    )
    # Líneas seguras (objetivo >= 80 %)
    seg = []
    for r in res.itertuples():
        s = mod.linea_segura(r.met, r.esp)
        if s:
            p, lado, l = s
            seg.append((r.liga, r.met, p, (r.real > l) if lado == "más" else (r.real < l)))
    seg = pd.DataFrame(seg, columns=["liga", "met", "p", "ok"])
    tabla = []
    for met in M.METRICAS:
        g, gl = seg[seg.met == met], seg[(seg.met == met) & (seg.liga == "laliga")]
        tabla.append(dict(metrica=M.NOMBRES[met], n=len(g), prob_media=g.p.mean(),
                          acierto=g.ok.mean(), acierto_laliga=gl.ok.mean()))
    salida["lineas_seguras"] = pd.DataFrame(tabla)
    salida["lineas"] = lineas
    salida["dispersion"] = mod.dispersion
    return salida, res


if __name__ == "__main__":
    df, _ = M.unir_fuentes()
    res, mod, calib = ejecutar(df)
    salida, detalle = evaluar(res, mod, calib)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    for k in ["Todas las ligas", "LaLiga"]:
        print(f"\n===== {k} =====")
        print(salida[k].round(3).to_string(index=False))
    print("\n===== Líneas seguras (>=80 %) =====")
    print(salida["lineas_seguras"].round(3).to_string(index=False))
    print("\n===== Calibración =====")
    print(salida["calibracion"].round(3).to_string(index=False))
    print("\n===== Goles +/-2.5 vs Bet365 =====")
    print(salida["casa"])
    print("\nDispersión:", {k: round(v, 2) for k, v in salida["dispersion"].items()})
    Path("backtest_resultados.json").write_text(json.dumps({
        k: (v.to_dict(orient="records") if isinstance(v, pd.DataFrame) else v)
        for k, v in salida.items()}, default=str, ensure_ascii=False, indent=1), encoding="utf-8")
    detalle.to_csv("backtest_detalle.csv", index=False)
