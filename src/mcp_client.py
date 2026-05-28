"""
Async wrapper around the horizon-mcp MCP server.

Spawns `node C:\github\horizon-mcp\dist\index.js` as a subprocess and
communicates via the MCP stdio protocol using the `mcp` Python SDK.

Usage:
    async with HorizonMCPClient(config) as client:
        b64_png = await client.screenshot(screen=0)
        windows = await client.list_windows()
        await client.focus_window("PVDI")

See CLAUDE.md §"How horizon-mcp works" for the full tool reference.

TODO (Step 1): implement all methods below.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import ProcessInfo


class HorizonMCPClient:
    """Manages a long-lived connection to the horizon-mcp stdio server."""

    def __init__(self, server_path: str, command: str = "node") -> None:
        self._server_path = server_path
        self._command = command
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

    async def _call(self, tool: str, **kwargs: Any) -> Any:
        assert self._session, "Client not connected — use async with"
        result = await self._session.call_tool(tool, arguments=kwargs)
        return result

    async def screenshot(self, screen: int = 0) -> bytes:
        """Return raw PNG bytes for the given monitor index."""
        # TODO: call "screenshot" tool, extract base64 from result.content[0].data
        raise NotImplementedError

    async def list_windows(self) -> list[ProcessInfo]:
        """Return all visible windows."""
        # TODO: call "list_windows", parse JSON, return list[ProcessInfo]
        raise NotImplementedError

    async def focus_window(self, target: str) -> str:
        """Bring window to foreground. Returns status string from server."""
        # TODO: call "focus_window" with target=target
        raise NotImplementedError
