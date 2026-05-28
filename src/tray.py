"""
Windows system tray icon for horizon-monitor.

Auto-starts monitoring on launch. Right-click menu provides:
  Pause / Resume / Stop / Start   — monitor lifecycle
  Last event line                 — most recent extracted message
  Quit                            — stop monitor and exit

The async monitor loop runs in a background thread; the tray runs in
the main thread (required by pystray on Windows).
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw
import pystray

from .extractor import Extractor
from .mcp_client import HorizonMCPClient
from .models import MessageEvent
from .notifier import Notifier
from .poller import Poller

_STATUS_COLORS = {
    "monitoring": (34, 197, 94),
    "paused":     (251, 146, 60),
    "stopped":    (107, 114, 128),
}


def _make_icon(status: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _STATUS_COLORS.get(status, _STATUS_COLORS["stopped"])
    d.ellipse([2, 2, size - 2, size - 2], fill=(20, 20, 30, 255))
    d.ellipse([8, 8, size - 8, size - 8], fill=(*color, 255))
    # "H" letterform
    d.rectangle([18, 20, 24, 44], fill=(255, 255, 255, 220))
    d.rectangle([40, 20, 46, 44], fill=(255, 255, 255, 220))
    d.rectangle([18, 30, 46, 36], fill=(255, 255, 255, 220))
    return img


class TrayApp:
    def __init__(self, config: dict, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._status = "stopped"
        self._last_event: MessageEvent | None = None
        self._event_queue: queue.Queue[MessageEvent] = queue.Queue()

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
        }[self._status]

        if self._last_event:
            snippet = self._last_event.message[:45].replace("\n", " ")
            last_label = f"{self._last_event.speaker}: {snippet}"
        else:
            last_label = "No events yet"

        items = [
            pystray.MenuItem(status_label, None, enabled=False),
            pystray.MenuItem(last_label, None, enabled=False),
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
        else:
            items.append(pystray.MenuItem("Start", self._on_start))

        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        ]
        return tuple(items)

    def _refresh(self) -> None:
        if not self._icon:
            return
        self._icon.icon = _make_icon(self._status)
        self._icon.title = f"horizon-monitor — {self._status}"
        self._icon.menu = pystray.Menu(self._menu_items)

    # -------------------------------------------------------- tray callbacks

    def _on_start(self, icon, item) -> None:
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
                events = await extractor.extract(png, window_title=state.window_title)
                for ev in events:
                    self._event_queue.put(ev)
                    notifier.notify_if_needed(ev)

            await poller.run(on_change=on_change, stop=self._stop_ev, pause=self._pause_ev)

    def _drain_events(self) -> None:
        """Update tray with latest event; exits when monitor stops."""
        while True:
            try:
                ev = self._event_queue.get(timeout=1.0)
                self._last_event = ev
                self._refresh()
            except queue.Empty:
                if self._status == "stopped":
                    break

    # -------------------------------------------------------------- entry

    def run(self) -> None:
        """Start monitoring and show tray icon. Blocks until Quit."""
        self._start_monitor()
        self._icon = pystray.Icon(
            name="horizon-monitor",
            icon=_make_icon("monitoring"),
            title="horizon-monitor — monitoring",
            menu=pystray.Menu(self._menu_items),
        )
        self._icon.run()
