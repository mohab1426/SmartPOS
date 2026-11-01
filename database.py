"""SQLite storage and idempotent schema migrations for SmartPOS.

Operational data is deliberately kept outside the installation directory.  The
legacy project-local database is copied once (never moved) into the per-user
application-data directory so upgrades cannot overwrite it.
"""
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from services.security import SUPPORTED_SCHEMA_VERSION

PROJECT_DIR = Path(__file__).resolve().parent
LEGACY_DATA_DIR = PROJECT_DIR / "data"


def _application_data_root() -> Path:
    override = os.environ.get("SMARTPOS_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "SmartPOS" / "data"
    if sys_platform := os.environ.get("XDG_DATA_HOME"):
        return Path(sys_platform).expanduser() / "smartpos"
    if os.name == "posix" and Path("/System/Library").exists():
        return Path.home() / "Library" / "Application Support" / "SmartPOS"
    return Path.home() / ".local" / "share" / "smartpos" / "data"


DATA_DIR = _application_data_root()
DATABASE_PATH = DATA_DIR / "smartpos.db"

def _application_config_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "SmartPOS"
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]).expanduser() / "smartpos"
    return Path.home() / ".config" / "smartpos"

CONFIG_DIR = _application_config_root()
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
MIGRATION_BACKUPS_DIR = DATA_DIR / "migration_backups"
_STORAGE_READY = False


def _sqlite_backup(source_path: Path, destination_path: Path, *, strict: bool = True) -> None:
    """Copy via the SQLite backup API (WAL-safe). ``strict`` also validates the copy."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(source_path), timeout=30)
    destination = sqlite3.connect(str(destination_path), timeout=30)
    try:
        source.backup(destination)
        if strict:
            check = destination.execute("PRAGMA integrity_check").fetchone()
            fk_errors = destination.execute("PRAGMA foreign_key_check").fetchall()
            if not check or check[0] != "ok" or fk_errors:
                raise RuntimeError("SQLite backup validation failed")
    finally:
        destination.close()
        source.close()


def ensure_storage_layout() -> None:
    """Create user-data folders, safely migrate legacy data, quarantine pending restores."""
    global _STORAGE_READY
    if _STORAGE_READY:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MIGRATION_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    legacy_db = LEGACY_DATA_DIR / "smartpos.db"
    if not DATABASE_PATH.exists() and legacy_db.exists() and legacy_db.resolve() != DATABASE_PATH.resolve():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        preserved = MIGRATION_BACKUPS_DIR / f"pre_migration_{stamp}.db"
        _sqlite_backup(legacy_db, preserved)
        temp = DATABASE_PATH.with_suffix(".migration.tmp")
        _sqlite_backup(legacy_db, temp)
        os.replace(temp, DATABASE_PATH)

    # A pending restore must never be executed implicitly. Preserve suspicious
    # one-shot restore files for manual investigation, outside release packages.
    for pending in (LEGACY_DATA_DIR / "smartpos_restore_pending.db", DATA_DIR / "smartpos_restore_pending.db"):
        if pending.is_file():
            quarantine = DATA_DIR / "quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            target = quarantine / f"{pending.parent.name}_pending_restore_{stamp}.quarantined.db"
            if pending.resolve() != target.resolve():
                try:
                    shutil.move(str(pending), str(target))
                except OSError:
                    # Install locations such as Program Files may be read-only.
                    # Copy a quarantine record when possible; never execute it.
                    try:
                        shutil.copy2(str(pending), str(target))
                    except OSError:
                        # Leaving an inert legacy file in place is safer than
                        # preventing the application from starting.
                        pass
    _STORAGE_READY = True


class ClosingSQLiteConnection(sqlite3.Connection):
    """SQLite connection whose context manager also closes the file handle.

    The stdlib sqlite3 context manager commits/rolls back but does not close the
    connection. SmartPOS uses ``with get_connection()`` throughout the GUI, so
    closing on exit prevents idle handles and restore/backup locking problems.
    """
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_connection():
    """Return a configured SQLite connection to per-user SmartPOS storage."""
    ensure_storage_layout()
    connection = sqlite3.connect(str(DATABASE_PATH), timeout=20, factory=ClosingSQLiteConnection)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def _snapshot_before_migration() -> Path | None:
    """Keep a recoverable copy of an existing database before it is upgraded in place."""
    ensure_storage_layout()
    if not DATABASE_PATH.is_file() or DATABASE_PATH.stat().st_size == 0:
        return None
    try:
        probe = sqlite3.connect(f"file:{DATABASE_PATH.as_posix()}?mode=ro", uri=True, timeout=20)
        try:
            has = probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            version = probe.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0] if has else 0
        finally:
            probe.close()
    except sqlite3.DatabaseError:
        version = -1  # unreadable/corrupt: still preserve whatever is on disk
    if version >= SUPPORTED_SCHEMA_VERSION:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = MIGRATION_BACKUPS_DIR / f"pre_migration_v{version}_to_v{SUPPORTED_SCHEMA_VERSION}_{stamp}.db"
    try:
        _sqlite_backup(DATABASE_PATH, target, strict=False)
    except sqlite3.DatabaseError:
        shutil.copy2(str(DATABASE_PATH), str(target))
    return target


def initialize_database():
    """Create required tables and safely add newer columns to existing databases."""
    _snapshot_before_migration()
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                purchase_price INTEGER NOT NULL CHECK (purchase_price >= 0),
                selling_price INTEGER NOT NULL CHECK (selling_price >= 0),
                stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
                min_stock INTEGER NOT NULL DEFAULT 0 CHECK (min_stock >= 0)
            )
        """)

        product_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(products)").fetchall()
        }
        optional_columns = {
            "barcode": "TEXT",
            "size": "TEXT NOT NULL DEFAULT ''",
            "color": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT 'عام'",
        }
        for column, definition in optional_columns.items():
            if column not in product_columns:
                connection.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")

        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode_unique "
            "ON products(barcode) WHERE barcode IS NOT NULL AND barcode <> ''"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                subtotal INTEGER NOT NULL CHECK (subtotal >= 0),
                discount INTEGER NOT NULL DEFAULT 0 CHECK (discount >= 0),
                total INTEGER NOT NULL CHECK (total >= 0),
                payment_method TEXT NOT NULL DEFAULT 'نقدي',
                amount_paid INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
                change_amount INTEGER NOT NULL DEFAULT 0 CHECK (change_amount >= 0)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                product_size TEXT NOT NULL DEFAULT '',
                product_color TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
                line_total INTEGER NOT NULL CHECK (line_total >= 0),
                unit_cost INTEGER NOT NULL DEFAULT -1,
                FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
        """)
        item_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(invoice_items)").fetchall()
        }
        if "unit_cost" not in item_columns:
            # -1 means this historical item's purchase cost was not captured at sale time.
            connection.execute(
                "ALTER TABLE invoice_items ADD COLUMN unit_cost INTEGER NOT NULL DEFAULT -1"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(invoice_id)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                phone TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS purchase_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                supplier_name TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL CHECK (total >= 0),
                notes TEXT NOT NULL DEFAULT ''
            )
        """)
        purchase_columns = {row[1] for row in connection.execute("PRAGMA table_info(purchase_invoices)").fetchall()}
        if "supplier_id" not in purchase_columns:
            connection.execute("ALTER TABLE purchase_invoices ADD COLUMN supplier_id INTEGER REFERENCES suppliers(id)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS purchase_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_invoice_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_cost INTEGER NOT NULL CHECK (unit_cost >= 0),
                line_total INTEGER NOT NULL CHECK (line_total >= 0),
                FOREIGN KEY(purchase_invoice_id) REFERENCES purchase_invoices(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_purchase_items_invoice ON purchase_items(purchase_invoice_id)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS sales_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_number TEXT NOT NULL UNIQUE,
                invoice_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                total_refund INTEGER NOT NULL CHECK (total_refund >= 0),
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(invoice_id) REFERENCES invoices(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS sales_return_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_id INTEGER NOT NULL,
                invoice_item_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_refund INTEGER NOT NULL CHECK (unit_refund >= 0),
                line_refund INTEGER NOT NULL CHECK (line_refund >= 0),
                FOREIGN KEY(return_id) REFERENCES sales_returns(id) ON DELETE CASCADE,
                FOREIGN KEY(invoice_item_id) REFERENCES invoice_items(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sales_returns_invoice ON sales_returns(invoice_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sales_return_items_return ON sales_return_items(return_id)")
        # Cashier shift support. These are additive migrations; existing invoices remain intact.
        invoice_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)").fetchall()}
        if "shift_id" not in invoice_columns:
            connection.execute("ALTER TABLE invoices ADD COLUMN shift_id INTEGER")
        if "user_id" not in invoice_columns:
            connection.execute("ALTER TABLE invoices ADD COLUMN user_id INTEGER")
        # Daily visible invoice numbers are additive; the original unique invoice_number
        # and numeric id remain untouched for compatibility with old records and returns.
        if "daily_number" not in invoice_columns:
            connection.execute("ALTER TABLE invoices ADD COLUMN daily_number TEXT")
        if "business_date" not in invoice_columns:
            connection.execute("ALTER TABLE invoices ADD COLUMN business_date TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_business_daily_number "
            "ON invoices(business_date, daily_number) "
            "WHERE business_date IS NOT NULL AND daily_number IS NOT NULL"
        )

        connection.execute("""
            CREATE TABLE IF NOT EXISTS cash_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                opened_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                opening_cash INTEGER NOT NULL DEFAULT 0 CHECK (opening_cash >= 0),
                closed_at TEXT,
                expected_cash INTEGER,
                actual_cash INTEGER,
                difference INTEGER,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cash_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                movement_type TEXT NOT NULL CHECK (movement_type IN ('مصروف','سحب','إيداع')),
                amount INTEGER NOT NULL CHECK (amount > 0),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(shift_id) REFERENCES cash_shifts(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_cash_shifts_status ON cash_shifts(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_cash_movements_shift ON cash_movements(shift_id)")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin','cashier')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)

        # Versioned, additive migrations. Existing invoice and purchase rows are
        # intentionally preserved; historical unknown payment/refund methods are
        # never guessed or silently rewritten.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)

        def columns(table):
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}

        def add_column(table, name, definition):
            if name not in columns(table):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

        # Migration 1: real customer credit, payments, returns metadata, and audit trail.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name COLLATE NOCASE)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS customer_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                transaction_type TEXT NOT NULL CHECK(transaction_type IN ('sale_credit','collection','sales_return_credit','adjustment')),
                debit INTEGER NOT NULL DEFAULT 0 CHECK(debit >= 0),
                credit INTEGER NOT NULL DEFAULT 0 CHECK(credit >= 0),
                source_type TEXT,
                source_id INTEGER,
                user_id INTEGER,
                shift_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                business_date TEXT,
                notes TEXT NOT NULL DEFAULT '',
                CHECK(NOT (debit > 0 AND credit > 0)),
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(shift_id) REFERENCES cash_shifts(id)
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_customer_ledger_customer ON customer_ledger(customer_id, id)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_ledger_source ON customer_ledger(source_type,source_id,transaction_type) WHERE source_type IS NOT NULL AND source_id IS NOT NULL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS invoice_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                tender TEXT NOT NULL CHECK(tender IN ('cash','credit','legacy_other')),
                amount INTEGER NOT NULL CHECK(amount > 0),
                customer_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                user_id INTEGER,
                FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice ON invoice_payments(invoice_id)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC)")
        add_column("invoices", "customer_id", "INTEGER REFERENCES customers(id)")
        add_column("invoices", "credit_amount", "INTEGER NOT NULL DEFAULT 0 CHECK(credit_amount >= 0)")
        add_column("sales_returns", "refund_method", "TEXT NOT NULL DEFAULT 'unknown'")
        add_column("sales_returns", "shift_id", "INTEGER REFERENCES cash_shifts(id)")
        add_column("sales_returns", "user_id", "INTEGER REFERENCES users(id)")
        add_column("sales_returns", "customer_id", "INTEGER REFERENCES customers(id)")
        add_column("sales_returns", "business_date", "TEXT")
        add_column("cash_movements", "business_date", "TEXT")
        add_column("purchase_invoices", "business_date", "TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sales_returns_shift ON sales_returns(shift_id, refund_method)")

        # Migration 2: stock movement history and product parent/variant grouping.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS inventory_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                movement_type TEXT NOT NULL CHECK(movement_type IN ('opening_balance','purchase','sale','sales_return','adjustment')),
                quantity_delta INTEGER NOT NULL CHECK(quantity_delta <> 0),
                unit_cost INTEGER NOT NULL DEFAULT -1 CHECK(unit_cost >= -1),
                reference_type TEXT NOT NULL DEFAULT '',
                reference_id INTEGER,
                reference_line_id INTEGER,
                user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                business_date TEXT,
                reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_inventory_movements_product ON inventory_movements(product_id,id)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_movement_source_line ON inventory_movements(reference_type,reference_line_id,movement_type) WHERE reference_line_id IS NOT NULL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS product_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        add_column("products", "variant_group_id", "INTEGER REFERENCES product_groups(id)")
        for product_id, product_name in connection.execute("SELECT id,name FROM products WHERE variant_group_id IS NULL").fetchall():
            connection.execute("INSERT OR IGNORE INTO product_groups(name) VALUES(?)", (product_name,))
            group_id = connection.execute("SELECT id FROM product_groups WHERE name=? COLLATE NOCASE", (product_name,)).fetchone()[0]
            connection.execute("UPDATE products SET variant_group_id=? WHERE id=?", (group_id, product_id))
        connection.execute("CREATE INDEX IF NOT EXISTS idx_products_variant_group ON products(variant_group_id)")

        # Migration 3: deterministic 04:00 business-date backfill for all legacy rows.
        connection.execute("""
            UPDATE invoices SET business_date = CASE
                WHEN CAST(strftime('%H',created_at) AS INTEGER) < 4 THEN date(created_at,'-1 day')
                ELSE date(created_at) END
            WHERE business_date IS NULL OR business_date=''
        """)
        for table in ("sales_returns", "cash_movements", "purchase_invoices"):
            connection.execute(f"""
                UPDATE {table} SET business_date = CASE
                    WHEN CAST(strftime('%H',created_at) AS INTEGER) < 4 THEN date(created_at,'-1 day')
                    ELSE date(created_at) END
                WHERE business_date IS NULL OR business_date=''
            """)
        # Legacy payments are imported as settled tenders. A historical non-cash
        # label is deliberately represented as legacy_other, not reclassified.
        legacy_invoices = connection.execute("""
            SELECT i.id,i.payment_method,i.total,i.user_id
            FROM invoices i LEFT JOIN invoice_payments p ON p.invoice_id=i.id
            WHERE p.id IS NULL
        """).fetchall()
        for invoice_id, method, total, user_id in legacy_invoices:
            if method == 'نقدي':
                cash_net = int(total)
                tender = 'cash'
            else:
                cash_net = int(total)
                tender = 'legacy_other'
            if cash_net > 0:
                connection.execute("INSERT INTO invoice_payments(invoice_id,tender,amount,user_id) VALUES(?,?,?,?)", (invoice_id,tender,cash_net,user_id))
        # Legacy refund tender was never collected; mark as unknown rather than
        # incorrectly deducting it from cash shifts.
        connection.execute("UPDATE sales_returns SET refund_method='unknown' WHERE refund_method IS NULL OR refund_method=''")

        # The current stock becomes an explicit migration opening balance. It is
        # a baseline from migration time, not a fabricated historical movement.
        products_to_seed = connection.execute("""
            SELECT p.id,p.stock_quantity,p.purchase_price
            FROM products p
            WHERE p.stock_quantity<>0 AND NOT EXISTS (
                SELECT 1 FROM inventory_movements m
                WHERE m.product_id=p.id AND m.movement_type='opening_balance'
                  AND m.reference_type='migration'
            )
        """).fetchall()
        for product_id, quantity, unit_cost in products_to_seed:
            connection.execute("""
                INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,unit_cost,
                    reference_type,reference_id,created_at,business_date,reason)
                VALUES(?,'opening_balance',?,?,'migration',?,datetime('now','localtime'),
                    CASE WHEN CAST(strftime('%H','now','localtime') AS INTEGER)<4
                         THEN date('now','localtime','-1 day') ELSE date('now','localtime') END,
                    'رصيد افتتاحي تم ترحيله عند تفعيل سجل المخزون')
            """, (product_id, quantity, unit_cost, product_id))

        # Migration 4: enforce references that could not be retrofitted to the old
        # invoices table using SQLite ALTER TABLE (without rebuilding history).
        connection.executescript("""
            CREATE TRIGGER IF NOT EXISTS trg_invoices_shift_insert
            BEFORE INSERT ON invoices WHEN NEW.shift_id IS NOT NULL
            BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM cash_shifts WHERE id=NEW.shift_id)
                THEN RAISE(ABORT,'invalid invoice shift_id') END; END;
            CREATE TRIGGER IF NOT EXISTS trg_invoices_user_insert
            BEFORE INSERT ON invoices WHEN NEW.user_id IS NOT NULL
            BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM users WHERE id=NEW.user_id)
                THEN RAISE(ABORT,'invalid invoice user_id') END; END;
            CREATE TRIGGER IF NOT EXISTS trg_invoices_shift_update
            BEFORE UPDATE OF shift_id ON invoices WHEN NEW.shift_id IS NOT NULL
            BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM cash_shifts WHERE id=NEW.shift_id)
                THEN RAISE(ABORT,'invalid invoice shift_id') END; END;
            CREATE TRIGGER IF NOT EXISTS trg_invoices_user_update
            BEFORE UPDATE OF user_id ON invoices WHEN NEW.user_id IS NOT NULL
            BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM users WHERE id=NEW.user_id)
                THEN RAISE(ABORT,'invalid invoice user_id') END; END;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_shift_per_user
            ON cash_shifts(user_id) WHERE status='open';
        """)
        # Migration 5: enforce critical invariants in SQLite as a second line of
        # defence, and make financial/inventory history append-only.
        connection.executescript("""
            CREATE TRIGGER IF NOT EXISTS trg_invoices_shift_user_insert
            BEFORE INSERT ON invoices
            WHEN NEW.shift_id IS NOT NULL AND NEW.user_id IS NOT NULL
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM cash_shifts s WHERE s.id=NEW.shift_id
                      AND s.user_id=NEW.user_id AND s.status='open'
                ) THEN RAISE(ABORT,'invoice requires an open shift owned by its user') END;
                SELECT CASE WHEN NEW.credit_amount > NEW.total
                    THEN RAISE(ABORT,'invoice credit cannot exceed invoice total') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_invoices_shift_user_update
            BEFORE UPDATE OF shift_id,user_id,credit_amount,total ON invoices
            BEGIN
                SELECT CASE WHEN NEW.shift_id IS NOT NULL AND NEW.user_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM cash_shifts s WHERE s.id=NEW.shift_id AND s.user_id=NEW.user_id)
                    THEN RAISE(ABORT,'invoice shift must belong to invoice user') END;
                SELECT CASE WHEN NEW.credit_amount < 0 OR NEW.credit_amount > NEW.total
                    THEN RAISE(ABORT,'invalid invoice credit amount') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_prevent_delete_referenced_user
            BEFORE DELETE ON users
            WHEN EXISTS(SELECT 1 FROM invoices WHERE user_id=OLD.id)
              OR EXISTS(SELECT 1 FROM cash_shifts WHERE user_id=OLD.id)
              OR EXISTS(SELECT 1 FROM customer_ledger WHERE user_id=OLD.id)
              OR EXISTS(SELECT 1 FROM inventory_movements WHERE user_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'user has historical business records; deactivate instead'); END;
            CREATE TRIGGER IF NOT EXISTS trg_prevent_delete_referenced_shift
            BEFORE DELETE ON cash_shifts
            WHEN EXISTS(SELECT 1 FROM invoices WHERE shift_id=OLD.id)
              OR EXISTS(SELECT 1 FROM cash_movements WHERE shift_id=OLD.id)
              OR EXISTS(SELECT 1 FROM customer_ledger WHERE shift_id=OLD.id)
              OR EXISTS(SELECT 1 FROM sales_returns WHERE shift_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'shift has historical business records; do not delete'); END;
            CREATE TRIGGER IF NOT EXISTS trg_validate_sales_return_insert
            BEFORE INSERT ON sales_returns
            BEGIN
                SELECT CASE WHEN NEW.refund_method NOT IN ('cash','customer_credit','unknown')
                    THEN RAISE(ABORT,'invalid sales return refund_method') END;
                SELECT CASE WHEN NEW.refund_method='cash' AND (NEW.shift_id IS NULL OR NEW.user_id IS NULL
                    OR NOT EXISTS(SELECT 1 FROM cash_shifts s WHERE s.id=NEW.shift_id
                                  AND s.user_id=NEW.user_id AND s.status='open'))
                    THEN RAISE(ABORT,'cash refund requires its cashier open shift') END;
                SELECT CASE WHEN NEW.refund_method='customer_credit' AND (NEW.customer_id IS NULL
                    OR NOT EXISTS(SELECT 1 FROM invoices i WHERE i.id=NEW.invoice_id AND i.customer_id=NEW.customer_id))
                    THEN RAISE(ABORT,'customer-credit refund must match invoice customer') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_validate_invoice_payment_insert
            BEFORE INSERT ON invoice_payments
            BEGIN
                SELECT CASE WHEN COALESCE((SELECT SUM(amount) FROM invoice_payments WHERE invoice_id=NEW.invoice_id),0) + NEW.amount
                    > (SELECT total FROM invoices WHERE id=NEW.invoice_id)
                    THEN RAISE(ABORT,'invoice payment allocations exceed invoice total') END;
                SELECT CASE WHEN NEW.tender='credit' AND (NEW.customer_id IS NULL OR NOT EXISTS(
                    SELECT 1 FROM invoices i WHERE i.id=NEW.invoice_id AND i.customer_id=NEW.customer_id AND i.credit_amount>0))
                    THEN RAISE(ABORT,'credit payment requires matching customer account') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_validate_sales_return_update
            BEFORE UPDATE ON sales_returns
            BEGIN
                SELECT CASE WHEN OLD.refund_method <> 'unknown'
                    OR NEW.refund_method NOT IN ('cash','customer_credit')
                    OR NEW.invoice_id <> OLD.invoice_id
                    OR NEW.total_refund <> OLD.total_refund
                    OR NEW.created_at <> OLD.created_at
                    THEN RAISE(ABORT,'only an unclassified legacy return can be classified') END;
                SELECT CASE WHEN NEW.refund_method='cash' AND (NEW.shift_id IS NULL OR NEW.user_id IS NULL
                    OR NOT EXISTS(SELECT 1 FROM cash_shifts s WHERE s.id=NEW.shift_id AND s.user_id=NEW.user_id))
                    THEN RAISE(ABORT,'historical cash refund must reference its actual shift cashier') END;
                SELECT CASE WHEN NEW.refund_method='customer_credit' AND (NEW.customer_id IS NULL
                    OR NOT EXISTS(SELECT 1 FROM invoices i WHERE i.id=NEW.invoice_id AND i.customer_id=NEW.customer_id))
                    THEN RAISE(ABORT,'historical credit refund must match invoice customer') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_sales_return_items_no_update
            BEFORE UPDATE ON sales_return_items BEGIN SELECT RAISE(ABORT,'return lines are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS trg_sales_return_items_no_delete
            BEFORE DELETE ON sales_return_items BEGIN SELECT RAISE(ABORT,'return lines are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS trg_sales_returns_no_delete
            BEFORE DELETE ON sales_returns BEGIN SELECT RAISE(ABORT,'sales returns are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS trg_inventory_movements_no_update
            BEFORE UPDATE ON inventory_movements BEGIN SELECT RAISE(ABORT,'inventory movement ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trg_inventory_movements_no_delete
            BEFORE DELETE ON inventory_movements BEGIN SELECT RAISE(ABORT,'inventory movement ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trg_customer_ledger_no_update
            BEFORE UPDATE ON customer_ledger BEGIN SELECT RAISE(ABORT,'customer ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trg_customer_ledger_no_delete
            BEFORE DELETE ON customer_ledger BEGIN SELECT RAISE(ABORT,'customer ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
            BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
            BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trg_invoice_payments_no_update
            BEFORE UPDATE ON invoice_payments BEGIN SELECT RAISE(ABORT,'payment allocations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS trg_invoice_payments_no_delete
            BEFORE DELETE ON invoice_payments BEGIN SELECT RAISE(ABORT,'payment allocations are immutable'); END;
        """)

        # Migration 7: close remaining gaps that can be expressed safely as
        # database-level invariants without rebuilding legacy financial tables.
        connection.executescript("""
            CREATE TRIGGER IF NOT EXISTS trg_invoice_math_insert
            BEFORE INSERT ON invoices
            BEGIN
                SELECT CASE WHEN NEW.discount > NEW.subtotal OR NEW.total <> NEW.subtotal - NEW.discount
                    THEN RAISE(ABORT,'invoice total must equal subtotal minus discount') END;
                SELECT CASE WHEN NEW.credit_amount < 0 OR NEW.credit_amount > NEW.total
                    THEN RAISE(ABORT,'invalid invoice credit amount') END;
                SELECT CASE WHEN NEW.credit_amount > 0 AND NEW.customer_id IS NULL
                    THEN RAISE(ABORT,'credit invoice requires a customer account') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_invoice_math_update
            BEFORE UPDATE OF subtotal,discount,total,credit_amount,customer_id ON invoices
            BEGIN
                SELECT CASE WHEN NEW.discount > NEW.subtotal OR NEW.total <> NEW.subtotal - NEW.discount
                    THEN RAISE(ABORT,'invoice total must equal subtotal minus discount') END;
                SELECT CASE WHEN NEW.credit_amount < 0 OR NEW.credit_amount > NEW.total
                    THEN RAISE(ABORT,'invalid invoice credit amount') END;
                SELECT CASE WHEN NEW.credit_amount > 0 AND NEW.customer_id IS NULL
                    THEN RAISE(ABORT,'credit invoice requires a customer account') END;
                SELECT CASE WHEN COALESCE((SELECT SUM(amount) FROM invoice_payments WHERE invoice_id=OLD.id),0) > NEW.total
                    THEN RAISE(ABORT,'invoice total cannot be lower than recorded payments') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_return_line_validate_insert
            BEFORE INSERT ON sales_return_items
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM invoice_items ii JOIN sales_returns sr ON sr.id=NEW.return_id
                    WHERE ii.id=NEW.invoice_item_id AND ii.product_id=NEW.product_id
                      AND ii.invoice_id=sr.invoice_id
                ) THEN RAISE(ABORT,'return line must belong to its source invoice and product') END;
                SELECT CASE WHEN NEW.quantity > (
                    SELECT ii.quantity-COALESCE((SELECT SUM(old.quantity) FROM sales_return_items old
                        WHERE old.invoice_item_id=NEW.invoice_item_id),0)
                    FROM invoice_items ii WHERE ii.id=NEW.invoice_item_id
                ) THEN RAISE(ABORT,'returned quantity exceeds sold quantity') END;
                SELECT CASE WHEN NEW.line_refund < 0 OR NEW.unit_refund < 0
                    THEN RAISE(ABORT,'return refund cannot be negative') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_customer_ledger_amount_insert
            BEFORE INSERT ON customer_ledger
            BEGIN
                SELECT CASE WHEN (NEW.debit + NEW.credit) <= 0
                    THEN RAISE(ABORT,'customer ledger entry must have a positive amount') END;
            END;
        """)

        # Categories were previously created lazily by the Products screen, which broke purchase
        # receiving on a fresh install. The table now belongs to the schema.
        connection.execute("CREATE TABLE IF NOT EXISTS product_categories (name TEXT PRIMARY KEY COLLATE NOCASE)")

        # Migration 8: checkout idempotency key, login lockout, return disposition, policy settings.
        add_column("invoices", "request_id", "TEXT")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_request_id ON invoices(request_id) WHERE request_id IS NOT NULL")
        add_column("users", "failed_logins", "INTEGER NOT NULL DEFAULT 0")
        add_column("users", "locked_until", "TEXT")
        add_column("sales_return_items", "disposition", "TEXT NOT NULL DEFAULT 'restock' CHECK(disposition IN ('restock','damaged'))")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_by INTEGER REFERENCES users(id)
            )
        """)

        migrations = [
            (1, "customer_credit_payment_and_audit_ledger"),
            (2, "inventory_movements_and_product_variants"),
            (3, "business_date_backfill_and_legacy_payment_import"),
            (4, "referential_integrity_and_open_shift_guard"),
            (5, "database_invariants_and_append_only_ledgers"),
            (6, "controlled_legacy_return_classification"),
            (7, "invoice_math_return_quantity_and_ledger_constraints"),
            (8, "checkout_idempotency_lockout_return_disposition_settings"),
        ]
        for version, name in migrations:
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version,name) VALUES(?,?)", (version,name))


if __name__ == "__main__":
    initialize_database()
    print("Database initialized successfully!")
    print(f"Database location: {DATABASE_PATH}")
