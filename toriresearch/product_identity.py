"""Versioned, bounded product identities and an explicit human-review pilot.

Rules describe what current listing text says. They do not establish a unit's
condition or turn a model name into a price comparable. Inspection fields stay
unknown until an operator records them. Derivations and reviews use enrichment,
never overwrite source observations, and expire when identity inputs change.
"""

import copy
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, metrics, schema

RULE_VERSION = "product-identity-rules-v3"
COMPARISON_POLICY_VERSION = "product-comparison-policy-v1"
REVIEW_VERSION = "product-identity-review-v2"
TASK = "product_identity"
REVIEW_TASK = "product_identity_review"
INPUT_FIELDS = ("title", "brand_norm", "model_norm", "category_norm", "category_root_norm", "condition_norm")
INSPECTION_FIELDS = ("functionality", "completeness", "defects", "offer_kind", "quantity")
ENUMS = {
    "edition": {"disc", "digital"},
    "mount": {"canon-ef", "canon-rf", "sony-fe"},
    "functionality": {"working", "defective"},
    "completeness": {"complete", "incomplete"},
    "defects": {"none_disclosed", "disclosed"},
    "offer_kind": {"single_unit", "bundle", "multiple_units", "accessory"},
}
ATTRIBUTES = set(ENUMS) | {"storage", "hardware_code", "battery_health_pct", "quantity"}
_CACHE = {}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _fold(value):
    return re.sub(r"\W+", "", str(value or "").casefold())


def _validate_attribute(key, value):
    if key not in ATTRIBUTES:
        raise ValueError("unsupported identity attribute")
    if value is None:
        return None
    if key in ENUMS:
        if not isinstance(value, str) or value not in ENUMS[key]:
            raise ValueError(f"invalid {key}")
    elif key in ("quantity", "battery_health_pct"):
        lower, upper = (1, 100) if key == "quantity" else (0, 100)
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f"invalid {key}")
    elif key == "hardware_code":
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,31}", value):
            raise ValueError("invalid hardware_code")
    elif key == "storage":
        if not isinstance(value, str) or not re.fullmatch(r"(?:[1-9]\d{0,3}GB|[1-8]TB)", value):
            raise ValueError("invalid storage")
    return value


def load(path=None):
    """Raw hashes refresh mappings; reviews bind source facts and model meaning."""
    path = Path(path or config.ROOT / "config/product_identity.json")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    key = str(path.resolve())
    if key in _CACHE and _CACHE[key]["sha256"] == digest:
        return _CACHE[key]
    doc = json.loads(raw)
    if not isinstance(doc, dict) or not isinstance(doc.get("version"), str) or not doc["version"]:
        raise ValueError("identity catalog needs a version")
    families = doc.get("families")
    if not isinstance(families, list) or not 1 <= len(families) <= 100:
        raise ValueError("identity catalog needs 1–100 families")
    by_family, by_model = {}, {}
    for family in families:
        if not isinstance(family, dict) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", family.get("id", "")):
            raise ValueError("invalid identity family")
        if family["id"] in by_family:
            raise ValueError("duplicate identity family")
        for field in ("label", "product_type", "home"):
            if not isinstance(family.get(field), str) or not family[field]:
                raise ValueError("identity family metadata is missing")
        checks = family.get("comparison_fields", [])
        if not isinstance(checks, list) or set(checks) - ATTRIBUTES:
            raise ValueError("invalid comparison fields")
        hints = family.get("hints")
        if not isinstance(hints, list) or not hints or any(not isinstance(x, str) or not x for x in hints):
            raise ValueError("identity family needs bounded hints")
        family["_hints"] = [re.compile(pattern, re.IGNORECASE) for pattern in hints]
        models = family.get("models")
        if not isinstance(models, list) or not models:
            raise ValueError("identity family needs a model catalog")
        for model in models:
            if not isinstance(model, dict) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", model.get("id", "")):
                raise ValueError("invalid model key")
            if model["id"] in by_model:
                raise ValueError("duplicate model key")
            for field in ("brand", "label"):
                if not isinstance(model.get(field), str) or not model[field]:
                    raise ValueError("identity model metadata is missing")
            fields = model.get("variant_fields")
            if not isinstance(fields, list) or not fields or set(fields) - ATTRIBUTES:
                raise ValueError("invalid variant fields")
            values = model.get("attributes", {})
            if not isinstance(values, dict):
                raise ValueError("invalid catalog attributes")
            for field, value in values.items():
                _validate_attribute(field, value)
            aliases = model.get("aliases")
            if not isinstance(aliases, list) or not aliases or any(not isinstance(x, str) or not x for x in aliases):
                raise ValueError("identity model needs aliases")
            model["_aliases"] = [re.compile(pattern, re.IGNORECASE) for pattern in aliases]
            model["_family"] = family["id"]
            by_model[model["id"]] = model
        by_family[family["id"]] = family
    result = dict(version=doc["version"], sha256=digest, families=by_family, models=by_model)
    _CACHE[key] = result
    return result


def input_digest(row, catalog=None):
    catalog = catalog or load()
    return _hash(dict(rule_version=RULE_VERSION, catalog_sha256=catalog["sha256"],
                      inputs=source_inputs(row)))


def source_inputs(row):
    inputs = {key: row.get(key) for key in INPUT_FIELDS}
    if row.get("product_attribute_facts_sha256"):
        inputs["product_attribute_facts_sha256"] = row["product_attribute_facts_sha256"]
    return inputs


def fact_digest(row):
    """Source-bound human evidence survives unrelated catalog and rule edits."""
    return _hash(dict(platform=row.get("platform"), listing_id=row.get("listing_id"),
                      inputs=source_inputs(row)))


def model_semantics(expected, catalog):
    model = catalog["models"].get(expected["model_key"])
    return _hash(dict(family=expected["family"], model=None if model is None else
                      {key: model[key] for key in ("id", "brand", "attributes", "_family")}))


def attribute_matrix(catalog=None):
    catalog = catalog or load()
    return {key: dict(identity_critical_by_model={model["id"]: list(model["variant_fields"])
                                                for model in family["models"]},
                      offer_adjustments=["condition"]+family["comparison_fields"],
                      purchase_checks=list(INSPECTION_FIELDS)+["condition"]+family["comparison_fields"],
                      purchase_identity_checks_by_model={model["id"]: ["model_key"]+list(model["variant_fields"])
                                                         for model in family["models"]},
                      policy_version=COMPARISON_POLICY_VERSION, status="provisional",
                      rationale="Required hardware codes remain distinct until reviewed equivalence evidence exists. Battery percentages are preserved; no unvalidated quality bands or price adjustments are applied.")
            for key, family in catalog["families"].items()}


def _levels(result, catalog):
    family = catalog["families"].get(result["family"])
    model_research = bool(result["model_key"] and not result["conflicts"])
    variant_context = bool(result["variant_key"] and result["status"] != "EXCLUDED" and not result["flags"])
    comparable = bool(result["comparison_key"])
    model = catalog["models"].get(result["model_key"])
    checks = list(dict.fromkeys(["model_key"]+(model["variant_fields"] if model else [])+
                               list(INSPECTION_FIELDS)+["condition"]+(family["comparison_fields"] if family else [])))
    verification = (result.get("review") or {}).get("verification", {})
    missing_inspection = [key for key in checks if verification.get(key) != "physical_inspection"]
    physical = comparable and not missing_inspection
    result.update(comparison_policy_version=COMPARISON_POLICY_VERSION,
                  model_research_key=_hash(dict(family=result["family"], model_key=result["model_key"])) if model_research else None,
                  comparison_level="purchase_eligible" if physical else "reviewed_comparable_offer" if comparable
                  else "variant_asking_price_context" if variant_context else "model_research" if model_research else "unsupported",
                  comparison_levels=dict(model_research=model_research, variant_asking_price_context=variant_context,
                                         reviewed_comparable_offer=comparable, purchase_eligible=physical),
                  purchase_identity_eligible=physical, missing_physical_checks=missing_inspection,
                  requires_physical_inspection=not physical,
                  comparison_level_scope="Product identity and offer evidence only; purchase eligibility also requires price, full costs, resale/outcome evidence and resource checks.")
    return result


def _evidence(row, field, match=None, value=None):
    try:
        sources = json.loads(row.get("field_sources_json") or "{}")
    except (TypeError, ValueError):
        sources = {}
    source = sources.get(field) if isinstance(sources, dict) else None
    text = str(row.get(field) or "")
    observed = metrics.parse_ts(row.get("source_observed_at"))
    return dict(field=field, source=source, source_observed_at=observed.astimezone(timezone.utc).isoformat() if observed else None,
                value=value, input_sha256=hashlib.sha256(text.encode()).hexdigest(),
                span=list(match.span()) if match is not None else None,
                basis="seller_text" if field in ("title", "model_norm") else "stored_observation")


def _finish(result, catalog):
    model = catalog["models"].get(result["model_key"])
    attrs = result["attributes"]
    result["variant_key"] = result["comparison_key"] = None
    result["missing_variant_fields"] = []
    result["missing_comparison_fields"] = []
    if not result["candidate_families"]:
        result["status"] = "OUTSIDE_PILOT"
        return _levels(result, catalog)
    if result["conflicts"]:
        result["status"] = "AMBIGUOUS"
        return _levels(result, catalog)
    family = catalog["families"].get(result["family"])
    if not model:
        result["missing_variant_fields"] = ["model_key"]
        result["status"] = "INCOMPLETE"
        return _levels(result, catalog)
    result["missing_variant_fields"] = [key for key in model["variant_fields"] if attrs.get(key) is None]
    if not result["missing_variant_fields"]:
        result["variant_key"] = _hash(dict(policy_version=COMPARISON_POLICY_VERSION, family=result["family"],
                                          model_semantics=model_semantics(result, catalog), variant_fields=model["variant_fields"],
                                          model_key=model["id"],
                                          attributes={key: attrs[key] for key in model["variant_fields"]}))
    required = ["condition"] + list(INSPECTION_FIELDS) + family["comparison_fields"]
    result["missing_comparison_fields"] = [key for key in required if attrs.get(key) is None]
    excluded = (attrs.get("offer_kind") in ("accessory", "bundle", "multiple_units")
                or attrs.get("quantity") not in (None, 1) or attrs.get("functionality") == "defective"
                or attrs.get("completeness") == "incomplete" or attrs.get("defects") == "disclosed"
                or attrs.get("condition") == "for_parts")
    if excluded:
        result["status"] = "EXCLUDED"
    elif result["variant_key"] and not result["missing_comparison_fields"]:
        result["status"] = "COMPARABLE"
        result["comparison_key"] = _hash(dict(variant_key=result["variant_key"],
                                              offer={key: attrs[key] for key in required}))
    else:
        result["status"] = "IDENTIFIED" if result["variant_key"] else "INCOMPLETE"
    return _levels(result, catalog)


def derive(row, catalog=None):
    """Pure current-text derivation. Missing words cannot imply inspection facts."""
    catalog = catalog or load()
    title = str(row.get("title") or "")
    model_text = str(row.get("model_norm") or "")
    result = dict(rule_version=RULE_VERSION, catalog_version=catalog["version"],
                  catalog_sha256=catalog["sha256"], input_sha256=input_digest(row, catalog),
                  family=None, candidate_families=[], model_key=None, brand=None, model=None,
                  product_type=None, attributes={key: None for key in ATTRIBUTES | {"condition"}},
                  evidence={}, conflicts=[], flags=[], basis="local_rules", review=None,
                  requires_physical_inspection=True, pilot_scope=False)
    candidates = set()
    matches = []
    for family in catalog["families"].values():
        if any(pattern.search(title) or pattern.search(model_text) for pattern in family["_hints"]):
            candidates.add(family["id"])
        for model in family["models"]:
            for field, text in (("title", title), ("model_norm", model_text)):
                hits = [(match, model) for pattern in model["_aliases"] for match in pattern.finditer(text)]
                # Keep the longest identifier at an overlapping span, e.g. Pro
                # Max rather than a normal iPhone named inside its identifier.
                matches.extend((field, match, entry) for match, entry in hits)
    chosen = []
    for field, match, model in sorted(matches, key=lambda hit: -(hit[1].end()-hit[1].start())):
        if any(field == f and match.start() < m.end() and m.start() < match.end() for f, m, _ in chosen):
            continue
        chosen.append((field, match, model))
        candidates.add(model["_family"])
    result["candidate_families"] = sorted(candidates)
    result["pilot_scope"] = bool(candidates)
    if len(candidates) == 1:
        result["family"] = next(iter(candidates))
        result["product_type"] = catalog["families"][result["family"]]["product_type"]
    elif candidates:
        result["conflicts"].append("multiple_families")
    keys = {model["id"] for _, _, model in chosen}
    if len(keys) > 1:
        result["conflicts"].append("multiple_models")
    elif chosen:
        field, match, model = chosen[0]
        result.update(model_key=model["id"], brand=model["brand"], model=model["label"])
        result["evidence"]["model_key"] = _evidence(row, field, match, model["id"])
        for key, value in model["attributes"].items():
            result["attributes"][key] = value
            result["evidence"][key] = _evidence(row, field, match, value)
        if row.get("brand_norm") and _fold(row["brand_norm"]) != _fold(model["brand"]):
            result["conflicts"].append("recorded_brand_conflict")
    if result["family"]:
        family = catalog["families"][result["family"]]
        root = row.get("category_root_norm")
        if row.get("platform") == "tori" and root and _fold(root) != _fold(family["home"]):
            result["conflicts"].append("category_conflict")
    condition = row.get("condition_norm")
    if condition in schema.ENUM_CONDITION and condition != "unknown":
        result["attributes"]["condition"] = condition
        result["evidence"]["condition"] = _evidence(row, "condition_norm", value=condition)
    patterns = {
        "storage": re.compile(r"\b([1-9]\d{0,3})\s*(GB|TB)\b", re.IGNORECASE),
        "hardware_code": re.compile(r"\b(A\d{4}|SM-S\d{3}[A-Z0-9]*|C(?:UH|FI)-?\d{4}[A-Z]?|(?:HAC|HDH|HEG)-\d{3})\b", re.IGNORECASE),
        "quantity": re.compile(r"\b([1-9]\d?)\s*(?:kpl|pcs|st)\b", re.IGNORECASE),
        "battery_health_pct": re.compile(r"\b(?:akun\s+kunto|battery\s+health|batterihälsa)\s*:?\s*(100|[1-9]?\d)\s*%", re.IGNORECASE),
    }
    for key, pattern in patterns.items():
        hits = list(pattern.finditer(title))
        values = {(match.group(1)+match.group(2).upper() if key == "storage" else
                   match.group(1).upper() if key == "hardware_code" else int(match.group(1))) for match in hits}
        if len(values) > 1:
            result["conflicts"].append("multiple_"+key)
        elif hits:
            value = next(iter(values))
            result["attributes"][key] = value
            result["evidence"][key] = _evidence(row, "title", hits[0], value)
    editions = [(value, match) for value, pattern in
                (("digital", r"\b(?:digital|digitaalinen)\b"),
                 ("disc", r"\b(?:disc|disk|levyasemallinen|levyasema)\b"))
                if (match := re.search(pattern, title, re.IGNORECASE))]
    if len(editions) > 1:
        result["conflicts"].append("multiple_edition")
    elif editions:
        value, match = editions[0]
        result["attributes"]["edition"] = value
        result["evidence"]["edition"] = _evidence(row, "title", match, value)
    if re.search(r"\b(?:kuori|kotelo|suojalasi|case|cover|ohjain|controller|adapter|sovitin)\b", title, re.IGNORECASE):
        result["flags"].append("inspect_accessory_or_bundle")
    if re.search(r"\b(?:paketti|bundle|setti|kit)\b", title, re.IGNORECASE):
        result["flags"].append("inspect_offer_contents")
    source_attributes = row.get("product_attribute_evidence")
    if source_attributes:
        from . import product_attributes
        result["source_attributes"] = source_attributes
        result["conflicts"].extend("source_conflict:"+field for field in source_attributes["conflicts"])
        source_model = catalog["models"].get(source_attributes["values"].get("model_key"))
        if source_model and not result["model_key"]:
            if result["family"] and result["family"] != source_model["_family"]:
                result["conflicts"].append("source_conflict:family")
            else:
                family = catalog["families"][source_model["_family"]]
                result.update(model_key=source_model["id"], brand=source_model["brand"], model=source_model["label"],
                              family=family["id"], product_type=family["product_type"], pilot_scope=True)
                result["candidate_families"] = sorted(set(result["candidate_families"]) | {family["id"]})
                result["attributes"].update(source_model["attributes"])
                result["evidence"]["model_key"] = dict(basis="unverified_seller_assertion", value=source_model["id"],
                        assertions=[entry for entry in source_attributes["assertions"] if entry["attribute"] == "model_key"])
        for field, value in source_attributes["values"].items():
            if field == "model_key":
                if result["model_key"] and value != result["model_key"]:
                    result["conflicts"].append("source_conflict:model_key")
                continue
            if field not in product_attributes.IDENTITY_FIELDS or not result["family"]:
                continue
            old = result["attributes"].get(field)
            if old is not None and old != value:
                result["conflicts"].append("source_conflict:"+field)
            else:
                try:
                    value = value if field == "condition" else _validate_attribute(field, value)
                except ValueError:
                    result["flags"].append("unsupported_source_attribute:"+field)
                    continue
                result["attributes"][field] = value
                result["evidence"][field] = dict(basis="unverified_seller_assertion",
                        value=value, assertions=[entry for entry in source_attributes["assertions"] if entry["attribute"] == field])
        if (source_attributes["values"].get("functionality") == "defective"
                or source_attributes["values"].get("offer_kind")
                or source_attributes["values"].get("quantity", 1) > 1
                or any(entry["attribute"] in {"defect_type","missing_contents"} for entry in source_attributes["assertions"])):
            result["flags"].append("inspect_disclosed_damage_or_contents")
    return _finish(result, catalog)


def _expected(value, catalog):
    if not isinstance(value, dict) or set(value) != {"family", "model_key", "attributes"}:
        raise ValueError("expected identity needs family, model_key and attributes")
    family, key = value["family"], value["model_key"]
    if family is not None and (not isinstance(family, str) or family not in set(catalog["families"]) | {"outside_pilot"}):
        raise ValueError("unknown reviewed family")
    model = catalog["models"].get(key) if isinstance(key, str) else None
    if key is not None and (not model or model["_family"] != family):
        raise ValueError("reviewed model does not belong to the family")
    attrs = value["attributes"]
    if not isinstance(attrs, dict) or set(attrs)-ATTRIBUTES-{"condition"}:
        raise ValueError("unsupported reviewed attributes")
    clean = {field: None for field in ATTRIBUTES | {"condition"}}
    for field, item in attrs.items():
        if field == "condition":
            if item is not None and item not in schema.ENUM_CONDITION:
                raise ValueError("invalid reviewed condition")
            clean[field] = None if item == "unknown" else item
        else:
            clean[field] = _validate_attribute(field, item)
    if model:
        for field, item in model["attributes"].items():
            if clean[field] is not None and clean[field] != item:
                raise ValueError("reviewed attribute conflicts with the model alias")
    return dict(family=family, model_key=key, attributes=clean)


def _apply_review(prediction, record, catalog):
    result = copy.deepcopy(prediction)
    expected = _expected(record["expected"], catalog)
    model = catalog["models"].get(expected["model_key"])
    family = expected["family"] if expected["family"] in catalog["families"] else None
    candidates = ([family] if family else [] if expected["family"] == "outside_pilot" else prediction["candidate_families"])
    result.update(family=family, candidate_families=candidates,
                  pilot_scope=bool(prediction.get("pilot_scope") or prediction["candidate_families"] or family),
                  model_key=expected["model_key"], attributes=expected["attributes"], conflicts=[], flags=[],
                  brand=model["brand"] if model else None, model=model["label"] if model else None,
                  product_type=catalog["families"][family]["product_type"] if family else None,
                  basis="operator_review", review={key: record[key] for key in
                      ("review_id", "review_source", "reviewed_at", "verification", "source", "review_outcome", "purpose") if key in record},
                  requires_physical_inspection=record["review_source"] != "physical_inspection")
    result["evidence"] = {field: dict(basis="operator_review", review_id=record["review_id"],
                                     verification=record.get("verification", {}).get(field, "legacy_unverified"),
                                     review_source=record["review_source"], value=item)
                          for field, item in expected["attributes"].items() if item is not None}
    result["evidence"]["model_key"] = dict(basis="operator_review", value=expected["model_key"],
                                           review_id=record["review_id"],
                                           verification=record.get("verification", {}).get("model_key", "legacy_unverified"))
    return _finish(result, catalog)


def _reviews(rows, catalog):
    current, stale = {}, 0
    next_row = {row["listing_pk"]: row for row in rows}
    digests = {row["listing_pk"]: input_digest(row, catalog) for row in rows}
    keys = list(digests)
    for offset in range(0, len(keys), 500):
        batch = keys[offset:offset+500]
        entries = db.query("SELECT listing_pk, output_json FROM enrichment WHERE task=? AND listing_pk IN ("+
                           ",".join("?" for _ in batch)+") ORDER BY observed_at DESC, enrichment_pk DESC",
                           [REVIEW_TASK]+batch)
        for entry in entries:
            try:
                record = json.loads(entry["output_json"])
                row = next_row[entry["listing_pk"]]
                if record.get("review_version") == REVIEW_VERSION:
                    valid = (record.get("source_input_sha256") == fact_digest(row)
                             and record.get("model_semantics_sha256") == model_semantics(record["expected"], catalog))
                else:
                    valid = (record["catalog_sha256"] == catalog["sha256"] and record["rule_version"] == RULE_VERSION
                             and record["input_sha256"] == digests[entry["listing_pk"]])
                if not valid:
                    stale += 1
                    continue
                _expected(record["expected"], catalog)
                if record["review_source"] not in ("listing_review", "physical_inspection"):
                    raise ValueError("invalid review source")
                current.setdefault(entry["listing_pk"], record)
            except (KeyError, TypeError, ValueError):
                stale += 1
    return current, stale


def _scoped(prediction, product_type, catalog):
    """An accepted pilot type can require inspection without inventing identity."""
    if prediction["pilot_scope"]:
        return prediction
    family = next((key for key,value in catalog["families"].items() if value["product_type"] == product_type),None)
    if family:
        prediction.update(pilot_scope=True, family=family, candidate_families=[family], product_type=product_type)
        prediction["flags"].append("scope_from_accepted_product_type")
        return _finish(prediction,catalog)
    return prediction


def resolve_rows(rows, catalog=None, product_types=None):
    """Current identities, with only current operator reviews applied."""
    catalog = catalog or load()
    with db.read_snapshot():
        from . import product_attributes, listing_analysis
        rows = product_attributes.attach(rows)
        if product_types is None:
            from . import analysis
            product_types = analysis.accepted_product_types(rows)
        reviews, _ = _reviews(rows, catalog)
        rows = listing_analysis.attach(rows)
        resolved = {row["listing_pk"]: _scoped((_apply_review(pred, reviews[row["listing_pk"]], catalog)
                                            if row["listing_pk"] in reviews else pred),
                                           product_types.get(row["listing_pk"]),catalog)
                for row in rows for pred in [derive(row, catalog)]}
        for row in rows:
            if row["listing_analysis"]["status"] != "not_analyzed":
                resolved[row["listing_pk"]]["listing_analysis"] = row["listing_analysis"]
        return resolved


def _stored_rows(filters=None):
    from . import analysis
    where, values, applied = metrics.listing_filters(**(filters or {"platform": "tori", "subplatform": "standard"}))
    clause = " WHERE " + " AND ".join(where) if where else ""
    from . import product_attributes
    return product_attributes.attach(db.query("SELECT " + analysis.FIELDS + ", canonical_item_id, relist_lineage_id FROM listings"+clause+" ORDER BY listing_pk", values)), applied


def _store(row, task, digest, output, catalog, conn):
    model = "local_rules" if task == TASK else "operator_review" if task == REVIEW_TASK else "local_audit"
    basis = ("Uncalibrated bounded rules" if task == TASK else "Operator supplied review" if task == REVIEW_TASK
             else "Frozen audit or grouping metadata; no reviewed label inferred")
    conn.execute("INSERT INTO enrichment (listing_pk,platform,listing_id,task,input_sha256,model,provider,"
                 "prompt_version,schema_version,output_json,confidence_basis,validation,observed_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (platform,listing_id,task,input_sha256) DO NOTHING",
                 (row["listing_pk"], row["platform"], row["listing_id"], task, digest,
                  model, "local", RULE_VERSION, catalog["sha256"][:12], _json(output), basis,
                  "bounded_identity", datetime.now(timezone.utc).isoformat()))


def backfill(filters=None, limit=5000, after=0):
    """Bounded local reprocessing with a resumable cursor. No collection/model calls."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000:
        raise ValueError("identity limit must be between 1 and 100000")
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ValueError("identity cursor must be nonnegative")
    from . import analysis, product_attributes
    catalog = load()
    where, values, applied = metrics.listing_filters(**(filters or {"platform": "tori", "subplatform": "standard"}))
    where.append("listing_pk>?")
    rows = db.query("SELECT "+analysis.FIELDS+" FROM listings WHERE "+" AND ".join(where)+
                    " ORDER BY listing_pk LIMIT ?", values+[after, limit+1])
    rows = product_attributes.attach(rows)
    result = dict(filters=applied, rule_version=RULE_VERSION, catalog_version=catalog["version"],
                  scanned=0, stored=0, cached=0, outside_pilot=0, stale_inputs=0, next_after=after, has_more=len(rows)>limit)
    # Short write transactions keep the collector's normal updates unblocked.
    for offset in range(0, min(len(rows), limit), 100):
        batch = rows[offset:min(offset+100, limit)]
        conn = db.connect()
        try:
            with conn.transaction():
                for row in batch:
                    result["scanned"] += 1
                    result["next_after"] = row["listing_pk"]
                    current = conn.execute("SELECT "+analysis.FIELDS+" FROM listings WHERE listing_pk=?",
                                           (row["listing_pk"],)).fetchone()
                    current = product_attributes.attach([dict(current)], conn=conn)[0] if current else None
                    if not current or input_digest(current, catalog) != input_digest(row, catalog):
                        result["stale_inputs"] += 1
                        continue
                    pred = derive(row, catalog)
                    if not pred["candidate_families"]:
                        result["outside_pilot"] += 1
                        continue
                    cached = conn.execute("SELECT 1 FROM enrichment WHERE platform=? AND listing_id=? AND task=? AND input_sha256=?",
                                          (row["platform"], row["listing_id"], TASK, pred["input_sha256"])).fetchone()
                    if cached:
                        result["cached"] += 1
                    else:
                        _store(row, TASK, pred["input_sha256"], pred, catalog, conn)
                        result["stored"] += 1
        finally:
            conn.close()
    return result


def audit_template(filters=None, family=None, limit=30, purpose="development"):
    """Deterministic samples per family, without prefilled ground-truth labels."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("audit limit must be between 1 and 100 per family")
    catalog = load()
    from . import identity_audit
    if purpose not in ("development", "holdout"):
        raise ValueError("invalid audit purpose")
    if family is not None and family not in catalog["families"]:
        raise ValueError("unknown pilot family")
    with db.read_snapshot():
        rows, applied = _stored_rows(filters)
        original_n = len(rows)
        rows = identity_audit.eligible_rows(rows,purpose)
        if purpose == "holdout" and not rows:
            raise ValueError("no independent unreviewed listings available for holdout")
        predicted = {row["listing_pk"]: derive(row, catalog) for row in rows}
    selected, populations, seen = [], {}, set()
    for key in ([family] if family else list(catalog["families"])+["unmatched"]):
        pool = [row for row in rows if (key in predicted[row["listing_pk"]]["candidate_families"]
                                      if key != "unmatched" else not predicted[row["listing_pk"]]["candidate_families"])]
        populations[key] = len(pool)
        selected_in_family = 0
        for row in sorted(pool, key=lambda item: _hash([catalog["sha256"], item["platform"], item["listing_id"]])):
            if row["listing_pk"] in seen:
                continue
            selected.append(row)
            seen.add(row["listing_pk"])
            selected_in_family += 1
            if selected_in_family >= limit:
                break
    result = identity_audit.create(selected, applied, populations, purpose=purpose)
    result["excluded_prior_samples"] = original_n-len(rows)
    return result


def import_audit(document):
    """Validate a source-linked batch atomically; blank examples stay unlabelled."""
    from . import identity_audit
    if isinstance(document, dict) and document.get("version") == 1:
        reviews = document.get("reviews")
        if isinstance(reviews, list) and reviews and all(isinstance(item, dict) and item.get("expected") is None for item in reviews):
            return dict(inserted=0, cached=0, blank_examples=len(reviews), note="Blank legacy audit needs upgrade before review.")
    return identity_audit.import_document(document)


def preview_audit(document):
    from . import identity_audit
    return identity_audit.preview(document)


def upgrade_audit(document):
    from . import identity_audit
    return identity_audit.upgrade(document)


def _precision(correct, total):
    if not total:
        return dict(n=0, correct=0, precision=None, wilson_ci95=None)
    p, z = correct/total, 1.96
    denominator = 1+z*z/total
    center = (p+z*z/(2*total))/denominator
    half = z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/denominator
    return dict(n=total, correct=correct, precision=p,
                wilson_ci95=dict(lo=max(0,center-half), hi=min(1,center+half)))


def quality_rows(rows, identities=None, catalog=None):
    """Coverage is observed; accuracy is limited to frozen, reviewed predictions."""
    catalog = catalog or load()
    from . import product_attributes
    rows = product_attributes.attach(rows)
    reviews, stale = _reviews(rows, catalog)
    if identities is None:
        identities = resolve_rows(rows,catalog)
    families = []
    for key, family in catalog["families"].items():
        members = [entry for entry in identities.values() if key in entry["candidate_families"]]
        missing = Counter(field for entry in members for field in
                          entry["missing_variant_fields"]+entry["missing_comparison_fields"])
        model_n = model_ok = variant_n = variant_ok = n_reviewed = model_unscored = variant_unscored = 0
        for record in reviews.values():
            pred = record["prediction"]
            expected = _apply_review(pred, record, catalog)
            if key not in pred["candidate_families"] and expected["family"] != key:
                continue
            n_reviewed += 1
            known_outside = record["expected"]["family"] == "outside_pilot"
            if pred["model_key"] is not None:
                if known_outside or expected["model_key"] is not None:
                    model_n += 1
                    model_ok += int((pred["family"], pred["model_key"]) == (expected["family"], expected["model_key"]))
                else:
                    model_unscored += 1
            if pred["variant_key"] is not None:
                if (expected["variant_key"] is not None or known_outside
                        or (expected["model_key"] is not None and expected["model_key"] != pred["model_key"])):
                    variant_n += 1
                    variant_ok += int(pred["variant_key"] == expected["variant_key"])
                else:
                    variant_unscored += 1
        families.append(dict(family=key, label=family["label"], n_mentions=len(members),
                             n_model_identified=sum(entry["model_key"] is not None and not entry["conflicts"] for entry in members),
                             n_variant_identified=sum(entry["variant_key"] is not None for entry in members),
                             n_comparable=sum(entry["comparison_key"] is not None for entry in members),
                             model_coverage=sum(entry["model_key"] is not None and not entry["conflicts"] for entry in members)/len(members) if members else None,
                             variant_coverage=sum(entry["variant_key"] is not None for entry in members)/len(members) if members else None,
                             status_counts=dict(Counter(entry["status"] for entry in members)),
                             missing_fields=dict(sorted(missing.items(), key=lambda item: (-item[1], item[0]))),
                             n_current_reviews=n_reviewed, reviewed_model_precision=_precision(model_ok,model_n),
                             n_unscored_model_predictions=model_unscored, n_unscored_variant_predictions=variant_unscored,
                             reviewed_variant_precision=_precision(variant_ok,variant_n)))
    from . import identity_audit
    evaluation_reviews = dict(reviews)
    evaluation_reviews.update(identity_audit.frozen_holdouts(rows,catalog))
    return dict(generated_at=datetime.now(timezone.utc).isoformat(), rule_version=RULE_VERSION,
                catalog_version=catalog["version"], catalog_sha256=catalog["sha256"], n_listings=len(rows),
                n_pilot_mentions=sum(bool(entry["candidate_families"]) for entry in identities.values()),
                n_comparable=sum(entry["comparison_key"] is not None for entry in identities.values()),
                n_current_reviews=len(reviews), n_stale_review_records=stale, families=families,
                attribute_collection=product_attributes.coverage(rows),
                reviewed_attribute_facts=dict(Counter(key for review in reviews.values()
                    for key,value in review["expected"]["attributes"].items() if value is not None)),
                comparison_policy_version=COMPARISON_POLICY_VERSION, attribute_matrix=attribute_matrix(catalog),
                comparison_levels=dict(Counter(entry["comparison_level"] for entry in identities.values())),
                audit_evaluation=identity_audit.evaluation(evaluation_reviews, catalog),
                basis="Current text rules and explicit operator reviews; coverage and reviewed accuracy are separate.",
                limits="Family mentions can overlap, and listings outside rule coverage may still belong to these families. Recall is unmeasured. Accuracy describes scored reviewed predictions only, not the whole market; unknown review labels are unscored. Unreviewed functionality, completeness, defects and quantity stay unknown. Comparability does not establish resale proceeds, selling speed or profit.")


def quality_report(filters=None):
    with db.read_snapshot():
        rows, applied = _stored_rows(filters)
        return dict(quality_rows(rows), filters=applied)


def render_quality(report):
    lines = ["Product identity pilot", f"{report['catalog_version']} · {report['n_pilot_mentions']} pilot mentions / {report['n_listings']} listings",
             f"{report['n_comparable']} comparable single units · {report['n_current_reviews']} current operator reviews"]
    attributes = report.get("attribute_collection", {})
    lines.append(f"Source attribute claims: {attributes.get('n_with_assertions',0)} listings; "
                 f"{attributes.get('n_with_conflicts',0)} with conflicting claims (unreviewed)")
    for family in report["families"]:
        accuracy = family["reviewed_model_precision"]
        measured = f"{accuracy['precision']:.1%} on {accuracy['n']} reviewed predictions" if accuracy["precision"] is not None else "not measured"
        lines += [f"  {family['label']}: {family['n_mentions']} mentions, {family['n_model_identified']} models, "
                  f"{family['n_variant_identified']} variants, {family['n_comparable']} comparable; model precision {measured}"]
        if family["missing_fields"]:
            lines.append("    missing: "+", ".join(f"{field} ({n})" for field,n in list(family["missing_fields"].items())[:6]))
    if report.get("audit_evaluation"):
        audited = report["audit_evaluation"]
        lines.append(f"  Frozen holdout: {audited['n_eligible_holdout_reviews']} eligible reviewed listings; physical-unit groups score once")
        if audited["review_outcomes"]:
            lines.append("  Review diagnostics: "+", ".join(f"{key} ({n})" for key,n in audited["review_outcomes"].items()))
    return "\n".join(lines+[report["limits"]])


def catalog_report():
    catalog = load()
    return dict(version=catalog["version"], sha256=catalog["sha256"],
                comparison_policy_version=COMPARISON_POLICY_VERSION, attribute_matrix=attribute_matrix(catalog),
                review_families=list(catalog["families"])+["outside_pilot", None],
                inspection_fields=list(INSPECTION_FIELDS), conditions=schema.ENUM_CONDITION,
                attribute_enums={key: sorted(values) for key, values in ENUMS.items()},
                families=[dict(family=key, label=family["label"], comparison_fields=family["comparison_fields"],
                               models=[{field: model[field] for field in ("id", "brand", "label", "aliases", "variant_fields", "attributes")}
                                       for model in family["models"]]) for key, family in catalog["families"].items()])
