import sys, os, shutil, tempfile, sqlite3, hashlib, json, pathlib
proj = sys.argv[1]; pack = sys.argv[2]
sys.path.insert(0, proj); os.chdir(proj)
import database
TABLES = ["products","invoices","invoice_items","cash_shifts","cash_movements","sales_returns","sales_return_items","purchase_invoices","purchase_items","users","suppliers"]
def snap(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {t: c.execute(f"select count(*) from {t}").fetchone()[0] for t in TABLES}
    out["sum_invoice_total"] = c.execute("select sum(total) from invoices").fetchone()[0]
    out["sum_stock"] = c.execute("select sum(stock_quantity) from products").fetchone()[0]
    out["sum_refund"] = c.execute("select sum(total_refund) from sales_returns").fetchone()[0]
    c.close(); return out
results = {}
for f in sorted(pathlib.Path(pack).rglob("*.db")):
    rel = str(f.relative_to(pack))
    before_hash = hashlib.sha256(f.read_bytes()).hexdigest()
    td = pathlib.Path(tempfile.mkdtemp())
    database.DATA_DIR = td/"data"; database.DATABASE_PATH = td/"data"/"smartpos.db"; database.CONFIG_DIR = td/"cfg"
    database.BACKUP_DIR = td/"data"/"backups"; database.LOG_DIR = td/"data"/"logs"
    database.MIGRATION_BACKUPS_DIR = td/"data"/"migration_backups"; database.LEGACY_DATA_DIR = td/"nolegacy"; database._STORAGE_READY = False
    (td/"data").mkdir(parents=True); shutil.copy2(f, database.DATABASE_PATH)
    b = snap(database.DATABASE_PATH)
    try:
        database.initialize_database(); database.initialize_database()  # twice = idempotency
        a = snap(database.DATABASE_PATH)
        c = sqlite3.connect(database.DATABASE_PATH)
        integ = c.execute("pragma integrity_check").fetchone()[0]; fk = len(c.execute("pragma foreign_key_check").fetchall())
        ver = c.execute("select max(version) from schema_migrations").fetchone()[0]
        unknown = c.execute("select count(*), coalesce(sum(total_refund),0) from sales_returns where refund_method='unknown'").fetchone()
        unk_cost = c.execute("select count(*) from invoice_items where unit_cost<0").fetchone()[0]
        led = c.execute("select count(*) from customer_ledger").fetchone()[0]
        mv = c.execute("select count(*) from inventory_movements").fetchone()[0]
        drift = c.execute("select count(*) from products p where p.stock_quantity <> (select coalesce(sum(quantity_delta),0) from inventory_movements m where m.product_id=p.id)").fetchone()[0]
        results[rel] = dict(preserved=(b==a), integrity=integ, fk=fk, version=ver, unknown_returns=unknown, unknown_cost_lines=unk_cost, ledger=led, movements=mv, stock_vs_ledger_drift_products=drift, src_untouched=(hashlib.sha256(f.read_bytes()).hexdigest()==before_hash))
        if b!=a: results[rel]["diff"]={k:(b[k],a[k]) for k in b if b[k]!=a[k]}
    except Exception as e:
        results[rel] = dict(error=repr(e))
    shutil.rmtree(td, ignore_errors=True)
print(json.dumps(results, indent=1, ensure_ascii=False))
