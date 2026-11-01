"""Startup logging and crash reporting for the packaged (windowed) application.

A windowed PyInstaller EXE has no console, so an unhandled exception during start-up used to end the
process with no dialog and no log line.  This module (a) configures a rotating log file before anything
else runs, (b) installs ``sys.excepthook`` and (c) shows a plain-language dialog that names the log file.
It has no hard dependency on Qt so it can be unit-tested without a display.
"""
from __future__ import annotations

import logging
import sys
import tempfile
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "smartpos.log"
MAX_LOG_BYTES = 1_000_000
LOG_BACKUPS = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_state: dict = {"log_path": None}


def configure_logging(log_dir: str | Path) -> logging.Logger:
    """Attach a rotating file handler (1 MB x 5) to the root logger; fall back to the temp dir if the
    data folder is not writable.  Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for candidate in (Path(log_dir), Path(tempfile.gettempdir()) / "SmartPOS_logs"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            path = candidate / LOG_FILE_NAME
            if any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == str(path.resolve())
                   for h in root.handlers):
                _state["log_path"] = path
                break
            handler = RotatingFileHandler(path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
            handler.setFormatter(logging.Formatter(_FORMAT))
            root.addHandler(handler)
            _state["log_path"] = path
            break
        except OSError:
            continue
    return logging.getLogger("smartpos")


def current_log_path() -> Path | None:
    return _state["log_path"]


def _show_dialog(title: str, text: str) -> None:
    """Best effort: show a Qt message box if a QApplication exists; never raise."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            QMessageBox.critical(None, title, text)
    except Exception:  # noqa: BLE001 - the crash reporter itself must never crash
        pass


def _message(error: BaseException) -> str:
    log = current_log_path()
    where = f"\n\nتفاصيل الخطأ محفوظة في الملف:\n{log}" if log else ""
    return f"تعذر تشغيل البرنامج أو حدث خطأ غير متوقع.\n\n{error}{where}"


def report_startup_failure(logger: logging.Logger, error: BaseException) -> None:
    logger.error("Startup failed: %s", error, exc_info=(type(error), error, error.__traceback__))
    _show_dialog("SmartPOS — فشل التشغيل", _message(error))


def install_excepthook(logger: logging.Logger) -> None:
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Unhandled exception\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        _show_dialog("SmartPOS — خطأ غير متوقع", _message(exc))
    sys.excepthook = hook
