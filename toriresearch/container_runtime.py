"""Run the loopback dashboard and a loopback bridge to the host's local model."""

import os
import signal
import subprocess
import sys
import time

from . import config, db, schema


def check_storage():
    """Refuse an empty/mismounted database instead of creating replacement data."""
    if config.POSTGRES_DSN:
        raise RuntimeError("This local Docker setup uses the existing SQLite database")
    if not config.SQLITE_PATH.is_file():
        raise RuntimeError("Existing database is missing; check the data bind mount")
    conn = db.connect()
    try:
        missing = {table["name"]: sorted({col[0] for col in table["columns"]}-db.existing_columns(conn, table["name"]))
                   for table in schema.TABLES}
        missing = {table: columns for table, columns in missing.items() if columns}
        if missing:
            raise RuntimeError("Database schema needs an additive update: run python3 -m toriresearch.cli init in the owning runtime")
        from . import backups
        if backups.unexpected_empty():
            raise RuntimeError('The saved research corpus is unexpectedly empty; review recovery before starting collection')
        print(f"Using saved database: {config.SQLITE_PATH}; {conn.execute('SELECT COUNT(*) FROM listings').fetchone()[0]} listings", flush=True)
    finally:
        conn.close()


def main():
    check_storage()
    port = int(os.environ.get("TORI_HOST_MODEL_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid local model port")
    stopped = False
    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = []
    try:
        children.append(subprocess.Popen(["socat", "TCP-LISTEN:18080,bind=127.0.0.1,reuseaddr,fork",
                                           f"TCP:host.docker.internal:{port}"]))
        children.append(subprocess.Popen([sys.executable, "-m", "toriresearch.cli", "dashboard",
                                          "--host", "127.0.0.1", "--port", "8787", "--refresh", "30"]))
        children.append(subprocess.Popen([sys.executable, '-m', 'toriresearch.collection_observer']))
        if config.LISTING_ANALYSIS_ENABLED:
            children.append(subprocess.Popen([sys.executable, '-m', 'toriresearch.listing_worker']))
        while not stopped:
            for child in children:
                if child.poll() is not None:
                    return child.returncode or 1
            time.sleep(.2)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    sys.exit(main())
