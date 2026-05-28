from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MessageEvent(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    speaker: str
    message: str
    app: Literal["teams", "symphony", "unknown"] = "unknown"
    window_title: str = ""
    directed_at_user: bool = False

    def doc_id(self) -> str:
        """Stable ID for deduplication in ChromaDB."""
        content = f"{self.speaker}:{self.message[:80]}"
        return str(hash(content) & 0xFFFFFFFF)


class ScreenState(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    screenshot_hash: str
    window_title: str
    changed: bool
    extracted_events: list[MessageEvent] = Field(default_factory=list)


class ProcessInfo(BaseModel):
    pid: int
    name: str
    title: str

    @classmethod
    def from_mcp(cls, d: dict) -> "ProcessInfo":
        return cls(pid=d["Id"], name=d["Name"], title=d["MainWindowTitle"])
