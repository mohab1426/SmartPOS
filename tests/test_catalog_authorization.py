"""Product / category / supplier maintenance: admin-only at the service layer, audited with old/new."""
from __future__ import annotations

import json

import pytest

from helpers import connect
from services import catalog
from services.sales import PAYMENT_CASH, create_sale

ARGS = dict(code="NEW-1", name="New Item", purchase_price=300, selling_price=900, quantity=4, min_stock=1)


def _audit(con, action):
    return [json.loads(r[0] or "{}") for r in con.execute(
        "SELECT details FROM audit_log WHERE action=? ORDER BY id", (action,))]


def test_cashier_cannot_use_any_catalog_operation(store_db, cashier):
    con = connect(store_db["path"])
    pid = store_db["product_id"]
    calls = [
        lambda: catalog.create_product(con, actor=cashier, **ARGS),
        lambda: catalog.update_product(con, actor=cashier, product_id=pid, code="SKU-1", barcode=None,
                                       name="Test Shirt", size="", color="", category="عام",
                                       selling_price=1, min_stock=1),
        lambda: catalog.delete_product(con, actor=cashier, product_id=pid),
        lambda: catalog.add_category(con, actor=cashier, name="X"),
        lambda: catalog.rename_category(con, actor=cashier, old_name="X", new_name="Y"),
        lambda: catalog.delete_category(con, actor=cashier, name="X"),
        lambda: catalog.save_supplier(con, actor=cashier, name="S"),
        lambda: catalog.delete_supplier(con, actor=cashier, supplier_id=1),
    ]
    for call in calls:
        with pytest.raises(PermissionError):
            call()
    # a forged role in the UI dict must not help: the role is re-read from the database
    forged = dict(cashier, role="admin")
    with pytest.raises(PermissionError):
        catalog.create_product(con, actor=forged, **ARGS)
    assert con.execute("SELECT selling_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 1000
    con.close()


def test_price_change_is_audited_with_old_and_new_values(store_db):
    con = connect(store_db["path"])
    pid, admin = store_db["product_id"], store_db["user"]
    result = catalog.update_product(con, actor=admin, product_id=pid, code="SKU-1", barcode=None,
                                    name="Test Shirt", size="", color="", category="عام",
                                    selling_price=1500, min_stock=1)
    assert result["changes"]["selling_price"] == [1000, 1500]
    assert _audit(con, "product_price_changed")[-1] == {"old": 1000, "new": 1500, "code": "SKU-1"}
    assert con.execute("SELECT selling_price FROM products WHERE id=?", (pid,)).fetchone()[0] == 1500
    con.close()


def test_stock_change_needs_reason_and_creates_movement(store_db):
    con = connect(store_db["path"])
    pid, admin = store_db["product_id"], store_db["user"]
    base = dict(actor=admin, product_id=pid, code="SKU-1", barcode=None, name="Test Shirt", size="",
                color="", category="عام", selling_price=1000, min_stock=1)
    with pytest.raises(ValueError, match="سبب"):
        catalog.update_product(con, new_quantity=7, stock_reason="  ", **base)
    assert con.execute("SELECT stock_quantity FROM products WHERE id=?", (pid,)).fetchone()[0] == 10
    catalog.update_product(con, new_quantity=7, stock_reason="جرد", **base)
    assert con.execute("SELECT stock_quantity FROM products WHERE id=?", (pid,)).fetchone()[0] == 7
    delta = con.execute("SELECT quantity_delta,reason FROM inventory_movements WHERE product_id=? "
                        "AND movement_type='adjustment'", (pid,)).fetchall()
    assert delta == [(-3, "جرد")]
    con.close()


def test_product_lifecycle_and_delete_protection(store_db):
    con = connect(store_db["path"])
    admin = store_db["user"]
    pid = catalog.create_product(con, actor=admin, **ARGS)
    assert con.execute("SELECT stock_quantity FROM products WHERE id=?", (pid,)).fetchone()[0] == 4
    with pytest.raises(ValueError, match="مرتبط"):       # has an opening-balance movement
        catalog.delete_product(con, actor=admin, product_id=pid)
    empty = catalog.create_product(con, actor=admin, **dict(ARGS, code="NEW-2", quantity=0))
    catalog.delete_product(con, actor=admin, product_id=empty)
    assert con.execute("SELECT COUNT(*) FROM products WHERE id=?", (empty,)).fetchone()[0] == 0
    assert _audit(con, "product_deleted")[-1]["code"] == "NEW-2"
    with pytest.raises(Exception):                        # duplicate product code
        catalog.create_product(con, actor=admin, **ARGS)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


def test_categories_and_suppliers(store_db):
    con = connect(store_db["path"])
    admin = store_db["user"]
    catalog.add_category(con, actor=admin, name="أحذية")
    with pytest.raises(ValueError, match="موجود"):
        catalog.add_category(con, actor=admin, name="أحذية")
    catalog.rename_category(con, actor=admin, old_name="أحذية", new_name="شنط")
    with pytest.raises(ValueError):
        catalog.rename_category(con, actor=admin, old_name="عام", new_name="Z")
    with pytest.raises(ValueError):
        catalog.delete_category(con, actor=admin, name="عام")
    catalog.delete_category(con, actor=admin, name="شنط")
    sid = catalog.save_supplier(con, actor=admin, name="مورد أ", phone="1")
    catalog.save_supplier(con, actor=admin, name="مورد أ 2", supplier_id=sid)
    with pytest.raises(ValueError, match="بنفس الاسم"):
        catalog.save_supplier(con, actor=admin, name="مورد أ 2")
    assert _audit(con, "supplier_updated")[-1]["old"][0] == "مورد أ"
    catalog.delete_supplier(con, actor=admin, supplier_id=sid)
    con.close()


def test_sold_product_cannot_be_deleted(store_db):
    con = connect(store_db["path"])
    admin = store_db["user"]
    create_sale(con, user=admin, cart_lines=[{"product_id": store_db["product_id"], "quantity": 1}],
                discount=0, payment_method=PAYMENT_CASH, cash_received=1000)
    with pytest.raises(ValueError, match="مرتبط"):
        catalog.delete_product(con, actor=admin, product_id=store_db["product_id"])
    con.close()
