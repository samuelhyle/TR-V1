"""Read-only item context joining retained evidence without inferring attribution."""

import json
from datetime import datetime, timezone

from . import acquisition_evidence, db, inventory, metrics, owned_outcomes, source_review, product_profiles

VERSION = "item-workspace-v1"
HISTORY_LIMIT = 50
SEEDS = {"listing_pk", "decision_pk", "sku", "draft_id", "order_id"}


def _json(value):
    try:
        result = json.loads(value) if value else None
        return result if isinstance(result, dict) else None
    except (ValueError, TypeError):
        return None


def _key(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        number = int(value)
        if str(number) != str(value) or not 0 < number < 2**63:
            raise ValueError
        return number
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a positive integer") from None


def view(**identifiers):
    """Resolve exactly one explicit entry point in a single database snapshot.

    Source-ID overlap is navigable context. Only a checked acquisition receipt
    attributes a decision to a physical purchase. Relists and model predictions
    cannot join units, and expired sources cannot be reconstructed as current.
    """
    if set(identifiers) - SEEDS or len(identifiers) != 1:
        raise ValueError("provide exactly one item identifier")
    kind, value = next(iter(identifiers.items()))
    value = _key(value, kind) if kind.endswith("_pk") else inventory._text(value, kind, 64, True)
    with db.read_snapshot():
        return _view(db.connect(), kind, value)


def _view(conn, kind, value):
    now = datetime.now(timezone.utc)
    selected_decision = None
    unit = None
    if kind == "listing_pk":
        anchor = db.query_one("SELECT platform,listing_id AS source_listing_id,title FROM listings WHERE listing_pk=?", (value,))
    elif kind == "decision_pk":
        anchor = db.query_one("SELECT * FROM research_decisions WHERE decision_pk=?", (value,))
        selected_decision = value
    else:
        if kind == "sku":
            sku = value
        else:
            table = "listing_drafts" if kind == "draft_id" else "order_lines"
            row = db.query_one(f"SELECT sku FROM {table} WHERE {kind}=?", (value,))
            if not row:
                raise ValueError("item reference not found")
            sku = row["sku"]
        unit = inventory.get(sku)
        anchor = unit["purchase"]
    if not anchor:
        raise ValueError("item reference not found")
    platform, source_id = anchor["platform"], anchor["source_listing_id"]
    listing = db.query_one("SELECT listing_pk,platform,subplatform,listing_id,title,last_seen_at,listed_price_eur,status_norm FROM listings WHERE platform=? AND listing_id=?", (platform, source_id)) if source_id else None
    context = source_review.view(listing["listing_pk"]) if listing else None
    if unit:
        purchase = unit["purchase"]
    elif source_id:
        purchase = db.query_one("SELECT p.*,u.sku FROM purchases p LEFT JOIN inventory_units u ON u.purchase_pk=p.purchase_pk WHERE p.platform=? AND p.source_listing_id=?", (platform, source_id))
    else:
        purchase = None
    if unit is None and purchase and purchase["sku"]:
        unit = inventory.get(purchase["sku"])
    purchase_pk = purchase["purchase_pk"] if purchase else None
    clauses, params = [], []
    if source_id:
        clauses.append("(d.platform=? AND d.source_listing_id=?)")
        params.extend((platform, source_id))
    if purchase_pk:
        clauses.append("EXISTS(SELECT 1 FROM acquisition_attempts a WHERE a.decision_pk=d.decision_pk AND a.purchase_pk=?)")
        params.append(purchase_pk)
    if selected_decision:
        clauses.append("d.decision_pk=?")
        params.append(selected_decision)
    where = " WHERE " + " OR ".join(clauses) if clauses else " WHERE 1=0"
    # The explicitly selected record is available even beyond the history cap.
    rows = db.query("SELECT d.* FROM research_decisions d" + where + " ORDER BY d.decision_pk DESC LIMIT ?", params+[HISTORY_LIMIT])
    if selected_decision and not any(row["decision_pk"] == selected_decision for row in rows):
        rows.append(anchor)
    if selected_decision:
        rows.sort(key=lambda row: row["decision_pk"] != selected_decision)
    links = acquisition_evidence.load([purchase_pk], decision_ids=[row["decision_pk"] for row in rows], now=now)
    decisions = []
    for row in rows:
        linked = [link for link in links.values() if link.get("decision_pk") == row["decision_pk"]]
        explicit = next((link for link in linked if link["status"] == "explicit"), None)
        evidence, scenario, checks = (_json(row[key]) for key in ("evidence_json", "scenario_json", "checks_json"))
        decisions.append(dict(decision_pk=row["decision_pk"], listing_pk=row["listing_pk"], platform=row["platform"], source_listing_id=row["source_listing_id"],
                              recorded_at=row["recorded_at"], decision=row["decision"], notes=row["notes"], checks=checks, evidence=evidence, scenario=scenario,
                              snapshot_status="retained" if evidence is not None and checks is not None else "unreadable",
                              relation="receipt_linked" if explicit else "invalid_receipt" if linked else "source_context",
                              acquisition_links=linked))
    attempts = db.query("SELECT a.attempt_id,a.decision_pk,a.purchase_pk,a.status,a.reason,a.evidence_ref,a.occurred_at,a.recorded_at FROM acquisition_attempts a JOIN research_decisions d ON d.decision_pk=a.decision_pk" + where + " ORDER BY a.recorded_at DESC,a.attempt_id LIMIT ?", params+[HISTORY_LIMIT])
    outcome = owned_outcomes._report(conn, purchase_pk) if purchase_pk else None
    drafts, orders = [], []
    if unit:
        drafts = db.query("SELECT draft_id,sku,channel,version,unit_version,updated_at FROM listing_drafts WHERE sku=? ORDER BY updated_at DESC,draft_id LIMIT ?", (unit["sku"], HISTORY_LIMIT))
        for row in drafts:
            row["binding_current"] = row["unit_version"] == unit["version"]
        orders = db.query("SELECT order_id,sku,cycle,channel,state,fulfilment,version,sold_at,updated_at,evidence_ref,resale_pairing_json FROM order_lines WHERE sku=? ORDER BY cycle DESC LIMIT ?", (unit["sku"], HISTORY_LIMIT))
        for row in orders:
            row["resale_pairing"] = _json(row.pop("resale_pairing_json"))
    title = unit["title"] if unit else (listing or {}).get("title") or anchor.get("title_raw") or anchor.get("title")
    if not title and kind == "decision_pk":
        title = (_json(anchor["evidence_json"]) or {}).get("title")
    result = dict(version=VERSION, generated_at=now.isoformat(), seed={kind: value}, title=title or "Retained item",
                  source=dict(platform=platform, listing_id=source_id, listing=listing, context=context, retained=listing is not None),
                  selected_decision_pk=selected_decision, decisions=decisions, attempts=attempts,
                  purchase={key: purchase.get(key) for key in ("purchase_pk", "platform", "source_listing_id", "purchased_at", "acquisition_cost_eur")} if purchase else None,
                  owned={key: unit.get(key) for key in ("sku", "title", "kind", "state", "version", "facts_version", "purchase_pk", "local_preview", "latest_inspection", "stock_phase")} if unit else None,
                  outcome=outcome, evidence_steps=owned_outcomes.evidence_steps(outcome) if unit else [], drafts=drafts, orders=orders,
                  limits=["Source overlap is context; only a validated receipt attributes a decision to a received physical unit.",
                          "Current source facts, retained decisions and owned physical inspections are separate evidence.",
                          "Reads do not create reviews, train models or infer a sale. Expired sources remain unavailable."])
    result["history"] = _history(conn, result, unit)
    identity = (context or {}).get('current_identity') or {}
    result['profile'] = product_profiles.summary(identity, identity.get('listing_analysis'), listing)
    from . import forecast_evidence
    result['forecasts'] = forecast_evidence.for_listings([listing['listing_pk']] if listing else [])
    counts = dict(decisions=db.scalar("SELECT COUNT(*) FROM research_decisions d"+where, params),
                  attempts=db.scalar("SELECT COUNT(*) FROM acquisition_attempts a JOIN research_decisions d ON d.decision_pk=a.decision_pk"+where, params),
                  drafts=db.scalar("SELECT COUNT(*) FROM listing_drafts WHERE sku=?", (unit["sku"],)) if unit else 0,
                  orders=db.scalar("SELECT COUNT(*) FROM order_lines WHERE sku=?", (unit["sku"],)) if unit else 0)
    result["coverage"] = dict(totals=counts, limit=HISTORY_LIMIT, truncated=[key for key, total in counts.items() if total > len(result[key])])
    revision = {key: result[key] for key in result if key != "generated_at"}
    if result["owned"]:
        # An uninspected preview's prospective expiry is computed from "now".
        # Read clocks do not constitute new evidence; readiness and its reasons do.
        revision["owned"] = {**result["owned"], "local_preview": {key: value for key, value in result["owned"]["local_preview"].items() if key != "valid_until"}}
    result["revision"] = inventory._hash(revision)
    return result


def _history(conn, result, unit):
    events = []
    def add(identifier, label, evidence_class, recorded_at, *, occurred_at=None, evidence_ref=None, action=None):
        events.append(dict(id=identifier, label=label, evidence_class=evidence_class, recorded_at=recorded_at,
                           occurred_at=occurred_at, evidence_ref=evidence_ref, action=action))
    context = result["source"]["context"]
    if context:
        for row in context["history"]:
            add("source:"+str(row["enrichment_pk"]), "Source review" if row["current"] else "Earlier / stale source review", "reviewed_source", row.get("reviewed_at"),
                occurred_at=row.get("checked_at"), evidence_ref=row.get("evidence_ref"), action={"type": "source_review"})
    for row in result["decisions"]:
        add("decision:"+str(row["decision_pk"]), "Decision: "+row["decision"], "frozen_decision", row["recorded_at"], action={"type": "decision", "id": row["decision_pk"]})
    for row in result["attempts"]:
        add("attempt:"+row["attempt_id"], "Acquisition attempt: "+row["status"], "acquisition_receipt", row["recorded_at"], occurred_at=row["occurred_at"], evidence_ref=row["evidence_ref"], action={"type": "decision", "id": row["decision_pk"]})
    if unit:
        for row in unit["history"]:
            add("unit:"+str(row["event_pk"]), "Owned item: "+row["event_type"].replace("_", " "), "owned_physical", row["recorded_at"], action={"type": "owned"})
        for row in result["drafts"]:
            add("draft:"+row["draft_id"], "Saved listing draft · revision "+str(row["version"]), "listing_preparation", row["updated_at"], action={"type": "draft", "id": row["draft_id"]})
        for row in db.query("SELECT e.event_pk,e.order_id,e.event_type,e.recorded_at,e.effective_at,e.evidence_ref FROM order_events e JOIN order_lines o ON o.order_id=e.order_id WHERE o.sku=? ORDER BY e.event_pk DESC LIMIT ?", (unit["sku"], HISTORY_LIMIT)):
            add("order:"+str(row["event_pk"]), "Order: "+row["event_type"].replace("_", " "), "actual_trade", row["recorded_at"], occurred_at=row["effective_at"], evidence_ref=row["evidence_ref"], action={"type": "order", "id": row["order_id"]})
    if result["outcome"]:
        for row in result["outcome"]["events"][-HISTORY_LIMIT:]:
            add("outcome:"+str(row["event_pk"]), "Outcome: "+row["event_type"].replace("_", " "), "owned_outcome", row["recorded_at"], occurred_at=row["effective_at"], evidence_ref=row["evidence_ref"], action={"type": "outcome", "panel": "review"})
    events.sort(key=lambda row: (metrics.parse_ts(row["recorded_at"]) or datetime.min.replace(tzinfo=timezone.utc), row["id"]), reverse=True)
    return dict(items=events[:HISTORY_LIMIT], returned=min(len(events), HISTORY_LIMIT), truncated=len(events) > HISTORY_LIMIT,
                note="Latest retained events; recording and observation times are shown separately. Older records remain in their original editors.")
