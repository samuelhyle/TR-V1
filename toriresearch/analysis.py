"""Decision research from stored evidence. No collection or price prediction.

Marketplace comparisons describe asking prices. Profit comes from our own
transactions; scenario assumptions never become observations.
"""

import json
import hashlib
import math
import statistics
from collections import Counter, defaultdict
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import collection, config, db, economics, metrics, offer_evidence, product_identity, purchases

DIMENSIONS = {"category_root_norm", "category_norm", "municipality_norm",
              "condition_norm", "product_type"}
# What a peer group actually matched on, strongest first. A group key that holds a
# NULL matches on its absence rather than its value: two listings with no recorded
# brand sit in the same group because neither claims one, not because anything
# says they are the same product. Naming the difference is what keeps the
# shortlist from implying a model match it never made -- and it matters most
# because `model_norm` is unrecorded for almost every stored listing, so without
# this every row would read as a same-model comparison while in fact comparing a
# product type. Insertion order is the ranking order; `IDENTITY_RANK` reads it.
IDENTITY_BASES = {
    "canonical_variant": (
        "reviewed canonical variant and offer",
        "Same detailed category and market, canonical model and required variant attributes, with matching reviewed condition, functionality, completeness, defects and single-unit offer"),
    "brand_and_model": (
        "brand and model",
        "Same detailed category, condition and accepted product type, with a matching recorded brand and model"),
    "model": (
        "model, brand unrecorded",
        "Same detailed category, condition and accepted product type, with a matching recorded model; brand is unrecorded on both sides"),
    "brand": (
        "brand, model unrecorded",
        "Same detailed category, condition and accepted product type, with a matching recorded brand; model is unrecorded on both sides"),
    "product_type_only": (
        "product type only, no brand or model recorded",
        "Same detailed category, condition and accepted product type only; neither brand nor model is recorded on either side, so this compares a product type rather than a product"),
}
IDENTITY_RANK = {name: rank for rank, name in enumerate(IDENTITY_BASES)}
SALE_OUTCOMES = {"sold_confirmed", "sold_inferred"}
LOCAL_ZONE = ZoneInfo("Europe/Helsinki")
FIELDS = ("listing_pk, platform, subplatform, listing_id, title, category_raw_id, category_norm, "
          "category_root_norm, municipality_norm, condition_norm, seller_type_norm, "
          "brand_norm, model_norm, listed_price_eur, first_seen_at, last_seen_at, price_observed_at, "
          "last_live_at, last_probed_at, observation_coverage_end, outcome_observed_at, "
          "status_norm, outcome_status, coverage, is_reserved, is_giveaway, "
          "shipping_cost_buyer_eur, buyer_fee_eur, field_sources_json, source_observed_at")


def amount(value):
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def is_fresh(row, cutoff):
    seen = metrics.parse_ts(row["last_seen_at"])
    return bool(seen and seen >= cutoff and row["status_norm"] == "active"
                and row["outcome_status"] in ("unknown", "active")
                and row["coverage"] == "collecting"
                and not row["is_reserved"] and not row["is_giveaway"])


def is_priced(row, cutoff):
    price = amount(row["listed_price_eur"])
    observed = metrics.parse_ts(row["price_observed_at"] or row["last_seen_at"])
    return bool(is_fresh(row, cutoff) and price is not None and price > 0
                and observed and observed >= cutoff)


def accepted_product_types(rows):
    """Use one current, validated derivation per item; never count cache history."""
    return {pk: entry["product_type"] for pk, entry in product_type_evidence(rows).items()
            if entry["accepted"]}


def product_type_evidence(rows):
    """The derivation behind every product type, including the ones refused.

    `accepted_product_types` keeps only the accepted labels. This keeps the same walk and
    also reports why each derivation was accepted or refused, so a reader can be
    told the real reason instead of being handed a bare "unknown".

    The acceptance rules are unchanged, including the subtle one: a current entry
    that carries a matching input digest blocks older entries for the same
    listing, even when it is then refused for low confidence. A stale-but-matching
    label is worse than no label, so the newest attempt is the only candidate.
    """
    from .enrich import tasks, vocabulary
    vocab = vocabulary.load()
    version = vocab["sha256"][:12]
    compatible = vocabulary.compatible_versions(vocab)
    by_key = {(row["platform"], row["listing_id"]): row for row in rows}
    found, seen = {}, set()
    enriched = db.query(
        "SELECT platform, listing_id, output_json, confidence, confidence_basis, model_confidence_raw, "
        "model, provider, input_sha256, prompt_version, schema_version, validation, observed_at, latency_ms "
        "FROM enrichment WHERE task='product_type' ORDER BY observed_at DESC, enrichment_pk DESC")
    for entry in enriched:
        key = (entry["platform"], entry["listing_id"])
        row = by_key.get(key)
        if not row or key in seen:
            continue
        record = {"accepted": False, "reason": "not_evaluated", "product_type": None,
                  "model": entry["model"], "provider": entry["provider"],
                  "prompt_version": entry["prompt_version"], "schema_version": entry["schema_version"],
                  "confidence": amount(entry["confidence"]), "confidence_basis": entry["confidence_basis"],
                  "validation_score": amount(entry["confidence"]),
                  "semantic_accuracy_probability": None,
                  "model_confidence_raw": amount(entry["model_confidence_raw"]),
                  "validation": entry["validation"], "observed_at": entry["observed_at"],
                  "input_sha256": entry["input_sha256"], "vocabulary_version": version,
                  "vocabulary_identity_version": vocab["identity_version"],
                  "vocabulary_raw_sha256": vocab["raw_sha256"],
                  "compatible_legacy_vocabulary": entry["schema_version"] in compatible and entry["schema_version"] != version,
                  "input_sha256_matches": False, "latency_ms": entry["latency_ms"]}
        if entry["schema_version"] not in compatible or entry["prompt_version"] != tasks.PROMPT_VERSION:
            record["reason"] = "version_changed"
            found.setdefault(row["listing_pk"], record)
            continue
        if not tasks.same_model(entry["model"]):
            record["reason"] = "model_changed"
            found.setdefault(row["listing_pk"], record)
            continue
        digest = tasks.input_digest(row["title"], row["category_norm"], entry["schema_version"])
        if entry["input_sha256"] != digest:
            record["reason"] = "input_changed"
            found.setdefault(row["listing_pk"], record)
            continue
        seen.add(key)
        record["input_sha256_matches"] = True
        if entry["output_json"] in (None, "null", ""):
            record["reason"] = "inference_failed"
            found[row["listing_pk"]] = record
            continue
        try:
            label = json.loads(entry["output_json"] or "null").get("product_type")
        except (ValueError, AttributeError, TypeError):
            record["reason"] = "unreadable_output"
            found[row["listing_pk"]] = record
            continue
        record["product_type"] = label if isinstance(label, str) else None
        if label == vocab["abstain"]:
            record["reason"] = "abstained"
            found[row["listing_pk"]] = record
            continue
        confidence = amount(entry["confidence"]) or 0
        if not 0.7 <= confidence <= 1:
            record["reason"] = "low_confidence"
            found[row["listing_pk"]] = record
            continue
        home = vocab["homes"].get(label) if isinstance(label, str) else None
        actual = tasks._root_of(row["category_norm"])
        accepted = (label in vocab["allowed"] and label != vocab["abstain"] and home
                    and actual and tasks._same_home(home, actual))
        if not accepted:
            # Each refusal gets the reason that actually applies. These were
            # previously collapsed into `not_in_vocabulary` whenever either the
            # vocabulary declared no home or the listing carried no platform
            # category, and both of those are lies: the label *is* in the
            # vocabulary. An operator reading `not_in_vocabulary` goes looking
            # for a label problem and never finds the missing `homes` entry that
            # is dropping every listing in that vertical.
            if label == vocab["abstain"]:
                record["reason"] = "abstained"
            elif label not in vocab["allowed"]:
                record["reason"] = "not_in_vocabulary"
            elif home is None:
                record["reason"] = "no_declared_home"
            elif actual is None:
                record["reason"] = "no_platform_category"
            else:
                record["reason"] = "category_mismatch"
            found[row["listing_pk"]] = record
            continue
        record["accepted"] = True
        record["reason"] = "accepted"
        found[row["listing_pk"]] = record
    from . import listing_analysis
    for row in listing_analysis.attach(rows):
        automatic = row["listing_analysis"]
        if automatic["status"] == "not_analyzed":
            continue
        if automatic["status"] != "current":
            if row["listing_pk"] in found:
                found[row["listing_pk"]].update(accepted=False,reason="listing_analysis_pending")
            continue
        label = automatic["product_type"]
        home = vocab["homes"].get(label)
        accepted = bool(label!=vocab["abstain"] and home and tasks._same_home(home,tasks._root_of(row["category_norm"]) or "")
                        and not automatic["blockers"])
        found[row["listing_pk"]] = dict(product_type=label,accepted=accepted,reason="accepted" if accepted else "listing_analysis_blocked",
            model=config.LLM_MODEL,provider="llama.cpp",prompt_version=listing_analysis.PROMPT_VERSION,
            schema_version=automatic["identity"]["profiles"][:12],confidence=0.7,validation_score=0.7,
            semantic_accuracy_probability=None,model_confidence_raw=None,confidence_basis="validated_source_candidates_not_accuracy",
            validation="source_validated",observed_at=automatic["analyzed_at"],input_sha256=automatic["input_sha256"],
            input_sha256_matches=True,latency_ms=automatic.get("usage",{}).get("latency_ms"),
            vocabulary_version=version,vocabulary_identity_version=vocab["identity_version"],vocabulary_raw_sha256=vocab["raw_sha256"],
            compatible_legacy_vocabulary=False)
    return found


def classification_quality(rows, evidence=None):
    """One current state per listing, including stale and missing derivations."""
    from .enrich import tasks, vocabulary
    evidence = product_type_evidence(rows) if evidence is None else evidence
    reasons = Counter(evidence[row["listing_pk"]]["reason"] if row["listing_pk"] in evidence else "never_classified"
                      for row in rows)
    states = {
        "accepted": reasons["accepted"], "stale_input": reasons["input_changed"],
        "stale_version": reasons["version_changed"] + reasons["model_changed"],
        "low_confidence": reasons["low_confidence"], "abstained": reasons["abstained"],
        "failed": reasons["inference_failed"] + reasons["unreadable_output"],
        "never_classified": reasons["never_classified"],
        "validation_refused": sum(count for reason, count in reasons.items()
                                  if reason in ("not_in_vocabulary", "no_declared_home", "no_platform_category", "category_mismatch")),
    }
    return dict(n_listings=len(rows), counts=states, reasons=dict(reasons),
                vocabulary=vocabulary.coverage(), model=config.LLM_MODEL,
                model_revision=config.LLM_MODEL_REVISION, prompt_version=tasks.PROMPT_VERSION,
                basis="Current input/version validation; confidence gates are not measured classification accuracy.")


def price_panel(rows, cutoff, min_n=None):
    fresh = [row for row in rows if is_fresh(row, cutoff)]
    prices = [float(row["listed_price_eur"]) for row in fresh if is_priced(row, cutoff)]
    result = {"n_available": len(fresh), "n_priced": len(prices),
              "price_coverage": len(prices) / len(fresh) if fresh else None,
              "status": "INSUFFICIENT DATA"}
    if len(prices) >= (config.MIN_N_FOR_REPORT if min_n is None else min_n):
        p = metrics.percentiles(prices)
        ci = metrics.bootstrap_median_ci(prices)
        result.update(status="OK", median_eur=round(p[0.5], 2),
                      p25_eur=round(p[0.25], 2), p75_eur=round(p[0.75], 2),
                      median_ci95={k: round(ci[k], 2) for k in ("lo", "hi")} if ci else None)
    return result


def portfolio(now=None):
    """Exact ledger totals and supported distributions, across all our trades."""
    now = now or datetime.now(timezone.utc)
    rows = purchases.ledger_rows()
    sold = [row for row in rows if row["outcome_status"] in SALE_OUTCOMES]
    stock = [row for row in rows if row["outcome_status"] in (None, "returned_review")]
    margins, durations, incomplete = [], [], 0
    category_trades = defaultdict(list)
    for row in sold:
        margin, _ = purchases._margin(row)
        if margin:
            margins.append(margin["gross_margin_eur"])
            category_trades[row["category_norm"] or "Uncategorised"].append(margin["gross_margin_eur"])
        else:
            incomplete += 1
        start, end = metrics.parse_ts(row["purchased_at"]), metrics.parse_ts(row["sale_at"])
        if start and end and start <= end <= now:
            durations.append((end - start).total_seconds() / 86400)
    buckets = {"0–7d": 0, "8–30d": 0, "31–60d": 0, "61d+": 0, "Unknown age": 0}
    aging = []
    for row in stock:
        start = metrics.parse_ts(row["purchased_at"])
        age = (now - start).total_seconds() / 86400 if start and start <= now else None
        bucket = "Unknown age" if age is None else "0–7d" if age <= 7 else "8–30d" if age <= 30 else "31–60d" if age <= 60 else "61d+"
        buckets[bucket] += 1
        aging.append({"purchase_pk": row["purchase_pk"], "title": row["title_raw"],
                      "age_days": round(age, 1) if age is not None else None,
                      "acquisition_cost_eur": amount(row["acquisition_cost_eur"])})
    capital = sum(Decimal(str(row["acquisition_cost_eur"])) for row in stock if row["acquisition_cost_eur"] is not None)
    unknown_acquisition = sum(row["acquisition_cost_eur"] is None for row in stock)
    known_landed = sum(Decimal(str(row[field])) for row in stock
                       for field in ("acquisition_cost_eur", "inbound_shipping_cost_eur", "buyer_fee_eur")
                       if row[field] is not None)
    unknown_landed = sum(any(row[field] is None for field in
                             ("acquisition_cost_eur", "inbound_shipping_cost_eur", "buyer_fee_eur")) for row in stock)
    writeoffs = [purchases._margin(row)[0] for row in rows if row["outcome_status"] == "written_off"]
    known_writeoffs = sum(row is not None for row in writeoffs)
    unknown_writeoffs = len(writeoffs) - known_writeoffs
    net_results = margins + [row["gross_margin_eur"] for row in writeoffs if row]
    return {"scope": "All your own trades; independent of marketplace filters",
            "n_purchases": len(rows), "n_sold": len(sold), "n_in_stock": len(stock),
            "n_other_outcomes": len(rows)-len(sold)-len(stock),
            "n_complete_margins": len(margins), "n_unknown_cost_sales": incomplete,
            "n_complete_writeoffs": known_writeoffs, "n_unknown_cost_writeoffs": unknown_writeoffs,
            "n_complete_outcomes": len(margins) + known_writeoffs,
            "realized_profit_eur": round(sum(net_results), 2) if net_results else None,
            "profit_is_partial": bool(incomplete or unknown_writeoffs),
            "revenue_eur": round(sum(float(row["final_sale_price_eur"] or 0) for row in sold), 2),
            "acquisition_capital_eur": None if unknown_acquisition else float(capital),
            "known_acquisition_capital_eur": float(capital), "n_unknown_acquisition_costs": unknown_acquisition,
            "known_landed_capital_eur": float(known_landed),
            "n_unknown_landed_costs": unknown_landed,
            "n_losses": sum(value < 0 for value in net_results),
            "median_profit_eur": round(statistics.median(margins), 2) if len(margins) >= config.MIN_N_FOR_REPORT else None,
            "median_days_to_sale": round(statistics.median(durations), 1) if len(durations) >= config.MIN_N_FOR_REPORT else None,
            "n_valid_sale_durations": len(durations), "age_buckets": buckets,
            "oldest_stock": sorted(aging, key=lambda item: item["age_days"] if item["age_days"] is not None else -1, reverse=True)[:8],
            "by_category": [{"label": key, "n": len(values), "total_profit_eur": round(sum(values), 2),
                             "median_profit_eur": round(statistics.median(values), 2) if len(values) >= config.MIN_N_FOR_REPORT else None}
                            for key, values in sorted(category_trades.items(), key=lambda item: -sum(item[1]))]}


def _identity_basis(brand, model):
    return ("brand_and_model" if brand and model else "model" if model
            else "brand" if brand else "product_type_only")


# The quantiles a peer comparison reports. The extremes are included so a reader
# sees the whole range the percentage was measured inside, not only its middle:
# "40% below the median" means something different in a group spanning 60-200 than
# in one spanning 90-110, and the median alone cannot show which.
GAP_QUANTILES = (0.0, 0.25, 0.5, 0.75, 1.0)


def peer_cohorts(rows, cutoff, product_types, identities=None, relationship_admissions=None,
                 priced_only=True):
    """Index the comparable cohort every listing on this slice shares.

    One walk, shared by the shortlist and by the per-item research view, because
    the two must agree on what a peer *is*. A candidate row is a cohort statistic
    and asking "is this specific item a candidate" is the same question asked
    about one member of a cohort; computing the group key twice would let the
    shortlist call something a candidate that its own detail page cannot
    reproduce from the same database.

    Returns `(cohorts, index)`. `cohorts` maps a group key to its members and
    `index` maps `listing_pk` to that key, so a caller can iterate cohorts once
    (the shortlist) or resolve one listing's cohort in constant time (the
    per-item view). Each cohort list *includes* the listing it is keyed by,
    because it is the sample the statistics are computed over; consumers exclude
    the subject themselves and report that they did.
    """
    identities = product_identity.resolve_rows(rows, product_types=product_types) if identities is None else identities
    groups = defaultdict(list)
    for row in rows:
        category, condition = row["category_norm"], row["condition_norm"]
        product_type = product_types.get(row["listing_pk"])
        identity = identities[row["listing_pk"]]
        if not offer_evidence.evaluate(row, identity)["eligible"]:
            continue
        canonical = identity["comparison_key"]
        if identity["pilot_scope"]:
            # Pilot variants cannot fall back to a broad product-type discount.
            # A model name with unknown capacity or offer contents is research
            # debt, not an exact-product price candidate.
            if canonical is None:
                continue
            product_type = identity["product_type"]
            condition = identity["attributes"]["condition"]
        # A root category cannot make unrelated products comparable.
        if (not category or category == row["category_root_norm"] or not condition
                or condition == "unknown" or not product_type
                or (priced_only and not is_priced(row, cutoff)) or row.get("is_giveaway")):
            continue
        key = (row["platform"], row["subplatform"], category, condition,
               product_type, identity["brand"] if canonical else row.get("brand_norm"),
               identity["model"] if canonical else row.get("model_norm"), canonical)
        groups[key].append(row)
    from . import relationships
    keys = {row["listing_pk"]: key for key, peers in groups.items() for row in peers}
    admitted = [row for peers in groups.values() for row in peers]
    relationship_blocks = relationships.comparison_admissions(admitted, keys)
    if relationship_admissions is not None:
        relationship_admissions.update(relationship_blocks)
    groups = {key: [row for row in peers if row["listing_pk"] not in relationship_blocks]
              for key, peers in groups.items()}
    groups = {key: peers for key, peers in groups.items() if peers}
    return groups, {row["listing_pk"]: key for key, peers in groups.items() for row in peers}


# The group-key fields, in the order `peer_cohorts` builds them. Named so a
# research view can show which fields were matched without unpacking a tuple.
COHORT_FIELDS = ("platform", "subplatform", "category_norm", "condition_norm",
                 "product_type", "brand_norm", "model_norm", "canonical_comparison_key")


def cohort_key(key):
    """A cohort key as named fields, so a payload never ships a positional tuple."""
    return dict(zip(COHORT_FIELDS, key))


def leave_one_out(ordered, omitted):
    """Quantiles over `ordered` with the element at `omitted` left out.

    The candidate must not be compared against a benchmark that contains it, and
    it must not be one of its own peers. Excluding it by index rather than by
    re-sorting a copy per candidate is what keeps a large group linear rather
    than quadratic.
    """
    n_peers = len(ordered) - 1
    out = {}
    for quantile in GAP_QUANTILES:
        index = quantile * (n_peers - 1)
        lo, hi = math.floor(index), math.ceil(index)
        low = ordered[lo + (lo >= omitted)]
        high = ordered[hi + (hi >= omitted)]
        low_amount, high_amount = Decimal(str(low)), Decimal(str(high))
        out[quantile] = float(low_amount + (high_amount - low_amount) * Decimal(str(index - lo)))
    return out


def qualifies_as_candidate(price, quantiles, min_gap_pct):
    """The two tests a price-comparison candidate must pass, in one place.

    Below the cohort's lower quartile *and* at least `min_gap_pct` below its
    median. They are separate tests and neither scales the other: a loose
    percentage must not turn "bottom quartile" into "a bargain", and a wide
    lower quartile is not a discount.

    Shared with the research desk so its per-item page cannot report a candidate
    the list would not show, or deny one the list did. Two copies of this rule
    would drift, and the drift would be invisible: the list and its own detail
    page would disagree about the same database.
    """
    return price < quantiles[0.25] and meets_minimum_gap(price, quantiles[0.5], min_gap_pct)


def meets_minimum_gap(price, median, min_gap_pct):
    """Compare decimal amounts without rejecting an exact percentage boundary."""
    return Decimal(str(price)) * 100 <= Decimal(str(median)) * (100 - Decimal(str(min_gap_pct)))


def gap_evidence(price, interval, min_gap_pct):
    """Positive and threshold-sized gaps are separate claims about the interval."""
    below = None if not interval else price < interval["lo"]
    survives = None if not interval else meets_minimum_gap(price, interval["lo"], min_gap_pct)
    conservative = (round((1 - price / interval["lo"]) * 100, 1)
                    if interval and interval["lo"] > 0 else None)
    return dict(gap_survives_ci=below, ask_below_median_ci=below,
                min_gap_survives_ci=survives, conservative_gap_pct=conservative)


def benchmark_context(row, key, peers, cutoff, filters=None, identities=None):
    """Name the shared cohort and the actual subject-excluded evidence revision."""
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                         default=str, allow_nan=False).encode()).hexdigest()
    filters = {k: v for k, v in (filters or {}).items() if v is not None and v != ""}
    identities = identities if identities is not None else product_identity.resolve_rows(peers + [row])
    def identity_revision(peer):
        identity = identities.get(peer["listing_pk"], {})
        review = identity.get("review") or {}
        return dict(input_sha256=identity.get("input_sha256"), review_id=review.get("review_id"),
                    reviewed_at=review.get("reviewed_at"), catalog_sha256=identity.get("catalog_sha256"),
                    rule_version=identity.get("rule_version"), comparison_policy=identity.get("comparison_policy_version"))
    cohort_id = digest(dict(key=cohort_key(key), policy=offer_evidence.VERSION))
    members = [(p["listing_pk"], p["listed_price_eur"], p.get("price_observed_at"),
                p.get("source_observed_at"), p.get("last_seen_at"), identity_revision(p))
               for p in peers if p["listing_pk"] != row["listing_pk"]]
    revision = digest(dict(cohort_id=cohort_id, subject=row["listing_pk"],
                           subject_price=row["listed_price_eur"], members=sorted(members),
                           subject_identity=identity_revision(row),
                           filters=filters or {}, cutoff=cutoff, minimum=config.MIN_N_FOR_REPORT))
    return dict(cohort_id=cohort_id, benchmark_revision=revision,
                comparison_policy_version=offer_evidence.VERSION,
                effective_filters=filters or {}, freshness_cutoff=cutoff.isoformat(),
                reference_at=(cutoff + timedelta(hours=config.PANEL_MAX_AGE_HOURS)).isoformat())


def comparison_reference(reference_at=None):
    """Pin all comparison reads to a recent reference time under the same contract."""
    now = datetime.now(timezone.utc)
    reference = now if reference_at is None else metrics.parse_ts(reference_at)
    if (reference is None or reference > now + timedelta(minutes=1)
            or reference < now - timedelta(hours=config.PANEL_MAX_AGE_HOURS)):
        raise ValueError("reference_at must be a recent timestamp, not in the future")
    return reference


def comparison_admission(row, identity, relationship_admission=None):
    admission = offer_evidence.evaluate(row, identity)
    if relationship_admission:
        admission["eligible"] = False
        admission["reasons"] = list(dict.fromkeys(admission["reasons"] + [relationship_admission["reason"]]))
        admission["listing_relationship"] = relationship_admission
    return admission


def resolve_peer_cohort(row, cutoff, filters=None, fields=None):
    """One scoped lookup shared by shortlist detail, evidence and provenance."""
    options = dict(filters or {})
    options.setdefault("platform", row["platform"])
    options.setdefault("subplatform", row["subplatform"])
    where, params, applied = metrics.listing_filters(**options)
    where += ["platform=?", "subplatform=?", "category_norm=?"]
    params += [row["platform"], row["subplatform"], row.get("category_norm")]
    rows = db.query("SELECT " + (fields or FIELDS) + " FROM listings WHERE " + " AND ".join(where), params)
    types = accepted_product_types(rows)
    identities = product_identity.resolve_rows(rows, product_types=types)
    relationship_admissions = {}
    cohorts, index = peer_cohorts(rows, cutoff, types, identities, relationship_admissions)
    key = index.get(row["listing_pk"])
    outcome_cohorts, _ = peer_cohorts(rows, cutoff, types, identities, priced_only=False) if key else ({}, {})
    return dict(rows=rows, types=types, identities=identities, key=key,
                peers=cohorts.get(key, []), filters={k: v for k, v in applied.items() if v is not None},
                incidence_peers=outcome_cohorts.get(key, []),
                cohorts=cohorts, index=index, outcome_cohorts=outcome_cohorts,
                relationship_admissions=relationship_admissions,
                relationship_admission=relationship_admissions.get(row["listing_pk"]),
                subject_in_scope=any(p["listing_pk"] == row["listing_pk"] for p in rows))


def comparison_coverage(rows, cutoff, types, identities=None):
    """Expose comparison debt separately from descriptive asking-price readiness."""
    identities = identities or product_identity.resolve_rows(rows, product_types=types)
    fresh = [row for row in rows if is_priced(row, cutoff)]
    reasons = Counter(reason for row in fresh
                      for reason in offer_evidence.evaluate(row, identities[row["listing_pk"]])["reasons"])
    relationship_admissions = {}
    cohorts, _ = peer_cohorts(rows, cutoff, types, identities, relationship_admissions)
    reasons.update(admission["reason"] for admission in relationship_admissions.values())
    return dict(fresh_priced=len(fresh), current_type_labels=sum(row["listing_pk"] in types for row in fresh),
                reviewed_eligible_observations=sum(len(peers) for peers in cohorts.values()),
                eligible_cohorts=len(cohorts), supported_cohorts=sum(len(peers) > config.MIN_N_FOR_REPORT for peers in cohorts.values()),
                blocked_by_reason=dict(reasons), blocked_reasons_overlap=True,
                duplicate_observations_excluded=sum(a["reason"] == "duplicate_observation" for a in relationship_admissions.values()),
                peer_count_basis="Current listing representatives; accepted duplicate/version links count once. Possible relists stay separate; public physical units are unverified.",
                policy_version=offer_evidence.VERSION,
                note="Category and product-type prices remain descriptive market context. A discount needs a reviewed product and offer comparison.")


def research_inventory(rows, cutoff, identities, limit=25):
    """Current source offers to inspect when supported comparisons are unavailable.

    These are observed listings, not price candidates. Missing review facts stay
    missing; no peer benchmark, discount or resale estimate is attached.
    """
    ranked = []
    explicit_blocks = {"accessory_offer", "bundle_offer", "multiple_units_offer",
                       "incompatible_offer_condition", "conflicting_offer_evidence"}
    for row in rows:
        if not is_priced(row, cutoff):
            continue
        identity = identities[row["listing_pk"]]
        admission = comparison_admission(row, identity)
        priority = (not bool(explicit_blocks.intersection(admission["reasons"])),
                    bool(identity.get("variant_key")), bool(identity.get("model_key")),
                    metrics.parse_ts(row["last_seen_at"]).timestamp(), row["listing_pk"])
        ranked.append((priority, row, admission))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    fields = ("listing_pk", "listing_id", "title", "platform", "subplatform",
              "category_norm", "condition_norm", "municipality_norm", "last_seen_at",
              "price_observed_at")
    return {"total": len(ranked), "returned": min(limit, len(ranked)),
            "items": [{**{field: row[field] for field in fields},
                       "ask_eur": amount(row["listed_price_eur"]),
                       "listing_analysis": identities[row["listing_pk"]].get("listing_analysis"),
                       "review_reasons": admission["reasons"]}
                      for _, row, admission in ranked[:limit]],
            "basis": "Current observed asking prices; these listings are research inventory, not supported price candidates."}


def research_candidates(rows, cutoff, product_types, identities=None,
                        min_gap_pct=20.0, limit=12, peer_stats=False, peer_rows=0, filters=None):
    """Leave the candidate out of same-category/condition peer comparisons.

    `min_gap_pct` and `limit` default to the analyst view's own values so the
    shortlist there is unchanged by the research desk asking for a different
    width or depth. Two filters are applied and they are not the same test: an
    item must sit below the group's lower quartile *and* at least `min_gap_pct`
    below its median, so a group with an unusually high median cannot qualify an
    item that merely sits inside its spread.
    """
    if isinstance(min_gap_pct, bool) or not isinstance(min_gap_pct, (int, float)):
        raise ValueError("minimum gap must be a number")
    if not math.isfinite(float(min_gap_pct)) or not 0 <= float(min_gap_pct) < 100:
        raise ValueError("minimum gap must be between 0 and 100 percent")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("candidate limit must be a positive integer or omitted")
    identities = product_identity.resolve_rows(rows, product_types=product_types) if identities is None else identities
    candidates = []
    cohorts, _index = peer_cohorts(rows, cutoff, product_types, identities)
    for key, peers in cohorts.items():
        _, _, category, condition, product_type, brand, model, canonical = key
        if len(peers) <= config.MIN_N_FOR_REPORT:
            continue
        ordered = sorted(float(row["listed_price_eur"]) for row in peers)
        identity = "canonical_variant" if canonical else _identity_basis(brand, model)
        for row in peers:
            price = float(row["listed_price_eur"])
            p = leave_one_out(ordered, bisect_left(ordered, price))
            if not qualifies_as_candidate(price, p, min_gap_pct):
                continue
            sample = peer_sample(ordered, bisect_left(ordered, price))
            interval = sample["median_ci95"]
            # A gap measured against a median this wide is not the claim a bare
            # percentage makes. The group behind today's shortlist has a median
            # of 97 with a 95% interval of 78-180; only a candidate under 78 has
            # a discount that survives the uncertainty in its own benchmark.
            candidate = {"listing_pk": row["listing_pk"], "listing_id": row["listing_id"],
                               "title": row["title"], "platform": row["platform"], "subplatform": row["subplatform"],
                               "category_norm": category, "condition_norm": condition,
                               "municipality_norm": row["municipality_norm"], "ask_eur": price,
                               "seller_type_norm": row.get("seller_type_norm"),
                               "peer_median_eur": round(p[0.5], 2), "peer_p25_eur": round(p[0.25], 2),
                               "peer_median_ci95": interval,
                               "gap_pct": round((1-price/p[0.5])*100, 1), "n_peers": len(ordered) - 1,
                               "product_type": product_type, "last_seen_at": row["last_seen_at"],
                               "brand_norm": brand, "model_norm": model,
                               "identity_basis": identity,
                               "product_identity": identities[row["listing_pk"]] if canonical else None,
                               "requires_variant_verification": True,
                               "basis": IDENTITY_BASES[identity][1]
                                        + "; variants require inspection; asking prices only"}
            candidate.update(gap_evidence(price, interval, min_gap_pct))
            candidate.update(benchmark_context(row, key, peers, cutoff, filters, identities))
            candidate["min_gap_pct"] = min_gap_pct
            if peer_stats:
                if sample is not None:
                    candidate["peer_sample"] = sample
            if peer_rows:
                candidate["peers"] = peer_examples(peers, row["listing_pk"], peer_rows)
            candidates.append(candidate)
    # Strongest verified identity first, then a gap that survives its own
    # interval, then the size of the gap. A wide discount on an unidentified
    # product type is not better research than a narrower one on a named model,
    # and ordering by gap alone used to say that it was.
    ranked = sorted(candidates, key=lambda row: (IDENTITY_RANK[row["identity_basis"]],
                                                 row["min_gap_survives_ci"] is not True,
                                                 -row["gap_pct"], -row["n_peers"],
                                                 row["listing_pk"]))
    return ranked[:limit] if limit else ranked


def peer_sample(ordered, omitted):
    """The distribution a candidate's percentage was measured inside.

    The reported extremes exclude the candidate, like the median does, so the
    range a reader sees is the range it was compared against rather than one its
    own price widened. The bootstrap interval is attached because a distribution
    without one invites exactly the over-reading this codebase refuses: a median
    from a wide group is not a benchmark to subtract from.

    Returns None for a list too short to leave anything behind. A one-element
    sample has no median to report, and returning a degenerate distribution here
    would let a caller print one.
    """
    if len(ordered) < 2 or len(ordered) - 1 < config.MIN_N_FOR_REPORT:
        return None
    p = leave_one_out(ordered, omitted)
    interval = metrics.bootstrap_median_ci(ordered[:omitted] + ordered[omitted + 1:])
    return {"n": len(ordered) - 1, "min_eur": round(p[0.0], 2), "p25_eur": round(p[0.25], 2),
            "median_eur": round(p[0.5], 2), "p75_eur": round(p[0.75], 2), "max_eur": round(p[1.0], 2),
            "median_ci95": ({"lo": interval["lo"], "hi": interval["hi"]}
                            if interval else None),
            "bootstrap_reps": (interval or {}).get("reps"),
            "ci_method": "percentile bootstrap on the median",
            "subject_excluded": True,
            "basis": "Asking prices of the other fresh priced items in the same cohort"}


def peer_examples(peers, listing_pk, limit):
    """A bounded, price-ordered sample of the cohort, with the subject removed.

    Bounded because these are rows a reader may open, not the cohort itself: the
    cohort is summarised by `peer_sample` and re-read on demand, so returning
    every peer would make a wide group the response size for no added evidence.
    """
    others = [peer for peer in peers if peer["listing_pk"] != listing_pk]
    return [{key: peer.get(key) for key in
             ("listing_pk", "listing_id", "title", "listed_price_eur", "condition_norm",
              "municipality_norm", "seller_type_norm", "brand_norm", "model_norm", "last_seen_at")}
            for peer in sorted(others, key=lambda peer: (float(peer["listed_price_eur"]), peer["listing_pk"]))[:limit]]


def buy_recommendation(market, listing, product_type, cutoff, min_n=config.MIN_N_FOR_REPORT,
                        horizon=30, product_types=None, cost_assumptions=None, identities=None):
    """Inspectable evidence and complete-cost scenarios, never raw-count forecasts."""
    if not is_priced(listing, cutoff) or not market:
        return None
    ask = float(listing["listed_price_eur"])
    product_types = accepted_product_types(market) if product_types is None else product_types
    identities = product_identity.resolve_rows(market, product_types=product_types) if identities is None else identities
    identity = identities[listing["listing_pk"]]
    matching = ("platform", "subplatform", "category_norm", "condition_norm", "brand_norm", "model_norm")
    segment_members = [row for row in market if not row.get("is_giveaway")
                       and all(row.get(key) == listing.get(key) for key in matching)
                       and product_type and product_types.get(row["listing_pk"]) == product_type]
    if identity["pilot_scope"]:
        key = identity["comparison_key"]
        product_type = identity["product_type"]
        segment_members = [row for row in market if key and not row.get("is_giveaway")
                           and all(row.get(field) == listing.get(field) for field in ("platform", "subplatform", "category_norm"))
                           and identities[row["listing_pk"]]["comparison_key"] == key]
    if not segment_members:
        # Missing classification still permits a research card, with no peer
        # outcome inference. The candidate itself supplies availability only.
        segment_members = [listing]
    scope = {key: listing.get(key) for key in matching}
    scope["product_type"] = product_type
    if identity["pilot_scope"]:
        scope["canonical_comparison_key"] = identity["comparison_key"]
    # Keep sold and competing terminal members. Availability filters belong to
    # current asks, not the cohort used to measure observed sale-marker incidence.
    liq = metrics.liquidity_rows(segment_members, horizon, min_n=min_n, filters=scope)
    sale_evidence = {kind: dict(n_events=value["n_events"],
        estimate=value.get("estimates", {}).get(f"{horizon}d"),
        status="OK" if value.get("estimates", {}).get(f"{horizon}d") is not None else "INSUFFICIENT DATA")
        for kind, value in liq["sell_through_by_evidence"].items()}
    from . import realization
    expected_price = price_interval = None
    price_basis = "unknown; comparable owned resale evidence required"
    if identity["pilot_scope"]:
        price_basis = "unknown; historical resale evidence for this canonical variant and offer is required"
    elif product_type and listing.get("brand_norm") and listing.get("model_norm"):
        price_view = realization.realized_price_estimate(
            category_norm=listing.get("category_norm"), condition_norm=listing.get("condition_norm"),
            product_type=product_type, platform=listing["platform"], subplatform=listing["subplatform"],
            brand_norm=listing["brand_norm"], model_norm=listing["model_norm"],
            exclude_listing_pk=listing["listing_pk"], min_n=min_n)
        if price_view["status"] == "ESTIMATE":
            expected_price = price_view["estimate"]["expected_realized_eur"]
            price_interval = price_view["estimate"].get("interval_eur")
            price_basis = "comparable owned resale benchmark; variants require inspection"
    costs = {key: None for key in economics.COST_FIELDS}
    costs.update(inbound_shipping_cost_eur=listing.get("shipping_cost_buyer_eur"),
                 buyer_fee_eur=listing.get("buyer_fee_eur"))
    if cost_assumptions is not None:
        if not isinstance(cost_assumptions, dict) or set(cost_assumptions)-set(economics.COST_FIELDS):
            raise ValueError("unsupported cost assumptions")
        costs.update(cost_assumptions)
    calculation = economics.unit_economics(ask, expected_price, costs)
    selling_fees_known = costs["platform_fee_eur"] is not None and costs["payment_fee_eur"] is not None
    risk_flags = []
    risk_flags.append("individual sale probability unknown; observed marker incidence is separate")
    if not selling_fees_known:
        risk_flags.append("sale-side fees unknown; margin not computed")
    if calculation["unknown_costs"]:
        risk_flags.append("incomplete costs: " + ", ".join(calculation["unknown_costs"]))
    if expected_price is None:
        risk_flags.append("no realization evidence for this segment")
    risk_flags.append("verify exact variant, functionality and completeness before buying")
    if ask > 200 and listing.get("condition_norm") in ("poor", "for_parts"):
        risk_flags.append("high condition risk at this price")
    if not listing.get("condition_norm") or listing.get("condition_norm") == "unknown":
        risk_flags.append("condition unknown")

    return {
        "listing_pk": listing["listing_pk"],
        "product_identity": identity if identity["pilot_scope"] else None,
        "listing_id": listing["listing_id"],
        "title": listing["title"],
        "ask_eur": ask,
        "acquisition_cost_eur": round(ask, 2),
        "acquisition_cost_basis": "advertised item price; additional costs recorded separately",
        "expected_sale_price_eur": round(expected_price, 2) if expected_price else None,
        "expected_sale_price_basis": price_basis,
        "expected_sale_price_interval_eur": price_interval,
        "horizon_days": horizon,
        "p_sale_within_horizon": None,
        "sale_evidence_by_class": sale_evidence,
        "n_followed_to_horizon": liq["horizon_support"][f"{horizon}d"],
        "selling_fees_known": selling_fees_known,
        "expected_gross_margin_eur": calculation["net_profit_eur"],
        "expected_gross_margin_pct": calculation["net_margin_pct"],
        "economics": calculation,
        "cost_basis": "observed inbound fields and explicit user assumptions; no default fees or cost zeroes",
        "status": "scenario" if calculation["status"] == "COMPLETE" else "research_only",
        "decision": "REVIEW",
        "confidence_tier": "very_low",
        "risk_flags": risk_flags,
        "evidence": (f"Individual sale probability unknown; {liq['listings_tracked']} comparable tracked listings; "
                     f"{horizon}-day observed incidence is split by evidence class; "
                     f"price basis: {price_basis}; "
                     f"fee basis: {'explicit cost assumptions' if selling_fees_known else 'unknown'}"),
        "computed_at": datetime.now(timezone.utc).isoformat()
    }


def _generate_buy_recommendations(market, cutoff, min_n=config.MIN_N_FOR_REPORT, horizon=30, product_types=None, identities=None):
    """Generate buy recommendations for the top candidates in the market slice."""

    # First, research candidates (already filters by price gap)
    product_types = accepted_product_types(market) if product_types is None else product_types
    identities = product_identity.resolve_rows(market, product_types=product_types) if identities is None else identities
    candidates = research_candidates(market, cutoff, product_types, identities)
    listings_by_pk = {row["listing_pk"]: row for row in market}

    recommendations = []
    for candidate in candidates[:20]:  # Top 20 candidates
        # Candidates are summaries (ask_eur); recommendations need the stored
        # listing fields, including listed_price_eur and observation timestamps.
        listing = listings_by_pk[candidate["listing_pk"]]
        rec = buy_recommendation(market, listing, product_types.get(candidate["listing_pk"]),
                                  cutoff, min_n, horizon=horizon, product_types=product_types, identities=identities)
        if rec:
            recommendations.append(rec)

    # Sort by expected margin (desc), then by confidence
    recommendations.sort(key=lambda r: (
        r["expected_gross_margin_eur"] is None,
        -r["expected_gross_margin_eur"] if r["expected_gross_margin_eur"] is not None else 0,
        -{"very_low": 0, "low": 1, "medium": 2, "high": 3}.get(r["confidence_tier"], 0)
    ))

    return recommendations[:12]


def grid_coverage(scope):
    """Sweep cells this scope is responsible for, and how many are unvisited.

    Reads the grid rather than the listings, so a focus category with no
    observations yet still reports the collection debt it carries.
    """
    conditions, params = scope.conditions(grid=True)
    if not conditions:
        return {"cells": 0, "crawled": 0, "pending": 0, "locations": 0,
                "coverage_pct": None, "median_hours_since_crawl": None}
    where = "platform='tori' AND active=TRUE AND " + " AND ".join(conditions)
    reference = datetime.now(timezone.utc)
    national = db.query("SELECT last_page1_at,next_due_at,coverage_state FROM sweep_targets WHERE " + where +
                        " AND location_grain='national'", params)
    nation_ages = [max(0,(reference-stamp).total_seconds()/60) for stamp in
                   (metrics.parse_ts(r['last_page1_at']) for r in national) if stamp]
    # Nationwide front pages and geographical grid debt describe different coverage.
    where += " AND location_grain<>'national'"
    row = db.query_one(
        "SELECT COUNT(*) AS cells, SUM(CASE WHEN last_crawled_at IS NULL THEN 1 ELSE 0 END) AS pending, "
        "COUNT(DISTINCT location_id) AS locations, "
        "SUM(CASE WHEN last_crawled_at IS NOT NULL THEN 1 ELSE 0 END) AS crawled "
        "FROM sweep_targets WHERE " + where, params)
    ages = [(datetime.now(timezone.utc) - metrics.parse_ts(row_["last_crawled_at"])).total_seconds() / 3600
            for row_ in db.query(
                "SELECT last_crawled_at FROM sweep_targets WHERE " + where +
                " AND last_crawled_at IS NOT NULL", params)
            if metrics.parse_ts(row_["last_crawled_at"])]
    cells, crawled = row["cells"] or 0, row["crawled"] or 0
    return {"cells": cells, "crawled": crawled, "pending": row["pending"] or 0,
            "locations": row["locations"] or 0,
            "coverage_pct": round(100 * crawled / cells, 1) if cells else None,
            "median_hours_since_crawl": round(statistics.median(ages), 1) if ages else None,
            "national_front_pages": {'targets':len(national),'observed':len(nation_ages),
                                     'oldest_age_minutes':round(max(nation_ages),1) if nation_ages else None,
                                     'refresh_seconds':config.COLLECTION_INTERVAL_SECONDS,
                                     'capped':sum(r['coverage_state']=='capped' for r in national)},
            "note": "Geographical cells pair a municipality or region with a category. Front-page visits and depth caps measure partial inventory coverage."}


def focus_breakdown(filters=None, horizon=30):
    """One measured panel per configured focus category.

    The operator configured a handful of verticals on purpose: breadth cannot
    reach the evidence threshold, concentration can. So this reports each
    vertical separately -- supply, asking prices, sale evidence, and the follow-up
    debt that stands between the current state and a reportable estimate --
    rather than a single blended number that hides which vertical is ready.

    Every figure is the same one the rest of the workspace reports, computed on
    the same cohort definition, so a vertical cannot look better here than it does
    in the market panels. Categories the configuration names but the facet tree
    has not labelled are still listed, with the path as its name, because a
    silent omission would read as "nothing to collect".
    """
    if isinstance(horizon, bool) or str(horizon) not in ('7', '14', '30', '60', '90'):
        raise ValueError('unsupported sale horizon')
    horizon = int(horizon)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    where, params, applied = metrics.listing_filters(**(filters or {}))
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = db.query("SELECT " + FIELDS + " FROM listings" + clause, params)
    market = [row for row in rows if not row["is_giveaway"] and row["listed_price_eur"] != 0]
    types = accepted_product_types(rows)
    tasks = collection.task_snapshot()
    panels = []
    for path in config.FOCUS_CATEGORIES:
        scope = collection.Scope(categories=(path,))
        name = scope.category_names[0] if scope.category_names else path
        members = [row for row in market if scope.matches(row)]
        liq = metrics.liquidity_rows(members, horizon, filters={"focus_category": path})
        collection_members = [row for row in rows if scope.matches(row)]
        follow = collection.summarize(collection_members, now, scope, types)
        follow['queue'] = collection.queue_summary(collection_members, now, scope, tasks)
        rates = {evidence: report.get("estimates", {}).get(f"{horizon}d")
                 for evidence, report in liq["sell_through_by_evidence"].items()}
        panels.append({
            "path": path, "name": name,
            # `Scope.category_names` always holds one entry per path, falling back
            # to the raw id when the facet tree has no label for it. An operator
            # reading "4.404.0" needs to know the taxonomy has not caught up,
            # rather than trusting it as a category name.
            "resolved_label": name != path,
            "root": name.split(" > ")[0] if name != path else None,
            "n_tracked": len(members), **price_panel(members, cutoff),
            "sold_declared": liq["sold_inferred_seller_declared"],
            "sold_verified": liq["sold_confirmed"],
            "sale_rates": rates, "sale_status": ('OK' if any(rate is not None for rate in rates.values())
                                                   else 'INSUFFICIENT DATA' if liq['status'] == 'OK'
                                                   else liq['status']),
            "horizon_support": liq["horizon_support"][f"{horizon}d"],
            "followup_coverage": liq["followup_coverage"],
            "liquidity": liq, "followup": follow,
            "grid": grid_coverage(scope),
            "freshness": {
                "discovered_24h":sum(bool(metrics.parse_ts(r['first_seen_at']) and metrics.parse_ts(r['first_seen_at']) >= now-timedelta(days=1)) for r in collection_members),
                "observed_24h":sum(bool(metrics.parse_ts(r['last_seen_at']) and metrics.parse_ts(r['last_seen_at']) >= now-timedelta(days=1)) for r in collection_members),
                "accepted_classifications":sum(r['listing_pk'] in types for r in collection_members),
                "cycle_interval_seconds":config.COLLECTION_INTERVAL_SECONDS,
            },
            "basis": "Observed. Asking prices are not clearing prices; a sale marker is not a realised price.",
        })
        panel = panels[-1]
        def gate(n, ready):
            return {'count': n, 'required': config.MIN_N_FOR_REPORT,
                    'missing': max(0, config.MIN_N_FOR_REPORT-n), 'ready': ready}
        panel['evidence_gates'] = {
            'prices': gate(panel['n_priced'], panel['status'] == 'OK'),
            'horizon': gate(panel['horizon_support'], panel['horizon_support'] >= config.MIN_N_FOR_REPORT),
            'sales': {kind: gate(report['n_events'], rates[kind] is not None)
                      for kind, report in liq['sell_through_by_evidence'].items()}}
        if panel['status'] != 'OK':
            step = ('prices', 'Build fresh price evidence',
                    f"Needs {panel['evidence_gates']['prices']['missing']} more fresh priced items. Inspect product identity and condition as the sample grows.", 'observations')
        elif not panel['evidence_gates']['horizon']['ready']:
            step = ('horizon', 'Maintain successful follow-up',
                    f"{panel['horizon_support']} of {config.MIN_N_FOR_REPORT} observations reach {horizon} days. Repeated successful checks build horizon support; more discoveries alone cannot clear this gate.", 'collection')
        elif panel['sale_status'] != 'OK':
            step = ('sales', 'Build sale-marker evidence',
                    f"Each sale class needs {config.MIN_N_FOR_REPORT} markers. Continue bounded follow-up; disappeared listings do not establish sales.", 'collection')
        else:
            step = ('peers', 'Inspect comparable products',
                    'Price and sale evidence support this horizon. Verify model, variant, condition and completeness before comparing individual items.', 'observations')
        panel['next_step'] = dict(zip(('code', 'title', 'detail', 'view'), step))
    totals = collection.summarize(rows, now, collection.Scope(), types)
    totals['queue'] = collection.queue_summary(rows, now, collection.Scope(), tasks)
    return {"generated_at": now.isoformat(), "filters": applied, "horizon": horizon,
            "min_n": config.MIN_N_FOR_REPORT, "max_age_hours": config.PANEL_MAX_AGE_HOURS,
            "timezone": "Europe/Helsinki",
            "n_configured": len(config.FOCUS_CATEGORIES),
            "focus_municipalities": list(config.FOCUS_MUNICIPALITIES),
            "panels": panels, "totals": totals,
            "n_selected": len(rows), "n_market": len(market),
            "prices_ready": sum(panel["status"] == "OK" for panel in panels),
            "sale_ready": sum(panel["sale_status"] == "OK" for panel in panels),
            "note": ("Each vertical is measured on its own cohort. A vertical reports no median until it "
                     "reaches the minimum number of fresh priced observations, and no sale rate until "
                     "each evidence class has enough events and follow-up at the selected horizon. "
                     "Collection counts include all stored items in scope; market estimates exclude giveaways and zero prices. "
                     "Queued counts describe due admitted tasks when bounded follow-up is enabled; "
                     "historical probe timestamps include failed attempts.")}


def snapshot(filters=None, dimension="category_root_norm", days=30, horizon=30):
    if dimension not in DIMENSIONS:
        raise ValueError("unsupported analysis dimension")
    if isinstance(days, bool) or isinstance(horizon, bool):
        raise ValueError("analysis windows must be integers")
    if str(days) not in ("7", "30", "90") or str(horizon) not in ("7", "14", "30", "60", "90"):
        raise ValueError("unsupported analysis window or horizon")
    days, horizon = int(days), int(horizon)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    where, params, applied = metrics.listing_filters(**(filters or {}))
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = db.query("SELECT " + FIELDS + " FROM listings" + clause, params)
    market = [row for row in rows if not row["is_giveaway"] and row["listed_price_eur"] != 0]
    types = accepted_product_types(market)
    identities = product_identity.resolve_rows(market, product_types=types)
    collection_scope = collection.Scope()
    groups = defaultdict(list)
    for row in market:
        key = types.get(row["listing_pk"]) if dimension == "product_type" else row[dimension]
        if key:
            groups[key].append(row)
    matrix = []
    for key, members in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:60]:
        liq = metrics.liquidity_rows(members, horizon, filters={dimension: key})
        rates = {evidence: report.get("estimates", {}).get(f"{horizon}d")
                 for evidence, report in liq["sell_through_by_evidence"].items()}
        matrix.append({"label": key, "n_tracked": len(members), **price_panel(members, cutoff),
                       "sold_declared": liq["sold_inferred_seller_declared"],
                       "sold_verified": liq["sold_confirmed"], "sale_rates": rates,
                       "followup_coverage": liq["followup_coverage"],
                       "horizon_support": liq["horizon_support"][f"{horizon}d"],
                       "n_stale": sum(bool(metrics.parse_ts(row["last_seen_at"]) and metrics.parse_ts(row["last_seen_at"]) < cutoff) for row in members),
                       "followup": collection.summarize(members, now, collection_scope, types),
                       "basis": "derived" if dimension == "product_type" else "observed"})
    today = now.astimezone(LOCAL_ZONE).date()
    timeline = {str(today-timedelta(days=offset)): {"date": str(today-timedelta(days=offset)),
                "new_observations": 0, "sold_declared": 0, "sold_verified": 0, "price_reductions": 0}
                for offset in range(days-1, -1, -1)}
    for row in market:
        for field, count in (("first_seen_at", "new_observations"), ("outcome_observed_at", row["outcome_status"])):
            stamp = metrics.parse_ts(row[field])
            key = str(stamp.astimezone(LOCAL_ZONE).date()) if stamp else None
            count = {"sold_inferred": "sold_declared", "sold_confirmed": "sold_verified"}.get(count, count)
            if key in timeline and count in timeline[key]:
                timeline[key][count] += 1
    reductions = db.query("SELECT e.listing_pk, e.event_at, e.before_value, e.after_value FROM events e "
                          "WHERE e.event_type='price_changed' AND e.event_at>=?", ((now-timedelta(days=days)).isoformat(),))
    selected = {row["listing_pk"] for row in market}
    for event in reductions:
        stamp = metrics.parse_ts(event["event_at"])
        key = str(stamp.astimezone(LOCAL_ZONE).date()) if stamp else None
        try:
            before, after = amount(event["before_value"]), amount(event["after_value"])
        except (ValueError, TypeError):
            continue
        if key in timeline and event["listing_pk"] in selected and before is not None and after is not None and after < before:
            timeline[key]["price_reductions"] += 1
    liq = metrics.liquidity_rows(market, horizon, filters=applied)
    owned = portfolio(now)
    available = [row for row in market if is_fresh(row, cutoff)]
    risks = []
    def risk(code, severity, title, detail, count, view):
        if count:
            risks.append(dict(code=code, severity=severity, title=title, detail=detail, count=count, view=view))
    risk("followup", "amber", "Sale evidence needs follow-up", "Listings without a status probe. Repeated checks build outcome and timing evidence.", sum(not row["last_probed_at"] for row in market), "collection")
    risk("stale", "amber", "Stale observations", "Older observations are excluded from current asking-price comparisons.", sum(bool(metrics.parse_ts(row["last_seen_at"]) and metrics.parse_ts(row["last_seen_at"]) < cutoff) for row in market), "collection")
    risk("condition", "amber", "Condition is unknown", "Check condition before treating low asking prices as comparable.", sum(not row["condition_norm"] or row["condition_norm"] == "unknown" for row in available), "observations")
    risk("costs", "red", "Recorded sales have incomplete costs", "Complete the ledger costs to establish actual profit.", owned["n_unknown_cost_sales"], "ledger")
    risk("aging", "amber", "Capital tied up for over 30 days", "Review your oldest stock and its carrying costs.", owned["age_buckets"]["31–60d"]+owned["age_buckets"]["61d+"], "ledger")
    demand = ('OK' if any(report.get('estimates', {}).get(f'{horizon}d') is not None
                          for report in liq['sell_through_by_evidence'].values())
              else 'INSUFFICIENT DATA' if liq['status'] == 'OK' else liq['status'])
    from . import opportunities
    opportunity_board = opportunities.board({k:v for k,v in (filters or {}).items() if v is not None and v != ""}, days=days, horizon=horizon, limit=24, reference_at=now)
    candidates = research_candidates(market, cutoff, types, identities, filters=applied)
    return {"opportunities": opportunity_board, "generated_at": now.isoformat(), "filters": applied, "dimension": dimension,
            "days": days, "horizon": horizon, "timezone": "Europe/Helsinki",
            "min_n": config.MIN_N_FOR_REPORT, "max_age_hours": config.PANEL_MAX_AGE_HOURS,
            "n_selected": len(rows), "n_market": len(market), "n_available": len(available),
            "n_classified": len(types), "matrix": matrix, "n_groups": len(groups),
            "n_ungrouped": sum(not (types.get(row["listing_pk"]) if dimension == "product_type" else row[dimension]) for row in market),
            "timeline": list(timeline.values()), "liquidity": liq, "portfolio": owned,
            "followup": collection.summarize(market, now, collection_scope, types),
            "candidates": candidates, "risks": risks,
            "research_inventory": (research_inventory(market, cutoff, identities)
                                   if not candidates else {"items": [], "total": 0}),
            "comparison_coverage": comparison_coverage(market, cutoff, types, identities),
            "product_identity": product_identity.quality_rows(market, identities),
            "classification_quality": classification_quality(market),
            "buy_recommendations": _generate_buy_recommendations(market, cutoff, horizon=horizon, product_types=types, identities=identities),
            "readiness": {"prices": price_panel(market, cutoff)["status"], "demand": demand,
                          "profit": "OK" if owned["n_complete_margins"] >= config.MIN_N_FOR_REPORT else "INSUFFICIENT DATA"}}


def _shortlist_line(row):
    """One shortlist row: the gap, the benchmark it was measured against, and what
    that benchmark is worth.

    The peer median is printed with the interval it was estimated over. A bare
    "64% below the median" reads as a fact about the item when it is a comparison
    against a number that may be anywhere in a wide band, and on a thin or mixed
    peer group that band is wide enough to contain the ask itself.
    """
    interval = row.get("peer_median_ci95")
    median = (f'EUR {row["peer_median_eur"]:.0f}'
              + (f' (95% CI {interval["lo"]:.0f}-{interval["hi"]:.0f})' if interval else ''))
    lines = [f'  {row["gap_pct"]:>5.1f}% under peer median  EUR {row["ask_eur"]:.0f} '
             f'vs {median} (n={row["n_peers"]})  {row["title"][:40]}']
    identity = IDENTITY_BASES[row["identity_basis"]][0]
    verdict = ("minimum gap survives that interval" if row.get("min_gap_survives_ci")
               else "minimum gap is not established by that interval" if interval
               else "no interval available")
    lines.append(f"    peers matched on {identity}; {verdict}")
    return lines


def render_analysis(report):
    """A readable brief that refuses to print an estimate the data cannot carry."""
    applied = {k: v for k, v in (report.get("filters") or {}).items() if v}
    lines = [
        f"Analyst brief  ·  generated {report['generated_at']}",
        f"segment: {', '.join(f'{k}={v}' for k, v in sorted(applied.items())) or 'all markets'}"
        f"  ·  grouped by {report['dimension']}"
        f"  ·  {report['horizon']}d sale horizon  ·  {report['timezone']}",
        f"selected {report['n_selected']} observations, {report['n_market']} priced-market "
        f"items, {report['n_available']} currently available",
        "",
        "readiness",
    ]
    for question, state in report["readiness"].items():
        lines.append(f"  {question:<8} {state}")
    if report.get("product_identity"):
        lines += ["", product_identity.render_quality(report["product_identity"])]
    followup = report.get("followup")
    if followup:
        lines += ["", "follow-up capacity",
                  f"  {followup['n_known_condition']} known conditions / {followup['n_tracked']} tracked; "
                  f"{followup['n_classified']} current accepted classifications",
                  f"  {followup['n_due']} probes due, {followup['n_due_in_scope']} within collection focus; "
                  f"{followup['n_never_probed']} never probed",
                  f"  minimum {followup['probe_runs_at_budget']} full-budget runs at "
                  f"{followup['item_budget']} items/run for the current in-scope backlog",
                  f"  reserve {followup['repeat_probe_reserve']} slots/run for due repeats "
                  "across collection focus; unused slots are borrowed"]
    lines += ["", f"segments ({report['n_groups']} groups, {report['n_ungrouped']} ungrouped)"]
    for row in report["matrix"][:15]:
        if row["status"] == "OK":
            ci = row.get("median_ci95")
            price = (f"median EUR {row['median_eur']:.0f}"
                     + (f" (95% CI {ci['lo']:.0f}-{ci['hi']:.0f})" if ci else ""))
        else:
            price = f"no supported price (needs {config.MIN_N_FOR_REPORT} priced)"
        sold = row["sold_declared"] + row["sold_verified"]
        lines.append(f"  {row['label'][:38]:<38} n={row['n_tracked']:<5} "
                     f"avail={row['n_available']:<5} {price}  sold={sold}")
    if report["candidates"]:
        lines += ["", "price research shortlist (asking prices only, verify before acting)"]
        for row in report["candidates"][:8]:
            lines += _shortlist_line(row)
    else:
        lines += ["", "shortlist: none. A candidate needs a known condition, a detailed "
                      "category, and more peers than the reporting threshold."]
    portfolio = report["portfolio"]
    lines += ["", "your own trades (independent of market filters)"]
    lines.append(f"  purchases {portfolio['n_purchases']}, sold {portfolio['n_sold']}, "
                 f"in stock {portfolio['n_in_stock']}")
    if portfolio["realized_profit_eur"] is not None:
        partial = " PARTIAL - some sales have unknown costs" if portfolio["profit_is_partial"] else ""
        lines.append(f"  realized profit EUR {portfolio['realized_profit_eur']:.2f} "
                     f"over {portfolio['n_complete_margins']} fully costed sales{partial}")
    else:
        lines.append("  realized profit: none established. Record purchases and their sales.")
    lines.append(f"  capital in stock EUR {portfolio['acquisition_capital_eur']:.2f}")
    if report["risks"]:
        lines += ["", "what to look out for"]
        for risk in report["risks"]:
            lines.append(f"  [{risk['severity']}] {risk['title']} ({risk['count']}) - {risk['detail']}")

    recs = report.get("buy_recommendations") or []
    if recs:
        lines += ["", "buy/no-buy view (derived from observed asks and our own trades)"]
        for rec in recs[:8]:
            sale = rec.get("expected_sale_price_eur")
            sale_text = f"{sale:.0f}" if sale is not None else "unknown"
            margin = rec.get("expected_gross_margin_eur")
            if margin is not None:
                margin_text = (f"  exp margin EUR {margin:.2f} "
                               f"({rec.get('expected_gross_margin_pct', 0) * 100:.1f}%)")
            else:
                margin_text = "  exp margin unknown"
            price_basis = rec.get("expected_sale_price_basis", "")
            lines.append(f"  {rec['listing_id']}: ask EUR {rec['ask_eur']:.0f}, "
                         f"exp sale EUR {sale_text} ({price_basis}), "
                         f"p_sale {format(rec['p_sale_within_horizon'], '.0%') if rec.get('p_sale_within_horizon') is not None else 'unknown'} "
                         f"confidence {rec.get('confidence_tier', '?')}{margin_text}")
            if rec.get("risk_flags"):
                lines.append(f"    risks: {' | '.join(rec['risk_flags'])}")
    else:
        lines += ["", "buy/no-buy view: none. A recommendation needs a priced market slice "
                      "with enough tracked listings to support a sell-through estimate."]

    lines += ["", "Asking prices are not clearing prices. Sale dates are detection dates. "
              "Every estimate above carries n.", ""]
    return "\n".join(lines)
