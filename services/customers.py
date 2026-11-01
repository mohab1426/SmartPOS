from __future__ import annotations
from datetime import datetime
from services.accounting import business_date_for
from services.security import require_active_user
from services.tx import write_transaction


class AmbiguousCustomerError(ValueError):
    """A customer name matches more than one active customer: the caller must choose by id."""


class DuplicateCustomerError(ValueError):
    """Creating this customer would duplicate an existing active name (needs explicit confirmation)."""


def find_customers_by_name(connection, name: str, *, active_only: bool = True) -> list[dict]:
    """Return every customer whose name equals ``name`` (case-insensitive), oldest first."""
    name = (name or "").strip()
    if not name:
        return []
    sql = "SELECT id,name,phone FROM customers WHERE name=? COLLATE NOCASE"
    if active_only:
        sql += " AND active=1"
    rows = connection.execute(sql + " ORDER BY id", (name,)).fetchall()
    return [{"id": int(r[0]), "name": r[1], "phone": r[2] or ""} for r in rows]


def create_customer(connection, name: str, phone: str = "", *, allow_duplicate: bool = False) -> int:
    """Explicitly create a customer account and return its id.

    Customer names are not unique, but creating a second active customer with an existing name is
    only done when the caller passes ``allow_duplicate=True`` (the UI sets it after the operator has
    seen the existing customers and confirmed).  Runs inside the caller's transaction.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("اسم العميل مطلوب")
    existing = find_customers_by_name(connection, name)
    if existing and not allow_duplicate:
        raise DuplicateCustomerError("يوجد عميل بنفس الاسم؛ اختر العميل الموجود أو أكّد إنشاء عميل جديد بنفس الاسم")
    cur = connection.execute("INSERT INTO customers(name,phone) VALUES(?,?)", (name, (phone or "").strip()))
    return int(cur.lastrowid)


def get_or_create_customer(connection, name: str, phone: str = "") -> int:
    """Legacy helper, kept for scripts: resolve by name only when the match is unique.

    It never picks "the first" of several customers with the same name: that raises
    ``AmbiguousCustomerError``.  Checkout does not use this function; it uses ids.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("اسم العميل مطلوب")
    matches = find_customers_by_name(connection, name)
    if len(matches) > 1:
        raise AmbiguousCustomerError("عدة عملاء بنفس الاسم؛ حدّد العميل بمعرّفه")
    if matches:
        if (phone or "").strip():
            connection.execute("UPDATE customers SET phone=? WHERE id=? AND (phone='' OR phone IS NULL)",
                               (phone.strip(), matches[0]["id"]))
        return matches[0]["id"]
    return create_customer(connection, name, phone)


def build_customer_labels(rows: list[tuple[int, str, str]]) -> list[str]:
    """Unique, human-readable selector labels for ``(id, name, phone)`` rows (same order as input).

    A name that is shared by several customers is shown with the phone number (or ``#id``) so that
    each entry can be told apart and a typed label can only ever match ONE customer.
    """
    counts: dict[str, int] = {}
    for _cid, name, _phone in rows:
        counts[name.strip().casefold()] = counts.get(name.strip().casefold(), 0) + 1
    labels: list[str] = []
    used: set[str] = set()
    for cid, name, phone in rows:
        label = name.strip()
        if counts[label.casefold()] > 1:
            suffix = (phone or "").strip() or f"#{cid}"
            label = f"{label} ({suffix})"
        if label.casefold() in used:
            label = f"{label} #{cid}"
        used.add(label.casefold())
        labels.append(label)
    return labels


def customer_balance(connection, customer_id: int) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(debit-credit),0) FROM customer_ledger WHERE customer_id=?",
        (customer_id,)
    ).fetchone()
    return int(row[0])


def add_customer_ledger_entry(connection, *, customer_id: int, transaction_type: str,
                              debit: int = 0, credit: int = 0,
                              source_type: str | None = None, source_id: int | None = None,
                              user_id: int | None = None, shift_id: int | None = None,
                              notes: str = "", created_at: str | None = None) -> int:
    if debit < 0 or credit < 0 or (debit and credit):
        raise ValueError("Ledger entry must have one non-negative debit or credit")
    timestamp = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = connection.execute("""
        INSERT INTO customer_ledger(customer_id,transaction_type,debit,credit,
            source_type,source_id,user_id,shift_id,created_at,business_date,notes)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (customer_id,transaction_type,debit,credit,source_type,source_id,user_id,
          shift_id,timestamp,business_date_for(timestamp),notes))
    return int(cur.lastrowid)


def collect_customer_payment(connection, *, customer_id: int, amount: int,
                              user_id: int, shift_id: int, notes: str = "") -> int:
    """Collect cash against a customer's debt into the collector's own open shift."""
    if amount <= 0:
        raise ValueError("Collection amount must be positive")
    with write_transaction(connection):   # write lock first: balance check cannot race
        require_active_user(connection, {"id": user_id})
        shift = connection.execute(
            "SELECT id FROM cash_shifts WHERE id=? AND user_id=? AND status='open'",
            (shift_id,user_id)
        ).fetchone()
        if not shift:
            raise ValueError("An open shift owned by this user is required")
        balance = customer_balance(connection, customer_id)
        if balance <= 0:
            raise ValueError("This customer has no outstanding debt")
        if amount > balance:
            raise ValueError("Collection cannot exceed the outstanding customer balance")
        ledger_id = add_customer_ledger_entry(connection, customer_id=customer_id,
            transaction_type="collection", credit=amount, source_type="collection",
            source_id=None, user_id=user_id, shift_id=shift_id,
            notes=notes or "تحصيل نقدي من العميل")
        connection.execute("""
            INSERT INTO cash_movements(shift_id,user_id,movement_type,amount,reason,business_date)
            VALUES(?,?,'إيداع',?,?,?)
        """, (shift_id,user_id,amount,f"تحصيل مديونية عميل #{customer_id}",business_date_for(datetime.now())))
    return ledger_id
