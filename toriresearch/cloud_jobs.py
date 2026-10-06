"""PostgreSQL-backed worker queue, also testable against an isolated SQLite DB.

Jobs are claimed once. A lost worker marks a job interrupted instead of replaying
an operation whose effects may already have committed.
"""
import base64
import hashlib
import json
import secrets
from datetime import timedelta

from . import auth, config, db


def enqueue(kind, payload, actor, job_id=None):
    if kind not in {'model', 'collection', 'operation', 'analysis'} or not isinstance(payload, dict):
        raise ValueError('Unsupported worker request.')
    conn = db.connect()
    job_id = job_id or secrets.token_hex(16)
    try:
        with conn.transaction():
            # Bound simultaneous requests even across independent web processes.
            conn.execute("UPDATE auth_users SET updated_at=updated_at WHERE user_id=?", (actor,))
            category = "kind='analysis'" if kind == 'analysis' else "kind!='analysis'"
            busy = conn.execute("SELECT COUNT(*) AS n FROM cloud_jobs WHERE actor_id=? AND status IN ('queued','running','cancelling') AND " + category, (actor,)).fetchone()['n']
            existing = conn.execute('SELECT * FROM cloud_jobs WHERE job_id=?', (job_id,)).fetchone()
            if existing:
                if existing['kind'] != kind or json.loads(existing['payload_json']) != payload:
                    raise ValueError('This job identifier was already used for another request.')
                return public(existing)
            if busy >= (6 if kind == 'analysis' else 2):
                raise auth.Denied('Wait for your current worker requests to finish.', 429)
            if kind == 'analysis':
                conn.execute("DELETE FROM cloud_jobs WHERE kind='analysis' AND status IN ('done','failed','interrupted','cancelled') AND finished_at<?",
                             ((auth.now()-timedelta(hours=1)).isoformat(),))
            conn.execute('INSERT INTO cloud_jobs (job_id,actor_id,kind,status,payload_json,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING',
                         (job_id, actor, kind, 'queued', json.dumps(payload, allow_nan=False), auth.now().isoformat()))
            retained = conn.execute('SELECT * FROM cloud_jobs WHERE job_id=?', (job_id,)).fetchone()
            if retained['kind'] != kind or json.loads(retained['payload_json']) != payload:
                raise ValueError('This job identifier was already used for another request.')
        return {'job_id': job_id, 'status': 'queued', **{k: payload[k] for k in ('family', 'action') if k in payload}}
    finally:
        conn.close()


def public(row):
    payload = json.loads(row['payload_json'])
    result = json.loads(row['result_json']) if row['result_json'] else None
    native = result if row['kind'] == 'model' and isinstance(result, dict) else {}
    return dict(job_id=row['job_id'], status=row['status'], created_at=str(row['created_at']),
                started_at=str(row['started_at']) if row['started_at'] else None,
                finished_at=str(row['finished_at']) if row['finished_at'] else None, result=native.get('result', result),
                error=row['error'], can_cancel=row['kind'] == 'model' and row['status'] in {'queued', 'running'},
                **{k: payload[k] for k in ('family', 'action', 'scope') if k in payload},
                **({key: native[key] for key in ('target_count', 'output', 'warnings') if key in native}),
                native_job_id=native.get('job_id'))


def cancel(job_id, actor=None):
    conn = db.connect()
    try:
        with conn.transaction():
            row = conn.execute('SELECT * FROM cloud_jobs WHERE job_id=?', (job_id,)).fetchone()
            if not row or (actor is not None and row['actor_id'] != actor):
                raise ValueError('This job is unavailable.')
            if row['kind'] != 'model' or row['status'] not in {'queued', 'running'}:
                raise ValueError('This job cannot be cancelled.')
            status = 'cancelled' if row['status'] == 'queued' else 'cancelling'
            conn.execute('UPDATE cloud_jobs SET status=?,finished_at=? WHERE job_id=? AND status=?',
                         (status, auth.now().isoformat() if status == 'cancelled' else None, job_id, row['status']))
        return {'ok': True, 'job_id': job_id}
    finally:
        conn.close()


def jobs(actor=None, kind=None):
    clauses, params = [], []
    if actor:
        clauses.append('actor_id=?'); params.append(actor)
    if kind:
        clauses.append('kind=?'); params.append(kind)
    return [public(row) for row in db.query('SELECT * FROM cloud_jobs' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') +
                                           ' ORDER BY created_at DESC LIMIT 12', params)]


def claim(worker, analysis_only=False):
    conn = db.connect()
    try:
        with conn.transaction():
            stamp = auth.now()
            conn.execute("UPDATE cloud_jobs SET status='interrupted',error=?,finished_at=? WHERE status IN ('running','cancelling') AND heartbeat_at<?",
                         ('Worker stopped. Review the recorded result before explicitly retrying.', stamp.isoformat(), (stamp-timedelta(minutes=5)).isoformat()))
            lock = ' FOR UPDATE SKIP LOCKED' if conn.dialect == 'postgres' else ''
            allowed = " AND kind='analysis'" if analysis_only else ''
            row = conn.execute("SELECT * FROM cloud_jobs WHERE status='queued'" + allowed + " ORDER BY created_at LIMIT 1" + lock).fetchone()
            if not row:
                return None
            changed = conn.execute("UPDATE cloud_jobs SET status='running',started_at=?,heartbeat_at=?,worker_id=? WHERE job_id=? AND status='queued'",
                                   (stamp.isoformat(), stamp.isoformat(), worker, row['job_id']))
            if not changed.rowcount:
                return None
            return dict(row)
    finally:
        conn.close()


def heartbeat(job_id, worker):
    db.execute("UPDATE cloud_jobs SET heartbeat_at=? WHERE job_id=? AND worker_id=? AND status IN ('running','cancelling')",
               (auth.now().isoformat(), job_id, worker))


def finish(job_id, worker, status, result=None, error=None):
    if status not in {'done', 'experimental', 'failed', 'interrupted', 'cancelled'}:
        raise ValueError('Invalid job outcome.')
    return db.execute("UPDATE cloud_jobs SET status=?,result_json=?,error=?,finished_at=? WHERE job_id=? AND worker_id=? AND status IN ('running','cancelling')",
                      (status, json.dumps(result, default=str, allow_nan=False) if result is not None else None,
                       error, auth.now().isoformat(), job_id, worker))


def sync_models(upload=False):
    directory = config.DATA_DIR / 'models'
    directory.mkdir(parents=True, exist_ok=True)
    if upload:
        for path in directory.iterdir():
            if not path.is_file() or path.is_symlink() or path.suffix not in {'.json', '.pkl', '.pickle', '.joblib'}:
                continue
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Model artifact exceeds the cloud storage limit.')
            content = path.read_bytes(); digest = hashlib.sha256(content).hexdigest()
            db.execute('INSERT INTO cloud_artifacts (artifact_key,content_base64,sha256,updated_at) VALUES (?,?,?,?) '
                       'ON CONFLICT(artifact_key) DO UPDATE SET content_base64=excluded.content_base64,sha256=excluded.sha256,updated_at=excluded.updated_at',
                       (path.name, base64.b64encode(content).decode(), digest, auth.now().isoformat()))
    else:
        for row in db.query('SELECT * FROM cloud_artifacts'):
            name = row['artifact_key']
            if not name or name != __import__('pathlib').Path(name).name or '\\' in name or name.startswith('.'):
                raise ValueError('Invalid model artifact name.')
            path = directory / name
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']:
                continue
            content = base64.b64decode(row['content_base64'], validate=True)
            if len(content)>32*1024*1024 or hashlib.sha256(content).hexdigest()!=row['sha256']:
                raise ValueError('Invalid model artifact checksum.')
            from .fileio import write_bytes
            write_bytes(path, content)
