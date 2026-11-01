# Remediation Port Report

**GitHub base:** `main` @ `73582d935bfbe6fd442595183533d8d7c8fb7c44` (from `SmartPOS_Latest_Main.bundle`, HEAD verified).
**Working branch:** `remediation/port-and-continue` (created from that commit; `main` untouched, nothing pushed or merged).
**Method:** three-way comparison of GitHub `main` (G), the earlier `SmartPOS_Complete_Source_UI_Fixed.zip` (U) and my
previous remediation output `SmartPOS_Complete_Source_Remediation.zip` (R). Both archives were available in the workspace.

## Comparison result
* G vs U: GitHub only added `build_windows.ps1` (`python -m venv`), UTF-8 stdout in `scripts/smoke_test.py`, the layout
  regression scripts, `tests/test_gui_layout.py` and the `tests/conftest.py` close-before-yield fix. GitHub `app.py` and
  `ui_theme.py` are byte-identical to U, i.e. the invoice-panel layout fix is already on GitHub.
* R vs U: 18 files (listed below). All 14 pre-existing files I had changed are **byte-identical in G and U**, so the
  remediation patch applied to G with no conflicts and no manual merging (`git apply --check` clean).

## Port table
| Change | Present on GitHub before port? | Action | Files | Verification |
|---|---|---|---|---|
| Invoice-panel layout fix (scroll area + pinned footer) | **Yes** (identical to U) | Skipped | `app.py`, `ui_theme.py` | `diff` G vs U identical; GUI layout test **not run here** |
| GitHub-only fixes (venv command, UTF-8 output, conftest close, layout test edits) | Yes (GitHub-only) | Preserved untouched | `build_windows.ps1`, `scripts/*`, `tests/conftest.py`, `tests/test_gui_layout.py` | not modified by me (diff vs `73582d9`) |
| `services/catalog.py` + admin enforcement + audit old/new | No | **Ported** | `services/catalog.py`, `tests/test_catalog_authorization.py` | 6 tests pass |
| Catalog UI wiring in `app.py` | No | **Ported** | `app.py` | compiled; stub build only; **GUI unverified** |
| Tiered backup retention, second-location copy, stale warning | No | **Ported** | `services/backup.py`, `app.py`, tests | tests pass; warning label GUI unverified |
| `startup_guard.py`, rotating log, crash dialog | No | **Ported** | `startup_guard.py`, `app.py`, specs, CI compile list, tests | 4 tests pass; dialog/frozen EXE not run |
| Shift-close compare inside transaction + blind count | No | **Ported** | `services/shifts.py`, `app.py`, tests | service test passes; UI unverified |
| Stock-adjustment reason before transaction | No | **Ported** | `services/catalog.py`, `app.py` | service test passes; UI unverified |
| Duplicate cart-line stock check | No | **Ported** | `services/sales.py`, test | test passes |
| PBKDF2 600k with legacy compatibility | No | **Ported** | `services/auth.py`, test | test passes |
| Literal `\n` Arabic message fixes (6 lines) | No | **Ported** | `app.py` | compiled; not viewed |
| PyInstaller hidden imports for new modules | No (new modules) | **Ported** | `SmartPOS.spec`, `SmartPOS_Smoke.spec` (two-line additions only, specs not replaced) | frozen build **not run** |
| CI compile list, TESTING/CHANGELOG docs | No | **Ported** (additive) | `.github/workflows/ci.yml`, `docs/*`, `CHANGELOG.md` | n/a |
| `docs/screenshots/POS_before_defect.png` | No | **Skipped** (not remediation work) | — | — |

Commits: `66df19d` services+tests, `93afcc6` UI wiring, `bba6dbb` packaging/docs.
After the port the suite ran **104 passed / 0 failed** (GitHub baseline 88 + 16 ported tests) with a stand-in runner.

## Regression review of ported files
No ported hunk touches a file that GitHub changed. GitHub's `conftest.py` change (closing the fixture connection before
`yield`) is compatible with all new tests, which open their own connections. The new 600k-iteration hashing made the full
run about 18 s slower under the stand-in runner (28 s total).

## Follow-up fixes after the port (new work, not previously done)
| Item | Result |
|---|---|
| F-03 backups on GUI thread | Automatic backup now runs in `AutoBackupThread`; job helper tested off-thread; **GUI unverified** |
| F-06 restore | Existing rollback test already covers failure after activation; hardened: failed rollback names the safety copy; new test |
| Cash refund beyond drawer | **Reproduced** (expected cash went to -2000); fixed with guard + 2 tests. Policy change aligned with expense rule |
| Store-credit semantics | Probed: refund-to-credit after paid debt gives -1000 balance netted by next credit sale; documented by test (no code change) |
| C-03 customer identity | `selected_customer_id` + UI confirm for unknown names; 1 test |
| mypy | 3 call sites narrowed; **mypy not available, not re-run** |
| Coverage | Custom stdlib measurement 92.4 % of `services/`+`database.py`+`startup_guard.py`; `services/money.py` was 0 % -> tests added |
