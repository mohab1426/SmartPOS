"""Unknown historical product cost: never treated as a real zero cost (audit F-03 / accounting policy).

Policy (no schema change): a product holding stock whose recorded cost is <= 0 has an UNKNOWN cost.
Sales snapshot -1, reports exclude those lines from profit, and purchases on top of such stock need an
explicit, audited administrator opening cost.
"""
from __future__ import annotations

import json

import pytest

from services.accounting import cost_snapshot, is_cost_unknown
from services.inventory import UnknownCostError, products_with_unknown_cost
from services.purchasing import receive_purchase
from services.reporting import get_report_snapshot, inventory_value
from services.sales import PAYMENT_CASH, create_sale
from helpers import add_product, connect, stock


def _zero_cost_product(con, stock_qty=8, code="UNK-1"):
    return add_product(con, code=code, name="Unknown Cost Item", cost=0, price=1000, stock=stock_qty)


def _buy(con, user, pid, qty=8, cost=5000, **kw):
    return receive_purchase(con, user=user, supplier_id=None, supplier_name="Acme", notes="",
                            lines=[{"product_id": pid, "quantity": qty, "cost": cost, "code": "X", "name": "X"}], **kw)


def _cost(con, pid):
    return con.execute("SELECT purchase_price FROM products WHERE id=?", (pid,)).fetchone()[0]


def test_helpers_classify_cost():
    assert [is_cost_unknown(v) for v in (None, -1, 0, 1, 500)] == [True, True, True, False, False]
    assert [cost_snapshot(v) for v in (None, -1, 0, 1, 500)] == [-1, -1, -1, 1, 500]


def test_positive_stock_with_unknown_cost_is_listed_and_known_cost_stock_is_not(store_db):
    con = connect(store_db["path"])
    unknown = _zero_cost_product(con)
    _zero_cost_product(con, stock_qty=0, code="UNK-EMPTY")           # no stock: nothing to value
    listed = products_with_unknown_cost(con)
    assert [p["id"] for p in listed] == [unknown]                    # store_db's SKU-1 has cost 400
    value = inventory_value(con)
    assert value["unknown_cost_units"] == 8 and value["zero_cost_units"] == 8
    assert value["value"] == 10 * 400                                # unknown units are NOT valued at 0 silently
    assert [p["id"] for p in value["unknown_cost_products"]] == [unknown]
    con.close()


def test_sale_of_unknown_cost_stock_snapshots_unknown_not_zero(store_db):
    con = connect(store_db["path"])
    pid = _zero_cost_product(con)
    create_sale(con, user=store_db["user"], cart_lines=[{"product_id": pid, "quantity": 2}], discount=0,
                payment_method=PAYMENT_CASH, cash_received=2000)
    assert con.execute("SELECT unit_cost FROM invoice_items").fetchone()[0] == -1
    assert con.execute("SELECT unit_cost FROM inventory_movements WHERE reference_type='invoice_item'").fetchone()[0] == -1
    con.close()


def test_profit_excludes_unknown_cost_lines_and_keeps_known_ones(store_db):
    con = connect(store_db["path"])
    admin, known_pid = store_db["user"], store_db["product_id"]
    unknown_pid = _zero_cost_product(con)
    create_sale(con, user=admin, cart_lines=[{"product_id": known_pid, "quantity": 1}], discount=0,
                payment_method=PAYMENT_CASH, cash_received=1000)           # revenue 1000, cost 400
    create_sale(con, user=admin, cart_lines=[{"product_id": unknown_pid, "quantity": 3}], discount=0,
                payment_method=PAYMENT_CASH, cash_received=3000)           # revenue 3000, cost unknown
    snap = get_report_snapshot(con, "2000-01-01", "2999-12-31")
    assert snap["unknown_cost_units"] == 3 and snap["unknown_cost_revenue"] == 3000
    assert snap["audited_revenue"] == 1000 and snap["audited_gross_profit"] == 600   # 1000 - 400
    assert [p["id"] for p in snap["unknown_cost_products"]] == [unknown_pid]
    con.close()


def test_legacy_zero_cost_invoice_lines_are_not_trusted_as_free(store_db):
    """A historical line stored with unit_cost 0 is reported as unknown-cost, not 100% margin."""
    con = connect(store_db["path"])
    admin, pid = store_db["user"], store_db["product_id"]
    sale = create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 2}], discount=0,
                       payment_method=PAYMENT_CASH, cash_received=2000)
    con.execute("DROP TRIGGER IF EXISTS trg_invoice_items_no_update")
    con.execute("UPDATE invoice_items SET unit_cost=0 WHERE invoice_id=?", (sale["invoice_id"],))
    con.commit()
    snap = get_report_snapshot(con, "2000-01-01", "2999-12-31")
    assert snap["unknown_cost_units"] == 2 and snap["audited_revenue"] == 0 and snap["unknown_cost_revenue"] == 2000
    con.close()


def test_purchase_on_unknown_cost_stock_is_refused_and_writes_nothing(store_db):
    con = connect(store_db["path"])
    pid = _zero_cost_product(con)
    with pytest.raises(UnknownCostError) as err:
        _buy(con, store_db["user"], pid)
    assert [p["id"] for p in err.value.products] == [pid]
    assert stock(con, pid) == 8 and _cost(con, pid) == 0              # NOT averaged to 2500
    assert con.execute("SELECT COUNT(*) FROM purchase_invoices").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM purchase_items").fetchone()[0] == 0
    con.close()


def test_purchase_with_explicit_opening_cost_uses_it_and_is_audited(store_db):
    con = connect(store_db["path"])
    admin = store_db["user"]
    pid = _zero_cost_product(con)
    _buy(con, admin, pid, qty=8, cost=5000, opening_costs={pid: 3000})
    assert stock(con, pid) == 16 and _cost(con, pid) == 4000          # (8*3000 + 8*5000) / 16
    row = con.execute("SELECT details FROM audit_log WHERE action='unknown_cost_resolved'").fetchone()
    assert json.loads(row[0])["admin_opening_unit_cost"] == 3000
    con.close()


def test_opening_cost_must_be_positive(store_db):
    con = connect(store_db["path"])
    pid = _zero_cost_product(con)
    for bad in (0, -5):
        with pytest.raises(ValueError):
            _buy(con, store_db["user"], pid, opening_costs={pid: bad})
    assert stock(con, pid) == 8 and con.execute("SELECT COUNT(*) FROM purchase_invoices").fetchone()[0] == 0
    con.close()


def test_first_purchase_with_known_cost_on_empty_stock_needs_no_resolution(store_db):
    con = connect(store_db["path"])
    pid = _zero_cost_product(con, stock_qty=0)
    _buy(con, store_db["user"], pid, qty=8, cost=5000)
    assert stock(con, pid) == 8 and _cost(con, pid) == 5000
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='unknown_cost_resolved'").fetchone()[0] == 0
    con.close()


def test_normal_known_cost_purchase_is_unchanged(store_db):
    con = connect(store_db["path"])
    pid = store_db["product_id"]                                      # 10 @ 400
    _buy(con, store_db["user"], pid, qty=10, cost=600)
    assert _cost(con, pid) == 500 and stock(con, pid) == 20
    con.close()


def test_same_unknown_product_on_two_lines_is_resolved_and_audited_once(store_db):
    con = connect(store_db["path"])
    pid = _zero_cost_product(con)
    receive_purchase(con, user=store_db["user"], supplier_id=None, supplier_name="", notes="",
        lines=[{"product_id": pid, "quantity": 2, "cost": 5000, "code": "X", "name": "X"},
               {"product_id": pid, "quantity": 2, "cost": 6000, "code": "X", "name": "X"}], opening_costs={pid: 4000})
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='unknown_cost_resolved'").fetchone()[0] == 1
    assert stock(con, pid) == 12
    con.close()
