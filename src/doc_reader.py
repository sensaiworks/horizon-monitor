"""
Read an on-screen document (Word, a PDF viewer, Notepad, VS Code, …) that is
open in the VDI into plain text, via keyboard paging + Windows OCR.

Why this exists, and why it is NOT backfill.py
-----------------------------------------------
`backfill.py` scrolls a Teams chat with the mouse WHEEL. For a *document* the
wheel is the wrong tool. Field-tested on this Horizon VDI (2026-10-16): a
16-notch wheel pulse moved an open Word page by only a few pixels — the client
swallows almost all of the wheel — so a 12-page document would take hundreds of
stalled pulses and still barely move. The keyboard is reliable where the wheel
is not: `Ctrl+Home` jumps to the top, a single `PageDown` advances exactly one
viewport every time, `Ctrl+End` jumps to the end. So this reader PAGES with the
keyboard and OCRs each viewport (Windows OCR — FREE, offline, no Claude API),
then stitches the viewports into one text, deduping the overlap each PageDown
leaves behind.

End-of-document detection
-------------------------
PageDown past the last page leaves the viewport unchanged. When two consecutive
OCR frames carry the same text, the bottom has been reached and we stop — no need
to know the page count up front.

SAFETY — never read while a meeting is screen-sharing
-----------------------------------------------------
Driving the VDI (focusing the Horizon window, paging a document) is visible to
everyone in a Teams/Webex call when the user is sharing their screen. Before
touching the session this reader OCRs for the live-share banner ("You're sharing
your screen") and the in-call "Stop sharing" control, and REFUSES if a share
looks active. Field-tested 2026-10-16: a document-read request arrived while the
user was mid-scrum and actively sharing their screen; opening a private document
or chat then would have broadcast it to the whole call. The guard turns that into
an explicit, safe refusal (`DocReadBlocked`) instead of a silent exposure. Pass
`force=True` only when you already know nothing is being shared.

Preconditions (like backfill, this does NOT open the file itself)
-----------------------------------------------------------------
  * [control].enabled = true;
  * the document already open and MAXIMIZED ALONE on its monitor. If other
    windows (Teams, Explorer) overlap it, a PageDown can land on the wrong
    window — maximized-and-alone keeps keyboard focus on the document. This was
    the concrete failure the first time round: a Teams chat surfaced over Word
    and the Page Downs started scrolling the chat instead.

Entry points
------------
  DocReader(config).read_frontmost(client)   # async, drives the remote
  stitch_pages([[line, ...], ...])            # pure, unit-testable
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .scroller import lines_sorted


# --------------------------------------------------------------------------- #
# Pure helpers — unit-testable offline against saved OCR frames.              #
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    """Alnum-only lowercase key, so two OCR passes of the same rendered line
    compare equal despite punctuation/spacing jitter."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def frame_lines(ocr: object) -> list[str]:
    """Non-empty OCR text lines of one viewport, in reading order."""
    return [t for t in (l.get("text", "").strip() for l in lines_sorted(ocr)) if t]


def frames_same(a: list[str], b: list[str]) -> bool:
    """True if two viewports carry the same text (⇒ PageDown moved nothing ⇒
    bottom of document). Compares on the normalized key to ignore OCR jitter."""
    ja = "\n".join(_norm(x) for x in a)
    jb = "\n".join(_norm(x) for x in b)
    return bool(ja) and ja == jb


def stitch_pages(pages: list[list[str]]) -> str:
    """Join overlapping viewports into one document text.

    Each PageDown leaves some lines from the previous viewport visible at the top
    of the next, so naively concatenating repeats them. For every new page we find
    the longest suffix of what we have so far that equals the new page's prefix
    (matched on the normalized key) and append only the non-overlapping tail.
    """
    out: list[str] = []
    out_norm: list[str] = []
    for lines in pages:
        norm = [_norm(x) for x in lines]
        overlap = 0
        max_k = min(len(out_norm), len(norm))
        for k in range(max_k, 0, -1):
            # ignore blank normalized keys when matching the seam
            if out_norm[-k:] == norm[:k]:
                overlap = k
                break
        out.extend(lines[overlap:])
        out_norm.extend(norm[overlap:])
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# Live reader — drives the remote via HorizonMCPClient.                        #
# --------------------------------------------------------------------------- #

class DocReadBlocked(RuntimeError):
    """Raised by the safety guard when a screen-share/meeting looks active."""


@dataclass
class DocReadResult:
    pages: int = 0
    chars: int = 0
    stopped: str = ""         # "bottom" | "max_pages"
    text: str = ""

    def summary(self) -> str:
        return (f"{self.pages} viewports -> {self.chars} chars; "
                f"stopped: {self.stopped}")


class DocReader:
    """Page the front-most VDI document top-to-bottom and OCR it into text."""

    # Phrases that mean a screen share / presentation is live. Lowercased and
    # matched as substrings so OCR spacing/case noise still trips them.
    _SHARE_FLAGS = (
        "sharing your screen",
        "stop sharing",
        "you're sharing",
        "youre sharing",
        "you're presenting",
        "youre presenting",
        "stop presenting",
    )

    def __init__(self, config: dict, on_log: Callable[[str], None] | None = None) -> None:
        self._config = config
        self._log = on_log or (lambda _s: None)
        dr = config.get("doc_reader", {})
        ctl = config.get("control", {})
        self._focus_target = ctl.get("focus_target", "PVDI")
        self._screen = int(dr.get("screen", ctl.get("screen", 0)))
        # Reading region on that monitor: the document body, excluding the Word
        # ribbon (top) and status bar (bottom). Field default matches a maximized
        # Word window at 1920x1080, 100% zoom.
        self._region = tuple(dr.get("region", [540, 185, 850, 830]))
        self._max_pages = int(dr.get("max_pages", 60))
        self._settle_ms = int(dr.get("settle_ms", 500))
        self._page_wait_ms = int(dr.get("page_wait_ms", 450))
        self._focus_click = bool(dr.get("focus_click", True))
        flags = dr.get("share_flags")
        self._share_flags = tuple(f.lower() for f in flags) if flags else self._SHARE_FLAGS

    async def screen_share_active(self, client, screens: list[int] | None = None) -> str | None:
        """Return a human reason if a live screen share is detected, else None.

        OCRs each monitor full-frame (free) and looks for the share/presenting
        banners. A one-off check, so the extra OCR calls are cheap insurance.
        """
        if screens is None:
            try:
                screens = [int(m["index"]) for m in await client.list_monitors()]
            except Exception:
                screens = [self._screen]
        for s in screens:
            try:
                text = (await client.ocr(screen=s)).lower()
            except Exception:
                continue
            for flag in self._share_flags:
                if flag in text:
                    return (f"a screen share looks active (found {flag!r} on "
                            f"monitor {s}) — refusing to drive the VDI so the "
                            f"document isn't broadcast to the call")
        return None

    async def read_frontmost(
        self, client, *, max_pages: int | None = None, screen: int | None = None,
        force: bool = False, should_stop: Callable[[], bool] | None = None,
    ) -> DocReadResult:
        """Read the front-most, maximized document into text. See module docstring
        for preconditions. Raises DocReadBlocked if a screen share is detected
        (unless force=True)."""
        screen = self._screen if screen is None else screen
        max_pages = self._max_pages if max_pages is None else max_pages
        rx, ry, rw, rh = self._region

        # 0) Horizon must hold OS focus for keystrokes to reach the remote.
        await client.focus_window(self._focus_target)
        await client.wait(400)

        # 1) SAFETY: never drive a session that's being screen-shared.
        if not force:
            reason = await self.screen_share_active(client)
            if reason:
                raise DocReadBlocked(reason)

        # 2) Give the document keyboard focus, then jump to the top. The click
        #    lands in the top page margin (whitespace), not on body text/links.
        if self._focus_click:
            await client.click(rx + rw // 2, ry + 12, screen=screen)
        await client.press_key("Ctrl+Home")
        await client.wait(self._settle_ms)

        # 3) Page down one viewport at a time, OCR each, stop when a page repeats.
        pages: list[list[str]] = []
        prev: list[str] | None = None
        result = DocReadResult()
        for i in range(max_pages):
            if should_stop and should_stop():
                result.stopped = "cancelled"
                break
            ocr = await client.ocr(x=rx, y=ry, width=rw, height=rh, screen=screen)
            lines = frame_lines(ocr)
            if prev is not None and frames_same(prev, lines):
                result.stopped = "bottom"
                break
            pages.append(lines)
            prev = lines
            self._log(f"viewport {i:02d}: {len(lines)} lines")
            await client.press_key("PageDown")
            await client.wait(self._page_wait_ms)
        else:
            result.stopped = "max_pages"

        result.text = stitch_pages(pages)
        result.pages = len(pages)
        result.chars = len(result.text)
        return result
