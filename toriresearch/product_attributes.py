"""Bounded seller assertions from responses already fetched by the collector.

Only matched product tokens survive extraction, never a description or arbitrary
extras. Snapshots live in listing_observations and inherit listing retention.
These assertions are source claims; they are not physical inspection or labels.
"""

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict

from . import config, db, metrics

VERSION = "product-attributes-v1"
SNAPSHOT_KEY = "product_attributes"
MAX_TEXT = 16000
MAX_ASSERTIONS = 96
SOURCES = ("item_page", "search_docs", "search_itemlist")
EXTRA_FIELDS = {"model", "condition", "storage", "storage_capacity", "battery_health",
                "battery_health_pct", "mount", "hardware_code", "edition", "quantity"}
IDENTITY_FIELDS = {"storage", "hardware_code", "edition", "mount", "battery_health_pct", "condition"}
ENUMS = {
    "edition": {"disc", "digital"},
    "mount": {"canon-ef", "canon-rf", "sony-fe"},
    "functionality": {"working", "defective"},
    "offer_kind": {"bundle", "accessory", "multiple_units"},
    "bundle_contents": {"controller", "charger", "box"},
    "missing_contents": {"controller", "charger", "box"},
    "defect_type": {"cracked", "scratches", "optical_fungus", "focus_fault"},
}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def facts_digest(assertions):
    facts = sorted({_json({key:entry[key] for key in ("attribute", "value", "source_field", "verification_basis")}) for entry in assertions})
    return _hash(facts) if facts else None


def _normalise(attribute, token):
    """Whole-token validation: no arbitrary text becomes a retained value."""
    text = token.strip().casefold()
    if attribute == "storage":
        match = re.fullmatch(r"(\d{1,4})\s*(gb|tb)", text)
        if match and 1 <= int(match[1]) <= (8 if match[2] == "tb" else 8192):
            return f"{int(match[1])}{match[2].upper()}"
    elif attribute == "hardware_code":
        if re.fullmatch(r"a\d{4}|cfi[- ]?\d{4}[ab]?|hac[- ]?001(?:\(-01\))?|heg[- ]?001|hdh[- ]?001", text):
            return text.replace(" ", "").upper()
    elif attribute in {"battery_health_pct", "quantity", "controller_count"}:
        if re.fullmatch(r"\d{1,3}\s*%?", text):
            number = int(text.rstrip("% "))
            lo, hi = (0, 100) if attribute == "battery_health_pct" else (1, 20)
            return number if lo <= number <= hi else None
    elif attribute == "mount":
        return {"canon ef": "canon-ef", "canon-ef": "canon-ef", "ef": "canon-ef",
                "canon rf": "canon-rf", "canon-rf": "canon-rf", "rf": "canon-rf",
                "sony fe": "sony-fe", "sony-fe": "sony-fe", "fe": "sony-fe"}.get(text)
    elif attribute == "edition":
        return {"digital": "digital", "digitaalinen": "digital", "disc": "disc",
                "levyasemallinen": "disc"}.get(text)
    elif attribute == "condition":
        from .ingest.itempage import normalise_condition
        value = normalise_condition(token)[1]
        return value if value != "unknown" else None
    return None


def extract(payload, title_field="title"):
    """Inspect named fields only; malformed optional extras fail locally.

    Extra names beyond model/condition are defensive candidate names, not a
    claim that the source supplies those fields. Text rules need explicit units
    or labels and never infer a working/complete unit from silence.
    """
    from . import product_identity
    catalog = product_identity.load()
    assertions, scanned, warnings = [], {}, set()

    def add(attribute, value, field, text, span, kind):
        if value is None or len(assertions) >= MAX_ASSERTIONS:
            return
        # Retain only a validated matched token, not adjacent seller prose.
        token = text[span[0]:span[1]]
        if len(token) > 80 or any(char in token for char in "\r\n<>@"):
            return
        entry = dict(attribute=attribute, value=value, source_field=field,
                     span=list(span), evidence_token=token,
                     input_sha256=hashlib.sha256(text.encode()).hexdigest(),
                     assertion_kind=kind, verification_basis="seller_statement",
                     extractor_version=VERSION)
        if entry not in assertions:
            assertions.append(entry)

    def models(text, field, kind):
        hits = [(match, model) for model in catalog["models"].values()
                for pattern in model["_aliases"] for match in pattern.finditer(text)]
        chosen = []
        for match, model in sorted(hits, key=lambda hit: -(hit[0].end()-hit[0].start())):
            if any(match.start() < old.end() and old.start() < match.end() for old in chosen):
                continue
            chosen.append(match)
            add("model_key", model["id"], field, text, match.span(), kind)

    fields = {}
    for field in (title_field, "description"):
        value = payload.get(field)
        if value is None:
            scanned[field] = "absent"
        elif not isinstance(value, str):
            scanned[field] = "malformed"
        else:
            fields[field] = value[:MAX_TEXT]
            scanned[field] = "bounded" if len(value) > MAX_TEXT else "present"
    title = fields.get(title_field, "")
    families = {family["id"] for family in catalog["families"].values()
                if any(pattern.search(title) for pattern in family["_hints"])}
    models(title, title_field, "seller_text_claim")
    for entry in assertions:
        if entry["attribute"] == "model_key":
            families.add(catalog["models"][entry["value"]]["_family"])
    extras = payload.get("extras")
    scanned["extras"] = "absent" if extras is None else "present" if isinstance(extras, list) else "malformed"
    for extra in extras[:64] if isinstance(extras, list) else ():
        if not isinstance(extra, dict) or not isinstance(extra.get("id"), str) or extra["id"] not in EXTRA_FIELDS:
            continue
        key = extra["id"]
        values = extra.get("values")
        values = list(values[:8]) if isinstance(values, list) else []
        if extra.get("value") is not None:
            values.append(extra["value"])
        for raw in values[:8]:
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                warnings.add("malformed_optional_extra")
                continue
            text = str(raw).strip()
            if not text or len(text) > 80:
                warnings.add("unsupported_optional_extra")
                continue
            field = "extras."+key
            if key == "model":
                models(text, field, "observed_platform_field")
                for entry in assertions:
                    if entry["attribute"] == "model_key":
                        families.add(catalog["models"][entry["value"]]["_family"])
                continue
            attribute = {"storage_capacity": "storage", "battery_health": "battery_health_pct"}.get(key, key)
            value = _normalise(attribute, text)
            if value is None:
                warnings.add("unsupported_optional_extra")
            else:
                add(attribute, value, field, text, (0, len(text)), "observed_platform_field")

    patterns = [
        ("storage", r"(?<![\w.])(\d{1,4}\s*(?:GB|TB))\b", 1),
        ("battery_health_pct", r"\b(?:akun\s+kunto|akun\s+kapasiteetti|battery\s+health|batterihälsa)\s*[:=]?\s*(\d{1,3}\s*%)", 1),
        ("hardware_code", r"\b(A\d{4}|CFI[- ]?\d{4}[AB]?|HAC[- ]?001(?:\(-01\))?|HEG[- ]?001|HDH[- ]?001)\b", 1),
        ("mount", r"\b(?:mount|kiinnitys|bajonetti)\s*[:=]?\s*(Canon[ -]EF|Canon[ -]RF|Sony[ -]FE|EF|RF|FE)\b", 1),
        ("quantity", r"\b(?:määrä|quantity)\s*[:=]\s*(\d{1,2})\b", 1),
        ("quantity", r"\b(\d{1,2})\s*(?:kpl\s+)?(?:puhelinta|konsolia|objektiivia)\b", 1),
        ("controller_count", r"\b(\d{1,2})\s*(?:ohjainta|ohjain|controllers?)\b", 1),
    ]
    for field, text in fields.items():
        for attribute, pattern, group in patterns:
            if attribute == "storage" and not families & {"phones", "consoles"}:
                continue
            for match in re.finditer(pattern, text, re.I):
                if attribute == "storage":
                    # RAM and add-on cards are not a device's internal capacity.
                    before,after = text[max(0,match.start()-24):match.start()],text[match.end():match.end()+24]
                    if (re.search(r"\b(?:RAM|SD|microSD|muistikortti)\s*[:=]?\s*$",before,re.I)
                            or re.search(r"^\s*(?:RAM|SD|microSD|muistikortti)\b",after,re.I)):
                        continue
                add(attribute, _normalise(attribute, match[group]), field, text,
                    match.span(group), "seller_text_claim")
        if field == title_field or field == "description":
            # A supported model mentioned in prose is not necessarily the unit
            # for sale. Description model claims must be explicitly labelled.
            if field == "description":
                for match in re.finditer(r"\b(?:malli|model)\s*[:=]\s*([^\n.;]{1,80})", text, re.I):
                    part = match[1]
                    before = len(assertions)
                    models(part, field, "seller_text_claim")
                    for entry in assertions[before:]:
                        entry["span"] = [pos+match.start(1) for pos in entry["span"]]
                        entry["input_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            for attribute, value, pattern in [
                ("edition", "digital", r"\b(?:digital|digitaalinen)\b"),
                ("edition", "disc", r"\b(?:disc|levyasemallinen)\b"),
                ("functionality", "defective", r"\b(?:rikkinäinen|ei\s+toimi|not\s+working)\b"),
                ("functionality", "working", r"\b(?:täysin\s+toimiva|fully\s+working)\b"),
                ("defect_type", "cracked", r"\b(?:haljennut|säröillä|cracked)\b"),
                ("defect_type", "scratches", r"\b(?:naarmuja|scratches)\b"),
                ("defect_type", "optical_fungus", r"\b(?:linssisientä|lens\s+fungus)\b"),
                ("defect_type", "focus_fault", r"\b(?:autofocus\s+fault|tarkennusvika)\b"),
                ("offer_kind", "bundle", r"\b(?:bundle|paketti|setti)\b"),
            ]:
                for match in re.finditer(pattern, text, re.I):
                    prefix = text[max(0,match.start()-35):match.start()]
                    if re.search(r"\b(?:ei|no|without|not|ilman)\s+(?:\w+\s+){0,2}$", prefix, re.I):
                        continue
                    add(attribute, value, field, text, match.span(), "seller_text_claim")
            for value, pattern in [("controller", r"(?:ohjain|controller)"),
                                   ("charger", r"(?:laturi|charger)"), ("box", r"(?:laatikko|box)")]:
                for match in re.finditer(r"\b(?:mukana|includes?)\s+("+pattern+r")\b", text, re.I):
                    prefix = text[max(0,match.start()-12):match.start()]
                    if not re.search(r"\b(?:ei|not)\s*$", prefix, re.I):
                        add("bundle_contents", value, field, text, match.span(1), "seller_text_claim")
                for match in re.finditer(r"\b(?:puuttuu|missing|without|ilman)\s+("+pattern+r")\b",text,re.I):
                    add("missing_contents",value,field,text,match.span(1),"seller_text_claim")
    if len(assertions) == MAX_ASSERTIONS:
        warnings.add("assertion_limit_reached")
    return dict(extractor_version=VERSION, scanned_fields=scanned, assertions=assertions,
                warnings=sorted(warnings))


def snapshot(extracted, observed_at):
    """Validate the parser's bounded projection before it enters persistence."""
    stamp = metrics.parse_ts(observed_at)
    if stamp is None or not isinstance(extracted, dict) or extracted.get("extractor_version") != VERSION:
        raise ValueError("invalid product attribute snapshot")
    assertions = extracted.get("assertions")
    if not isinstance(assertions, list) or len(assertions) > MAX_ASSERTIONS:
        raise ValueError("invalid product attribute assertions")
    clean = copy.deepcopy(extracted)
    if set(clean) != {"extractor_version", "scanned_fields", "assertions", "warnings"}:
        raise ValueError("unexpected product attribute payload")
    if not isinstance(clean["scanned_fields"], dict) or any(
            key not in {"title", "heading", "description", "extras"} or
            value not in {"absent", "malformed", "present", "bounded"} for key,value in clean["scanned_fields"].items()):
        raise ValueError("invalid product attribute field inventory")
    if not isinstance(clean["warnings"], list) or set(clean["warnings"])-{
            "malformed_optional_extra", "unsupported_optional_extra", "assertion_limit_reached"}:
        raise ValueError("invalid product attribute warnings")
    from . import product_identity
    for entry in assertions:
        if not isinstance(entry, dict) or set(entry) != {"attribute", "value", "source_field", "span", "evidence_token",
                "input_sha256", "assertion_kind", "verification_basis", "extractor_version"}:
            raise ValueError("invalid product attribute assertion")
        key, value = entry["attribute"], entry["value"]
        if key == "model_key":
            valid = isinstance(value, str) and value in product_identity.load()["models"]
        elif key in ENUMS:
            valid = isinstance(value, str) and value in ENUMS[key]
        elif key == "condition":
            valid = isinstance(value, str) and value in product_identity.schema.ENUM_CONDITION and value != "unknown"
        else:
            valid = _normalise(key, str(value)) == value and value is not None
        token = entry["evidence_token"]
        span = entry["span"]
        if isinstance(token, str):
            if key == "model_key":
                valid = valid and any(pattern.fullmatch(token) for pattern in product_identity.load()["models"].get(value,{}).get("_aliases",[]))
            elif key in ENUMS:
                allowed_tokens = {
                    "edition": {"digital", "digitaalinen", "disc", "levyasemallinen"},
                    "mount": {"canon ef", "canon-ef", "canon rf", "canon-rf", "sony fe", "sony-fe", "ef", "rf", "fe"},
                    "functionality": {"rikkinäinen", "ei toimi", "not working", "täysin toimiva", "fully working"},
                    "offer_kind": {"bundle", "paketti", "setti"},
                    "bundle_contents": {"ohjain", "controller", "laturi", "charger", "laatikko", "box"},
                    "missing_contents": {"ohjain", "controller", "laturi", "charger", "laatikko", "box"},
                    "defect_type": {"haljennut", "säröillä", "cracked", "naarmuja", "scratches", "linssisientä", "lens fungus", "autofocus fault", "tarkennusvika"},
                }
                valid = valid and re.sub(r"\s+", " ", token.casefold()) in allowed_tokens[key]
                if key in {"edition", "mount"}:
                    valid = valid and _normalise(key,token) == value
            else:
                valid = valid and _normalise(key,token) == value
        if (not valid or entry["source_field"] not in {"title", "heading", "description"} | {"extras."+key for key in EXTRA_FIELDS}
                or entry["assertion_kind"] not in {"observed_platform_field", "seller_text_claim"}
                or entry["verification_basis"] != "seller_statement" or entry["extractor_version"] != VERSION
                or not isinstance(token, str) or not 1 <= len(token) <= 80 or any(char in token for char in "\r\n<>@")
                or not isinstance(span, list) or len(span) != 2 or any(type(pos) is not int for pos in span)
                or not 0 <= span[0] < span[1] <= MAX_TEXT or span[1]-span[0] != len(token)
                or not isinstance(entry["input_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["input_sha256"])):
            raise ValueError("invalid product attribute evidence")
    clean["observed_at"] = stamp.isoformat()
    return clean


def attach(rows, conn=None, as_of=None):
    """Latest retained snapshot per source, optionally frozen at a decision time.

    Rich detail and sparse search claims stay separately attributable. Differing
    values are conflicts, never reconciled by a guess or source rank. No older
    assertion is substituted when a newer snapshot drops a claim.
    """
    rows = [dict(row) for row in rows]
    ids = list(dict.fromkeys(row["listing_pk"] for row in rows if row.get("listing_pk") is not None))
    found = defaultdict(list)
    date_field = "observed_at" if config.POSTGRES_DSN else "julianday(observed_at)"
    cutoff = metrics.parse_ts(as_of) if as_of is not None else None
    if as_of is not None and cutoff is None:
        raise ValueError("attribute as_of needs a valid timestamp")
    for start in range(0,len(ids),400):
        part = ids[start:start+400]
        params = list(part)
        where = "listing_pk IN ("+",".join("?" for _ in part)+") AND source IN ('item_page','search_docs','search_itemlist') AND fields_json LIKE '%\"product_attributes\":%'"
        if cutoff:
            where += " AND "+date_field+("<=?" if config.POSTGRES_DSN else "<=julianday(?)")
            params.append(cutoff.isoformat())
        query = "SELECT * FROM (SELECT listing_pk,source,observed_at,fields_json,ROW_NUMBER() OVER (PARTITION BY listing_pk,source ORDER BY "+date_field+" DESC,observation_pk DESC) AS r FROM listing_observations WHERE "+where+") AS snapshots WHERE r=1"
        selected = [dict(row) for row in conn.execute(query,params).fetchall()] if conn else db.query(query,params)
        for item in selected:
            snap = json.loads(item["fields_json"])[SNAPSHOT_KEY]
            found[item["listing_pk"]].append(dict(source=item["source"], **snap))
    for row in rows:
        snapshots = found[row.get("listing_pk")]
        assertions = [dict(entry, source=snap["source"], observed_at=snap["observed_at"])
                      for snap in snapshots for entry in snap["assertions"]]
        grouped = defaultdict(list)
        for entry in assertions:
            grouped[entry["attribute"]].append(entry["value"])
        multivalued = {"bundle_contents", "missing_contents", "defect_type"}
        conflicts = sorted(key for key,values in grouped.items() if key not in multivalued and len({_json(v) for v in values}) > 1)
        conflicts.extend("contents:"+value for value in sorted(set(grouped["bundle_contents"]) & set(grouped["missing_contents"])))
        values = {key: vals[0] for key,vals in grouped.items() if key not in conflicts and key not in multivalued}
        row["product_attribute_facts_sha256"] = facts_digest(assertions)
        row["product_attribute_evidence"] = dict(extractor_version=VERSION, assertions=assertions, values=values,
                 conflicts=conflicts, snapshots=[{k:snap[k] for k in ("source", "observed_at", "scanned_fields", "warnings")} for snap in snapshots],
                 basis="Source claims extracted by local rules; uncalibrated and not physically verified.")
    return rows


def coverage(rows):
    """Yield of latest retained observations; not whole-market field coverage."""
    sources = {}
    for source in SOURCES:
        selected = [row["product_attribute_evidence"] for row in rows
                    if any(snap["source"] == source for snap in row.get("product_attribute_evidence",{}).get("snapshots",[]))]
        counts = Counter(entry["attribute"] for evidence in selected for entry in evidence["assertions"] if entry["source"] == source)
        with_facts = sum(any(entry["source"] == source for entry in evidence["assertions"]) for evidence in selected)
        sources[source] = dict(n_latest_successful_observations=len(selected), n_with_assertions=with_facts,
                               assertion_yield=with_facts/len(selected) if selected else None, assertions_by_attribute=dict(counts))
    return dict(extractor_version=VERSION, n_listings=len(rows),
                n_with_assertions=sum(bool(row.get("product_attribute_evidence",{}).get("assertions")) for row in rows),
                n_with_conflicts=sum(bool(row.get("product_attribute_evidence",{}).get("conflicts")) for row in rows),
                sources=sources, limits="Latest retained successful observations with this extractor only; not all requests or whole-market coverage. Seller assertions are not reviewed accuracy or inspection.")
