import dataclasses
import time

import pytest

from minichain import cripto
from minichain.cadena import Cadena, Config, ErrorValidacion, Mempool, genesis
from minichain.modelo import Bloque, Transaccion, minar, raiz_merkle
from minichain.nodo import Nodo, pedir

U = cripto.UNIDAD
RAPIDA = Config(dificultad_inicial=6, dificultad_min=1, tiempo_objetivo_ms=1000, ajuste_cada=5, madurez=0)


def cartera():
    k = cripto.nueva_clave()
    return k, cripto.direccion(cripto.publica(k))


def minar_bloque(c: Cadena, minero: str, txs=(), marca=None):
    b = c.plantilla(minero, list(txs), marca)
    assert minar(b)
    c.añadir(b)
    return b


@pytest.fixture
def escena():
    c = Cadena(RAPIDA)
    alice, bob = cartera(), cartera()
    minar_bloque(c, alice[1])  # alice gana 50 MC
    return c, alice, bob


# ------------------------------------------------------------------ cripto

def test_direccion_y_firma():
    k, d = cartera()
    assert cripto.es_direccion(d) and not cripto.es_direccion("mcXYZ")
    f = cripto.firmar(k, b"hola")
    assert cripto.verificar(cripto.publica(k), b"hola", f)
    assert not cripto.verificar(cripto.publica(k), b"hola!", f)


def test_cantidades():
    assert cripto.a_unidades("1.5") == 150_000_000
    assert cripto.a_unidades("0.00000001") == 1
    with pytest.raises(ValueError):
        cripto.a_unidades("0.000000001")
    with pytest.raises(ValueError):
        cripto.a_unidades("-1")
    assert cripto.formato(150_000_000) == "1.5 MC"
    assert cripto.formato(5 * U) == "5 MC"


def test_merkle_cambia_con_cualquier_tx():
    a = raiz_merkle(["aa" * 32, "bb" * 32, "cc" * 32])
    b = raiz_merkle(["aa" * 32, "bb" * 32, "cd" * 32])
    assert a != b and len(a) == 64


# ------------------------------------------------------------------ transacciones

def test_transferencia_normal(escena):
    c, (ka, a), (_, b) = escena
    tx = Transaccion.nueva(ka, b, 10 * U, U // 100, 0)
    minar_bloque(c, a, [tx])
    assert c.estado.saldo(b) == 10 * U
    # alice: 50 - 10 - 0.01 + 50 (2º bloque) + 0.01 (comisión, la cobra ella como minera)
    assert c.estado.saldo(a) == 90 * U


def test_firma_falsificada_rechazada(escena):
    c, (ka, a), (kb, b) = escena
    tx = Transaccion.nueva(ka, b, 10 * U, 0, 0)
    trucada = dataclasses.replace(tx, cantidad=40 * U)  # misma firma, otra cantidad
    with pytest.raises(ErrorValidacion, match="firma"):
        c.estado.copia().aplicar_tx(trucada)
    # bob intenta gastar el dinero de alice firmando con su clave
    robo = dataclasses.replace(Transaccion.nueva(kb, b, 10 * U, 0, 0), de=a)
    with pytest.raises(ErrorValidacion, match="firma"):
        c.estado.copia().aplicar_tx(robo)


def test_doble_gasto_y_repeticion(escena):
    c, (ka, a), (_, b) = escena
    tx = Transaccion.nueva(ka, b, 30 * U, 0, 0)
    minar_bloque(c, b, [tx])
    with pytest.raises(ErrorValidacion, match="nonce"):  # repetir la misma tx
        c.estado.copia().aplicar_tx(tx)
    tx2 = Transaccion.nueva(ka, b, 30 * U, 0, 1)  # solo le quedan 20
    with pytest.raises(ErrorValidacion, match="saldo"):
        c.estado.copia().aplicar_tx(tx2)


def test_valores_raros_rechazados(escena):
    c, (ka, a), (_, b) = escena
    for mala in (Transaccion.nueva(ka, b, 0, 0, 0), Transaccion.nueva(ka, b, U, -1, 0),
                 Transaccion.nueva(ka, a, U, 0, 0), Transaccion.nueva(ka, "mcNOVALIDA", U, 0, 0)):
        with pytest.raises(ErrorValidacion):
            c.estado.copia().aplicar_tx(mala)
    with pytest.raises(ValueError):
        Transaccion.desde({**Transaccion.nueva(ka, b, U, 0, 0).dict(), "cantidad": 1.5})


# ------------------------------------------------------------------ bloques

def test_recompensa_inflada_rechazada(escena):
    c, (_, a), _ = escena
    b = c.plantilla(a, [])
    cb = dataclasses.replace(b.transacciones[0], cantidad=b.transacciones[0].cantidad + 1)
    b = Bloque(b.indice, b.previo, b.marca, b.dificultad, a, [cb])
    minar(b)
    with pytest.raises(ErrorValidacion, match="recompensa"):
        c.añadir(b)


def test_trabajo_insuficiente_y_enlace(escena):
    c, (_, a), _ = escena
    b = c.plantilla(a, [])
    while b.cumple_trabajo():
        b.nonce += 1
    with pytest.raises(ErrorValidacion, match="trabajo"):
        c.añadir(b)
    b2 = c.plantilla(a, [])
    b2.previo = "ab" * 32
    minar(b2)
    with pytest.raises(ErrorValidacion, match="enlaza"):
        c.añadir(b2)


def test_tx_alterada_dentro_de_bloque_rompe_merkle(escena):
    c, (ka, a), (_, b) = escena
    blq = c.plantilla(a, [Transaccion.nueva(ka, b, U, 0, 0)])
    minar(blq)
    d = blq.dict()
    d["transacciones"][1]["a"] = a
    with pytest.raises(ValueError, match="Merkle"):
        Bloque.desde(d)


def test_marca_futura_rechazada(escena):
    c, (_, a), _ = escena
    b = c.plantilla(a, [], ahora_ms=int(time.time() * 1000) + 10 * 60_000)
    minar(b)
    with pytest.raises(ErrorValidacion, match="futuro"):
        c.añadir(b)


def test_ajuste_de_dificultad_sube_si_va_rapido():
    c = Cadena(RAPIDA)
    _, a = cartera()
    t = genesis().marca
    for i in range(1, 11):
        minar_bloque(c, a, marca=t + i * 10)  # 10 ms por bloque: muy rápido
    assert c.dificultad_para(c.bloques) > RAPIDA.dificultad_inicial


def test_halving():
    c = Cadena(dataclasses.replace(RAPIDA, mitad_cada=3))
    assert c.recompensa(1) == 50 * U and c.recompensa(3) == 25 * U and c.recompensa(6) == 12.5 * U


# ------------------------------------------------------------------ consenso

def test_gana_la_cadena_con_mas_trabajo():
    _, a = cartera()
    _, b = cartera()
    c1, c2 = Cadena(RAPIDA), Cadena(RAPIDA)
    for _ in range(2):
        minar_bloque(c1, a)
    for _ in range(4):
        minar_bloque(c2, b)
    assert c1.reemplazar(c2.bloques) is not None
    assert c1.punta.hash == c2.punta.hash and c1.estado.saldo(a) == 0
    assert c1.reemplazar(c2.bloques[:3]) is None  # menos trabajo: se ignora


def test_reorganizacion_devuelve_tx_huerfanas_a_la_mempool():
    ka, a = cartera()
    _, b = cartera()
    comun = Cadena(RAPIDA)
    minar_bloque(comun, a)  # alice tiene 50 en ambas ramas
    rama1, rama2 = Cadena(RAPIDA), Cadena(RAPIDA)
    rama1.reemplazar(comun.bloques)
    rama2.reemplazar(comun.bloques)
    tx = Transaccion.nueva(ka, b, 7 * U, 0, 0)
    minar_bloque(rama1, a, [tx])               # la tx solo está en la rama 1
    for _ in range(3):
        minar_bloque(rama2, b)                 # la rama 2 tiene más trabajo
    huerfanas = rama1.reemplazar(rama2.bloques)
    assert [t.id for t in huerfanas] == [tx.id]
    m = Mempool()
    assert m.readmitir(huerfanas, rama1) == 1  # sigue siendo válida: vuelve a la cola
    minar_bloque(rama1, b, m.seleccionar(rama1))
    assert rama1.estado.saldo(b) >= 7 * U and rama1.buscar_tx(tx.id)


def test_recompensa_inmadura_no_se_puede_gastar():
    c = Cadena(dataclasses.replace(RAPIDA, madurez=3))
    ka, a = cartera()
    _, b = cartera()
    minar_bloque(c, a)                          # recompensa en el bloque 1
    tx = Transaccion.nueva(ka, b, U, 0, 0)
    assert c.inmaduro(a) == 50 * U
    with pytest.raises(ErrorValidacion, match="inmaduras"):
        Mempool().añadir(tx, c)
    minar_bloque(c, b)
    minar_bloque(c, b)                          # ahora el siguiente es el bloque 4 = 1 + 3
    assert c.inmaduro(a) == 0
    Mempool().añadir(tx, c)
    minar_bloque(c, b, [tx])
    assert c.estado.saldo(b) >= U


def test_cadena_invalida_no_reemplaza():
    _, a = cartera()
    c1, c2 = Cadena(RAPIDA), Cadena(RAPIDA)
    minar_bloque(c1, a)
    for _ in range(3):
        minar_bloque(c2, a)
    c2.bloques[2].nonce += 1  # rompe la prueba de trabajo de un bloque intermedio
    assert not c1.reemplazar(c2.bloques)
    assert c1.altura == 1


def test_persistencia(tmp_path, escena):
    c, (ka, a), (_, b) = escena
    minar_bloque(c, a, [Transaccion.nueva(ka, b, 5 * U, 0, 0)])
    c.guardar(tmp_path / "c.json")
    c2 = Cadena.cargar(tmp_path / "c.json", RAPIDA)
    assert c2.punta.hash == c.punta.hash and c2.estado.saldos == c.estado.saldos


# ------------------------------------------------------------------ mempool

def test_mempool_orden_de_nonces_y_comisiones(escena):
    c, (ka, a), (kb, b) = escena
    m = Mempool()
    t0 = Transaccion.nueva(ka, b, U, 1, 0)
    t1 = Transaccion.nueva(ka, b, U, 1000, 1)  # más comisión pero depende de t0
    m.añadir(t0, c)
    m.añadir(t1, c)
    with pytest.raises(ErrorValidacion):
        m.añadir(Transaccion.nueva(ka, b, U, 0, 5), c)  # nonce con hueco
    sel = m.seleccionar(c)
    assert [t.nonce for t in sel] == [0, 1]
    minar_bloque(c, a, sel)
    m.purgar(c)
    assert len(m) == 0


def test_mempool_no_permite_gastar_dos_veces(escena):
    c, (ka, a), (_, b) = escena
    m = Mempool()
    m.añadir(Transaccion.nueva(ka, b, 40 * U, 0, 0), c)
    with pytest.raises(ErrorValidacion, match="saldo"):
        m.añadir(Transaccion.nueva(ka, b, 40 * U, 0, 1), c)


# ------------------------------------------------------------------ red

def _nodo(puerto, pares=None):
    n = Nodo(f"http://127.0.0.1:{puerto}", None, RAPIDA, pares, log=lambda m: None)
    srv = n.servidor("127.0.0.1", puerto)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return n, srv


def _esperar(cond, t=20):
    fin = time.time() + t
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_dos_nodos_se_sincronizan_y_propagan():
    ka, a = cartera()
    _, b = cartera()
    n1, s1 = _nodo(28601)
    n2, s2 = _nodo(28602, ["http://127.0.0.1:28601"])
    try:
        for p in list(n2.pares):
            n2._presentarse(p)
        assert "http://127.0.0.1:28602" in n1.pares
        n1.empezar_a_minar(a)
        assert _esperar(lambda: n2.cadena.altura >= 2)
        n1.parar_de_minar()
        assert _esperar(lambda: n1.cadena.punta.hash == n2.cadena.punta.hash)

        # tx enviada al nodo 2 por HTTP → llega al nodo 1 → se mina → ambos la ven
        nonce = pedir(f"http://127.0.0.1:28602/api/cuenta/{a}")["nonce"]
        tx = Transaccion.nueva(ka, b, 3 * U, 0, nonce)
        r = pedir("http://127.0.0.1:28602/api/tx", tx.dict())
        assert r["ok"]
        assert _esperar(lambda: tx.id in n1.mempool.txs)
        n1.empezar_a_minar(a)
        assert _esperar(lambda: n2.cadena.buscar_tx(tx.id) is not None)
        n1.parar_de_minar()
        assert _esperar(lambda: n2.cadena.estado.saldo(b) == 3 * U)
        estado = pedir("http://127.0.0.1:28602/api/estado")
        assert estado["altura"] == n2.cadena.altura
    finally:
        n1.parar_de_minar()
        s1.shutdown()
        s2.shutdown()


def test_api_rechaza_basura():
    n, s = _nodo(28603)
    try:
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as e:
            pedir("http://127.0.0.1:28603/api/tx", {"de": "x"})
        assert e.value.code in (400, 422)
        assert pedir("http://127.0.0.1:28603/api/estado")["altura"] == 0
    finally:
        s.shutdown()
