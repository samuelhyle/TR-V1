"""Small local storage measurements; never fetches marketplace data."""

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import backups, config, db


def measure(reference=None):
    reference = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn = db.connect()
    started = time.monotonic()
    try:
        # Representative reads used by queue/status pages, without loading rows.
        for sql in ("SELECT COUNT(*) FROM listings",
                    "SELECT COUNT(*) FROM followup_tasks WHERE state='ready' AND due_at<=?",
                    "SELECT COUNT(*) FROM aggregate_partitions WHERE state='open'"):
            conn.execute(sql, (reference.isoformat(),) if "?" in sql else ()).fetchone()
    finally:
        conn.close()
    read_ms = (time.monotonic()-started)*1000
    lock_ms, busy = 0., 0
    sizes = (None, None)
    if not config.POSTGRES_DSN:
        sizes = tuple(path.stat().st_size if path.exists() else 0 for path in
                      (config.SQLITE_PATH, config.SQLITE_PATH.with_name(config.SQLITE_PATH.name+"-wal")))
        # Roll back an empty reservation. Limit the probe to 50 ms so it cannot
        # hold up collection; record contention instead of retrying the probe.
        probe = sqlite3.connect(str(config.SQLITE_PATH), timeout=.05)
        started = time.monotonic()
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            busy = 1
        finally:
            lock_ms = (time.monotonic()-started)*1000
            probe.close()
    conn = db.connect()
    try:
        conn.execute("INSERT INTO storage_checks(day,checks,busy_checks,read_ms_sum,read_ms_max,"
                     "lock_ms_sum,lock_ms_max,database_bytes,wal_bytes) VALUES (?,1,?,?,?,?,?,?,?) "
                     "ON CONFLICT(day) DO UPDATE SET checks=storage_checks.checks+1,"
                     "busy_checks=storage_checks.busy_checks+excluded.busy_checks,"
                     "read_ms_sum=storage_checks.read_ms_sum+excluded.read_ms_sum,"
                     "read_ms_max=CASE WHEN excluded.read_ms_max>storage_checks.read_ms_max "
                     "THEN excluded.read_ms_max ELSE storage_checks.read_ms_max END,"
                     "lock_ms_sum=storage_checks.lock_ms_sum+excluded.lock_ms_sum,"
                     "lock_ms_max=CASE WHEN excluded.lock_ms_max>storage_checks.lock_ms_max "
                     "THEN excluded.lock_ms_max ELSE storage_checks.lock_ms_max END,"
                     "database_bytes=excluded.database_bytes,wal_bytes=excluded.wal_bytes",
                     (reference.date().isoformat(), busy, read_ms, read_ms, lock_ms, lock_ms, *sizes))
        conn.execute("DELETE FROM storage_checks WHERE day<?",
                     ((reference.date()-timedelta(days=89)).isoformat(),))
    finally:
        conn.close()
    return {"read_ms": round(read_ms, 2), "lock_ms": round(lock_ms, 2), "busy": bool(busy)}


def report(reference=None):
    reference = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    days = db.query("SELECT * FROM storage_checks WHERE day>=? ORDER BY day",
                    [(reference.date()-timedelta(days=89)).isoformat()])
    for row in days:
        row["mean_read_ms"] = round(row.pop("read_ms_sum") / row["checks"], 2)
        row["mean_lock_ms"] = round(row.pop("lock_ms_sum") / row["checks"], 2)
        row["read_ms_max"] = round(row["read_ms_max"], 2)
        row["lock_ms_max"] = round(row["lock_ms_max"], 2)
    return {"backend": db.dialect(), "sample_retention_days": 90, "days": days,
            "backup": backups.status(reference),
            "note": "Status-query and empty write-reservation samples, not a load test. "
                    "Busy samples detect contention at these instants, not every blocked collector write. "
                    "PostgreSQL has read timings only; native backup and writer telemetry require deployment setup."}
