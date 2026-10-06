"""Public release identity shared by local, hosted and packaged applications."""
import hashlib
import os
from functools import lru_cache
from pathlib import Path

from . import schema

ROOT = Path(__file__).resolve().parents[1]
API_VERSION = 1


@lru_cache(maxsize=1)
def source_hash():
    digest = hashlib.sha256()
    paths = [path for path in (ROOT / 'toriresearch').rglob('*')
             if path.is_file() and path.suffix in {'.py', '.js', '.html', '.css'}]
    paths += [ROOT / 'config' / name for name in
              ('product_identity.json', 'product_profiles.json', 'vocabulary.yaml')]
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode() + b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def describe():
    return {'api_version': API_VERSION, 'schema_version': schema.SCHEMA_VERSION,
            'source_hash': source_hash(),
            'revision': os.environ.get('TORI_RELEASE_REVISION') or os.environ.get('COMMIT_REF') or 'unknown'}
