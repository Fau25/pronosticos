"""
Ligas adicionales desde ESPN (sin clave), para pronosticar la Champions con equipos de fuera de las 5 grandes.
No se muestran como ligas en la web: solo alimentan el modelo (nivel de cada equipo) y la clasificación.

Uso:
  python extra_ligas.py                                   -> actualiza ±10 días
  python extra_ligas.py historico 2024-07-01 2026-10-21   -> descarga un rango a datos/extra_historico.json
"""

import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import champions as UCL
from modelo import BD

# código: (slug ESPN, nombre, abreviatura)
EXTRA = {
    "por": ("por.1", "Primeira Liga", "POR"), "ned": ("ned.1", "Eredivisie", "NED"),
    "bel": ("bel.1", "Pro League", "BEL"), "sco": ("sco.1", "Premiership", "ESC"),
    "tur": ("tur.1", "Süper Lig", "TUR"), "aut": ("aut.1", "Bundesliga (AUT)", "AUT"),
    "gre": ("gre.1", "Super League", "GRE"), "den": ("den.1", "Superliga", "DIN"),
    "nor": ("nor.1", "Eliteserien", "NOR"), "swe": ("swe.1", "Allsvenskan", "SUE"),
}
AÑO_NATURAL = {"nor", "swe"}       # ligas que van de primavera a otoño
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard?dates={}"
HISTORICO = Path(__file__).resolve().parent / "datos" / "extra_historico.json"
COLS = ["espn_id", "liga", "fecha_utc", "local", "visitante", "completado", "goles_local", "goles_visitante",
        "goles_local_1t", "goles_visitante_1t", "tiros_local", "tiros_visitante", "remates_local",
        "remates_visitante", "corners_local", "corners_visitante", "amarillas_local", "amarillas_visitante",
        "rojas_local", "rojas_visitante"]


def inicio_temporada(liga, hoy=None):
    hoy = hoy or date.today()
    if liga in AÑO_NATURAL:
        return date(hoy.year, 1, 1)
    return date(hoy.year if hoy.month >= 7 else hoy.year - 1, 7, 1)


def tabla(con):
    con.execute(f"""CREATE TABLE IF NOT EXISTS extra_partidos (
        {', '.join(c + (' TEXT' if c in ('espn_id', 'liga', 'fecha_utc', 'local', 'visitante') else ' INTEGER') for c in COLS)},
        PRIMARY KEY (espn_id))""")


def guardar(con, filas):
    tabla(con)
    for f in filas:
        con.execute(f"INSERT OR REPLACE INTO extra_partidos ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                    [f.get(c) for c in COLS])


def _una_liga(liga, desde, hasta):
    slug = EXTRA[liga][0]
    filas, errores, d = [], 0, desde
    while d <= hasta:
        for intento in range(3):
            try:
                datos = UCL._leer(URL.format(slug, d.strftime("%Y%m%d")))
                for ev in datos.get("events", []):
                    f = UCL._procesar(ev)
                    if f:
                        f["liga"] = liga
                        filas.append(f)
                break
            except Exception:
                time.sleep(2)
                if intento == 2:
                    errores += 1
        time.sleep(0.15)
        d += timedelta(days=1)
    return liga, filas, errores


def descargar(desde, hasta, log=print):
    todas = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for liga, filas, err in ex.map(lambda l: _una_liga(l, desde, hasta), EXTRA):
            log(f"  [extra] {liga}: {len(filas)} partidos ({err} días con error)")
            todas += filas
    return list({f["espn_id"]: f for f in todas}.values())


def actualizar(log=print):
    with sqlite3.connect(BD) as con:
        tabla(con)
        if con.execute("SELECT COUNT(*) FROM extra_partidos").fetchone()[0] == 0 and HISTORICO.exists():
            guardar(con, json.loads(HISTORICO.read_text(encoding="utf-8")))
        hoy = date.today()
        guardar(con, descargar(hoy - timedelta(days=10), hoy + timedelta(days=10), log))
        con.commit()
        n = con.execute("SELECT COUNT(*), SUM(completado) FROM extra_partidos").fetchone()
    log(f"  [extra] en la base: {n[0]} partidos ({n[1] or 0} jugados)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "historico":
        filas = descargar(date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3]))
        HISTORICO.write_text(json.dumps(filas, ensure_ascii=False), encoding="utf-8")
        print("guardado", len(filas))
    else:
        actualizar()
