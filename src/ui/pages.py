"""Tab pages for the horizon-monitor window.

Phase 1 scope:
  - MonitorPage  : full alert controls laid out; "Test beep" works. Engine wiring lands
                   in a later phase (the toggle/terms are kept in memory for now).
  - CollectPage / PullPage / PushPage : styled previews of the planned feature.
  - AskPage      : fully functional Q&A over the RAG store (reuses QueryAgent streaming).

Every page is a self-contained QWidget so the shell can add "more to come" with no churn.
"""

from __future__ import annotations

import asyncio
import os
import threading

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from .theme import COLORS


# --------------------------------------------------------------------- helpers

def _card(title: str = "", subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
    """A rounded surface card; returns (frame, inner_layout) for adding content."""
    card = QFrame()
    card.setProperty("class", "card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 16)
    lay.setSpacing(10)
    if title:
        t = QLabel(title)
        t.setObjectName("SectionTitle")
        lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("Dim")
        s.setWordWrap(True)
        lay.addWidget(s)
    return card, lay


def _page_scaffold(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
    """A scrollable page with a title/subtitle header; returns (page, body_layout)."""
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(24, 20, 24, 20)
    outer.setSpacing(14)

    head = QVBoxLayout()
    head.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    head.addWidget(t)
    head.addWidget(s)
    outer.addLayout(head)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    inner = QWidget()
    body = QVBoxLayout(inner)
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(14)
    scroll.setWidget(inner)
    outer.addWidget(scroll, 1)
    return page, body


def _coming_soon(text: str) -> QLabel:
    lbl = QLabel("🛠  " + text)
    lbl.setObjectName("Dim")
    lbl.setWordWrap(True)
    return lbl


# ----------------------------------------------------------------- Monitor tab

class MonitorPage(QWidget):
    """Watch for your name / keywords → local beep + (later) Telegram ping.

    Presence-only: alerts say *who* mentioned you, never the message text.
    """

    def __init__(self) -> None:
        super().__init__()
        page, body = _page_scaffold(
            "Monitor",
            "Get pinged the moment someone mentions you — so stepping away never looks "
            "like you went dark. No message content ever leaves this machine.",
        )

        # On/off
        top, toplay = _card()
        row = QHBoxLayout()
        self.enabled = QCheckBox("Watch for mentions")
        self.enabled.setFont(QFont("Segoe UI", 11))
        row.addWidget(self.enabled)
        row.addStretch(1)
        self._state = QLabel("Off")
        self._state.setObjectName("Dim")
        row.addWidget(self._state)
        toplay.addLayout(row)
        self.enabled.toggled.connect(
            lambda on: self._state.setText("Armed — will alert on a match" if on else "Off")
        )
        body.addWidget(top)

        # Watch terms
        terms_card, terms_lay = _card(
            "Watch terms", "Your name and any keywords that count as 'about you'."
        )
        add_row = QHBoxLayout()
        self.term_input = QLineEdit()
        self.term_input.setPlaceholderText("e.g. Dmitry, my-project, the deploy…")
        add_btn = QPushButton("Add")
        add_row.addWidget(self.term_input, 1)
        add_row.addWidget(add_btn)
        terms_lay.addLayout(add_row)
        self.terms = QListWidget()
        self.terms.setMaximumHeight(130)
        terms_lay.addWidget(self.terms)
        rm_btn = QPushButton("Remove selected")
        terms_lay.addWidget(rm_btn, 0, Qt.AlignLeft)
        add_btn.clicked.connect(self._add_term)
        self.term_input.returnPressed.connect(self._add_term)
        rm_btn.clicked.connect(self._remove_term)
        body.addWidget(terms_card)

        # Notify channels
        notify_card, notify_lay = _card("How to alert me")
        beep_row = QHBoxLayout()
        self.beep = QCheckBox("Beep on this machine")
        self.beep.setChecked(True)
        test_beep = QPushButton("Test")
        test_beep.setMaximumWidth(70)
        test_beep.clicked.connect(self._test_beep)
        beep_row.addWidget(self.beep)
        beep_row.addStretch(1)
        beep_row.addWidget(test_beep)
        notify_lay.addLayout(beep_row)

        tg_row = QHBoxLayout()
        self.telegram = QCheckBox("Ping my phone via Telegram")
        cfg_btn = QPushButton("Configure…")
        cfg_btn.setMaximumWidth(110)
        cfg_btn.setEnabled(False)
        cfg_btn.setToolTip("Telegram bot setup arrives with the alert engine (next phase).")
        tg_row.addWidget(self.telegram)
        tg_row.addStretch(1)
        tg_row.addWidget(cfg_btn)
        notify_lay.addLayout(tg_row)
        notify_lay.addWidget(_coming_soon(
            "Telegram sends only ‘🔔 <Name> mentioned you’ — never the message body."
        ))
        body.addWidget(notify_card)

        # Cooldown
        cd_card, cd_lay = _card("Don't spam me")
        cd_row = QHBoxLayout()
        cd_row.addWidget(QLabel("Re-alert cooldown:"))
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 120)
        self.cooldown.setValue(5)
        self.cooldown.setSuffix(" min")
        cd_row.addWidget(self.cooldown)
        cd_row.addStretch(1)
        cd_lay.addLayout(cd_row)
        body.addWidget(cd_card)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

    def _add_term(self) -> None:
        text = self.term_input.text().strip()
        if text:
            self.terms.addItem(text)
            self.term_input.clear()

    def _remove_term(self) -> None:
        for item in self.terms.selectedItems():
            self.terms.takeItem(self.terms.row(item))

    def _test_beep(self) -> None:
        try:
            import winsound
            winsound.Beep(880, 250)
        except Exception:
            from PySide6.QtWidgets import QApplication
            QApplication.beep()


# ----------------------------------------------------------------- preview tabs

class CollectPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        page, body = _page_scaffold(
            "Collect",
            "Record selected channels into a private knowledge base you can question later.",
        )
        c1, l1 = _card("Channels to collect",
                       "Channels fill in here as they're seen on screen — tick the ones to keep.")
        l1.addWidget(_coming_soon("Channel picker + allow-all, wired to the capture engine."))
        body.addWidget(c1)
        c2, l2 = _card("Knowledge base", "Stored events, channels, date range, size.")
        l2.addWidget(_coming_soon("Stats card + Purge / retention controls (already in backend)."))
        body.addWidget(c2)
        c3, l3 = _card("Ask the knowledge base")
        l3.addWidget(_coming_soon("Embedded Q&A — for now use the Ask tab."))
        body.addWidget(c3)
        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)


class PullPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        page, body = _page_scaffold(
            "Pull code",
            "Bring a file (or a whole project) out of the remote VS Code so AI can read it.",
        )
        c1, l1 = _card("Pull a file")
        l1.addWidget(_coming_soon(
            "Open file → scroll + OCR page-by-page, stitched, with a progress bar."))
        l1.addWidget(_coming_soon(
            "Heads-up: clipboard copy-out is blocked on this VDI, so reads are OCR — "
            "lossy. Pulled files get a ‘verify before trusting’ flag."))
        body.addWidget(c1)
        c2, l2 = _card("Pull the whole project")
        l2.addWidget(_coming_soon("Get the file tree → checklist → pull each file in turn."))
        body.addWidget(c2)
        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)


class PushPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        page, body = _page_scaffold(
            "Push code",
            "Send new or AI-edited files back into the remote VS Code project.",
        )
        c1, l1 = _card("Push a file")
        l1.addWidget(_coming_soon(
            "Open target → paste whole document (the reliable write channel)."))
        l1.addWidget(_coming_soon(
            "Shows a diff vs the last pulled version + an explicit Confirm before every "
            "write, so a real file is never silently clobbered."))
        body.addWidget(c1)
        c2, l2 = _card("Push multiple files")
        l2.addWidget(_coming_soon("Batch new/updated files; new files created via Ctrl+N → paste → save."))
        body.addWidget(c2)
        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)


# --------------------------------------------------------------------- Ask tab

class _AskSignals(QObject):
    chunk = Signal(str)
    done = Signal()
    error = Signal(str)


class AskPage(QWidget):
    """Functional Q&A over the accumulated RAG store (Claude Sonnet, streamed)."""

    def __init__(self, config: dict, api_key: str) -> None:
        super().__init__()
        self._config = config
        self._api_key = api_key
        self._rag = None
        self._sig = _AskSignals()
        self._sig.chunk.connect(self._append)
        self._sig.done.connect(self._on_done)
        self._sig.error.connect(lambda e: (self._append(f"\n[Error: {e}]\n"), self._on_done()))

        page, body = _page_scaffold(
            "Ask",
            "Natural-language questions over everything collected so far.",
        )
        card, lay = _card()
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("What did anyone say about the deployment this morning?")
        self.send = QPushButton("Ask")
        self.send.setObjectName("Primary")
        row.addWidget(self.input, 1)
        row.addWidget(self.send)
        lay.addLayout(row)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMinimumHeight(280)
        lay.addWidget(self.out)
        body.addWidget(card, 1)
        QVBoxLayout(self).addWidget(page)

        self.send.clicked.connect(self._ask)
        self.input.returnPressed.connect(self._ask)

    def _append(self, text: str) -> None:
        self.out.insertPlainText(text)
        self.out.ensureCursorVisible()

    def _on_done(self) -> None:
        self.send.setEnabled(True)
        self.input.setEnabled(True)
        self.input.setFocus()

    def _ask(self) -> None:
        q = self.input.text().strip()
        if not q or not self.send.isEnabled():
            return
        self.input.clear()
        self.out.setPlainText(f"Q: {q}\n\nA: ")
        self.send.setEnabled(False)
        self.input.setEnabled(False)
        threading.Thread(target=self._run, args=(q,), daemon=True).start()

    def _run(self, q: str) -> None:
        try:
            from src.rag import RAGPipeline
            from src.agent import QueryAgent
            if self._rag is None:
                cfg = self._config["rag"]
                import os
                self._rag = RAGPipeline(
                    db_path=cfg["db_path"],
                    collection_name=cfg["collection_name"],
                    embedding_provider=cfg["embedding_provider"],
                    voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
                    top_k=cfg["top_k"],
                )
                self._rag.connect()
            agent = QueryAgent(
                api_key=self._api_key,
                model=self._config["claude"]["query_model"],
                rag=self._rag,
                max_tokens=self._config["claude"]["max_tokens"],
            )
            agent.query(q, on_chunk=lambda t: self._sig.chunk.emit(t))
            self._sig.done.emit()
        except Exception as exc:  # noqa: BLE001 — surface any failure in the UI
            self._sig.error.emit(str(exc))


# ------------------------------------------------------------------ Assist tab

class _AssistSignals(QObject):
    """Marshal the agent's worker-thread callbacks onto the Qt UI thread."""
    text = Signal(str)
    action = Signal(str)
    result = Signal(str)
    status = Signal(str)
    finished = Signal()
    error = Signal(str)


class AssistPage(QWidget):
    """Direct help: describe a task, the agent looks at the remote screen and does it.

    Advise mode is read-only (suggests the steps); Act mode performs them, confirming
    every input action. Wraps src.assistant.ComputerUseAgent on a worker thread.
    """

    def __init__(self, config: dict, api_key: str) -> None:
        super().__init__()
        self._config = config
        self._api_key = api_key
        self._agent = None          # built lazily on first run
        self._running = False

        self._sig = _AssistSignals()
        self._sig.text.connect(lambda s: self._say("Assistant", s))
        self._sig.action.connect(self._on_action)
        self._sig.result.connect(lambda s: self._say("·", s))
        self._sig.status.connect(self._set_status)
        self._sig.finished.connect(self._on_finished)
        self._sig.error.connect(lambda e: (self._say("Error", e), self._on_finished()))

        page, body = _page_scaffold(
            "Assist",
            "Describe a task on the remote desktop in plain language. The agent looks at "
            "the screen and helps — read-only advice, or hands-on with your confirmation.",
        )

        # Mode + status
        mode_card, mode_lay = _card()
        mode_row = QHBoxLayout()
        self.mode_advise = QRadioButton("Advise (read-only)")
        self.mode_act = QRadioButton("Act (perform, confirm each step)")
        self.mode_advise.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.mode_advise)
        grp.addButton(self.mode_act)
        mode_row.addWidget(self.mode_advise)
        mode_row.addWidget(self.mode_act)
        mode_row.addStretch(1)
        self.new_btn = QPushButton("New session")
        self.new_btn.setMaximumWidth(120)
        self.new_btn.clicked.connect(self._new_session)
        mode_row.addWidget(self.new_btn)
        mode_lay.addLayout(mode_row)
        self.status = QLabel("Ready.")
        self.status.setObjectName("Dim")
        mode_lay.addWidget(self.status)
        body.addWidget(mode_card)

        # Transcript
        chat_card, chat_lay = _card()
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setMinimumHeight(200)
        chat_lay.addWidget(self.transcript)

        # Confirmation bar (hidden until an action is proposed in Act mode)
        self.confirm_bar = QFrame()
        self.confirm_bar.setProperty("class", "card")
        cb = QHBoxLayout(self.confirm_bar)
        cb.setContentsMargins(12, 8, 12, 8)
        self.confirm_label = QLabel()
        self.confirm_label.setWordWrap(True)
        cb.addWidget(self.confirm_label, 1)
        self.btn_confirm = QPushButton("Confirm")
        self.btn_confirm.setObjectName("Primary")
        self.btn_skip = QPushButton("Skip")
        self.btn_stop_action = QPushButton("Stop")
        self.btn_confirm.clicked.connect(lambda: self._resolve("confirm"))
        self.btn_skip.clicked.connect(lambda: self._resolve("skip"))
        self.btn_stop_action.clicked.connect(lambda: self._resolve("stop"))
        for b in (self.btn_confirm, self.btn_skip, self.btn_stop_action):
            cb.addWidget(b)
        self.confirm_bar.setVisible(False)
        chat_lay.addWidget(self.confirm_bar)
        body.addWidget(chat_card, 1)

        # Input row
        in_card, in_lay = _card()
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "e.g. Help me switch from this feature branch to master — do not stash my changes"
        )
        self.send = QPushButton("Send")
        self.send.setObjectName("Primary")
        self.stop = QPushButton("Stop")
        self.stop.setEnabled(False)
        row.addWidget(self.input, 1)
        row.addWidget(self.send)
        row.addWidget(self.stop)
        in_lay.addLayout(row)
        body.addWidget(in_card)

        QVBoxLayout(self).addWidget(page)

        self.send.clicked.connect(self._send)
        self.input.returnPressed.connect(self._send)
        self.stop.clicked.connect(self._stop_run)

    # ----------------------------------------------------------- transcript

    def _say(self, who: str, text: str) -> None:
        self.transcript.appendPlainText(f"{who}: {text}" if who else text)
        self.transcript.ensureCursorVisible()

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    # ----------------------------------------------------------- run control

    def _ensure_agent(self):
        if self._agent is None:
            from src.assistant import ComputerUseAgent
            agent = ComputerUseAgent(self._config, self._api_key)
            agent.on_text = self._sig.text.emit
            agent.on_action = self._sig.action.emit
            agent.on_result = self._sig.result.emit
            agent.on_status = self._sig.status.emit
            agent.on_finished = self._sig.finished.emit
            agent.on_error = self._sig.error.emit
            self._agent = agent
        return self._agent

    def _send(self) -> None:
        goal = self.input.text().strip()
        if not goal or self._running:
            return
        mode = "act" if self.mode_act.isChecked() else "advise"
        self.input.clear()
        self._say("You", goal)
        self._set_status("Working…")
        self._running = True
        self.send.setEnabled(False)
        self.input.setEnabled(False)
        self.stop.setEnabled(True)
        agent = self._ensure_agent()
        threading.Thread(target=agent.run_turn, args=(goal, mode), daemon=True).start()

    def _stop_run(self) -> None:
        if self._agent is not None:
            self._agent.request_stop()
        self._set_status("Stopping…")

    def _on_finished(self) -> None:
        self._running = False
        self.confirm_bar.setVisible(False)
        self.send.setEnabled(True)
        self.input.setEnabled(True)
        self.stop.setEnabled(False)
        self._set_status("Ready.")
        self.input.setFocus()

    def _new_session(self) -> None:
        if self._running:
            return
        if self._agent is not None:
            self._agent.reset()
        self.transcript.clear()
        self._set_status("New session — ready.")

    # ----------------------------------------------------------- confirmation

    def _on_action(self, desc: str) -> None:
        self._say("▶ Proposes", desc)
        self.confirm_label.setText(f"Perform this action?  {desc}")
        self.confirm_bar.setVisible(True)
        self._set_status("Waiting for your confirmation…")

    def _resolve(self, decision: str) -> None:
        self.confirm_bar.setVisible(False)
        if self._agent is not None:
            self._agent.resolve_confirmation(decision)
        self._set_status("Working…" if decision != "stop" else "Stopping…")


# ------------------------------------------------------------------ Remote tab

class _RemoteSignals(QObject):
    """Marshal control-worker status onto the Qt UI thread."""
    status = Signal(str)
    keepalive = Signal(bool)   # True = running, False = stopped


class RemotePage(QWidget):
    """Drive the Horizon session directly: unlock, keep awake, focus, launch an app.

    These are the opt-in WRITE actions from RemoteController, gated behind
    [control].enabled. Each runs on a worker thread with its own MCP connection
    (mirrors the CLI `remote <action>` and the legacy tray), so the UI never blocks.
    """

    def __init__(self, config: dict, api_key: str = "") -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        # keep-awake worker handle (anti-idle nudges)
        self._ka_thread: threading.Thread | None = None
        self._ka_stop: threading.Event | None = None

        self._sig = _RemoteSignals()
        self._sig.status.connect(self._set_status)
        self._sig.keepalive.connect(self._reflect_keepalive)

        page, body = _page_scaffold(
            "Remote",
            "Reach into the Horizon session itself — unlock it, stop it locking, bring it "
            "forward, or open an app inside it. These type and click into a corporate "
            "desktop, so they only run when you trigger them.",
        )

        if not self._control_enabled:
            warn, wlay = _card(
                "Remote control is off",
                "Set [control].enabled = true in config.toml to enable these actions.",
            )
            body.addWidget(warn)

        # Status line (shared by every action)
        self.status = QLabel("Ready." if self._control_enabled else "Disabled.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)

        # --- Unlock
        unlock_card, ul = _card(
            "Unlock remote desktop",
            "Sends Ctrl+Alt+Del and types your password (HORIZON_PASSWORD in .env). "
            "Duo/MFA is approved on your phone — this only gets you to the prompt and signs in.",
        )
        self.type_only = QCheckBox("Type only — don't press Sign in (verify the entry first)")
        ul.addWidget(self.type_only)
        self.btn_unlock = QPushButton("Unlock remote desktop")
        self.btn_unlock.setObjectName("Primary")
        self.btn_unlock.clicked.connect(self._unlock)
        ul.addWidget(self.btn_unlock, 0, Qt.AlignLeft)
        body.addWidget(unlock_card)

        # --- Keep awake
        ka_interval = float(config.get("control", {}).get("keepalive_interval_seconds", 120))
        ka_card, kal = _card(
            "Keep awake",
            f"Nudges the cursor one pixel every {ka_interval:g}s so the session never idles "
            "into a lock. Each nudge briefly steals local focus — use it when you step away.",
        )
        ka_row = QHBoxLayout()
        self.btn_keepalive = QPushButton("Start")
        self.btn_keepalive.clicked.connect(self._toggle_keepalive)
        self.ka_state = QLabel("Off")
        self.ka_state.setObjectName("Dim")
        ka_row.addWidget(self.btn_keepalive)
        ka_row.addWidget(self.ka_state)
        ka_row.addStretch(1)
        kal.addLayout(ka_row)
        body.addWidget(ka_card)

        # --- Quick actions
        qa_card, qal = _card(
            "Quick actions",
            "Bring the Horizon window forward, or open/activate an app inside the remote "
            "via its Start menu (e.g. Microsoft Teams, Symphony).",
        )
        self.btn_front = QPushButton("Bring Horizon to front")
        self.btn_front.clicked.connect(
            lambda: self._run_control("bring to front", lambda c: c.bring_to_front())
        )
        qal.addWidget(self.btn_front, 0, Qt.AlignLeft)
        app_row = QHBoxLayout()
        self.app_input = QLineEdit()
        self.app_input.setPlaceholderText("App name as in the remote Start menu…")
        self.btn_launch = QPushButton("Launch / activate")
        self.btn_launch.clicked.connect(self._launch)
        self.app_input.returnPressed.connect(self._launch)
        app_row.addWidget(self.app_input, 1)
        app_row.addWidget(self.btn_launch)
        qal.addLayout(app_row)
        body.addWidget(qa_card)

        body.addWidget(self.status)
        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        if not self._control_enabled:
            for b in (self.btn_unlock, self.btn_keepalive, self.btn_front, self.btn_launch):
                b.setEnabled(False)
            self.app_input.setEnabled(False)
            self.type_only.setEnabled(False)

    # ------------------------------------------------------------ helpers

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _controller(self, client):
        ctl = self._config.get("control", {})
        from src.controller import RemoteController
        return RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            screen=ctl.get("screen", 0),
        )

    def _run_control(self, label: str, factory) -> None:
        """Run one controller coroutine on a worker thread with a fresh MCP client.

        `factory` takes a RemoteController and returns the coroutine to await.
        """
        if not self._control_enabled:
            self._sig.status.emit("Remote control disabled — set [control].enabled=true.")
            return

        def worker() -> None:
            async def go() -> None:
                from src.mcp_client import HorizonMCPClient
                cfg = self._config
                async with HorizonMCPClient(
                    cfg["mcp"]["server_path"], cfg["mcp"]["command"]
                ) as client:
                    await factory(self._controller(client))

            self._sig.status.emit(f"{label}…")
            try:
                asyncio.run(go())
                self._sig.status.emit(f"{label} — done.")
            except Exception as exc:  # noqa: BLE001 — surface failures in the UI
                self._sig.status.emit(f"{label} failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ actions

    def _unlock(self) -> None:
        password = os.environ.get("HORIZON_PASSWORD", "")
        if not password:
            self._sig.status.emit("HORIZON_PASSWORD not set in .env — cannot unlock.")
            return
        submit = not self.type_only.isChecked()
        tail = " (signing in)" if submit else " (type only — press Sign in yourself)"
        self._run_control(
            "unlock" + tail, lambda c: c.unlock(password, submit=submit)
        )

    def _launch(self) -> None:
        app = self.app_input.text().strip()
        if not app:
            return
        self._run_control(
            f"launch/activate '{app}'", lambda c: c.launch_or_activate(app)
        )

    # ------------------------------------------------------------ keep-awake

    def _toggle_keepalive(self) -> None:
        if not self._control_enabled:
            self._sig.status.emit("Remote control disabled — set [control].enabled=true.")
            return
        if self._ka_thread:                       # running -> stop
            if self._ka_stop:
                self._ka_stop.set()
            self._ka_thread = None
            self._sig.keepalive.emit(False)
            self._sig.status.emit("Keep-awake: off.")
        else:                                     # stopped -> start
            self._ka_stop = threading.Event()
            self._ka_thread = threading.Thread(
                target=self._ka_worker, args=(self._ka_stop,), daemon=True
            )
            self._ka_thread.start()
            self._sig.keepalive.emit(True)
            self._sig.status.emit("Keep-awake: on.")

    def _reflect_keepalive(self, running: bool) -> None:
        self.btn_keepalive.setText("Stop" if running else "Start")
        self.ka_state.setText("On — nudging the remote" if running else "Off")

    def _ka_worker(self, stop: threading.Event) -> None:
        """Hold one MCP connection and nudge the remote until `stop` is set."""
        ctl = self._config.get("control", {})
        secs = float(ctl.get("keepalive_interval_seconds", 120.0))

        async def go() -> None:
            from src.mcp_client import HorizonMCPClient
            cfg = self._config
            async with HorizonMCPClient(
                cfg["mcp"]["server_path"], cfg["mcp"]["command"]
            ) as client:
                c = self._controller(client)
                while not stop.is_set():
                    try:
                        await c.nudge()
                    except Exception as exc:  # noqa: BLE001
                        self._sig.status.emit(f"Keep-awake nudge failed: {exc}")
                    stop.wait(secs)

        try:
            asyncio.run(go())
        except Exception as exc:  # noqa: BLE001
            self._sig.status.emit(f"Keep-awake stopped: {exc}")
