"""Business-day (04:00) boundaries and report reconciliation."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from services.accounting import business_date_for, business_day_bounds
from services.reporting import get_report_snapshot, get_top_products_net, unclassified_returns
from services.sales import PAYMENT_CASH, create_sale, process_return
from helpers import connect, line_ids


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-31 03:59:59", "2026-10-30"),   # still yesterday's trading day
    ("2026-10-31 04:00:00", "2026-10-31"),   # new day starts exactly at 04:00
    ("2026-10-31 00:00:00", "2026-10-30"),   # midnight belongs to the previous day
    ("2026-10-31 23:59:59", "2026-10-31"),
    ("2026-11-01 00:30:00", "2026-10-31"),   # month boundary
    ("2027-01-01 02:00:00", "2026-12-31"),   # year boundary
])
def test_business_date_boundaries(stamp, expected):
    assert business_date_for(stamp) == expected
    assert business_date_for(datetime.fromisoformat(stamp)) == expected


def test_business_day_bounds_are_half_open_and_consistent_with_business_date():
    start, end = business_day_bounds("2026-10-31")
    assert (start, end) == ("2026-10-31 04:00:00", "2026-11-01 04:00:00")
    assert business_date_for(start) == "2026-10-31"
    assert business_date_for("2026-11-01 03:59:59") == "2026-10-31"
    assert business_date_for(end) == "2026-11-01"           # end bound is exclusive
    assert business_day_bounds(date(2026, 10, 31)) == (start, end)
    with pytest.raises(TypeError):
        business_date_for(12345)


def _invoice(con, store_db, created_at, subtotal, discount=0, cost=400, qty=1):
    """Insert an invoice directly (fixed timestamp) the way the service would store it."""
    bd = business_date_for(created_at)
    cur = con.execute(
        "INSERT INTO invoices(invoice_number,created_at,subtotal,discount,total,payment_method,amount_paid,"
        "change_amount,shift_id,user_id,daily_number,business_date) VALUES(?,?,?,?,?,'نقدي',?,0,?,?,?,?)",
        (f"INV-{created_at}", created_at, subtotal, discount, subtotal - discount, subtotal - discount,
         store_db["shift_id"], store_db["user"]["id"], f"{con.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]+1:04d}", bd))
    con.execute("INSERT INTO invoice_items(invoice_id,product_id,product_code,product_name,quantity,unit_price,line_total,unit_cost) "
                "VALUES(?,?,?,?,?,?,?,?)", (cur.lastrowid, store_db["product_id"], "SKU-1", "Test Shirt", qty, subtotal // qty, subtotal, cost))
    con.commit()
    return cur.lastrowid


def test_report_boundaries_inclusive_dates_and_midnight_sales(store_db):
    con = connect(store_db["path"])
    try:
        _invoice(con, store_db, "2026-10-30 03:59:59", 1000)      # belongs to 10-29
        _invoice(con, store_db, "2026-10-30 04:00:00", 2000)      # 10-30
        _invoice(con, store_db, "2026-10-31 01:30:00", 3000)      # 10-30 (after midnight)
        _invoice(con, store_db, "2026-10-31 04:00:00", 4000)      # 10-31
        day = lambda a, b: get_report_snapshot(con, a, b)
        assert day("2026-10-29", "2026-10-29")["sales_total"] == 1000
        assert day("2026-10-30", "2026-10-30")["sales_total"] == 5000          # both sides of calendar midnight
        assert day("2026-10-31", "2026-10-31")["sales_total"] == 4000
        whole = day("2026-10-29", "2026-10-31")
        assert whole["sales_total"] == 10000 == sum(day(d, d)["sales_total"] for d in ("2026-10-29", "2026-10-30", "2026-10-31"))
        assert whole["count"] == 4
        with pytest.raises(ValueError):
            day("2026-10-31", "2026-10-30")
    finally:
        con.close()


def test_net_sales_discounts_returns_and_profit_reconcile(store_db):
    con = connect(store_db["path"]); admin = store_db["user"]
    try:
        sale = create_sale(con, user=admin, cart_lines=[{"product_id": store_db["product_id"], "quantity": 4}],
                           discount=400, payment_method=PAYMENT_CASH, cash_received=3600)   # 4*1000-400, cost 400 each
        process_return(con, user=admin, invoice_id=sale["invoice_id"],
                       item_quantities={line_ids(con, sale["invoice_id"])[0]: 1}, refund_method="نقدي")   # refund 900
        today = sale["business_date"]
        snap = get_report_snapshot(con, today, today)
        assert (snap["subtotal"], snap["discounts"], snap["sales_total"]) == (4000, 400, 3600)
        assert snap["returns_total"] == 900 and snap["net_sales"] == 2700
        assert snap["sold_cost"] == 1600 and snap["returned_cost"] == 400 and snap["net_cost"] == 1200
        assert snap["audited_revenue"] == 2700 and snap["unknown_cost_revenue"] == 0
        assert snap["audited_gross_profit"] == 1500 == snap["net_sales"] - snap["net_cost"]
        # top products: net of returns (3 units), same date semantics as the snapshot
        assert get_top_products_net(con, today, today) == [("Test Shirt", 3, 2700)]
        assert sum(v for _n, _q, v in get_top_products_net(con, today, today, limit=50)) == snap["net_sales"]
        # dashboard numbers == detailed invoice rows
        assert sum(r[5] for r in snap["invoice_rows"]) == snap["sales_total"]
    finally:
        con.close()


def test_unknown_cost_revenue_is_excluded_from_audited_profit(store_db):
    """Regression: lines with unknown cost (-1) used to be counted as 100% margin."""
    con = connect(store_db["path"])
    try:
        _invoice(con, store_db, "2026-10-30 12:00:00", 5000, cost=1000)       # known: revenue 5000, cost 1000
        _invoice(con, store_db, "2026-10-30 13:00:00", 8000, cost=-1)         # unknown cost
        snap = get_report_snapshot(con, "2026-10-30", "2026-10-30")
        assert snap["unknown_cost_units"] == 1
        assert snap["unknown_cost_revenue"] == 8000
        assert snap["audited_revenue"] == 5000 and snap["net_cost"] == 1000
        assert snap["audited_gross_profit"] == 4000                           # not 12000 as the old figure implied
        assert snap["estimated_profit"] == 12000                              # legacy field, flagged by unknown_cost_units
    finally:
        con.close()


def test_returns_use_return_business_date_and_flag_unknown_refunds(store_db):
    con = connect(store_db["path"])
    try:
        inv = _invoice(con, store_db, "2026-10-01 12:00:00", 2000)
        con.execute("INSERT INTO sales_returns(return_number,invoice_id,total_refund,refund_method,business_date,created_at) "
                    "VALUES('R1',?,2000,'unknown','2026-10-05','2026-10-05 12:00:00')", (inv,))
        con.commit()
        assert get_report_snapshot(con, "2026-10-01", "2026-10-01")["returns_total"] == 0
        oct5 = get_report_snapshot(con, "2026-10-05", "2026-10-05")
        assert oct5["returns_total"] == 2000 and oct5["unclassified_return_total"] == 2000
        assert len(unclassified_returns(con)) == 1
    finally:
        con.close()
