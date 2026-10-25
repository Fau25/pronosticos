"""
Resultados de las 5 grandes ligas desde ESPN (datos públicos, sin clave), para tener la misma noche
lo que football-data publica con días de retraso: córners, tarjetas, tiros, remates y goles por parte.

- Tabla espn_ligas (mismas columnas que ucl_partidos + liga y nombres emparejados con Understat).
- exportar_web.py los usa SOLO para marcar ✓/✗ mientras football-data no tenga el partido;
  cuando llega football-data, manda football-data. El modelo sigue aprendiendo de football-data.

Uso: python ligas_espn.py            -> últimos 4 días y próximos 2
     python ligas_espn.py 10         -> últimos 10 días
"""
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

import champions as UCL
from comun import avisar
from modelo import BD

SLUG = {"laliga": "esp.1", "premier": "eng.1", "seriea": "ita.1", "bundesliga": "ger.1", "ligue1": "fra.1"}
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard?dates={}"


def _tabla(con):
    con.execute("""CREATE TABLE IF NOT EXISTS espn_ligas (
        espn_id TEXT PRIMARY KEY, liga TEXT, fecha_utc TEXT, local_espn TEXT, visitante_espn TEXT,
        local TEXT, visitante TEXT, completado INTEGER, goles_local INTEGER, goles_visitante INTEGER,
        goles_local_1t INTEGER, goles_visitante_1t INTEGER,
        tiros_local INTEGER, tiros_visitante INTEGER, remates_local INTEGER, remates_visitante INTEGER,
        corners_local INTEGER, corners_visitante INTEGER,
        amarillas_local INTEGER, amarillas_visitante INTEGER, rojas_local INTEGER, rojas_visitante INTEGER,
        actualizado TEXT)""")


def _nombre(con, liga, nombre_espn):
    """Nombre ESPN -> nombre Understat de esa liga (None si no se reconoce)."""
    l, n = UCL.emparejar(con, nombre_espn)
    return n if l == liga else None


def descargar(dias_atras=4, dias_adelante=2, log=print):
    hoy = datetime.now(timezone.utc).date()
    leidos, sin_nombre, errores = 0, set(), 0
    with sqlite3.connect(BD) as con:
        _tabla(con)
        for liga, slug in SLUG.items():
            d = hoy - timedelta(days=dias_atras)
            while d <= hoy + timedelta(days=dias_adelante):
                try:
                    datos = UCL._leer(URL.format(slug, d.strftime("%Y%m%d")))
                    for ev in datos.get("events", []):
                        f = UCL._procesar(ev)
                        if not f:
                            continue
                        loc, vis = _nombre(con, liga, f["local"]), _nombre(con, liga, f["visitante"])
                        if not loc or not vis:
                            sin_nombre.update(x for x, y in ((f["local"], loc), (f["visitante"], vis)) if not y)
                        f.update(liga=liga, local_espn=f.pop("local"), visitante_espn=f.pop("visitante"),
                                 local=loc, visitante=vis, actualizado=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                        f.pop("fase", None)
                        con.execute(f"INSERT OR REPLACE INTO espn_ligas ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                                    list(f.values()))
                        leidos += 1
                except Exception as e:
                    errores += 1
                    log(f"  [espn] {liga} {d}: error ({e})")
                time.sleep(0.3)
                d += timedelta(days=1)
        con.commit()
        resumen = con.execute("""SELECT liga, COUNT(*), SUM(completado), SUM(corners_local IS NOT NULL)
                                 FROM espn_ligas GROUP BY liga""").fetchall()
        ultimos = con.execute("""SELECT fecha_utc, local, visitante, goles_local, goles_visitante, corners_local + corners_visitante,
                                        amarillas_local + amarillas_visitante, tiros_local + tiros_visitante
                                 FROM espn_ligas WHERE completado = 1 ORDER BY fecha_utc DESC LIMIT 5""").fetchall()
    log(f"  [espn] {leidos} partidos leídos ({errores} errores)")
    for l, n, j, c in resumen:
        log(f"  [espn] {l}: {n} en la base, {j or 0} jugados, {c or 0} con córners")
    for u in ultimos:
        log(f"   {u[0][:16]} {u[1]} {u[3]}-{u[4]} {u[2]} · córners {u[5]} · amarillas {u[6]} · tiros {u[7]}")
    if sin_nombre:
        log(f"  [espn] equipos sin emparejar: {', '.join(sorted(sin_nombre))}")
        avisar(f"ESPN: equipos sin emparejar con Understat ({', '.join(sorted(sin_nombre))})")
    if errores >= 5:
        avisar(f"ESPN: {errores} errores al leer las 5 ligas")


def hay_partidos_en_juego(margen_h=3.5):
    """Para la actualización horaria: ¿hay algún partido de nuestras ligas o de la Champions que haya
    empezado y no haya terminado hace más de margen_h horas? (sin base de datos: solo ESPN)."""
    ahora = datetime.now(timezone.utc)
    for slug in list(SLUG.values()) + ["uefa.champions"]:
        for d in {ahora.date(), (ahora - timedelta(hours=6)).date()}:
            try:
                datos = UCL._leer(URL.format(slug, d.strftime("%Y%m%d")))
            except Exception:
                continue
            for ev in datos.get("events", []):
                inicio = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                if inicio <= ahora <= inicio + timedelta(hours=2 + margen_h):
                    return True
    return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "en_juego":
        print("si" if hay_partidos_en_juego() else "no")
    else:
        descargar(int(sys.argv[1]) if len(sys.argv) > 1 else 4)
