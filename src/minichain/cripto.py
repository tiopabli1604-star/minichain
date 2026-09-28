"""Primitivas criptográficas: hashes, claves Ed25519, direcciones y cantidades."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

UNIDAD = 100_000_000  # 1 MC = 10^8 unidades (como los satoshis)
PREFIJO = "mc"


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def canon(obj) -> bytes:
    """JSON canónico: mismas claves → mismos bytes → mismo hash en todos los nodos."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def nueva_clave() -> str:
    k = Ed25519PrivateKey.generate()
    return k.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()


def publica(privada_hex: str) -> str:
    k = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privada_hex))
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def direccion(publica_hex: str) -> str:
    return PREFIJO + sha256(bytes.fromhex(publica_hex))[:40]


def es_direccion(s: str) -> bool:
    if not isinstance(s, str) or len(s) != len(PREFIJO) + 40 or not s.startswith(PREFIJO):
        return False
    try:
        int(s[len(PREFIJO):], 16)
        return True
    except ValueError:
        return False


def firmar(privada_hex: str, datos: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privada_hex)).sign(datos).hex()


def verificar(publica_hex: str, datos: bytes, firma_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(publica_hex)).verify(bytes.fromhex(firma_hex), datos)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def a_unidades(texto: str | int | float | Decimal) -> int:
    """'1.5' → 150000000. Rechaza más de 8 decimales y negativos."""
    try:
        d = Decimal(str(texto))
    except InvalidOperation:
        raise ValueError(f"Cantidad inválida: {texto!r}") from None
    if d < 0:
        raise ValueError("La cantidad no puede ser negativa")
    u = d * UNIDAD
    if u != u.to_integral_value():
        raise ValueError("Máximo 8 decimales")
    return int(u)


def formato(unidades: int) -> str:
    signo = "-" if unidades < 0 else ""
    e, f = divmod(abs(unidades), UNIDAD)
    dec = f"{f:08d}".rstrip("0")
    return f"{signo}{e}{'.' + dec if dec else ''} MC"
