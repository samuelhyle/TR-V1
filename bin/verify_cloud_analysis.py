#!/usr/bin/env python3
"""Compare a full SQLite backup with its PostgreSQL import at one research clock.

This is a read-only migration rehearsal, not a model fit. Credentials are read
from TORI_PG_DSN and never written to the proof. Comparisons cover complete
analysis/focus/opportunity reports and all five stored model families.
"""
import argparse
import json
import math
import re
import sqlite3
import sys
import time
from contextlib import ExitStack
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toriresearch import (analysis, cloud_migration, collection, config, dashboard, db,
                         forecast_evidence, market_intel, metrics, model_workspace,
                         opportunities, outcome_audit, product_identity, research_desk, schema)  # noqa: E402
from toriresearch.ingest import sweep  # noqa: E402
from toriresearch.predict import _common, _linkage  # noqa: E402

STORED_PRECISIONS = {}
for table in schema.TABLES:
    for name, kind, *_ in table['columns']:
        if match := re.search(r'NUMERIC\(\d+,(\d+)\)', kind):
            STORED_PRECISIONS[name] = max(STORED_PRECISIONS.get(name, 0), int(match[1]))


def differences(left, right, path='$', limit=20):
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                result.append(path + '.' + key + ': missing field')
            else:
                result.extend(differences(left[key], right[key], path + '.' + key, limit))
            if len(result) >= limit:
                break
        return result[:limit]
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        if len(left) != len(right):
            return [path + ': different row count']
        result = []
        for index, (a, b) in enumerate(zip(left, right)):
            result.extend(differences(a, b, path + '[' + str(index) + ']', limit))
            if len(result) >= limit:
                break
        return result[:limit]
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        # PostgreSQL REAL is float32, and monetary NUMERIC fields have declared
        # decimal precision. Compare reports to that precision, not binary identity.
        field = path.rsplit('.', 1)[-1]
        precision = STORED_PRECISIONS.get(field)
        if precision is None and field.startswith('mean_'):
            # An average inherits the quantization error of its stored inputs.
            precision = STORED_PRECISIONS.get(field.removeprefix('mean_'))
        absolute = .5 * 10**-precision + 1e-12 if precision is not None else 1e-6
        relative = 0 if precision is not None else 1e-6
        return [] if math.isclose(left, right, rel_tol=relative, abs_tol=absolute) else [path + ': numeric mismatch']
    return [] if left == right else [path + ': value mismatch']


def normalized(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(item) for item in value]
    return value


def compare(bundle, runtime_root):
    manifest = cloud_migration.verify(bundle)
    target_dsn = config.POSTGRES_DSN
    if not target_dsn:
        raise ValueError('Configure the isolated PostgreSQL target with TORI_PG_DSN.')
    reference = datetime.now(timezone.utc)

    class ResearchClockMeta(type):
        def __instancecheck__(cls, value):
            # PostgreSQL returns real datetime instances; SQLite returns text.
            return isinstance(value, datetime)

    class ResearchClock(datetime, metaclass=ResearchClockMeta):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(reference.timestamp(), tz)

    modules = (analysis, metrics, collection, model_workspace, opportunities, outcome_audit, product_identity, research_desk,
               forecast_evidence, market_intel, sweep, _common, _linkage)
    readers = [(name + '/' + market, lambda market=market, name=name: {
        'analysis': analysis.snapshot, 'focus': analysis.focus_breakdown,
        'opportunities': opportunities.board}[name]({'platform': 'tori', 'subplatform': market}))
        for market in ('standard', 'diili') for name in ('analysis', 'focus', 'opportunities')]
    readers += [('models/' + family, lambda family=family: model_workspace.report({'family': family}))
                for family in model_workspace.FAMILIES]
    skipped = {'schema_meta', 'auth_users', 'auth_sessions', 'auth_events', 'auth_login_attempts',
               'cloud_memberships', 'cloud_jobs', 'cloud_artifacts'}
    imported = {table['name'] for table in schema.TABLES} - skipped
    counts = {table: db.scalar('SELECT COUNT(*) FROM ' + table) for table in sorted(imported)
              if table in manifest['database_counts']}
    count_mismatches = [table for table, count in counts.items() if count != manifest['database_counts'][table]]
    report = {'reference_at': reference.isoformat(), 'checks': [], 'matched': not count_mismatches,
              'numeric_comparison': {'relative_tolerance': 1e-6, 'default_absolute_tolerance': 1e-6,
                                     'mean_fields_inherit_stored_input_precision': True,
                                     'stored_decimal_places': STORED_PRECISIONS},
              'table_counts': counts, 'count_mismatches': count_mismatches,
              'excluded_source_tables': sorted(set(manifest['database_counts']) - imported)}
    def immutable_source():
        connection = sqlite3.connect((Path(bundle) / 'database/research.sqlite').resolve().as_uri()
                                     + '?mode=ro&immutable=1', uri=True)
        connection.row_factory = sqlite3.Row
        return db._Connection(connection, 'sqlite')

    with ExitStack() as stack:
        stack.enter_context(patch.object(db, '_open_sqlite', immutable_source))
        for module in modules:
            if getattr(module, 'datetime', None) is datetime:
                stack.enter_context(patch.object(module, 'datetime', ResearchClock))
        for name, reader in readers:
            outputs, timings = [], []
            for source in ('sqlite', 'postgres'):
                root = Path(bundle) / 'state' if source == 'sqlite' else Path(runtime_root)
                config.POSTGRES_DSN = None if source == 'sqlite' else target_dsn
                config.SQLITE_PATH = Path(bundle) / 'database/research.sqlite'
                config.DATA_DIR, config.REPORTS_DIR = root / 'data', root / 'reports'
                dashboard.invalidate()
                started = time.monotonic()
                with db.read_snapshot():
                    value = reader()
                outputs.append(normalized(value))
                timings.append(round(time.monotonic() - started, 3))
            delta = differences(*outputs)
            report['checks'].append({'name': name, 'matched': not delta,
                                     'seconds': dict(zip(('sqlite', 'postgres'), timings)),
                                     'differences': delta})
            report['matched'] &= not delta
            print(json.dumps(report['checks'][-1]), flush=True)
    config.POSTGRES_DSN = target_dsn
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    proof = compare(args.bundle, args.runtime_root)
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.report.write_text(json.dumps(proof, indent=2) + '\n')
    args.report.chmod(0o600)
    return 0 if proof['matched'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
