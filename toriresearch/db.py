"""Database access. SQLite for local development, Postgres for deployment."""

import logging
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from . import config, schema

log = logging.getLogger("toriresearch.db")

# What counts as "this connection is unusable, drop it and let the next caller
# open a fresh one". PostgreSQL raises psycopg2 exceptions, which are not
# `sqlite3.DatabaseError`, so catching only the SQLite type meant a failed query
# on Postgres kept a handle already known to be bad, and the next caller
# inherited the failure instead of a clean connection.
#
# Resolved once at import. psycopg2 is an optional dependency -- the collector
# and ordinary reports are stdlib-only -- and its absence simply means the
# Postgres branch is unreachable, because `connect()` would fail on the same
# import first.
_CONNECTION_ERRORS = [sqlite3.DatabaseError]
try:
    import psycopg2 as _psycopg2
except ImportError:  # pragma: no cover - the SQLite-only default
    pass
else:
    _CONNECTION_ERRORS.extend([_psycopg2.Error, _psycopg2.InterfaceError])
_CONNECTION_ERRORS = tuple(_CONNECTION_ERRORS)
_READ_SNAPSHOT = ContextVar("tori_read_snapshot", default=None)


class _SnapshotConnection:
    """A borrowed read connection; helpers cannot commit or close its snapshot.

    This used to expose `execute` and `close` and nothing else, which meant any
    helper reaching for `conn.dialect`, `conn.transaction()` or `conn.commit()`
    from inside `read_snapshot()` raised `AttributeError` rather than doing the
    obvious thing. Eight write-path helpers read `conn.dialect` -- `drafts`,
    `trades` (x2), `outbox`, `inventory` (x3), `purchases`, `followup`,
    `aggregates` -- and there are twenty `read_snapshot()` blocks, so the trap
    was one refactor away from a 500.

    Writes stay refused. `execute` still rejects anything but `SELECT`, and the
    write/transaction attributes below raise a message that names the mistake
    instead of failing with `AttributeError`. `dialect` is a pure read, so it is
    delegated rather than refused.
    """

    def __init__(self, conn):
        self._connection = conn

    @property
    def dialect(self):
        return self._connection.dialect

    def execute(self, sql, params=()):
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("only SELECT is permitted inside a read snapshot")
        return self._connection.execute(sql, params)

    def close(self):
        pass

    def _refuse(self, name):
        raise ValueError(f"{name} is not available inside a read snapshot; "
                         "open a writable connection with db.connect() instead")

    def transaction(self, *args, **kwargs):
        self._refuse("transaction()")

    def commit(self):
        self._refuse("commit()")

    def rollback(self):
        self._refuse("rollback()")

    def __getattr__(self, name):
        # Anything not defined above is delegated to the real connection so a
        # read helper keeps working, except the names that would end or widen
        # the snapshot, which are already refused above.
        return getattr(self._connection, name)

    def __repr__(self):
        return f"<snapshot {self._connection!r}>"


@contextmanager
def read_snapshot():
    """Pin all query helpers to one read-only, repeatable snapshot in this context."""
    from . import local_runtime
    local_runtime.assert_database_owner()
    if _READ_SNAPSHOT.get() is not None:
        yield
        return
    conn = connect()
    token = None
    try:
        if conn.dialect == "postgres":
            conn._conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        else:
            conn._conn.execute("PRAGMA query_only=ON")
            conn._conn.execute("BEGIN")
        conn._transaction_depth = 1
        token = _READ_SNAPSHOT.set(_SnapshotConnection(conn))
        yield
    finally:
        if token is not None:
            _READ_SNAPSHOT.reset(token)
        conn._conn.rollback()
        conn.close()

# SQLite's INSERT OR IGNORE has no Postgres equivalent. Rather than fork every
# ingester on dialect, the connection rewrites it on the way in.
_SQLITE_ONLY = (
    (re.compile(r"\bINSERT OR IGNORE INTO\b", re.I), "INSERT INTO"),
    (re.compile(r"\bINSERT OR REPLACE INTO\b", re.I), "INSERT INTO"),
)


class _Connection:
    """Dialect shim: one SQL surface, rendered per backend.

    All application SQL is written in SQLite's `?` placeholder style and
    translated on execute. `ON CONFLICT DO NOTHING` and `RETURNING` are used
    in preference to `INSERT OR IGNORE` so the same statement is valid on both
    backends without rewriting.
    """

    def __init__(self, conn, dialect):
        self._conn = conn
        self._dialect = dialect
        self._transaction_depth = 0

    @property
    def dialect(self):
        return self._dialect

    def _translate(self, sql, params):
        if self._dialect != "postgres":
            return sql, params
        out = sql
        ignore = bool(re.search(r"\bINSERT OR IGNORE INTO\b", sql, re.I))
        if re.search(r"\bINSERT OR REPLACE INTO\b", sql, re.I):
            raise ValueError("use an explicit ON CONFLICT update instead of INSERT OR REPLACE")
        for pattern, replacement in _SQLITE_ONLY:
            out = pattern.sub(replacement, out)
        if ignore and "ON CONFLICT" not in out.upper():
            out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        out = re.sub(r"json_extract\(([\w.]+),\s*'\$\.([\w]+)'\)",
                     lambda match: f"(CAST({match[1]} AS JSONB)->>'{match[2]}')", out)
        if params:
            qmarks = out.count("?")
            if qmarks and qmarks != len(params):
                raise ValueError(
                    f"placeholder/parameter mismatch: {qmarks} '?' for {len(params)} params"
                )
            # psycopg2 also interprets '%' inside SQL literals when binding.
            out = out.replace("%", "%%").replace("?", "%s")
        return out, params

    def execute(self, sql, params=()):
        stmt, args = self._translate(sql, params)
        if self._dialect == "postgres":
            cur = self._conn.cursor()
            if args:
                cur.execute(stmt, args)
            else:
                cur.execute(stmt)
            if not self._transaction_depth:
                self._conn.commit()
            return cur
        cur = self._conn.execute(stmt, args) if args else self._conn.execute(stmt)
        if not self._transaction_depth:
            self._conn.commit()
        return cur

    @contextmanager
    def transaction(self):
        """Group related writes, rolling back all of them on failure."""
        outer = self._transaction_depth == 0
        if outer and self._dialect == "sqlite":
            self._conn.execute("BEGIN IMMEDIATE")
        self._transaction_depth += 1
        try:
            yield self
            if outer:
                self._conn.commit()
        except BaseException:
            if outer:
                self._conn.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def connect():
    from . import local_runtime
    local_runtime.assert_database_owner()
    borrowed = _READ_SNAPSHOT.get()
    if borrowed is not None:
        return borrowed
    if config.POSTGRES_DSN:
        import psycopg2
        from psycopg2.extras import DictCursor
        return _Connection(psycopg2.connect(config.POSTGRES_DSN,
                                           cursor_factory=DictCursor), "postgres")
    return _open_sqlite()


# `PRAGMA journal_mode = WAL` used to run on every single connection. It is a
# property of the database file rather than of a connection, so on a database
# that is already in WAL mode it is a pure no-op -- 200 repeats took 1 ms and
# left the file byte-identical -- and it was re-issued hundreds of times per
# dashboard refresh for nothing.
#
# It was not harmless, though. It is the statement that reads the file header,
# so it is the one place where a transient file-level condition became a failed
# request. That is how "Could not check the run status. database disk image is
# malformed" reached the page: a WAL checkpoint extending the growing database
# was briefly observable as a header claiming more pages than the file held,
# SQLite rejected the header, and the exception propagated out of the pragma
# rather than being retried. `PRAGMA integrity_check` was `ok` throughout --
# the image was never actually corrupt.
#
# WAL is therefore confirmed once per database file instead, and only when that
# file is not already in that mode. A restored or freshly created database still
# ends up in WAL, because this runs before the first real query on it.
#
# Keyed by path rather than a plain flag: `config.SQLITE_PATH` is resolved per
# call, so a process that legitimately points at a second database (the test
# fixtures rotate one per test) must not carry the first file's answer over.
_JOURNAL_MODE_CHECKED = set()


def _ensure_journal_mode(conn, path):
    if path in _JOURNAL_MODE_CHECKED:
        return
    if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        conn.execute("PRAGMA journal_mode = WAL")
    _JOURNAL_MODE_CHECKED.add(path)


# Failures this store genuinely produces and that a retry resolves. The first
# three are ordinary lock contention. The last is the one measured above: a
# header read that is inconsistent for the length of one checkpoint and reads
# back correctly immediately afterwards, so it is worth another attempt rather
# than a failed request. Anything else -- a real corrupt image, a missing table,
# a constraint violation -- is deliberately not retried, because retrying it
# would hide a fault that needs a human.
_TRANSIENT_SQLITE = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database disk image is malformed",
)


def _transient(exc):
    message = str(exc).lower()
    return any(text in message for text in _TRANSIENT_SQLITE)


def _open_sqlite():
    """Open one connection, retrying only the failures a retry can clear."""
    from . import local_runtime
    local_runtime.assert_database_owner()
    attempts = config.SQLITE_CONNECT_ATTEMPTS
    path = str(config.SQLITE_PATH)
    for attempt in range(1, attempts + 1):
        conn = None
        try:
            conn = sqlite3.connect(path,
                                   timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000)
            conn.row_factory = sqlite3.Row
            # Wait for the write lock instead of failing on sight. Without this
            # the collector holding a transaction turned into "database is
            # locked" on an unrelated dashboard request.
            conn.execute(f"PRAGMA busy_timeout = {config.SQLITE_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys = ON")
            if config.SQLITE_MMAP_BYTES:
                conn.execute(f"PRAGMA mmap_size={config.SQLITE_MMAP_BYTES}")
            _ensure_journal_mode(conn, path)
            return _Connection(conn, "sqlite")
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if attempt == attempts or not _transient(exc):
                raise
            time.sleep(0.05 * attempt)
            log.warning("database open failed (%s), retrying: %s", type(exc).__name__, exc)


# One connection per thread, reused by the per-query helpers below.
#
# `connect()` is called directly in 62 places that all close what they are
# given, so its contract stays "a connection you own". The helpers did not:
# `db.query`, `db.scalar`, `db.query_one` and `db.execute` each opened and
# closed their own, which meant one dashboard snapshot opened the database 264
# times and took 5.9 s doing it. Each of those opens was another chance to hit
# the header race above, so the count was also the failure rate.
#
# Autocommit semantics are unchanged: SELECTs run outside an explicit
# transaction, so a reused connection still sees the newest committed data on
# every statement, exactly as a fresh connection did.
#
# The path is remembered with the connection because `config.SQLITE_PATH` is
# resolved per call, not captured at import. Anything that repoints it -- the
# test fixtures rotate a fresh tmp file per test, and a restore repoints it for
# real -- must not be handed a connection to the previous file.
_LOCAL = threading.local()


def _pooled():
    """One cached connection per thread, for the configured dialect.

    Postgres is deliberately not pooled. `connect()` opens a fresh connection
    per call there, and pooling it would mean deciding what invalidates it --
    a DSN change, a dropped connection, a `search_path` set per test -- and this
    only ever held one connection per thread against a local file. The read
    helpers below call this unconditionally, so the dialect check is not
    optional: without it, setting `TORI_PG_DSN` made `db.query()` read a SQLite
    file while `db.connect()` returned Postgres, and the two disagreed about
    what database the process was using.
    """
    if config.POSTGRES_DSN:
        return connect()
    conn = getattr(_LOCAL, "connection", None)
    if conn is not None and getattr(_LOCAL, "path", None) != str(config.SQLITE_PATH):
        _discard_pooled()
        conn = None
    if conn is None:
        conn = _open_sqlite()
        _LOCAL.connection = conn
        _LOCAL.path = str(config.SQLITE_PATH)
    return conn


def _discard_pooled():
    """Drop this thread's connection after a failure or a change of database.

    A connection that hit a corrupt read may still be holding a bad page cache
    or a half-finished statement, so the next caller should get a clean one.
    """
    conn = getattr(_LOCAL, "connection", None)
    _LOCAL.connection = None
    _LOCAL.path = None
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _borrow():
    """The connection the per-query helpers should use in this context."""
    borrowed = _READ_SNAPSHOT.get()
    return borrowed if borrowed is not None else _pooled()


def dialect():
    return "postgres" if config.POSTGRES_DSN else "sqlite"


def _recover_statement(conn):
    """Return the connection to a usable state after one failed statement.

    SQLite lets the next statement proceed after a constraint or syntax error;
    Postgres does not. Once a statement fails inside a transaction, Postgres
    marks it aborted and refuses every subsequent command until the transaction
    is rolled back -- so without this, the first unaddable column turned every
    remaining one into the same error, and `unmigratable` reported one real
    problem as eleven identical ones.

    Rolling back is only safe here because each `ALTER TABLE ... ADD COLUMN` is
    its own implicit transaction and the failures are recorded, not swallowed:
    the columns that succeeded are already committed, and the ones that failed
    are reported to the caller. Nothing partial is left in flight.
    """
    if dialect() != "postgres":
        return
    try:
        conn._conn.rollback()
    except Exception:
        # A connection that cannot be recovered is reported by the caller's own
        # failure rather than hidden behind this one.
        log.exception("could not recover the connection after a failed migration statement")


def existing_columns(conn, table):
    if dialect() == "postgres":
        try:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
                "AND table_schema=current_schema()",
                (table,),
            ).fetchall()
        except Exception:
            # A caller arriving here with an already-aborted transaction would
            # otherwise see the abort reported in place of the real cause. This
            # runs inside `migrate`'s loop, so one unrecovered failure poisons
            # every column check after it -- which is how a single unaddable
            # column became eleven identical-looking errors.
            _recover_statement(conn)
            raise
        return {r[0] for r in rows}
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn):
    """Bring one database to the declared shape, and prove which shape that is.

    Two halves, and the second is the one that used to be missing.

    **Shape.** `CREATE TABLE IF NOT EXISTS` silently does nothing when the table
    is already there, so a schema change reaches new databases and not old ones.
    That is how a collector ends up writing columns that do not exist and
    failing on the one database holding real data. `ADD COLUMN` is reconciled
    unconditionally, because it is the one schema change that is safe and
    idempotent to apply retroactively.

    What ADD COLUMN deliberately does NOT do, because doing it silently would be
    worse than not doing it:

      * add NOT NULL to a populated column without a default -- the statement
        fails, or succeeds and rewrites history;
      * change a column's type -- needs a USING clause and a per-column rewrite;
      * add UNIQUE or CHECK constraints -- these need a full-table scan and can
        fail on data that already violates them, and a failed constraint added
        mid-migration leaves the database half-migrated.

    Those need a per-change decision about backfill, which is what
    `schema.MIGRATIONS` is for. Append a step, bump `SCHEMA_VERSION`, and this
    runs it on exactly the databases that have not applied it yet.

    **Version.** The recorded version is what makes "this database is current" a
    checkable claim rather than an assumption. Without it the only evidence a
    database was ever at the declared shape was that nobody had noticed a
    problem. `schema_meta` now records the applied version, and a database whose
    recorded version is *ahead* of the code is refused outright: that means the
    deployment was rolled back, and running older code against newer data is how
    a collector writes values the current schema cannot hold.

    Returns `(added, unmigratable)`. A migration that cannot run raises rather
    than returning, because a version that was not advanced is a database that
    is provably behind -- a louder and more actionable state than a partial list.
    """
    added, unmigratable = [], []
    for table in schema.TABLES:
        table_name = table["name"]
        present = existing_columns(conn, table_name)
        if not present:
            # Table not created yet on this database. `CREATE TABLE IF NOT
            # EXISTS` in init_db makes it, so there is nothing to migrate.
            continue
        for col, decl, _note in table["columns"]:
            if col in present:
                continue
            d = _sqlite_column_type(decl) if dialect() == "sqlite" else decl
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {d}")
            except Exception as exc:
                # One failed column must not abandon the rest, but it must be
                # reported: a partial migration is the exact failure mode this
                # function exists to prevent, so it is surfaced, not swallowed.
                unmigratable.append(f"{table_name}.{col}: {exc}")
                log.warning("could not add column %s.%s: %s", table_name, col, exc)
                _recover_statement(conn)
                continue
            added.append(f"{table_name}.{col}")

    if unmigratable:
        # Do not stamp a version onto a database that is not at the shape. The
        # version is the claim; it has to be earned.
        return added, unmigratable

    _apply_migrations(conn)
    return added, unmigratable


def _apply_migrations(conn):
    """Run every `schema.MIGRATIONS` step this database has not applied."""
    current = recorded_version(conn)
    if current is None:
        # Pre-versioning database. Its columns have just been reconciled against
        # the declaration, so it is stamped with the version whose steps are
        # known to be no-ops on an already-reconciled shape. That is the
        # strongest honest claim available -- it is why the baseline step is
        # documented as reconciliation rather than as a real transformation.
        # Additive reconciliation cannot replace an existing CHECK constraint.
        # Run the idempotent steps on adopted databases as well as versioned
        # ones, so adopting a legacy schema cannot skip its horizon migration.
        current = 0
    if current > schema.SCHEMA_VERSION:
        raise RuntimeError(
            f"database is at schema version {current} but this code declares "
            f"{schema.SCHEMA_VERSION}. The deployment was rolled back or the "
            f"database is newer than the collector. Running older code against "
            f"a newer schema would write values the schema cannot hold; "
            f"restore the matching code version instead."
        )
    applied = []
    for version, name, fn in schema.MIGRATIONS:
        if version <= current:
            continue
        log.info("applying schema migration %d (%s)", version, name)
        fn(conn)
        _record_version(conn, version)
        applied.append(f"{version}:{name}")
    return applied


def recorded_version(conn):
    """The version this database is at, or None if it predates versioning."""
    try:
        row = conn.execute(
            "SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'"
        ).fetchone()
    except Exception:
        # No schema_meta table yet -- a pre-versioning database.
        return None
    if row is None:
        return None
    try:
        # sqlite3.Row and psycopg2 DictRow both index by name.
        return int(row["meta_value"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _record_version(conn, version):
    if dialect() == "postgres":
        conn.execute("INSERT INTO schema_meta (meta_key, meta_value, applied_at) "
                     "VALUES ('schema_version', ?, now()) "
                     "ON CONFLICT (meta_key) DO UPDATE SET meta_value=excluded.meta_value, "
                     "applied_at=excluded.applied_at", (str(version),))
        return
    conn.execute("INSERT INTO schema_meta (meta_key, meta_value, applied_at) "
                 "VALUES ('schema_version', ?, (datetime('now'))) "
                 "ON CONFLICT (meta_key) DO UPDATE SET meta_value=excluded.meta_value, "
                 "applied_at=excluded.applied_at", (str(version),))


def _sqlite_column_type(decl):
    """Translate one Postgres column declaration into SQLite's type vocabulary.

    Applied to ALTER TABLE as well as to CREATE TABLE. Kept in one place so the
    two paths cannot drift: a column created by CREATE TABLE and the same column
    added later by ALTER TABLE must end up with identical types, or an old
    database silently diverges from a new one.
    """
    d = decl
    d = d.replace("BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                  "INTEGER PRIMARY KEY AUTOINCREMENT")
    d = d.replace("BIGINT", "INTEGER")
    for num in ("14,2", "18,8", "5,4", "8,3", "9,6"):
        d = d.replace(f"NUMERIC({num})", "REAL")
    d = d.replace("TIMESTAMPTZ", "TEXT").replace("BOOLEAN", "INTEGER")
    d = d.replace("CURRENT_TIMESTAMP", "(datetime('now'))")
    d = d.replace("DEFAULT now()", "DEFAULT (datetime('now'))")
    return d


def init_db():
    # Indexes are applied after the reconciliation checks below, not as part of
    # this DDL. `listing_identity` in particular has to come after the duplicate
    # scan: creating it first turns the explicit reconciliation refusal into an
    # opaque UNIQUE violation that names the constraint instead of the data
    # problem.
    render = schema.render_postgres if dialect() == "postgres" else schema.render_sqlite
    ddl = render(include_indexes=False)
    conn = connect()
    try:
        if dialect() == "postgres":
            with conn.cursor() as cur:
                for statement in _split(ddl):
                    cur.execute(statement)
        else:
            for statement in _split(ddl):
                conn.execute(statement)
        conn.commit()
        added, unmigratable = migrate(conn)
        if added:
            log.info("migrated %d new columns: %s", len(added), ", ".join(added))
        if unmigratable:
            # Not fatal, but not silent either. A database that is quietly not
            # the declared shape is how the collector ends up writing columns
            # that do not exist, so this is raised rather than logged and
            # forgotten.
            raise RuntimeError(
                f"schema migration incomplete on {len(unmigratable)} column(s); "
                "this database is NOT at the declared shape and collection will "
                "fail on it:\n  " + "\n  ".join(unmigratable)
            )
        # Legacy databases had subplatform in their identity constraint. Add the
        # stable key only after checking it; merging rows requires an explicit
        # decision about conflicting observations and references.
        duplicate = conn.execute(
            "SELECT platform, listing_id FROM listings GROUP BY platform, listing_id "
            "HAVING COUNT(*) > 1 LIMIT 1").fetchone()
        if duplicate:
            raise RuntimeError("duplicate platform/listing identities: reconcile "
                               "existing rows before applying the identity migration")
        for statement in _split(schema.index_ddl(dialect())):
            conn.execute(statement)
        log.info("schema initialised (%s dialect, %d tables, %d indexes, v%d)",
                 dialect(), len(schema.TABLES), len(schema.INDEXES), schema.SCHEMA_VERSION)
    finally:
        conn.close()
    return ddl


def _split(ddl):
    statements = []
    for chunk in ddl.split(";"):
        lines = []
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            lines.append(line)
        sql = "\n".join(lines).strip()
        if sql:
            statements.append(sql)
    return statements


def query(sql, params=None):
    conn = _borrow()
    try:
        cur = conn.execute(sql, list(params or []))
        rows = cur.fetchall()
    except _CONNECTION_ERRORS:
        # Only the pooled handle is discarded: a read_snapshot() owns its own
        # connection and is responsible for closing it.
        if _READ_SNAPSHOT.get() is None:
            _discard_pooled()
        raise
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(dict(r))
        elif hasattr(r, "keys"):          # sqlite3.Row and RealDictRow
            out.append({k: r[k] for k in r.keys()})
        else:
            out.append(tuple(r))
    return out


def scalar(sql, params=None):
    """First column of the first row, or None."""
    conn = _borrow()
    try:
        cur = conn.execute(sql, list(params or []))
        row = cur.fetchone()
    except _CONNECTION_ERRORS:
        if _READ_SNAPSHOT.get() is None:
            _discard_pooled()
        raise
    if row is None:
        return None
    return row[0]


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    conn = _borrow()
    try:
        cur = conn.execute(sql, params or [])
        conn.commit()
        return cur.lastrowid
    except _CONNECTION_ERRORS:
        if _READ_SNAPSHOT.get() is None:
            _discard_pooled()
        raise


OBSERVATION_COLUMNS = (
    "platform", "subplatform", "listing_id", "title", "seller_type_norm",
    "seller_id", "seller_version_id",
    "category_raw_id", "category_norm", "category_root_norm", "category_label_confidence",
    "brand_norm", "model_norm", "condition_raw", "condition_norm", "condition_label_raw",
    "municipality_norm", "region_norm", "listed_price_original", "currency_original",
    "listed_price_eur", "is_giveaway", "platform_edited_at", "first_seen_at", "last_seen_at",
    "observation_coverage_end", "status_raw", "status_norm", "coverage",
    "outcome_status", "outcome_confidence", "outcome_observed_at",
    "outcome_label_raw", "image_count", "normalization_version", "source_observed_at",
    "last_live_at", "last_probed_at", "is_reserved", "field_sources_json",
    "platform_touched_at", "has_buy_now", "has_shipping",
    "price_observed_at", "platform_published_at",
    "shipping_cost_buyer_eur", "buyer_fee_eur", "total_buyer_cost_eur", "buy_now_price_eur",
    "promotion_active", "promotion_type", "fx_rate_to_eur", "fx_rate_date", "fx_source",
    "field_missing_reasons_json",
)

# Fields refreshed only when the incoming record actually carries a value, so a
# sparse channel (search JSON-LD) never erases what a rich one (item page) found.
# `seller_id` belongs here for the same reason: only the item page carries an
# owner, and a search observation must not blank the linkage back out.
_COALESCE = (
    "title", "seller_type_norm", "seller_id", "category_raw_id", "category_norm",
    "category_root_norm", "category_label_confidence", "brand_norm", "model_norm", "condition_raw",
    "condition_norm", "condition_label_raw", "municipality_norm", "region_norm", "listed_price_original",
    "currency_original", "listed_price_eur", "is_giveaway", "platform_edited_at", "status_raw",
    "image_count", "platform_touched_at", "has_buy_now", "has_shipping", "platform_published_at",
    "shipping_cost_buyer_eur", "buyer_fee_eur", "buy_now_price_eur", "promotion_active", "promotion_type",
    "fx_rate_to_eur", "fx_rate_date", "fx_source",
)
_PRICE_COMPONENTS = ("shipping_cost_buyer_eur", "buyer_fee_eur", "total_buyer_cost_eur",
                     "promotion_active", "fx_rate_to_eur", "fx_rate_date", "fx_source")
_HISTORY_FIELDS = tuple(c for c in _COALESCE if c != "seller_id") + (
    "subplatform",
    "status_norm", "coverage", "outcome_status", "outcome_label_raw", "outcome_confidence",
    "outcome_observed_at", "observation_coverage_end", "is_reserved", "price_observed_at",
    "total_buyer_cost_eur", "field_sources_json", "field_missing_reasons_json")

# A disappearance cannot erase stronger sale evidence.
_OUTCOME_RANK = {"unknown": 0, "active": 0, "withdrawn": 1, "expired": 1,
                 "sold_inferred": 2, "sold_confirmed": 3}
_STATE_COLUMNS = ("status_norm", "coverage", "outcome_status", "outcome_confidence",
                  "outcome_observed_at", "outcome_label_raw", "observation_coverage_end")
_SOURCE_RANK = {"atom": 0, "search_itemlist": 1, "unknown": 2,
                "search_docs": 2, "ebay_browse": 2, "item_page": 3, "own_sale": 4}
_LOOKUP = "SELECT * FROM listings WHERE platform = ? AND listing_id = ?"


def _same_observed_value(field, before, after):
    """Compare DB-native dates/decimals with incoming source values consistently."""
    if before is None or after is None:
        return before is after
    if field.endswith("_at"):
        from .metrics import parse_ts
        return parse_ts(before) == parse_ts(after)
    if field == "fx_rate_date":
        return str(before) == str(after)
    if field.endswith("_eur") or field == "fx_rate_to_eur":
        from decimal import Decimal
        return Decimal(str(before)) == Decimal(str(after))
    return before == after


def resolve_seller(conn, platform, seller_id, observed_at=None):
    """Return the open SCD-2 seller version for a pseudonymous key.

    `seller_id` here is a key from `privacy.seller_key`, never a platform owner
    identifier; this function is the only place seller identity is turned into
    a row, and it has no way to see the raw value.

    A new version is opened only when the previous one is closed. The project
    does not yet collect seller attributes that change over time, so opening a
    version per observation would produce thousands of identical rows with new
    ids and no history worth reading.
    """
    if not seller_id:
        return None
    row = conn.execute(
        "SELECT seller_version_id, valid_to FROM sellers "
        "WHERE platform = ? AND seller_id = ? "
        "ORDER BY valid_from DESC, seller_version_id DESC LIMIT 1",
        (platform, seller_id),
    ).fetchone()
    if row is not None and row["valid_to"] is None:
        return row["seller_version_id"]
    cursor = conn.execute(
        "INSERT INTO sellers (platform, seller_id, valid_from, observed_at) "
        "VALUES (?, ?, ?, ?) RETURNING seller_version_id",
        (platform, seller_id, observed_at, observed_at),
    )
    created = cursor.fetchone()
    return created[0] if created else None


def record_observation(rec, conn=None):
    """Merge an allowlisted observation using stable identity and source quality.

    Sale evidence only advances. A later rich active observation can correct a
    non-sale exit. Sparse channels cannot erase richer fields.
    """
    from .metrics import parse_ts
    import math
    for field in ("listed_price_original", "listed_price_eur", "buy_now_price_eur",
                  "shipping_cost_buyer_eur", "buyer_fee_eur", "fx_rate_to_eur"):
        value = rec.get(field)
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0):
            raise ValueError(f"{field} must be a finite nonnegative amount")
    if rec.get("promotion_active") is not None and not isinstance(rec["promotion_active"], bool):
        raise ValueError("promotion_active must be an observed boolean or unknown")

    def latest(*values):
        valid = [value for value in values if parse_ts(value) is not None]
        return max(valid, key=parse_ts) if valid else None

    owns = conn is None
    conn = conn or connect()
    try:
        with conn.transaction():
            prior_row = conn.execute(_LOOKUP, (rec["platform"], str(rec["listing_id"]))).fetchone()
            prior = dict(prior_row) if prior_row is not None else None
            payload = {c: rec.get(c) for c in OBSERVATION_COLUMNS}
            payload.update(platform=rec["platform"], listing_id=str(rec["listing_id"]),
                           subplatform=rec.get("subplatform") or "standard",
                           first_seen_at=rec["first_seen_at"],
                           last_seen_at=rec.get("last_seen_at") or rec["first_seen_at"])
            payload["source_observed_at"] = payload["last_seen_at"]
            observed = parse_ts(payload["source_observed_at"])
            previous_observed = parse_ts(prior["last_seen_at"] or prior["source_observed_at"]) if prior else None
            older = bool(observed and previous_observed and observed < previous_observed)
            if older:
                payload["source_observed_at"] = prior["source_observed_at"] or prior["last_seen_at"]
            payload["normalization_version"] = rec.get("normalization_version") or "norm-2026-10-01"
            payload["status_norm"] = rec.get("status_norm") or "active"
            payload["coverage"] = rec.get("coverage") or "collecting"
            payload["outcome_status"] = rec.get("outcome_status") or "unknown"
            source = rec.get("_source", "unknown")
            if source not in _SOURCE_RANK:
                raise ValueError("unrecognized observation source")
            sources = json.loads(prior["field_sources_json"] or "{}") if prior else {}
            incoming_outcome = payload["outcome_status"]
            if prior:
                payload["first_seen_at"] = prior["first_seen_at"]
                payload["last_seen_at"] = latest(payload["last_seen_at"], prior["last_seen_at"])
                for field in _COALESCE:
                    incoming = payload[field]
                    old_source = sources.get(field, "unknown")
                    descriptive = (field.startswith("category_") or field.startswith("condition_")
                                   or field in ("brand_norm", "model_norm", *_PRICE_COMPONENTS, "promotion_type", "buy_now_price_eur"))
                    if incoming is None or (older and prior[field] is not None) or (
                            descriptive and prior[field] is not None and _SOURCE_RANK[source] < _SOURCE_RANK.get(old_source, 0)):
                        payload[field] = prior[field]
                    else:
                        sources[field] = source
                # Only the embedded search response explicitly identifies Diili.
                if source != "search_docs" or older:
                    payload["subplatform"] = prior["subplatform"]
                else:
                    sources["subplatform"] = source
                previous_outcome = prior["outcome_status"]
                advances = _OUTCOME_RANK.get(incoming_outcome, 0) > _OUTCOME_RANK.get(previous_outcome, 0)
                reopens = (previous_outcome in ("withdrawn", "expired", "active", "unknown")
                           and source == "item_page" and not older
                           and incoming_outcome in ("unknown", "active")
                           and payload["status_norm"] == "active" and payload["coverage"] == "collecting"
                           and parse_ts(rec.get("last_live_at")) is not None)
                if not advances and previous_outcome != "unknown" and not reopens:
                    for field in _STATE_COLUMNS:
                        payload[field] = prior[field]
                elif incoming_outcome == "unknown" and (source != "item_page" or older):
                    for field in _STATE_COLUMNS:
                        payload[field] = prior[field]
                payload["last_live_at"] = latest(prior["last_live_at"], payload["last_live_at"])
                if previous_outcome != "unknown" and incoming_outcome == "unknown" and not reopens:
                    payload["last_live_at"] = prior["last_live_at"]
                payload["last_probed_at"] = latest(prior["last_probed_at"], payload["last_probed_at"])
                if payload["is_reserved"] is None or older:
                    payload["is_reserved"] = prior["is_reserved"]
            else:
                for field in _COALESCE:
                    if payload[field] is not None:
                        sources[field] = source
                sources["subplatform"] = source
            if payload["outcome_status"] == "unknown" and payload["coverage"] == "gone_uncertain" and prior:
                payload["observation_coverage_end"] = (prior["observation_coverage_end"]
                    or prior["last_live_at"] or prior["last_seen_at"])
            if payload["outcome_status"] not in ("unknown", "active"):
                payload["observation_coverage_end"] = payload["observation_coverage_end"] or payload["outcome_observed_at"] or payload["last_seen_at"]
            if payload["is_giveaway"]:
                payload["listed_price_eur"] = None
            price_supplied = (rec.get("listed_price_original") is not None
                              and (not older or prior["listed_price_original"] is None))
            payload["price_observed_at"] = (rec.get("price_observed_at") or rec.get("last_seen_at") or rec["first_seen_at"]
                                             if price_supplied else prior["price_observed_at"] if prior else None)
            payload["field_sources_json"] = json.dumps(sources, sort_keys=True)
            reasons = json.loads(prior["field_missing_reasons_json"] or "{}") if prior else {}
            incoming_reasons = json.loads(rec.get("field_missing_reasons_json") or "{}")
            if not isinstance(incoming_reasons, dict) or any(
                    key not in OBSERVATION_COLUMNS or value not in schema.ENUM_MISSING_REASON
                    for key, value in incoming_reasons.items()):
                raise ValueError("missing reasons must name supported fields and reasons")
            for field, reason in incoming_reasons.items():
                if payload.get(field) is None:
                    reasons[field] = reason
            for field in tuple(reasons):
                if payload.get(field) is not None:
                    reasons.pop(field)
            payload["field_missing_reasons_json"] = json.dumps(reasons, sort_keys=True) if reasons else None
            components = [payload.get(field) for field in ("listed_price_eur", "shipping_cost_buyer_eur", "buyer_fee_eur")]
            from decimal import Decimal
            payload["total_buyer_cost_eur"] = (float(sum(Decimal(str(value)) for value in components))
                                                if all(value is not None for value in components) else None)
            # Resolved after the merge, because `seller_id` only survives a
            # sparse observation by coalescing with the prior value. The key is
            # already pseudonymous by the time it gets here; with it disabled
            # this still links rows that were collected under a salt, so a
            # switch-off stops new collection without orphaning old series.
            payload["seller_version_id"] = resolve_seller(
                conn, payload["platform"], payload["seller_id"],
                payload["source_observed_at"])
            if conn.dialect == "postgres":
                for field in ("is_giveaway", "is_reserved", "has_buy_now", "has_shipping", "promotion_active"):
                    if payload[field] is not None:
                        payload[field] = bool(payload[field])
            cols = ", ".join(OBSERVATION_COLUMNS)
            marks = ", ".join("?" for _ in OBSERVATION_COLUMNS)
            updates = ", ".join(f"{c}=excluded.{c}" for c in OBSERVATION_COLUMNS
                                if c not in ("platform", "listing_id", "first_seen_at"))
            conn.execute(f"INSERT INTO listings ({cols}) VALUES ({marks}) "
                         f"ON CONFLICT (platform, listing_id) DO UPDATE SET {updates}",
                         [payload[c] for c in OBSERVATION_COLUMNS])
            row = conn.execute(_LOOKUP, (payload["platform"], payload["listing_id"])).fetchone()
            original_stamp = rec.get("source_observed_at") or rec.get("last_seen_at") or rec["first_seen_at"]
            observed_fields = {c: rec[c] for c in _HISTORY_FIELDS if c in rec and rec[c] is not None}
            if rec.get("_listing_analysis_input") is not None:
                from . import listing_analysis
                capsule = rec["_listing_analysis_input"]
                observed_fields["listing_analysis_input"] = dict(version=listing_analysis.VERSION,
                    content_sha256=listing_analysis._hash(capsule),warnings=capsule["warnings"])
            attribute_change = None
            if rec.get("_product_attributes") is not None:
                from . import product_attributes
                if source not in product_attributes.SOURCES:
                    raise ValueError("product assertions require a listing response source")
                observed_fields[product_attributes.SNAPSHOT_KEY] = product_attributes.snapshot(rec["_product_attributes"], original_stamp)
                previous_snapshot = conn.execute("SELECT observed_at,fields_json FROM listing_observations WHERE listing_pk=? "
                    "AND source=? AND fields_json LIKE '%\"product_attributes\":%' ORDER BY "+
                    ("observed_at" if conn.dialect == "postgres" else "julianday(observed_at)")+" DESC,observation_pk DESC LIMIT 1",
                    (row["listing_pk"],source)).fetchone()
                before_attributes = (json.loads(previous_snapshot["fields_json"])[product_attributes.SNAPSHOT_KEY]["assertions"]
                                     if previous_snapshot else [])
                before_hash = product_attributes.facts_digest(before_attributes)
                after_hash = product_attributes.facts_digest(observed_fields[product_attributes.SNAPSHOT_KEY]["assertions"])
                if previous_snapshot and parse_ts(previous_snapshot["observed_at"]) == parse_ts(original_stamp) and before_hash != after_hash:
                    raise ValueError("different product claims cannot reuse an observation timestamp")
                if not older and before_hash != after_hash:
                    attribute_change = {"before":before_hash,"after":after_hash}
            conn.execute("INSERT INTO listing_observations "
                         "(listing_pk,observed_at,source,normalization_version,state_at,fields_json,state_json) "
                         "VALUES (?,?,?,?,?,?,?) ON CONFLICT (listing_pk,observed_at,source) DO NOTHING",
                         (row["listing_pk"], original_stamp, source, payload["normalization_version"], payload["last_seen_at"],
                          json.dumps(observed_fields, sort_keys=True, default=str),
                          json.dumps({c:payload[c] for c in _HISTORY_FIELDS}, sort_keys=True, default=str)))
            if prior:
                changed_fields = {c: {"before":prior[c], "after":payload[c]} for c in _COALESCE
                                  if c not in ("seller_id", "listed_price_eur", "listed_price_original")
                                  and not _same_observed_value(c, prior[c], payload[c])}
                if attribute_change:
                    changed_fields["product_attribute_claims_sha256"] = attribute_change
                if changed_fields:
                    emit_event(conn, row["listing_pk"], "attribute_changed", original_stamp, original_stamp,
                               payload={"source":source, "changes":changed_fields})
            # Store measurements from the incoming record, never carried-forward
            # values from the merged row. Late measurements remain historically
            # useful without replacing current state.
            if rec.get("listed_price_original") is not None:
                measured = rec.get("price_observed_at") or rec.get("last_seen_at") or rec["first_seen_at"]
                recorded = rec.get("source_observed_at") or rec.get("last_seen_at") or rec["first_seen_at"]
                conn.execute("INSERT INTO price_observations "
                             "(listing_pk,observed_at,price_observed_at,source,listed_price_eur,listed_price_original,currency_original) "
                             "VALUES (?,?,?,?,?,?,?) ON CONFLICT (listing_pk,observed_at,source) DO NOTHING",
                             (row["listing_pk"], recorded, measured, source, rec.get("listed_price_eur"),
                              rec["listed_price_original"], rec.get("currency_original")))
            classification_fields = ("title", "category_norm", "category_raw_id", "category_root_norm", "municipality_norm")
            if prior is None or any(prior.get(field) != payload.get(field) for field in classification_fields):
                conn.execute("INSERT INTO classification_changes (listing_pk,changed_at) VALUES (?,?) "
                             "ON CONFLICT(listing_pk) DO UPDATE SET changed_at=excluded.changed_at",
                             (row["listing_pk"], payload["source_observed_at"]))
            old_price = prior["listed_price_eur"] if prior else None
            new_price = payload["listed_price_eur"]
            changed = old_price is not None and new_price is not None and abs(float(old_price)-float(new_price)) > 0.005
            open_interval = conn.execute("SELECT 1 FROM price_history WHERE listing_pk=? AND valid_to IS NULL",
                                         (row["listing_pk"],)).fetchone()
            components_changed = bool(prior and not older and any(
                not _same_observed_value(c, prior[c], payload[c]) for c in _PRICE_COMPONENTS))
            if changed or components_changed or (old_price is not None and new_price is None and payload["is_giveaway"]):
                close_open_price(conn, row["listing_pk"], payload["source_observed_at"])
                open_interval = None
                if changed or (old_price is not None and new_price is None and payload["is_giveaway"]):
                    emit_event(conn, row["listing_pk"], "price_changed", payload["source_observed_at"],
                               payload["source_observed_at"], before=str(old_price),
                               after=str(new_price) if new_price is not None else "unavailable")
            if new_price is not None and not open_interval and payload["listed_price_original"] is not None:
                open_price(conn, row["listing_pk"], payload["source_observed_at"] if components_changed else
                           payload["price_observed_at"] or payload["source_observed_at"],
                           new_price, payload["listed_price_original"], payload["currency_original"],
                           payload["source_observed_at"], components=payload)
            if prior is None:
                # Preserve only the covariates actually available at entry. A
                # later detail fetch must not become a historical model feature.
                from .predict._common import capture_entry_features
                capture_entry_features(conn, row["listing_pk"], payload)
                from . import observability
                observability.record_rows(payload["platform"])
                emit_event(conn, row["listing_pk"], "first_seen", payload["first_seen_at"], payload["source_observed_at"])
            from . import collection_events
            from . import listing_analysis
            conn.execute("SAVEPOINT listing_analysis_enqueue")
            try:
                listing_analysis.capture(conn, dict(row), rec, original_stamp, source)
            except Exception as exc:
                conn.execute("ROLLBACK TO SAVEPOINT listing_analysis_enqueue")
                log.warning("Listing analysis enqueue deferred: %s", type(exc).__name__)
            finally:
                conn.execute("RELEASE SAVEPOINT listing_analysis_enqueue")
            kind = 'new' if prior is None else 'changed' if changed or changed_fields else 'reobserved'
            collection_events.attribute(conn, row['listing_pk'], rec, kind=kind,
                                        commit_key=str(original_stamp)+':'+source)
            return row["listing_pk"], prior is None
    finally:
        if owns:
            conn.close()


def emit_event(conn, listing_pk, event_type, event_at, observed_at, before=None,
               after=None, payload=None, confidence=None):
    """Append an event. Idempotent on (listing_pk, event_type, event_at).

    Observation is discrete: a listing seen unchanged at 09:00 and 10:00 has two
    observations but one state transition. The uniqueness constraint is what
    stops a repeated scrape from inflating the event log and every rate derived
    from it.
    """
    import json
    payload_json = json.dumps(payload, sort_keys=True, default=str) if payload is not None else None
    cursor = conn.execute(
        """INSERT INTO events (listing_pk, event_type, event_at, observed_at,
               before_value, after_value, payload_json, event_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (listing_pk, event_type, event_at) DO NOTHING""",
        (listing_pk, event_type, event_at, observed_at, before, after,
         payload_json, confidence),
    )
    from . import observability
    if cursor.rowcount:
        observability.record_events(event_type)


def listing_state_asof(platform, listing_id, asof):
    """Return a retained accepted version, never today's row as a past state.

    Legacy listings without observation versions remain unavailable. Late source
    observations carry the later accepted-state clock and cannot leak backwards.
    """
    from .metrics import parse_ts
    cutoff = parse_ts(asof)
    if cutoff is None:
        raise ValueError("asof must be a valid timestamp")
    rows = query("SELECT o.* FROM listing_observations o JOIN listings l ON l.listing_pk=o.listing_pk "
                 "WHERE l.platform=? AND l.listing_id=? ORDER BY o.observation_pk", (platform, str(listing_id)))
    eligible = [row for row in rows if parse_ts(row["state_at"]) and parse_ts(row["state_at"]) <= cutoff
                and parse_ts(row["observed_at"]) and parse_ts(row["observed_at"]) <= cutoff]
    if not eligible:
        return None
    version = max(eligible, key=lambda row:(parse_ts(row["state_at"]), row["observation_pk"]))
    return {"state":json.loads(version["state_json"]), "source":version["source"],
            "observed_at":version["observed_at"], "state_at":version["state_at"],
            "normalization_version":version["normalization_version"]}


def repair_disappearance_outcomes(apply=False, now=None):
    """Audit legacy unlabeled expiry guesses; preview unless apply is explicit.

    Original transitions remain in the log. Source-labeled exits, sales and rows
    without the old low-confidence transition are never silently reclassified.
    """
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    conn = connect()
    try:
        with conn.transaction():
            rows = conn.execute("SELECT l.* FROM listings l WHERE l.outcome_status='expired' "
                "AND l.coverage='gone_uncertain' AND l.outcome_confidence<=0.5 "
                "AND COALESCE(l.outcome_label_raw,'')='' AND EXISTS "
                "(SELECT 1 FROM events e WHERE e.listing_pk=l.listing_pk AND e.event_type='status_changed' "
                "AND e.after_value='expired' AND e.event_confidence<=0.5 AND e.event_at=l.outcome_observed_at)").fetchall()
            for row in rows if apply else ():
                censor = row["last_live_at"] or row["first_seen_at"]
                emit_event(conn, row["listing_pk"], "status_changed", stamp, stamp,
                    before="expired", after="unknown", payload={"correction_version":"unlabeled-expiry-v1",
                    "reason":"No explicit terminal cause was retained",
                    "prior_outcome_observed_at":str(row["outcome_observed_at"]),
                    "prior_outcome_confidence":float(row["outcome_confidence"]),
                    "observation_coverage_end":str(censor)})
                conn.execute("UPDATE listings SET outcome_status='unknown', outcome_confidence=NULL, "
                    "outcome_observed_at=NULL, observation_coverage_end=? WHERE listing_pk=?", (censor, row["listing_pk"]))
            return {"correction_version":"unlabeled-expiry-v1", "eligible":len(rows),
                    "applied":len(rows) if apply else 0, "preview":not apply,
                    "followup":"Corrected unknown outcomes can be re-admitted through normal bounded follow-up",
                    "derived_outputs":"Regenerate reports and retrain current methods; historical snapshots remain unchanged"}
    finally:
        conn.close()


def close_open_price(conn, listing_pk, valid_to):
    conn.execute(
        "UPDATE price_history SET valid_to = ? WHERE listing_pk = ? AND valid_to IS NULL",
        (valid_to, listing_pk),
    )


def open_price(conn, listing_pk, valid_from, price_eur, price_original,
               currency, observed_at, components=None):
    components = components or {}
    conn.execute(
        """INSERT INTO price_history (listing_pk, valid_from, valid_to,
               currency_original, listed_price_original, listed_price_eur, observed_at,
               shipping_cost_buyer_eur,buyer_fee_eur,total_buyer_cost_eur,promotion_active,fx_rate_to_eur,fx_rate_date,fx_source,
               price_observed_at)
           VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (listing_pk, valid_from, currency, price_original, price_eur, observed_at,
         *[components.get(c) for c in _PRICE_COMPONENTS], components.get("price_observed_at") or valid_from),
    )


def purge_expired_listings(conn, cutoff, preserve_history=True):
    """Delete expired market rows inside the caller's transaction.

    Also used on isolated recovery copies; never relies on the live DB path.
    Owned purchase/sale links retain their existing exemption.
    """
    if preserve_history and existing_columns(conn, "aggregate_partitions"):
        from datetime import timedelta
        from . import aggregates, metrics
        # A failure rolls back the caller's deletion. Recovery copies never
        # invent snapshots for days after the original collection occurred.
        aggregates.materialize(conn, metrics.parse_ts(cutoff) + timedelta(days=config.LISTING_RETENTION_DAYS))
    ids = [row[0] for row in conn.execute(
        "SELECT l.listing_pk FROM listings l WHERE l.last_seen_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM purchases p WHERE p.sold_listing_pk=l.listing_pk "
        "OR (p.platform=l.platform AND p.source_listing_id=l.listing_id)) "
        "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.listing_pk=l.listing_pk)",
        (cutoff,)).fetchall()]
    children = ["events", "price_history", "price_observations", "listing_observations", "media_hashes", "enrichment"]
    children += [table for table in ("sale_predictions", "price_trajectories", "predictive_feature_snapshots")
                 if existing_columns(conn, table)]
    for pk in ids:
        for table in children:
            conn.execute(f"DELETE FROM {table} WHERE listing_pk=?", (pk,))
        conn.execute("DELETE FROM listings WHERE listing_pk=?", (pk,))
    return len(ids)


def acquire_lock(name, ttl_seconds=3600):
    """Kernel-owned lock, released automatically if the process dies."""
    from . import local_runtime
    local_runtime.assert_database_owner()
    import fcntl
    import os

    lock_dir = config.DATA_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    try:
        handle = path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None, f"{name} already running"
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle, None
    except OSError as exc:
        return None, f"cannot take {name} lock: {exc}"


def release_lock(handle):
    if handle is None:
        return
    handle.close()


def active_locks():
    """Lock files remain on disk; only a held kernel lock represents a run."""
    from . import local_runtime
    if local_runtime.owner() and not local_runtime.in_container():
        return local_runtime.active_locks()
    import fcntl
    held = []
    for path in (config.DATA_DIR / "locks").glob("*.lock"):
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(path.stem)
    return sorted(held)
