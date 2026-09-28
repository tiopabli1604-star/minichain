"""Carteras locales: claves privadas guardadas en ~/.minichain/carteras/<nombre>.json.

Es una blockchain de aprendizaje/uso local: las claves se guardan SIN cifrar.
No reutilices estas claves para nada de valor real."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import cripto

DIR = Path(os.environ.get("MINICHAIN_HOME", Path.home() / ".minichain")) / "carteras"


def _ruta(nombre: str) -> Path:
    if not re.fullmatch(r"[\w\-]{1,40}", nombre):
        raise ValueError("Nombre de cartera: solo letras, números, - y _ (máx. 40)")
    return DIR / f"{nombre}.json"


def crear(nombre: str) -> dict:
    p = _ruta(nombre)
    if p.exists():
        raise FileExistsError(f"Ya existe la cartera '{nombre}'")
    priv = cripto.nueva_clave()
    pub = cripto.publica(priv)
    c = {"nombre": nombre, "direccion": cripto.direccion(pub), "publica": pub, "privada": priv}
    DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(c, indent=2), encoding="utf-8")
    return c


def cargar(nombre: str) -> dict:
    p = _ruta(nombre)
    if not p.exists():
        raise FileNotFoundError(f"No existe la cartera '{nombre}'. Créala con: minichain cartera nueva {nombre}")
    return json.loads(p.read_text(encoding="utf-8"))


def listar() -> list[dict]:
    if not DIR.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(DIR.glob("*.json"))]


def resolver(nombre_o_dir: str) -> str:
    """Acepta una dirección 'mc…' o el nombre de una cartera local."""
    if cripto.es_direccion(nombre_o_dir):
        return nombre_o_dir
    return cargar(nombre_o_dir)["direccion"]
