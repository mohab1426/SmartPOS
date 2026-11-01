from __future__ import annotations

from datetime import datetime, timedelta, date

BUSINESS_DAY_CUTOFF_HOUR = 4


def business_date_for(value: str | datetime | date, cutoff_hour: int = BUSINESS_DAY_CUTOFF_HOUR) -> str:
    """Return the store's YYYY-MM-DD business date (day begins at 04:00)."""
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raise TypeError("value must be an ISO datetime, datetime, or date")
    day = parsed.date() - timedelta(days=1) if parsed.hour < cutoff_hour else parsed.date()
    return day.isoformat()


def business_day_bounds(day: str | date, cutoff_hour: int = BUSINESS_DAY_CUTOFF_HOUR) -> tuple[str, str]:
    """Return local naive SQL datetime bounds [start, end) for a business day."""
    day_value = date.fromisoformat(day) if isinstance(day, str) else day
    start = datetime.combine(day_value, datetime.min.time()).replace(hour=cutoff_hour)
    end = start + timedelta(days=1)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def calculate_expected_cash(connection, shift_id: int) -> int:
    """Compute drawer cash from net cash tenders, cash refunds and cash movements.

    Legacy cash invoices without a payment row are counted by their net invoice
    total. Historical refunds with unknown tender are intentionally not guessed.
    """
    shift = connection.execute(
        "SELECT opening_cash FROM cash_shifts WHERE id=?", (shift_id,)
    ).fetchone()
    if not shift:
        return 0

    cash_sales = connection.execute("""
        SELECT COALESCE(SUM(
            CASE WHEN EXISTS(SELECT 1 FROM invoice_payments ip WHERE ip.invoice_id=i.id)
                 THEN COALESCE((SELECT SUM(ip.amount) FROM invoice_payments ip
                                WHERE ip.invoice_id=i.id AND ip.tender='cash'),0)
                 WHEN i.payment_method='نقدي' THEN i.total
                 ELSE 0 END
        ),0)
        FROM invoices i WHERE i.shift_id=?
    """, (shift_id,)).fetchone()[0]
    cash_refunds = connection.execute("""
        SELECT COALESCE(SUM(total_refund),0) FROM sales_returns
        WHERE shift_id=? AND refund_method='cash'
    """, (shift_id,)).fetchone()[0]
    movements = connection.execute("""
        SELECT COALESCE(SUM(CASE WHEN movement_type='إيداع' THEN amount ELSE -amount END),0)
        FROM cash_movements WHERE shift_id=?
    """, (shift_id,)).fetchone()[0]
    return int(shift[0] + cash_sales - cash_refunds + movements)


UNKNOWN_COST = -1   # stored in invoice_items.unit_cost / inventory_movements.unit_cost for "cost unknown"


def is_cost_unknown(purchase_price) -> bool:
    """True when a product's recorded cost cannot be trusted as a real cost.

    Without a durable ``cost_known`` column (a schema migration that is deliberately NOT part of this
    change, see docs/INVENTORY_COSTING.md) a recorded cost of zero or less is treated as *unknown*,
    never as "the goods were free".
    """
    return purchase_price is None or int(purchase_price) <= 0


def cost_snapshot(purchase_price) -> int:
    """Cost to snapshot on a sale / stock movement: the real cost, or ``UNKNOWN_COST`` (-1)."""
    return UNKNOWN_COST if is_cost_unknown(purchase_price) else int(purchase_price)


def moving_weighted_average_cost(old_qty: int, old_unit_cost: int,
                                 received_qty: int, received_unit_cost: int) -> int:
    """Calculate integer-piaster moving weighted average with half-up rounding."""
    if min(old_qty, received_qty, old_unit_cost, received_unit_cost) < 0:
        raise ValueError("Quantities and costs must be non-negative")
    if received_qty == 0:
        return int(old_unit_cost)
    if old_qty == 0:
        return int(received_unit_cost)
    total_qty = old_qty + received_qty
    weighted_value = old_qty * old_unit_cost + received_qty * received_unit_cost
    return (weighted_value + total_qty // 2) // total_qty


def unclassified_refunds_for_shift(connection, shift_id: int) -> int:
    """Value of legacy returns whose refund tender cannot be proven for a shift.

    Legacy returns have no recorded refund shift; the original sale's shift is
    used only as a warning association, never as an assumed cash deduction.
    """
    row = connection.execute("""
        SELECT COALESCE(SUM(sr.total_refund),0)
        FROM sales_returns sr LEFT JOIN invoices i ON i.id=sr.invoice_id
        WHERE sr.refund_method='unknown' AND COALESCE(sr.shift_id,i.shift_id)=?
    """, (shift_id,)).fetchone()
    return int(row[0] or 0)
