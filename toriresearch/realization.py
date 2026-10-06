"""Expected-realized-price estimation. Ask prices never become clearing prices here.

Tori does not publish what anything cleared for, and eBay's sold-price API sits
behind a Limited Release wall. Three evidence classes remain, and each has a
different standing:

1. **Our own resales.** Verified sale proceeds paired with our resale listing's
   asking price observed before the sale. Purchase negotiation ratios describe
   acquisition and are never used to estimate resale proceeds.
2. **eBay Product Research captures.** Three years of sold aggregates including
   Best Offer actuals, hand-captured for a bounded keyword list. Official, free,
   and from a *different market*. Transferring its ratio to Finnish classifieds
   is an assumption, not a measurement.
3. **Ask-price distribution.** What the segment currently asks. Never a clearing
   price on its own; it is the quantity the ratio is applied to.

The estimator blends what exists and refuses to invent what does not. Every
output carries the evidence class, n, and an interval. When the interval spans
a decision's break-even, the honest answer is "do not buy", not a point estimate
that looks decisive.
"""

import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from functools import wraps

from . import config, db, economics, metrics


def _read_snapshot(function):
    @wraps(function)
    def read(*args, **kwargs):
        with db.read_snapshot():
            return function(*args, **kwargs)
    return read


def _ratio(numerator, denominator):
    if numerator is None or denominator is None:
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        return None
    ratio = numerator / denominator
    # A paid price above the observed ask is possible (fees, upgrades, a stale
    # ask) but a ratio beyond 2x means the pairing is wrong, not that someone
    # overpaid twofold. Keep it visible rather than silently dropping it.
    if ratio <= 0 or ratio > 2:
        return None
    return ratio


def _historical_asks(rows, time_field):
    """Latest price actually observed before each transaction, including unchanged asks."""
    pks = sorted({row["listing_pk"] for row in rows if row.get("listing_pk")})
    observations = {}
    for offset in range(0, len(pks), 500):
        chunk = pks[offset:offset + 500]
        marks = ",".join("?" for _ in chunk)
        values = db.query("SELECT listing_pk, listed_price_eur, price_observed_at AS price_at, observed_at "
                          "FROM price_observations WHERE listing_pk IN (" + marks + ") UNION ALL "
                          "SELECT listing_pk, listed_price_eur, valid_from AS price_at, observed_at "
                          "FROM price_history WHERE listing_pk IN (" + marks + ")", chunk + chunk)
        for observation in values:
            stamp, recorded = metrics.parse_ts(observation["price_at"]), metrics.parse_ts(observation["observed_at"])
            price = observation["listed_price_eur"]
            if stamp and recorded and price is not None and math.isfinite(float(price)) and float(price) > 0:
                observations.setdefault(observation["listing_pk"], []).append((stamp, recorded, float(price)))
    now = datetime.now(timezone.utc)
    result = {}
    for index, row in enumerate(rows):
        cutoff = metrics.parse_ts(row.get(time_field))
        if not cutoff or cutoff > now:
            continue
        candidates = [value for value in observations.get(row.get("listing_pk"), [])
                      if value[0] <= cutoff and value[1] <= cutoff]
        # Legacy current-state prices are usable only with explicit pre-trade
        # price and recording timestamps. Later edits cannot rewrite a pairing.
        stamp = metrics.parse_ts(row.get("price_observed_at") or row.get("source_observed_at"))
        recorded = metrics.parse_ts(row.get("source_observed_at"))
        price = row.get("ask_eur")
        if (stamp and recorded and stamp <= cutoff and recorded <= cutoff and price is not None
                and math.isfinite(float(price)) and float(price) > 0):
            candidates.append((stamp, recorded, float(price)))
        if candidates:
            stamp, recorded, price = max(candidates, key=lambda value: (value[0], value[1]))
            result[index] = dict(ask_eur=price, price_observed_at=stamp.isoformat(),
                                 price_recorded_at=recorded.isoformat())
    return result


def _summarize_ratios(ratios, min_n):
    if isinstance(min_n, bool) or not isinstance(min_n, int) or min_n < 1:
        raise ValueError("min_n must be a positive integer")
    if len(ratios) < min_n:
        return dict(status="INSUFFICIENT DATA", note=f"{len(ratios)} paired trades below threshold {min_n}.")
    values = [row["ratio"] for row in ratios]
    ci = metrics.bootstrap_median_ci(values)
    return dict(status="OK", median_ratio=round(statistics.median(values), 4),
                p25_ratio=round(metrics.percentiles(values)[0.25], 4),
                p75_ratio=round(metrics.percentiles(values)[0.75], 4),
                median_ci95={key: round(ci[key], 4) for key in ("lo", "hi")} if ci else None,
                min_ratio=round(min(values), 4), max_ratio=round(max(values), 4),
                n_below_ask=sum(value < .999 for value in values),
                n_at_or_above_ask=sum(value >= .999 for value in values))


def freeze_resale_pairing(conn, platform, listing_id, sold_at):
    """Capture only evidence available at sale entry. Never recompute this blob.

    Identity uses retained accepted state, including its state clock. Historical
    reviews are applied only when their source-fact and model meaning still match.
    A current label is not substituted for missing historical identity evidence.
    """
    from . import inventory, product_attributes, product_identity
    result = dict(version="owned-resale-v1", frozen_at=inventory._now().isoformat(),
                  platform=platform, listing_id=listing_id, sold_at=str(sold_at),
                  ask=None, identity=None, market=None, status="unpaired")
    raw = conn.execute("SELECT * FROM listings WHERE platform=? AND listing_id=?", (platform, listing_id)).fetchone() if listing_id else None
    if not raw:
        return result
    row, cutoff = dict(raw), metrics.parse_ts(sold_at)
    if not cutoff or cutoff > inventory._now():
        raise ValueError("resale pairing requires an observed sale timestamp")
    result["listing_pk"] = row["listing_pk"]
    candidates = []
    observations = conn.execute("SELECT listed_price_eur, price_observed_at AS price_at, observed_at FROM price_observations WHERE listing_pk=? UNION ALL SELECT listed_price_eur, valid_from AS price_at, observed_at FROM price_history WHERE listing_pk=?", (row["listing_pk"], row["listing_pk"])).fetchall()
    observations = [dict(item) for item in observations]
    observations.append(dict(listed_price_eur=row["listed_price_eur"], price_at=row.get("price_observed_at"), observed_at=row.get("source_observed_at")))
    for item in observations:
        stamp, recorded = metrics.parse_ts(item["price_at"]), metrics.parse_ts(item["observed_at"])
        price = item["listed_price_eur"]
        if stamp and recorded and stamp <= cutoff and recorded <= cutoff and price is not None and math.isfinite(float(price)) and float(price) > 0:
            candidates.append((stamp, recorded, float(price)))
    if candidates:
        stamp, recorded, price = max(candidates)
        result["ask"] = dict(ask_eur=price, price_observed_at=stamp.isoformat(), price_recorded_at=recorded.isoformat())
    snapshots = conn.execute("SELECT * FROM listing_observations WHERE listing_pk=?", (row["listing_pk"],)).fetchall()
    snapshots = [dict(item) for item in snapshots if metrics.parse_ts(item["state_at"]) and metrics.parse_ts(item["state_at"]) <= cutoff and metrics.parse_ts(item["observed_at"]) and metrics.parse_ts(item["observed_at"]) <= cutoff]
    if snapshots:
        import json
        snapshot = max(snapshots, key=lambda item: (metrics.parse_ts(item["state_at"]), item["observation_pk"]))
        historical = json.loads(snapshot["state_json"])
        historical.update(listing_pk=row["listing_pk"], platform=platform, listing_id=listing_id)
        historical = product_attributes.attach([historical], conn=conn, as_of=cutoff)[0]
        result["market"] = {key: historical.get(key) for key in ("platform", "subplatform", "category_norm", "condition_norm", "brand_norm", "model_norm")}
        catalog = product_identity.load()
        identity = product_identity.derive(historical, catalog)
        reviews = conn.execute("SELECT output_json, observed_at FROM enrichment WHERE listing_pk=? AND task=? ORDER BY enrichment_pk DESC", (row["listing_pk"], product_identity.REVIEW_TASK)).fetchall()
        for review in reviews:
            if not metrics.parse_ts(review["observed_at"]) or metrics.parse_ts(review["observed_at"]) > cutoff:
                continue
            try:
                record = json.loads(review["output_json"])
                if (record.get("review_version") == product_identity.REVIEW_VERSION
                        and record.get("source_input_sha256") == product_identity.fact_digest(historical)
                        and record.get("model_semantics_sha256") == product_identity.model_semantics(record["expected"], catalog)
                        and metrics.parse_ts(record.get("reviewed_at")) and metrics.parse_ts(record["reviewed_at"]) <= cutoff):
                    identity = product_identity._apply_review(identity, record, catalog)
                    break
            except (ValueError, KeyError, TypeError):
                continue
        result["identity"] = identity
        result["identity_state_at"] = str(snapshot["state_at"])
    result["status"] = "paired" if result["ask"] else "ask_unknown"
    result["basis"] = "Ask frozen at entry; canonical comparability requires retained pre-sale state and review."
    return result


def _frozen_resale_reason(pair, row, now):
    """Check retained pairing clocks without repairing them from current labels."""
    if pair.get("version") != "owned-resale-v1":
        return "Unknown frozen resale pairing version"
    ask, identity, market = (pair.get(key) for key in ("ask", "identity", "market"))
    if not all(isinstance(value, dict) and value for value in (ask, identity, market)):
        return "Frozen pre-sale ask, identity or market is missing"
    sold, frozen, purchased, recorded, identity_at, price_at, price_recorded = (
        metrics.parse_ts(value) for value in (row["sale_at"], pair.get("frozen_at"),
            row["purchased_at"], row["sale_recorded_at"], pair.get("identity_state_at"),
            ask.get("price_observed_at"), ask.get("price_recorded_at")))
    if (not all((sold, frozen, purchased, recorded, identity_at, price_at, price_recorded))
            or metrics.parse_ts(pair.get("sold_at")) != sold
            or not purchased <= sold <= frozen <= recorded <= now
            or identity_at > sold or price_at > sold or price_recorded > sold):
        return "Frozen resale chronology lacks recorded pre-sale evidence"
    if (pair.get("platform") != row["sale_platform"] or market.get("platform") != row["sale_platform"]
            or not isinstance(pair.get("listing_id"), str) or not pair["listing_id"]
            or type(pair.get("listing_pk")) is not int or pair["listing_pk"] <= 0
            or row.get("listing_pk") is not None and pair["listing_pk"] != row["listing_pk"]):
        return "Frozen pairing does not match the recorded resale listing"
    price = ask.get("ask_eur")
    if (isinstance(price, bool) or not isinstance(price, (int, float))
            or _ratio(float(row["final_sale_price_eur"] or 0), price) is None):
        return "Frozen resale ask or proceeds cannot form a valid ratio"
    if identity.get("pilot_scope") and not identity.get("comparison_key"):
        return "Frozen canonical identity lacks a comparable variant and offer"
    return None


@_read_snapshot
def own_trade_ratios(min_n=None):
    """Ask-to-paid ratios learned from our own purchases against observed asks.

    This is the only ratio on this platform that is a measurement rather than a
    transfer assumption. It requires the source listing to have been observed
    with a price, so an unobserved source contributes nothing rather than an
    imputed ask.
    """
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    rows = db.query(
        """SELECT p.purchase_pk, p.title_raw, p.platform, p.source_listing_id,
                  p.acquisition_cost_eur, p.purchased_at, p.category_norm, p.condition_norm,
                  l.listed_price_eur AS ask_eur, l.price_observed_at, l.source_observed_at, l.listing_pk
           FROM purchases p
           LEFT JOIN listings l ON l.platform = p.platform AND l.listing_id = p.source_listing_id
           WHERE p.acquisition_cost_eur IS NOT NULL""")
    asks = _historical_asks(rows, "purchased_at")
    ratios = []
    for index, row in enumerate(rows):
        if index not in asks:
            continue
        ask = asks[index]
        value = _ratio(float(row["acquisition_cost_eur"]), ask["ask_eur"])
        if value is None:
            continue
        ratios.append({"purchase_pk": row["purchase_pk"], "title": row["title_raw"],
                       "category_norm": row["category_norm"],
                       "condition_norm": row["condition_norm"],
                       **ask,
                       "paid_eur": round(float(row["acquisition_cost_eur"]), 2),
                       "ratio": round(value, 4)})
    unpaired = len(rows) - len(asks)
    rejected = len(asks) - len(ratios)
    report = {
        "n_purchases_with_observed_ask": len(asks),
        "n_unpaired_purchases": unpaired,
        "n_rejected_pairings": rejected,
        "n_valid_ratios": len(ratios),
        "min_n_threshold": min_n,
        "ratios": sorted(ratios, key=lambda r: r["purchase_pk"]),
        "status": "INSUFFICIENT DATA",
        "basis": "Acquisition negotiation: our paid purchase price divided by a source ask observed before purchase. Not resale realization.",
    }
    report.update(_summarize_ratios(ratios, min_n))
    return report


@_read_snapshot
def resale_trade_ratios(min_n=None, platform="tori", subplatform="standard",
                        category_norm=None, condition_norm=None, product_type=None,
                        brand_norm=None, model_norm=None, comparison_key=None,
                        comparison_policy_version=None):
    """Frozen owned resales qualify; unfrozen legacy sales remain separate context.

    SKU-managed transactions need an explicit resale listing relationship. An
    adopted legacy transaction is excluded rather than double-counted or reused
    after refunds and subsequent resale cycles.
    """
    from . import analysis
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    where = ["t.outcome_status='sold_confirmed'", "t.final_sale_price_eur>0",
             "t.resale_pairing_json IS NULL", "t.platform=l.platform", "t.platform=?", "l.subplatform=?",
             "NOT EXISTS (SELECT 1 FROM order_lines o WHERE o.legacy_transaction_pk=t.transaction_pk)",
             "NOT EXISTS (SELECT 1 FROM inventory_units u JOIN order_lines o ON o.sku=u.sku WHERE u.purchase_pk=t.purchase_pk)"]
    params = [platform, subplatform]
    for field, value in (("category_norm", category_norm), ("condition_norm", condition_norm),
                         ("brand_norm", brand_norm), ("model_norm", model_norm)):
        if value is not None:
            where.append("l." + field + "=?")
            params.append(value)
    rows = db.query("SELECT l.*, l.listed_price_eur AS ask_eur, t.transaction_pk, t.purchase_pk, p.purchased_at, "
                    "t.final_sale_price_eur, t.sale_at, t.observed_at AS transaction_observed_at "
                    "FROM transactions t JOIN listings l ON l.listing_pk=t.listing_pk "
                    "JOIN purchases p ON p.purchase_pk=t.purchase_pk WHERE "
                    + " AND ".join(where), params)
    if product_type is not None:
        types = analysis.accepted_product_types(rows)
        rows = [row for row in rows if types.get(row["listing_pk"]) == product_type]
    if comparison_key is not None or comparison_policy_version is not None:
        rows = []  # Unfrozen legacy labels cannot establish canonical comparability.
    asks = _historical_asks(rows, "sale_at")
    legacy_ratios, seen = [], set()
    now = datetime.now(timezone.utc)
    for index, row in enumerate(rows):
        # Multiple records for one physical acquisition cannot inflate n.
        if row["purchase_pk"] is None or row["purchase_pk"] in seen or index not in asks:
            continue
        sold, recorded = metrics.parse_ts(row["sale_at"]), metrics.parse_ts(row["transaction_observed_at"])
        purchased = metrics.parse_ts(row["purchased_at"])
        if not sold or not recorded or recorded > now or not purchased or purchased > sold:
            continue
        value = _ratio(float(row["final_sale_price_eur"]), asks[index]["ask_eur"])
        if value is None:
            continue
        seen.add(row["purchase_pk"])
        legacy_ratios.append(dict(transaction_pk=row["transaction_pk"], purchase_pk=row["purchase_pk"],
                           listing_pk=row["listing_pk"], **asks[index],
                           paid_eur=float(row["final_sale_price_eur"]), ratio=value,
                           pairing_basis="legacy_reconstructed_current_labels"))
    from . import trades
    frozen = db.query("SELECT t.purchase_pk,t.transaction_pk,t.listing_pk,t.sale_at,t.platform AS sale_platform,"
                      "t.observed_at AS sale_recorded_at,p.purchased_at,t.final_sale_price_eur,t.resale_pairing_json "
                      "FROM transactions t JOIN purchases p ON p.purchase_pk=t.purchase_pk "
                      "WHERE t.outcome_status='sold_confirmed' AND t.resale_pairing_json IS NOT NULL "
                      "AND NOT EXISTS (SELECT 1 FROM inventory_units u JOIN order_lines o ON o.sku=u.sku WHERE u.purchase_pk=t.purchase_pk)")
    managed = db.query("SELECT o.*,u.purchase_pk,p.purchased_at,e.sale_recorded_at FROM order_lines o "
                       "JOIN inventory_units u ON u.sku=o.sku JOIN purchases p ON p.purchase_pk=u.purchase_pk "
                       "LEFT JOIN (SELECT order_id,MIN(recorded_at) AS sale_recorded_at FROM order_events "
                       "WHERE event_type IN ('sale_recorded','legacy_imported') GROUP BY order_id) e ON e.order_id=o.order_id "
                       "WHERE o.resale_pairing_json IS NOT NULL ORDER BY o.cycle DESC")
    managed_cache = {}
    for row in managed:
        if row["sku"] not in managed_cache:
            managed_cache[row["sku"]] = trades.economics(row["sku"])
        economy = managed_cache[row["sku"]]
        current = economy["current_order"]
        if not economy["complete"] or economy["stock_phase"] != "sold" or not current or current["order_id"] != row["order_id"]:
            continue
        # Only the current settled sale contributes one ratio per physical unit.
        # Its receipts exclude its refunds; earlier refunded cycles stay in costs.
        entries = [entry for entry in economy["active_entries"] if entry["order_id"] == row["order_id"]]
        received = sum((entry["amount_minor"] or 0) * (1 if entry["kind"] == "sale_proceeds" else -1) for entry in entries if entry["kind"] in {"sale_proceeds", "refund"}) / 100
        frozen.append(dict(row, sale_at=row["sold_at"], sale_platform=row["channel"],
                           final_sale_price_eur=received))
    ratios, seen, excluded = [], set(), []
    for row in frozen:
        try:
            pair = json.loads(row["resale_pairing_json"])
        except (TypeError, ValueError):
            pair = None
        if not isinstance(pair, dict):
            excluded.append(dict(purchase_pk=row["purchase_pk"], transaction_pk=row.get("transaction_pk"),
                                 order_id=row.get("order_id"), scope="unknown",
                                 reason="Malformed frozen resale pairing"))
            continue
        market = pair.get("market")
        identity = pair.get("identity")
        if not isinstance(market, dict) or not market.get("platform") or not market.get("subplatform"):
            excluded.append(dict(purchase_pk=row["purchase_pk"], transaction_pk=row.get("transaction_pk"),
                                 order_id=row.get("order_id"), scope="unknown",
                                 reason="Frozen pre-sale platform or market partition is missing"))
            continue
        identity = identity if isinstance(identity, dict) else {}
        requested = dict(platform=platform, subplatform=subplatform, category_norm=category_norm,
                         condition_norm=condition_norm, brand_norm=brand_norm, model_norm=model_norm)
        if any(value is not None and market.get(key) != value for key, value in requested.items()):
            continue
        if product_type is not None and identity.get("product_type") != product_type:
            continue
        if comparison_key is not None and identity.get("comparison_key") != comparison_key:
            continue
        if comparison_policy_version is not None and identity.get("comparison_policy_version") != comparison_policy_version:
            continue
        reason = _frozen_resale_reason(pair, row, now)
        if reason:
            excluded.append(dict(purchase_pk=row["purchase_pk"], transaction_pk=row.get("transaction_pk"),
                                 order_id=row.get("order_id"), scope="requested_filters", reason=reason))
            continue
        ask = pair["ask"]
        value = _ratio(float(row["final_sale_price_eur"]), ask["ask_eur"])
        if row["purchase_pk"] in seen:
            continue
        seen.add(row["purchase_pk"])
        ratios.append(dict(purchase_pk=row["purchase_pk"], transaction_pk=row.get("transaction_pk"),
                           order_id=row.get("order_id"), listing_pk=pair["listing_pk"], **ask,
                           paid_eur=row["final_sale_price_eur"], ratio=value,
                           comparison_key=identity.get("comparison_key"),
                           comparison_policy_version=identity.get("comparison_policy_version"),
                           pairing_basis="frozen_pre_sale_evidence"))
    report = dict(n_valid_ratios=len(ratios), n_unfrozen_sales=len(rows),
                  n_unpaired_sales=len(rows)+sum(item["scope"] == "requested_filters" for item in excluded),
                  excluded_frozen_sales=excluded,
                  exclusion_scope_note="Unknown-scope frozen records cannot be assigned to the requested filters and are excluded from their unpaired count.",
                  min_n_threshold=min_n, ratios=ratios,
                  filters=dict(platform=platform, subplatform=subplatform, category_norm=category_norm,
                               condition_norm=condition_norm, product_type=product_type,
                               brand_norm=brand_norm, model_norm=model_norm),
                  basis="Verified owned resale proceeds divided by the ask frozen at sale entry with retained pre-sale identity and market; purchase discounts and unfrozen sales excluded.")
    report["n_frozen_ratios"] = len(ratios)
    report["filters"].update(comparison_key=comparison_key, comparison_policy_version=comparison_policy_version)
    report["legacy_context"] = dict(status="DESCRIPTIVE ONLY", eligible_for_benchmark=False,
                                  n_sales=len(rows), n_valid_ratios=len(legacy_ratios), ratios=legacy_ratios,
                                  statistics=_summarize_ratios(legacy_ratios, min_n),
                                  basis="Unfrozen legacy sales reconstructed with current labels; excluded from all resale benchmarks.")
    report["legacy_limit"] = report["legacy_context"]["basis"]
    report.update(_summarize_ratios(ratios, min_n))
    return report


def _ebay_ratio(platform="ebay", min_n=None):
    """Ratio evidence from Product Research captures, if any exist."""
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    rows = db.query(
        "SELECT keyword, avg_sold_price_eur, n_sold FROM sold_observations "
        "WHERE platform=? AND avg_sold_price_eur IS NOT NULL", (platform,))
    if not rows:
        return {"status": "NO DATA",
                "note": "No Product Research captures recorded. Run "
                        "'soldextract template', capture, then import."}
    values = [float(r["avg_sold_price_eur"]) for r in rows
              if r["avg_sold_price_eur"] and math.isfinite(float(r["avg_sold_price_eur"]))]
    if not values:
        return {"status": "NO DATA", "note": "Captures present but no usable sold prices."}
    return {
        "status": "OK" if len(values) >= min_n else "INSUFFICIENT DATA",
        "n_captures": len(values),
        "min_n_threshold": min_n,
        "median_avg_sold_eur": round(statistics.median(values), 2),
        "n_underlying_transactions": sum(int(r["n_sold"] or 0) for r in rows),
        "transfer_caveat": "eBay aggregates describe eBay sold stock. Applying them to Finnish "
                           "classifieds is an assumption and must be labelled as one.",
    }


@_read_snapshot
def realized_price_estimate(category_norm=None, condition_norm=None,
                            product_type=None, min_n=None, platform="tori",
                            subplatform="standard", brand_norm=None, model_norm=None,
                            exclude_listing_pk=None):
    """Scoped resale benchmark; its interval is not an individual-unit forecast."""
    from . import analysis
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    if isinstance(min_n, bool) or not isinstance(min_n, int) or min_n < 1:
        raise ValueError("min_n must be a positive integer")
    where, params, filters = metrics.listing_filters(category_norm=category_norm,
        condition_norm=condition_norm, platform=platform, subplatform=subplatform)
    for field, value in (("brand_norm", brand_norm), ("model_norm", model_norm)):
        if value is not None:
            where.append(field + "=?")
            params.append(value)
    rows = db.query("SELECT " + analysis.FIELDS + " FROM listings WHERE " + " AND ".join(where), params)
    if product_type is not None:
        types = analysis.accepted_product_types(rows)
        rows = [row for row in rows if types.get(row["listing_pk"]) == product_type]
    cutoff = datetime.now(timezone.utc)-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    rows = [row for row in rows if row["listing_pk"] != exclude_listing_pk]
    panel = analysis.price_panel(rows, cutoff, min_n=min_n)
    panel.update(n_with_price=panel["n_priced"], coverage_with_price=panel["price_coverage"], filters=filters)
    own = resale_trade_ratios(min_n=min_n, category_norm=category_norm, condition_norm=condition_norm,
                             product_type=product_type, platform=platform, subplatform=subplatform,
                             brand_norm=brand_norm, model_norm=model_norm)
    ebay = _ebay_ratio(min_n=min_n)
    evidence = {
        "ask_panel": {k: panel.get(k) for k in
                      ("status", "n_with_price", "coverage_with_price", "median_eur",
                       "p25_eur", "p75_eur", "median_ci95", "filters")},
        "resale_trade_ratios": {k: own.get(k) for k in
                             ("status", "n_valid_ratios", "median_ratio", "p25_ratio",
                              "p75_ratio", "median_ci95", "note", "basis", "n_frozen_ratios",
                              "n_unfrozen_sales", "n_unpaired_sales")},
        "ebay_captures": ebay,
    }
    estimate = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": own["filters"],
        "min_n_threshold": min_n,
        "evidence": evidence,
        "status": "INSUFFICIENT DATA",
        "estimate": None,
    }
    if panel.get("status") != "OK":
        estimate["blocking"] = "ASK PANEL"
        estimate["note"] = (f"Ask panel is {panel.get('status')}. Without a supported ask "
                            "median there is nothing to apply a realization ratio to.")
        return estimate
    if own.get("status") != "OK":
        estimate["blocking"] = "OWN RESALE RATIOS"
        estimate["note"] = (
            f"Owned resale ratio evidence is {own.get('status')} "
            f"({own.get('n_valid_ratios', 0)} frozen paired trades, needs {min_n}; "
            f"{own.get('n_unfrozen_sales', 0)} unfrozen sales excluded). "
            "Asking prices cannot be converted into an expected clearing price without "
            "comparable verified resale pairings. Purchase discounts do not establish resale prices. eBay captures are context only; "
            "they are a different market."
        )
        return estimate

    ratio = own["median_ratio"]
    ratio_ci = own.get("median_ci95")
    ask = panel["median_eur"]
    point = ask * ratio
    ask_ci = panel.get("median_ci95")
    estimate["status"] = "ESTIMATE"
    estimate["estimate"] = {
        "median_ask_eur": ask,
        "realization_ratio": ratio,
        "expected_realized_eur": round(point, 2),
        "interval_eur": [round(ask_ci["lo"] * ratio_ci["lo"], 2), round(ask_ci["hi"] * ratio_ci["hi"], 2)]
        if ratio_ci and ask_ci else None,
        "ask_ci_eur": panel.get("median_ci95"),
        "confidence": "owned-resale-benchmark",
        "interval_basis": "Envelope of ask-median and resale-ratio intervals; not a calibrated predictive interval",
        "requires_variant_verification": True,
        "caveat": ("Benchmark from verified resale pairings in the selected scope, using frozen pre-sale identity and market evidence. "
                   "Variants require inspection; this is never an observed clearing price or an accepted unit forecast."),
    }
    return estimate


def buy_no_buy(acquisition_cost_eur, expected_realized_eur, interval_eur=None,
               selling_costs_eur=None, min_margin_pct=0.15):
    """Decision arithmetic on an expected realized price. Never an observation.

    Returns a decision whose confidence follows the interval, not the point
    estimate: if the downside of the interval cannot clear costs, the answer is
    'do not buy' regardless of how attractive the midpoint looks.
    """
    if expected_realized_eur is None or acquisition_cost_eur is None:
        return {"status": "INSUFFICIENT DATA",
                "note": "Both an expected realized price and an acquisition cost are required."}
    floor = economics.amount(min_margin_pct, "min_margin_pct")
    if floor is None or floor >= 1:
        raise ValueError("min_margin_pct must be between zero and one")
    calculation = economics.unit_economics(acquisition_cost_eur, expected_realized_eur,
        {"other_costs_eur": selling_costs_eur}, required_costs=("other_costs_eur",))
    if calculation["status"] != "COMPLETE":
        return dict(status="INSUFFICIENT DATA", decision="REVIEW", decision_follows_interval=False,
                    unknown_costs=calculation["unknown_costs"],
                    note="Supply all non-acquisition costs explicitly; missing costs are unknown.")
    costs = calculation["total_cost_eur"]
    point_margin = calculation["net_profit_eur"]
    point_pct = calculation["net_margin_pct"]
    result = {
        "status": "ESTIMATE",
        "acquisition_cost_eur": round(float(acquisition_cost_eur), 2),
        "selling_costs_eur": round(float(selling_costs_eur), 2),
        "total_cost_eur": round(costs, 2),
        "expected_realized_eur": round(float(expected_realized_eur), 2),
        "expected_margin_eur": round(point_margin, 2),
        "expected_margin_pct": round(point_pct, 4) if point_pct is not None else None,
        "min_margin_pct_required": min_margin_pct,
        "basis": "Scenario arithmetic on an estimated realized price. Not an observed outcome.",
    }
    if interval_eur is not None:
        if not isinstance(interval_eur, (list, tuple)) or len(interval_eur) != 2:
            raise ValueError("interval must contain a lower and upper price")
        low, high = (economics.amount(value, "interval price") for value in interval_eur)
        expected = economics.amount(expected_realized_eur, "expected_realized_eur")
        if low is None or high is None or not low or not low <= expected <= high:
            raise ValueError("interval must be positive, ordered and contain the expected price")
        low, high = float(low), float(high)
        low_margin, high_margin = low - costs, high - costs
        result["interval_eur"] = [round(low, 2), round(high, 2)]
        result["downside_margin_eur"] = round(low_margin, 2)
        result["upside_margin_eur"] = round(high_margin, 2)
        if low_margin <= 0:
            result["decision"] = "DO NOT BUY"
            result["reason"] = (
                "The downside of the estimate interval does not clear costs. PROJECT_PLAN "
                "Phase 3: if the interval spans zero margin, the answer is don't buy."
            )
        elif low_margin / low < min_margin_pct if low else True:
            result["decision"] = "WEAK"
            result["reason"] = f"Downside margin is below the {min_margin_pct:.0%} floor."
        else:
            result["decision"] = "BUY"
            result["reason"] = "Even the downside of the interval clears costs above the floor."
        result["decision_follows_interval"] = True
    else:
        result["decision"] = "REVIEW" if point_margin > 0 else "DO NOT BUY"
        result["reason"] = ("No interval available, so the decision cannot follow the downside. "
                            "Treat the point estimate as a prompt to inspect, not a green light.")
        result["decision_follows_interval"] = False
    return result
