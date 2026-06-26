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
import re
import threading
from pathlib import Path

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

    def __init__(self, config: dict, api_key: str = "", pulled: dict | None = None) -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._busy = False
        # Shared {remote_path: pulled_text} so the Push tab can diff against what was pulled.
        self._pulled = pulled if pulled is not None else {}
        self._pull_path = ""

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
        self._pull_path = path
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
        if self._pull_path and text:
            self._pulled[self._pull_path] = text   # baseline for the Push tab's diff
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


class _PushSignals(QObject):
    status = Signal(str)
    done = Signal(str)     # success message
    error = Signal(str)


class PushPage(QWidget):
    """Write a local/edited file back into the remote VS Code.

    The reliable write channel is paste (type_text fights autocomplete/auto-indent):
    open the target, select-all, paste the whole document, optionally save. Before any
    write you see a unified diff vs the last pulled version of that file and must Confirm,
    so a real file is never silently clobbered. Gated behind [control].enabled.
    """

    def __init__(self, config: dict, api_key: str = "", pulled: dict | None = None) -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._pulled = pulled if pulled is not None else {}
        self._busy = False

        self._sig = _PushSignals()
        self._sig.status.connect(self._set_status)
        self._sig.done.connect(self._on_done)
        self._sig.error.connect(self._on_error)

        page, body = _page_scaffold(
            "Push code",
            "Send a new or AI-edited file back into the remote VS Code. You review a diff "
            "and confirm before anything is overwritten.",
        )

        if not self._control_enabled:
            warn, _ = _card(
                "Remote control is off",
                "Set [control].enabled = true in config.toml to push to the remote.",
            )
            body.addWidget(warn)

        # What to push
        src_card, sl = _card(
            "What to push",
            "Load a local file or paste/type the content below — this exact text replaces "
            "the remote document.",
        )
        load_row = QHBoxLayout()
        self.local_path = QLineEdit()
        self.local_path.setPlaceholderText("local file to load (optional)…")
        load_btn = QPushButton("Load…")
        load_btn.clicked.connect(self._load)
        load_row.addWidget(self.local_path, 1)
        load_row.addWidget(load_btn)
        sl.addLayout(load_row)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setMinimumHeight(150)
        self.editor.setPlaceholderText("Content to push…")
        sl.addWidget(self.editor)
        body.addWidget(src_card)

        # Where to push
        dst_card, dl = _card(
            "Where to push",
            "Remote path to open before pasting (blank = paste into the current editor).",
        )
        self.remote_path = QLineEdit()
        self.remote_path.setPlaceholderText("src/app.py")
        dl.addWidget(self.remote_path)
        self.save_after = QCheckBox("Save after paste (Ctrl+S)")
        self.save_after.setChecked(True)
        dl.addWidget(self.save_after)
        body.addWidget(dst_card)

        # Diff + push
        act_card, al = _card("Review & push")
        btn_row = QHBoxLayout()
        self.btn_diff = QPushButton("Preview diff")
        self.btn_diff.clicked.connect(self._preview_diff)
        self.btn_push = QPushButton("Push to remote")
        self.btn_push.setObjectName("Primary")
        self.btn_push.clicked.connect(self._push)
        btn_row.addWidget(self.btn_diff)
        btn_row.addWidget(self.btn_push)
        btn_row.addStretch(1)
        al.addLayout(btn_row)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(QFont("Consolas", 10))
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setMinimumHeight(150)
        al.addWidget(self.diff)
        self.status = QLabel("Ready." if self._control_enabled else "Disabled.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        al.addWidget(self.status)
        body.addWidget(act_card, 1)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        if not self._control_enabled:
            self.btn_push.setEnabled(False)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _load(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        fname, _ = QFileDialog.getOpenFileName(self, "Load file to push")
        if not fname:
            return
        try:
            with open(fname, encoding="utf-8") as fh:
                self.editor.setPlainText(fh.read())
            self.local_path.setText(fname)
            if not self.remote_path.text().strip():
                self.remote_path.setText(fname.replace("\\", "/").split("/")[-1])
            self._set_status(f"Loaded {fname}.")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Load failed: {exc}")

    def _baseline(self) -> tuple[str, bool]:
        """Last-pulled text for the remote path, and whether one was found."""
        key = self.remote_path.text().strip()
        if key in self._pulled:
            return self._pulled[key], True
        return "", False

    def _diff_lines(self) -> tuple[list[str], int, int, bool]:
        import difflib
        base, found = self._baseline()
        new = self.editor.toPlainText()
        a = base.splitlines()
        b = new.splitlines()
        diff = list(difflib.unified_diff(a, b, "pulled", "to push", lineterm=""))
        added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
        return diff, added, removed, found

    def _preview_diff(self) -> None:
        diff, added, removed, found = self._diff_lines()
        if not self.editor.toPlainText():
            self._set_status("Nothing to push — the content is empty.")
            return
        base_note = "vs last pulled" if found else "no pulled baseline — all new"
        self.diff.setPlainText(
            "\n".join(diff) if diff else "(identical to the pulled version)"
        )
        self._set_status(f"+{added} / -{removed} lines ({base_note}).")

    def _push(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if self._busy or not self._control_enabled:
            return
        text = self.editor.toPlainText()
        if not text:
            self._set_status("Nothing to push — the content is empty.")
            return
        target = self.remote_path.text().strip()
        save = self.save_after.isChecked()
        _diff, added, removed, found = self._diff_lines()
        where = f"open '{target}' then " if target else "the current remote editor — "
        base_note = "" if found else "  (no pulled baseline to compare against)"
        if QMessageBox.warning(
            self, "Confirm push",
            f"{where}select-all and paste {len(text)} chars"
            f"{' then save' if save else ''}.\n\n"
            f"Changes vs pulled: +{added} / -{removed} lines.{base_note}\n\n"
            "This overwrites the remote document. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return

        self._busy = True
        self.btn_push.setEnabled(False)
        threading.Thread(
            target=self._run, args=(text, target, save), daemon=True
        ).start()

    def _run(self, text: str, target: str, save: bool) -> None:
        async def go() -> None:
            from src.mcp_client import HorizonMCPClient
            from src.controller import RemoteController
            cfg = self._config
            ctl = cfg.get("control", {})
            async with HorizonMCPClient(
                cfg["mcp"]["server_path"], cfg["mcp"]["command"]
            ) as client:
                c = RemoteController(
                    client,
                    focus_target=ctl.get("focus_target", "PVDI"),
                    launch_wait=ctl.get("launch_wait_seconds", 1.5),
                    clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
                    copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
                    screen=ctl.get("screen", 0),
                )
                if target:
                    self._sig.status.emit(f"Opening {target} in the remote…")
                    await c.open_file(target)
                self._sig.status.emit("Pasting the document into the remote…")
                await c.paste_to_remote(text, replace_all=True, save=save)

        try:
            import asyncio
            asyncio.run(go())
            self._sig.done.emit(
                f"Pushed {len(text)} chars{' and saved' if save else ''}."
            )
        except Exception as exc:  # noqa: BLE001
            self._sig.error.emit(str(exc))

    def _on_done(self, msg: str) -> None:
        self._busy = False
        self.btn_push.setEnabled(True)
        self._set_status(msg)

    def _on_error(self, msg: str) -> None:
        self._busy = False
        self.btn_push.setEnabled(True)
        self._set_status(f"Push failed: {msg}")


# ---------------------------------------------------------------- Settings tab

def _toml_scalar(value) -> str:
    """Format a Python value as a TOML literal (scalars + string lists)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        inner = ", ".join(_toml_scalar(str(v)) for v in value)
        return f"[{inner}]"
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _update_toml(path: str, updates: dict[tuple[str, str], object]) -> None:
    """Surgically set `key = value` lines in-place, preserving comments/layout.

    `updates` maps (section, key) -> Python value. Only the matching line's value is
    rewritten (its trailing inline comment is kept). Missing keys are appended under
    their section header (added at end if the section itself is absent).
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    remaining = dict(updates)

    section = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        for (sec, key) in list(remaining):
            if sec == section and re.match(rf"^\s*{re.escape(key)}\s*=", line):
                hashidx = line.find("#")
                comment = "  " + line[hashidx:].rstrip("\n") if hashidx != -1 else ""
                indent = line[: len(line) - len(line.lstrip())]
                val = _toml_scalar(remaining.pop((sec, key)))
                lines[i] = f"{indent}{key} = {val}{comment}\n"

    # Append anything not found, grouped by section.
    if remaining:
        existing = {l.strip()[1:-1] for l in lines
                    if l.strip().startswith("[") and l.strip().endswith("]")}
        by_section: dict[str, list[str]] = {}
        for (sec, key), value in remaining.items():
            by_section.setdefault(sec, []).append(f"{key} = {_toml_scalar(value)}\n")
        tail = []
        for sec, kvs in by_section.items():
            if sec and sec not in existing:
                tail.append(f"\n[{sec}]\n")
            tail.extend(kvs)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(tail)

    p.write_text("".join(lines), encoding="utf-8")


def _update_env(path: str, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a flat .env, updating existing keys or appending new ones."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True) if p.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}\n"
            seen.add(m.group(1))
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}\n")
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    p.write_text("".join(lines), encoding="utf-8")


# .env keys surfaced in Settings: (env var, label).
_ENV_KEYS = [
    ("ANTHROPIC_API_KEY", "Anthropic API key"),
    ("VOYAGE_API_KEY", "Voyage API key (optional)"),
    ("HORIZON_PASSWORD", "Horizon unlock password"),
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token"),
    ("TELEGRAM_CHAT_ID", "Telegram chat id"),
]


class SettingsPage(QWidget):
    """Edit config.toml settings and .env secrets in-app.

    config.toml edits are surgical (comments preserved). Secrets are write-only — the
    current value is never shown; a blank field keeps it, typing a value replaces it.
    Most changes take effect on the next restart (the config is read at startup).
    """

    def __init__(self, config: dict) -> None:
        super().__init__()
        self._config = config
        # config.toml / .env live beside the .exe when frozen, else the repo root —
        # the same location main.py reads them from.
        import sys
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
        self.CONFIG_PATH = str(base / "config.toml")
        self.ENV_PATH = str(base / ".env")

        page, body = _page_scaffold(
            "Settings",
            "Tune the monitor and manage credentials. Most changes apply after a restart.",
        )

        poll = config.get("polling", {})
        win = config.get("windows", {})
        ret = config.get("retention", {})
        user = config.get("user", {})
        notif = config.get("notifications", {})
        control = config.get("control", {})

        # Monitoring
        mon_card, ml = _card("Monitoring")
        self.interval = self._spin(ml, "Poll interval", poll.get("interval_seconds", 3),
                                   1, 120, " s")
        self.threshold = self._spin(ml, "Change threshold (0–64)",
                                    poll.get("change_threshold", 10), 0, 64, "")
        self.retain = self._spin(ml, "Keep data for (0 = forever)",
                                 ret.get("retain_days", 0), 0, 3650, " days")
        titles_row = QHBoxLayout()
        titles_row.addWidget(QLabel("Window titles:"))
        self.titles = QLineEdit(", ".join(win.get("monitor_titles", [])))
        self.titles.setPlaceholderText("PVDI, horizon-client")
        titles_row.addWidget(self.titles, 1)
        ml.addLayout(titles_row)
        body.addWidget(mon_card)

        # Behaviour
        beh_card, bl = _card("Behaviour")
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Your display name:"))
        self.display_name = QLineEdit(user.get("display_name", ""))
        name_row.addWidget(self.display_name, 1)
        bl.addLayout(name_row)
        self.cooldown = self._spin(bl, "Re-alert cooldown",
                                   notif.get("cooldown_minutes", 5), 0, 240, " min")
        self.control_enabled = QCheckBox("Enable remote control (unlock, keep-awake, push)")
        self.control_enabled.setChecked(bool(control.get("enabled", False)))
        bl.addWidget(self.control_enabled)
        body.addWidget(beh_card)

        # Secrets
        sec_card, scl = _card(
            "Credentials (.env)",
            "Stored locally in .env, never displayed. Leave a field blank to keep the "
            "current value; type to replace it.",
        )
        self._env_edits: dict[str, QLineEdit] = {}
        for var, label in _ENV_KEYS:
            row = QHBoxLayout()
            tag = "set" if os.environ.get(var) else "not set"
            lbl = QLabel(f"{label}:")
            lbl.setMinimumWidth(200)
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(f"({tag} — blank to keep)")
            self._env_edits[var] = edit
            row.addWidget(lbl)
            row.addWidget(edit, 1)
            scl.addLayout(row)
        body.addWidget(sec_card)

        # Save
        self.btn_save = QPushButton("Save settings")
        self.btn_save.setObjectName("Primary")
        self.btn_save.clicked.connect(self._save)
        body.addWidget(self.btn_save, 0, Qt.AlignLeft)
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        body.addWidget(self.status)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

    def _spin(self, layout, label, value, lo, hi, suffix) -> QSpinBox:
        row = QHBoxLayout()
        row.addWidget(QLabel(label + ":"))
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(int(value))
        if suffix:
            sp.setSuffix(suffix)
        row.addWidget(sp)
        row.addStretch(1)
        layout.addLayout(row)
        return sp

    def _save(self) -> None:
        titles = [t.strip() for t in self.titles.text().split(",") if t.strip()]
        toml_updates = {
            ("polling", "interval_seconds"): self.interval.value(),
            ("polling", "change_threshold"): self.threshold.value(),
            ("retention", "retain_days"): self.retain.value(),
            ("windows", "monitor_titles"): titles,
            ("user", "display_name"): self.display_name.text().strip(),
            ("notifications", "cooldown_minutes"): self.cooldown.value(),
            ("control", "enabled"): self.control_enabled.isChecked(),
        }
        env_updates = {
            var: edit.text() for var, edit in self._env_edits.items() if edit.text()
        }
        try:
            _update_toml(self.CONFIG_PATH, toml_updates)
            if env_updates:
                _update_env(self.ENV_PATH, env_updates)
                for var, val in env_updates.items():
                    os.environ[var] = val   # apply secrets live; config needs a restart
                    self._env_edits[var].clear()
                    self._env_edits[var].setPlaceholderText("(set — blank to keep)")
            saved = "config.toml" + (" + .env" if env_updates else "")
            self.status.setText(f"Saved {saved}. Restart to apply config changes.")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Save failed: {exc}")


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

        # Direct (non-scrolling) layout so the input row stays pinned and visible — the
        # transcript scrolls internally instead of being pushed off-screen by a stretchy
        # transcript inside an outer scroll area.
        body = QVBoxLayout(self)
        body.setContentsMargins(24, 20, 24, 20)
        body.setSpacing(14)
        _title = QLabel("Assist")
        _title.setObjectName("PageTitle")
        _subtitle = QLabel(
            "Describe a task on the remote desktop in plain language. The agent looks at "
            "the screen and helps — read-only advice, or hands-on with your confirmation."
        )
        _subtitle.setObjectName("PageSubtitle")
        _subtitle.setWordWrap(True)
        body.addWidget(_title)
        body.addWidget(_subtitle)

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
