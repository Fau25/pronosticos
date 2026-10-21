"""
Motor de selección: de los partidos de un día, elige los más relevantes por liga.

Relevancia (0-100) =
   30 % nivel de los equipos   (puntos por partido de ambos)
   20 % igualdad               (diferencia de nivel pequeña)
   25 % lo que se juegan        (pelea por el título / Europa / descenso)
   15 % forma reciente          (puntos en los últimos 5)
   10 % rivalidad / clásico
"""

import sqlite3
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

MADRID = ZoneInfo("Europe/Madrid")

from modelo import BD

TEMPORADA_ACTUAL = 2026

CLASICOS = {
    "laliga": [("Barcelona", "Real Madrid"), ("Atletico Madrid", "Real Madrid"),
               ("Real Betis", "Sevilla"), ("Athletic Club", "Real Sociedad"),
               ("Barcelona", "Espanyol"), ("Celta Vigo", "Deportivo La Coruna"),
               ("Atletico Madrid", "Barcelona"), ("Valencia", "Villarreal"),
               ("Valencia", "Levante"), ("Real Madrid", "Getafe"), ("Real Madrid", "Rayo Vallecano")],
    "premier": [("Arsenal", "Tottenham"), ("Everton", "Liverpool"),
                ("Manchester City", "Manchester United"), ("Liverpool", "Manchester United"),
                ("Chelsea", "Tottenham"), ("Arsenal", "Chelsea"), ("Newcastle United", "Sunderland"),
                ("Arsenal", "Manchester United"), ("Liverpool", "Manchester City")],
    "bundesliga": [("Bayern Munich", "Borussia Dortmund"), ("Borussia Dortmund", "Schalke 04"),
                   ("Hamburger SV", "Werder Bremen"), ("Borussia M.Gladbach", "FC Cologne"),
                   ("Bayer Leverkusen", "FC Cologne"), ("Bayer Leverkusen", "Bayern Munich")],
    "ligue1": [("Marseille", "Paris Saint Germain"), ("Lyon", "Marseille"),
               ("Lens", "Lille"), ("Monaco", "Nice"), ("Lyon", "Paris Saint Germain"),
               ("Paris FC", "Paris Saint Germain")],
}

NOMBRE_LIGA = {"laliga": "LaLiga", "premier": "Premier League",
               "bundesliga": "Bundesliga", "ligue1": "Ligue 1"}


def clasificacion(con, liga, temporada=TEMPORADA_ACTUAL):
    p = pd.read_sql("""SELECT fecha, local, visitante, goles_local gl, goles_visitante gv
                       FROM us_partidos WHERE liga=? AND temporada=? AND jugado=1
                       ORDER BY fecha""", con, params=(liga, temporada))
    filas = []
    for r in p.itertuples():
        pl = 3 if r.gl > r.gv else 1 if r.gl == r.gv else 0
        pv = 3 if r.gv > r.gl else 1 if r.gl == r.gv else 0
        filas.append((r.fecha, r.local, pl, r.gl, r.gv))
        filas.append((r.fecha, r.visitante, pv, r.gv, r.gl))
    d = pd.DataFrame(filas, columns=["fecha", "equipo", "pts", "gf", "gc"])
    if d.empty:
        return pd.DataFrame(columns=["pos", "equipo", "pj", "pts", "dg", "ppp", "forma", "forma_txt"])
    t = d.groupby("equipo").agg(pj=("pts", "size"), pts=("pts", "sum"),
                                gf=("gf", "sum"), gc=("gc", "sum")).reset_index()
    t["dg"] = t.gf - t.gc
    t["ppp"] = t.pts / t.pj
    ult = d.sort_values("fecha").groupby("equipo").tail(5)
    t = t.merge(ult.groupby("equipo").pts.sum().rename("forma").reset_index(), on="equipo")
    letras = ult.assign(l=ult.pts.map({3: "V", 1: "E", 0: "D"})).groupby("equipo").l.apply("".join)
    t["forma_txt"] = t.equipo.map(letras)
    t = t.sort_values(["pts", "dg", "gf"], ascending=False).reset_index(drop=True)
    t["pos"] = t.index + 1
    return t


def _zona(pos, n):
    """Cuánto se juega un equipo por su posición (0-1)."""
    if pos <= 2:
        return 1.0          # título
    if pos <= 6:
        return 0.75         # Champions / Europa
    if pos > n - 4:
        return 0.9          # descenso (y el que lo roza)
    return 0.3


def partidos_del_dia(con, dia=None):
    """Partidos de un día en hora de España (Understat guarda las horas en UTC)."""
    dia = dia or date.today()
    p = pd.read_sql("""SELECT understat_id, liga, fecha, hora, local, visitante, jugado
                       FROM us_partidos WHERE fecha BETWEEN ? AND ?""",
                    con, params=((dia - timedelta(days=1)).isoformat(),
                                 (dia + timedelta(days=1)).isoformat()))
    if p.empty:
        return p
    utc = pd.to_datetime(p.fecha + " " + p.hora.fillna("00:00"), errors="coerce").dt.tz_localize("UTC")
    madrid = utc.dt.tz_convert(MADRID)
    p["fecha"] = madrid.dt.strftime("%Y-%m-%d")
    p["hora"] = madrid.dt.strftime("%H:%M")
    return p[p.fecha == dia.isoformat()].sort_values("hora").reset_index(drop=True)


def puntuar(con, partidos):
    if partidos.empty:
        return partidos.assign(relevancia=[], motivo=[])
    tablas = {liga: clasificacion(con, liga) for liga in partidos.liga.unique()}
    puntos, motivos = [], []
    for r in partidos.itertuples():
        t = tablas[r.liga].set_index("equipo")
        n = len(t) or 20
        if r.local not in t.index or r.visitante not in t.index:
            puntos.append(0); motivos.append("sin datos de clasificación"); continue
        a, b = t.loc[r.local], t.loc[r.visitante]
        nivel = min((a.ppp + b.ppp) / 2 / 2.5, 1)
        igualdad = max(0, 1 - abs(a.ppp - b.ppp) / 1.5)
        zona = (_zona(a.pos, n) + _zona(b.pos, n)) / 2
        forma = (a.forma + b.forma) / 30
        clasico = 1.0 if tuple(sorted((r.local, r.visitante))) in \
            {tuple(sorted(x)) for x in CLASICOS.get(r.liga, [])} else 0.0
        score = 100 * (0.30 * nivel + 0.20 * igualdad + 0.25 * zona + 0.15 * forma + 0.10 * clasico)
        m = [f"{int(a.pos)}º vs {int(b.pos)}º"]
        if clasico:
            m.append("clásico/derbi")
        if a.pos <= 2 or b.pos <= 2:
            m.append("pelea por el título")
        elif a.pos <= 6 and b.pos <= 6:
            m.append("duelo por Europa")
        if a.pos > n - 4 or b.pos > n - 4:
            m.append("zona de descenso")
        if a.forma >= 12 or b.forma >= 12:
            m.append("equipo en racha")
        puntos.append(round(score, 1)); motivos.append(" · ".join(m))
    return partidos.assign(relevancia=puntos, motivo=motivos)


def seleccionar(dia=None, por_liga=2):
    with sqlite3.connect(BD) as con:
        p = puntuar(con, partidos_del_dia(con, dia))
    if p.empty:
        return p
    return (p.sort_values("relevancia", ascending=False)
             .groupby("liga").head(por_liga)
             .sort_values(["liga", "relevancia"], ascending=[True, False])
             .reset_index(drop=True))


if __name__ == "__main__":
    import sys
    dia = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    print(seleccionar(dia).to_string())
