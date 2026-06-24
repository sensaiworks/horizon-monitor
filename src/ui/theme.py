"""Dark, flat, modern theme for the horizon-monitor UI.

`apply_theme(app)` sets the Fusion base style (reliable and consistent across Windows
versions) plus a QSS stylesheet. `make_status_icon(status)` draws the tray/window icon
with QPainter so the UI has no PIL dependency.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

# Palette --------------------------------------------------------------------
COLORS = {
    "bg":        "#0f1117",
    "surface":   "#181b24",
    "surface2":  "#1f2430",
    "border":    "#2a2f3a",
    "text":      "#e6e8ee",
    "text_dim":  "#9aa3b2",
    "accent":    "#22c55e",
    "accent_hi": "#16a34a",
    "warn":      "#fb923c",
    "danger":    "#ef4444",
    "info":      "#38bdf8",
}

# Status -> dot color (mirrors the old tray icon states).
STATUS_COLORS = {
    "monitoring": "#22c55e",
    "running":    "#22c55e",
    "paused":     "#fb923c",
    "stopped":    "#6b7280",
    "locked":     "#ef4444",
}


def _qss() -> str:
    c = COLORS
    return f"""
    QWidget {{
        background: {c['bg']};
        color: {c['text']};
        font-family: 'Segoe UI', sans-serif;
        font-size: 13px;
    }}
    QFrame#Header {{
        background: {c['surface']};
        border-bottom: 1px solid {c['border']};
    }}
    QFrame#NavRail {{
        background: {c['surface']};
        border-right: 1px solid {c['border']};
    }}
    QLabel#AppTitle {{ font-size: 15px; font-weight: 600; }}
    QLabel#PageTitle {{ font-size: 18px; font-weight: 600; }}
    QLabel#PageSubtitle {{ color: {c['text_dim']}; font-size: 12px; }}
    QLabel#SectionTitle {{ font-size: 13px; font-weight: 600; color: {c['text']}; }}
    QLabel#Dim {{ color: {c['text_dim']}; }}

    /* Pills (status chips) */
    QLabel.pill {{
        background: {c['surface2']};
        border: 1px solid {c['border']};
        border-radius: 11px;
        padding: 3px 10px;
        color: {c['text_dim']};
        font-size: 12px;
    }}

    /* Nav rail buttons */
    QPushButton#NavButton {{
        text-align: left;
        padding: 9px 14px;
        border: none;
        border-radius: 8px;
        color: {c['text_dim']};
        font-size: 13px;
        background: transparent;
    }}
    QPushButton#NavButton:hover {{ background: {c['surface2']}; color: {c['text']}; }}
    QPushButton#NavButton:checked {{
        background: {c['surface2']};
        color: {c['text']};
        font-weight: 600;
        border-left: 3px solid {c['accent']};
    }}

    /* Cards */
    QFrame.card {{
        background: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 12px;
    }}

    /* Buttons */
    QPushButton {{
        background: {c['surface2']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 7px 14px;
        color: {c['text']};
    }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:disabled {{ color: {c['text_dim']}; border-color: {c['border']}; }}
    QPushButton#Primary {{
        background: {c['accent']};
        border: none;
        color: #07140c;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {c['accent_hi']}; }}
    QPushButton#Danger {{ background: {c['danger']}; border: none; color: white; }}

    /* Inputs */
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {{
        background: {c['surface2']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 6px 8px;
        selection-background-color: {c['accent']};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {c['accent']}; }}
    QListWidget {{
        background: {c['surface2']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 4px;
    }}
    QListWidget::item {{ padding: 5px 6px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {c['border']}; color: {c['text']}; }}
    QCheckBox {{ spacing: 8px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['text_dim']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(_qss())


def make_status_icon(status: str = "stopped", size: int = 64) -> QIcon:
    """Draw the chat-bars glyph on a dark disc, tinted by status."""
    color = QColor(STATUS_COLORS.get(status, STATUS_COLORS["stopped"]))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    p.setBrush(QColor(20, 20, 30))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2 * s, 2 * s, 60 * s, 60 * s)
    p.setBrush(color)
    p.drawEllipse(8 * s, 8 * s, 48 * s, 48 * s)
    p.setBrush(QColor(255, 255, 255, 225))
    p.drawRect(18 * s, 20 * s, 6 * s, 24 * s)   # left bar
    p.drawRect(40 * s, 20 * s, 6 * s, 24 * s)   # right bar
    p.drawRect(18 * s, 30 * s, 28 * s, 6 * s)   # crossbar
    p.end()
    return QIcon(pm)
