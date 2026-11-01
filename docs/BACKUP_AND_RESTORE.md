# Backup and restore

## Backup
`services/backup.create_database_backup` uses the **SQLite online backup API** (safe while the app runs and
correct for WAL databases: uncheckpointed WAL content is included — tested). The copy is validated
(`integrity_check`, required tables, supported schema version, no foreign-key violations) and old copies are
pruned (default keep 30). The GUI creates an automatic backup every 10 minutes and shows its status; the
folder is configurable.

## Restore (administrator only)
`restore_database_file(source, live_db, safety_dir, user, admin_password=...)`:
1. caller must be an admin dict **and** an active admin in the live database;
2. password re-entry is verified against the live database (wrong/empty/None → `PermissionError`, nothing touched);
3. source is validated read-only (exists, integrity ok, SmartPOS tables, schema not newer than this build, FK ok);
4. a verified pre-restore safety copy `pre_restore_<ts>.db` is written to `migration_backups/`;
5. data is copied into the live database through the backup API and re-validated;
6. on **any** failure (including exceptions in step 5) the live database is restored from the safety copy;
7. an audit event is written to the restored DB (if it has `audit_log`) and to `restore_events.jsonl`.
The GUI additionally asks for explicit confirmation and then re-runs the schema upgrade.

Limit: the swap is performed by SQLite's backup API on the open file, not by an atomic file rename; SQLite makes
the page copy transactional, and the safety copy covers the remaining failure modes. A hard power loss
*during* the copy is recovered by SQLite's journal; if a restored DB is wrong, copy the safety file back manually.

## Manual recovery
Close the app, delete `smartpos.db-wal` and `smartpos.db-shm`, copy the wanted `.db` over `smartpos.db`, start the app.
