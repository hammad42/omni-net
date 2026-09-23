"""
core/router_engine.py — health monitoring, uplink state, failover.

Why TCP probes instead of ping:
  `ping -S <src>` sets the source address but not the egress interface, so under
  weak-host-send the probe can leave via the wrong NIC and report a dead link as
  alive (or vice versa). A TCP connect() with IP_UNICAST_IF is pinned to the
  wire, needs no raw-socket privilege, measures handshake RTT, and proves the
  path all the way to a real service on :443 — not just to the first router.

Failover honesty:
  Changing metrics redirects *new* connections. Sockets already established
  through the dead NIC cannot survive: the remote peer is talking to that NIC's
  public IP, and no amount of local routing changes that. Sub-500 ms here means
  "new connections are on the healthy link within 500 ms". Existing flows are
  rescued only for traffic that goes through our proxy (see load_balancer.py),
  where we can re-dial upstream transparently, and only for connections that have
  not yet streamed a body.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .adapter_manager import Adapter, AdapterManager
from .netutil import bind_socket_to_uplink
from config import Config, MODE_AGGREGATE, MODE_AUTO, MODE_FORCE

log = logging.getLogger("omninet.router")

STATE_UP = "up"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"


@dataclass
class Uplink:
    adapter: Adapter
    weight: int = 100
    enabled: bool = True
    friendly: str = ""

    state: str = STATE_UNKNOWN
    rtt_ms: float = 0.0
    loss_pct: float = 0.0
    consecutive_ok: int = 0
    consecutive_fail: int = 0
    last_probe_ts: float = 0.0
    history: List[bool] = field(default_factory=list)

    # live counters (maintained by the load balancer)
    active_conns: int = 0
    bytes_up: int = 0
    bytes_down: int = 0

    @property
    def name(self) -> str:
        return self.adapter.name

    @property
    def ip(self) -> str:
        return self.adapter.ip

    @property
    def if_index(self) -> int:
        return self.adapter.if_index

    @property
    def healthy(self) -> bool:
        return self.enabled and self.state == STATE_UP and bool(self.ip)

    @property
    def display(self) -> str:
        return self.friendly or self.adapter.name


# --------------------------------------------------------------------------- #
def tcp_probe(host: str, port: int, src_ip: str, if_index: int,
              timeout: float = 1.5) -> Optional[float]:
    """Return handshake RTT in ms, or None on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        bind_socket_to_uplink(s, src_ip, if_index)
        t0 = time.perf_counter()
        s.connect((host, port))
        return (time.perf_counter() - t0) * 1000.0
    except OSError:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


class RouterEngine:
    """Owns uplink state. Runs one monitor thread; probes run in parallel so a
    dead link's timeout never delays a healthy link's result."""

    def __init__(self, cfg: Config, adapters: AdapterManager) -> None:
        self.cfg = cfg
        self.adapters = adapters
        self.uplinks: Dict[str, Uplink] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._rr_state: Dict[str, int] = {}
        self._last_applied: Dict[str, int] = {}

        self.on_change: Optional[Callable[[], None]] = None
        self.on_event: Optional[Callable[[str, str], None]] = None   # (level, message)

    # ------------------------------------------------------------------ #
    def log_event(self, level: str, msg: str) -> None:
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(msg)
        if self.on_event:
            try:
                self.on_event(level, msg)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Uplink discovery
    # ------------------------------------------------------------------ #
    def sync_uplinks(self) -> None:
        self.adapters.refresh()
        with self._lock:
            live = {}
            for a in self.adapters.physical():
                uc = self.cfg.uplink(a.name)
                existing = self.uplinks.get(a.name)
                if existing:
                    existing.adapter = a
                    existing.weight = uc.weight
                    existing.enabled = uc.enabled
                    existing.friendly = uc.friendly
                    if not a.is_routable and existing.state != STATE_DOWN:
                        existing.state = STATE_DOWN
                        existing.rtt_ms = 0.0
                    live[a.name] = existing
                else:
                    up_init = Uplink(adapter=a, weight=uc.weight,
                                     enabled=uc.enabled, friendly=uc.friendly)
                    if not a.is_routable:
                        up_init.state = STATE_DOWN
                    live[a.name] = up_init
            gone = set(self.uplinks) - set(live)
            for name in gone:
                self.log_event("warning", f"Adapter removed: {name}")
            added = set(live) - set(self.uplinks)
            for name in added:
                self.log_event("info", f"Adapter detected: {name}")
            changed = bool(gone or added)
            self.uplinks = live
        if changed and self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def healthy_uplinks(self) -> List[Uplink]:
        with self._lock:
            return [u for u in self.uplinks.values()
                    if u.healthy and u.adapter.is_routable]

    def candidates(self) -> List[Uplink]:
        """Enabled links with an address, healthy or not (UI shows all)."""
        with self._lock:
            return [u for u in self.uplinks.values()
                    if u.enabled and u.adapter.is_routable]

    # ------------------------------------------------------------------ #
    # Probing
    # ------------------------------------------------------------------ #
    def _probe_uplink(self, u: Uplink) -> None:
        results: List[Optional[float]] = []
        for target in self.cfg.probe_targets:
            host, _, port = target.partition(":")
            rtt = tcp_probe(host.strip(), int(port or 443), u.ip, u.if_index,
                            self.cfg.probe_timeout_s)
            results.append(rtt)
            if rtt is not None:
                break          # one success is enough; don't burn the timeout

        ok = any(r is not None for r in results)
        good = [r for r in results if r is not None]

        with self._lock:
            u.last_probe_ts = time.time()
            u.history.append(ok)
            if len(u.history) > 20:
                u.history.pop(0)
            u.loss_pct = 100.0 * (1 - (sum(u.history) / len(u.history)))
            if ok:
                u.rtt_ms = round(min(good), 1)
                u.consecutive_ok += 1
                u.consecutive_fail = 0
                if u.state != STATE_UP and u.consecutive_ok >= self.cfg.recover_threshold:
                    u.state = STATE_UP
                    self.log_event("info", f"{u.display}: link UP ({u.rtt_ms} ms)")
                elif u.state == STATE_UNKNOWN:
                    u.state = STATE_UP
            else:
                u.consecutive_fail += 1
                u.consecutive_ok = 0
                if u.state != STATE_DOWN and u.consecutive_fail >= self.cfg.fail_threshold:
                    u.state = STATE_DOWN
                    u.rtt_ms = 0.0
                    self.log_event("warning", f"{u.display}: link DOWN")

    def probe_round(self) -> None:
        ups = self.candidates()
        threads = [threading.Thread(target=self._probe_uplink, args=(u,), daemon=True)
                   for u in ups]
        for t in threads:
            t.start()
        deadline = time.time() + self.cfg.probe_timeout_s * 2 + 0.5
        for t in threads:
            t.join(max(0.05, deadline - time.time()))

    # ------------------------------------------------------------------ #
    # Route policy
    # ------------------------------------------------------------------ #
    def preferred_uplink(self) -> Optional[Uplink]:
        """The link the *system* default route should point at."""
        healthy = self.healthy_uplinks()
        if not healthy:
            return None

        if self.cfg.mode == MODE_FORCE and self.cfg.forced_adapter:
            forced = self.uplinks.get(self.cfg.forced_adapter)
            if forced and forced.healthy:
                return forced
            # forced link is dead -> fall through to auto rather than go offline
            self.log_event("warning",
                           f"Forced adapter {self.cfg.forced_adapter} unavailable; using failover")

        def rank(u: Uplink):
            wired = 0 if not u.adapter.is_wifi else 1
            return (wired, -u.weight, u.rtt_ms or 9999)

        return sorted(healthy, key=rank)[0]

    def apply_routing(self, force: bool = False) -> None:
        """Push interface metrics so the preferred link wins the default route."""
        if not self.cfg.manage_metrics:
            if force:
                self.log_event("warning", "Metrics management is disabled in Settings; Windows default route not updated.")
            return
        pref = self.preferred_uplink()
        if pref is None:
            return

        mapping: Dict[str, int] = {}
        others = [u for u in self.candidates() if u.name != pref.name]
        mapping[pref.name] = self.cfg.uplink(pref.name).base_metric or 10

        def rank(u: Uplink):
            return (u.state != STATE_UP, 0 if not u.adapter.is_wifi else 1,
                    -u.weight, u.rtt_ms or 9999)

        for i, u in enumerate(sorted(others, key=rank), start=1):
            mapping[u.name] = (self.cfg.uplink(pref.name).base_metric or 10) + 10 * i

        if not force and mapping == self._last_applied:
            return

        t0 = time.perf_counter()
        self.adapters.set_metrics(mapping)
        self._last_applied = dict(mapping)
        dt = (time.perf_counter() - t0) * 1000
        self.log_event("info",
                       f"Default route -> {pref.display} (metric {mapping[pref.name]}, {dt:.0f} ms)")
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Load-balancer selection
    # ------------------------------------------------------------------ #
    def select_for_new_connection(self, prefer_name: str = "auto") -> Optional[Uplink]:
        """Pick the uplink for one outbound connection.

        - explicit rule target wins if that link is healthy
        - MODE_AGGREGATE  -> smooth weighted round robin across healthy links
        - MODE_FORCE/AUTO -> the preferred link
        """
        with self._lock:
            if prefer_name and prefer_name != "auto":
                u = self.uplinks.get(prefer_name)
                if u and u.healthy:
                    return u
                if u and not u.healthy:
                    self.log_event("warning",
                                   f"Rule target {prefer_name} is down; spilling to failover")

            if self.cfg.mode == MODE_AGGREGATE:
                healthy = self.healthy_uplinks()
                if not healthy:
                    return None
                return self._swrr(healthy)

            return self.preferred_uplink()

    def _swrr(self, pool: List[Uplink]) -> Uplink:
        """Smooth weighted round robin (nginx algorithm): spreads load in the
        configured ratio without bursting, unlike naive modulo round robin."""
        total = 0
        best: Optional[Uplink] = None
        best_val = None
        for u in pool:
            w = max(1, u.weight)
            cur = self._rr_state.get(u.name, 0) + w
            self._rr_state[u.name] = cur
            total += w
            if best_val is None or cur > best_val:
                best, best_val = u, cur
        assert best is not None
        self._rr_state[best.name] = self._rr_state[best.name] - total
        return best

    # ------------------------------------------------------------------ #
    # Monitor thread
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="omninet-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        self.sync_uplinks()
        self.probe_round()
        self.apply_routing(force=True)
        last_sync = time.time()

        while not self._stop.is_set():
            t0 = time.time()
            try:
                # Re-enumerate adapters occasionally: USB dongles come and go and
                # their ifIndex changes on every re-plug.
                if t0 - last_sync > 4:
                    self.sync_uplinks()
                    last_sync = t0

                before = {n: u.state for n, u in self.uplinks.items()}
                self.probe_round()
                after = {n: u.state for n, u in self.uplinks.items()}

                if before != after:
                    self.apply_routing(force=True)      # state flip: react now
                else:
                    self.apply_routing()

                if self.on_change:
                    self.on_change()
            except Exception as e:
                log.exception("monitor loop error: %s", e)

            elapsed = time.time() - t0
            self._stop.wait(max(0.25, self.cfg.probe_interval_s - elapsed))

    # ------------------------------------------------------------------ #
    def set_mode(self, mode: str, forced_adapter: str = "") -> None:
        self.cfg.mode = mode
        if forced_adapter:
            self.cfg.forced_adapter = forced_adapter
        self.log_event("info", f"Mode -> {mode}" + (f" ({forced_adapter})" if forced_adapter else ""))
        self.apply_routing(force=True)

    def shutdown(self) -> None:
        self.stop()
        if self.cfg.restore_on_exit:
            try:
                self.adapters.restore_all()
                self.log_event("info", "Restored original interface metrics")
            except Exception as e:
                log.error("restore failed: %s", e)
