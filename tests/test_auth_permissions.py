"""Authentication, password workflows and service-layer authorization (no UI involved)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import database
from services import auth
from services.backup import create_database_backup, restore_database_file
from services.inventory import adjust_stock, apply_stocktake, receive_purchase_line, record_damage
from services.purchasing import receive_purchase
from services.sales import PAYMENT_CASH, classify_legacy_return, create_sale
from services.settings import get_setting, set_setting
from services.shifts import add_cash_movement, close_shift
from helpers import connect


def test_fresh_database_has_no_default_users(tmp_path, monkeypatch):
    data = tmp_path / "fresh"
    for name, value in {"DATA_DIR": data, "DATABASE_PATH": data / "smartpos.db", "CONFIG_DIR": tmp_path / "cfg",
                        "BACKUP_DIR": data / "b", "LOG_DIR": data / "l", "MIGRATION_BACKUPS_DIR": data / "m",
                        "LEGACY_DATA_DIR": tmp_path / "nolegacy", "_STORAGE_READY": False}.items():
        monkeypatch.setattr(database, name, value)
    database.initialize_database()
    con = connect(database.DATABASE_PATH)
    try:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        admin = auth.create_first_admin(con, username="owner", display_name="Owner", password="Str0ng#Secret")
        assert admin["role"] == "admin"
        with pytest.raises(PermissionError):      # first-run setup can only happen once
            auth.create_first_admin(con, username="x", display_name="X", password="Another#Secret1")
    finally:
        con.close()


def test_login_valid_invalid_and_uniform_error(store_db):
    con = connect(store_db["path"])
    try:
        ok = auth.authenticate(con, "ADMIN", store_db["admin_password"])      # case-insensitive username
        assert ok["role"] == "admin" and "password_hash" not in ok
        with pytest.raises(PermissionError) as wrong:
            auth.authenticate(con, "admin", "not-the-password")
        with pytest.raises(PermissionError) as unknown:
            auth.authenticate(con, "nobody", "whatever123")
        assert str(wrong.value) == str(unknown.value)          # no user enumeration
    finally:
        con.close()


def test_account_locks_after_repeated_failures_and_unlocks(store_db):
    con = connect(store_db["path"])
    try:
        t0 = datetime(2026, 10, 31, 12, 0, 0)
        for _ in range(auth.MAX_FAILED_LOGINS):
            with pytest.raises(PermissionError):
                auth.authenticate(con, "admin", "bad-password-1", now=t0)
        with pytest.raises(PermissionError) as unknown:
            auth.authenticate(con, "nobody", "whatever123", now=t0)
        with pytest.raises(PermissionError) as locked:               # even the right password is refused ...
            auth.authenticate(con, "admin", store_db["admin_password"], now=t0 + timedelta(minutes=1))
        # ... and the refusal is indistinguishable from an unknown user (login-audit finding: no enumeration)
        assert str(locked.value) == str(unknown.value) == auth.GENERIC_LOGIN_ERROR
        ok = auth.authenticate(con, "admin", store_db["admin_password"],
                               now=t0 + timedelta(minutes=auth.LOCKOUT_MINUTES + 1))
        assert ok["username"] == "admin"
    finally:
        con.close()


def test_password_policy_and_hash_format():
    for bad in ("short", "12345678", "password", "admin"):
        with pytest.raises(ValueError):
            auth.validate_password_policy(bad, "admin")
    with pytest.raises(ValueError):
        auth.validate_password_policy("Sameasuser", "sameasuser")
    h1, h2 = auth.hash_password("Correct#Horse9"), auth.hash_password("Correct#Horse9")
    assert h1 != h2                                            # per-user random salt
    assert auth.verify_password("Correct#Horse9", h1) and not auth.verify_password("correct#horse9", h1)
    assert not auth.verify_password("x", "malformed-without-separator")


def test_password_change_and_admin_reset(store_db, cashier):
    con = connect(store_db["path"])
    try:
        with pytest.raises(PermissionError):
            auth.change_own_password(con, user=cashier, current_password="wrong", new_password="Brand#New-Pass1")
        auth.change_own_password(con, user=cashier, current_password=cashier["password"],
                                 new_password="Brand#New-Pass1")
        assert auth.authenticate(con, "cashier1", "Brand#New-Pass1")["id"] == cashier["id"]
        with pytest.raises(PermissionError):                   # old password no longer works
            auth.authenticate(con, "cashier1", cashier["password"])
        with pytest.raises(PermissionError):                   # a cashier cannot reset anyone's password
            auth.reset_user_password(con, actor=cashier, target_user_id=store_db["user"]["id"],
                                     new_password="Hijack#Attempt1")
        auth.reset_user_password(con, actor=store_db["user"], target_user_id=cashier["id"],
                                 new_password="Reset#By-Admin1")
        assert auth.authenticate(con, "cashier1", "Reset#By-Admin1")
        logged = " ".join(r[0] for r in con.execute("SELECT details FROM audit_log"))
        assert "Reset#By-Admin1" not in logged and "Brand#New-Pass1" not in logged   # secrets never audited
    finally:
        con.close()


def test_user_management_requires_live_admin(store_db, cashier):
    con = connect(store_db["path"])
    try:
        with pytest.raises(PermissionError):
            auth.create_user(con, actor=cashier, username="evil", display_name="Evil", password="Evil#Password1", role="admin")
        with pytest.raises(PermissionError):
            auth.set_user_active(con, actor=cashier, target_user_id=store_db["user"]["id"], active=False)
        forged = dict(cashier, role="admin")                   # UI dict claims admin; the database says cashier
        with pytest.raises(PermissionError):
            auth.create_user(con, actor=forged, username="evil2", display_name="Evil", password="Evil#Password1")
        with pytest.raises(ValueError, match="آخر حساب مدير"):
            auth.set_user_active(con, actor=store_db["user"], target_user_id=store_db["user"]["id"], active=False)
        auth.set_user_active(con, actor=store_db["user"], target_user_id=cashier["id"], active=False)
        with pytest.raises(PermissionError):                   # deactivated user cannot transact
            create_sale(con, user=cashier, cart_lines=[{"product_id": store_db["product_id"], "quantity": 1}],
                        discount=0, payment_method=PAYMENT_CASH, cash_received=1000)
    finally:
        con.close()


def test_cashier_denied_every_admin_operation_directly(store_db, cashier, tmp_path):
    con = connect(store_db["path"])
    pid = store_db["product_id"]
    try:
        backup = create_database_backup(store_db["path"], tmp_path / "b")
        with pytest.raises(PermissionError):   # restore: right password for HER account is still not enough
            restore_database_file(backup, store_db["path"], tmp_path / "s", cashier, admin_password=cashier["password"])
        with pytest.raises(PermissionError):
            adjust_stock(con, product_id=pid, new_quantity=1, user_id=cashier["id"], reason="x")
        with pytest.raises(PermissionError):
            record_damage(con, product_id=pid, quantity=1, user_id=cashier["id"], reason="x")
        with pytest.raises(PermissionError):
            apply_stocktake(con, counts={pid: 1}, user_id=cashier["id"], reason="x")
        with pytest.raises(PermissionError):
            receive_purchase(con, user=cashier, supplier_id=None, supplier_name="", notes="",
                             lines=[{"product_id": pid, "quantity": 1, "cost": 100, "code": "c", "name": "n"}])
        with pytest.raises(PermissionError):
            set_setting(con, actor=cashier, key="cashier_can_process_returns", value="0")
        with pytest.raises(PermissionError):                   # discount is admin-only
            create_sale(con, user=cashier, cart_lines=[{"product_id": pid, "quantity": 1}], discount=100,
                        payment_method=PAYMENT_CASH, cash_received=1000)
        assert con.execute("SELECT stock_quantity FROM products WHERE id=?", (pid,)).fetchone()[0] == 10
    finally:
        con.close()


def test_purchase_receiving_cannot_be_bypassed_with_missing_user(store_db):
    """Regression: receive_purchase_line used to skip the admin check when user_id was None."""
    con = connect(store_db["path"])
    try:
        pi = con.execute("INSERT INTO purchase_invoices(invoice_number,total) VALUES('PUR-X',1)").lastrowid
        con.commit()
        with pytest.raises(PermissionError):
            receive_purchase_line(con, purchase_invoice_id=pi, product_id=store_db["product_id"],
                                  product_code="SKU-1", product_name="Test Shirt", quantity=5,
                                  unit_cost=100, user_id=None)
        assert con.execute("SELECT stock_quantity FROM products").fetchone()[0] == 10
    finally:
        con.close()


def test_cashier_cannot_close_or_move_cash_in_someone_elses_shift(store_db, cashier):
    con = connect(store_db["path"])
    try:
        admin_shift = store_db["shift_id"]
        with pytest.raises(PermissionError):
            add_cash_movement(con, user=cashier, shift_id=admin_shift, movement_type="إيداع", amount=100, reason="x")
        with pytest.raises(PermissionError):
            close_shift(con, user=cashier, shift_id=admin_shift, actual_cash=0)
        assert con.execute("SELECT status FROM cash_shifts WHERE id=?", (admin_shift,)).fetchone()[0] == "open"
    finally:
        con.close()


def test_cashier_cannot_classify_legacy_returns_or_read_admin_only_settings(store_db, cashier):
    con = connect(store_db["path"])
    try:
        with pytest.raises(PermissionError):
            classify_legacy_return(con, admin_user=cashier, return_id=1, refund_method="نقدي", shift_id=cashier["shift_id"])
        assert get_setting(con, "cashier_can_process_returns") == "1"
        with pytest.raises(KeyError):
            get_setting(con, "no_such_setting")
        with pytest.raises(ValueError):
            set_setting(con, actor=store_db["user"], key="cashier_can_process_returns", value="maybe")
    finally:
        con.close()


# ---- PBKDF2 upgrade (audit S-02) -------------------------------------------------------------------
def test_legacy_hash_still_logs_in_and_is_upgraded(store_db):
    import hashlib
    from helpers import connect
    from services.auth import PBKDF2_ITERATIONS, authenticate, needs_rehash, verify_password
    con = connect(store_db["path"])
    legacy = "abc123salt$" + hashlib.pbkdf2_hmac("sha256", b"Legacy#Pass-2026", b"abc123salt", 240_000).hex()
    assert verify_password("Legacy#Pass-2026", legacy) and needs_rehash(legacy)
    assert not verify_password("wrong", legacy)
    con.execute("UPDATE users SET password_hash=? WHERE id=?", (legacy, store_db["user"]["id"]))
    con.commit()
    with pytest.raises(PermissionError):
        authenticate(con, "admin", "wrong")
    authenticate(con, "admin", "Legacy#Pass-2026")
    stored = con.execute("SELECT password_hash FROM users WHERE id=?", (store_db["user"]["id"],)).fetchone()[0]
    assert stored.startswith(f"pbkdf2_sha256${PBKDF2_ITERATIONS}$") and not needs_rehash(stored)
    assert verify_password("Legacy#Pass-2026", stored)
    authenticate(con, "admin", "Legacy#Pass-2026")                    # still works after the upgrade
    con.close()


def test_inactive_account_gets_same_generic_error(store_db):
    con = connect(store_db["path"])
    try:
        admin = auth.authenticate(con, "admin", store_db["admin_password"])
        uid = auth.create_user(con, actor=admin, username="temp_cashier", display_name="Temp",
                               password="Temp-Pass-9921", role="cashier")
        auth.set_user_active(con, actor=admin, target_user_id=uid, active=False)
        with pytest.raises(PermissionError) as inactive:
            auth.authenticate(con, "temp_cashier", "Temp-Pass-9921")     # right password, inactive account
        with pytest.raises(PermissionError) as unknown:
            auth.authenticate(con, "nobody", "whatever123")
        assert str(inactive.value) == str(unknown.value) == auth.GENERIC_LOGIN_ERROR
    finally:
        con.close()


def test_failed_login_never_logs_or_returns_credentials(store_db, caplog):
    con = connect(store_db["path"])
    try:
        with caplog.at_level("DEBUG"):
            with pytest.raises(PermissionError) as exc:
                auth.authenticate(con, "admin", "very-secret-guess-77")
        assert "very-secret-guess-77" not in str(exc.value)
        assert "very-secret-guess-77" not in caplog.text
        rows = con.execute("SELECT details FROM audit_log").fetchall()
        assert all("very-secret-guess-77" not in str(r[0]) for r in rows)
    finally:
        con.close()
