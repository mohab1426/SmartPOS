"""Real SmartPOS window: logout must drop the previous user's cart and permissions (offscreen Qt)."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_logout_clears_cart_and_permissions_between_users(qapp, store_db, cashier, monkeypatch):
    import app as smartpos_app
    users = iter([dict(cashier), dict(store_db["user"])])

    def fake_login(self):
        u = next(users)
        self.current_user = {k: u[k] for k in ("id", "username", "display_name", "role")}
        return True
    monkeypatch.setattr(smartpos_app.SmartPOS, "setup_or_login", fake_login)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)   # no modal waits in tests

    win = smartpos_app.SmartPOS()
    assert win.current_user["role"] == "cashier"
    admin_nav = [b for b, (_i, title, _c) in zip(win.nav_buttons, win.nav_items)
                 if title not in ("الرئيسية", "الكاشير", "الورديات والخزنة")]
    assert admin_nav and not any(b.isVisible() or b.isEnabled() and b.isVisible() for b in admin_nav)
    win.cart[999] = {"name": "leftover", "price": 1000, "quantity": 2}
    win.logout()                                    # -> second user is the admin
    assert win.current_user["role"] == "admin"
    assert win.cart == {}                           # the cashier's cart did not leak to the next session
    assert win.discount_input.value() == 0 and win.paid_input.value() == 0
    assert all(not b.isHidden() for b in admin_nav)  # admin pages are available to the admin only
    win.close()


def test_logout_without_new_login_leaves_no_session(qapp, store_db, cashier, monkeypatch):
    import app as smartpos_app
    first = {"done": False}

    def fake_login(self):
        if not first["done"]:
            first["done"] = True
            self.current_user = {k: cashier[k] for k in ("id", "username", "display_name", "role")}
            return True
        return False                                 # user cancels the next login dialog
    monkeypatch.setattr(smartpos_app.SmartPOS, "setup_or_login", fake_login)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)   # no modal waits in tests
    quit_called = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quit_called.append(1)))
    win = smartpos_app.SmartPOS()
    win.logout()
    assert win.current_user is None and quit_called == [1]
    assert not win.isVisible()
    win.close()
