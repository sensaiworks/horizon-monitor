"""
Windows desktop notification service.

Fires a toast notification when a MessageEvent has directed_at_user=True.
Deduplicates using a short-term cache keyed on (speaker, message_prefix)
with a configurable cooldown window.

Uses plyer.notification.notify() — works on Windows, macOS, Linux.

TODO (Step 4): implement `notify_if_needed()`.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from datetime import datetime, timedelta

from .models import MessageEvent


class Notifier:
    def __init__(self, enabled: bool = True, cooldown_minutes: int = 5) -> None:
        self._enabled = enabled
        self._cooldown = timedelta(minutes=cooldown_minutes)
        # {dedup_key: last_notified_datetime}
        self._seen: OrderedDict[str, datetime] = OrderedDict()

    def _dedup_key(self, event: MessageEvent) -> str:
        raw = f"{event.speaker}:{event.message[:60]}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _is_duplicate(self, key: str) -> bool:
        if key not in self._seen:
            return False
        return datetime.utcnow() - self._seen[key] < self._cooldown

    def notify_if_needed(self, event: MessageEvent) -> bool:
        """
        Show a toast notification if event is directed at user and not a duplicate.
        Returns True if a notification was shown.

        TODO (Step 4):
          - Check self._enabled and event.directed_at_user
          - Compute dedup key, check _is_duplicate()
          - Call plyer.notification.notify(title=..., message=..., app_name="horizon-monitor", timeout=8)
          - Record key in self._seen
          - Prune old entries from self._seen (keep last 200)
        """
        raise NotImplementedError
