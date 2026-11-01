# Installation, upgrade, backup

**Install:** copy the `dist\SmartPOS` folder (from `BUILD_WINDOWS.md`) anywhere (e.g. `C:\SmartPOS`) and run
`SmartPOS.exe`. First start: create the administrator account (min 8 chars, no defaults exist).
**Existing data:** do not copy `.db` files by hand. Choose ONE snapshot (see `DATABASE_RECOVERY_REPORT.md`) and import it
with `python import_legacy_database.py <file>` before the first start (refuses to overwrite a database that has data).
**Upgrade:** close SmartPOS, make a backup from the app (or copy `%LOCALAPPDATA%\SmartPOS\smartpos.db`), replace the
program folder, start. The database is upgraded automatically and a `pre_migration_*.db` snapshot is written to
`%LOCALAPPDATA%\SmartPOS\migration_backups`. **Rollback:** see `MIGRATIONS.md`.
**Backups:** automatic every 10 minutes (last 30 kept) into the configured folder — point it at a different drive.
**Restore:** Settings → Restore (admin password required); see `BACKUP_AND_RESTORE.md`.
