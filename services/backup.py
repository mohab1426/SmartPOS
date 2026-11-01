from __future__ import annotations
import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from services.auth import verify_password
from services.security import require_admin_user, validate_restore_database


def validate_database_file(path: str | Path) -> None:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database file not found: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=20)
    try:
        validate_restore_database(connection)
    finally:
        connection.close()


_STAMP_FORMAT = "%Y%m%d_%H%M%S_%f"


def _backup_time(path: Path) -> datetime:
    """Backup time from the file name (falls back to mtime for hand-copied files)."""
    try:
        return datetime.strptime(path.stem[len("SmartPOS_backup_"):], _STAMP_FORMAT)
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime)


def tiered_retention_keep(backups: list[Path], now: datetime, *, recent_hours: int = 3,
                          hourly_hours: int = 48, daily_days: int = 60, monthly_months: int = 12) -> set[Path]:
    """Decide which backups survive pruning (pure function, no deletion).

    * newer than ``recent_hours``        -> keep every copy
    * up to ``hourly_hours`` old         -> newest copy of each hour
    * up to ``daily_days`` old           -> newest copy of each day
    * up to ``monthly_months`` months    -> newest copy of each month
    * older                              -> removed
    The newest copy is always kept.  With 10-minute backups this is at most ~140 small files
    while still allowing recovery from a mistake noticed days or weeks later.
    """
    stamped = sorted(((_backup_time(p), p) for p in backups), key=lambda item: item[0], reverse=True)
    keep: set[Path] = set()
    seen: set[tuple] = set()
    for index, (when, path) in enumerate(stamped):
        age = now - when
        if index == 0 or age < timedelta(hours=recent_hours):
            keep.add(path)
            continue
        if age < timedelta(hours=hourly_hours):
            bucket = ("h", when.year, when.month, when.day, when.hour)
        elif age < timedelta(days=daily_days):
            bucket = ("d", when.year, when.month, when.day)
        elif age < timedelta(days=30 * monthly_months):
            bucket = ("m", when.year, when.month)
        else:
            continue
        if bucket not in seen:
            seen.add(bucket)
            keep.add(path)
    return keep


def apply_tiered_retention(backup_dir: str | Path, now: datetime | None = None) -> list[Path]:
    """Delete backups outside the tiered policy; returns the deleted paths."""
    backups = list(Path(backup_dir).glob("SmartPOS_backup_*.db"))
    keep = tiered_retention_keep(backups, now or datetime.now())
    removed = [p for p in backups if p not in keep]
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def latest_backup_age(backup_dir: str | Path, now: datetime | None = None) -> timedelta | None:
    """Age of the newest backup, or None when there is none (used for the stale-backup warning)."""
    backups = list(Path(backup_dir).glob("SmartPOS_backup_*.db"))
    if not backups:
        return None
    return (now or datetime.now()) - max(_backup_time(p) for p in backups)


def copy_backup_to_secondary(backup_path: str | Path, secondary_dir: str | Path) -> Path:
    """Copy a validated backup to a second location (USB drive / synced folder); raises on failure."""
    secondary = Path(secondary_dir).expanduser().resolve()
    secondary.mkdir(parents=True, exist_ok=True)
    target = secondary / Path(backup_path).name
    shutil.copy2(backup_path, target)
    check = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=20)
    try:
        validate_restore_database(check)
    except Exception:
        check.close()
        target.unlink(missing_ok=True)
        raise
    check.close()
    return target


def create_database_backup(database_path: str | Path, backup_dir: str | Path,
                           retain: int = 30, *, tiered: bool = False) -> Path:
    """Create a validated SQLite backup. ``tiered=True`` applies the long-history tiered retention
    policy (see ``tiered_retention_keep``) instead of keeping only the newest ``retain`` files."""
    database_path = Path(database_path).resolve()
    backup_dir = Path(backup_dir).expanduser().resolve()
    backup_dir.mkdir(parents=True,exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = backup_dir / f"SmartPOS_backup_{stamp}.db"
    source = sqlite3.connect(str(database_path),timeout=30)
    target = sqlite3.connect(str(destination),timeout=30)
    try:
        source.backup(target)
        validate_restore_database(target)
    except Exception:
        target.close()
        source.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
    if tiered:
        apply_tiered_retention(backup_dir)
        return destination
    backups = sorted(backup_dir.glob("SmartPOS_backup_*.db"),key=lambda p:p.stat().st_mtime,reverse=True)
    for stale in backups[max(1,int(retain)):]:
        stale.unlink(missing_ok=True)
    return destination


def run_backup_job(database_path: str | Path, backup_dir: str | Path, *, tiered: bool = True):
    """Run one backup for a background thread: never raises, returns ``(path, None)`` or ``(None, error)``.

    The job opens its own SQLite connections, so it is safe to run off the GUI thread.
    """
    try:
        return create_database_backup(database_path, backup_dir, tiered=tiered), None
    except Exception as exc:  # noqa: BLE001 - reported to the caller instead of killing the thread
        return None, f"{type(exc).__name__}: {exc}"


def _record_restore_event(safety_dir: Path, database_path: Path, event: dict) -> None:
    """Audit a restore twice: in the restored DB (if it has an audit_log) and in a JSONL file
    next to the safety copies, which survives even when the restored DB predates audit_log."""
    safety_dir.mkdir(parents=True, exist_ok=True)
    with open(safety_dir / "restore_events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    con = sqlite3.connect(str(database_path), timeout=30)
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone():
            con.execute("INSERT INTO audit_log(user_id,username,action,entity_type,entity_id,details) "
                        "VALUES(?,?,?,?,?,?)",
                        (event["user_id"], event["username"], "database_restored", "database",
                         event["source_name"], json.dumps(event, ensure_ascii=False, sort_keys=True)))
            con.commit()
    finally:
        con.close()


def restore_database_file(source_path: str | Path, database_path: str | Path,
                          safety_dir: str | Path, current_user: dict, *,
                          admin_password: str) -> Path:
    """Restore through the SQLite backup API with a verified rollback copy.

    Requirements enforced here (not in the UI): an active administrator re-read from the
    live database, correct password re-entry, a valid/integral/supported source file, a
    pre-restore safety snapshot, automatic rollback if activation fails, and an audit event.
    """
    require_admin_user(current_user)
    database_path = Path(database_path).resolve()
    live_auth = sqlite3.connect(str(database_path), timeout=20)
    try:
        auth_row = live_auth.execute("SELECT role,active,password_hash,username FROM users WHERE id=?",
                                     (current_user.get("id"),)).fetchone()
    finally:
        live_auth.close()
    if not auth_row or auth_row[0] != "admin" or not auth_row[1]:
        raise PermissionError("لا يوجد حساب مدير نشط مطابق داخل قاعدة البيانات الحالية")
    if not admin_password or not verify_password(admin_password, auth_row[2]):
        raise PermissionError("كلمة مرور المدير غير صحيحة")
    source_path = Path(source_path).expanduser().resolve()
    safety_dir = Path(safety_dir).resolve()
    if source_path == database_path:
        raise ValueError("لا يمكن استعادة قاعدة البيانات من نفس الملف المفتوح")
    validate_database_file(source_path)
    safety_dir.mkdir(parents=True,exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safety_path = safety_dir / f"pre_restore_{stamp}.db"

    live = sqlite3.connect(str(database_path),timeout=30)
    safety = sqlite3.connect(str(safety_path),timeout=30)
    try:
        live.backup(safety)
        validate_restore_database(safety)
    finally:
        safety.close()
        live.close()
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro",uri=True,timeout=30)
    destination = sqlite3.connect(str(database_path),timeout=30)
    try:
        source.backup(destination)
        validate_restore_database(destination)
    except BaseException:
        destination.close()
        source.close()
        # Roll the live DB back from the safety snapshot if activation/validation failed.
        try:
            rollback_source = sqlite3.connect(str(safety_path),timeout=30)
            rollback_target = sqlite3.connect(str(database_path),timeout=30)
            try:
                rollback_source.backup(rollback_target)
            finally:
                rollback_target.close()
                rollback_source.close()
        except Exception as rollback_error:
            # Never leave the owner guessing: say exactly where the untouched pre-restore copy is.
            raise RuntimeError(
                f"فشلت الاستعادة وتعذر الرجوع تلقائيًا للقاعدة السابقة ({rollback_error}). "
                f"النسخة السابقة سليمة ومحفوظة هنا: {safety_path}"
            ) from rollback_error
        raise
    else:
        destination.close()
        source.close()
    _record_restore_event(safety_dir, database_path, {
        "at": datetime.now().isoformat(timespec="seconds"), "user_id": current_user.get("id"),
        "username": auth_row[3], "source_name": source_path.name, "safety_copy": safety_path.name})
    return safety_path
