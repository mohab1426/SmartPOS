# Testing

```
python -m pip install -r requirements-dev.txt
python -m compileall -q app.py database.py services tests scripts
python -m ruff check .
python -m pytest -q
```
Tests use isolated temporary databases (`tests/conftest.py`) and call services directly (no UI). Modules:
`test_services` (original 20 regression tests), `test_auth_permissions`, `test_checkout_returns`,
`test_shifts_ledger`, `test_inventory`, `test_reporting_business_day`, `test_database_backup`.
Covered: login/lockout/password workflows, cashier denied every admin service, checkout (cash/credit/split/discount/
change/idempotency/rollback/concurrency), returns (full/partial/over/duplicate/foreign-invoice/cash vs credit/damaged/
authorization), shifts & expected cash, debt repayments & ledger reconciliation (incl. concurrent collection),
legacy UNKNOWN refund lifecycle, WAC maths, damage/stocktake/adjustment, report net/audited figures, 04:00 boundaries,
migrations (legacy shape, repeat, snapshot, originals untouched), WAL-correct backup, invalid backup rejection,
restore security, interrupted-restore rollback, pending-restore quarantine, FK enforcement.
**Not covered:** the Qt UI (no offscreen Qt tests exist; PySide6 was unavailable when this was produced), printing,
barcode scanning, PyInstaller output. Use `scripts/verify_migrations_on_copies.py <project> <recovery-pack>` for the
supplied databases.

## GUI layout regression (cashier invoice panel)
```
python scripts/layout_regression_test.py --platform offscreen --out layout_output
python -m pytest -q tests/test_gui_layout.py      # same test; skipped when PySide6 is not installed
```
Launches the real window on an isolated database, logs in, opens the cashier, resizes to 1280x780, 1366x768,
1440x900, 1024x768 and 1600x900 and asserts on real widget geometry (no overlap between action buttons, subtotal,
discount widgets and inputs; hit-testing; clipping via size hints; checkout/total always inside the panel; every
control reachable through the scroll area). Writes `POS_<W>x<H>.png` and `layout_results.json` to `--out`.
Look at the PNGs as well: the assertions prove geometry, not aesthetics.

## Tests added by the audit remediation
`test_catalog_authorization` (cashier denied every catalog operation, forged role ignored, price audit old/new, stock
reason, delete protection), `test_startup_guard`, tiered-retention/secondary-copy tests in `test_database_backup`,
legacy-hash upgrade in `test_auth_permissions`, duplicate-cart-line and stale-expected-cash tests in
`test_checkout_returns`. Total with the originals: 104 service-level tests (+1 GUI layout test that needs PySide6).
