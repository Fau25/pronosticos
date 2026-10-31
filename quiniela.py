"""
La Quiniela (sección de prueba).

- Lista de la jornada: se lee de quiniela15.com (web no oficial de quinielistas; la de Loterías bloquea
  las consultas automáticas). Como mucho una vez cada 12 horas.
- Pronóstico 1X2: goles esperados de cada equipo con el mismo modelo de la app (Primera, Segunda y Liga F)
  -> Poisson -> probabilidades 1, X y 2, con un ajuste de empates calibrado con partidos pasados.
- Pleno al 15: categoría de goles más probable de cada equipo (0, 1, 2, M).
- Cada jornada se guarda en quinielas/; al cierre el pronóstico queda congelado y después se añaden los resultados.
- Fiabilidad: prueba "a ciegas" con la temporada pasada y la actual (cada partido pronosticado solo con lo
  anterior), guardada en datos/quiniela_fiabilidad.json y recalculada cada semana.
"""
import html
import json
import math
import re
import sqlite3
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import modelo as M
from comun import avisar, temporada

RAIZ = Path(__file__).resolve().parent
CARPETA = RAIZ / "quinielas"
FIAB = RAIZ / "datos" / "quiniela_fiabilidad.json"
URL = "https://www.quiniela15.com/"
MADRID = ZoneInfo("Europe/Madrid")
LIGAS_Q = ("laliga", "es2", "esf")
NOMBRE_LIGA = {"laliga": "Primera", "es2": "Segunda", "esf": "Liga F", "eu": "Europa"}
LIGAS_ES = ("laliga", "es2")   # se buscan primero; si no aparece, en el resto de ligas (partidos europeos)
DOBLES = 3          # dobles sugeridos (los partidos más inciertos)
INICIO_FIAB = pd.Timestamp("2025-08-01")


# ------------------------------------------------------------------ lista de la jornada

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pronosticos personales)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def leer_lista(texto=None):
    """-> {jornada, cierre (ISO, hora de España), partidos: [{n, local, visitante, local_completo, visitante_completo, cuando}]}"""
    h = texto if texto is not None else _get(URL)
    m = re.search(r"Pron[oó]stico de la jornada (\d+)</h2>.*?Cierra el (\d{2}/\d{2}/\d{4}) a las (\d{2}:\d{2})", h, re.S)
    if not m:
        raise RuntimeError("no encuentro la jornada en quiniela15")
    jornada = int(m.group(1))
    cierre = datetime.strptime(f"{m.group(2)} {m.group(3)}", "%d/%m/%Y %H:%M").replace(tzinfo=MADRID)
    tabla = h[m.end(): h.find("</table>", m.end())]
    partidos = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tabla, re.S):
        tds = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", td))).strip()
               for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        alts = [html.unescape(a) for a in re.findall(r'alt="Escudo del? ([^"]+)"', tr)]
        if len(tds) < 5 or not tds[0].isdigit():
            continue
        partidos.append(dict(n=int(tds[0]), local=tds[2], visitante=tds[4], cuando=tds[-1],
                             local_completo=alts[0] if alts else tds[2],
                             visitante_completo=alts[1] if len(alts) > 1 else tds[4]))
    if len(partidos) != 15:
        raise RuntimeError(f"la jornada {jornada} tiene {len(partidos)} partidos (se esperaban 15)")
    return {"jornada": jornada, "cierre": cierre.isoformat(), "partidos": partidos}


# ------------------------------------------------------------------ emparejar equipos

_GENERICAS = {"cf", "fc", "cd", "ud", "sd", "rc", "rcd", "sad", "club", "de", "del", "la", "el", "futbol",
              "femenino", "femenina", "f", "c", "d", "u", "s", "balompie", "union", "deportiva", "y"}
_SINONIMOS = {"at": "atletico", "atl": "atletico", "r": "real", "ath": "athletic", "dep": "deportivo",
              "bilbao": "athletic", "vallecano": "rayo", "coruna": "deportivo",
              # nombres en español de equipos y ciudades europeas
              "oporto": "porto", "napoles": "napoli", "munchen": "munich", "atenas": "athens", "praga": "prague",
              "lisboa": "lisbon", "brujas": "brugge", "amberes": "antwerp", "colonia": "cologne", "estambul": "istanbul",
              "turin": "torino", "londres": "london",
              "man": "manchester", "rb": "rasenballsport"}


def _tokens(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(f\)", " ", s)
    ws = re.sub(r"[^a-z0-9 ]", " ", s).split()
    return {_SINONIMOS.get(w, w) for w in ws} - _GENERICAS


def _es_femenino(*nombres):
    t = " ".join(nombres).lower()
    return "(f)" in t or "femenin" in t or "cff" in t


def _parecido(textos, cand):
    tc = _tokens(cand)
    mejor = 0.0
    for t in textos:
        tt = _tokens(t)
        if not tt or not tc:
            continue
        jac = len(tt & tc) / len(tt | tc)
        cubre = len(tt & tc) / len(tc)      # el nombre corto del modelo está contenido en el de la quiniela
        letras = SequenceMatcher(None, " ".join(sorted(tt)), " ".join(sorted(tc))).ratio()
        mejor = max(mejor, jac, 0.9 * cubre, letras * 0.9 if letras >= 0.8 else 0)   # parecido de letras solo si es alto
    return mejor


def candidatos(con):
    """{liga: [nombres]} con los equipos de esta temporada en el modelo (todas las ligas)."""
    t = temporada()
    out = {}
    for liga, n in con.execute("SELECT DISTINCT liga, local FROM us_partidos WHERE temporada=?", (t,)):
        out.setdefault(liga, []).append(n)
    ini = f"{t}-07-01"
    try:
        for liga, n in con.execute("SELECT DISTINCT liga, local FROM extra_partidos WHERE fecha_utc >= ?", (ini,)):
            out.setdefault(liga, []).append(n)
    except sqlite3.Error:
        pass
    return out


def emparejar(cands, corto, completo, femenino):
    if femenino:
        return _emparejar(cands, corto, completo, ["esf"])
    l, n = _emparejar(cands, corto, completo, LIGAS_ES)
    if n:
        return l, n
    otras = [x for x in cands if x not in LIGAS_ES and x != "esf"]
    return _emparejar(cands, corto, completo, otras, minimo=0.6)


def _emparejar(cands, corto, completo, ligas, minimo=0.5):
    puntos = sorted(((_parecido([corto, completo], c), liga, c) for liga in ligas for c in cands.get(liga, [])),
                    reverse=True)
    if not puntos or puntos[0][0] < minimo:
        return None, None
    if len(puntos) > 1 and puntos[1][0] >= puntos[0][0] - 0.02 and puntos[1][2] != puntos[0][2]:
        return None, None     # empate: mejor no adivinar
    return puntos[0][1], puntos[0][2]


# ------------------------------------------------------------------ probabilidades

def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def prob_1x2(lh, la, k_empate=1.0, maxg=10):
    ph = [_pois(i, lh) for i in range(maxg + 1)]
    pa = [_pois(j, la) for j in range(maxg + 1)]
    p1 = sum(ph[i] * pa[j] for i in range(maxg + 1) for j in range(maxg + 1) if i > j)
    px = sum(ph[i] * pa[i] for i in range(maxg + 1)) * k_empate
    p2 = sum(ph[i] * pa[j] for i in range(maxg + 1) for j in range(maxg + 1) if i < j)
    s = p1 + px + p2
    return [p1 / s, px / s, p2 / s]


def categorias(lam):
    """Probabilidad de 0, 1, 2 y M (3 o más) goles."""
    c = [_pois(0, lam), _pois(1, lam), _pois(2, lam)]
    return c + [max(0.0, 1 - sum(c))]


CAT = ["0", "1", "2", "M"]


def _cat(goles):
    return "M" if goles >= 3 else str(int(goles))


def _signo(gl, gv):
    return "1" if gl > gv else "X" if gl == gv else "2"


# ------------------------------------------------------------------ fiabilidad (prueba a ciegas)

def calcular_fiabilidad(df):
    """Recorre por fecha los partidos de Primera, Segunda y Liga F: pronostica cada uno solo con lo anterior."""
    sub = df[df.liga.isin(LIGAS_Q) & df.goles_local.notna()].sort_values("fecha")
    mod, filas = M.Modelo(), []
    for fecha, jornada in sub.groupby("fecha", sort=True):
        jornada = jornada.to_dict("records")
        if fecha >= INICIO_FIAB:
            for p in jornada:
                if mod.partidos[(p["liga"], p["local"])] < 5 or mod.partidos[(p["liga"], p["visitante"])] < 5:
                    continue
                e = mod.esperado(p["liga"], p["local"], p["visitante"], "goles")
                if e:
                    filas.append((p["liga"], e[0], e[1], int(p["goles_local"]), int(p["goles_visitante"])))
        for p in jornada:
            mod.actualizar(p)
    if len(filas) < 200:
        return None
    # ajuste de empates: el que mejor explica lo ocurrido (máxima verosimilitud)
    def logv(k):
        return sum(math.log(max(prob_1x2(lh, la, k)["1X2".index(_signo(gl, gv))], 1e-9)) for _, lh, la, gl, gv in filas)
    k = max([x / 100 for x in range(90, 161, 5)], key=logv)
    res = {"k_empate": k, "desde": INICIO_FIAB.strftime("%Y-%m-%d"), "por_liga": {},
           "generado": datetime.now(MADRID).strftime("%Y-%m-%d %H:%M")}
    grupos = {"total": filas, **{l: [f for f in filas if f[0] == l] for l in LIGAS_Q}}
    for nombre, fs in grupos.items():
        if not fs:
            continue
        ac = dob = ple = 0
        for _, lh, la, gl, gv in fs:
            p = prob_1x2(lh, la, k)
            orden = sorted(range(3), key=lambda i: -p[i])
            real = "1X2".index(_signo(gl, gv))
            ac += real == orden[0]
            dob += real in orden[:2]
            ple += (CAT[max(range(4), key=lambda i: categorias(lh)[i])] == _cat(gl)) and \
                   (CAT[max(range(4), key=lambda i: categorias(la)[i])] == _cat(gv))
        n = len(fs)
        d = {"n": n, "acierto": round(ac / n, 4), "con_doble": round(dob / n, 4), "pleno": round(ple / n, 4),
             "de_14": round(14 * ac / n, 1)}
        if nombre == "total":
            res.update(d)
        else:
            res["por_liga"][nombre] = d
    return res


def fiabilidad(df):
    try:
        f = json.loads(FIAB.read_text(encoding="utf-8"))
        if datetime.now(MADRID) - datetime.strptime(f["generado"], "%Y-%m-%d %H:%M").replace(tzinfo=MADRID) < timedelta(days=7):
            return f
    except (OSError, ValueError, KeyError):
        pass
    f = calcular_fiabilidad(df)
    if f:
        FIAB.write_text(json.dumps(f, ensure_ascii=False, indent=1), encoding="utf-8")
    return f


# ------------------------------------------------------------------ pronóstico y resultados

def _pronosticar(mod, con, lista, k):
    cands = candidatos(con)
    sin = []
    for p in lista["partidos"]:
        fem = _es_femenino(p["local"], p["visitante"], p["local_completo"], p["visitante_completo"])
        l1, n1 = emparejar(cands, p["local"], p["local_completo"], fem)
        l2, n2 = emparejar(cands, p["visitante"], p["visitante_completo"], fem)
        p.update(liga=None, eq_local=n1, eq_visitante=n2, prob=None)
        if not n1 or not n2:
            sin += [x for x, y in ((p["local"], n1), (p["visitante"], n2)) if not y]
            continue
        # misma liga, o partido europeo entre ligas (como en Champions: media de ambas y nivel de cada liga)
        p["liga"] = l1 if l1 == l2 else "eu"
        if l1 != l2:
            p.update(liga_local=l1, liga_visitante=l2)
        if mod.partidos[(l1, n1)] < 3 or mod.partidos[(l2, n2)] < 3:
            continue
        e = mod.esperado(l1, n1, n2, "goles", liga_v=l2)
        if not e:
            continue
        lh, la = e
        pr = prob_1x2(lh, la, k)
        p.update(prob=[round(x, 4) for x in pr], goles_esp=[round(lh, 2), round(la, 2)],
                 signo="1X2"[max(range(3), key=lambda i: pr[i])])
        if p["n"] == 15:
            cl, cv = categorias(lh), categorias(la)
            il, iv = max(range(4), key=lambda i: cl[i]), max(range(4), key=lambda i: cv[i])
            p["pleno"] = {"local": CAT[il], "visitante": CAT[iv], "prob": round(cl[il] * cv[iv], 4),
                          "cat_local": [round(x, 3) for x in cl], "cat_visitante": [round(x, 3) for x in cv]}
    # dobles: los partidos más inciertos (entre los 14 primeros)
    inciertos = sorted((p for p in lista["partidos"] if p["prob"] and p["n"] <= 14), key=lambda p: max(p["prob"]))
    for p in lista["partidos"]:
        p["doble"] = None
    for p in inciertos[:DOBLES]:
        orden = sorted(range(3), key=lambda i: -p["prob"][i])
        p["doble"] = "".join(sorted("1X2"[i] for i in orden[:2]))
    if sin:
        avisar(f"Quiniela: equipos sin reconocer ({', '.join(sorted(set(sin)))})")
    return lista


def _resultado_europa(con, p, desde, hasta):
    """Goles de un partido europeo (Champions, Europa League o Conference) desde ESPN."""
    import champions as UCL
    quiero = ((p.get("liga_local"), p["eq_local"]), (p.get("liga_visitante"), p["eq_visitante"]))
    for tabla in ("europa_partidos", "ucl_partidos"):
        try:
            filas = con.execute(f"SELECT local, visitante, goles_local, goles_visitante FROM {tabla} "
                                "WHERE completado=1 AND substr(fecha_utc, 1, 10) BETWEEN ? AND ?", (desde, hasta)).fetchall()
        except sqlite3.Error:
            continue
        for loc, vis, gl, gv in filas:
            if gl is not None and (UCL.emparejar(con, loc, True), UCL.emparejar(con, vis, True)) == quiero:
                return int(gl), int(gv)
    return None


def _resultado(con, p, desde, hasta):
    """Goles del partido (local, visitante) desde ESPN/Understat, o None."""
    if not p.get("liga"):
        return None
    if p["liga"] == "eu":
        return _resultado_europa(con, p, desde, hasta)
    if p["liga"] == "laliga":
        consultas = [("SELECT goles_local, goles_visitante FROM espn_ligas WHERE completado=1 AND liga='laliga' "
                      "AND local=? AND visitante=? AND fecha_utc BETWEEN ? AND ?"),
                     ("SELECT goles_local, goles_visitante FROM us_partidos WHERE jugado=1 AND liga='laliga' "
                      "AND local=? AND visitante=? AND fecha BETWEEN ? AND ?")]
    else:
        consultas = [("SELECT goles_local, goles_visitante FROM extra_partidos WHERE completado=1 AND liga='" + p["liga"] +
                      "' AND local=? AND visitante=? AND fecha_utc BETWEEN ? AND ?")]
    for q in consultas:
        try:
            r = con.execute(q, (p["eq_local"], p["eq_visitante"], desde, hasta)).fetchone()
        except sqlite3.Error:
            r = None
        if r and r[0] is not None:
            return int(r[0]), int(r[1])
    return None


def _resumen(j):
    con_res = [p for p in j["partidos"] if p.get("resultado")]
    s = {"jugados": len(con_res), "aciertos": 0, "con_dobles": 0, "pronosticados": 0, "pleno": None}
    for p in con_res:
        if p["n"] == 15:
            if p.get("pleno"):
                s["pleno"] = p["pleno"]["local"] == p["resultado"]["cat"][0] and p["pleno"]["visitante"] == p["resultado"]["cat"][1]
            continue
        if not p.get("signo"):
            continue
        s["pronosticados"] += 1
        s["aciertos"] += p["signo"] == p["resultado"]["signo"]
        s["con_dobles"] += p["resultado"]["signo"] in (p.get("doble") or p["signo"])
    return s


def generar(mod, df, con, log=print):
    """Actualiza la jornada en curso y los resultados de las guardadas. -> datos para la web."""
    CARPETA.mkdir(exist_ok=True)
    fiab = fiabilidad(df)
    k = (fiab or {}).get("k_empate", 1.0)
    ahora = datetime.now(MADRID)
    estado_f = CARPETA / "_estado.json"
    try:
        estado = json.loads(estado_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        estado = {}
    guardadas = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(CARPETA.glob("20*.json"))}

    # 1) lista de la jornada (como mucho cada 12 h, o si la última guardada ya cerró)
    ultima = max(guardadas.values(), key=lambda j: j["cierre"], default=None)
    hace = ahora - datetime.fromisoformat(estado["leida"]) if estado.get("leida") else timedelta(days=99)
    if hace > timedelta(hours=12) or not ultima or datetime.fromisoformat(ultima["cierre"]) <= ahora:
        try:
            lista = leer_lista()
            estado["leida"] = ahora.isoformat()
            estado_f.write_text(json.dumps(estado), encoding="utf-8")
            clave = f"{temporada()}-{lista['jornada']:02d}"
            previa = guardadas.get(clave)
            if not previa or not previa.get("congelada"):
                j = _pronosticar(mod, con, lista, k)
                j.update(clave=clave, temporada=temporada(), generado=ahora.strftime("%Y-%m-%d %H:%M"), congelada=False)
                guardadas[clave] = j
                log(f"  [quiniela] jornada {lista['jornada']}: {sum(1 for p in j['partidos'] if p['prob'])}/15 pronosticados")
        except Exception as e:
            log(f"  [quiniela] no se pudo leer la lista: {e}")
            avisar("Quiniela: no se pudo leer la lista de la jornada (quiniela15.com)")

    # 2) congelar al cierre y añadir resultados
    for clave, j in guardadas.items():
        cierre = datetime.fromisoformat(j["cierre"])
        if ahora >= cierre:
            j["congelada"] = True
        if ahora < cierre or ahora > cierre + timedelta(days=12):
            continue
        desde, hasta = (cierre - timedelta(days=1)).strftime("%Y-%m-%d"), (cierre + timedelta(days=6)).strftime("%Y-%m-%d")
        for p in j["partidos"]:
            if p.get("resultado"):
                continue
            g = _resultado(con, p, desde, hasta)
            if g:
                p["resultado"] = {"goles": list(g), "signo": _signo(*g), "cat": [_cat(g[0]), _cat(g[1])]}
        j["resumen"] = _resumen(j)
    for clave, j in guardadas.items():
        (CARPETA / f"{clave}.json").write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")

    # 3) para la web: la jornada actual (o la última), el historial y la fiabilidad
    orden = sorted(guardadas.values(), key=lambda j: j["cierre"])
    actual = next((j for j in orden if datetime.fromisoformat(j["cierre"]) > ahora), orden[-1] if orden else None)
    historial = [{"clave": j["clave"], "jornada": j["jornada"], "cierre": j["cierre"], **(j.get("resumen") or _resumen(j))}
                 for j in orden if j.get("congelada")]
    return {"actual": actual, "anteriores": [j for j in orden if j.get("congelada")][-6:][::-1],
            "historial": historial, "fiabilidad": fiab, "nombre_liga": NOMBRE_LIGA}
