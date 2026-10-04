"""ui/theme.py — one place for colour and the global stylesheet."""

from __future__ import annotations

BG = "#0e1116"
BG_ALT = "#151a21"
CARD = "#1b2129"
CARD_HI = "#222a34"
BORDER = "#2b3540"
TEXT = "#e6edf3"
TEXT_DIM = "#8b98a5"
ACCENT = "#4da3ff"
GREEN = "#3fb950"
AMBER = "#d29922"
RED = "#f85149"
VIOLET = "#a371f7"

UPLINK_COLORS = ["#4da3ff", "#3fb950", "#a371f7", "#d29922", "#ff7b72"]

QSS = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: 'Segoe UI', 'Inter', sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BG}; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; top: -1px; background: {BG_ALT}; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_DIM};
    padding: 9px 20px; margin-right: 4px;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{ background: {BG_ALT}; color: {TEXT}; border: 1px solid {BORDER}; border-bottom: none; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

QPushButton {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 8px 16px; color: {TEXT};
}}
QPushButton:hover {{ background: {CARD_HI}; border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; border-color: {BORDER}; background: {BG_ALT}; }}
QPushButton[accent="true"] {{ background: {ACCENT}; color: #06121f; border: none; font-weight: 600; }}
QPushButton[accent="true"]:hover {{ background: #6bb4ff; }}
QPushButton[mode="on"] {{ background: {ACCENT}; color: #06121f; border: none; font-weight: 600; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 7px;
    padding: 7px 10px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {ACCENT};
    selection-color: #06121f; outline: none;
}}

QTableWidget {{
    background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 8px;
    gridline-color: {BORDER}; selection-background-color: {CARD_HI};
    selection-color: {TEXT}; alternate-background-color: {CARD};
}}
QHeaderView::section {{
    background: {CARD}; color: {TEXT_DIM}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 8px;
}}

QProgressBar {{
    background: {BG_ALT}; border: 1px solid {BORDER};
    border-radius: 7px; height: 16px; text-align: center; color: {TEXT};
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {BORDER}; background: {BG_ALT};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QMenu {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 7px 24px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {CARD_HI}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}

QToolTip {{ background: {CARD_HI}; color: {TEXT}; border: 1px solid {ACCENT}; border-radius: 6px; padding: 6px 10px; font-size: 12px; }}
"""
