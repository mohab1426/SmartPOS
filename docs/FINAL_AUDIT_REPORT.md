# SmartPOS — Final Audit Report (release 1.1.0-rc1, 2026-10-31)

**Scope and honesty note.** The original `SmartPOS_Full_Audit_Report` was *not* supplied; the findings C-01…M-04 below come
from the supplied *Remediation Status* document. Its claims were treated as unverified and re-checked against the code and
by running tests. Status vocabulary: FIXED = implemented **and** covered by passing automated tests; PARTIALLY FIXED;
NOT FIXED; UNVERIFIED = cannot be checked in this environment. **GUI, printing, scanner and Windows EXE behaviour is
UNVERIFIED for every row** (PySide6, PyInstaller, Windows unavailable here).

## A. Previously reported findings

| ID | Status | Evidence (files / tests) | Remaining risk |
|---|---|---|---|
| C-01 Cashier backup/restore | **FIXED** (service) | `services/backup.py` (live-DB admin check + password re-entry); `test_cashier_denied_every_admin_operation_directly`, `test_restore_requires_correct_admin_password` | UI wiring edited, not run |
| C-02 Startup pending restore | **FIXED** | `database.ensure_storage_layout` quarantines; `test_pending_restore_file_is_never_applied_at_startup` | none known |
| H-01 Cash refund reconciliation | **FIXED** for new returns; legacy needs owner action | `process_return`, `classify_legacy_return`, `reporting.unclassified_returns`; `test_cash_refund_hits_the_refunding_users_shift`, `test_unknown_historical_return_lifecycle_and_shift_recalculation` | 2 historical returns (895.54) stay UNKNOWN until an admin classifies them |
| H-02 Credit / split payment | **FIXED** | `services/sales.py`, `customers.py`; `test_credit_sale_requires_named_customer_and_split_math`, `test_debt_repayments_…`, `test_ledger_reconciles_…` | customers matched by name only |
| H-03 Business day / net product reporting | **FIXED** (+1 bug found & fixed: top-product value ignored discounts) | `accounting.py`, `reporting.py`; `test_business_date_boundaries[…]`, `test_report_boundaries_…`, `test_net_sales_discounts_returns_and_profit_reconcile` | cutoff 04:00 is a constant, not configurable (see N-12) |
| H-04 Purchase costing | **FIXED** for new receipts | `inventory.receive_purchase_line`, `purchasing.py`; `test_wac_formula_exact_values`, `test_purchase_receiving_…` | historical cost unknowable for 6 supplied lines (not invented) |
| H-05 Stock movement audit | **PARTIALLY FIXED** | ledger append-only; new `record_damage`, `apply_stocktake`, return disposition (tests in `test_inventory.py`, `test_checkout_returns.py`) | no GUI for damage/stocktake; no purchase returns; no parent/variant catalogue |
| H-06 Divergent snapshots / stale EXE | **PARTIALLY FIXED** | no DB in source; `DATABASE_RECOVERY_REPORT.md` shows snapshots *conflict* (invoice #17 differs); no canonical DB chosen | owner must decide; **no EXE built** |
| H-07 Per-user data / backup | **FIXED** (source) | `database.py` paths, `backup.py`; `test_backup_is_correct_for_wal_…`, `test_backup_retention_…` | Windows path behaviour UNVERIFIED |
| M-01 Monolithic app / SQL coupling | **PARTIALLY FIXED** | all financial/security write paths now in services | `app.py` still ~2 900 lines; 14 UI-side writes remain (N-11) |
| M-02 Tests and dependencies | **PARTIALLY FIXED** | 88 tests, pinned deps, CI incl. ruff | tests executed with a stand-in runner, **not real pytest/ruff** (offline) — run once in CI |
| M-03 UI formatting and scale | **UNVERIFIED** | — | needs Windows smoke test |
| M-04 Schema integrity | **PARTIALLY FIXED** | migration 8 + triggers; FK + integrity checks in tests | invoice shift/user remain trigger-guarded, not physical FKs |

## B. New findings from this audit

| ID | Sev. | Finding | Status |
|---|---|---|---|
| N-01 | Critical | `receive_purchase_line(..., user_id=None)` skipped the admin check (authorization bypass) | **FIXED** `test_purchase_receiving_cannot_be_bypassed_with_missing_user` |
| N-02 | High | No safety snapshot before in-place migration of an existing DB | **FIXED** `test_legacy_database_upgrade_…` (mutation-checked) |
| N-03 | High | Users, passwords, shifts, cash movements, purchases changed by raw SQL in the UI with UI-only guards | **FIXED** → `auth.py`, `shifts.py`, `purchasing.py` |
| N-04 | High | Profit counted unknown-cost lines as 100 % margin | **FIXED** `audited_*` fields |
| N-05 | High | No duplicate-checkout protection | **FIXED** idempotency key |
| N-06 | Medium | Concurrent debt collections could race (check-then-write) | **FIXED** (mutation-checked) |
| N-07 | Medium | Top-product value ignored discounts; did not sum to net sales | **FIXED** |
| N-08 | Medium | Fresh install: `product_categories` missing → purchase receiving fails | **FIXED** |
| N-09 | Medium | Restore password only checked in the GUI; no restore audit event | **FIXED** |
| N-10 | Low | `round(float*100)` money conversion (1.005 → 100) | **FIXED** `services/money.py` |
| N-11 | High | Product price edit/delete and category/supplier CRUD authorized only by hidden UI | **NOT FIXED** |
| N-12 | Medium | Business-day cutoff/timezone not configurable (spec §6) | **NOT FIXED** |
| N-13 | Medium | Status doc said migrations are "versioned"; they are one idempotent routine (version rows are labels) | **DOCUMENTED** (`docs/MIGRATIONS.md`) |
| N-14 | Info | Returns allowed to cashiers by default | **POLICY FLAG** (`cashier_can_process_returns`) — owner decision |

## C. Acceptance criteria (spec §14)
Met and tested: no auto-restore; cashier denied admin ops via direct service calls (except N-11 catalogue CRUD); safe/tested backup+restore;
cash/credit/split; ledger reconciliation; returns↔shifts; UNKNOWN refunds preserved and flagged; WAC; unknown cost not fabricated;
inventory auditable; migrations pass on copies of all 7 supplied DBs; automated tests for critical workflows; none skipped.
**Not met / unverified:** reports reconcile (partly: customer debt, inventory value and audited profit exist; supplier balances and
returning-customer reports not implemented); business-day configurable; refreshed RTL UI intact (unverified); Windows packaging
(spec/script prepared, never run); installation docs written but untested; modular architecture (partial).

**Conclusion: release candidate. Not "production-ready" by the criteria you set.**
