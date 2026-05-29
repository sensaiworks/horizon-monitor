"""
Claude Vision extraction — screenshot → (list[MessageEvent], is_lock_screen).

Sends the PNG screenshot to Claude Haiku with a structured prompt.
Returns parsed MessageEvent objects and a bool indicating if the remote
desktop is showing a lock screen.

Model: claude-haiku-4-5-20251001 (fast, cheap, sufficient for UI parsing)
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime

import anthropic

from .models import MessageEvent

_MAX_TOKENS = 1024

EXTRACTION_PROMPT = """\
You are analyzing a screenshot of a Windows remote desktop.

Return a JSON object ONLY, no explanation, no markdown:
{{
  "lock_screen": true | false,
  "messages": [
    {{
      "speaker": "Full Name or username",
      "message": "exact message text",
      "app": "teams" | "symphony" | "unknown",
      "directed_at_user": true | false
    }}
  ]
}}

lock_screen is true if the screen shows a Windows lock screen (clock visible,
"Press Ctrl+Alt+Delete to unlock", dark/black screen with no chat content).

If lock_screen is true, messages must be [].

messages contains all visible chat messages from Microsoft Teams or Symphony.
directed_at_user is true if the message @mentions "{user}", uses their first name
"{user}" directly, or is a direct/private message thread to them.

If no chat app is visible or no messages are present, return messages: [].
"""


class Extractor:
    def __init__(self, api_key: str, model: str, user_display_name: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._user = user_display_name

    async def extract(self, png_bytes: bytes, window_title: str = "") -> tuple[list[MessageEvent], bool]:
        """
        Send screenshot to Claude Vision.
        Returns (events, is_lock_screen).
        """
        b64 = base64.b64encode(png_bytes).decode()
        prompt = self._build_prompt()

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        raw = response.content[0].text
        items, is_lock_screen = self._parse_response(raw)
        now = datetime.utcnow()
        events = []
        for item in items:
            try:
                events.append(
                    MessageEvent(
                        timestamp=now,
                        speaker=item.get("speaker", "unknown"),
                        message=item.get("message", ""),
                        app=item.get("app", "unknown"),
                        directed_at_user=bool(item.get("directed_at_user", False)),
                        window_title=window_title,
                    )
                )
            except Exception:
                pass
        return events, is_lock_screen

    def _parse_response(self, text: str) -> tuple[list[dict], bool]:
        """Parse model response into (messages, is_lock_screen)."""
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data.get("messages", []), bool(data.get("lock_screen", False))
            if isinstance(data, list):
                # backward-compat with old prompt format
                return data, False
        except json.JSONDecodeError:
            pass
        return [], False

    def _build_prompt(self) -> str:
        return EXTRACTION_PROMPT.format(user=self._user)
