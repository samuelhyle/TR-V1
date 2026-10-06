"""Source-bound review imports and frozen development/holdout audit manifests.

No collection requests, model calls or ground-truth guesses occur here. The
enrichment journal retains predictions at export; editing a JSON template cannot
replace those predictions. Human unit grouping remains an explicit attestation.
"""

import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import db, product_identity as identity

AUDIT_TASK = "product_identity_audit"
GROUP_TASK = "product_identity_review_group"
HINT_TASK = "product_identity_review_lineage"
OUTCOMES = {"supported", "unknown", "accessory", "bundle", "multiple_units", "unsupported_model",
            "parser_error", "outside_pilot", "unavailable"}
BASES = {"seller_statement", "physical_inspection"}


def _uuid(value, name):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{name} must be a UUID") from None


def _source(row):
    from .research import market_url
    return dict(platform=row["platform"], listing_id=row["listing_id"],
                public_url=market_url(row["platform"], row["listing_id"]),
                observed_at=str(row.get("source_observed_at") or row.get("last_seen_at") or "") or None,
                input_sha256=identity.fact_digest(row))


def _unit_hint(row):
    if row.get("canonical_item_id"):
        return identity._hash(["canonical", row["canonical_item_id"]])
    if row.get("relist_lineage_id"):
        return identity._hash(["lineage", row["platform"], row["relist_lineage_id"]])
    return None


def eligible_rows(rows, purpose):
    """Keep exported diagnostic samples out of holdout selection, and vice versa."""
    blocked, hints = set(), {_unit_hint(row) for row in rows if _unit_hint(row)}
    for offset in range(0,len(rows),500):
        keys = [row["listing_pk"] for row in rows[offset:offset+500]]
        entries = db.query("SELECT listing_pk,task,output_json FROM enrichment WHERE task IN (?,?,?,?) AND listing_pk IN ("+
                           ",".join("?" for _ in keys)+")", [AUDIT_TASK,identity.REVIEW_TASK,"listing_analysis_audit","listing_analysis_review"]+keys)
        for entry in entries:
            record = json.loads(entry["output_json"])
            if record.get("purpose", "development") != purpose or (purpose == "holdout" and entry["task"] in {identity.REVIEW_TASK,"listing_analysis_review"}):
                blocked.add(entry["listing_pk"])
    blocked_hints = set()
    hashes = [identity._hash(["hint",hint]) for hint in hints]
    for offset in range(0,len(hashes),500):
        batch = hashes[offset:offset+500]
        entries = db.query("SELECT input_sha256,output_json FROM enrichment WHERE task IN (?,?) AND input_sha256 IN ("+
                           ",".join("?" for _ in batch)+")", [GROUP_TASK,HINT_TASK]+batch)
        blocked_hints.update(entry["input_sha256"] for entry in entries
                             if json.loads(entry["output_json"])["purpose"] != purpose)
    return [row for row in rows if row["listing_pk"] not in blocked and
            (not _unit_hint(row) or identity._hash(["hint",_unit_hint(row)]) not in blocked_hints)]


def create(rows, filters, populations, purpose="development", sampling="hash_order_per_family_and_unmatched", *, conn=None, audit_id=None):
    from . import inventory, product_attributes
    rows = product_attributes.attach(rows, conn=conn)
    if purpose not in ("development", "holdout"):
        raise ValueError("audit purpose must be development or holdout")
    if len(rows) > 500:
        raise ValueError("audit export is bounded to 500 examples")
    catalog = identity.load()
    audit_id = _uuid(audit_id, "audit_id") if audit_id else str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    reviews = []
    own_connection = conn is None
    conn = conn if conn is not None else db.connect()
    try:
        with conn.transaction():
            for pk in sorted({row["listing_pk"] for row in rows}):
                inventory._token_lock(conn, "identity-review-listing:" + str(pk))
            for row in rows:
                current = conn.execute("SELECT * FROM listings WHERE listing_pk=?", (row["listing_pk"],)).fetchone()
                current = product_attributes.attach([dict(current)], conn=conn)[0] if current else None
                if not current or identity.fact_digest(current) != identity.fact_digest(row):
                    raise ValueError("listing inputs changed during export; retry the template")
                if purpose == "holdout" and conn.execute("SELECT 1 FROM enrichment WHERE task IN (?,?) AND listing_pk=?",
                                                       (identity.REVIEW_TASK,"listing_analysis_review",row["listing_pk"])).fetchone():
                    raise ValueError("holdout must use listings without prior review labels")
                prior_audits = conn.execute("SELECT output_json FROM enrichment WHERE task IN (?,?,?) AND listing_pk=?",
                                            (AUDIT_TASK,"listing_analysis_audit","listing_analysis_review",row["listing_pk"])).fetchall()
                if any(json.loads(entry["output_json"])["purpose"] != purpose for entry in prior_audits):
                    raise ValueError("listing overlaps development and holdout samples")
                hint = _unit_hint(dict(current))
                if hint:
                    prior_groups = conn.execute("SELECT output_json FROM enrichment WHERE task IN (?,?) AND input_sha256=?",
                                                (GROUP_TASK,HINT_TASK,identity._hash(["hint",hint]))).fetchall()
                    if any(json.loads(entry["output_json"])["purpose"] != purpose for entry in prior_groups):
                        raise ValueError("known relist lineage overlaps development and holdout samples")
                prediction = identity.derive(row, catalog)
                source = _source(row)
                manifest = dict(audit_id=audit_id, purpose=purpose, created_at=created_at,
                                catalog_sha256=catalog["sha256"], rule_version=identity.RULE_VERSION,
                                comparison_policy_version=identity.COMPARISON_POLICY_VERSION,
                                source=source, prediction=prediction, unit_group_hint=_unit_hint(dict(current)),
                                inputs=dict(identity.source_inputs(row), platform=row.get("platform"), listing_id=row.get("listing_id")))
                identity._store(row, AUDIT_TASK, identity._hash([audit_id, row["listing_pk"]]), manifest, catalog, conn)
                if hint:
                    identity._store(row, GROUP_TASK, identity._hash(["hint",hint]),
                                    dict(purpose=purpose,unit_group=None), catalog, conn)
                reviews.append(dict(review_id=str(uuid.uuid4()), listing_pk=row["listing_pk"],
                                    input_sha256=prediction["input_sha256"],
                                    source=dict(source, availability="not_checked", checked_at=None),
                                    title=row.get("title"), observed_condition=row.get("condition_norm"),
                                    missing_fields=sorted(set(prediction["missing_variant_fields"]+
                                                              prediction["missing_comparison_fields"])),
                                    review_source="listing_review", reviewer=None, review_outcome=None,
                                    verification={}, unit_group=None, unit_group_hint=manifest["unit_group_hint"],
                                    group_verified=False, expected=None))
    finally:
        if own_connection:
            conn.close()
    return dict(version=2, audit_id=audit_id, purpose=purpose, created_at=created_at,
                rule_version=identity.RULE_VERSION, catalog_sha256=catalog["sha256"], filters=filters,
                sampling=sampling+"; overlapping mentions deduplicated", populations=populations,
                attribute_matrix=identity.attribute_matrix(catalog),
                instructions="Expected labels are blank. Inspect the linked source; record availability, checked_at (timezone required), reviewer and review_outcome. Each known family, model_key and attribute needs a verification basis: seller_statement or physical_inspection. Unknown values remain null. Unavailable public sources cannot supply seller labels; verified physical-unit inspection may supply inspected facts. Holdouts require a verified unit_group UUID shared by relists of one physical unit; development and holdout samples cannot overlap. Verify lineage hints rather than treating them as distinct units. Predictions are frozen separately in the local journal; never use a prediction as its own label.",
                review_outcomes=sorted(OUTCOMES), verification_bases=sorted(BASES), reviews=reviews)


def upgrade(document):
    """Keep the original blank diagnostic sample, using its current source facts."""
    if (not isinstance(document, dict) or document.get("version") != 1
            or not isinstance(document.get("reviews"), list) or not 1 <= len(document["reviews"]) <= 500):
        raise ValueError("upgrade needs a version-1 audit with 1–500 blank examples")
    rows, missing, changed, seen = [], [], [], set()
    for example in document["reviews"]:
        if not isinstance(example, dict) or example.get("expected") is not None:
            raise ValueError("only blank diagnostic templates can be upgraded; completed reviews need source validation")
        pk = example.get("listing_pk")
        if isinstance(pk, bool) or not isinstance(pk, int) or pk < 1 or pk in seen:
            raise ValueError("upgrade needs unique positive listing keys")
        seen.add(pk)
        row = db.query_one("SELECT * FROM listings WHERE listing_pk=?", (pk,))
        if not row:
            missing.append(pk)
            continue
        if row.get("title") != example.get("title"):
            changed.append(pk)
        rows.append(row)
    result = create(rows, document.get("filters", {}), document.get("populations", {}),
                    sampling="original_development_sample_with_current_source_facts")
    result["upgrade"] = dict(original_examples=len(document["reviews"]), unavailable_local_rows=missing,
                             changed_titles=changed, note="Original sample retained where rows exist; all expected labels remain blank.")
    return result


def _checked_at(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None or date > datetime.now(timezone.utc)+timedelta(minutes=5):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("source checked_at needs a past timestamp with a timezone") from None
    return date.isoformat()


def _record(example, document, conn, catalog):
    from . import analysis, product_attributes
    token = _uuid(example.get("review_id"), "review_id")
    pk = example.get("listing_pk")
    if isinstance(pk, bool) or not isinstance(pk, int) or pk < 1:
        raise ValueError("listing_pk must be a positive integer")
    row = conn.execute("SELECT "+analysis.FIELDS+", canonical_item_id, relist_lineage_id FROM listings WHERE listing_pk=?", (pk,)).fetchone()
    if not row:
        raise ValueError("listing no longer exists locally")
    row = product_attributes.attach([dict(row)], conn=conn)[0]
    manifest_row = conn.execute("SELECT output_json FROM enrichment WHERE task=? AND listing_pk=? AND input_sha256=?",
                                (AUDIT_TASK, pk, identity._hash([document["audit_id"], pk]))).fetchone()
    if not manifest_row:
        raise ValueError("audit snapshot not found; export a template in this database")
    manifest = json.loads(manifest_row["output_json"])
    if (manifest["purpose"] != document["purpose"] or manifest["catalog_sha256"] != document["catalog_sha256"]
            or manifest["rule_version"] != document["rule_version"]):
        raise ValueError("audit header differs from its frozen snapshot")
    if manifest["source"]["input_sha256"] != identity.fact_digest(row):
        raise ValueError("listing identity inputs changed; create a new template")
    if manifest.get("unit_group_hint") != _unit_hint(row):
        raise ValueError("unit lineage changed; export a new grouping snapshot")
    supplied_source = example.get("source")
    if not isinstance(supplied_source, dict) or any(supplied_source.get(key) != value for key,value in manifest["source"].items()):
        raise ValueError("source identity differs from its frozen snapshot")
    availability = supplied_source.get("availability")
    if availability not in ("available", "unavailable"):
        raise ValueError("source availability must be checked explicitly")
    source = dict(manifest["source"], availability=availability, checked_at=_checked_at(supplied_source.get("checked_at")))
    review_source = example.get("review_source")
    if review_source not in ("listing_review", "physical_inspection"):
        raise ValueError("invalid review_source")
    if availability == "available" and review_source == "listing_review" and not source["public_url"]:
        raise ValueError("listing review needs a supported public source link")
    reviewer = example.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 80 or "\n" in reviewer:
        raise ValueError("reviewer needs a short nonempty identifier")
    expected = identity._expected(example["expected"], catalog)
    known = {key for key in ("family", "model_key") if expected[key] is not None}
    known.update(key for key,value in expected["attributes"].items() if value is not None)
    verification = example.get("verification")
    if (not isinstance(verification, dict) or set(verification) != known
            or any(value not in BASES for value in verification.values())):
        raise ValueError("verification must give a valid basis for every known label and attribute")
    if review_source == "listing_review" and "physical_inspection" in verification.values():
        raise ValueError("listing_review cannot assert physical inspection")
    outcome = example.get("review_outcome")
    if outcome not in OUTCOMES:
        raise ValueError("review_outcome must identify supported, unknown or a diagnostic reason")
    if outcome == "unavailable" and availability != "unavailable":
        raise ValueError("unavailable outcome requires unavailable source")
    if outcome == "unknown" and known:
        raise ValueError("unknown outcome must leave labels unknown")
    if outcome == "outside_pilot" and expected["family"] != "outside_pilot":
        raise ValueError("outside_pilot outcome needs an explicit outside label")
    if outcome == "unsupported_model" and (expected["family"] not in catalog["families"] or expected["model_key"] is not None):
        raise ValueError("unsupported_model needs a known family and unknown model")
    if outcome == "supported" and expected["model_key"] is None:
        raise ValueError("supported outcome needs a known model")
    if outcome in ("accessory", "bundle", "multiple_units") and expected["attributes"]["offer_kind"] != outcome:
        raise ValueError("diagnostic offer outcome must match offer_kind")
    group = example.get("unit_group")
    group = _uuid(group, "unit_group") if group is not None else None
    if not isinstance(example.get("group_verified"), bool):
        raise ValueError("group_verified must be a boolean")
    if document["purpose"] == "holdout" and (not group or not example["group_verified"]):
        raise ValueError("holdout needs a verified physical-unit group")
    if group and not example["group_verified"]:
        raise ValueError("unit_group requires explicit verification")
    physical_source = (review_source == "physical_inspection" and bool(group) and example["group_verified"]
                       and all(value == "physical_inspection" for value in verification.values()))
    if availability == "unavailable" and ((known and not physical_source) or (not known and outcome != "unavailable")):
        raise ValueError("unavailable source cannot establish expected labels without verified physical-unit inspection")
    hint = manifest.get("unit_group_hint")
    hashes = ([identity._hash(group)] if group else [])+([identity._hash(["hint",hint])] if hint else [])
    if hashes:
        markers = conn.execute("SELECT output_json FROM enrichment WHERE task IN (?,?) AND input_sha256 IN ("+
                               ",".join("?" for _ in hashes)+")", [GROUP_TASK,HINT_TASK]+hashes).fetchall()
        if any(json.loads(marker["output_json"])["purpose"] != document["purpose"] for marker in markers):
            raise ValueError("physical unit overlaps development and holdout")
        if document["purpose"] == "holdout" and any(json.loads(marker["output_json"]).get("unit_group") not in (None,group)
                                                   for marker in markers):
            raise ValueError("known relist lineage must share one verified unit_group")
    if document["purpose"] == "holdout":
        prior = conn.execute("SELECT output_json FROM enrichment WHERE task=? AND listing_pk=?",
                             (identity.REVIEW_TASK, pk)).fetchall()
        if any(json.loads(entry["output_json"]).get("purpose", "development") != "holdout" for entry in prior):
            raise ValueError("listing already has development labels")
    annotations = {}
    from . import inventory
    for key, limit in (("evidence_ref", 500), ("review_note", 1000)):
        if key in example:
            annotations[key] = inventory._text(example[key], key, limit, True)
    if "supersedes_review_id" in example:
        annotations["supersedes_review_id"] = _uuid(example["supersedes_review_id"], "superseded review") if example["supersedes_review_id"] else None
    return row, dict(review_version=identity.REVIEW_VERSION, review_id=token, audit_id=document["audit_id"],
                     input_sha256=manifest["prediction"]["input_sha256"], source_input_sha256=identity.fact_digest(row),
                     model_semantics_sha256=identity.model_semantics(expected, catalog),
                     rule_version=manifest["rule_version"], catalog_sha256=manifest["catalog_sha256"],
                     comparison_policy_version=manifest["comparison_policy_version"],
                     expected=expected, prediction=manifest["prediction"], source=source,
                     prediction_inputs=manifest["inputs"],
                     review_source=review_source, reviewer=reviewer.strip(), review_outcome=outcome,
                     verification=verification, purpose=document["purpose"], unit_group=group,
                     group_verified=example["group_verified"], unit_group_hint=hint,
                     reviewed_at=datetime.now(timezone.utc).isoformat(), **annotations)


def _prepare(document, conn):
    if not isinstance(document, dict) or document.get("version") != 2:
        raise ValueError("completed reviews need a version-2 source-linked template; upgrade a blank version-1 audit first")
    _uuid(document.get("audit_id"), "audit_id")
    if document.get("purpose") not in ("development", "holdout"):
        raise ValueError("invalid audit purpose")
    reviews = document.get("reviews")
    if not isinstance(reviews, list) or not 1 <= len(reviews) <= 500:
        raise ValueError("audit needs 1–500 examples")
    catalog, ready, errors, tokens, listings, groups, hints = identity.load(), [], [], set(), set(), {}, {}
    inserted = cached = blank = 0
    for index, example in enumerate(reviews):
        try:
            if not isinstance(example, dict):
                raise ValueError("invalid audit example")
            if example.get("expected") is None:
                blank += 1
                continue
            row, record = _record(example, document, conn, catalog)
            pk, token, group = row["listing_pk"], record["review_id"], record["unit_group"]
            if token in tokens or pk in listings:
                raise ValueError("duplicate review_id or listing in one batch")
            tokens.add(token)
            listings.add(pk)
            if group:
                model = catalog["models"].get(record["expected"]["model_key"])
                label = identity._hash(dict(family=record["expected"]["family"], model_key=record["expected"]["model_key"],
                                            attributes={key:record["expected"]["attributes"][key]
                                                        for key in model["variant_fields"]} if model else {}))
                if group in groups and groups[group] != label:
                    raise ValueError("relists in one physical-unit group have conflicting expected identities")
                groups[group] = label
            hint = record["unit_group_hint"]
            if hint and document["purpose"] == "holdout":
                if hint in hints and hints[hint] != group:
                    raise ValueError("known relist lineage must share one verified unit_group")
                hints[hint] = group
            prior = conn.execute("SELECT listing_pk, output_json FROM enrichment WHERE task=? AND input_sha256=?",
                                 (identity.REVIEW_TASK, identity._hash(token))).fetchone()
            if prior:
                old = json.loads(prior["output_json"])
                fields = set(record)-{"reviewed_at"}
                if prior["listing_pk"] != pk or any(old.get(key) != record[key] for key in fields):
                    raise ValueError("review_id was already used for different evidence")
                cached += 1
            else:
                inserted += 1
                ready.append((row, record))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(dict(index=index, listing_pk=example.get("listing_pk") if isinstance(example, dict) else None,
                               error=str(exc)))
    return ready, dict(valid=not errors, would_insert=inserted, cached=cached, blank_examples=blank,
                       errors=errors, purpose=document["purpose"],
                       normalized_reviews=[dict(listing_pk=row["listing_pk"], expected=record["expected"],
                                                review_outcome=record["review_outcome"], verification=record["verification"])
                                           for row,record in ready])


def preview(document):
    """Read-only validation with per-example errors and normalized values."""
    with db.read_snapshot():
        conn = db.connect()
        try:
            _, result = _prepare(document, conn)
            return result
        except ValueError as exc:
            return dict(valid=False, would_insert=0, cached=0, blank_examples=0,
                        errors=[dict(index=None, error=str(exc))], normalized_reviews=[])
        finally:
            conn.close()


def import_document(document, *, conn=None):
    from . import inventory
    own_connection = conn is None
    catalog, conn = identity.load(), conn if conn is not None else db.connect()
    try:
        with conn.transaction():
            # UI corrections and CLI imports share a listing lock. A stale UI
            # preview cannot overwrite a newer completed operator review.
            if isinstance(document, dict) and isinstance(document.get("reviews"), list):
                keys = sorted({item["listing_pk"] for item in document["reviews"] if isinstance(item, dict)
                               and type(item.get("listing_pk")) is int and item["listing_pk"] > 0})
                for pk in keys:
                    inventory._token_lock(conn, "identity-review-listing:" + str(pk))
            ready, result = _prepare(document, conn)
            if not result["valid"]:
                raise ValueError("audit validation failed: "+"; ".join(item["error"] for item in result["errors"][:8]))
            for row, record in ready:
                identity._store(row, identity.REVIEW_TASK, identity._hash(record["review_id"]), record, catalog, conn)
                if record["unit_group"]:
                    identity._store(row, GROUP_TASK, identity._hash(record["unit_group"]),
                                    dict(purpose=record["purpose"], unit_group=record["unit_group"]), catalog, conn)
                if record["unit_group_hint"]:
                    identity._store(row, HINT_TASK, identity._hash(["hint",record["unit_group_hint"]]),
                                    dict(purpose=record["purpose"], unit_group=record["unit_group"]), catalog, conn)
    finally:
        if own_connection:
            conn.close()
    return dict(inserted=result["would_insert"], cached=result["cached"], blank_examples=result["blank_examples"],
                note="Only source-validated operator evidence was recorded; predictions and seller claims remain separate from inspection facts.")


def evaluation(reviews, catalog):
    eligible, excluded = [], Counter()
    for record in reviews.values():
        if record.get("purpose") != "holdout":
            continue
        if (record.get("catalog_sha256") != catalog["sha256"] or record.get("rule_version") != identity.RULE_VERSION
                or record.get("comparison_policy_version") != identity.COMPARISON_POLICY_VERSION):
            excluded["prediction_version_changed"] += 1
        elif record.get("review_outcome") in ("unknown", "unavailable"):
            excluded[record["review_outcome"]] += 1
        elif not record.get("group_verified") or not record.get("unit_group"):
            excluded["unit_group_unverified"] += 1
        else:
            eligible.append(record)
    families = {}
    for key in catalog["families"]:
        groups = defaultdict(list)
        for record in eligible:
            if key in record["prediction"]["candidate_families"] or record["expected"]["family"] == key:
                groups[record["unit_group"]].append(record)
        model_scores, variant_scores, abstained, critical = [], [], 0, 0
        for members in groups.values():
            m_scores, v_scores = [], []
            known_models, known_variants = set(), set()
            for record in members:
                pred, expected = record["prediction"], identity._apply_review(record["prediction"], record, catalog)
                if expected["model_key"]:
                    known_models.add((expected["family"],expected["model_key"]))
                if expected["variant_key"]:
                    known_variants.add(expected["variant_key"])
                outside = record["expected"]["family"] == "outside_pilot"
                if pred["model_key"] is None:
                    abstained += 1
                elif outside or expected["model_key"] is not None:
                    m_scores.append((pred["family"],pred["model_key"]) == (expected["family"],expected["model_key"]))
                if pred["variant_key"] and (outside or expected["variant_key"] or
                        (expected["model_key"] and expected["model_key"] != pred["model_key"])):
                    v_scores.append(pred["variant_key"] == expected["variant_key"])
            if m_scores:
                model_scores.append(all(m_scores))
            if v_scores:
                variant_scores.append(all(v_scores))
            critical += int(any(not score for score in m_scores+v_scores) or len(known_models)>1 or len(known_variants)>1)
        model = identity._precision(sum(model_scores), len(model_scores))
        variant = identity._precision(sum(variant_scores), len(variant_scores))
        enough = all(item["wilson_ci95"] and item["wilson_ci95"]["lo"] >= .95 for item in (model,variant))
        families[key] = dict(n_unit_groups=len(groups), n_reviewed_listings=sum(map(len,groups.values())),
                             n_duplicate_listings=sum(max(0,len(group)-1) for group in groups.values()),
                             n_abstained_model_predictions=abstained, model_precision=model, variant_precision=variant,
                             audited_scored_model_coverage=len(model_scores)/len(groups) if groups else None,
                             audited_scored_variant_coverage=len(variant_scores)/len(groups) if groups else None,
                             unresolved_critical_mismatches=critical,
                             proposed_precision_gate_met=bool(enough and not critical),
                             automatic_comparison_promoted=False,
                             promotion_reason="Comparison policy needs independent review; a proposed precision gate alone cannot approve automatic buying.")
    unmatched = [record for record in eligible if not record["prediction"]["candidate_families"]]
    return dict(families=families, n_eligible_holdout_reviews=len(eligible), excluded=dict(excluded),
                unmatched_audit=dict(n_reviewed_listings=len(unmatched),
                                     n_missed_family_groups=len({record["unit_group"] for record in unmatched
                                                               if record["expected"]["family"] in catalog["families"]})),
                review_outcomes=dict(Counter(record.get("review_outcome", "legacy_unverified") for record in reviews.values())),
                purposes=dict(Counter(record.get("purpose", "legacy_development") for record in reviews.values())),
                limits="Holdout predictions were frozen before labels. Relists count once per operator-verified unit group; grouping is a human attestation, not inferred from matching titles. Development and holdout units are kept separate. Labelled unmatched examples assess misses only within the audited sampling frame; whole-market recall is unmeasured.")


def frozen_holdouts(rows, catalog):
    """Evaluate recorded source snapshots even after their live listing changes."""
    records = {}
    keys = [row["listing_pk"] for row in rows]
    for offset in range(0,len(keys),500):
        batch = keys[offset:offset+500]
        entries = db.query("SELECT listing_pk,output_json FROM enrichment WHERE task=? AND listing_pk IN ("+
                           ",".join("?" for _ in batch)+") ORDER BY observed_at DESC,enrichment_pk DESC",
                           [identity.REVIEW_TASK]+batch)
        for entry in entries:
            try:
                record = json.loads(entry["output_json"])
                if record.get("review_version") != identity.REVIEW_VERSION or record.get("purpose") != "holdout":
                    continue
                inputs = record["prediction_inputs"]
                if identity.fact_digest(inputs) != record["source_input_sha256"]:
                    continue
                identity._expected(record["expected"],catalog)
                if identity.model_semantics(record["expected"],catalog) != record["model_semantics_sha256"]:
                    continue
                records.setdefault(entry["listing_pk"],record)
            except (KeyError,ValueError,TypeError):
                continue
    return records
