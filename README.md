# Pronósticos ⚽

Pronósticos estadísticos de LaLiga, Premier League, Serie A, Bundesliga, Ligue 1 y Champions, con seguimiento de aciertos, combinada y escalera.

- **Web (instalable en el móvil):** https://fau25.github.io/pronosticos/
- Se actualiza sola a las 00:30 y a las 7:30 (hora de España) con GitHub Actions (`.github/workflows/actualizar.yml`): descarga resultados, recalcula, marca ✓/✗ y publica en GitHub Pages.
- Para lanzarla a mano: **Actions → Actualizar pronósticos → Run workflow**.

## Cómo funciona
- `actualizar_datos.py`: football-data.co.uk (resultados, tiros, córners, tarjetas) y Understat (xG y tiros por minuto).
- `champions.py`, `europa.py`, `extra_ligas.py`: ESPN (Champions, competiciones europeas y 10 ligas para medir el nivel de cada liga).
- `modelo.py` + `backtest.py`: modelo de ataque/defensa por métrica; los días ya jugados se calculan solo con partidos anteriores.
- `exportar_web.py` + `web_plantilla.html` + `estaticos/`: genera la web en `docs/` (últimos 30 días en la página, histórico resumido en `web/historico.json`).
- La base de datos vive en la caché de Actions, con copia semanal en la release `copia-bd` (cifrada si existe el secreto `BD_CLAVE`).

## Aviso
+18. Pronósticos estadísticos orientativos, sin ninguna garantía; no son consejos de apuesta. Juega con responsabilidad · 900 200 225 · jugarbien.es.
Datos: football-data.co.uk, Understat y ESPN (uso personal, sin fines comerciales).
