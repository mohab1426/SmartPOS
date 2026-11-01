"""Small admin-controlled key/value settings (authorization policy flags)."""
from __future__ import annotations

from services.audit import write_audit_log
from services.security import require_active_admin
from services.tx import write_transaction

# key -> (allowed values, default)
SETTINGS: dict[str, tuple[set[str], str]] = {
    # Cashiers may register returns for invoices unless an admin turns this off.
    "cashier_can_process_returns": ({"0", "1"}, "1"),
}


def get_setting(connection, key: str) -> str:
    if key not in SETTINGS:
        raise KeyError(f"Unknown setting: {key}")
    row = connection.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else SETTINGS[key][1]


def set_setting(connection, *, actor: dict, key: str, value: str) -> None:
    if key not in SETTINGS:
        raise KeyError(f"Unknown setting: {key}")
    value = str(value)
    if value not in SETTINGS[key][0]:
        raise ValueError(f"Invalid value for {key}")
    with write_transaction(connection):
        require_active_admin(connection, actor)
        old = get_setting(connection, key)
        connection.execute(
            "INSERT INTO app_settings(key,value,updated_by) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=datetime('now','localtime'),updated_by=excluded.updated_by",
            (key, value, actor["id"]))
        write_audit_log(connection, user_id=actor["id"], username=actor.get("username", ""),
                        action="setting_changed", entity_type="app_settings", entity_id=key,
                        details={"old": old, "new": value})
