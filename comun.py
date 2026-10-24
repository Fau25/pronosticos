"""Utilidades comunes.
- temporada(): año de inicio de la temporada en curso (cambia el 1 de julio), para no tener años fijos en el código.
- avisar()/leer(): avisos de las descargas (fuente que falla en parte, equipos sin emparejar...).
  Cada script los añade a avisos.txt y exportar_web.py los muestra discretos en la web."""
from datetime import date
from pathlib import Path


def temporada(hoy=None):
    hoy = hoy or date.today()
    return hoy.year if hoy.month >= 7 else hoy.year - 1


def temporadas_fd(n=3):
    """Últimas n temporadas como (código football-data, año Understat): [('2425', 2024), ...]"""
    t = temporada()
    return [(f"{(a) % 100:02d}{(a + 1) % 100:02d}", a) for a in range(t - n + 1, t + 1)]

ARCHIVO = Path(__file__).resolve().parent / "avisos.txt"


def avisar(msg):
    print("  AVISO:", msg, flush=True)
    with open(ARCHIVO, "a", encoding="utf-8") as f:
        f.write(msg.replace("\n", " ") + "\n")


def leer():
    try:
        return [l.strip() for l in ARCHIVO.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []
