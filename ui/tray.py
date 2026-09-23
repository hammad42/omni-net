"""ui/tray.py — tray icon, quick switching, state balloons."""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from config import MODE_AGGREGATE, MODE_AUTO, MODE_FORCE, save as save_config
from core.stats import human_bps
from . import theme
from .widgets import make_icon


class TrayController:
    def __init__(self, window) -> None:
        self.win = window
        self.svc = window.svc
        self.cfg = window.cfg

        self.tray = QSystemTrayIcon(make_icon(), window)
        self.tray.setToolTip("OmniNet")
        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.tray.show()

        self._last_degraded = None
        self.rebuild()

        self.timer = QTimer(window)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)

    # ------------------------------------------------------------------ #
    def rebuild(self) -> None:
        self.menu.clear()
        show = QAction("Open OmniNet", self.menu)
        show.triggered.connect(self._show)
        self.menu.addAction(show)
        self.menu.addSeparator()

        for key, label in ((MODE_AUTO, "Auto failover"),
                           (MODE_AGGREGATE, "Aggregate both lines")):
            a = QAction(label, self.menu)
            a.setCheckable(True)
            a.setChecked(self.cfg.mode == key)
            a.triggered.connect(lambda _c, k=key: self._set_mode(k))
            self.menu.addAction(a)

        sub = self.menu.addMenu("Force link")
        for u in self.svc.router.candidates():
            a = QAction(f"{u.display}  ({u.state})", sub)
            a.setCheckable(True)
            a.setChecked(self.cfg.mode == MODE_FORCE and self.cfg.forced_adapter == u.name)
            a.triggered.connect(lambda _c, n=u.name: self._force(n))
            sub.addAction(a)

        self.menu.addSeparator()
        flush = QAction("Flush DNS cache", self.menu)
        flush.triggered.connect(self.svc.adapters.flush_dns)
        self.menu.addAction(flush)
        rescan = QAction("Rescan adapters", self.menu)
        rescan.triggered.connect(self.win.rebuild_uplinks)
        self.menu.addAction(rescan)
        self.menu.addSeparator()
        quit_a = QAction("Quit", self.menu)
        quit_a.triggered.connect(self.win.quit_app)
        self.menu.addAction(quit_a)

    # ------------------------------------------------------------------ #
    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show()

    def _show(self) -> None:
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def _set_mode(self, mode: str) -> None:
        self.svc.router.set_mode(mode)
        save_config(self.cfg)
        self.rebuild()

    def _force(self, name: str) -> None:
        self.svc.router.set_mode(MODE_FORCE, name)
        save_config(self.cfg)
        self.rebuild()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        router = self.svc.router
        all_links = router.candidates()
        healthy = router.healthy_uplinks()
        pref = router.preferred_uplink()
        names = [u.name for u in all_links]
        rx, tx = self.svc.stats.totals(names)

        degraded = len(healthy) < len(all_links)
        self.tray.setIcon(make_icon(degraded=degraded))
        self.tray.setToolTip(
            f"OmniNet — {self.cfg.mode}\n"
            f"via {pref.display if pref else 'no link'}\n"
            f"↓ {human_bps(rx)}   ↑ {human_bps(tx)}\n"
            f"{len(healthy)}/{len(all_links)} links up")

        if self._last_degraded is not None and degraded != self._last_degraded:
            if degraded:
                down = [u.display for u in all_links if not u.healthy]
                self.tray.showMessage("Link down", ", ".join(down) +
                                      f" — traffic on {pref.display if pref else 'none'}",
                                      QSystemTrayIcon.MessageIcon.Warning, 4000)
            else:
                self.tray.showMessage("All links restored", "Every WAN line is healthy again",
                                      QSystemTrayIcon.MessageIcon.Information, 3000)
        self._last_degraded = degraded
