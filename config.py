"""
OmniNet — persistent configuration.

Config lives in %APPDATA%\\OmniNet\\config.json so it survives PyInstaller
one-file extraction (never write next to the .exe — it may be in Program Files).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List

APP_NAME = "OmniNet"


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


CONFIG_PATH = config_dir() / "config.json"
LOG_PATH = config_dir() / "omninet.log"


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
MODE_AUTO = "auto"            # failover, lowest-latency healthy link wins
MODE_FORCE = "force"          # pin everything to one interface
MODE_AGGREGATE = "aggregate"  # spread NEW connections across all healthy links

ALL_MODES = (MODE_AUTO, MODE_FORCE, MODE_AGGREGATE)


@dataclass
class UplinkConfig:
    """Per-adapter user settings. Keyed by adapter *name* (connection name),
    not ifIndex — ifIndex changes when a USB dongle is re-plugged."""
    name: str
    enabled: bool = True
    weight: int = 100            # relative share for the load balancer
    base_metric: int = 10        # interface metric applied when this link is preferred
    friendly: str = ""           # e.g. "Strong Signal"


@dataclass
class AppRule:
    """exe name (lowercase, no path) -> uplink target."""
    exe: str
    target: str = "auto"         # adapter name, or "auto"
    enabled: bool = True
    note: str = ""


@dataclass
class Config:
    mode: str = MODE_AUTO
    forced_adapter: str = ""

    proxy_host: str = "127.0.0.1"
    socks_port: int = 1080
    http_port: int = 8080
    proxy_enabled: bool = True

    # Ports our own upstream sockets bind to. WinDivert excludes this range so
    # we never capture our own traffic and loop forever.
    upstream_port_low: int = 50000
    upstream_port_high: int = 50999

    probe_targets: List[str] = field(default_factory=lambda: ["1.1.1.1:443", "8.8.8.8:443", "20.219.1.1:443", "20.43.132.128:443"])
    probe_interval_s: float = 3.0
    probe_timeout_s: float = 1.5
    fail_threshold: int = 2       # consecutive failed rounds before a link is DOWN
    recover_threshold: int = 2    # consecutive good rounds before a link is UP again
    rtt_switch_threshold_ms: float = 130.0  # don't switch uplinks if current link RTT is below this

    manage_metrics: bool = True   # allow writing interface metrics
    allow_adapter_disable: bool = False  # PnP disable is off by default (risk of stuck states)
    restore_on_exit: bool = True

    transparent_mode: bool = False   # WinDivert capture (experimental)
    start_minimised: bool = False

    uplinks: Dict[str, UplinkConfig] = field(default_factory=dict)
    rules: List[AppRule] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    def uplink(self, name: str) -> UplinkConfig:
        if name not in self.uplinks:
            self.uplinks[name] = UplinkConfig(name=name)
        return self.uplinks[name]

    def rule_for(self, exe: str) -> AppRule | None:
        exe = (exe or "").lower()
        for r in self.rules:
            if r.enabled and r.exe.lower() == exe:
                return r
        return None


_lock = threading.Lock()


def load() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    try:
        raw: Dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return Config()

    cfg = Config()
    for k, v in raw.items():
        if k == "uplinks":
            cfg.uplinks = {name: UplinkConfig(**d) for name, d in (v or {}).items()}
        elif k == "rules":
            cfg.rules = [AppRule(**d) for d in (v or [])]
        elif hasattr(cfg, k):
            setattr(cfg, k, v)
    if cfg.mode not in ALL_MODES:
        cfg.mode = MODE_AUTO
    return cfg


def save(cfg: Config) -> None:
    data = asdict(cfg)
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
