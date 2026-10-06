"""Expiring structural counts from normal requests, with no page diagnostics."""

import logging
from datetime import datetime, timedelta, timezone

from . import config, db

VERSION = "parser-checks-v1"


def failure(platform, source, parser="unknown", result="parse_error", raw_count=0):
    # A diagnostic failure must not mask a refusal or change its stop behavior.
    try:
        record(platform, source, parser, result, raw_count=raw_count)
    except Exception as exc:
        logging.getLogger("toriresearch.parser_health").warning("diagnostic write failed: %s", type(exc).__name__)


def record(platform, source, parser, result="ok", records=(), raw_count=None, missing_id=None, conn=None, reference=None):
    if source not in ("search", "itempage") or parser not in ("docs", "itemlist", "hydration", "http404", "unknown") or result not in ("ok", "parse_error", "refusal"):
        raise ValueError("Unsupported parser diagnostic class")
    reference = reference or datetime.now(timezone.utc)
    raw_count = len(records) if raw_count is None else max(0, int(raw_count))
    counts = [1, raw_count, len(records), max(0, raw_count-len(records)) if missing_id is None else missing_id,
              sum(not r.get("title") for r in records),
              sum(r.get("price", r.get("listed_price_original")) is None for r in records),
              sum(not r.get("category_raw_id", r.get("category_path")) for r in records)]
    owns = conn is None
    conn = conn or db.connect()
    try:
        conn.execute("INSERT INTO parser_checks (day,platform,source,parser,result,checks,raw_rows,parsed_rows,missing_id,missing_title,missing_price,missing_category) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day,platform,source,parser,result) DO UPDATE SET "
                     "checks=parser_checks.checks+excluded.checks,raw_rows=parser_checks.raw_rows+excluded.raw_rows, "
                     "parsed_rows=parser_checks.parsed_rows+excluded.parsed_rows,missing_id=parser_checks.missing_id+excluded.missing_id, "
                     "missing_title=parser_checks.missing_title+excluded.missing_title,missing_price=parser_checks.missing_price+excluded.missing_price, "
                     "missing_category=parser_checks.missing_category+excluded.missing_category",
                     [reference.astimezone(timezone.utc).date().isoformat(), platform, source, parser, result, *counts])
        expire(conn, reference)
    finally:
        if owns:
            conn.close()


def expire(conn=None, reference=None):
    reference = reference or datetime.now(timezone.utc)
    cutoff = (reference.astimezone(timezone.utc).date()-timedelta(days=config.PARSER_DIAGNOSTIC_DAYS-1)).isoformat()
    if conn is None:
        return db.execute("DELETE FROM parser_checks WHERE day<?", (cutoff,))
    return conn.execute("DELETE FROM parser_checks WHERE day<?", (cutoff,)).rowcount


def report(reference=None):
    reference = reference or datetime.now(timezone.utc)
    cutoff = (reference.astimezone(timezone.utc).date()-timedelta(days=config.PARSER_DIAGNOSTIC_DAYS-1)).isoformat()
    rows = db.query("SELECT * FROM parser_checks WHERE day>=? ORDER BY day,source,parser,result", (cutoff,))
    totals = {field:sum(r[field] for r in rows) for field in
              ("checks", "raw_rows", "parsed_rows", "missing_id", "missing_title", "missing_price", "missing_category")}
    successful_search = sum(r["checks"] for r in rows if r["source"] == "search" and r["result"] == "ok")
    fallback = sum(r["checks"] for r in rows if r["source"] == "search" and r["parser"] == "itemlist" and r["result"] == "ok")
    return {"version": VERSION, "retention_days": config.PARSER_DIAGNOSTIC_DAYS, "totals": totals,
            "parse_errors": sum(r["checks"] for r in rows if r["result"] == "parse_error"),
            "refusals": sum(r["checks"] for r in rows if r["result"] == "refusal"),
            "search_fallback_share": fallback/successful_search if successful_search else None,
            "missing_id_rate": totals["missing_id"]/totals["raw_rows"] if totals["raw_rows"] else None,
            "null_rates": {field:totals[field]/totals["parsed_rows"] if totals["parsed_rows"] else None
                           for field in ("missing_title", "missing_price", "missing_category")},
            "by_day": rows,
            "note": "Counts describe authorized requests, not fixture replays. Missing fields remain unknown. No listing IDs, URLs, titles, coordinates, page bodies or model responses are stored in diagnostics."}
