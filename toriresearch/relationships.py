"""Reversible listing relationships, with no inferred public units or sales."""

import argparse
import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from . import db

VERSION = "listing-relationships-v1"
KINDS = {"duplicate", "possible_relist", "listing_version"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def latest(conn=None):
    query = ("SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY claim_id ORDER BY revision DESC) AS r "
             "FROM listing_relationship_events) AS reviews WHERE r=1")
    return [dict(r) for r in conn.execute(query).fetchall()] if conn else db.query(query)


def record(left_pk, right_pk, kind, state, basis, reason, client_token, sku=None, reference=None):
    """Record an explicit operator attestation; retries and reversals retain history.

    Owned verification requires an existing SKU anchored by its purchase or
    published channel listing. The second listing's association is the explicit
    operator attestation, never a text-match decision by this code.
    """
    if any(type(pk) is not int or pk <= 0 for pk in (left_pk, right_pk)) or left_pk == right_pk:
        raise ValueError("Two distinct positive listing keys are required")
    left_pk, right_pk = sorted((left_pk, right_pk))
    if kind not in KINDS or state not in {"accepted", "retracted"} or basis not in {"operator_review", "owned_inventory"}:
        raise ValueError("Unsupported relationship kind, state or evidence basis")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("A relationship reason of 1 to 500 characters is required")
    if (basis == "owned_inventory") != bool(sku):
        raise ValueError("Owned evidence requires a SKU; public reviews cannot claim one")
    try:
        token = str(uuid.UUID(client_token))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A UUID submission token is required") from None
    claim = _digest([left_pk, right_pk, kind])
    fingerprint = _digest([claim, state, basis, reason, sku])
    lock, error = db.acquire_lock("listing_relationships")
    if lock is None:
        raise RuntimeError(error)
    conn = db.connect()
    try:
        with conn.transaction():
            if conn.dialect == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(?)", (729814621,))
            prior = conn.execute("SELECT * FROM listing_relationship_events WHERE event_id=?", (token,)).fetchone()
            if prior:
                if prior["request_sha256"] != fingerprint:
                    raise ValueError("Submission token already used with different contents")
                return dict(prior)
            rows = [dict(r) for r in conn.execute("SELECT * FROM listings WHERE listing_pk IN (?,?)", (left_pk, right_pk)).fetchall()]
            if len(rows) != 2:
                raise ValueError("Both listings must exist")
            current = latest(conn)
            previous = next((r for r in current if r["claim_id"] == claim), None)
            if state == "retracted" and (not previous or previous["state"] != "accepted"):
                raise ValueError("Only an accepted relationship can be retracted")
            if basis == "owned_inventory" and state == "accepted":
                owned = conn.execute("SELECT p.platform,p.source_listing_id,p.sold_listing_pk FROM inventory_units u "
                                     "JOIN purchases p ON p.purchase_pk=u.purchase_pk WHERE u.sku=?", (sku,)).fetchone()
                if not owned:
                    raise ValueError("Owned SKU must exist")
                channels = conn.execute("SELECT channel,external_id FROM channel_listings WHERE sku=?", (sku,)).fetchall()
                anchored = any((r["platform"] == owned["platform"] and r["listing_id"] == owned["source_listing_id"])
                               or r["listing_pk"] == owned["sold_listing_pk"]
                               or any(r["platform"] == c["channel"] and r["listing_id"] == c["external_id"] for c in channels)
                               for r in rows)
                if not anchored:
                    raise ValueError("One listing must be anchored to the owned purchase or channel listing")
                if any(r["state"] == "accepted" and r["basis"] == "owned_inventory" and r["sku"] != sku
                       and {left_pk, right_pk} & {r["left_pk"], r["right_pk"]} for r in current):
                    raise ValueError("Relationship conflicts with an existing owned SKU")
            revision = (previous["revision"] if previous else 0)+1
            stamp = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
            conn.execute("INSERT INTO listing_relationship_events "
                         "(event_id,claim_id,revision,left_pk,right_pk,kind,state,basis,sku,reason,request_sha256,recorded_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (token,claim,revision,left_pk,right_pk,kind,state,basis,sku,reason,fingerprint,stamp))
            return dict(conn.execute("SELECT * FROM listing_relationship_events WHERE event_id=?", (token,)).fetchone())
    finally:
        conn.close()
        db.release_lock(lock)


def suggestions(rows, limit=50):
    """Same retained seller and exact normalized title: a review hint only.

    Star links bound output even when a seller reuses one title for many units.
    A seller may have separate identical items; missing seller keys never match.
    """
    groups = defaultdict(list)
    for row in rows:
        title = re.sub(r"\W+", " ", (row.get("title") or "").casefold()).strip()
        if row.get("seller_id") and len(title) >= 12:
            groups[(row.get("platform"), row["seller_id"], title)].append(row)
    result, pairs = [], 0
    for group in groups.values():
        pairs += len(group)*(len(group)-1)//2
        group.sort(key=lambda r: r["listing_pk"])
        for row in group[1:]:
            if len(result) < limit:
                result.append(dict(left_pk=group[0]["listing_pk"], right_pk=row["listing_pk"],
                                   kind="possible_relist", basis="same_retained_seller_and_title",
                                   verified_same_unit=False, reason="Seller may offer distinct identical units"))
    return dict(candidate_pairs=pairs, returned=len(result), links=result,
                truncated=pairs > len(result), version=VERSION)


def summary(rows):
    ids = {r["listing_pk"] for r in rows if r.get("listing_pk") is not None}
    current = [r for r in (latest() if ids else []) if r["state"] == "accepted" and ids & {r["left_pk"], r["right_pk"]}]
    labels = {pk: r["sku"] for r in current if r["basis"] == "owned_inventory"
              for pk in (r["left_pk"], r["right_pk"]) if pk in ids}
    claims = {}
    conflicts = 0
    for row in rows:
        evidence = row.get("product_attribute_evidence", {})
        values = {a["value"] for a in evidence.get("assertions", []) if a["attribute"] == "quantity"}
        if len(values) == 1:
            claims[row["listing_pk"]] = next(iter(values))
        elif len(values) > 1:
            conflicts += 1
    return dict(version=VERSION, observed_listings=len(ids), verified_owned_units=len(set(labels.values())),
                listings_linked_to_owned_units=len(labels), public_physical_units=None,
                listings_without_verified_unit=len(ids-labels.keys()),
                accepted_public_relationships=sum(r["basis"] != "owned_inventory" for r in current),
                seller_quantity_claims=len(claims), seller_multiunit_claims=sum(v > 1 for v in claims.values()),
                conflicting_quantity_claims=conflicts,
                quantity_assessed_listings=sum("product_attribute_evidence" in r for r in rows),
                quantity_unknown_listings=len(ids)-len(claims),
                note="Listing counts and sale markers are observations, not physical-unit supply or transaction volume. Public links and quantity claims never verify units. Owned SKUs count once across accepted links; reversals change only this ledger.")


def comparison_admissions(rows, keys):
    """One current representative for explicit duplicate/version links only.

    Possible relists and similar titles stay separate observations. Conflicting
    admitted comparison identities block a linked component pending review.
    This does not estimate public physical-unit supply.
    """
    by_pk = {row["listing_pk"]: row for row in rows if row["listing_pk"] in keys}
    parents = {pk: pk for pk in by_pk}

    def root(pk):
        while parents[pk] != pk:
            parents[pk] = parents[parents[pk]]
            pk = parents[pk]
        return pk

    for link in latest() if by_pk else []:
        left, right = link["left_pk"], link["right_pk"]
        if (link["state"] == "accepted" and link["kind"] in {"duplicate", "listing_version"}
                and left in by_pk and right in by_pk):
            parents[root(right)] = root(left)
    components = defaultdict(list)
    for pk in by_pk:
        components[root(pk)].append(pk)
    result = {}
    for members in components.values():
        if len(members) < 2:
            continue
        if len({keys[pk] for pk in members}) > 1:
            result.update({pk: dict(eligible=False, reason="linked_identity_conflict") for pk in members})
            continue
        from . import metrics
        representative = max(members, key=lambda pk: (
            (metrics.parse_ts(by_pk[pk].get("price_observed_at") or by_pk[pk].get("last_seen_at"))
             or datetime.min.replace(tzinfo=timezone.utc)), pk))
        result.update({pk: dict(eligible=False, reason="duplicate_observation", representative_pk=representative)
                       for pk in members if pk != representative})
    return result


def versions(listing_pk, limit=100):
    """Read bounded source versions from the existing observation journal.

    Repeated identical facts are checks, not new units. Source streams stay
    separate so a sparse search response cannot erase rich detail facts.
    """
    if type(listing_pk) is not int or listing_pk <= 0:
        raise ValueError("Listing key must be positive")
    from . import product_attributes
    rows = db.query("SELECT observation_pk,observed_at,source,fields_json FROM listing_observations "
                    "WHERE listing_pk=? ORDER BY observation_pk", (listing_pk,))
    previous, result = {}, []
    keys = ("title","model_norm","category_raw_id","listed_price_eur","status_norm","outcome_status","coverage")
    for row in rows:
        fields = json.loads(row["fields_json"])
        facts = {key: fields[key] for key in keys if key in fields}
        attributes = fields.get("product_attributes")
        if attributes is not None:
            facts["product_attributes"] = product_attributes.facts_digest(attributes["assertions"])
        prior = previous.get(row["source"])
        changed = [key for key in sorted(set(facts) | set(prior or {})) if prior is None or facts.get(key) != prior.get(key)]
        if prior is None or changed:
            result.append(dict(observation_pk=row["observation_pk"],observed_at=str(row["observed_at"]),source=row["source"],
                               input_sha256=_digest(facts), changed_fields=changed))
        previous[row["source"]] = facts
    return dict(listing_pk=listing_pk, retained_observations=len(rows), retained_source_versions=len(result),
                versions=result[-limit:], truncated=len(result)>limit,
                basis="Stable listing identity, not verified physical-unit continuity")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", type=int, metavar="LISTING_PK")
    parser.add_argument("--left", type=int)
    parser.add_argument("--right", type=int)
    parser.add_argument("--kind", choices=sorted(KINDS))
    parser.add_argument("--state", choices=("accepted", "retracted"), default="accepted")
    parser.add_argument("--basis", choices=("operator_review", "owned_inventory"), default="operator_review")
    parser.add_argument("--sku")
    parser.add_argument("--reason")
    parser.add_argument("--token")
    args = parser.parse_args()
    if args.versions is not None:
        print(json.dumps(versions(args.versions), indent=2))
    elif any(value is not None for value in (args.left, args.right, args.kind)):
        print(json.dumps(record(args.left,args.right,args.kind,args.state,args.basis,args.reason,args.token,args.sku), indent=2))
    else:
        from . import product_attributes
        rows = product_attributes.attach(db.query("SELECT listing_pk,platform,title,seller_id FROM listings WHERE platform='tori'"))
        print(json.dumps(dict(summary=summary(rows), suggestions=suggestions(rows)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
