"""
ui/widgets.py — hand-painted widgets.

Everything is drawn with QPainter rather than pulling in pyqtgraph/matplotlib:
one fewer dependency, a much smaller PyInstaller bundle, and no numpy/Qt binding
conflicts at freeze time. A rolling sparkline is ~40 lines of paintEvent.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap, QIcon)
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                             QVBoxLayout, QWidget)

from core.stats import human_bps, human_bytes
from . import theme


# --------------------------------------------------------------------------- #
class Sparkline(QWidget):
    """Filled area chart of the last N samples. Two series: down and up."""

    def __init__(self, color: str = theme.ACCENT, points: int = 120, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.down: Deque[float] = deque(maxlen=points)
        self.up: Deque[float] = deque(maxlen=points)
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def push(self, down_bps: float, up_bps: float) -> None:
        self.down.append(max(0.0, down_bps))
        self.up.append(max(0.0, up_bps))
        self.update()

    def clear(self) -> None:
        self.down.clear()
        self.up.clear()
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.setPen(QPen(QColor(theme.BORDER), 1, Qt.PenStyle.DotLine))
        for i in range(1, 4):
            y = h * i / 4
            p.drawLine(0, int(y), w, int(y))

        if len(self.down) < 2:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "sampling…")
            return

        peak = max(max(self.down, default=1.0), max(self.up, default=1.0), 1.0)
        self._series(p, self.down, peak, self.color, fill=True)
        self._series(p, self.up, peak, QColor(theme.TEXT_DIM), fill=False)

        p.setPen(QColor(theme.TEXT_DIM))
        f = QFont(self.font())
        f.setPointSize(7)
        p.setFont(f)
        p.drawText(QRectF(4, 2, w - 8, 14), Qt.AlignmentFlag.AlignLeft, f"peak {human_bps(peak)}")

    def _series(self, p: QPainter, data, peak: float, color: QColor, fill: bool) -> None:
        w, h = self.width(), self.height()
        n = len(data)
        step = w / max(1, n - 1)
        pts: List[QPointF] = [
            QPointF(i * step, h - (v / peak) * (h - 12) - 4) for i, v in enumerate(data)
        ]

        if fill:
            path = QPainterPath(QPointF(0, h))
            for pt in pts:
                path.lineTo(pt)
            path.lineTo(QPointF(w, h))
            path.closeSubpath()
            grad = QLinearGradient(0, 0, 0, h)
            c0 = QColor(color)
            c0.setAlpha(110)
            c1 = QColor(color)
            c1.setAlpha(10)
            grad.setColorAt(0, c0)
            grad.setColorAt(1, c1)
            p.fillPath(path, QBrush(grad))

        line = QPainterPath(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)
        pen = QPen(color, 1.8 if fill else 1.0)
        if not fill:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.strokePath(line, pen)


# --------------------------------------------------------------------------- #
class UplinkCard(QFrame):
    """One NIC: name, state dot, live rates, RTT/loss, mini graph."""

    def __init__(self, name: str, color: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.color = color
        self.setObjectName("uplinkCard")
        self.setStyleSheet(
            f"#uplinkCard {{ background: {theme.CARD}; border: 1px solid {theme.BORDER};"
            f" border-radius: 12px; }}"
        )
        self.setMinimumWidth(300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 15px;")
        self.title = QLabel(name)
        self.title.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.badge = QLabel("")
        self.badge.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        top.addWidget(self.dot)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.badge)
        lay.addLayout(top)

        self.desc = QLabel("")
        self.desc.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.desc.setWordWrap(True)
        lay.addWidget(self.desc)

        rates = QHBoxLayout()
        self.down_lbl = QLabel("↓ —")
        self.down_lbl.setStyleSheet(f"color: {color}; font-size: 17px; font-weight: 700;")
        self.up_lbl = QLabel("↑ —")
        self.up_lbl.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px;")
        rates.addWidget(self.down_lbl)
        rates.addStretch(1)
        rates.addWidget(self.up_lbl)
        lay.addLayout(rates)

        self.graph = Sparkline(color)
        self.graph.setMinimumHeight(64)
        lay.addWidget(self.graph)

        self.meta = QLabel("")
        self.meta.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        lay.addWidget(self.meta)

    # ------------------------------------------------------------------ #
    def update_state(self, *, state: str, ip: str, gateway: str, description: str,
                     rtt: float, loss: float, metric: int, conns: int,
                     down_bps: float, up_bps: float, preferred: bool) -> None:
        col = {"up": theme.GREEN, "down": theme.RED}.get(state, theme.AMBER)
        self.dot.setStyleSheet(f"color: {col}; font-size: 15px;")
        self.badge.setText("PREFERRED" if preferred else state.upper())
        self.badge.setStyleSheet(
            f"color: {theme.ACCENT if preferred else theme.TEXT_DIM}; font-size: 11px;"
            f" font-weight: {'700' if preferred else '400'};")
        self.desc.setText(description)
        self.down_lbl.setText(f"↓ {human_bps(down_bps)}")
        self.up_lbl.setText(f"↑ {human_bps(up_bps)}")
        self.graph.push(down_bps, up_bps)
        rtt_s = f"{rtt:.0f} ms" if rtt else "—"
        self.meta.setText(
            f"{ip or 'no address'} → {gateway or 'no gateway'}   ·   "
            f"rtt {rtt_s}   ·   loss {loss:.0f}%   ·   metric {metric}   ·   {conns} conns")


# --------------------------------------------------------------------------- #
def make_icon(primary: str = theme.ACCENT, secondary: str = theme.GREEN,
              degraded: bool = False) -> QIcon:
    """Tray icon drawn at runtime: two stacked bars, one per WAN line.
    No .ico file to ship, no --add-data path to get wrong."""
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    p.setBrush(QBrush(QColor(theme.BG_ALT)))
    p.setPen(QPen(QColor(theme.BORDER), 3))
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(primary)))
    p.drawRoundedRect(14, 18, 36, 10, 5, 5)
    p.setBrush(QBrush(QColor(theme.RED if degraded else secondary)))
    p.drawRoundedRect(14, 36, 24, 10, 5, 5)
    p.end()
    return QIcon(pm)


def pill(text: str, color: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background: {theme.CARD}; color: {color}; border: 1px solid {theme.BORDER};"
        f" border-radius: 10px; padding: 3px 10px; font-size: 11px;")
    return lbl
