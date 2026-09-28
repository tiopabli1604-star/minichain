"""minichain — blockchain propia con prueba de trabajo.

  minichain cartera nueva alice              crea una cartera (claves Ed25519)
  minichain cartera lista
  minichain nodo --minar alice               arranca un nodo en :8545 y mina para alice
  minichain nodo --puerto 8546 --pares http://127.0.0.1:8545     segundo nodo
  minichain saldo alice
  minichain enviar alice bob 12.5 --comision 0.01
  minichain tx <id>                          estado de una transacción
  minichain demo                             3 nodos + transacciones, todo automático

Explorador web: abre http://127.0.0.1:8545 en el navegador.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import urllib.error
import webbrowser
from pathlib import Path

from . import carteras, cripto
from .cadena import Config
from .modelo import Transaccion
from .nodo import Nodo, pedir

REDES = {
    "principal": Config(),
    # calibrado con un Ryzen 7 7735HS (~380k hashes/s por hilo en Python)
    "pruebas": Config(dificultad_inicial=20, dificultad_min=8, tiempo_objetivo_ms=3_000, ajuste_cada=5,
                      madurez=5),
}
DIR_DATOS = Path(__file__).resolve().parents[2] / "datos"
NODO_DEF = "http://127.0.0.1:8545"


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _api(url, datos=None):
    try:
        return pedir(url, datos)
    except urllib.error.HTTPError as e:
        try:
            import json
            msg = json.loads(e.read().decode()).get("error", str(e))
        except Exception:
            msg = str(e)
        raise SystemExit(f"✗ El nodo rechazó la petición: {msg}") from None
    except (urllib.error.URLError, OSError):
        raise SystemExit(f"✗ No hay ningún nodo en {url.split('/api')[0]}. Arráncalo con: minichain nodo") from None


# ------------------------------------------------------------------ comandos

def cmd_cartera(a):
    if a.accion == "nueva":
        if not a.nombre:
            raise SystemExit("Indica un nombre: minichain cartera nueva alice")
        c = carteras.crear(a.nombre)
        print(f"Cartera '{c['nombre']}' creada\n  dirección: {c['direccion']}\n"
              f"  guardada en {carteras.DIR}  (clave SIN cifrar: solo para uso local)")
    elif a.accion == "lista":
        cs = carteras.listar()
        if not cs:
            print("No hay carteras. Crea una con: minichain cartera nueva alice")
        for c in cs:
            print(f"  {c['nombre']:<16} {c['direccion']}")
    elif a.accion == "ver":
        c = carteras.cargar(a.nombre)
        print(f"{c['nombre']}\n  dirección: {c['direccion']}\n  clave pública: {c['publica']}")
    return 0


def cmd_saldo(a):
    d = carteras.resolver(a.quien)
    r = _api(f"{a.nodo}/api/cuenta/{d}")
    print(f"{a.quien}: {cripto.formato(r['saldo'])}")
    if r["inmaduro"]:
        print(f"  gastable ya: {cripto.formato(r['disponible'])} "
              f"({cripto.formato(r['inmaduro'])} de recompensas recientes aún madurando)")
    if r["saldo_pendiente"] != r["saldo"]:
        print(f"  con pendientes: {cripto.formato(r['saldo_pendiente'])}")
    if a.historial:
        for t in r["historial"][: a.historial]:
            signo = "+" if t["a"] == d else "-"
            otro = t["de"] if t["a"] == d else t["a"]
            print(f"  bloque {t['altura']:>6}  {signo}{cripto.formato(t['cantidad']):>18}  "
                  f"{'recompensa de minado' if t['de'] == 'COINBASE' else otro}")
    return 0


def cmd_enviar(a):
    c = carteras.cargar(a.de)
    destino = carteras.resolver(a.a)
    cuenta = _api(f"{a.nodo}/api/cuenta/{c['direccion']}")
    tx = Transaccion.nueva(c["privada"], destino, cripto.a_unidades(a.cantidad),
                           cripto.a_unidades(a.comision), cuenta["nonce"])
    r = _api(f"{a.nodo}/api/tx", tx.dict())
    print(f"✓ Enviada {cripto.formato(tx.cantidad)} de {a.de} a {a.a}\n  id: {r['id']}\n"
          "  Se confirmará cuando un minero la incluya en un bloque.")
    if a.esperar:
        print("  Esperando confirmación…", end="", flush=True)
        while True:
            time.sleep(1)
            t = _api(f"{a.nodo}/api/tx/{r['id']}")
            if t.get("confirmada"):
                print(f" ✓ en el bloque {t['altura']}")
                break
            print(".", end="", flush=True)
    return 0


def cmd_tx(a):
    t = _api(f"{a.nodo}/api/tx/{a.id}")
    estado = f"confirmada en el bloque {t['altura']}" if t["confirmada"] else "pendiente (en mempool)"
    print(f"{t['id']}\n  {t['de']} → {t['a']}\n  {cripto.formato(t['cantidad'])} "
          f"(comisión {cripto.formato(t['comision'])})\n  {estado}")
    return 0


def arrancar_nodo(puerto, host="127.0.0.1", red="principal", pares=None, datos=None,
                  minar_para=None, log=print, publico=None) -> tuple[Nodo, object]:
    url = publico or f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{puerto}"
    nodo = Nodo(url, datos, REDES[red], pares, log=log)
    srv = nodo.servidor(host, puerto)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for p in list(nodo.pares):
        nodo.pool.submit(nodo._presentarse, p)
    if minar_para:
        nodo.empezar_a_minar(minar_para)
    return nodo, srv


def cmd_nodo(a):
    datos = Path(a.datos) if a.datos else DIR_DATOS / f"{a.red}-{a.puerto}"
    minero = carteras.resolver(a.minar) if a.minar else None
    nodo, _ = arrancar_nodo(a.puerto, a.host, a.red, a.pares, datos, minero, publico=a.publico)
    print(f"Nodo minichain ({a.red}) en {nodo.url}")
    print(f"  cadena: altura {nodo.cadena.altura} · datos en {datos}")
    print(f"  explorador: http://127.0.0.1:{a.puerto}")
    if minero:
        print(f"  minando para {a.minar} ({minero})")
    if a.pares:
        print(f"  pares: {', '.join(a.pares)}")
    print("Ctrl+C para parar.\n")
    if a.abrir:
        webbrowser.open(f"http://127.0.0.1:{a.puerto}")
    try:
        while True:
            time.sleep(30)
            nodo.sincronizar()
    except KeyboardInterrupt:
        nodo.parar_de_minar()
        print("\nNodo detenido.")
    return 0


def cmd_demo(a):
    """Tres nodos en la misma máquina, dos mineros compitiendo, transacciones entre carteras."""
    import tempfile
    base = 18545
    tmp = Path(tempfile.mkdtemp(prefix="minichain-demo-"))
    nombres = ["minero1", "minero2", "alice", "bob"]
    cs = {}
    for n in nombres:
        priv = cripto.nueva_clave()
        cs[n] = {"privada": priv, "direccion": cripto.direccion(cripto.publica(priv))}

    def log_de(nombre):
        return lambda m: print(f"  [{nombre}] {m}")

    urls = [f"http://127.0.0.1:{base + i}" for i in range(3)]
    n1, _ = arrancar_nodo(base, red="pruebas", datos=tmp / "n1", log=log_de("nodo1"),
                          minar_para=cs["minero1"]["direccion"])
    n2, _ = arrancar_nodo(base + 1, red="pruebas", pares=[urls[0]], datos=tmp / "n2",
                          log=log_de("nodo2"), minar_para=cs["minero2"]["direccion"])
    n3, _ = arrancar_nodo(base + 2, red="pruebas", pares=[urls[1]], datos=tmp / "n3", log=log_de("nodo3"))
    print(f"3 nodos en marcha ({', '.join(urls)}). Explorador: {urls[2]}\n")
    if a.abrir:
        webbrowser.open(urls[2])

    def saldo(nodo, quien):
        return nodo.cadena.estado.saldo(cs[quien]["direccion"])

    print("Esperando a que minero1 tenga fondos gastables (las recompensas maduran en 5 bloques)…")
    while n1.cuenta(cs["minero1"]["direccion"])["disponible"] < 20 * cripto.UNIDAD:
        time.sleep(0.5)

    def enviar(de, a_, cantidad, nodo):
        cuenta = nodo.cuenta(cs[de]["direccion"])
        tx = Transaccion.nueva(cs[de]["privada"], cs[a_]["direccion"], cripto.a_unidades(cantidad),
                               cripto.a_unidades("0.01"), cuenta["nonce"])
        nodo.recibir_tx(tx.dict())
        print(f"\n→ {de} envía {cantidad} MC a {a_} (vía {nodo.url})")
        return tx

    enviar("minero1", "alice", "15", n1)
    tx = None
    t0 = time.time()
    while time.time() - t0 < 180:
        time.sleep(0.5)
        if tx is None and n3.cuenta(cs["alice"]["direccion"])["disponible"] >= 5 * cripto.UNIDAD:
            tx = enviar("alice", "bob", "4.5", n3)
        if tx and n3.cadena.buscar_tx(tx.id):
            break

    # Dos mineros a la vez pueden dejar un empate temporal (dos puntas con el mismo trabajo).
    # Como en Bitcoin, el siguiente bloque lo desempata: paramos minero2 y dejamos que minero1 mine uno más.
    print("\nParando minero2 para que el siguiente bloque de minero1 deshaga cualquier empate…")
    n2.parar_de_minar()
    objetivo = max(n.cadena.altura for n in (n1, n2, n3)) + 1
    while n1.cadena.altura < objetivo:
        time.sleep(0.2)
    n1.parar_de_minar()
    fin = time.time() + 15
    while time.time() < fin and len({n.cadena.punta.hash for n in (n1, n2, n3)}) > 1:
        for n in (n2, n3):
            n.sincronizar()
        time.sleep(0.5)
    print("\n── Resultado (visto desde cada nodo) ──")
    for i, n in enumerate((n1, n2, n3), 1):
        print(f"nodo{i}: altura {n.cadena.altura}, punta {n.cadena.punta.hash[:12]}…")
    print()
    for q in nombres:
        print(f"  {q:<8} {cripto.formato(saldo(n3, q)):>20}")
    iguales = len({n.cadena.punta.hash for n in (n1, n2, n3)}) == 1
    print("\n" + ("✓ Los tres nodos están de acuerdo (consenso)." if iguales else
                  "… los nodos aún están sincronizando la última punta (normal con dos mineros)."))
    if a.abrir:
        print("\nLa demo sigue corriendo para que explores. Ctrl+C para salir.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    for n in (n1, n2):
        n.parar_de_minar()
    return 0


def construir_parser():
    ap = argparse.ArgumentParser(prog="minichain", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("cartera", help="crear/listar carteras")
    p.add_argument("accion", choices=["nueva", "lista", "ver"])
    p.add_argument("nombre", nargs="?")
    p.set_defaults(func=cmd_cartera)

    p = sub.add_parser("nodo", help="arrancar un nodo")
    p.add_argument("--puerto", type=int, default=8545)
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para aceptar otros PCs de tu red")
    p.add_argument("--publico", help="URL con la que te ven los demás (p. ej. http://192.168.1.20:8545)")
    p.add_argument("--red", choices=list(REDES), default="principal")
    p.add_argument("--pares", nargs="*", default=[], help="URLs de otros nodos")
    p.add_argument("--minar", metavar="CARTERA", help="minar y cobrar en esta cartera/dirección")
    p.add_argument("--datos", help="carpeta donde guardar la cadena")
    p.add_argument("--abrir", action="store_true", help="abrir el explorador")
    p.set_defaults(func=cmd_nodo)

    p = sub.add_parser("saldo")
    p.add_argument("quien", help="nombre de cartera o dirección")
    p.add_argument("--historial", type=int, nargs="?", const=10, default=0)
    p.add_argument("--nodo", default=NODO_DEF)
    p.set_defaults(func=cmd_saldo)

    p = sub.add_parser("enviar")
    p.add_argument("de", help="cartera que paga")
    p.add_argument("a", help="cartera o dirección destino")
    p.add_argument("cantidad")
    p.add_argument("--comision", default="0.001")
    p.add_argument("--esperar", action="store_true", help="esperar a que se confirme")
    p.add_argument("--nodo", default=NODO_DEF)
    p.set_defaults(func=cmd_enviar)

    p = sub.add_parser("tx")
    p.add_argument("id")
    p.add_argument("--nodo", default=NODO_DEF)
    p.set_defaults(func=cmd_tx)

    p = sub.add_parser("demo", help="3 nodos y transacciones automáticas")
    p.add_argument("--abrir", action="store_true", help="abrir explorador y dejarlo corriendo")
    p.set_defaults(func=cmd_demo)
    return ap


def main(argv=None) -> int:
    _utf8()
    a = construir_parser().parse_args(argv)
    try:
        return a.func(a)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        raise SystemExit(f"✗ {e}") from None


if __name__ == "__main__":
    raise SystemExit(main())
