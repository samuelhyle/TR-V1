"""Durable bounded collection telemetry, independent of browser connections."""

import csv
import io
import json
import logging
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone

from . import config, db

_CONTEXT = ContextVar('collection_event_context', default=(None, None))
FIELDS = frozenset(('stage', 'status', 'reason', 'error_class', 'revision', 'source_rps', 'item_rps',
                    'host', 'http_status', 'bytes', 'duration_ms', 'attempt', 'wait_seconds',
                    'requests', 'observations', 'new', 'changed', 'classified', 'cached', 'seconds',
                    'exit_code', 'logger', 'message', 'counts'))
SNAPSHOT_FIELDS = ('platform', 'listing_id', 'title', 'category_norm', 'condition_norm',
                   'municipality_norm', 'listed_price_eur', 'status_norm', 'outcome_status',
                   'source_observed_at')


def now():
    return datetime.now(timezone.utc).isoformat()


def available():
    if not config.POSTGRES_DSN and not config.SQLITE_PATH.is_file():
        return False
    conn = db.connect()
    try:
        sql = ("SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='collection_events'"
               if config.POSTGRES_DSN else "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_events'")
        return conn.execute(sql).fetchone() is not None
    finally:
        conn.close()


def context():
    run, job = _CONTEXT.get()
    return run or os.environ.get('TORI_COLLECTION_RUN_ID'), job or os.environ.get('TORI_COLLECTION_JOB_ID')


def bind(run_id, job_id=None):
    return _CONTEXT.set((run_id, job_id))


def reset(token):
    _CONTEXT.reset(token)


def emit(event_type, payload=None, *, severity='info', conn=None, run_id=None, job_id=None):
    run, job = context()
    run, job = run_id or run, job_id or job
    if not run:
        return
    safe = {k: v for k, v in (payload or {}).items() if k in FIELDS}
    owned = conn is None
    conn = conn or db.connect()
    try:
        if job and 'stage' not in safe:
            stage = conn.execute('SELECT job FROM collection_job_runs WHERE job_run_id=?', (job,)).fetchone()
            if stage:
                safe['stage'] = stage[0]
        conn.execute('INSERT INTO collection_events (run_id,job_run_id,at,severity,event_type,payload_json) VALUES (?,?,?,?,?,?)',
                     (run, job, now(), severity, event_type, json.dumps(safe, allow_nan=False, default=str)))
    finally:
        if owned:
            conn.close()


def attribute(conn, listing_pk, rec, *, kind, commit_key):
    """Called inside the observation/result transaction; rollback covers attribution."""
    run, job = context()
    if not run or not job:
        return
    snapshot = {k: rec.get(k) for k in SNAPSHOT_FIELDS if k in rec}
    if kind == 'classification':
        snapshot.update({k: rec.get(k) for k in ('product_type', 'confidence', 'validation', 'input_sha256', 'cached')})
    stamp = rec.get('source_observed_at') or rec.get('observed_at') or now()
    result = conn.execute('INSERT INTO collection_run_observations '
                          '(run_id,job_run_id,listing_pk,commit_key,observed_at,kind,snapshot_json) '
                          'VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_run_id,listing_pk,commit_key) DO NOTHING',
                          (run, job, listing_pk, commit_key, stamp, kind, json.dumps(snapshot, default=str, allow_nan=False)))
    if result.rowcount:
        # One aggregate update, no identity or source text in operational events.
        row = conn.execute('SELECT result_json FROM collection_job_runs WHERE job_run_id=?', (job,)).fetchone()
        data = json.loads(row[0]) if row else {}
        progress = data.setdefault('progress', {})
        counter = 'classified' if kind == 'classification' else 'observations'
        progress[counter] = progress.get(counter, 0) + 1
        if kind in ('new', 'changed'):
            progress[kind] = progress.get(kind, 0) + 1
        data['progress_at'] = now()
        conn.execute('UPDATE collection_job_runs SET result_json=? WHERE job_run_id=?', (json.dumps(data), job))


def checkpoint(stage, counts):
    from .collection_health import sanitize_stats
    emit('source_unit_committed', {'stage': stage, 'counts': sanitize_stats(counts)})


def bounded_int(value, default=100, maximum=200):
    try:
        result = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise ValueError('invalid pagination value') from None
    if result < 0 or result > maximum:
        raise ValueError('pagination value is out of range')
    return result


def read(run_id, after=0, limit=100, severity=None, stage=None, event_type=None):
    after = bounded_int(after, 0, 2**63-1)
    limit = bounded_int(limit, 100)
    if limit == 0:
        raise ValueError('limit must be positive')
    clauses, values = ['run_id=?', 'event_pk>?'], [run_id, after]
    for column, value in (('severity', severity), ('event_type', event_type)):
        if value:
            clauses.append(column+'=?')
            values.append(value)
    if stage:
        clauses.append("json_extract(payload_json,'$.stage')=?")
        values.append(stage)
    rows = db.query('SELECT * FROM collection_events WHERE '+ ' AND '.join(clauses)+' ORDER BY event_pk LIMIT ?', values+[limit])
    for row in rows:
        row['id'] = row['event_pk']
        row['payload'] = json.loads(row.pop('payload_json'))
    earliest = db.scalar('SELECT MIN(event_pk) FROM collection_events WHERE run_id=?', (run_id,))
    # Global event IDs need not be contiguous. Only retention within this run matters.
    return {'events': rows, 'next_cursor': rows[-1]['id'] if rows else after,
            'earliest_cursor': earliest, 'has_more': len(rows) == limit}


def data(run_id, after=0, limit=50, kind=None, direction="asc"):
    after = bounded_int(after, 0, 2**63-1)
    limit = bounded_int(limit, 50)
    if not limit:
        raise ValueError('limit must be positive')
    if direction not in ('asc', 'desc'):
        raise ValueError('unsupported data direction')
    clauses, args = ['run_id=?'], [run_id]
    if after or direction == 'asc':
        clauses.append('attribution_pk'+('>' if direction == 'asc' else '<')+'?')
        args.append(after)
    if kind:
        if kind not in ('new', 'reobserved', 'changed', 'classification'):
            raise ValueError('unsupported data kind')
        clauses.append('kind=?')
        args.append(kind)
    rows = db.query('SELECT * FROM collection_run_observations WHERE '+' AND '.join(clauses)+' ORDER BY attribution_pk '+direction+' LIMIT ?', args+[limit])
    for row in rows:
        row['snapshot'] = json.loads(row.pop('snapshot_json'))
    return {'rows': rows, 'next_cursor': rows[-1]['attribution_pk'] if rows else after,
            'has_more': len(rows) == limit, 'basis': 'Immutable normalized evidence observed during this run.'}


def export(run_id, resource, after=0):
    """Bounded export pages keep database reads and response memory predictable."""
    if resource not in ('events', 'data'):
        raise ValueError('export resource must be events or data')
    report = read(run_id, after, 200) if resource == 'events' else data(run_id, after, 200)
    columns = ['run_id', 'id', 'at', 'severity', 'event_type', 'payload'] if resource == 'events' else [
        'run_id', 'attribution_pk', 'job_run_id', 'listing_pk', 'observed_at', 'kind', 'snapshot']
    run = db.query_one('SELECT origin,started_at,record_json FROM collection_cycles WHERE run_id=?', (run_id,))
    if not run:
        raise ValueError('run not found')
    record = json.loads(run['record_json'])
    metadata = {'run_origin': run['origin'], 'run_started_at': str(run['started_at']),
                'settings': record.get('settings'), 'scope': record.get('context', {}).get('scope'),
                'exported_at': now(), 'basis': 'Retained normalized evidence; classifications are derived.'}
    columns += list(metadata)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in report.get('events', report.get('rows', [])):
        row = {**row, **metadata}
        cells = []
        for key in columns:
            value = row.get(key, '')
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                value = "'"+value
            cells.append(value)
        writer.writerow(cells)
    return output.getvalue(), report['next_cursor'], report['has_more']


class JournalHandler(logging.Handler):
    """Retain fixed log templates; arbitrary exception/source substitutions are redacted."""
    def emit(self, record):
        if not context()[0] or not record.name.startswith('toriresearch'):
            return
        try:
            # Logs use repository-owned templates. Dynamic substitutions can carry
            # source bodies or credentials, so only finite numeric arguments survive.
            message = str(record.msg)
            if record.args:
                message = re.sub(r'%[-+0-9.#]*[sdrf]', '[detail]', message)
            else:
                message = 'Operational log; see stage result and request events.'
            emit('log', {'logger': record.name[:100], 'message': message[:400]},
                 severity='error' if record.levelno >= logging.ERROR else 'warning' if record.levelno >= logging.WARNING else 'info')
        except Exception:
            # Telemetry failure must not recursively log itself.
            pass


def install_logging():
    root = logging.getLogger('toriresearch')
    root.setLevel(logging.INFO)
    if not any(isinstance(h, JournalHandler) for h in root.handlers):
        root.addHandler(JournalHandler())
