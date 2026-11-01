"""Cash shift lifecycle with service-layer authorization."""
from __future__ import annotations

from datetime import datetime

from services.accounting import business_date_for, calculate_expected_cash
from services.audit import write_audit_log
from services.security import require_active_user
from services.tx import write_transaction

MOVEMENT_TYPES = {"مصروف", "سحب", "إيداع"}


def open_shift(connection, *, user: dict, opening_cash: int) -> int:
    if opening_cash < 0:
        raise ValueError("رصيد بداية الخزنة لا يمكن أن يكون سالبًا")
    with write_transaction(connection):
        require_active_user(connection, user)
        if connection.execute("SELECT 1 FROM cash_shifts WHERE status='open' AND user_id=?",
                              (user["id"],)).fetchone():
            raise ValueError("لديك وردية مفتوحة بالفعل. أغلقها قبل فتح وردية جديدة.")
        cur = connection.execute("INSERT INTO cash_shifts(user_id,opening_cash,status) VALUES(?,?,'open')",
                                 (user["id"], int(opening_cash)))
        write_audit_log(connection, user_id=user["id"], username=user.get("username", ""),
                        action="cash_shift_opened", entity_type="cash_shift", entity_id=cur.lastrowid,
                        details={"opening_cash": int(opening_cash)})
    return int(cur.lastrowid)


def _own_open_shift(connection, user: dict, shift_id: int) -> None:
    """A user may act only on their own open shift (admins included: the drawer is personal)."""
    row = connection.execute("SELECT user_id,status FROM cash_shifts WHERE id=?", (shift_id,)).fetchone()
    if not row or row[1] != "open" or int(row[0]) != int(user["id"]):
        raise PermissionError("هذه الوردية غير مفتوحة أو لا تخص المستخدم الحالي")


def add_cash_movement(connection, *, user: dict, shift_id: int, movement_type: str,
                      amount: int, reason: str) -> int:
    if movement_type not in MOVEMENT_TYPES:
        raise ValueError("نوع حركة الخزنة غير صالح")
    if amount <= 0:
        raise ValueError("مبلغ الحركة يجب أن يكون أكبر من صفر")
    if not (reason or "").strip():
        raise ValueError("سبب حركة الخزنة مطلوب")
    with write_transaction(connection):
        require_active_user(connection, user)
        _own_open_shift(connection, user, shift_id)
        if movement_type in {"مصروف", "سحب"}:
            available = calculate_expected_cash(connection, shift_id)
            if amount > available:
                raise ValueError(f"المبلغ أكبر من الرصيد المتوقع في الخزنة ({available} قرش)")
        cur = connection.execute(
            "INSERT INTO cash_movements(shift_id,user_id,movement_type,amount,reason,business_date) "
            "VALUES(?,?,?,?,?,?)",
            (shift_id, user["id"], movement_type, int(amount), reason.strip(),
             business_date_for(datetime.now())))
        write_audit_log(connection, user_id=user["id"], username=user.get("username", ""),
                        action="cash_movement_recorded", entity_type="cash_movement",
                        entity_id=cur.lastrowid,
                        details={"type": movement_type, "amount": int(amount), "reason": reason.strip()})
    return int(cur.lastrowid)


def close_shift(connection, *, user: dict, shift_id: int, actual_cash: int,
                expected_cash: int | None = None) -> dict:
    """Close the user's own open shift; expected cash is computed here, never by the UI.

    ``expected_cash`` is optional: when the UI showed the cashier an expected figure, pass it so the
    comparison happens INSIDE the transaction. If the figure changed meanwhile the shift stays open
    and a ValueError is raised (previously the shift was closed first and the UI then complained).
    """
    if actual_cash < 0:
        raise ValueError("المبلغ الفعلي لا يمكن أن يكون سالبًا")
    with write_transaction(connection):
        require_active_user(connection, user)
        _own_open_shift(connection, user, shift_id)
        expected = calculate_expected_cash(connection, shift_id)
        if expected_cash is not None and int(expected_cash) != expected:
            raise ValueError("تغيّر الرصيد المتوقع للخزنة أثناء الإغلاق؛ راجع الحركات ثم أعد المحاولة. "
                             "لم يتم إغلاق الوردية.")
        difference = int(actual_cash) - expected
        cur = connection.execute(
            "UPDATE cash_shifts SET closed_at=datetime('now','localtime'),expected_cash=?,"
            "actual_cash=?,difference=?,status='closed' WHERE id=? AND status='open'",
            (expected, int(actual_cash), difference, shift_id))
        if cur.rowcount != 1:
            raise ValueError("الوردية لم تعد مفتوحة")
        write_audit_log(connection, user_id=user["id"], username=user.get("username", ""),
                        action="cash_shift_closed", entity_type="cash_shift", entity_id=shift_id,
                        details={"expected": expected, "actual": int(actual_cash), "difference": difference})
    return {"shift_id": shift_id, "expected": expected, "actual": int(actual_cash), "difference": difference}
