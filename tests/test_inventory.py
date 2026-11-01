"""Inventory ledger, weighted-average cost, damage, stocktake, atomic purchase receiving."""
from __future__ import annotations

import pytest

from services.accounting import moving_weighted_average_cost as wac
from services.inventory import (adjust_stock, apply_stocktake, get_stock_ledger_balance, record_damage)
from services.purchasing import receive_purchase
from services.reporting import inventory_value, stock_ledger_discrepancies
from services.sales import PAYMENT_CASH, create_sale
from helpers import add_product, connect, stock


def purchase(con, user, pid, qty, cost, **kw):
    return receive_purchase(con, user=user, supplier_id=None, supplier_name="Acme", notes="",
        lines=[{"product_id": pid, "quantity": qty, "cost": cost, "code": "SKU-1", "name": "Test Shirt"}], **kw)


def test_wac_formula_exact_values():
    assert wac(10, 400, 10, 600) == 500                  # (4000+6000)/20
    assert wac(3, 1000, 4, 1500) == 1286                 # 9000/7 = 1285.71 -> half-up
    assert wac(1, 100, 2, 101) == 101                    # 302/3 = 100.67 -> 101 (not floored to 100)
    assert wac(2, 100, 1, 101) == 100                    # 301/3 = 100.33 -> 100
    assert wac(0, 777, 5, 300) == 300                    # zero stock: new cost wins
    assert wac(5, 300, 0, 999) == 300                    # nothing received: unchanged
    for bad in ((-1, 1, 1, 1), (1, -1, 1, 1), (1, 1, -1, 1), (1, 1, 1, -1)):
        with pytest.raises(ValueError):
            wac(*bad)


def test_wac_has_no_systematic_floor_drift():
    """1000 receipts at 1-piaster-different costs must stay within 1 piaster of the exact mean."""
    qty, cost, exact_value = 1, 100, 100
    for i in range(1000):
        unit = 100 + (i % 2)
        cost = wac(qty, cost, 1, unit); qty += 1; exact_value += unit
    assert abs(cost - exact_value / qty) <= 1


def test_purchase_receiving_updates_stock_cost_and_ledger(store_db):
    con = connect(store_db["path"]); admin = store_db["user"]; pid = store_db["product_id"]
    try:
        r = purchase(con, admin, pid, 10, 600)          # on hand 10 @ 400
        assert r["total"] == 6000
        assert stock(con, pid) == 20
        assert con.execute("SELECT purchase_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 500
        purchase(con, admin, pid, 20, 800)              # 20 @ 500 + 20 @ 800 -> 650
        assert con.execute("SELECT purchase_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 650
        assert stock(con, pid) == 40 == get_stock_ledger_balance(con, pid)
        assert con.execute("SELECT COUNT(*) FROM purchase_items").fetchone()[0] == 2
        assert inventory_value(con)["value"] == 40 * 650
        assert stock_ledger_discrepancies(con) == []
    finally:
        con.close()


def test_sale_after_purchase_snapshots_cost_on_invoice_line(store_db):
    con = connect(store_db["path"]); admin = store_db["user"]; pid = store_db["product_id"]
    try:
        purchase(con, admin, pid, 10, 600)
        create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                    payment_method=PAYMENT_CASH, cash_received=1000)
        purchase(con, admin, pid, 10, 2000)             # later cost change must not alter history
        assert con.execute("SELECT unit_cost FROM invoice_items").fetchone()[0] == 500
    finally:
        con.close()


def test_purchase_is_atomic_when_a_later_line_fails(store_db):
    con = connect(store_db["path"]); admin = store_db["user"]; pid = store_db["product_id"]
    try:
        with pytest.raises(ValueError):
            receive_purchase(con, user=admin, supplier_id=None, supplier_name="", notes="", lines=[
                {"product_id": pid, "quantity": 5, "cost": 700, "code": "a", "name": "a"},
                {"product_id": 99999, "quantity": 1, "cost": 1, "code": "b", "name": "b"}])
        assert stock(con, pid) == 10
        assert con.execute("SELECT purchase_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 400
        assert con.execute("SELECT COUNT(*) FROM purchase_invoices").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM purchase_items").fetchone()[0] == 0
        with pytest.raises(ValueError):
            purchase(con, admin, pid, 0, 100)
        with pytest.raises(ValueError):
            purchase(con, admin, pid, 1, -1)
    finally:
        con.close()


def test_purchase_can_create_new_product_at_receipt(store_db):
    con = connect(store_db["path"]); admin = store_db["user"]
    try:
        new = {"t1": {"code": "NEW-1", "barcode": None, "name": "Blouse", "size": "M", "color": "Red",
                      "category": "Tops", "selling": 9000, "minimum": 2}}
        receive_purchase(con, user=admin, supplier_id=None, supplier_name="", notes="n", new_products=new,
                         lines=[{"product_id": None, "temp_id": "t1", "quantity": 4, "cost": 4000, "code": "NEW-1", "name": "Blouse"}])
        pid = con.execute("SELECT id FROM products WHERE code='NEW-1'").fetchone()[0]
        assert stock(con, pid) == 4 and get_stock_ledger_balance(con, pid) == 4
        assert con.execute("SELECT purchase_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 4000
    finally:
        con.close()


def test_manual_adjustment_damage_and_negative_stock_rules(store_db):
    con = connect(store_db["path"]); admin = store_db["user"]; pid = store_db["product_id"]
    try:
        assert adjust_stock(con, product_id=pid, new_quantity=7, user_id=admin["id"], reason="recount") == -3
        with pytest.raises(ValueError, match="reason"):
            adjust_stock(con, product_id=pid, new_quantity=8, user_id=admin["id"], reason="  ")
        with pytest.raises(ValueError, match="negative"):
            adjust_stock(con, product_id=pid, new_quantity=-1, user_id=admin["id"], reason="oops")
        record_damage(con, product_id=pid, quantity=2, user_id=admin["id"], reason="torn in storage")
        assert stock(con, pid) == 5
        with pytest.raises(ValueError, match="exceeds"):
            record_damage(con, product_id=pid, quantity=6, user_id=admin["id"], reason="too many")
        with pytest.raises(ValueError, match="reason"):
            record_damage(con, product_id=pid, quantity=1, user_id=admin["id"], reason="")
        movements = con.execute("SELECT reference_type,quantity_delta,user_id,reason FROM inventory_movements "
                                "WHERE movement_type='adjustment' ORDER BY id").fetchall()
        assert movements == [("manual_adjustment", -3, admin["id"], "recount"),
                             ("damage", -2, admin["id"], "torn in storage")]
        assert stock_ledger_discrepancies(con) == []
    finally:
        con.close()


def test_stocktake_applies_only_variances_atomically(store_db):
    con = connect(store_db["path"]); admin = store_db["user"]; p1 = store_db["product_id"]
    p2 = add_product(con, stock=5)
    try:
        variances = apply_stocktake(con, counts={p1: 10, p2: 3}, user_id=admin["id"], reason="Oct cycle count")
        assert variances == [{"product_id": p2, "system": 5, "counted": 3, "delta": -2}]
        assert stock(con, p2) == 3 and stock(con, p1) == 10
        before = stock(con, p1)
        with pytest.raises(ValueError):
            apply_stocktake(con, counts={p1: 1, 987654: 2}, user_id=admin["id"], reason="bad")
        assert stock(con, p1) == before                   # first line rolled back with the failing second
        with pytest.raises(ValueError):
            apply_stocktake(con, counts={p1: -1}, user_id=admin["id"], reason="bad")
        with pytest.raises(ValueError):
            apply_stocktake(con, counts={p1: 1}, user_id=admin["id"], reason="")
        assert stock_ledger_discrepancies(con) == []
    finally:
        con.close()


def test_inventory_ledger_is_append_only(store_db):
    con = connect(store_db["path"])
    try:
        with pytest.raises(Exception, match="append-only"):
            con.execute("UPDATE inventory_movements SET quantity_delta=99")
        with pytest.raises(Exception, match="append-only"):
            con.execute("DELETE FROM inventory_movements")
    finally:
        con.close()
