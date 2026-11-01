"""Schema migration, snapshots, WAL-safe backup, restore security and recovery."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import database
from services import backup as backup_mod
from services.backup import create_database_backup, restore_database_file, validate_database_file
from services.security import SUPPORTED_SCHEMA_VERSION
from helpers import connect

FIXTURES = Path(__file__).parent / "fixtures"


def _point_database_at(tmp_path, monkeypatch, name="live"):
    data = tmp_path / name
    for key, value in {"DATA_DIR": data, "DATABASE_PATH": data / "smartpos.db", "CONFIG_DIR": tmp_path / (name + "-cfg"),
                       "BACKUP_DIR": data / "backups", "LOG_DIR": data / "logs",
                       "MIGRATION_BACKUPS_DIR": data / "migration_backups",
                       "LEGACY_DATA_DIR": tmp_path / "no-legacy", "_STORAGE_READY": False}.items():
        monkeypatch.setattr(database, key, value)
    data.mkdir(parents=True, exist_ok=True)
    return data


def _legacy_db(path: Path):
    """A database shaped exactly like the supplied pre-migration snapshots, with a few rows."""
    con = sqlite3.connect(path)
    con.executescript((FIXTURES / "legacy_v0_schema.sql").read_text(encoding="utf-8"))
    con.execute("INSERT INTO users(id,username,display_name,password_hash,role,active) VALUES(1,'old','Old Admin','salt$hash','admin',1)")
    con.execute("INSERT INTO products(id,code,name,purchase_price,selling_price,stock_quantity,min_stock) VALUES(1,'P1','Shirt',400,1000,7,1)")
    con.execute("INSERT INTO cash_shifts(id,user_id,opening_cash,status) VALUES(1,1,10000,'closed')")
    con.execute("INSERT INTO invoices(id,invoice_number,created_at,subtotal,discount,total,payment_method,amount_paid,change_amount,shift_id,user_id,daily_number,business_date) "
                "VALUES(1,'INV-1','2026-10-01 10:00:00',2000,0,2000,'نقدي',2000,0,1,1,'0001','2026-10-01')")
    con.execute("INSERT INTO invoices(id,invoice_number,created_at,subtotal,discount,total,payment_method,amount_paid,change_amount,shift_id,user_id,daily_number,business_date) "
                "VALUES(2,'INV-2','2026-10-01 02:00:00',1000,0,1000,'فيزا',1000,0,1,1,'0002','2026-09-30')")
    con.execute("INSERT INTO invoice_items(invoice_id,product_id,product_code,product_name,quantity,unit_price,line_total,unit_cost) VALUES(1,1,'P1','Shirt',2,1000,2000,-1)")
    con.execute("INSERT INTO invoice_items(invoice_id,product_id,product_code,product_name,quantity,unit_price,line_total,unit_cost) VALUES(2,1,'P1','Shirt',1,1000,1000,400)")
    con.execute("INSERT INTO sales_returns(id,return_number,invoice_id,created_at,total_refund,notes) VALUES(1,'RET-1',1,'2026-10-02 11:00:00',1000,'old')")
    con.execute("INSERT INTO sales_return_items(return_id,invoice_item_id,product_id,product_name,quantity,unit_refund,line_refund) VALUES(1,1,1,'Shirt',1,1000,1000)")
    con.commit(); con.close()


def _counts(path):
    con = sqlite3.connect(path)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("users", "products", "invoices", "invoice_items", "sales_returns", "sales_return_items", "cash_shifts")}
    finally:
        con.close()


def test_legacy_database_upgrade_preserves_history_and_flags_unknowns(tmp_path, monkeypatch):
    data = _point_database_at(tmp_path, monkeypatch)
    _legacy_db(database.DATABASE_PATH)
    before = _counts(database.DATABASE_PATH)
    database.initialize_database()
    assert _counts(database.DATABASE_PATH) == before
    snaps = list((data / "migration_backups").glob("pre_migration_v0_to_v8_*.db"))      # safety snapshot taken first
    assert len(snaps) == 1 and snaps[0].read_bytes() != b""
    assert hashlib.sha256(Path(snaps[0]).read_bytes()).hexdigest() != ""
    snap_con = sqlite3.connect(snaps[0])
    assert snap_con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2        # snapshot holds PRE-migration data
    assert "request_id" not in [r[1] for r in snap_con.execute("PRAGMA table_info(invoices)")]
    snap_con.close()
    con = connect(database.DATABASE_PATH)
    try:
        assert con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SUPPORTED_SCHEMA_VERSION
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        # legacy return is NOT guessed: stays unknown; legacy card invoice stays readable, as legacy tender
        assert con.execute("SELECT refund_method FROM sales_returns").fetchone()[0] == "unknown"
        assert con.execute("SELECT payment_method FROM invoices WHERE id=2").fetchone()[0] == "فيزا"
        assert con.execute("SELECT tender,amount FROM invoice_payments WHERE invoice_id=2").fetchone() == ("legacy_other", 1000)
        assert con.execute("SELECT unit_cost FROM invoice_items WHERE id=1").fetchone()[0] == -1       # unknown cost not fabricated
        # stock is anchored to an explicit opening balance, not rewritten
        assert con.execute("SELECT stock_quantity FROM products").fetchone()[0] == 7
        assert con.execute("SELECT SUM(quantity_delta) FROM inventory_movements").fetchone()[0] == 7
    finally:
        con.close()


def test_migration_is_repeatable_and_snapshots_only_when_needed(tmp_path, monkeypatch):
    data = _point_database_at(tmp_path, monkeypatch)
    _legacy_db(database.DATABASE_PATH)
    database.initialize_database()
    database._STORAGE_READY = False
    first = _counts(database.DATABASE_PATH)
    for _ in range(3):
        database.initialize_database()
    assert _counts(database.DATABASE_PATH) == first
    assert len(list((data / "migration_backups").glob("pre_migration_*"))) == 1       # up-to-date DB: no new snapshot
    con = connect(database.DATABASE_PATH)
    assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == SUPPORTED_SCHEMA_VERSION
    assert con.execute("SELECT COUNT(*) FROM inventory_movements WHERE reference_type='migration'").fetchone()[0] == 1
    con.close()


def test_original_file_is_never_modified_by_import_copy(tmp_path, monkeypatch):
    """Operating on a COPY of a supplied snapshot must leave the original byte-identical."""
    original = tmp_path / "supplied.db"; _legacy_db(original)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    _point_database_at(tmp_path, monkeypatch)
    shutil.copy2(original, database.DATABASE_PATH)
    database.initialize_database()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest


def test_backup_is_correct_for_wal_database_with_uncheckpointed_writes(store_db, tmp_path):
    writer = database.get_connection()                      # WAL mode, stays open
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        writer.execute("UPDATE products SET selling_price=7777 WHERE id=?", (store_db["product_id"],))
        writer.commit()
        wal = Path(str(store_db["path"]) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0      # change lives in the WAL, not yet in the main file
        backup = create_database_backup(store_db["path"], tmp_path / "bk")
    finally:
        writer.close()
    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT selling_price FROM products WHERE id=?", (store_db["product_id"],)).fetchone()[0] == 7777
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()


def test_backup_retention_keeps_newest(store_db, tmp_path):
    for _ in range(5):
        create_database_backup(store_db["path"], tmp_path / "bk", retain=3)
    assert len(list((tmp_path / "bk").glob("SmartPOS_backup_*.db"))) == 3


def test_invalid_backups_are_rejected(store_db, tmp_path):
    garbage = tmp_path / "garbage.db"; garbage.write_bytes(b"this is not a database" * 50)
    foreign = tmp_path / "foreign.db"
    c = sqlite3.connect(foreign); c.execute("CREATE TABLE t(x)"); c.commit(); c.close()
    newer = tmp_path / "newer.db"; shutil.copy2(store_db["path"], newer)
    c = sqlite3.connect(newer); c.execute("INSERT INTO schema_migrations(version,name) VALUES(99,'future')"); c.commit(); c.close()
    for bad in (garbage, foreign, newer, tmp_path / "missing.db"):
        with pytest.raises((ValueError, sqlite3.DatabaseError, FileNotFoundError)):
            validate_database_file(bad)
        with pytest.raises((ValueError, sqlite3.DatabaseError, FileNotFoundError)):
            restore_database_file(bad, store_db["path"], tmp_path / "s", store_db["user"],
                                  admin_password=store_db["admin_password"])
    con = connect(store_db["path"])
    assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1          # live DB untouched
    con.close()


def test_restore_requires_correct_admin_password(store_db, tmp_path):
    backup = create_database_backup(store_db["path"], tmp_path / "bk")
    for pw in ("", "wrong-password", None):
        with pytest.raises(PermissionError, match="كلمة مرور"):
            restore_database_file(backup, store_db["path"], tmp_path / "s", store_db["user"], admin_password=pw)
    assert not (tmp_path / "s").exists() or not list((tmp_path / "s").glob("pre_restore_*"))   # refused before touching anything
    with pytest.raises(PermissionError):                    # forged admin dict that does not exist in the database
        restore_database_file(backup, store_db["path"], tmp_path / "s", {"id": 777, "role": "admin", "active": 1},
                              admin_password=store_db["admin_password"])


def test_restore_replaces_data_keeps_safety_copy_and_audits(store_db, tmp_path):
    con = connect(store_db["path"]); con.execute("UPDATE products SET selling_price=1111"); con.commit(); con.close()
    backup = create_database_backup(store_db["path"], tmp_path / "bk")
    con = connect(store_db["path"]); con.execute("UPDATE products SET selling_price=2222"); con.commit(); con.close()
    safety = restore_database_file(backup, store_db["path"], tmp_path / "safety", store_db["user"],
                                   admin_password=store_db["admin_password"])
    live = connect(store_db["path"]); old = sqlite3.connect(safety)
    try:
        assert live.execute("SELECT selling_price FROM products").fetchone()[0] == 1111
        assert old.execute("SELECT selling_price FROM products").fetchone()[0] == 2222      # pre-restore state recoverable
        assert live.execute("SELECT action FROM audit_log WHERE action='database_restored'").fetchone() is not None
        events = (tmp_path / "safety" / "restore_events.jsonl").read_text(encoding="utf-8").splitlines()
        assert json.loads(events[-1])["safety_copy"] == safety.name
        assert live.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        live.close(); old.close()


def test_interrupted_restore_rolls_back_to_previous_state(store_db, tmp_path, monkeypatch):
    con = connect(store_db["path"]); con.execute("UPDATE products SET selling_price=1111"); con.commit(); con.close()
    backup = create_database_backup(store_db["path"], tmp_path / "bk")
    con = connect(store_db["path"]); con.execute("UPDATE products SET selling_price=3333"); con.commit(); con.close()
    real, calls = backup_mod.validate_restore_database, {"n": 0}

    def explode_after_activation(connection):
        calls["n"] += 1
        if calls["n"] == 3:                  # 1: source file, 2: safety copy, 3: restored destination
            raise RuntimeError("simulated failure / power cut during activation")
        return real(connection)
    monkeypatch.setattr(backup_mod, "validate_restore_database", explode_after_activation)
    with pytest.raises(RuntimeError, match="simulated"):
        restore_database_file(backup, store_db["path"], tmp_path / "safety", store_db["user"],
                              admin_password=store_db["admin_password"])
    assert calls["n"] == 3
    live = connect(store_db["path"])
    try:
        assert live.execute("SELECT selling_price FROM products").fetchone()[0] == 3333      # rolled back, not half-restored
        assert live.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        live.close()


def test_pending_restore_file_is_never_applied_at_startup(tmp_path, monkeypatch):
    data = _point_database_at(tmp_path, monkeypatch)
    legacy = tmp_path / "no-legacy"; legacy.mkdir()
    database.initialize_database()
    con = connect(database.DATABASE_PATH); con.execute("INSERT INTO app_settings(key,value) VALUES('marker','live')"); con.commit(); con.close()
    evil = sqlite3.connect(legacy / "smartpos_restore_pending.db"); evil.execute("CREATE TABLE evil(x)"); evil.commit(); evil.close()
    shutil.copy2(legacy / "smartpos_restore_pending.db", data / "smartpos_restore_pending.db")
    database._STORAGE_READY = False
    database.initialize_database()
    con = connect(database.DATABASE_PATH)
    try:
        assert con.execute("SELECT value FROM app_settings WHERE key='marker'").fetchone()[0] == "live"
        assert con.execute("SELECT name FROM sqlite_master WHERE name='evil'").fetchone() is None
    finally:
        con.close()
    assert not (data / "smartpos_restore_pending.db").exists()                  # moved aside, not executed
    assert list((data / "quarantine").glob("*quarantined.db"))


def test_foreign_keys_are_enforced_on_every_app_connection(store_db):
    con = database.get_connection()
    try:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO invoice_items(invoice_id,product_id,product_code,product_name,quantity,unit_price,line_total,unit_cost) "
                        "VALUES(424242,1,'x','x',1,1,1,1)")
    finally:
        con.close()


# ---- tiered retention (audit F-01) ---------------------------------------------------------------
def _touch_backup(folder, when):
    path = folder / f"SmartPOS_backup_{when.strftime('%Y%m%d_%H%M%S_%f')}.db"
    path.write_bytes(b"x")
    return path


def test_tiered_retention_keeps_days_of_history_with_bounded_files(tmp_path):
    from datetime import datetime, timedelta
    from services.backup import apply_tiered_retention
    now = datetime(2026, 11, 1, 12, 0, 0)
    # 10-minute backups for 90 days
    for i in range(0, 90 * 144):
        _touch_backup(tmp_path, now - timedelta(minutes=10 * i))
    removed = apply_tiered_retention(tmp_path, now)
    left = sorted(tmp_path.glob("SmartPOS_backup_*.db"))
    assert len(removed) > 0 and len(left) <= 18 + 48 + 60 + 13        # bounded file count
    ages = [now - __import__("datetime").datetime.strptime(p.stem[16:], "%Y%m%d_%H%M%S_%f") for p in left]
    assert min(ages) < timedelta(minutes=11)                           # newest kept
    assert any(timedelta(hours=40) < a < timedelta(hours=49) for a in ages)   # yesterday is recoverable
    assert any(timedelta(days=30) < a < timedelta(days=60) for a in ages)     # a month ago is recoverable
    assert not any(a > timedelta(days=366) for a in ages)
    # idempotent
    assert apply_tiered_retention(tmp_path, now) == []


def test_tiered_retention_never_deletes_the_only_backup_and_reports_age(tmp_path):
    from datetime import datetime, timedelta
    from services.backup import apply_tiered_retention, latest_backup_age
    now = datetime(2026, 11, 1, 12, 0, 0)
    assert latest_backup_age(tmp_path, now) is None
    _touch_backup(tmp_path, now - timedelta(days=500))
    assert apply_tiered_retention(tmp_path, now) == []                 # newest is always kept
    assert latest_backup_age(tmp_path, now) > timedelta(days=499)


def test_create_backup_tiered_flag_and_secondary_copy(store_db, tmp_path):
    from services.backup import copy_backup_to_secondary, create_database_backup
    primary, second = tmp_path / "primary", tmp_path / "usb"
    backup = create_database_backup(store_db["path"], primary, tiered=True)
    assert backup.exists()
    copied = copy_backup_to_secondary(backup, second)
    assert copied.exists() and copied.parent == second.resolve()
    bad = primary / "SmartPOS_backup_20260101_000000_000000.db"
    bad.write_bytes(b"not a database")
    import pytest as _p
    with _p.raises(Exception):
        copy_backup_to_secondary(bad, second)
    assert not (second / bad.name).exists()                            # invalid copy is removed


def test_failed_rollback_reports_where_the_safety_copy_is(store_db, tmp_path, monkeypatch):
    con = connect(store_db["path"]); con.execute("UPDATE products SET selling_price=1111"); con.commit(); con.close()
    backup = create_database_backup(store_db["path"], tmp_path / "bk")
    real_validate, real_connect = backup_mod.validate_restore_database, sqlite3.connect
    state = {"validate": 0, "connect": 0, "armed": False}

    def explode_after_activation(connection):
        state["validate"] += 1
        if state["validate"] == 3:
            state["armed"] = True                       # from now on, opening the rollback source fails
            raise RuntimeError("simulated failure during activation")
        return real_validate(connection)

    def flaky_connect(*args, **kwargs):
        if state["armed"]:
            raise OSError("disk unavailable")
        return real_connect(*args, **kwargs)
    monkeypatch.setattr(backup_mod, "validate_restore_database", explode_after_activation)
    monkeypatch.setattr(sqlite3, "connect", flaky_connect)
    with pytest.raises(RuntimeError, match="pre_restore_") as excinfo:
        restore_database_file(backup, store_db["path"], tmp_path / "safety", store_db["user"],
                              admin_password=store_db["admin_password"])
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert "disk unavailable" in str(excinfo.value)
    safety = sorted((tmp_path / "safety").glob("pre_restore_*.db"))
    assert safety and str(safety[0]) in str(excinfo.value)          # the message names the real file
    check = real_connect(safety[0])
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_run_backup_job_never_raises_and_works_off_the_main_thread(store_db, tmp_path):
    import threading
    from services.backup import run_backup_job
    out = {}

    def work():
        out["ok"] = run_backup_job(store_db["path"], tmp_path / "bg")
        blocker = tmp_path / "a-file"
        blocker.write_text("x")
        out["bad"] = run_backup_job(store_db["path"], blocker / "sub")      # impossible folder
    thread = threading.Thread(target=work)
    thread.start()
    thread.join(30)
    path, error = out["ok"]
    assert error is None and path.exists()
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    bad_path, bad_error = out["bad"]
    assert bad_path is None and bad_error                      # reported, thread did not die
