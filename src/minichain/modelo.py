"""Transacciones y bloques."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from . import cripto

COINBASE = "COINBASE"


@dataclass(frozen=True)
class Transaccion:
    de: str
    a: str
    cantidad: int
    comision: int
    nonce: int
    marca: int
    clave_publica: str = ""
    firma: str = ""

    # ---------- construcción
    @classmethod
    def nueva(cls, privada: str, a: str, cantidad: int, comision: int, nonce: int) -> "Transaccion":
        pub = cripto.publica(privada)
        tx = cls(cripto.direccion(pub), a, int(cantidad), int(comision), int(nonce),
                 int(time.time() * 1000), pub)
        return cls(**{**asdict(tx), "firma": cripto.firmar(privada, tx.datos_firmados())})

    @classmethod
    def coinbase(cls, minero: str, cantidad: int, altura: int, marca: int) -> "Transaccion":
        return cls(COINBASE, minero, cantidad, 0, altura, marca)

    # ---------- identidad
    def datos_firmados(self) -> bytes:
        d = asdict(self)
        d.pop("firma")
        return cripto.canon(d)

    @property
    def id(self) -> str:
        return cripto.sha256(cripto.canon(asdict(self)))

    @property
    def es_coinbase(self) -> bool:
        return self.de == COINBASE

    def firma_valida(self) -> bool:
        if self.es_coinbase:
            return False
        return (bool(self.clave_publica) and cripto.direccion(self.clave_publica) == self.de
                and cripto.verificar(self.clave_publica, self.datos_firmados(), self.firma))

    # ---------- serialización
    def dict(self) -> dict:
        return {**asdict(self), "id": self.id}

    @classmethod
    def desde(cls, d: dict) -> "Transaccion":
        campos = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        tx = cls(**campos)
        for k in ("cantidad", "comision", "nonce", "marca"):
            if not isinstance(getattr(tx, k), int) or isinstance(getattr(tx, k), bool):
                raise ValueError(f"Campo {k} debe ser entero")
        for k in ("de", "a", "clave_publica", "firma"):
            if not isinstance(getattr(tx, k), str):
                raise ValueError(f"Campo {k} debe ser texto")
        return tx


def raiz_merkle(ids: list[str]) -> str:
    if not ids:
        return cripto.sha256(b"")
    nivel = ids[:]
    while len(nivel) > 1:
        if len(nivel) % 2:
            nivel.append(nivel[-1])
        nivel = [cripto.sha256(bytes.fromhex(nivel[i]) + bytes.fromhex(nivel[i + 1]))
                 for i in range(0, len(nivel), 2)]
    return nivel[0]


@dataclass
class Bloque:
    indice: int
    previo: str
    marca: int
    dificultad: int           # bits a cero exigidos al principio del hash
    minero: str
    transacciones: list[Transaccion] = field(default_factory=list)
    nonce: int = 0
    raiz: str = ""

    def __post_init__(self):
        if not self.raiz:
            self.raiz = raiz_merkle([t.id for t in self.transacciones])

    def prefijo(self) -> bytes:
        """Cabecera sin nonce: se calcula una vez y se reutiliza al minar."""
        return cripto.canon({"indice": self.indice, "previo": self.previo, "marca": self.marca,
                             "dificultad": self.dificultad, "minero": self.minero, "raiz": self.raiz}) + b"|"

    @property
    def hash(self) -> str:
        return cripto.sha256(self.prefijo() + str(self.nonce).encode())

    def cumple_trabajo(self) -> bool:
        return cumple(self.hash, self.dificultad)

    def dict(self, con_tx: bool = True) -> dict:
        d = {"indice": self.indice, "previo": self.previo, "marca": self.marca,
             "dificultad": self.dificultad, "minero": self.minero, "nonce": self.nonce,
             "raiz": self.raiz, "hash": self.hash, "num_tx": len(self.transacciones)}
        if con_tx:
            d["transacciones"] = [t.dict() for t in self.transacciones]
        return d

    @classmethod
    def desde(cls, d: dict) -> "Bloque":
        txs = [Transaccion.desde(t) for t in d.get("transacciones", [])]
        b = cls(int(d["indice"]), str(d["previo"]), int(d["marca"]), int(d["dificultad"]),
                str(d["minero"]), txs, int(d["nonce"]), str(d.get("raiz", "")))
        if b.raiz != raiz_merkle([t.id for t in txs]):
            raise ValueError("La raíz Merkle no coincide con las transacciones")
        return b


def cumple(hash_hex: str, dificultad: int) -> bool:
    return int(hash_hex, 16) < (1 << (256 - dificultad))


def minar(bloque: Bloque, parar=None, cada: int = 20_000) -> bool:
    """Busca un nonce válido. `parar()` se consulta cada `cada` intentos (p. ej. si
    llegó un bloque nuevo por la red). Devuelve True si lo encontró."""
    import hashlib
    objetivo = 1 << (256 - bloque.dificultad)
    base = hashlib.sha256(bloque.prefijo())
    nonce = bloque.nonce
    while True:
        h = base.copy()
        h.update(str(nonce).encode())
        if int.from_bytes(h.digest(), "big") < objetivo:
            bloque.nonce = nonce
            return True
        nonce += 1
        if parar and nonce % cada == 0 and parar():
            bloque.nonce = nonce
            return False
