"""
Direct-help agent — a screen-aware computer-use loop scoped to the remote VDI.

The user states a goal in natural language ("switch to master, don't stash my changes").
This agent then runs Anthropic's **computer use** tool in a loop: it screenshots the
remote desktop (via horizon-mcp), Claude reasons over the pixels and emits an action
(click / type / key / scroll / screenshot / zoom), we execute it against the Horizon
session, screenshot again, and repeat — exactly the "agent loop" from the computer-use docs.

WHY computer use (not custom click tools): the model is purpose-trained for GUI grounding,
so it locates the right element and returns pixel-accurate coordinates. horizon-mcp's
screenshot/click/scroll all take a monitor `screen` index where coordinates are 0-based
from that monitor's top-left — so the screenshot we send Claude and the click we send back
share one coordinate frame. We downscale large screenshots to fit the API image limit and
scale Claude's coordinates back to native before clicking.

TWO MODES (reliability + the user's read-only requirement):
  - ADVISE (default): read-only. Claude may screenshot/zoom to SEE the screen, but every
    input action is refused — it must tell the user what to do instead. Nothing is touched.
  - ACT: Claude may drive the remote, but EVERY input action is gated behind an explicit
    user Confirm / Skip / Stop. A Stop aborts the loop instantly; a step budget caps runaways.

This module is UI-agnostic: it exposes callback hooks and a thread-safe confirmation
handshake. The PySide6 Assist page (or the `assist` CLI) drives it from a worker thread.

Beta: requires the `computer-use-2025-11-24` header (Opus 4.8/4.7/4.6, Sonnet 4.6, Opus 4.5)
and the `computer_20251124` tool. Model defaults to claude-opus-4-8.
"""

from __future__ import annotations

import asyncio
import base64
import io
import threading
from typing import Any, Callable

import anthropic
from PIL import Image

from .controller import RemoteController
from .mcp_client import HorizonMCPClient

# Computer-use tool wiring for the current Opus/Sonnet generation. The beta header is
# required whenever the computer tool is in `tools`.
COMPUTER_TOOL_TYPE = "computer_20251124"
COMPUTER_USE_BETA = "computer-use-2025-11-24"
DEFAULT_MODEL = "claude-opus-4-8"

# Input actions — the ones that change remote state. These are refused in Advise mode and
# gated behind Confirm/Skip/Stop in Act mode. screenshot/zoom/wait are read-only and always
# allowed.
_INPUT_ACTIONS = {
    "left_click", "right_click", "middle_click", "double_click", "triple_click",
    "left_click_drag", "left_mouse_down", "left_mouse_up", "mouse_move",
    "type", "key", "hold_key", "scroll",
}

# X11/xdotool-style keysyms (what the computer tool emits in `key`/`hold_key`) mapped to the
# names horizon-mcp's key_combo expects. Best-effort — refine against a live session.
_MODIFIERS = {
    "ctrl": "Ctrl", "control": "Ctrl",
    "alt": "Alt", "option": "Alt",
    "shift": "Shift",
    "super": "Win", "cmd": "Win", "win": "Win", "meta": "Win", "command": "Win",
}
_KEYSYMS = {
    "return": "Enter", "enter": "Enter", "kp_enter": "Enter",
    "tab": "Tab", "escape": "Esc", "esc": "Esc",
    "backspace": "Backspace", "delete": "Delete", "del": "Delete",
    "space": "Space",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "page_up": "PageUp", "prior": "PageUp", "page_down": "PageDown", "next": "PageDown",
    "home": "Home", "end": "End", "insert": "Insert",
}


def _translate_key(spec: str) -> list[str]:
    """Turn a computer-tool key spec ('ctrl+s', 'Return', 'shift+Tab') into a key_combo list."""
    keys: list[str] = []
    for part in spec.replace(" ", "").split("+"):
        if not part:
            continue
        low = part.lower()
        if low in _MODIFIERS:
            keys.append(_MODIFIERS[low])
        elif low in _KEYSYMS:
            keys.append(_KEYSYMS[low])
        elif low.startswith("f") and low[1:].isdigit():
            keys.append(low.upper())            # F1..F12
        elif len(part) == 1:
            keys.append(part.upper())           # single character key
        else:
            keys.append(part)                   # pass through; horizon-mcp may know it
    return keys


def _noop(*_a: Any, **_k: Any) -> None:
    pass


class ComputerUseAgent:
    """A screen-aware computer-use agent over the remote Horizon desktop.

    Hooks (assign callables; all default to no-ops). They fire on the worker thread, so a
    Qt UI should marshal them to the UI thread via signals:
      on_text(str)     — Claude's user-facing narration for a turn
      on_action(str)   — an input action is proposed (Act mode); show Confirm/Skip/Stop
      on_result(str)   — outcome of an executed/declined action
      on_status(str)   — coarse progress ("Looking at the screen…", "Thinking…")
      on_finished()    — the turn ended (Claude is waiting for the next user message)
      on_error(str)    — an exception aborted the turn

    Confirmation handshake: when on_action fires, the worker blocks until the UI calls
    resolve_confirmation("confirm"|"skip"|"stop"). request_stop() aborts the whole turn.
    `messages` persists across turns so the conversation (and the screen history) carries.
    """

    def __init__(
        self,
        config: dict,
        api_key: str,
        *,
        model: str | None = None,
    ) -> None:
        self._config = config
        mcp = config["mcp"]
        self._server_path = mcp["server_path"]
        self._command = mcp["command"]

        assist = config.get("assist", {})
        ctl = config.get("control", {})
        self._focus_target = assist.get("focus_target", ctl.get("focus_target", "PVDI"))
        self._screen = int(assist.get("screen", config.get("polling", {}).get("screen_index", 0)))
        self._max_steps = int(assist.get("max_steps", 20))
        self._max_long_edge = int(assist.get("max_long_edge", 1568))
        self._effort = assist.get("effort", "high")
        self._user_name = config.get("user", {}).get("display_name", "the user")
        self._model = model or assist.get("model") or DEFAULT_MODEL

        self._anthropic = anthropic.Anthropic(api_key=api_key)

        # Conversation + per-run scaling state.
        self.messages: list[dict] = []
        self._scale = 1.0          # display-px / native-px (<=1.0)
        self._disp_w = 0
        self._disp_h = 0

        # Thread-safe control surface.
        self._stop = threading.Event()
        self._confirm_event = threading.Event()
        self._confirm_decision: str | None = None

        # Hooks.
        self.on_text: Callable[[str], None] = _noop
        self.on_action: Callable[[str], None] = _noop
        self.on_result: Callable[[str], None] = _noop
        self.on_status: Callable[[str], None] = _noop
        self.on_finished: Callable[[], None] = _noop
        self.on_error: Callable[[str], None] = _noop

    # ------------------------------------------------------------- public API

    def run_turn(self, user_text: str, mode: str = "advise") -> None:
        """Run one user turn to completion (blocking — call from a worker thread)."""
        self._stop.clear()
        try:
            asyncio.run(self._session(user_text, mode))
        except Exception as exc:  # surface, don't crash the UI thread
            self.on_error(f"{type(exc).__name__}: {exc}")
        finally:
            self.on_finished()

    def request_stop(self) -> None:
        """Abort the current turn at the next safe point; unblocks a pending confirmation."""
        self._stop.set()
        self._confirm_decision = "stop"
        self._confirm_event.set()

    def resolve_confirmation(self, decision: str) -> None:
        """UI thread → answer a pending action proposal ('confirm' | 'skip' | 'stop')."""
        self._confirm_decision = decision
        if decision == "stop":
            self._stop.set()
        self._confirm_event.set()

    def reset(self) -> None:
        """Forget the conversation (start a fresh session)."""
        self.messages = []

    # --------------------------------------------------------------- the loop

    async def _session(self, user_text: str, mode: str) -> None:
        steps = 0
        async with HorizonMCPClient(self._server_path, self._command) as client:
            self._client = client
            self._controller = RemoteController(client, focus_target=self._focus_target)

            self.on_status("Looking at the screen…")
            b64 = await self._grab_display(first=True)
            self.messages.append(
                {
                    "role": "user",
                    # Text BEFORE the image — the docs note this improves click accuracy.
                    "content": [
                        {"type": "text", "text": user_text},
                        self._image_block(b64),
                    ],
                }
            )

            tools = [
                {
                    "type": COMPUTER_TOOL_TYPE,
                    "name": "computer",
                    "display_width_px": self._disp_w,
                    "display_height_px": self._disp_h,
                    "display_number": 1,
                    "enable_zoom": True,
                }
            ]

            while not self._stop.is_set():
                self.on_status("Thinking…")
                resp = await asyncio.to_thread(self._create, tools, mode)
                self.messages.append({"role": "assistant", "content": resp.content})

                for block in resp.content:
                    if block.type == "text" and block.text.strip():
                        self.on_text(block.text.strip())

                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not tool_uses:
                    break  # Claude finished or asked a question — wait for the next turn.

                results = []
                for tu in tool_uses:
                    if self._stop.is_set():
                        results.append(self._result(tu.id, "Stopped by the user.", False))
                        continue
                    content, is_err = await self._handle_action(dict(tu.input), mode)
                    results.append(self._result(tu.id, content, is_err))
                self.messages.append({"role": "user", "content": results})

                if self._stop.is_set():
                    self.on_text("Stopped.")
                    break

                steps += 1
                if steps >= self._max_steps:
                    self.on_text(
                        f"(Reached the {self._max_steps}-action limit for one turn — pausing. "
                        "Send another message if you'd like me to keep going.)"
                    )
                    break

    def _create(self, tools: list[dict], mode: str):
        """One Messages API call (sync; runs in a worker via asyncio.to_thread)."""
        return self._anthropic.beta.messages.create(
            model=self._model,
            max_tokens=4096,
            system=self._system_prompt(mode),
            tools=tools,
            messages=self.messages,
            betas=[COMPUTER_USE_BETA],
            output_config={"effort": self._effort},
            thinking={"type": "adaptive"},
        )

    # ----------------------------------------------------------- action execution

    async def _handle_action(self, params: dict, mode: str) -> tuple[Any, bool]:
        """Execute one computer-tool action. Returns (tool_result content, is_error)."""
        action = params.get("action", "")

        # Read-only actions: always allowed, never need confirmation.
        if action == "screenshot":
            self.on_status("Looking at the screen…")
            return [self._image_block(await self._grab_display())], False
        if action == "zoom":
            return [self._image_block(await self._grab_zoom(params.get("region")))], False
        if action == "wait":
            await asyncio.sleep(min(float(params.get("duration", 1) or 1), 5.0))
            return "Waited.", False

        # Everything below changes remote state.
        if action not in _INPUT_ACTIONS:
            return f"Unsupported action: {action}", True

        if mode != "act":
            # Advise (read-only): refuse and tell Claude to instruct the user instead.
            return (
                "Read-only (Advise) mode — input actions are disabled. Do NOT attempt to "
                "click, type, scroll, or press keys. Instead, tell the user exactly what to "
                "do (which element to click, what to type) in plain language, then finish.",
                True,
            )

        desc = self._describe(params)
        decision = await asyncio.to_thread(self._request_confirmation, desc)
        if decision == "stop":
            self._stop.set()
            self.on_result("Stopped.")
            return "Stopped by the user.", False
        if decision == "skip":
            self.on_result(f"Skipped: {desc}")
            return (
                "The user declined this action (skipped). Do not retry it as-is — suggest a "
                "different approach or ask the user how they'd like to proceed.",
                False,
            )

        # Confirmed — perform it.
        try:
            outcome = await self._perform(params)
        except Exception as exc:
            self.on_result(f"Failed: {desc} ({exc})")
            return f"Action failed: {exc}", True
        self.on_result(f"Done: {desc}")
        return outcome, False

    async def _perform(self, params: dict) -> str:
        """Drive the remote for a confirmed input action."""
        action = params["action"]
        await self._controller.ensure_foreground()
        c = self._client

        if action in ("left_click", "right_click", "middle_click",
                      "double_click", "triple_click", "left_click_drag"):
            x, y = self._to_native(params["coordinate"])
            # NOTE: a modifier in params["text"] (shift/ctrl+click) is not honored —
            # horizon-mcp can't hold a key *during* a click, so we perform a plain click
            # rather than send a stray keystroke. Revisit if modified clicks are needed.
            if action == "right_click":
                await c.click(x, y, button="right", screen=self._screen)
            elif action == "middle_click":
                await c.click(x, y, button="middle", screen=self._screen)
            elif action == "double_click":
                await c.double_click(x, y, screen=self._screen)
            elif action == "triple_click":
                for _ in range(3):
                    await c.click(x, y, screen=self._screen)
            else:  # left_click / left_click_drag (drag falls back to a click)
                await c.click(x, y, screen=self._screen)
            return f"Clicked at ({x}, {y})."

        if action == "mouse_move":
            x, y = self._to_native(params["coordinate"])
            await c.move_mouse(x, y)
            return f"Moved cursor to ({x}, {y})."

        if action == "type":
            await c.type_text(params.get("text", ""))
            return "Typed the text."

        if action in ("key", "hold_key"):
            keys = _translate_key(params.get("text", ""))
            if not keys:
                return "No key to press."
            hold = int(float(params.get("duration", 0) or 0) * 1000) or None
            await c.key_combo(keys, hold_ms=hold)
            return f"Pressed {'+'.join(keys)}."

        if action == "scroll":
            x, y = self._to_native(params["coordinate"])
            direction = params.get("scroll_direction", "down")
            amount = int(params.get("scroll_amount", 3) or 3)
            await c.scroll(x, y, direction, amount, screen=self._screen)
            return f"Scrolled {direction} {amount} at ({x}, {y})."

        return f"Unsupported action: {action}"

    def _modifier_combo(self, text: str | None) -> list[str]:
        """Map a click/scroll `text` modifier ('shift','ctrl','alt','super') to key_combo names."""
        if not text:
            return []
        return [_MODIFIERS[p.lower()] for p in text.split("+") if p.lower() in _MODIFIERS]

    # ----------------------------------------------------------- screenshots

    async def _grab_display(self, first: bool = False) -> str:
        """Screenshot the VDI monitor, downscale to the image limit, return base64 PNG.

        On the first grab of a turn this also fixes the display dimensions and scale factor
        used for the tool definition and all coordinate mapping for the turn.
        """
        png = await self._client.screenshot(screen=self._screen)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        nat_w, nat_h = img.size
        if first:
            self._scale = min(1.0, self._max_long_edge / max(nat_w, nat_h))
            self._disp_w = max(1, round(nat_w * self._scale))
            self._disp_h = max(1, round(nat_h * self._scale))
        disp = img.resize((self._disp_w, self._disp_h)) if (self._disp_w, self._disp_h) != (nat_w, nat_h) else img
        return self._encode(disp)

    async def _grab_zoom(self, region: Any) -> str:
        """Crop a region (in display coords) from a fresh native screenshot, at full detail."""
        png = await self._client.screenshot(screen=self._screen)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        if region and len(region) == 4:
            x1, y1 = self._to_native([region[0], region[1]])
            x2, y2 = self._to_native([region[2], region[3]])
            x1, x2 = sorted((max(0, x1), min(img.width, x2)))
            y1, y2 = sorted((max(0, y1), min(img.height, y2)))
            if x2 > x1 and y2 > y1:
                img = img.crop((x1, y1, x2, y2))
        # Keep the crop within the image limit too.
        long_edge = max(img.size)
        if long_edge > self._max_long_edge:
            s = self._max_long_edge / long_edge
            img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))))
        return self._encode(img)

    @staticmethod
    def _encode(img: Image.Image) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _to_native(self, coord: Any) -> tuple[int, int]:
        """Map display-space coordinates from Claude back to native screen pixels."""
        x, y = coord[0], coord[1]
        s = self._scale or 1.0
        return round(x / s), round(y / s)

    @staticmethod
    def _image_block(b64: str) -> dict:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        }

    @staticmethod
    def _result(tool_use_id: str, content: Any, is_error: bool) -> dict:
        block: dict = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error:
            block["is_error"] = True
        return block

    # ----------------------------------------------------------- confirmation

    def _request_confirmation(self, desc: str) -> str:
        """Block the worker until the UI resolves the proposal (runs via asyncio.to_thread)."""
        if self._stop.is_set():
            return "stop"
        self._confirm_decision = None
        self._confirm_event.clear()
        self.on_action(desc)
        self._confirm_event.wait()
        return self._confirm_decision or "stop"

    # ----------------------------------------------------------- descriptions

    def _describe(self, params: dict) -> str:
        """Human-readable one-liner for an action proposal (shown with Confirm/Skip/Stop)."""
        action = params.get("action", "")
        if action in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            x, y = self._to_native(params.get("coordinate", [0, 0]))
            verb = action.replace("_", " ")
            mod = params.get("text")
            return f"{verb} at ({x}, {y})" + (f" with {mod}" if mod else "")
        if action == "type":
            text = params.get("text", "")
            short = text if len(text) <= 60 else text[:57] + "…"
            return f"type: {short!r}"
        if action in ("key", "hold_key"):
            return f"press key: {params.get('text', '')}"
        if action == "scroll":
            x, y = self._to_native(params.get("coordinate", [0, 0]))
            return f"scroll {params.get('scroll_direction', 'down')} " \
                   f"{params.get('scroll_amount', 3)} at ({x}, {y})"
        if action == "mouse_move":
            x, y = self._to_native(params.get("coordinate", [0, 0]))
            return f"move cursor to ({x}, {y})"
        return action

    def _system_prompt(self, mode: str) -> str:
        acting = mode == "act"
        mode_block = (
            "MODE: ACT (hands-on). You may perform actions, but every input action "
            "(click, type, key, scroll) is shown to the user for Confirm / Skip / Stop "
            "before it runs. Take ONE action at a time, then screenshot and verify the "
            "result before the next. Keep steps minimal and reversible."
            if acting else
            "MODE: ADVISE (read-only). You may take screenshots and zoom to SEE the screen, "
            "but you must NOT perform any input action — they are disabled and will be "
            "refused. Your job is to tell the user, step by step, exactly what to click and "
            "type so they can do it themselves. Be concrete (name the on-screen element and "
            "its location). When you've given the full instructions, finish your turn."
        )
        return (
            "You are a hands-on assistant operating a remote Windows desktop inside an "
            "Omnissa Horizon VDI, by looking at screenshots and controlling the mouse and "
            "keyboard with the computer tool.\n\n"
            "The applications in this desktop (Git Bash / MINGW64, VS Code, File Explorer, "
            "Microsoft Teams, Symphony, etc.) are pixels inside the VDI — not separate OS "
            "windows. Locate the right window visually and click it to focus before typing "
            "into it.\n\n"
            f"{mode_block}\n\n"
            "SAFETY — this is a real corporate machine:\n"
            "- For anything destructive or hard to undo (deleting or discarding changes, "
            "force/overwrite operations, closing without saving, sending a message, "
            "git reset/checkout that drops work), STOP and ask the user first. Never assume.\n"
            "- Honor the user's explicit constraints exactly (e.g. 'do not stash').\n"
            "- Never type passwords or credentials.\n"
            "- If the screen is unexpected or you're unsure, ask the user rather than guessing.\n\n"
            f"The user's name is {self._user_name}. After each action, take a screenshot and "
            "briefly state what you see before continuing. When the task is complete, or you "
            "need information only the user can provide, end your turn with a clear message."
        )
