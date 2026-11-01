"""Authentication, user management and password workflows (service layer).

Every privileged function re-reads the acting user from the database; a
``user`` dict handed over by the UI is never trusted for its role.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from services.audit import write_audit_log
from services.security import require_active_admin, require_active_user
from services.tx import write_transaction

LEGACY_PBKDF2_ITERATIONS = 240_000   # hashes stored as ``salt$digest`` (no iteration count)
PBKDF2_ITERATIONS = 600_000          # new hashes: ``pbkdf2_sha256$<iterations>$salt$digest``
MIN_PASSWORD_LENGTH = 8
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 5
_COMMON_PASSWORDS = {"12345678", "123456789", "1234567890", "password", "password1",
                     "admin123", "admin1234", "qwertyui", "11111111", "00000000",
                     "iloveyou", "abcd1234", "smartpos", "smartpos1"}
_DUMMY_HASH = ""  # filled lazily; used to equalise timing for unknown usernames
# One message for unknown user, wrong password, inactive account AND temporarily locked account, so an
# unauthenticated caller cannot learn which usernames exist.  The login screen shows a *static* notice
# that repeated failures lock accounts for a few minutes (it applies to every name, so it leaks nothing).
GENERIC_LOGIN_ERROR = "بيانات الدخول غير صحيحة أو الحساب غير متاح حاليًا"


def _pbkdf2(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()


def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-HMAC-SHA256, per-user random salt, iteration count stored in the hash."""
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${_pbkdf2(password, salt, PBKDF2_ITERATIONS)}"


def _parse_hash(stored: str) -> tuple[int, str, str]:
    """Return (iterations, salt, digest) for both the new and the legacy (``salt$digest``) formats."""
    if stored.startswith("pbkdf2_sha256$"):
        _, iterations, salt, digest = stored.split("$", 3)
        return int(iterations), salt, digest
    salt, digest = stored.split("$", 1)
    return LEGACY_PBKDF2_ITERATIONS, salt, digest


def verify_password(password: str, stored: str) -> bool:
    try:
        iterations, salt, expected = _parse_hash(stored)
        return hmac.compare_digest(_pbkdf2(password, salt, iterations), expected)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _parse_hash(stored)[0] < PBKDF2_ITERATIONS
    except Exception:
        return False


def validate_password_policy(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("كلمة المرور يجب ألا تقل عن 8 أحرف")
    if password.strip().lower() in _COMMON_PASSWORDS or (password.isdigit() and len(set(password)) == 1):
        raise ValueError("كلمة المرور شائعة وسهلة التخمين؛ اختر كلمة مرور أقوى")
    if username and password.strip().lower() == username.strip().lower():
        raise ValueError("كلمة المرور يجب ألا تطابق اسم المستخدم")


def _public(row) -> dict:
    return {"id": row[0], "username": row[1], "display_name": row[2], "role": row[3], "active": row[4]}


def authenticate(connection, username: str, password: str, *, now: datetime | None = None) -> dict:
    """Verify credentials with lockout. Same error for unknown user / wrong password."""
    global _DUMMY_HASH
    now = now or datetime.now()
    generic = PermissionError(GENERIC_LOGIN_ERROR)
    row = connection.execute(
        "SELECT id,username,display_name,role,active,password_hash,failed_logins,locked_until "
        "FROM users WHERE username=? COLLATE NOCASE", ((username or "").strip(),)).fetchone()
    if row is None:
        if not _DUMMY_HASH:
            _DUMMY_HASH = hash_password("dummy-password-for-timing")
        verify_password(password or "", _DUMMY_HASH)
        raise generic
    user_id, _u, _d, _r, active, stored, failed, locked_until = row
    ok = verify_password(password or "", stored)     # always pay the hashing cost (timing parity with unknown users)
    if locked_until and datetime.fromisoformat(locked_until) > now:
        raise generic                                  # locked: refuse even a correct password, same message
    with write_transaction(connection):
        if not ok or not active:
            failed = int(failed or 0) + 1
            lock = None
            if failed >= MAX_FAILED_LOGINS:
                lock = (now + timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                failed = 0
                write_audit_log(connection, user_id=user_id, username=row[1], action="account_locked",
                                entity_type="user", entity_id=user_id, details={"minutes": LOCKOUT_MINUTES})
            connection.execute("UPDATE users SET failed_logins=?,locked_until=? WHERE id=?",
                               (failed, lock, user_id))
        else:
            connection.execute("UPDATE users SET failed_logins=0,locked_until=NULL WHERE id=?", (user_id,))
            if needs_rehash(stored):      # transparently upgrade old hashes after a successful login
                connection.execute("UPDATE users SET password_hash=? WHERE id=?",
                                   (hash_password(password), user_id))
    if not ok or not active:
        raise generic
    return _public(row)


def create_first_admin(connection, *, username: str, display_name: str, password: str) -> dict:
    """First-run setup. Allowed only while the users table is empty."""
    username = username.strip()
    validate_password_policy(password, username)
    if not username or not display_name.strip():
        raise ValueError("اسم المستخدم والاسم الظاهر مطلوبان")
    with write_transaction(connection):
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise PermissionError("تم إعداد حساب المدير بالفعل")
        cur = connection.execute(
            "INSERT INTO users(username,display_name,password_hash,role) VALUES(?,?,?,'admin')",
            (username, display_name.strip(), hash_password(password)))
        write_audit_log(connection, user_id=cur.lastrowid, username=username, action="first_admin_created",
                        entity_type="user", entity_id=cur.lastrowid)
    return {"id": cur.lastrowid, "username": username, "display_name": display_name.strip(),
            "role": "admin", "active": 1}


def create_user(connection, *, actor: dict, username: str, display_name: str,
                password: str, role: str = "cashier") -> int:
    if role not in {"admin", "cashier"}:
        raise ValueError("دور المستخدم غير صالح")
    username = username.strip()
    if not username or not display_name.strip():
        raise ValueError("اسم المستخدم والاسم الظاهر مطلوبان")
    validate_password_policy(password, username)
    with write_transaction(connection):
        require_active_admin(connection, actor)
        cur = connection.execute(
            "INSERT INTO users(username,display_name,password_hash,role) VALUES(?,?,?,?)",
            (username, display_name.strip(), hash_password(password), role))
        write_audit_log(connection, user_id=actor["id"], username=actor.get("username", ""),
                        action="user_created", entity_type="user", entity_id=cur.lastrowid,
                        details={"username": username, "role": role})
    return int(cur.lastrowid)


def change_own_password(connection, *, user: dict, current_password: str, new_password: str) -> None:
    with write_transaction(connection):
        require_active_user(connection, user)
        row = connection.execute("SELECT username,password_hash FROM users WHERE id=?",
                                 (user["id"],)).fetchone()
        if not verify_password(current_password or "", row[1]):
            raise PermissionError("كلمة المرور الحالية غير صحيحة")
        validate_password_policy(new_password, row[0])
        if verify_password(new_password, row[1]):
            raise ValueError("كلمة المرور الجديدة يجب أن تختلف عن الحالية")
        connection.execute("UPDATE users SET password_hash=?,failed_logins=0,locked_until=NULL WHERE id=?",
                           (hash_password(new_password), user["id"]))
        write_audit_log(connection, user_id=user["id"], username=row[0],
                        action="password_changed", entity_type="user", entity_id=user["id"])


def reset_user_password(connection, *, actor: dict, target_user_id: int, new_password: str) -> None:
    with write_transaction(connection):
        require_active_admin(connection, actor)
        row = connection.execute("SELECT username FROM users WHERE id=?", (target_user_id,)).fetchone()
        if not row:
            raise ValueError("المستخدم غير موجود")
        validate_password_policy(new_password, row[0])
        connection.execute("UPDATE users SET password_hash=?,failed_logins=0,locked_until=NULL WHERE id=?",
                           (hash_password(new_password), target_user_id))
        write_audit_log(connection, user_id=actor["id"], username=actor.get("username", ""),
                        action="password_reset", entity_type="user", entity_id=target_user_id)


def set_user_active(connection, *, actor: dict, target_user_id: int, active: bool) -> None:
    with write_transaction(connection):
        require_active_admin(connection, actor)
        row = connection.execute("SELECT role,active FROM users WHERE id=?", (target_user_id,)).fetchone()
        if not row:
            raise ValueError("المستخدم غير موجود")
        if not active and row[0] == "admin" and row[1]:
            others = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                (target_user_id,)).fetchone()[0]
            if others == 0:
                raise ValueError("لا يمكن إيقاف آخر حساب مدير نشط")
        connection.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, target_user_id))
        write_audit_log(connection, user_id=actor["id"], username=actor.get("username", ""),
                        action="user_activated" if active else "user_deactivated",
                        entity_type="user", entity_id=target_user_id)
