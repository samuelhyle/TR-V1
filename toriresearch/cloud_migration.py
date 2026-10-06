"""Private, verified transfer of a database backup and its filesystem artifacts.

The owning runtime creates the SQLite backup. This module only reads that verified
copy; it never opens the live database or copies its WAL. A bundle is a directory
that can be transferred with SFTP and verified again on the deployment disk.
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import release

ACCOUNT_FIELDS = {'user_id', 'username', 'password_hash', 'role', 'active',
                  'must_change_password', 'created_at'}
LOCAL_ENV_KEYS = {'TORI_DB', 'TORI_DATA_DIR', 'TORI_REPORTS_DIR', 'TORI_PG_DSN',
                  'TORI_ENV_FILE', 'TORI_NO_ENV_FILE', 'TORI_PERMISSION_REGISTRY',
                  'TORI_GATEWAY_KEY', 'TORI_PUBLIC_ORIGIN', 'TORI_PREVIEW_ORIGINS',
                  'TORI_WORKSPACE_ID', 'TORI_MODEL_UPSTREAM', 'TORI_LLM_URL',
                  'TORI_CLOUD_COLLECTION_ENABLED', 'TORI_CLOUD_BOOTSTRAP',
                  'TORI_CLOUD_READ_ONLY', 'TORI_LLM_ENABLED', 'TORI_ANALYST_ENABLED',
                  'TORI_LISTING_ANALYSIS_ENABLED'}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def private(path):
    if Path(path).stat().st_mode & 0o077:
        raise ValueError('Migration credentials and bundle files must be private (chmod 600).')


def checked_path(root, relative):
    name = PurePosixPath(relative)
    if (not relative or name.is_absolute() or '..' in name.parts or '\\' in relative
            or name.as_posix() != relative):
        raise ValueError('Invalid migration artifact path.')
    result = Path(root) / relative
    for path in (result, *result.parents):
        if path.is_symlink():
            raise ValueError('Migration paths must not contain symbolic links.')
        if path == Path(root):
            break
    return result


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as stream:
        stream.write(content)
    path.chmod(0o600)


def portable_environment(path):
    lines = []
    if path and Path(path).is_file():
        private(path)
        for line in Path(path).read_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key = line.split('=', 1)[0].strip()
            if key.startswith(('TORI_', 'EBAY_')) and key not in LOCAL_ENV_KEYS:
                lines.append(line)
    return ('\n'.join(lines) + '\n').encode()


def package(backup, accounts, data_dir, reports_dir, output, site_id, origin, env_file=None):
    output, backup = Path(output), Path(backup)
    if output.exists():
        raise ValueError('The migration output already exists; choose a new private directory.')
    url = urlparse(origin)
    if (not site_id or url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.path not in {'', '/'} or url.query or url.fragment):
        raise ValueError('Name the intended Netlify site and its HTTPS origin.')
    private(accounts)
    users = json.loads(Path(accounts).read_text())
    if (not isinstance(users.get('users'), list) or not users['users']
            or not any(u.get('role') == 'developer' and u.get('active') for u in users['users'])):
        raise ValueError('Export the current preview accounts, including an active developer.')
    users = {'users': [{key: row[key] for key in ACCOUNT_FIELDS} for row in users['users']]}
    source_manifest = json.loads((backup / 'manifest.json').read_text())
    database = backup / 'research.sqlite'
    if (source_manifest.get('restore_verified') is not True
            or digest(database) != source_manifest.get('sha256')):
        raise ValueError('Use a restore-verified backup matching its checksum.')
    source = sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        counts = {name: source.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for (name,) in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        model_files = [row[0] for row in source.execute('SELECT DISTINCT artifact_path FROM predictive_models')]
    finally:
        source.close()
    for name in model_files:
        if name != Path(name).name or not (Path(data_dir) / 'models' / name).is_file():
            raise ValueError('A registered model artifact is missing; resolve it before migration.')

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix='.cloud-migration-', dir=output.parent))
    entries = []
    try:
        def add(source_path, relative):
            target = checked_path(staging, relative)
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError('Migration artifacts must be regular files.')
            before = source_path.stat()
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source_path, target)
            target.chmod(0o600)
            after = source_path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('An artifact changed during export; retry after its job finishes.')
            entries.append({'path': relative, 'bytes': target.stat().st_size, 'sha256': digest(target)})

        add(database, 'database/research.sqlite')
        write_private(staging / 'database/manifest.json', json.dumps(source_manifest).encode())
        write_private(staging / 'accounts.json', json.dumps(users).encode())
        write_private(staging / 'state/config/local.env', portable_environment(env_file))
        for relative in ('database/manifest.json', 'accounts.json', 'state/config/local.env'):
            path = staging / relative
            entries.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': digest(path)})
        for name in ('models', 'model_jobs', 'authorizations', 'rates'):
            directory = Path(data_dir) / name
            if not directory.exists():
                continue
            for path in sorted(directory.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Migration artifacts must not contain symbolic links.')
                if path.is_file():
                    add(path, 'state/data/' + path.relative_to(data_dir).as_posix())
        for name in ('collection_stop.json', 'collection_baseline.json', 'tori_location_labels.json'):
            path = Path(data_dir) / name
            if path.exists():
                add(path, 'state/data/' + name)
        if Path(reports_dir).exists():
            for path in sorted(Path(reports_dir).rglob('*')):
                if path.is_symlink():
                    raise ValueError('Migration reports must not contain symbolic links.')
                if path.is_file():
                    add(path, 'state/reports/' + path.relative_to(reports_dir).as_posix())
        manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'release': release.describe(), 'target': {'site_id': site_id, 'origin': origin},
                    'database_counts': counts, 'source_backup_at': source_manifest['created_at'],
                    'files': entries, 'bytes': sum(e['bytes'] for e in entries)}
        write_private(staging / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
        verify(staging)
        staging.rename(output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify(bundle):
    root = Path(bundle)
    private(root / 'manifest.json')
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), list):
        raise ValueError('Unsupported migration manifest.')
    names = set()
    for entry in manifest['files']:
        path = checked_path(root, entry['path'])
        if entry['path'] in names or not path.is_file():
            raise ValueError('A migration artifact is duplicated or missing.')
        names.add(entry['path'])
        private(path)
        if path.stat().st_size != entry['bytes'] or digest(path) != entry['sha256']:
            raise ValueError('A migration artifact failed checksum verification.')
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Migration paths must not contain symbolic links.')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != names | {'manifest.json'}:
        raise ValueError('The migration directory contains unlisted files.')
    if not {'database/research.sqlite', 'database/manifest.json', 'accounts.json'} <= names:
        raise ValueError('The migration database or accounts are missing.')
    return manifest


def restore(bundle, runtime_root, site_id, origin):
    manifest = verify(bundle)
    if manifest['target'] != {'site_id': site_id, 'origin': origin}:
        raise ValueError('This migration bundle belongs to a different deployment.')
    if manifest['release']['schema_version'] != release.describe()['schema_version']:
        raise ValueError('Run the migration with its matching schema version.')
    transfers = []
    for entry in manifest['files']:
        if not entry['path'].startswith('state/'):
            continue
        relative = entry['path'].removeprefix('state/')
        target = checked_path(runtime_root, relative)
        if target.exists() and (not target.is_file() or digest(target) != entry['sha256']):
            raise ValueError('The runtime already contains different state; existing files were preserved.')
        transfers.append((Path(bundle) / entry['path'], target))
    receipt = {'verified': True, 'restored_files': len(transfers),
               'database_counts': manifest['database_counts'], 'target': manifest['target'],
               'release': manifest['release'], 'bundle_sha256': digest(Path(bundle) / 'manifest.json')}
    receipt_path = checked_path(runtime_root, 'migration-receipt.json')
    if receipt_path.exists():
        if json.loads(receipt_path.read_text()) != receipt:
            raise ValueError('This runtime has a different migration receipt.')
    for source, target in transfers:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
        target.chmod(0o600)
    if not receipt_path.exists():
        write_private(receipt_path, (json.dumps(receipt, indent=2) + '\n').encode())
    return receipt
