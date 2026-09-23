"""
ui/main_window.py — the whole GUI.

Threading rule enforced throughout: background threads never touch a widget.
The engines push text through a QObject signal bridge, and a 1 Hz QTimer pulls
state out of the service objects. Anything else eventually produces the classic
"QObject::setParent: Cannot set parent, new parent is in a different thread"
crash that people spend evenings chasing.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Dict, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog,
                             QFormLayout, QGridLayout, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QSizePolicy, QSpinBox,
                             QTableWidget, QTableWidgetItem, QTabWidget,
                             QVBoxLayout, QWidget, QMainWindow)

from config import MODE_AGGREGATE, MODE_AUTO, MODE_FORCE, save as save_config
from core import Services
from core.downloader import DownloadProgress, SegmentedDownload
from core.stats import human_bps, human_bytes
from . import theme
from .widgets import Sparkline, UplinkCard, make_icon, pill

log = logging.getLogger("omninet.ui")


class Bridge(QObject):
    event = pyqtSignal(str, str)
    progress = pyqtSignal(object)
    refreshed = pyqtSignal()


class MainWindow(QMainWindow):
    def __init__(self, services: Services) -> None:
        super().__init__()
        self.svc = services
        self.cfg = services.cfg
        self.bridge = Bridge()
        self.cards: Dict[str, UplinkCard] = {}
        self._download: Optional[SegmentedDownload] = None
        self._force_quit = False

        self.setWindowTitle("OmniNet — Multi-WAN Engine")
        self.setWindowIcon(make_icon())
        self.resize(1120, 760)
        self.setStyleSheet(theme.QSS)

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(12)

        lay.addLayout(self._build_header())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dashboard(), "Dashboard")
        self.tabs.addTab(self._build_rules(), "App Rules")
        self.tabs.addTab(self._build_downloader(), "Turbo Download")
        self.tabs.addTab(self._build_settings(), "Settings")
        self.tabs.addTab(self._build_log(), "Log")
        lay.addWidget(self.tabs, 1)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        lay.addWidget(self.status)

        self.svc.router.on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)
        self.svc.router.on_change = lambda: self.bridge.refreshed.emit()
        self.bridge.event.connect(self._on_event)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.refreshed.connect(self.sync_cards)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        QTimer.singleShot(400, self.rebuild_uplinks)

    # ------------------------------------------------------------------ #
    # Header / mode switcher
    # ------------------------------------------------------------------ #
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("OmniNet")
        title.setStyleSheet("font-size: 21px; font-weight: 700;")
        sub = QLabel("Multi-WAN routing, per-app binding and link bonding")
        sub.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(title)
        col.addWidget(sub)
        row.addLayout(col)
        row.addStretch(1)

        self.mode_buttons: Dict[str, QPushButton] = {}
        for key, label in ((MODE_AUTO, "Auto failover"),
                           (MODE_FORCE, "Force one link"),
                           (MODE_AGGREGATE, "Aggregate")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.clicked.connect(lambda _c, k=key: self.set_mode(k))
            self.mode_buttons[key] = b
            row.addWidget(b)

        self.force_combo = QComboBox()
        self.force_combo.setMinimumWidth(170)
        self.force_combo.currentTextChanged.connect(self._force_changed)
        row.addWidget(self.force_combo)
        return row

    def set_mode(self, mode: str) -> None:
        forced = self.force_combo.currentData() or ""
        self.svc.router.set_mode(mode, forced if mode == MODE_FORCE else "")
        save_config(self.cfg)
        self._sync_mode_buttons()

    def _force_changed(self, _t: str) -> None:
        name = self.force_combo.currentData()
        if name:
            self.cfg.forced_adapter = name
            if self.cfg.mode == MODE_FORCE:
                self.svc.router.set_mode(MODE_FORCE, name)
                save_config(self.cfg)

    def _sync_mode_buttons(self) -> None:
        for key, b in self.mode_buttons.items():
            on = key == self.cfg.mode
            b.setChecked(on)
            b.setProperty("mode", "on" if on else "off")
            b.style().unpolish(b)
            b.style().polish(b)
        self.force_combo.setEnabled(self.cfg.mode == MODE_FORCE)

    # ------------------------------------------------------------------ #
    # Dashboard
    # ------------------------------------------------------------------ #
    def _build_dashboard(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        top = QHBoxLayout()
        self.total_down = QLabel("↓ —")
        self.total_down.setStyleSheet(f"font-size: 26px; font-weight: 700; color: {theme.ACCENT};")
        self.total_up = QLabel("↑ —")
        self.total_up.setStyleSheet(f"font-size: 20px; font-weight: 600;")
        cap = QLabel("Aggregate throughput")
        cap.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        left = QVBoxLayout()
        left.setSpacing(2)
        left.addWidget(cap)
        rr = QHBoxLayout()
        rr.addWidget(self.total_down)
        rr.addSpacing(18)
        rr.addWidget(self.total_up)
        left.addLayout(rr)
        top.addLayout(left)
        top.addStretch(1)

        self.pill_links = pill("links —", theme.TEXT)
        self.pill_conns = pill("conns —", theme.TEXT)
        self.pill_proxy = pill("proxy —", theme.TEXT)
        for p in (self.pill_links, self.pill_conns, self.pill_proxy):
            top.addWidget(p)
        lay.addLayout(top)

        self.agg_graph = Sparkline(theme.ACCENT)
        self.agg_graph.setMinimumHeight(110)
        lay.addWidget(self.agg_graph)

        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(12)
        holder = QWidget()
        holder.setLayout(self.cards_grid)
        lay.addWidget(holder, 1)

        btns = QHBoxLayout()
        rescan = QPushButton("Rescan adapters")
        rescan.clicked.connect(self.rebuild_uplinks)
        flush = QPushButton("Flush DNS cache")
        flush.clicked.connect(lambda: self.svc.adapters.flush_dns())
        repair = QPushButton("Repair selected adapter")
        repair.clicked.connect(self._repair_selected)
        btns.addWidget(rescan)
        btns.addWidget(flush)
        btns.addWidget(repair)
        btns.addStretch(1)
        lay.addLayout(btns)
        return w

    def _target_uplinks(self) -> List[Uplink]:
        """Return the list of uplinks that should have cards on the dashboard.
        Always displays all physical Ethernet ports (even if cable is currently unplugged),
        plus any routable candidate adapters (e.g. connected Wi-Fi or USB dongles),
        plus any card that is currently displayed on the dashboard so it doesn't vanish on disconnect.
        """
        all_ups = self.svc.router.uplinks
        candidates = {u.name for u in self.svc.router.candidates()}
        out: List[Uplink] = []
        for name, u in all_ups.items():
            if not u.enabled:
                continue
            is_wired_physical = not u.adapter.is_wifi and not u.adapter.virtual
            if is_wired_physical or name in candidates or name in self.cards:
                out.append(u)
        return out or list(all_ups.values())

    def sync_cards(self) -> None:
        """Dynamically add, remove, and reorder cards as adapters appear, connect, or disconnect.
        Preserves existing card instances and their sparkline trend history.
        """
        target_ups = self._target_uplinks()
        target_names = [u.name for u in target_ups]
        current_names = list(self.cards.keys())

        if target_names == current_names:
            return

        # 1. Remove cards that are no longer on the system
        for name in list(self.cards.keys()):
            if name not in target_names:
                card = self.cards.pop(name)
                self.cards_grid.removeWidget(card)
                card.setParent(None)
                card.deleteLater()

        # 2. Detach remaining cards from grid layout temporarily
        for i in reversed(range(self.cards_grid.count())):
            item = self.cards_grid.itemAt(i)
            if item and item.widget():
                self.cards_grid.removeWidget(item.widget())

        # 3. Add or restore cards in target order
        for idx, u in enumerate(target_ups):
            card = self.cards.get(u.name)
            if not card:
                color = theme.UPLINK_COLORS[len(self.cards) % len(theme.UPLINK_COLORS)]
                card = UplinkCard(u.display, color)
                self.cards[u.name] = card
            self.cards_grid.addWidget(card, idx // 2, idx % 2)

        # 4. Refresh target combos and weight settings
        cur = self.force_combo.currentData()
        self.force_combo.blockSignals(True)
        self.force_combo.clear()
        for u in target_ups:
            self.force_combo.addItem(u.display, u.name)
        if cur:
            i = self.force_combo.findData(cur)
            if i >= 0:
                self.force_combo.setCurrentIndex(i)
        elif self.cfg.forced_adapter:
            i = self.force_combo.findData(self.cfg.forced_adapter)
            if i >= 0:
                self.force_combo.setCurrentIndex(i)
        self.force_combo.blockSignals(False)

        self._refresh_target_combos()
        self._sync_mode_buttons()
        self._rebuild_weight_rows()

    def rebuild_uplinks(self) -> None:
        self.svc.router.sync_uplinks()
        for i in reversed(range(self.cards_grid.count())):
            item = self.cards_grid.itemAt(i)
            if item and item.widget():
                item.widget().setParent(None)
        self.cards.clear()
        self.sync_cards()

    def _repair_selected(self) -> None:
        name = self.force_combo.currentData()
        if not name:
            return
        if QMessageBox.question(
                self, "Repair adapter",
                f"Re-enable the device node for '{name}'?\n\n"
                "Use this if the adapter is stuck after a disable cycle.") \
                != QMessageBox.StandardButton.Yes:
            return
        ok = self.svc.adapters.repair_pnp(name)
        self._on_event("info" if ok else "error",
                       f"Repair {'succeeded' if ok else 'failed'} for {name}")

    # ------------------------------------------------------------------ #
    # App rules
    # ------------------------------------------------------------------ #
    def _build_rules(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        note = QLabel(
            "Rules apply to traffic that reaches the local proxy. Point the app at "
            f"SOCKS5 {self.cfg.proxy_host}:{self.cfg.socks_port} (or HTTP :{self.cfg.http_port}), "
            "or enable transparent capture in Settings to cover apps that ignore proxy settings.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        lay.addWidget(note)

        self.rules_table = QTableWidget(0, 5)
        self.rules_table.setHorizontalHeaderLabels(
            ["Executable", "Bound to", "Enabled", "Connections", "Traffic"])
        self.rules_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_table.setAlternatingRowColors(True)
        self.rules_table.verticalHeader().setVisible(False)
        lay.addWidget(self.rules_table, 1)

        row = QHBoxLayout()
        self.rule_exe = QLineEdit()
        self.rule_exe.setPlaceholderText("chrome.exe  (or browse…)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_exe)
        self.rule_target = QComboBox()
        self.rule_target.setMinimumWidth(180)
        add = QPushButton("Add rule")
        add.setProperty("accent", "true")
        add.clicked.connect(self._add_rule)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self._remove_rule)
        row.addWidget(self.rule_exe, 1)
        row.addWidget(browse)
        row.addWidget(QLabel("→"))
        row.addWidget(self.rule_target)
        row.addWidget(add)
        row.addWidget(rem)
        lay.addLayout(row)

        launch = QHBoxLayout()
        self.fb_path = QLineEdit()
        self.fb_path.setPlaceholderText("Launch a legacy app with ForceBindIP (full path to .exe)")
        fb_browse = QPushButton("…")
        fb_browse.setMaximumWidth(40)
        fb_browse.clicked.connect(lambda: self._pick_into(self.fb_path))
        fb_go = QPushButton("Launch bound")
        fb_go.clicked.connect(self._forcebind_launch)
        launch.addWidget(self.fb_path, 1)
        launch.addWidget(fb_browse)
        launch.addWidget(fb_go)
        lay.addLayout(launch)

        warn = QLabel("ForceBindIP injects a DLL. Never use it on anti-cheat protected games "
                      "(Valorant, EAC/BattlEye titles) — route those with a proxy rule instead.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.AMBER}; font-size: 11px;")
        lay.addWidget(warn)
        return w

    def _pick_into(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select executable", "", "Executables (*.exe)")
        if path:
            edit.setText(path)

    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select executable", "", "Executables (*.exe)")
        if path:
            self.rule_exe.setText(Path(path).name)

    def _refresh_target_combos(self) -> None:
        for combo in (self.rule_target,):
            cur = combo.currentData()
            combo.clear()
            combo.addItem("Auto (follow mode)", "auto")
            for u in self.svc.router.candidates():
                combo.addItem(u.display, u.name)
            if cur:
                i = combo.findData(cur)
                if i >= 0:
                    combo.setCurrentIndex(i)

    def _add_rule(self) -> None:
        exe = self.rule_exe.text().strip()
        if not exe:
            return
        self.svc.rules.add_rule(exe, self.rule_target.currentData() or "auto")
        save_config(self.cfg)
        self.rule_exe.clear()
        self._fill_rules_table()

    def _remove_rule(self) -> None:
        r = self.rules_table.currentRow()
        if r < 0:
            return
        item = self.rules_table.item(r, 0)
        if item:
            self.svc.rules.remove_rule(item.text())
            save_config(self.cfg)
            self._fill_rules_table()

    def _fill_rules_table(self) -> None:
        stats = self.svc.proxy.stats.by_app
        self.rules_table.setRowCount(len(self.cfg.rules))
        for r, rule in enumerate(self.cfg.rules):
            s = stats.get(rule.exe.lower(), {})
            target = rule.target
            u = self.svc.router.uplinks.get(target)
            vals = [rule.exe,
                    u.display if u else "Auto",
                    "yes" if rule.enabled else "no",
                    str(s.get("conns", 0)),
                    human_bytes(s.get("up", 0) + s.get("down", 0))]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.rules_table.setItem(r, c, it)

    def _forcebind_launch(self) -> None:
        path = self.fb_path.text().strip()
        if not path:
            return
        target = self.rule_target.currentData()
        u = self.svc.router.uplinks.get(target) or self.svc.router.preferred_uplink()
        if not u:
            self._on_event("error", "No uplink available")
            return
        ok, msg = self.svc.forcebind.launch(path, u.ip)
        self._on_event("info" if ok else "error", msg)

    # ------------------------------------------------------------------ #
    # Downloader
    # ------------------------------------------------------------------ #
    def _build_downloader(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        note = QLabel("Splits one file into range requests spread over every healthy link. "
                      "This is the only mode where a single transfer reaches the combined "
                      "speed of both lines — it needs a server that supports HTTP Range.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        lay.addWidget(note)

        form = QFormLayout()
        self.dl_url = QLineEdit()
        self.dl_url.setPlaceholderText("https://…")
        self.dl_dest = QLineEdit(str(Path.home() / "Downloads"))
        self.dl_conns = QSpinBox()
        self.dl_conns.setRange(1, 16)
        self.dl_conns.setValue(4)
        form.addRow("URL", self.dl_url)
        form.addRow("Save to", self.dl_dest)
        form.addRow("Connections per link", self.dl_conns)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.dl_start = QPushButton("Start download")
        self.dl_start.setProperty("accent", "true")
        self.dl_start.clicked.connect(self._start_download)
        self.dl_cancel = QPushButton("Cancel")
        self.dl_cancel.setEnabled(False)
        self.dl_cancel.clicked.connect(lambda: self._download and self._download.cancel())
        row.addWidget(self.dl_start)
        row.addWidget(self.dl_cancel)
        row.addStretch(1)
        lay.addLayout(row)

        self.dl_bar = QProgressBar()
        self.dl_bar.setValue(0)
        lay.addWidget(self.dl_bar)
        self.dl_info = QLabel("Idle")
        self.dl_info.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.dl_info)
        self.dl_split = QLabel("")
        self.dl_split.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        lay.addWidget(self.dl_split)
        lay.addStretch(1)
        return w

    def _start_download(self) -> None:
        url = self.dl_url.text().strip()
        if not url:
            return
        ups = self.svc.router.healthy_uplinks()
        if not ups:
            self._on_event("error", "No healthy uplink")
            return
        name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download.bin"
        dest = Path(self.dl_dest.text().strip() or ".") / name

        self._download = SegmentedDownload(
            url, str(dest), ups,
            on_progress=lambda p: self.bridge.progress.emit(p),
            connections_per_link=self.dl_conns.value())
        self.dl_start.setEnabled(False)
        self.dl_cancel.setEnabled(True)
        threading.Thread(target=self._download.run, daemon=True).start()

    def _on_progress(self, p: DownloadProgress) -> None:
        self.dl_bar.setValue(int(p.pct))
        self.dl_info.setText(
            f"{human_bytes(p.done)} / {human_bytes(p.total)}"
            + (f"   ·   {human_bps(p.rate_bps)} average" if p.rate_bps else "")
            + (f"   ·   ERROR: {p.error}" if p.error else ""))
        parts = []
        for name, n in sorted(p.per_uplink.items(), key=lambda kv: -kv[1]):
            u = self.svc.router.uplinks.get(name)
            share = 100.0 * n / p.done if p.done else 0
            parts.append(f"{u.display if u else name}: {human_bytes(n)} ({share:.0f}%)")
        self.dl_split.setText("   ·   ".join(parts))
        if p.finished:
            self.dl_start.setEnabled(True)
            self.dl_cancel.setEnabled(False)
            self._on_event("info", f"Download finished: {p.path}")

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def _build_settings(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        form = QFormLayout()
        self.set_socks = QSpinBox(); self.set_socks.setRange(1, 65535)
        self.set_socks.setValue(self.cfg.socks_port)
        self.set_http = QSpinBox(); self.set_http.setRange(1, 65535)
        self.set_http.setValue(self.cfg.http_port)
        self.set_interval = QSpinBox(); self.set_interval.setRange(1, 60)
        self.set_interval.setValue(int(self.cfg.probe_interval_s))
        self.set_fail = QSpinBox(); self.set_fail.setRange(1, 10)
        self.set_fail.setValue(self.cfg.fail_threshold)

        self.set_metrics = QCheckBox("Let OmniNet manage interface metrics")
        self.set_metrics.setChecked(self.cfg.manage_metrics)
        self.set_restore = QCheckBox("Restore original metrics on exit")
        self.set_restore.setChecked(self.cfg.restore_on_exit)
        self.set_transparent = QCheckBox("Transparent capture (WinDivert driver, experimental)")
        self.set_transparent.setChecked(self.cfg.transparent_mode)
        self.set_tray = QCheckBox("Start minimised to tray")
        self.set_tray.setChecked(self.cfg.start_minimised)

        form.addRow("SOCKS5 port", self.set_socks)
        form.addRow("HTTP proxy port", self.set_http)
        form.addRow("Probe interval (s)", self.set_interval)
        form.addRow("Failures before DOWN", self.set_fail)
        lay.addLayout(form)
        for c in (self.set_metrics, self.set_restore, self.set_transparent, self.set_tray):
            lay.addWidget(c)

        lay.addWidget(QLabel("Link weights (share of new connections in Aggregate mode)"))
        self.weights_box = QVBoxLayout()
        holder = QWidget()
        holder.setLayout(self.weights_box)
        lay.addWidget(holder)

        save_btn = QPushButton("Apply and save")
        save_btn.setProperty("accent", "true")
        save_btn.clicked.connect(self._save_settings)
        lay.addWidget(save_btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        return w

    def _rebuild_weight_rows(self) -> None:
        if not hasattr(self, "weights_box"):
            return
        while self.weights_box.count():
            item = self.weights_box.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self.weight_spins: Dict[str, QSpinBox] = {}
        for u in self.svc.router.candidates():
            row = QHBoxLayout()
            lbl = QLabel(u.display)
            lbl.setMinimumWidth(240)
            sp = QSpinBox(); sp.setRange(1, 1000)
            sp.setValue(self.cfg.uplink(u.name).weight)
            row.addWidget(lbl)
            row.addWidget(sp)
            row.addStretch(1)
            wrap = QWidget(); wrap.setLayout(row)
            self.weights_box.addWidget(wrap)
            self.weight_spins[u.name] = sp

    def _save_settings(self) -> None:
        restart_proxy = (self.set_socks.value() != self.cfg.socks_port
                         or self.set_http.value() != self.cfg.http_port)
        self.cfg.socks_port = self.set_socks.value()
        self.cfg.http_port = self.set_http.value()
        self.cfg.probe_interval_s = float(self.set_interval.value())
        self.cfg.fail_threshold = self.set_fail.value()
        self.cfg.manage_metrics = self.set_metrics.isChecked()
        self.cfg.restore_on_exit = self.set_restore.isChecked()
        self.cfg.start_minimised = self.set_tray.isChecked()
        for name, sp in getattr(self, "weight_spins", {}).items():
            self.cfg.uplink(name).weight = sp.value()

        want_transparent = self.set_transparent.isChecked()
        if want_transparent != self.cfg.transparent_mode:
            self.cfg.transparent_mode = want_transparent
            if want_transparent:
                ok, msg = self.svc.redirector.start()
                self._on_event("info" if ok else "error", f"Transparent mode: {msg}")
                if not ok:
                    self.cfg.transparent_mode = False
                    self.set_transparent.setChecked(False)
            else:
                self.svc.redirector.stop()
                self._on_event("info", "Transparent mode off")

        save_config(self.cfg)
        self.svc.router.sync_uplinks()
        if restart_proxy:
            self._on_event("warning", "Restart OmniNet for the new proxy ports to take effect")
        self._on_event("info", "Settings saved")

    # ------------------------------------------------------------------ #
    # Log
    # ------------------------------------------------------------------ #
    def _build_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        lay.addWidget(self.log_view)
        row = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.log_view.clear)
        row.addWidget(clear)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _on_event(self, level: str, msg: str) -> None:
        colors = {"error": theme.RED, "warning": theme.AMBER}
        col = colors.get(level, theme.TEXT_DIM)
        self.log_view.appendHtml(f'<span style="color:{col}">[{level}]</span> {msg}')

    # ------------------------------------------------------------------ #
    # Periodic refresh
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.sync_cards()
        router = self.svc.router
        pref = router.preferred_uplink()
        names = []
        for name, card in self.cards.items():
            u = router.uplinks.get(name)
            if not u:
                continue
            names.append(name)
            rate = self.svc.stats.rate_for(name)
            card.update_state(
                state=u.state, ip=u.ip, gateway=u.adapter.gateway,
                description=u.adapter.description, rtt=u.rtt_ms, loss=u.loss_pct,
                metric=u.adapter.if_metric, conns=u.active_conns,
                down_bps=rate.rx_bps, up_bps=rate.tx_bps,
                preferred=bool(pref and pref.name == name))

        rx, tx = self.svc.stats.totals(names)
        self.total_down.setText(f"↓ {human_bps(rx)}")
        self.total_up.setText(f"↑ {human_bps(tx)}")
        self.agg_graph.push(rx, tx)

        healthy = len(router.healthy_uplinks())
        self.pill_links.setText(f"{healthy}/{len(self.cards)} links up")
        st = self.svc.proxy.stats
        self.pill_conns.setText(f"{st.active_conns} active · {st.total_conns} total")
        self.pill_proxy.setText(
            f"proxy {'on ' + str(self.cfg.socks_port) if self.svc.proxy.running else 'off'}"
            + (" · divert" if self.svc.redirector.running else ""))

        self._fill_rules_table()
        mode_str = f"mode {self.cfg.mode}"
        if not self.cfg.manage_metrics:
            mode_str += " (metrics unmanaged!)"
        self.status.setText(
            f"{mode_str}   ·   failovers {st.failovers}   ·   "
            f"failed dials {st.failed_conns}   ·   "
            f"preferred: {pref.display if pref else 'none'}")
        self._sync_mode_buttons()

    # ------------------------------------------------------------------ #
    def closeEvent(self, ev: QCloseEvent) -> None:
        if self._force_quit:
            ev.accept()
            return
        ev.ignore()
        self.hide()

    def quit_app(self) -> None:
        self._force_quit = True
        self.close()
