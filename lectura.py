"""
Lectura del partido en texto, generada en local a partir de los datos (gratis, sin API).
"""


def _forma(txt):
    if not txt:
        return "sin datos de forma"
    v, e, d = txt.count("V"), txt.count("E"), txt.count("D")
    return f"{v}V {e}E {d}D en los últimos {len(txt)}"


def generar(partido, pron, ctx):
    loc, vis = partido["local"], partido["visitante"]
    por = {f["metrica"]: f for f in pron}
    lineas = []

    # 1) Contexto
    cl, cv = ctx.get(loc), ctx.get(vis)
    if cl and cv:
        lineas.append(f"**Contexto:** {loc} es {cl['posicion']}º con {cl['puntos']} pts "
                      f"({_forma(cl['ultimos_5'])}); {vis} es {cv['posicion']}º con {cv['puntos']} pts "
                      f"({_forma(cv['ultimos_5'])}). {partido.get('motivo', '').capitalize()}.")

    # 2) Quién domina
    t = por.get("tiros")
    if t:
        a, b = t["esperado_local"], t["esperado_visitante"]
        if max(a, b) >= 1.4 * min(a, b):
            dom, sub = (loc, vis) if a > b else (vis, loc)
            lineas.append(f"**Dominio:** se espera que {dom} lleve el peso del partido "
                          f"({max(a, b):.0f} tiros frente a {min(a, b):.0f} de {sub}).")
        else:
            lineas.append(f"**Dominio:** partido equilibrado en llegadas "
                          f"({a:.0f} tiros {loc} – {b:.0f} {vis}).")

    # 3) Por partes
    g1, g2 = por.get("goles_1t"), por.get("goles_2t")
    if g1 and g2:
        if g2["esperado_total"] >= 1.3 * g1["esperado_total"]:
            lineas.append(f"**Por partes:** más actividad esperada tras el descanso "
                          f"({g1['esperado_total']:.1f} goles esperados en la 1ª parte "
                          f"y {g2['esperado_total']:.1f} en la 2ª).")
        else:
            lineas.append(f"**Por partes:** reparto parecido entre las dos mitades "
                          f"({g1['esperado_total']:.1f} y {g2['esperado_total']:.1f} goles esperados).")

    # 4) Pronósticos más sólidos (línea normal)
    solidos = sorted([f for f in pron if f.get("prob") and f["prob"] >= 0.6],
                     key=lambda f: f["prob"], reverse=True)[:3]
    if solidos:
        txt = "; ".join(f"{f['nombre']}: {f['eleccion']} de {f['linea']} ({f['prob']:.0%})"
                        for f in solidos)
        lineas.append(f"**Lo más sólido (línea normal):** {txt}.")
    else:
        lineas.append("**Lo más sólido:** ningún pronóstico en línea normal supera el 60 %; "
                      "partido difícil de leer.")

    # 5) Líneas seguras más holgadas
    seguras = sorted([f for f in pron if f.get("segura_prob")],
                     key=lambda f: f["segura_prob"], reverse=True)[:3]
    if seguras:
        txt = "; ".join(f"{f['nombre']}: {f['segura_lado']} de {f['segura_linea']} "
                        f"({f['segura_prob']:.0%})" for f in seguras)
        lineas.append(f"**Líneas seguras más holgadas:** {txt}.")

    # 6) Lo que el modelo no ve
    lineas.append("**Ojo:** el modelo solo ve números. No sabe de bajas, rotaciones por "
                  "competición europea, árbitro ni motivación. Revisa las alineaciones antes del partido.")
    return "\n\n".join(f"- {l}" for l in lineas)
