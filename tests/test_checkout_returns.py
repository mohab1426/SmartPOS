"""Checkout, idempotency, rollback, returns/refunds and inventory disposition."""
from __future__ import annotations

import pytest

from services.accounting import calculate_expected_cash
from services.customers import customer_balance
from services.inventory import get_stock_ledger_balance
from services.reporting import stock_ledger_discrepancies
from services.sales import PAYMENT_CASH, PAYMENT_CREDIT, PAYMENT_SPLIT, create_sale, process_return
from services.settings import set_setting
from helpers import connect, ensure_customer, line_ids, stock


def sell(con, store_db, qty=1, method=PAYMENT_CASH, cash=None, discount=0, name="", user=None, **kw):
    price_total = 1000 * qty - discount
    cash = price_total if cash is None else cash
    if name and "selected_customer_id" not in kw:
        # customers are identified by id: the helper resolves (or explicitly creates) the named test customer
        kw["selected_customer_id"] = ensure_customer(con, name)
    return create_sale(con, user=user or store_db["user"],
                       cart_lines=[{"product_id": store_db["product_id"], "quantity": qty}],
                       discount=discount, payment_method=method, cash_received=cash, **kw)


def test_cash_sale_change_stock_and_ledger(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=3, cash=5000)
        assert (sale["total"], sale["change"], sale["cash_amount"], sale["credit_amount"]) == (3000, 2000, 3000, 0)
        assert stock(con, store_db["product_id"]) == 7 == get_stock_ledger_balance(con, store_db["product_id"])
        assert stock_ledger_discrepancies(con) == []
    finally:
        con.close()


def test_insufficient_cash_and_unsupported_methods_rejected(store_db):
    con = connect(store_db["path"])
    try:
        with pytest.raises(ValueError, match="أقل من إجمالي"):
            sell(con, store_db, qty=2, cash=1999)
        for method in ("بطاقة", "Visa", "فيزا", "محفظة إلكترونية", "card", ""):
            with pytest.raises(ValueError, match="غير مدعومة"):
                sell(con, store_db, method=method)
        assert con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
        assert stock(con, store_db["product_id"]) == 10
    finally:
        con.close()


def test_credit_sale_requires_named_customer_and_split_math(store_db):
    con = connect(store_db["path"])
    try:
        with pytest.raises(ValueError):
            sell(con, store_db, qty=1, method=PAYMENT_CREDIT, cash=0, name="")
        credit = sell(con, store_db, qty=2, method=PAYMENT_CREDIT, cash=0, name="Mona")
        assert (credit["cash_amount"], credit["credit_amount"]) == (0, 2000)
        split = sell(con, store_db, qty=3, method=PAYMENT_SPLIT, cash=1200, name="Mona")
        assert (split["cash_amount"], split["credit_amount"], split["change"]) == (1200, 1800, 0)
        assert customer_balance(con, credit["customer_id"]) == 3800
        with pytest.raises(ValueError):                         # a "split" that is really fully cash
            sell(con, store_db, qty=1, method=PAYMENT_SPLIT, cash=1000, name="Mona")
        with pytest.raises(ValueError):                         # credit sale cannot carry cash
            sell(con, store_db, qty=1, method=PAYMENT_CREDIT, cash=500, name="Mona")
        # cash drawer only sees the cash part
        assert calculate_expected_cash(con, store_db["shift_id"]) == 10000 + 1200
    finally:
        con.close()


def test_discount_validation_and_admin_discount(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=2, discount=300)
        assert sale["total"] == 1700
        with pytest.raises(ValueError, match="الخصم"):
            sell(con, store_db, qty=1, discount=1001, cash=5000)
        with pytest.raises(ValueError):
            sell(con, store_db, qty=1, discount=-5, cash=5000)
    finally:
        con.close()


def test_stale_price_and_overselling_are_rejected(store_db):
    con = connect(store_db["path"])
    try:
        with pytest.raises(ValueError, match="تم تغيير سعر"):
            create_sale(con, user=store_db["user"],
                        cart_lines=[{"product_id": store_db["product_id"], "quantity": 1, "unit_price": 1}],
                        discount=0, payment_method=PAYMENT_CASH, cash_received=1000)
        with pytest.raises(ValueError, match="المخزون غير كافٍ"):
            sell(con, store_db, qty=11, cash=99999)
        assert stock(con, store_db["product_id"]) == 10
    finally:
        con.close()


def test_duplicate_checkout_is_idempotent(store_db):
    con = connect(store_db["path"])
    try:
        first = sell(con, store_db, qty=1, request_id="attempt-1")
        again = sell(con, store_db, qty=1, request_id="attempt-1")          # double-click / retry
        assert first["duplicate"] is False and again["duplicate"] is True
        assert again["invoice_id"] == first["invoice_id"]
        assert con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
        assert stock(con, store_db["product_id"]) == 9                      # deducted once
        assert calculate_expected_cash(con, store_db["shift_id"]) == 11000   # paid once
        other = sell(con, store_db, qty=1, request_id="attempt-2")
        assert other["invoice_id"] != first["invoice_id"]
    finally:
        con.close()


def test_idempotency_key_cannot_be_replayed_by_another_user(store_db, cashier):
    con = connect(store_db["path"])
    try:
        sell(con, store_db, request_id="shared-key")
        with pytest.raises(PermissionError):
            sell(con, store_db, request_id="shared-key", user=cashier)
    finally:
        con.close()


def test_failed_checkout_leaves_no_partial_changes(store_db):
    """Second line is invalid, so nothing from the first line (invoice, stock, ledger) may survive."""
    con = connect(store_db["path"])
    try:
        before = (con.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0],
                  con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
        with pytest.raises(ValueError):
            create_sale(con, user=store_db["user"],
                        cart_lines=[{"product_id": store_db["product_id"], "quantity": 2},
                                    {"product_id": 99999, "quantity": 1}],
                        discount=0, payment_method=PAYMENT_CASH, cash_received=99999)
        assert con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM invoice_payments").fetchone()[0] == 0
        assert stock(con, store_db["product_id"]) == 10
        assert (con.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]) == before
        assert not con.in_transaction
    finally:
        con.close()


def test_sale_requires_the_sellers_own_open_shift(store_db):
    con = connect(store_db["path"])
    try:
        con.execute("UPDATE cash_shifts SET status='closed' WHERE id=?", (store_db["shift_id"],))
        con.commit()
        with pytest.raises(ValueError, match="وردية"):
            sell(con, store_db)
    finally:
        con.close()


def test_two_terminals_cannot_oversell_last_unit(store_db, cashier):
    """Concurrent checkouts on separate connections: exactly one wins the last unit."""
    import threading
    con = connect(store_db["path"])
    con.execute("UPDATE products SET stock_quantity=1 WHERE id=?", (store_db["product_id"],))
    con.execute("INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,unit_cost,reference_type,"
                "reason) VALUES(?,'adjustment',-9,400,'test_fixture','align ledger')", (store_db["product_id"],))
    con.commit(); con.close()
    results = []

    def worker(user):
        c = connect(store_db["path"]); c.execute("PRAGMA busy_timeout=15000")
        try:
            sell(c, store_db, qty=1, user=user); results.append("ok")
        except ValueError:
            results.append("rejected")
        finally:
            c.close()
    threads = [threading.Thread(target=worker, args=(u,)) for u in (store_db["user"], cashier)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(results) == ["ok", "rejected"]
    con = connect(store_db["path"])
    assert stock(con, store_db["product_id"]) == 0 and stock_ledger_discrepancies(con) == []
    con.close()


# ------------------------------------------------------------------ returns

def test_partial_then_remaining_return_and_no_over_return(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=4, cash=4000)
        item = line_ids(con, sale["invoice_id"])[0]
        r1 = process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                            item_quantities={item: 1}, refund_method="نقدي")
        assert r1["refund_total"] == 1000
        with pytest.raises(ValueError, match="الكمية المتاحة"):
            process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                           item_quantities={item: 4}, refund_method="نقدي")      # only 3 left
        r2 = process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                            item_quantities={item: 3}, refund_method="نقدي")
        assert r2["refund_total"] == 3000
        with pytest.raises(ValueError):                                          # duplicate full return
            process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                           item_quantities={item: 1}, refund_method="نقدي")
        assert stock(con, store_db["product_id"]) == 10
        assert calculate_expected_cash(con, store_db["shift_id"]) == 10000       # sold 4000, refunded 4000
    finally:
        con.close()


def test_return_of_item_from_unrelated_invoice_rejected(store_db):
    con = connect(store_db["path"])
    try:
        a = sell(con, store_db, qty=1); b = sell(con, store_db, qty=1)
        foreign_item = line_ids(con, b["invoice_id"])[0]
        with pytest.raises(ValueError, match="غير موجود"):
            process_return(con, user=store_db["user"], invoice_id=a["invoice_id"],
                           item_quantities={foreign_item: 1}, refund_method="نقدي")
        assert con.execute("SELECT COUNT(*) FROM sales_returns").fetchone()[0] == 0
    finally:
        con.close()


def test_discounted_invoice_refund_never_exceeds_amount_paid(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=3, discount=500)               # pays 2500 for 3 units
        item = line_ids(con, sale["invoice_id"])[0]
        total_refund = 0
        for _ in range(3):
            total_refund += process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                                           item_quantities={item: 1}, refund_method="نقدي")["refund_total"]
        assert total_refund == 2500                                   # 833+834+833, no lost/created piaster
    finally:
        con.close()


def test_customer_credit_refund_is_not_cash_and_reduces_debt(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=2, method=PAYMENT_CREDIT, cash=0, name="Heba")
        cust = sale["customer_id"]
        assert customer_balance(con, cust) == 2000
        before_cash = calculate_expected_cash(con, store_db["shift_id"])
        item = line_ids(con, sale["invoice_id"])[0]
        r = process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                           item_quantities={item: 1}, refund_method="رصيد العميل")
        assert r["refund_total"] == 1000 and r["shift_id"] is None
        assert customer_balance(con, cust) == 1000
        assert calculate_expected_cash(con, store_db["shift_id"]) == before_cash      # no cash left the drawer
        row = con.execute("SELECT refund_method,shift_id,customer_id FROM sales_returns").fetchone()
        assert row == ("customer_credit", None, cust)
    finally:
        con.close()


def test_cash_refund_limited_to_cash_actually_paid(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=3, method=PAYMENT_SPLIT, cash=500, name="Sara")   # 500 cash, 2500 credit
        item = line_ids(con, sale["invoice_id"])[0]
        with pytest.raises(ValueError, match="تتجاوز المبلغ النقدي"):
            process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                           item_quantities={item: 1}, refund_method="نقدي")
        process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                       item_quantities={item: 1}, refund_method="رصيد العميل")
    finally:
        con.close()


def test_credit_refund_needs_a_customer_account(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db)
        with pytest.raises(ValueError, match="حساب عميل"):
            process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"],
                           item_quantities={line_ids(con, sale["invoice_id"])[0]: 1}, refund_method="رصيد العميل")
    finally:
        con.close()


def test_return_authorization_policy(store_db, cashier):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=2, cash=2000)
        item = line_ids(con, sale["invoice_id"])[0]
        # default policy: an active cashier with her own open shift may refund cash
        process_return(con, user=cashier, invoice_id=sale["invoice_id"], item_quantities={item: 1},
                       refund_method="نقدي")
        # unauthenticated / unknown / inactive callers never can
        for ghost in (None, {}, {"id": 4242, "role": "admin"}):
            with pytest.raises(PermissionError):
                process_return(con, user=ghost, invoice_id=sale["invoice_id"], item_quantities={item: 1},
                               refund_method="نقدي")
        # an administrator can restrict returns to administrators only
        set_setting(con, actor=store_db["user"], key="cashier_can_process_returns", value="0")
        with pytest.raises(PermissionError, match="للمدير فقط"):
            process_return(con, user=cashier, invoice_id=sale["invoice_id"], item_quantities={item: 1},
                           refund_method="نقدي")
        process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"], item_quantities={item: 1},
                       refund_method="نقدي")
    finally:
        con.close()


def test_cash_refund_hits_the_refunding_users_shift(store_db, cashier):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=1)                                  # sold on ADMIN's shift
        item = line_ids(con, sale["invoice_id"])[0]
        r = process_return(con, user=cashier, invoice_id=sale["invoice_id"], item_quantities={item: 1},
                           refund_method="نقدي")
        assert r["shift_id"] == cashier["shift_id"]
        assert calculate_expected_cash(con, cashier["shift_id"]) == 5000 - 1000
        assert calculate_expected_cash(con, store_db["shift_id"]) == 10000 + 1000
    finally:
        con.close()


def test_damaged_return_keeps_stock_but_logs_both_movements(store_db):
    con = connect(store_db["path"])
    try:
        sale = sell(con, store_db, qty=2)
        item = line_ids(con, sale["invoice_id"])[0]
        process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"], item_quantities={item: 1},
                       refund_method="نقدي", disposition="damaged")
        assert stock(con, store_db["product_id"]) == 8                       # NOT restocked
        assert get_stock_ledger_balance(con, store_db["product_id"]) == 8
        kinds = [r[0] for r in con.execute("SELECT reference_type FROM inventory_movements "
                                           "WHERE reference_type IN ('sales_return_item','return_damage')")]
        assert sorted(kinds) == ["return_damage", "sales_return_item"]
        assert con.execute("SELECT disposition FROM sales_return_items").fetchone()[0] == "damaged"
        with pytest.raises(ValueError):
            process_return(con, user=store_db["user"], invoice_id=sale["invoice_id"], item_quantities={item: 1},
                           refund_method="نقدي", disposition="resell-later")
    finally:
        con.close()


# ---- audit F-04 / duplicate cart lines ---------------------------------------------------------------
def test_duplicate_cart_lines_give_friendly_stock_error(store_db):
    con = connect(store_db["path"])
    pid, admin = store_db["product_id"], store_db["user"]
    lines = [{"product_id": pid, "quantity": 6}, {"product_id": pid, "quantity": 6}]     # 12 > stock 10
    with pytest.raises(ValueError, match="المخزون غير كافٍ"):
        create_sale(con, user=admin, cart_lines=lines, discount=0, payment_method=PAYMENT_CASH,
                    cash_received=100000)
    assert stock(con, pid) == 10 and con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
    ok = [{"product_id": pid, "quantity": 5}, {"product_id": pid, "quantity": 5}]        # exactly stock
    create_sale(con, user=admin, cart_lines=ok, discount=0, payment_method=PAYMENT_CASH, cash_received=10000)
    assert stock(con, pid) == 0
    con.close()


def test_close_shift_with_stale_expected_cash_leaves_shift_open(store_db):
    from services.shifts import add_cash_movement, close_shift
    con = connect(store_db["path"])
    admin, shift_id = store_db["user"], store_db["shift_id"]
    expected = calculate_expected_cash(con, shift_id)
    add_cash_movement(con, user=admin, shift_id=shift_id, movement_type="مصروف", amount=100, reason="x")
    with pytest.raises(ValueError, match="لم يتم إغلاق"):
        close_shift(con, user=admin, shift_id=shift_id, actual_cash=expected, expected_cash=expected)
    assert con.execute("SELECT status FROM cash_shifts WHERE id=?", (shift_id,)).fetchone()[0] == "open"
    fresh = calculate_expected_cash(con, shift_id)
    result = close_shift(con, user=admin, shift_id=shift_id, actual_cash=fresh, expected_cash=fresh)
    assert result["difference"] == 0
    assert con.execute("SELECT status FROM cash_shifts WHERE id=?", (shift_id,)).fetchone()[0] == "closed"
    con.close()


# ---- refund vs drawer cash, and store-credit semantics (follow-up audit pass) --------------------------
def _item_ids(con, invoice_id):
    return line_ids(con, invoice_id)


def test_cash_refund_cannot_exceed_drawer_cash_but_can_after_deposit(store_db):
    from services.shifts import add_cash_movement
    con = connect(store_db["path"])
    admin, shift_id, pid = store_db["user"], store_db["shift_id"], store_db["product_id"]
    sale = create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 2}], discount=0,
                       payment_method=PAYMENT_CASH, cash_received=2000)
    drawer = calculate_expected_cash(con, shift_id)
    add_cash_movement(con, user=admin, shift_id=shift_id, movement_type="سحب", amount=drawer, reason="تفريغ")
    assert calculate_expected_cash(con, shift_id) == 0
    items = _item_ids(con, sale["invoice_id"])
    with pytest.raises(ValueError, match="الخزنة لا تحتوي"):
        process_return(con, user=admin, invoice_id=sale["invoice_id"], item_quantities={items[0]: 2},
                       refund_method="نقدي")
    assert stock(con, pid) == 8 and con.execute("SELECT COUNT(*) FROM sales_returns").fetchone()[0] == 0
    assert calculate_expected_cash(con, shift_id) == 0                  # nothing was written
    add_cash_movement(con, user=admin, shift_id=shift_id, movement_type="إيداع", amount=2000, reason="تغطية")
    process_return(con, user=admin, invoice_id=sale["invoice_id"], item_quantities={items[0]: 2},
                   refund_method="نقدي")
    assert calculate_expected_cash(con, shift_id) == 0 and stock(con, pid) == 10
    con.close()


def test_credit_refund_of_an_already_paid_debt_is_store_credit_netted_by_next_credit_sale(store_db):
    from services.customers import collect_customer_payment
    con = connect(store_db["path"])
    admin, shift_id, pid = store_db["user"], store_db["shift_id"], store_db["product_id"]
    sale = create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                       payment_method=PAYMENT_CREDIT, cash_received=0, new_customer={"name": "Ali"})
    cid = sale["customer_id"]
    collect_customer_payment(con, customer_id=cid, amount=1000, user_id=admin["id"], shift_id=shift_id)
    assert customer_balance(con, cid) == 0
    process_return(con, user=admin, invoice_id=sale["invoice_id"],
                   item_quantities={line_ids(con, sale["invoice_id"])[0]: 1}, refund_method="رصيد العميل")
    assert customer_balance(con, cid) == -1000          # supported: the shop owes the customer 10.00
    create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                payment_method=PAYMENT_CREDIT, cash_received=0, selected_customer_id=cid)
    assert customer_balance(con, cid) == 0              # the next credit sale consumes the store credit
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


# ---- customer identity (audit C-03) ---------------------------------------------------------------------
def test_two_customers_with_the_same_name_are_kept_apart_by_id(store_db):
    con = connect(store_db["path"])
    admin, pid = store_db["user"], store_db["product_id"]
    first = con.execute("INSERT INTO customers(name,phone) VALUES('Mohamed Ali','0100')").lastrowid
    second = con.execute("INSERT INTO customers(name,phone) VALUES('Mohamed Ali','0122')").lastrowid
    con.commit()
    create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                payment_method=PAYMENT_CREDIT, cash_received=0, customer_name="Mohamed Ali",
                selected_customer_id=second)
    assert customer_balance(con, second) == 1000 and customer_balance(con, first) == 0
    # A typed name alone is NEVER enough to attach a debt (the old behaviour silently picked the
    # oldest "Mohamed Ali"): it is rejected and nothing is written.
    with pytest.raises(ValueError, match="عميل مسجل"):
        create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                    payment_method=PAYMENT_CREDIT, cash_received=0, customer_name="Mohamed Ali")
    assert customer_balance(con, first) == 0 and customer_balance(con, second) == 1000
    with pytest.raises(ValueError, match="العميل المحدد"):
        create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                    payment_method=PAYMENT_CREDIT, cash_received=0, selected_customer_id=999)
    con.execute("UPDATE customers SET active=0 WHERE id=?", (first,))
    con.commit()
    with pytest.raises(ValueError, match="العميل المحدد"):
        create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                    payment_method=PAYMENT_CREDIT, cash_received=0, selected_customer_id=first)
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2        # nobody was created
    con.close()


def _invoice_count(con):
    return con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]


def test_credit_sale_with_ambiguous_typed_name_is_rejected_and_writes_nothing(store_db):
    con = connect(store_db["path"])
    admin, pid = store_db["user"], store_db["product_id"]
    con.execute("INSERT INTO customers(name) VALUES('Ahmed')"); con.execute("INSERT INTO customers(name) VALUES('Ahmed')")
    con.commit()
    for method, cash in ((PAYMENT_CREDIT, 0), (PAYMENT_SPLIT, 400)):
        with pytest.raises(ValueError, match="عميل مسجل"):
            create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                        payment_method=method, cash_received=cash, customer_name="Ahmed")
    assert _invoice_count(con) == 0 and stock(con, pid) == 10
    assert con.execute("SELECT COUNT(*) FROM customer_ledger").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
    con.close()


def test_cash_sale_with_unknown_typed_name_creates_no_customer(store_db):
    con = connect(store_db["path"])
    sale = sell(con, store_db, qty=1, name="")
    sale2 = create_sale(con, user=store_db["user"], cart_lines=[{"product_id": store_db["product_id"], "quantity": 1}],
                        discount=0, payment_method=PAYMENT_CASH, cash_received=1000, customer_name="Totally New Person")
    assert sale["customer_id"] is None and sale2["customer_id"] is None
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM customer_ledger").fetchone()[0] == 0
    con.close()


def test_explicit_new_customer_is_created_with_the_sale_and_gets_the_debt(store_db):
    con = connect(store_db["path"])
    admin, pid = store_db["user"], store_db["product_id"]
    sale = create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 2}], discount=0,
                       payment_method=PAYMENT_SPLIT, cash_received=500,
                       new_customer={"name": "  Nadia  ", "phone": "0111"})
    cid = sale["customer_id"]
    assert con.execute("SELECT name,phone FROM customers WHERE id=?", (cid,)).fetchone() == ("Nadia", "0111")
    assert customer_balance(con, cid) == 1500
    con.close()


def test_new_customer_with_an_existing_name_needs_explicit_duplicate_confirmation(store_db):
    con = connect(store_db["path"])
    admin, pid = store_db["user"], store_db["product_id"]
    con.execute("INSERT INTO customers(name) VALUES('Ahmed')"); con.commit()
    lines = [{"product_id": pid, "quantity": 1}]
    with pytest.raises(ValueError, match="بنفس الاسم"):
        create_sale(con, user=admin, cart_lines=lines, discount=0, payment_method=PAYMENT_CREDIT,
                    cash_received=0, new_customer={"name": "ahmed"})
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1 and _invoice_count(con) == 0
    sale = create_sale(con, user=admin, cart_lines=lines, discount=0, payment_method=PAYMENT_CREDIT,
                       cash_received=0, new_customer={"name": "ahmed", "allow_duplicate": True})
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
    assert customer_balance(con, sale["customer_id"]) == 1000
    con.close()


def test_new_customer_rules_selected_and_new_are_mutually_exclusive_and_credit_only(store_db):
    con = connect(store_db["path"])
    admin, pid = store_db["user"], store_db["product_id"]
    cid = ensure_customer(con, "Existing")
    lines = [{"product_id": pid, "quantity": 1}]
    with pytest.raises(ValueError):
        create_sale(con, user=admin, cart_lines=lines, discount=0, payment_method=PAYMENT_CREDIT,
                    cash_received=0, selected_customer_id=cid, new_customer={"name": "X"})
    with pytest.raises(ValueError, match="الآجل"):
        create_sale(con, user=admin, cart_lines=lines, discount=0, payment_method=PAYMENT_CASH,
                    cash_received=1000, new_customer={"name": "X"})
    with pytest.raises(ValueError, match="اسم العميل"):
        create_sale(con, user=admin, cart_lines=lines, discount=0, payment_method=PAYMENT_CREDIT,
                    cash_received=0, new_customer={"name": "   "})
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1 and _invoice_count(con) == 0
    con.close()


def test_failed_checkout_leaves_no_orphan_customer_or_receivable(store_db, monkeypatch):
    import services.sales as sales_module
    con = connect(store_db["path"])
    admin, pid = store_db["user"], store_db["product_id"]

    def boom(*_a, **_k):
        raise RuntimeError("injected failure after the customer was created")

    monkeypatch.setattr(sales_module, "add_customer_ledger_entry", boom)
    with pytest.raises(RuntimeError):
        create_sale(con, user=admin, cart_lines=[{"product_id": pid, "quantity": 1}], discount=0,
                    payment_method=PAYMENT_CREDIT, cash_received=0, new_customer={"name": "Ghost"})
    assert con.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM customer_ledger").fetchone()[0] == 0
    assert _invoice_count(con) == 0 and stock(con, pid) == 10
    con.close()


def test_customer_selector_labels_are_unique_for_duplicate_names():
    from services.customers import build_customer_labels
    labels = build_customer_labels([(1, "Ahmed", "0100"), (2, "Ahmed", ""), (3, "ahmed", "0100"), (4, "Mona", "")])
    assert labels == ["Ahmed (0100)", "Ahmed (#2)", "ahmed (0100) #3", "Mona"]
    assert len({x.casefold() for x in labels}) == len(labels)


def test_legacy_get_or_create_customer_refuses_ambiguous_names(store_db):
    from services.customers import AmbiguousCustomerError, get_or_create_customer
    con = connect(store_db["path"])
    con.execute("INSERT INTO customers(name) VALUES('Ahmed')"); con.execute("INSERT INTO customers(name) VALUES('Ahmed')")
    con.commit()
    with pytest.raises(AmbiguousCustomerError):
        get_or_create_customer(con, "Ahmed")
    con.close()
