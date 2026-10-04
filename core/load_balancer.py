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
from .router_engine import RouterEngine, Uplink, UplinkSnapshot
from config import Config

log = logging.getLogger("omninet.lb")

BUF = 65536
CONNECT_TIMEOUT = 8.0
STALL_TIMEOUT = 120.0
HEDGE_DELAY = 0.3


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
        if len(bucket) > 2048:
            dead_keys = [k for k, v in bucket.items() if v.get("conns", 0) == 0]
            for k in dead_keys[:512]:
                bucket.pop(k, None)
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
        self._dns_cache: Dict[str, Tuple[str, float]] = {}

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._servers: list = []
        self.running = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if self.running:
            return True
        self._thread = threading.Thread(target=self._run, name="omninet-proxy", daemon=True)
        self._thread.start()
        for _ in range(50):
            if self.running:
                break
            time.sleep(0.05)
        return self.running

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
            self._loop.run_forever()
        except Exception as e:
            log.exception("proxy loop died: %s", e)
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                if pending:
                    for t in pending:
                        t.cancel()
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                self.running = False
                try:
                    self._loop.close()
                except Exception:
                    pass

    async def _serve(self) -> None:
        host = self.cfg.proxy_host
        specs = [(self.cfg.socks_port, self._handle_auto),
                 (self.cfg.http_port, self._handle_auto)]
        if self.cfg.transparent_mode:
            specs.append((self.cfg.transparent_port, self._handle_transparent))

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

            def _cancel_and_stop():
                try:
                    for task in asyncio.all_tasks(self._loop):
                        if not task.done():
                            task.cancel()
                except Exception:
                    pass
                self._loop.stop()

            self._loop.call_soon_threadsafe(_cancel_and_stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.running = False

    # ------------------------------------------------------------------ #
    # Upstream dialling
    # ------------------------------------------------------------------ #
    async def _resolve_host(self, host: str, port: int) -> str:
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass

        now = time.time()
        cached = self._dns_cache.get(host)
        if cached and cached[1] > now:
            return cached[0]

        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET,
                                       type=socket.SOCK_STREAM)
        if not infos:
            raise OSError(f"DNS failed for {host}")
        addr = infos[0][4][0]
        if len(self._dns_cache) > 1024:
            self._dns_cache.clear()
        self._dns_cache[host] = (addr, now + 300.0)
        return addr

    async def _open_upstream(self, uplink: Uplink | UplinkSnapshot, host: str, port: int):
        loop = asyncio.get_running_loop()
        addr = await self._resolve_host(host, port)

        u_ip = uplink.ip
        u_if_index = uplink.if_index
        if not u_ip or not u_if_index:
            raise OSError(f"Uplink {uplink.display} has no routable IP or interface index")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(False)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            src_port = self.ports.next() if self.cfg.transparent_mode else 0
            bind_socket_to_uplink(s, u_ip, u_if_index, src_port=src_port,
                                  allow_ephemeral_fallback=not self.cfg.transparent_mode)
            await asyncio.wait_for(loop.sock_connect(s, (addr, port)), CONNECT_TIMEOUT)
        except BaseException:
            s.close()
            raise
        return await asyncio.open_connection(sock=s)

    async def _dial(self, host: str, port: int, target_name: str, exe: str):
        """Pick an uplink, connect, and fail over to the next healthy link on
        error. Uses hedged dials (Happy Eyeballs across uplinks) so slow/dead links
        hedge to the next candidate after HEDGE_DELAY (300ms).
        Returns (reader, writer, uplink)."""
        with self.router._lock:
            first = self.router.select_for_new_connection(target_name)
            first_snap = first.snapshot() if first else None
            healthy_snaps = [u.snapshot() for u in self.router.healthy_uplinks()]

        order: list[UplinkSnapshot] = []
        seen: set[str] = set()
        if first_snap and first_snap.healthy and first_snap.ip:
            order.append(first_snap)
            seen.add(first_snap.name)
        for snap in healthy_snaps:
            if snap.name not in seen and snap.ip and snap.if_index:
                order.append(snap)
                seen.add(snap.name)

        if not order:
            self.stats.failed_conns += 1
            raise OSError("no healthy uplink")

        pending_tasks: dict[asyncio.Task, tuple[int, UplinkSnapshot]] = {}
        candidate_idx = 0
        last_err: Optional[Exception] = None

        async def _dial_one(snap: UplinkSnapshot):
            return await self._open_upstream(snap, host, port)

        t0 = asyncio.create_task(_dial_one(order[0]))
        pending_tasks[t0] = (0, order[0])
        candidate_idx = 1

        while pending_tasks:
            timeout = HEDGE_DELAY if candidate_idx < len(order) else CONNECT_TIMEOUT
            done, _ = await asyncio.wait(
                list(pending_tasks.keys()),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED
            )

            if done:
                for t in done:
                    idx, snap = pending_tasks.pop(t)
                    try:
                        r, w = t.result()
                        # Winner found! Cancel and close any losing tasks
                        for other_task, _ in list(pending_tasks.items()):
                            other_task.cancel()
                            try:
                                if other_task.done() and not other_task.cancelled():
                                    res = other_task.result()
                                    if res:
                                        _close(res[1])
                            except Exception:
                                pass
                        if idx > 0:
                            self.stats.failovers += 1
                            log.info("failover: %s:%s via %s (after %s)",
                                     host, port, snap.display, order[0].display)
                        live_u = self.router.uplinks.get(snap.name)
                        return r, w, live_u if live_u is not None else snap
                    except Exception as e:
                        last_err = e
                        log.debug("dial %s:%s via %s failed: %s", host, port, snap.display, e)

            # If hedged timeout expired (or candidate failed) and more candidates remain:
            if candidate_idx < len(order):
                next_snap = order[candidate_idx]
                t_next = asyncio.create_task(_dial_one(next_snap))
                pending_tasks[t_next] = (candidate_idx, next_snap)
                candidate_idx += 1

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

    async def _client_context(self, writer: asyncio.StreamWriter) -> Tuple[str, str]:
        peer = writer.get_extra_info("peername") or ("", 0)
        cport = int(peer[1]) if len(peer) > 1 else 0
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.rules.target_for_client_port, cport)

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
            raw_host = await reader.readexactly(n)
            try:
                host = raw_host.decode("idna")
            except Exception:
                host = raw_host.decode("utf-8", "ignore")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]

        target, exe = await self._client_context(writer)
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
            uri_str = target_uri.decode("ascii", "ignore")
            if uri_str.startswith("["):
                host, _, rest_p = uri_str[1:].partition("]:")
                port = int(rest_p) if rest_p.isdigit() else 443
            else:
                host, _, port_s = uri_str.rpartition(":")
                port = int(port_s) if port_s.isdigit() else 443
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

        target, exe = await self._client_context(writer)
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
        target, exe = await self._client_context(writer)
        try:
            ur, uw, uplink = await self._dial(host, port, target, exe)
        except Exception:
            _close(writer)
            return
        await self._relay(reader, writer, ur, uw, uplink, exe, f"{host}:{port}")

    # ------------------------------------------------------------------ #
    # Relay
    # ------------------------------------------------------------------ #
    async def _relay(self, cr, cw, ur, uw, uplink: Uplink | UplinkSnapshot, exe: str, label: str) -> None:
        self.stats.total_conns += 1
        self.stats.active_conns += 1
        if isinstance(uplink, Uplink):
            uplink.active_conns += 1
        self.stats.bump(self.stats.by_uplink, uplink.name, "conns")
        self.stats.bump(self.stats.by_app, exe or "unknown", "conns")

        async def pump(src, dst, direction: str) -> None:
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(src.read(BUF), timeout=STALL_TIMEOUT)
                    except asyncio.TimeoutError:
                        break
                    if not data:
                        break
                    dst.write(data)
                    try:
                        await asyncio.wait_for(dst.drain(), timeout=STALL_TIMEOUT)
                    except asyncio.TimeoutError:
                        break
                    n = len(data)
                    if direction == "up":
                        if isinstance(uplink, Uplink):
                            uplink.bytes_up += n
                        self.stats.bump(self.stats.by_uplink, uplink.name, "up", n)
                        self.stats.bump(self.stats.by_app, exe or "unknown", "up", n)
                    else:
                        if isinstance(uplink, Uplink):
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

        task_up = asyncio.create_task(pump(cr, uw, "up"))
        task_down = asyncio.create_task(pump(ur, cw, "down"))
        try:
            done, pending = await asyncio.wait([task_up, task_down], return_when=asyncio.FIRST_COMPLETED)
            if pending:
                _, still_pending = await asyncio.wait(pending, timeout=5.0)
                for t in still_pending:
                    t.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)
        finally:
            for t in (task_up, task_down):
                if not t.done():
                    t.cancel()
            self.stats.active_conns -= 1
            if isinstance(uplink, Uplink):
                uplink.active_conns = max(0, uplink.active_conns - 1)
            await asyncio.gather(_close_async(cw), _close_async(uw), return_exceptions=True)


def _close(w) -> None:
    try:
        w.close()
    except Exception:
        pass


async def _close_async(w) -> None:
    try:
        w.close()
        await w.wait_closed()
    except Exception:
        pass


def _host_from_headers(lines) -> Tuple[str, int]:
    for ln in lines[1:]:
        if ln.lower().startswith(b"host:"):
            v = ln.split(b":", 1)[1].strip().decode("ascii", "ignore")
            if v.startswith("["):
                h, _, rest = v[1:].partition("]")
                if rest.startswith(":") and rest[1:].isdigit():
                    return h, int(rest[1:])
                return h, 80
            h, _, p = v.rpartition(":")
            if h and p.isdigit():
                return h, int(p)
            return v, 80
    return "", 80
