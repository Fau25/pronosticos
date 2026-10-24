"""
Partidos europeos (Champions, Europa League, Conference) desde los datos públicos de ESPN.

Sirve para:
  - saber qué equipos jugaron en Europa entre semana (cansancio / rotaciones)
  - medir el nivel de cada liga con los cruces entre ligas

Uso:
  python europa.py                          -> actualiza los últimos/próximos 10 días
  python europa.py historico 2024-07-01 2026-10-20  -> descarga un rango y lo guarda en datos/europa_historico.json
"""

import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import champions as UCL
from modelo import BD

COMPETICIONES = {"ucl": "uefa.champions", "uel": "uefa.europa", "uecl": "uefa.europa.conf"}
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard?dates={}"
HISTORICO = Path(__file__).resolve().parent / "datos" / "europa_historico.json"


def _tabla(con):
    con.execute("""CREATE TABLE IF NOT EXISTS europa_partidos (
        espn_id TEXT, comp TEXT, fecha_utc TEXT, local TEXT, visitante TEXT, completado INTEGER,
        goles_local INTEGER, goles_visitante INTEGER, tiros_local INTEGER, tiros_visitante INTEGER,
        remates_local INTEGER, remates_visitante INTEGER, corners_local INTEGER, corners_visitante INTEGER,
        amarillas_local INTEGER, amarillas_visitante INTEGER, rojas_local INTEGER, rojas_visitante INTEGER,
        PRIMARY KEY (espn_id, comp))""")


COLS = ["espn_id", "comp", "fecha_utc", "local", "visitante", "completado", "goles_local", "goles_visitante",
        "tiros_local", "tiros_visitante", "remates_local", "remates_visitante", "corners_local",
        "corners_visitante", "amarillas_local", "amarillas_visitante", "rojas_local", "rojas_visitante"]


def _guardar(con, filas):
    for f in filas:
        con.execute(f"INSERT OR REPLACE INTO europa_partidos ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                    [f.get(c) for c in COLS])


def descargar(desde, hasta, log=print):
    filas, errores = [], 0
    for comp, slug in COMPETICIONES.items():
        d = desde
        while d <= hasta:
            try:
                datos = UCL._leer(URL.format(slug, d.strftime("%Y%m%d")))
                for ev in datos.get("events", []):
                    f = UCL._procesar(ev)
                    if f:
                        f["comp"] = comp
                        filas.append(f)
            except Exception as e:
                errores += 1
                if errores <= 5:
                    log(f"  [europa] {comp} {d}: error ({e})")
            time.sleep(0.25)
            d += timedelta(days=1)
    vistos = {}
    for f in filas:
        vistos[(f["espn_id"], f["comp"])] = f
    filas = list(vistos.values())
    log(f"  [europa] {len(filas)} partidos ({errores} errores)")
    if errores >= 5:
        from comun import avisar
        avisar(f"ESPN: {errores} errores al leer las competiciones europeas")
    return filas


def cargar_historico(con):
    if HISTORICO.exists():
        _guardar(con, json.loads(HISTORICO.read_text(encoding="utf-8")))


def actualizar(log=print):
    with sqlite3.connect(BD) as con:
        _tabla(con)
        if con.execute("SELECT COUNT(*) FROM europa_partidos").fetchone()[0] == 0:
            cargar_historico(con)
        hoy = date.today()
        _guardar(con, descargar(hoy - timedelta(days=10), hoy + timedelta(days=10), log))
        con.commit()
        n = con.execute("SELECT comp, COUNT(*) FROM europa_partidos GROUP BY comp").fetchall()
    log(f"  [europa] en la base: {dict(n)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "historico":
        a, b = date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
        filas = descargar(a, b)
        HISTORICO.parent.mkdir(exist_ok=True)
        HISTORICO.write_text(json.dumps(filas, ensure_ascii=False), encoding="utf-8")
        print("guardado", HISTORICO, len(filas))
    else:
        actualizar()
