"""Validated persistent collector commands and live source pacing."""

import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import backups, collection_events as events, config, db
from .metrics import parse_ts


class Conflict(ValueError):
    pass


class CollectionHalted(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def key():
    return backups.source_key()


def defaults():
    return {'source_rps': min(config.RATE_LIMIT.get(h, config.RATE_LIMIT['default'])[0] /
                              config.RATE_LIMIT.get(h, config.RATE_LIMIT['default'])[1] for h in ('tori.fi', 'www.tori.fi')),
            'item_rps': config.ITEM_RPS, 'sweep_budget': config.SWEEP_BUDGET,
            'sweep_pages': config.SWEEP_PAGES, 'item_budget': config.ITEM_BUDGET, 'enrich_budget': config.ENRICH_BUDGET,
            'categories': list(config.FOCUS_CATEGORIES), 'municipalities': list(config.FOCUS_MUNICIPALITIES)}


def ceilings():
    # Bootstrap ceilings are pinned in the supervisor environment before profile
    # overrides, so lowering a run's pace never silently lowers its editable ceiling.
    supplied = os.environ.get('TORI_COLLECTION_CEILINGS')
    return json.loads(supplied) if supplied else defaults()


def current():
    settings = defaults()
    if not events.available():
        return {'available': False, 'settings': settings, 'revision': 0, 'collection_enabled': True, 'ceilings': ceilings()}
    row = db.query_one('SELECT c.*,p.settings_json FROM collection_control c JOIN collection_profiles p ON p.profile_id=c.profile_id WHERE collector_key=?', (key(),))
    return {'available': True, 'settings': json.loads(row['settings_json']) if row else settings,
            'profile_id': row['profile_id'] if row else None,
            'revision': row['revision'] if row else 0, 'collection_enabled': bool(row['collection_enabled']) if row else True,
            'ceilings': ceilings(), 'updated_at': str(row['updated_at']) if row else None}


def validate(settings, base=None):
    if not isinstance(settings, dict) or set(settings)-set(defaults()):
        raise ValueError('unsupported collection settings')
    result = dict(base or defaults())
    result.update(settings)
    caps = ceilings()
    for field in ('source_rps', 'item_rps'):
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value > caps[field]:
            raise ValueError(f'{field} must be finite, greater than zero, and at most {caps[field]}')
    for field in ('sweep_budget', 'sweep_pages', 'item_budget', 'enrich_budget'):
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < (1 if field == 'sweep_pages' else 0) or value > caps[field]:
            raise ValueError(f'{field} must be an integer within the configured ceiling {caps[field]}')
    for field in ('categories', 'municipalities'):
        value = result[field]
        if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not v.strip() or len(v) > 150 or ',' in v for v in value):
            raise ValueError(f'invalid {field}')
        result[field] = sorted(set(v.strip() for v in value))
    from . import permissions
    for action in ('discovery', 'detail', 'enrichment'):
        permissions.require('tori', action, category_paths=result['categories'], municipalities=result['municipalities'])
    return result


def environment(settings):
    names = {'source_rps': 'TORI_RPS', 'item_rps': 'TORI_ITEM_RPS', 'sweep_budget': 'TORI_SWEEP_BUDGET',
             'sweep_pages': 'TORI_SWEEP_PAGES', 'item_budget': 'TORI_ITEM_BUDGET', 'enrich_budget': 'TORI_ENRICH_BUDGET',
             'categories': 'TORI_FOCUS_CATEGORIES', 'municipalities': 'TORI_FOCUS_MUNICIPALITIES'}
    result = {names[k]: ','.join(v) if isinstance(v, list) else str(v) for k, v in settings.items()}
    result['TORI_COLLECTION_CEILINGS'] = json.dumps(ceilings())
    result['TORI_COLLECTION_PROFILE_JSON'] = json.dumps(settings)
    return result


def _initialize(conn):
    collector = key()
    if conn.execute('SELECT 1 FROM collection_control WHERE collector_key=?', (collector,)).fetchone():
        return
    profile = uuid.uuid4().hex
    conn.execute('INSERT INTO collection_profiles (profile_id,created_at,settings_json) VALUES (?,?,?)',
                 (profile, events.now(), json.dumps(defaults())))
    conn.execute('INSERT INTO collection_control (collector_key,profile_id,revision,collection_enabled,updated_at) VALUES (?,?,0,TRUE,?) ON CONFLICT(collector_key) DO NOTHING',
                 (collector, profile, events.now()))


def settings(payload):
    if set(payload)-{'settings', 'expected_revision'}:
        raise ValueError('unsupported settings fields')
    before = current()
    proposed = validate(payload.get('settings'), before['settings'])
    expected = payload.get('expected_revision')
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise ValueError('expected_revision is required')
    conn = db.connect()
    try:
        with conn.transaction():
            _initialize(conn)
            profile = uuid.uuid4().hex
            conn.execute('INSERT INTO collection_profiles (profile_id,created_at,settings_json) VALUES (?,?,?)', (profile, events.now(), json.dumps(proposed)))
            result = conn.execute('UPDATE collection_control SET profile_id=?,revision=revision+1,updated_at=? WHERE collector_key=? AND revision=?',
                                  (profile, events.now(), key(), expected))
            if result.rowcount != 1:
                raise Conflict('Settings changed in another session; refresh before saving.')
    finally:
        conn.close()
    return current()


def initialize():
    if events.available():
        conn = db.connect()
        try:
            with conn.transaction():
                _initialize(conn)
        finally:
            conn.close()
    return current()


def active():
    if not events.available():
        return None
    row = db.query_one("SELECT * FROM collection_cycles WHERE collector_key=? AND status='running' ORDER BY started_at DESC LIMIT 1", (key(),))
    return detail(row['run_id']) if row else None


def preview(payload=None):
    from . import continuous, compliance
    payload = payload or {}
    if set(payload)-{'settings'}:
        raise ValueError('unsupported preview fields')
    state = current()
    proposed = validate(payload.get('settings', {}), state['settings'])
    previous = continuous.latest_start()
    next_at = previous+timedelta(seconds=config.COLLECTION_INTERVAL_SECONDS-5) if previous else datetime.now(timezone.utc)
    run = active()
    locks = db.active_locks()
    from .http import collection_stop
    from . import schema
    conn = db.connect()
    try:
        migrated = db.recorded_version(conn) == schema.SCHEMA_VERSION
    finally:
        conn.close()
    blocked = (not state['available'] or not migrated or not config.contact_email_ok() or
               bool(run or any(n in locks for n in ('collection', 'collection-source', 'sweep', 'itempage'))) or
               next_at > datetime.now(timezone.utc))
    reason = ('schema_update_required' if not state['available'] or not migrated else
              'missing_contact' if not config.contact_email_ok() else 'collector_busy' if run or any(n in locks for n in ('collection', 'collection-source', 'sweep', 'itempage')) else
              'cadence_not_due' if next_at > datetime.now(timezone.utc) else None)
    return {**state, 'settings': proposed, 'can_start': not blocked, 'start_reason': reason,
            'next_eligible_at': next_at.isoformat(), 'active_run': run, 'platform_pause': collection_stop(),
            'interval_seconds': config.COLLECTION_INTERVAL_SECONDS, 'runtime_seconds': config.COLLECTION_RUNTIME_SECONDS,
            'individual_action_caps': compliance.ALLOWED_ACTIONS,
            'retention_days': config.COLLECTION_RUN_RETENTION_DAYS}


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', value):
        raise ValueError('client_token must contain 8 to 100 letters, numbers, underscores or hyphens')
    return value


def command_result(command_id):
    row = db.query_one('SELECT * FROM collection_commands WHERE command_id=?', (command_id,))
    if not row:
        raise ValueError('command not found')
    row['payload'] = json.loads(row.pop('payload_json'))
    return row


def finish_command(command_id, status, reason=None, run_id=None):
    if command_id:
        db.execute('UPDATE collection_commands SET status=?,reason=?,run_id=COALESCE(?,run_id),applied_at=? WHERE command_id=?',
                   (status, reason, run_id, events.now(), command_id))


def launch(command_id, proposed):
    env = dict(os.environ)
    env.update(environment(proposed))
    env.update(TORI_COLLECTION_COMMAND_ID=command_id, TORI_RUN_ORIGIN='manual', PYTHONUNBUFFERED='1')
    # Supervisor lives independently of the request thread; the durable journal
    # is the source of status. Console output is retained by structured telemetry.
    subprocess.Popen([sys.executable, '-m', 'toriresearch.operations'], cwd=str(config.ROOT), env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def start(payload):
    if set(payload)-{'client_token', 'settings'}:
        raise ValueError('unsupported start fields')
    token = _token(payload.get('client_token'))
    existing = db.query_one('SELECT command_id FROM collection_commands WHERE collector_key=? AND client_token=?', (key(), token))
    if existing:
        row = command_result(existing['command_id'])
        if row['command'] != 'start' or row['payload'].get('requested_settings') != payload.get('settings', {}):
            raise Conflict('client_token was already used for a different request')
        return row
    planned = preview({'settings': payload.get('settings', {})})
    if not planned['can_start']:
        # A simultaneous retry may have acquired worker ownership between the
        # initial token lookup and readiness check. Return that accepted identity.
        existing = db.query_one('SELECT command_id FROM collection_commands WHERE collector_key=? AND client_token=?', (key(), token))
        if existing:
            row = command_result(existing['command_id'])
            if row['command'] == 'start' and row['payload'].get('requested_settings') == payload.get('settings', {}):
                return row
            raise Conflict('client_token was already used for a different request')
        raise Conflict(planned['start_reason']+'; next eligible: '+planned['next_eligible_at'])
    command_id = uuid.uuid4().hex
    encoded = {'settings': planned['settings'], 'revision': planned['revision'], 'requested_settings': payload.get('settings', {})}
    conn = db.connect()
    try:
        with conn.transaction():
            _initialize(conn)
            encoded['profile_id'] = conn.execute('SELECT profile_id FROM collection_control WHERE collector_key=?', (key(),)).fetchone()[0]
            result = conn.execute('INSERT INTO collection_commands (command_id,client_token,collector_key,command,accepted_at,status,payload_json) '
                                  "VALUES (?,?,?,'start',?,'pending',?) ON CONFLICT(collector_key,client_token) DO NOTHING",
                                  (command_id, token, key(), events.now(), json.dumps(encoded)))
    finally:
        conn.close()
    if result.rowcount:
        try:
            launch(command_id, planned['settings'])
        except OSError:
            finish_command(command_id, 'failed', 'launch_failed')
    else:
        command_id = db.scalar('SELECT command_id FROM collection_commands WHERE collector_key=? AND client_token=?', (key(), token))
    accepted = command_result(command_id)
    if accepted['command'] != 'start' or accepted['payload'].get('requested_settings') != payload.get('settings', {}):
        raise Conflict('client_token was already used for a different request')
    return accepted


def control(payload):
    if set(payload)-{'command', 'client_token', 'run_id'}:
        raise ValueError('unsupported control fields')
    command = payload.get('command')
    if command not in ('pause', 'resume', 'stop'):
        raise ValueError('command must be pause, resume or stop')
    token = _token(payload.get('client_token'))
    run_id = payload.get('run_id')
    if command == 'stop' and (not isinstance(run_id, str) or not run_id):
        raise ValueError('stop requires run_id')
    conn = db.connect()
    try:
        with conn.transaction():
            existing = conn.execute('SELECT command_id,command,run_id FROM collection_commands WHERE collector_key=? AND client_token=?', (key(), token)).fetchone()
            if existing:
                if existing['command'] != command or existing['run_id'] != run_id:
                    raise Conflict('client_token was already used for a different request')
                command_id = existing['command_id']
            else:
                if command == 'stop' and not conn.execute("SELECT 1 FROM collection_cycles WHERE run_id=? AND collector_key=? AND status='running'", (run_id, key())).fetchone():
                    raise Conflict('run is no longer active')
                command_id = uuid.uuid4().hex
                conn.execute('INSERT INTO collection_commands (command_id,client_token,collector_key,command,run_id,accepted_at,status,payload_json) VALUES (?,?,?,?,?,?,?,?)',
                             (command_id, token, key(), command, run_id, events.now(), 'pending', '{}'))
                if command != 'stop':
                    _initialize(conn)
                    conn.execute('UPDATE collection_control SET collection_enabled=?,revision=revision+1,updated_at=? WHERE collector_key=?',
                                 (command == 'resume', events.now(), key()))
                    if not conn.execute("SELECT 1 FROM collection_cycles WHERE collector_key=? AND status='running'", (key(),)).fetchone():
                        conn.execute("UPDATE collection_commands SET status='applied',applied_at=? WHERE command_id=?", (events.now(), command_id))
    finally:
        conn.close()
    return command_result(command_id)


def checkpoint(source=True):
    run_id, job_id = events.context()
    state = current()
    if run_id and state['available']:
        owner = db.query_one('SELECT status FROM collection_cycles WHERE run_id=?', (run_id,))
        if not owner or owner['status'] != 'running':
            raise CollectionHalted('operator_cancel' if owner and owner['status'] == 'cancelled' else 'owner_lost')
    initial = os.environ.get('TORI_COLLECTION_INITIAL_REVISION')
    initial_profile = os.environ.get('TORI_COLLECTION_INITIAL_PROFILE_ID')
    profile = os.environ.get('TORI_COLLECTION_PROFILE_JSON')
    same_profile = state.get('profile_id') == initial_profile if initial_profile is not None else initial is not None and state['revision'] <= int(initial)
    if run_id and profile and same_profile:
        # Unsaved manual-run settings remain the baseline until a newer
        # persisted revision explicitly changes the running collector's pace.
        state = {**state, 'settings': json.loads(profile)}
    if not state['available']:
        return state
    conn = db.connect()
    try:
        with conn.transaction():
            rows = conn.execute("SELECT command_id,command FROM collection_commands WHERE collector_key=? AND status='pending' AND command IN ('pause','resume','stop') AND (run_id IS NULL OR run_id=?) ORDER BY accepted_at", (key(), run_id)).fetchall()
            stopped = False
            for row in rows:
                if row['command'] == 'stop' and not run_id:
                    continue
                stopped = stopped or row['command'] == 'stop'
                conn.execute("UPDATE collection_commands SET status='applied',applied_at=? WHERE command_id=?", (events.now(), row['command_id']))
                events.emit('control_applied', {'reason': 'operator_'+row['command'], 'revision': state['revision']}, conn=conn)
            if run_id:
                cancelled = stopped or conn.execute("SELECT 1 FROM collection_commands WHERE run_id=? AND command='stop' AND status='applied'", (run_id,)).fetchone()
                if job_id:
                    row = conn.execute('SELECT result_json FROM collection_job_runs WHERE job_run_id=?', (job_id,)).fetchone()
                    if row:
                        progress = json.loads(row[0])
                        if progress.get('applied_revision') != state['revision']:
                            if progress.get('effective_source_rps') is not None and (
                                    progress['effective_source_rps'] != state['settings']['source_rps'] or
                                    progress.get('effective_item_rps') != state['settings']['item_rps']):
                                progress['mixed_pace'] = True
                            progress.update(applied_revision=state['revision'], effective_source_rps=state['settings']['source_rps'], effective_item_rps=state['settings']['item_rps'])
                            conn.execute('UPDATE collection_job_runs SET result_json=? WHERE job_run_id=?', (json.dumps(progress), job_id))
                            events.emit('settings_applied', {'revision': state['revision'], 'source_rps': state['settings']['source_rps'], 'item_rps': state['settings']['item_rps']}, conn=conn)
            else:
                cancelled = False
    finally:
        conn.close()
    if cancelled:
        raise CollectionHalted('operator_cancel')
    if source and not state['collection_enabled']:
        raise CollectionHalted('operator_pause')
    return state


def wait(seconds, source=True):
    """Interruptible local waits; no DB transaction is held while sleeping."""
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        checkpoint(source)
        time.sleep(min(0.5, max(0, end-time.monotonic())))


def detail(run_id):
    row = db.query_one('SELECT * FROM collection_cycles WHERE run_id=? AND collector_key=?', (run_id, key()))
    if not row:
        raise ValueError('run not found')
    row['record'] = json.loads(row.pop('record_json'))
    row['jobs'] = db.query('SELECT * FROM collection_job_runs WHERE run_id=? ORDER BY started_at,attempt', (run_id,))
    for job in row['jobs']:
        job['result'] = json.loads(job.pop('result_json'))
        job.pop('lease_token', None)
    row['counts'] = {v['kind']: v['n'] for v in db.query('SELECT kind,COUNT(*) AS n FROM collection_run_observations WHERE run_id=? GROUP BY kind', (run_id,))}
    row['attribution_available'] = bool(row['record'].get('settings')) or bool(row['counts'])
    row['http_attempts'] = db.scalar("SELECT COUNT(*) FROM collection_events WHERE run_id=? AND event_type='request_started'", (run_id,))
    row['http_completed'] = db.scalar("SELECT COUNT(*) FROM collection_events WHERE run_id=? AND event_type='request_completed'", (run_id,))
    attempts = db.query("SELECT at,payload_json FROM collection_events WHERE run_id=? AND event_type='request_started' ORDER BY event_pk DESC LIMIT 100", (run_id,))
    sampled = [parse_ts(v['at']) for v in attempts]
    span = (sampled[0]-sampled[-1]).total_seconds() if len(sampled)>1 else 0
    row['measured_rps'] = round((len(sampled)-1)/span, 3) if span>0 else None
    row['recent_attempts'] = sum(stamp >= datetime.now(timezone.utc)-timedelta(seconds=30) for stamp in sampled)
    row['recent_http_statuses'] = {str(v['status']):v['n'] for v in db.query("SELECT json_extract(payload_json,'$.http_status') AS status,COUNT(*) AS n FROM collection_events WHERE run_id=? AND event_type='request_completed' GROUP BY json_extract(payload_json,'$.http_status')", (run_id,))}
    return row


def runs(after=0, limit=20):
    offset = events.bounded_int(after, 0, 1000000)
    count = events.bounded_int(limit, 20, 100)
    if not count:
        raise ValueError('limit must be positive')
    rows = db.query('SELECT run_id,origin,started_at,finished_at,status,record_json FROM collection_cycles WHERE collector_key=? ORDER BY started_at DESC,run_id DESC LIMIT ? OFFSET ?', (key(), count, offset))
    for row in rows:
        record = json.loads(row.pop('record_json'))
        row.update(current_stage=record.get('current_stage'), reason=record.get('terminal_reason'))
    return {'runs': rows, 'next_cursor': offset+len(rows), 'has_more': len(rows) == count}


def recover():
    """Startup reconciliation never automatically replays old start requests."""
    if not events.available():
        return
    collector = key()
    cutoff = (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    conn = db.connect()
    try:
        with conn.transaction():
            conn.execute("UPDATE collection_commands SET status='interrupted',reason='launch_owner_lost',applied_at=? WHERE collector_key=? AND command='start' AND status='pending' AND run_id IS NULL AND accepted_at<?", (events.now(), collector, cutoff))
            if any(n in db.active_locks() for n in ('collection','collection-source','sweep','itempage')):
                return
            for row in conn.execute("SELECT run_id,record_json FROM collection_cycles WHERE collector_key=? AND status='running'", (collector,)).fetchall():
                record=json.loads(row['record_json'])
                record.update(status='interrupted',terminal_reason='owner_lost',finished_at=events.now())
                conn.execute("UPDATE collection_cycles SET status='interrupted',finished_at=?,record_json=? WHERE run_id=?", (events.now(),json.dumps(record),row['run_id']))
                conn.execute("UPDATE collection_job_runs SET status='interrupted',finished_at=?,error_class='owner_lost',lease_token=NULL,lease_until=NULL WHERE run_id=? AND status='running'", (events.now(),row['run_id']))
                conn.execute("UPDATE collection_commands SET status='interrupted',reason='owner_lost',applied_at=? WHERE run_id=? AND status='running'", (events.now(),row['run_id']))
                events.emit('run_finished', {'status':'interrupted','reason':'owner_lost'}, conn=conn, run_id=row['run_id'])
    finally:
        conn.close()
