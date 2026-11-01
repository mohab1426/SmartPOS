from __future__ import annotations

# Highest schema version this build understands (see database.py migrations).
SUPPORTED_SCHEMA_VERSION = 8


def is_admin(user) -> bool:
    return bool(user and user.get("role") == "admin" and user.get("active", 1))


def require_admin_user(user) -> None:
    if not is_admin(user):
        raise PermissionError("This operation requires an active administrator account.")


def validate_restore_database(connection) -> None:
    check = connection.execute("PRAGMA integrity_check").fetchone()
    if not check or check[0] != "ok":
        raise ValueError("Backup failed SQLite integrity_check")
    required = {"products", "invoices", "invoice_items", "users"}
    tables = {r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not required.issubset(tables):
        raise ValueError("The selected file is not a supported SmartPOS backup")
    # Pre-migration SmartPOS backups are supported; initialization will upgrade
    # them after activation. Newer schema versions are rejected, not downgraded.
    if "schema_migrations" in tables:
        version = connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        if int(version) > SUPPORTED_SCHEMA_VERSION:
            raise ValueError(f"Backup schema version {version} is newer than this application")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ValueError(f"Backup has {len(violations)} foreign-key violations")


def require_active_user(connection, user) -> str:
    """Resolve authorization from the live database, never from a stale UI dict."""
    if not user or user.get("id") is None:
        raise PermissionError("يجب تسجيل الدخول أولًا")
    row = connection.execute("SELECT role,active FROM users WHERE id=?", (int(user["id"]),)).fetchone()
    if not row or not row[1]:
        raise PermissionError("حساب المستخدم غير نشط أو لم يعد موجودًا")
    return str(row[0])


def require_active_admin(connection, user) -> None:
    """Re-check administrator privileges against the live users table."""
    role = require_active_user(connection, user)
    if role != "admin":
        raise PermissionError("هذه العملية متاحة لحساب المدير النشط فقط")
