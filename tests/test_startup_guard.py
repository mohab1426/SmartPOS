"""Startup logging / crash reporting (audit F-02, F-07). No display needed."""
from __future__ import annotations

import logging
import sys

import startup_guard


def _reset():
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, startup_guard.RotatingFileHandler):
            root.removeHandler(handler)
            handler.close()


def test_logging_is_rotating_and_writes_to_the_data_folder(tmp_path):
    _reset()
    logger = startup_guard.configure_logging(tmp_path / "logs")
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, startup_guard.RotatingFileHandler)]
    assert handlers and handlers[0].maxBytes == startup_guard.MAX_LOG_BYTES and handlers[0].backupCount == 5
    startup_guard.configure_logging(tmp_path / "logs")                    # idempotent: no duplicate handler
    assert len([h for h in logging.getLogger().handlers
                if isinstance(h, startup_guard.RotatingFileHandler)]) == 1
    logger.info("hello")
    handlers[0].flush()
    assert "hello" in (tmp_path / "logs" / "smartpos.log").read_text(encoding="utf-8")
    _reset()


def test_unwritable_log_dir_falls_back_to_temp(tmp_path):
    _reset()
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    startup_guard.configure_logging(blocker / "logs")                     # parent is a file -> mkdir fails
    log_path = startup_guard.current_log_path()
    assert log_path is not None and "SmartPOS_logs" in str(log_path)
    _reset()


def test_startup_failure_is_logged_with_traceback(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(startup_guard, "_show_dialog", lambda title, text: shown.append(text))  # never block on a real modal box
    _reset()
    logger = startup_guard.configure_logging(tmp_path / "logs")
    try:
        raise RuntimeError("database disk image is malformed")
    except RuntimeError as exc:
        startup_guard.report_startup_failure(logger, exc)
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / "logs" / "smartpos.log").read_text(encoding="utf-8")
    assert "Startup failed" in text and "malformed" in text and "Traceback" in text
    assert len(shown) == 1 and "malformed" in shown[0]       # the user is told, too
    _reset()


def test_excepthook_logs_unhandled_exceptions(tmp_path, monkeypatch):
    monkeypatch.setattr(startup_guard, "_show_dialog", lambda title, text: None)
    _reset()
    original = sys.excepthook
    try:
        logger = startup_guard.configure_logging(tmp_path / "logs")
        startup_guard.install_excepthook(logger)
        try:
            raise ValueError("boom-123")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "boom-123" in (tmp_path / "logs" / "smartpos.log").read_text(encoding="utf-8")
    finally:
        sys.excepthook = original
        _reset()
