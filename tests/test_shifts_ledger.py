"""Cash shifts, expected-cash formula, customer ledger and historical reconciliation."""
from __future__ import annotations

import pytest

from services.accounting import calculate_expected_cash
from services.customers import collect_customer_payment, customer_balance
from services.reporting import customer_balances, customer_ledger_discrepancies, unclassified_returns
from services.sales import PAYMENT_CREDIT, PAYMENT_SPLIT, PAYMENT_CASH, classify_legacy_return, create_sale, process_return
from services.shifts import add_cash_movement, close_shift, open_shift
from helpers import connect, ensure_customer, line_ids


def sale(con, store_db, qty, method, cash, name="", user=None):
    extra = {"selected_customer_id": ensure_customer(con, name)} if name else {}
    return create_sale(con, user=user or store_db["user"],
                       cart_lines=[{"product_id": store_db["product_id"], "quantity": qty}],
                       discount=0, payment_method=method, cash_received=cash, **extra)


def test_expected_cash_formula_end_to_end_and_close_variance(store_db):
    con = connect(store_db["path"]); admin, shift = store_db["user"], store_db["shift_id"]
    try:
        # opening 10000
        s1 = sale(con, store_db, 2, PAYMENT_CASH, 5000)                         # +2000 cash
        sale(con, store_db, 3, PAYMENT_CREDIT, 0, "Nour")                        # credit: no cash
        sale(con, store_db, 2, PAYMENT_SPLIT, 700, "Nour")                       # +700 cash
        nour = con.execute("SELECT id FROM customers WHERE name='Nour'").fetchone()[0]
        collect_customer_payment(con, customer_id=nour, amount=1500, user_id=admin["id"], shift_id=shift)   # +1500
        con.commit()
        add_cash_movement(con, user=admin, shift_id=shift, movement_type="إيداع", amount=400, reason="float")       # +400
        add_cash_movement(con, user=admin, shift_id=shift, movement_type="مصروف", amount=250, reason="cleaning")  # -250
        add_cash_movement(con, user=admin, shift_id=shift, movement_type="سحب", amount=100, reason="owner")        # -100
        item = line_ids(con, s1["invoice_id"])[0]
        process_return(con, user=admin, invoice_id=s1["invoice_id"], item_quantities={item: 1}, refund_method="نقدي")  # -1000
        expected = 10000 + 2000 + 700 + 1500 + 400 - 250 - 100 - 1000
        assert calculate_expected_cash(con, shift) == expected == 13250
        closed = close_shift(con, user=admin, shift_id=shift, actual_cash=13000)
        assert closed == {"shift_id": shift, "expected": 13250, "actual": 13000, "difference": -250}
        assert con.execute("SELECT status,expected_cash,actual_cash,difference FROM cash_shifts WHERE id=?",
                           (shift,)).fetchone() == ("closed", 13250, 13000, -250)
        with pytest.raises(PermissionError):                                       # closed shift is frozen
            add_cash_movement(con, user=admin, shift_id=shift, movement_type="إيداع", amount=1, reason="late")
        with pytest.raises(PermissionError):
            close_shift(con, user=admin, shift_id=shift, actual_cash=1)
    finally:
        con.close()


def test_shift_lifecycle_rules(store_db, cashier):
    con = connect(store_db["path"])
    try:
        with pytest.raises(ValueError, match="مفتوحة بالفعل"):
            open_shift(con, user=store_db["user"], opening_cash=0)
        with pytest.raises(ValueError):
            open_shift(con, user=cashier, opening_cash=-1)
        close_shift(con, user=cashier, shift_id=cashier["shift_id"], actual_cash=5000)
        sid = open_shift(con, user=cashier, opening_cash=7000)
        assert calculate_expected_cash(con, sid) == 7000
        with pytest.raises(ValueError, match="أكبر من الرصيد"):                    # cannot withdraw more than drawer holds
            add_cash_movement(con, user=cashier, shift_id=sid, movement_type="سحب", amount=7001, reason="x")
        with pytest.raises(ValueError):
            add_cash_movement(con, user=cashier, shift_id=sid, movement_type="سحب", amount=10, reason="  ")
        with pytest.raises(ValueError):
            add_cash_movement(con, user=cashier, shift_id=sid, movement_type="هدية", amount=10, reason="x")
    finally:
        con.close()


def test_debt_repayments_partial_multiple_full_and_overpayment(store_db):
    con = connect(store_db["path"]); admin, shift = store_db["user"], store_db["shift_id"]
    try:
        s = sale(con, store_db, 5, PAYMENT_CREDIT, 0, "Dina"); cid = s["customer_id"]
        assert customer_balance(con, cid) == 5000
        collect_customer_payment(con, customer_id=cid, amount=1000, user_id=admin["id"], shift_id=shift)
        collect_customer_payment(con, customer_id=cid, amount=1500, user_id=admin["id"], shift_id=shift)
        assert customer_balance(con, cid) == 2500
        with pytest.raises(ValueError, match="exceed"):
            collect_customer_payment(con, customer_id=cid, amount=2501, user_id=admin["id"], shift_id=shift)
        collect_customer_payment(con, customer_id=cid, amount=2500, user_id=admin["id"], shift_id=shift)
        assert customer_balance(con, cid) == 0
        with pytest.raises(ValueError, match="no outstanding"):
            collect_customer_payment(con, customer_id=cid, amount=1, user_id=admin["id"], shift_id=shift)
        with pytest.raises(ValueError):
            collect_customer_payment(con, customer_id=cid, amount=0, user_id=admin["id"], shift_id=shift)
        assert calculate_expected_cash(con, shift) == 10000 + 5000      # every collection is cash in the drawer
        assert customer_ledger_discrepancies(con) == []
    finally:
        con.close()


def test_collection_must_use_collectors_own_open_shift(store_db, cashier):
    con = connect(store_db["path"])
    try:
        s = sale(con, store_db, 1, PAYMENT_CREDIT, 0, "Omar")
        with pytest.raises(ValueError, match="open shift"):
            collect_customer_payment(con, customer_id=s["customer_id"], amount=100,
                                     user_id=cashier["id"], shift_id=store_db["shift_id"])
        with pytest.raises(PermissionError):
            collect_customer_payment(con, customer_id=s["customer_id"], amount=100, user_id=9999, shift_id=1)
    finally:
        con.close()


def test_ledger_reconciles_with_documents_after_mixed_activity(store_db):
    con = connect(store_db["path"]); admin, shift = store_db["user"], store_db["shift_id"]
    try:
        a = sale(con, store_db, 3, PAYMENT_CREDIT, 0, "A"); b = sale(con, store_db, 2, PAYMENT_SPLIT, 500, "B")
        process_return(con, user=admin, invoice_id=a["invoice_id"], item_quantities={line_ids(con, a["invoice_id"])[0]: 1},
                       refund_method="رصيد العميل")
        collect_customer_payment(con, customer_id=b["customer_id"], amount=700, user_id=admin["id"], shift_id=shift)
        balances = {name: bal for _i, name, bal in customer_balances(con)}
        assert balances == {"A": 2000, "B": 800}
        assert customer_ledger_discrepancies(con) == []
        with pytest.raises(Exception, match="append-only"):
            con.execute("DELETE FROM customer_ledger")
        con.rollback()
        # corrupt the ledger the only way still possible (bypass triggers) and prove the check notices
        con.execute("DROP TRIGGER trg_customer_ledger_no_update")
        con.execute("UPDATE customer_ledger SET debit=debit+1 WHERE transaction_type='sale_credit' AND customer_id=?",
                    (a["customer_id"],))
        assert customer_ledger_discrepancies(con)[0]["kind"] == "credit_sales"
        con.rollback()
    finally:
        con.close()


def _make_legacy_unknown_return(con, store_db):
    s = sale(con, store_db, 2, PAYMENT_CASH, 2000)
    cur = con.execute("INSERT INTO sales_returns(return_number,invoice_id,total_refund,refund_method,business_date) "
                      "VALUES('RET-OLD',?,1000,'unknown','2026-10-01')", (s["invoice_id"],))
    con.execute("INSERT INTO sales_return_items(return_id,invoice_item_id,product_id,product_name,quantity,unit_refund,line_refund)"
                " VALUES(?,?,?,?,1,1000,1000)", (cur.lastrowid, line_ids(con, s["invoice_id"])[0],
                                                 store_db["product_id"], "Test Shirt"))
    con.commit()
    return cur.lastrowid, s


def test_unknown_historical_return_lifecycle_and_shift_recalculation(store_db):
    con = connect(store_db["path"]); admin, shift = store_db["user"], store_db["shift_id"]
    try:
        rid, s = _make_legacy_unknown_return(con, store_db)
        assert [r[0] for r in unclassified_returns(con)] == [rid]
        before = calculate_expected_cash(con, shift)
        assert before == 12000                                            # unknown refund is NOT guessed as cash
        out = classify_legacy_return(con, admin_user=admin, return_id=rid, refund_method="نقدي", shift_id=shift)
        assert out["new_expected_cash"] == before - 1000 == calculate_expected_cash(con, shift)
        assert unclassified_returns(con) == []
        audit = con.execute("SELECT action,username FROM audit_log WHERE action='legacy_return_classified'").fetchone()
        assert audit == ("legacy_return_classified", "admin")
        with pytest.raises(ValueError, match="مصنّف بالفعل"):              # cannot be re-classified / silently rewritten
            classify_legacy_return(con, admin_user=admin, return_id=rid, refund_method="رصيد العميل")
    finally:
        con.close()


def test_classifying_historical_return_requires_admin_and_real_target(store_db, cashier):
    con = connect(store_db["path"])
    try:
        rid, s = _make_legacy_unknown_return(con, store_db)
        with pytest.raises(PermissionError):
            classify_legacy_return(con, admin_user=cashier, return_id=rid, refund_method="نقدي", shift_id=cashier["shift_id"])
        with pytest.raises(ValueError):                                    # no shift supplied -> no guessing
            classify_legacy_return(con, admin_user=store_db["user"], return_id=rid, refund_method="نقدي")
        with pytest.raises(ValueError, match="حساب عميل"):                  # invoice has no customer account
            classify_legacy_return(con, admin_user=store_db["user"], return_id=rid, refund_method="رصيد العميل")
        assert con.execute("SELECT refund_method FROM sales_returns WHERE id=?", (rid,)).fetchone()[0] == "unknown"
        # a new cash refund on an invoice that still has an unclassified legacy return is blocked
        with pytest.raises(ValueError, match="مرتجعات قديمة"):
            process_return(con, user=store_db["user"], invoice_id=s["invoice_id"],
                           item_quantities={line_ids(con, s["invoice_id"])[0]: 1}, refund_method="نقدي")
    finally:
        con.close()


def test_two_terminals_cannot_both_collect_the_same_debt(store_db, cashier):
    """Check-then-write race: both collectors try to take the full 2000; exactly one may succeed."""
    import threading
    con = connect(store_db["path"])
    s = sale(con, store_db, 2, PAYMENT_CREDIT, 0, "Racer"); cid = s["customer_id"]
    con.close()
    outcome = []

    def collect(user_id, shift_id):
        c = connect(store_db["path"]); c.execute("PRAGMA busy_timeout=15000")
        try:
            collect_customer_payment(c, customer_id=cid, amount=2000, user_id=user_id, shift_id=shift_id)
            c.commit(); outcome.append("ok")
        except ValueError:
            outcome.append("rejected")
        except Exception as exc:                     # e.g. 'database is locked' would mean the lock is wrong
            outcome.append(f"error:{exc}")
        finally:
            c.close()
    threads = [threading.Thread(target=collect, args=(store_db["user"]["id"], store_db["shift_id"])),
               threading.Thread(target=collect, args=(cashier["id"], cashier["shift_id"]))]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(outcome) == ["ok", "rejected"], outcome
    con = connect(store_db["path"])
    assert customer_balance(con, cid) == 0                      # never negative / double-collected
    con.close()
