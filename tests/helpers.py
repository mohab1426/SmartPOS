"""Shared helpers for service-level tests (plain sqlite3 connections, FK enforcement on)."""
from __future__ import annotations

import sqlite3


def connect(path):
    con = sqlite3.connect(path, timeout=20)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def add_product(con, code="SKU-2", name="Second Item", cost=500, price=1200, stock=5):
    pid = con.execute(
        "INSERT INTO products(code,name,purchase_price,selling_price,stock_quantity,min_stock) VALUES(?,?,?,?,?,?)",
        (code, name, cost, price, stock, 0)).lastrowid
    if stock:
        con.execute("INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,unit_cost,"
                    "reference_type,reference_id,reason) VALUES(?,'opening_balance',?,?,'test_fixture',?,'fixture')",
                    (pid, stock, cost, pid))
    con.commit()
    return pid


def stock(con, pid):
    return con.execute("SELECT stock_quantity FROM products WHERE id=?", (pid,)).fetchone()[0]


def line_ids(con, invoice_id):
    return [r[0] for r in con.execute("SELECT id FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,))]


def ensure_customer(con, name, phone=""):
    """Test helper: the id of the single customer called ``name`` (created explicitly if missing)."""
    from services.customers import create_customer, find_customers_by_name
    found = find_customers_by_name(con, name)
    if len(found) > 1:
        raise AssertionError(f"ambiguous test customer {name!r}")
    if found:
        return found[0]["id"]
    cid = create_customer(con, name, phone)
    con.commit()
    return cid
