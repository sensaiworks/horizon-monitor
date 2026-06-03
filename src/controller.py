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

from .mcp_client import HorizonMCPClient


class RemoteController:
    def __init__(
        self,
        client: HorizonMCPClient,
        focus_target: str = "PVDI",
        launch_wait: float = 1.5,
    ) -> None:
        self._client = client
        self._focus_target = focus_target
        self._launch_wait = launch_wait

    async def ensure_foreground(self) -> None:
        """Bring the local Horizon client to the foreground so input reaches the remote."""
        await self._client.focus_window(self._focus_target)
        await asyncio.sleep(0.4)

    # alias used by the tray / CLI when the user just wants the window up front
    async def bring_to_front(self) -> None:
        await self.ensure_foreground()

    async def unlock(self, password: str) -> None:
        """Send Ctrl+Alt+Del to the remote, then enter the password.

        Uses paste_text (more reliable than typing in a remote password field) and
        restores the prior clipboard afterward so the secret does not linger there.
        """
        await self.ensure_foreground()
        await self._client.key_combo(["Ctrl", "Alt", "Insert"])  # Horizon's Ctrl+Alt+Del
        await asyncio.sleep(2.5)                                 # wait for the login prompt
        if password:
            prior = ""
            try:
                prior = await self._client.get_clipboard()
            except Exception:
                pass
            try:
                await self._client.paste_text(password)
                await asyncio.sleep(0.3)
                await self._client.key_combo(["Enter"])
            finally:
                # Clear/restore the clipboard so the password does not persist.
                try:
                    await self._client.set_clipboard(prior or "")
                except Exception:
                    pass

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
