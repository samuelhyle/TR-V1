"""Owned inventory, human inspections and deterministic listing-preparation previews.

Marketplace observations cannot supply physical inspection results. Preview
records are local evidence snapshots, not credentials or authority to publish.
"""

import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, schema

RULE_VERSION = "inventory-preview-2026-10-01-v1"
INSPECTION_MAX_DAYS = 30  # Internal review cadence, not a statutory interval.
KINDS = {"technology": "Technology", "bicycle": "Bicycle", "scooter": "Scooter"}
CHANNELS = {"local_draft": "Local preparation", "ebay": "eBay", "tradera": "Tradera",
            "tori": "Tori", "etsy": "Etsy vintage"}
STATES = ("in_stock", "quarantined", "retired")
OUTCOMES = ("unknown", "pass", "fail", "not_applicable")
CHECKS = {
    "ownership": "Ownership and right to sell", "identity": "Model and authenticity",
    "condition": "Condition and disclosed defects", "completeness": "Included parts and accessories",
    "safety": "Physical safety assessment", "recall": "Recall check",
    "battery": "Battery condition and measured facts", "charger": "Charger compatibility and condition",
    "electrical": "Electrical condition", "device_locks": "Activation locks and device restrictions",
    "frame": "Frame and structural condition", "brakes": "Brakes", "steering": "Steering",
    "drivetrain": "Drivetrain", "tyres": "Tyres", "age_evidence": "Manufacture date evidence",
}
BASE_CHECKS = ("ownership", "identity", "condition", "completeness", "safety", "recall")
SOURCES = {
    "tori": "https://tuki.tori.fi/hc/fi/articles/22686660870802-Torin-k%C3%A4ytt%C3%A4miseen-liittyv%C3%A4t-s%C3%A4%C3%A4nn%C3%B6t",
    "ebay": "https://developer.ebay.com/develop/guides/sell/authorization",
    "tradera": "https://api.tradera.com/",
    "etsy": "https://help.etsy.com/hc/en-us/articles/23948763872151-Does-Etsy-Allow-Drop-Shipping-or-Reselling",
}
FACT_KEYS = {"brand", "model", "condition", "battery_presence", "battery_wh",
             "mains_powered", "lock_applicable", "manufactured_on"}
REVIEW_KEYS = {"permission", "account", "product_policy"}


class Conflict(ValueError):
    """A stale edit or retry with changed contents requires a fresh read."""


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def rules_sha256():
    return _hash({"version": RULE_VERSION, "sources": SOURCES, "checks": CHECKS,
                  "base": BASE_CHECKS, "inspection_max_days": INSPECTION_MAX_DAYS})


def _text(value, name, maximum=300, required=False):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise ValueError(f"{name} is required and must fit {maximum} characters" if required
                         else f"{name} must fit {maximum} characters")
    return value


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _money(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a finite nonnegative amount")
    try:
        amount = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite nonnegative amount") from None
    if not math.isfinite(amount) or not 0 <= amount <= 1e9:
        raise ValueError(f"{name} must be a finite nonnegative amount")
    return amount


def _facts(value):
    if not isinstance(value, dict) or set(value) - FACT_KEYS:
        raise ValueError("unsupported inventory facts")
    result = {"condition": "unknown", "battery_presence": "unknown",
              "mains_powered": "unknown", "lock_applicable": "unknown"}
    for key, item in value.items():
        if key == "battery_wh":
            result[key] = _money(item, "battery Wh")
            if result[key] is not None and result[key] > 5000:
                raise ValueError("battery Wh must be at most 5000")
        else:
            result[key] = _text(item, key, 100)
    for key, choices in {"condition": schema.ENUM_CONDITION,
                         "battery_presence": ("unknown", "present", "absent"),
                         "mains_powered": ("unknown", "yes", "no"),
                         "lock_applicable": ("unknown", "yes", "no")}.items():
        if result[key] not in choices:
            raise ValueError(f"unsupported {key}")
    if result.get("manufactured_on"):
        try:
            stamp = date.fromisoformat(result["manufactured_on"])
        except ValueError:
            raise ValueError("manufacture date must be YYYY-MM-DD") from None
        if stamp.isoformat() != result["manufactured_on"] or stamp > _now().date():
            raise ValueError("manufacture date cannot be in the future")
    if result["battery_presence"] == "absent" and result.get("battery_wh") is not None:
        raise ValueError("an absent battery cannot have a Wh measurement")
    return result


def template(unit):
    """Applicability is explicit. Unknown applicability remains a missing fact."""
    facts, kind = unit["facts"], unit["kind"]
    checks = [{"key": key, "label": CHECKS[key], "applicability": "required"}
              for key in BASE_CHECKS]
    if kind in ("bicycle", "scooter"):
        for key in ("frame", "brakes", "steering", "tyres"):
            checks.append({"key": key, "label": CHECKS[key], "applicability": "required"})
        if kind == "bicycle":
            checks.append({"key": "drivetrain", "label": CHECKS["drivetrain"], "applicability": "required"})
    battery = facts.get("battery_presence", "unknown")
    for key in ("battery", "charger"):
        checks.append({"key": key, "label": CHECKS[key], "applicability":
                       "not_applicable" if battery == "absent" else
                       "required" if battery == "present" else "unknown"})
    mains = facts.get("mains_powered", "unknown")
    electrical = ("required" if battery == "present" or mains == "yes" else
                  "not_applicable" if battery == "absent" and mains == "no" else "unknown")
    checks.append({"key": "electrical", "label": CHECKS["electrical"], "applicability": electrical})
    if kind == "technology":
        locks = facts.get("lock_applicable", "unknown")
        checks.append({"key": "device_locks", "label": CHECKS["device_locks"], "applicability":
                       "required" if locks == "yes" else "not_applicable" if locks == "no" else "unknown"})
    checks.append({"key": "age_evidence", "label": CHECKS["age_evidence"], "applicability": "optional"})
    return checks


def _hydrate_units(conn, rows):
    """Add the derived fields every unit reader expects, in one pass per table.

    Kept separate from `_unit` so the derivation lives in exactly one place.
    Reading a page of drafts means reading a page of units, and re-deriving
    `stock_phase` in the caller would be how the two copies drift.
    """
    units = {}
    for row in rows:
        unit = dict(row)
        unit["facts"] = json.loads(unit.pop("facts_json"))
        units[unit["sku"]] = unit
    skus = list(units)
    if not skus:
        return units
    marks = ",".join("?" for _ in skus)
    outcome = {row[0] for row in conn.execute(
        "SELECT DISTINCT t.purchase_pk FROM transactions t JOIN inventory_units u "
        "ON u.purchase_pk=t.purchase_pk WHERE u.sku IN (" + marks + ")", tuple(skus))}
    written_off = {row[0] for row in conn.execute(
        "SELECT DISTINCT sku FROM financial_entries WHERE kind='write_off' AND order_id IS NULL AND sku IN (" + marks + ")", tuple(skus))}
    phases = {row[0]: row[1] for row in conn.execute(
        "SELECT sku, phase FROM stock_lifecycle WHERE sku IN (" + marks + ")", tuple(skus))}
    for sku, unit in units.items():
        recorded = unit["purchase_pk"] in outcome
        phase = "written_off" if sku in written_off else phases.get(sku)
        if phase is not None:
            unit["stock_phase"] = phase
            unit["has_recorded_outcome"] = phase != "available"
        else:
            unit["stock_phase"] = "legacy_outcome" if recorded else "available"
            unit["has_recorded_outcome"] = recorded
    return units


def _unit(conn, sku, lock=False):
    sku = _text(sku, "SKU", 64, True)
    suffix = " FOR UPDATE" if lock and conn.dialect == "postgres" else ""
    row = conn.execute("SELECT * FROM inventory_units WHERE sku=?" + suffix, (sku,)).fetchone()
    if row is None:
        raise ValueError("owned unit not found")
    return _hydrate_units(conn, [row])[sku]


def _units(conn, skus):
    """Every unit for `skus`, keyed by SKU, in a fixed number of queries."""
    skus = sorted({sku for sku in skus if sku})
    if not skus:
        return {}
    marks = ",".join("?" for _ in skus)
    return _hydrate_units(conn, conn.execute(
        "SELECT * FROM inventory_units WHERE sku IN (" + marks + ")", tuple(skus)).fetchall())


def _version(unit, expected_version):
    _integer(expected_version, "expected version")
    if unit["version"] != expected_version:
        raise Conflict("This unit changed. Reload it before saving.")


def _unreserved(conn, sku):
    if conn.execute("SELECT 1 FROM inventory_reservations WHERE sku=?", (sku,)).fetchone():
        raise Conflict("This unit is reserved for a pending or active channel listing. Reconcile the listing before changing its inspected facts.")


def _token(value):
    return _text(value, "client token", 100, True)


def _token_lock(conn, token):
    if conn.dialect == "postgres":
        key = int(hashlib.sha256(token.encode()).hexdigest()[:15], 16)
        conn.execute("SELECT pg_advisory_xact_lock(?)", (key,))


def _write_connection():
    """Owned evidence stays readable/writable only by the local account."""
    conn = db.connect()
    try:
        if conn.dialect == "sqlite":
            for suffix in ("", "-wal", "-shm"):
                path = config.SQLITE_PATH.with_name(config.SQLITE_PATH.name + suffix)
                if path.exists():
                    path.chmod(0o600)
        return conn
    except Exception:
        conn.close()
        raise


def _replay(conn, table, token, fingerprint):
    # Table names come only from the three call sites, never request fields.
    row = conn.execute(f"SELECT * FROM {table} WHERE client_token=?", (token,)).fetchone()
    if row and row["request_sha256"] != fingerprint:
        raise Conflict("This retry token was already used with different contents.")
    return dict(row) if row else None


def _event(conn, unit, event_type, reason):
    conn.execute("INSERT INTO inventory_events "
                 "(sku, unit_version, event_type, recorded_at, reason, snapshot_json) VALUES (?, ?, ?, ?, ?, ?)",
                 (unit["sku"], unit["version"], event_type, _now().isoformat(), reason, _json(unit)))


def create(client_token, kind, title="", facts=None, sku=None, purchase_pk=None,
           acquisition_cost_eur=None, shipping_cost_eur=None, buyer_fee_eur=None,
           platform="offline", source_listing_id=None, purchased_on=None, subplatform="standard", acquisition_receipt=None):
    """Create one physical unit, optionally linking an existing ledger purchase.

    Offline purchases use NULL source identity. A marketplace ID is provenance,
    never a fabricated key for inventory.
    """
    token = _token(client_token)
    if kind not in KINDS:
        raise ValueError("unsupported inventory kind")
    facts = _facts({} if facts is None else facts)
    title = _text(title, "title", 300)
    if sku is not None:
        sku = _text(sku, "SKU", 64, True).upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,63}", sku):
            raise ValueError("SKU must use letters, numbers, dots, underscores or hyphens")
    if purchase_pk is not None:
        _integer(purchase_pk, "purchase ID")
        if any(v is not None for v in (acquisition_cost_eur, shipping_cost_eur, buyer_fee_eur,
                                       source_listing_id, purchased_on)) or platform != "offline" or subplatform != "standard":
            raise ValueError("linking a purchase cannot overwrite its costs or source")
    else:
        acquisition_cost_eur = _money(acquisition_cost_eur, "acquisition cost")
        shipping_cost_eur = _money(shipping_cost_eur, "inbound shipping")
        buyer_fee_eur = _money(buyer_fee_eur, "buyer fee")
        if acquisition_cost_eur is None or not title:
            raise ValueError("title and actual acquisition cost are required")
        if platform not in ("offline", "tori", "ebay", "tradera", "other"):
            raise ValueError("unsupported acquisition source")
        if subplatform not in ("standard", "diili") or platform != "tori" and subplatform != "standard":
            raise ValueError("unsupported acquisition market")
        source_listing_id = _text(source_listing_id, "source listing ID", 100, True) if source_listing_id else None
        if platform == "offline" and source_listing_id:
            raise ValueError("offline purchases do not have marketplace listing IDs")
    if acquisition_receipt is not None:
        if not isinstance(acquisition_receipt, dict) or set(acquisition_receipt) - {"decision_pk", "reason", "evidence_ref", "occurred_on"} or not {"decision_pk", "reason", "evidence_ref"} <= set(acquisition_receipt):
            raise ValueError("acquisition receipt requires a decision, reason and evidence reference")
    purchased_at = None
    if purchased_on is not None:
        purchased_on = _text(purchased_on, "purchase date", 100, True)
        try:
            if len(purchased_on) == 10:
                day = date.fromisoformat(purchased_on)
                helsinki = ZoneInfo("Europe/Helsinki")
                if day.isoformat() != purchased_on or day > _now().astimezone(helsinki).date():
                    raise ValueError()
                parsed = datetime.combine(day, time.min, tzinfo=helsinki)
            else:
                parsed = datetime.fromisoformat(purchased_on.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed > _now():
                    raise ValueError()
        except ValueError:
            raise ValueError("purchase date must be YYYY-MM-DD or a timezone-aware timestamp, never future") from None
        purchased_at = parsed.astimezone(timezone.utc).isoformat()
    request = {"kind": kind, "title": title, "facts": facts, "sku": sku,
                         "purchase_pk": purchase_pk, "cost": acquisition_cost_eur,
                         "shipping": shipping_cost_eur, "fee": buyer_fee_eur,
                         "platform": platform, "subplatform": subplatform, "source": source_listing_id, "purchased_on": purchased_on}
    if acquisition_receipt is not None:
        request["acquisition_receipt"] = acquisition_receipt
    fingerprint = _hash(request)
    conn = _write_connection()
    try:
        with conn.transaction():
            _token_lock(conn, token)
            previous = _replay(conn, "inventory_units", token, fingerprint)
            if previous:
                return {"sku": previous["sku"], "replayed": True}
            if sku and conn.execute("SELECT 1 FROM inventory_units WHERE sku=?", (sku,)).fetchone():
                raise Conflict("That SKU already belongs to an owned unit")
            if purchase_pk is not None:
                suffix = " FOR UPDATE" if conn.dialect == "postgres" else ""
                purchase = conn.execute("SELECT * FROM purchases WHERE purchase_pk=?" + suffix, (purchase_pk,)).fetchone()
                if not purchase:
                    raise ValueError("purchase not found")
                if conn.execute("SELECT 1 FROM transactions WHERE purchase_pk=?", (purchase_pk,)).fetchone():
                    raise ValueError("choose an unsold purchase for owned inventory")
                if conn.execute("SELECT 1 FROM inventory_units WHERE purchase_pk=?", (purchase_pk,)).fetchone():
                    raise Conflict("This purchase already has an owned SKU")
                title = title or purchase["title_raw"]
            else:
                purchase = conn.execute(
                    "INSERT INTO purchases (platform, subplatform, source_listing_id, title_raw, category_norm, "
                    "acquisition_cost_eur, shipping_cost_eur, buyer_fee_eur, purchased_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING purchase_pk",
                    (platform, subplatform, source_listing_id, title, KINDS[kind], acquisition_cost_eur,
                     shipping_cost_eur, buyer_fee_eur, purchased_at or _now().isoformat())).fetchone()
                if not purchase:
                    raise Conflict("This source purchase already exists. Link its purchase ID instead.")
                purchase_pk = purchase[0]
            sku = sku or "TR-" + uuid.uuid4().hex[:16].upper()
            now = _now().isoformat()
            conn.execute("INSERT INTO inventory_units (sku, purchase_pk, client_token, request_sha256, "
                         "title, kind, facts_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (sku, purchase_pk, token, fingerprint, title, kind, _json(facts), now, now))
            _event(conn, _unit(conn, sku), "created", "Owned unit recorded")
            if acquisition_receipt is not None:
                from . import owned_outcomes
                owned_outcomes._attempt(conn, token, status="acquired", purchase_pk=purchase_pk, **acquisition_receipt)
            return {"sku": sku, "replayed": False}
    finally:
        conn.close()


def update(sku, expected_version, title, kind, facts, state, reason):
    title = _text(title, "title", 300, True)
    reason = _text(reason, "change reason", 1000, True)
    facts = _facts(facts)
    if kind not in KINDS or state not in STATES:
        raise ValueError("unsupported kind or inventory state")
    conn = _write_connection()
    try:
        with conn.transaction():
            unit = _unit(conn, sku, lock=True)
            _version(unit, expected_version)
            if unit["stock_phase"] == "written_off" and state != "retired":
                raise Conflict("Written-off stock cannot be reopened as available inventory.")
            changed_facts = (title, kind, facts) != (unit["title"], unit["kind"], unit["facts"])
            if not changed_facts and state == unit["state"]:
                return {"sku": sku, "version": unit["version"]}
            _unreserved(conn, sku)
            conn.execute("UPDATE inventory_units SET title=?, kind=?, state=?, facts_json=?, "
                         "version=version+1, facts_version=facts_version+?, updated_at=? WHERE sku=?",
                         (title, kind, state, _json(facts), int(changed_facts), _now().isoformat(), sku))
            _event(conn, _unit(conn, sku), "updated", reason)
            return {"sku": sku, "version": unit["version"] + 1}
    finally:
        conn.close()


def inspect(client_token, sku, expected_version, inspector, competent, evidence_ref, checks):
    token = _token(client_token)
    inspector = _text(inspector, "inspector", 80, True)
    evidence_ref = _text(evidence_ref, "owned evidence reference", 500)
    if not isinstance(competent, bool):
        raise ValueError("competence confirmation must be true or false")
    if not isinstance(checks, dict) or set(checks) - set(CHECKS):
        raise ValueError("unsupported inspection checks")
    cleaned = {}
    for key, check in checks.items():
        if not isinstance(check, dict) or set(check) - {"outcome", "reason"}:
            raise ValueError("inspection checks need outcome and reason")
        outcome = check.get("outcome", "unknown")
        if outcome not in OUTCOMES:
            raise ValueError("unsupported inspection outcome")
        reason = _text(check.get("reason", ""), "inspection reason", 1000, outcome != "unknown")
        if outcome != "unknown" and not evidence_ref:
            raise ValueError("observed inspection outcomes require an owned evidence reference")
        cleaned[key] = {"outcome": outcome, "reason": reason}
    fingerprint = _hash({"sku": sku, "version": expected_version, "inspector": inspector,
                         "competent": competent, "evidence": evidence_ref, "checks": cleaned})
    conn = _write_connection()
    try:
        with conn.transaction():
            _token_lock(conn, token)
            previous = _replay(conn, "inspections", token, fingerprint)
            if previous:
                return {"sku": sku, "inspection_pk": previous["inspection_pk"], "replayed": True}
            unit = _unit(conn, sku, lock=True)
            _version(unit, expected_version)
            _unreserved(conn, sku)
            if set(cleaned) - {check["key"] for check in template(unit)}:
                raise ValueError("inspection checks do not match this kind of unit")
            pk = conn.execute("INSERT INTO inspections (sku, facts_version, client_token, request_sha256, "
                              "recorded_at, inspector, competent, evidence_ref, checks_json) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING inspection_pk",
                              (sku, unit["facts_version"], token, fingerprint, _now().isoformat(),
                               inspector, competent, evidence_ref, _json(cleaned))).fetchone()[0]
            conn.execute("UPDATE inventory_units SET version=version+1, updated_at=? WHERE sku=?",
                         (_now().isoformat(), sku))
            _event(conn, _unit(conn, sku), "inspected", f"Inspection #{pk} by {inspector}")
            return {"sku": sku, "inspection_pk": pk, "replayed": False}
    finally:
        conn.close()


def _inspection(row):
    if not row:
        return None
    row = dict(row)
    row["checks"] = json.loads(row.pop("checks_json"))
    row["competent"] = bool(row["competent"])
    # Opaque retry tokens are not useful evidence in the browser.
    row.pop("client_token", None)
    row.pop("request_sha256", None)
    return row


def _reviews(reviews):
    if not isinstance(reviews, dict) or set(reviews) - REVIEW_KEYS:
        raise ValueError("unsupported channel review fields")
    cleaned = {}
    for key in sorted(REVIEW_KEYS):
        item = reviews.get(key, {})
        if not isinstance(item, dict) or set(item) - {"outcome", "reason", "evidence_ref"}:
            raise ValueError("channel reviews need outcome, reason and evidence reference")
        outcome = item.get("outcome", "unknown")
        if outcome not in ("unknown", "pass", "fail"):
            raise ValueError("unsupported channel review outcome")
        cleaned[key] = {"outcome": outcome,
                        "reason": _text(item.get("reason", ""), "review reason", 1000, outcome != "unknown"),
                        "evidence_ref": _text(item.get("evidence_ref", ""), "review evidence", 500, outcome != "unknown")}
    return cleaned


def preview(unit, inspection, channel, reviews=None):
    """Pure evaluation. An allowed result means ready for local preparation only."""
    if channel not in CHANNELS:
        raise ValueError("unsupported channel")
    reviews = _reviews({} if reviews is None else reviews)
    reasons, missing = [], []

    def flag(code, message, blocked=False):
        reasons.append({"code": code, "message": message, "severity": "blocked" if blocked else "review"})

    if unit["state"] != "in_stock":
        flag("stock_state", "Unit is quarantined or retired.", True)
    if unit.get("has_recorded_outcome"):
        flag("recorded_trade", "This purchase has a recorded ledger outcome. Reconcile its stock lifecycle before preparation.", True)
    facts = unit["facts"]
    for field in ("condition", "battery_presence", "mains_powered") + (("lock_applicable",) if unit["kind"] == "technology" else ()):
        if facts.get(field, "unknown") == "unknown":
            missing.append(field)
            flag("missing_fact", f"Record {field.replace('_', ' ')}.")
    if facts.get("battery_presence") == "present" and (facts.get("battery_wh") is None or facts["battery_wh"] <= 0):
        missing.append("battery_wh")
        flag("battery_measurement", "Record positive battery capacity in Wh from owned evidence.")
    if facts.get("condition") in ("poor", "for_parts"):
        flag("parts_review", "Poor/parts-only stock needs a separate disclosure and safety workflow.")
    current = inspection is not None and inspection["facts_version"] == unit["facts_version"]
    if not inspection:
        flag("no_inspection", "A physical inspection is required.")
    elif not current:
        flag("stale_inspection", "Facts changed after inspection. Inspect the current unit facts.")
    elif _now() - datetime.fromisoformat(str(inspection["recorded_at"])) > timedelta(days=INSPECTION_MAX_DAYS):
        current = False
        flag("aged_inspection", "Inspection exceeds the internal 30-day review interval.")
    if current:
        if not inspection["competent"]:
            flag("competence", "A competent human must assess the applicable physical checks.")
        if not inspection["evidence_ref"]:
            flag("inspection_evidence", "Owned inspection evidence is missing.")
        for check in template(unit):
            outcome = inspection["checks"].get(check["key"], {}).get("outcome", "unknown")
            if outcome == "fail":
                flag("failed_" + check["key"], check["label"] + " failed.", True)
            elif check["applicability"] == "required" and outcome != "pass":
                flag("check_" + check["key"], check["label"] + " needs a passed inspection.")
            elif check["applicability"] == "unknown":
                flag("scope_" + check["key"], "Confirm applicability of " + check["label"].lower() + ".")
            elif check["applicability"] == "not_applicable" and outcome == "pass":
                flag("contradiction_" + check["key"], check["label"] + " contradicts the recorded applicability.")
    if channel == "etsy":
        manufactured = facts.get("manufactured_on")
        if not manufactured:
            missing.append("manufactured_on")
            flag("vintage_date", "Ordinary resale requires proven vintage age for this pilot.")
        else:
            today = _now().date()
            try:
                cutoff = today.replace(year=today.year - 20)
            except ValueError:
                cutoff = today.replace(year=today.year - 20, day=28)
            if date.fromisoformat(manufactured) >= cutoff:
                flag("modern_resale", "This ordinary resale item is not over 20 years old.", True)
        if not current or inspection["checks"].get("age_evidence", {}).get("outcome") != "pass":
            flag("vintage_evidence", "Vintage age needs a passed evidence check.")
    if channel != "local_draft":
        labels = {"permission": "Permission for the proposed seller action",
                  "account": "Appropriate seller account", "product_policy": "Current product, safety and shipping rules"}
        for key, review in reviews.items():
            if review["outcome"] != "pass":
                flag("channel_" + key, labels[key] + " needs evidenced review.", review["outcome"] == "fail")
    status = "blocked" if any(r["severity"] == "blocked" for r in reasons) else "requires_review" if reasons else "allowed"
    inputs = {"unit": {k: unit[k] for k in ("sku", "title", "kind", "state", "version", "facts_version", "facts", "has_recorded_outcome")},
              "inspection": inspection, "channel": channel, "reviews": reviews}
    valid_until = (_now() + timedelta(days=INSPECTION_MAX_DAYS))
    if inspection:
        valid_until = datetime.fromisoformat(str(inspection["recorded_at"])) + timedelta(days=INSPECTION_MAX_DAYS)
    return {"sku": unit["sku"], "channel": channel, "status": status,
            "unit_version": unit["version"], "facts_version": unit["facts_version"],
            "rule_version": RULE_VERSION, "rules_sha256": rules_sha256(),
            "input_sha256": _hash(inputs), "inspection_pk": inspection["inspection_pk"] if inspection else None,
            "reasons": reasons, "missing_facts": missing, "sources": [SOURCES[channel]] if channel in SOURCES else [],
            "reviews": reviews, "inputs": inputs, "publication_enabled": False,
            "valid_until": valid_until.isoformat(),
            "note": "Preparation preview only. Account/rule reviews are operator evidence; no seller credentials are verified or publishing performed."}


def evaluate(client_token, sku, expected_version, channel, reviews=None):
    token = _token(client_token)
    reviews = _reviews({} if reviews is None else reviews)
    if channel not in CHANNELS:
        raise ValueError("unsupported channel")
    fingerprint = _hash({"sku": sku, "version": expected_version, "channel": channel, "reviews": reviews})
    conn = _write_connection()
    try:
        with conn.transaction():
            _token_lock(conn, token)
            previous = _replay(conn, "platform_eligibility", token, fingerprint)
            if previous:
                result = json.loads(previous["result_json"])
                return _evaluation_view(result, _unit(conn, sku), previous["recorded_at"])
            unit = _unit(conn, sku, lock=True)
            _version(unit, expected_version)
            inspection = _inspection(conn.execute("SELECT * FROM inspections WHERE sku=? ORDER BY inspection_pk DESC LIMIT 1", (sku,)).fetchone())
            result = preview(unit, inspection, channel, reviews)
            stamp = _now().isoformat()
            pk = conn.execute("INSERT INTO platform_eligibility (sku, unit_version, client_token, request_sha256, "
                              "channel, status, rule_version, rules_sha256, input_sha256, recorded_at, result_json) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING evaluation_pk",
                              (sku, unit["version"], token, fingerprint, channel, result["status"], RULE_VERSION,
                               result["rules_sha256"], result["input_sha256"], stamp, _json(result))).fetchone()[0]
            result["evaluation_pk"] = pk
            # Include the durable primary key in the frozen result as well.
            conn.execute("UPDATE platform_eligibility SET result_json=? WHERE evaluation_pk=?", (_json(result), pk))
            return _evaluation_view(result, unit, stamp)
    finally:
        conn.close()


def _evaluation_view(result, unit, recorded_at):
    result = dict(result)
    result["recorded_at"] = recorded_at
    result["stale"] = (result["unit_version"] != unit["version"] or result["rules_sha256"] != rules_sha256()
                       or result["inputs"]["unit"]["has_recorded_outcome"] != unit["has_recorded_outcome"]
                       or _now() >= datetime.fromisoformat(result["valid_until"]))
    if result["stale"]:
        result["historical_status"] = result["status"]
        result["status"] = "requires_review"
    return result


def get(sku):
    with db.read_snapshot():
        conn = db.connect()
        unit = _unit(conn, sku)
        unit.pop("client_token", None)
        unit.pop("request_sha256", None)
        unit["purchase"] = db.query_one("SELECT * FROM purchases WHERE purchase_pk=?", (unit["purchase_pk"],))
        unit["checks_template"] = template(unit)
        unit["inspections"] = [_inspection(row) for row in db.query("SELECT * FROM inspections WHERE sku=? ORDER BY inspection_pk DESC LIMIT 30", (sku,))]
        unit["latest_inspection"] = unit["inspections"][0] if unit["inspections"] else None
        unit["local_preview"] = preview(unit, unit["latest_inspection"], "local_draft")
        unit["evaluations"] = [_evaluation_view(json.loads(row["result_json"]), unit, row["recorded_at"])
                               for row in db.query("SELECT result_json, recorded_at FROM platform_eligibility WHERE sku=? ORDER BY evaluation_pk DESC LIMIT 20", (sku,))]
        unit["history"] = [{k: v for k, v in row.items() if k != "snapshot_json"} for row in db.query("SELECT * FROM inventory_events WHERE sku=? ORDER BY event_pk DESC LIMIT 50", (sku,))]
        return unit


def listing(search="", state="", page=1):
    search = _text(search, "search", 200)
    if state and state not in STATES:
        raise ValueError("unsupported inventory state")
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        raise ValueError("page must be a positive integer") from None
    where, params = [], []
    if search:
        where.append("(LOWER(u.title) LIKE ? ESCAPE '!' OR LOWER(u.sku) LIKE ? ESCAPE '!')")
        pattern = "%" + search.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        params.extend([pattern, pattern])
    if state:
        where.append("u.state=?")
        params.append(state)
    clause = " WHERE " + " AND ".join(where) if where else ""
    with db.read_snapshot():
        total = db.scalar("SELECT COUNT(*) FROM inventory_units u" + clause, params)
        pages = max(1, (total + 24) // 25)
        page = min(page, pages)
        rows = db.query("SELECT u.sku, u.title, u.kind, u.state, u.version, u.facts_version, u.updated_at, "
                        "u.purchase_pk, p.platform, p.source_listing_id, p.acquisition_cost_eur, s.phase AS stock_phase "
                        "FROM inventory_units u JOIN purchases p ON p.purchase_pk=u.purchase_pk LEFT JOIN stock_lifecycle s ON s.sku=u.sku" + clause +
                        " ORDER BY u.created_at DESC, u.sku LIMIT 25 OFFSET ?", params + [(page - 1) * 25])
        counts = {state: 0 for state in STATES}
        counts.update({row["state"]: row["n"] for row in db.query("SELECT state, COUNT(*) AS n FROM inventory_units GROUP BY state")})
        candidates = db.query("SELECT p.purchase_pk, p.title_raw, p.acquisition_cost_eur FROM purchases p "
                              "WHERE NOT EXISTS (SELECT 1 FROM inventory_units u WHERE u.purchase_pk=p.purchase_pk) "
                              "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.purchase_pk=p.purchase_pk) "
                              "ORDER BY p.purchase_pk DESC LIMIT 200")
    return {"items": rows, "total": total, "page": page, "pages": pages, "counts": counts,
            "unlinked_purchases": candidates, "kinds": KINDS, "channels": CHANNELS,
            "rule_version": RULE_VERSION}


def action(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
        raise ValueError("action and params are required")
    name, params = payload.get("action"), payload["params"]
    writers = {
        "create": (create, {"client_token", "kind", "title", "facts", "sku", "purchase_pk",
                            "acquisition_cost_eur", "shipping_cost_eur", "buyer_fee_eur", "platform", "subplatform", "source_listing_id", "purchased_on", "acquisition_receipt"}, {"client_token", "kind"}),
        "update": (update, {"sku", "expected_version", "title", "kind", "facts", "state", "reason"}, {"sku", "expected_version", "title", "kind", "facts", "state", "reason"}),
        "inspect": (inspect, {"client_token", "sku", "expected_version", "inspector", "competent", "evidence_ref", "checks"}, {"client_token", "sku", "expected_version", "inspector", "competent", "evidence_ref", "checks"}),
        "evaluate": (evaluate, {"client_token", "sku", "expected_version", "channel", "reviews"}, {"client_token", "sku", "expected_version", "channel"}),
    }
    if not isinstance(name, str) or name not in writers:
        raise ValueError("unsupported inventory action")
    writer, allowed, required = writers[name]
    if set(params) - allowed or not required <= set(params):
        raise ValueError("unsupported or missing inventory fields")
    result = writer(**params)
    return {"ok": True, "result": result}
