"""CaptureEngine — the single background pipeline behind the desktop app.

One asyncio loop on a worker thread does poll → screenshot → change-detect → extract
ONCE per change, then fans each extracted MessageEvent out to toggleable *sinks*:

  - alert  : presence-only — if a message is directed at the user (or matches a watch
             term), fire on_alert(event). NO message content leaves the machine.
  - collect: dedup-gate into SQLite + embed new ones into ChromaDB (the RAG store).

Both sinks are independently switchable at runtime (`alert_enabled`/`collect_enabled`),
so the Monitor and Collect tabs just flip flags on the same engine rather than each
running their own capture loop.

The engine is UI-agnostic: it exposes plain callback hooks (on_status/on_event/on_alert/
on_log/on_error). The UI sets them to Qt-signal emitters so updates land on the UI thread.
Mirrors the headless `python main.py monitor` orchestration, refactored into a class.
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta, timezone

from ..models import MessageEvent


class CaptureEngine:
    def __init__(self, config: dict, api_key: str) -> None:
        self._config = config
        self._api_key = api_key

        # Hooks — replaced by the UI with Qt-signal emitters. Default no-ops so the
        # engine is usable (and testable) without a UI attached.
        self.on_status = lambda state: None      # "running" | "paused" | "stopped" | "error"
        self.on_event = lambda ev: None          # a newly-stored MessageEvent
        self.on_alert = lambda ev: None          # presence alert (directed/keyword match)
        self.on_log = lambda msg: None           # human-readable progress line
        self.on_error = lambda msg: None         # fatal error string

        # Sink toggles + alert config (the UI flips these live).
        self.alert_enabled = True
        self.collect_enabled = True
        self.telegram_enabled = False
        self.watch_terms: list[str] = []

        # Presence-only Telegram pings (credentials from .env). Built up-front so the
        # Settings "Send test" button shares the same instance; never sends message bodies.
        from ..telegram import TelegramNotifier
        self.telegram = TelegramNotifier(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
        )
        self.telegram.on_error = lambda msg: self.on_log(f"Telegram failed: {msg}")

        self._state = "stopped"
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_ev: asyncio.Event | None = None   # bound to the worker loop
        self._pause_ev: asyncio.Event | None = None

    # ------------------------------------------------------------- state

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str) -> None:
        self._state = state
        self.on_status(state)

    # ------------------------------------------------------- thread control

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self._loop and self._pause_ev:
            self._loop.call_soon_threadsafe(self._pause_ev.set)
            self._set_state("paused")

    def resume(self) -> None:
        if self._loop and self._pause_ev:
            self._loop.call_soon_threadsafe(self._pause_ev.clear)
            self._set_state("running")

    def stop(self) -> None:
        if self._loop and self._stop_ev:
            self._loop.call_soon_threadsafe(self._stop_ev.set)
        # don't join from the UI thread — the worker flips state to "stopped" on exit

    # ------------------------------------------------------------ worker

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001 — surface any startup/loop failure
            self._set_state("error")
            self.on_error(str(exc))

    async def _run(self) -> None:
        from ..mcp_client import HorizonMCPClient
        from ..poller import Poller
        from ..extractor import Extractor
        from ..rag import RAGPipeline
        from ..store import EventStore

        cfg = self._config
        poll_cfg = cfg["polling"]
        rag_cfg = cfg["rag"]

        self._loop = asyncio.get_running_loop()
        self._stop_ev = asyncio.Event()
        self._pause_ev = asyncio.Event()

        extractor = Extractor(
            api_key=self._api_key,
            model=cfg["claude"]["vision_model"],
            user_display_name=cfg["user"]["display_name"],
        )
        rag = RAGPipeline(
            db_path=rag_cfg["db_path"],
            collection_name=rag_cfg["collection_name"],
            embedding_provider=rag_cfg["embedding_provider"],
            voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
            top_k=rag_cfg["top_k"],
        )
        rag.connect()
        store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
        store.connect()

        retain_days = int(cfg.get("retention", {}).get("retain_days", 0) or 0)
        if retain_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
            removed = store.expire_older_than(cutoff)
            rag.delete_older_than(cutoff)
            if removed:
                self.on_log(f"Retention: dropped {removed} event(s) older than {retain_days}d.")

        async with HorizonMCPClient(
            server_path=cfg["mcp"]["server_path"], command=cfg["mcp"]["command"]
        ) as client:
            poller = Poller(
                client=client,
                interval=poll_cfg["interval_seconds"],
                change_threshold=poll_cfg["change_threshold"],
                screen_index=poll_cfg["screen_index"],
                monitor_titles=cfg["windows"]["monitor_titles"],
                focus_before_shot=poll_cfg["focus_before_shot"],
            )

            async def on_change(state, png: bytes) -> None:
                events, is_locked = await extractor.extract(
                    png, window_title=state.window_title
                )
                if is_locked:
                    self.on_log("Remote desktop is locked.")
                    return
                if not events:
                    return

                if self.collect_enabled:
                    new = store.ingest(events)          # dedup gate
                    if new:
                        rag.ingest(new)                 # embed only the new ones
                        self.on_log(f"Collected +{len(new)} ({store.count()} total).")
                        for ev in new:
                            self.on_event(ev)
                        self._fire_alerts(new)
                else:
                    # Not collecting, but still alert on directed/keyword messages.
                    self._fire_alerts(events)

            self.on_log(
                f"Monitoring every {poll_cfg['interval_seconds']}s "
                f"(alerts {'on' if self.alert_enabled else 'off'}, "
                f"collect {'on' if self.collect_enabled else 'off'})."
            )
            self._set_state("running")
            await poller.run(
                on_change=on_change, stop=self._stop_ev, pause=self._pause_ev
            )

        self._set_state("stopped")
        self.on_log("Monitoring stopped.")

    # ------------------------------------------------------------- alerts

    def _fire_alerts(self, events: list[MessageEvent]) -> None:
        """Presence-only: notify for messages directed at the user or matching a term."""
        if not self.alert_enabled:
            return
        terms = [t.lower() for t in self.watch_terms if t.strip()]
        for ev in events:
            hit = ev.directed_at_user or any(
                t in ev.message.lower() or t in ev.speaker.lower() for t in terms
            )
            if hit:
                self.on_alert(ev)
                if self.telegram_enabled:
                    self.telegram.notify_presence(ev.speaker, ev.channel)
