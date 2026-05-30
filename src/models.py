from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MessageEvent(BaseModel):
    timestamp: datetime = Field(default_factory=_utcnow)
    speaker: str
    message: str
    app: Literal["teams", "symphony", "unknown"] = "unknown"
    window_title: str = ""
    directed_at_user: bool = False

    def doc_id(self) -> str:
        """Stable, process-independent ID for deduplication in ChromaDB.

        Must NOT use the builtin hash(): str hashing is salted per process
        (PYTHONHASHSEED), so the same message would get a different id on every
        restart and dedup would never fire across runs.
        """
        content = f"{self.speaker}:{self.message[:80]}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


class ScreenState(BaseModel):
    timestamp: datetime = Field(default_factory=_utcnow)
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
