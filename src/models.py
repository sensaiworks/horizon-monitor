from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MessageEvent(BaseModel):
    timestamp: datetime = Field(default_factory=_utcnow)  # when we captured it
    speaker: str
    message: str
    app: Literal["teams", "symphony", "outlook", "unknown"] = "unknown"
    window_title: str = ""
    directed_at_user: bool = False
    # The chat's own timestamp as rendered on screen (e.g. "10:32 AM",
    # "Yesterday 14:05"). Free-form because each app formats it differently;
    # may be "" when not visible.
    chat_time: str = ""
    # The conversation/channel/room/thread the message belongs to, as shown in
    # the UI (e.g. "Deployments", "John Smith"). "" when not identifiable.
    channel: str = ""

    def doc_id(self) -> str:
        """Stable, process-independent ID for deduplication.

        Must NOT use the builtin hash(): str hashing is salted per process
        (PYTHONHASHSEED), so the same message would get a different id on every
        restart and dedup would never fire across runs.

        Keying on channel + speaker + on-screen time + message means the *same*
        message re-read across overlapping frames collapses to one row, while two
        genuinely separate "ok"s from the same person (different time/channel)
        stay distinct. chat_time/channel fall back to "" gracefully.
        """
        content = f"{self.channel}|{self.speaker}|{self.chat_time}|{self.message[:120]}"
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
