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
Always pass screen=0 or screen=1 explicitly. See CLAUDE.md §"Known issues".

GOTCHA: focus_window steals focus from the user. If focus_before_shot is False and
Horizon is on a secondary monitor, screenshots still work without stealing focus.

TODO (Step 1): implement `run()` and `_detect_change()`.
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime

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
        focus_before_shot: bool = True,
    ) -> None:
        self._client = client
        self._interval = interval
        self._threshold = change_threshold
        self._screen = screen_index
        self._monitor_titles = [t.lower() for t in (monitor_titles or ["pvdi"])]
        self._focus = focus_before_shot
        self._prev_hash: imagehash.ImageHash | None = None

    async def find_horizon_window(self) -> ProcessInfo | None:
        """Return the first window whose title matches any monitor_titles entry."""
        windows = await self._client.list_windows()
        for w in windows:
            if any(t in w.title.lower() for t in self._monitor_titles):
                return w
        return None

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

    async def run(self, on_change, dry_run: bool = False) -> None:
        """
        Poll forever. Calls `on_change(state: ScreenState, png: bytes)` when changed.
        If dry_run=True, prints changed/unchanged without calling on_change.
        """
        # TODO: implement poll loop
        # Pseudocode:
        #   while True:
        #     window = await self.find_horizon_window()
        #     if window and self._focus: await self._client.focus_window(str(window.pid))
        #     png = await self._client.screenshot(screen=self._screen)
        #     changed, hash_str = self._detect_change(png)
        #     state = ScreenState(screenshot_hash=hash_str, window_title=window.title if window else "", changed=changed)
        #     if dry_run: print(f"{'CHANGED' if changed else 'same   '} {hash_str}")
        #     elif changed: await on_change(state, png)
        #     await asyncio.sleep(self._interval)
        raise NotImplementedError
