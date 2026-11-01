from __future__ import annotations

from datetime import datetime
from services.accounting import business_date_for, cost_snapshot, is_cost_unknown, moving_weighted_average_cost
from services.security import require_active_admin
from services.tx import write_transaction


class UnknownCostError(ValueError):
    """Receiving stock for products that already hold units of unknown cost.

    ``products`` is a list of ``{"id", "code", "name", "stock"}`` dicts so the UI can ask the
    administrator for an explicit opening cost for each of them.
    """

    def __init__(self, products: list[dict]):
        self.products = products
        names = "، ".join(p["name"] for p in products)
        super().__init__(
            "تكلفة المخزون الحالي غير معروفة للأصناف: " + names + ". استلام كمية جديدة سيحسب متوسط التكلفة "
            "وكأن المخزون القديم مجاني (يُظهر ربحًا أعلى من الحقيقي). حدّد تكلفة الوحدة للمخزون الحالي صراحةً قبل الاستلام.")


def products_with_unknown_cost(connection, product_ids=None) -> list[dict]:
    """Products holding units (stock > 0) whose recorded cost is zero / unverified."""
    sql = "SELECT id,code,name,stock_quantity,purchase_price FROM products WHERE stock_quantity>0 AND purchase_price<=0"
    params: tuple = ()
    if product_ids is not None:
        ids = sorted({int(i) for i in product_ids})
        if not ids:
            return []
        sql += " AND id IN (" + ",".join("?" * len(ids)) + ")"
        params = tuple(ids)
    rows = connection.execute(sql + " ORDER BY name,id", params).fetchall()
    return [{"id": int(r[0]), "code": r[1], "name": r[2], "stock": int(r[3])} for r in rows]


def record_inventory_movement(connection, *, product_id: int, movement_type: str,
                              quantity_delta: int, unit_cost: int = -1,
                              reference_type: str = "", reference_id: int | None = None,
                              reference_line_id: int | None = None,
                              user_id: int | None = None, reason: str = "",
                              created_at: str | None = None) -> int:
    if quantity_delta == 0:
        return 0
    timestamp = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    business_date = business_date_for(timestamp)
    cursor = connection.execute("""
        INSERT INTO inventory_movements(
            product_id,movement_type,quantity_delta,unit_cost,reference_type,
            reference_id,reference_line_id,user_id,created_at,business_date,reason
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (product_id, movement_type, quantity_delta, unit_cost, reference_type,
          reference_id, reference_line_id, user_id, timestamp, business_date, reason))
    return int(cursor.lastrowid)


def adjust_stock(connection, *, product_id: int, new_quantity: int, user_id: int,
                 reason: str) -> int:
    if new_quantity < 0:
        raise ValueError("Stock quantity cannot be negative")
    if not (reason or "").strip():
        raise ValueError("A stock adjustment reason is required")
    with write_transaction(connection):
        require_active_admin(connection, {"id": user_id})
        product = connection.execute(
            "SELECT stock_quantity,purchase_price FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if not product:
            raise ValueError("Product not found")
        old_quantity, unit_cost = map(int, product)
        delta = new_quantity - old_quantity
        if delta:
            connection.execute("UPDATE products SET stock_quantity=? WHERE id=?", (new_quantity, product_id))
            record_inventory_movement(connection, product_id=product_id,
                movement_type="adjustment", quantity_delta=delta, unit_cost=cost_snapshot(unit_cost),
                reference_type="manual_adjustment", reference_id=None,
                user_id=user_id, reason=reason.strip())
    return delta


def record_damage(connection, *, product_id: int, quantity: int, user_id: int, reason: str) -> int:
    """Write off damaged/unsellable units. Admin only, reason mandatory, never below zero."""
    if quantity <= 0:
        raise ValueError("Damaged quantity must be positive")
    if not (reason or "").strip():
        raise ValueError("A damage reason is required")
    with write_transaction(connection):
        require_active_admin(connection, {"id": user_id})
        product = connection.execute(
            "SELECT stock_quantity,purchase_price FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            raise ValueError("Product not found")
        if quantity > int(product[0]):
            raise ValueError("Damaged quantity exceeds stock on hand")
        connection.execute("UPDATE products SET stock_quantity=stock_quantity-? WHERE id=?",
                           (quantity, product_id))
        return record_inventory_movement(connection, product_id=product_id, movement_type="adjustment",
            quantity_delta=-quantity, unit_cost=cost_snapshot(product[1]), reference_type="damage",
            user_id=user_id, reason=reason.strip())


def apply_stocktake(connection, *, counts: dict[int, int], user_id: int, reason: str) -> list[dict]:
    """Apply a physical count atomically. Returns one dict per variance (zero variances are skipped)."""
    if not (reason or "").strip():
        raise ValueError("A stocktake reason/reference is required")
    if not counts:
        raise ValueError("No counted products supplied")
    if any(int(q) < 0 for q in counts.values()):
        raise ValueError("Counted quantity cannot be negative")
    variances: list[dict] = []
    with write_transaction(connection):
        require_active_admin(connection, {"id": user_id})
        for product_id, counted in counts.items():
            row = connection.execute(
                "SELECT stock_quantity,purchase_price FROM products WHERE id=?", (int(product_id),)).fetchone()
            if not row:
                raise ValueError(f"Product {product_id} not found")
            system_qty, cost = int(row[0]), int(row[1])
            delta = int(counted) - system_qty
            if delta == 0:
                continue
            connection.execute("UPDATE products SET stock_quantity=? WHERE id=?", (int(counted), int(product_id)))
            record_inventory_movement(connection, product_id=int(product_id), movement_type="adjustment",
                quantity_delta=delta, unit_cost=cost_snapshot(cost), reference_type="stocktake",
                user_id=user_id, reason=reason.strip())
            variances.append({"product_id": int(product_id), "system": system_qty,
                              "counted": int(counted), "delta": delta})
    return variances


def receive_purchase_line(connection, *, purchase_invoice_id: int, product_id: int,
                          product_code: str, product_name: str, quantity: int,
                          unit_cost: int, user_id: int | None,
                          opening_unit_cost: int | None = None) -> int:
    """Receive one purchase line and return the new moving-average cost.

    When the product already holds units whose cost is unknown (cost <= 0, stock > 0) the old units
    are NEVER treated as free: the caller must pass ``opening_unit_cost`` (an explicit, audited
    administrator decision, > 0), otherwise ``UnknownCostError`` is raised and nothing is written.
    """
    if user_id is None:
        raise PermissionError("Purchase receiving requires an authenticated administrator")
    require_active_admin(connection, {"id": user_id})
    if quantity <= 0 or unit_cost < 0:
        raise ValueError("Purchase quantity must be positive and unit cost non-negative")
    product = connection.execute(
        "SELECT stock_quantity,purchase_price FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if not product:
        raise ValueError(f"Product {product_id} not found")
    old_qty, old_cost = map(int, product)
    if old_qty > 0 and is_cost_unknown(old_cost):
        if opening_unit_cost is None:
            row = connection.execute("SELECT id,code,name,stock_quantity FROM products WHERE id=?", (product_id,)).fetchone()
            raise UnknownCostError([{"id": int(row[0]), "code": row[1], "name": row[2], "stock": int(row[3])}])
        if int(opening_unit_cost) <= 0:
            raise ValueError("تكلفة المخزون الحالي المحددة يجب أن تكون أكبر من صفر")
        old_cost = int(opening_unit_cost)
    new_cost = moving_weighted_average_cost(old_qty, old_cost, quantity, unit_cost)
    line_total = quantity * unit_cost
    cur = connection.execute("""
        INSERT INTO purchase_items(
            purchase_invoice_id,product_id,product_code,product_name,quantity,unit_cost,line_total
        ) VALUES(?,?,?,?,?,?,?)
    """, (purchase_invoice_id, product_id, product_code, product_name,
          quantity, unit_cost, line_total))
    connection.execute("UPDATE products SET stock_quantity=?,purchase_price=? WHERE id=?",
                       (old_qty + quantity, new_cost, product_id))
    record_inventory_movement(connection, product_id=product_id, movement_type="purchase",
        quantity_delta=quantity, unit_cost=unit_cost, reference_type="purchase_item",
        reference_id=purchase_invoice_id, reference_line_id=cur.lastrowid,
        user_id=user_id, reason=f"استلام فاتورة شراء {purchase_invoice_id}")
    return new_cost


def get_stock_ledger_balance(connection, product_id: int) -> int:
    return int(connection.execute(
        "SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements WHERE product_id=?",
        (product_id,)
    ).fetchone()[0])


def get_or_create_product_group(connection, name: str) -> int:
    """Every apparel SKU is attached to a parent style group by its base name."""
    normalized = (name or "").strip()
    if not normalized:
        raise ValueError("Product group name is required")
    connection.execute("INSERT OR IGNORE INTO product_groups(name) VALUES(?)", (normalized,))
    row = connection.execute("SELECT id FROM product_groups WHERE name=? COLLATE NOCASE", (normalized,)).fetchone()
    return int(row[0])
