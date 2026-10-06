"""Durable hourly ownership, bounded job evidence and collection policy previews."""

import json
import math
import uuid
import os
import threading
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import collection_health, config, db
from .metrics import parse_ts

VERSION = 'continuous-jobs-v1'
SOURCE_JOBS = frozenset(('atom', 'sweep', 'itempage'))


class Heartbeat:
    """Short journal writes keep liveness visible while source/model jobs wait."""

    def __init__(self, run_id):
        self.run_id = run_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='collection-heartbeat', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(30):
            try:
                db.execute("UPDATE collection_cycles SET heartbeat_at=? WHERE run_id=? AND status='running'",
                           (datetime.now(timezone.utc).isoformat(),self.run_id))
            except Exception as exc:
                logging.getLogger(__name__).warning('Heartbeat write failed: %s',type(exc).__name__)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1)


class CollectorBusy(RuntimeError):
    pass


@contextmanager
def source_owner():
    """Manual source commands serialize with complete cycles in the same runtime."""
    run_id = os.environ.get('TORI_COLLECTION_RUN_ID')
    name = 'collection'
    if run_id:
        if not available():
            raise CollectorBusy('Collection owner is unavailable')
        row = db.query_one("SELECT run_id FROM collection_cycles WHERE run_id=? AND status='running'", (run_id,))
        if not row or 'collection' not in db.active_locks():
            raise CollectorBusy('Collection owner is no longer active')
        name = 'collection-source'
    lock, error = db.acquire_lock(name)
    if lock is None:
        raise CollectorBusy(error)
    try:
        if run_id:
            row = db.query_one("SELECT run_id FROM collection_cycles WHERE run_id=? AND status='running'", (run_id,))
            if not row or 'collection' not in db.active_locks():
                raise CollectorBusy('Collection owner is no longer active')
        yield
    finally:
        db.release_lock(lock)


def slot(reference):
    seconds = config.COLLECTION_INTERVAL_SECONDS
    return datetime.fromtimestamp(math.floor(reference.timestamp()/seconds)*seconds, timezone.utc).isoformat()


def available():
    if not config.POSTGRES_DSN and not config.SQLITE_PATH.is_file():
        return False
    conn = db.connect()
    try:
        query = ("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='collection_cycles'"
                 if config.POSTGRES_DSN else "SELECT name FROM sqlite_master WHERE type='table' AND name='collection_cycles'")
        return conn.execute(query).fetchone() is not None
    finally:
        conn.close()


def plan(reference=None):
    reference = reference or datetime.now(timezone.utc)
    runtime = config.COLLECTION_RUNTIME_SECONDS
    report = config.COLLECTION_REPORT_SECONDS
    reserve = config.ENRICH_RUNTIME_SECONDS + 15 + 3*report + 30
    return {'version': VERSION, 'schedule_slot': slot(reference),
            'interval_seconds': config.COLLECTION_INTERVAL_SECONDS,
            'runtime_seconds': runtime, 'source_and_maintenance_seconds': max(0, runtime-reserve),
            'classification_seconds': config.ENRICH_RUNTIME_SECONDS,
            'report_seconds': report, 'shutdown_seconds': 30,
            'jobs': [{'job': 'atom', 'cadence': 'each_cycle'},
                     {'job': 'sweep', 'cadence': 'each_cycle', 'requests': config.SWEEP_BUDGET, 'pages': config.SWEEP_PAGES},
                     {'job': 'itempage', 'cadence': 'each_cycle', 'attempts': config.ITEM_BUDGET,
                      'repeat_interval_hours': config.PROBE_INTERVAL_HOURS},
                     {'job': 'enrich', 'cadence': 'each_cycle', 'candidates': config.ENRICH_BUDGET},
                     {'job': 'repair', 'cadence': 'daily_or_identity_change',
                      'batch_size': config.ENRICH_REPAIR_BATCH, 'batches_per_cycle': config.ENRICH_REPAIR_BATCHES}],
            'observer_seconds': config.COLLECTION_OBSERVER_SECONDS,
            'run_retention_days': config.COLLECTION_RUN_RETENTION_DAYS,
            'source_limits_changed': False, 'context': collection_health.context()}


def latest_start():
    if not available():
        return None
    return parse_ts(db.scalar('SELECT MAX(started_at) FROM collection_cycles WHERE collector_key=?',
                              (collection_health.context()['source_key'],)))


def _record(state):
    """Only bounded orchestration facts are permitted into the journal."""
    allowed = ('run_id', 'started_at', 'finished_at', 'status', 'exit_code', 'run_origin', 'measurement_version',
               'context', 'schedule_slot', 'platform_pause', 'followup', 'deadline_at', 'current_stage',
               'settings', 'settings_revision', 'command_id', 'terminal_reason', 'mixed_pace')
    result = {key: state[key] for key in allowed if key in state}
    result['stages'] = []
    for stage in state.get('stages', []):
        data = {key: stage[key] for key in ('stage', 'started_at', 'finished_at', 'exit_code', 'timed_out',
                                          'status', 'error_class', 'last_observation_at') if key in stage}
        data['stats'] = collection_health.sanitize_stats(stage.get('stats'))
        result['stages'].append(data)
    backup = state.get('backup', {})
    if backup:
        result['backup'] = {key: backup[key] for key in ('status', 'created_at', 'restore_verified') if key in backup}
    return json.dumps(result, default=str)


def begin(state):
    """Called while holding the collector lock; an abandoned owner is recoverable."""
    reference = parse_ts(state['started_at'])
    key = state['context']['source_key']
    stamp = reference.isoformat()
    state['schedule_slot'] = slot(reference)
    state['deadline_at'] = (reference+timedelta(seconds=config.COLLECTION_RUNTIME_SECONDS)).isoformat()
    conn = db.connect()
    try:
        with conn.transaction():
            # Acquiring the kernel collection lock proves the prior coordinator
            # no longer owns it. A live source child still has its stage lock.
            stale = conn.execute("SELECT run_id,record_json FROM collection_cycles WHERE collector_key=? AND status='running'", (key,)).fetchall()
            for row in stale:
                record = json.loads(row['record_json'])
                record.update(status='interrupted', finished_at=stamp, exit_code=124, terminal_reason='owner_lost')
                conn.execute("UPDATE collection_cycles SET status='interrupted',finished_at=?,record_json=? WHERE run_id=?",
                             (stamp, json.dumps(record), row['run_id']))
                conn.execute("UPDATE collection_job_runs SET status='interrupted',finished_at=?,error_class='owner_lost', "
                             "lease_token=NULL,lease_until=NULL WHERE run_id=? AND status='running'", (stamp, row['run_id']))
                if db.existing_columns(conn, 'collection_commands'):
                    conn.execute("UPDATE collection_commands SET status='interrupted',reason='owner_lost',applied_at=? WHERE run_id=? AND status='running'", (stamp,row['run_id']))
            result = conn.execute("INSERT INTO collection_cycles (run_id,collector_key,schedule_slot,policy_key,origin,started_at,heartbeat_at,deadline_at,status,record_json) "
                                  "VALUES (?,?,?,?,?,?,?,?,'running',?) ON CONFLICT(collector_key,schedule_slot) DO NOTHING",
                                  (state['run_id'], key, state['schedule_slot'], state['context']['policy_key'], state['run_origin'],
                                   stamp, stamp, state['deadline_at'], _record(state)))
            return result.rowcount == 1
    finally:
        conn.close()


def start_job(state, job, timeout):
    reference = datetime.now(timezone.utc)
    job_id, token = uuid.uuid4().hex, uuid.uuid4().hex
    conn = db.connect()
    try:
        with conn.transaction():
            attempt = conn.execute('SELECT COUNT(*) FROM collection_job_runs WHERE run_id=? AND job=?', (state['run_id'], job)).fetchone()[0]+1
            conn.execute("INSERT INTO collection_job_runs (job_run_id,run_id,job,attempt,started_at,lease_token,lease_until,status,result_json) "
                         "VALUES (?,?,?,?,?,?,?,'running','{}')",
                         (job_id,state['run_id'],job,attempt,reference.isoformat(),token,
                          (reference+timedelta(seconds=timeout)).isoformat()))
        return job_id
    finally:
        conn.close()


def finish_job(job_id, result):
    stats = collection_health.sanitize_stats(result.get('stats'))
    code = result.get('exit_code', 0)
    status = result.get('status') or ('cancelled' if result.get('reason') == 'operator_cancel' else 'partial' if result.get('timed_out') or stats.get('runtime_limited') else
                                    'paused' if code in (69,75) else 'failed' if code or stats.get('errors') or stats.get('skipped_no_provider') else
                                    'skipped' if stats.get('status') == 'skipped' else 'succeeded')
    safe = {key: result[key] for key in ('started_at','finished_at','exit_code','last_observation_at','timed_out') if key in result}
    previous = db.query_one('SELECT result_json FROM collection_job_runs WHERE job_run_id=?', (job_id,))
    retained = json.loads(previous['result_json']) if previous else {}
    retained.update(safe)
    safe = retained
    safe['stats'] = stats
    db.execute('UPDATE collection_job_runs SET status=?,finished_at=?,lease_token=NULL,lease_until=NULL,error_class=?,result_json=? WHERE job_run_id=?',
               (status,result.get('finished_at') or datetime.now(timezone.utc).isoformat(),result.get('error_class') or
                ('timeout' if result.get('timed_out') else 'stage_error' if status=='failed' else None),json.dumps(safe),job_id))


def finish(state):
    conn = db.connect()
    try:
        with conn.transaction():
            stamp = state['finished_at']
            conn.execute('UPDATE collection_cycles SET status=?,finished_at=?,heartbeat_at=?,record_json=? WHERE run_id=?',
                         (state['status'], stamp, stamp, _record(state), state['run_id']))
            conn.execute("UPDATE collection_job_runs SET status='interrupted',finished_at=?,error_class='owner_lost', "
                         "lease_token=NULL,lease_until=NULL WHERE run_id=? AND status='running'", (stamp,state['run_id']))
            cutoff = (parse_ts(stamp)-timedelta(days=config.COLLECTION_RUN_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM collection_cycles WHERE finished_at<? AND status<>'running'", (cutoff,))
            if db.existing_columns(conn, 'collection_commands'):
                conn.execute("DELETE FROM collection_commands WHERE accepted_at<? AND status NOT IN ('pending','running')", (cutoff,))
                conn.execute('DELETE FROM collection_profiles WHERE created_at<? AND profile_id NOT IN (SELECT profile_id FROM collection_control)', (cutoff,))
    finally:
        conn.close()


def progress(state):
    db.execute('UPDATE collection_cycles SET record_json=?,heartbeat_at=? WHERE run_id=?',
               (_record(state),datetime.now(timezone.utc).isoformat(),state['run_id']))


def jobs(reference=None):
    reference = reference or datetime.now(timezone.utc)
    if not available():
        return {'version': VERSION, 'available': False, 'jobs': {}}
    key = collection_health.context()['source_key']
    rows = db.query("SELECT j.job,MAX(j.started_at) AS last_attempt_at, "
                    "MAX(CASE WHEN j.status='succeeded' THEN j.finished_at END) AS last_success_at, "
                    "MAX(json_extract(j.result_json,'$.last_observation_at')) AS last_observation_at "
                    "FROM collection_job_runs j JOIN collection_cycles c ON c.run_id=j.run_id WHERE c.collector_key=? GROUP BY j.job", (key,))
    result = {}
    for row in rows:
        status = db.scalar('SELECT j.status FROM collection_job_runs j JOIN collection_cycles c ON c.run_id=j.run_id '
                           'WHERE c.collector_key=? AND j.job=? ORDER BY j.started_at DESC,j.attempt DESC LIMIT 1', (key,row['job']))
        result[row['job']] = {'status':status,'last_attempt_at':str(row['last_attempt_at']),
                             'last_success_at':str(row['last_success_at']) if row['last_success_at'] else None,
                             'last_observation_at':row['last_observation_at']}
    for item in result.values():
        stamp = parse_ts(item['last_success_at'])
        item['success_age_hours'] = max(0,(reference-stamp).total_seconds()/3600) if stamp else None
    latest = db.query_one('SELECT started_at,heartbeat_at,schedule_slot,status FROM collection_cycles '
                          'WHERE collector_key=? ORDER BY started_at DESC LIMIT 1', (key,))
    if latest:
        latest = {name:str(value) if value is not None else None for name,value in latest.items()}
    missed = max(0,int((reference-parse_ts(latest['schedule_slot'])).total_seconds()//config.COLLECTION_INTERVAL_SECONDS)) if latest else None
    return {'version': VERSION, 'available': True, 'jobs': result,'latest_cycle':latest,'elapsed_slots_since_start':missed}
