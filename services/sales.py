from __future__ import annotations
from datetime import datetime
from services.accounting import business_date_for, calculate_expected_cash, cost_snapshot
from services.audit import write_audit_log
from services.customers import create_customer, add_customer_ledger_entry
from services.inventory import record_inventory_movement
from services.security import require_admin_user, require_active_user
from services.settings import get_setting

PAYMENT_CASH = "نقدي"
PAYMENT_CREDIT = "آجل"
PAYMENT_SPLIT = "نقدي + آجل"


def allocate_net_line_values(lines: list[tuple[int, int]], invoice_total: int, invoice_subtotal: int) -> dict[int, int]:
    """Allocate invoice-level discount to lines using largest remainder.

    `lines` is [(invoice_item_id, gross_line_total), ...]. The returned integer
    amounts always sum exactly to the invoice net total when gross lines sum to
    the invoice subtotal. This prevents a few piasters being created/lost when
    several lines are rounded independently.
    """
    if invoice_total < 0 or invoice_subtotal < 0 or invoice_total > invoice_subtotal:
        raise ValueError("Invalid invoice totals")
    if not lines:
        return {}
    gross_sum = sum(int(amount) for _item_id, amount in lines)
    if gross_sum != invoice_subtotal:
        raise ValueError("Invoice lines do not match invoice subtotal")
    if invoice_subtotal == 0:
        return {int(item_id): 0 for item_id, _amount in lines}
    allocations = {}
    remainders = []
    allocated = 0
    for item_id, gross in lines:
        numerator = int(gross) * int(invoice_total)
        base, remainder = divmod(numerator, int(invoice_subtotal))
        allocations[int(item_id)] = base
        allocated += base
        remainders.append((remainder, int(item_id)))
    remaining = int(invoice_total) - allocated
    for _remainder, item_id in sorted(remainders, key=lambda pair: (-pair[0], pair[1]))[:remaining]:
        allocations[item_id] += 1
    return allocations


def calculate_line_return_refund(net_line_value: int, sold_quantity: int,
                                 already_returned_quantity: int,
                                 already_refunded_value: int, requested_quantity: int) -> int:
    """Return the incremental refund for a partial line return (piasters)."""
    values = (net_line_value, sold_quantity, already_returned_quantity,
              already_refunded_value, requested_quantity)
    if any(int(value) < 0 for value in values) or sold_quantity <= 0:
        raise ValueError("Invalid return allocation values")
    if requested_quantity <= 0 or already_returned_quantity + requested_quantity > sold_quantity:
        raise ValueError("Return quantity is outside the sold quantity")
    cumulative_qty = already_returned_quantity + requested_quantity
    # Integer half-up rounding; the final full return equals the allocated line value.
    target_cumulative = (int(net_line_value) * cumulative_qty * 2 + sold_quantity) // (2 * sold_quantity)
    return max(0, target_cumulative - int(already_refunded_value))


def create_sale(connection, *, user: dict, cart_lines: list[dict],
                discount: int, payment_method: str, cash_received: int,
                customer_name: str = "", request_id: str | None = None,
                selected_customer_id: int | None = None,
                new_customer: dict | None = None) -> dict:
    """Create a sale atomically; cart_lines contain product_id and quantity.

    Prices and costs are re-read from the database inside the write transaction.
    A credit balance can only be created for an identified customer.

    Customer identity is ``customer_id`` only (names are not unique):

    * ``selected_customer_id`` - an existing, active customer chosen by id;
    * ``new_customer`` - ``{"name", "phone", "allow_duplicate"}``: an EXPLICIT request to create a
      new debtor inside this sale's transaction (credit/split sales only; a rolled-back sale leaves
      no customer behind);
    * credit and split sales without either are rejected;
    * ``customer_name`` is only a display hint and is NEVER used to find or create a customer, so a
      name typed into a cash sale cannot create or attach anyone.

    ``request_id`` is an idempotency key generated once per checkout attempt. If an
    invoice with the same key already exists (double click, retry after a timeout)
    that invoice is returned with ``duplicate=True`` and nothing new is written.
    """
    if not user or user.get("id") is None:
        raise PermissionError("يجب تسجيل الدخول أولًا")
    if not cart_lines:
        raise ValueError("لا يمكن حفظ فاتورة فارغة")
    if payment_method not in {PAYMENT_CASH, PAYMENT_CREDIT, PAYMENT_SPLIT}:
        raise ValueError("طريقة الدفع غير مدعومة. الدفع بالبطاقة غير متاح للبيع الجديد.")
    if discount < 0 or cash_received < 0:
        raise ValueError("الخصم أو المبلغ النقدي غير صالح")

    connection.execute("BEGIN IMMEDIATE")
    try:
        actual_role = require_active_user(connection, user)
        if request_id:
            prior = connection.execute(
                "SELECT id,daily_number,total,credit_amount,change_amount,customer_id,business_date,user_id "
                "FROM invoices WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if int(prior[7]) != int(user["id"]):
                    raise PermissionError("مفتاح الطلب مستخدم من مستخدم آخر")
                connection.rollback()
                return {"invoice_id": prior[0], "invoice_number": prior[1], "total": prior[2],
                        "cash_amount": prior[2] - prior[3], "credit_amount": prior[3],
                        "change": prior[4], "customer_id": prior[5], "business_date": prior[6],
                        "duplicate": True}
        if discount and actual_role != "admin":
            raise PermissionError("الخصم متاح للمدير فقط")
        shift = connection.execute(
            "SELECT id FROM cash_shifts WHERE status='open' AND user_id=? ORDER BY id DESC LIMIT 1",
            (user["id"],)
        ).fetchone()
        if not shift:
            raise ValueError("لا توجد وردية مفتوحة لهذا المستخدم")
        normalized = []
        subtotal = 0
        # The same product may appear on several cart lines: validate the TOTAL per product so the
        # cashier gets a clear message instead of a raw database CHECK failure.
        requested: dict[int, int] = {}
        for item in cart_lines:
            if int(item["quantity"]) <= 0:
                raise ValueError("كمية المنتج يجب أن تكون أكبر من صفر")
            requested[int(item["product_id"])] = requested.get(int(item["product_id"]), 0) + int(item["quantity"])
        for item in cart_lines:
            product_id = int(item["product_id"])
            quantity = int(item["quantity"])
            product = connection.execute("""
                SELECT id,code,name,COALESCE(size,''),COALESCE(color,''),
                       selling_price,purchase_price,stock_quantity
                FROM products WHERE id=?
            """, (product_id,)).fetchone()
            if not product:
                raise ValueError(f"المنتج رقم {product_id} لم يعد موجودًا")
            if product[7] < requested[product_id]:
                raise ValueError(f"المخزون غير كافٍ للمنتج: {product[2]}")
            if "unit_price" in item and int(item["unit_price"]) != int(product[5]):
                raise ValueError(f"تم تغيير سعر المنتج {product[2]}؛ حدّث الفاتورة قبل الحفظ")
            line_total = int(product[5]) * quantity
            subtotal += line_total
            normalized.append((product, quantity, line_total))

        if discount > subtotal:
            raise ValueError("الخصم لا يمكن أن يتجاوز إجمالي الفاتورة")
        total = subtotal - discount
        if payment_method == PAYMENT_CASH:
            if cash_received < total:
                raise ValueError("المبلغ النقدي المستلم أقل من إجمالي الفاتورة")
            cash_amount, credit_amount = total, 0
            change = cash_received - total
        elif payment_method == PAYMENT_CREDIT:
            if cash_received != 0:
                raise ValueError("في البيع الآجل يجب أن يكون المدفوع النقدي صفرًا")
            cash_amount, credit_amount, change = 0, total, 0
        else:
            if cash_received >= total:
                raise ValueError("في الدفع المختلط يجب أن يكون الجزء الآجل أكبر من صفر؛ استخدم نقدي للدفع الكامل")
            cash_amount, credit_amount, change = cash_received, total - cash_received, 0

        customer_id = None
        if selected_customer_id is not None and new_customer is not None:
            raise ValueError("لا يمكن تحديد عميل موجود وإنشاء عميل جديد في نفس العملية")
        if selected_customer_id is not None:
            if not connection.execute("SELECT 1 FROM customers WHERE id=? AND active=1",
                                      (int(selected_customer_id),)).fetchone():
                raise ValueError("العميل المحدد غير موجود أو غير نشط")
            customer_id = int(selected_customer_id)
        elif new_customer is not None:
            if not credit_amount:
                raise ValueError("إنشاء عميل جديد متاح للبيع الآجل أو المختلط فقط")
            customer_id = create_customer(connection, str(new_customer.get("name", "")),
                                          str(new_customer.get("phone", "")),
                                          allow_duplicate=bool(new_customer.get("allow_duplicate")))
        elif credit_amount:
            raise ValueError("البيع الآجل يتطلب اختيار عميل مسجل أو إنشاء عميل جديد بشكل صريح")

        now = datetime.now()
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        business_day = business_date_for(now)
        invoice_number = "INV-" + now.strftime("%Y%m%d-%H%M%S-%f")
        last_daily = connection.execute(
            "SELECT MAX(CAST(daily_number AS INTEGER)) FROM invoices WHERE business_date=?",
            (business_day,)
        ).fetchone()[0]
        daily_number = f"{(last_daily or 0) + 1:04d}"
        cur = connection.execute("""
            INSERT INTO invoices(invoice_number,created_at,subtotal,discount,total,payment_method,
                amount_paid,change_amount,shift_id,user_id,daily_number,business_date,customer_id,credit_amount,
                request_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (invoice_number,now_text,subtotal,discount,total,payment_method,cash_received,
              change,shift[0],user["id"],daily_number,business_day,customer_id,credit_amount,
              request_id or None))
        invoice_id = int(cur.lastrowid)

        for product, quantity, line_total in normalized:
            connection.execute("""
                INSERT INTO invoice_items(invoice_id,product_id,product_code,product_name,
                    product_size,product_color,quantity,unit_price,line_total,unit_cost)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (invoice_id,product[0],product[1],product[2],product[3],product[4],
                  quantity,product[5],line_total,cost_snapshot(product[6])))
            item_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute("UPDATE products SET stock_quantity=stock_quantity-? WHERE id=?",
                               (quantity,product[0]))
            record_inventory_movement(connection, product_id=product[0],movement_type="sale",
                quantity_delta=-quantity,unit_cost=cost_snapshot(product[6]),reference_type="invoice_item",
                reference_id=invoice_id,reference_line_id=item_id,user_id=user["id"],
                created_at=now_text,reason=f"فاتورة بيع {daily_number}")

        if cash_amount:
            connection.execute("INSERT INTO invoice_payments(invoice_id,tender,amount,customer_id,created_at,user_id) VALUES(?,'cash',?,?,?,?)",
                                (invoice_id,cash_amount,customer_id,now_text,user["id"]))
        if credit_amount:
            connection.execute("INSERT INTO invoice_payments(invoice_id,tender,amount,customer_id,created_at,user_id) VALUES(?,'credit',?,?,?,?)",
                                (invoice_id,credit_amount,customer_id,now_text,user["id"]))
            add_customer_ledger_entry(connection,customer_id=int(customer_id),
                transaction_type="sale_credit",debit=credit_amount,source_type="invoice",
                source_id=invoice_id,user_id=user["id"],shift_id=shift[0],
                notes=f"مديونية فاتورة {daily_number}",created_at=now_text)

        write_audit_log(connection,user_id=user["id"],username=user.get("username",""),
            action="create_sale",entity_type="invoice",entity_id=invoice_id,
            details={"invoice_number":daily_number,"total":total,"cash":cash_amount,
                     "credit":credit_amount,"discount":discount})
        connection.commit()
        return {"invoice_id":invoice_id,"invoice_number":daily_number,"total":total,
                "cash_amount":cash_amount,"credit_amount":credit_amount,"change":change,
                "customer_id":customer_id,"business_date":business_day,"duplicate":False}
    except Exception:
        connection.rollback()
        raise


def process_return(connection, *, user: dict, invoice_id: int,
                   item_quantities: dict[int, int], refund_method: str,
                   disposition: str = "restock") -> dict:
    """Register return, stock movements and cash/account refund atomically.

    ``disposition``: ``restock`` returns units to sellable stock; ``damaged`` records the
    return and immediately writes the units off (stock unchanged, both movements logged).
    Cashiers may process returns only while the admin policy
    ``cashier_can_process_returns`` is enabled (default: enabled).
    """
    if not user or user.get("id") is None:
        raise PermissionError("يجب تسجيل الدخول أولًا")
    if refund_method not in {"نقدي", "رصيد العميل"}:
        raise ValueError("طريقة رد المبلغ غير معروفة")
    if disposition not in {"restock", "damaged"}:
        raise ValueError("حالة المخزون للمرتجع غير معروفة")
    if not item_quantities or any(int(q) <= 0 for q in item_quantities.values()):
        raise ValueError("حدد كمية صحيحة لمرتجع واحد على الأقل")
    connection.execute("BEGIN IMMEDIATE")
    try:
        role = require_active_user(connection, user)
        if role != "admin" and get_setting(connection, "cashier_can_process_returns") != "1":
            raise PermissionError("المرتجعات متاحة للمدير فقط حاليًا")
        invoice = connection.execute(
            "SELECT subtotal,total,customer_id,shift_id FROM invoices WHERE id=?", (invoice_id,)
        ).fetchone()
        if not invoice:
            raise ValueError("الفاتورة الأصلية غير موجودة")
        subtotal,total,customer_id,_original_shift = invoice
        invoice_lines = connection.execute(
            "SELECT id,line_total FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)
        ).fetchall()
        net_line_values = allocate_net_line_values(invoice_lines, int(total), int(subtotal))
        now = datetime.now()
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        business_day = business_date_for(now)
        shift_id = None
        if refund_method == "نقدي":
            shift = connection.execute(
                "SELECT id FROM cash_shifts WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                (user["id"],)
            ).fetchone()
            if not shift:
                raise ValueError("رد المبلغ نقدًا يتطلب وردية مفتوحة لهذا المستخدم")
            shift_id = int(shift[0])
        elif not customer_id:
            raise ValueError("لا يمكن إضافة المرتجع إلى رصيد العميل لأن الفاتورة ليست مرتبطة بحساب عميل")

        selected_lines = []
        refund_total = 0
        if refund_method == "نقدي":
            unknown_prior = int(connection.execute(
                "SELECT COALESCE(SUM(total_refund),0) FROM sales_returns WHERE invoice_id=? AND refund_method='unknown'",
                (invoice_id,)
            ).fetchone()[0])
            if unknown_prior:
                raise ValueError(
                    f"للفـاتورة مرتجعات قديمة بقيمة {unknown_prior} قرش غير محددة طريقة ردها. "
                    "يجب على المدير تصنيف المرتجعات القديمة بعد مراجعة السجلات قبل رد أي مبلغ نقدًا."
                )
            payment_rows = connection.execute(
                "SELECT tender,amount FROM invoice_payments WHERE invoice_id=?", (invoice_id,)
            ).fetchall()
            if payment_rows:
                cash_paid = sum(int(amount) for tender, amount in payment_rows if tender == "cash")
            else:
                legacy_method = connection.execute(
                    "SELECT payment_method FROM invoices WHERE id=?", (invoice_id,)
                ).fetchone()[0]
                cash_paid = int(total) if legacy_method == PAYMENT_CASH else 0
            prior_cash_refunds = int(connection.execute(
                "SELECT COALESCE(SUM(total_refund),0) FROM sales_returns WHERE invoice_id=? AND refund_method='cash'",
                (invoice_id,)
            ).fetchone()[0])
            cash_refundable = max(0, cash_paid - prior_cash_refunds)
        else:
            cash_refundable = None
        for item_id, raw_qty in item_quantities.items():
            qty = int(raw_qty)
            line = connection.execute("""
                SELECT ii.id,ii.product_id,ii.product_name,ii.quantity,ii.unit_price,ii.line_total,
                       ii.unit_cost,p.stock_quantity,
                       COALESCE((SELECT SUM(sri.quantity) FROM sales_return_items sri
                                 WHERE sri.invoice_item_id=ii.id),0) AS returned_qty,
                       COALESCE((SELECT SUM(sri.line_refund) FROM sales_return_items sri
                                 WHERE sri.invoice_item_id=ii.id),0) AS returned_value
                FROM invoice_items ii JOIN products p ON p.id=ii.product_id
                WHERE ii.id=? AND ii.invoice_id=?
            """, (int(item_id),invoice_id)).fetchone()
            if not line:
                raise ValueError(f"سطر الفاتورة {item_id} غير موجود")
            iid,pid,name,sold_qty,unit_price,line_total,unit_cost,stock,already_qty,already_value = line
            available = sold_qty - already_qty
            if qty > available:
                raise ValueError(f"الكمية المتاحة للإرجاع من {name} هي {available}")
            net_line_value = net_line_values.get(int(iid), 0)
            line_refund = calculate_line_return_refund(
                net_line_value, int(sold_qty), int(already_qty), int(already_value), qty
            )
            refund_total += line_refund
            selected_lines.append((iid,pid,name,qty,unit_cost,line_refund))

        if refund_method == "نقدي" and refund_total > cash_refundable:
            raise ValueError(
                f"قيمة الرد النقدي ({refund_total}) تتجاوز المبلغ النقدي المتاح للرد ({cash_refundable}). "
                "رد الجزء المتبقي إلى رصيد العميل إذا كان له حساب."
            )
        if refund_method == "نقدي" and shift_id is not None and refund_total:
            # Same rule as expenses/withdrawals: a cash refund is paid out of the drawer, so it cannot
            # exceed the cash the drawer is expected to hold (expected cash must never go negative).
            drawer_cash = calculate_expected_cash(connection, int(shift_id))
            if refund_total > drawer_cash:
                raise ValueError(
                    f"الخزنة لا تحتوي على نقدية كافية للرد ({refund_total} قرش مطلوب، المتوقع في الخزنة {drawer_cash} قرش). "
                    "سجّل إيداعًا في الخزنة أولًا أو رد المبلغ إلى رصيد العميل."
                )
        return_number = "RET-" + now.strftime("%Y%m%d-%H%M%S-%f")
        cur = connection.execute("""
            INSERT INTO sales_returns(return_number,invoice_id,created_at,total_refund,notes,
                refund_method,shift_id,user_id,customer_id,business_date)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (return_number,invoice_id,now_text,refund_total,"مرتجع مرتبط بفاتورة بيع",
              "cash" if refund_method == "نقدي" else "customer_credit",shift_id,
              user["id"],customer_id,business_day))
        return_id = int(cur.lastrowid)
        for item_id,pid,name,qty,unit_cost,line_refund in selected_lines:
            unit_refund = (line_refund + qty//2) // qty if qty else 0
            cur_item = connection.execute("""
                INSERT INTO sales_return_items(return_id,invoice_item_id,product_id,product_name,
                    quantity,unit_refund,line_refund,disposition) VALUES(?,?,?,?,?,?,?,?)
            """, (return_id,item_id,pid,name,qty,unit_refund,line_refund,disposition))
            line_id = int(cur_item.lastrowid)
            record_inventory_movement(connection,product_id=pid,movement_type="sales_return",
                quantity_delta=qty,unit_cost=unit_cost,reference_type="sales_return_item",
                reference_id=return_id,reference_line_id=line_id,user_id=user["id"],
                created_at=now_text,reason=f"مرتجع {return_number}")
            if disposition == "restock":
                connection.execute("UPDATE products SET stock_quantity=stock_quantity+? WHERE id=?",(qty,pid))
            else:
                # Damaged return: units come back and are written off in the same transaction,
                # so on-hand stock is unchanged and the ledger shows both movements.
                record_inventory_movement(connection,product_id=pid,movement_type="adjustment",
                    quantity_delta=-qty,unit_cost=unit_cost,reference_type="return_damage",
                    reference_id=return_id,reference_line_id=line_id,user_id=user["id"],
                    created_at=now_text,reason=f"مرتجع تالف {return_number}")

        if refund_method == "رصيد العميل" and refund_total:
            add_customer_ledger_entry(connection,customer_id=int(customer_id),
                transaction_type="sales_return_credit",credit=refund_total,
                source_type="sales_return",source_id=return_id,user_id=user["id"],
                notes=f"إضافة رصيد بسبب المرتجع {return_number}",created_at=now_text)

        write_audit_log(connection,user_id=user["id"],username=user.get("username",""),
            action="sales_return",entity_type="sales_return",entity_id=return_id,
            details={"return_number":return_number,"invoice_id":invoice_id,
                     "amount":refund_total,"refund_method":refund_method})
        connection.commit()
        return {"return_id":return_id,"return_number":return_number,
                "refund_total":refund_total,"shift_id":shift_id,"customer_id":customer_id,
                "disposition":disposition}
    except Exception:
        connection.rollback()
        raise


def classify_legacy_return(connection, *, admin_user: dict, return_id: int,
                           refund_method: str, shift_id: int | None = None) -> dict:
    """Admin-controlled classification of legacy returns whose tender was unknown.

    This intentionally requires an explicit human decision. The application
    cannot infer whether an old refund was cash or customer credit.
    """
    require_admin_user(admin_user)
    admin_row = connection.execute("SELECT role,active FROM users WHERE id=?", (admin_user.get("id"),)).fetchone()
    if not admin_row or admin_row[0] != "admin" or not admin_row[1]:
        raise PermissionError("يجب استخدام حساب مدير نشط لتصنيف المرتجعات التاريخية")
    if refund_method not in {"نقدي", "رصيد العميل"}:
        raise ValueError("طريقة رد المبلغ غير مدعومة")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("""
            SELECT sr.id,sr.invoice_id,sr.total_refund,sr.refund_method,i.customer_id,i.shift_id
            FROM sales_returns sr JOIN invoices i ON i.id=sr.invoice_id WHERE sr.id=?
        """, (int(return_id),)).fetchone()
        if not row:
            raise ValueError("المرتجع غير موجود")
        _rid, invoice_id, amount, current_method, invoice_customer_id, _original_shift_id = row
        if current_method != "unknown":
            raise ValueError("هذا المرتجع مصنّف بالفعل ولا يمكن تغيير تسويته")
        now_text = datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        if refund_method == "نقدي":
            selected_shift = int(shift_id or 0)
            shift = connection.execute("SELECT id,user_id,status,expected_cash,actual_cash FROM cash_shifts WHERE id=?", (selected_shift,)).fetchone()
            if not shift:
                raise ValueError("اختر الوردية التي تم رد المبلغ منها وفق السجلات الفعلية")
            user_id = int(shift[1])
            customer_id = invoice_customer_id
            tender_code = "cash"
        else:
            if not invoice_customer_id:
                raise ValueError("الفاتورة الأصلية غير مرتبطة بحساب عميل؛ لا يمكن تصنيف المرتجع كرصد عميل")
            selected_shift = None
            user_id = int(admin_user["id"])
            customer_id = int(invoice_customer_id)
            tender_code = "customer_credit"
        connection.execute("""
            UPDATE sales_returns SET refund_method=?,shift_id=?,user_id=?,customer_id=? WHERE id=?
        """, (tender_code,selected_shift,user_id,customer_id,int(return_id)))
        old_expected = None
        new_expected = None
        if tender_code == "cash":
            old_expected = shift[3]
            new_expected = calculate_expected_cash(connection, int(selected_shift))
            actual_cash = shift[4]
            difference = (int(actual_cash) - new_expected) if actual_cash is not None else None
            connection.execute("UPDATE cash_shifts SET expected_cash=?,difference=? WHERE id=?",
                               (new_expected,difference,selected_shift))
        if tender_code == "customer_credit" and int(amount) > 0:
            add_customer_ledger_entry(connection,customer_id=int(customer_id),
                transaction_type="sales_return_credit",credit=int(amount),
                source_type="sales_return",source_id=int(return_id),
                user_id=int(admin_user["id"]),notes="تصنيف مرتجع تاريخي كرصيد عميل بعد مراجعة السجلات",
                created_at=now_text)
        write_audit_log(connection,user_id=int(admin_user["id"]),
            username=admin_user.get("username", ""),action="legacy_return_classified",
            entity_type="sales_return",entity_id=int(return_id),
            details={"invoice_id":int(invoice_id),"amount":int(amount),
                     "refund_method":refund_method,"shift_id":selected_shift,
                     "old_expected_cash":old_expected,"new_expected_cash":new_expected})
        connection.commit()
        return {"return_id":int(return_id),"refund_total":int(amount),
                "refund_method":refund_method,"shift_id":selected_shift,
                "old_expected_cash":old_expected,"new_expected_cash":new_expected}
    except Exception:
        connection.rollback()
        raise
