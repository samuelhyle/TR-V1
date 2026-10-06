"""The buy side. This is the asset.

Tori never publishes what anything sold for, and eBay's sold data sits behind a
Limited Release wall. Nobody can scrape the other side of this table. Every row
here is a verified realized transaction that compounds with each trade and that
no platform can revoke. Record from the first day, without exception.
"""

import logging
import json
import math
from datetime import datetime, timezone

from . import config, db, economics

log = logging.getLogger("toriresearch.purchases")

UNKNOWN_COST = {
    "platform_fee_eur", "payment_fee_eur", "seller_shipping_cost_eur",
    "packaging_cost_eur", "other_variable_cost_eur", "tax_cost_eur",
}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _money(value, name):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a monetary amount")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return amount


def record(title, acquisition_cost_eur, platform="tori", subplatform="standard",
           source_listing_id=None, category_norm=None, brand_norm=None,
           model_norm=None, condition_norm=None, purchased_at=None,
           currency="EUR", fx_rate_to_eur=None, fx_rate_date=None,
           fx_source=None, shipping_cost_eur=None, buyer_fee_eur=None,
           notes=None, attributes=None):
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required")
    if subplatform not in config.SUBPLATFORMS:
        raise ValueError("unsupported subplatform")
    acquisition_cost_eur = _money(acquisition_cost_eur, "acquisition cost")
    if acquisition_cost_eur is None:
        raise ValueError("acquisition cost is required")
    shipping_cost_eur = _money(shipping_cost_eur, "inbound shipping")
    buyer_fee_eur = _money(buyer_fee_eur, "buyer fee")
    if not source_listing_id:
        raise ValueError(
            "source_listing_id is required. Without it a purchase cannot be joined to "
            "the listing it came from and the dataset degrades to an unusable ledger."
        )
    if currency != "EUR" and not fx_rate_to_eur:
        raise ValueError(
            f"currency {currency} requires fx_rate_to_eur. Never record a converted "
            "price without the rate, its date and its source."
        )

    date_supplied = purchased_at is not None
    if purchased_at is None:
        purchased_at = _utcnow()
    elif len(purchased_at) == 10:
        purchased_at = f"{purchased_at}T00:00:00+00:00"

    conn = db.connect()
    try:
        with conn.transaction():
            existing = conn.execute(
                "SELECT purchase_pk, purchased_at FROM purchases WHERE platform=? AND source_listing_id=?",
                (platform, source_listing_id),
            ).fetchone()
            if existing:
                if conn.execute("SELECT 1 FROM financial_entries f JOIN inventory_units u ON u.sku=f.sku WHERE u.purchase_pk=? AND f.kind='write_off' AND f.order_id IS NULL", (existing[0],)).fetchone():
                    raise ValueError("This purchase was written off. Correct its costs through outcomes correct_cost to preserve history.")
                if conn.execute("SELECT 1 FROM order_lines o JOIN inventory_units u ON u.sku=o.sku WHERE u.purchase_pk=?", (existing[0],)).fetchone():
                    raise ValueError("This purchase has SKU-linked trades. Correct acquisition costs through the financial journal to preserve history.")
                if not date_supplied:
                    purchased_at = existing[1]
                conn.execute(
                    """UPDATE purchases SET purchased_at=?, title_raw=?, category_norm=?, brand_norm=?,
                           model_norm=?, condition_norm=?, acquisition_cost_eur=?, shipping_cost_eur=?,
                           buyer_fee_eur=?, fx_rate_to_eur=?, fx_rate_date=?, fx_source=?, notes=?,
                           subplatform=?, condition_at_buy_norm=?, attributes_json=?
                       WHERE purchase_pk=?""",
                    (purchased_at, title, category_norm, brand_norm, model_norm, condition_norm,
                     acquisition_cost_eur, shipping_cost_eur, buyer_fee_eur, fx_rate_to_eur,
                     fx_rate_date, fx_source, notes, subplatform, condition_norm,
                     attributes if isinstance(attributes, str) or attributes is None else json.dumps(attributes),
                     existing[0]),
                )
                _refresh_margin(conn, existing[0])
                return existing[0]

            cur = conn.execute(
                """INSERT INTO purchases (purchased_at, platform, subplatform, source_listing_id,
                       title_raw, category_norm, brand_norm, model_norm, condition_norm,
                       condition_at_buy_norm, attributes_json, acquisition_cost_eur,
                       shipping_cost_eur, buyer_fee_eur, fx_rate_to_eur, fx_rate_date,
                       fx_source, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (purchased_at, platform, subplatform, source_listing_id, title, category_norm,
                 brand_norm, model_norm, condition_norm, condition_norm,
                 attributes if isinstance(attributes, str) or attributes is None else json.dumps(attributes),
                 acquisition_cost_eur, shipping_cost_eur, buyer_fee_eur, fx_rate_to_eur,
                 fx_rate_date, fx_source, notes),
            )
            return conn.execute("SELECT purchase_pk FROM purchases WHERE platform=? AND source_listing_id=?",
                                (platform, source_listing_id)).fetchone()[0]
    finally:
        conn.close()


def list_purchases():
    return db.query(
        """SELECT purchase_pk, purchased_at, platform, subplatform, source_listing_id,
                  title_raw, category_norm, brand_norm, model_norm, condition_norm,
                  acquisition_cost_eur, shipping_cost_eur, buyer_fee_eur, sold_listing_pk,
                  (SELECT sku FROM inventory_units u WHERE u.purchase_pk=purchases.purchase_pk) AS inventory_sku
           FROM purchases ORDER BY purchased_at DESC, purchase_pk DESC"""
    )


def _margin(txn):
    """C8 correction: a missing cost is unknown, not zero.

    secondhand.pdf's margin SQL wrapped every cost in COALESCE(..., 0) while the
    same document stated that a missing fee which means unknown must null the
    whole margin. The query overrode its own rule and overstated margin. Here any
    unknown component nulls the result and records why.
    """
    if txn.get("managed_sku"):
        return txn.get("net_margin"), None if txn.get("net_margin") else "not_exposed"
    revenue = txn.get("final_sale_price_eur")
    if revenue is None:
        return None, "not_exposed"
    if txn.get("acquisition_cost_eur") is None:
        return None, "not_observed"

    fields = sorted(UNKNOWN_COST | {"inbound_shipping_cost_eur", "buyer_fee_eur"})
    result = economics.unit_economics(txn["acquisition_cost_eur"], revenue,
                                     {key: txn.get(key) for key in fields}, required_costs=fields)
    if result["status"] != "COMPLETE":
        return None, "not_exposed"
    pct = result["net_margin_pct"]
    return {"gross_margin_eur": result["net_profit_eur"],
            "gross_margin_pct": round(pct, 6) if pct is not None else None}, None


def _refresh_margin(conn, purchase_pk):
    row = conn.execute(
        "SELECT t.*, p.acquisition_cost_eur, p.buyer_fee_eur, "
        "p.shipping_cost_eur AS inbound_shipping_cost_eur FROM transactions t "
        "JOIN purchases p ON p.purchase_pk=t.purchase_pk WHERE p.purchase_pk=?",
        (purchase_pk,)).fetchone()
    if row is None:
        return
    margin, reason = (_margin(dict(row)) if row["outcome_status"] in ("sold_confirmed", "sold_inferred")
                      else (None, "not_applicable"))
    conn.execute(
        "UPDATE transactions SET gross_margin_eur=?, gross_margin_pct=?, "
        "margin_known=?, margin_missing_reason=?, observed_at=? WHERE purchase_pk=?",
        (margin["gross_margin_eur"] if margin else None,
         margin["gross_margin_pct"] if margin else None, bool(margin), reason,
         _utcnow(), purchase_pk))


def ledger_rows():
    """Compatibility rows, replacing managed legacy rows with SKU net economics."""
    rows = db.query(
        """SELECT p.purchase_pk, p.title_raw, p.category_norm, p.brand_norm,
                  p.acquisition_cost_eur, p.purchased_at, p.sold_listing_pk,
                  p.shipping_cost_eur AS inbound_shipping_cost_eur, p.buyer_fee_eur,
                  t.final_sale_price_eur, t.platform_fee_eur, t.payment_fee_eur,
                  t.seller_shipping_cost_eur, t.packaging_cost_eur,
                  t.other_variable_cost_eur, t.tax_cost_eur, t.sale_at,
                  t.outcome_status
           FROM purchases p
           LEFT JOIN transactions t ON t.purchase_pk = p.purchase_pk
           ORDER BY p.purchased_at DESC"""
    )
    from . import trades
    managed = trades.ledger_rows()
    managed_purchases = {row["purchase_pk"] for row in managed}
    return [row for row in rows if row["purchase_pk"] not in managed_purchases] + managed


def realized_economics():
    """One result per physical purchase, including its refunds and resale cycles."""
    rows = ledger_rows()
    out = []
    for r in rows:
        # Inventory status is decided by whether a sale exists, not by whether we
        # happened to list the item ourselves. Most sales go through no listing
        # we ever scraped, and calling those "in_stock" hides every sale.
        extra = {"managed_sku": r["managed_sku"], "trade_economics": r["trade_economics"]} if r.get("managed_sku") else {}
        if r["outcome_status"] is None:
            out.append({
                "purchase_pk": r["purchase_pk"],
                "title": r["title_raw"],
                "status": "in_stock",
                "acquisition_cost_eur": r["acquisition_cost_eur"],
                **extra,
            })
            continue
        margin, reason = (_margin(r) if r["outcome_status"] in ("sold_confirmed", "sold_inferred", "written_off")
                          else (None, "not_applicable"))
        out.append({
            "purchase_pk": r["purchase_pk"],
            "title": r["title_raw"],
            "status": r["outcome_status"],
            "acquisition_cost_eur": r["acquisition_cost_eur"],
            "final_sale_price_eur": r["final_sale_price_eur"],
            "sale_at": r["sale_at"],
            "sale_costs": {field: r[field] for field in sorted(UNKNOWN_COST)},
            **extra,
            **({"margin": margin} if margin else
               {"margin": None, "margin_missing_reason": reason}),
        })
    return out


def summary():
    rows = realized_economics()
    sold = [r for r in rows if r["status"] in ("sold_confirmed", "sold_inferred") and r.get("margin")]
    unknown = [r for r in rows if r["status"] in ("sold_confirmed", "sold_inferred")
               and r.get("margin") is None]
    margins = [r["margin"]["gross_margin_eur"] for r in sold]
    pcts = [r["margin"]["gross_margin_pct"] for r in sold if r["margin"]["gross_margin_pct"] is not None]

    from .metrics import bootstrap_median_ci, percentiles, suppress
    result = {
        "purchases_recorded": len(rows),
        "sold_with_complete_costs": len(sold),
        "sold_with_unknown_costs": len(unknown),
        "min_n_threshold": config.MIN_N_FOR_REPORT,
        "net_profit_total_eur": round(sum(row["margin"]["gross_margin_eur"] for row in rows if row.get("margin")), 2) if any(row.get("margin") for row in rows) else None,
    }
    if suppress(len(sold), config.MIN_N_FOR_REPORT):
        result["status"] = "INSUFFICIENT DATA"
        result["note"] = (
            f"Only {len(sold)} sales with fully known costs, below threshold "
            f"{config.MIN_N_FOR_REPORT}. This is expected early, and it is exactly why "
            "the table starts recording now: the constraint is time, not method."
        )
        return result
    result["status"] = "OK"
    result["median_gross_margin_eur"] = round(percentiles(margins)[0.50], 2)
    result["median_gross_margin_pct"] = round(percentiles(pcts)[0.50], 4) if len(pcts) >= config.MIN_N_FOR_REPORT else None
    result["median_margin_ci95"] = bootstrap_median_ci(margins)
    result["n_losses"] = sum(1 for r in sold if r["margin"]["gross_margin_eur"] < 0)
    result["n_profitable"] = sum(1 for r in sold if r["margin"]["gross_margin_eur"] > 0)
    result["n_break_even"] = sum(1 for r in sold if r["margin"]["gross_margin_eur"] == 0)
    result["worst_margin_eur"] = round(min([r["margin"]["gross_margin_eur"] for r in sold], default=0), 2)
    result["best_margin_eur"] = round(max([r["margin"]["gross_margin_eur"] for r in sold], default=0), 2)
    return result


def fee_basis(min_n=None, platform=None, subplatform=None, category_norm=None, condition_norm=None):
    """Observed sale-side fee rates from our own completed transactions.

    The buy/no-buy engine must not invent a fee percentage. Until our own sales
    carry enough observations, fees are unknown and the margin they would
    produce is reported as unknown rather than assumed at a plausible-looking
    constant. Rates are per-transaction ratios, so a flat fee on a cheap item
    shows up as the large rate it actually is instead of being averaged away.
    """
    from .metrics import percentiles
    min_n = config.MIN_N_FOR_REPORT if min_n is None else min_n
    where = ["t.final_sale_price_eur > 0", "t.outcome_status='sold_confirmed'",
             "NOT EXISTS (SELECT 1 FROM order_lines o WHERE o.legacy_transaction_pk=t.transaction_pk)"]
    params = []
    for field, value in (("t.platform", platform), ("p.subplatform", subplatform),
                         ("p.category_norm", category_norm), ("p.condition_norm", condition_norm)):
        if value is not None:
            where.append(field + "=?")
            params.append(value)
    rows = db.query("SELECT t.platform_fee_eur, t.payment_fee_eur, t.final_sale_price_eur "
                    "FROM transactions t LEFT JOIN purchases p ON p.purchase_pk=t.purchase_pk WHERE "
                    + " AND ".join(where), params)
    usable, platform_rates, payment_rates = [], [], []
    for row in rows:
        price = float(row["final_sale_price_eur"])
        platform_fee = row["platform_fee_eur"]
        payment_fee = row["payment_fee_eur"]
        if platform_fee is None and payment_fee is None:
            continue
        usable.append(row)
        if platform_fee is not None and math.isfinite(float(platform_fee)) and float(platform_fee) >= 0:
            platform_rates.append(float(platform_fee) / price)
        if payment_fee is not None and math.isfinite(float(payment_fee)) and float(payment_fee) >= 0:
            payment_rates.append(float(payment_fee) / price)
    report = {
        "n_sales_with_price": len(rows),
        "n_sales_with_any_fee": len(usable),
        "n_platform_fee_observations": len(platform_rates),
        "n_payment_fee_observations": len(payment_rates),
        "min_n_threshold": min_n,
        "platform_fee_rate": None,
        "payment_fee_rate": None,
        "status": "INSUFFICIENT DATA",
    }
    if len(usable) < min_n:
        report["note"] = (
            f"{len(usable)} sales with recorded fee data, below threshold {min_n}. "
            "Sale-side fee rates are unknown, so expected margins that depend on them "
            "are reported as unknown rather than estimated at a default."
        )
        return report
    report["status"] = "OK"
    if len(platform_rates) >= min_n:
        report["platform_fee_rate"] = round(percentiles(platform_rates)[0.50], 6)
        report["platform_fee_rate_n"] = len(platform_rates)
    if len(payment_rates) >= min_n:
        report["payment_fee_rate"] = round(percentiles(payment_rates)[0.50], 6)
        report["payment_fee_rate_n"] = len(payment_rates)
    if report["platform_fee_rate"] is None or report["payment_fee_rate"] is None:
        report["status"] = "PARTIAL"
        report["note"] = ("Sales clear the threshold but a fee side has insufficient observations; "
                          "margins depending on that side stay unknown.")
    return report


def expected_profit_estimate(acquisition_cost_eur, sale_price_eur, platform_fee_eur=None,
                              payment_fee_eur=None, buyer_fee_eur=None,
                              seller_shipping_cost_eur=None, packaging_cost_eur=None,
                              other_variable_cost_eur=None, tax_cost_eur=None,
                              inbound_shipping_cost_eur=None, repair_cost_eur=None,
                              labour_cost_eur=None, holding_cost_eur=None,
                              return_loss_reserve_eur=None, overhead_cost_eur=None):
    """Calculate expected profit given price and cost assumptions.

    Used by the buy/no-buy engine. Unknown costs result in None margin.
    """
    values = locals()
    result = economics.unit_economics(acquisition_cost_eur, sale_price_eur,
                                     {key: values[key] for key in economics.COST_FIELDS})
    if result["status"] != "COMPLETE":
        return None
    pct = result["net_margin_pct"]
    return {"gross_margin_eur": result["net_profit_eur"],
            "gross_margin_pct": round(pct, 6) if pct is not None else None}


def record_sale(source_listing_id, final_sale_price_eur, sold_listing_id=None,
                sold_at=None, platform="tori", platform_fee_eur=None,
                payment_fee_eur=None, seller_shipping_cost_eur=None,
                packaging_cost_eur=None, other_variable_cost_eur=None,
                tax_cost_eur=None, buyer_fee_eur=None, shipping_method=None,
                outcome_status="sold_confirmed", notes=None):
    """Close a purchase: record what it cleared for and why the margin may be null.

    This is the only writer of realised prices the project owns. Until it exists
    a purchase row can never be completed, so `realized_economics` can never
    return a margin and the moat stays a wish.

    Costs left as None stay None. A missing fee is unknown, not zero, and the
    margin is nulled rather than overstated -- the rule secondhand.pdf broke and
    this module documents at length.
    """
    if outcome_status not in ("sold_confirmed", "sold_inferred", "withdrawn", "expired"):
        raise ValueError(f"unsupported outcome_status {outcome_status!r}")
    if outcome_status in ("sold_confirmed", "sold_inferred") and final_sale_price_eur is None:
        raise ValueError(
            "a sale with no realised price is not a sale; record final_sale_price_eur "
            "or mark the outcome withdrawn/expired"
        )
    date_supplied = sold_at is not None
    if sold_at is None:
        sold_at = _utcnow()
    elif len(sold_at) == 10:
        sold_at = f"{sold_at}T00:00:00+00:00"

    conn = db.connect()
    try:
        with conn.transaction():
            purchase = conn.execute(
                "SELECT purchase_pk, acquisition_cost_eur, shipping_cost_eur, buyer_fee_eur FROM purchases "
                "WHERE platform = ? AND source_listing_id = ?",
                (platform, source_listing_id),
            ).fetchone()
            if purchase is None:
                raise ValueError(
                    f"no purchase recorded for {platform}/{source_listing_id}. "
                    "Record the purchase with 'buy' first; a sale cannot precede it."
                )
            purchase_pk = purchase["purchase_pk"] if hasattr(purchase, "keys") else purchase[0]
            # Share the physical-unit lock with publication dispatch. P4 will
            # replace this legacy sale path with order-linked stock transitions.
            suffix = " FOR UPDATE" if conn.dialect == "postgres" else ""
            owned = conn.execute("SELECT sku FROM inventory_units WHERE purchase_pk=?" + suffix,
                                 (purchase_pk,)).fetchone()
            if owned and conn.execute("SELECT 1 FROM financial_entries WHERE sku=? AND kind='write_off' AND order_id IS NULL", (owned[0],)).fetchone():
                raise ValueError("Written-off stock cannot be recorded as a legacy sale.")
            if owned and conn.execute("SELECT 1 FROM order_lines WHERE sku=?", (owned[0],)).fetchone():
                raise ValueError("This unit uses SKU-linked sales and returns. Record adjustments in its financial journal.")
            if owned and conn.execute("SELECT 1 FROM inventory_reservations WHERE sku=?", (owned[0],)).fetchone():
                raise ValueError("Owned stock is reserved by a channel request. Reconcile its listing before recording a manual sale.")
            acquisition = purchase["acquisition_cost_eur"] if hasattr(purchase, "keys") else purchase[1]

            costs = {
                "platform_fee_eur": platform_fee_eur,
                "payment_fee_eur": payment_fee_eur,
                "seller_shipping_cost_eur": seller_shipping_cost_eur,
                "packaging_cost_eur": packaging_cost_eur,
                "other_variable_cost_eur": other_variable_cost_eur,
                "tax_cost_eur": tax_cost_eur,
            }
            final_sale_price_eur = _money(final_sale_price_eur, "realized price")
            costs = {field: _money(value, field) for field, value in costs.items()}
            platform_fee_eur, payment_fee_eur = costs["platform_fee_eur"], costs["payment_fee_eur"]
            seller_shipping_cost_eur, packaging_cost_eur = costs["seller_shipping_cost_eur"], costs["packaging_cost_eur"]
            other_variable_cost_eur, tax_cost_eur = costs["other_variable_cost_eur"], costs["tax_cost_eur"]
            if buyer_fee_eur is not None:
                buyer_fee_eur = _money(buyer_fee_eur, "buyer fee")
                conn.execute("UPDATE purchases SET buyer_fee_eur=? WHERE purchase_pk=?",
                             (buyer_fee_eur, purchase_pk))
            else:
                buyer_fee_eur = purchase[3]
            margin, reason = _margin({
                "final_sale_price_eur": final_sale_price_eur,
                "acquisition_cost_eur": acquisition,
                "inbound_shipping_cost_eur": purchase[2],
                "buyer_fee_eur": buyer_fee_eur,
                **costs,
            })
            if outcome_status not in ("sold_confirmed", "sold_inferred"):
                margin, reason = None, "not_applicable"

            sold_listing_pk = None
            if sold_listing_id:
                row = conn.execute(
                    "SELECT listing_pk FROM listings WHERE platform = ? AND listing_id = ?",
                    (platform, sold_listing_id),
                ).fetchone()
                if row is not None:
                    sold_listing_pk = row["listing_pk"] if hasattr(row, "keys") else row[0]

            # One sale per purchase. Keying on the sale timestamp made every
            # re-record a new transaction, so correcting a typo produced a second
            # sale and the margin summary counted the same item twice.
            transaction_id = f"{platform}:purchase:{purchase_pk}"
            if not date_supplied:
                previous = conn.execute(
                    "SELECT sale_at FROM transactions WHERE platform=? AND platform_transaction_id=?",
                    (platform, transaction_id)).fetchone()
                if previous:
                    sold_at = previous[0]
            from . import realization
            previous = conn.execute("SELECT resale_pairing_json FROM transactions WHERE platform=? AND platform_transaction_id=?", (platform, transaction_id)).fetchone()
            # Corrections retain the original pairing, including an unknown ask.
            pairing = previous[0] if previous else json.dumps(realization.freeze_resale_pairing(conn, platform, sold_listing_id, sold_at), sort_keys=True)
            conn.execute(
                """INSERT INTO transactions
                       (purchase_pk, listing_pk, platform, platform_transaction_id,
                        sale_at, final_sale_price_eur,
                        platform_fee_eur, payment_fee_eur, seller_shipping_cost_eur,
                        packaging_cost_eur, other_variable_cost_eur, tax_cost_eur,
                        gross_margin_eur, gross_margin_pct, margin_known,
                        margin_missing_reason, shipping_method, outcome_status,
                        observed_at, resale_pairing_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (platform, platform_transaction_id) DO UPDATE SET
                       listing_pk = excluded.listing_pk,
                       shipping_method = excluded.shipping_method,
                       sale_at = excluded.sale_at,
                       final_sale_price_eur = excluded.final_sale_price_eur,
                       platform_fee_eur = excluded.platform_fee_eur,
                       payment_fee_eur = excluded.payment_fee_eur,
                       seller_shipping_cost_eur = excluded.seller_shipping_cost_eur,
                       packaging_cost_eur = excluded.packaging_cost_eur,
                       other_variable_cost_eur = excluded.other_variable_cost_eur,
                       tax_cost_eur = excluded.tax_cost_eur,
                       gross_margin_eur = excluded.gross_margin_eur,
                       gross_margin_pct = excluded.gross_margin_pct,
                       margin_known = excluded.margin_known,
                       margin_missing_reason = excluded.margin_missing_reason,
                       outcome_status = excluded.outcome_status,
                       observed_at = excluded.observed_at""",
                (purchase_pk, sold_listing_pk, platform, transaction_id, sold_at,
                 final_sale_price_eur,
                 platform_fee_eur, payment_fee_eur, seller_shipping_cost_eur,
                 packaging_cost_eur, other_variable_cost_eur, tax_cost_eur,
                 margin["gross_margin_eur"] if margin else None,
                 margin["gross_margin_pct"] if margin else None,
                 bool(margin), reason, shipping_method, outcome_status,
                 _utcnow(), pairing),
            )

            conn.execute(
                "UPDATE purchases SET sold_listing_pk = ?, notes = COALESCE(?, notes) "
                "WHERE purchase_pk = ?",
                (sold_listing_pk, notes, purchase_pk),
            )
            return {
                "purchase_pk": purchase_pk,
                "transaction_id": transaction_id,
                "final_sale_price_eur": final_sale_price_eur,
                "margin": margin,
                "margin_missing_reason": reason,
                "sold_listing_pk": sold_listing_pk,
                "outcome_status": outcome_status,
            }
    finally:
        conn.close()
