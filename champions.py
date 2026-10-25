"""
Champions League desde los datos públicos de ESPN (sin clave).

- descargar(): calendario, resultados y estadísticas (tiros, remates, córners, tarjetas, goles por parte)
  -> tabla ucl_partidos
- emparejar(): nombre ESPN -> (liga, nombre del equipo en nuestras ligas)

Fuente no oficial: si ESPN cambia el formato, la Champions deja de actualizarse pero el resto sigue.
"""

import difflib
import json
import re
import sqlite3
import time
import unicodedata
import urllib.request
from datetime import date, datetime, timedelta, timezone

from comun import avisar, temporada
from modelo import BD

URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard?dates={}"
INICIO_TEMPORADA = date(temporada(), 7, 1)  # la fase previa empieza en julio

ALIAS = {
    "internazionale": "Inter", "inter milan": "Inter", "rb leipzig": "RasenBallsport Leipzig",
    "paris saint germain": "Paris Saint Germain", "psg": "Paris Saint Germain",
    "tottenham hotspur": "Tottenham", "brighton hove albion": "Brighton", "tsg hoffenheim": "Hoffenheim",
    "olympique marseille": "Marseille", "olympique lyonnais": "Lyon", "lyon": "Lyon",
    "monaco": "Monaco", "milan": "AC Milan", "napoli": "Napoli", "atletico madrid": "Atletico Madrid",
    "bayern munich": "Bayern Munich", "bayern munchen": "Bayern Munich", "celta vigo": "Celta Vigo",
    "newcastle united": "Newcastle United", "wolverhampton wanderers": "Wolverhampton Wanderers",
    "athletic club": "Athletic Club", "athletic bilbao": "Athletic Club",
    "borussia monchengladbach": "Borussia M.Gladbach", "koln": "FC Cologne", "mainz": "Mainz 05",
    "stuttgart": "VfB Stuttgart", "rennais": "Rennes", "stade rennais": "Rennes", "hamburg": "Hamburger SV", "werder bremen": "Werder Bremen",
}
QUITAR = {"fc", "cf", "ac", "sc", "afc", "ssc", "as", "rc", "cd", "ud", "sv", "vfl", "club", "de", "the",
          "calcio", "1", "04", "05", "1899", "1846", "1909", "1913", "ogc", "losc", "stade", "olympique"}


def _norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " "))
    return " ".join(w for w in s.split() if w not in QUITAR)


_CACHE_EQ = {}


def _equipos_ligas(con, todas=False):
    """{nombre normalizado: (liga, nombre)} con los equipos de la temporada en curso (o de todas)."""
    clave = (id(con), todas)
    if clave not in _CACHE_EQ:
        filtro = "" if todas else "WHERE temporada = (SELECT MAX(temporada) FROM us_partidos)"
        filas = con.execute(f"SELECT liga, local, MAX(temporada) FROM us_partidos {filtro} "
                            "GROUP BY liga, local ORDER BY 3").fetchall()
        eqs = {_norm(n): (l, n) for l, n, _ in filas}
        # ligas adicionales (ESPN): los nombres ya coinciden con los de la Champions
        try:
            filtro_x = "" if todas else "WHERE fecha_utc >= date('now', '-400 days')"
            for l, n in con.execute(f"SELECT DISTINCT liga, local FROM extra_partidos {filtro_x}"):
                eqs.setdefault(_norm(n), (l, n))
        except sqlite3.Error:
            pass
        _CACHE_EQ[clave] = eqs
    return _CACHE_EQ[clave]


def emparejar(con, nombre_espn, todas=False):
    eqs = _equipos_ligas(con, todas)
    por_nombre = {n: (l, n) for l, n in eqs.values()}
    clave = _norm(nombre_espn)
    alias = ALIAS.get(clave) or ALIAS.get(_norm(nombre_espn.replace("-", " ")))
    if alias and alias in por_nombre:
        return por_nombre[alias]
    if clave in eqs:
        return eqs[clave]
    # coincidencia por palabras completas (p. ej. "tottenham hotspur" contiene "tottenham");
    # nada de parecidos aproximados: "rangers" no debe ser "angers"
    palabras = set(clave.split())
    cand = [k for k in eqs if k and (set(k.split()) <= palabras or palabras <= set(k.split()))]
    if len(cand) == 1:
        return eqs[cand[0]]
    m = difflib.get_close_matches(clave, list(eqs), n=1, cutoff=0.93)
    return eqs[m[0]] if m else (None, None)


# ------------------------------------------------------------------ descarga

def _leer(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pronosticos personales)",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _minuto(det):
    txt = (det.get("clock") or {}).get("displayValue") or ""
    m = re.match(r"(\d+)'(\+(\d+))?", txt)
    if not m:
        return None, None
    base = int(m.group(1))
    return base, 1 if base <= 45 else 2


def _tabla(con):
    con.execute("""CREATE TABLE IF NOT EXISTS ucl_partidos (
        espn_id TEXT PRIMARY KEY, fecha_utc TEXT, fase TEXT, local TEXT, visitante TEXT,
        completado INTEGER, goles_local INTEGER, goles_visitante INTEGER,
        goles_local_1t INTEGER, goles_visitante_1t INTEGER,
        tiros_local INTEGER, tiros_visitante INTEGER, remates_local INTEGER, remates_visitante INTEGER,
        corners_local INTEGER, corners_visitante INTEGER,
        amarillas_local INTEGER, amarillas_visitante INTEGER, rojas_local INTEGER, rojas_visitante INTEGER,
        actualizado TEXT)""")


def _procesar(ev):
    comp = ev["competitions"][0]
    eq = {c["homeAway"]: c for c in comp["competitors"]}
    if "home" not in eq or "away" not in eq:
        return None
    h, a = eq["home"], eq["away"]
    estado = ev.get("status", {}).get("type", {})
    fila = dict(espn_id=str(ev["id"]), fecha_utc=ev["date"], fase=(ev.get("season") or {}).get("slug", ""),
                local=h["team"]["displayName"], visitante=a["team"]["displayName"],
                completado=1 if estado.get("completed") else 0)
    if not fila["completado"]:
        return fila
    fila["goles_local"], fila["goles_visitante"] = int(h.get("score") or 0), int(a.get("score") or 0)
    st = {lado: {s["name"]: s.get("displayValue") for s in c.get("statistics", [])} for lado, c in eq.items()}

    def num(lado, nombre):
        v = st[lado].get(nombre)
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    for lado, suf in (("home", "local"), ("away", "visitante")):
        fila[f"tiros_{suf}"] = num(lado, "totalShots")
        fila[f"remates_{suf}"] = num(lado, "shotsOnTarget")
        fila[f"corners_{suf}"] = num(lado, "wonCorners")
    ids = {h["team"]["id"]: "local", a["team"]["id"]: "visitante"}
    g1 = {"local": 0, "visitante": 0}
    tarj = {"amarillas_local": 0, "amarillas_visitante": 0, "rojas_local": 0, "rojas_visitante": 0}
    detalles = comp.get("details") or []
    goles_det = 0
    for d in detalles:
        lado = ids.get((d.get("team") or {}).get("id"))
        if not lado or d.get("shootout"):
            continue
        _, parte = _minuto(d)
        if d.get("scoringPlay"):
            goles_det += 1
            if parte == 1:
                g1[lado] += 1
        if d.get("yellowCard"):
            tarj[f"amarillas_{lado}"] += 1
        if d.get("redCard"):
            tarj[f"rojas_{lado}"] += 1
    if detalles:
        fila.update(tarj)
        if goles_det == fila["goles_local"] + fila["goles_visitante"]:
            fila["goles_local_1t"], fila["goles_visitante_1t"] = g1["local"], g1["visitante"]
    return fila


def descargar(desde, hasta, log=print):
    with sqlite3.connect(BD) as con:
        _tabla(con)
        n_ok = n_err = 0
        d = desde
        while d <= hasta:
            try:
                datos = _leer(URL.format(d.strftime("%Y%m%d")))
                for ev in datos.get("events", []):
                    f = _procesar(ev)
                    if not f:
                        continue
                    f["actualizado"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    cols = ", ".join(f)
                    con.execute(f"INSERT OR REPLACE INTO ucl_partidos ({cols}) VALUES ({', '.join('?' * len(f))})",
                                list(f.values()))
                    n_ok += 1
            except Exception as e:
                n_err += 1
                log(f"  [champions] {d}: error ({e})")
                if n_err >= 5:
                    log("  [champions] demasiados errores, paro")
                    avisar("ESPN: no se pudo leer la Champions")
                    break
            time.sleep(0.4)
            d += timedelta(days=1)
        con.commit()
        total = con.execute("SELECT COUNT(*), SUM(completado) FROM ucl_partidos").fetchone()
    log(f"  [champions] {n_ok} partidos leídos · en la base: {total[0]} ({total[1] or 0} jugados)")


def actualizar(log=print):
    with sqlite3.connect(BD) as con:
        _tabla(con)
        vacia = con.execute("SELECT COUNT(*) FROM ucl_partidos").fetchone()[0] == 0
    hoy = date.today()
    desde = INICIO_TEMPORADA if vacia else hoy - timedelta(days=10)
    descargar(desde, hoy + timedelta(days=10), log)


if __name__ == "__main__":
    actualizar()
