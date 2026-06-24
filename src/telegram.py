"""Presence-only Telegram alerts.

When someone talks to the user while they've stepped away, ping their phone so they
don't look unresponsive — WITHOUT leaking anything sensitive. The message is ALWAYS
just "🔔 <Name> mentioned you [in <channel>]". The chat message body, never.

Transport is the Telegram Bot API over HTTPS (stdlib urllib — no extra dependency).
Credentials come from .env:
  TELEGRAM_BOT_TOKEN  — from @BotFather
  TELEGRAM_CHAT_ID    — your chat id (message the bot, then read it from
                        https://api.telegram.org/bot<token>/getUpdates)

Sends are fire-and-forget on a background thread so a slow/no network never stalls
the capture loop. `send_test()` is synchronous for the Settings "Send test" button.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request


class TelegramNotifier:
    _API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0) -> None:
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._timeout = timeout
        # Replaced by the caller to surface async send failures (e.g. into the UI log).
        self.on_error = lambda msg: None

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    @staticmethod
    def presence_text(speaker: str, channel: str = "") -> str:
        where = f" in {channel}" if channel else ""
        return f"🔔 {speaker} mentioned you{where}"

    def notify_presence(self, speaker: str, channel: str = "") -> None:
        """Fire-and-forget a presence ping; no-op if unconfigured."""
        if not self.configured:
            return
        text = self.presence_text(speaker, channel)

        def worker() -> None:
            try:
                self._send(text)
            except Exception as exc:  # noqa: BLE001 — report, never raise into the loop
                self.on_error(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def send_test(self) -> tuple[bool, str]:
        """Synchronous test send. Returns (ok, human-readable detail)."""
        if not self.configured:
            return False, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first."
        try:
            self._send("🔔 horizon-monitor: presence alerts are connected.")
            return True, "Sent — check your Telegram."
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _send(self, text: str) -> None:
        """POST one message to the Bot API; raise on transport or API error."""
        url = self._API.format(token=self._token)
        data = urllib.parse.urlencode({"chat_id": self._chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok", False):
            raise RuntimeError(body.get("description", "Telegram API returned ok=false"))
