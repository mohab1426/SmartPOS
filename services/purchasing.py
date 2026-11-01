"""Purchase receiving: one atomic, admin-only operation (supplier invoice -> stock + cost)."""
from __future__ import annotations

from datetime import datetime

from services.accounting import business_date_for
from services.audit import write_audit_log
from services.inventory import (UnknownCostError, get_or_create_product_group, products_with_unknown_cost,
                                receive_purchase_line)
from services.security import require_active_admin
from services.tx import write_transaction


def receive_purchase(connection, *, user: dict, supplier_id: int | None, supplier_name: str,
                     notes: str, lines: list[dict], new_products: dict | None = None,
                     opening_costs: dict[int, int] | None = None) -> dict:
    """Create a purchase invoice and receive every line (moving weighted-average cost).

    ``lines``: dicts with ``quantity``, ``cost`` (piasters), ``code``, ``name`` and either
    ``product_id`` or ``temp_id`` pointing into ``new_products`` (product created at receipt).

    ``opening_costs`` maps ``product_id -> unit cost (piasters)`` for products that already hold stock
    of UNKNOWN cost.  Without it such a purchase is refused with ``UnknownCostError`` (nothing is
    written) so unknown historical stock is never averaged in as if it were free.  Each supplied
    opening cost is an explicit administrator decision and is audited.
    """
    if not lines:
        raise ValueError("فاتورة الشراء فارغة")
    for line in lines:
        if int(line["quantity"]) <= 0 or int(line["cost"]) < 0:
            raise ValueError("كمية الشراء يجب أن تكون موجبة والتكلفة غير سالبة")
    new_products = new_products or {}
    opening_costs = {int(k): int(v) for k, v in (opening_costs or {}).items()}
    total = sum(int(x["quantity"]) * int(x["cost"]) for x in lines)
    now = datetime.now()
    invoice_number = "PUR-" + now.strftime("%Y%m%d-%H%M%S-%f")
    with write_transaction(connection):
        require_active_admin(connection, user)
        existing_ids = [int(x["product_id"]) for x in lines if x.get("product_id") is not None]
        unresolved = [p for p in products_with_unknown_cost(connection, existing_ids) if p["id"] not in opening_costs]
        if unresolved:
            raise UnknownCostError(unresolved)
        if any(v <= 0 for v in opening_costs.values()):
            raise ValueError("تكلفة المخزون الحالي المحددة يجب أن تكون أكبر من صفر")
        resolved = {p["id"]: p for p in products_with_unknown_cost(connection, existing_ids)}
        cur = connection.execute(
            "INSERT INTO purchase_invoices(invoice_number,supplier_name,total,notes,supplier_id,business_date) "
            "VALUES(?,?,?,?,?,?)",
            (invoice_number, supplier_name or "", total, (notes or "").strip(), supplier_id,
             business_date_for(now)))
        purchase_id = int(cur.lastrowid)
        created: dict = {}
        audited: set[int] = set()
        for line in lines:
            product_id = line.get("product_id")
            if product_id is None:
                temp_id = line["temp_id"]
                if temp_id not in created:
                    p = new_products[temp_id]
                    connection.execute("INSERT OR IGNORE INTO product_categories(name) VALUES(?)", (p["category"],))
                    group_id = get_or_create_product_group(connection, p["name"])
                    created[temp_id] = connection.execute(
                        "INSERT INTO products(code,barcode,name,size,color,category,purchase_price,"
                        "selling_price,stock_quantity,min_stock,variant_group_id) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                        (p["code"], p["barcode"], p["name"], p["size"], p["color"], p["category"],
                         int(line["cost"]), p["selling"], p["minimum"], group_id)).lastrowid
                product_id = created[temp_id]
            receive_purchase_line(connection, purchase_invoice_id=purchase_id, product_id=product_id,
                product_code=line["code"], product_name=line["name"], quantity=int(line["quantity"]),
                unit_cost=int(line["cost"]), user_id=user["id"],
                opening_unit_cost=opening_costs.get(int(product_id)) if int(product_id) in resolved else None)
            if int(product_id) in resolved and int(product_id) not in audited:
                audited.add(int(product_id))
                write_audit_log(connection, user_id=user["id"], username=user.get("username", ""),
                                action="unknown_cost_resolved", entity_type="product", entity_id=product_id,
                                details={"units_on_hand": resolved[int(product_id)]["stock"],
                                         "admin_opening_unit_cost": opening_costs[int(product_id)],
                                         "purchase_id": purchase_id})
        write_audit_log(connection, user_id=user["id"], username=user.get("username", ""),
                        action="purchase_received", entity_type="purchase_invoice", entity_id=purchase_id,
                        details={"invoice_number": invoice_number, "total": total})
    return {"purchase_id": purchase_id, "invoice_number": invoice_number, "total": total}
