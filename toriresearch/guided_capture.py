"""Bounded manual sold aggregates with immutable, replayable save receipts."""

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from . import db, inventory, soldextract

VERSION = "sold-capture-v1"
FIELDS = ["captured_at", "source", "platform", "keyword", "category_norm",
          "condition_norm", "query_url", "window_start", "window_end", "n_sold",
          "avg_sold_price_eur", "sold_price_min_eur", "sold_price_max_eur",
          "sell_through_rate", "avg_shipping_eur", "captured_by", "notes"]
CONDITIONS = {"new_unused", "like_new", "very_good", "good", "fair", "poor",
              "for_parts", "refurbished", "unknown"}


def _receipt(row):
    return {key: float(value) if isinstance(value, Decimal) else
            value.isoformat() if isinstance(value, (date, datetime)) else value
            for key, value in dict(row).items() if key in FIELDS or key == "sold_obs_pk"}


def recent(limit=20):
    """Retained receipts only; no item linkage or transaction attribution."""
    return [_receipt(row) for row in db.query(
        "SELECT * FROM sold_observations ORDER BY captured_at DESC,sold_obs_pk DESC LIMIT ?", (limit,))]


def capture(params):
    """Store one explicit aggregate, or return the exact existing receipt.

    The existing (platform, keyword, captured_at) key identifies a capture.
    Changed facts at that key conflict, including after a lost HTTP response.
    This path cannot write listing labels, owned outcomes or model approvals.
    """
    if not isinstance(params, dict) or set(params) - set(FIELDS):
        raise ValueError("provide only sold aggregate fields")
    values = dict(params)
    if values.get("source") != soldextract.SOURCE or values.get("platform") != "ebay":
        raise ValueError("select eBay Product Research as the capture source")
    for key, limit in (("keyword", 120), ("category_norm", 120), ("condition_norm", 32),
                       ("query_url", 2000), ("captured_by", 64), ("notes", 2000)):
        values[key] = inventory._text(values.get(key, ""), key, limit, key != "notes")
    if values["condition_norm"] not in CONDITIONS:
        raise ValueError("select the captured condition filter")
    try:
        url = urlsplit(values["query_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError
    except ValueError:
        raise ValueError("query_url must be an HTTP(S) source URL without credentials") from None
    try:
        stamp = datetime.fromisoformat(values.get("captured_at", "").replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
            raise ValueError
        window = [date.fromisoformat(values.get(key, "")) for key in ("window_start", "window_end")]
        if not window[0] <= window[1] <= stamp.date():
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ValueError("record an explicit capture time with offset and a completed observation window") from None
    values["captured_at"] = stamp.astimezone(timezone.utc).isoformat()
    if isinstance(values.get("n_sold"), bool):
        raise ValueError("n_sold must be 1..10000000")
    count = inventory._integer(values.get("n_sold"), "sample size")
    if count > 10_000_000:
        raise ValueError("n_sold must be 1..10000000")
    values["n_sold"] = count
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=soldextract.CAPTURE_TEMPLATE_FIELDS)
    writer.writeheader()
    writer.writerow({key: values.get(key) for key in soldextract.CAPTURE_TEMPLATE_FIELDS})
    record = soldextract._capture_records(output.getvalue(), values["captured_by"])[0]
    expected = dict(zip(FIELDS, record))
    for key in ("avg_sold_price_eur", "sold_price_min_eur", "sold_price_max_eur", "avg_shipping_eur"):
        number = expected[key]
        if number is not None and number > 1_000_000_000:
            raise ValueError(key + " exceeds the capture bound")
    rate = expected["sell_through_rate"]
    if rate is not None and round(rate, 4) != rate:
        raise ValueError("sell_through_rate supports at most four decimal places")
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            cur = conn.execute(
                "INSERT INTO sold_observations (" + ",".join(FIELDS) + ") VALUES (" +
                ",".join("?" for _ in FIELDS) + ") "
                "ON CONFLICT (platform,keyword,captured_at) DO NOTHING", record)
            row = conn.execute("SELECT * FROM sold_observations WHERE platform=? AND keyword=? AND captured_at=?",
                               ("ebay", expected["keyword"], expected["captured_at"])).fetchone()
            receipt = _receipt(row)
            if any(receipt[key] != value for key, value in expected.items()):
                raise inventory.Conflict("This capture time and keyword already identify different recorded facts. Review the retained receipt.")
            return dict(ok=True, version=VERSION, replayed=cur.rowcount == 0, receipt=receipt)
    finally:
        conn.close()
