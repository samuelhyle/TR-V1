#!/usr/bin/env python3
"""Manage the local Docker app while retaining the existing host data folders."""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check_app(url="http://127.0.0.1:8787/"):
    """Check gateway health, the login boundary and the owning runtime's data build."""
    for endpoint, required in (("healthz", ("status",)),):
        try:
            with urllib.request.urlopen(url + endpoint, timeout=45) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or any(key not in payload for key in required):
                raise ValueError("Incomplete dashboard response")
        except (OSError, ValueError) as exc:
            print(f"App check failed at /{endpoint}: {exc}", file=sys.stderr)
            return 1
        print(f"/{endpoint}: OK")
    try:
        urllib.request.urlopen(url + 'api/snapshot', timeout=15).close()
        print('App check failed: dashboard data is exposed without login.', file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            print(f'App check failed: login boundary returned {exc.code}.', file=sys.stderr)
            return 1
    except OSError:
        print('App check failed: gateway is unavailable.', file=sys.stderr)
        return 1
    command = ['docker', 'exec', 'toriresearch-app-1', 'python', '-c',
               "from toriresearch import dashboard; s=dashboard.cached_snapshot(); "
               "assert all(k in s for k in ('generated_at','health','panels'))"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        print('App check failed: cannot verify data in the owning runtime.', file=sys.stderr)
        return 1
    if result.returncode:
        print('App check failed: dashboard data build failed in the owning runtime.', file=sys.stderr)
        return 1
    print('/api/snapshot: login required; dashboard data build OK')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "logs", "check"))
    args = parser.parse_args()
    if args.action == "check":
        return check_app()
    container_root = ROOT
    ownership = ROOT/'data/local_runtime.json'
    if ownership.is_file():
        record = json.loads(ownership.read_text())
        if (record.get('runtime') == 'docker' and record.get('container') == 'toriresearch-app-1'
                and record.get('host_database') == str((ROOT/'data/research.sqlite').resolve())):
            container_root = Path(record['database']).parent.parent
    env = {**os.environ, "TORI_PROJECT_ROOT": str(container_root),
           "TORI_LOCAL_UID": str(os.getuid()), "TORI_LOCAL_GID": str(os.getgid())}
    base = ["docker", "compose", "--project-directory", str(ROOT), "-f", str(ROOT/"compose.yaml")]
    if args.action == "start":
        for path in (ROOT/"data/research.sqlite", ROOT/"reports", ROOT/"config/local.env"):
            if not path.exists():
                parser.error(f"Required saved state is missing: {path}")
        ownership = ROOT/'data/local_runtime.json'
        status_path = ROOT/'data/collection_status.json'
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        if status.get('status') == 'running':
            if ownership.exists():
                probe = subprocess.run(['docker','exec','toriresearch-app-1','python','-c',
                                        "from toriresearch import db; raise SystemExit(42 if 'collection' in db.active_locks() else 0)"],
                                       capture_output=True)
                if probe.returncode == 42:
                    parser.error('Collection is active; rebuild after this cycle finishes.')
            else:
                parser.error('A native collection cycle is active; finish it before the first Docker cutover.')
        if not ownership.exists():
            record = {'runtime':'docker','container':'toriresearch-app-1',
                      'database':str((ROOT/'data/research.sqlite').resolve()),
                      'enabled_at':datetime.now(timezone.utc).isoformat()}
            temporary = ownership.with_suffix('.tmp')
            temporary.write_text(json.dumps(record,indent=2)+'\n')
            temporary.chmod(0o600)
            temporary.replace(ownership)
        command = ["up", "-d", "--build", "--wait", "--wait-timeout", "120"]
    elif args.action == "stop":
        command = ["down"]
    elif args.action == "restart":
        probe = subprocess.run(['docker','exec','toriresearch-app-1','python','-c',
                                "from toriresearch import db; raise SystemExit(42 if 'collection' in db.active_locks() else 0)"],
                               capture_output=True)
        if probe.returncode == 42:
            parser.error('Collection is active; restart after this cycle finishes.')
        # Recreate both services together: web shares the app's network namespace.
        command = ["up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120"]
    elif args.action == "status":
        command = ["ps"]
    elif args.action == "logs":
        command = ["logs", "--tail", "100"]
    result = subprocess.run(base+command, cwd=ROOT, env=env)
    if result.returncode == 0 and args.action in ("start", "restart"):
        if check_app():
            return 1
        print("Local site and app: http://127.0.0.1:8787/")
        print("Data and reports stay in the repository's host folders.")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
