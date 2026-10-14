"""
Remote-control layer for the Horizon session — the *write* path.

Monitoring is read-only (screenshot/list_windows/focus_window). This module composes
horizon-mcp's input primitives into meaningful, user-triggered actions: unlock the
remote desktop, launch or un-minimise an app inside it, send a reply, scroll history.

WHY focus first, every time: horizon-mcp's click/type/key_combo go to whatever window
currently holds OS focus. Input only reaches the *remote* desktop when the local Horizon
client window is in the foreground, so every action calls ensure_foreground() first.

INSIDE vs OUTSIDE the remote: focus_window/list_windows only see *local* OS windows (the
Horizon client is one of them). Apps running *inside* the remote desktop — Teams, Symphony
— are just pixels in that client and are NOT separate OS windows, so focus_window cannot
reach them. To launch or un-minimise them we drive the remote's *own* Start menu via
key_combo(["Win"]) → type name → Enter (the Windows key needs real virtual-key codes,
which is exactly what key_combo provides and type_text/press_key cannot).

WHY this is opt-in ([control].enabled): these actions type into and click on a corporate
remote desktop and steal local focus. They must be user-triggered, never automatic.
"""

from __future__ import annotations

import asyncio
import json

from .mcp_client import HorizonMCPClient


def _shift_ocr_box(line: dict, ox: int, oy: int) -> dict:
    """Return `line` (an OCR {text,x,y,width,height,words} dict) with its own box and
    every word's box shifted by (ox, oy) — converts a screen-local OCR result to
    absolute virtual-desktop coordinates."""
    shifted = dict(line)
    if "x" in shifted:
        shifted["x"] = shifted["x"] + ox
    if "y" in shifted:
        shifted["y"] = shifted["y"] + oy
    shifted["words"] = [
        {**w, "x": w["x"] + ox, "y": w["y"] + oy} if "x" in w and "y" in w else w
        for w in (line.get("words") or [])
    ]
    return shifted


def _merge_overlap(acc: list[str], new: list[str]) -> int:
    """Append the non-overlapping tail of `new` onto `acc` in place; return lines added.

    Between two consecutive scroll captures the bottom of the old view repeats at the
    top of the new one. Find the largest k where acc's last k lines equal new's first k
    (the scroll overlap) and append only new[k:]; with no overlap, append all of `new`.
    Whitespace is normalized to absorb minor OCR jitter. Best-effort: OCR variance can
    occasionally misjudge the overlap, so callers treat the stitched result as a draft.
    """
    def norm(s: str) -> str:
        return " ".join(s.split())

    if not new:
        return 0
    na = [norm(s) for s in acc]
    nn = [norm(s) for s in new]
    for k in range(min(len(na), len(nn)), 0, -1):
        if na[-k:] == nn[:k]:
            tail = new[k:]
            acc.extend(tail)
            return len(tail)
    acc.extend(new)
    return len(new)


class RemoteController:
    def __init__(
        self,
        client: HorizonMCPClient,
        focus_target: str = "PVDI",
        launch_wait: float = 1.5,
        clipboard_sync: float = 0.6,
        copy_timeout: float = 6.0,
        screen: int = 0,
    ) -> None:
        self._client = client
        self._focus_target = focus_target
        self._launch_wait = launch_wait
        # The VDI's on-screen rectangle in ABSOLUTE virtual-desktop pixels
        # (Left, Top, Width, Height), auto-detected from the Horizon window on first use.
        # We deliberately work in absolute coords rather than a single list_monitors index
        # because this VDI SPANS TWO MONITORS: a per-monitor index only ever sees/clicks
        # one half, so OCR misses (and clicks miss) anything on the other half — which is
        # exactly how the unlock "Sign in" button got clicked on the wrong screen. `screen`
        # is kept only as a legacy fallback origin if the window rect can't be read.
        self._screen = screen
        self._rect: tuple[int, int, int, int] | None = None
        self._monitors: list[dict] | None = None
        # How long redirection takes to sync the clipboard between local and remote,
        # and how long to wait for a copied file to arrive before giving up.
        self._clipboard_sync = clipboard_sync
        self._copy_timeout = copy_timeout

    async def ensure_foreground(self) -> None:
        """Bring the local Horizon client to the foreground so input reaches the remote,
        and (once per controller) detect its absolute on-screen rectangle."""
        await self._client.focus_window(self._focus_target)
        await asyncio.sleep(0.4)
        await self.resolve_rect()

    async def resolve_rect(self) -> tuple[int, int, int, int]:
        """Detect the VDI's absolute on-screen rectangle (Left, Top, Width, Height).

        All spatial ops (screenshot region, OCR, clicks, scroll) use these absolute
        virtual-desktop coordinates so they address the WHOLE VDI correctly even when it
        spans multiple monitors. Cached after the first successful lookup; falls back to
        the configured monitor's origin only if the window can't be found.
        """
        if self._rect is not None:
            return self._rect
        try:
            r = await self._client.get_window_rect(self._focus_target)
            left, top = int(r["Left"]), int(r["Top"])
            w, h = int(r["Width"]), int(r["Height"])
            if w > 0 and h > 0:
                self._rect = (left, top, w, h)
        except Exception:  # noqa: BLE001 — fall back below
            pass
        if self._rect is None:
            self._rect = (0, 0, 1920, 1080)
        return self._rect

    async def _monitors_in_rect(
        self, rect: tuple[int, int, int, int]
    ) -> list[dict]:
        """List monitors (list_monitors() shape) that intersect `rect`, cached after
        the first lookup (the physical monitor layout doesn't change mid-session)."""
        if self._monitors is None:
            try:
                self._monitors = await self._client.list_monitors()
            except Exception:  # noqa: BLE001 — caller falls back to the whole rect
                self._monitors = []
        left, top, w, h = rect
        right, bottom = left + w, top + h
        return [
            m for m in self._monitors
            if m["x"] < right and m["x"] + m["width"] > left
            and m["y"] < bottom and m["y"] + m["height"] > top
        ]

    # alias used by the tray / CLI when the user just wants the window up front
    async def bring_to_front(self) -> None:
        await self.ensure_foreground()

    async def _remote_center(self) -> tuple[int, int]:
        """Centre of the VDI in ABSOLUTE virtual-desktop pixels (click with no `screen`)."""
        left, top, w, h = await self.resolve_rect()
        return left + w // 2, top + h // 2

    async def screen_center(self) -> tuple[int, int]:
        """Public centre of the remote surface (default scroll/click point), absolute."""
        return await self._remote_center()

    @staticmethod
    def _box_center(box: dict) -> tuple[int, int] | None:
        try:
            return (
                int(box["x"] + box["width"] / 2),
                int(box["y"] + box["height"] / 2),
            )
        except (KeyError, TypeError):
            return None

    async def find_text_point(
        self, needle: str, *, taskbar_only: bool = False, taskbar_height: int = 56,
        top_only: bool = False, top_height: int = 300, skip_top: int = 0,
    ) -> tuple[int, int] | None:
        """OCR the VDI and return the ABSOLUTE click point of the best match for
        `needle`.

        Tries, in order: (1) a LINE whose full text exactly equals `needle`, (2) a WORD
        within any line that exactly equals `needle`, (3) the first line CONTAINING
        `needle` as a substring. The exact passes run first because a naive substring
        search picks up unrelated matches that happen to appear earlier in OCR's
        reading order — e.g. searching "Print" against a print dialog matches the
        "Printer" label (which OCR reads before the actual Print button), so the click
        landed on the printer-name field instead of submitting the dialog, and the
        automated pull silently stalled waiting for a Save dialog that never opened.

        `taskbar_only=True` restricts every monitor's OCR to its bottom
        `taskbar_height` px, so desktop/window CONTENT can't match at all — use this
        for "does an app have a taskbar button" checks. Field-tested live: an
        unscoped search for "Notepad" matched other on-screen content unpredictably
        (whatever app happened to have that word visible), and a caller that force-
        closes whatever it clicks (Alt+F4) on a false match can close an unrelated
        window — this happened for real once, closing a chat app and an editor that
        had nothing to do with the pull. Always prefer the narrowest scope that can
        still find the target.

        `top_only=True` is the same idea for a horizontal BAND instead of the
        bottom strip: it searches `skip_top` to `skip_top + top_height` px from
        each monitor's top edge, skipping the first `skip_top` px entirely -- use
        this for window-chrome text (ribbon, address bar breadcrumb) that ALSO
        appears again elsewhere in the same window and would otherwise ambiguously
        match. Field-tested live, TWICE, on the same target: Explorer shows
        "This PC" in the window's OWN TITLE BAR (y~0-30), the address bar
        breadcrumb (y~150-260), AND the left navigation pane (can be hundreds of
        px down) -- an unscoped search matched the sidebar entry (wrong), and
        `top_only` with `skip_top=0` then matched the TITLE BAR instead (also
        wrong -- clicking there does nothing, so the whole launch silently no-
        opped with no exception anywhere). Only `skip_top` past the title bar
        band lands on the breadcrumb specifically. Mutually exclusive with
        `taskbar_only` -- if both are set, taskbar_only wins.

        OCR runs PER MONITOR (screen=N) rather than one call spanning the whole
        possibly-multi-monitor rect, then each monitor's LOCAL boxes are shifted back
        to absolute by its own origin and merged. Field-tested live: a single OCR call
        over a rect 2 monitors wide (3840x1080) returned systematically wrong box
        coordinates for text on the right-hand monitor (an internal image-resize in
        the OCR tool, presumably triggered past some pixel-count threshold) — the same
        "Print" button OCR'd correctly at the right pixel position when scanned alone
        at 1920x1080, but landed ~180px off when scanned as part of the full 3840-wide
        span. That silently sent every dialog click (Print, Save, Landscape, …) to the
        wrong spot, with the automated pull stalling waiting for a Save dialog that
        never opened, while an interactive human click at the visually-obvious
        coordinate worked immediately. Falls back to one whole-rect OCR call if
        list_monitors() is unavailable. Returns None if not found / OCR fails.
        """
        needle = needle.lower()
        rect = await self.resolve_rect()
        left, top, w, h = rect
        monitors = await self._monitors_in_rect(rect)
        lines: list[dict] = []
        try:
            if monitors:
                for mon in monitors:
                    if taskbar_only:
                        strip_h = min(taskbar_height, mon["height"])
                        raw = await self._client.ocr(
                            x=0, y=mon["height"] - strip_h,
                            width=mon["width"], height=strip_h,
                            screen=mon["index"],
                        )
                    elif top_only:
                        band_top = min(skip_top, mon["height"])
                        strip_h = min(top_height, mon["height"] - band_top)
                        raw = await self._client.ocr(
                            x=0, y=band_top, width=mon["width"], height=strip_h,
                            screen=mon["index"],
                        )
                    else:
                        raw = await self._client.ocr(screen=mon["index"])
                    data = json.loads(raw)
                    # `ocr(..., screen=N)` returns coordinates already relative to
                    # that MONITOR's (0,0) — even when a sub-region (x,y,w,h) is
                    # also passed to narrow the search area, e.g. the taskbar strip
                    # above. Field-tested live: adding the strip's own y-offset a
                    # second time here double-counted it, producing y values like
                    # ~2082 on a 1080px-tall monitor (physically impossible) and
                    # sending clicks to nowhere. Only mon["x"]/mon["y"] (the
                    # monitor's own virtual-desktop origin) belong in the shift.
                    ox, oy = mon["x"], mon["y"]
                    for line in data.get("lines") or []:
                        lines.append(_shift_ocr_box(line, ox, oy))
            else:
                if taskbar_only:
                    strip_top, strip_h = top + max(0, h - taskbar_height), taskbar_height
                elif top_only:
                    band_top = top + min(skip_top, h)
                    strip_top, strip_h = band_top, min(top_height, top + h - band_top)
                else:
                    strip_top, strip_h = top, h
                raw = await self._client.ocr(x=left, y=strip_top, width=w, height=strip_h)
                data = json.loads(raw)
                lines = data.get("lines") or []
        except Exception:  # noqa: BLE001 — OCR/parse failure → caller falls back
            return None
        for line in lines:
            if (line.get("text") or "").strip().lower() == needle:
                point = self._box_center(line)
                if point:
                    return point
        for line in lines:
            for word in line.get("words") or []:
                if (word.get("text") or "").strip().lower() == needle:
                    point = self._box_center(word)
                    if point:
                        return point
        for line in lines:
            if needle in (line.get("text") or "").lower():
                point = self._box_center(line)
                if point:
                    return point
        return None

    async def activate_taskbar(
        self, needles: list[str], taskbar_height: int = 56
    ) -> bool:
        """Bring an app to the front by clicking its TASKBAR button.

        OCRs only the bottom `taskbar_height` strip of the VDI (so desktop/window text
        can't match) and clicks the first button whose label contains any of `needles`
        (case-insensitive). Re-read every call, so it tolerates the buttons shifting as
        windows open/close — unlike a fixed coordinate. Returns True if a button was
        found and clicked, False if no label matched (e.g. the app isn't running).

        NOTE: a Windows taskbar click TOGGLES — clicking the already-foreground app
        minimizes it. Callers that rotate through several apps avoid this naturally by
        always clicking a different app than the current foreground one.
        """
        await self.ensure_foreground()
        left, top, w, h = await self.resolve_rect()
        strip_top = top + max(0, h - taskbar_height)
        try:
            raw = await self._client.ocr(x=left, y=strip_top, width=w, height=taskbar_height)
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — OCR/parse failure → report not-found
            return False
        wants = [n.lower() for n in needles if n and n.strip()]
        for line in data.get("lines") or []:
            text = (line.get("text") or "").lower()
            if any(n in text for n in wants):
                point = self._box_center(line)
                if point:
                    await self._client.click(point[0], point[1])  # absolute coords
                    return True
        return False

    async def unlock(self, password: str, *, submit: bool = True) -> None:
        """Advance the remote lock screen to the logon prompt and TYPE the password.

        The sequence is exactly what field-testing this VDI proved out:
          1. focus the local Horizon client (ensure_foreground)
          2. CLICK the remote surface — Ctrl+Alt+Del is ignored until the remote
             actually holds input focus
          3. Ctrl+Alt+Insert (Horizon's Ctrl+Alt+Del) -> the Windows logon prompt
          4. TYPE the password — the secure logon field REJECTS clipboard paste, and
             this VDI forwards keys by scan code (horizon-mcp type_text now sends real
             scan codes), so typing is the only channel that lands. Nothing touches
             the clipboard, so the secret never lingers there.
          5. submit by CLICKING the "Sign in" button (located via OCR) — field-tested:
             Enter alone does NOT submit this LogonUI, but the button click does. Falls
             back to Enter if the button can't be located.

        An empty password is never submitted — a blank/failed entry burns a login
        attempt and risks account lockout.
        """
        await self.ensure_foreground()
        cx, cy = await self._remote_center()
        await self._client.click(cx, cy)        # absolute coords; give the remote focus
        await asyncio.sleep(0.4)
        await self._client.key_combo(["Ctrl", "Alt", "Insert"])  # Horizon's Ctrl+Alt+Del
        await asyncio.sleep(2.5)                                 # wait for the login prompt
        if not password:
            return
        await self._client.type_text(password)
        if submit:
            await asyncio.sleep(0.4)
            point = await self.find_text_point("sign in")
            if point:
                await self._client.click(point[0], point[1])   # absolute virtual coords
            else:
                # Couldn't OCR the button — fall back to Enter (works on some configs).
                await self._client.key_combo(["Enter"])

    async def nudge(self) -> None:
        """Anti-idle keep-alive: focus the remote and send a benign F15 keypress.

        F15 is the classic "caffeine" key — it counts as input (so it resets the
        session's idle timer) but virtually no application binds it, so nothing visible
        happens: no typing, no click, and crucially NO pointer teleport. This avoids the
        earlier mouse-move approach, which jumped the cursor to a corner of whichever
        monitor the Horizon window resolved to (wrong/disruptive when the VDI spans
        multiple displays). Sending input needs the Horizon client foreground, so this
        briefly steals local focus — intended for when the user has stepped away.
        """
        await self.ensure_foreground()
        await self._client.key_combo(["F15"])

    async def open_start(self) -> None:
        await self.ensure_foreground()
        await self._client.key_combo(["Win"])
        await asyncio.sleep(0.8)

    async def open_run(self) -> None:
        await self.ensure_foreground()
        await self._client.key_combo(["Win", "R"])
        await asyncio.sleep(0.8)

    async def launch_or_activate(self, app: str) -> None:
        """Open the remote Start menu, search for `app`, and launch (or focus) it.

        For single-instance chat apps (Teams, Symphony) this brings an already-running,
        minimised window to the foreground; for others it may start a new instance.
        """
        await self.open_start()
        await self._client.type_text(app)
        await asyncio.sleep(self._launch_wait)
        await self._client.key_combo(["Enter"])
        await asyncio.sleep(self._launch_wait)

    async def run_command(self, command: str) -> None:
        """Open the Run dialog and execute a command (e.g. an exe path or 'teams')."""
        await self.open_run()
        await self._client.type_text(command)
        await asyncio.sleep(0.4)
        await self._client.key_combo(["Enter"])
        await asyncio.sleep(self._launch_wait)

    async def send_reply(self, text: str, submit: bool = True) -> None:
        """Type `text` into whatever input currently has focus in the remote, then
        optionally press Enter. The caller is responsible for the chat input being
        focused (e.g. the user clicked it, or a prior click_at)."""
        await self.ensure_foreground()
        await self._client.type_text(text)
        if submit:
            await asyncio.sleep(0.2)
            await self._client.key_combo(["Enter"])

    async def click_at(self, x: int, y: int, button: str = "left") -> None:
        await self.ensure_foreground()
        await self._client.click(x, y, button)

    async def scroll_history(
        self, x: int, y: int, direction: str = "up", amount: int = 3
    ) -> None:
        """Scroll the chat pane at (x, y) to reveal earlier/later messages."""
        await self.ensure_foreground()
        await self._client.scroll(x, y, direction, amount)

    # ------------------------------------------------------- code-editing bridge
    # Pull a file's text out of the remote editor, edit it locally (with AI that
    # isn't available inside the VDI), and push the whole document back.
    #
    # The transport is the clipboard, NOT OCR. OCR loses indentation and only sees
    # the visible region; type_text corrupts code via the editor's autocomplete /
    # auto-indent. A Ctrl+A/Ctrl+C in the remote editor copies the *entire* file
    # exactly; Horizon clipboard redirection syncs it to the local clipboard, where
    # get_clipboard() reads it losslessly. The write path is the mirror image.
    #
    # PREREQUISITE: Horizon clipboard redirection must be enabled (it usually is).
    # copy_from_remote() detects when it is not and raises a clear error.
    # PREREQUISITE: the editor pane (not a sidebar/terminal) must hold focus inside
    # the remote so Ctrl+A selects the document — screenshot first if unsure.

    # Null bytes can't appear in a text file, so this never collides with content.
    _COPY_SENTINEL = "\x00__horizon_pull__\x00"

    async def copy_from_remote(self) -> str:
        """Select-all + copy in the focused remote editor; return the full text.

        Seeds the local clipboard with a sentinel, triggers the remote copy, then
        polls until clipboard redirection delivers the file. Raises RuntimeError if
        the clipboard never changes (redirection disabled, or nothing was focused).
        """
        await self.ensure_foreground()
        await self._client.set_clipboard(self._COPY_SENTINEL)
        await asyncio.sleep(self._clipboard_sync)
        await self._client.key_combo(["Ctrl", "A"])
        await asyncio.sleep(0.2)
        await self._client.key_combo(["Ctrl", "C"])

        waited = 0.0
        poll = 0.3
        while waited < self._copy_timeout:
            await asyncio.sleep(poll)
            waited += poll
            text = await self._client.get_clipboard()
            if text and text != self._COPY_SENTINEL:
                return text
        raise RuntimeError(
            "Clipboard did not update after Ctrl+C — Horizon clipboard redirection "
            "may be disabled, or no editor pane was focused in the remote session."
        )

    async def paste_to_remote(
        self, text: str, *, replace_all: bool = True, save: bool = False
    ) -> None:
        """Replace the focused remote editor's contents with `text`.

        Stages `text` on the local clipboard (redirection syncs it to the remote),
        selects all + pastes, optionally saves, then restores the prior clipboard.
        """
        await self.ensure_foreground()
        prior = ""
        try:
            prior = await self._client.get_clipboard()
        except Exception:
            pass
        try:
            await self._client.set_clipboard(text)
            await asyncio.sleep(self._clipboard_sync)  # let redirection reach the remote
            if replace_all:
                await self._client.key_combo(["Ctrl", "A"])
                await asyncio.sleep(0.15)
            await self._client.key_combo(["Ctrl", "V"])
            await asyncio.sleep(self._clipboard_sync)   # let the paste land
            if save:
                await self._client.key_combo(["Ctrl", "S"])
                await asyncio.sleep(0.3)
        finally:
            try:
                await self._client.set_clipboard(prior or "")
            except Exception:
                pass

    async def open_file(self, path: str) -> None:
        """Open `path` in the remote VS Code via the Quick Open palette (Ctrl+P)."""
        await self.ensure_foreground()
        await self._client.key_combo(["Ctrl", "P"])
        await asyncio.sleep(0.4)
        await self._client.type_text(path)
        await asyncio.sleep(0.5)
        await self._client.key_combo(["Enter"])
        await asyncio.sleep(0.4)

    # ------------------------------------------------- OCR read bridge (DLP mode)
    # When the VDI blocks clipboard copy-OUT (remote -> local) — a common DLP
    # policy — copy_from_remote() above cannot work: there is no lossless channel
    # to get text off the remote. The only one left is the screen itself, so we
    # READ via screenshot + Windows OCR, scrolling the pane and stitching captures
    # for content taller than one screen. OCR is LOSSY: it confuses 1/l/I, drops
    # indentation, and mangles symbols — so treat anything read back as a DRAFT to
    # verify, never as source of truth, especially for code.
    #
    # The WRITE direction is unaffected: paste-IN (local -> remote) is allowed, so
    # paste_to_remote() and paste_at() stage the local clipboard and Ctrl+V.
    #
    # REGION COORDS: the optional `region` is in ACTUAL screen pixels, which differ
    # from screenshot pixels under Windows display scaling — coordinates eyeballed
    # off a screenshot will miss. Prefer full-screen OCR (region=None); it needs no
    # coordinates and is the reliable default.

    async def _ocr_lines(self, region: tuple[int, int, int, int] | None) -> list[str]:
        """OCR the VDI (or an absolute x,y,w,h region) and return non-blank text lines.

        With no region, OCR covers the whole VDI rectangle in absolute virtual-desktop
        coordinates — without that, a no-arg OCR scans the *primary* display, which is the
        local desktop, not the (possibly multi-monitor-spanning) remote.
        """
        if region:
            x, y, w, h = region
            raw = await self._client.ocr(x=x, y=y, width=w, height=h)
        else:
            left, top, w, h = await self.resolve_rect()
            raw = await self._client.ocr(x=left, y=top, width=w, height=h)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        # horizon-mcp returns lines as objects {text, x, y, width, height, words}, so
        # pull out each line's text (tolerate a bare-string shape just in case).
        out: list[str] = []
        for ln in data.get("lines") or []:
            text = ln.get("text", "") if isinstance(ln, dict) else str(ln)
            if text.strip():
                out.append(text)
        return out

    async def read_screen(
        self, region: tuple[int, int, int, int] | None = None
    ) -> str:
        """One-shot OCR of the remote screen (or a region). Lossy — verify any code."""
        await self.ensure_foreground()
        await asyncio.sleep(0.3)
        return "\n".join(await self._ocr_lines(region))

    async def read_scrolling(
        self,
        x: int,
        y: int,
        region: tuple[int, int, int, int] | None = None,
        max_screens: int = 12,
        scroll_amount: int = 12,
        settle: float = 0.6,
    ) -> tuple[str, int]:
        """Read a pane taller than one screen: OCR, scroll DOWN at (x, y), OCR again,
        stitching captures with overlap-dedup. Stops at the bottom (the view stops
        moving, or a capture adds nothing) or after max_screens. Returns
        (text, screens_captured).

        Scroll the pane to the TOP first (the caller's job) so this reads top-to-
        bottom. OCR is lossy and the overlap match is best-effort — verify the result.
        """
        await self.ensure_foreground()
        acc: list[str] = await self._ocr_lines(region)
        prev_norm = [" ".join(s.split()) for s in acc]
        screens = 1
        for _ in range(max_screens - 1):
            await self._client.scroll(x, y, "down", scroll_amount)  # absolute coords
            await asyncio.sleep(settle)
            new = await self._ocr_lines(region)
            new_norm = [" ".join(s.split()) for s in new]
            if new_norm == prev_norm:
                break  # the view did not move — bottom reached (or scroll missed)
            added = _merge_overlap(acc, new)
            prev_norm = new_norm
            screens += 1
            if added == 0:
                break  # everything new overlapped what we have — at the bottom
        return "\n".join(acc), screens

    async def paste_at(
        self,
        text: str,
        x: int | None = None,
        y: int | None = None,
        double: bool = False,
        save: bool = False,
        submit: bool = False,
    ) -> None:
        """Paste `text` into the remote at the cursor — write-IN is allowed under DLP.

        If (x, y) is given, click there first to focus the target app/field; pass
        double=True for the apps that need two clicks to take focus. Stages the local
        clipboard, Ctrl+V, then optionally Ctrl+S (save) or Enter (submit), and
        restores the prior clipboard. Unlike paste_to_remote() this does NOT select-
        all, so it inserts at the cursor instead of replacing the whole document.
        """
        await self.ensure_foreground()
        if x is not None and y is not None:
            if double:
                await self._client.double_click(x, y)
            else:
                await self._client.click(x, y)
            await asyncio.sleep(0.3)
        prior = ""
        try:
            prior = await self._client.get_clipboard()
        except Exception:
            pass
        try:
            await self._client.set_clipboard(text)
            await asyncio.sleep(self._clipboard_sync)  # let it reach the remote
            await self._client.key_combo(["Ctrl", "V"])
            await asyncio.sleep(self._clipboard_sync)  # let the paste land
            if save:
                await self._client.key_combo(["Ctrl", "S"])
                await asyncio.sleep(0.3)
            elif submit:
                await self._client.key_combo(["Enter"])
        finally:
            try:
                await self._client.set_clipboard(prior or "")
            except Exception:
                pass
