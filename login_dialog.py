"""Login and first-run administrator setup dialogs.

Presentation only: authentication itself stays in ``services.auth``.  The dialogs receive the service
call as a callable so they can be tested without a real database.

Behaviour
* Arabic-first, real right-to-left layout direction (not only text alignment).
* Enter in the username field moves to the password field; Enter in the password field submits.
* While a request is running the form is disabled and re-entrant submissions are ignored.
* The password field is cleared after a failed attempt; credentials are never logged or kept.
* Errors are shown inline (one generic message for every failed login - see services.auth).
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from app_version import APP_VERSION

LOCKOUT_NOTICE = "بعد عدة محاولات دخول خاطئة يُقفل الحساب مؤقتًا لبضع دقائق."


def _field(label_text: str, widget: QWidget, hint: str = "") -> QWidget:
    box = QWidget()
    box.setObjectName("FieldBox")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    label = QLabel(label_text)
    label.setObjectName("FieldLabel")
    lay.addWidget(label)
    lay.addWidget(widget)
    if hint:
        h = QLabel(hint)
        h.setObjectName("FieldHint")
        h.setWordWrap(True)
        lay.addWidget(h)
    return box


class _PasswordRow(QWidget):
    """Password input with a show/hide toggle that stays out of the Tab order (Tab: user -> pass -> submit)."""

    def __init__(self, placeholder: str = ""):
        super().__init__()
        self.setObjectName("FieldBox")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)   # passwords are LTR; placeholder stays readable
        self.edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.toggle = QPushButton("إظهار")
        self.toggle.setObjectName("PasswordToggle")
        self.toggle.setCheckable(True)
        self.toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.toggle.setToolTip("إظهار / إخفاء كلمة المرور")
        self.toggle.setAccessibleName("إظهار كلمة المرور")
        self.toggle.toggled.connect(self._toggled)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.toggle)

    def _toggled(self, shown: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.toggle.setText("إخفاء" if shown else "إظهار")

    def hide_text(self) -> None:
        self.toggle.setChecked(False)


class _BaseAuthDialog(QDialog):
    def __init__(self, parent, window_title: str, title: str, subtitle: str, min_width: int = 420):
        super().__init__(parent)
        self.setObjectName("LoginDialog")
        self.setWindowTitle(window_title)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setMinimumWidth(min_width)
        self.user: dict | None = None
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 18)
        card = QFrame()
        card.setObjectName("LoginCard")
        outer.addWidget(card, 1)
        self.body = QVBoxLayout(card)
        self.body.setContentsMargins(30, 26, 30, 26)
        self.body.setSpacing(13)

        logo = QLabel("S")
        logo.setObjectName("LoginLogo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(52, 52)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(logo)
        row.addStretch()
        self.body.addLayout(row)
        t = QLabel(title)
        t.setObjectName("LoginTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(t)
        s = QLabel(subtitle)
        s.setObjectName("LoginSubtitle")
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s.setWordWrap(True)
        self.body.addWidget(s)

        self.error_label = QLabel("")
        self.error_label.setObjectName("FormError")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName("رسالة خطأ")
        self.error_label.hide()

        footer = QLabel(f"Smart POS  •  {APP_VERSION}")
        footer.setObjectName("FieldHint")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(footer)

    def showEvent(self, event):
        super().showEvent(event)
        needed = self.layout().totalHeightForWidth(self.width())   # wrapped Arabic labels need height-for-width
        needed = max(needed, self.sizeHint().height())
        if needed > self.height():
            self.setMinimumHeight(needed)
            self.resize(self.width(), needed)

    def _fix_heights(self) -> None:
        for edit in self.findChildren(QLineEdit):
            edit.setMinimumHeight(42)      # keeps the 2px focus border from clipping the text line

    # -- shared helpers -------------------------------------------------------------------------
    def show_error(self, message: str, *fields: QLineEdit) -> None:
        self.error_label.setText(message)
        self.error_label.show()
        for edit in fields:
            self._mark_invalid(edit, True)

    def clear_error(self) -> None:
        self.error_label.hide()
        self.error_label.setText("")
        for edit in self.findChildren(QLineEdit):
            self._mark_invalid(edit, False)

    @staticmethod
    def _mark_invalid(edit: QLineEdit, invalid: bool) -> None:
        edit.setProperty("invalid", "true" if invalid else "false")
        edit.style().unpolish(edit)
        edit.style().polish(edit)

    def set_busy(self, busy: bool, button: QPushButton, idle_text: str, busy_text: str) -> None:
        self._busy = busy
        button.setEnabled(not busy)
        button.setText(busy_text if busy else idle_text)
        for edit in self.findChildren(QLineEdit):
            edit.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            QApplication.processEvents()          # paint the busy state before the (CPU-bound) hash check
        else:
            QApplication.restoreOverrideCursor()


class LoginDialog(_BaseAuthDialog):
    """``authenticate(username, password) -> user dict``; must raise PermissionError on failure."""

    def __init__(self, authenticate: Callable[[str, str], dict], parent=None):
        super().__init__(parent, "تسجيل الدخول | Smart POS", "Smart POS", "سجّل الدخول للمتابعة")
        self._authenticate = authenticate
        self.username = QLineEdit()
        self.username.setPlaceholderText("اسم المستخدم")
        self.username.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.username.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.username.setAccessibleName("اسم المستخدم")
        self.password_row = _PasswordRow("كلمة المرور")
        self.password = self.password_row.edit
        self.password.setAccessibleName("كلمة المرور")
        self.body.addWidget(self.error_label)
        self.body.addWidget(_field("اسم المستخدم", self.username))
        self.body.addWidget(_field("كلمة المرور", self.password_row))
        self.submit = QPushButton("تسجيل الدخول")
        self.submit.setObjectName("LoginPrimary")
        self.submit.setDefault(True)
        self.body.addSpacing(4)
        self.body.addWidget(self.submit)
        notice = QLabel(LOCKOUT_NOTICE)
        notice.setObjectName("FieldHint")
        notice.setWordWrap(True)
        notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(notice)

        self.submit.clicked.connect(self.attempt_login)
        self.username.returnPressed.connect(self._enter_in_username)
        self.password.returnPressed.connect(self.attempt_login)
        self.username.textEdited.connect(lambda _t: self.clear_error())
        self.password.textEdited.connect(lambda _t: self.clear_error())
        self.setTabOrder(self.username, self.password)
        self.setTabOrder(self.password, self.submit)
        self._fix_heights()
        QTimer.singleShot(0, self.username.setFocus)

    def _enter_in_username(self) -> None:
        if self.password.text():
            self.attempt_login()
        else:
            self.password.setFocus()

    def attempt_login(self) -> None:
        if self._busy:                       # ignore double clicks / repeated Enter while verifying
            return
        username, password = self.username.text().strip(), self.password.text()
        if not username:
            self.show_error("اكتب اسم المستخدم.", self.username)
            self.username.setFocus()
            return
        if not password:
            self.show_error("اكتب كلمة المرور.", self.password)
            self.password.setFocus()
            return
        self.clear_error()
        self.set_busy(True, self.submit, "تسجيل الدخول", "جارٍ التحقق…")
        try:
            user = self._authenticate(username, password)
        except PermissionError as exc:
            self.set_busy(False, self.submit, "تسجيل الدخول", "جارٍ التحقق…")
            self.password.clear()
            self.password_row.hide_text()
            self.show_error(str(exc), self.username, self.password)
            self.password.setFocus()
            return
        except Exception:                    # never show internals or credentials; the caller logs the traceback
            self.set_busy(False, self.submit, "تسجيل الدخول", "جارٍ التحقق…")
            self.password.clear()
            logging.getLogger("smartpos").exception("Unexpected error during login")
            self.show_error("تعذر تسجيل الدخول بسبب خطأ غير متوقع. حاول مرة أخرى.")
            return
        self.set_busy(False, self.submit, "تسجيل الدخول", "جارٍ التحقق…")
        self.password.clear()                # do not keep the secret in the widget after success
        self.user = user
        self.accept()


class FirstRunDialog(_BaseAuthDialog):
    """First launch: create the administrator. ``create_admin(username, display_name, password) -> user dict``."""

    def __init__(self, create_admin: Callable[[str, str, str], dict], parent=None):
        super().__init__(parent, "إعداد حساب المدير لأول مرة", "مرحبًا بك في Smart POS",
                         "أنشئ حساب المدير الذي يملك الصلاحيات الكاملة. احفظ كلمة المرور في مكان آمن.", 460)
        self._create_admin = create_admin
        self.username = QLineEdit("admin")
        self.username.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.username.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.display = QLineEdit("مدير النظام")
        self.password_row = _PasswordRow()
        self.password = self.password_row.edit
        self.confirm_row = _PasswordRow()
        self.confirm = self.confirm_row.edit
        self.body.addWidget(self.error_label)
        self.body.addWidget(_field("اسم المستخدم", self.username))
        self.body.addWidget(_field("الاسم الظاهر", self.display))
        self.body.addWidget(_field("كلمة المرور", self.password_row, "8 أحرف على الأقل، وليست كلمة شائعة."))
        self.body.addWidget(_field("تأكيد كلمة المرور", self.confirm_row))
        self.submit = QPushButton("إنشاء حساب المدير")
        self.submit.setObjectName("LoginPrimary")
        self.submit.setDefault(True)
        self.body.addSpacing(4)
        self.body.addWidget(self.submit)
        self.submit.clicked.connect(self.attempt_create)
        self.confirm.returnPressed.connect(self.attempt_create)
        for edit in (self.username, self.display, self.password, self.confirm):
            edit.textEdited.connect(lambda _t: self.clear_error())
        self._fix_heights()
        QTimer.singleShot(0, self.password.setFocus)

    def attempt_create(self) -> None:
        if self._busy:
            return
        username, display, pw = self.username.text().strip(), self.display.text().strip(), self.password.text()
        if not username or not display:
            self.show_error("اكتب اسم المستخدم والاسم الظاهر.", *[e for e in (self.username, self.display) if not e.text().strip()])
            return
        if pw != self.confirm.text():
            self.show_error("كلمتا المرور غير متطابقتين.", self.password, self.confirm)
            self.confirm.setFocus()
            return
        self.clear_error()
        self.set_busy(True, self.submit, "إنشاء حساب المدير", "جارٍ الإنشاء…")
        try:
            user = self._create_admin(username, display, pw)
        except (ValueError, PermissionError) as exc:
            self.set_busy(False, self.submit, "إنشاء حساب المدير", "جارٍ الإنشاء…")
            self.show_error(str(exc), self.password)
            self.password.setFocus()
            return
        except Exception as exc:
            self.set_busy(False, self.submit, "إنشاء حساب المدير", "جارٍ الإنشاء…")
            if exc.__class__.__name__ == "IntegrityError":
                self.show_error("اسم المستخدم موجود بالفعل. اختر اسمًا مختلفًا.", self.username)
                return
            logging.getLogger("smartpos").exception("Unexpected error during first-admin setup")
            self.show_error("تعذر إنشاء الحساب بسبب خطأ غير متوقع.")
            return
        self.set_busy(False, self.submit, "إنشاء حساب المدير", "جارٍ الإنشاء…")
        self.password.clear()
        self.confirm.clear()
        self.user = user
        self.accept()
