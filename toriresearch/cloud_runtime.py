"""Supervise one persistent application, its jobs and collection scheduler.

One backend instance shares its private filesystem with its child processes.
Business records live in PostgreSQL; model files, reports and source controls
live on the deployment's persistent disk. No host checkout is required.
"""
import os
import json
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import config, db, schema, fileio


def configured():
    if not config.POSTGRES_DSN:
        raise RuntimeError('Configure TORI_PG_DSN for the persistent cloud database.')
    if len(os.environ.get('TORI_GATEWAY_KEY', '')) < 48:
        raise RuntimeError('Configure a strong private gateway key.')
    origin = urlparse(os.environ.get('TORI_PUBLIC_ORIGIN', ''))
    if origin.scheme != 'https' or not origin.hostname or origin.path not in {'', '/'}:
        raise RuntimeError('Configure the HTTPS Netlify origin.')


def prerequisites():
    configured()
    receipt_path = os.environ.get('TORI_MIGRATION_RECEIPT')
    if receipt_path:
        receipt = json.loads(Path(receipt_path).read_text())
        if (receipt.get('verified') is not True
                or receipt.get('target', {}).get('origin') != os.environ['TORI_PUBLIC_ORIGIN']
                or receipt.get('release', {}).get('schema_version') != schema.SCHEMA_VERSION):
            raise RuntimeError('Restore the verified migration artifacts for this deployment.')
    for directory in (config.DATA_DIR, config.REPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = db.connect()
    try:
        if db.recorded_version(connection) != schema.SCHEMA_VERSION:
            raise RuntimeError('Migrate the cloud database before starting the application.')
        if not connection.execute('SELECT user_id FROM cloud_memberships LIMIT 1').fetchone():
            raise RuntimeError('Import the existing workspace accounts before starting the application.')
    finally:
        connection.close()


class BootstrapHandler(BaseHTTPRequestHandler):
    """Expose liveness only while the runtime disk is being provisioned."""

    def do_GET(self):
        body = json.dumps({'status': 'awaiting_migration', 'ready': False}).encode()
        self.send_response(200 if self.path == '/healthz' else 503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()

    def log_message(self, *_):
        pass


def await_provisioning(stop, port):
    """Keep shell/SFTP access available without exposing an empty application.

    Render's persistent disk is available only to its running instance. A
    crash-looping prerequisite check made the documented seed commands impossible
    to execute. Health here means the bootstrap process is alive; the release
    preflight separately requires authenticated application/worker readiness.
    """
    configured()  # Invalid credentials/origins remain configuration errors.
    server = ThreadingHTTPServer(('0.0.0.0', port), BootstrapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not stop.is_set():
            try:
                prerequisites()
            except Exception:
                # Database exceptions may contain a DSN. Never log their text.
                stop.wait(2)
            else:
                return True
        return False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def schedule(stop):
    interval = int(os.environ.get('TORI_COLLECTION_INTERVAL_SECONDS', '900'))
    if interval < 900:
        raise ValueError('Collection intervals must be at least 15 minutes.')
    # Collection is enabled at the final cutover, after the former collector
    # has been stopped. Preview deployments never collect from the platform.
    if os.environ.get('TORI_CLOUD_COLLECTION_ENABLED') != '1' or os.environ.get('TORI_CLOUD_READ_ONLY') == '1':
        return
    while not stop.is_set():
        env = dict(os.environ, TORI_RUN_ORIGIN='scheduled')
        with subprocess.Popen([sys.executable, '-m', 'toriresearch.operations'], env=env) as process:
            while process.poll() is None:
                if stop.wait(1):
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
        stop.wait(interval)


def main():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    port = int(os.environ.get('PORT', '10000'))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid application port.')
    if os.environ.get('TORI_CLOUD_BOOTSTRAP') == '1':
        if not await_provisioning(stop, port):
            return
    else:
        prerequisites()
    commands = [
        [sys.executable, '-m', 'gunicorn', 'toriresearch.cloud_app:application',
         '--bind', '0.0.0.0:' + str(port), '--workers', '1', '--threads', '4',
         '--timeout', '60', '--access-logfile', '-', '--error-logfile', '-'],
        [sys.executable, '-m', 'toriresearch.cloud_worker'],
        [sys.executable, '-m', 'toriresearch.collection_observer'],
    ]
    upstream = os.environ.get('TORI_MODEL_UPSTREAM')
    if upstream:
        target = urlparse('tcp://' + upstream)
        if not target.hostname or not target.port or target.username or target.password or target.path:
            raise ValueError('Use a private model hostname and port.')
        commands.insert(0, ['socat', 'TCP-LISTEN:18080,bind=127.0.0.1,reuseaddr,fork',
                            'TCP:' + target.hostname + ':' + str(target.port)])
    if config.LISTING_ANALYSIS_ENABLED and os.environ.get('TORI_CLOUD_READ_ONLY') != '1':
        commands.append([sys.executable, '-m', 'toriresearch.listing_worker'])
    processes = []
    scheduler = threading.Thread(target=schedule, args=(stop,), daemon=True)
    try:
        for command in commands:
            processes.append(subprocess.Popen(command, start_new_session=True))
        scheduler.start()
        last_heartbeat = 0
        while not stop.wait(.5):
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError('A supervised application process stopped: ' + str(process.returncode))
            if time.monotonic() - last_heartbeat > 5:
                fileio.write_text(config.DATA_DIR / 'runtime-heartbeat.json',
                                  json.dumps({'updated_at': time.time(), 'workers_ready': True}))
                last_heartbeat = time.monotonic()
    finally:
        stop.set()
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        scheduler.join(timeout=15)


if __name__ == '__main__':
    main()
