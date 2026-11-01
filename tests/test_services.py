from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta

import pytest

import database
from services.accounting import (
    business_date_for,
    calculate_expected_cash,
    moving_weighted_average_cost,
    unclassified_refunds_for_shift,
)
from services.backup import create_database_backup, restore_database_file, validate_database_file
from services.customers import customer_balance, collect_customer_payment
from services.inventory import receive_purchase_line, get_stock_ledger_balance
from services.reporting import get_report_snapshot
from services.sales import (
    PAYMENT_CASH,
    PAYMENT_CREDIT,
    PAYMENT_SPLIT,
    create_sale,
    process_return,
    classify_legacy_return,
)
from services.security import is_admin, require_admin_user


def connect(path):
    con = sqlite3.connect(path, timeout=20)
    con.row_factory = None
    con.execute("PRAGMA foreign_keys=ON")
    return con


def test_business_day_boundary():
    assert business_date_for("2026-10-31 03:59:59") == "2026-10-30"
    assert business_date_for("2026-10-31 04:00:00") == "2026-10-31"
    assert business_date_for("2026-10-31 15:30:00") == "2026-10-31"


def test_wac_half_up_and_zero_stock():
    assert moving_weighted_average_cost(2, 500, 2, 1000) == 750
    assert moving_weighted_average_cost(1, 100, 1, 101) == 101
    assert moving_weighted_average_cost(0, 500, 3, 875) == 875
    with pytest.raises(ValueError):
        moving_weighted_average_cost(-1, 500, 1, 500)


def test_cash_sale_and_return_reconcile_drawer_and_stock(store_db):
    con = connect(store_db["path"])
    try:
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 2, "unit_price": 1000}],
            discount=0, payment_method=PAYMENT_CASH, cash_received=2500)
        assert sale["total"] == 2000
        assert sale["change"] == 500
        assert calculate_expected_cash(con, store_db["shift_id"]) == 12000
        invoice_item_id = con.execute("SELECT id FROM invoice_items WHERE invoice_id=?", (sale["invoice_id"],)).fetchone()[0]
        returned = process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
            item_quantities={invoice_item_id: 1}, refund_method="نقدي")
        assert returned["refund_total"] == 1000
        assert calculate_expected_cash(con, store_db["shift_id"]) == 11000
        assert con.execute("SELECT stock_quantity FROM products WHERE id=?", (store_db["product_id"],)).fetchone()[0] == 9
        assert get_stock_ledger_balance(con, store_db["product_id"]) == 9
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_credit_and_split_sales_are_real_receivables(store_db):
    con = connect(store_db["path"])
    try:
        credit = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 1, "unit_price": 1000}],
            discount=0, payment_method=PAYMENT_CREDIT, cash_received=0, new_customer={"name": "Customer A"})
        assert credit["credit_amount"] == 1000
        assert customer_balance(con, credit["customer_id"]) == 1000
        assert calculate_expected_cash(con, store_db["shift_id"]) == 10000

        split = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 1, "unit_price": 1000}],
            discount=0, payment_method=PAYMENT_SPLIT, cash_received=400, selected_customer_id=credit["customer_id"])
        assert split["cash_amount"] == 400
        assert split["credit_amount"] == 600
        assert customer_balance(con, credit["customer_id"]) == 1600
        assert calculate_expected_cash(con, store_db["shift_id"]) == 10400
        with pytest.raises(ValueError, match="طريقة الدفع غير مدعومة"):
            create_sale(con, user=store_db["user"],
                cart_lines=[{"product_id": store_db["product_id"], "quantity": 1}],
                discount=0, payment_method="Visa", cash_received=1000)
    finally:
        con.close()


def test_debt_collection_reduces_receivable_and_increases_cash(store_db):
    con = connect(store_db["path"])
    try:
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 2}],
            discount=0, payment_method=PAYMENT_CREDIT, cash_received=0, new_customer={"name": "Customer B"})
        collect_customer_payment(con, customer_id=sale["customer_id"], amount=750,
            user_id=store_db["user"]["id"], shift_id=store_db["shift_id"], notes="partial payment")
        assert customer_balance(con, sale["customer_id"]) == 1250
        assert calculate_expected_cash(con, store_db["shift_id"]) == 10750
        with pytest.raises(ValueError, match="exceed"):
            collect_customer_payment(con, customer_id=sale["customer_id"], amount=2000,
                user_id=store_db["user"]["id"], shift_id=store_db["shift_id"])
    finally:
        con.close()


def test_split_cash_refund_cannot_exceed_original_cash_tender(store_db):
    con = connect(store_db["path"])
    try:
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 2}],
            discount=0, payment_method=PAYMENT_SPLIT, cash_received=1000, new_customer={"name": "Customer C"})
        item_id = con.execute("SELECT id FROM invoice_items WHERE invoice_id=?", (sale["invoice_id"],)).fetchone()[0]
        with pytest.raises(ValueError, match="تتجاوز المبلغ النقدي"):
            process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                item_quantities={item_id: 2}, refund_method="نقدي")
        assert con.execute("SELECT COUNT(*) FROM sales_returns").fetchone()[0] == 0
        credit_return = process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
            item_quantities={item_id: 2}, refund_method="رصيد العميل")
        assert credit_return["refund_total"] == 2000
        assert customer_balance(con, sale["customer_id"]) == -1000
    finally:
        con.close()


def test_inventory_purchase_uses_weighted_average_and_ledger(store_db):
    con = connect(store_db["path"])
    try:
        cur = con.execute("INSERT INTO purchase_invoices(invoice_number,total) VALUES('PUR-TEST',4000)")
        purchase_id = cur.lastrowid
        new_cost = receive_purchase_line(con, purchase_invoice_id=purchase_id,
            product_id=store_db["product_id"], product_code="SKU-1", product_name="Test Shirt",
            quantity=2, unit_cost=1000, user_id=store_db["user"]["id"])
        assert new_cost == 500
        assert con.execute("SELECT purchase_price,stock_quantity FROM products WHERE id=?", (store_db["product_id"],)).fetchone() == (500,12)
        assert get_stock_ledger_balance(con, store_db["product_id"]) == 12
        assert con.execute("SELECT COUNT(*) FROM purchase_items WHERE purchase_invoice_id=?", (purchase_id,)).fetchone()[0] == 1
    finally:
        con.close()


def test_report_products_are_net_of_returns(store_db):
    con = connect(store_db["path"])
    try:
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 3}],
            discount=0, payment_method=PAYMENT_CASH, cash_received=3000)
        item_id = con.execute("SELECT id FROM invoice_items WHERE invoice_id=?", (sale["invoice_id"],)).fetchone()[0]
        process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
            item_quantities={item_id: 1}, refund_method="نقدي")
        snapshot = get_report_snapshot(con, sale["business_date"], sale["business_date"])
        assert snapshot["top_products"][0][1] == 2
        assert snapshot["top_products"][0][2] == 2000
        assert snapshot["returns_total"] == 1000
    finally:
        con.close()


def test_unknown_legacy_return_is_warned_and_must_be_classified(store_db):
    con = connect(store_db["path"])
    try:
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 2}],
            discount=0, payment_method=PAYMENT_CASH, cash_received=2000)
        item_id = con.execute("SELECT id FROM invoice_items WHERE invoice_id=?", (sale["invoice_id"],)).fetchone()[0]
        cur = con.execute("""INSERT INTO sales_returns(return_number,invoice_id,total_refund,refund_method)
            VALUES('RET-LEGACY',?,500,'unknown')""", (sale["invoice_id"],))
        return_id = cur.lastrowid
        con.execute("INSERT INTO sales_return_items(return_id,invoice_item_id,product_id,product_name,quantity,unit_refund,line_refund) VALUES(?,?,?,?,1,500,500)",
                    (return_id,item_id,store_db["product_id"],"Test Shirt"))
        con.commit()
        assert unclassified_refunds_for_shift(con, store_db["shift_id"]) == 500
        with pytest.raises(ValueError, match="مرتجعات قديمة"):
            process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                item_quantities={item_id: 1}, refund_method="نقدي")
        classify_legacy_return(con, admin_user=store_db["user"], return_id=return_id,
            refund_method="نقدي", shift_id=store_db["shift_id"])
        assert unclassified_refunds_for_shift(con, store_db["shift_id"]) == 0
        assert calculate_expected_cash(con, store_db["shift_id"]) == 11500
        assert con.execute("SELECT refund_method FROM sales_returns WHERE id=?",(return_id,)).fetchone()[0] == "cash"
    finally:
        con.close()


def test_cashier_cannot_classify_legacy_return_or_restore(store_db, tmp_path):
    con = connect(store_db["path"])
    try:
        with pytest.raises(PermissionError):
            require_admin_user({"id": 2, "role": "cashier", "active": 1})
        backup_path = create_database_backup(store_db["path"], tmp_path / "backups")
        with pytest.raises(PermissionError):
            restore_database_file(backup_path, store_db["path"], tmp_path / "safety",
                                 {"id": 2, "role": "cashier", "active": 1}, admin_password="x")
        assert is_admin(store_db["user"]) is True
        validate_database_file(backup_path)
    finally:
        con.close()


def test_backup_restore_creates_safety_snapshot(store_db, tmp_path):
    con = connect(store_db["path"])
    con.execute("UPDATE products SET selling_price=1234 WHERE id=?", (store_db["product_id"],))
    con.commit(); con.close()
    backup = create_database_backup(store_db["path"], tmp_path / "backups")
    con = connect(store_db["path"])
    con.execute("UPDATE products SET selling_price=2345 WHERE id=?", (store_db["product_id"],))
    con.commit(); con.close()
    safety = restore_database_file(backup, store_db["path"], tmp_path / "safety", store_db["user"],
                                   admin_password=store_db["admin_password"])
    assert safety.exists()
    con = connect(store_db["path"])
    try:
        assert con.execute("SELECT selling_price FROM products WHERE id=?", (store_db["product_id"],)).fetchone()[0] == 1234
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_migration_is_idempotent_and_quarantines_pending_copy(store_db, tmp_path, monkeypatch):
    # Simulate an old installation folder with its DB and an unsafe pending restore.
    legacy = store_db["legacy_dir"]
    legacy.mkdir(parents=True, exist_ok=True)
    source = store_db["path"]
    shutil.copy2(source, legacy / "smartpos.db")
    shutil.copy2(source, legacy / "smartpos_restore_pending.db")
    store_db["path"].unlink()
    monkeypatch.setattr(database, "_STORAGE_READY", False)
    database.ensure_storage_layout()
    assert store_db["path"].exists()
    assert list((store_db["data_dir"] / "quarantine").glob("*.quarantined.db"))
    database.initialize_database()
    database.initialize_database()
    con = connect(store_db["path"])
    try:
        assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 8
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()


def test_append_only_ledgers_are_protected(store_db):
    con = connect(store_db["path"])
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("UPDATE inventory_movements SET reason='changed' WHERE product_id=?", (store_db["product_id"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM inventory_movements WHERE product_id=?", (store_db["product_id"],))
    finally:
        con.close()



def test_discount_is_enforced_by_service_not_only_the_gui(store_db):
    con = connect(store_db["path"])
    try:
        con.execute("INSERT INTO users(username,display_name,password_hash,role,active) VALUES('cashier','Cashier','hash','cashier',1)")
        cashier_id = con.execute("SELECT id FROM users WHERE username='cashier'").fetchone()[0]
        con.commit()
        cashier = {"id": cashier_id, "username": "cashier", "role": "admin", "active": 1}
        with pytest.raises(PermissionError, match="الخصم متاح للمدير فقط"):
            create_sale(con, user=cashier,
                cart_lines=[{"product_id": store_db["product_id"], "quantity": 1}],
                discount=100, payment_method=PAYMENT_CASH, cash_received=900)
        con.execute("UPDATE users SET active=0 WHERE id=?", (store_db["user"]["id"],))
        con.commit()
        inactive = {**store_db["user"], "active": 1}
        with pytest.raises(PermissionError, match="غير نشط"):
            create_sale(con, user=inactive,
                cart_lines=[{"product_id": store_db["product_id"], "quantity": 1}],
                discount=0, payment_method=PAYMENT_CASH, cash_received=1000)
        assert con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
    finally:
        con.close()


def test_classifying_historical_cash_refund_reconciles_closed_shift(store_db):
    con = connect(store_db["path"])
    try:
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 2}],
            discount=0, payment_method=PAYMENT_CASH, cash_received=2000)
        item_id = con.execute("SELECT id FROM invoice_items WHERE invoice_id=?", (sale["invoice_id"],)).fetchone()[0]
        return_id = con.execute("""INSERT INTO sales_returns(return_number,invoice_id,total_refund,refund_method)
            VALUES('RET-CLOSED-LEGACY',?,500,'unknown')""", (sale["invoice_id"],)).lastrowid
        con.execute("""INSERT INTO sales_return_items(return_id,invoice_item_id,product_id,product_name,
            quantity,unit_refund,line_refund) VALUES(?,?,?,?,1,500,500)""",
            (return_id,item_id,store_db["product_id"],"Test Shirt"))
        con.execute("UPDATE cash_shifts SET status='closed',closed_at='2026-10-30 17:00:00',expected_cash=12000,actual_cash=11500,difference=-500 WHERE id=?", (store_db["shift_id"],))
        con.commit()
        result = classify_legacy_return(con, admin_user=store_db["user"], return_id=return_id,
            refund_method="نقدي", shift_id=store_db["shift_id"])
        assert result["old_expected_cash"] == 12000
        assert result["new_expected_cash"] == 11500
        expected, actual, difference = con.execute(
            "SELECT expected_cash,actual_cash,difference FROM cash_shifts WHERE id=?",
            (store_db["shift_id"],)).fetchone()
        assert (expected, actual, difference) == (11500, 11500, 0)
    finally:
        con.close()


def test_net_product_report_subtracts_returns_in_return_period(store_db):
    con = connect(store_db["path"])
    try:
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 2}],
            discount=0, payment_method=PAYMENT_CASH, cash_received=2000)
        sold_day = (datetime.fromisoformat(sale["business_date"]).date() - timedelta(days=1)).isoformat()
        con.execute("UPDATE invoices SET business_date=? WHERE id=?", (sold_day,sale["invoice_id"]))
        con.commit()
        item_id = con.execute("SELECT id FROM invoice_items WHERE invoice_id=?", (sale["invoice_id"],)).fetchone()[0]
        process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
            item_quantities={item_id: 1}, refund_method="نقدي")
        report = get_report_snapshot(con, sale["business_date"], sale["business_date"])
        assert report["top_products"][0][1:] == (-1, -1000)
    finally:
        con.close()


def test_new_database_invariants_reject_invalid_return_and_invoice_math(store_db):
    con = connect(store_db["path"])
    try:
        with pytest.raises(sqlite3.IntegrityError, match="subtotal minus discount"):
            con.execute("""INSERT INTO invoices(invoice_number,subtotal,discount,total,payment_method)
                VALUES('BAD-MATH',1000,100,950,'نقدي')""")
        con.rollback()
        sale = create_sale(con, user=store_db["user"],
            cart_lines=[{"product_id": store_db["product_id"], "quantity": 1}],
            discount=0, payment_method=PAYMENT_CASH, cash_received=1000)
        item_id = con.execute("SELECT id,product_id FROM invoice_items WHERE invoice_id=?", (sale["invoice_id"],)).fetchone()
        return_id = con.execute("INSERT INTO sales_returns(return_number,invoice_id,total_refund,refund_method) VALUES('RET-VALIDATE',?,0,'unknown')", (sale["invoice_id"],)).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="returned quantity exceeds sold quantity"):
            con.execute("""INSERT INTO sales_return_items(return_id,invoice_item_id,product_id,product_name,
                quantity,unit_refund,line_refund) VALUES(?,?,?,?,2,1000,2000)""",
                (return_id,item_id[0],item_id[1],"Test Shirt"))
        con.rollback()
    finally:
        con.close()


def test_get_connection_context_manager_closes_sqlite_handle(store_db):
    connection = database.get_connection()
    with connection as active_connection:
        assert active_connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_inventory_mutations_require_live_admin(store_db):
    con = connect(store_db["path"])
    try:
        con.execute(
            "INSERT INTO users(username,display_name,password_hash,role,active) VALUES(?,?,?,?,1)",
            ("cashier_inv", "Cashier", "test-hash", "cashier"),
        )
        cashier_id = con.execute("SELECT id FROM users WHERE username='cashier_inv'").fetchone()[0]
        with pytest.raises(PermissionError):
            from services.inventory import adjust_stock
            adjust_stock(con, product_id=store_db["product_id"], new_quantity=8,
                         user_id=cashier_id, reason="should be rejected")
        purchase_id = con.execute("INSERT INTO purchase_invoices(invoice_number,total) VALUES('PUR-DENIED',1000)").lastrowid
        with pytest.raises(PermissionError):
            receive_purchase_line(con, purchase_invoice_id=purchase_id,
                product_id=store_db["product_id"], product_code="SKU-1", product_name="Test Shirt",
                quantity=1, unit_cost=1000, user_id=cashier_id)
        assert con.execute("SELECT stock_quantity FROM products WHERE id=?", (store_db["product_id"],)).fetchone()[0] == 10
    finally:
        con.close()


def test_legacy_importer_imports_only_into_empty_database(store_db, tmp_path, monkeypatch):
    import import_legacy_database

    source = tmp_path / "chosen_snapshot.db"
    database._sqlite_backup(store_db["path"], source)
    target_dir = tmp_path / "new-live-data"
    target_config = tmp_path / "new-live-config"
    monkeypatch.setattr(database, "DATA_DIR", target_dir)
    monkeypatch.setattr(database, "DATABASE_PATH", target_dir / "smartpos.db")
    monkeypatch.setattr(database, "CONFIG_DIR", target_config)
    monkeypatch.setattr(database, "BACKUP_DIR", target_dir / "backups")
    monkeypatch.setattr(database, "LOG_DIR", target_dir / "logs")
    monkeypatch.setattr(database, "MIGRATION_BACKUPS_DIR", target_dir / "migration_backups")
    monkeypatch.setattr(database, "LEGACY_DATA_DIR", tmp_path / "no-legacy-folder")
    monkeypatch.setattr(database, "_STORAGE_READY", False)

    destination, safety, counts = import_legacy_database.import_snapshot(source)
    assert destination == target_dir / "smartpos.db"
    assert safety is None
    assert counts["products"] == 1
    assert counts["users"] == 1
    assert source.exists()

    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        import_legacy_database.import_snapshot(source)
