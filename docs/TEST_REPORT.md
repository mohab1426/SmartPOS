# SmartPOS — Test Report (1.1.0-rc1, 2026-10-31)

## Environment (what was actually available)
Python 3.12.3, Linux, **no network**. Not installed and not installable: `pytest`, `ruff`, `mypy`, `PySide6`, `PyInstaller`.
Consequently the suite was executed with a **minimal stand-in runner** I wrote (fixtures, `raises`, `parametrize`,
`monkeypatch`, `tmp_path`) — kept outside the deliverable. Tests use only plain pytest features, but **they have not been run
under real pytest**; CI (`.github/workflows/ci.yml`) will do that on first push.

## Commands and results
| Command | Result |
|---|---|
| `python -m compileall -q app.py database.py services tests scripts import_legacy_database.py ui_theme.py` | OK |
| stand-in runner over `tests/` | **88 passed, 0 failed, 0 skipped** |
| (baseline, before changes) same runner, original 20 tests | 20 passed — confirms the status doc's "20 passed" |
| `ruff check .` | **NOT RUN** (unavailable). Substitute: own AST checks for undefined names, unused imports, unused locals → none remaining |
| type checking (mypy) | **NOT RUN** |
| `scripts/verify_migrations_on_copies.py` on the 7 supplied DBs | 7/7: schema v8, counts & totals preserved, `integrity_check=ok`, FK violations 0, run twice (idempotent), originals byte-identical |
| PyInstaller build / Windows EXE / GUI smoke tests | **NOT RUN** |

## Test inventory (83 functions → 88 cases; one test is parametrized ×6)
test_services 20 (original) · test_auth_permissions 10 · test_checkout_returns 19 · test_shifts_ledger 8 ·
test_inventory 9 · test_reporting_business_day 6 (+5 parametrized cases) · test_database_backup 11.

## Mutation check (do the tests detect regressions?)
10 deliberate faults injected one at a time: 9 killed by failing tests. The 1 survivor (removing only the explicit
`user_id is None` guard) is an *equivalent mutant*: `require_active_admin` independently rejects a missing id. A first
round also exposed an untested race (debt collection); a concurrency test was added and now kills that mutant.

## Real defects found by the new tests
Fresh-DB `product_categories` missing; top-product value ignored discounts; (plus the audit findings N-01…N-10).

## Not covered
Qt UI (no offscreen tests), printing/barcodes, EXE, Windows paths, performance with large data, multi-PC access to one DB file.
