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
    QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox,
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
        cfg_btn.setToolTip("Set up the bot token / chat id and send a test ping.")
        cfg_btn.clicked.connect(self._configure_telegram)
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

    def _configure_telegram(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        from src.telegram import TelegramNotifier

        tn = TelegramNotifier(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
        )
        if not tn.configured:
            QMessageBox.information(
                self,
                "Telegram presence alerts",
                "Pings your phone with only '🔔 <Name> mentioned you' — never the message "
                "body.\n\nTo enable:\n"
                "1. In Telegram, message @BotFather → /newbot → copy the token.\n"
                "2. Message your new bot once, then open\n"
                "   https://api.telegram.org/bot<token>/getUpdates and copy your chat id.\n"
                "3. Add to .env:\n"
                "     TELEGRAM_BOT_TOKEN=...\n"
                "     TELEGRAM_CHAT_ID=...\n"
                "4. Restart the app, then Configure → Send test.",
            )
            return
        if QMessageBox.question(
            self, "Telegram", "Credentials found. Send a test ping now?"
        ) == QMessageBox.StandardButton.Yes:
            ok, detail = tn.send_test()
            (QMessageBox.information if ok else QMessageBox.warning)(
                self, "Telegram test", detail
            )


# ----------------------------------------------------------------- preview tabs

class _CollectSignals(QObject):
    status = Signal(str)
    refreshed = Signal(dict, list)   # stats dict, channel list


class CollectPage(QWidget):
    """Record selected channels into the knowledge base + manage what's stored.

    The channel picker / record toggle feed the engine's collect sink (the main window
    syncs them). Stats + purge talk straight to EventStore/RAGPipeline (same stores the
    engine writes); reads are instant via SQLite, purges run on a worker thread.
    """

    def __init__(self, config: dict, api_key: str = "") -> None:
        super().__init__()
        self._config = config
        self._store = None   # lazy EventStore (SQLite — fast, read on the UI thread)

        self._sig = _CollectSignals()
        self._sig.status.connect(self._set_status)
        self._sig.refreshed.connect(self._apply_refresh)

        page, body = _page_scaffold(
            "Collect",
            "Record conversations into a private, on-disk knowledge base you can question "
            "in the Ask tab. Choose which channels to keep, and prune what you don't.",
        )

        # Record on/off
        rec_card, rl = _card()
        rec_row = QHBoxLayout()
        self.record = QCheckBox("Record conversations to the knowledge base")
        self.record.setFont(QFont("Segoe UI", 11))
        self.record.setChecked(True)
        rec_row.addWidget(self.record)
        rec_row.addStretch(1)
        rl.addLayout(rec_row)
        body.addWidget(rec_card)

        # Channels
        ch_card, cl = _card(
            "Channels to collect",
            "Collect everything, or tick the channels to keep. They appear here as "
            "they're seen on screen — Refresh after new ones show up.",
        )
        self.collect_all = QCheckBox("Collect all channels")
        self.collect_all.setChecked(True)
        cl.addWidget(self.collect_all)
        self.channels = QListWidget()
        self.channels.setMaximumHeight(150)
        cl.addWidget(self.channels)
        refresh_btn = QPushButton("Refresh channels")
        cl.addWidget(refresh_btn, 0, Qt.AlignLeft)
        body.addWidget(ch_card)

        # Knowledge base stats + purge
        kb_card, kl = _card("Knowledge base")
        self.stats_label = QLabel("…")
        self.stats_label.setWordWrap(True)
        kl.addWidget(self.stats_label)

        older_row = QHBoxLayout()
        older_row.addWidget(QLabel("Delete events older than"))
        self.older_days = QSpinBox()
        self.older_days.setRange(1, 365)
        self.older_days.setValue(30)
        self.older_days.setSuffix(" days")
        older_row.addWidget(self.older_days)
        self.btn_older = QPushButton("Delete")
        older_row.addWidget(self.btn_older)
        older_row.addStretch(1)
        kl.addLayout(older_row)

        self.btn_purge = QPushButton("Purge everything…")
        kl.addWidget(self.btn_purge, 0, Qt.AlignLeft)
        body.addWidget(kb_card)

        self.status = QLabel("")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        self.collect_all.toggled.connect(lambda on: self.channels.setEnabled(not on))
        refresh_btn.clicked.connect(self.refresh)
        self.btn_older.clicked.connect(self._delete_older)
        self.btn_purge.clicked.connect(self._purge_all)
        self.channels.setEnabled(not self.collect_all.isChecked())
        self.refresh()

    # ------------------------------------------------------------ helpers

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _store_conn(self):
        if self._store is None:
            from src.store import EventStore
            self._store = EventStore(
                self._config["rag"].get("events_db", "./data/events.db")
            )
            self._store.connect()
        return self._store

    def selected_channels(self) -> list[str]:
        out = []
        for i in range(self.channels.count()):
            it = self.channels.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.text())
        return out

    def refresh(self) -> None:
        try:
            store = self._store_conn()
            self._apply_refresh(store.stats(), store.channels())
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not read the store: {exc}")

    def _apply_refresh(self, stats: dict, channels: list) -> None:
        total = stats.get("total", 0)
        first, last = stats.get("first"), stats.get("last")
        span = f"{first[:10]} → {last[:10]}" if first else "no events yet"
        top = ", ".join(f"{c} ({n})" for c, n in stats.get("channels", [])[:5]) or "—"
        self.stats_label.setText(
            f"{total} event(s) · {span}\nTop channels: {top}"
        )
        keep = set(self.selected_channels())
        self.channels.blockSignals(True)
        self.channels.clear()
        for ch in channels:
            it = QListWidgetItem(ch)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(
                Qt.CheckState.Checked if (not keep or ch in keep) else Qt.CheckState.Unchecked
            )
            self.channels.addItem(it)
        self.channels.blockSignals(False)

    # ------------------------------------------------------------ purge ops

    def _run_store_op(self, label: str, fn) -> None:
        """Run fn(store, rag) -> result-message on a worker thread, then refresh."""
        self._set_status(f"{label}…")

        def worker() -> None:
            try:
                import os
                from src.rag import RAGPipeline
                store = self._store_conn()
                rcfg = self._config["rag"]
                rag = RAGPipeline(
                    db_path=rcfg["db_path"],
                    collection_name=rcfg["collection_name"],
                    embedding_provider=rcfg["embedding_provider"],
                    voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
                    top_k=rcfg["top_k"],
                )
                rag.connect()
                msg = fn(store, rag)
                self._sig.refreshed.emit(store.stats(), store.channels())
                self._sig.status.emit(msg)
            except Exception as exc:  # noqa: BLE001
                self._sig.status.emit(f"{label} failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _delete_older(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        days = self.older_days.value()
        if QMessageBox.question(
            self, "Delete old events",
            f"Delete every event older than {days} day(s) from both stores?",
        ) != QMessageBox.StandardButton.Yes:
            return

        def fn(store, rag) -> str:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            removed = store.expire_older_than(cutoff)
            rag.delete_older_than(cutoff)
            return f"Deleted {removed} event(s) older than {days}d."

        self._run_store_op(f"Deleting events older than {days}d", fn)

    def _purge_all(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.warning(
            self, "Purge everything",
            "Delete ALL stored events and embeddings? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return

        def fn(store, rag) -> str:
            removed = store.purge_all()
            rag.purge_all()
            return f"Purged everything ({removed} event(s) removed)."

        self._run_store_op("Purging the knowledge base", fn)


class _PullSignals(QObject):
    status = Signal(str)
    result = Signal(str, int)   # text, screens captured
    error = Signal(str)


class PullPage(QWidget):
    """Read a file out of the remote VS Code into a local viewer.

    Clipboard copy-OUT is blocked on this VDI (DLP), so the default channel is OCR:
    open the file, then scroll + OCR page-by-page and stitch the captures. OCR is LOSSY
    (confuses 1/l/I, drops indentation) — the result is flagged 'verify before trusting'.
    An optional 'lossless clipboard' path is offered for VDIs where copy-out is allowed.

    Drives the remote (scroll/type), so it's gated behind [control].enabled like Remote.
    """

    def __init__(self, config: dict, api_key: str = "") -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._busy = False

        self._sig = _PullSignals()
        self._sig.status.connect(self._set_status)
        self._sig.result.connect(self._on_result)
        self._sig.error.connect(self._on_error)

        page, body = _page_scaffold(
            "Pull code",
            "Read a file out of the remote VS Code so local AI can work with it. Copy-out "
            "is blocked here, so this OCRs the editor — lossy, so verify before trusting.",
        )

        if not self._control_enabled:
            warn, _ = _card(
                "Remote control is off",
                "Set [control].enabled = true in config.toml to pull from the remote.",
            )
            body.addWidget(warn)

        # Pull controls
        ctl_card, cl = _card(
            "Pull a file",
            "Give a path to open it in the remote VS Code first, or leave blank to read "
            "what's already on screen. The view is read top-to-bottom.",
        )
        self.path = QLineEdit()
        self.path.setPlaceholderText("src/app.py  — or blank to read the current screen")
        cl.addWidget(self.path)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("Max screens:"))
        self.max_screens = QSpinBox()
        self.max_screens.setRange(1, 60)
        self.max_screens.setValue(12)
        opt_row.addWidget(self.max_screens)
        opt_row.addSpacing(16)
        self.lossless = QCheckBox("Try lossless clipboard copy first")
        self.lossless.setToolTip(
            "Only works where Horizon clipboard copy-out is enabled; falls back to OCR."
        )
        opt_row.addWidget(self.lossless)
        opt_row.addStretch(1)
        cl.addLayout(opt_row)

        self.btn_pull = QPushButton("Pull")
        self.btn_pull.setObjectName("Primary")
        self.btn_pull.clicked.connect(self._pull)
        cl.addWidget(self.btn_pull, 0, Qt.AlignLeft)
        self.status = QLabel("Ready." if self._control_enabled else "Disabled.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        cl.addWidget(self.status)
        body.addWidget(ctl_card)

        # Result
        res_card, rl = _card("Pulled text")
        self.warn = QLabel("⚠ OCR is lossy — verify before trusting, especially code.")
        self.warn.setObjectName("Dim")
        self.warn.setWordWrap(True)
        self.warn.setVisible(False)
        rl.addWidget(self.warn)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(QFont("Consolas", 10))
        self.out.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.out.setMinimumHeight(220)
        rl.addWidget(self.out)
        self.btn_save = QPushButton("Save to file…")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        rl.addWidget(self.btn_save, 0, Qt.AlignLeft)
        body.addWidget(res_card, 1)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        if not self._control_enabled:
            self.btn_pull.setEnabled(False)
            self.path.setEnabled(False)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _controller(self, client):
        ctl = self._config.get("control", {})
        from src.controller import RemoteController
        return RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
            screen=ctl.get("screen", 0),
        )

    def _pull(self) -> None:
        if self._busy or not self._control_enabled:
            return
        path = self.path.text().strip()
        max_screens = self.max_screens.value()
        lossless = self.lossless.isChecked()
        self._busy = True
        self.btn_pull.setEnabled(False)
        self.btn_save.setEnabled(False)
        threading.Thread(
            target=self._run, args=(path, max_screens, lossless), daemon=True
        ).start()

    def _run(self, path: str, max_screens: int, lossless: bool) -> None:
        async def go() -> tuple[str, int]:
            from src.mcp_client import HorizonMCPClient
            cfg = self._config
            async with HorizonMCPClient(
                cfg["mcp"]["server_path"], cfg["mcp"]["command"]
            ) as client:
                c = self._controller(client)
                if path:
                    self._sig.status.emit(f"Opening {path} in the remote…")
                    await c.open_file(path)
                # Lossless clipboard copy first (works only if copy-out is enabled).
                if lossless:
                    self._sig.status.emit("Trying lossless clipboard copy…")
                    try:
                        return await c.copy_from_remote(), 0
                    except Exception as exc:  # noqa: BLE001 — fall back to OCR
                        self._sig.status.emit(f"Clipboard copy unavailable ({exc}); OCR…")
                self._sig.status.emit("Reading the screen via OCR (scroll-stitching)…")
                sx, sy = await c.screen_center()
                return await c.read_scrolling(sx, sy, max_screens=max_screens)

        try:
            import asyncio
            text, screens = asyncio.run(go())
            self._sig.result.emit(text, screens)
        except Exception as exc:  # noqa: BLE001
            self._sig.error.emit(str(exc))

    def _on_result(self, text: str, screens: int) -> None:
        self._busy = False
        self.btn_pull.setEnabled(True)
        self.out.setPlainText(text)
        ocr = screens > 0
        self.warn.setVisible(ocr)
        self.btn_save.setEnabled(bool(text))
        how = f"OCR · {screens} screen(s)" if ocr else "lossless clipboard"
        self._set_status(f"Pulled {len(text)} chars ({how}).")

    def _on_error(self, msg: str) -> None:
        self._busy = False
        self.btn_pull.setEnabled(True)
        self._set_status(f"Pull failed: {msg}")

    def _save(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        text = self.out.toPlainText()
        if not text:
            return
        suggested = (self.path.text().strip().split("/")[-1] or "pulled.txt")
        fname, _ = QFileDialog.getSaveFileName(self, "Save pulled text", suggested)
        if not fname:
            return
        try:
            with open(fname, "w", encoding="utf-8") as fh:
                fh.write(text)
            self._set_status(f"Saved {len(text)} chars → {fname}")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Save failed: {exc}")


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
