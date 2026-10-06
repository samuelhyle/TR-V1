"""Versioned product field definitions shared by extraction and presentation."""

import hashlib
import json
import math
import re

from . import config

_CACHE = {}
_FILE_KEY = None


def load():
    global _FILE_KEY
    path = config.ROOT / "config/product_profiles.json"
    stat = path.stat()
    file_key = (str(path),stat.st_mtime_ns,stat.st_size)
    if file_key == _FILE_KEY and _CACHE:
        return next(iter(_CACHE.values()))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest in _CACHE:
        _FILE_KEY = file_key
        return _CACHE[digest]
    doc = json.loads(raw)
    for key, profile in doc["profiles"].items():
        if not re.fullmatch(r"[a-z_]+", key) or set(profile["fields"]) - doc["fields"].keys():
            raise ValueError("invalid product profile")
        profile["_hints"] = [re.compile(pattern, re.I) for pattern in profile["hints"]]
    doc["sha256"] = digest
    _CACHE.clear()
    _CACHE[digest] = doc
    _FILE_KEY = file_key
    return doc


def valid_value(field, value):
    definition = load()["fields"].get(field)
    if not definition:
        return False
    kind = definition["type"]
    if kind == "boolean":
        return type(value) is bool
    if kind in {"integer", "number"}:
        return (type(value) in ({int} if kind == "integer" else {int, float})
                and math.isfinite(value) and definition["minimum"] <= value <= definition["maximum"])
    if kind == "enum":
        return isinstance(value, str) and value in definition["enum"]
    return (isinstance(value, str) and 0 < len(value) <= definition["max_length"]
            and not any(char in value for char in "\n\r<>@{}")
            and not re.search(r"https?://|ignore.{0,20}instructions|system\s*prompt", value, re.I))


def field_schema(field):
    definition = load()["fields"][field]
    schema = {"type": definition["type"] if definition["type"] != "enum" else "string"}
    if schema["type"] == "text":
        schema = {"type": "string", "minLength": 1, "maxLength": definition["max_length"]}
    for key in ("enum", "minimum", "maximum"):
        if key in definition:
            schema[key] = definition[key]
    return schema


def summary(identity=None, automatic=None, listing=None):
    """Present a profile without promoting source claims into reviewed identity."""
    identity, automatic, listing = identity or {}, automatic or {}, listing or {}
    doc = load()
    current = automatic.get('status') == 'current'
    profile_id = automatic.get('profile') if current else None
    basis = 'retained_automatic_source_analysis' if profile_id in doc['profiles'] else 'unresolved'
    if identity.get('review'):
        family = identity.get('family')
        if profile_id not in doc['profiles'] or doc['profiles'][profile_id]['family'] != family:
            choices = [key for key, profile in doc['profiles'].items() if profile['family'] == family]
            text = ' '.join(str(value or '') for value in (identity.get('model'), listing.get('title')))
            hinted = [key for key in choices if any(pattern.search(text) for pattern in doc['profiles'][key]['_hints'])]
            profile_id = hinted[0] if len(hinted) == 1 else choices[0] if len(choices) == 1 else 'unknown'
        basis = 'reviewed_source_identity' if profile_id != 'unknown' else 'reviewed_family_profile_unresolved'
    elif profile_id not in doc['profiles']:
        hinted = [key for key, profile in doc['profiles'].items() if any(pattern.search(listing.get('title') or '') for pattern in profile['_hints'])]
        profile_id = hinted[0] if len(hinted) == 1 else 'unknown'
        basis = 'provisional_title_hint' if profile_id != 'unknown' else 'unresolved'
    profile = doc['profiles'][profile_id]
    canonical = dict(identity.get('attributes') or {}) if identity.get('review') else {}
    if identity.get('review'):
        canonical.update(manufacturer=identity.get('brand'), model=identity.get('model'))
    aliases = {'storage_gb': 'storage', 'ram_gb': 'ram', 'cellular_variant': 'cellular', 'edition': 'edition'}
    fields = []
    for key in profile['fields']:
        value = canonical.get(key, canonical.get(aliases.get(key)))
        source = automatic.get('fields', {}).get(key, {}) if current and automatic.get('profile') == profile_id else {}
        reviewed = value is not None
        fields.append(dict(key=key, label=doc['fields'][key]['label'], required=key in profile['comparison_required'],
                           state='known' if reviewed else source.get('state', 'unknown'),
                           value=value if reviewed else source.get('value'),
                           basis='reviewed_source_identity' if reviewed else source.get('basis') or 'not_recorded'))
    return dict(version=doc['version'], sha256=doc['sha256'], profile_id=profile_id, label=profile['label'],
                family=profile['family'], selection_basis=basis, fields=fields,
                missing_fields=[field['key'] for field in fields if field['required'] and field['state'] not in ('known', 'explicit_no')],
                missing_reviewed_identity=list(dict.fromkeys(identity.get('missing_variant_fields', []) + identity.get('missing_comparison_fields', []))),
                comparison_supported=bool(identity.get('comparison_key') and identity.get('review')),
                note='Profile fields describe retained source evidence. They do not establish physical inspection or promote an offer into comparable inventory.')
