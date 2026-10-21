"""
Combinada del día: elige 2-4 pronósticos (de 1 o 2 partidos) que juntos lleguen a la cuota
objetivo con la MAYOR probabilidad de acierto posible.

- Probabilidad de cada pronóstico: la del modelo (calibrada en todas las líneas en el backtest).
- Cuota justa = 1 / probabilidad.
- Cuota estimada en la casa = cuota justa recortada por el margen típico de las casas.
- Si hay cuota real disponible (goles +/-2,5 de football-data), se usa esa.
"""

import itertools
import math

import numpy as np

# mercados que suelen ofrecer las casas; un pronóstico por familia y partido
FAMILIA = {"goles": "goles", "goles_1t": "goles", "goles_2t": "goles",
           "tiros": "tiros", "remates": "remates", "corners": "corners", "tarjetas": "tarjetas"}
MARGEN = 0.06          # margen típico de la casa por selección
P_MIN, P_MAX = 0.55, 0.92   # por encima de ~92 % las casas apenas pagan (cuota < 1,05)


def cuota_casa_estimada(p):
    return 1 / min(max(p, 0.01) * (1 + MARGEN), 0.99)


def candidatos(mod, partido, cuotas_reales=None):
    """Todas las selecciones razonables de un partido: (prob, texto, slot, cuota_est, cuota_real)."""
    out = []
    for met, fam in FAMILIA.items():
        e = mod.esperado(partido.get("liga_local", partido["liga"]), partido["local"], partido["visitante"],
                         met, partido.get("liga_visitante"))
        if e is None:
            continue
        total = e[0] + e[1]
        for l in np.arange(0.5, 50, 1.0):
            pm = mod.prob_mas(met, total, l)
            for p, lado in ((pm, "Más"), (1 - pm, "Menos")):
                if P_MIN <= p <= P_MAX:
                    real = None
                    if cuotas_reales and met == "goles" and l == 2.5:
                        real = cuotas_reales.get("mas25" if lado == "Más" else "menos25")
                    out.append(dict(partido=f"{partido['local']} – {partido['visitante']}",
                                    slot=(partido["understat_id"], fam), metrica=met,
                                    texto=f"{lado} de {l} {NOMBRE_CORTO[met]}",
                                    prob=p, cuota_justa=1 / p,
                                    cuota_casa=real or cuota_casa_estimada(p),
                                    cuota_es_real=bool(real)))
    return out


NOMBRE_CORTO = {"goles": "goles", "goles_1t": "goles (1ª parte)", "goles_2t": "goles (2ª parte)",
                "tiros": "tiros", "remates": "remates a puerta", "corners": "córners",
                "tarjetas": "tarjetas"}


def _reducir(cands, por_slot=7):
    """Por cada familia/partido deja unas pocas opciones repartidas en probabilidad."""
    por = {}
    for c in cands:
        por.setdefault(c["slot"], []).append(c)
    out = {}
    for slot, lista in por.items():
        lista.sort(key=lambda c: -c["prob"])
        cubos = {}
        for c in lista:
            cubos.setdefault(round(c["prob"] / 0.06), c)
        out[slot] = sorted(cubos.values(), key=lambda c: -c["prob"])[:por_slot]
    return out


def mejor_por_acierto(mod, partidos, prob_minima, patas=(1, 2, 3, 4), cuotas_reales=None):
    """La combinada con MAYOR cuota cuya probabilidad de acierto sea >= prob_minima."""
    cands = []
    for p in partidos:
        cands += candidatos(mod, p, (cuotas_reales or {}).get(p["understat_id"]))
    slots = _reducir(cands)
    mejor = None
    for n in patas:
        for combo_slots in itertools.combinations(list(slots), n):
            for sel in itertools.product(*(slots[s] for s in combo_slots)):
                prob = math.prod(c["prob"] for c in sel)
                if prob < prob_minima:
                    continue
                cuota = math.prod(c["cuota_casa"] for c in sel)
                if mejor is None or cuota > mejor["cuota_casa"]:
                    mejor = dict(selecciones=list(sel), prob=prob, cuota_casa=cuota,
                                 cuota_justa=1 / prob)
    return mejor


def mejor_combinada(mod, partidos, cuota_objetivo, patas=(2, 3, 4), cuotas_reales=None):
    """partidos: lista de dicts (1 o 2). Devuelve la combinada con más probabilidad
    cuya cuota estimada en la casa sea >= cuota_objetivo."""
    cands = []
    for p in partidos:
        cands += candidatos(mod, p, (cuotas_reales or {}).get(p["understat_id"]))
    slots = _reducir(cands)
    ids = {p["understat_id"] for p in partidos}
    mejor = None
    claves = list(slots)
    for n in patas:
        for combo_slots in itertools.combinations(claves, n):
            if len({s[0] for s in combo_slots}) < len(ids):
                continue      # con 2 partidos, al menos una selección de cada uno
            for sel in itertools.product(*(slots[s] for s in combo_slots)):
                cuota = math.prod(c["cuota_casa"] for c in sel)
                if cuota < cuota_objetivo:
                    continue
                prob = math.prod(c["prob"] for c in sel)
                if mejor is None or prob > mejor["prob"]:
                    mejor = dict(selecciones=list(sel), prob=prob, cuota_casa=cuota,
                                 cuota_justa=1 / prob)
    return mejor
