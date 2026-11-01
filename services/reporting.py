from __future__ import annotations

from services.inventory import products_with_unknown_cost
from services.sales import allocate_net_line_values


def _money_rows(connection, sql, params):
    return connection.execute(sql, params).fetchall()


def get_top_products_net(connection, date_from: str, date_to: str, limit: int = 8):
    """Net sold quantity and net value per product: (name, net_qty, net_value).

    Value is *after invoice discounts* (allocated to lines with the same largest-remainder
    rule used by returns) and after refunds booked in the period, so the values of all
    products sum exactly to the period's net sales.
    """
    totals: dict[int, list] = {}          # product_id -> [name, qty, value]

    def bucket(product_id, name):
        return totals.setdefault(int(product_id), [name, 0, 0])

    for invoice_id, subtotal, total in connection.execute(
            "SELECT id,subtotal,total FROM invoices WHERE business_date BETWEEN ? AND ?", (date_from, date_to)).fetchall():
        lines = connection.execute(
            "SELECT id,product_id,product_name,quantity,line_total FROM invoice_items WHERE invoice_id=? ORDER BY id",
            (invoice_id,)).fetchall()
        net = allocate_net_line_values([(i, g) for i, _p, _n, _q, g in lines], int(total), int(subtotal))
        for item_id, product_id, name, qty, _gross in lines:
            b = bucket(product_id, name)
            b[1] += int(qty); b[2] += net[int(item_id)]
    for product_id, name, qty, refund in connection.execute("""
            SELECT ii.product_id,ii.product_name,sri.quantity,sri.line_refund
            FROM sales_return_items sri
            JOIN invoice_items ii ON ii.id=sri.invoice_item_id
            JOIN sales_returns sr ON sr.id=sri.return_id
            WHERE sr.business_date BETWEEN ? AND ?""", (date_from, date_to)):
        b = bucket(product_id, name)
        b[1] -= int(qty); b[2] -= int(refund)
    rows = [(name, qty, value) for name, qty, value in totals.values() if qty != 0]
    rows.sort(key=lambda r: (-r[1], -r[2], str(r[0]).casefold()))
    return rows[:int(limit)]


def get_report_snapshot(connection, date_from: str, date_to: str) -> dict:
    """Single consistent business-date snapshot for reports and dashboard totals."""
    if date_from > date_to:
        raise ValueError("date_from must not be after date_to")
    invoice_totals = connection.execute("""
        SELECT COUNT(*),COALESCE(SUM(subtotal),0),COALESCE(SUM(discount),0),COALESCE(SUM(total),0)
        FROM invoices WHERE business_date BETWEEN ? AND ?
    """,(date_from,date_to)).fetchone()
    returns_total = connection.execute(
        "SELECT COALESCE(SUM(total_refund),0) FROM sales_returns WHERE business_date BETWEEN ? AND ?",
        (date_from,date_to)).fetchone()[0]
    unclassified_return_total = connection.execute(
        "SELECT COALESCE(SUM(total_refund),0) FROM sales_returns WHERE business_date BETWEEN ? AND ? AND refund_method='unknown'",
        (date_from,date_to)).fetchone()[0]
    sold_cost,unknown_units = connection.execute("""
        SELECT COALESCE(SUM(CASE WHEN ii.unit_cost>0 THEN ii.unit_cost*ii.quantity ELSE 0 END),0),
               COALESCE(SUM(CASE WHEN ii.unit_cost<=0 THEN ii.quantity ELSE 0 END),0)
        FROM invoice_items ii JOIN invoices i ON i.id=ii.invoice_id
        WHERE i.business_date BETWEEN ? AND ?
    """,(date_from,date_to)).fetchone()
    returned_cost = connection.execute("""
        SELECT COALESCE(SUM(CASE WHEN ii.unit_cost>0 THEN ii.unit_cost*sri.quantity ELSE 0 END),0)
        FROM sales_return_items sri JOIN sales_returns sr ON sr.id=sri.return_id
        JOIN invoice_items ii ON ii.id=sri.invoice_item_id
        WHERE sr.business_date BETWEEN ? AND ?
    """,(date_from,date_to)).fetchone()[0]
    expenses = connection.execute("""
        SELECT COALESCE(SUM(amount),0) FROM cash_movements
        WHERE movement_type='مصروف' AND business_date BETWEEN ? AND ?
    """,(date_from,date_to)).fetchone()[0]
    invoice_rows = connection.execute("""
        SELECT i.id,COALESCE(i.daily_number,i.invoice_number),i.created_at,i.subtotal,
               i.discount,i.total,COUNT(ii.id)
        FROM invoices i LEFT JOIN invoice_items ii ON ii.invoice_id=i.id
        WHERE i.business_date BETWEEN ? AND ?
        GROUP BY i.id ORDER BY i.id DESC
    """,(date_from,date_to)).fetchall()
    monthly_rows = connection.execute("""
        SELECT substr(business_date,1,7),COUNT(*),COALESCE(SUM(total),0),COALESCE(SUM(discount),0)
        FROM invoices WHERE business_date BETWEEN ? AND ?
        GROUP BY substr(business_date,1,7) ORDER BY 1
    """,(date_from,date_to)).fetchall()
    monthly_returns = dict(connection.execute("""
        SELECT substr(business_date,1,7),COALESCE(SUM(total_refund),0)
        FROM sales_returns WHERE business_date BETWEEN ? AND ? GROUP BY 1
    """,(date_from,date_to)).fetchall())
    monthly_expenses = dict(connection.execute("""
        SELECT substr(business_date,1,7),COALESCE(SUM(amount),0)
        FROM cash_movements WHERE movement_type='مصروف' AND business_date BETWEEN ? AND ? GROUP BY 1
    """,(date_from,date_to)).fetchall())
    monthly_costs = dict(connection.execute("""
        SELECT substr(i.business_date,1,7),COALESCE(SUM(CASE WHEN ii.unit_cost>0 THEN ii.unit_cost*ii.quantity ELSE 0 END),0)
        FROM invoices i JOIN invoice_items ii ON ii.invoice_id=i.id
        WHERE i.business_date BETWEEN ? AND ? GROUP BY 1
    """,(date_from,date_to)).fetchall())
    monthly_return_costs = dict(connection.execute("""
        SELECT substr(sr.business_date,1,7),COALESCE(SUM(CASE WHEN ii.unit_cost>0 THEN ii.unit_cost*sri.quantity ELSE 0 END),0)
        FROM sales_returns sr JOIN sales_return_items sri ON sri.return_id=sr.id
        JOIN invoice_items ii ON ii.id=sri.invoice_item_id
        WHERE sr.business_date BETWEEN ? AND ? GROUP BY 1
    """,(date_from,date_to)).fetchall())
    span_days = __import__('datetime').date.fromisoformat(date_to) - __import__('datetime').date.fromisoformat(date_from)
    if span_days.days <= 45:
        trend_rows = connection.execute("""
            SELECT business_date,COALESCE(SUM(total),0) FROM invoices
            WHERE business_date BETWEEN ? AND ? GROUP BY business_date ORDER BY business_date
        """,(date_from,date_to)).fetchall()
        trend_returns = dict(connection.execute("""
            SELECT business_date,COALESCE(SUM(total_refund),0) FROM sales_returns
            WHERE business_date BETWEEN ? AND ? GROUP BY business_date
        """,(date_from,date_to)).fetchall())
    else:
        trend_rows = connection.execute("""
            SELECT substr(business_date,1,7),COALESCE(SUM(total),0) FROM invoices
            WHERE business_date BETWEEN ? AND ? GROUP BY substr(business_date,1,7) ORDER BY 1
        """,(date_from,date_to)).fetchall()
        trend_returns = dict(connection.execute("""
            SELECT substr(business_date,1,7),COALESCE(SUM(total_refund),0) FROM sales_returns
            WHERE business_date BETWEEN ? AND ? GROUP BY substr(business_date,1,7)
        """,(date_from,date_to)).fetchall())

    known_rev, unknown_rev = _net_revenue_by_cost_reliability(connection, date_from, date_to)
    count,subtotal,discounts,sales_total = map(int,invoice_totals)
    net_sales = sales_total - int(returns_total)
    net_cost = int(sold_cost) - int(returned_cost)
    return {
        "count":count,"subtotal":int(subtotal),"discounts":int(discounts),
        "sales_total":sales_total,"returns_total":int(returns_total),
        "unclassified_return_total":int(unclassified_return_total),
        "net_sales":net_sales,"sold_cost":int(sold_cost),
        "returned_cost":int(returned_cost),"net_cost":net_cost,
        "expenses":int(expenses),"estimated_profit":net_sales-net_cost-int(expenses),
        "unknown_cost_units":int(unknown_units),
        "unknown_cost_products":products_with_unknown_cost(connection),
        # Audited figures exclude every line whose cost is unknown or unverified (unit_cost <= 0: -1 = unknown,
        # 0 = recorded as free, which is never trusted as a real cost):
        # their revenue is reported separately instead of being counted as 100% margin.
        "audited_revenue":known_rev,"unknown_cost_revenue":unknown_rev,
        "audited_gross_profit":known_rev-net_cost,
        "audited_net_profit":known_rev-net_cost-int(expenses),
        "invoice_rows":invoice_rows,
        "monthly_rows":monthly_rows,"monthly_returns":monthly_returns,
        "monthly_expenses":monthly_expenses,"monthly_costs":monthly_costs,
        "monthly_return_costs":monthly_return_costs,"trend_rows":trend_rows,
        "trend_returns":trend_returns,
        "top_products":get_top_products_net(connection,date_from,date_to)
    }


def _net_revenue_by_cost_reliability(connection, date_from: str, date_to: str) -> tuple[int, int]:
    """Net (post-discount, post-return) revenue split into (known-cost, unknown-cost) lines."""
    known = unknown = 0
    invoices = connection.execute(
        "SELECT id,subtotal,total FROM invoices WHERE business_date BETWEEN ? AND ?",
        (date_from, date_to)).fetchall()
    for invoice_id, subtotal, total in invoices:
        lines = connection.execute(
            "SELECT id,line_total,unit_cost FROM invoice_items WHERE invoice_id=? ORDER BY id",
            (invoice_id,)).fetchall()
        net = allocate_net_line_values([(i, g) for i, g, _c in lines], int(total), int(subtotal))
        for item_id, _gross, cost in lines:
            if cost > 0:
                known += net[int(item_id)]
            else:
                unknown += net[int(item_id)]
    for refund, cost in connection.execute("""
            SELECT sri.line_refund, ii.unit_cost
            FROM sales_return_items sri JOIN sales_returns sr ON sr.id=sri.return_id
            JOIN invoice_items ii ON ii.id=sri.invoice_item_id
            WHERE sr.business_date BETWEEN ? AND ?""", (date_from, date_to)):
        if cost > 0:
            known -= int(refund)
        else:
            unknown -= int(refund)
    return known, unknown


def unclassified_returns(connection) -> list[tuple]:
    """Historical returns whose refund tender is unknown (administrator reconciliation queue)."""
    return connection.execute("""
        SELECT sr.id,sr.return_number,sr.created_at,sr.total_refund,sr.invoice_id,i.shift_id
        FROM sales_returns sr JOIN invoices i ON i.id=sr.invoice_id
        WHERE sr.refund_method='unknown' ORDER BY sr.id""").fetchall()


def customer_balances(connection) -> list[tuple]:
    """(customer_id, name, balance) where balance = SUM(debit-credit) of the ledger; >0 = owes us."""
    return connection.execute("""
        SELECT c.id,c.name,COALESCE(SUM(l.debit-l.credit),0)
        FROM customers c LEFT JOIN customer_ledger l ON l.customer_id=c.id
        GROUP BY c.id ORDER BY c.name COLLATE NOCASE""").fetchall()


def customer_ledger_discrepancies(connection) -> list[dict]:
    """Compare the ledger with the source documents; an empty list means it reconciles."""
    problems = []
    for cid, name, ledger_debit in connection.execute("""
            SELECT c.id,c.name,COALESCE((SELECT SUM(debit) FROM customer_ledger WHERE customer_id=c.id
                                          AND transaction_type='sale_credit'),0) FROM customers c"""):
        invoiced = connection.execute(
            "SELECT COALESCE(SUM(credit_amount),0) FROM invoices WHERE customer_id=?", (cid,)).fetchone()[0]
        if invoiced != ledger_debit:
            problems.append({"customer_id": cid, "name": name, "kind": "credit_sales",
                             "documents": int(invoiced), "ledger": int(ledger_debit)})
        returned = connection.execute(
            "SELECT COALESCE(SUM(total_refund),0) FROM sales_returns WHERE customer_id=? "
            "AND refund_method='customer_credit'", (cid,)).fetchone()[0]
        ledger_ret = connection.execute(
            "SELECT COALESCE(SUM(credit),0) FROM customer_ledger WHERE customer_id=? "
            "AND transaction_type='sales_return_credit'", (cid,)).fetchone()[0]
        if returned != ledger_ret:
            problems.append({"customer_id": cid, "name": name, "kind": "return_credits",
                             "documents": int(returned), "ledger": int(ledger_ret)})
    return problems


def stock_ledger_discrepancies(connection) -> list[tuple]:
    """Products whose on-hand quantity differs from the sum of their inventory movements."""
    return connection.execute("""
        SELECT p.id,p.name,p.stock_quantity,COALESCE(SUM(m.quantity_delta),0)
        FROM products p LEFT JOIN inventory_movements m ON m.product_id=p.id
        GROUP BY p.id HAVING p.stock_quantity <> COALESCE(SUM(m.quantity_delta),0)""").fetchall()


def inventory_value(connection) -> dict:
    """Stock valued at moving-average cost, with unknown-cost stock reported separately.

    ``value`` covers ONLY units with a known cost; units whose cost is zero/unverified are not valued
    at 0 silently: they are counted in ``unknown_cost_units`` (``zero_cost_units`` is kept as an alias)
    and listed in ``unknown_cost_products``.  The true inventory value is therefore AT LEAST ``value``.
    """
    value, units, unknown_units = connection.execute("""
        SELECT COALESCE(SUM(CASE WHEN purchase_price>0 THEN stock_quantity*purchase_price ELSE 0 END),0),
               COALESCE(SUM(stock_quantity),0),
               COALESCE(SUM(CASE WHEN purchase_price<=0 THEN stock_quantity ELSE 0 END),0)
        FROM products""").fetchone()
    products = products_with_unknown_cost(connection)
    return {"value": int(value), "units": int(units), "zero_cost_units": int(unknown_units),
            "unknown_cost_units": int(unknown_units), "unknown_cost_products": products}
