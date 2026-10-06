"""Private, bounded SQLite recovery snapshots and isolated restore rehearsals."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db
from .fileio import write_text
from .metrics import parse_ts


def source_key():
    value = config.POSTGRES_DSN or str(config.SQLITE_PATH.resolve())
    return hashlib.sha256(value.encode()).hexdigest()


def unexpected_empty():
    """A previously backed-up corpus must not silently become a fresh database."""
    previous = _read(config.DATA_DIR/'collection_backup_manifest.json') or {}
    if (previous.get('source_key') != source_key() or
            previous.get('restored_counts',{}).get('listings',0) <= 0):
        return False
    return db.scalar('SELECT COUNT(*) FROM listings') == 0


def _read(path):
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def _digest(path, deadline=None):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('Backup checksum exceeded its time budget')
            digest.update(block)
    return digest.hexdigest()


def status(reference=None):
    reference = reference or datetime.now(timezone.utc)
    if config.POSTGRES_DSN:
        return {"status": "unsupported", "note": "Configure PostgreSQL-native backups separately."}
    manifest = _read(config.DATA_DIR / "collection_backup_manifest.json")
    if not manifest or manifest.get("source_key") != source_key():
        return {"status": "missing", "age_hours": None, "off_host_copy": False}
    stamp = parse_ts(manifest.get("created_at"))
    age = (reference - stamp).total_seconds() / 3600 if stamp else None
    valid = (age is not None and age >= 0 and
             Path(manifest.get("backup_database", "")).is_file() and
             manifest.get("integrity") == "ok" and manifest.get("restore_verified") is True)
    return {"status": "ok" if valid and age < 24 else "stale" if valid else "invalid",
            "age_hours": round(age, 2) if age is not None else None,
            "created_at": manifest.get("created_at"),
            "restore_verified": manifest.get("restore_verified", False),
            "timings": manifest.get("timings"),
            "database_bytes": manifest.get("database_bytes"),
            "off_host_copy": False}


def _copy(source, destination, deadline):
    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError("SQLite backup exceeded its time budget")
    # Pin one WAL snapshot so concurrent enrichment cannot restart each page batch.
    owns_snapshot = not source.in_transaction
    if owns_snapshot:
        source.execute('BEGIN')
        source.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()
    try:
        source.backup(destination, pages=256, progress=progress, sleep=0.05)
    finally:
        if owns_snapshot:
            source.rollback()


def _check(conn):
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("SQLite backup integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("SQLite backup foreign-key check failed")


def _bound_queries(conn, deadline):
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)


def _expire(conn, reference):
    cutoff = (reference - timedelta(days=config.LISTING_RETENTION_DAYS)).isoformat()
    with conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_analysis_inputs'").fetchone():
            # Recovery artifacts retain hashes, never temporary seller prose.
            conn.execute("UPDATE listing_analysis_inputs SET input_json=NULL")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='parser_checks'").fetchone():
            cutoff_day = (reference.astimezone(timezone.utc).date()-timedelta(days=config.PARSER_DIAGNOSTIC_DAYS-1)).isoformat()
            conn.execute("DELETE FROM parser_checks WHERE day<?", (cutoff_day,))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_checks'").fetchone():
            conn.execute("DELETE FROM storage_checks WHERE day<?", ((reference.date()-timedelta(days=89)).isoformat(),))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_cycles'").fetchone():
            conn.execute('DELETE FROM collection_cycles WHERE started_at<?',
                         ((reference-timedelta(days=config.COLLECTION_RUN_RETENTION_DAYS)).isoformat(),))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='aggregate_partitions'").fetchone():
            # Close snapshots at their original evidence cutoff, without using
            # the recovery clock to invent a newly observed market day.
            conn.execute("UPDATE aggregate_partitions SET state='frozen',freeze_cutoff=captured_at "
                         "WHERE kind='market' AND state='open' AND period_day<?", (reference.date().isoformat(),))
            conn.execute("UPDATE aggregate_partitions SET state='frozen',freeze_cutoff=captured_at "
                         "WHERE kind='cohort' AND state='open' AND period_day IN "
                         "(SELECT substr(e.enrolled_at,1,10) FROM followup_enrollments e JOIN listings l "
                         "ON l.listing_pk=e.listing_pk WHERE l.last_seen_at<?)", (cutoff,))
        return db.purge_expired_listings(conn, cutoff, preserve_history=False)


def verify(manifest=None, reference=None, timeout=120):
    """Restore into a temporary DB, enforce retention and read it; never replace live data."""
    started = time.monotonic()
    reference = reference or datetime.now(timezone.utc)
    manifest = manifest or _read(config.DATA_DIR / "collection_backup_manifest.json")
    if not manifest or manifest.get("source_key") != source_key():
        raise ValueError("No backup for the configured database")
    path = Path(manifest["backup_database"])
    deadline = started + timeout
    if _digest(path, deadline) != manifest.get("sha256"):
        raise ValueError("Backup checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="tori-restore-") as directory:
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        restored = sqlite3.connect(str(Path(directory) / "restored.sqlite"))
        try:
            _copy(source, restored, deadline)
            restored.execute("PRAGMA foreign_keys=ON")
            _bound_queries(restored, deadline)
            _check(restored)
            removed = _expire(restored, reference)
            _check(restored)
            counts = {table: restored.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("listings", "price_history", "events", "purchases")}
            history_states = {}
            for table in ("followup_tasks", "followup_enrollments", "sweep_page_commits", "listing_search_attributions", "discovery_state", "enrichment_tasks", "parser_checks", "aggregate_partitions", "storage_checks", "classification_changes", "classification_repair", "collection_cycles", "collection_job_runs"):
                if restored.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    counts[table] = restored.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if "aggregate_partitions" in counts:
                history_states = dict(restored.execute("SELECT state,COUNT(*) FROM aggregate_partitions GROUP BY state").fetchall())
        finally:
            restored.close()
            source.close()
    # Pause metadata is carried with the snapshot. A rehearsal never clears it.
    pause = _read(path.parent / "collection_stop.json")
    return {"status": "ok", "integrity": "ok", "restore_verified": True,
            "restore_seconds": round(time.monotonic()-started, 4),
            "history_states": history_states,
            "expired_on_restore": removed, "restored_counts": counts,
            "platform_pause": (pause or {}).get("status")}


def ensure(reference=None, force=False, timeout=120):
    """Create and verify a snapshot when due, preserving the last good manifest on failure."""
    from . import local_runtime
    local_runtime.assert_database_owner()
    if unexpected_empty():
        raise RuntimeError('The research database is unexpectedly empty; preserve the existing recovery backup')
    reference = reference or datetime.now(timezone.utc)
    if timeout <= 0:
        raise TimeoutError('No time remains for a recovery snapshot')
    deadline = time.monotonic() + min(120, timeout)
    if config.POSTGRES_DSN:
        return status(reference)
    lock, error = db.acquire_lock("backup")
    if error:
        return {"status": "skipped", "reason": "Another backup is running"}
    staging = None
    unpublished = None
    try:
        previous = status(reference)
        if not force and previous["status"] == "ok":
            return {**previous, "created": False}
        root = config.DATA_DIR / "collection_backups"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
        target = staging / "research.sqlite"
        source = sqlite3.connect(config.SQLITE_PATH.resolve().as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(str(target))
        started = time.monotonic()
        try:
            _copy(source, destination, deadline)
            copy_seconds = time.monotonic()-started
            destination.execute("PRAGMA foreign_keys=ON")
            _bound_queries(destination, deadline)
            _expire(destination, reference)
            _check(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(target, 0o600)
        pause = _read(config.DATA_DIR / "collection_stop.json")
        if pause:
            write_text(staging / "collection_stop.json", json.dumps(pause))
        manifest = {"format_version": 1, "source_key": source_key(),
                    "created_at": reference.isoformat(), "backup_database": str(target),
                    "database_bytes": target.stat().st_size,
                    "sha256": _digest(target, deadline), "integrity": "ok", "off_host_copy": False}
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('No time remains to verify the recovery snapshot')
        proof = verify(manifest, reference, timeout=remaining)
        manifest["timings"] = {"copy_seconds": round(copy_seconds, 4),
                               "restore_seconds": proof["restore_seconds"],
                               "total_seconds": round(time.monotonic()-started, 4)}
        manifest.update({key: proof[key] for key in
                         ("restore_verified", "expired_on_restore", "restored_counts", "platform_pause")})
        final = root / ("auto-" + reference.strftime("%Y%m%dT%H%M%S%fZ"))
        staging.rename(final)
        staging = None
        unpublished = final
        manifest["backup_database"] = str(final / "research.sqlite")
        write_text(final / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        write_text(config.DATA_DIR / "collection_backup_manifest.json",
                   json.dumps(manifest, indent=2) + "\n")
        unpublished = None
        # Rotate only this implementation's expired, same-database directories.
        cutoff = reference - timedelta(days=config.BACKUP_RETENTION_DAYS)
        for directory in root.glob("auto-*"):
            metadata = _read(directory / "manifest.json")
            stamp = parse_ts((metadata or {}).get("created_at"))
            if (directory != final and not directory.is_symlink() and metadata and
                    metadata.get("format_version") == 1 and metadata.get("source_key") == source_key()
                    and stamp and stamp < cutoff):
                shutil.rmtree(directory)
        return {"status": "ok", "created": True, "restore_verified": True,
                "created_at": reference.isoformat(), "timings": manifest["timings"],
                "age_hours": 0, "off_host_copy": False}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if unpublished is not None:
            shutil.rmtree(unpublished, ignore_errors=True)
        db.release_lock(lock)
