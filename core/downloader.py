"""
core/downloader.py — the one place where you really do get line1 + line2 on a
single file.

A single TCP connection cannot be split across two WANs (see load_balancer.py).
But an HTTP server that honours Range requests lets us turn one download into N
independent connections — and those we can scatter across every uplink. Eight
chunks on the 1 Gbps line plus eight on the 100 Mbps line finish in the time a
1.1 Gbps link would take, minus the slowest trailing chunk.

Work distribution is a shared queue, not a fixed proportional split. If the
TP-Link line is congested it simply pulls fewer chunks; no measurement, no
tuning, no stranded work at the end. The only static parameter is chunk size,
which trades scheduling granularity against per-request overhead.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection

from .netutil import socket_options_for
from .router_engine import Uplink

log = logging.getLogger("omninet.dl")

MIN_CHUNK = 1 << 20            # 1 MiB
MAX_CHUNKS_PER_LINK = 16
STREAM_BLOCK = 1 << 16


class BoundAdapter(HTTPAdapter):
    """requests adapter whose sockets are pinned to one uplink.

    source_address gives the socket the right IP; socket_options carries
    IP_UNICAST_IF so the packet actually leaves through that NIC. urllib3
    applies both before connect(), which is the only moment they matter."""

    def __init__(self, src_ip: str, if_index: int, **kw):
        self._src_ip = src_ip
        self._if_index = if_index
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["source_address"] = (self._src_ip, 0)
        pool_kwargs["socket_options"] = (
            list(HTTPConnection.default_socket_options)
            + socket_options_for(self._src_ip, self._if_index)
        )
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def session_for(uplink: Uplink, pool: int = 8) -> requests.Session:
    s = requests.Session()
    ad = BoundAdapter(uplink.ip, uplink.if_index, pool_connections=pool, pool_maxsize=pool)
    s.mount("http://", ad)
    s.mount("https://", ad)
    s.headers["User-Agent"] = "OmniNet/1.0"
    return s


@dataclass
class DownloadProgress:
    total: int = 0
    done: int = 0
    per_uplink: dict = field(default_factory=dict)     # name -> bytes
    rate_bps: float = 0.0
    finished: bool = False
    error: str = ""
    path: str = ""

    @property
    def pct(self) -> float:
        return 100.0 * self.done / self.total if self.total else 0.0


class SegmentedDownload:
    def __init__(self, url: str, dest: str, uplinks: List[Uplink],
                 on_progress: Optional[Callable[[DownloadProgress], None]] = None,
                 connections_per_link: int = 4) -> None:
        self.url = url
        self.dest = Path(dest)
        self.uplinks = [u for u in uplinks if u.ip]
        self.on_progress = on_progress
        self.conns_per_link = max(1, connections_per_link)
        self.progress = DownloadProgress(path=str(dest))
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._q: "queue.Queue[tuple[int,int]]" = queue.Queue()

    # ------------------------------------------------------------------ #
    def probe(self) -> tuple[int, bool]:
        """Return (content_length, supports_range)."""
        u = self.uplinks[0]
        with session_for(u, pool=2) as s:
            r = s.get(self.url, headers={"Range": "bytes=0-0"},
                      stream=True, timeout=15, allow_redirects=True)
            try:
                if r.status_code == 206:
                    cr = r.headers.get("Content-Range", "")
                    total = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
                    return total, True
                total = int(r.headers.get("Content-Length") or 0)
                return total, False
            finally:
                r.close()

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------------ #
    def run(self) -> DownloadProgress:
        if not self.uplinks:
            self.progress.error = "no usable uplink"
            self.progress.finished = True
            return self.progress
        try:
            total, ranged = self.probe()
        except Exception as e:
            self.progress.error = f"probe failed: {e}"
            self.progress.finished = True
            return self.progress

        self.progress.total = total
        self.dest.parent.mkdir(parents=True, exist_ok=True)

        if not ranged or total <= MIN_CHUNK or len(self.uplinks) == 1:
            reason = "server does not support Range" if not ranged else "file too small to split"
            log.info("single-stream download (%s)", reason)
            return self._single()

        # Pre-allocate so every worker can seek+write independently.
        with open(self.dest, "wb") as f:
            f.truncate(total)

        workers = len(self.uplinks) * self.conns_per_link
        chunk = max(MIN_CHUNK, total // (len(self.uplinks) * MAX_CHUNKS_PER_LINK))
        pos = 0
        while pos < total:
            end = min(total - 1, pos + chunk - 1)
            self._q.put((pos, end))
            pos = end + 1

        t0 = time.time()
        threads = []
        for i in range(workers):
            u = self.uplinks[i % len(self.uplinks)]
            t = threading.Thread(target=self._worker, args=(u,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        dt = max(0.001, time.time() - t0)
        self.progress.rate_bps = self.progress.done * 8 / dt
        self.progress.finished = True
        self._emit()
        return self.progress

    # ------------------------------------------------------------------ #
    def _worker(self, uplink: Uplink) -> None:
        s = session_for(uplink, pool=self.conns_per_link)
        try:
            while not self._cancel.is_set():
                try:
                    start, end = self._q.get_nowait()
                except queue.Empty:
                    return
                for attempt in range(3):
                    try:
                        self._fetch_range(s, uplink, start, end)
                        break
                    except Exception as e:
                        if attempt == 2:
                            with self._lock:
                                self.progress.error = f"chunk {start}-{end}: {e}"
                            log.warning("chunk %s-%s failed on %s: %s",
                                        start, end, uplink.display, e)
                        else:
                            time.sleep(0.4 * (attempt + 1))
        finally:
            s.close()

    def _fetch_range(self, s: requests.Session, uplink: Uplink, start: int, end: int) -> None:
        headers = {"Range": f"bytes={start}-{end}"}
        with s.get(self.url, headers=headers, stream=True, timeout=30) as r:
            if r.status_code not in (200, 206):
                raise OSError(f"HTTP {r.status_code}")
            offset = start
            with open(self.dest, "r+b") as f:
                f.seek(start)
                for block in r.iter_content(STREAM_BLOCK):
                    if self._cancel.is_set():
                        return
                    if not block:
                        continue
                    f.write(block)
                    offset += len(block)
                    with self._lock:
                        self.progress.done += len(block)
                        self.progress.per_uplink[uplink.name] = (
                            self.progress.per_uplink.get(uplink.name, 0) + len(block))
                    self._emit()

    def _single(self) -> DownloadProgress:
        u = self.uplinks[0]
        s = session_for(u, pool=2)
        try:
            with s.get(self.url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(self.dest, "wb") as f:
                    for block in r.iter_content(STREAM_BLOCK):
                        if self._cancel.is_set():
                            break
                        f.write(block)
                        with self._lock:
                            self.progress.done += len(block)
                            self.progress.per_uplink[u.name] = (
                                self.progress.per_uplink.get(u.name, 0) + len(block))
                        self._emit()
        except Exception as e:
            self.progress.error = str(e)
        finally:
            s.close()
            self.progress.finished = True
            self._emit()
        return self.progress

    _last_emit = 0.0

    def _emit(self) -> None:
        if not self.on_progress:
            return
        now = time.time()
        if now - self._last_emit < 0.1 and not self.progress.finished:
            return
        self._last_emit = now
        try:
            self.on_progress(self.progress)
        except Exception:
            pass
