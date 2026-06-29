"""System-wide user-activity detection (Windows) so automation yields to the user.

The auto-monitor drives the remote by switching apps, which STEALS local focus — so the
moment the user touches this PC we must back off. Windows' GetLastInputInfo reports the
tick (ms, from GetTickCount) of the most recent input event of ANY kind (mouse OR
keyboard), system-wide. That is exactly the signal we need:

  - idle_ms()      → how long since the last input (used for the "resume after 90s" gate
                     while we're paused and producing no input of our own).
  - user_active()  → did input arrive *after our last automated action*? The capture loop
                     calls mark_self_input() right after each switch/click/keystroke, so a
                     later tick can only mean the user did something.

Our own injected input may or may not bump GetLastInputInfo depending on how the Horizon
client forwards it. mark_self_input() makes the design correct either way: if our input
counts, the mark captures it and excludes it; if it doesn't, the mark simply records the
user's last input and user_active() still only trips on genuinely new user input.

Non-Windows / API failure degrades safely: idle_ms() returns a large number (treat as
idle) and user_active() returns False, so the loop runs without the yield behavior rather
than crashing.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


try:
    _user32 = ctypes.windll.user32          # type: ignore[attr-defined]
    _kernel32 = ctypes.windll.kernel32      # type: ignore[attr-defined]
    _AVAILABLE = True
except (AttributeError, OSError):           # not Windows
    _user32 = _kernel32 = None
    _AVAILABLE = False


def _last_input_tick() -> int:
    """GetTickCount value (ms) of the most recent system-wide input, or 0 on failure."""
    if not _AVAILABLE:
        return 0
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return int(info.dwTime)


def _now_tick() -> int:
    if not _AVAILABLE:
        return 0
    return int(_kernel32.GetTickCount())


class ActivityMonitor:
    """Tracks user vs. our-own input so the capture loop can yield to the user."""

    def __init__(self) -> None:
        # Baseline: treat whatever happened up to now as "not the user's doing".
        self._self_tick = _last_input_tick()

    def mark_self_input(self) -> None:
        """Record that WE just produced the latest input. Call right after each action."""
        self._self_tick = _last_input_tick()

    def user_active(self) -> bool:
        """True if input arrived after our last mark — i.e. the user touched the PC."""
        if not _AVAILABLE:
            return False
        return _last_input_tick() != self._self_tick

    def idle_ms(self) -> int:
        """Milliseconds since the last input of any kind. Large (idle) if unavailable."""
        if not _AVAILABLE:
            return 1 << 30
        return max(0, _now_tick() - _last_input_tick())
