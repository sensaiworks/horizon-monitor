"""PySide6 desktop UI for horizon-monitor.

A single tray icon + tabbed window (left nav rail). The window is the home for every
feature; closing it hides to tray rather than quitting. Launched via `python main.py app`.

Public entry point: ``from src.ui import run`` then ``run(config, api_key)``.
"""

from __future__ import annotations

from .app import run

__all__ = ["run"]
