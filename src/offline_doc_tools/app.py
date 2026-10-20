"""Application entry point."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import __version__
from .main_window import MainWindow
from .updater.helper import write_startup_marker
from .updater.runtime import build_runtime

log = logging.getLogger(__name__)


def log_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "OfflineDocTools" / "logs"
    return Path.home() / ".local" / "state" / "offline-doc-tools" / "logs"


def configure_logging() -> None:
    """Log to a small rotating local file. Never logs document contents."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        directory = log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            directory / "app.log", maxBytes=512_000, backupCount=1, encoding="utf-8"
        )
    except OSError:
        # A read-only profile must not stop the app; fall back to stderr if there is one.
        handler = logging.StreamHandler() if sys.stderr else logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)

    def _log_unhandled(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _log_unhandled


def safe_runtime():
    """The updater, or None if setting it up fails: converting must never depend on it."""
    try:
        return build_runtime()
    except Exception as exc:
        log.error("Updates are unavailable: %s", type(exc).__name__)
        return None


def main() -> int:
    if sys.argv[1:2] == ["--apply-update"]:
        # The replacement helper: this executable, run from the staged update (see updater/).
        from .updater import helper

        return helper.main(sys.argv[2:])
    configure_logging()
    log.info("Starting version %s", __version__)
    app = QApplication(sys.argv)
    app.setApplicationName("Offline Doc Tools")
    window = MainWindow(updates=safe_runtime())
    window.show()
    # Tells an update helper that this version came up (only when it asked for it).
    QTimer.singleShot(0, lambda: write_startup_marker(os.environ, __version__))
    return app.exec()
