"""Reglas de consenso, estado de cuentas y mempool."""
from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from . import cripto
from .modelo import Bloque, Transaccion, raiz_merkle


class ErrorValidacion(Exception):
    pass


@dataclass(frozen=True)
class Config:
    dificultad_inicial: int = 22        # ~4M hashes de media: ~10 s en un PC normal
    dificultad_min: int = 8
    dificultad_max: int = 60
    tiempo_objetivo_ms: int = 10_000    # un bloque cada ~10 s
    ajuste_cada: int = 10               # recalcular dificultad cada N bloques
    recompensa_inicial: int = 50 * cripto.UNIDAD
    mitad_cada: int = 10_000            # halving
    max_tx_bloque: int = 500
    max_futuro_ms: int = 2 * 60_000
    comision_minima: int = 0
    madurez: int = 20                   # bloques a esperar para gastar una recompensa de minado


GENESIS_MARCA = 1_790_000_000_000  # fija: todos los nodos comparten el mismo génesis
MINERO_NULO = cripto.PREFIJO + "0" * 40


def genesis() -> Bloque:
    return Bloque(0, "0" * 64, GENESIS_MARCA, 0, MINERO_NULO, [], 0)


@dataclass
class Estado:
    saldos: dict[str, int]
    nonces: dict[str, int]  # siguiente nonce esperado por cuenta

    @classmethod
    def vacio(cls) -> "Estado":
        return cls({}, {})

    def copia(self) -> "Estado":
        return Estado(dict(self.saldos), dict(self.nonces))

    def saldo(self, d: str) -> int:
        return self.saldos.get(d, 0)

    def nonce(self, d: str) -> int:
        return self.nonces.get(d, 0)

    def aplicar_tx(self, tx: Transaccion, bloqueado: int = 0):
        """Valida y aplica una transacción normal (no coinbase). `bloqueado` es la parte
        del saldo que aún no se puede gastar (recompensas inmaduras). Lanza ErrorValidacion."""
        if tx.es_coinbase:
            raise ErrorValidacion("coinbase fuera de lugar")
        if not cripto.es_direccion(tx.a):
            raise ErrorValidacion("dirección destino inválida")
        if tx.cantidad <= 0:
            raise ErrorValidacion("la cantidad debe ser positiva")
        if tx.comision < 0:
            raise ErrorValidacion("comisión negativa")
        if tx.de == tx.a:
            raise ErrorValidacion("no puedes enviarte a ti mismo")
        if not tx.firma_valida():
            raise ErrorValidacion("firma inválida")
        esperado = self.nonce(tx.de)
        if tx.nonce != esperado:
            raise ErrorValidacion(f"nonce {tx.nonce} incorrecto (se esperaba {esperado})")
        coste = tx.cantidad + tx.comision
        disponible = self.saldo(tx.de) - bloqueado
        if disponible < coste:
            extra = f", {cripto.formato(bloqueado)} de recompensas aún inmaduras" if bloqueado else ""
            raise ErrorValidacion(f"saldo insuficiente ({cripto.formato(max(disponible, 0))} disponible "
                                  f"< {cripto.formato(coste)}{extra})")
        self.saldos[tx.de] = self.saldo(tx.de) - coste
        self.saldos[tx.a] = self.saldo(tx.a) + tx.cantidad
        self.nonces[tx.de] = esperado + 1


class Cadena:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.bloques: list[Bloque] = [genesis()]
        self.estado = Estado.vacio()
        self.indice_tx: dict[str, int] = {}  # id de tx → altura del bloque

    # ------------------------------------------------------------ consultas
    @property
    def punta(self) -> Bloque:
        return self.bloques[-1]

    @property
    def altura(self) -> int:
        return len(self.bloques) - 1

    def trabajo(self, bloques: list[Bloque] | None = None) -> int:
        return sum(1 << b.dificultad for b in (bloques or self.bloques))

    def inmaduro(self, direccion: str, previos: list[Bloque] | None = None) -> int:
        """Recompensas de minado de `direccion` que aún no se pueden gastar en el bloque
        que va después de `previos` (una recompensa del bloque h madura en h + madurez)."""
        previos = self.bloques if previos is None else previos
        m = self.config.madurez
        if m <= 0:
            return 0
        siguiente = len(previos)
        total = 0
        for b in previos[max(1, siguiente - m + 1):]:
            cb = b.transacciones[0]
            if cb.a == direccion:
                total += cb.cantidad
        return total

    def recompensa(self, altura: int) -> int:
        return self.config.recompensa_inicial >> (altura // self.config.mitad_cada)

    def dificultad_para(self, previos: list[Bloque]) -> int:
        """Dificultad exigida al bloque que va después de `previos`."""
        c = self.config
        i = len(previos)
        if i <= 1:
            return c.dificultad_inicial
        actual = previos[-1].dificultad
        if i % c.ajuste_cada != 0 or i <= c.ajuste_cada:
            return actual
        ventana = previos[-c.ajuste_cada:]
        tardado = max(ventana[-1].marca - ventana[0].marca, 1)
        esperado = c.tiempo_objetivo_ms * (c.ajuste_cada - 1)
        cambio = max(-2, min(2, round(math.log2(esperado / tardado))))
        return max(c.dificultad_min, min(c.dificultad_max, actual + cambio))

    # ------------------------------------------------------------ validación
    def validar_bloque(self, b: Bloque, previos: list[Bloque], estado: Estado,
                       ahora_ms: int | None = None) -> Estado:
        """Devuelve el nuevo estado si el bloque es válido; si no, ErrorValidacion."""
        ahora_ms = ahora_ms or int(time.time() * 1000)
        prev = previos[-1]
        if b.indice != prev.indice + 1:
            raise ErrorValidacion(f"índice {b.indice}, se esperaba {prev.indice + 1}")
        if b.previo != prev.hash:
            raise ErrorValidacion("no enlaza con el bloque anterior")
        mediana = statistics.median(x.marca for x in previos[-11:])
        if b.marca <= mediana and b.indice > 1:
            raise ErrorValidacion("marca de tiempo demasiado antigua")
        if b.marca > ahora_ms + self.config.max_futuro_ms:
            raise ErrorValidacion("marca de tiempo en el futuro")
        esperada = self.dificultad_para(previos)
        if b.dificultad != esperada:
            raise ErrorValidacion(f"dificultad {b.dificultad}, se esperaba {esperada}")
        if not b.cumple_trabajo():
            raise ErrorValidacion("prueba de trabajo insuficiente")
        if not cripto.es_direccion(b.minero):
            raise ErrorValidacion("dirección de minero inválida")
        if not b.transacciones or not b.transacciones[0].es_coinbase:
            raise ErrorValidacion("la primera transacción debe ser la recompensa (coinbase)")
        if len(b.transacciones) > self.config.max_tx_bloque + 1:
            raise ErrorValidacion("demasiadas transacciones")
        if b.raiz != raiz_merkle([t.id for t in b.transacciones]):
            raise ErrorValidacion("raíz Merkle incorrecta")
        ids = [t.id for t in b.transacciones]
        if len(set(ids)) != len(ids):
            raise ErrorValidacion("transacciones duplicadas")

        nuevo = estado.copia()
        comisiones = 0
        for tx in b.transacciones[1:]:
            if tx.id in self.indice_tx and self.indice_tx[tx.id] < b.indice:
                raise ErrorValidacion("transacción ya incluida en un bloque anterior")
            nuevo.aplicar_tx(tx, self.inmaduro(tx.de, previos))
            comisiones += tx.comision
        cb = b.transacciones[0]
        if cb.a != b.minero or cb.nonce != b.indice or cb.comision != 0:
            raise ErrorValidacion("coinbase mal formada")
        if cb.cantidad != self.recompensa(b.indice) + comisiones:
            raise ErrorValidacion("la recompensa no cuadra")
        nuevo.saldos[cb.a] = nuevo.saldo(cb.a) + cb.cantidad
        return nuevo

    def añadir(self, b: Bloque) -> None:
        self.estado = self.validar_bloque(b, self.bloques, self.estado)
        self.bloques.append(b)
        for t in b.transacciones:
            self.indice_tx[t.id] = b.indice

    def reemplazar(self, bloques: list[Bloque]) -> list[Transaccion] | None:
        """Regla de consenso: gana la cadena válida con MÁS TRABAJO acumulado.
        Devuelve None si no se reemplaza; si se reemplaza, las transacciones que estaban
        en bloques descartados (huérfanos) para devolverlas a la mempool."""
        if not bloques or bloques[0].hash != genesis().hash:
            return None
        if self.trabajo(bloques) <= self.trabajo():
            return None
        nueva = Cadena(self.config)
        try:
            for b in bloques[1:]:
                nueva.añadir(b)
        except ErrorValidacion:
            return None
        hashes_nuevos = {b.hash for b in nueva.bloques}
        huerfanas = [t for b in self.bloques if b.hash not in hashes_nuevos
                     for t in b.transacciones[1:] if t.id not in nueva.indice_tx]
        self.bloques, self.estado, self.indice_tx = nueva.bloques, nueva.estado, nueva.indice_tx
        return huerfanas

    # ------------------------------------------------------------ minería
    def plantilla(self, minero: str, candidatas: list[Transaccion], ahora_ms: int | None = None) -> Bloque:
        ahora_ms = ahora_ms or int(time.time() * 1000)
        mediana = statistics.median(x.marca for x in self.bloques[-11:])
        marca = max(ahora_ms, int(mediana) + 1)
        indice = self.altura + 1
        comisiones = sum(t.comision for t in candidatas)
        cb = Transaccion.coinbase(minero, self.recompensa(indice) + comisiones, indice, marca)
        return Bloque(indice, self.punta.hash, marca, self.dificultad_para(self.bloques), minero,
                      [cb, *candidatas])

    # ------------------------------------------------------------ consultas
    def buscar_tx(self, txid: str) -> tuple[Transaccion, int] | None:
        h = self.indice_tx.get(txid)
        if h is None:
            return None
        for t in self.bloques[h].transacciones:
            if t.id == txid:
                return t, h
        return None

    def historial(self, d: str, limite: int = 100) -> list[dict]:
        out = []
        for b in reversed(self.bloques):
            for t in b.transacciones:
                if t.de == d or t.a == d:
                    out.append({**t.dict(), "altura": b.indice, "hora": b.marca})
                    if len(out) >= limite:
                        return out
        return out

    # ------------------------------------------------------------ disco
    def guardar(self, ruta: Path) -> None:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps([b.dict() for b in self.bloques]), encoding="utf-8")
        tmp.replace(ruta)

    @classmethod
    def cargar(cls, ruta: Path, config: Config | None = None) -> "Cadena":
        c = cls(config)
        if ruta.exists():
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            bloques = [Bloque.desde(d) for d in datos]
            if len(bloques) > 1 and c.reemplazar(bloques) is None:
                raise ErrorValidacion(f"La cadena guardada en {ruta} no es válida")
        return c


class Mempool:
    """Transacciones pendientes. Solo acepta las que serían válidas encima de la
    cadena actual más las pendientes del mismo remitente (nonces consecutivos)."""

    def __init__(self, maximo: int = 5000):
        self.txs: dict[str, Transaccion] = {}
        self.maximo = maximo

    def __len__(self):
        return len(self.txs)

    # aplicar_tx comprueba todo ANTES de modificar el estado, así que un fallo no lo deja a medias.

    def estado_pendiente(self, cadena: Cadena) -> Estado:
        est = cadena.estado.copia()
        for tx in sorted(self.txs.values(), key=lambda t: (t.de, t.nonce)):
            try:
                est.aplicar_tx(tx, cadena.inmaduro(tx.de))
            except ErrorValidacion:
                pass
        return est

    def añadir(self, tx: Transaccion, cadena: Cadena) -> None:
        if tx.id in self.txs or tx.id in cadena.indice_tx:
            raise ErrorValidacion("transacción ya conocida")
        if len(self.txs) >= self.maximo:
            raise ErrorValidacion("mempool llena")
        if tx.comision < cadena.config.comision_minima:
            raise ErrorValidacion("comisión por debajo del mínimo")
        self.estado_pendiente(cadena).aplicar_tx(tx, cadena.inmaduro(tx.de))  # lanza si no encaja
        self.txs[tx.id] = tx

    def readmitir(self, txs: list[Transaccion], cadena: Cadena) -> int:
        """Devuelve a la cola las transacciones de bloques huérfanos que sigan siendo válidas."""
        n = 0
        for tx in sorted(txs, key=lambda t: (t.de, t.nonce)):
            try:
                self.añadir(tx, cadena)
                n += 1
            except ErrorValidacion:
                pass
        return n

    def seleccionar(self, cadena: Cadena) -> list[Transaccion]:
        """Las más rentables primero, respetando el orden de nonces de cada cuenta."""
        est = cadena.estado.copia()
        pendientes = sorted(self.txs.values(), key=lambda t: -t.comision)
        elegidas: list[Transaccion] = []
        usadas: set[str] = set()
        cambiado = True
        while cambiado and len(elegidas) < cadena.config.max_tx_bloque:
            cambiado = False
            for tx in pendientes:
                if tx.id in usadas:
                    continue
                try:
                    est.aplicar_tx(tx, cadena.inmaduro(tx.de))
                except ErrorValidacion:
                    continue
                elegidas.append(tx)
                usadas.add(tx.id)
                cambiado = True
                if len(elegidas) >= cadena.config.max_tx_bloque:
                    break
        return elegidas

    def purgar(self, cadena: Cadena) -> None:
        """Quita las ya minadas y las que dejaron de ser válidas (p. ej. tras una reorganización)."""
        est = cadena.estado.copia()
        vivas = {}
        for tx in sorted(self.txs.values(), key=lambda t: (t.de, t.nonce)):
            if tx.id in cadena.indice_tx:
                continue
            try:
                est.aplicar_tx(tx, cadena.inmaduro(tx.de))
                vivas[tx.id] = tx
            except ErrorValidacion:
                pass
        self.txs = vivas
