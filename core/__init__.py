"""OmniNet core services."""

from __future__ import annotations

import logging
from typing import Optional

from config import Config, load as load_config, save as save_config
from .adapter_manager import AdapterManager
from .app_binder import ForceBindIP, RuleEngine, TransparentRedirector
from .load_balancer import MultiWanProxy
from .router_engine import RouterEngine
from .stats import ThroughputMonitor

log = logging.getLogger("omninet.core")


class Services:
    """Single container the UI talks to, so no widget ever touches a raw thread."""

    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or load_config()
        self.adapters = AdapterManager()
        self.router = RouterEngine(self.cfg, self.adapters)
        self.rules = RuleEngine(self.cfg)
        self.stats = ThroughputMonitor()
        self.forcebind = ForceBindIP()
        self.redirector = TransparentRedirector(self.cfg, self.cfg.socks_port + 1)
        self.proxy = MultiWanProxy(self.cfg, self.router, self.rules, self.redirector)

    def start(self) -> None:
        self.stats.start()
        self.router.start()
        if self.cfg.proxy_enabled:
            self.proxy.start()
        if self.cfg.transparent_mode:
            ok, msg = self.redirector.start()
            if not ok:
                log.warning("transparent mode unavailable: %s", msg)

    def stop(self) -> None:
        try:
            self.redirector.stop()
        except Exception:
            pass
        try:
            self.proxy.stop()
        except Exception:
            pass
        self.stats.stop()
        self.router.shutdown()
        save_config(self.cfg)


__all__ = [
    "Services", "AdapterManager", "RouterEngine", "RuleEngine",
    "MultiWanProxy", "ThroughputMonitor", "ForceBindIP", "TransparentRedirector",
    "load_config", "save_config",
]
