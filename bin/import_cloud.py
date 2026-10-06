#!/usr/bin/env python3
"""Import a verified SQLite snapshot and current accounts into a NEW cloud DB.

This command refuses a populated target. It never reads the live host SQLite
database: pass the database from a verified backup, using its owning runtime.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toriresearch import auth, backups, config, db, schema  # noqa: E402


def table_order():
    remaining = {table['name']: {reference for _, declaration, _ in table['columns']
                                 for reference in re.findall(r'REFERENCES\s+(\w+)', declaration)
                                 if reference != table['name']} for table in schema.TABLES}
    ordered = []
    while remaining:
        ready = [name for name, dependencies in remaining.items() if dependencies <= set(ordered)]
        if not ready:
            raise ValueError('The schema contains a foreign-key cycle.')
        for name in ready:
            ordered.append(name)
            del remaining[name]
    return ordered


def migrate(source_path, accounts, old_root=None, new_root=None):
    source_path = Path(source_path).resolve()
    if source_path == config.SQLITE_PATH.resolve() and not config.POSTGRES_DSN:
        raise ValueError('The source and target must be different databases.')
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro&immutable=1', uri=True)
    source.row_factory = sqlite3.Row
    target = db.connect()
    counts = {}
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('The source snapshot failed its integrity check.')
        if source.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('The source snapshot has broken references.')
        skipped = {'schema_meta', 'auth_users', 'auth_sessions', 'auth_events', 'auth_login_attempts',
                   'cloud_memberships', 'cloud_jobs', 'cloud_artifacts'}
        tables = {table['name']: table for table in schema.TABLES}
        source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with target.transaction():
            for name in tables:
                if name != 'schema_meta' and target.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]:
                    raise ValueError('Cloud target already contains data; migration was refused.')
            for name in table_order():
                if name in skipped or name not in source_tables:
                    continue
                present = {row[1] for row in source.execute('PRAGMA table_info(' + name + ')')}
                columns = [(column, declaration) for column, declaration, _ in tables[name]['columns'] if column in present]
                names = [column for column, _ in columns]
                override = ' OVERRIDING SYSTEM VALUE' if target.dialect == 'postgres' and any('IDENTITY' in d for _, d in columns) else ''
                statement = 'INSERT INTO ' + name + ' (' + ','.join(names) + ')' + override + ' VALUES (' + ','.join('?' for _ in names) + ')'
                copied = 0
                cursor = source.execute('SELECT ' + ','.join(names) + ' FROM ' + name)
                while True:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break
                    values = []
                    for row in batch:
                        value = [bool(row[column]) if row[column] is not None and 'BOOLEAN' in declaration else row[column]
                                 for column, declaration in columns]
                        if old_root and new_root:
                            value = [v.replace(str(old_root), str(new_root)) if isinstance(v, str) else v for v in value]
                        values.append(value)
                    if target.dialect == 'postgres':
                        from psycopg2.extras import execute_batch
                        translated, _ = target._translate(statement, values[0])
                        with target._conn.cursor() as pg_cursor:
                            execute_batch(pg_cursor, translated, values, page_size=1000)
                    else:
                        target._conn.executemany(statement, values)
                    copied += len(batch)
                counts[name] = copied
                if target.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] != copied:
                    raise ValueError('The migrated row count differs for ' + name)
                for column, declaration in columns:
                    if target.dialect == 'postgres' and 'IDENTITY' in declaration:
                        target.execute("SELECT setval(pg_get_serial_sequence(?,?),GREATEST(COALESCE(MAX(" + column + "),0),1),MAX(" + column + ") IS NOT NULL) FROM " + name,
                                       (name, column))
            stamp = auth.now().isoformat()
            workspace_id = os.environ.get('TORI_WORKSPACE_ID', 'default')
            for user in accounts['users']:
                # The stored credential is carried across byte for byte.
                #
                # It used to be rewritten here, because the hosted function and this
                # runtime both claimed the name `scrypt-v1` with different field
                # encodings and neither could read the other's. Both now write and read
                # `scrypt-v2$N$r$p$hex$hex`, so one name means one thing and a
                # credential is portable in either direction without translation. The
                # two older shapes are still accepted, so an account provisioned before
                # the change keeps working.
                encoded = user['password_hash']
                if not (re.fullmatch(r'scrypt-v2\$\d+\$\d+\$\d+\$[a-f0-9]{32}\$[a-f0-9]{64}', encoded)
                        or re.fullmatch(r'scrypt-netlify-v1\$[a-f0-9]{32}\$[a-f0-9]{64}', encoded)
                        or re.fullmatch(r'scrypt-v1\$[a-f0-9]{32}\$[a-f0-9]{64}', encoded)):
                    raise ValueError('Unexpected source password format; credentials were not replaced.')
                if encoded.startswith('scrypt-v1$'):
                    encoded = encoded.replace('scrypt-v1$', 'scrypt-netlify-v1$', 1)
                role = user['role']
                if role not in {'developer', 'viewer'}:
                    raise ValueError('Unsupported source account role.')
                target.execute('INSERT INTO auth_users(user_id,username,password_hash,role,active,must_change_password,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                               (user['user_id'], auth.username(user['username']), encoded, role, bool(user['active']),
                                bool(user['must_change_password']), user['created_at'], stamp))
                target.execute('INSERT INTO cloud_memberships(user_id,workspace_id,role) VALUES (?,?,?)',
                               (user['user_id'], workspace_id, role))
            if not any(user['role'] == 'developer' and user['active'] for user in accounts['users']):
                raise ValueError('At least one active developer is required.')
            # Existing collection profiles retain their settings under the new
            # database identity. Historical records continue to use their IDs.
            old_key = hashlib.sha256(str((Path(old_root) / 'data/research.sqlite').resolve()).encode()).hexdigest() if old_root else None
            if old_key:
                for name in ('collection_control', 'collection_commands', 'collection_cycles'):
                    target.execute('UPDATE ' + name + ' SET collector_key=? WHERE collector_key=?', (backups.source_key(), old_key))
            target.execute("UPDATE collection_cycles SET status='interrupted',finished_at=? WHERE status='running'", (stamp,))
            target.execute("UPDATE collection_commands SET status='failed',reason='cloud_migration',applied_at=? WHERE status='pending'", (stamp,))
            if target.dialect == 'sqlite' and target.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('The migrated database has broken references.')
        return {'status': 'ok', 'tables': counts, 'accounts': len(accounts['users']), 'schema_version': schema.SCHEMA_VERSION}
    finally:
        source.close()
        target.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--accounts', type=Path, required=True)
    parser.add_argument('--old-root', type=Path)
    parser.add_argument('--new-root', type=Path)
    args = parser.parse_args()
    if not config.POSTGRES_DSN:
        parser.error('Configure TORI_PG_DSN; cloud imports must use PostgreSQL.')
    if args.accounts.stat().st_mode & 0o077:
        parser.error('Account export must be private (chmod 600).')
    manifest_path = args.database.parent / 'manifest.json'
    if not manifest_path.is_file():
        parser.error('Use a verified backup with its manifest, rather than the live database.')
    manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256()
    with args.database.open('rb') as snapshot:
        for block in iter(lambda: snapshot.read(1024*1024), b''):
            digest.update(block)
    if not manifest.get('restore_verified') or digest.hexdigest() != manifest.get('sha256'):
        parser.error('The source snapshot does not match its verified backup manifest.')
    db.init_db()
    print(json.dumps(migrate(args.database, json.loads(args.accounts.read_text()), args.old_root, args.new_root), indent=2))
