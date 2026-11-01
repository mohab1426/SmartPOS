#!/usr/bin/env python3
"""Safely import one explicitly selected legacy SmartPOS database snapshot.

This command never merges snapshots and never overwrites a populated live DB.
Run it before the first GUI launch and specify the exact snapshot to import.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import database
from services.security import validate_restore_database


PROTECTED_TABLES = (
    "users", "products", "invoices", "invoice_items", "cash_shifts",
    "cash_movements", "purchase_invoices", "purchase_items", "sales_returns",
)


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for table in PROTECTED_TABLES:
        if table in tables:
            result[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    return result


def _validate(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise FileNotFoundError(f"Snapshot not found: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        validate_restore_database(connection)
        return _counts(connection)
    finally:
        connection.close()


def _copy_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(str(destination_path), timeout=30)
    try:
        source.backup(target)
        validate_restore_database(target)
    finally:
        target.close()
        source.close()


def _has_business_data(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    connection = sqlite3.connect(str(path), timeout=20)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return False
        return any(_counts(connection).values())
    finally:
        connection.close()


def import_snapshot(source: Path) -> tuple[Path, Path | None, dict[str, int]]:
    source = source.expanduser().resolve()
    _validate(source)

    database.ensure_storage_layout()
    destination = database.DATABASE_PATH.resolve()
    if source == destination:
        raise ValueError("The selected snapshot is already the live database.")
    if _has_business_data(destination):
        raise RuntimeError(
            f"A populated database already exists at {destination}. Refusing to overwrite it. "
            "Use the authenticated in-app restore workflow after making a backup."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    temporary = destination.with_name(destination.name + f".{stamp}.importing")
    safety_path = None
    if destination.exists() and destination.stat().st_size:
        safety_path = database.MIGRATION_BACKUPS_DIR / f"pre_import_empty_{stamp}.db"
        database._sqlite_backup(destination, safety_path)

    try:
        _copy_database(source, temporary)
        os.replace(temporary, destination)
        database.initialize_database()
        verified = sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            validate_restore_database(verified)
            final_counts = _counts(verified)
        finally:
            verified.close()
    except Exception:
        temporary.unlink(missing_ok=True)
        if safety_path and safety_path.exists():
            os.replace(safety_path, destination)
        else:
            destination.unlink(missing_ok=True)
        raise
    return destination, safety_path, final_counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely import one SmartPOS database snapshot.")
    parser.add_argument("snapshot", type=Path, help="Exact .db file to import from the recovery pack")
    args = parser.parse_args()
    try:
        target, safety, counts = import_snapshot(args.snapshot)
        print("Import completed and schema migrations validated.")
        print(f"Live database: {target}")
        if safety:
            print(f"Safety copy of the previous empty DB: {safety}")
        for table, count in counts.items():
            print(f"{table}: {count}")
        return 0
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
