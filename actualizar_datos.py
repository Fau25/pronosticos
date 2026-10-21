"""
Actualiza la base de datos pronosticos.db con:

  1) football-data.co.uk  -> resultados, tiros, remates, córners, tarjetas y cuotas
                             de LaLiga, Bundesliga, Premier y Ligue 1 (tabla 'partidos')
                             + próximos partidos con cuotas (tabla 'proximos')
  2) Understat (Scrapling) -> partidos con xG (tabla 'us_partidos')
                             + cada tiro con su minuto, xG y resultado (tabla 'us_tiros')

Es INCREMENTAL: lo ya descargado de Understat no se vuelve a pedir.
Se puede cerrar a mitad y al volver a ejecutarlo sigue donde lo dejó.

Uso:
    python actualizar_datos.py            -> todo
    python actualizar_datos.py laliga     -> solo LaLiga
    python actualizar_datos.py rapido     -> solo la temporada en curso (lo usa el botón de la app)
"""

import codecs
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from scrapling.fetchers import Fetcher

CARPETA = Path(__file__).resolve().parent
BD = CARPETA / "pronosticos.db"
REGISTRO = CARPETA / "registro_actualizacion.txt"

# código football-data, nombre Understat
LIGAS = {
    "laliga":     ("SP1", "La_liga"),
    "premier":    ("E0",  "EPL"),
    "bundesliga": ("D1",  "Bundesliga"),
    "ligue1":     ("F1",  "Ligue_1"),
    "seriea":     ("I1",  "Serie_A"),
}
# temporada football-data, temporada Understat (año de inicio)
TEMPORADAS = [("2425", 2024), ("2526", 2025), ("2627", 2026)]

PAUSA = 1.0  # segundos entre peticiones a Understat, para no cargar su servidor

COLUMNAS_FD = {
    "Date": "fecha", "Time": "hora",
    "HomeTeam": "local", "AwayTeam": "visitante",
    "FTHG": "goles_local", "FTAG": "goles_visitante", "FTR": "resultado",
    "HTHG": "goles_local_1t", "HTAG": "goles_visitante_1t",
    "HS": "tiros_local", "AS": "tiros_visitante",
    "HST": "tiros_puerta_local", "AST": "tiros_puerta_visitante",
    "HC": "corners_local", "AC": "corners_visitante",
    "HY": "amarillas_local", "AY": "amarillas_visitante",
    "HR": "rojas_local", "AR": "rojas_visitante",
    "Referee": "arbitro",
    "B365H": "b365_1", "B365D": "b365_x", "B365A": "b365_2",
    "AvgH": "media_1", "AvgD": "media_x", "AvgA": "media_2",
    "MaxH": "max_1", "MaxD": "max_x", "MaxA": "max_2",
    "B365>2.5": "b365_mas25", "B365<2.5": "b365_menos25",
    "Avg>2.5": "media_mas25", "Avg<2.5": "media_menos25",
}

_log_file = open(REGISTRO, "w", encoding="utf-8")


def log(msg=""):
    print(msg, flush=True)
    _log_file.write(msg + "\n")
    _log_file.flush()


# ---------------------------------------------------------------- football-data

def descargar_football_data(ligas, temporadas):
    tablas = []
    for nombre in ligas:
        codigo = LIGAS[nombre][0]
        for temp_fd, _ in temporadas:
            url = f"https://www.football-data.co.uk/mmz4281/{temp_fd}/{codigo}.csv"
            try:
                df = pd.read_csv(url, encoding="latin-1")
            except Exception as e:
                log(f"  [football-data] {nombre} {temp_fd}: no disponible ({e})")
                continue
            df = df[[c for c in COLUMNAS_FD if c in df.columns]].rename(columns=COLUMNAS_FD)
            df = df.dropna(subset=["local", "visitante"])
            df["fecha"] = pd.to_datetime(df["fecha"], dayfirst=True, errors="coerce").dt.strftime("%Y-%m-%d")
            df["temporada"] = temp_fd
            df["liga"] = nombre
            log(f"  [football-data] {nombre} {temp_fd}: {len(df)} partidos")
            tablas.append(df)
    if not tablas:
        raise SystemExit("No se pudo descargar nada de football-data.co.uk")
    todo = pd.concat(tablas, ignore_index=True)
    with sqlite3.connect(BD) as con:
        existe = con.execute("SELECT 1 FROM sqlite_master WHERE name='partidos'").fetchone()
        if existe:
            # se reemplazan solo esas ligas y temporadas; el resto se conserva
            temps = sorted(todo.temporada.unique())
            con.execute(f"DELETE FROM partidos WHERE temporada IN ({','.join('?' * len(temps))}) "
                        f"AND liga IN ({','.join('?' * len(ligas))})", [*temps, *ligas])
            cols = [r[1] for r in con.execute("PRAGMA table_info(partidos)")]
            for c in todo.columns:
                if c not in cols:
                    con.execute(f'ALTER TABLE partidos ADD COLUMN "{c}"')
            todo.to_sql("partidos", con, if_exists="append", index=False)
        else:
            todo.to_sql("partidos", con, if_exists="replace", index=False)
    log(f"  -> tabla 'partidos': {len(todo)} filas ({', '.join(ligas)})")


def descargar_proximos(ligas):
    """Próximos partidos con cuotas (se actualiza cada día en football-data)."""
    try:
        fx = pd.read_csv("https://www.football-data.co.uk/fixtures.csv", encoding="utf-8-sig")
        codigos = {LIGAS[n][0]: n for n in ligas}
        fx = fx[fx["Div"].isin(codigos)].copy()
        fx["liga"] = fx["Div"].map(codigos)
        fx = fx[[c for c in COLUMNAS_FD if c in fx.columns] + ["liga"]].rename(columns=COLUMNAS_FD)
        fx["fecha"] = pd.to_datetime(fx["fecha"], dayfirst=True, errors="coerce").dt.strftime("%Y-%m-%d")
        with sqlite3.connect(BD) as con:
            fx.to_sql("proximos", con, if_exists="replace", index=False)
        log(f"  -> tabla 'proximos': {len(fx)} partidos por jugar")
    except Exception as e:
        log(f"  [football-data] próximos partidos no disponibles ({e})")


# ---------------------------------------------------------------- Understat

def _texto(page):
    body = page.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return body


def _decodificar_jsonparse(s):
    """Understat mete el JSON escapado (\\x22...) dentro de JSON.parse('...')."""
    d = codecs.decode(s, "unicode_escape")
    try:
        d = d.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return json.loads(d)


def _variable_js(html, nombre):
    m = re.search(rf"var\s+{nombre}\s*=\s*JSON\.parse\('(.*?)'\)", html, re.S)
    return _decodificar_jsonparse(m.group(1)) if m else None


def understat_calendario(liga_us, temporada):
    """Lista de partidos de una liga/temporada (jugados y por jugar)."""
    ref = f"https://understat.com/league/{liga_us}/{temporada}"
    # 1º intento: endpoint JSON que usa la propia web
    try:
        page = Fetcher.get(
            f"https://understat.com/getLeagueData/{liga_us}/{temporada}",
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": ref},
            timeout=30,
        )
        if page.status == 200:
            datos = json.loads(_texto(page))
            if datos.get("dates"):
                return datos["dates"]
    except Exception:
        pass
    # 2º intento: datos incrustados en el HTML de la página de la liga
    page = Fetcher.get(ref, timeout=30)
    datos = _variable_js(_texto(page), "datesData")
    if datos is None:
        raise RuntimeError(f"No encuentro el calendario en {ref} (estado {page.status})")
    return datos


def understat_tiros(match_id):
    """Todos los tiros de un partido: lista de dicts."""
    ref = f"https://understat.com/match/{match_id}"
    try:
        page = Fetcher.get(
            f"https://understat.com/getMatchData/{match_id}",
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": ref},
            timeout=30,
        )
        if page.status == 200:
            datos = json.loads(_texto(page))
            tiros = datos.get("shots", datos)
            if isinstance(tiros, dict) and "h" in tiros:
                return tiros["h"] + tiros["a"]
    except Exception:
        pass
    page = Fetcher.get(ref, timeout=30)
    html = _texto(page)
    tiros = _variable_js(html, "shotsData")
    if tiros is None:
        (CARPETA / "debug_understat_partido.html").write_text(html, encoding="utf-8")
        raise RuntimeError(
            f"No encuentro los tiros en {ref} (estado {page.status}). "
            "He guardado el HTML en debug_understat_partido.html"
        )
    return tiros["h"] + tiros["a"]


def preparar_tablas(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS us_partidos (
            understat_id INTEGER PRIMARY KEY, liga TEXT, temporada INTEGER,
            fecha TEXT, hora TEXT, local TEXT, visitante TEXT,
            goles_local INTEGER, goles_visitante INTEGER,
            xg_local REAL, xg_visitante REAL,
            prob_local REAL, prob_empate REAL, prob_visitante REAL,
            jugado INTEGER, tiros_descargados INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS us_tiros (
            tiro_id INTEGER PRIMARY KEY, understat_id INTEGER,
            equipo TEXT, local_visitante TEXT, minuto INTEGER, parte INTEGER,
            resultado TEXT, a_puerta INTEGER, xg REAL,
            situacion TEXT, tipo TEXT, jugador TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tiros_partido ON us_tiros(understat_id);
    """)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def guardar_calendario(con, liga, temporada, partidos):
    for p in partidos:
        dt = p.get("datetime") or ""
        con.execute("""
            INSERT INTO us_partidos (understat_id, liga, temporada, fecha, hora, local, visitante,
                goles_local, goles_visitante, xg_local, xg_visitante,
                prob_local, prob_empate, prob_visitante, jugado)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(understat_id) DO UPDATE SET
                fecha=excluded.fecha, hora=excluded.hora,
                goles_local=excluded.goles_local, goles_visitante=excluded.goles_visitante,
                xg_local=excluded.xg_local, xg_visitante=excluded.xg_visitante,
                prob_local=excluded.prob_local, prob_empate=excluded.prob_empate,
                prob_visitante=excluded.prob_visitante, jugado=excluded.jugado
        """, (
            int(p["id"]), liga, temporada, dt[:10], dt[11:16],
            p["h"]["title"], p["a"]["title"],
            p["goals"]["h"], p["goals"]["a"],
            _f(p.get("xG", {}).get("h")), _f(p.get("xG", {}).get("a")),
            _f((p.get("forecast") or {}).get("w")), _f((p.get("forecast") or {}).get("d")),
            _f((p.get("forecast") or {}).get("l")),
            1 if p.get("isResult") else 0,
        ))
    con.commit()


A_PUERTA = {"Goal", "SavedShot"}  # gol o parada del portero (los postes no cuentan como a puerta)


def guardar_tiros(con, match_id, tiros):
    for t in tiros:
        minuto = int(t.get("minute", 0))
        equipo = t.get("h_team") if t.get("h_a") == "h" else t.get("a_team")
        con.execute("""
            INSERT OR REPLACE INTO us_tiros (tiro_id, understat_id, equipo, local_visitante,
                minuto, parte, resultado, a_puerta, xg, situacion, tipo, jugador)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(t["id"]), match_id, equipo, t.get("h_a"),
            minuto, 1 if minuto < 45 else 2,
            t.get("result"), 1 if t.get("result") in A_PUERTA else 0, _f(t.get("xG")),
            t.get("situation"), t.get("shotType"), t.get("player"),
        ))
    con.execute("UPDATE us_partidos SET tiros_descargados=1 WHERE understat_id=?", (match_id,))
    con.commit()


def descargar_understat(ligas, temporadas):
    with sqlite3.connect(BD) as con:
        preparar_tablas(con)

        for nombre in ligas:
            liga_us = LIGAS[nombre][1]
            for _, temp_us in temporadas:
                try:
                    partidos = understat_calendario(liga_us, temp_us)
                except Exception as e:
                    log(f"  [understat] {nombre} {temp_us}: ERROR calendario -> {e}")
                    continue
                guardar_calendario(con, nombre, temp_us, partidos)
                jugados = sum(1 for p in partidos if p.get("isResult"))
                log(f"  [understat] {nombre} {temp_us}: {len(partidos)} partidos ({jugados} jugados)")
                time.sleep(PAUSA)

        pendientes = con.execute(f"""
            SELECT understat_id, liga, fecha, local, visitante FROM us_partidos
            WHERE jugado=1 AND tiros_descargados=0 AND liga IN ({",".join("?" * len(ligas))})
            ORDER BY CASE liga WHEN 'laliga' THEN 0 ELSE 1 END, fecha
        """, ligas).fetchall()

        total = len(pendientes)
        if total == 0:
            log("  [understat] tiros: todo al día")
            return
        minutos = round(total * (PAUSA + 0.6) / 60)
        log(f"\n  [understat] Descargando tiros de {total} partidos (~{minutos} min).")
        log("  Puedes cerrar la ventana cuando quieras: la próxima vez sigue donde lo dejó.\n")

        errores_seguidos = 0
        for i, (mid, liga, fecha, loc, vis) in enumerate(pendientes, 1):
            try:
                tiros = understat_tiros(mid)
                guardar_tiros(con, mid, tiros)
                errores_seguidos = 0
                if i == 1 or i % 25 == 0 or i == total:
                    log(f"    {i}/{total}  {liga}  {fecha}  {loc} - {vis}  ({len(tiros)} tiros)")
            except Exception as e:
                errores_seguidos += 1
                log(f"    ERROR partido {mid} ({loc} - {vis}): {e}")
                if errores_seguidos >= 5:
                    log("\n  5 errores seguidos: paro aquí para no insistir. Avisa a Claude.")
                    return
            time.sleep(PAUSA)


# ---------------------------------------------------------------- resumen

def crear_vistas():
    with sqlite3.connect(BD) as con:
        con.executescript("""
            DROP VIEW IF EXISTS us_estadisticas;
            CREATE VIEW us_estadisticas AS
            SELECT p.understat_id, p.liga, p.temporada, p.fecha, p.local, p.visitante,
                   p.goles_local, p.goles_visitante, p.xg_local, p.xg_visitante,
                   SUM(t.local_visitante='h')                         AS tiros_local,
                   SUM(t.local_visitante='a')                         AS tiros_visitante,
                   SUM(t.local_visitante='h' AND t.a_puerta=1)        AS remates_puerta_local,
                   SUM(t.local_visitante='a' AND t.a_puerta=1)        AS remates_puerta_visitante,
                   SUM(t.local_visitante='h' AND t.parte=1)           AS tiros_local_1t,
                   SUM(t.local_visitante='a' AND t.parte=1)           AS tiros_visitante_1t,
                   SUM(t.local_visitante='h' AND t.parte=2)           AS tiros_local_2t,
                   SUM(t.local_visitante='a' AND t.parte=2)           AS tiros_visitante_2t,
                   SUM(t.local_visitante='h' AND t.parte=1 AND t.a_puerta=1) AS remates_puerta_local_1t,
                   SUM(t.local_visitante='a' AND t.parte=1 AND t.a_puerta=1) AS remates_puerta_visitante_1t,
                   SUM(t.local_visitante='h' AND t.parte=2 AND t.a_puerta=1) AS remates_puerta_local_2t,
                   SUM(t.local_visitante='a' AND t.parte=2 AND t.a_puerta=1) AS remates_puerta_visitante_2t,
                   ROUND(SUM(CASE WHEN t.local_visitante='h' AND t.parte=1 THEN t.xg END), 2) AS xg_local_1t,
                   ROUND(SUM(CASE WHEN t.local_visitante='a' AND t.parte=1 THEN t.xg END), 2) AS xg_visitante_1t,
                   ROUND(SUM(CASE WHEN t.local_visitante='h' AND t.parte=2 THEN t.xg END), 2) AS xg_local_2t,
                   ROUND(SUM(CASE WHEN t.local_visitante='a' AND t.parte=2 THEN t.xg END), 2) AS xg_visitante_2t
            FROM us_partidos p JOIN us_tiros t USING (understat_id)
            WHERE p.tiros_descargados = 1
            GROUP BY p.understat_id;
        """)
        log("\n=== RESUMEN DE LA BASE DE DATOS ===")
        for tabla in ["partidos", "proximos", "us_partidos", "us_tiros"]:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
                log(f"  {tabla:12s} {n:6d} filas")
            except sqlite3.Error:
                pass
        filas = con.execute("""
            SELECT liga, COUNT(*), SUM(tiros_descargados) FROM us_partidos
            WHERE jugado=1 GROUP BY liga
        """).fetchall()
        for liga, jugados, con_tiros in filas:
            log(f"  understat {liga:10s} {con_tiros or 0}/{jugados} partidos con tiros")

        ej = con.execute("""
            SELECT fecha, local, visitante, goles_local, goles_visitante,
                   xg_local, xg_visitante, tiros_local_1t, tiros_local_2t,
                   tiros_visitante_1t, tiros_visitante_2t
            FROM us_estadisticas WHERE liga='laliga' ORDER BY fecha DESC LIMIT 3
        """).fetchall()
        if ej:
            log("\n  Ejemplo (últimos de LaLiga) - tiros por parte:")
            for f, l, v, gl, gv, xl, xv, tl1, tl2, tv1, tv2 in ej:
                log(f"   {f} {l} {gl}-{gv} {v} | xG {xl or 0:.2f}-{xv or 0:.2f} | "
                    f"tiros 1T {tl1}-{tv1}, 2T {tl2}-{tv2}")


# ---------------------------------------------------------------- main

RAPIDO = False
TODAS = list(TEMPORADAS)

if __name__ == "__main__":
    if "rapido" in [a.lower() for a in sys.argv[1:]]:
        RAPIDO = True
        TEMPORADAS = TEMPORADAS[-1:]   # solo la temporada en curso
    elegidas = [a.lower() for a in sys.argv[1:] if a.lower() in LIGAS] or list(LIGAS)
    log(f"Actualización {datetime.now():%Y-%m-%d %H:%M}  ligas: {', '.join(elegidas)}\n")
    # ligas sin histórico (recién añadidas): se descargan completas aunque sea modo rápido
    def sin_historia(tabla, col_temp, valor):
        if not (RAPIDO and BD.exists()):
            return []
        with sqlite3.connect(BD) as con:
            try:
                hay = {r[0] for r in con.execute(f"SELECT DISTINCT liga FROM {tabla} WHERE {col_temp} = ?", (valor,))}
            except sqlite3.Error:
                hay = set()
        return [l for l in elegidas if l not in hay]

    def por_grupos(funcion, nuevas):
        if nuevas:
            log(f"  Ligas sin histórico, descarga completa: {', '.join(nuevas)}")
        normales = [l for l in elegidas if l not in nuevas]
        if normales:
            funcion(normales, TEMPORADAS)
        if nuevas:
            funcion(nuevas, TODAS)

    log("1) football-data.co.uk")
    por_grupos(descargar_football_data, sin_historia("partidos", "temporada", TODAS[0][0]))
    descargar_proximos(elegidas)
    log("\n2) Understat")
    por_grupos(descargar_understat, sin_historia("us_partidos", "temporada", TODAS[0][1]))
    crear_vistas()
    log("\nTerminado.")
