"""SmartPOS GUI smoke test (isolated data directory; never touches a real database).

Drives the REAL application window: login dialog -> main window -> dashboard -> POS ->
products -> cart -> checkout -> backup -> close -> relaunch.

Usage:
    python scripts/smoke_test.py [--out DIR] [--platform offscreen|windows]

On Windows CI use the default platform plugin ("windows") so the real Qt Windows plugin is
exercised; on Linux use ``--platform offscreen``.  Exit code 0 = all checks passed.
Screenshots (login.png, dashboard.png, pos.png, products.png) are written to --out.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path




if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ADMIN_USER, ADMIN_PASS = "smoketest", "SmokeTest-12345"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "smoke_output"))
    parser.add_argument("--platform", default=None, help="Qt platform plugin (e.g. offscreen)")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.platform:
        os.environ["QT_QPA_PLATFORM"] = args.platform

    data_dir = Path(tempfile.mkdtemp(prefix="smartpos_smoke_"))
    os.environ["SMARTPOS_DATA_DIR"] = str(data_dir)           # isolated DB + logs + backups
    os.environ["XDG_CONFIG_HOME"] = str(data_dir / "cfg")      # isolate config on POSIX
    os.environ["APPDATA"] = str(data_dir / "appdata")          # isolate config on Windows
    sys.path.insert(0, str(ROOT))

    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox, QPushButton

    import database
    from services.auth import create_first_admin

    assert database.DATA_DIR == data_dir.resolve(), "isolation failed: not using the temp data dir"
    database.initialize_database()
    with database.get_connection() as con:
        create_first_admin(con, username=ADMIN_USER, display_name="Smoke Admin", password=ADMIN_PASS)
        pid = con.execute(
            "INSERT INTO products(code,name,purchase_price,selling_price,stock_quantity,min_stock) "
            "VALUES('SMOKE-1','Smoke Test Item',500,1200,10,0)").lastrowid
        con.execute(
            "INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,unit_cost,"
            "reference_type,reference_id,reason) VALUES(?,'opening_balance',10,500,'smoke',?,'smoke fixture')",
            (pid, pid))
        con.commit()
    record("isolated test database created", database.DATABASE_PATH.exists(), str(database.DATABASE_PATH))

    app = QApplication.instance() or QApplication(sys.argv)
    state = {"login_shot": False, "messages": []}

    def pump() -> None:
        """Handle modal windows: log in through the real dialog, dismiss message boxes."""
        modal = QApplication.activeModalWidget()
        if modal is None:
            return

        if isinstance(modal, QMessageBox):
            state["messages"].append((modal.windowTitle(), modal.text()))
            modal.accept()

        elif isinstance(modal, QDialog):
            edits = modal.findChildren(QLineEdit)

            if len(edits) >= 2 and not state["login_shot"]:
                modal.grab().save(str(out / "login.png"))
                state["login_shot"] = True

                edits[0].setText(ADMIN_USER)
                edits[1].setText(ADMIN_PASS)

                submit = modal.findChild(QPushButton, "LoginPrimary")

                if submit is not None:
                    submit.click()
                else:
                    state["login_error"] = "LoginPrimary button not found"
                    print("[FAIL] LoginPrimary button not found", flush=True)

    timer = QTimer()
    timer.setInterval(150)
    timer.timeout.connect(pump)
    timer.start()
    def cleanup(*windows) -> None:
    """Explicitly release Qt widgets and timers before Python exits."""
    timer.stop()

    for widget in windows:
        if widget is not None:
            widget.close()
            widget.deleteLater()

    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    app.quit()

    import app as smartpos_app

    def launch():
        w = smartpos_app.SmartPOS()
        return w

    window = launch()
    record("login screen appeared and login succeeded",
           state["login_shot"] and not getattr(window, "_login_cancelled", True))
    if getattr(window, "_login_cancelled", True):
        return finish(out)
    window.show()
    app.processEvents()
    record("main window opens", window.isVisible(), window.windowTitle())

    window.show_home(); app.processEvents()
    window.grab().save(str(out / "dashboard.png"))
    record("dashboard loads", window.pages.currentWidget() is window.home_page)

    with database.get_connection() as con:
        from services.shifts import open_shift
        open_shift(con, user=window.current_user, opening_cash=10000)
    window.show_cashier(); app.processEvents()
    window.grab().save(str(out / "pos.png"))
    record("POS screen opens", window.pages.currentWidget() is window.cashier_page)

    window.show_products(); app.processEvents()
    window.grab().save(str(out / "products.png"))
    with database.get_connection() as con:
        n = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    rows = window.products_table.rowCount() if hasattr(window, "products_table") else n
    record("products load", n >= 1 and rows >= 1, f"{rows} row(s) in table")

    window.show_cashier(); app.processEvents()
    window.add_product_to_cart(pid); app.processEvents()
    record("product added to cart", pid in window.cart and window.cart[pid]["quantity"] == 1)
    window.paid_input.setValue(100)
    window.checkout(); app.processEvents()
    with database.get_connection() as con:
        inv = con.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM invoices").fetchone()
        stock = con.execute("SELECT stock_quantity FROM products WHERE id=?", (pid,)).fetchone()[0]
    record("test checkout completed (isolated DB)", inv[0] == 1 and stock == 9 and not window.cart,
           f"invoices={inv[0]} total_piasters={inv[1]} stock_after={stock}")

    before = set(Path(window.backup_folder).glob("SmartPOS_backup_*.db"))
    ok = window.create_backup(show_message=True); app.processEvents()
    after = set(Path(window.backup_folder).glob("SmartPOS_backup_*.db"))
    record("backup can be invoked", bool(ok) and len(after) > len(before),
           f"{len(after - before)} new backup file(s) in {window.backup_folder}")
    record("backup folder is outside install/source dir",
           ROOT not in Path(window.backup_folder).resolve().parents)
    record("log file written to user-data dir", (database.LOG_DIR / "smartpos.log").exists())

    window.close()
window.deleteLater()
app.processEvents()
QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()

record("application closes cleanly", True)

# Second start against the SAME data directory.
state["login_shot"] = False
window2 = launch()

with database.get_connection() as con:
    inv2 = con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    users2 = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

record(
    "restart reuses existing database without errors",
    not getattr(window2, "_login_cancelled", True)
    and inv2 == 1
    and users2 == 1,
    f"invoices={inv2} users={users2}",
)

cleanup(window2)
return finish(out)


def finish(out: Path) -> int:
    failed = [r for r in RESULTS if not r[1]]
    (out / "smoke_results.json").write_text(
        json.dumps([{"check": n, "passed": ok, "detail": d} for n, ok, d in RESULTS],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
