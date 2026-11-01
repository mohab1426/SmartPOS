"""Product, category and supplier maintenance (service layer).

Every function re-checks the acting user against the live ``users`` table, so a cashier cannot
reach these operations through any alternate code path, and writes an ``audit_log`` row that
contains the old and new values (price edits are the classic fraud vector).
"""
from __future__ import annotations

import sqlite3

from services.audit import write_audit_log
from services.inventory import adjust_stock, get_or_create_product_group, record_inventory_movement
from services.security import require_active_admin
from services.tx import write_transaction

DEFAULT_CATEGORY = "عام"
_PRODUCT_REFERENCES = (
    ("invoice_items", "فواتير البيع"),
    ("purchase_items", "فواتير الشراء"),
    ("sales_return_items", "المرتجعات"),
    ("inventory_movements", "سجل حركات المخزون"),
)


def _who(actor: dict) -> dict:
    return {"user_id": actor["id"], "username": actor.get("username", "")}


def _ensure_categories(connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS product_categories (name TEXT PRIMARY KEY COLLATE NOCASE)")
    connection.execute(
        "INSERT OR IGNORE INTO product_categories(name) "
        "SELECT DISTINCT COALESCE(NULLIF(trim(category),''),?) FROM products", (DEFAULT_CATEGORY,))


def _money(value, label: str) -> int:
    if isinstance(value, bool) or int(value) != value or int(value) < 0:
        raise ValueError(f"{label} يجب أن يكون عددًا صحيحًا غير سالب (بالقرش)")
    return int(value)


def create_product(connection, *, actor: dict, code: str, name: str, purchase_price: int,
                   selling_price: int, quantity: int, min_stock: int, barcode: str | None = None,
                   size: str = "", color: str = "", category: str = DEFAULT_CATEGORY) -> int:
    code, name = (code or "").strip(), (name or "").strip()
    if not code or not name:
        raise ValueError("كود المنتج واسمه مطلوبان")
    purchase_price, selling_price = _money(purchase_price, "سعر الشراء"), _money(selling_price, "سعر البيع")
    quantity, min_stock = _money(quantity, "الكمية"), _money(min_stock, "الحد الأدنى")
    category = (category or "").strip() or DEFAULT_CATEGORY
    with write_transaction(connection):
        require_active_admin(connection, actor)
        _ensure_categories(connection)
        connection.execute("INSERT OR IGNORE INTO product_categories(name) VALUES(?)", (category,))
        group_id = get_or_create_product_group(connection, name)
        pid = connection.execute(
            "INSERT INTO products(code,barcode,name,size,color,category,purchase_price,selling_price,"
            "stock_quantity,min_stock,variant_group_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (code, barcode or None, name, size, color, category, purchase_price, selling_price,
             quantity, min_stock, group_id)).lastrowid
        if quantity:
            record_inventory_movement(connection, product_id=pid, movement_type="opening_balance",
                                      quantity_delta=quantity, unit_cost=purchase_price,
                                      reference_type="product_creation", reference_id=pid,
                                      user_id=actor["id"], reason="رصيد افتتاحي عند إنشاء المنتج")
        write_audit_log(connection, **_who(actor), action="product_created", entity_type="product",
                        entity_id=pid, details={"code": code, "name": name, "quantity": quantity,
                                                "selling_price": selling_price,
                                                "purchase_price": purchase_price})
    return int(pid)


def update_product(connection, *, actor: dict, product_id: int, code: str, barcode: str | None,
                   name: str, size: str, color: str, category: str, selling_price: int,
                   min_stock: int, new_quantity: int | None = None, stock_reason: str = "") -> dict:
    """Update a product; returns ``{"changes": {field: [old, new]}}``.

    A quantity change needs a non-empty ``stock_reason`` and is written as an auditable
    inventory movement (the UI must collect the reason *before* calling, not mid-transaction).
    """
    code, name = (code or "").strip(), (name or "").strip()
    if not code or not name:
        raise ValueError("كود المنتج واسمه مطلوبان")
    selling_price, min_stock = _money(selling_price, "سعر البيع"), _money(min_stock, "الحد الأدنى")
    category = (category or "").strip() or DEFAULT_CATEGORY
    with write_transaction(connection):
        require_active_admin(connection, actor)
        old = connection.execute(
            "SELECT code,COALESCE(barcode,''),name,COALESCE(size,''),COALESCE(color,''),"
            "COALESCE(NULLIF(trim(category),''),?),selling_price,min_stock,stock_quantity "
            "FROM products WHERE id=?", (DEFAULT_CATEGORY, product_id)).fetchone()
        if not old:
            raise ValueError("المنتج لم يعد موجودًا")
        if new_quantity is not None and int(new_quantity) != old[8] and not (stock_reason or "").strip():
            raise ValueError("سبب تعديل المخزون مطلوب")
        _ensure_categories(connection)
        connection.execute("INSERT OR IGNORE INTO product_categories(name) VALUES(?)", (category,))
        group_id = get_or_create_product_group(connection, name)
        connection.execute(
            "UPDATE products SET code=?,barcode=?,name=?,size=?,color=?,category=?,selling_price=?,"
            "min_stock=?,variant_group_id=? WHERE id=?",
            (code, barcode or None, name, size, color, category, selling_price, min_stock, group_id,
             product_id))
        new = (code, barcode or "", name, size, color, category, selling_price, min_stock)
        fields = ("code", "barcode", "name", "size", "color", "category", "selling_price", "min_stock")
        changes = {f: [o, n] for f, o, n in zip(fields, old[:8], new) if o != n}
        if new_quantity is not None and int(new_quantity) != old[8]:
            adjust_stock(connection, product_id=product_id, new_quantity=int(new_quantity),
                         user_id=actor["id"], reason=stock_reason)
            changes["stock_quantity"] = [old[8], int(new_quantity)]
        if changes:
            write_audit_log(connection, **_who(actor), action="product_updated", entity_type="product",
                            entity_id=product_id, details={"changes": changes})
            if "selling_price" in changes:
                write_audit_log(connection, **_who(actor), action="product_price_changed",
                                entity_type="product", entity_id=product_id,
                                details={"old": old[6], "new": selling_price, "code": code})
    return {"changes": changes}


def delete_product(connection, *, actor: dict, product_id: int) -> None:
    with write_transaction(connection):
        require_active_admin(connection, actor)
        row = connection.execute("SELECT code,name FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise ValueError("المنتج لم يعد موجودًا")
        for table, label in _PRODUCT_REFERENCES:
            if connection.execute(f"SELECT 1 FROM {table} WHERE product_id=? LIMIT 1",  # noqa: S608 constant names
                                  (product_id,)).fetchone():
                raise ValueError(f"لا يمكن حذف المنتج لأنه مرتبط بسجلات {label}. "
                                 "يمكنك تعديل بياناته أو تركه بكمية صفر للحفاظ على السجلات القديمة.")
        connection.execute("DELETE FROM products WHERE id=?", (product_id,))
        write_audit_log(connection, **_who(actor), action="product_deleted", entity_type="product",
                        entity_id=product_id, details={"code": row[0], "name": row[1]})


def add_category(connection, *, actor: dict, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("اسم التصنيف مطلوب")
    with write_transaction(connection):
        require_active_admin(connection, actor)
        _ensure_categories(connection)
        if connection.execute("SELECT 1 FROM product_categories WHERE name=? COLLATE NOCASE", (name,)).fetchone():
            raise ValueError("التصنيف موجود بالفعل")
        connection.execute("INSERT INTO product_categories(name) VALUES(?)", (name,))
        write_audit_log(connection, **_who(actor), action="category_created", entity_type="category",
                        entity_id=name)


def rename_category(connection, *, actor: dict, old_name: str, new_name: str) -> None:
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("اسم التصنيف مطلوب")
    if old_name.casefold() == DEFAULT_CATEGORY.casefold():
        raise ValueError("لا يمكن تغيير اسم التصنيف الافتراضي «عام»")
    with write_transaction(connection):
        require_active_admin(connection, actor)
        _ensure_categories(connection)
        if new_name.casefold() != old_name.casefold() and connection.execute(
                "SELECT 1 FROM product_categories WHERE name=? COLLATE NOCASE", (new_name,)).fetchone():
            raise ValueError("يوجد تصنيف بالاسم نفسه بالفعل")
        connection.execute("UPDATE product_categories SET name=? WHERE name=? COLLATE NOCASE", (new_name, old_name))
        connection.execute("UPDATE products SET category=? WHERE category=? COLLATE NOCASE", (new_name, old_name))
        write_audit_log(connection, **_who(actor), action="category_renamed", entity_type="category",
                        entity_id=old_name, details={"old": old_name, "new": new_name})


def delete_category(connection, *, actor: dict, name: str) -> None:
    if name.casefold() == DEFAULT_CATEGORY.casefold():
        raise ValueError("لا يمكن حذف تصنيف «عام» الافتراضي")
    with write_transaction(connection):
        require_active_admin(connection, actor)
        _ensure_categories(connection)
        used = connection.execute(
            "SELECT COUNT(*) FROM products WHERE COALESCE(NULLIF(trim(category),''),?)=? COLLATE NOCASE",
            (DEFAULT_CATEGORY, name)).fetchone()[0]
        if used:
            raise ValueError(f"لا يمكن حذف «{name}» لأنه مرتبط بـ {used} منتج")
        connection.execute("DELETE FROM product_categories WHERE name=? COLLATE NOCASE", (name,))
        write_audit_log(connection, **_who(actor), action="category_deleted", entity_type="category",
                        entity_id=name)


def save_supplier(connection, *, actor: dict, name: str, phone: str = "", address: str = "",
                  notes: str = "", supplier_id: int | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("اسم المورد مطلوب")
    with write_transaction(connection):
        require_active_admin(connection, actor)
        try:
            if supplier_id is None:
                supplier_id = connection.execute(
                    "INSERT INTO suppliers(name,phone,address,notes) VALUES(?,?,?,?)",
                    (name, phone.strip(), address.strip(), notes.strip())).lastrowid
                action, old = "supplier_created", None
            else:
                old = connection.execute("SELECT name,phone,address,notes FROM suppliers WHERE id=?",
                                         (supplier_id,)).fetchone()
                if not old:
                    raise ValueError("المورد لم يعد موجودًا")
                connection.execute("UPDATE suppliers SET name=?,phone=?,address=?,notes=? WHERE id=?",
                                   (name, phone.strip(), address.strip(), notes.strip(), supplier_id))
                action = "supplier_updated"
        except sqlite3.IntegrityError as exc:
            raise ValueError("يوجد مورد مسجل بنفس الاسم. استخدم اسمًا مختلفًا.") from exc
        write_audit_log(connection, **_who(actor), action=action, entity_type="supplier",
                        entity_id=supplier_id,
                        details={"old": list(old) if old else None,
                                 "new": [name, phone.strip(), address.strip(), notes.strip()]})
    return int(supplier_id)


def delete_supplier(connection, *, actor: dict, supplier_id: int) -> None:
    with write_transaction(connection):
        require_active_admin(connection, actor)
        row = connection.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not row:
            raise ValueError("المورد لم يعد موجودًا")
        if connection.execute("SELECT COUNT(*) FROM purchase_invoices WHERE supplier_id=?",
                              (supplier_id,)).fetchone()[0]:
            raise ValueError("المورد مرتبط بفواتير شراء. احتفظ به حتى لا تفقد سجل الموردين.")
        connection.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
        write_audit_log(connection, **_who(actor), action="supplier_deleted", entity_type="supplier",
                        entity_id=supplier_id, details={"name": row[0]})
