"""Detect when the Horizon client opens your webcam (or mic) — i.e. when the VDI /
Teams / Webex inside the session is using your camera.

Your webcam is a LOCAL device that the Horizon client redirects into the remote session
(Real-Time Audio-Video). So remote camera use shows up on THIS machine as the Horizon
client process opening the camera — and Windows records exactly that, per app, under:

  HKCU\\…\\CapabilityAccessManager\\ConsentStore\\webcam\\…\\<app>
      LastUsedTimeStart / LastUsedTimeStop   (FILETIME; Stop == 0 ⇒ in use right now)

On this machine the camera user is `horizon-protocol.exe` under "VMware Horizon View
Client", so the default match term is "horizon". Windows keeps only the LAST start/stop
per app, so a full history needs polling: WebcamWatcher snapshots the registry on an
interval and emits an event on every transition (camera turned on / off), writing each to
a log file and firing callbacks the UI wires to a notification.

Read-only: it only reads the registry, never changes camera state.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import winreg
except ImportError:  # non-Windows (tests/CI) — scanning yields nothing
    winreg = None  # type: ignore

_CONSENT = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore"
)
# FILETIME epoch (1601-01-01) to Unix epoch, in seconds.
_FT_EPOCH = 11644473600


def filetime_to_unix(ft: int) -> float:
    """Convert a Windows FILETIME (100 ns ticks since 1601) to Unix seconds."""
    return ft / 1e7 - _FT_EPOCH


def _short_app(app_key: str) -> str:
    """The exe name from a ConsentStore key (path with '#' for '\\'), else the key."""
    return app_key.replace("#", "\\").rsplit("\\", 1)[-1] or app_key


def scan(capability: str = "webcam", match: tuple[str, ...] = ("horizon",)) -> dict[str, tuple[int, int]]:
    """Return {app_key: (start_filetime, stop_filetime)} for matching apps.

    stop == 0 means the app is using the device right now. Apps are matched by a
    case-insensitive substring of their registry key (the exe path). Empty on non-Windows.
    """
    if winreg is None:
        return {}
    base = rf"{_CONSENT}\{capability}"
    terms = [t.lower() for t in match if t.strip()]
    out: dict[str, tuple[int, int]] = {}

    def read_app(parent, name: str) -> None:
        try:
            with winreg.OpenKey(parent, name) as k:
                try:
                    start, _ = winreg.QueryValueEx(k, "LastUsedTimeStart")
                except FileNotFoundError:
                    return
                try:
                    stop, _ = winreg.QueryValueEx(k, "LastUsedTimeStop")
                except FileNotFoundError:
                    stop = 0
                if terms and not any(t in name.lower() for t in terms):
                    return
                out[name] = (int(start), int(stop))
        except OSError:
            return

    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
    except FileNotFoundError:
        return out
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if sub == "NonPackaged":   # desktop apps nest one level deeper
                try:
                    with winreg.OpenKey(root, sub) as npk:
                        j = 0
                        while True:
                            try:
                                app = winreg.EnumKey(npk, j)
                            except OSError:
                                break
                            j += 1
                            read_app(npk, app)
                except OSError:
                    pass
            else:
                read_app(root, sub)
    return out


@dataclass
class WebcamEvent:
    kind: str             # "on" | "off" | "in-use-at-start"
    app: str              # friendly exe name
    when: datetime        # local time of the transition
    duration_s: float     # for "off": how long the device was on (0 otherwise)
    device: str = "webcam"  # which capability — "webcam" | "microphone"


class WebcamWatcher:
    """Polls the capability registry and emits an event on each camera on/off transition.

    Callbacks (set by the caller; default no-ops):
      on_event(WebcamEvent) — every transition (the UI notifies / logs from this)
      on_log(str)           — human-readable status lines
    The first poll seeds a baseline WITHOUT firing on/off alerts (so an already-running
    call when you launch the app doesn't spam you); if the camera is already in use it
    emits one informational "in-use-at-start" event instead.
    """

    def __init__(
        self,
        capability: str = "webcam",
        match: tuple[str, ...] = ("horizon",),
        log_path: str = "./data/webcam_access.log",
        interval: float = 5.0,
        scan_fn=scan,
    ) -> None:
        self._cap = capability
        self._match = tuple(match)
        self._log_path = Path(log_path)
        self._interval = interval
        self._scan = scan_fn
        self._prev: dict[str, tuple[int, int]] = {}
        self._seeded = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.on_event = lambda ev: None
        self.on_log = lambda msg: None

    # --------------------------------------------------------------- queries

    def in_use_now(self) -> list[str]:
        """Friendly names of matching apps currently holding the device (stop == 0)."""
        return [
            _short_app(a)
            for a, (start, stop) in self._scan(self._cap, self._match).items()
            if start and stop == 0
        ]

    def last_access(self) -> WebcamEvent | None:
        """The most recent access on record (from the registry's last start/stop)."""
        snap = self._scan(self._cap, self._match)
        best = None
        for app, (start, stop) in snap.items():
            if not start:
                continue
            in_use = stop == 0
            when = datetime.now() if in_use else _to_local(stop)
            ev = WebcamEvent(
                kind="on" if in_use else "off",
                app=_short_app(app),
                when=_to_local(start) if in_use else when,
                duration_s=0 if in_use else max(0.0, (stop - start) / 1e7),
                device=self._cap,
            )
            if best is None or ev.when > best.when:
                best = ev
        return best

    # ----------------------------------------------------------- poll + run

    def poll_once(self) -> list[WebcamEvent]:
        """Compare a fresh snapshot to the previous one; emit + return any transitions."""
        cur = self._scan(self._cap, self._match)
        events: list[WebcamEvent] = []

        dev = self._cap
        if not self._seeded:
            self._seeded = True
            self._prev = cur
            for app, (start, stop) in cur.items():
                if start and stop == 0:
                    events.append(WebcamEvent("in-use-at-start", _short_app(app),
                                              _to_local(start), 0, dev))
            for ev in events:
                self._emit(ev)
            return events

        for app, (start, stop) in cur.items():
            pstart, pstop = self._prev.get(app, (0, 0))
            in_use, was = (stop == 0 and start > 0), (pstop == 0 and pstart > 0)
            if in_use and not was:
                events.append(WebcamEvent("on", _short_app(app), _to_local(start), 0, dev))
            elif was and not in_use:
                events.append(WebcamEvent("off", _short_app(app), _to_local(stop),
                                          max(0.0, (stop - start) / 1e7), dev))
            elif not in_use and not was and start > pstart > 0:
                # A full on→off happened between polls (Windows kept only the last).
                events.append(WebcamEvent("off", _short_app(app), _to_local(stop),
                                          max(0.0, (stop - start) / 1e7), dev))
        self._prev = cur
        for ev in events:
            self._emit(ev)
        return events

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — keep watching despite a bad poll
                self.on_log(f"Webcam watch error: {exc}")
            self._stop.wait(self._interval)

    # ------------------------------------------------------------- internals

    def _emit(self, ev: WebcamEvent) -> None:
        self._append_log(ev)
        self.on_event(ev)

    def _append_log(self, ev: WebcamEvent) -> None:
        line = (
            f"{ev.when.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"{ev.kind.upper():16}{ev.device:11}{ev.app}"
            + (f"  ({ev.duration_s:.0f}s)" if ev.kind == 'off' and ev.duration_s else "")
        )
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001 — logging must never crash the watcher
            pass


def _to_local(ft: int) -> datetime:
    """FILETIME → local naive datetime (for display/logging)."""
    return datetime.fromtimestamp(filetime_to_unix(ft))
