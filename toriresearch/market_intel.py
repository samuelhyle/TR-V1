"""Stored asking-price research and explicit scenarios, with no invented returns.

Broad category distributions describe supply. Product peers use the shared
identity checks; realized prices and sale probabilities remain unknown without
supported evidence. Reading research does not collect data or cache buy signals.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import analysis, config, db, metrics, research

METHOD_VERSION = "market-research-v2"


def _amount(value):
    return analysis.amount(value)


def fetch_market_price_estimates(category_norm=None, condition_norm=None,
                                municipality_norm=None, region_norm=None,
                                force=False, focus_path=None):
    """Fresh broad ask distributions. force explicitly refreshes the local cache."""
    now = datetime.now(timezone.utc)
    cutoff = now-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    where, params, filters = metrics.listing_filters(category_norm=category_norm,
        condition_norm=condition_norm, municipality_norm=municipality_norm,
        region_norm=region_norm, focus_path=focus_path, platform="tori", subplatform="standard")
    clause = " WHERE " + " AND ".join(where) if where else ""
    groups = defaultdict(list)
    with db.read_snapshot():
        rows = db.query("SELECT " + analysis.FIELDS + ", region_norm FROM listings"+clause, params)
    for row in rows:
        if analysis.is_fresh(row, cutoff):
            groups[(row["category_norm"] or "unknown", row["condition_norm"] or "unknown",
                    row["municipality_norm"] or "", row["region_norm"] or "")].append(row)
    results = []
    for (category, condition, municipality, region), members in sorted(groups.items()):
        summary = analysis.price_panel(members, cutoff)
        if summary["status"] != "OK":
            continue
        for quartile, field in ((.25,"p25_eur"), (.5,"median_eur"), (.75,"p75_eur")):
            results.append(dict(category_norm=category, condition_norm=condition,
                municipality_norm=municipality, region_norm=region,
                price_quartile=quartile, price_eur=summary[field],
                n_listings=summary["n_available"], n_fresh=summary["n_priced"],
                price_coverage=summary["price_coverage"], median_ci95=summary["median_ci95"],
                cutoff_at=cutoff.isoformat(), computed_at=now.isoformat(),
                source="stored_observations", filters=filters,
                basis="Broad asking-price context; product comparability is not established"))
    if force:
        _cache_market_price_estimates(results)
    return results


def _cache_market_price_estimates(estimates):
    columns = ("category_norm", "condition_norm", "municipality_norm", "region_norm",
               "price_quartile", "price_eur", "n_listings", "n_fresh", "cutoff_at", "source", "computed_at")
    conn = db.connect()
    try:
        with conn.transaction():
            for row in estimates:
                conn.execute("INSERT INTO market_price_estimates ("+",".join(columns)+") VALUES ("
                    +",".join("?" for _ in columns)+") ON CONFLICT "
                    "(category_norm,condition_norm,municipality_norm,region_norm,price_quartile) "
                    "DO UPDATE SET "+",".join(c+"=excluded."+c for c in columns[5:]),
                    [row[c] for c in columns])
    finally:
        conn.close()


def compute_buy_recommendation(listing_pk, model_version=METHOD_VERSION, assumptions=None):
    """Return inspectable research or a labeled scenario, never an invented forecast."""
    if isinstance(listing_pk, bool) or not isinstance(listing_pk, int) or listing_pk <= 0:
        raise ValueError("listing_pk must be a positive integer")
    with db.read_snapshot():
        row = db.query_one("SELECT * FROM listings WHERE listing_pk=?", (listing_pk,))
        if not row:
            return None
        context = research.listing_evidence(row["platform"], row["listing_id"])
    evidence = context["evidence"]
    cutoff = datetime.now(timezone.utc)-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    enough = analysis.is_priced(row, cutoff) and evidence["peer_prices"]["status"] == "OK"
    return dict(status="scenario" if assumptions is not None else "research_only" if enough else "insufficient_data",
        reason=None if enough else "unsupported_product_peers_or_price",
        listing_pk=listing_pk, model_version=model_version, evidence=evidence,
        scenario=research.scenario(assumptions),
        expected_sale_price_eur=None, p_sale_within_90d=None,
        expected_gross_margin_eur=None, expected_gross_margin_pct=None,
        recommended_max_buy_price_eur=None,
        unit_quantity_basis="Public listing counts and seller quantity claims do not establish physical-unit throughput",
        basis="Peer asks describe supply. Scenario amounts are explicit user assumptions, not observed or predicted sale outcomes.")


def _compute_realization_ratio(category_norm, condition_norm):
    """Unavailable without comparable owned resale asks and realized transactions."""
    return None


def _estimate_sale_probability(category_norm, condition_norm, market_median, n_listings):
    """A supply count cannot establish a calibrated sale probability."""
    return None


def _cache_buy_recommendation(recommendation):
    raise ValueError("Unvalidated buy forecasts cannot be cached; save explicit research decisions instead")


def run_continuous_intelligence(cycle=None):
    """Read one coherent research snapshot; no collection, model activation or writes."""
    with db.read_snapshot():
        state = analysis.snapshot()
        from . import internet_research
        internet = internet_research.monitor()
    return dict(cycle=cycle or 1, status="research_only", method_version=METHOD_VERSION,
        generated_at=state["generated_at"], filters=state["filters"],
        asking_price_context=state["matrix"], candidates=state["candidates"],
        liquidity=state["liquidity"], portfolio=state["portfolio"],
        candidate_recommendations=0,
        internet_research=internet,
        basis="Candidates require inspection. Complete owned results and explicit scenarios remain separate from market asks.")


def intelligence_scheduler():
    return run_continuous_intelligence()


if __name__ == "__main__":
    print(json.dumps(run_continuous_intelligence(), indent=2, ensure_ascii=False, default=str))
