"""Nodo de red: API HTTP, propagación (gossip) a pares, sincronización y minero."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import cripto
from .cadena import Cadena, Config, ErrorValidacion, Mempool
from .modelo import Bloque, Transaccion, minar

MAX_CUERPO = 8 * 1024 * 1024
HTML = Path(__file__).with_name("explorador.html")


def pedir(url: str, datos: dict | None = None, timeout: float = 5.0):
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    req = urllib.request.Request(url, data=cuerpo, method="POST" if cuerpo else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class Nodo:
    def __init__(self, url_propia: str, datos: Path | None = None, config: Config | None = None,
                 pares: list[str] | None = None, log=print):
        self.url = url_propia.rstrip("/")
        self.ruta = datos / "cadena.json" if datos else None
        self.cadena = Cadena.cargar(self.ruta, config) if self.ruta else Cadena(config)
        self.mempool = Mempool()
        self.pares: set[str] = {p.rstrip("/") for p in pares or [] if p.rstrip("/") != self.url}
        self.cerrojo = threading.RLock()
        self.log = log
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.minando = threading.Event()
        self.version = 0  # sube con cada cambio de punta: interrumpe al minero
        self.hilo_minero: threading.Thread | None = None
        self.bloques_minados = 0

    # ---------------------------------------------------------------- gossip
    def difundir(self, ruta: str, datos: dict, excepto: str | None = None):
        for p in list(self.pares):
            if p != excepto:
                self.pool.submit(self._enviar, p, ruta, datos)

    def _enviar(self, par: str, ruta: str, datos: dict):
        try:
            pedir(par + ruta, {**datos, "origen": self.url}, timeout=5)
        except (urllib.error.URLError, OSError, ValueError):
            pass  # par caído: ya se sincronizará

    def conectar(self, par: str) -> bool:
        par = par.rstrip("/")
        if not par or par == self.url:
            return False
        nuevo = par not in self.pares
        self.pares.add(par)
        if nuevo:
            self.pool.submit(self._presentarse, par)
        return nuevo

    def _presentarse(self, par: str):
        try:
            r = pedir(par + "/api/pares", {"url": self.url})
            for otro in r.get("pares", []):
                if otro != self.url and otro not in self.pares:
                    self.pares.add(otro)
        except (urllib.error.URLError, OSError, ValueError):
            return
        self.sincronizar_con(par)

    def sincronizar_con(self, par: str) -> bool:
        try:
            est = pedir(par + "/api/estado")
            if int(est["trabajo"]) <= self.cadena.trabajo():
                return False
            datos = pedir(par + "/api/cadena", timeout=60)
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return False
        try:
            bloques = [Bloque.desde(d) for d in datos]
        except (ValueError, KeyError, TypeError):
            return False
        with self.cerrojo:
            huerfanas = self.cadena.reemplazar(bloques)
            if huerfanas is None:
                return False
            self._punta_cambiada()
            vueltas = self.mempool.readmitir(huerfanas, self.cadena)
            extra = f" · {vueltas} tx de bloques huérfanos vuelven a la cola" if vueltas else ""
            self.log(f"⇄ cadena adoptada de {par}: altura {self.cadena.altura}{extra}")
        return True

    def sincronizar(self):
        for p in list(self.pares):
            self.sincronizar_con(p)

    # ---------------------------------------------------------------- entrada
    def recibir_tx(self, d: dict, origen: str | None = None) -> Transaccion:
        tx = Transaccion.desde(d)
        with self.cerrojo:
            self.mempool.añadir(tx, self.cadena)
        self.difundir("/api/tx", tx.dict(), excepto=origen)
        return tx

    def recibir_bloque(self, d: dict, origen: str | None = None) -> str:
        b = Bloque.desde(d)
        with self.cerrojo:
            if b.indice <= self.cadena.altura and self.cadena.bloques[b.indice].hash == b.hash:
                return "conocido"
            if b.previo == self.cadena.punta.hash:
                self.cadena.añadir(b)  # lanza ErrorValidacion si es inválido
                self._punta_cambiada()
                resultado = "añadido"
            elif b.indice > self.cadena.altura:
                resultado = "bifurcación"
            else:
                return "antiguo"
        if resultado == "bifurcación":
            # nos faltan bloques o hay otra rama: pedir la cadena completa al que nos lo mandó
            if origen:
                self.pool.submit(self.sincronizar_con, origen)
            return resultado
        self.log(f"▣ bloque {b.indice} recibido ({len(b.transacciones) - 1} tx)")
        self.difundir("/api/bloque", b.dict(), excepto=origen)
        return resultado

    def _punta_cambiada(self):
        self.version += 1
        self.mempool.purgar(self.cadena)
        if self.ruta:
            self.cadena.guardar(self.ruta)

    # ---------------------------------------------------------------- minero
    def empezar_a_minar(self, direccion: str):
        if self.hilo_minero and self.hilo_minero.is_alive():
            return
        self.minando.set()
        self.hilo_minero = threading.Thread(target=self._bucle_minero, args=(direccion,), daemon=True)
        self.hilo_minero.start()

    def parar_de_minar(self):
        self.minando.clear()

    def _bucle_minero(self, direccion: str):
        while self.minando.is_set():
            with self.cerrojo:
                version = self.version
                b = self.cadena.plantilla(direccion, self.mempool.seleccionar(self.cadena))
            t0 = time.time()
            ok = minar(b, parar=lambda: self.version != version or not self.minando.is_set())
            if not ok:
                continue
            with self.cerrojo:
                if self.version != version:
                    continue
                try:
                    self.cadena.añadir(b)
                except ErrorValidacion as e:
                    self.log(f"✗ bloque propio inválido: {e}")
                    continue
                self._punta_cambiada()
                self.bloques_minados += 1
            self.log(f"⛏ bloque {b.indice} minado en {time.time() - t0:.1f}s · dificultad "
                     f"{b.dificultad} · {len(b.transacciones) - 1} tx · {b.hash[:16]}…")
            self.difundir("/api/bloque", b.dict())

    # ---------------------------------------------------------------- vistas
    def estado(self) -> dict:
        with self.cerrojo:
            c = self.cadena
            return {"url": self.url, "altura": c.altura, "punta": c.punta.hash,
                    "dificultad": c.dificultad_para(c.bloques), "trabajo": str(c.trabajo()),
                    "mempool": len(self.mempool), "pares": sorted(self.pares),
                    "minando": self.minando.is_set(), "recompensa": c.recompensa(c.altura + 1),
                    "cuentas": len(c.estado.saldos),
                    "circulante": sum(c.estado.saldos.values())}

    def cuenta(self, d: str) -> dict:
        with self.cerrojo:
            pend = self.mempool.estado_pendiente(self.cadena)
            inmaduro = self.cadena.inmaduro(d)
            return {"direccion": d, "saldo": self.cadena.estado.saldo(d), "inmaduro": inmaduro,
                    "disponible": self.cadena.estado.saldo(d) - inmaduro,
                    "saldo_pendiente": pend.saldo(d), "nonce": pend.nonce(d),
                    "historial": self.cadena.historial(d)}

    # ---------------------------------------------------------------- servidor
    def servidor(self, host: str, puerto: int) -> ThreadingHTTPServer:
        nodo = self

        class Manejador(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, codigo=200):
                cuerpo = json.dumps(obj).encode()
                self.send_response(codigo)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(cuerpo)

            def _error(self, msg, codigo=400):
                self._json({"error": msg}, codigo)

            def do_GET(self):
                u = urlparse(self.path)
                partes = [p for p in u.path.split("/") if p]
                q = parse_qs(u.query)
                try:
                    if not partes:
                        cuerpo = HTML.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(cuerpo)))
                        self.end_headers()
                        self.wfile.write(cuerpo)
                        return
                    if partes[0] != "api":
                        return self._error("no encontrado", 404)
                    ruta = partes[1:]
                    if ruta == ["estado"]:
                        return self._json(nodo.estado())
                    if ruta == ["cadena"]:
                        with nodo.cerrojo:
                            return self._json([b.dict() for b in nodo.cadena.bloques])
                    if ruta == ["bloques"]:
                        n = min(int(q.get("n", ["20"])[0]), 200)
                        with nodo.cerrojo:
                            hasta = int(q.get("hasta", [nodo.cadena.altura])[0])
                            sel = nodo.cadena.bloques[max(0, hasta - n + 1): hasta + 1]
                            return self._json([b.dict(con_tx=False) for b in reversed(sel)])
                    if len(ruta) == 2 and ruta[0] == "bloque":
                        with nodo.cerrojo:
                            clave = ruta[1]
                            if clave.isdigit() and int(clave) <= nodo.cadena.altura:
                                return self._json(nodo.cadena.bloques[int(clave)].dict())
                            for b in nodo.cadena.bloques:
                                if b.hash == clave:
                                    return self._json(b.dict())
                        return self._error("bloque no encontrado", 404)
                    if len(ruta) == 2 and ruta[0] == "tx":
                        with nodo.cerrojo:
                            r = nodo.cadena.buscar_tx(ruta[1])
                            if r:
                                return self._json({**r[0].dict(), "altura": r[1], "confirmada": True})
                            t = nodo.mempool.txs.get(ruta[1])
                            if t:
                                return self._json({**t.dict(), "confirmada": False})
                        return self._error("transacción no encontrada", 404)
                    if len(ruta) == 2 and ruta[0] == "cuenta":
                        if not cripto.es_direccion(ruta[1]):
                            return self._error("dirección inválida")
                        return self._json(nodo.cuenta(ruta[1]))
                    if ruta == ["mempool"]:
                        with nodo.cerrojo:
                            return self._json([t.dict() for t in nodo.mempool.txs.values()])
                    if ruta == ["ricos"]:
                        with nodo.cerrojo:
                            top = sorted(nodo.cadena.estado.saldos.items(), key=lambda kv: -kv[1])[:20]
                        return self._json([{"direccion": d, "saldo": s} for d, s in top])
                    return self._error("no encontrado", 404)
                except (ValueError, IndexError) as e:
                    return self._error(str(e))

            def do_POST(self):
                largo = int(self.headers.get("Content-Length", 0))
                if largo > MAX_CUERPO:
                    return self._error("cuerpo demasiado grande", 413)
                try:
                    d = json.loads(self.rfile.read(largo) or b"{}")
                except json.JSONDecodeError:
                    return self._error("JSON inválido")
                origen = d.pop("origen", None) if isinstance(d, dict) else None
                if origen:
                    nodo.conectar(origen)
                ruta = urlparse(self.path).path.rstrip("/")
                try:
                    if ruta == "/api/tx":
                        tx = nodo.recibir_tx(d, origen)
                        return self._json({"ok": True, "id": tx.id})
                    if ruta == "/api/bloque":
                        return self._json({"ok": True, "resultado": nodo.recibir_bloque(d, origen)})
                    if ruta == "/api/pares":
                        if d.get("url"):
                            nodo.conectar(str(d["url"]))
                        return self._json({"pares": sorted(nodo.pares)})
                    return self._error("no encontrado", 404)
                except ErrorValidacion as e:
                    return self._error(str(e), 422)
                except (ValueError, KeyError, TypeError) as e:
                    return self._error(f"datos inválidos: {e}")

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

        return ThreadingHTTPServer((host, puerto), Manejador)
