"""Owned-fact templates, immutable review receipts and local publishing intents.

An approval reviews preparation. It never creates seller credentials, grants
authority or enables an unsupported channel action.
"""

import json
import uuid
from decimal import Decimal, InvalidOperation

from . import db, inventory

TEMPLATE_VERSION = "owned-listing-2026-10-02-v2"
FIELDS = {"included_items", "defects", "notes"}
CHANNELS = ("local_draft", "tori")
Conflict = inventory.Conflict


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("price must be a positive EUR amount with at most two decimal places")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 < amount <= 1000000000 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError()
        return int(amount * 100)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError("price must be a positive EUR amount with at most two decimal places") from None


def _target(channel, environment):
    if channel not in CHANNELS:
        raise ValueError("unsupported draft channel")
    if (channel == "local_draft" and environment != "local" or
            channel != "local_draft" and environment not in ("sandbox", "production") or
            channel not in ("local_draft", "ebay") and environment != "production"):
        raise ValueError("unsupported channel environment")


def _connection(channel, environment, reviews=None):
    account = (reviews or {}).get("account", {})
    # Binds operator account evidence, not verified remote identity.
    return "operator:" + inventory._hash(account) if account.get("outcome") == "pass" else None


def _inspection(conn, sku):
    result = inventory._inspection(conn.execute(
        "SELECT * FROM inspections WHERE sku=? ORDER BY inspection_pk DESC LIMIT 1", (sku,)).fetchone())
    # PostgreSQL timestamps and SQLite text must produce the same binding.
    return json.loads(inventory._json(result)) if result else None


def _binding(conn, unit, channel, environment, inspection=None, eligibility=None):
    # `inspection` and `eligibility` may be pre-loaded by a caller reading many
    # drafts at once; the defaults keep single-draft callers unchanged.
    if eligibility is None:
        eligibility = conn.execute(
            "SELECT result_json FROM platform_eligibility WHERE sku=? AND channel=? "
            "ORDER BY evaluation_pk DESC LIMIT 1", (unit["sku"], channel)).fetchone()
    reviews = {}
    if eligibility:
        result = json.loads(eligibility[0])
        if not inventory._evaluation_view(result, unit, None)["stale"]:
            reviews = result["reviews"]
    if inspection is None:
        inspection = _inspection(conn, unit["sku"])
    preview = inventory.preview(unit, inspection, channel, reviews)
    return {"template_version": TEMPLATE_VERSION, "rules_sha256": inventory.rules_sha256(),
            "unit": preview["inputs"]["unit"], "inspection": preview["inputs"]["inspection"],
            "reviews": preview["reviews"], "account_binding": _connection(channel, environment, preview["reviews"])}


def _content(unit, price_eur, disclosures):
    if not isinstance(disclosures, dict) or set(disclosures) - FIELDS:
        raise ValueError("unsupported draft disclosures")
    disclosures = {key: inventory._text(disclosures.get(key, ""), key.replace("_", " "),
                                       3000 if key == "defects" else 2000) for key in sorted(FIELDS)}
    facts = unit["facts"]
    title = " ".join(facts.get(key, "").strip() for key in ("brand", "model")).strip() or unit["title"]
    # Condition, measurements and operator disclosures are the only claims.
    # A passed safety/recall check is evidence, never a blanket product guarantee.
    conditions = {"new_unused": "Uusi, käyttämätön", "like_new": "Kuin uusi", "very_good": "Erittäin hyvä",
                  "good": "Hyvä", "fair": "Tyydyttävä", "poor": "Huono", "for_parts": "Varaosiksi",
                  "refurbished": "Kunnostettu", "unknown": "Ei vahvistettu"}
    lines = ["Kunto: " + conditions[facts["condition"]]]
    if facts.get("battery_presence") in ("present", "absent"):
        lines.append("Akku: " + ("on" if facts["battery_presence"] == "present" else "ei ole"))
    if facts.get("battery_wh") is not None:
        lines.append("Kirjattu akun kapasiteetti: " + str(facts["battery_wh"]) + " Wh")
    if facts.get("manufactured_on"):
        lines.append("Kirjattu valmistuspäivä: " + facts["manufactured_on"])
    for key, label in (("included_items", "Mukana"), ("defects", "Tiedossa olevat viat"), ("notes", "Lisätiedot")):
        if disclosures[key]:
            lines.append(label + ": " + disclosures[key])
    return {"title": title[:80], "description": "\n\n".join(lines), "price_minor": _price(price_eur),
            "currency": "EUR", "quantity": 1, "language": "fi", "disclosures": disclosures,
            "authorship": "Owned-fact template and operator disclosures; no AI-generated claims."}


def _draft(conn, draft_id, lock=False):
    draft_id = inventory._text(draft_id, "draft ID", 64, True)
    suffix = " FOR UPDATE" if lock and conn.dialect == "postgres" else ""
    row = conn.execute("SELECT * FROM listing_drafts WHERE draft_id=?" + suffix, (draft_id,)).fetchone()
    if not row:
        raise ValueError("listing draft not found")
    result = dict(row)
    result["content"] = json.loads(result.pop("content_json"))
    result["binding"] = json.loads(result.pop("binding_json"))
    return result


def _snapshot(draft):
    return {key: draft[key] for key in ("draft_id", "sku", "channel", "environment", "version", "content", "binding")}


def review(conn, draft, unit=None, inspection=None, eligibility=None):
    # `unit`, `inspection` and `eligibility` may be pre-loaded when many drafts
    # are reviewed in one pass, so `listing()` does not re-read the same owned
    # unit once per draft and once again per approval. The defaults keep every
    # single-draft caller unchanged.
    unit = inventory._unit(conn, draft["sku"]) if unit is None else unit
    inspection = _inspection(conn, draft["sku"]) if inspection is None else inspection
    binding = draft["binding"]
    preview = inventory.preview(unit, inspection, draft["channel"], binding["reviews"])
    reasons = [reason["message"] for reason in preview["reasons"]]
    stale = (binding["unit"] != preview["inputs"]["unit"] or binding["inspection"] != inspection or
             binding["rules_sha256"] != inventory.rules_sha256() or
             binding["template_version"] != TEMPLATE_VERSION or
             binding["account_binding"] != _connection(draft["channel"], draft["environment"], preview["reviews"]))
    # New policy evidence also invalidates the old draft, even if facts stayed put.
    if _binding(conn, unit, draft["channel"], draft["environment"],
                inspection, eligibility) != binding:
        stale = True
    if stale:
        reasons.insert(0, "Facts, inspection, channel review, connection or template changed. Save a fresh draft revision before review.")
    for field in ("brand", "model"):
        if not unit["facts"].get(field):
            reasons.append("Record the inspected " + field + " in inventory.")
    for field in ("included_items", "defects"):
        if not draft["content"]["disclosures"][field]:
            reasons.append("Record " + field.replace("_", " ") + "; explicitly state none when applicable.")
    return {"ready": not reasons, "stale": stale, "reasons": reasons,
            "inspection_pk": preview["inspection_pk"], "valid_until": preview["valid_until"]}


def save(client_token, sku, expected_unit_version, channel, environment, price_eur,
         disclosures, draft_id=None, expected_version=None):
    token = inventory._token(client_token)
    _target(channel, environment)
    inventory._integer(expected_unit_version, "unit version")
    if draft_id is not None:
        inventory._integer(expected_version, "draft version")
    elif expected_version is not None:
        raise ValueError("draft ID is required with a draft version")
    fingerprint = inventory._hash({"sku": sku, "unit_version": expected_unit_version,
        "channel": channel, "environment": environment, "price": _price(price_eur),
        "disclosures": disclosures, "draft_id": draft_id, "version": expected_version})
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "draft:" + token)
            previous = inventory._replay(conn, "draft_events", token, fingerprint)
            if previous:
                return {"draft_id": previous["draft_id"], "version": previous["version"], "replayed": True}
            unit = inventory._unit(conn, sku, lock=True)
            inventory._version(unit, expected_unit_version)
            content = _content(unit, price_eur, disclosures)
            binding = _binding(conn, unit, channel, environment)
            stamp = inventory._now().isoformat()
            if draft_id is not None:
                old = _draft(conn, draft_id, lock=True)
                if (old["sku"], old["channel"], old["environment"]) != (sku, channel, environment):
                    raise ValueError("a draft's SKU and channel connection cannot change")
                if old["version"] != expected_version:
                    raise Conflict("This draft changed. Reload it before saving.")
                version = old["version"] + 1
                conn.execute("UPDATE listing_drafts SET version=?, unit_version=?, content_json=?, binding_json=?, updated_at=? WHERE draft_id=?",
                             (version, unit["version"], inventory._json(content), inventory._json(binding), stamp, draft_id))
            else:
                draft_id, version = uuid.uuid4().hex, 1
                conn.execute("INSERT INTO listing_drafts (draft_id, sku, channel, environment, version, unit_version, content_json, binding_json, created_at, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (draft_id, sku, channel, environment, version, unit["version"],
                             inventory._json(content), inventory._json(binding), stamp, stamp))
            conn.execute("INSERT INTO draft_events (draft_id, version, client_token, request_sha256, recorded_at, snapshot_json) VALUES (?, ?, ?, ?, ?, ?)",
                         (draft_id, version, token, fingerprint, stamp, inventory._json(_snapshot(_draft(conn, draft_id)))))
            return {"draft_id": draft_id, "version": version, "replayed": False}
    finally:
        conn.close()


def approve(client_token, draft_id, expected_version, reviewer, evidence_ref, facts_confirmed):
    token = inventory._token(client_token)
    inventory._integer(expected_version, "draft version")
    reviewer = inventory._text(reviewer, "reviewer", 80, True)
    evidence_ref = inventory._text(evidence_ref, "review evidence", 500, True)
    if facts_confirmed is not True:
        raise ValueError("Confirm the title, description, included items, defects and price against owned evidence")
    fingerprint = inventory._hash({"draft_id": draft_id, "version": expected_version,
                                  "reviewer": reviewer, "evidence_ref": evidence_ref, "facts_confirmed": True})
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "approval:" + token)
            previous = inventory._replay(conn, "draft_approvals", token, fingerprint)
            if previous:
                return {"draft_id": draft_id, "approval_id": previous["approval_id"], "replayed": True}
            draft = _draft(conn, draft_id)
            inventory._unit(conn, draft["sku"], lock=True)
            draft = _draft(conn, draft_id, lock=True)
            if draft["version"] != expected_version:
                raise Conflict("This draft changed. Reload it before reviewing.")
            result = review(conn, draft)
            if not result["ready"]:
                raise Conflict("Draft needs review: " + " ".join(result["reasons"]))
            snapshot, approval_id = _snapshot(draft), uuid.uuid4().hex
            conn.execute("INSERT INTO draft_approvals (approval_id, draft_id, draft_version, client_token, request_sha256, reviewer, evidence_ref, snapshot_sha256, snapshot_json, recorded_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (approval_id, draft_id, expected_version, token, fingerprint,
                         reviewer, evidence_ref, inventory._hash(snapshot), inventory._json(snapshot), inventory._now().isoformat()))
            return {"draft_id": draft_id, "approval_id": approval_id, "replayed": False}
    finally:
        conn.close()


def approval_check(conn, approval, draft=None, unit=None, inspection=None, eligibility=None):
    draft = _draft(conn, approval["draft_id"]) if draft is None else draft
    result = review(conn, draft, unit, inspection, eligibility)
    if inventory._hash(_snapshot(draft)) != approval["snapshot_sha256"]:
        result["ready"], result["stale"] = False, True
        result["reasons"].insert(0, "The approved draft revision changed. Review the current revision.")
    return result


def get(draft_id):
    from . import outbox
    with db.read_snapshot():
        conn = db.connect()
        draft = _draft(conn, draft_id)
        draft["current_unit_version"] = inventory._unit(conn, draft["sku"])["version"]
        draft["review"] = review(conn, draft)
        draft["approvals"] = []
        for row in db.query("SELECT * FROM draft_approvals WHERE draft_id=? ORDER BY recorded_at DESC, approval_id DESC LIMIT 20", (draft_id,)):
            draft["approvals"].append({"approval_id": row["approval_id"], "draft_version": row["draft_version"],
                "reviewer": row["reviewer"], "evidence_ref": row["evidence_ref"], "recorded_at": row["recorded_at"],
                "current": approval_check(conn, row)["ready"]})
        draft["actions"] = [outbox._view(conn, row) for row in db.query(
            "SELECT o.* FROM action_outbox o JOIN draft_approvals a ON a.approval_id=o.approval_id WHERE a.draft_id=? ORDER BY o.created_at DESC LIMIT 30", (draft_id,))]
        draft["history"] = db.query("SELECT version, recorded_at, snapshot_json FROM draft_events WHERE draft_id=? ORDER BY version DESC LIMIT 30", (draft_id,))
        for row in draft["history"]:
            row["snapshot"] = json.loads(row.pop("snapshot_json"))
        draft["publication_enabled"] = False
        return draft


def _listing_rows(conn, draft_ids):
    """Review every draft on one page with a fixed number of queries.

    `get()` per row was the old shape and it was quadratic in the worst way: 25
    drafts became 25 `get()` calls, each re-reading its unit, its inspection,
    its eligibility, and every one of its approvals, each approval re-running
    the whole review again. One page was thousands of round trips.

    Drafts, approvals, units and inspections are now read once each for the
    page, keyed by draft id and SKU, and `review()` receives the pre-loaded unit
    and inspection. `approval_check()` is still per approval because it compares
    that approval's recorded snapshot against the current one -- but it now
    reuses the already-loaded unit rather than fetching its own.
    """
    if not draft_ids:
        return []
    marks = ",".join("?" for _ in draft_ids)
    drafts = {}
    for row in conn.execute("SELECT * FROM listing_drafts WHERE draft_id IN (" + marks + ")",
                            tuple(draft_ids)).fetchall():
        entry = dict(row)
        entry["content"] = json.loads(entry.pop("content_json"))
        entry["binding"] = json.loads(entry.pop("binding_json"))
        drafts[entry["draft_id"]] = entry

    approvals = {}
    for row in conn.execute(
            "SELECT * FROM draft_approvals WHERE draft_id IN (" + marks + ") "
            "ORDER BY recorded_at DESC, approval_id DESC", tuple(draft_ids)).fetchall():
        approvals.setdefault(row["draft_id"], []).append(dict(row))

    units = inventory._units(conn, [draft["sku"] for draft in drafts.values()])
    inspections = {}
    skus = sorted(units)
    if skus:
        marks = ",".join("?" for _ in skus)
        rows = conn.execute("SELECT * FROM inspections WHERE sku IN (" + marks +
                            ") ORDER BY inspection_pk DESC", tuple(skus)).fetchall()
        seen = set()
        for row in rows:
            inspected = inventory._inspection(row)
            if not inspected:
                continue
            inspected = json.loads(inventory._json(inspected))
            # Latest inspection per SKU wins; rows arrive newest-first.
            if inspected["sku"] not in seen:
                seen.add(inspected["sku"])
                inspections[inspected["sku"]] = inspected

    channels = sorted({draft["channel"] for draft in drafts.values()})
    eligibility = {}
    if skus and channels:
        sku_marks = ",".join("?" for _ in skus)
        chan_marks = ",".join("?" for _ in channels)
        for row in conn.execute(
                "SELECT sku, channel, result_json FROM platform_eligibility "
                "WHERE sku IN (" + sku_marks + ") AND channel IN (" + chan_marks + ") "
                "ORDER BY evaluation_pk DESC", tuple(skus) + tuple(channels)).fetchall():
            # Latest evaluation per (sku, channel) wins; newest-first ordering
            # means the first row seen for a pair is the current one.
            eligibility.setdefault((row["sku"], row["channel"]), (row["result_json"],))

    items = []
    for draft_id in draft_ids:
        draft = drafts.get(draft_id)
        if draft is None:
            continue
        unit = units.get(draft["sku"])
        inspection = inspections.get(draft["sku"])
        found = eligibility.get((draft["sku"], draft["channel"]))
        entry = {key: draft[key] for key in ("draft_id", "sku", "channel", "environment",
                                             "version", "content", "updated_at")}
        entry["review"] = (review(conn, draft, unit, inspection, found) if unit is not None
                           else {"ready": False, "stale": False,
                                 "reasons": ["Owned unit not found."],
                                 "inspection_pk": None, "valid_until": None})
        entry["approvals"] = []
        for row in approvals.get(draft_id, [])[:20]:
            entry["approvals"].append({
                "approval_id": row["approval_id"], "draft_version": row["draft_version"],
                "reviewer": row["reviewer"], "evidence_ref": row["evidence_ref"],
                "recorded_at": row["recorded_at"],
                "current": (approval_check(conn, row, draft, unit, inspection, found)["ready"]
                            if unit is not None else False)})
        items.append(entry)
    return items


def listing(sku="", page=1):
    from . import outbox
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        raise ValueError("page must be a positive integer") from None
    sku = inventory._text(sku, "SKU", 64)
    clause, params = (" WHERE sku=?", [sku]) if sku else ("", [])
    with db.read_snapshot():
        conn = db.connect()
        total = db.scalar("SELECT COUNT(*) FROM listing_drafts" + clause, params)
        pages = max(1, (total + 24) // 25)
        page = min(page, pages)
        ids = [row["draft_id"] for row in db.query(
            "SELECT draft_id FROM listing_drafts" + clause +
            " ORDER BY updated_at DESC, draft_id LIMIT 25 OFFSET ?", params + [(page - 1) * 25])]
        items = _listing_rows(conn, ids)
        units = db.query("SELECT sku, title, version FROM inventory_units WHERE state='in_stock' ORDER BY updated_at DESC LIMIT 200")
        queue = [outbox._view(db.connect(), row) for row in db.query("SELECT * FROM action_outbox ORDER BY updated_at DESC LIMIT 50")]
        counts = {row["state"]: row["n"] for row in db.query("SELECT state, COUNT(*) AS n FROM action_outbox GROUP BY state")}
    return {"items": items, "total": total, "page": page, "pages": pages, "units": units,
            "queue": queue, "queue_counts": counts, "template_version": TEMPLATE_VERSION, "publication_enabled": False}


def action(payload):
    from . import outbox
    writers = {
        "save": (save, {"client_token", "sku", "expected_unit_version", "channel", "environment", "price_eur", "disclosures", "draft_id", "expected_version"},
                 {"client_token", "sku", "expected_unit_version", "channel", "environment", "price_eur", "disclosures"}),
        "approve": (approve, {"client_token", "draft_id", "expected_version", "reviewer", "evidence_ref", "facts_confirmed"},
                    {"client_token", "draft_id", "expected_version", "reviewer", "evidence_ref", "facts_confirmed"}),
        "enqueue": (outbox.enqueue, {"approval_id"}, {"approval_id"}),
        "check": (outbox.refresh, {"action_id"}, {"action_id"}),
        "cancel": (outbox.cancel, {"action_id", "reason"}, {"action_id", "reason"}),
    }
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
        raise ValueError("action and params are required")
    name, params = payload.get("action"), payload["params"]
    if not isinstance(name, str) or name not in writers:
        raise ValueError("unsupported draft action")
    writer, allowed, required = writers[name]
    if set(params) - allowed or not required <= set(params):
        raise ValueError("unsupported or missing draft fields")
    return {"ok": True, "result": writer(**params)}
