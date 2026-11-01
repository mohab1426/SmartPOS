"""Runs the real-window cashier layout regression test (scripts/layout_regression_test.py).

Skipped automatically when PySide6 is not installed.  Uses the offscreen Qt platform, an isolated
temporary data directory and never touches a real database.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]


def test_cashier_layout_regression(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "layout_regression_test.py"), "--out", str(tmp_path)],
        cwd=ROOT, env=env, capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-3000:]
