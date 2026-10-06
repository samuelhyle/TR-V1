#!/usr/bin/env python3
"""Package, verify or restore a private full-data cloud migration."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toriresearch import cloud_migration, config  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pack = commands.add_parser('pack')
    pack.add_argument('--backup', type=Path, required=True)
    pack.add_argument('--accounts', type=Path, required=True)
    pack.add_argument('--output', type=Path, required=True)
    pack.add_argument('--data-dir', type=Path, default=config.DATA_DIR)
    pack.add_argument('--reports-dir', type=Path, default=config.REPORTS_DIR)
    pack.add_argument('--env-file', type=Path, default=config.ROOT / 'config/local.env')
    pack.add_argument('--site-id', required=True)
    pack.add_argument('--origin', required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--bundle', type=Path, required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--bundle', type=Path, required=True)
    restore.add_argument('--runtime-root', type=Path, required=True)
    restore.add_argument('--site-id', required=True)
    restore.add_argument('--origin', required=True)
    args = vars(parser.parse_args())
    command = args.pop('command')
    result = getattr(cloud_migration, 'package' if command == 'pack' else command)(**args)
    if command in {'pack', 'verify'}:
        result = {key: result[key] for key in ('version', 'created_at', 'target', 'database_counts', 'bytes')}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
