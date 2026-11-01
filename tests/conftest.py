from __future__ import annotations

from pathlib import Path

import pytest

import database
from services.auth import hash_password

ADMIN_PASSWORD = "Admin#Pass-2026"
CASHIER_PASSWORD = "Cashier#Pass-2026"


@pytest.fixture
def store_db(tmp_path: Path, monkeypatch):
    """Create an isolated migrated SmartPOS database with one admin, SKU and shift."""
    data_dir = tmp_path / "user-data"
    config_dir = tmp_path / "user-config"
    legacy_dir = tmp_path / "legacy-install-data"
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    monkeypatch.setattr(database, "DATABASE_PATH", data_dir / "smartpos.db")
    monkeypatch.setattr(database, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(database, "BACKUP_DIR", data_dir / "backups")
    monkeypatch.setattr(database, "LOG_DIR", data_dir / "logs")
    monkeypatch.setattr(database, "MIGRATION_BACKUPS_DIR", data_dir / "migration_backups")
    monkeypatch.setattr(database, "LEGACY_DATA_DIR", legacy_dir)
    monkeypatch.setattr(database, "_STORAGE_READY", False)

    database.initialize_database()
    con = database.get_connection()
    con.execute(
        "INSERT INTO users(username,display_name,password_hash,role,active) VALUES(?,?,?,?,1)",
        ("admin", "Store Admin", hash_password(ADMIN_PASSWORD), "admin"),
    )
    user_id = con.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    con.execute(
        "INSERT INTO products(code,name,purchase_price,selling_price,stock_quantity,min_stock) VALUES(?,?,?,?,?,?)",
        ("SKU-1", "Test Shirt", 400, 1000, 10, 1),
    )
    product_id = con.execute("SELECT id FROM products WHERE code='SKU-1'").fetchone()[0]
    con.execute(
        "INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,unit_cost,reference_type,reference_id,reason) "
        "VALUES(?,'opening_balance',10,400,'test_fixture',?,'fixture stock')",
        (product_id, product_id),
    )
    con.execute("INSERT INTO cash_shifts(user_id,opening_cash,status) VALUES(?,10000,'open')", (user_id,))
    shift_id = con.execute("SELECT id FROM cash_shifts WHERE user_id=?", (user_id,)).fetchone()[0]
    con.commit()
    user = {"id": user_id, "username": "admin", "display_name": "Store Admin", "role": "admin", "active": 1}
    con.close()

    yield {
        "path": database.DATABASE_PATH,
        "data_dir": data_dir,
        "legacy_dir": legacy_dir,
        "user": user,
        "product_id": product_id,
        "shift_id": shift_id,
        "admin_password": ADMIN_PASSWORD,
    }


@pytest.fixture
def cashier(store_db):
    """A second, non-admin user with her own open shift (created on demand)."""
    con = database.get_connection()
    try:
        con.execute("INSERT INTO users(username,display_name,password_hash,role,active) VALUES(?,?,?,?,1)",
                    ("cashier1", "Cashier One", hash_password(CASHIER_PASSWORD), "cashier"))
        uid = con.execute("SELECT id FROM users WHERE username='cashier1'").fetchone()[0]
        con.execute("INSERT INTO cash_shifts(user_id,opening_cash,status) VALUES(?,5000,'open')", (uid,))
        sid = con.execute("SELECT id FROM cash_shifts WHERE user_id=?", (uid,)).fetchone()[0]
        con.commit()
    finally:
        con.close()
    return {"id": uid, "username": "cashier1", "display_name": "Cashier One", "role": "cashier",
            "active": 1, "shift_id": sid, "password": CASHIER_PASSWORD}
