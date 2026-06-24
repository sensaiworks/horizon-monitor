"""Main window: status header + left nav rail + stacked pages + activity log.

The window is the home for every feature. Closing it hides to tray (handled in app.py via
the tray; closeEvent here just hides). Start/Pause/Stop currently update the engine pill and
log — the real CaptureEngine is wired in the next phase.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .engine import CaptureEngine
from .pages import (
    AskPage, AssistPage, CollectPage, MonitorPage, PullPage, PushPage, RemotePage,
)
from .theme import COLORS, STATUS_COLORS
from .pages import _coming_soon  # reuse the dim "coming soon" label helper


class _EngineSignals(QObject):
    """Marshal CaptureEngine worker-thread hooks onto the Qt UI thread."""
    status = Signal(str)
    event = Signal(object)   # MessageEvent
    alert = Signal(object)   # MessageEvent
    log = Signal(str)
    error = Signal(str)

# (page label, emoji, page-factory-key)
_NAV = [
    ("Monitor", "🔔", "monitor"),
    ("Collect", "📚", "collect"),
    ("Pull code", "⬇", "pull"),
    ("Push code", "⬆", "push"),
    ("Assist", "🤝", "assist"),
    ("Remote", "🔓", "remote"),
    ("__sep__", "", ""),
    ("Ask", "💬", "ask"),
    ("Settings", "⚙", "settings"),
]


class MainWindow(QMainWindow):
    # emitted when the user wants the app to actually quit (tray "Quit")
    quit_requested = Signal()

    def __init__(self, config: dict, api_key: str) -> None:
        super().__init__()
        self._config = config
        self._api_key = api_key
        self._engine_state = "stopped"

        # Real capture engine (built lazily on first Start) + its UI-thread signals.
        self._engine: CaptureEngine | None = None
        self._engine_sig = _EngineSignals()
        self._engine_sig.status.connect(self._on_engine_status)
        self._engine_sig.event.connect(self._on_engine_event)
        self._engine_sig.alert.connect(self._on_engine_alert)
        self._engine_sig.log.connect(self.log)
        self._engine_sig.error.connect(self._on_engine_error)

        self.setWindowTitle("horizon-monitor")
        self.resize(960, 680)
        self.setMinimumSize(760, 520)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_nav())
        self._stack = QStackedWidget()
        self._build_pages()
        body.addWidget(self._stack, 1)
        body_w = QWidget()
        body_w.setLayout(body)
        outer.addWidget(body_w, 1)

        outer.addWidget(self._build_activity_log())

        self._nav_buttons[0].setChecked(True)
        self._stack.setCurrentIndex(0)
        self.log("Ready. Capture engine wiring lands in the next phase.")

    # --------------------------------------------------------------- header

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(56)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(10)

        title = QLabel("horizon-monitor")
        title.setObjectName("AppTitle")
        lay.addWidget(title)
        lay.addSpacing(8)

        self._engine_pill = QLabel()
        self._engine_pill.setProperty("class", "pill")
        lay.addWidget(self._engine_pill)
        lay.addStretch(1)

        self._btn_start = QPushButton("Start")
        self._btn_start.setObjectName("Primary")
        self._btn_pause = QPushButton("Pause")
        self._btn_stop = QPushButton("Stop")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_pause.clicked.connect(self._on_pause)
        self._btn_stop.clicked.connect(self._on_stop)
        for b in (self._btn_start, self._btn_pause, self._btn_stop):
            lay.addWidget(b)

        self._refresh_engine()
        return header

    def _refresh_engine(self) -> None:
        labels = {"running": "● Running", "paused": "⏸ Paused", "stopped": "○ Stopped"}
        color = STATUS_COLORS.get(self._engine_state, COLORS["text_dim"])
        self._engine_pill.setText(labels.get(self._engine_state, self._engine_state))
        self._engine_pill.setStyleSheet(
            f"color: {color}; border: 1px solid {COLORS['border']};"
            f" border-radius: 11px; padding: 3px 10px; background: {COLORS['surface2']};"
        )
        self._btn_start.setEnabled(self._engine_state != "running")
        self._btn_pause.setEnabled(self._engine_state == "running")
        self._btn_stop.setEnabled(self._engine_state != "stopped")

    # ----------------------------------------------------------------- nav

    def _build_nav(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("NavRail")
        rail.setFixedWidth(168)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(10, 12, 10, 12)
        lay.setSpacing(4)

        self._nav_buttons: list[QPushButton] = []
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        idx = 0
        for label, emoji, _key in _NAV:
            if label == "__sep__":
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setStyleSheet(f"color: {COLORS['border']};")
                lay.addWidget(line)
                continue
            btn = QPushButton(f"  {emoji}   {label}")
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            page_index = idx
            btn.clicked.connect(lambda _=False, i=page_index: self._stack.setCurrentIndex(i))
            self._nav_group.addButton(btn)
            self._nav_buttons.append(btn)
            lay.addWidget(btn)
            idx += 1

        lay.addStretch(1)
        ver = QLabel("v0 · shell")
        ver.setObjectName("Dim")
        ver.setAlignment(Qt.AlignCenter)
        lay.addWidget(ver)
        return rail

    def _build_pages(self) -> None:
        # Order must match the non-separator entries in _NAV.
        self._monitor_page = MonitorPage()
        self._stack.addWidget(self._monitor_page)
        self._stack.addWidget(CollectPage())
        self._stack.addWidget(PullPage())
        self._stack.addWidget(PushPage())
        self._stack.addWidget(AssistPage(self._config, self._api_key))
        self._stack.addWidget(RemotePage(self._config, self._api_key))
        self._stack.addWidget(AskPage(self._config, self._api_key))
        self._stack.addWidget(self._settings_page())

        # Keep the running engine's alert settings in sync with the Monitor tab.
        self._monitor_page.enabled.toggled.connect(lambda _on: self._sync_engine_config())

    def _settings_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 20)
        t = QLabel("Settings")
        t.setObjectName("PageTitle")
        lay.addWidget(t)
        lay.addWidget(_coming_soon(
            "Edit config.toml in-app (poll interval, window titles, retention, control "
            "enable) and manage .env keys."))
        lay.addStretch(1)
        return page

    # --------------------------------------------------------- activity log

    def _build_activity_log(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Header")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 6, 12, 8)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        lbl = QLabel("Activity")
        lbl.setObjectName("SectionTitle")
        self._collapse = QPushButton("Hide")
        self._collapse.setMaximumWidth(64)
        bar.addWidget(lbl)
        bar.addStretch(1)
        bar.addWidget(self._collapse)
        lay.addLayout(bar)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(96)
        lay.addWidget(self._log)

        def toggle() -> None:
            vis = self._log.isVisible()
            self._log.setVisible(not vis)
            self._collapse.setText("Show" if vis else "Hide")
        self._collapse.clicked.connect(toggle)
        return frame

    def log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.appendPlainText(f"{ts}  {message}")

    # ----------------------------------------------------- engine handlers

    def _ensure_engine(self) -> CaptureEngine:
        if self._engine is None:
            engine = CaptureEngine(self._config, self._api_key)
            engine.on_status = self._engine_sig.status.emit
            engine.on_event = self._engine_sig.event.emit
            engine.on_alert = self._engine_sig.alert.emit
            engine.on_log = self._engine_sig.log.emit
            engine.on_error = self._engine_sig.error.emit
            self._engine = engine
        return self._engine

    def _sync_engine_config(self) -> None:
        """Push the Monitor tab's alert settings onto the engine before/while running."""
        if self._engine is None:
            return
        mp = self._monitor_page
        self._engine.alert_enabled = mp.enabled.isChecked()
        terms = [mp.terms.item(i).text() for i in range(mp.terms.count())]
        name = self._config.get("user", {}).get("display_name", "")
        if name:
            terms.append(name)
        self._engine.watch_terms = terms

    def _on_start(self) -> None:
        if self._engine_state == "paused" and self._engine is not None:
            self._engine.resume()
            return
        engine = self._ensure_engine()
        self._sync_engine_config()
        self.log("Starting monitor…")
        engine.start()

    def _on_pause(self) -> None:
        if self._engine is not None:
            self._engine.pause()

    def _on_stop(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        else:
            self._engine_state = "stopped"
            self._refresh_engine()

    # ---- worker-thread callbacks (arrive on the UI thread via _engine_sig)

    def _on_engine_status(self, state: str) -> None:
        self._engine_state = "running" if state == "running" else (
            "paused" if state == "paused" else "stopped"
        )
        self._refresh_engine()
        if state == "error":
            self.log("Engine error — see message above.")

    def _on_engine_event(self, ev) -> None:
        ch = f" «{ev.channel}»" if getattr(ev, "channel", "") else ""
        self.log(f"Collected [{ev.app}{ch}] {ev.speaker}: {ev.message[:60]}")

    def _on_engine_alert(self, ev) -> None:
        # Presence-only: name (+ channel), never the message body.
        ch = f" in {ev.channel}" if getattr(ev, "channel", "") else ""
        self.log(f"🔔 {ev.speaker} mentioned you{ch}")
        try:
            import winsound
            winsound.Beep(880, 250)
        except Exception:
            from PySide6.QtWidgets import QApplication
            QApplication.beep()

    def _on_engine_error(self, msg: str) -> None:
        self.log(f"Engine error: {msg}")
        self._engine_state = "stopped"
        self._refresh_engine()

    # ----------------------------------------------------------- window/tray

    def set_engine_icon_status(self) -> str:
        return self._engine_state

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt override
        """Hide to tray instead of quitting (the tray menu's Quit really exits)."""
        event.ignore()
        self.hide()
