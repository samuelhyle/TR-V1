"""Keep a Docker-owned SQLite database inside one operating-system runtime.

Docker Desktop bind mounts preserve files, but native macOS and Linux SQLite
locks do not coordinate. Host CLI commands therefore enter the owning container;
direct native database access fails closed while that owner is configured.
"""

import json
import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

from . import config


def owner():
    if config.POSTGRES_DSN:
        return None
    try:
        value = json.loads((config.DATA_DIR/'local_runtime.json').read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError('Local runtime ownership is unreadable; repair data/local_runtime.json before database access') from exc
    if (not isinstance(value, dict) or value.get('runtime') != 'docker' or
            value.get('container') != 'toriresearch-app-1' or not isinstance(value.get('database'), str)):
        raise RuntimeError('Local runtime ownership is invalid; repair data/local_runtime.json before database access')
    if 'host_database' in value and (not isinstance(value['host_database'], str) or not Path(value['host_database']).is_absolute()):
        raise RuntimeError('Local runtime ownership is invalid; repair data/local_runtime.json before database access')
    # A moved host checkout can keep its original container bind destination.
    # Both paths identify the declared owner; native SQLite must still refuse.
    paths = {value['database'], value.get('host_database')}
    return value if str(config.SQLITE_PATH.resolve()) in paths else None


def in_container():
    return Path('/.dockerenv').exists()


def assert_database_owner():
    if owner() and not in_container():
        raise RuntimeError('SQLite is owned by the local Docker app. Use the CLI, which runs inside Docker; '
                           'start it with python3 bin/local_app.py start if it is stopped.')


def _docker():
    found = shutil.which('docker')
    if found:
        return found
    for path in ('/usr/local/bin/docker', '/Applications/Docker.app/Contents/Resources/bin/docker'):
        if os.access(path, os.X_OK):
            return path
    raise RuntimeError('Docker CLI is unavailable; start Docker Desktop and the local app.')


def command(module, args):
    record = owner()
    if not record:
        raise RuntimeError('No local Docker database owner is configured')
    options = []
    # Scheduler overrides must reach the owning runtime as well as launchd.
    if 'TORI_COLLECTION_INTERVAL_SECONDS' in os.environ:
        options = ['--env','TORI_COLLECTION_INTERVAL_SECONDS='+os.environ['TORI_COLLECTION_INTERVAL_SECONDS']]
    workdir = Path(record['database']).parent.parent if record.get('host_database') else config.DATA_DIR.parent
    return [_docker(), 'exec', '-i', *options, '--workdir', str(workdir),
            '--env', 'TORI_RUN_ORIGIN='+os.environ.get('TORI_RUN_ORIGIN', 'manual'),
            record['container'], 'python', '-m', module, *args]


def delegate(module, args=None):
    """Return an exit code when a host command was routed, or None locally."""
    if not owner() or in_container():
        return None
    arguments = list(sys.argv[1:] if args is None else args)
    if module == 'toriresearch.cli' and arguments and arguments[0] in ('dashboard', 'dev'):
        return open_dashboard(arguments[1:])
    result = subprocess.run(command(module, arguments))
    if result.returncode:
        print('The Docker command failed. Check Docker Desktop and python3 bin/local_app.py status.', file=sys.stderr)
    return result.returncode


def open_dashboard(arguments):
    """Open the existing Docker service instead of starting a duplicate server."""
    from . import compliance
    parser = argparse.ArgumentParser(prog='toriresearch dashboard (Docker)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--refresh', type=int, default=30)
    parser.add_argument('--open', action='store_true')
    options = parser.parse_args(arguments)
    compliance.assert_not_exposed(options.host)
    if options.port != 8787:
        parser.error('The Docker app uses port 8787; manage it with python3 bin/local_app.py start.')
    url = 'http://127.0.0.1:8787/'
    try:
        with urllib.request.urlopen(url+'healthz',timeout=8) as response:
            if response.status != 200:
                raise RuntimeError('Local app health check failed')
    except Exception:
        result = subprocess.run([sys.executable,str(config.ROOT/'bin/local_app.py'),'start'])
        if result.returncode:
            return result.returncode
    print('Docker dashboard: '+url)
    print('Code is rebuilt with python3 bin/local_app.py start; the Docker service controls refresh cadence.')
    if options.open:
        webbrowser.open(url)
    return 0


def active_locks():
    record = owner()
    result = subprocess.run([_docker(), 'exec', record['container'], 'python', '-c',
                             'import json; from toriresearch import db; print(json.dumps(db.active_locks()))'],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('Cannot inspect Docker-owned collection locks; start the local app first')
    return json.loads(result.stdout)
