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
        # Channels to record. Empty = collect everything (allow-all); otherwise only
        # events whose channel is in this list are stored. Alerts ignore this filter.
        self.collect_channels: list[str] = []

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
        if state == self._state:
            return
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

        cap = cfg.get("capture", {})
        targets = self._targets(cap)
        control_on = bool(cfg.get("control", {}).get("enabled", False))
        # Rotation DRIVES the remote (focus-steal + taskbar clicks to switch apps). It is
        # gated behind an explicit [capture].rotate flag (default off): until calibrated it
        # is off, and auto-start runs the read-only passive monitor, which never steals
        # focus or types anything.
        rotate = bool(cap.get("rotate", False))
        stitch = bool(targets) and control_on and rotate

        async with HorizonMCPClient(
            server_path=cfg["mcp"]["server_path"], command=cfg["mcp"]["command"]
        ) as client:
            if stitch:
                await self._loop_stitch(client, extractor, rag, store, cap)
            else:
                if targets and rotate and not control_on:
                    self.on_log(
                        "App rotation needs [control].enabled = true — passively "
                        "monitoring the current screen instead."
                    )
                await self._loop_passive(client, extractor, rag, store, poll_cfg)

        self._set_state("stopped")
        self.on_log("Monitoring stopped.")

    # ------------------------------------------------------------- sinks

    async def _sink(self, events: list[MessageEvent], store, rag) -> None:
        """Fan extracted events to the collect (SQLite + RAG) and alert sinks.

        Alerts always fire (presence-only) even when collecting is off, and ignore the
        collect-channel filter — a mention should ping you on any channel.
        """
        if self.collect_enabled:
            wanted = events
            if self.collect_channels:
                allow = set(self.collect_channels)
                wanted = [e for e in events if (e.channel or "") in allow]
            new = store.ingest(wanted) if wanted else []
            if new:
                rag.ingest(new)                 # embed only the new ones
                self.on_log(f"Collected +{len(new)} ({store.count()} total).")
                for ev in new:
                    self.on_event(ev)
        self._fire_alerts(events)

    async def _loop_passive(self, client, extractor, rag, store, poll_cfg) -> None:
        """Read-only single-screen monitoring (the original behavior)."""
        from ..poller import Poller

        poller = Poller(
            client=client,
            interval=poll_cfg["interval_seconds"],
            change_threshold=poll_cfg["change_threshold"],
            screen_index=poll_cfg["screen_index"],
            monitor_titles=self._config["windows"]["monitor_titles"],
            focus_before_shot=poll_cfg["focus_before_shot"],
        )

        async def on_change(state, png: bytes) -> None:
            events, is_locked = await extractor.extract(png, window_title=state.window_title)
            if is_locked:
                self.on_log("Remote desktop is locked.")
                return
            if events:
                await self._sink(events, store, rag)

        self.on_log(
            f"Monitoring every {poll_cfg['interval_seconds']}s "
            f"(alerts {'on' if self.alert_enabled else 'off'}, "
            f"collect {'on' if self.collect_enabled else 'off'})."
        )
        self._set_state("running")
        await poller.run(on_change=on_change, stop=self._stop_ev, pause=self._pause_ev)

    @staticmethod
    def _targets(cap: dict) -> list[dict]:
        """Normalize [capture] into a list of {name, match:[...]} rotation targets.

        Prefers the `[[capture.targets]]` array-of-tables (name + taskbar-label match
        terms); falls back to a bare `apps` list (each name matched against itself).
        """
        raw = cap.get("targets")
        if raw:
            out = []
            for t in raw:
                name = str(t.get("name", "")).strip()
                if not name:
                    continue
                match = [str(m).strip() for m in (t.get("match") or []) if str(m).strip()]
                out.append({"name": name, "match": match or [name]})
            return out
        return [
            {"name": a, "match": [a]}
            for a in (str(x).strip() for x in cap.get("apps", []))
            if a
        ]

    async def _loop_stitch(self, client, extractor, rag, store, cap) -> None:
        """Rotate through remote apps, capturing each; yield to the user; auto-unlock.

        Brings each app to the front by clicking its TASKBAR button (RemoteController.
        activate_taskbar, OCR-matched so it tolerates the buttons shifting), then runs the
        same extract→collect/alert pipeline with a PER-APP change detector (so switching
        apps is not mistaken for new content). Because switching steals local focus, an
        ActivityMonitor pauses the loop the instant the user touches this PC and resumes
        only after they have been idle for idle_resume_seconds. If the session is locked it
        unlocks it with HORIZON_PASSWORD (rate-limited so it never re-submits in a loop).
        Manual Pause/Stop still take precedence over the activity gate.
        """
        import io

        import imagehash
        from PIL import Image

        from ..controller import RemoteController
        from ..idle import ActivityMonitor

        ctl_cfg = self._config.get("control", {})
        controller = RemoteController(
            client,
            focus_target=ctl_cfg.get("focus_target", "PVDI"),
            launch_wait=ctl_cfg.get("launch_wait_seconds", 1.5),
            screen=ctl_cfg.get("screen", 0),
        )
        targets = self._targets(cap)
        settle = float(cap.get("settle_seconds", 1.2))
        cycle = float(cap.get("cycle_seconds", 8))
        resume_ms = int(float(cap.get("idle_resume_seconds", 90)) * 1000)
        threshold = int(self._config["polling"]["change_threshold"])
        auto_unlock = bool(cap.get("auto_unlock", True))
        unlock_cooldown = float(cap.get("unlock_cooldown_seconds", 120))
        password = os.environ.get("HORIZON_PASSWORD", "")

        rect = await controller.resolve_rect()   # absolute VDI (Left, Top, W, H)
        activity = ActivityMonitor()
        prev_hash: dict = {}
        running = True            # cold start: begin monitoring immediately
        last_unlock: float | None = None
        loop = asyncio.get_running_loop()
        activity.mark_self_input()

        self.on_log(
            f"Auto-monitor: rotating {', '.join(t['name'] for t in targets)} "
            f"(~{cycle:.0f}s between cycles); yields to you on activity, resumes after "
            f"{resume_ms // 1000}s idle. Collect {'on' if self.collect_enabled else 'off'}."
        )
        self._set_state("running")

        while not self._stop_ev.is_set():
            # Manual Pause (button) fully idles the loop, regardless of activity.
            if self._pause_ev.is_set():
                self._set_state("paused")
                await asyncio.sleep(0.5)
                continue

            # Yield-to-user gate.
            if not running:
                if activity.idle_ms() >= resume_ms:
                    running = True
                    activity.mark_self_input()      # reset baseline before we act again
                    self._set_state("running")
                    self.on_log("Resumed — you've been idle.")
                else:
                    self._set_state("paused")
                    await asyncio.sleep(1.0)
                    continue
            elif activity.user_active():
                running = False
                self._set_state("paused")
                self.on_log(
                    f"Paused — you're using the PC. Resuming after "
                    f"{resume_ms // 1000}s of no input."
                )
                await asyncio.sleep(1.0)
                continue

            # One rotation through the apps.
            for tgt in targets:
                name, needles = tgt["name"], tgt["match"]
                if self._stop_ev.is_set() or self._pause_ev.is_set():
                    break
                if activity.user_active():          # user grabbed control mid-cycle
                    break
                try:
                    found = await controller.activate_taskbar(needles)
                    activity.mark_self_input()       # the switch was ours, not the user
                    if not found:
                        self.on_log(f"{name}: no taskbar button matched — skipped.")
                        continue
                    await asyncio.sleep(settle)
                    png = await client.screenshot_region(*rect)   # whole VDI, both halves
                    if not png:
                        continue

                    cur = imagehash.average_hash(Image.open(io.BytesIO(png)))
                    prev = prev_hash.get(name)
                    changed = prev is None or (prev - cur) > threshold
                    if not changed:
                        continue
                    prev_hash[name] = cur

                    events, is_locked = await extractor.extract(png, window_title=name)
                    if is_locked:
                        now = loop.time()
                        if not auto_unlock:
                            self.on_log("Locked — auto-unlock is off.")
                        elif not password:
                            self.on_log("Locked — set HORIZON_PASSWORD to auto-unlock.")
                        elif last_unlock is None or (now - last_unlock) > unlock_cooldown:
                            last_unlock = now
                            self.on_log("Locked — unlocking the session…")
                            await controller.unlock(password)
                            await asyncio.sleep(settle)
                            activity.mark_self_input()
                        prev_hash.pop(name, None)    # re-extract once unlocked
                        continue

                    if events:
                        await self._sink(events, store, rag)
                except Exception as exc:  # noqa: BLE001 — one app must not kill the loop
                    self.on_log(f"{name}: capture failed — {exc}")

            # Inter-cycle wait — break out early on stop / manual pause / user activity.
            # No re-mark here: the last switch is our most recent input, so any newer
            # tick is the user's and must trip user_active().
            waited = 0.0
            while (
                waited < cycle
                and not self._stop_ev.is_set()
                and not self._pause_ev.is_set()
                and not activity.user_active()
            ):
                await asyncio.sleep(0.5)
                waited += 0.5

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
