"""
Poll loop and change detection.

Runs every `config.polling.interval_seconds`. On each tick:
  1. Find the Horizon window via list_windows()
  2. Optionally focus it (config.polling.focus_before_shot)
  3. Take a screenshot (screen index from config.polling.screen_index)
  4. Compute perceptual hash via imagehash
  5. Compare with previous hash; if Hamming distance > change_threshold → changed
  6. Emit ScreenState(changed=True/False, ...)
  7. If changed: pass PNG bytes to Extractor

GOTCHA: Combined screenshot (no screen arg) returns empty bytes on some configs.
Always pass screen=0 or screen=1 explicitly.

GOTCHA: focus_window steals focus from the user. If focus_before_shot is False and
Horizon is on a secondary monitor, screenshots still work without stealing focus.
"""

from __future__ import annotations

import asyncio
import io

import imagehash
from PIL import Image

from .mcp_client import HorizonMCPClient
from .models import ProcessInfo, ScreenState


class Poller:
    def __init__(
        self,
        client: HorizonMCPClient,
        interval: float = 3.0,
        change_threshold: int = 10,
        screen_index: int = 0,
        monitor_titles: list[str] | None = None,
        focus_before_shot: bool = False,
    ) -> None:
        self._client = client
        self._interval = interval
        self._threshold = change_threshold
        self._screen = screen_index
        self._monitor_titles = [t.lower() for t in (monitor_titles or ["pvdi"])]
        self._focus = focus_before_shot
        self._prev_hash: imagehash.ImageHash | None = None
        # Absolute VDI rectangle (Left, Top, W, H). The Horizon window can SPAN monitors,
        # so a single-monitor screenshot only captures half of it; when we can resolve the
        # rect we grab the whole window via screenshot_region instead. Falls back to the
        # configured screen index if the rect can't be read.
        self._rect: tuple[int, int, int, int] | None = None
        self._rect_tried = False

    async def find_horizon_window(self) -> ProcessInfo | None:
        """Return the first window whose title matches any monitor_titles entry."""
        windows = await self._client.list_windows()
        for w in windows:
            if any(t in w.title.lower() for t in self._monitor_titles):
                return w
        return None

    async def _resolve_rect(self) -> tuple[int, int, int, int] | None:
        """Resolve the Horizon window's absolute rect once (cached). None if not found."""
        if self._rect_tried:
            return self._rect
        self._rect_tried = True
        for title in self._monitor_titles:
            try:
                r = await self._client.get_window_rect(title)
                w, h = int(r["Width"]), int(r["Height"])
                if w > 0 and h > 0:
                    self._rect = (int(r["Left"]), int(r["Top"]), w, h)
                    return self._rect
            except Exception:  # noqa: BLE001 — try the next title, else fall back
                continue
        return self._rect

    async def _grab(self) -> bytes:
        """Capture the whole Horizon window (spans monitors) when its rect is known;
        otherwise fall back to a single-monitor screenshot."""
        rect = await self._resolve_rect()
        if rect:
            return await self._client.screenshot_region(*rect)
        return await self._client.screenshot(screen=self._screen)

    def _detect_change(self, png_bytes: bytes) -> tuple[bool, str]:
        """Return (changed, hash_str). Updates internal previous hash."""
        img = Image.open(io.BytesIO(png_bytes))
        current = imagehash.average_hash(img)
        if self._prev_hash is None:
            self._prev_hash = current
            return True, str(current)
        distance = self._prev_hash - current
        changed = distance > self._threshold
        if changed:
            self._prev_hash = current
        return changed, str(current)

    async def run(
        self,
        on_change,
        dry_run: bool = False,
        stop: asyncio.Event | None = None,
        pause: asyncio.Event | None = None,
    ) -> None:
        """
        Poll forever. Calls `on_change(state: ScreenState, png: bytes)` when changed.
        If dry_run=True, prints changed/unchanged without calling on_change.
        stop/pause are asyncio.Events set from outside (e.g. tray callbacks).
        """
        while True:
            if stop and stop.is_set():
                break
            if pause and pause.is_set():
                await asyncio.sleep(1.0)
                continue

            try:
                # Skip list_windows when not focusing — saves one PowerShell spawn per cycle
                if self._focus:
                    window = await self.find_horizon_window()
                    await self._client.focus_window(str(window.pid))
                else:
                    window = None

                png = await self._grab()
                if not png:
                    print("WARNING: empty screenshot bytes — check screen_index in config.toml", flush=True)
                    await asyncio.sleep(self._interval)
                    continue

                changed, hash_str = self._detect_change(png)
                state = ScreenState(
                    screenshot_hash=hash_str,
                    window_title=window.title if window else "",
                    changed=changed,
                )

                if dry_run:
                    win_label = f"[{window.title}]" if window else "[no window]"
                    print(f"{'CHANGED' if changed else 'same   '} {hash_str}  {win_label}", flush=True)
                elif changed:
                    await on_change(state, png)

            except Exception as exc:
                print(f"ERROR in poll cycle: {exc}", flush=True)

            await asyncio.sleep(self._interval)
