"""Debug mode: a detailed log file with everything the editor does (Qt-free at import time).

enable() writes ~/.dayz_terrain_editor/logs/debug_<date>_<time>.log, disable() stops it.
Logger names are "terrain_editor.<area>"; Qt messages go to "terrain_editor.qt" and
unhandled exceptions to "terrain_editor.errors" while the log is on.
"""
from __future__ import annotations

import logging
import platform
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".dayz_terrain_editor" / "logs"
ROOT = "terrain_editor"
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"

_handler: logging.Handler | None = None
_path: Path | None = None
_qt_handler_ref = None           # keeps the Qt message handler alive
_orig_excepthook = None


def get_logger(area: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{area}")


def is_enabled() -> bool:
    return _handler is not None


def current_log_path() -> Path | None:
    return _path


def _install_qt(on: bool) -> None:
    global _qt_handler_ref
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return
    if not on:
        qInstallMessageHandler(None)
        _qt_handler_ref = None
        return
    qt_log = get_logger("qt")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message):
        qt_log.log(levels.get(mode, logging.WARNING), "%s", message)

    _qt_handler_ref = handler
    qInstallMessageHandler(handler)


def enable(log_dir: Path | str = LOG_DIR) -> Path:
    """Start the log file (idempotent). Returns the log path."""
    global _handler, _path
    if _handler is not None:
        return _path
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"debug_{datetime.now():%Y%m%d_%H%M%S}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    _handler, _path = handler, path
    _install_qt(True)
    log = get_logger("debug")
    log.info("debug mode ON, log file: %s", path)
    log.info("python %s, platform %s", sys.version.split()[0], platform.platform())
    return path


def disable() -> None:
    global _handler, _path
    if _handler is None:
        return
    get_logger("debug").info("debug mode OFF")
    _install_qt(False)
    root = logging.getLogger(ROOT)
    root.removeHandler(_handler)
    _handler.close()
    root.setLevel(logging.WARNING)
    _handler, _path = None, None


def install_excepthook() -> None:
    """Log unhandled exceptions with their traceback (only while the log is on)."""
    global _orig_excepthook
    if _orig_excepthook is not None:
        return
    _orig_excepthook = sys.excepthook

    def hook(exc_type, exc, tb):
        if is_enabled():
            get_logger("errors").error("unhandled exception", exc_info=(exc_type, exc, tb))
        _orig_excepthook(exc_type, exc, tb)

    sys.excepthook = hook
