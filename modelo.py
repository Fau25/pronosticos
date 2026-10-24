"""
Motor analítico y predictivo.

- unir_fuentes(): junta football-data (córners, tarjetas, cuotas) con Understat
  (tiros, remates y xG por parte) en una sola tabla de partidos.
- Modelo: para cada métrica, cada equipo tiene un índice de ataque ("lo que genera")
  y de defensa ("lo que concede"), relativos a la media de su liga y actualizados
  partido a partido dando más peso a lo reciente. Valor esperado:
      esperado_local = media_liga_local * ataque_local * defensa_visitante
  y lo mismo para el visitante. Con eso y la dispersión observada de cada métrica
  se calcula la probabilidad de superar cualquier línea (más/menos de X).
"""

import math
import sqlite3
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

BD = Path(__file__).resolve().parent / "pronosticos.db"

# métrica: (columna local, columna visitante, tipo de distribución)
METRICAS = {
    "goles":      ("goles_local", "goles_visitante", "poisson"),
    "goles_1t":   ("goles_local_1t", "goles_visitante_1t", "poisson"),
    "goles_2t":   ("goles_local_2t", "goles_visitante_2t", "poisson"),
    "xg":         ("xg_local", "xg_visitante", "normal"),
    "xg_1t":      ("xg_local_1t", "xg_visitante_1t", "normal"),
    "xg_2t":      ("xg_local_2t", "xg_visitante_2t", "normal"),
    "tiros":      ("tiros_local", "tiros_visitante", "negbin"),
    "tiros_1t":   ("tiros_local_1t", "tiros_visitante_1t", "negbin"),
    "tiros_2t":   ("tiros_local_2t", "tiros_visitante_2t", "negbin"),
    "remates":    ("remates_local", "remates_visitante", "negbin"),
    "remates_1t": ("remates_local_1t", "remates_visitante_1t", "negbin"),
    "remates_2t": ("remates_local_2t", "remates_visitante_2t", "negbin"),
    "corners":    ("corners_local", "corners_visitante", "negbin"),
    "tarjetas":   ("tarjetas_local", "tarjetas_visitante", "negbin"),
}

NOMBRES = {
    "goles": "Goles", "goles_1t": "Goles 1ª parte", "goles_2t": "Goles 2ª parte",
    "xg": "xG", "xg_1t": "xG 1ª parte", "xg_2t": "xG 2ª parte",
    "tiros": "Tiros", "tiros_1t": "Tiros 1ª parte", "tiros_2t": "Tiros 2ª parte",
    "remates": "Remates a puerta", "remates_1t": "Remates 1ª parte",
    "remates_2t": "Remates 2ª parte",
    "corners": "Córners", "tarjetas": "Tarjetas",
}

# Nivel relativo aproximado de cada liga (solo para partidos entre ligas, p. ej. Champions)
FUERZA = {"premier": 1.08, "laliga": 1.05, "seriea": 1.03, "bundesliga": 1.02, "ligue1": 0.95}
# si existe la medición real (medir_ligas.py, con los cruces europeos), sustituye a los valores a ojo
try:
    import json as _json
    _f = Path(__file__).resolve().parent / "datos" / "fuerza_ligas.json"
    if _f.exists():
        FUERZA.update(_json.loads(_f.read_text(encoding="utf-8")).get("fuerza_final", {}))
except Exception:
    pass

# Ajustes de contexto (se activan solo si mejoran el backtest)
USAR = {"arbitro": False, "cansancio": False}   # probados en oct-2026: ninguno de los dos mejora el backtest
DECAY_ARB, PRIOR_ARB = 0.97, 6.0   # memoria y prudencia del índice de cada árbitro

DECAY = 0.92       # peso de cada partido anterior (más bajo = más importancia a lo reciente)
PRIOR = 4.0        # partidos "ficticios" en la media de la liga para equipos con poco historial
VENTANA_LIGA = 300  # partidos para calcular la media de la liga


# ------------------------------------------------------------------ datos

def unir_fuentes(con=None):
    con = con or sqlite3.connect(BD)
    fd = pd.read_sql("SELECT * FROM partidos", con)
    us = pd.read_sql("SELECT * FROM us_estadisticas", con)
    fd["fecha"] = pd.to_datetime(fd["fecha"])
    us["fecha"] = pd.to_datetime(us["fecha"])

    # 1) emparejar por liga + fecha (±1 día por husos horarios) + marcador
    cand = fd.merge(us, on="liga", suffixes=("_fd", "_us"))
    cand = cand[(cand.fecha_fd - cand.fecha_us).abs() <= pd.Timedelta(days=1)]
    cand = cand[(cand.goles_local_fd == cand.goles_local_us) &
                (cand.goles_visitante_fd == cand.goles_visitante_us)]
    # 2) diccionario de nombres por votación (el nombre que más se repite)
    votos = pd.concat([
        cand[["liga", "local_fd", "local_us"]].set_axis(["liga", "fd", "us"], axis=1),
        cand[["liga", "visitante_fd", "visitante_us"]].set_axis(["liga", "fd", "us"], axis=1),
    ])
    mapa = (votos.groupby(["liga", "fd", "us"]).size().reset_index(name="n")
            .sort_values("n", ascending=False).drop_duplicates(["liga", "fd"]))
    mapa = mapa.drop_duplicates(["liga", "us"])
    nombre = {(r.liga, r.fd): r.us for r in mapa.itertuples()}
    fd["local"] = [nombre.get((l, n), n) for l, n in zip(fd.liga, fd.local)]
    fd["visitante"] = [nombre.get((l, n), n) for l, n in zip(fd.liga, fd.visitante)]

    # 3) unir
    us_cols = ["liga", "local", "visitante", "fecha", "understat_id",
               "xg_local", "xg_visitante",
               "tiros_local_1t", "tiros_visitante_1t", "tiros_local_2t", "tiros_visitante_2t",
               "remates_puerta_local", "remates_puerta_visitante",
               "remates_puerta_local_1t", "remates_puerta_visitante_1t",
               "remates_puerta_local_2t", "remates_puerta_visitante_2t",
               "xg_local_1t", "xg_visitante_1t", "xg_local_2t", "xg_visitante_2t"]
    fd["_fila_fd"] = range(len(fd))
    m = fd.merge(us[us_cols].rename(columns={"fecha": "fecha_us"}),
                 on=["liga", "local", "visitante"], how="left")
    m = m[m.fecha_us.isna() | ((m.fecha - m.fecha_us).abs() <= pd.Timedelta(days=1))]
    # partidos que Understat aún no ha cerrado pero se cruzaron con un enfrentamiento anterior
    # de los mismos equipos (y por eso se descartaron arriba): se conservan sin datos de Understat
    faltan = fd[~fd._fila_fd.isin(m._fila_fd)]
    if not faltan.empty:
        m = pd.concat([m, faltan], ignore_index=True)
    m = m.drop(columns="_fila_fd").drop_duplicates(["liga", "fecha", "local", "visitante"])

    # Tiros: los totales de football-data (son los que usan las casas);
    # por partes y remates: Understat
    m = m.rename(columns={
        "remates_puerta_local": "remates_local", "remates_puerta_visitante": "remates_visitante",
        "remates_puerta_local_1t": "remates_local_1t", "remates_puerta_visitante_1t": "remates_visitante_1t",
        "remates_puerta_local_2t": "remates_local_2t", "remates_puerta_visitante_2t": "remates_visitante_2t",
    })
    # remates totales: football-data (fuente oficial de liquidación)
    m["remates_local"] = m["tiros_puerta_local"]
    m["remates_visitante"] = m["tiros_puerta_visitante"]
    m["goles_local_2t"] = m.goles_local - m.goles_local_1t
    m["goles_visitante_2t"] = m.goles_visitante - m.goles_visitante_1t
    m["tarjetas_local"] = m.amarillas_local + m.rojas_local
    m["tarjetas_visitante"] = m.amarillas_visitante + m.rojas_visitante
    ext = partidos_extra(con)
    if not ext.empty:
        m = pd.concat([m, ext], ignore_index=True)
    m = m.sort_values(["fecha", "liga"]).reset_index(drop=True)
    return m, nombre


def partidos_extra(con=None):
    """Partidos jugados de las ligas adicionales (ESPN) con las mismas columnas que el resto.
    No tienen xG ni tiros por partes: esas métricas quedan vacías y el modelo las ignora."""
    import json
    con = con or sqlite3.connect(BD)
    try:
        e = pd.read_sql("SELECT * FROM extra_partidos WHERE completado = 1", con)
    except Exception:
        f = Path(__file__).resolve().parent / "datos" / "extra_historico.json"
        if not f.exists():
            return pd.DataFrame()
        e = pd.DataFrame([x for x in json.loads(f.read_text(encoding="utf-8")) if x.get("completado")])
    if e.empty:
        return e
    e = e.dropna(subset=["goles_local", "goles_visitante"]).copy()
    e["fecha"] = pd.to_datetime(e.fecha_utc, utc=True).dt.tz_convert("Europe/Madrid").dt.normalize().dt.tz_localize(None)
    out = pd.DataFrame({
        "fecha": e.fecha, "liga": e.liga, "local": e.local, "visitante": e.visitante,
        "goles_local": e.goles_local, "goles_visitante": e.goles_visitante,
        "goles_local_1t": e.get("goles_local_1t"), "goles_visitante_1t": e.get("goles_visitante_1t"),
        "tiros_local": e.tiros_local, "tiros_visitante": e.tiros_visitante,
        "remates_local": e.remates_local, "remates_visitante": e.remates_visitante,
        "tiros_puerta_local": e.remates_local, "tiros_puerta_visitante": e.remates_visitante,
        "corners_local": e.corners_local, "corners_visitante": e.corners_visitante,
    })
    out["goles_local_2t"] = out.goles_local - out.goles_local_1t
    out["goles_visitante_2t"] = out.goles_visitante - out.goles_visitante_1t
    tiene_tarj = e.amarillas_local.notna()
    out["tarjetas_local"] = (e.amarillas_local + e.rojas_local.fillna(0)).where(tiene_tarj)
    out["tarjetas_visitante"] = (e.amarillas_visitante + e.rojas_visitante.fillna(0)).where(tiene_tarj)
    return out


def partidos_europeos(con=None):
    """Partidos europeos (Champions/Europa/Conference) de equipos de nuestras ligas: fecha (España), liga, equipo."""
    import json
    import champions as UCL
    con = con or sqlite3.connect(BD)
    filas = []
    try:
        filas += [tuple(r) for r in con.execute("SELECT fecha_utc, local, visitante FROM europa_partidos")]
    except sqlite3.Error:
        pass
    hist = Path(__file__).resolve().parent / "datos" / "europa_historico.json"
    if not filas and hist.exists():
        filas = [(x["fecha_utc"], x["local"], x["visitante"]) for x in json.loads(hist.read_text(encoding="utf-8"))]
    try:
        filas += [tuple(r) for r in con.execute("SELECT fecha_utc, local, visitante FROM ucl_partidos")]
    except sqlite3.Error:
        pass
    out = []
    for f, a, b in filas:
        dia = pd.Timestamp(f).tz_convert("Europe/Madrid").normalize().tz_localize(None)
        for n in (a, b):
            liga, eq = UCL.emparejar(con, n, todas=True)
            if eq:
                out.append((dia, liga, eq))
    return pd.DataFrame(out, columns=["fecha", "liga", "equipo"]).drop_duplicates()


def marcar_europa(df, eu, dias=4):
    """Añade europa_local / europa_visitante: el equipo jugó en Europa en los 'dias' anteriores."""
    if eu is None or eu.empty:
        return df.assign(europa_local=False, europa_visitante=False)
    por_eq = eu.groupby(["liga", "equipo"]).fecha.apply(lambda s: sorted(s)).to_dict()

    def jugo(liga, eq, fecha):
        for f in por_eq.get((liga, eq), []):
            if pd.Timedelta(days=1) <= fecha - f <= pd.Timedelta(days=dias):
                return True
        return False
    df = df.copy()
    df["europa_local"] = [jugo(l, e, f) for l, e, f in zip(df.liga, df.local, df.fecha)]
    df["europa_visitante"] = [jugo(l, e, f) for l, e, f in zip(df.liga, df.visitante, df.fecha)]
    return df


# ------------------------------------------------------------------ modelo

class Modelo:
    """Índices de ataque/defensa por equipo y métrica, actualizados partido a partido."""

    def __init__(self):
        # [liga][metrica]['h'/'a'] -> deque de valores
        self.liga = defaultdict(lambda: defaultdict(lambda: {"h": deque(maxlen=VENTANA_LIGA),
                                                             "a": deque(maxlen=VENTANA_LIGA)}))
        # [liga, equipo][metrica] -> [suma_pesos_ataque, suma_ataque, suma_pesos_def, suma_def]
        self.eq = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]))
        self.dispersion = {}      # métrica -> varianza/media (o desviación típica para xG)
        self.partidos = defaultdict(int)  # partidos vistos por equipo
        self.platt = {}           # métrica -> (a, b) corrección de exceso de confianza
        self.arb = defaultdict(lambda: [0.0, 0.0])   # (liga, árbitro) -> [pesos, suma de ratios de tarjetas]
        self.ajuste_cansancio = {}                   # métrica -> (factor propio, factor del rival) tras jugar en Europa

    def media_liga(self, liga, met, lado):
        v = self.liga[liga][met][lado]
        return float(np.mean(v)) if len(v) >= 20 else None

    def _indice(self, liga, eq, met, cual):
        sw_a, s_a, sw_d, s_d = self.eq[(liga, eq)][met]
        if cual == "ataque":
            return (s_a + PRIOR) / (sw_a + PRIOR)
        return (s_d + PRIOR) / (sw_d + PRIOR)

    def factor_arbitro(self, liga, arbitro):
        w, s = self.arb[(liga, arbitro)]
        return (s + PRIOR_ARB) / (w + PRIOR_ARB)

    def esperado(self, liga, local, visitante, met, liga_v=None, ctx=None):
        """Valor esperado (local, visitante). Con liga_v distinta: partido entre ligas (Champions),
        se usa la media de ambas ligas y un ajuste aproximado por nivel de cada liga."""
        liga_v = liga_v or liga
        lh, la = self.media_liga(liga, met, "h"), self.media_liga(liga, met, "a")
        if liga_v != liga:
            vh, va = self.media_liga(liga_v, met, "h"), self.media_liga(liga_v, met, "a")
            if None in (lh, la, vh, va):
                return None
            lh, la = (lh + vh) / 2, (la + va) / 2
        if lh is None or la is None:
            return None
        f = 1.0 if (liga_v == liga or met == "tarjetas") else FUERZA.get(liga, 1) / FUERZA.get(liga_v, 1)
        el = lh * self._indice(liga, local, met, "ataque") * self._indice(liga_v, visitante, met, "defensa") * f
        ev = la * self._indice(liga_v, visitante, met, "ataque") * self._indice(liga, local, met, "defensa") / f
        if ctx:
            if met == "tarjetas" and USAR["arbitro"] and ctx.get("arbitro"):
                k = self.factor_arbitro(liga, ctx["arbitro"])
                el, ev = el * k, ev * k
            if USAR["cansancio"] and met in self.ajuste_cansancio:
                propio, rival = self.ajuste_cansancio[met]
                if ctx.get("europa_local"):
                    el, ev = el * propio, ev * rival
                if ctx.get("europa_visitante"):
                    ev, el = ev * propio, el * rival
        return el, ev

    def actualizar(self, fila):
        liga, loc, vis = fila["liga"], fila["local"], fila["visitante"]
        arb = fila.get("arbitro")
        tl, tv = fila.get("tarjetas_local"), fila.get("tarjetas_visitante")
        if arb and isinstance(arb, str) and tl is not None and tv is not None and not (pd.isna(tl) or pd.isna(tv)):
            mh, ma = self.media_liga(liga, "tarjetas", "h"), self.media_liga(liga, "tarjetas", "a")
            if mh and ma:
                e = self.arb[(liga, arb.strip())]
                e[0] = e[0] * DECAY_ARB + 1
                e[1] = e[1] * DECAY_ARB + (float(tl) + float(tv)) / (mh + ma)
        for met, (cl, cv, _) in METRICAS.items():
            yl, yv = fila.get(cl), fila.get(cv)
            if yl is None or yv is None or pd.isna(yl) or pd.isna(yv):
                continue
            lh, la = self.media_liga(liga, met, "h"), self.media_liga(liga, met, "a")
            if lh and la and lh > 0 and la > 0:
                for eq, ataque, defensa in ((loc, yl / lh, yv / la), (vis, yv / la, yl / lh)):
                    e = self.eq[(liga, eq)][met]
                    e[0] = e[0] * DECAY + 1; e[1] = e[1] * DECAY + ataque
                    e[2] = e[2] * DECAY + 1; e[3] = e[3] * DECAY + defensa
            self.liga[liga][met]["h"].append(float(yl))
            self.liga[liga][met]["a"].append(float(yv))
        self.partidos[(liga, loc)] += 1
        self.partidos[(liga, vis)] += 1

    # ---------- probabilidades

    def prob_mas(self, met, media, linea):
        """P(valor > linea) para una línea .5"""
        tipo = METRICAS[met][2]
        if tipo == "poisson":
            return float(1 - stats.poisson.cdf(math.floor(linea), media))
        if tipo == "negbin":
            phi = max(self.dispersion.get(met, 1.0), 1.01)
            n = media / (phi - 1)          # varianza = phi * media
            p = n / (n + media)
            return float(1 - stats.nbinom.cdf(math.floor(linea), n, p))
        sd = self.dispersion.get(met, 0.8) * math.sqrt(max(media, 0.05))
        return float(1 - stats.norm.cdf(linea, media, sd))


    def prob(self, met, media, linea):
        """P(más de la línea) ya corregida para que '75 %' signifique de verdad 75 %."""
        p = min(max(self.prob_mas(met, media, linea), 1e-4), 1 - 1e-4)
        if met in self.platt:
            a, b = self.platt[met]
            z = a + b * math.log(p / (1 - p))
            p = 1 / (1 + math.exp(-z))
        return p

    def linea_segura(self, met, media, objetivo=0.80):
        """La línea más exigente en la que el modelo da >= objetivo de probabilidad.
        Usa la probabilidad sin corregir: en el backtest es la que mejor acierta en estas líneas
        (dice ~83 % y acierta ~84 %)."""
        mejor = None
        for l in np.arange(0.5, 60, 1.0):
            p = self.prob_mas(met, media, l)
            for prob, lado in ((p, "más"), (1 - p, "menos")):
                if prob >= objetivo and (mejor is None or prob < mejor[0]):
                    mejor = (prob, lado, float(l))
        return mejor

    def pronosticar(self, liga, local, visitante, liga_v=None, ctx=None):
        """Pronóstico completo de un partido -> lista de dicts, una por métrica."""
        salida = []
        for met in METRICAS:
            e = self.esperado(liga, local, visitante, met, liga_v, ctx)
            if e is None:
                continue
            total = e[0] + e[1]
            linea = getattr(self, "lineas", {}).get(met)
            fila = dict(metrica=met, nombre=NOMBRES[met],
                        esperado_local=round(e[0], 2), esperado_visitante=round(e[1], 2),
                        esperado_total=round(total, 2))
            if linea is not None:
                p = self.prob(met, total, linea)
                fila.update(linea=linea, eleccion="más" if p >= 0.5 else "menos",
                            prob=round(max(p, 1 - p), 3))
            seg = self.linea_segura(met, total)
            if seg:
                fila.update(segura_lado=seg[1], segura_linea=seg[2], segura_prob=round(seg[0], 3))
            salida.append(fila)
        return salida


def entrenar():
    """Entrena el modelo con todo el histórico (misma receta que el backtest)."""
    import backtest
    df, nombres = unir_fuentes()
    _, mod, _ = backtest.ejecutar(df)
    mod.nombres = nombres      # nombre football-data -> nombre Understat
    return mod, df


def elegir_linea(valores):
    """La línea .5 que deja más cerca del 50 % los partidos por encima/debajo."""
    v = np.asarray(valores, dtype=float)
    candidatas = np.arange(math.floor(v.min()) + 0.5, math.ceil(v.max()) + 0.5, 0.5 if v.max() < 6 else 1.0)
    candidatas = [c for c in candidatas if c % 1 == 0.5]
    return float(min(candidatas, key=lambda c: abs((v > c).mean() - 0.5)))


def calibrar_platt(modelo, reales, lineas):
    """Ajusta, por métrica, una corrección logística sobre el periodo de calentamiento."""
    from scipy.optimize import minimize
    df = pd.DataFrame(reales, columns=["met", "mu", "y"])
    for met, g in df.groupby("met"):
        l = lineas[met]
        p = np.clip([modelo.prob_mas(met, mu, l) for mu in g.mu], 1e-4, 1 - 1e-4)
        x = np.log(p / (1 - p))
        y = (g.y.values > l).astype(float)

        def perdida(ab):
            z = ab[0] + ab[1] * x
            q = 1 / (1 + np.exp(-z))
            q = np.clip(q, 1e-6, 1 - 1e-6)
            return -np.mean(y * np.log(q) + (1 - y) * np.log(1 - q))
        modelo.platt[met] = tuple(minimize(perdida, [0.0, 1.0]).x)


def calibrar_dispersion(modelo, reales):
    """reales: lista de (met, esperado_total, valor_real) -> fija dispersión por métrica."""
    df = pd.DataFrame(reales, columns=["met", "mu", "y"])
    for met, g in df.groupby("met"):
        tipo = METRICAS[met][2]
        if tipo == "negbin":
            modelo.dispersion[met] = float(((g.y - g.mu) ** 2).sum() / g.mu.sum())
        elif tipo == "normal":
            modelo.dispersion[met] = float(np.sqrt(((g.y - g.mu) ** 2 / g.mu.clip(lower=0.05)).mean()))
