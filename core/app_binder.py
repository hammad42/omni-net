"""
core/app_binder.py — "send chrome.exe out of the TP-Link line".

Windows has no native per-application interface binding. There are exactly three
mechanisms that work, and OmniNet implements all three because each one fails on
a different class of application:

  A. Proxy + PID attribution  (default, no driver, no injection)
     The app connects to our local SOCKS5/HTTP proxy. We look up the client's
     source port in the TCP table, resolve it to a PID, then to an .exe name, and
     pick the uplink from the rule table. Zero risk. Only covers apps that honour
     a proxy (browsers, Discord, Steam downloads, curl, most updaters).

  B. Transparent capture with WinDivert  (universal for TCP, needs a driver)
     A kernel filter redirects the app's outbound TCP to the proxy, so even
     proxy-ignorant apps are covered, and the PID attribution from (A) still
     works because the TCP table still shows the app's original 5-tuple.
     Caveats: signed driver load, UDP is not covered (QUIC, most game traffic),
     and some endpoint-protection suites object.

  C. ForceBindIP  (legacy, DLL injection)
     Launches the target with BindIP.dll injected, hooking WSASocket/bind so the
     process is born bound to one source IP.
     DO NOT use this on anything with anti-cheat. Vanguard (Valorant), EAC,
     BattlEye and Ricochet all treat a foreign DLL in the process as an attack.
     At best the game refuses to start; at worst you get a hardware ban. Your
     brief listed valorant.exe — route it with (A)/(B) or a dedicated link, never
     with injection.

Note on source-IP binding in general: binding the source address alone is not
enough (see netutil.bind_socket_to_uplink). Everything here ultimately funnels
into that function, which also sets IP_UNICAST_IF.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import psutil

from .netutil import CREATE_NO_WINDOW, IS_WINDOWS
from config import AppRule, Config

log = logging.getLogger("omninet.binder")


# --------------------------------------------------------------------------- #
# A. PID attribution
# --------------------------------------------------------------------------- #
class ProcessResolver:
    """local TCP port -> (pid, exe name). Cached; psutil.net_connections() is a
    ~10-40 ms syscall and we cannot afford it on every connection."""

    def __init__(self, ttl: float = 1.0) -> None:
        self.ttl = ttl
        self._ports: Dict[int, int] = {}          # lport -> pid
        self._names: Dict[int, str] = {}          # pid -> exe
        self._stamp = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        ports: Dict[int, int] = {}
        try:
            for c in psutil.net_connections(kind="tcp"):
                if c.laddr and c.pid:
                    ports[c.laddr.port] = c.pid
        except (psutil.AccessDenied, PermissionError):
            log.warning("net_connections denied — run elevated for per-app rules")
        except Exception as e:
            log.debug("net_connections failed: %s", e)
        self._ports = ports
        self._stamp = time.time()

    def pid_for_port(self, port: int) -> Optional[int]:
        with self._lock:
            if time.time() - self._stamp > self.ttl:
                self._refresh()
            pid = self._ports.get(port)
            if pid is None:
                # A brand-new connection can pre-date our snapshot by microseconds.
                self._refresh()
                pid = self._ports.get(port)
            return pid

    def name_for_pid(self, pid: int) -> str:
        name = self._names.get(pid)
        if name:
            return name
        try:
            name = psutil.Process(pid).name().lower()
        except Exception:
            name = ""
        if name:
            if len(self._names) > 512:
                self._names.clear()
            self._names[pid] = name
        return name

    def app_for_port(self, port: int) -> Tuple[Optional[int], str]:
        pid = self.pid_for_port(port)
        if pid is None:
            return None, ""
        return pid, self.name_for_pid(pid)


# --------------------------------------------------------------------------- #
# Rule engine
# --------------------------------------------------------------------------- #
class RuleEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.resolver = ProcessResolver()
        self.hits: Dict[str, int] = {}
        self.bytes_by_app: Dict[str, int] = {}

    def target_for_client_port(self, port: int) -> Tuple[str, str]:
        """Return (uplink_name_or_auto, exe_name)."""
        _pid, exe = self.resolver.app_for_port(port)
        if not exe:
            return "auto", ""
        rule = self.cfg.rule_for(exe)
        self.hits[exe] = self.hits.get(exe, 0) + 1
        return (rule.target if rule else "auto"), exe

    def account(self, exe: str, nbytes: int) -> None:
        if exe:
            self.bytes_by_app[exe] = self.bytes_by_app.get(exe, 0) + nbytes

    # -- rule CRUD used by the UI --------------------------------------- #
    def add_rule(self, exe_path: str, target: str, note: str = "") -> AppRule:
        exe = os.path.basename(exe_path).lower()
        for r in self.cfg.rules:
            if r.exe.lower() == exe:
                r.target, r.note, r.enabled = target, note or r.note, True
                return r
        rule = AppRule(exe=exe, target=target, note=note)
        self.cfg.rules.append(rule)
        return rule

    def remove_rule(self, exe: str) -> None:
        self.cfg.rules = [r for r in self.cfg.rules if r.exe.lower() != exe.lower()]


# --------------------------------------------------------------------------- #
# C. ForceBindIP
# --------------------------------------------------------------------------- #
@dataclass
class ForceBindIP:
    exe_path: str = ""

    ANTICHEAT = ("valorant", "vanguard", "fortnite", "apex", "r5apex", "pubg",
                 "csgo", "cs2", "eac", "battleye", "bf2042", "cod", "destiny2",
                 "rustclient", "escapefromtarkov", "gameoverlayui")

    def locate(self) -> Optional[str]:
        if self.exe_path and Path(self.exe_path).exists():
            return self.exe_path
        for cand in ("ForceBindIP64.exe", "ForceBindIP.exe"):
            found = shutil.which(cand)
            if found:
                self.exe_path = found
                return found
            local = Path(__file__).resolve().parent.parent / "bin" / cand
            if local.exists():
                self.exe_path = str(local)
                return str(local)
        return None

    @classmethod
    def is_risky(cls, target_exe: str) -> bool:
        low = os.path.basename(target_exe).lower()
        return any(tag in low for tag in cls.ANTICHEAT)

    def launch(self, target_exe: str, src_ip: str, args: str = "") -> Tuple[bool, str]:
        if not IS_WINDOWS:
            return False, "Windows only"
        if self.is_risky(target_exe):
            return False, ("Refused: this executable is protected by anti-cheat. "
                           "DLL injection can trigger a ban. Use a proxy rule instead.")
        path = self.locate()
        if not path:
            return False, "ForceBindIP64.exe not found (place it in OmniNet\\bin\\)"
        import subprocess
        cmd = [path, src_ip, target_exe]
        if args:
            cmd += args.split()
        try:
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
            return True, f"Launched {os.path.basename(target_exe)} bound to {src_ip}"
        except Exception as e:
            return False, str(e)


# --------------------------------------------------------------------------- #
# B. WinDivert transparent redirector  (EXPERIMENTAL)
# --------------------------------------------------------------------------- #
class TransparentRedirector:
    """Redirects outbound TCP into the local proxy so that proxy-unaware apps
    are still routed by our rules.

    Packet flow:
        app:12345 -> 1.2.3.4:443        (captured, outbound)
        rewritten to 127.0.0.1:12345 -> 127.0.0.1:PROXY   (reinjected)
        proxy answers from 127.0.0.1:PROXY -> 127.0.0.1:12345 (captured, loopback)
        rewritten to 1.2.3.4:443 -> app_ip:12345          (reinjected inbound)

    `table` maps the app's ephemeral port to its original destination; the proxy
    reads it to learn where the client actually wanted to go.

    Limitations, stated plainly:
      * TCP only. QUIC/UDP (Chrome, most games) bypasses this entirely — either
        disable QUIC in the app or accept that UDP follows the system route.
      * IPv4 only in this implementation.
      * Requires the WinDivert driver; some AV/EDR products will block it.
    """

    def __init__(self, cfg: Config, proxy_port: int) -> None:
        self.cfg = cfg
        self.proxy_port = proxy_port
        self.table: Dict[int, Tuple[str, int]] = {}    # client port -> (orig dst ip, port)
        self._client_src: Dict[int, str] = {}          # client port -> original src ip
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.last_error = ""

    def _filter(self) -> str:
        lo, hi = self.cfg.upstream_port_low, self.cfg.upstream_port_high
        return (
            "tcp and ip and ("
            f"(outbound and ip.DstAddr != 127.0.0.1 and tcp.DstPort != {self.proxy_port} "
            f"and (tcp.SrcPort < {lo} or tcp.SrcPort > {hi})) "
            f"or (ip.SrcAddr == 127.0.0.1 and tcp.SrcPort == {self.proxy_port})"
            ")"
        )

    def start(self) -> Tuple[bool, str]:
        try:
            import pydivert  # noqa: F401
        except ImportError:
            self.last_error = "pydivert is not installed (pip install pydivert)"
            return False, self.last_error
        if self._thread and self._thread.is_alive():
            return True, "already running"
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="omninet-divert", daemon=True)
        self._thread.start()
        time.sleep(0.4)
        return self.running, (self.last_error or "started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        self.running = False
        self.table.clear()

    def _loop(self) -> None:
        import pydivert
        try:
            with pydivert.WinDivert(self._filter()) as w:
                self.running = True
                log.info("WinDivert active: %s", self._filter())
                while not self._stop.is_set():
                    try:
                        packet = w.recv()
                    except Exception as e:
                        if self._stop.is_set():
                            break
                        log.debug("divert recv: %s", e)
                        continue
                    try:
                        self._handle(packet)
                    except Exception as e:
                        log.debug("divert handle: %s", e)
                    try:
                        w.send(packet, recalculate_checksum=True)
                    except Exception as e:
                        log.debug("divert send: %s", e)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.error("WinDivert failed: %s", self.last_error)
        finally:
            self.running = False

    def _handle(self, p) -> None:
        # Return path: proxy -> client
        if p.src_addr == "127.0.0.1" and p.src_port == self.proxy_port:
            orig = self.table.get(p.dst_port)
            if not orig:
                return
            p.src_addr, p.src_port = orig[0], orig[1]
            p.dst_addr = self._client_src.get(p.dst_port, p.dst_addr)
            p.direction = 1  # inbound
            return

        # Forward path: client -> Internet
        if p.tcp and p.tcp.syn and not p.tcp.ack:
            self.table[p.src_port] = (p.dst_addr, p.dst_port)
            self._client_src[p.src_port] = p.src_addr
            if len(self.table) > 4096:
                for k in list(self.table)[:1024]:
                    self.table.pop(k, None)
                    self._client_src.pop(k, None)
        if p.src_port not in self.table:
            return
        p.src_addr = "127.0.0.1"
        p.dst_addr = "127.0.0.1"
        p.dst_port = self.proxy_port

    def original_destination(self, client_port: int) -> Optional[Tuple[str, int]]:
        return self.table.get(client_port)
