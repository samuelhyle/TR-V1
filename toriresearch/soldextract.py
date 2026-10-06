"""Realized prices, the permission-free way.

The blocker is that nobody publishes what Finnish second-hand goods clear for.
But eBay's seller-side Product Research view publishes exactly that, free, for
eBay: three years of sold data, with average sold price, sold price range,
sell-through rate, and -- critically -- the ACTUAL price paid even when Best
Offer was accepted. eBay's own help page draws this contrast against what the
API exposes (90 days, last-sale-only, no Best Offer).

It has no API. So this module supports human capture for a bounded keyword list.
That is not a compromise; for aggregate parameters it is the higher-quality
route by eBay's own documentation, and it carries no ToS question at all.

Captures describe eBay sold aggregates. A realization ratio additionally needs
a matched asking-price cohort and time window; this module does not have that
pairing and never manufactures a ratio from a sold average alone.
"""

import csv
import io
import logging
import math
from datetime import date, datetime, timezone

from . import db

log = logging.getLogger("toriresearch.sold")

CAPTURE_TEMPLATE_FIELDS = [
    "captured_at", "platform", "keyword", "category_norm", "condition_norm",
    "query_url", "window_start", "window_end", "n_sold",
    "avg_sold_price_eur", "sold_price_min_eur", "sold_price_max_eur",
    "sell_through_rate", "avg_shipping_eur", "notes",
]

SOURCE = "ebay.product_research.ui"


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def template_csv():
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CAPTURE_TEMPLATE_FIELDS)
    writer.writeheader()
    writer.writerow({
        "captured_at": "2026-10-08T10:00:00+00:00",
        "platform": "ebay",
        "keyword": "iphone 15 128gb",
        "category_norm": "Phones",
        "condition_norm": "very_good",
        "query_url": "https://www.ebay.com/sch/i.html?_nkw=iphone+15+128gb&_sacat=9355",
        "window_start": "2025-09-08",
        "window_end": "2026-10-08",
        "n_sold": "",
        "avg_sold_price_eur": "",
        "sold_price_min_eur": "",
        "sold_price_max_eur": "",
        "sell_through_rate": "",
        "avg_shipping_eur": "",
        "notes": "Replace example query and dates, then enter observed aggregates; rates are fractions (0–1)",
    })
    return buffer.getvalue()


def _capture_records(csv_text, captured_by=None):
    """Validate a complete file before either capture path writes anything."""
    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"keyword", "platform", "avg_sold_price_eur"}
    fields = set(reader.fieldnames or [])
    missing = required - fields
    if missing:
        raise ValueError(f"capture CSV missing required columns: {sorted(missing)}")

    from .metrics import parse_ts

    def values(row, line):
        keyword = (row.get("keyword") or "").strip()
        if not keyword:
            return None
        if (row.get("platform") or "").strip() != "ebay":
            raise ValueError(f"capture row {line}: Product Research platform must be ebay")
        raw_capture = (row.get("captured_at") or "").strip() or captured_default
        stamp = parse_ts(raw_capture)
        if stamp is None:
            raise ValueError(f"capture row {line}: invalid captured_at")
        captured = stamp.astimezone(timezone.utc).isoformat()

        def num(key):
            raw = (row.get(key) or "").strip().replace(",", ".").replace(" ", "")
            if not raw:
                return None
            try:
                number = float(raw)
            except ValueError:
                raise ValueError(f"capture row {line}: invalid {key}") from None
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"capture row {line}: {key} must be finite and nonnegative")
            return number

        numbers = {key: num(key) for key in (
            "n_sold", "avg_sold_price_eur", "sold_price_min_eur", "sold_price_max_eur",
            "sell_through_rate", "avg_shipping_eur")}
        count, average = numbers["n_sold"], numbers["avg_sold_price_eur"]
        if average is None:
            raise ValueError(f"capture row {line}: avg_sold_price_eur is required")
        if count is not None and not count.is_integer():
            raise ValueError(f"capture row {line}: n_sold must be an integer")
        rate = numbers["sell_through_rate"]
        if rate is not None and rate > 1:
            raise ValueError(f"capture row {line}: sell_through_rate must be a fraction between 0 and 1")
        low, high = numbers["sold_price_min_eur"], numbers["sold_price_max_eur"]
        if (low is not None and low > average) or (high is not None and high < average):
            raise ValueError(f"capture row {line}: average must fall within the sold price range")
        windows = []
        for key in ("window_start", "window_end"):
            raw = (row.get(key) or "").strip()
            try:
                windows.append(date.fromisoformat(raw).isoformat() if raw else None)
            except ValueError:
                raise ValueError(f"capture row {line}: invalid {key}") from None
        if all(windows) and windows[0] > windows[1]:
            raise ValueError(f"capture row {line}: window_end precedes window_start")
        return (
            captured, SOURCE, "ebay", keyword,
            (row.get("category_norm") or "").strip() or None,
            (row.get("condition_norm") or "").strip() or None,
            (row.get("query_url") or "").strip() or None, *windows,
            int(count) if count is not None else None, average, low, high, rate,
            numbers["avg_shipping_eur"], captured_by, (row.get("notes") or "").strip() or None)

    captured_default = _utcnow()
    records = []
    for row in reader:
        record = values(row, reader.line_num)
        if record is not None:
            records.append(record)
    return records


def import_capture(csv_text, captured_by=None):
    """Import manually captured Product Research aggregates."""
    records = _capture_records(csv_text, captured_by)
    imported = 0
    conn = db.connect()
    try:
        with conn.transaction():
            for record in records:
                cur = conn.execute(
                    """INSERT INTO sold_observations
                           (captured_at, source, platform, keyword, category_norm, condition_norm,
                            query_url, window_start, window_end, n_sold, avg_sold_price_eur,
                            sold_price_min_eur, sold_price_max_eur, sell_through_rate,
                            avg_shipping_eur, captured_by, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (platform, keyword, captured_at) DO NOTHING""", record)
                imported += cur.rowcount
    finally:
        conn.close()
    log.info("imported %d sold observations", imported)
    return {"imported": imported, "duplicates": len(records) - imported}


def _obs(where=None, params=None, min_n=None):
    sql = "SELECT * FROM sold_observations WHERE avg_sold_price_eur IS NOT NULL"
    if where:
        sql += f" AND {where}"
    rows = db.query(sql, params)
    if min_n is not None and len(rows) < min_n:
        return None
    return rows


def realization_ratio(keyword=None, platform="ebay", min_obs=3):
    """Supported capture summary; a ratio awaits a matched asking-price cohort."""
    from .metrics import percentiles, suppress

    sold = _obs("platform = ?", (platform,))
    if keyword:
        sold = [r for r in sold if r["keyword"] == keyword]
    if not sold:
        return {
            "status": "NO SOLD DATA",
            "note": "No Product Research captures recorded. Run "
                    "'soldextract template' and capture a bounded keyword list.",
            "min_obs_required": min_obs,
        }

    n_sold = sum(r["n_sold"] or 0 for r in sold)
    avgs = [float(r["avg_sold_price_eur"]) for r in sold]

    report = {
        "platform": platform,
        "keyword": keyword,
        "sold_observations": len(sold),
        "underlying_transactions_n_sold": n_sold,
        "min_obs_required": min_obs,
        "realization_ratio": None,
        "ratio_status": "NO MATCHED ASK DATA",
    }
    if suppress(len(sold), min_obs):
        report["status"] = "INSUFFICIENT DATA"
        report["note"] = f"{len(sold)} capture(s), below {min_obs}. No price estimate reported."
        return report
    report.update({
        "median_avg_sold_price_eur": round(percentiles(avgs)[0.50], 2),
        "sold_price_min_eur": round(min(r["sold_price_min_eur"] for r in sold
                                        if r["sold_price_min_eur"] is not None), 2)
        if any(r["sold_price_min_eur"] is not None for r in sold) else None,
        "sold_price_max_eur": round(max(r["sold_price_max_eur"] for r in sold
                                        if r["sold_price_max_eur"] is not None), 2)
        if any(r["sold_price_max_eur"] is not None for r in sold) else None,
    })
    rates = [float(r["sell_through_rate"]) for r in sold if r["sell_through_rate"] is not None]
    if rates:
        report["median_sell_through_rate"] = round(percentiles(rates)[0.50], 4)

    report["status"] = "OK"
    report["confidence"] = (
        "Aggregate, keyword-level, from an official seller-side view. Applies to "
        "eBay sold stock, not to Finnish classifieds. Transfer to Tori is an "
        "assumption, not a measurement, and must be reported as such."
    )
    return report


def estimate_realized(median_ask_eur, ratio, ratio_ci=None):
    """Apply a realization ratio to an ask median. Always carries the interval."""
    if median_ask_eur is None or not ratio:
        return None
    point = median_ask_eur * ratio
    out = {
        "median_ask_eur": round(median_ask_eur, 2),
        "realization_ratio": round(ratio, 4),
        "estimated_realized_eur": round(point, 2),
        "status": "ESTIMATE",
        "caveat": (
            "Derived from an ask median times a realization ratio transferred from a "
            "different market. Not an observed clearing price. Do not present as one."
        ),
    }
    if ratio_ci:
        out["estimated_realized_range_eur"] = [
            round(median_ask_eur * ratio_ci["lo"], 2),
            round(median_ask_eur * ratio_ci["hi"], 2),
        ]
    return out


def summary():
    rows = db.query("SELECT * FROM sold_observations")
    return {
        "captures": len(rows),
        "keywords": len({r["keyword"] for r in rows}),
        "transactions_represented": sum(r["n_sold"] or 0 for r in rows),
        "keywords_with_sell_through": len({r["keyword"] for r in rows
                                           if r["sell_through_rate"] is not None}),
        "status": "OK" if len(rows) else "EMPTY",
    }
