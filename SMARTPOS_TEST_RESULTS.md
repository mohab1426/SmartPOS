# SmartPOS Test Results (executed in this sandbox, Linux, Python 3.12.3, PySide6 6.12.0, pytest 9.2.0, ruff 0.17.3, QT_QPA_PLATFORM=offscreen)

Note: requirements.txt pins PySide6==6.8.2; the sandbox used 6.12.0. Results with 6.8.2 are unverified.

| Command | Result |
|---|---|
| `python -m compileall -q .` | OK |
| `python -m ruff check .` | All checks passed (one pre-existing unused import was auto-fixed) |
| `python -m pytest -q -rs` | **205 passed**, 0 skipped (baseline before changes: 148 passed) |
| `python scripts/layout_regression_test.py --platform offscreen` | **134/134** checks passed (baseline 134/134 before changes; same count after) |

## New tests (57)
- `test_theme_contrast.py`: 36 contrast pairs, palette fidelity, no unresolved tokens, no raw hex in app.py / ui_theme.py / login_dialog.py.
- `test_login_ui.py`: RTL direction, validation, success, failure behaviour, duplicate-submit guard, Enter key, show/hide toggle, lockout notice, first-run.
- `test_session_isolation.py`: logout clears cart/discount/payment, admin navigation per role, cancelled re-login leaves no session.
- `test_auth_permissions.py`: +2 (inactive account uniform error, no credentials in error/log/audit); lockout test changed to assert the uniform message.

## Test changes you should know about
- The old lockout assertion matched the message "قفل"; it was replaced because that message was the disclosure being fixed.
- `scripts/layout_regression_test.py`: accepts the new login button name and has a watchdog; no layout assertion was modified.
- `test_startup_guard.py`: the modal error box is stubbed (it blocked once a QApplication existed) and the stub is asserted.

## NOT executed / unverified
Windows runs of anything; PyInstaller build; real EXE launch; PySide6 6.8.2; physical Arabic font rendering; screen-by-screen UI beyond cashier and login; layout at resolutions other than the five in the script; the GitHub Actions workflow (edited, never run); backup/restore end-to-end through the GUI; timing-equality of login responses; any test of the unknown-cost UI.
