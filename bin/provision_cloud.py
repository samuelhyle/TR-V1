#!/usr/bin/env python3
"""Seed the persistent disk and database a cloud deployment needs before it can start.

`cloud_runtime.prerequisites()` refuses to launch unless the database holds at least
one account membership, and `permissions.load()` refuses to authorise collection
unless the registry file exists on the disk. Both of those are deliberate: a
deployment that starts with no accounts, or with no record of why collection is
permitted, would answer requests it has no business answering. But it means the
service cannot start until an operator has put those two things in place, and nothing
in the repository said how.

This is that step. It is idempotent and it is separate from the data migration:
`bin/import_cloud.py` moves the rows, this moves the files and verifies that the
result is a service which can actually answer.

It does four things, in order, stopping at the first that cannot be done safely:

1. Copy the permission registry and the operator's collection confirmation onto the
   persistent disk, creating the directory the runtime expects.
2. Copy the private environment file, so `TORI_CONTACT_EMAIL` and the eBay
   credentials are present. Without the contact address the collector reports
   `missing_contact` and the compliance gate refuses to start a run.
3. Check the database holds a schema at the current version and at least one active
   developer membership. A database that does not is reported with the command that
   fixes it rather than being silently repaired -- an empty schema and an empty
   workspace are different faults and only one of them is a migration away.
4. Report what is configured and what is not, so the operator can see the remaining
   gaps without reading the logs.

Example:
  TORI_PG_DSN=postgres://... python3 bin/provision_cloud.py --data-dir /var/lib/tori/data
  TORI_PG_DSN=postgres://... python3 bin/provision_cloud.py --check
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from toriresearch import config, permissions, schema  # noqa: E402


# What the runtime expects to find on the disk, and where it comes from locally.
REQUIRED_FILES = [
    # The registry the runtime authorises collection against. Naming this one wrongly
    # is what made the first cloud deployment unconfigurable: `tori_collection.json`
    # holds the operator's confirmation and has no `entries` key, so pointing at it
    # makes every permission check raise.
    ('authorizations/registry.json', ROOT / 'data/authorizations/registry.json'),
    ('authorizations/tori_collection.json', ROOT / 'data/authorizations/tori_collection.json'),
]


def copied(report):
    for relative, origin in REQUIRED_FILES:
        if not origin.is_file():
            report.append({'path': relative, 'status': 'missing-source',
                           'detail': f'{origin} does not exist; create it from the local workspace'})
            continue
        target = config.DATA_DIR / relative
        if target.is_file() and target.read_bytes() == origin.read_bytes():
            report.append({'path': relative, 'status': 'unchanged'})
            continue
        existed = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(origin, target)
        target.chmod(0o600)
        report.append({'path': relative, 'status': 'updated' if existed else 'copied'})


def env_target():
    """Where the runtime looks for its private environment file."""
    return Path(os.environ.get('TORI_ENV_FILE')
                or (config.DATA_DIR.parent / 'config' / 'local.env'))


def seed_environment(report, source):
    """Copy the private environment file onto the disk when it is not there yet.

    Never overwrites one that exists. The file holds the contact address and the
    seller API credentials; replacing it would silently rotate them out from under a
    running deployment, and `--seed-env-from` is the only thing that writes here.
    """
    target = env_target()
    if target.is_file():
        report.append({'path': str(target), 'status': 'present'})
        return
    if source and Path(source).is_file():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target)
        target.chmod(0o600)
        report.append({'path': str(target), 'status': 'copied'})
        return
    report.append({'path': str(target), 'status': 'absent',
                   'detail': 'the runtime reads it silently; without it there is no contact '
                             'address and no eBay credentials, so collection reports '
                             'missing_contact and seller connections cannot complete'})


def check_database(report):
    """The two conditions `prerequisites()` enforces, checked before the deploy finds them."""
    from toriresearch import db
    if not config.POSTGRES_DSN:
        report.append({'path': 'database', 'status': 'skipped', 'detail': 'TORI_PG_DSN is not set'})
        return False
    connection = db.connect()
    try:
        version = db.recorded_version(connection)
        if version != schema.SCHEMA_VERSION:
            report.append({'path': 'database', 'status': 'wrong-version',
                           'detail': f'recorded {version}, this build needs {schema.SCHEMA_VERSION}; '
                                     'run bin/import_cloud.py against an empty target'})
            return False
        developers = connection.execute(
            "SELECT COUNT(*) AS n FROM cloud_memberships m JOIN auth_users u ON u.user_id=m.user_id "
            "WHERE m.role='developer' AND u.active=TRUE AND m.workspace_id=?",
            (os.environ.get('TORI_WORKSPACE_ID', 'default'),)).fetchone()
        if not developers['n']:
            report.append({'path': 'database', 'status': 'no-developer',
                           'detail': 'no active developer membership; run bin/import_cloud.py'})
            return False
        report.append({'path': 'database', 'status': 'ready',
                       'detail': f'schema {version}, {developers["n"]} active developer(s)'})
        return True
    finally:
        connection.close()


def check_authorities(report):
    """Whether collection can be authorised at all, which is a file question."""
    try:
        loaded = permissions.load()
    except permissions.PermissionDenied as exc:
        report.append({'path': 'permissions', 'status': 'denied', 'detail': str(exc)})
        return False
    granted = sorted(entry['id'] for entry in loaded['entries'] if permissions._active(entry, permissions.datetime.now(permissions.timezone.utc)))
    report.append({'path': 'permissions', 'status': 'ready',
                   'detail': f'{len(granted)} active: {", ".join(granted) or "none"}'})
    return bool(granted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, help='Where TORI_DATA_DIR points on the deployment.')
    parser.add_argument('--env-file', type=Path,
                        help='Where TORI_ENV_FILE points on the deployment.')
    parser.add_argument('--seed-env-from', type=Path,
                        help='A local private environment file to copy to that path.')
    parser.add_argument('--check', action='store_true',
                        help='Report what is missing without changing anything.')
    arguments = parser.parse_args()
    if arguments.data_dir:
        os.environ['TORI_DATA_DIR'] = str(arguments.data_dir)
        config.DATA_DIR = arguments.data_dir
    if arguments.env_file:
        os.environ['TORI_ENV_FILE'] = str(arguments.env_file)

    report = []
    if arguments.check:
        for relative in ('authorizations/registry.json', 'authorizations/tori_collection.json'):
            target = config.DATA_DIR / relative
            report.append({'path': str(target), 'status': 'present' if target.is_file() else 'absent'})
        seed_environment(report, None)
    else:
        copied(report)
        seed_environment(report, arguments.seed_env_from)

    database_ready = check_database(report)
    authorised = check_authorities(report)
    print(json.dumps(report, indent=2))
    if not (database_ready and authorised):
        print('\nThe deployment cannot start until the items above are resolved. '
              'See the module docstring for the command that fixes each.', file=sys.stderr)
        return 1
    print('\nThe deployment has a schema, at least one developer and a collection authority.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
