"""
Combinada del día: elige 2-4 pronósticos (de 1 o 2 partidos) que juntos lleguen a la cuota
objetivo con la MAYOR probabilidad de acierto posible.

- Probabilidad de cada pronóstico: la del modelo (calibrada en todas las líneas en el backtest).
- Cuota justa = 1 / probabilidad.
- Cuota estimada en la casa = cuota justa recortada por el margen típico de las casas.
- Si hay cuota real disponible (goles +/-2,5 de football-data), se usa esa.
"""

import itertools
import json
import math
from pathlib import Path

import numpy as np

# mercados que suelen ofrecer las casas; un pronóstico por familia y partido
FAMILIA = {"goles": "goles", "goles_1t": "goles", "goles_2t": "goles",
           "tiros": "tiros", "remates": "remates", "corners": "corners", "tarjetas": "tarjetas"}
MARGEN = 0.06          # margen típico de la casa por selección
P_MIN, P_MAX = 0.55, 0.92   # por encima de ~92 % las casas apenas pagan (cuota < 1,05)


# Acierto conjunto real / producto de probabilidades, para dos selecciones del MISMO partido
# (medido en el backtest: misma dirección suelen salir juntas, direcciones opuestas al revés)
_CORR_F = Path(__file__).resolve().parent / "datos" / "correlaciones.json"
CORRELACION = json.loads(_CORR_F.read_text(encoding="utf-8")) if _CORR_F.exists() else {}
ORDEN_FAM = ["goles", "tiros", "remates", "corners", "tarjetas"]


def _lado(texto):
    return "más" if texto.startswith("Más") else "menos"


def factor_correlacion(selecciones):
    """Corrige la probabilidad conjunta de las selecciones que comparten partido."""
    f = 1.0
    for a, b in itertools.combinations(selecciones, 2):
        if a["slot"][0] != b["slot"][0]:
            continue
        (fa, la), (fb, lb) = sorted([(a["slot"][1], _lado(a["texto"])), (b["slot"][1], _lado(b["texto"]))],
                                    key=lambda x: ORDEN_FAM.index(x[0]))
        f *= CORRELACION.get(f"{fa}|{la}|{fb}|{lb}", 1.0)
    # solo se penaliza (probado: premiar las que van juntas hace que el buscador infle la probabilidad)
    return min(f, 1.0)


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
