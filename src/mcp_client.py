"""
Async wrapper around the horizon-mcp MCP server.

Spawns `node C:/github/horizon-mcp/dist/index.js` as a subprocess and
communicates via the MCP stdio protocol using the `mcp` Python SDK.

Usage:
    async with HorizonMCPClient(config) as client:
        b64_png = await client.screenshot(screen=0)
        windows = await client.list_windows()
        await client.focus_window("PVDI")

See the horizon-mcp project for the full tool reference. Monitoring uses the
read paths (screenshot, list_windows, focus_window); the control paths (click,
key_combo, paste_text, scroll, …) drive the remote session and are only invoked
through RemoteController behind the opt-in [control] config flag.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import ProcessInfo


class HorizonMCPClient:
    """Manages a long-lived connection to the horizon-mcp stdio server."""

    # Generous by design: a `screenshot` of a 2-monitor VDI plus ~300ms of
    # PowerShell startup is normally well under 10s, so 90s means "this call is
    # never coming back", not "this call is slow". See _call's docstring.
    DEFAULT_CALL_TIMEOUT_SECONDS = 90.0

    def __init__(
        self, server_path: str, command: str = "node",
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._server_path = server_path
        self._command = command
        self._call_timeout = call_timeout_seconds
        self._session: ClientSession | None = None
        self._cm = None

    async def __aenter__(self) -> "HorizonMCPClient":
        params = StdioServerParameters(
            command=self._command,
            args=[self._server_path],
        )
        self._cm = stdio_client(params)
        read, write = await self._cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.__aexit__(*exc)
        if self._cm:
            await self._cm.__aexit__(*exc)

    # Tools that only OBSERVE. Safe to retry after a timeout because running one
    # twice is indistinguishable from running it once. Anything that sends input
    # (click/type_text/press_key/key_combo/paste_text/scroll/...) is deliberately
    # NOT in here: a timed-out click may well have landed, and re-sending it would
    # double-click a button or type a command twice into the remote.
    _IDEMPOTENT_TOOLS = frozenset({
        "screenshot", "screenshot_region", "screenshot_window", "ocr",
        "list_windows", "list_monitors", "get_window_rect",
        "get_foreground_window", "get_clipboard", "get_pixel_color",
    })

    async def _call(self, tool: str, **kwargs: Any) -> Any:
        """Invoke one MCP tool, with a HARD timeout.

        The timeout is not defensive boilerplate -- it is load-bearing. The stdio
        SDK's call_tool() waits for a response forever, so a single lost/never-sent
        reply wedges the whole caller with no error, no log line and no CPU use.
        Field-tested the hard way 2026-08-24: a `pull-payload` run sat blocked for a
        full hour mid-print (0.000s CPU over 12s, no MCP child process alive, the
        remote left sitting on an open Print dialog). Nothing above could recover,
        because `manifest_timeout_seconds` and every other guard only get evaluated
        BETWEEN tool calls, never during one.

        Converting that hang into a bounded exception is what makes the layers above
        work as designed: pull_payload's manifest poll retries, and a failed print
        surfaces as a normal error whose PDFs `--resume` can pick up.
        """
        assert self._session, "Client not connected — use async with"
        # Drop None kwargs so optional server params fall back to their defaults.
        args = {k: v for k, v in kwargs.items() if v is not None}
        attempts = 2 if tool in self._IDEMPOTENT_TOOLS else 1
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(
                    self._session.call_tool(tool, arguments=args),
                    timeout=self._call_timeout,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:
                if attempt < attempts:
                    continue
                raise TimeoutError(
                    f"horizon-mcp tool {tool!r} did not respond within "
                    f"{self._call_timeout:g}s"
                    + (f" (retried {attempts}x)" if attempts > 1 else "")
                    + ". The MCP server or its PowerShell child is wedged; the "
                    "remote session was left mid-action, so check it by hand before "
                    "re-running."
                ) from exc

    @staticmethod
    def _first_text(result: Any) -> str:
        """Best-effort status text from a tool result (some tools return no body)."""
        content = getattr(result, "content", None) or []
        if content and getattr(content[0], "text", None) is not None:
            return content[0].text
        return ""

    async def screenshot(self, screen: int = 0) -> bytes:
        """Return raw PNG bytes for the given monitor index."""
        result = await self._call("screenshot", screen=screen)
        b64: str = result.content[0].data
        return base64.b64decode(b64)

    async def screenshot_region(
        self, x: int, y: int, width: int, height: int, screen: int | None = None
    ) -> bytes:
        """Return raw PNG bytes for a rectangle. With `screen` omitted, x,y are absolute
        virtual-desktop pixels — the way to capture a VDI window that spans monitors."""
        result = await self._call(
            "screenshot_region", x=x, y=y, width=width, height=height, screen=screen
        )
        b64: str = result.content[0].data
        return base64.b64decode(b64)

    async def list_windows(self) -> list[ProcessInfo]:
        """Return all visible windows."""
        result = await self._call("list_windows")
        data = json.loads(result.content[0].text)
        return [ProcessInfo.from_mcp(d) for d in data]

    async def focus_window(self, target: str) -> str:
        """Bring window to foreground. Returns status string from server."""
        result = await self._call("focus_window", target=target)
        return result.content[0].text

    async def press_key(self, key: str) -> str:
        """Send a key or key combination to the focused window."""
        result = await self._call("press_key", key=key)
        return result.content[0].text

    async def type_text(self, text: str) -> str:
        """Type a string into the focused window."""
        result = await self._call("type_text", text=text)
        return result.content[0].text

    # ----------------------------------------------------------- control tools
    # These drive the remote session (mouse/keyboard). Input reaches the *remote*
    # desktop only when the local Horizon client window has OS focus — callers
    # (see RemoteController) focus it first.

    async def click(
        self, x: int, y: int, button: str = "left", screen: int | None = None
    ) -> str:
        """Click at a pixel coordinate.

        Pass `screen` (a `list_monitors` index) to treat x,y as 0-based from that
        monitor's top-left — the same frame a `screenshot(screen=N)` is in — so image
        coordinates map directly without virtual-desktop offsets.
        """
        return self._first_text(
            await self._call("click", x=x, y=y, button=button, screen=screen)
        )

    async def double_click(self, x: int, y: int, screen: int | None = None) -> str:
        """Double-click at a pixel coordinate (see click() for `screen`)."""
        return self._first_text(
            await self._call("double_click", x=x, y=y, screen=screen)
        )

    async def move_mouse(self, x: int, y: int, screen: int | None = None) -> str:
        """Move the cursor without clicking (hover).

        Pass `screen` (a list_monitors index) so x,y are 0-based from that monitor's
        top-left — the same frame as screenshot(screen=N)/click(screen=N) — instead of
        absolute virtual-desktop coordinates that could land on another display.
        """
        return self._first_text(
            await self._call("move_mouse", x=x, y=y, screen=screen)
        )

    async def key_combo(
        self, keys: list[str], times: int | None = None, hold_ms: int | None = None
    ) -> str:
        """Press a keyboard chord by virtual-key code (handles Win key + modifiers).

        e.g. ["Win"] = Start, ["Win", "R"] = Run, ["Ctrl", "Alt", "Insert"] =
        Ctrl+Alt+Del to the remote. Modifiers first, main key last.
        """
        return self._first_text(
            await self._call("key_combo", keys=keys, times=times, holdMs=hold_ms)
        )

    async def paste_text(self, text: str) -> str:
        """Place text on the clipboard and paste with Ctrl+V — more reliable than
        type_text for password fields and arbitrary characters in a remote session."""
        return self._first_text(await self._call("paste_text", text=text))

    async def get_clipboard(self) -> str:
        """Return the remote/host clipboard text."""
        return self._first_text(await self._call("get_clipboard"))

    async def set_clipboard(self, text: str) -> str:
        """Write text to the clipboard (pass '' to clear a staged secret)."""
        return self._first_text(await self._call("set_clipboard", text=text))

    async def scroll(
        self,
        x: int,
        y: int,
        direction: str,
        amount: int | None = None,
        screen: int | None = None,
    ) -> str:
        """Scroll the mouse wheel at (x, y). direction is 'up' or 'down'.

        Pass `screen` (a `list_monitors` index) so x,y are read relative to that
        monitor's top-left, matching the frame a screenshot(screen=N) was taken in.
        """
        return self._first_text(
            await self._call(
                "scroll", x=x, y=y, direction=direction, amount=amount, screen=screen
            )
        )

    async def get_foreground_window(self) -> str:
        """Return the title/info of the currently focused local window."""
        return self._first_text(await self._call("get_foreground_window"))

    async def get_window_rect(self, target: str) -> dict:
        """Return a window's rectangle as a dict (Title, Pid, Left/Top/Right/Bottom,
        Width, Height, Screen, Device). `Screen` is the monitor index it's mostly on —
        the value to pass as `screen` to the spatial tools. Raises on parse failure."""
        return json.loads(self._first_text(await self._call("get_window_rect", target=target)))

    async def list_monitors(self) -> list[dict]:
        """Return [{index, device, primary, x, y, width, height, dpi, scale}, ...] —
        `index` is the value to pass as `screen` elsewhere; x,y are that monitor's
        top-left in absolute virtual-desktop pixels. Raises on parse failure."""
        return json.loads(self._first_text(await self._call("list_monitors")))

    async def ocr(
        self,
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
        screen: int | None = None,
    ) -> str:
        """Windows built-in OCR over the screen (or a region) — returns JSON text.

        Free/offline; useful as a cheap change pre-filter before Claude Vision. The JSON
        carries per-line/word boxes `{text, x, y, width, height}`. Pass `screen` (a
        list_monitors index) so any returned coordinates are 0-based from that monitor —
        the same frame as click(screen=N) — so an OCR'd box can be clicked directly."""
        return self._first_text(
            await self._call("ocr", x=x, y=y, width=width, height=height, screen=screen)
        )

    async def wait(self, ms: int) -> str:
        """Server-side pause to let the remote session catch up between actions."""
        return self._first_text(await self._call("wait", ms=ms))
