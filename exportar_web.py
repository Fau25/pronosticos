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
    """Un día pasado está cerrado cuando todos sus partidos tienen resultado completo (incl. córners)
    o están aplazados (sin resultado varios días después)."""
    return all(p.get("aplazado") or (p.get("real") and "corners" in p["real"] and "tarjetas" in p["real"])
               for p in dia_json["partidos"])


DIAS_APLAZADO = 3     # sin resultado tantos días después -> aplazado (no cuenta en aciertos)
DIAS_WEB = 30         # días pasados que carga la web al abrir; el resto va resumido en historico.json
_CLAS = {}


def _clas(con, liga):
    """Clasificación de una liga, calculada una sola vez por ejecución."""
    if liga not in _CLAS:
        _CLAS[liga] = S.clasificacion(con, liga).set_index("equipo")
    return _CLAS[liga]


AVISOS = []   # avisos de esta ejecución (se muestran discretos en la web)


# ------------------------------------------------------------------ el "porqué": últimos 5 partidos

_HIST = {}
ULT_METS = ["goles", "goles_1t", "goles_2t", "xg", "xg_1t", "xg_2t", "tiros", "tiros_1t", "tiros_2t",
            "remates", "remates_1t", "remates_2t", "corners", "tarjetas"]


def preparar_historial(df):
    """(liga, equipo) -> lista de (fecha, rival, en_casa, {métrica: total del partido}) ordenada por fecha."""
    _HIST.clear()
    for r in df.itertuples(index=False):
        d = r._asdict()
        tot = {}
        for met in ULT_METS:
            cl, cv, _ = M.METRICAS[met]
            a, b = d.get(cl), d.get(cv)
            if a is not None and b is not None and not (pd.isna(a) or pd.isna(b)):
                tot[met] = round(float(a) + float(b), 2)
        for eq, rival, casa in ((r.local, r.visitante, True), (r.visitante, r.local, False)):
            _HIST.setdefault((r.liga, eq), []).append((r.fecha, rival, casa, tot))
    for v in _HIST.values():
        v.sort(key=lambda x: x[0])


def ultimos(liga, equipo, fecha, n=5):
    """Los n últimos partidos del equipo antes de 'fecha' (más reciente primero)."""
    f = pd.Timestamp(fecha)
    previos = [x for x in _HIST.get((liga, equipo), []) if x[0] < f][-n:][::-1]
    return {"rivales": [("vs " if c else "en ") + str(r) for _, r, c, _ in previos],
            "valores": {met: [t.get(met) for _, _, _, t in previos] for met in ULT_METS
                        if any(t.get(met) is not None for _, _, _, t in previos)}}


def _partidos_liga(con, mod, todos, px, nombres, reales_df):
    partidos = []
    todos = todos.sort_values("relevancia", ascending=False)
    top = set(todos.groupby("liga").head(2).understat_id)
    for r in todos.itertuples():
        uid = int(r.understat_id)
        p = dict(understat_id=uid, liga=r.liga, fecha=r.fecha, hora=r.hora,
                 local=r.local, visitante=r.visitante, motivo=r.motivo,
                 relevancia=float(r.relevancia), destacado=uid in top, aplazado=bool(getattr(r, "aplazado", False)))
        if mod.partidos[(r.liga, r.local)] < 3 or mod.partidos[(r.liga, r.visitante)] < 3:
            continue
        pron = mod.pronosticar(r.liga, r.local, r.visitante)
        if not pron:
            continue
        t = _clas(con, r.liga)
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

        p["ultimos"] = {"local": ultimos(r.liga, r.local, r.fecha), "visitante": ultimos(r.liga, r.visitante, r.fecha)}
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
            t = _clas(con, lg)
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
        p["ultimos"] = {"local": ultimos(r.liga_local, r.local, r.fecha),
                        "visitante": ultimos(r.liga_visitante, r.visitante, r.fecha)}
        p.update(
            pocos_datos=[], contexto=ctx, pronosticos=pron, lectura=L.generar(p, pron, ctx), real=real,
            selecciones=[dict(familia=slot[1], metrica=c["metrica"], texto=c["texto"],
                              linea=float(c["texto"].split(" de ")[1].split()[0]),
                              lado="más" if c["texto"].startswith("Más") else "menos",
                              prob=round(c["prob"], 4), cuota=round(c["cuota_casa"], 3), real=False)
                         for slot, lista in cands.items() for c in lista])
        partidos.append(p)
    return partidos


ESC_FAM = {"goles", "goles_1t", "goles_2t", "tiros", "remates", "corners", "tarjetas"}
# cuota estimada de cada escalón entre 1,15 y 1,20  (-> probabilidad real ~79-82 %)
ESC_CUOTA_MIN, ESC_CUOTA_MAX, ESC_CUOTA_OBJ = 1.15, 1.20, 1.175


def escalera_dia(mod, partidos):
    """4 opciones del día para la escalera (partidos distintos, cuota estimada 1,15-1,20) y una recomendada."""
    mejores = []
    for p in partidos:
        cand = None
        for f in p["pronosticos"]:
            if f["metrica"] not in ESC_FAM:
                continue
            for l in [x + 0.5 for x in range(0, 45)]:
                pm = mod.prob_mas(f["metrica"], f["esperado_total"], l)
                for prob, lado in ((pm, "más"), (1 - pm, "menos")):
                    q = C.cuota_casa_estimada(prob)
                    if ESC_CUOTA_MIN <= q <= ESC_CUOTA_MAX and (cand is None or abs(q - ESC_CUOTA_OBJ) < abs(cand["cuota"] - ESC_CUOTA_OBJ)):
                        nombre = M.NOMBRES[f["metrica"]].lower()
                        cand = dict(understat_id=p["understat_id"], liga=p["liga"], hora=p["hora"],
                                    partido=f"{p['local']} – {p['visitante']}", metrica=f["metrica"],
                                    lado=lado, linea=l, prob=round(prob, 4),
                                    cuota=round(C.cuota_casa_estimada(prob), 3),
                                    texto=f"{lado.capitalize()} de {str(l).replace('.', ',')} {nombre}")
        if cand:
            mejores.append(cand)
    # se prefieren ligas a Champions (experimental), y luego la probabilidad
    # 4 opciones del día; la recomendada es la más probable, con preferencia por mercados que ofrecen
    # todas las casas (goles, córners, tarjetas) y por ligas antes que Champions (experimental)
    habitual = {"goles", "goles_1t", "goles_2t", "corners", "tarjetas"}
    for c in mejores:
        c["puntos"] = c["prob"] + (0.01 if c["metrica"] in habitual else 0) - (0.03 if c["liga"] == "ucl" else 0)
    mejores.sort(key=lambda c: -c["puntos"])
    opciones = mejores[:4]
    for i, c in enumerate(opciones):
        c["recomendada"] = i == 0
        motivos = ["la más segura del día" if i == 0 else None,
                   "mercado que ofrecen todas las casas" if c["metrica"] in habitual else None,
                   "pronóstico experimental (Champions)" if c["liga"] == "ucl" else None]
        c["motivo"] = " · ".join(m for m in motivos if m)
        del c["puntos"]
    return sorted(opciones, key=lambda c: (not c["recomendada"], c["hora"]))


def exportar(desde, hasta):
    CACHE.mkdir(exist_ok=True)
    df, nombres = M.unir_fuentes()
    preparar_historial(df)
    mod_actual = _modelo_hasta(df, nombres, pd.Timestamp.max.normalize())
    con = sqlite3.connect(M.BD)
    try:
        px = pd.read_sql("SELECT liga, local, visitante, b365_mas25, b365_menos25 FROM proximos", con)
    except Exception:
        px = pd.DataFrame()
    # partidos con resultado en football-data pero que Understat aún no ha cerrado:
    # se les asigna el id del calendario de Understat para no esperar a que se ponga al día
    cal = pd.read_sql("SELECT understat_id, liga, fecha, local, visitante FROM us_partidos", con)
    cal["fecha"] = pd.to_datetime(cal["fecha"])
    sin_id = df[df.understat_id.isna()]
    if not sin_id.empty:
        cruce = sin_id.reset_index().merge(cal, on=["liga", "local", "visitante"], suffixes=("", "_cal"))
        cruce = cruce[(cruce.fecha - cruce.fecha_cal).abs() <= pd.Timedelta(days=1)]
        df.loc[cruce["index"], "understat_id"] = cruce["understat_id_cal"].values
        if len(cruce):
            print(f"  {len(cruce)} partidos con resultado solo en football-data (Understat aún sin cerrar)")
    reales_df = df.dropna(subset=["understat_id"]).copy()
    reales_df["understat_id"] = reales_df.understat_id.astype(int)
    reales_df = reales_df.drop_duplicates("understat_id").set_index("understat_id")
    # al revés: Understat ya tiene el resultado pero football-data aún no lo ha publicado.
    # Se marca lo que ya se sabe con certeza (goles, xG, tiros y remates por partes, todo de Understat);
    # córners, tarjetas, tiros totales y goles por parte llegan con football-data en la siguiente actualización
    us = pd.read_sql("SELECT * FROM us_estadisticas", con)
    us = us[~us.understat_id.astype(int).isin(reales_df.index)].copy()
    if not us.empty:
        us = us.rename(columns={"remates_puerta_local_1t": "remates_local_1t", "remates_puerta_visitante_1t": "remates_visitante_1t",
                                "remates_puerta_local_2t": "remates_local_2t", "remates_puerta_visitante_2t": "remates_visitante_2t"})
        cols = ["goles_local", "goles_visitante", "xg_local", "xg_visitante", "xg_local_1t", "xg_visitante_1t",
                "xg_local_2t", "xg_visitante_2t", "tiros_local_1t", "tiros_visitante_1t", "tiros_local_2t",
                "tiros_visitante_2t", "remates_local_1t", "remates_visitante_1t", "remates_local_2t", "remates_visitante_2t"]
        us["understat_id"] = us.understat_id.astype(int)
        reales_df = pd.concat([reales_df, us.set_index("understat_id")[cols]])
        print(f"  {len(us)} partidos con resultado solo en Understat (football-data aún sin publicar)")

    ahora = pd.Timestamp.now(tz="Europe/Madrid")
    salida = {"generado": ahora.strftime("%Y-%m-%d %H:%M"), "generado_ts": int(ahora.timestamp() * 1000),
              "ultimo_dato": con.execute("SELECT MAX(fecha) FROM us_partidos WHERE jugado=1").fetchone()[0],
              "dias": [], "correlaciones": C.CORRELACION}
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
        if not todos.empty:  # jugado si lo dice Understat o si football-data ya tiene el resultado
            todos["jugado"] = todos.jugado.astype(bool) | todos.understat_id.astype(int).isin(reales_df.index)
            todos["aplazado"] = ~todos.jugado & (dia < HOY - timedelta(days=DIAS_APLAZADO))
        ucl = S.puntuar_ucl(con, S.partidos_ucl_del_dia(con, dia))
        if not ucl.empty and dia >= HOY - timedelta(days=1):
            fuera = ucl[ucl.liga_local.isna() | ucl.liga_visitante.isna()]
            if len(fuera):
                eqs = sorted({e for r in fuera.itertuples() for e, l in ((r.local, r.liga_local), (r.visitante, r.liga_visitante))
                              if l is None or pd.isna(l)})
                AVISOS.append(f"{dia:%d/%m}: {len(fuera)} partido(s) de Champions sin pronóstico (sin datos de liga de {', '.join(eqs)})")
        if todos.empty and ucl.empty:
            dia += timedelta(days=1)
            continue
        estados = list(todos.jugado | todos.aplazado) if not todos.empty else []
        estados += list(ucl.jugado) if not ucl.empty else []
        jugado_dia = (not todos.empty and bool(todos.jugado.any())) or (not ucl.empty and bool(ucl.jugado.any()))
        todo_jugado = all(estados)
        mod = _modelo_hasta(df, nombres, dia) if jugado_dia else mod_actual
        partidos = []
        if not todos.empty:
            partidos += _partidos_liga(con, mod, todos, px, nombres, reales_df)
        if not ucl.empty:
            partidos += _partidos_ucl(con, mod, ucl)
        if partidos:
            dj = {"dia": dia.isoformat(), "jugado": jugado_dia, "partidos": partidos,
                  "escalera": escalera_dia(mod, partidos)}
            dj["completo"] = bool(todo_jugado and _completo(dj))
            if dj["completo"]:
                guardado.write_text(json.dumps(dj, ensure_ascii=False, default=float), encoding="utf-8")
            salida["dias"].append(dj)
            print(dia, len(partidos), "cerrado" if dj["completo"] else ("jugado" if jugado_dia else ""))
        dia += timedelta(days=1)
    return salida


INICIO = date(2026, 10, 9)   # primer día con seguimiento de aciertos
HOY = pd.Timestamp.now(tz="Europe/Madrid").date()
APOSTABLES = {"goles", "goles_1t", "goles_2t", "tiros", "remates", "corners", "tarjetas"}


def _ok(p, met, lado, linea):
    real = (p.get("real") or {}).get(met)
    if real is None or not lado or linea is None:
        return None
    return bool(real > linea) if lado.lower().startswith("más") else bool(real < linea)


def _destacados(p):
    ap = [f for f in p["pronosticos"] if f["metrica"] in APOSTABLES]
    normal = max((f for f in ap if (f.get("prob") or 0) >= 0.55), key=lambda f: f["prob"], default=None)
    segura = min((f for f in ap if f.get("segura_prob")), key=lambda f: f["segura_prob"], default=None)
    return normal, segura


def _resumen_dia(x):
    """Conteos de la web para un día: partidos por liga, escalera con su resultado y aciertos por partido."""
    cuenta = {}
    for p in x["partidos"]:
        cuenta[p["liga"]] = cuenta.get(p["liga"], 0) + 1
    por_id = {p["understat_id"]: p for p in x["partidos"]}
    esc = []
    for e in x.get("escalera", []):
        p = por_id.get(e["understat_id"])
        esc.append({**e, "ok": _ok(p, e["metrica"], e["lado"], e["linea"]) if p else None,
                    "real": ((p or {}).get("real") or {}).get(e["metrica"])})
    res = []
    for p in x["partidos"]:
        if not p.get("real") or p.get("aplazado"):
            continue
        normal, segura = _destacados(p)
        res.append({"l": p["liga"], "d": bool(p["destacado"]),
                    "n": _ok(p, normal["metrica"], normal["eleccion"], normal["linea"]) if normal else None,
                    "s": _ok(p, segura["metrica"], segura["segura_lado"], segura["segura_linea"]) if segura else None,
                    "m": {f["metrica"]: [_ok(p, f["metrica"], f["eleccion"], f["linea"]) if (f.get("prob") or 0) >= 0.55 else None,
                                         _ok(p, f["metrica"], f.get("segura_lado"), f.get("segura_linea"))]
                          for f in p["pronosticos"]}})
    return cuenta, esc, res


def _corte():
    return (HOY - timedelta(days=DIAS_WEB)).isoformat()


def indice(d):
    """Índice ligero que va dentro de la página: solo los últimos DIAS_WEB días y los próximos."""
    dias = []
    for x in d["dias"]:
        if x["dia"] < _corte():
            continue
        cuenta, esc, _ = _resumen_dia(x)
        dias.append({"dia": x["dia"], "jugado": x["jugado"], "n": cuenta, "escalera": esc})
    return {"generado": d["generado"], "generado_ts": d["generado_ts"], "ultimo_dato": d["ultimo_dato"],
            "estado": estado_fuentes(), "correlaciones": d.get("correlaciones", {}), "dias": dias}


def historico(d):
    """Todo el histórico, resumido: se descarga solo al abrir Aciertos o Escalera.
    res: [día, liga, destacado, [aciertos, total] más probable, [aciertos, total] segura,
          {métrica: [aciertos, total más probable, aciertos, total segura]}, nº de partidos]  (agrupado por día, liga y destacado)
    escalera: escalones recomendados de cada día."""
    filas, escalera = {}, []
    for x in d["dias"]:
        if not x["jugado"]:
            continue
        _, esc, res = _resumen_dia(x)
        escalera += [{**e, "dia": x["dia"]} for e in esc if e.get("recomendada") is not False]
        for r in res:
            k = (x["dia"], r["l"], r["d"])
            f = filas.setdefault(k, [x["dia"], r["l"], r["d"], [0, 0], [0, 0], {}, 0])
            f[6] += 1
            for i, ok in ((3, r["n"]), (4, r["s"])):
                if ok is not None:
                    f[i][1] += 1; f[i][0] += int(ok)
            for met, (okn, oks) in r["m"].items():
                m = f[5].setdefault(met, [0, 0, 0, 0])
                if okn is not None:
                    m[1] += 1; m[0] += int(okn)
                if oks is not None:
                    m[3] += 1; m[2] += int(oks)
    return {"generado": d["generado"], "res": list(filas.values()), "escalera": escalera}


NOMBRE_FUENTE = {"futdata": "resultados de las 5 ligas", "champions": "Champions", "europa": "competiciones europeas",
                 "extra": "ligas adicionales", "nivel": "nivel de las ligas"}


def estado_fuentes():
    """Lee estado.json (lo escribe el workflow con el resultado de cada paso) -> {fallos: [...], origen_bd}."""
    f = Path(__file__).resolve().parent / "estado.json"
    try:
        e = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fallos": [], "avisos": _avisos()}
    fallos = [NOMBRE_FUENTE.get(k, k) for k, v in (e.get("pasos") or {}).items() if v == "failure"]
    if e.get("origen_bd") == "semilla":
        fallos.append("la base de datos guardada (se recuperó la copia inicial)")
    return {"fallos": fallos, "origen_bd": e.get("origen_bd"), "avisos": _avisos()}


def _avisos():
    """Avisos de los scripts de descarga (avisos.txt) y de esta ejecución."""
    import comun
    return (comun.leer() + AVISOS)[:8]


if __name__ == "__main__":
    import sys
    a = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else INICIO
    b = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else HOY + timedelta(days=8)
    d = exportar(a, b)
    raiz = Path(__file__).resolve().parent
    # un archivo por día (se descarga solo cuando se abre ese día)
    carpeta = raiz / "docs" / "web" / "dias"
    if carpeta.exists():
        for f in carpeta.glob("*.json"):
            f.unlink()
    carpeta.mkdir(parents=True, exist_ok=True)
    for x in d["dias"]:
        if x["dia"] < _corte():
            continue
        (carpeta / f"{x['dia']}.json").write_text(
            json.dumps({"dia": x["dia"], "partidos": x["partidos"]}, ensure_ascii=False, default=float,
                       separators=(",", ":")), encoding="utf-8")
    (raiz / "docs" / "web" / "historico.json").write_text(
        json.dumps(historico(d), ensure_ascii=False, default=float, separators=(",", ":")), encoding="utf-8")
    datos = json.dumps(indice(d), ensure_ascii=False, default=float, separators=(",", ":")).replace("</", "<\\/")
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
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}img{max-width:100%}</style>
"""
    cuerpo = plantilla.replace("__DATOS__", datos).replace('<div class="wrap">', '</head>\n<body>\n<div class="wrap">', 1)
    html = cabeza + cuerpo + "\n</body>\n</html>\n"
    (raiz / "docs" / "index.html").write_text(html, encoding="utf-8")
    (raiz / "docs" / ".nojekyll").write_text("", encoding="utf-8")
    # app instalable: iconos, manifest y service worker (versión = sello de esta generación)
    import shutil
    for f in (raiz / "estaticos").iterdir():
        if f.name == "sw.js":
            version = d["generado"].replace(" ", "-").replace(":", "")
            (raiz / "docs" / "sw.js").write_text(f.read_text(encoding="utf-8").replace("__VERSION__", version),
                                                 encoding="utf-8")
        else:
            shutil.copy(f, raiz / "docs" / f.name)
    print("listo", d["generado"], "->", raiz / "docs" / "index.html", f"({len(html) // 1024} KB)")
