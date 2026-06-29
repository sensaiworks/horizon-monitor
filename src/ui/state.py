"""Persist small UI preferences across restarts (watch terms, channel picks, toggles).

Stored as JSON next to the SQLite store (data/ui_state.json) so it travels with the rest
of the on-disk state and stays out of git. Best-effort: a missing/unreadable file yields
defaults and a write failure is swallowed — UI preferences are never worth crashing over.
"""

from __future__ import annotations

import json
from pathlib import Path


def _path(config: dict) -> Path:
    events_db = config.get("rag", {}).get("events_db", "./data/events.db")
    return Path(events_db).parent / "ui_state.json"


def load(config: dict) -> dict:
    try:
        return json.loads(_path(config).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt file → defaults
        return {}


def save(config: dict, data: dict) -> None:
    try:
        p = _path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — never fatal
        pass
