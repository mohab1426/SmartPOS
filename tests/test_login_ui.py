"""Login / first-run dialog behaviour (offscreen Qt; no database involved)."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from login_dialog import FirstRunDialog, LoginDialog, LOCKOUT_NOTICE  # noqa: E402
from services.auth import GENERIC_LOGIN_ERROR  # noqa: E402

USER = {"id": 7, "username": "sara", "display_name": "Sara", "role": "cashier", "active": 1}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make(qapp, fn):
    d = LoginDialog(fn)
    d.show()
    return d


def test_dialog_is_really_right_to_left(qapp):
    d = make(qapp, lambda u, p: USER)
    assert d.layoutDirection() == Qt.LayoutDirection.RightToLeft
    d.close()


def test_empty_fields_show_validation_and_never_call_service(qapp):
    calls = []
    d = make(qapp, lambda u, p: calls.append((u, p)) or USER)
    d.attempt_login()
    assert d.error_label.isVisible() and not calls
    d.username.setText("sara")
    d.attempt_login()
    assert d.error_label.isVisible() and not calls
    d.close()


def test_success_returns_user_and_clears_password(qapp):
    d = make(qapp, lambda u, p: USER if (u, p) == ("sara", "Right#Pass-1") else (_ for _ in ()).throw(PermissionError("x")))
    d.username.setText(" sara ")
    d.password.setText("Right#Pass-1")
    d.attempt_login()
    assert d.result() == d.DialogCode.Accepted and d.user == USER
    assert d.password.text() == ""


def test_failure_shows_generic_message_clears_password_and_stays_open(qapp):
    def fail(u, p):
        raise PermissionError(GENERIC_LOGIN_ERROR)
    d = make(qapp, fail)
    d.username.setText("sara")
    d.password.setText("wrong-guess")
    d.password_row.toggle.setChecked(True)
    d.attempt_login()
    assert d.isVisible() and d.user is None
    assert d.error_label.text() == GENERIC_LOGIN_ERROR
    assert d.password.text() == "" and d.password.echoMode() == QLineEdit.EchoMode.Password
    assert d.submit.isEnabled() and d.username.isEnabled() and d.password.isEnabled()   # form usable again
    d.close()


def test_reentrant_submit_is_ignored_while_busy(qapp):
    calls = []
    d = make(qapp, lambda u, p: calls.append(1) or USER)
    d.username.setText("sara")
    d.password.setText("Right#Pass-1")
    d._busy = True                       # simulates a verification already in progress
    d.attempt_login()
    assert calls == []
    d.close()


def test_service_is_called_exactly_once_per_accepted_submit(qapp):
    calls = []

    def ok(u, p):
        calls.append(1)
        return USER
    d = make(qapp, ok)
    d.username.setText("sara")
    d.password.setText("Right#Pass-1")
    d.attempt_login()
    d.attempt_login()                    # dialog is already accepted/closed; a second call must not authenticate twice
    assert calls == [1]                  # the cleared password makes the 2nd call a validation error, not a request


def test_enter_moves_from_username_to_password_then_submits(qapp):
    calls = []
    d = make(qapp, lambda u, p: calls.append((u, p)) or USER)
    d.username.setText("sara")
    d.username.returnPressed.emit()
    assert not calls                      # password empty -> only moves focus
    d.password.setText("Right#Pass-1")
    QTest.keyClick(d.password, Qt.Key.Key_Return)
    assert calls == [("sara", "Right#Pass-1")]


def test_password_toggle_switches_echo_mode_and_is_not_in_tab_order(qapp):
    d = make(qapp, lambda u, p: USER)
    assert d.password.echoMode() == QLineEdit.EchoMode.Password
    d.password_row.toggle.setChecked(True)
    assert d.password.echoMode() == QLineEdit.EchoMode.Normal
    d.password_row.toggle.setChecked(False)
    assert d.password.echoMode() == QLineEdit.EchoMode.Password
    assert d.password_row.toggle.focusPolicy() == Qt.FocusPolicy.NoFocus
    d.close()


def test_static_lockout_notice_is_present_for_everyone(qapp):
    d = make(qapp, lambda u, p: USER)
    texts = [w.text() for w in d.findChildren(type(d.error_label))]
    assert LOCKOUT_NOTICE in texts
    d.close()


def test_first_run_requires_matching_passwords_and_reports_service_errors(qapp):
    seen = []

    def create(u, n, p):
        seen.append((u, n, p))
        if p == "weakpass":
            raise ValueError("كلمة المرور شائعة")
        return {"id": 1, "username": u, "display_name": n, "role": "admin", "active": 1}
    d = FirstRunDialog(create)
    d.show()
    d.password.setText("Strong#Pass-9")
    d.confirm.setText("different")
    d.attempt_create()
    assert d.error_label.isVisible() and not seen
    d.password.setText("weakpass")
    d.confirm.setText("weakpass")
    d.attempt_create()
    assert "شائعة" in d.error_label.text() and d.user is None
    d.password.setText("Strong#Pass-9")
    d.confirm.setText("Strong#Pass-9")
    d.attempt_create()
    assert d.user["role"] == "admin" and d.password.text() == "" and d.result() == d.DialogCode.Accepted
