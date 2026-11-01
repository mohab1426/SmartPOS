from __future__ import annotations
import json


def write_audit_log(connection, *, user_id: int | None, username: str,
                    action: str, entity_type: str, entity_id: str | int = "",
                    details: dict | str | None = None) -> int:
    if isinstance(details, dict):
        details_value = json.dumps(details, ensure_ascii=False, sort_keys=True)
    else:
        details_value = str(details or "")
    cur = connection.execute("""
        INSERT INTO audit_log(user_id,username,action,entity_type,entity_id,details)
        VALUES(?,?,?,?,?,?)
    """, (user_id, username or "", action, entity_type, str(entity_id), details_value))
    return int(cur.lastrowid)
