# Architecture

```
app.py            PySide6 UI (still large: ~2 900 lines) — calls services, no business rules for the flows below
ui_theme.py       stylesheet / widget polish
database.py       paths, connection factory, schema + migrations
services/         business logic, all authorization and transactions
  tx.py           write_transaction(): BEGIN IMMEDIATE + commit/rollback, or join caller's transaction
  security.py     live-DB role checks, restore validation
  auth.py         hashing, login/lockout, users, password change/reset
  sales.py        checkout (idempotent), returns, legacy classification
  customers.py    customer accounts, ledger, collections
  shifts.py       open / cash movement / close
  inventory.py    movements, adjust, damage, stocktake, purchase lines (WAC)
  purchasing.py   atomic purchase receiving
  accounting.py   business day, expected cash, WAC formula
  reporting.py    snapshots, net/audited profit, reconciliation reports
  backup.py       backup + secured restore
  settings.py     admin policy flags     audit.py  audit log     money.py  piaster conversion
tests/            pytest suite (isolated temp databases)       scripts/  verification helpers
```
UI → services → SQLite. Services take an open connection and a user dict and never import Qt.

**What is NOT done:** `app.py` was not split into `ui/views|dialogs`; a repository layer was not introduced;
14 UI-side INSERT/UPDATE/DELETE statements remain (product, category and supplier CRUD) — these have **no service-layer authorization** yet, plus the read queries for tables. Splitting a 2 900-line
Qt module without being able to run Qt here was judged a greater risk to the working UI than benefit; the
financial and security-relevant write paths were moved first.
