"""Statistics that refuse to overstate what the data supports.

Three rules enforced throughout:

1. Every estimate is reported with n and, for prices, coverage.
2. Anything below MIN_N_FOR_REPORT returns "insufficient data" rather than a
   number. A precise-looking median from six observations is worse than no
   median, because it gets believed.
3. Durations are treated as censored survival, not as completed sales. A
   listing that vanished is not a listing that sold.
"""

import json
import math
import random
import statistics
from functools import lru_cache
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from . import config, db

MIN_N = config.MIN_N_FOR_REPORT
BOOTSTRAP_REPS = 2000
LIQUIDITY_FIELDS = ("listing_pk, first_seen_at, last_seen_at, observation_coverage_end, "
                    "last_live_at, last_probed_at, outcome_status, coverage, is_reserved")


def percentiles(values, ps=(0.25, 0.50, 0.75)):
    ordered = sorted(values)
    if not ordered:
        return {}
    out = {}
    for p in ps:
        idx = p * (len(ordered) - 1)
        lo = math.floor(idx)
        hi = math.ceil(idx)
        out[p] = ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)
    return out


def bootstrap_median_ci(values, reps=BOOTSTRAP_REPS, alpha=0.05, seed=17):
    """Percentile bootstrap CI on the median.

    Draw the middle uniform order statistics and map them through the empirical
    quantile function. This has the same distribution as taking the median of
    n draws with replacement, including ties and both middle values for even n.
    It avoids allocating and sorting n prices for every replicate.
    """
    if len(values) < 3:
        return None
    return dict(_bootstrap_median_ci(tuple(float(v) for v in values), reps, alpha, seed))


@lru_cache(maxsize=128)
def _bootstrap_median_ci(values, reps, alpha, seed):
    """Reuse an interval until the selected price sample actually changes."""
    rng = random.Random(seed)
    n = len(values)
    ordered = sorted(values)
    meds = [_bootstrap_median(ordered, rng) for _ in range(reps)]
    meds.sort()
    lo = meds[int((alpha / 2) * reps)]
    hi = meds[int((1 - alpha / 2) * reps) - 1]
    return {"lo": lo, "hi": hi, "reps": reps}


def _bootstrap_median(ordered, rng):
    """Sample an empirical-bootstrap median via uniform order statistics.

    U_(k) ~ Beta(k, n+1-k). Given U_(k)=u, the next order statistic
    is u+(1-u)*Beta(1,n-k). See the uniform and conditional order-statistic
    results in https://www.cs.cmu.edu/afs/cs/academic/class/15750-s16/Handouts/orderstats.pdf.
    Mapping floor(n*U) into sorted prices preserves repeated price masses.
    """
    n = len(ordered)
    k = (n+1)//2
    lower = rng.betavariate(k, n+1-k)
    left = ordered[min(n-1, int(n*lower))]
    if n % 2:
        return left
    upper = lower+(1-lower)*rng.betavariate(1, n-k)
    right = ordered[min(n-1, int(n*upper))]
    return float((Decimal(str(left)) + Decimal(str(right))) / 2)


def suppress(n, min_n=MIN_N):
    return n < min_n


def _days(a, b):
    return (b - a).total_seconds() / 86400.0


@lru_cache(maxsize=32768)
def _parse_ts_cached(text):
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_ts(value):
    # Memoised because it is the hottest leaf in the whole process: one dashboard
    # snapshot calls it 677k times over the same few distinct timestamp strings,
    # and `datetime.fromisoformat` dominated the profile. A snapshot re-reads each
    # `last_seen_at`, `last_probed_at` and `first_seen_at` repeatedly while
    # grouping and summarising the same cohort. `datetime` is immutable and the
    # cache is bounded, so this cannot serve a stale or shared-mutable result.
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str):
            return None
        return _parse_ts_cached(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def listing_filters(category_norm=None, region_norm=None, municipality_norm=None,
                    platform="tori", subplatform="standard", category_root_norm=None,
                    condition_norm=None, seller_type_norm=None, focus_path=None):
    """One slice definition shared by statistics, reports, and the dashboard.

    `focus_path` narrows the slice to one configured vertical. It is resolved
    through the same `collection.Scope` the collector probes with, so a vertical
    filter cannot select a different cohort than the queue that fills it. An
    unconfigured path matches nothing rather than everything: a filter that
    silently fell back to the whole market would show numbers from segments the
    operator did not ask for, and every one of them would look measured.
    """
    conditions, params = [], []
    filters = locals().copy()
    filters.pop("conditions")
    filters.pop("params")
    for field, value in filters.items():
        if field != "focus_path" and value is not None and value != "":
            conditions.append(f"{field} = ?")
            params.append(value)
    if focus_path:
        from . import collection
        if focus_path in tuple(config.FOCUS_CATEGORIES):
            scoped, values = collection.Scope(categories=(focus_path,)).conditions()
            conditions.extend(scoped)
            params.extend(values)
        else:
            conditions.append("1=0")
    else:
        filters["focus_path"] = None
    return conditions, params, filters


def ask_price_panel(category_norm=None, region_norm=None, municipality_norm=None,
                    min_n=None, platform="tori", subplatform="standard",
                    category_root_norm=None, condition_norm=None, seller_type_norm=None,
                    max_age_hours=None, focus_path=None):
    """Fresh, available asking prices with completeness of the selected slice."""
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    max_age_hours = config.PANEL_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    where, params, filters = listing_filters(category_norm, region_norm, municipality_norm,
                                            platform, subplatform, category_root_norm,
                                            condition_norm, seller_type_norm, focus_path)
    where.extend(["status_norm='active'", "coverage='collecting'",
                  "COALESCE(is_giveaway, FALSE)=FALSE", "COALESCE(is_reserved, FALSE)=FALSE"])
    rows = db.query("SELECT listed_price_eur, last_seen_at, price_observed_at FROM listings WHERE " +
                    " AND ".join(where), params)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    fresh = [r for r in rows if parse_ts(r["last_seen_at"]) and parse_ts(r["last_seen_at"]) >= cutoff]
    prices = [float(r["listed_price_eur"]) for r in fresh
              if r["listed_price_eur"] is not None and math.isfinite(float(r["listed_price_eur"]))
              and r["listed_price_eur"] > 0
              and (parse_ts(r["price_observed_at"]) or parse_ts(r["last_seen_at"])) >= cutoff]
    total = len(fresh)
    report = {"filters": filters, "n_with_price": len(prices), "n_listings_total": total,
              "coverage_with_price": len(prices) / total if total else None,
              "n_stale_excluded": len(rows) - total, "max_age_hours": max_age_hours,
              "newest_observed_at": max((r["last_seen_at"] for r in fresh), default=None),
              "min_n_threshold": min_n,
              "sampling_note": "Observed search-page sample; completeness is not market coverage."}
    if suppress(len(prices), min_n):
        report.update(status="INSUFFICIENT DATA",
                      note=f"n={len(prices)} below threshold {min_n}. No estimate reported.")
        return report
    pcts = percentiles(prices)
    ci = bootstrap_median_ci(prices)
    report.update(status="OK", median_eur=round(pcts[0.50], 2),
                  p25_eur=round(pcts[0.25], 2), p75_eur=round(pcts[0.75], 2),
                  min_eur=round(min(prices), 2), max_eur=round(max(prices), 2),
                  median_ci95={k: round(ci[k], 2) for k in ("lo", "hi")} if ci else None,
                  interpretation="ASK prices of fresh, available items in this segment. Not clearing prices.")
    return report


def km_survival(durations, events, horizons):
    """Kaplan-Meier survival estimate. Right-censored observations retained.

    The risk set shrinks by both events and censorings, which is what makes the
    estimate correct when listings are lost from observation before they sell.
    """
    pairs = sorted(zip(durations, events))
    n_total = len(pairs)
    curve = []
    s = 1.0
    removed = 0
    i = 0
    while i < len(pairs):
        t = pairs[i][0]
        d = c = 0
        while i < len(pairs) and pairs[i][0] == t:
            if pairs[i][1]:
                d += 1
            else:
                c += 1
            i += 1
        n_at_risk = n_total - removed
        if n_at_risk > 0 and d > 0:
            s *= 1 - d / n_at_risk
        curve.append((t, s, n_at_risk))
        removed += d + c

    results = {}
    for h in horizons:
        surv = 1.0
        for t, sv, _ in curve:
            if t <= h:
                surv = sv
        results[h] = surv
    return curve, results


def cumulative_incidence(durations, outcomes, horizons, evidence):
    """Aalen-Johansen incidence with other terminal outcomes as competing events.

    Unknown outcomes are censored. Unlike one-minus-KM, withdrawals and other
    sales cannot be removed as if they might still have the event of interest.
    """
    pairs = sorted(zip(durations, outcomes))
    at_risk, survival, incidence = len(pairs), 1.0, 0.0
    curve = []
    i = 0
    while i < len(pairs):
        t = pairs[i][0]
        group = []
        while i < len(pairs) and pairs[i][0] == t:
            group.append(pairs[i][1])
            i += 1
        terminal = sum(outcome in ("sold_confirmed", "sold_inferred", "withdrawn", "expired")
                       for outcome in group)
        events = group.count(evidence)
        if at_risk:
            incidence += survival * events / at_risk
            survival *= 1 - terminal / at_risk
        curve.append((t, incidence))
        at_risk -= len(group)
    return {h: next((value for t, value in reversed(curve) if t <= h), 0.0)
            for h in horizons}


def liquidity(horizon_days=30, category_norm=None, region_norm=None,
              municipality_norm=None, min_n=None, platform="tori", subplatform="standard",
              category_root_norm=None, condition_norm=None, seller_type_norm=None,
              focus_path=None):
    """Observed terminal incidence, separately reported for each evidence class."""
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    conditions, params, filters = listing_filters(category_norm, region_norm, municipality_norm,
                                                  platform, subplatform, category_root_norm,
                                                  condition_norm, seller_type_norm, focus_path)
    conditions.append("first_seen_at IS NOT NULL")
    # Projected, not `SELECT *`. `liquidity_rows` reads nine of the 62 columns
    # on `listings`; selecting the whole table materialised all 62 for every row
    # of a 28k-listing cohort, and row-to-dict conversion was a quarter of the
    # dashboard snapshot's cost. Add a column here only if `liquidity_rows`
    # reads it.
    listings = db.query("SELECT " + LIQUIDITY_FIELDS + " FROM listings WHERE " + " AND ".join(conditions), params)
    return liquidity_rows(listings, horizon_days, min_n, filters)


def price_dynamics(listing_pks, min_n=None):
    """Price-cut and relist evidence for a cohort, from append-only events.

    Every figure carries n. A cut is only counted when both the previous and the
    new ask are known finite numbers, so a parse failure never becomes a price
    movement. Percentages are computed per listing and summarised, because a
    single EUR 50 cut means different things on a EUR 60 item and a EUR 2,000 one.
    """
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    pks = sorted({pk for pk in listing_pks if pk is not None})
    report = {"n_listings": len(pks), "min_n_threshold": min_n,
              "status": "INSUFFICIENT DATA"}
    if not pks:
        report.update(status="NO DATA", note="No listings in this cohort.")
        return report
    cuts_per_listing, cuts_total, invalid = {}, 0, 0
    drops = []
    for offset in range(0, len(pks), 400):
        chunk = pks[offset:offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = db.query(
            "SELECT listing_pk, before_value, after_value FROM events "
            f"WHERE event_type='price_changed' AND listing_pk IN ({placeholders})",
            chunk)
        for row in rows:
            try:
                before, after = float(row["before_value"]), float(row["after_value"])
            except (TypeError, ValueError):
                invalid += 1
                continue
            if not (math.isfinite(before) and math.isfinite(after)) or before <= 0 or after <= 0:
                invalid += 1
                continue
            cuts_total += 1
            cuts_per_listing[row["listing_pk"]] = cuts_per_listing.get(row["listing_pk"], 0) + 1
            if after < before:
                drops.append((before - after) / before)
    relisted = set()
    for offset in range(0, len(pks), 400):
        chunk = pks[offset:offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        for row in db.query(
                "SELECT DISTINCT listing_pk FROM events "
                f"WHERE event_type='relisted' AND listing_pk IN ({placeholders})", chunk):
            relisted.add(row["listing_pk"])
        # relist_lineage_id is the durable marker, but nothing writes it yet;
        # count both so a future writer is picked up without a code change here.
        for row in db.query(
                "SELECT listing_pk FROM listings "
                f"WHERE listing_pk IN ({placeholders}) AND relist_lineage_id IS NOT NULL", chunk):
            relisted.add(row["listing_pk"])
    n_relisted = len(relisted)
    n_with_cuts = len(cuts_per_listing)
    report.update(
        n_price_change_events=cuts_total, n_listings_price_changed=n_with_cuts,
        n_invalid_price_events=invalid, n_relisted=n_relisted,
        relist_share=round(n_relisted / len(pks), 4),
        price_change_share=round(n_with_cuts / len(pks), 4),
        median_drops=round(statistics.median(drops), 4) if drops else None,
        n_price_drops=len(drops),
        mean_drop_share=round(statistics.fmean(drops), 4) if drops else None,
        max_drop_share=round(max(drops), 4) if drops else None,
    )
    if cuts_total < min_n:
        report["status"] = "INSUFFICIENT DATA"
        report["note"] = (f"{cuts_total} valid price-change events below threshold {min_n}; "
                          "shares and counts are reported, summaries are not.")
        report.pop("median_drops", None)
        report.pop("mean_drop_share", None)
        report.pop("max_drop_share", None)
        return report
    report["status"] = "OK"
    report["basis"] = ("Observed price-change events for listings in scope. A price cut is not a "
                       "sale; cuts are only observable while a listing remains under observation.")
    return report


def liquidity_rows(listings, horizon_days=30, min_n=None, filters=None):
    """The same evidence rules for an already selected cohort of observations."""
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    durations, outcomes, intervals = [], [], []
    counts = {key: 0 for key in ("sold_confirmed", "sold_inferred", "withdrawn", "expired")}
    gone = observable = reserved = 0
    for row in listings:
        start = parse_ts(row["first_seen_at"])
        end = parse_ts(row["observation_coverage_end"]) or parse_ts(row["last_seen_at"])
        if not start or not end or end < start:
            continue
        outcome = row["outcome_status"] or "unknown"
        durations.append(_days(start, end))
        outcomes.append(outcome)
        if outcome in counts:
            counts[outcome] += 1
            last_live = parse_ts(row.get("last_live_at"))
            if last_live and last_live <= end:
                intervals.append(_days(last_live, end))
        elif row["coverage"] == "gone_uncertain":
            gone += 1
        else:
            observable += 1
            reserved += bool(row.get("is_reserved"))
    horizons = sorted(set([7, 14, 30, 60, 90, float(horizon_days)]))
    if horizons[0] <= 0 or not all(math.isfinite(h) for h in horizons):
        raise ValueError("horizons must be finite and positive")
    support = {h: sum(duration >= h for duration in durations) for h in horizons}
    supported = [h for h in horizons if support[h] >= min_n]
    def label(h):
        return f"{h:g}d"
    report = {"horizon_days_requested": horizon_days, "filters": filters,
              "listings_tracked": len(durations), "invalid_timing_excluded": len(listings)-len(durations),
              "resolved_terminal": sum(counts.values()),
              "sold_confirmed": counts["sold_confirmed"],
              "sold_inferred_seller_declared": counts["sold_inferred"],
              "withdrawn": counts["withdrawn"], "expired": counts["expired"],
              "expired_or_404": counts["expired"],  # compatibility alias, new 404s remain unknown
              "gone_uncertain": gone, "still_observable": observable, "reserved": reserved,
              "min_n_threshold": min_n,
              "observation_span_days": round(max(durations), 2) if durations else 0.0,
              "horizon_support": {label(h): support[h] for h in horizons},
              "terminal_interval_median_days": statistics.median(intervals) if intervals else None,
              "followup_coverage": sum(bool(row.get("last_probed_at")) for row in listings)/len(listings) if listings else None,
              "duration_basis": "Days since first observed to detected marker or explicit exit. Marker transitions occur between checks; sellers can mark sold late, so actual transaction time and age before discovery are unknown.",
              "realized_price_note": "The price a sold listing cleared for is not published by the platform.",
              "sell_through_basis": "Cumulative incidence of observed sale markers with withdrawals and other evidence classes as competing events; unknown outcomes are censored. The sample and missing outcomes can bias estimates.",
              "selection_note": "Automatic detail checks use a bounded stratified cohort; manual priority entries and pooled admission batches do not represent the whole market. Unadmitted listings retain discovery observations but have different outcome-detection coverage." if config.FOLLOWUP_ENABLED else
                                "Detail checks follow the legacy focused queue; missing outcomes can bias estimates."}
    evidence_reports = {}
    for evidence in ("sold_confirmed", "sold_inferred"):
        evidence_report = {"n_events": counts[evidence], "min_n_threshold": min_n,
                           "status": "INSUFFICIENT DATA"}
        if counts[evidence] >= min_n and supported:
            values = cumulative_incidence(durations, outcomes, supported, evidence)
            # Cohort bootstrap includes censorings and competing events together.
            samples = {h: [] for h in supported}
            rng = random.Random(17)
            pairs = list(zip(durations, outcomes))
            for _ in range(400):
                replicate = rng.choices(pairs, k=len(pairs))
                ds, os = zip(*replicate)
                estimates = cumulative_incidence(ds, os, supported, evidence)
                for h in supported:
                    samples[h].append(estimates[h])
            evidence_report.update(status="OK", estimates={label(h): {
                "share": round(values[h], 4), "n_followed_to_horizon": support[h],
                "ci95": {"lo": round(sorted(samples[h])[10], 4),
                         "hi": round(sorted(samples[h])[389], 4)}} for h in supported})
        evidence_reports[evidence] = evidence_report
    report["sell_through_by_evidence"] = evidence_reports
    report["price_dynamics"] = price_dynamics([row.get("listing_pk") for row in listings], min_n)
    from . import outcome_audit, relationships
    report["measurement_cohorts"] = outcome_audit.describe(listings, horizon_days, min_n)
    report["physical_unit_evidence"] = relationships.summary(listings)
    report["denominator_basis"] = "Observed listings; public physical-unit and transaction quantities remain unknown"
    if not durations:
        report["status"] = "NO DATA"
    elif not any(counts.values()):
        report.update(status="NOT ESTIMABLE", note="No resolved outcomes. Counts and follow-up coverage are available; no curve is reported.")
    elif any(r["status"] == "OK" for r in evidence_reports.values()):
        report["status"] = "OK"
    else:
        report.update(status="INSUFFICIENT DATA", note="Each evidence class needs enough events and enough follow-up at the horizon. No unsupported estimates are reported.")
    return report


def render_panel(report):
    if report.get("status") != "OK":
        lines = [
            f"status: {report.get('status')}",
            f"n with ask price: {report['n_with_price']} / {report['n_listings_total']} listings",
        ]
        if report.get("note"):
            lines.append(f"note: {report['note']}")
        return "\n".join(lines)
    ci = report["median_ci95"]
    ci_text = f"95% CI {ci['lo']} - {ci['hi']}" if ci else "CI unavailable"
    return (
        f"median ask: EUR {report['median_eur']}  ({ci_text})\n"
        f"P25 {report['p25_eur']}  P75 {report['p75_eur']}  range {report['min_eur']} - {report['max_eur']}\n"
        f"n = {report['n_with_price']}   coverage = {report['coverage_with_price']:.1%}\n"
        f"{report['interpretation']}"
    )


def render_liquidity(report):
    lines = [
        f"status: {report['status']}",
        f"listings tracked: {report['listings_tracked']}",
        f"resolved terminal: {report['resolved_terminal']}",
        f"  sold (seller-declared): {report['sold_inferred_seller_declared']}",
        f"  sold (verified): {report['sold_confirmed']}",
        f"  withdrawn: {report['withdrawn']}",
        f"  observed expiry: {report['expired']}",
        f"gone, outcome unknown: {report['gone_uncertain']}",
        f"still observable: {report['still_observable']}",
    ]
    if report.get("note"):
        lines.append(f"\nnote: {report['note']}")
    if report.get("selection_note"):
        lines.append(f"\nselection: {report['selection_note']}")
    for evidence, result in report.get("sell_through_by_evidence", {}).items():
        lines.append(f"\n{evidence}: {result['status']} (events={result['n_events']})")
        for horizon, estimate in result.get("estimates", {}).items():
            ci = estimate["ci95"]
            lines.append(f"  {horizon}: {estimate['share']:.1%} "
                         f"(95% CI {ci['lo']:.1%}–{ci['hi']:.1%})")
    lines.append(report.get("duration_basis", ""))
    return "\n".join(lines)


def write_reports():
    from . import analysis, collection
    from .fileio import write_text
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with db.read_snapshot():
        panel = ask_price_panel()
        liq = liquidity()
        followup = collection.coverage()
        focus = analysis.focus_breakdown()
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ask_price_panel": panel,
        "liquidity": liq,
        "collection": followup,
        "focus_verticals": focus,
    }
    path = config.REPORTS_DIR / "market_state.json"
    write_text(path, json.dumps(doc, indent=2, ensure_ascii=False))

    md = [
        "# Market state",
        "",
        f"Generated {doc['generated_at']}",
        "",
        "## Ask-price panel",
        "",
        f"- n with ask price: {panel['n_with_price']} of {panel['n_listings_total']} listings",
        f"- coverage: {panel['coverage_with_price']}",
        f"- status: **{panel['status']}**",
    ]
    if panel.get("median_eur") is not None:
        ci = panel["median_ci95"]
        ci_text = f"95% CI {ci['lo']} - {ci['hi']}" if ci else "CI unavailable"
        md.append(f"- median ask: EUR {panel['median_eur']} ({ci_text})")
        md.append(f"- P25 {panel['p25_eur']} / P75 {panel['p75_eur']}")
    if panel.get("note"):
        md.append(f"- {panel['note']}")
    md += [
        "",
        "## Liquidity",
        "",
        f"- status: **{liq['status']}**",
        f"- listings tracked: {liq['listings_tracked']}",
        f"- resolved terminal: {liq['resolved_terminal']}",
    ]
    if liq.get("note"):
        md.append("")
        md.append(f"> {liq['note']}")
    md += ["", render_liquidity(liq), ""]
    md += ["## Collection follow-up", "",
           "Configured Tori cohort across Standard and Diili; marketplace panels above use their own filters.", "",
           f"- category focus: {', '.join(followup['focus_category_names']) or 'All categories'}",
           f"- municipality focus: {', '.join(followup['focus_municipalities']) or 'All municipalities'}",
           f"- tracked: {followup['n_tracked']}",
           f"- known condition: {followup['n_known_condition']}",
           f"- current accepted classifications: {followup['n_classified']}",
           f"- never probed (unresolved): {followup['n_never_probed']}",
           f"- probes due / repeat checks due: {followup['n_due']} / {followup['n_due_repeat']}",
           f"- repeat slots reserved per run: {followup['repeat_probe_reserve']} of {followup['item_budget']}",
           f"- oldest due repeat (hours): {followup['oldest_due_repeat_hours']}",
           f"- minimum full-budget runs: {followup['probe_runs_at_budget']} at {followup['item_budget']} items/run", "",
           followup['capacity_note'], ""]
    md += [
        "",
        "## Focus verticals",
        "",
        "Each configured category measured on its own cohort. A blended figure can clear the"
        " threshold while every vertical inside it is individually too thin to support"
        " anything.",
        "",
    ]
    if not focus["panels"]:
        md.append("No focus categories configured.")
    else:
        md += [
            f"- configured: {focus['n_configured']} · with a supported asking-price median: "
            f"{focus['prices_ready']} · with a supported sale rate at {focus['horizon']}d: {focus['sale_ready']}",
            "",
            "| Vertical | Path | Tracked | Priced | Median ask | 95% CI | Declared | Verified | Checks queued | Grid cells |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for vertical in focus["panels"]:
            name = vertical["name"] if vertical["resolved_label"] else f"{vertical['path']} (unlabelled)"
            if vertical["status"] == "OK":
                ci = vertical.get("median_ci95")
                median = f"EUR {vertical['median_eur']:.0f}"
                interval = (f"{ci['lo']:.0f}-{ci['hi']:.0f}" if ci else "unavailable")
            else:
                median = f"needs {focus['min_n']}"
                interval = "-"
            grid = vertical["grid"]
            md.append(
                f"| {name} | {vertical['path']} | {vertical['n_tracked']} | {vertical['n_priced']} "
                f"| {median} | {interval} | {vertical['sold_declared']} | {vertical['sold_verified']} "
                f"| {vertical['followup']['queue']['due']} "
                f"| {grid['crawled']}/{grid['cells']} |")
        md.append("")
        md.append("Unvisited cells have never been read, so their supply is unknown rather than absent.")
    md += [
        "",
        "## Standing caveats",
        "",
        "- Ask prices are not clearing prices.",
        "- Seller-declared sales are voluntary and lag real sales. Observation and censoring can bias estimates.",
        "- Detail follow-up uses a bounded sample and manual priorities. Pooled admission cohorts do not represent the whole market.",
        "- Disappearance without a stated outcome is not sale. Relisting, withdrawal and expiry stay indistinguishable.",
        "- The price a sold listing cleared for is not published. Sell-through is measurable; realised price is not.",
        "- Every figure above carries n. Below the threshold, no figure is reported.",
    ]
    write_text(config.REPORTS_DIR / "market_state.md", "\n".join(md))
    return path
