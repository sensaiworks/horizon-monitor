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
        """Show a toast if event is directed at user and not a duplicate. Returns True if shown."""
        if not self._enabled or not event.directed_at_user:
            return False
        key = self._dedup_key(event)
        if self._is_duplicate(key):
            return False
        try:
            from plyer import notification
            notification.notify(
                title=f"horizon-monitor — {event.speaker}",
                message=event.message[:200],
                app_name="horizon-monitor",
                timeout=8,
            )
        except Exception:
            pass
        self._seen[key] = datetime.utcnow()
        while len(self._seen) > 200:
            self._seen.popitem(last=False)
        return True
