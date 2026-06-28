"""QApplication + system tray bootstrap.

`run(config, api_key)` shows the window and a tray icon. The tray is the app's true home:
left-click toggles the window, the menu mirrors Start/Pause/Stop + Show/Quit. Closing the
window hides to tray; only the tray's Quit (or quit_requested) exits the process.
"""

from __future__ import annotations

import sys

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .main_window import MainWindow
from .theme import apply_theme, make_status_icon


def _build_tray(app: QApplication, win: MainWindow) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(make_status_icon("stopped"), parent=app)
    tray.setToolTip("horizon-monitor")

    menu = QMenu()

    def toggle_window() -> None:
        if win.isVisible() and not win.isMinimized():
            win.hide()
        else:
            win.showNormal()
            win.raise_()
            win.activateWindow()

    act_show = QAction("Show / hide window", menu)
    act_show.triggered.connect(toggle_window)
    act_start = QAction("Start", menu)
    act_start.triggered.connect(win._on_start)
    act_pause = QAction("Pause", menu)
    act_pause.triggered.connect(win._on_pause)
    act_stop = QAction("Stop", menu)
    act_stop.triggered.connect(win._on_stop)
    act_quit = QAction("Quit", menu)

    def really_quit() -> None:
        tray.hide()
        app.quit()

    act_quit.triggered.connect(really_quit)
    win.quit_requested.connect(really_quit)

    menu.addAction(act_show)
    menu.addSeparator()
    menu.addAction(act_start)
    menu.addAction(act_pause)
    menu.addAction(act_stop)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)

    def on_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.Trigger:  # left-click
            toggle_window()

    tray.activated.connect(on_activated)

    # Keep the tray icon coloured by engine state.
    def refresh_icon() -> None:
        tray.setIcon(make_status_icon(win.set_engine_icon_status()))
    mp = win._monitor_page
    for btn in (mp.btn_start, mp.btn_pause, mp.btn_stop):
        btn.clicked.connect(refresh_icon)

    tray.show()
    return tray


def run(config: dict, api_key: str) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("horizon-monitor")
    app.setQuitOnLastWindowClosed(False)  # closing the window must not kill the tray
    apply_theme(app)

    win = MainWindow(config, api_key)
    win._tray = _build_tray(app, win)  # keep a reference so it isn't GC'd
    win.show()
    return app.exec()
