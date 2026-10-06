"""The bounded vocabulary, and the prompts built from it.

A deliberately minimal YAML reader, because the project takes a hard dependency
position of no third-party packages and a 40-line subset parser is cheaper to
audit than a parser plus a lockfile. It handles exactly what
`config/vocabulary.yaml` uses: scalars, `- item` lists and null. Anything else
raises rather than guessing.

The vocabulary is the security boundary and the statistical lever simultaneously,
which is why it lives in a file a human edits rather than in code. See the
comments in that file.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import config

_REQUIRED_TOP_KEYS = ("version", "product_type")
IDENTITY_VERSION = "vocabulary-semantic-v1"


def _scalar(text):
    text = text.strip()
    if text in ("null", "~", ""):
        return None
    if text in ("true", "false"):
        return text == "true"
    if text.startswith(("'", '"')) and text.endswith(("'", '"')) and len(text) > 1:
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _lines(text, legacy=False):
    """Comment-stripped, blank-free lines."""
    out = []
    for raw in text.splitlines():
        quote, skip = None, False
        for index, char in enumerate(raw if not legacy else ""):
            if skip:
                skip = False
                continue
            if quote:
                if char == "\\" and quote == '"':
                    skip = True
                elif char == quote:
                    if quote == "'" and raw[index:index+2] == "''":
                        skip = True
                    else:
                        quote = None
            elif char in ("'", '"') and (index == 0 or raw[index-1].isspace()):
                quote = char
            elif char == "#" and (index == 0 or raw[index-1].isspace()):
                raw = raw[:index]
                break
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(raw.rstrip())
    return out


def parse(text, legacy=False):
    """Parse the subset of YAML the vocabulary file uses.

    Scalar, `- item` list, and nested `key: value` mapping. A key with no inline
    value takes its shape from the next line, because `homes:` is a mapping and
    `product_type:` is a list and only the following line distinguishes them.
    """
    lines = _lines(text, legacy=legacy)
    doc = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith((" ", "\t")):
            raise ValueError(f"unexpected indent in vocabulary: {line!r}")
        if ":" not in line:
            raise ValueError(f"cannot parse vocabulary line: {line!r}")
        name, _, value = line.partition(":")
        key = name.strip()
        value = value.strip()

        if value:
            doc[key] = _scalar(value)
            i += 1
            continue

        block = []
        j = i + 1
        while j < len(lines) and lines[j].startswith((" ", "\t")):
            block.append(lines[j].strip())
            j += 1
        if not block:
            doc[key] = None
            i += 1
            continue
        if all(b.startswith("- ") or b == "-" for b in block):
            doc[key] = [_scalar(b[2:]) for b in block]
        else:
            mapping = {}
            for entry in block:
                if ":" not in entry:
                    raise ValueError(f"cannot parse vocabulary entry: {entry!r}")
                sub_key, _, sub_value = entry.partition(":")
                mapping[sub_key.strip() if legacy else _scalar(sub_key.strip())] = _scalar(sub_value)
            doc[key] = mapping
        i = j

    for required in _REQUIRED_TOP_KEYS:
        if required not in doc:
            raise ValueError(f"vocabulary is missing required key {required!r}")
    if not isinstance(doc["product_type"], list) or len(doc["product_type"]) < 2:
        raise ValueError(
            "vocabulary product_type must be a list with at least 2 entries")
    return doc


_cached = {}


def load(path=None, use_cache=True):
    """Load and validate the vocabulary. Raises on anything malformed."""
    path = Path(path or config.ENRICH_VOCAB_PATH)
    key = str(path)

    if not path.exists():
        raise FileNotFoundError(
            f"no vocabulary at {path}. The classifier cannot run without a bounded "
            "vocabulary; copy config/vocabulary.yaml and edit it to your verticals."
        )
    raw = path.read_bytes()
    raw_digest = hashlib.sha256(raw).hexdigest()
    if use_cache and key in _cached and _cached[key]["raw_sha256"] == raw_digest:
        return _cached[key]
    doc = parse(raw.decode("utf-8"))
    try:
        legacy_compatible = parse(raw.decode("utf-8"), legacy=True) == doc
    except ValueError:
        legacy_compatible = False
    types = doc["product_type"]
    if any(not isinstance(value, str) or not value.strip() for value in types):
        raise ValueError("vocabulary product_type entries must be nonempty strings")
    if len(set(types)) != len(types):
        dupes = sorted({t for t in types if types.count(t) > 1})
        raise ValueError(f"vocabulary has duplicate product_type entries: {dupes}")

    abstain = doc.get("abstain") or "unknown"
    if abstain not in types:
        raise ValueError(
            f"abstain value {abstain!r} is not in product_type; the model needs a "
            "legal way to decline"
        )
    active = doc.get("active")
    if active is not None and (not isinstance(active, list) or not active
                               or any(not isinstance(value, str) for value in active)):
        raise ValueError("vocabulary active must be a nonempty list of product types")
    if active:
        missing = [a for a in active if a not in types]
        if missing:
            raise ValueError(f"vocabulary active list has unknown entries: {missing}")
        if abstain not in active or len(set(active)) != len(active):
            raise ValueError("vocabulary active must contain abstain and no duplicates")

    homes = doc.get("homes") or {}
    if not isinstance(homes, dict):
        raise ValueError("vocabulary `homes` must be a mapping of product_type to "
                         "platform top-level category")
    orphaned = [t for t in homes if t not in types]
    if orphaned:
        raise ValueError(f"vocabulary homes has unknown product_type keys: {orphaned}")
    if any(not isinstance(value, str) or not value.strip() for value in homes.values()):
        raise ValueError("vocabulary homes must contain nonempty category labels")

    # A type with no home cannot be accepted as a peer group, and that is correct
    # for a catch-all. It has to be *declared*, because a missing home is
    # otherwise indistinguishable from an oversight, and an oversight silently
    # removes a whole vertical from analysis with a reason code that blames the
    # vocabulary rather than the configuration.
    unhomed = doc.get("unhomed") or []
    if not isinstance(unhomed, list) or any(not isinstance(value, str) for value in unhomed):
        raise ValueError("vocabulary unhomed must be a list of product types")
    unknown_unhomed = [t for t in unhomed if t not in types]
    if unknown_unhomed:
        raise ValueError(f"vocabulary unhomed has unknown entries: {unknown_unhomed}")
    contradiction = sorted(set(unhomed) & set(homes))
    if contradiction:
        raise ValueError(f"vocabulary unhomed types also declare a home: {contradiction}")

    out = {
        "version": doc["version"],
        "abstain": abstain,
        "product_type": list(types),
        "allowed": [a for a in (active or types) if a in types],
        "homes": homes,
        "unhomed": list(unhomed),
        "raw_sha256": raw_digest,
        "identity_version": IDENTITY_VERSION,
        "legacy_compatible": legacy_compatible,
        "path": str(path),
    }
    # Ordering of allowed values can affect decoding, so lists retain order.
    # Mapping layout, whitespace and full-line comments do not change meaning.
    semantic = {name: out[name] for name in
                ("identity_version", "version", "abstain", "product_type", "allowed", "homes", "unhomed")}
    out["semantic_json"] = json.dumps(semantic, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    out["sha256"] = hashlib.sha256(out["semantic_json"].encode("utf-8")).hexdigest()
    if use_cache:
        _cached[key] = out
    return out


def remember(conn, doc=None):
    """Record a proven raw/semantic relationship inside the caller's transaction."""
    doc = doc or load()
    conn.execute("INSERT INTO enrichment_vocabularies "
                 "(raw_sha256,semantic_sha256,identity_version,legacy_compatible,semantic_json,observed_at) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(raw_sha256,semantic_sha256) DO NOTHING",
                 (doc["raw_sha256"], doc["sha256"], IDENTITY_VERSION, doc["legacy_compatible"], doc["semantic_json"],
                  datetime.now(timezone.utc).isoformat()))


def compatible_versions(doc=None, conn=None):
    """Only byte hashes whose meaning was actually observed as identical are valid.

    Reads never register a version. The currently parsed file proves its own
    legacy hash; reconciliation durably remembers it before migration begins.
    Older, different vocabularies are never assumed compatible.
    """
    from .. import db
    doc = doc or load()
    sql = "SELECT raw_sha256 FROM enrichment_vocabularies WHERE semantic_sha256=? AND identity_version=? AND legacy_compatible=TRUE"
    params = (doc["sha256"], IDENTITY_VERSION)
    records = conn.execute(sql, params).fetchall() if conn is not None else db.query(sql, params)
    return tuple(dict.fromkeys([doc["sha256"][:12], *([doc["raw_sha256"][:12]] if doc["legacy_compatible"] else []),
                               *(row["raw_sha256"][:12] for row in records)]))


def coverage():
    """How settled the vocabulary is. A short list concentrates statistical power."""
    doc = load()
    return {
        "version": doc["version"],
        "product_types": len(doc["allowed"]),
        "abstain": doc["abstain"],
        "sha256": doc["sha256"][:16],
        "raw_sha256": doc["raw_sha256"][:16],
        "semantic_sha256": doc["sha256"],
        "identity_version": IDENTITY_VERSION,
        "implementation_sha256": implementation_digest(),
        "active_subset": len(doc["allowed"]) != len(doc["product_type"]),
        "note": (
            "Every value the model may emit comes from this list. It is the "
            "prompt-injection boundary and the reason n concentrates per cell."
        ),
    }


def implementation_digest():
    """Fingerprint the code producing and accepting these classifications."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for name in ("analysis.py", "enrich/vocabulary.py", "enrich/tasks.py", "enrich/queue.py", "enrich/runner.py"):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()
