"""
core/load_balancer.py — the multi-WAN dispatcher.

WHAT THIS CAN AND CANNOT DO — read this before you benchmark it.

Can:
  * Spread *different* TCP connections across both WAN links, weighted. Ten
    parallel downloads, a browser opening 40 sockets, a torrent client with 200
    peers: all of that sums to line1 + line2. This is real aggregation at the
    flow level and it is what dispatch-proxy, Speedify's "streaming" mode and
    every consumer "internet bonding" app actually do.
  * Pin an app to a link (see app_binder.py).
  * Retry a failed connect on the other link within milliseconds.

Cannot:
  * Make ONE TCP connection run at line1 + line2. A single TCP flow has one
    source IP; the far end replies to that IP; packets can only come back over
    the link that owns it. Splitting a single flow requires both endpoints to
    cooperate — MPTCP (needs a multipath-aware server), or a bonding VPN where a
    remote server reassembles your two streams. If you want single-flow bonding
    you need a box on the other side; there is no local-only trick, and anything
    advertising otherwise is doing per-flow balancing and calling it bonding.
    The segmented downloader (core/downloader.py) is the practical workaround:
    it turns one download into many flows so it *does* hit the combined rate.
  * Rescue an in-flight connection when its link dies. The peer is talking to
    that link's public IP. We re-dial only connections that have not yet sent
    payload.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

from .app_binder import RuleEngine, TransparentRedirector
from .netutil import bind_socket_to_uplink
from .router_engine import RouterEngine, Uplink
from config import Config

log = logging.getLogger("omninet.lb")

BUF = 65536
CONNECT_TIMEOUT = 8.0


@dataclass
class ProxyStats:
    total_conns: int = 0
    active_conns: int = 0
    failed_conns: int = 0
    failovers: int = 0
    by_uplink: dict = field(default_factory=dict)      # name -> {"conns":n,"up":b,"down":b}
    by_app: dict = field(default_factory=dict)         # exe  -> {"conns":n,"up":b,"down":b}

    def bump(self, bucket: dict, key: str, field_: str, n: int = 1) -> None:
        if not key:
            return
        d = bucket.setdefault(key, {"conns": 0, "up": 0, "down": 0})
        d[field_] += n


class SourcePortPool:
    """Outbound sockets bind to a known port range so the WinDivert filter can
    exclude our own traffic (otherwise the redirector captures the proxy's own
    upstream connections and we get an infinite loop)."""

    def __init__(self, low: int, high: int) -> None:
        self.low, self.high = low, high
        self._cycle = itertools.cycle(range(low, high + 1))
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            return next(self._cycle)


class MultiWanProxy:
    """SOCKS5 + HTTP CONNECT + transparent, all sharing one selection policy."""

    def __init__(self, cfg: Config, router: RouterEngine, rules: RuleEngine,
                 redirector: Optional[TransparentRedirector] = None) -> None:
        self.cfg = cfg
        self.router = router
        self.rules = rules
        self.redirector = redirector
        self.stats = ProxyStats()
        self.ports = SourcePortPool(cfg.upstream_port_low, cfg.upstream_port_high)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._servers: list = []
        self.running = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.running:
            return
        self._thread = threading.Thread(target=self._run, name="omninet-proxy", daemon=True)
        self._thread.start()
        for _ in range(50):
            if self.running:
                break
            time.sleep(0.05)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
            self._loop.run_forever()
        except Exception as e:
            log.exception("proxy loop died: %s", e)
        finally:
            self.running = False

    async def _serve(self) -> None:
        host = self.cfg.proxy_host
        specs = [(self.cfg.socks_port, self._handle_auto),
                 (self.cfg.http_port, self._handle_auto)]
        if self.cfg.transparent_mode:
            specs.append((self.cfg.socks_port + 1, self._handle_transparent))

        for port, handler in specs:
            try:
                srv = await asyncio.start_server(handler, host, port, backlog=512)
                self._servers.append(srv)
                log.info("listening on %s:%s", host, port)
            except OSError as e:
                log.error("cannot bind %s:%s — %s", host, port, e)
        self.running = bool(self._servers)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            for srv in self._servers:
                try:
                    srv.close()
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.running = False

    # ------------------------------------------------------------------ #
    # Upstream dialling
    # ------------------------------------------------------------------ #
    async def _open_upstream(self, uplink: Uplink, host: str, port: int):
        loop = asyncio.get_running_loop()
        try:
            ipaddress.ip_address(host)
            addr = host
        except ValueError:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET,
                                           type=socket.SOCK_STREAM)
            if not infos:
                raise OSError(f"DNS failed for {host}")
            addr = infos[0][4][0]

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(False)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            src_port = self.ports.next() if self.cfg.transparent_mode else 0
            bind_socket_to_uplink(s, uplink.ip, uplink.if_index, src_port=src_port)
            await asyncio.wait_for(loop.sock_connect(s, (addr, port)), CONNECT_TIMEOUT)
        except Exception:
            s.close()
            raise
        return await asyncio.open_connection(sock=s)

    async def _dial(self, host: str, port: int, target_name: str, exe: str):
        """Pick an uplink, connect, and fail over to the next healthy link on
        error. Returns (reader, writer, uplink)."""
        tried: set[str] = set()
        first = self.router.select_for_new_connection(target_name)
        order = []
        if first:
            order.append(first)
        for u in self.router.healthy_uplinks():
            if not first or u.name != first.name:
                order.append(u)

        last_err: Optional[Exception] = None
        for i, uplink in enumerate(order):
            if uplink.name in tried:
                continue
            tried.add(uplink.name)
            try:
                r, w = await self._open_upstream(uplink, host, port)
                if i > 0:
                    self.stats.failovers += 1
                    log.info("failover: %s:%s via %s (after %s)",
                             host, port, uplink.display, order[0].display)
                return r, w, uplink
            except Exception as e:
                last_err = e
                log.debug("dial %s:%s via %s failed: %s", host, port, uplink.display, e)
        self.stats.failed_conns += 1
        raise last_err or OSError("no healthy uplink")

    # ------------------------------------------------------------------ #
    # Protocol front ends
    # ------------------------------------------------------------------ #
    async def _handle_auto(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        """One listener speaks both SOCKS5 and HTTP — sniff the first byte."""
        try:
            head = await asyncio.wait_for(reader.read(1), 15)
        except Exception:
            writer.close()
            return
        if not head:
            writer.close()
            return
        try:
            if head[0] == 0x05:
                await self._socks5(head, reader, writer)
            else:
                await self._http(head, reader, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            log.debug("client handler error: %s", e)
        finally:
            _close(writer)

    def _client_context(self, writer: asyncio.StreamWriter) -> Tuple[str, str]:
        peer = writer.get_extra_info("peername") or ("", 0)
        return self.rules.target_for_client_port(int(peer[1]) if len(peer) > 1 else 0)

    # -- SOCKS5 ---------------------------------------------------------- #
    async def _socks5(self, head: bytes, reader, writer) -> None:
        nmethods = (await reader.readexactly(1))[0]
        await reader.readexactly(nmethods)
        writer.write(b"\x05\x00")                      # no auth
        await writer.drain()

        hdr = await reader.readexactly(4)
        ver, cmd, _rsv, atyp = hdr
        if ver != 5 or cmd != 1:                        # CONNECT only
            writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return

        if atyp == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            n = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(n)).decode("idna", "ignore")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]

        target, exe = self._client_context(writer)
        try:
            ur, uw, uplink = await self._dial(host, port, target, exe)
        except Exception:
            writer.write(b"\x05\x01\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return

        writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await writer.drain()
        await self._relay(reader, writer, ur, uw, uplink, exe, f"{host}:{port}")

    # -- HTTP ------------------------------------------------------------ #
    async def _http(self, head: bytes, reader, writer) -> None:
        raw = head + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 20)
        lines = raw.split(b"\r\n")
        try:
            method, target_uri, version = lines[0].split(b" ", 2)
        except ValueError:
            return
        method_s = method.decode("ascii", "ignore").upper()

        if method_s == "CONNECT":
            host, _, port_s = target_uri.decode().rpartition(":")
            port = int(port_s or 443)
            rest = b""
        else:
            uri = target_uri.decode("ascii", "ignore")
            if uri.startswith("http://"):
                authority, _, path = uri[7:].partition("/")
                host, _, port_s = authority.partition(":")
                port = int(port_s or 80)
                new_first = b" ".join([method, ("/" + path).encode(), version])
                rest = b"\r\n".join([new_first] + lines[1:])
            else:
                host, port = _host_from_headers(lines)
                if not host:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    return
                rest = raw

        target, exe = self._client_context(writer)
        try:
            ur, uw, uplink = await self._dial(host, port, target, exe)
        except Exception:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return

        if method_s == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            uw.write(rest)
            await uw.drain()

        await self._relay(reader, writer, ur, uw, uplink, exe, f"{host}:{port}")

    # -- transparent ----------------------------------------------------- #
    async def _handle_transparent(self, reader, writer) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        cport = int(peer[1]) if len(peer) > 1 else 0
        orig = self.redirector.original_destination(cport) if self.redirector else None
        if not orig:
            _close(writer)
            return
        host, port = orig
        target, exe = self.rules.target_for_client_port(cport)
        try:
            ur, uw, uplink = await self._dial(host, port, target, exe)
        except Exception:
            _close(writer)
            return
        await self._relay(reader, writer, ur, uw, uplink, exe, f"{host}:{port}")

    # ------------------------------------------------------------------ #
    # Relay
    # ------------------------------------------------------------------ #
    async def _relay(self, cr, cw, ur, uw, uplink: Uplink, exe: str, label: str) -> None:
        self.stats.total_conns += 1
        self.stats.active_conns += 1
        uplink.active_conns += 1
        self.stats.bump(self.stats.by_uplink, uplink.name, "conns")
        self.stats.bump(self.stats.by_app, exe or "unknown", "conns")

        async def pump(src, dst, direction: str) -> None:
            try:
                while True:
                    data = await src.read(BUF)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
                    n = len(data)
                    if direction == "up":
                        uplink.bytes_up += n
                        self.stats.bump(self.stats.by_uplink, uplink.name, "up", n)
                        self.stats.bump(self.stats.by_app, exe or "unknown", "up", n)
                    else:
                        uplink.bytes_down += n
                        self.stats.bump(self.stats.by_uplink, uplink.name, "down", n)
                        self.stats.bump(self.stats.by_app, exe or "unknown", "down", n)
            except Exception:
                pass
            finally:
                try:
                    dst.write_eof()
                except Exception:
                    pass

        try:
            await asyncio.gather(pump(cr, uw, "up"), pump(ur, cw, "down"))
        finally:
            self.stats.active_conns -= 1
            uplink.active_conns = max(0, uplink.active_conns - 1)
            _close(cw)
            _close(uw)


def _close(w) -> None:
    try:
        w.close()
    except Exception:
        pass


def _host_from_headers(lines) -> Tuple[str, int]:
    for ln in lines[1:]:
        if ln.lower().startswith(b"host:"):
            v = ln.split(b":", 1)[1].strip().decode("ascii", "ignore")
            h, _, p = v.rpartition(":")
            if h and p.isdigit():
                return h, int(p)
            return v, 80
    return "", 80
