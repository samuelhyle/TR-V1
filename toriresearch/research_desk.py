"""The price research desk: a wide, explainable shortlist and its per-item evidence.

A shortlist row is one number -- a percentage below a peer median -- and one number
is not a decision. The analyst view already refuses to imply otherwise: it prints
the median's interval beside the median and names how the peers were matched. This
module is that same comparison, widened and made inspectable, for the case where
the question is no longer "is anything cheap in my market" but "what do I know
about this specific item, and what would I still have to go and look at".

Three things are deliberate about how it is built.

**The cohort is computed once.** `analysis.peer_cohorts` is the single definition
of which listings are comparable, and both the shortlist and the per-item view read
it. A desk that ranked items by one rule and explained them by another would let
the explanation disagree with the ranking, and the reader would have no way to tell
which one was wrong.

**Every gap carries the uncertainty in its own benchmark.** A peer median with a
95% interval of 78-180 does not make an item at 97 look 20% cheap. `min_gap_survives_ci`
tests the chosen threshold; `gap_survives_ci` retains its positive-gap meaning.
Both are reported as `null`, not as
`false`, when there is no interval to test against -- an untested claim and a failed
one are different things.

**Asking prices stay asking prices.** Nothing here converts a peer median into an
expected resale price, and nothing here converts a discount into a profit. Proceeds,
fees and repairs are the operator's assumptions, held in `research.scenario` and
evaluated on request. The most valuable thing this endpoint can return on a thin
market is the sentence saying the evidence does not support a number yet.
"""

import json
from datetime import datetime, timedelta, timezone

from . import analysis, config, db, metrics, product_identity, purchases, realization, listing_analysis, product_profiles

# Defaults for a page that is read by a person choosing a threshold, not by a script.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_PEER_ROWS = 12
MAX_PEER_ROWS = 100
SESSION_LIMIT = 50
SESSION_SELECTION_LIMIT = 50
HISTOGRAM_BINS = 8
# A price distribution with fewer than this many peers in it is not a shape.
MIN_HISTOGRAM_PEERS = 5

SESSION_FILTER_FIELDS = {"platform", "subplatform", "category_root_norm", "category_norm",
                         "municipality_norm", "condition_norm", "seller_type_norm", "focus_path"}


def _json(value):
    """Deterministic JSON for stored rows. NaN is not JSON and no browser accepts it."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      default=lambda item: item.isoformat())


def _text(value, label, limit, required=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label} must be text of at most {limit} characters")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} is required")
    return value


def _bounded_int(value, label, low, high, default):
    """A query-string integer, refusing anything that is not exactly one.

    `int("3.5")` raises but `int(" 3 ")` succeeds and `int(True)` is 1, so the
    string is compared to its own normalised form rather than coerced and hoped
    for. A threshold the operator did not choose must not be silently substituted.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    text = str(value).strip()
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer") from None
    if str(number) != text or not low <= number <= high:
        raise ValueError(f"{label} must be {low}..{high}")
    return number


def _flag(value, default=False):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _cutoff():
    return datetime.now(timezone.utc) - timedelta(hours=config.PANEL_MAX_AGE_HOURS)


def _horizon(value):
    horizon = _bounded_int(value, "sale horizon", 1, 365, 30)
    if str(horizon) not in ("7", "14", "30", "60", "90"):
        raise ValueError("sale horizon must be 7, 14, 30, 60 or 90 days")
    return horizon


# --------------------------------------------------------------------------
# the shortlist
# --------------------------------------------------------------------------

def candidates(filters=None, min_gap_pct=20, limit=DEFAULT_LIMIT, peer_stats=True,
               peer_rows=0, horizon=30):
    """Every qualifying candidate in the slice, not just the analyst view's twelve.

    Widening the list is only safe because the ordering is not the ranking: a
    shortlist of twelve is a summary of the best-supported comparisons, and a list
    of two hundred is a work queue. The caller chooses; the evidence attached to
    each row does not change, so a long list cannot promote a weak comparison by
    being long.
    """
    from . import research
    filters = research.clean_filters({} if filters is None else filters)
    min_gap_pct = _bounded_int(min_gap_pct, "minimum gap", 1, 99, 20)
    limit = _bounded_int(limit, "candidate limit", 1, MAX_LIMIT, DEFAULT_LIMIT)
    peer_rows = _bounded_int(peer_rows, "peer rows", 0, MAX_PEER_ROWS, 0)
    horizon = _horizon(horizon)
    with db.read_snapshot():
        return _candidates(filters, min_gap_pct, limit, bool(peer_stats), peer_rows, horizon)


def _candidates(filters, min_gap_pct, limit, peer_stats, peer_rows, horizon):
    where, params, _applied = metrics.listing_filters(**filters)
    rows = db.query("SELECT " + analysis.FIELDS + " FROM listings WHERE " + " AND ".join(where), params)
    market = [row for row in rows if not row["is_giveaway"] and row["listed_price_eur"] != 0]
    cutoff = _cutoff()
    types = analysis.accepted_product_types(market)
    identities = product_identity.resolve_rows(market, product_types=types)
    found = analysis.research_candidates(market, cutoff, types, identities,
                                         min_gap_pct=min_gap_pct, limit=None,
                                         peer_stats=peer_stats, peer_rows=peer_rows, filters=filters)
    # The headline count is what the threshold would admit with no limit applied,
    # so a truncated page says so instead of implying the list ended.
    total = len(found)
    report = _envelope(found[:limit], filters, min_gap_pct, limit, total, len(rows), len(market),
                     cutoff, horizon, peer_stats, peer_rows)
    coverage = analysis.comparison_coverage(market, cutoff, types, identities)
    report["comparison_coverage"] = coverage
    report["summary"]["blocked_by_reason"] = coverage["blocked_by_reason"]
    report["summary"]["blocked_reasons_overlap"] = True
    report["research_inventory"] = (analysis.research_inventory(market, cutoff, identities, limit)
                                    if not found else {"items": [], "total": 0})
    return report


def _envelope(found, filters, min_gap_pct, limit, total, n_selected, n_market, cutoff, horizon,
              peer_stats, peer_rows):
    """Wrap the rows in the context a reader needs before trusting any of them."""
    by_basis = {}
    for row in found:
        by_basis[row["identity_basis"]] = by_basis.get(row["identity_basis"], 0) + 1
    gaps = sorted(row["gap_pct"] for row in found)
    robust = sum(row["gap_survives_ci"] is True for row in found)
    untested = sum(row["gap_survives_ci"] is None for row in found)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_at": (cutoff + timedelta(hours=config.PANEL_MAX_AGE_HOURS)).isoformat(),
        "filters": filters,
        "min_n": config.MIN_N_FOR_REPORT,
        "max_age_hours": config.PANEL_MAX_AGE_HOURS,
        "min_gap_pct": min_gap_pct,
        "horizon_days": horizon,
        "candidates": found,
        "summary": {
            "total_candidates": total,
            "returned": len(found),
            "truncated": total > len(found),
            "by_identity_basis": by_basis,
            "gap_survives_ci": robust,
            "min_gap_survives_ci": sum(row["min_gap_survives_ci"] is True for row in found),
            "distinct_cohorts": len({row["cohort_id"] for row in found}),
            "gap_untested": untested,
            "median_gap_pct": round(gaps[len(gaps) // 2], 1) if gaps else None,
            "n_selected": n_selected,
            "n_market": n_market,
            "peer_stats_included": peer_stats,
            "peer_rows_included": peer_rows,
        },
        "requirements": {
            "min_peers": config.MIN_N_FOR_REPORT,
            "detailed_category": "A top-level category cannot make unrelated products comparable",
            "known_condition": "An unknown condition disqualifies the cohort entirely",
            "accepted_product_type": "Derived product type must pass its own version, confidence and category-home checks",
            "reviewed_offer": "A supported product variant and reviewed working single-unit offer are required; broad types remain market context",
            "both_filters": "Below the peer lower quartile and at least the minimum gap below the peer median",
        },
        "note": ("Asking prices, not clearing prices. A peer median is a comparison against what other "
                 "sellers are asking for right now, so a gap is a reason to inspect an item, never a "
                 "margin. Sale-marker incidence for a cohort is reported separately and is not an "
                 "individual sale probability."),
    }


# --------------------------------------------------------------------------
# one candidate, in full
# --------------------------------------------------------------------------

def candidate(listing_pk, peer_rows=DEFAULT_PEER_ROWS, horizon=30, min_gap_pct=20,
              filters=None, reference_at=None, expected_revision=None):
    """Everything the desk knows about one listing, and what it still does not.

    The candidate row is recomputed from the same cohort function and the same
    predicate the list uses, so this page cannot describe an item as a bargain
    that the list does not, or miss one the list shows. `min_gap_pct` is accepted
    for that reason: a reader who widened the list to 5% and then opened an item
    must see it judged by the 5% they chose, not silently by a default.

    When the item does not qualify, the reason is named rather than an empty
    panel. "Its own price is not below its cohort" and "its cohort has 6 peers,
    not 30" are different research positions and lead to different next steps.
    """
    peer_rows = _bounded_int(peer_rows, "peer rows", 1, MAX_PEER_ROWS, DEFAULT_PEER_ROWS)
    horizon = _horizon(horizon)
    min_gap_pct = _bounded_int(min_gap_pct, "minimum gap", 1, 99, 20)
    listing_pk = _bounded_int(listing_pk, "listing key", 1, 2**62, None)
    if listing_pk is None:
        raise ValueError("a listing key is required")
    from . import research
    filters = research.clean_filters({} if filters is None else filters)
    reference = analysis.comparison_reference(reference_at)
    if expected_revision is not None:
        expected_revision = _text(expected_revision, "benchmark revision", 64, required=True)
    with db.read_snapshot():
        return _candidate(listing_pk, peer_rows, horizon, min_gap_pct, filters, reference, expected_revision)


def _candidate(listing_pk, peer_rows, horizon, min_gap_pct=20, filters=None,
               reference=None, expected_revision=None, cohort_cache=None, summary_only=False):
    from . import research
    row = db.query_one("SELECT " + analysis.FIELDS + " FROM listings WHERE listing_pk=?", (listing_pk,))
    if not row:
        raise ValueError("choose a listing already stored in this workspace")
    reference = reference or datetime.now(timezone.utc)
    cutoff = reference - timedelta(hours=config.PANEL_MAX_AGE_HOURS)

    # The cohort has to be read, not inferred. A listing's group key names a
    # category, and the *other* members of that group are other rows in the
    # database -- handing `peer_cohorts` the single subject row would produce a
    # cohort of one and a median computed from the item being judged. So the
    # same-category superset is loaded, and the shared cohort function does the
    # narrowing, which is also what keeps this page's cohort identical to the one
    # the list ranked the item in.
    scope_key = (row["platform"], row["subplatform"], row.get("category_norm"),
                 _json(filters or {}))
    resolved = (cohort_cache or {}).get(scope_key)
    if resolved is None:
        resolved = analysis.resolve_peer_cohort(row, cutoff, filters)
        if cohort_cache is not None:
            cohort_cache[scope_key] = resolved
    else:
        key = resolved["index"].get(listing_pk)
        resolved = dict(resolved, key=key, peers=resolved["cohorts"].get(key, []),
                        incidence_peers=resolved["outcome_cohorts"].get(key, []),
                        relationship_admission=resolved["relationship_admissions"].get(listing_pk),
                        subject_in_scope=any(p["listing_pk"] == listing_pk for p in resolved["rows"]))
    key, peers = resolved["key"], resolved["peers"]
    identity = resolved["identities"].get(listing_pk)
    if identity is None:
        identity = product_identity.resolve_rows([row])[listing_pk]
    eligibility = analysis.comparison_admission(row, identity, resolved["relationship_admission"])

    ordered = sorted(float(peer["listed_price_eur"]) for peer in peers)
    price = analysis.amount(row["listed_price_eur"])
    quantiles, stats, interval = None, None, None
    verdict = None
    if price is not None and len(ordered) > config.MIN_N_FOR_REPORT:
        from bisect import bisect_left
        quantiles = analysis.leave_one_out(ordered, bisect_left(ordered, price))
        stats = analysis.peer_sample(ordered, bisect_left(ordered, price))
        interval = stats["median_ci95"]
    metadata = analysis.benchmark_context(row, key, peers, cutoff, resolved["filters"], resolved["identities"]) if key else {}
    if not resolved["subject_in_scope"]:
        verdict = ("outside_requested_scope", "This listing is outside the selected market filters.")
    elif not eligibility["eligible"]:
        verdict = (eligibility["reasons"][0],
                   "A supported comparison needs a current reviewed product variant and single-unit offer. "
                   "Admission is blocked by: " + ", ".join(eligibility["reasons"]) + ".")
    elif expected_revision and metadata.get("benchmark_revision") != expected_revision:
        verdict = ("benchmark_changed", "The peer evidence has changed. Refresh the shortlist before deciding.")
    elif price is None or not analysis.is_priced(row, cutoff):
        verdict = ("not_priced", "The asking price is missing or the observation is outside the freshness "
                                 "window, so there is nothing to compare.")
    elif key is None:
        verdict = ("no_comparable_cohort", "This listing has no cohort to be compared against: it needs a "
                                           "detailed category, a known condition and an accepted derived "
                                           "product type. See the classification panel.")
    elif stats is None:
        verdict = ("cohort_too_small", f"There are {max(0, len(peers)-1)} other eligible priced observations; "
                   f"{config.MIN_N_FOR_REPORT} other observations are required for a benchmark.")
    elif price >= quantiles[0.25]:
        # The lower-quartile rule is a rule about position in the distribution, not
        # a percentage, so it does not move with the reader's threshold. Say which
        # test failed, because "not below the cohort" and "below it but not by
        # enough" lead to different decisions.
        verdict = ("not_below_quartile", f"It asks {price:.0f}. Its cohort's lower quartile is "
                                         f"{quantiles[0.25]:.0f} across {stats['n']} peers, so this item "
                                         f"is not in the bottom quarter of what comparable sellers are "
                                         f"asking.")
    elif not analysis.qualifies_as_candidate(price, quantiles, min_gap_pct):
        verdict = ("gap_too_shallow", f"It asks {price:.0f}, below the cohort's lower quartile of "
                                      f"{quantiles[0.25]:.0f} but within {min_gap_pct}% of the median of "
                                      f"{quantiles[0.5]:.0f}. Lowering the minimum gap in the list can "
                                      f"include it.")
    # Otherwise it qualifies: below the cohort's lower quartile and at least
    # `min_gap_pct` under its median. `verdict` stays None, which the response
    # reports as `qualifies: true` rather than as a missing verdict.

    identity_basis = ("canonical_variant" if key and key[7]
                      else analysis._identity_basis(key[5] if key else row.get("brand_norm"),
                                                     key[6] if key else row.get("model_norm")))
    candidate_row = (dict(_candidate_row(row, quantiles, stats, key, identity_basis, min_gap_pct), **metadata)
                     if verdict is None else None)
    verdict_payload = {"qualifies": verdict is None, "code": verdict[0] if verdict else None,
                       "detail": verdict[1] if verdict else None, "min_gap_pct": min_gap_pct,
                       "min_n": config.MIN_N_FOR_REPORT}
    if summary_only:
        return {"listing": {"title": row["title"], "ask_eur": price, "price_observed_at": row["price_observed_at"]},
                "candidate": candidate_row, "verdict": verdict_payload}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_at": reference.isoformat(), "filters": resolved["filters"],
        "eligibility": eligibility, **metadata,
        "identity": identity,
        "profile": product_profiles.summary(identity, identity.get("listing_analysis"), row),
        "listing_analysis": identity.get("listing_analysis") or listing_analysis.attach([row])[0]["listing_analysis"],
        "listing_pk": listing_pk,
        "listing": {
            "listing_pk": listing_pk, "listing_id": row["listing_id"], "title": row["title"],
            "platform": row["platform"], "subplatform": row["subplatform"],
            "category_norm": row["category_norm"], "category_root_norm": row["category_root_norm"],
            "condition_norm": row["condition_norm"], "municipality_norm": row["municipality_norm"],
            "seller_type_norm": row["seller_type_norm"], "brand_norm": row["brand_norm"],
            "model_norm": row["model_norm"], "ask_eur": price,
            "buyer_shipping_eur": analysis.amount(row["shipping_cost_buyer_eur"]),
            "buyer_fee_eur": analysis.amount(row["buyer_fee_eur"]),
            "last_seen_at": row["last_seen_at"], "price_observed_at": row["price_observed_at"],
            "status_norm": row["status_norm"], "outcome_status": row["outcome_status"],
            "coverage": row["coverage"],
        },
        "candidate": candidate_row,
        "verdict": verdict_payload,
        "cohort": _cohort(row, peers, key, stats, identity_basis, horizon, resolved["incidence_peers"]),
        "inspection": inspection_checklist(row, key, identity_basis, stats, identity),
        "owned_comparables": owned_comparables(row),
        "decisions": _recorded_decisions(row),
        "links": {
            "source": {"url": research.market_url(row["platform"], row["listing_id"])},
            "provenance": {"path": "/api/research/provenance", "params": {"platform": row["platform"],
                                                                         "listing_id": row["listing_id"]},
                           "purpose": "Which channel wrote each field, every retained price measurement, and warnings"},
            "evidence": {"path": "/api/research/listing", "params": {"platform": row["platform"],
                                                                    "listing_id": row["listing_id"]},
                         "purpose": "Stored inspection context and peer examples for this exact listing"},
            "decide": {"path": "/api/research", "method": "POST",
                       "purpose": "Record a Watch / Inspect / Reject / Pursue decision with these assumptions"},
        },
        "note": ("Nothing on this page is a resale price or a profit figure. The cohort statistics are "
                 "asking prices of other sellers' current listings; proceeds, fees and repairs are your "
                 "own assumptions to enter."),
    }


def _cohort_superset(row, filters=None):
    """Every stored listing in the subject's own detailed category and market.

    A deliberately wide superset: the shared cohort function then narrows it to
    the exact group, so the narrowing rule lives in one place rather than being
    restated here as a second, driftable copy of the matching conditions. Reading
    only the subject would produce a cohort of one and a "median" computed from
    the item being judged.

    A listing with no detailed category has no superset to read, and gets itself
    alone -- which the cohort function then refuses, for the right reason.
    """
    category = row.get("category_norm")
    if not category or category == row.get("category_root_norm"):
        return [row]
    return analysis.resolve_peer_cohort(row, _cutoff(), filters)["rows"]


def _candidate_row(row, quantiles, stats, key, identity_basis, min_gap_pct):
    """The shortlist row for one listing, or None when it does not qualify.

    It runs the shared predicate, so this page and the list cannot disagree about
    the same database. Returning a candidate-shaped row for an item the list would
    not show would be worse than returning nothing: a consumer reading
    `candidate.gap_pct` would find `0.0` and conclude the item is priced exactly
    at its cohort's median, which is a measured claim this item does not support.
    """
    if not stats or stats["n"] < config.MIN_N_FOR_REPORT:
        return None
    price = float(row["listed_price_eur"])
    if not analysis.qualifies_as_candidate(price, quantiles, min_gap_pct):
        return None
    _category, _sub, category, condition, product_type, brand, model, _canonical = key
    interval = stats["median_ci95"]
    return {
        "listing_pk": row["listing_pk"], "listing_id": row["listing_id"], "title": row["title"],
        "platform": row["platform"], "subplatform": row["subplatform"],
        "category_norm": category, "condition_norm": condition,
        "municipality_norm": row["municipality_norm"], "ask_eur": price,
        "seller_type_norm": row.get("seller_type_norm"),
        "peer_median_eur": stats["median_eur"], "peer_p25_eur": stats["p25_eur"],
        "peer_median_ci95": interval,
        **analysis.gap_evidence(price, interval, min_gap_pct),
        "gap_pct": round((1 - price / quantiles[0.5]) * 100, 1),
        "n_peers": stats["n"], "product_type": product_type, "brand_norm": brand, "model_norm": model,
        "last_seen_at": row["last_seen_at"], "identity_basis": identity_basis,
        "peer_sample": stats, "requires_variant_verification": True,
        "min_gap_pct": min_gap_pct,
        "basis": analysis.IDENTITY_BASES[identity_basis][1]
                 + "; variants require inspection; asking prices only",
    }


def _recorded_decisions(row):
    """Decisions you already recorded about this listing, newest first.

    The desk exists to reach a decision, so showing whether one already exists is
    part of the item's evidence rather than a separate trip to the journal. It
    also stops the most common quiet failure in this workflow: recording a second,
    different assessment of an item you already rejected or already bought, with
    no sight of the first.

    Reassessment is allowed and is the intended workflow -- a new price or a new
    inspection legitimately produces a new entry -- so this lists them rather than
    blocking anything. What it must never do is present the newest entry as *the*
    decision when an older one says something different, so the count is reported
    alongside the latest.
    """
    rows = db.query(
        "SELECT d.decision_pk, d.recorded_at, d.decision, d.notes, d.listing_pk "
        "FROM research_decisions d WHERE d.platform=? AND d.source_listing_id=? "
        "ORDER BY d.decision_pk DESC LIMIT 8", (row["platform"], row["listing_id"]))
    if not rows:
        return {"status": "NONE", "n": 0, "latest": None, "items": [],
                "note": ("No decision recorded for this listing yet. Recording one attaches these "
                         "asking-price figures and your cost assumptions to the item; it does not "
                         "record a purchase or confirm that anything sold.")}
    counts = {}
    for entry in db.query("SELECT decision, COUNT(*) AS n FROM research_decisions "
                          "WHERE platform=? AND source_listing_id=? GROUP BY decision",
                          (row["platform"], row["listing_id"])):
        counts[entry["decision"]] = entry["n"]
    latest = rows[0]
    disagreeing = sum(count for decision, count in counts.items() if decision != latest["decision"])
    return {
        "status": "RECORDED", "n": sum(counts.values()), "returned": len(rows),
        "counts": counts, "latest": dict(latest),
        "disagreeing_with_latest": disagreeing,
        "items": [dict(entry) for entry in rows],
        "note": ("Reassessment creates a new entry and keeps the earlier ones, so more than one row "
                 "here is a normal history rather than a duplicate. Comparing against your own past "
                 "reading is the only check this system has for whether your assumptions were right.")
                 + (f" {disagreeing} earlier entr{'y' if disagreeing == 1 else 'ies'} recorded a "
                    f"different decision." if disagreeing else ""),
    }


def _cohort(row, peers, key, stats, identity_basis, horizon, incidence_peers=None):
    """The cohort behind one item's percentage: its shape, its mix and its outcomes.

    The subject is removed from the price statistics and the breakdowns, and the
    incidence cohort keeps it -- a live listing contributes a censored observation
    and dropping it would quietly change the denominator. Which of the two happened
    is stated rather than assumed.
    """
    others = [peer for peer in peers if peer["listing_pk"] != row["listing_pk"]]
    price = analysis.amount(row["listed_price_eur"])
    rank = None
    if price is not None and others:
        cheaper = sum(1 for peer in others if float(peer["listed_price_eur"]) < price)
        rank = cheaper + 1
    incidence_peers = peers if incidence_peers is None else incidence_peers
    incidence = metrics.liquidity_rows(incidence_peers, horizon, filters=analysis.cohort_key(key) if key else {})
    rates = {evidence: report.get("estimates", {}).get(f"{horizon}d")
             for evidence, report in incidence["sell_through_by_evidence"].items()}
    return {
        "key": analysis.cohort_key(key) if key else None,
        "matched_fields": list(analysis.COHORT_FIELDS) if key else [],
        "identity_basis": identity_basis,
        "identity_basis_label": analysis.IDENTITY_BASES[identity_basis][0] if key else "Comparison unavailable",
        "identity_basis_detail": (analysis.IDENTITY_BASES[identity_basis][1] if key else
                                  "No supported peer cohort is established for this listing."),
        "identity": None,
        "n_cohort": len(peers),
        "n_incidence_cohort": len(incidence_peers),
        "n_peers": len(others),
        "subject_excluded_from_peers": True,
        "subject_excluded_from_incidence": False,
        "price_sample": stats,
        "price_rank_cheapest_first": rank,
        "histogram": _histogram(others) if stats else None,
        "conditions": _counts(others, "condition_norm"),
        "municipalities": _counts(others, "municipality_norm"),
        "seller_types": _counts(others, "seller_type_norm"),
        "sale_markers": {
            "horizon_days": horizon,
            "sold_declared": incidence["sold_inferred_seller_declared"],
            "sold_verified": incidence["sold_confirmed"],
            "estimates": rates,
            "status": ("OK" if any(value is not None for value in rates.values())
                       else incidence["status"]),
            "horizon_support": incidence["horizon_support"].get(f"{horizon}d"),
            "followup_coverage": incidence["followup_coverage"],
        },
        "basis": ("No supported peer cohort is established for this listing." if key is None else
                  "Asking prices of the cohort with this listing removed. A sale marker is a seller "
                  "declaring an outcome, not an observed clearing price, and the incidence cohort "
                  "includes this listing as one censored observation, plus matching terminal listings. "
                  "Terminal listings are excluded from the current asking-price benchmark."),
    }


def _counts(rows, field):
    tally = {}
    for row in rows:
        label = row.get(field) or "unrecorded"
        tally[label] = tally.get(label, 0) + 1
    return sorted(({"label": label, "n": count} for label, count in tally.items()),
                  key=lambda item: (-item["n"], item["label"]))


def _histogram(peers, bins=HISTOGRAM_BINS):
    """The cohort's price shape, or an explicit refusal to draw one.

    Bins over a handful of points are decoration, so below `MIN_HISTOGRAM_PEERS`
    this returns None and the caller shows the numbers instead. A histogram with
    two bins looks like a distribution and is not one.
    """
    prices = sorted(float(peer["listed_price_eur"]) for peer in peers if peer["listed_price_eur"] is not None)
    if len(prices) < MIN_HISTOGRAM_PEERS:
        return None
    low, high = prices[0], prices[-1]
    if high <= low:
        return {"bins": [low], "counts": [len(prices)], "n": len(prices),
                "note": "Every peer asks the same price; there is no shape to show."}
    width = (high - low) / bins
    counts = [0] * bins
    for price in prices:
        # The top edge is inclusive so the maximum lands in the last bin rather
        # than falling off the end of a half-open range.
        index = min(bins - 1, int((price - low) / width))
        counts[index] += 1
    return {"bins": [round(low + step * width, 2) for step in range(bins + 1)],
            "counts": counts, "n": len(prices),
            "note": "Bin counts of the cohort's asking prices with this listing removed."}


def inspection_checklist(row, key, identity_basis, stats, identity=None):
    """What a person must physically verify before this number means anything.

    The list is derived from what the stored record does and does not contain, so
    it names the specific unknowns on this item rather than a generic reminder.
    A checklist that always says the same four things gets skimmed; one that says
    "brand and model are both unrecorded, so this compares a product type" is a
    different act of reading.
    """
    canonical = bool(key and key[7])
    missing_identity = [name for name, value in (("brand", row.get("brand_norm")),
                                                 ("model", row.get("model_norm"))) if not value]
    if key is None:
        recognized = " ".join(filter(None, ((identity or {}).get("brand"), (identity or {}).get("model"))))
        missing = list(dict.fromkeys((identity or {}).get("missing_variant_fields", []) +
                                    (identity or {}).get("missing_comparison_fields", [])))
        if not recognized:
            identity_item = "Confirm the exact product being sold. "
        elif (identity or {}).get("review"):
            identity_item = f"Source review records {recognized}. "
        else:
            identity_item = f"Unreviewed model recognition: {recognized}. Confirm the actual item offered. "
        identity_item += ("No supported peer comparison is established. " +
                          ("Review: " + ", ".join(field.replace("_", " ") for field in missing) + "."
                           if missing else "Verify the brand and model, exact variant and single-unit offer against the source and physical unit."))
    elif canonical:
        identity_item = ("Confirm the reviewed variant against the physical unit: capacity, colour, "
                         "and that the offer really is a single unit.")
    elif identity_basis == "brand_and_model":
        identity_item = "Confirm the recorded brand and model are the ones physically present."
    elif missing_identity:
        identity_item = (f"{' and '.join(missing_identity).capitalize()} recorded for neither this item "
                         f"nor its peers, so this compares a product type rather than a product. "
                         f"Establish the model before treating the gap as comparable.")
    else:
        identity_item = f"Peers matched on {analysis.IDENTITY_BASES[identity_basis][0]}; confirm that is enough."

    condition = (identity or {}).get("attributes", {}).get("condition") or row.get("condition_norm")
    condition_item = (("Condition is unrecorded. Review the source and inspect physical condition before comparing this offer."
                      if key is None else "Condition is unrecorded on this listing; the cohort requires a known condition, "
                      "so verify it physically and expect the comparison to be provisional.")
                     if not condition or condition == "unknown"
                     else f"Condition is recorded as '{condition}', which is the seller's own word. "
                          f"Check it against the actual unit.")

    completeness = "Check the included contents against the seller's description and any reviewed offer facts."
    delivery = ("Price delivery before deciding. Buyer shipping and buyer fee are observed fields on this "
                "listing and are excluded from every peer comparison shown here.")
    price_moves = ("Check whether the ask is moving. A price reduced since first observation is a different "
                   "research finding from one that has sat unchanged.")
    return {
        "items": [
            {"key": "identity", "label": "Model and variant", "detail": identity_item,
             "required": True, "source": "derived from what the stored record contains"},
            {"key": "condition", "label": "Physical condition", "detail": condition_item,
             "required": True, "source": "derived from the recorded condition"},
            {"key": "completeness", "label": "Completeness", "detail": completeness,
             "required": True, "source": "always required; never measured remotely"},
            {"key": "delivery", "label": "Delivery cost", "detail": delivery,
             "required": True, "source": "observed buyer fields, excluded from the comparison"},
            {"key": "price_history", "label": "Ask history", "detail": price_moves,
             "required": False, "source": "available from the provenance panel"},
        ],
        "peer_price_facts_required": ["exact variant", "functionality", "included accessories"],
        "basis": ("A gap between asking prices is a reason to look at an item. It is not evidence of "
                  "profit, and it says nothing about what the item will actually sell for."),
        "price_context": stats,
    }


def owned_comparables(row):
    """Our own completed trades in the same category, if any exist.

    The only realized prices in this system are prices we recorded ourselves, so
    this is the one place a buyer can look for what something actually cleared
    for. It is deliberately not a benchmark: a handful of trades is context, and
    the margin is shown per trade with its own completeness rather than summarised
    into a rate.
    """
    category = row.get("category_norm")
    if not category:
        return {"status": "INSUFFICIENT DATA", "trades": [],
            "note": "This listing has no detailed category, so owned trades cannot be matched to it."}
    matches = [trade for trade in purchases.ledger_rows() if trade.get("category_norm") == category]
    trades = []
    for trade in matches[:8]:
        margin, reason = purchases._margin(dict(trade))
        trades.append({
            "purchase_pk": trade.get("purchase_pk"),
            "title": trade.get("title_raw"),
            "acquisition_cost_eur": trade.get("acquisition_cost_eur"),
            "final_sale_price_eur": trade.get("final_sale_price_eur"),
            "sale_at": trade.get("sale_at"),
            "outcome_status": trade.get("outcome_status"),
            "gross_margin_eur": margin["gross_margin_eur"] if margin else None,
            "gross_margin_pct": margin["gross_margin_pct"] if margin else None,
            "margin_known": bool(margin),
            "margin_missing_reason": None if margin else reason,
            "days_held": _days_held(trade),
        })
    return {
        "status": "OK" if trades else "INSUFFICIENT DATA",
        "category_norm": category,
        "n_matching_trades": len(matches),
        "trades": trades,
        "min_n": config.MIN_N_FOR_REPORT,
        "note": ("Recorded purchases and sales in this exact category, across all your trades and "
                 "independent of marketplace filters. A small number of trades is context, not a "
                 "benchmark, and an unknown cost is shown as unknown rather than as zero."
                 if trades else
                 # With no trades at all, a note about "a small number" would imply
                 # some exist. This is the only place a realized price can come
                 # from, so its absence is stated rather than filled with a peer
                 # median that is not one.
                 f"You have recorded no purchases or sales in '{category}'. These are the only "
                 f"realized prices in this system, so there is no comparable evidence for this "
                 f"category and none is substituted."),
    }


def _days_held(trade):
    bought = metrics.parse_ts(trade.get("purchased_at"))
    sold = metrics.parse_ts(trade.get("sale_at"))
    if not bought or not sold or bought > sold:
        return None
    return round((sold - bought).total_seconds() / 86400, 1)


def expected_proceeds(listing_pk, min_n=None):
    """What comparable owned resales imply this segment might clear for.

    This is the only path from a peer asking price to an expected resale price,
    and it runs through verified owned resale pairings rather than a conversion
    factor. It stays `INSUFFICIENT DATA` until enough paired trades exist, which
    on a new market is most segments for a long time -- and that is the honest
    answer rather than a number derived from asks.
    """
    listing_pk = _bounded_int(listing_pk, "listing key", 1, 2**62, None)
    if listing_pk is None:
        raise ValueError("a listing key is required")
    with db.read_snapshot():
        row = db.query_one("SELECT " + analysis.FIELDS + " FROM listings WHERE listing_pk=?", (listing_pk,))
        if not row:
            raise ValueError("choose a listing already stored in this workspace")
        types = analysis.accepted_product_types([row])
        product_type = types.get(listing_pk)
        identity = product_identity.resolve_rows([row], product_types=types)[listing_pk]
        if identity["pilot_scope"]:
            return {"status": "INSUFFICIENT DATA", "estimate": None,
                    "listing_pk": listing_pk, "product_type": identity["product_type"],
                    "blocking": "CANONICAL RESALE EVIDENCE",
                    "canonical_comparison_key": identity["comparison_key"],
                    "note": ("Historical verified resale pairings for this canonical variant and offer "
                             "are required. The legacy category/model realization ratio does not establish "
                             "variant-matched resale proceeds; current reviews cannot supply that history.")}
        estimate = realization.realized_price_estimate(
            category_norm=row.get("category_norm"), condition_norm=row.get("condition_norm"),
            product_type=product_type, platform=row["platform"], subplatform=row["subplatform"],
            brand_norm=row.get("brand_norm"), model_norm=row.get("model_norm"),
            exclude_listing_pk=listing_pk, min_n=min_n)
        estimate["listing_pk"] = listing_pk
        estimate["product_type"] = product_type
        return estimate


# --------------------------------------------------------------------------
# saved sessions
# --------------------------------------------------------------------------
#
# A session stores what the operator was looking at and what they concluded, so a
# research pass survives a browser being closed. It deliberately stores no prices
# and no evidence snapshot: a saved gap would be a stale number that reads as
# current, which is the specific failure this system is built to avoid. The rows
# are re-read live, and a listing that retention has since removed is reported as
# gone rather than shown with a price from when it was saved.

def sessions(name=None):
    """List saved sessions, newest first, with a summary rather than the whole body."""
    name = _text(name, "session name", 60) if name else None
    params, where = ([name], " WHERE name=?") if name else ([], "")
    rows = db.query("SELECT session_pk, name, filters_json, selected_pks_json, notes, scenarios_json, "
                    "created_at, updated_at FROM research_sessions" + where
                    + " ORDER BY updated_at DESC, session_pk DESC", params)
    items = []
    for row in rows:
        selected = _load(row["selected_pks_json"], [])
        items.append({
            "session_pk": row["session_pk"], "name": row["name"],
            "filters": json.loads(row["filters_json"]), "selected_pks": selected,
            "n_selected": len(selected), "notes": row["notes"],
            "scenarios": _load(row["scenarios_json"], {}),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })
    return {"sessions": items, "limit": SESSION_LIMIT,
            "note": ("A session stores the filters, the chosen listings and your own notes. It stores no "
                     "prices: reopening it re-reads the live evidence, because a saved gap would be a "
                     "number from a moment that has passed.")}


def save_session(name, filters, selected_pks=None, notes="", scenarios=None):
    """Create or replace one named session, enforcing the same bounds as segments."""
    name = _text(name, "session name", 60, required=True)
    from . import research
    filters = research.clean_filters(filters)
    notes = _text(notes, "session notes", 4000)
    selected = _selected(selected_pks)
    assumptions = _scenarios(scenarios, set(selected))
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    try:
        with conn.transaction():
            retained = conn.execute("SELECT scenarios_json FROM research_sessions WHERE name=?", (name,)).fetchone()
            if retained and any(isinstance(value, dict) and value.get('version') == 'scenario-workspace-v1'
                                for value in _load(retained[0], {}).values()):
                raise ValueError('This reusable scenario is an immutable copy. Save another session name for edits.')
            if (conn.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0] >= SESSION_LIMIT
                    and not conn.execute("SELECT 1 FROM research_sessions WHERE name=?", (name,)).fetchone()):
                raise ValueError(f"at most {SESSION_LIMIT} saved sessions")
            conn.execute(
                "INSERT INTO research_sessions (name, filters_json, selected_pks_json, notes, "
                "scenarios_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT (name) DO UPDATE SET filters_json=excluded.filters_json, "
                "selected_pks_json=excluded.selected_pks_json, notes=excluded.notes, "
                "scenarios_json=excluded.scenarios_json, updated_at=excluded.updated_at",
                (name, _json(filters), _json(selected), notes, _json(assumptions), now, now))
    finally:
        conn.close()
    return {"ok": True, **sessions()}


def delete_session(session_pk):
    session_pk = _bounded_int(session_pk, "session ID", 1, 2**62, None)
    if session_pk is None:
        raise ValueError("a session ID is required")
    db.execute("DELETE FROM research_sessions WHERE session_pk=?", (session_pk,))
    return {"ok": True, **sessions()}


def _selected(values):
    """Validate the chosen listing keys, preserving order and dropping duplicates.

    Existence is checked here, at save time, because a key that names nothing
    could only ever be reported as gone: the mistake would be invisible until the
    session was reopened and appeared to have lost every item in it. Retention may
    still remove a listing later, and that is reported honestly when it happens.
    """
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > SESSION_SELECTION_LIMIT:
        raise ValueError(f"select at most {SESSION_SELECTION_LIMIT} listings")
    seen, out = set(), []
    for value in values:
        key = _bounded_int(value, "listing key", 1, 2**62, None)
        if key is None:
            raise ValueError("listing keys must be integers")
        if key in seen:
            continue
        if not db.scalar("SELECT 1 FROM listings WHERE listing_pk=?", (key,)):
            raise ValueError(f"listing {key} is not stored in this workspace")
        seen.add(key)
        out.append(key)
    return out


def _scenarios(values, allowed):
    """Freeze operator cost assumptions per listing, without evaluating them here.

    Evaluation happens in `research.scenario` on request, so a stored assumption
    set is never a stored margin. A scenario for a listing that is not part of the
    session is refused rather than quietly kept, because it would reappear later
    attached to a selection the operator cannot see.
    """
    if values is None:
        return {}
    if not isinstance(values, dict) or len(values) > SESSION_SELECTION_LIMIT:
        raise ValueError("cost assumptions must be keyed by listing")
    out = {}
    for key, assumptions in values.items():
        listing_pk = _bounded_int(key, "listing key", 1, 2**62, None)
        if listing_pk is None or listing_pk not in allowed:
            raise ValueError("cost assumptions must belong to a selected listing")
        if assumptions is not None and not isinstance(assumptions, dict):
            raise ValueError("cost assumptions must be an object")
        out[str(listing_pk)] = assumptions
    return out


def _load(text, default):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def session_status(name=None, min_gap_pct=20, limit=DEFAULT_LIMIT, horizon=30):
    """Saved sessions with the live evidence for their choices, read together.

    A saved session whose listings have all aged out is reported as empty rather
    than as a list of prices that no longer exist, and one whose cohort no longer
    clears the reporting threshold says so instead of reporting a median from too
    few peers.
    """
    min_gap_pct = _bounded_int(min_gap_pct, "minimum gap", 1, 99, 20)
    horizon = _horizon(horizon)
    with db.read_snapshot():
        saved = sessions(name)
        chosen = [row for row in saved["sessions"] if row["n_selected"]]
        reference, cohort_cache = datetime.now(timezone.utc), {}
        for session in chosen:
            session["status"] = _session_status(session, min_gap_pct, horizon, reference, cohort_cache)
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "min_gap_pct": min_gap_pct, "horizon_days": horizon,
            "sessions": saved["sessions"], "limit": SESSION_LIMIT,
            "n_with_choices": len(chosen),
            "note": saved["note"] + (" Each session's chosen listings are re-read from live stored "
                                     "evidence; nothing here is restored from a stored price.")}


def _session_status(session, min_gap_pct, horizon, reference=None, cohort_cache=None):
    live, gone = [], 0
    for listing_pk in session["selected_pks"]:
        try:
            found = _candidate(listing_pk, DEFAULT_PEER_ROWS, horizon, min_gap_pct, filters=session["filters"],
                               reference=reference, cohort_cache=cohort_cache, summary_only=True)
        except ValueError:
            gone += 1
            continue
        live.append({"listing_pk": listing_pk, "title": found["listing"]["title"],
                     "ask_eur": found["listing"]["ask_eur"],
                     "price_observed_at": found["listing"]["price_observed_at"],
                     "verdict": found["verdict"],
                     "gap_pct": (found["candidate"] or {}).get("gap_pct"),
                     "candidate": found["candidate"]})
    return {"n_selected": len(session["selected_pks"]), "n_live": len(live), "n_gone": gone,
            "n_qualifying": sum(bool(entry["candidate"]) for entry in live),
            "listings": live,
            "basis": ("Re-read from stored observations now. Retention-removed listings are counted "
                      "as gone; stored listings that no longer qualify keep their current verdict. "
                      "No price is restored from when the session was saved.")}
