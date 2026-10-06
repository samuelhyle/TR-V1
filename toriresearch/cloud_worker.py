"""Execute durable cloud jobs in the persistent backend's worker process."""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from . import auth, cloud_jobs, config, dashboard, db, model_workspace

log = logging.getLogger(__name__)


def run_model(payload, row, worker, stop):
    cloud_jobs.sync_models()
    started = model_workspace.start(payload)
    native_id = started['job']['job_id']
    path = config.DATA_DIR / 'model_jobs' / (native_id + '.json')
    cancelled = False
    deadline = time.monotonic() + 1800
    while True:
        current = db.query_one('SELECT status FROM cloud_jobs WHERE job_id=?', (row['job_id'],))
        native = _read_job_state(path)
        if native is _unwritten:
            # The native job has been started but has not written its first state
            # file yet. That window is real -- the file is written by the child
            # process, on the same filesystem, some time after the spawn -- and
            # treating it as a failure would mark a running job failed and leave the
            # child going. Wait for it instead.
            if stop.is_set() or time.monotonic() > deadline:
                return 'interrupted', {'job_id': native_id}, 'The model did not report any state.'
            time.sleep(.2)
            continue
        if (stop.is_set() or current['status'] == 'cancelling') and not cancelled:
            try:
                model_workspace.cancel({'job_id': native_id})
            except ValueError:
                pass  # The native job may have completed between reads.
            cancelled = True
        db.execute("UPDATE cloud_jobs SET result_json=? WHERE job_id=? AND worker_id=? AND status IN ('running','cancelling')",
                   (json.dumps(native, default=str, allow_nan=False), row['job_id'], worker))
        if native['status'] in {'done', 'experimental', 'failed', 'interrupted', 'cancelled'}:
            cloud_jobs.sync_models(upload=True)
            return native['status'], native, native.get('error')
        time.sleep(.2)


# A read that can legitimately fail because a file is not there yet.
_unwritten = object()


def _read_job_state(path):
    """The native job's state, or `_unwritten` while it has not produced one.

    Polled every 200 ms against a file another process writes, so the ordinary
    races are all possible: not created yet, being replaced mid-read, or truncated
    between the two halves of a rewrite. Each is distinguishable from a job that has
    genuinely failed, and each must not be reported as one.
    """
    for _ in range(3):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return _unwritten
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            time.sleep(.05)  # A partial write; the next read will get the whole thing.
    return _unwritten


def run_operation(payload, stop):
    prepared = dashboard.validate_action(payload.get('action'), payload.get('params'))
    if not prepared['ok']:
        raise ValueError(prepared['error'])
    argv = [sys.executable, '-m', 'toriresearch.cli', payload['action']]
    for key, value in prepared['params'].items():
        flag = '--' + key.replace('_', '-')
        argv.extend([flag] if value is True else [flag, str(value)])
    started = time.monotonic()
    stamp = auth.now().isoformat()
    # The collection profile the local dashboard would have passed. A sweep run from
    # the workspace without it silently loses the configured ceilings and focus, and
    # `TORI_RUN_ORIGIN` is what distinguishes a scheduled run from a manual one in the
    # action log -- without it every cloud action is recorded as manual.
    from . import collection_control
    environment = dict(os.environ, **collection_control.environment(), TORI_RUN_ORIGIN='scheduled')
    with subprocess.Popen(argv, cwd=config.ROOT, env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if stop.is_set() or time.monotonic() - started > 1800:
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    stderr = 'Worker stopped.' if stop.is_set() else 'Operation exceeded its 30-minute limit.'
                    break
    dashboard._log(payload['action'], process.returncode, round(time.monotonic()-started, 1), stamp, stdout, stderr)
    return ('done' if process.returncode == 0 else 'interrupted' if stop.is_set() else 'failed',
            {'returncode': process.returncode, 'output': stdout[-1500:]}, stderr[-600:] if process.returncode else None)


def execute(row, worker, stop):
    heartbeat_stop = threading.Event()
    def keep_alive():
        while not heartbeat_stop.wait(15):
            cloud_jobs.heartbeat(row['job_id'], worker)
    thread = threading.Thread(target=keep_alive, daemon=True)
    thread.start()
    try:
        payload = json.loads(row['payload_json'])
        if row['kind'] == 'model':
            status, result, error = run_model(payload, row, worker, stop)
        elif row['kind'] == 'operation':
            status, result, error = run_operation(payload, stop)
        elif row['kind'] == 'analysis':
            from . import cloud_analysis
            result = cloud_analysis.compute(payload)
            status, error = ('interrupted', 'Worker stopped.') if stop.is_set() else ('done', None)
        else:
            raise ValueError('Unsupported worker job.')
        cloud_jobs.finish(row['job_id'], worker, status, result, error)
    except Exception as exc:
        log.exception('Cloud job failed: %s', row['job_id'])
        cloud_jobs.finish(row['job_id'], worker, 'failed', error=str(exc)[:300])
    finally:
        heartbeat_stop.set()
        thread.join(timeout=2)


def main():
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    worker = 'worker-' + str(os.getpid()) + '-' + os.urandom(6).hex()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            row = cloud_jobs.claim(worker, analysis_only=os.environ.get('TORI_CLOUD_READ_ONLY') == '1')
            if row:
                execute(row, worker, stop)
            else:
                stop.wait(1)
    finally:
        model_workspace.shutdown()


if __name__ == '__main__':
    main()
