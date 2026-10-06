"""Read-only decision attribution from retained acquisition receipts."""

from collections import defaultdict
from datetime import datetime, timezone

from . import db, metrics


def _classify(rows, now):
    result = dict(status="invalid", decision=None, attempt_id=None,
                  reason="More than one receipt claims this physical acquisition")
    if len(rows) != 1:
        return result
    row = rows[0]
    result["attempt_id"] = row["attempt_id"]
    result["decision_pk"] = row["decision_pk"]
    if (row["status"] != "acquired" or not row["sku"]
            or not isinstance(row["evidence_ref"], str) or not row["evidence_ref"].strip()):
        result["reason"] = "Acquisition receipt needs an acquired outcome, received SKU and evidence reference"
        return result
    if (not row["purchase_source"] or
            (row["purchase_platform"], row["purchase_source"]) !=
            (row["decision_platform"], row["decision_source"])):
        result["reason"] = "Acquisition receipt does not match the decision's recorded source"
        return result
    decision, purchase, created, occurred, recorded = (metrics.parse_ts(row[key]) for key in
        ("decision_recorded_at", "purchased_at", "unit_created_at", "occurred_at", "recorded_at"))
    if (not all((decision, purchase, created, occurred, recorded))
            or not decision <= purchase <= occurred <= recorded <= now
            or not purchase <= created <= recorded):
        result["reason"] = "Acquisition receipt chronology is missing, inconsistent or future-dated"
        return result
    result.update(status="explicit", reason="Decision explicitly linked to this received physical acquisition",
                  decision=dict(decision_pk=row["decision_pk"], decision=row["decision"],
                                recorded_at=row["decision_recorded_at"]))
    return result


def load(purchase_ids, *, decision_ids=(), now=None):
    """Batch authoritative receipts, including decisions outside a journal page.

    No source-ID overlap or latest-decision rule can substitute for a receipt.
    Current purchase corrections may invalidate its link but never rewrite it.
    Callers pin these reads to their existing database snapshot.
    """
    now = now or datetime.now(timezone.utc)
    purchase_ids = sorted({pk for pk in purchase_ids if pk is not None})
    decision_ids = sorted({pk for pk in decision_ids if pk is not None})
    groups, seen = defaultdict(list), set()
    for offset in range(0, max(len(purchase_ids), len(decision_ids)), 400):
        clauses, params = [], []
        for column, ids in (("a.purchase_pk", purchase_ids), ("a.decision_pk", decision_ids)):
            batch = ids[offset:offset+400]
            if batch:
                clauses.append(column+" IN ("+",".join("?" for _ in batch)+")")
                params.extend(batch)
        rows = db.query("SELECT a.*,p.platform AS purchase_platform,p.source_listing_id AS purchase_source,"
            "p.purchased_at,u.sku,u.created_at AS unit_created_at,d.platform AS decision_platform,"
            "d.source_listing_id AS decision_source,d.recorded_at AS decision_recorded_at,d.decision "
            "FROM acquisition_attempts a LEFT JOIN purchases p ON p.purchase_pk=a.purchase_pk "
            "LEFT JOIN inventory_units u ON u.purchase_pk=p.purchase_pk "
            "LEFT JOIN research_decisions d ON d.decision_pk=a.decision_pk "
            "WHERE a.purchase_pk IS NOT NULL AND ("+" OR ".join(clauses)+")", params)
        for row in rows:
            if row["attempt_id"] not in seen:
                seen.add(row["attempt_id"])
                groups[row["purchase_pk"]].append(row)
    return {pk: dict(_classify(rows, now), purchase_pk=pk) for pk, rows in groups.items()}
