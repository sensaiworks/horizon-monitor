"""
Windows system tray icon for horizon-monitor.

Right-click menu:
  ● status / last event / event count
  Recent messages ▶  (last 5)
  Ask...             (query dialog, also triggered by double-click)
  ─────
  Pause / Resume / Stop / Start
  Unlock Remote Desktop   (when remote is locked)
  ─────
  Quit

Lock screen: extractor sets the "locked" flag → red icon → Unlock item appears.
Unlock sequence: focus Horizon → Ctrl+Alt+Insert → wait → type HORIZON_PASSWORD → Enter.
"""

from __future__ import annotations

import asyncio
import collections
import os
import queue
import threading

from PIL import Image, ImageDraw
import pystray

from .agent import QueryAgent
from .extractor import Extractor
from .mcp_client import HorizonMCPClient
from .models import MessageEvent
from .controller import RemoteController
from .notifier import Notifier
from .poller import Poller
from .rag import RAGPipeline
from .store import EventStore

_STATUS_COLORS = {
    "monitoring": (34, 197, 94),
    "paused":     (251, 146, 60),
    "stopped":    (107, 114, 128),
    "locked":     (220, 38,  38),
}


def _make_icon(status: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _STATUS_COLORS.get(status, _STATUS_COLORS["stopped"])
    d.ellipse([2, 2, size - 2, size - 2], fill=(20, 20, 30, 255))
    d.ellipse([8, 8, size - 8, size - 8], fill=(*color, 255))
    d.rectangle([18, 20, 24, 44], fill=(255, 255, 255, 220))
    d.rectangle([40, 20, 46, 44], fill=(255, 255, 255, 220))
    d.rectangle([18, 30, 46, 36], fill=(255, 255, 255, 220))
    return img


class TrayApp:
    def __init__(self, config: dict, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._status = "stopped"
        self._remote_locked = False
        self._last_event: MessageEvent | None = None
        self._recent: collections.deque[MessageEvent] = collections.deque(maxlen=5)
        self._event_queue: queue.Queue[MessageEvent] = queue.Queue()
        self._db_count: int = 0

        self._monitor_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_ev: asyncio.Event | None = None
        self._pause_ev: asyncio.Event | None = None
        self._icon: pystray.Icon | None = None

    # ------------------------------------------------------------------ menu

    def _menu_items(self):
        status_label = {
            "monitoring": "● Monitoring",
            "paused":     "⏸ Paused",
            "stopped":    "○ Stopped",
            "locked":     "🔒 Remote Locked",
        }.get(self._status, self._status)

        if self._last_event:
            snippet = self._last_event.message[:42].replace("\n", " ")
            last_label = f"{self._last_event.speaker}: {snippet}"
        else:
            last_label = "No events yet"

        db_label = f"Database: {self._db_count} event{'s' if self._db_count != 1 else ''}"

        # Recent messages submenu
        if self._recent:
            recent_items = []
            for ev in reversed(self._recent):
                ts = ev.chat_time or ev.timestamp.strftime("%H:%M")
                line = f"{ts}  {ev.speaker}: {ev.message[:35].replace(chr(10), ' ')}"
                recent_items.append(pystray.MenuItem(line, None, enabled=False))
            recent_menu = pystray.MenuItem(
                "Recent messages",
                pystray.Menu(*recent_items),
            )
        else:
            recent_menu = pystray.MenuItem("Recent messages", None, enabled=False)

        items = [
            pystray.MenuItem(status_label, None, enabled=False),
            pystray.MenuItem(last_label, None, enabled=False),
            pystray.MenuItem(db_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            recent_menu,
            pystray.MenuItem("Ask...", self._on_ask, default=True),
            pystray.Menu.SEPARATOR,
        ]

        if self._status == "monitoring":
            items += [
                pystray.MenuItem("Pause", self._on_pause),
                pystray.MenuItem("Stop", self._on_stop),
            ]
        elif self._status == "paused":
            items += [
                pystray.MenuItem("Resume", self._on_resume),
                pystray.MenuItem("Stop", self._on_stop),
            ]
        elif self._status == "locked":
            items.append(pystray.MenuItem("Resume after unlock", self._on_start))
        else:
            items.append(pystray.MenuItem("Start", self._on_start))

        if self._remote_locked and self._control_enabled:
            items.append(pystray.MenuItem("Unlock Remote Desktop", self._on_unlock))

        # Remote-control actions — only when explicitly enabled in config.
        if self._control_enabled:
            items += [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Launch / activate app on remote…", self._on_launch_app),
                pystray.MenuItem("Bring Horizon to front", self._on_bring_to_front),
            ]

        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        ]
        return tuple(items)

    def _refresh(self) -> None:
        if not self._icon:
            return
        icon_status = "locked" if self._remote_locked else self._status
        self._icon.icon = _make_icon(icon_status)
        self._icon.title = f"horizon-monitor — {self._status}"
        self._icon.menu = pystray.Menu(self._menu_items)

    # -------------------------------------------------------- tray callbacks

    def _on_start(self, icon, item) -> None:
        self._remote_locked = False
        self._start_monitor()

    def _on_stop(self, icon, item) -> None:
        self._stop_monitor()

    def _on_pause(self, icon, item) -> None:
        if self._loop and self._pause_ev and self._status == "monitoring":
            self._loop.call_soon_threadsafe(self._pause_ev.set)
            self._status = "paused"
            self._refresh()

    def _on_resume(self, icon, item) -> None:
        if self._loop and self._pause_ev and self._status == "paused":
            self._loop.call_soon_threadsafe(self._pause_ev.clear)
            self._status = "monitoring"
            self._refresh()

    def _on_quit(self, icon, item) -> None:
        self._stop_monitor()
        icon.stop()

    def _on_ask(self, icon=None, item=None) -> None:
        threading.Thread(target=self._query_dialog_thread, daemon=True).start()

    def _on_unlock(self, icon, item) -> None:
        threading.Thread(target=self._unlock_worker, daemon=True).start()

    def _on_bring_to_front(self, icon, item) -> None:
        self._run_control("bring Horizon to front", lambda c: c.bring_to_front())

    def _on_launch_app(self, icon=None, item=None) -> None:
        # Ask for the app name on a Tk thread, then run the action.
        def worker() -> None:
            app = self._prompt_text("Launch / activate app on remote",
                                     "App name (as in the remote Start menu):")
            if app:
                self._run_control(f"launch/activate '{app}'",
                                  lambda c: c.launch_or_activate(app))
        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------------------- monitor lifecycle

    def _start_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._status = "monitoring"
        self._refresh()
        self._monitor_thread = threading.Thread(target=self._monitor_worker, daemon=True)
        self._monitor_thread.start()
        threading.Thread(target=self._drain_events, daemon=True).start()

    def _stop_monitor(self) -> None:
        if self._loop and self._stop_ev:
            self._loop.call_soon_threadsafe(self._stop_ev.set)
        self._status = "stopped"
        self._refresh()

    def _monitor_worker(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_ev = asyncio.Event()
        self._pause_ev = asyncio.Event()
        try:
            self._loop.run_until_complete(self._monitor_async())
        finally:
            self._loop.close()
            self._loop = None
            self._stop_ev = None
            self._pause_ev = None

    async def _monitor_async(self) -> None:
        cfg = self._config
        extractor = Extractor(
            api_key=self._api_key,
            model=cfg["claude"]["vision_model"],
            user_display_name=cfg["user"]["display_name"],
        )
        notifier = Notifier(
            enabled=cfg["notifications"]["enabled"],
            cooldown_minutes=cfg["notifications"]["cooldown_minutes"],
        )
        rag_cfg = cfg["rag"]
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
        self._db_count = store.count()

        poll = cfg["polling"]
        win = cfg["windows"]

        async with HorizonMCPClient(cfg["mcp"]["server_path"], cfg["mcp"]["command"]) as client:
            poller = Poller(
                client=client,
                interval=poll["interval_seconds"],
                change_threshold=poll["change_threshold"],
                screen_index=poll["screen_index"],
                monitor_titles=win["monitor_titles"],
                focus_before_shot=poll["focus_before_shot"],
            )

            async def on_change(state, png: bytes) -> None:
                events, is_locked = await extractor.extract(png, window_title=state.window_title)

                if is_locked != self._remote_locked:
                    self._remote_locked = is_locked
                    if is_locked:
                        print("Remote desktop is locked", flush=True)
                    self._refresh()

                if events:
                    new = store.ingest(events)   # dedup gate
                    if new:
                        rag.ingest(new)           # embed only the new ones
                        self._db_count = store.count()
                        print(f"stored +{len(new)} ({self._db_count} total)", flush=True)
                        # Notify and surface only genuinely new messages — not the
                        # same screen re-read every poll cycle.
                        for ev in new:
                            self._event_queue.put(ev)
                            notifier.notify_if_needed(ev)

            await poller.run(on_change=on_change, stop=self._stop_ev, pause=self._pause_ev)

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self._event_queue.get(timeout=1.0)
                self._last_event = ev
                self._recent.append(ev)
                self._refresh()
            except queue.Empty:
                if self._status == "stopped":
                    break

    # --------------------------------------------------------- Ask dialog

    def _make_rag(self) -> RAGPipeline:
        cfg = self._config["rag"]
        rag = RAGPipeline(
            db_path=cfg["db_path"],
            collection_name=cfg["collection_name"],
            embedding_provider=cfg["embedding_provider"],
            voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
            top_k=cfg["top_k"],
        )
        rag.connect()
        return rag

    def _query_dialog_thread(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        root = tk.Tk()
        root.title("Ask horizon-monitor")
        root.geometry("540x400")
        root.resizable(True, True)

        frame = tk.Frame(root)
        frame.pack(fill=tk.X, padx=10, pady=(10, 4))

        entry = tk.Entry(frame, font=("Segoe UI", 11))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)

        btn = tk.Button(frame, text="Ask", width=6)
        btn.pack(side=tk.LEFT, padx=(6, 0))

        entry.focus()

        txt = scrolledtext.ScrolledText(
            root, font=("Segoe UI", 10), wrap=tk.WORD, state=tk.DISABLED
        )
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        def _append(text: str) -> None:
            txt.config(state=tk.NORMAL)
            txt.insert(tk.END, text)
            txt.see(tk.END)
            txt.config(state=tk.DISABLED)

        def ask() -> None:
            q = entry.get().strip()
            if not q:
                return
            entry.delete(0, tk.END)
            txt.config(state=tk.NORMAL)
            txt.delete(1.0, tk.END)
            txt.config(state=tk.DISABLED)
            _append(f"Q: {q}\n\nA: ")
            btn.config(state=tk.DISABLED)

            def run() -> None:
                try:
                    rag = self._make_rag()
                    agent = QueryAgent(
                        api_key=self._api_key,
                        model=self._config["claude"]["query_model"],
                        rag=rag,
                        max_tokens=self._config["claude"]["max_tokens"],
                    )
                    agent.query(q, on_chunk=lambda t: root.after(0, lambda t=t: _append(t)))
                    root.after(0, lambda: _append("\n"))
                except Exception as exc:
                    root.after(0, lambda: _append(f"\n[Error: {exc}]"))
                finally:
                    root.after(0, lambda: btn.config(state=tk.NORMAL))

            threading.Thread(target=run, daemon=True).start()

        btn.config(command=ask)
        entry.bind("<Return>", lambda e: ask())
        root.mainloop()

    # --------------------------------------------------- remote control

    def _prompt_text(self, title: str, label: str) -> str:
        """Modal single-line input dialog. Returns "" if cancelled."""
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        value = simpledialog.askstring(title, label, parent=root)
        root.destroy()
        return (value or "").strip()

    def _controller(self, client: HorizonMCPClient) -> RemoteController:
        ctl = self._config.get("control", {})
        return RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
        )

    def _run_control(self, label, action) -> None:
        """Run one controller coroutine on its own loop + MCP client, off the UI thread.

        `action` is a callable taking a RemoteController and returning a coroutine.
        """
        if not self._control_enabled:
            print("Remote control disabled — set [control].enabled=true in config.toml", flush=True)
            return

        def worker() -> None:
            async def go() -> None:
                cfg = self._config
                async with HorizonMCPClient(cfg["mcp"]["server_path"], cfg["mcp"]["command"]) as client:
                    await action(self._controller(client))
            print(f"Remote: {label}…", flush=True)
            try:
                asyncio.run(go())
                print(f"Remote: {label} — done", flush=True)
            except Exception as exc:
                print(f"Remote: {label} failed: {exc}", flush=True)

        threading.Thread(target=worker, daemon=True).start()

    def _unlock_worker(self) -> None:
        password = os.environ.get("HORIZON_PASSWORD", "")
        if not password:
            print("HORIZON_PASSWORD not set in .env — cannot unlock", flush=True)
            return
        self._run_control("unlock remote desktop", lambda c: c.unlock(password))

    # -------------------------------------------------------------- entry

    def run(self) -> None:
        """Show tray icon in stopped state. User must click Start to begin monitoring."""
        self._icon = pystray.Icon(
            name="horizon-monitor",
            icon=_make_icon("stopped"),
            title="horizon-monitor — stopped",
            menu=pystray.Menu(self._menu_items),
        )
        self._icon.run()
