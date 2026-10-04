"""
core/stats.py — per-adapter throughput, sampled from the NIC counters.

We read psutil.net_io_counters(pernic=True), which on Windows keys by the
adapter's *connection name* ("Ethernet", "Ethernet 2", "Wi-Fi") — the same key
Get-NetAdapter reports as .Name, which is why the whole app keys on that string
instead of ifIndex.

Counters are what the NIC actually moved, so this reflects all traffic on the
link, not just what went through our proxy. That is what you want on a dashboard:
if the numbers disagree with the proxy's own byte counts, the difference is
traffic that bypassed the proxy, which is itself useful to see.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Tuple

import psutil


@dataclass
class NicSample:
    ts: float
    rx: int
    tx: int


@dataclass
class NicRate:
    name: str
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_total: int = 0
    tx_total: int = 0
    history: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=120))


class ThroughputMonitor:
    """Sample every `interval` seconds; keeps ~2 minutes of history for graphs."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.rates: Dict[str, NicRate] = {}
        self._prev: Dict[str, NicSample] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.aggregate: Deque[Tuple[float, float]] = deque(maxlen=120)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="omninet-stats", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        self._snapshot()
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                return
            self._snapshot()

    def _snapshot(self) -> None:
        now = time.time()
        try:
            counters = psutil.net_io_counters(pernic=True)
        except Exception:
            return
        agg_rx = agg_tx = 0.0
        with self._lock:
            # Prune adapters not seen for > 60 seconds (prevents unbounded memory leak on USB churn)
            stale_names = [n for n, s in self._prev.items() if (now - s.ts) > 60.0 and n not in counters]
            for n in stale_names:
                self._prev.pop(n, None)
                self.rates.pop(n, None)

            for name, c in counters.items():
                cur = NicSample(now, c.bytes_recv, c.bytes_sent)
                prev = self._prev.get(name)
                self._prev[name] = cur
                rate = self.rates.setdefault(name, NicRate(name=name))
                rate.rx_total, rate.tx_total = c.bytes_recv, c.bytes_sent
                if prev and cur.ts > prev.ts:
                    dt = cur.ts - prev.ts
                    rate.rx_bps = max(0.0, (cur.rx - prev.rx) * 8 / dt)
                    rate.tx_bps = max(0.0, (cur.tx - prev.tx) * 8 / dt)
                    rate.history.append((rate.rx_bps, rate.tx_bps))
                    agg_rx += rate.rx_bps
                    agg_tx += rate.tx_bps
            if agg_rx or agg_tx:
                self.aggregate.append((agg_rx, agg_tx))

    def prune_stale(self, max_age_s: float = 60.0) -> int:
        """Prune any cached adapter rates older than max_age_s."""
        now = time.time()
        pruned = 0
        with self._lock:
            stale = [n for n, s in self._prev.items() if (now - s.ts) > max_age_s]
            for n in stale:
                self._prev.pop(n, None)
                self.rates.pop(n, None)
                pruned += 1
        return pruned

    # ------------------------------------------------------------------ #
    def rate_for(self, name: str) -> NicRate:
        with self._lock:
            return self.rates.get(name) or NicRate(name=name)

    def totals(self, names: List[str]) -> Tuple[float, float]:
        with self._lock:
            rx = sum(self.rates[n].rx_bps for n in names if n in self.rates)
            tx = sum(self.rates[n].tx_bps for n in names if n in self.rates)
        return rx, tx


def human_bps(bps: float) -> str:
    for unit, div in (("Gb/s", 1e9), ("Mb/s", 1e6), ("Kb/s", 1e3)):
        if bps >= div:
            return f"{bps / div:.2f} {unit}"
    return f"{bps:.0f} b/s"


def human_bytes(n: float) -> str:
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n:.0f} B"
