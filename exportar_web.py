"""
Exporta a JSON los partidos (jugados y por jugar) con pronósticos, selecciones y resultados para la web.

- Partidos por jugar: modelo entrenado con todo lo disponible.
- Partidos ya jugados: modelo entrenado SOLO con los partidos anteriores a ese día
  (como si el pronóstico se hubiera hecho la víspera), y se añade el resultado real.

Uso:  python exportar_web.py 2026-10-09 2026-10-27
"""
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import backtest as B
import combinada as C
import lectura as L
import modelo as M
import seleccion as S


def _modelo_hasta(df, nombres, dia):
    _, mod, _ = B.ejecutar(df[df.fecha < pd.Timestamp(dia)])
    mod.nombres = nombres
    return mod


CACHE = Path(__file__).resolve().parent / "dias"


def _completo(dia_json):
    """Un día pasado está cerrado cuando todos sus partidos tienen resultado completo (incl. córners)."""
    return all(p.get("real") and "corners" in p["real"] and "tarjetas" in p["real"]
               for p in dia_json["partidos"])


def _partidos_liga(con, mod, todos, px, nombres, reales_df):
    partidos = []
    todos = todos.sort_values("relevancia", ascending=False)
    top = set(todos.groupby("liga").head(2).understat_id)
    for r in todos.itertuples():
        uid = int(r.understat_id)
        p = dict(understat_id=uid, liga=r.liga, fecha=r.fecha, hora=r.hora,
                 local=r.local, visitante=r.visitante, motivo=r.motivo,
                 relevancia=float(r.relevancia), destacado=uid in top)
        if mod.partidos[(r.liga, r.local)] < 3 or mod.partidos[(r.liga, r.visitante)] < 3:
            continue
        pron = mod.pronosticar(r.liga, r.local, r.visitante)
        if not pron:
            continue
        t = S.clasificacion(con, r.liga).set_index("equipo")
        ctx = {eq: dict(posicion=int(t.loc[eq].pos), puntos=int(t.loc[eq].pts),
                        ultimos_5=t.loc[eq].forma_txt) for eq in (r.local, r.visitante) if eq in t.index}

        # cuotas reales de goles +/-2,5
        cuotas = None
        if r.jugado and uid in reales_df.index and pd.notna(reales_df.loc[uid].get("b365_mas25")):
            f = reales_df.loc[uid]
            cuotas = {"mas25": float(f.b365_mas25), "menos25": float(f.b365_menos25)}
        elif not px.empty:
            for q in px.itertuples():
                if q.liga == r.liga and nombres.get((q.liga, q.local), q.local) == r.local \
                        and nombres.get((q.liga, q.visitante), q.visitante) == r.visitante \
                        and pd.notna(q.b365_mas25):
                    cuotas = {"mas25": float(q.b365_mas25), "menos25": float(q.b365_menos25)}
        cands = C._reducir(C.candidatos(mod, p, cuotas))

        # resultado real
        real = None
        if r.jugado and uid in reales_df.index:
            f = reales_df.loc[uid]
            real = {}
            for met, (cl, cv, _) in M.METRICAS.items():
                if pd.notna(f.get(cl)) and pd.notna(f.get(cv)):
                    real[met] = round(float(f[cl]) + float(f[cv]), 2)
            real["marcador"] = [int(f.goles_local), int(f.goles_visitante)]

        p.update(
            pocos_datos=[eq for eq in (r.local, r.visitante) if mod.partidos[(r.liga, eq)] < 5],
            contexto=ctx, pronosticos=pron, lectura=L.generar(p, pron, ctx), real=real,
            selecciones=[dict(familia=slot[1], metrica=c["metrica"], texto=c["texto"],
                              linea=float(c["texto"].split(" de ")[1].split()[0]),
                              lado="más" if c["texto"].startswith("Más") else "menos",
                              prob=round(c["prob"], 4), cuota=round(c["cuota_casa"], 3),
                              real=c["cuota_es_real"])
                         for slot, lista in cands.items() for c in lista])
        partidos.append(p)
    return partidos


def _partidos_ucl(con, mod, ucl):
    """Partidos de Champions entre equipos de nuestras ligas (pronóstico experimental)."""
    filas_ucl = pd.read_sql("SELECT * FROM ucl_partidos", con).set_index("espn_id")
    ucl = ucl.sort_values("relevancia", ascending=False)
    top = set(ucl[ucl.liga_local.notna() & ucl.liga_visitante.notna()].head(3).espn_id)
    partidos = []
    for r in ucl.itertuples():
        if not r.liga_local or not r.liga_visitante or pd.isna(r.liga_local) or pd.isna(r.liga_visitante):
            continue
        if mod.partidos[(r.liga_local, r.local)] < 3 or mod.partidos[(r.liga_visitante, r.visitante)] < 3:
            continue
        p = dict(understat_id=int(r.espn_id), liga="ucl", liga_local=r.liga_local, liga_visitante=r.liga_visitante,
                 fecha=r.fecha, hora=r.hora, local=r.local, visitante=r.visitante, motivo=r.motivo,
                 relevancia=float(r.relevancia), destacado=r.espn_id in top, experimental=True)
        pron = mod.pronosticar(r.liga_local, r.local, r.visitante, r.liga_visitante)
        if not pron:
            continue
        ctx = {}
        for eq, lg in ((r.local, r.liga_local), (r.visitante, r.liga_visitante)):
            t = S.clasificacion(con, lg).set_index("equipo")
            if eq in t.index:
                ctx[eq] = dict(posicion=int(t.loc[eq].pos), puntos=int(t.loc[eq].pts),
                               ultimos_5=t.loc[eq].forma_txt, liga=lg)
        cands = C._reducir(C.candidatos(mod, p, None))
        real = None
        f = filas_ucl.loc[r.espn_id] if r.espn_id in filas_ucl.index else None
        if r.jugado and f is not None and pd.notna(f.goles_local):
            gl, gv = int(f.goles_local), int(f.goles_visitante)
            real = {"marcador": [gl, gv], "goles": gl + gv}
            if pd.notna(f.goles_local_1t):
                g1 = int(f.goles_local_1t) + int(f.goles_visitante_1t)
                real["goles_1t"], real["goles_2t"] = g1, gl + gv - g1
            for met, a, b in (("tiros", "tiros_local", "tiros_visitante"),
                              ("remates", "remates_local", "remates_visitante"),
                              ("corners", "corners_local", "corners_visitante")):
                if pd.notna(f[a]) and pd.notna(f[b]):
                    real[met] = int(f[a]) + int(f[b])
            if pd.notna(f.amarillas_local):
                real["tarjetas"] = int(f.amarillas_local) + int(f.amarillas_visitante) + \
                    int(f.rojas_local or 0) + int(f.rojas_visitante or 0)
        p.update(
            pocos_datos=[], contexto=ctx, pronosticos=pron, lectura=L.generar(p, pron, ctx), real=real,
            selecciones=[dict(familia=slot[1], metrica=c["metrica"], texto=c["texto"],
                              linea=float(c["texto"].split(" de ")[1].split()[0]),
                              lado="más" if c["texto"].startswith("Más") else "menos",
                              prob=round(c["prob"], 4), cuota=round(c["cuota_casa"], 3), real=False)
                         for slot, lista in cands.items() for c in lista])
        partidos.append(p)
    return partidos


def exportar(desde, hasta):
    CACHE.mkdir(exist_ok=True)
    df, nombres = M.unir_fuentes()
    mod_actual = _modelo_hasta(df, nombres, pd.Timestamp.max.normalize())
    con = sqlite3.connect(M.BD)
    try:
        px = pd.read_sql("SELECT liga, local, visitante, b365_mas25, b365_menos25 FROM proximos", con)
    except Exception:
        px = pd.DataFrame()
    reales_df = df.dropna(subset=["understat_id"]).copy()
    reales_df["understat_id"] = reales_df.understat_id.astype(int)
    reales_df = reales_df.drop_duplicates("understat_id").set_index("understat_id")

    salida = {"generado": pd.Timestamp.now(tz="Europe/Madrid").strftime("%Y-%m-%d %H:%M"),
              "ultimo_dato": con.execute("SELECT MAX(fecha) FROM us_partidos WHERE jugado=1").fetchone()[0],
              "dias": []}
    dia = desde
    while dia <= hasta:
        guardado = CACHE / f"{dia.isoformat()}.json"
        if guardado.exists():
            dj = json.loads(guardado.read_text(encoding="utf-8"))
            if dj.get("completo"):
                salida["dias"].append(dj)
                print(dia, "de caché")
                dia += timedelta(days=1)
                continue
        todos = S.puntuar(con, S.partidos_del_dia(con, dia))
        ucl = S.puntuar_ucl(con, S.partidos_ucl_del_dia(con, dia))
        if todos.empty and ucl.empty:
            dia += timedelta(days=1)
            continue
        estados = list(todos.jugado) if not todos.empty else []
        estados += list(ucl.jugado) if not ucl.empty else []
        jugado_dia = any(estados)
        todo_jugado = all(estados)
        mod = _modelo_hasta(df, nombres, dia) if jugado_dia else mod_actual
        partidos = []
        if not todos.empty:
            partidos += _partidos_liga(con, mod, todos, px, nombres, reales_df)
        if not ucl.empty:
            partidos += _partidos_ucl(con, mod, ucl)
        if partidos:
            dj = {"dia": dia.isoformat(), "jugado": jugado_dia, "partidos": partidos}
            dj["completo"] = bool(todo_jugado and _completo(dj))
            if dj["completo"]:
                guardado.write_text(json.dumps(dj, ensure_ascii=False, default=float), encoding="utf-8")
            salida["dias"].append(dj)
            print(dia, len(partidos), "cerrado" if dj["completo"] else ("jugado" if jugado_dia else ""))
        dia += timedelta(days=1)
    return salida


INICIO = date(2026, 10, 9)   # primer día con seguimiento de aciertos

if __name__ == "__main__":
    import sys
    a = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else INICIO
    b = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today() + timedelta(days=8)
    d = exportar(a, b)
    datos = json.dumps(d, ensure_ascii=False, default=float).replace("</", "<\\/")
    raiz = Path(__file__).resolve().parent
    plantilla = (raiz / "web_plantilla.html").read_text(encoding="utf-8")
    cabeza = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#121411">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Pronósticos">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%23121411'/%3E%3Ccircle cx='32' cy='32' r='18' fill='%23d4f53c'/%3E%3C/svg%3E">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}img{max-width:100%}</style>
"""
    cuerpo = plantilla.replace("__DATOS__", datos).replace('<div class="wrap">', '</head>\n<body>\n<div class="wrap">', 1)
    html = cabeza + cuerpo + "\n</body>\n</html>\n"
    (raiz / "docs").mkdir(exist_ok=True)
    (raiz / "docs" / "index.html").write_text(html, encoding="utf-8")
    (raiz / "docs" / ".nojekyll").write_text("", encoding="utf-8")
    print("listo", d["generado"], "->", raiz / "docs" / "index.html")
