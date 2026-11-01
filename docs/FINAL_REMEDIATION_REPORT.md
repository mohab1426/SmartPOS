# SmartPOS — Audit Remediation Report (interim, partially verified)

**Base:** GitHub `main` @ `73582d935bfbe6fd442595183533d8d7c8fb7c44`; working branch `remediation/port-and-continue`. See `docs/REMEDIATION_PORT_REPORT.md` for what was ported vs already present. Sections below were written before the port; the addendum at the end supersedes them where they differ.

## 1. Executive summary
This pass implemented and tested the highest-value fixes that can be validated **without a GUI, Windows or
network**. It is **not** the complete remediation requested, and it is **not production-ready**. The sandbox had no
PySide6, no Windows, no GitHub access, no network, no real `pytest` and no `ruff`, so:

| Check | Result |
|---|---|
| Real `pytest` | **NOT RUN here.** A throwaway stand-in runner (fixtures/raises/parametrize only; not shipped) ran the suite: baseline **88 passed** (matches your real run), after changes **104 passed, 0 failed**, GUI layout test skipped (no PySide6). Please re-run real `pytest`. |
| `ruff` | **NOT RUN.** Substitute: `compileall` on all sources passes, plus an AST check for unused imports (one unused import in `app.py` was found and removed). |
| Database integrity | Run on a temp DB after create-product + sale: `integrity_check = ok`, `foreign_key_check` = 0 violations. Backup/restore tests validate integrity too. |
| Source GUI smoke test, GUI layout test | **NOT RUN** (no PySide6). The earlier invoice-panel layout fix is still unverified. |
| Frozen smoke test, Windows build, release package inspection, SHA-256 | **NOT RUN / NOT VERIFIED.** |
| Git baseline / final commit | **N/A**: the supplied archive has no `.git`; no commit hashes exist. |

Every GUI-facing change below (`app.py`) is **implemented but unverified**; its service-layer logic is tested.

## 2. Finding-by-finding status
| ID | Sev | Status | Evidence / note |
|---|---|---|---|
| F-01 backup retention | High | **Partially fixed** | Tiered retention (3 h all, 48 h hourly, 60 d daily, 12 mo monthly), validated second-location copy function, >24 h stale warning in sidebar; unit-tested (`test_database_backup`). **Not done:** UI to pick a second location, automatic off-device copy. |
| F-02 silent startup failure | High | **Implemented, unverified** | `startup_guard.py` (log first, excepthook, dialog naming the log). Logging/hook/fallback unit-tested; dialog and frozen-EXE behaviour not run. |
| F-03 backups on GUI thread | Med | **Still open** | Not changed. |
| F-04 shift close message | Med | **Fixed (service) / UI unverified** | `close_shift(expected_cash=)` compares inside the transaction; test shows the shift stays open. |
| F-05 modal inside write lock | Med | **Implemented, unverified** | Stock reason now asked before any transaction; service rejects a missing reason with no write (tested). Other modal-in-transaction sites were not searched for. |
| F-06 in-place restore | Med | **Not changed** | Suspected, never reproduced; existing rollback tests still pass. |
| F-07 unbounded files | Low | **Partially fixed** | Log rotation done (tested). `migration_backups/` pruning not done. |
| F-08 fake migrations | Med | **Still open (deliberate)** | The audit says do this only after the GUI runs in CI and with `verify_migrations_on_copies.py` on real data copies. Not attempted. |
| S-01 UI-only guards, no price audit | Med | **Fixed (service, verified) / GUI wiring unverified** | New `services/catalog.py`; cashier denied on all 8 operations, forged role ignored, price audit old/new (`test_catalog_authorization`). Remaining raw SQL in `app.py` is read-only listing/reporting. |
| S-02 PBKDF2 count | Low-Med | **Fixed and verified** | 600,000 stored per hash; legacy hashes verify and upgrade at login (test). |
| S-03 lock message leaks account | Low | **Open** | |
| S-04 cashier sees revenue | Low | **Open** | Owner policy decision. |
| S-05 no password-change UI | Low | **Open** | |
| S-06 / S-07 | Low/Info | **Open** | Documentation / code-signing not done. |
| C-01 cashier-return policy | High | **OWNER DECISION REQUIRED** | Either expose the setting and a cashier return entry point, or remove the setting and correct `docs/SECURITY.md`. Not changed. |
| C-02 stocktake/damage UI | Med | **Open** | Services exist; no UI added. |
| C-03 customer identity by name | Med | **Open** | Needs a customer-picker UI change. |
| C-04 blind count | Med | **Implemented, unverified** | Cashiers no longer see/pre-fill the expected amount; admin unchanged. **Policy choice, change if you disagree.** |
| C-05 literal `\n` | Low | **Fixed (6 lines), not visually checked** | |
| C-06 / C-07 / C-09 | Low | **Open / unchecked** | Invoice pagination, receipt details, `document.print` API check not done. |
| Roadmap #10 duplicate cart lines | — | **Fixed and verified** | Friendly stock error, no partial data (test). |
| mypy errors (3), coverage | — | **Open** | Tools unavailable. |

## 3. Files changed
`services/catalog.py` (new), `services/backup.py`, `services/auth.py`, `services/shifts.py`, `services/sales.py`,
`startup_guard.py` (new), `app.py`, `ui_theme.py` and the cashier layout work from the previous task,
`SmartPOS.spec`, `SmartPOS_Smoke.spec`, `.github/workflows/ci.yml`, tests (`test_catalog_authorization`,
`test_startup_guard`, additions to `test_database_backup`, `test_auth_permissions`, `test_checkout_returns`,
`test_gui_layout`), docs and `CHANGELOG.md`.

## 4. Database migrations
**None added.** No schema change. Password hashes are rewritten in the existing `password_hash` column (new
self-describing format; old format still accepted).

## 5. Security / accounting / inventory
Security: catalog, price, category and supplier changes are admin-enforced and audited in the service; stronger
password hashing. Accounting and inventory logic: **unchanged** except friendlier validation of duplicate cart
lines (same rule, clearer error) and the compare-inside-transaction guard for shift close. Returns, refunds,
ledger, WAC costing and reports were **not modified**; their existing tests pass.

## 6. OWNER DECISION REQUIRED
1. Which historical database snapshot is the true shop data (not chosen; nothing was selected or merged).
2. Actual refund method of historical returns marked UNKNOWN (not classified; no data touched).
3. Which products have unknown cost basis, and whether the suspicious margin is real or test data.
4. Cashier-return policy (C-01) and whether cashiers may see the dashboard (S-04).
5. Blind-count policy for cashiers (C-04), and the second backup location (USB/synced folder).

## 7. Remaining limitations and production-readiness
Not ready for live use. Still required: run real `pytest`/`ruff`; run the GUI smoke and layout tests (CI job
`gui-layout`) and inspect screenshots; Windows build, frozen smoke test and release inspection; a Windows test of
the real EXE with a **copy** of real data; then the open items above (at minimum F-08 and C-01..C-03 as decided).


## Addendum — status after porting onto GitHub main and follow-up work
| Check | Result |
|---|---|
| Real `pytest` / `ruff` / `mypy` | **NOT RUN** (not installed, no network). Stand-in runner: **128 passed, 0 failed, 1 skipped** (GUI layout test needs PySide6). `compileall` OK. |
| Coverage (custom `sys.monitoring` tool, approximate) | 92.4 % of 1,730 lines in `services/`, `database.py`, `startup_guard.py` (before the money/customer tests); **GUI `app.py`: 0 %** |
| Database validation | `integrity_check=ok`, `foreign_key_check` empty in backup/restore/refund/customer tests |
| Windows CI run / release artifact | **NOT RUN. No Windows run exists for the new commits, so no release artifact is provided.** Push the branch and run the workflow. |
| Final commit | see `git log` on `remediation/port-and-continue` (reported in the hand-off message) |

Newly resolved since the interim report: F-03 (implemented, GUI unverified), F-06 hardening, C-03 (implemented, GUI unverified),
cash-refund-vs-drawer guard (verified by tests), mypy narrowing (unverified by mypy), money tests.

Still open: F-08 (migrations; owner/CI preconditions in the audit), C-01 (owner decision), C-02 (stocktake/damage UI), C-06, C-07,
C-09 (print API names, unverifiable without PySide6), S-03 (left as is: local single-shop threat model), S-04 (owner policy), S-05,
S-06, S-07 (signing), F-07 `migration_backups/` pruning, UI to choose the second backup location.
Owner decisions: unchanged from the section above, plus: is blocking cash refunds when the drawer is short the desired policy?
Production-readiness: **not ready** until GUI smoke/layout tests and the Windows workflow pass on this branch.
