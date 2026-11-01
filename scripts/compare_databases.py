import sqlite3, sys, hashlib, os, glob
root = sys.argv[1]
files = sorted(glob.glob(root+"/**/*.db", recursive=True))
def q(c, s, a=()):
    try: return c.execute(s, a).fetchall()
    except Exception as e: return f"ERR {e}"
for f in files:
    h = hashlib.sha256(open(f,'rb').read()).hexdigest()
    c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    tables = [r[0] for r in q(c,"select name from sqlite_master where type='table' and name not like 'sqlite_%' order by 1")]
    print("\n###", os.path.relpath(f,root), h[:12], os.path.getsize(f))
    print(" jmode", q(c,"pragma journal_mode"), "uv", q(c,"pragma user_version"), "integrity", q(c,"pragma integrity_check"), "fk", len(q(c,"pragma foreign_key_check")))
    print(" tables", tables)
    if "schema_migrations" in tables: print(" migrations", q(c,"select * from schema_migrations"))
    print(" counts", {t: q(c,f"select count(*) from {t}")[0][0] for t in tables})
    cols = lambda t:[r[1] for r in q(c,f"pragma table_info({t})")]
    print(" invoices cols", cols("invoices"))
    print(" latest", q(c,"select max(created_at), count(*), sum(total), sum(discount) from invoices") if 'created_at' in cols('invoices') else q(c,"select * from invoices order by id desc limit 1"))
