# Migrations

`database.initialize_database()` is a single **idempotent** routine (CREATE IF NOT EXISTS / add-column-if-missing /
guarded backfills / CREATE TRIGGER IF NOT EXISTS) that runs on every start. `schema_migrations` records which
versions this build has applied (1…8) and is used to decide whether a safety snapshot is needed; it is a record, not a
gate. SQLite DDL auto-commits, so the upgrade is *not* one transaction — safety comes from (a) the snapshot taken
first and (b) idempotency: an interrupted upgrade finishes on the next start.

| v | Content |
|---|---|
|1|customers, customer_ledger, invoice_payments, audit_log, return metadata columns|
|2|inventory_movements, product_groups/variant columns|
|3|business_date backfill (04:00), legacy payment import (`legacy_other`), unknown refund method, opening balances|
|4|invoice shift/user validation triggers, one open shift per user|
|5|invariant triggers, append-only ledgers|
|6|controlled legacy return classification|
|7|invoice math / return quantity / ledger amount triggers|
|8|`invoices.request_id` (idempotent checkout), `users.failed_logins/locked_until`, `sales_return_items.disposition`, `app_settings`, `product_categories` moved into the schema|

**Snapshot:** before upgrading an existing database whose version < 8 the app writes
`migration_backups/pre_migration_v<old>_to_v8_<timestamp>.db` via the backup API (not blocked by FK warnings).
**Rollback:** close the app, remove `-wal/-shm`, copy the snapshot over `smartpos.db`.
**Newer database / older app:** restore validation rejects schema versions above the build's.
Verified on temp copies of all seven supplied databases (`scripts/verify_migrations_on_copies.py`) and on a synthetic
legacy database in `tests/test_database_backup.py`.
