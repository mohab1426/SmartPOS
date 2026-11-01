# Changelog

## Unreleased — Follow-up remediation on top of GitHub main 73582d9
- Cash refunds can no longer exceed the cash the drawer is expected to hold (same rule as expenses/withdrawals).
- Automatic backups run on a worker thread (`AutoBackupThread`, `run_backup_job`) so the cashier screen never freezes.
- Restore: if the automatic rollback itself fails, the error names the untouched pre-restore copy.
- Checkout identifies an existing customer by id (`selected_customer_id`); typing an unknown name for a credit/split
  sale asks for confirmation before creating a new debtor account.
- Tests: money conversion (was untested), refund-vs-drawer, store-credit semantics, rollback failure, backup job,
  same-name customers. mypy-flagged `int | None` call sites narrowed in `services/sales.py`.

## Unreleased — Audit remediation (service-level; see docs/FINAL_REMEDIATION_REPORT.md)
- New `services/catalog.py`: product create/update/delete, category and supplier maintenance are now admin-checked
  inside the service (live role re-read) and audited with old/new values, including `product_price_changed` (S-01).
- Backups: tiered retention (all copies for 3 h, hourly for 48 h, daily for 60 days, monthly for a year), optional
  validated copy to a second location, stale-backup (>24 h) warning in the sidebar (F-01, partial).
- `startup_guard.py`: rotating log (1 MB x 5) configured before startup, `sys.excepthook`, and a dialog that names the
  log file when startup fails (F-02, F-07 partial).
- `close_shift(..., expected_cash=)` compares the expected cash inside the transaction; the shift stays open if it
  changed (F-04). Cashiers now do a blind count (expected amount not shown/pre-filled) (C-04).
- Stock-adjustment reason is collected before any transaction starts (F-05).
- Sales: the same product on several cart lines is validated by total quantity with a friendly message.
- Passwords: PBKDF2 600,000 iterations stored per hash; legacy 240,000-iteration hashes keep working and are
  upgraded on the next successful login (S-02).
- Fixed six message strings that showed a literal backslash-n (C-05).

## Unreleased — Cashier invoice panel layout fix (frontend only)
- Fixed overlapping cart action buttons / buttons covering the subtotal label in the cashier invoice panel
  (`app.py::create_cashier_page`, `ui_theme.py`). Root cause: the window has an explicit minimum size
  (1000x650), so Qt did not enforce the invoice layout's own minimum height (~740 px) and the vertical
  stack was laid out below its minimum. The panel is now a scroll area (header, cart table, actions,
  discount, payment) plus a pinned footer (final total + checkout button).
- Action buttons rebuilt as two explicit rows with equal widths/heights and tooltips; cart table columns
  no longer clip their Arabic headers (two headers shortened, full names in tooltips).
- New `scripts/layout_regression_test.py` + `tests/test_gui_layout.py`: real-window geometry assertions at
  1280x780, 1366x768, 1440x900, 1024x768 and 1600x900 (skipped automatically if PySide6 is missing).
- No change to services, database, schema, accounting, checkout or signal connections.

## 1.1.0-rc1 — 2026-10-31
### Security
- Closed authorization bypass: `receive_purchase_line(user_id=None)` skipped the admin check.
- Service-layer authentication/user management (`services/auth.py`): per-user-salt PBKDF2, constant-time compare,
  generic login errors, lockout after 5 failures, password policy, change/reset workflows, last-admin protection.
- Restore now requires the admin password **in the service**, audits every restore, and rolls back on failure.
- Shifts, cash movements, purchase receiving, stocktake, damage and settings moved behind service-layer checks.
### Financial / inventory
- Idempotent checkout (`request_id`) prevents duplicate invoices on double-click/retry.
- Return authorization policy and per-return inventory disposition (`restock` / `damaged`).
- Audited profit excludes unknown-cost lines (previously counted as 100% margin).
- Top-products value is now discount-allocated and reconciles to net sales.
- Debt collection takes the write lock before the balance check (race fixed).
- Expenses/withdrawals cannot exceed expected drawer cash.
- GUI money input converted with Decimal instead of `round(float*100)`.
- New: `record_damage`, `apply_stocktake`, reconciliation reports (customer ledger, stock ledger, inventory value).
### Database
- Migration 8; pre-migration safety snapshot for existing databases (was missing).
- `product_categories` is now part of the schema (fresh-install purchase receiving failed without it).
### Quality
- 88 tests (was 20); mutation-checked; CI adds compile + ruff.

## 1.0.0 — 2026-10-31

- Removed implicit startup restore and quarantined legacy pending restore files.
- Added per-user storage paths, safe migration copy and SQLite connection cleanup.
- Added admin revalidation, restore re-authentication, safe restore rollback and backup retention.
- Added customer receivables, cash/credit/split payments, collections and return tender tracking.
- Corrected cash drawer reconciliation for recorded cash refunds and legacy refund classification.
- Added 04:00 business-date migration and return-aware net product reporting.
- Added moving weighted-average purchase receiving and stock movement ledger.
- Added SQLite business invariants, schema migration version 7 and append-only financial ledgers.
- Added automated tests, pinned direct dependencies, source-only release spec and explicit recovery import tool.
- Preserved the original database snapshots separately rather than choosing between divergent copies.

## 1.1.0-rc2 (2026-10-31) - packaging only
- FIX (blocking): app crashed right after login (`self.cart` / `change_label` used before creation in the cashier page). Found by the new GUI smoke test.
- Lint: removed an unused variable and import so CI `ruff` passes.
- Spec: UPX disabled (can corrupt Qt DLLs), all service modules listed, dev-only modules excluded.
- Added `scripts/smoke_test.py`, `SmartPOS_Smoke.spec` (CI-only), `.github/workflows/windows-release.yml`, new `build_windows.ps1`.
