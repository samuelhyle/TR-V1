"""Coarse dated research summaries, independently of source access.

Only today's market snapshot and open admission cohorts are reconciled. Missing
market days are never reconstructed from today's listing state. The fixed
dimensions are disjoint, counts are rounded/suppressed, prices are broad bands,
and seller keys exist only in temporary counters. These controls reduce disclosure
risk, but do not establish formal anonymization or representative market coverage.
"""

import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import collection, config, db, metrics
from .fileio import write_text
from .ingest import category

VERSION = "daily-history-v1"
MIN_GROUP = 50  # Separate from the statistical TORI_MIN_N setting.
ROUND_TO = 10
COHORT_OPEN_DAYS = 37
BATCH_SIZE = 1000
PRICE_EDGES = (10, 25, 50, 100, 200, 400, 800, 1600, 3200)
PRICE_BANDS = ("0–10", "10–25", "25–50", "50–100", "100–200", "200–400",
               "400–800", "800–1600", "1600–3200", "3200+")
OUTCOMES = ("sold_confirmed", "sold_inferred", "withdrawn", "expired", "active", "unknown")
NOTE = ("Observed collection sample, not market coverage. Counts are rounded down to tens; "
        "positive counts below 50 are suppressed. Asking-price bands are not realized sale prices. "
        "Admission cohorts describe evidence through their cutoff, not a sale probability. "
        "They use first admission; priority and sampled admissions remain separate. Latest retained evidence "
        "can be incomplete after missed runs, so unknown outcomes remain explicit. "
        "Do not sum retained-listing snapshots or average daily medians into an all-time estimate. "
        "Purged and rediscovered identities cannot be counted as exact all-time unique listings. "
        "Coarsening and suppression reduce disclosure risk; they are not proof of formal anonymization.")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _reference(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _count(n):
    return 0 if n == 0 else (n // ROUND_TO) * ROUND_TO if n >= MIN_GROUP else None


def _policy_version():
    from . import collection_health
    value = {"version": VERSION, "collection_policy": collection_health.context()["policy_key"],
             "fresh_hours": config.PANEL_MAX_AGE_HOURS, "listing_retention_days": config.LISTING_RETENTION_DAYS}
    return VERSION + "-" + hashlib.sha256(_json(value).encode()).hexdigest()[:12]


def _context(conn):
    # Scope fingerprints describe configuration, never identities or locations
    # of individual listings. Exact municipality settings are not exported.
    scope_key = hashlib.sha256(_json({"categories": sorted(config.FOCUS_CATEGORIES),
                                     "municipalities": sorted(config.FOCUS_MUNICIPALITIES)}).encode()).hexdigest()
    roots, labels = {}, {}
    for row in _rows(conn, "SELECT platform_category_path, platform_category_id, platform_label "
                    "FROM dim_category WHERE platform='tori' AND valid_to IS NULL ORDER BY valid_from"):
        path = str(row["platform_category_path"] or row["platform_category_id"])
        if re.fullmatch(r"0\.\d+", path):
            code = path.split(".")[1]
            roots[code] = "tori:" + code
            if row["platform_label"]:
                labels[row["platform_label"].strip()] = "tori:" + code
    for path in config.FOCUS_CATEGORIES:
        if re.fullmatch(r"[012]\.\d+(?:\.\d+)*", path):
            code = path.split(".")[1]
            roots.setdefault(code, "tori:" + code)
    revision = hashlib.sha256(_json([roots, labels]).encode()).hexdigest()[:12]
    return scope_key, category.TAXONOMY_VERSION + "-" + revision, roots, labels, _policy_version()


def _dimensions(row, roots, labels):
    raw = str(row.get("category_raw_id") or "")
    vertical = "unresolved"
    if re.fullmatch(r"[012]\.\d+(?:\.\d+)*", raw):
        vertical = roots.get(raw.split(".")[1], vertical)
    if vertical == "unresolved":
        vertical = labels.get((row.get("category_root_norm") or "").strip(), vertical)
    condition = row.get("condition_norm")
    condition = ("near_new" if condition in {"new_unused", "like_new", "refurbished"} else
                 "used" if condition in {"very_good", "good", "fair"} else
                 "parts" if condition in {"poor", "for_parts"} else "unknown")
    segment = row.get("subplatform") if row.get("subplatform") in {"standard", "diili"} else "other"
    return vertical, segment, condition


class _Cell:
    def __init__(self):
        self.n = 0
        self.counts = Counter({"outcome_" + outcome: 0 for outcome in OUTCOMES})
        self.prices = Counter()
        self.sellers = Counter()
        self.price_n = 0
        self.price_seller_n = 0

    def add_price(self, row):
        value = row.get("listed_price_eur")
        try:
            value = float(value)
        except (ValueError, TypeError):
            return
        if not math.isfinite(value) or value <= 0:
            return
        band = next((i for i, edge in enumerate(PRICE_EDGES) if value < edge), len(PRICE_EDGES))
        self.prices[band] += 1
        self.price_n += 1
        if row.get("seller_id"):
            self.sellers[row["seller_id"]] += 1
            self.price_seller_n += 1

    def release(self, dimensions, mode=None):
        if self.n < MIN_GROUP:
            return None
        result = {"vertical": dimensions[0], "market_segment": dimensions[1],
                  "condition": dimensions[2], "geography": "FI", "sample_n": _count(self.n),
                  "counts": {key: _count(value) for key, value in sorted(self.counts.items())}}
        if mode:
            result["admission_mode"] = mode
            return result
        # Prices need support from multiple observed sellers as well as listings.
        # Unknown sellers cannot certify diversity. No seller key is persisted.
        supported = (self.price_n >= MIN_GROUP and len(self.sellers) >= 20 and
                     self.price_seller_n >= self.price_n * .8 and
                     max(self.sellers.values(), default=0) <= self.price_n * .1)
        result["asking_prices"] = {"sample_n": _count(self.price_n),
                                    "status": "supported" if supported else "suppressed"}
        if supported:
            bands = [{"band_eur": band, "n": _count(self.prices[i])}
                     for i, band in enumerate(PRICE_BANDS)]
            quantiles = {}
            for name, fraction in (("p25", .25), ("median", .5), ("p75", .75)):
                cumulative = 0
                for i in range(len(PRICE_BANDS)):
                    cumulative += self.prices[i]
                    if cumulative >= self.price_n * fraction:
                        quantiles[name + "_band_eur"] = PRICE_BANDS[i] if self.prices[i] >= MIN_GROUP else None
                        break
            result["asking_prices"].update(bands=bands, **quantiles)
        return result


def _watermark(conn):
    return {table: dict(zip(("rows", "latest_at"), conn.execute(
        f"SELECT COUNT(*), MAX({column}) FROM {table}").fetchone()))
        for table, column in (("listings", "source_observed_at"), ("events", "observed_at"),
                              ("followup_enrollments", "enrolled_at"))}


def _write(conn, kind, day, scope, taxonomy, policy, reference, watermark, cells, state="open", cutoff=None):
    watermark = {key: {"rows": value["rows"],
                      "latest_at": metrics.parse_ts(value["latest_at"]).isoformat() if value["latest_at"] else None}
                 for key, value in watermark.items()}
    conn.execute("INSERT INTO aggregate_partitions (kind,period_day,scope_key,policy_version,taxonomy_version,"
                 "state,captured_at,freeze_cutoff,source_watermark_json,aggregates_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(kind,period_day,scope_key,policy_version,taxonomy_version) DO UPDATE SET "
                 "state=excluded.state,captured_at=excluded.captured_at,freeze_cutoff=excluded.freeze_cutoff,"
                 "source_watermark_json=excluded.source_watermark_json,aggregates_json=excluded.aggregates_json "
                 "WHERE aggregate_partitions.state='open'",
                 (kind, day, scope, policy, taxonomy, state, reference.isoformat(),
                  cutoff.isoformat() if cutoff else None, _json(watermark), _json(cells)))


def materialize(conn, reference=None):
    """Reconcile in the caller's transaction, before any evidence is deleted."""
    reference = _reference(reference)
    day = reference.date().isoformat()
    start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    scope, taxonomy, roots, labels, policy = _context(conn)
    # A changed collection scope closes its old series rather than rewriting it.
    conn.execute("UPDATE aggregate_partitions SET state='frozen',freeze_cutoff=captured_at "
                 "WHERE state='open' AND (scope_key<>? OR policy_version<>? OR taxonomy_version<>?)",
                 (scope, policy, taxonomy))
    conn.execute("UPDATE aggregate_partitions SET state='frozen',freeze_cutoff=captured_at "
                 "WHERE kind='market' AND state='open' AND period_day<?", (day,))
    watermark = _watermark(conn)
    scope_match = collection.Scope()
    clauses, params = scope_match.conditions()
    # Scope's expressions are unqualified and unambiguous: joined tables below
    # never contain category or municipality columns.
    where = " AND ".join(["l.platform='tori'", *clauses])
    cells = defaultdict(_Cell)
    last_pk = 0
    fresh_cutoff = reference - timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    retention_cutoff = reference - timedelta(days=config.LISTING_RETENTION_DAYS)
    fields = ("l.listing_pk,l.subplatform,l.category_raw_id,l.category_root_norm,l.condition_norm,"
              "l.seller_id,l.listed_price_eur,l.price_observed_at,l.first_seen_at,l.last_seen_at,"
              "l.status_norm,l.coverage,l.observation_coverage_end,l.is_reserved,l.is_giveaway,"
              "l.outcome_status,l.outcome_observed_at")
    while True:
        rows = _rows(conn, f"SELECT {fields},"
                     "(SELECT COUNT(*) FROM events e WHERE e.listing_pk=l.listing_pk AND e.observed_at>=? "
                     "AND e.observed_at<=?) AS event_rows,"
                     "(SELECT COUNT(*) FROM events e WHERE e.listing_pk=l.listing_pk AND e.event_type='price_changed' "
                     "AND e.observed_at>=? AND e.observed_at<=?) AS price_changes "
                     f"FROM listings l WHERE {where} AND l.listing_pk>? ORDER BY l.listing_pk LIMIT ?",
                     [start.isoformat(), reference.isoformat(), start.isoformat(), reference.isoformat(),
                      *params, last_pk, BATCH_SIZE])
        if not rows:
            break
        for row in rows:
            last_pk = row["listing_pk"]
            seen = metrics.parse_ts(row["last_seen_at"])
            first = metrics.parse_ts(row["first_seen_at"])
            if not seen or seen < retention_cutoff or not first or first > reference or seen > reference:
                continue
            cell = cells[_dimensions(row, roots, labels)]
            cell.n += 1
            cell.counts["retained_listings"] += 1
            cell.counts["discovered_today"] += int(start <= first <= reference)
            cell.counts["observed_listings_today"] += int(start <= seen <= reference)
            cell.counts["recorded_event_rows_today"] += row["event_rows"]
            cell.counts["price_change_events_today"] += row["price_changes"]
            cell.counts["price_changed_listings_today"] += int(row["price_changes"] > 0)
            outcome = row["outcome_status"] if row["outcome_status"] in OUTCOMES else "unknown"
            terminal_at = metrics.parse_ts(row["outcome_observed_at"])
            if outcome not in {"active", "unknown"} and (not terminal_at or terminal_at > reference):
                outcome = "unknown"
            cell.counts["outcome_" + outcome] += 1
            cell.counts["gone_uncertain"] += int(row["coverage"] == "gone_uncertain")
            available = (seen >= fresh_cutoff and row["status_norm"] == "active" and
                         row["coverage"] == "collecting" and not row["is_reserved"] and not row["is_giveaway"])
            cell.counts["fresh_available_listings"] += int(available)
            price_at = metrics.parse_ts(row["price_observed_at"] or row["last_seen_at"])
            if available and price_at and fresh_cutoff <= price_at <= reference:
                cell.add_price(row)
        if len(rows) < BATCH_SIZE:
            break
    released = [value for dimensions, cell in sorted(cells.items())
                if (value := cell.release(dimensions)) is not None]
    _write(conn, "market", day, scope, taxonomy, policy, reference, watermark, released)

    existing = _rows(conn, "SELECT period_day FROM aggregate_partitions WHERE kind='cohort' AND state='open' "
                     "AND scope_key=? AND policy_version=? AND taxonomy_version=?", (scope, policy, taxonomy))
    oldest_day = min([row["period_day"] for row in existing] +
                     [(start - timedelta(days=COHORT_OPEN_DAYS)).date().isoformat()])
    frozen = {row["period_day"] for row in _rows(conn,
              "SELECT period_day FROM aggregate_partitions WHERE kind='cohort' AND state='frozen' "
              "AND scope_key=? AND policy_version=? AND taxonomy_version=? AND period_day>=?",
              (scope, policy, taxonomy, oldest_day))}
    cohorts = defaultdict(lambda: defaultdict(_Cell))
    expires = {}
    enrollment_cursor = (oldest_day + "T00:00:00+00:00", "")
    while True:
        rows = _rows(conn, f"SELECT {fields},l.last_probed_at,t.last_success_at,e.enrollment_id,e.mode,e.enrolled_at "
                     "FROM followup_enrollments e JOIN listings l ON l.listing_pk=e.listing_pk "
                     "LEFT JOIN followup_tasks t ON t.listing_pk=l.listing_pk "
                     f"WHERE {where} AND e.enrolled_at>=? AND e.enrolled_at<=? "
                     "AND (e.enrolled_at>? OR (e.enrolled_at=? AND e.enrollment_id>?)) "
                     "AND NOT EXISTS (SELECT 1 FROM followup_enrollments prior WHERE prior.listing_pk=e.listing_pk "
                     "AND (prior.enrolled_at<e.enrolled_at OR (prior.enrolled_at=e.enrolled_at "
                     "AND prior.enrollment_id<e.enrollment_id))) "
                     "ORDER BY e.enrolled_at,e.enrollment_id LIMIT ?",
                     [*params, oldest_day+"T00:00:00+00:00", reference.isoformat(),
                      enrollment_cursor[0], enrollment_cursor[0], enrollment_cursor[1], BATCH_SIZE])
        if not rows:
            break
        for row in rows:
            enrollment_cursor = (row["enrolled_at"], row["enrollment_id"])
            enrolled = metrics.parse_ts(row["enrolled_at"])
            cohort_day = enrolled.date().isoformat()
            if cohort_day in frozen:
                continue
            dimension = (*_dimensions(row, roots, labels), row["mode"])
            cell = cohorts[cohort_day][dimension]
            cell.n += 1
            cutoff = min(reference, enrolled.replace(hour=0, minute=0, second=0, microsecond=0) +
                         timedelta(days=COHORT_OPEN_DAYS+1))
            success = metrics.parse_ts(row["last_success_at"])
            cell.counts["successful_followup"] += int(bool(success and success <= cutoff))
            cell.counts["followup_evidence_after_cutoff"] += int(bool(success and success > cutoff))
            terminal_at = metrics.parse_ts(row["outcome_observed_at"])
            outcome = row["outcome_status"] if row["outcome_status"] in OUTCOMES else "unknown"
            if outcome not in {"active", "unknown"} and (not terminal_at or terminal_at > cutoff):
                outcome = "unknown"
            seen = metrics.parse_ts(row["last_seen_at"])
            if outcome == "active" and seen and seen > cutoff:
                outcome = "unknown"
            cell.counts["outcome_" + outcome] += 1
            coverage_end = metrics.parse_ts(row["observation_coverage_end"])
            cell.counts["gone_uncertain"] += int(row["coverage"] == "gone_uncertain" and
                                                 bool(coverage_end and coverage_end <= cutoff))
            if seen:
                expires[cohort_day] = min(expires.get(cohort_day, seen), seen)
        if len(rows) < BATCH_SIZE:
            break
    for cohort_day in sorted(set(cohorts) | {row["period_day"] for row in existing}):
        cutoff = datetime.fromisoformat(cohort_day).replace(tzinfo=timezone.utc) + timedelta(days=COHORT_OPEN_DAYS+1)
        early_expiry = expires.get(cohort_day)
        is_frozen = reference >= cutoff or bool(early_expiry and early_expiry < retention_cutoff)
        values = [value for dimension, cell in sorted(cohorts[cohort_day].items())
                  if (value := cell.release(dimension[:3], dimension[3])) is not None]
        _write(conn, "cohort", cohort_day, scope, taxonomy, policy, reference, watermark, values,
               state="frozen" if is_frozen else "open", cutoff=min(reference, cutoff) if is_frozen else cutoff)
    return {"status": "ok", "market_cells": len(released), "cohort_partitions": len(cohorts)}


def run(reference=None):
    started = time.monotonic()
    conn = db.connect()
    try:
        if conn.dialect == "postgres":
            conn._conn.set_session(isolation_level="REPEATABLE READ")
        with conn.transaction():
            result = materialize(conn, reference)
    finally:
        conn.close()
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def report(reference=None, days=30):
    """Read bounded partitions; omit operational watermarks and raw identities."""
    reference = _reference(reference)
    days = max(1, min(int(days), 366))
    oldest = (reference.date()-timedelta(days=days-1)).isoformat()
    conn = db.connect()
    try:
        scope, taxonomy, _roots, _labels, policy = _context(conn)
        rows = _rows(conn, "SELECT kind,period_day,scope_key,policy_version,taxonomy_version,state,captured_at,"
                     "freeze_cutoff,aggregates_json FROM aggregate_partitions WHERE period_day>=? AND period_day<=? "
                     "AND scope_key=? AND policy_version=? AND taxonomy_version=? ORDER BY period_day,kind",
                     (oldest, reference.date().isoformat(), scope, policy, taxonomy))
        tracking_started = conn.execute("SELECT MIN(period_day) FROM aggregate_partitions WHERE kind='market' "
                                        "AND scope_key=? AND policy_version=? AND taxonomy_version=?",
                                        (scope, policy, taxonomy)).fetchone()[0]
    finally:
        conn.close()
    partitions = []
    market_days = {}
    for row in rows:
        row["cells"] = json.loads(row.pop("aggregates_json"))
        for key in ("captured_at", "freeze_cutoff"):
            row[key] = metrics.parse_ts(row[key]).isoformat() if row[key] else None
        if row["kind"] == "cohort":
            row["freeze_delay_hours"] = round(max(0, (metrics.parse_ts(row["captured_at"]) -
                metrics.parse_ts(row["freeze_cutoff"])).total_seconds()/3600), 2) if row["state"] == "frozen" else None
            row["uncertainty"] = "Latest retained evidence only; missed checks and evidence arriving after cutoff remain unknown."
        partitions.append(row)
        if row["kind"] == "market":
            market_days[row["period_day"]] = "available" if row["cells"] else "suppressed"
    calendar = [{"day": (reference.date()-timedelta(days=offset)).isoformat(),
                 "status": market_days.get((reference.date()-timedelta(days=offset)).isoformat(), "missing")}
                for offset in reversed(range(days))]
    latest = max((row["captured_at"] for row in partitions if row["kind"] == "market"), default=None)
    return {"version": VERSION, "policy_version": policy, "scope_key": scope, "taxonomy_version": taxonomy,
            "generated_at": reference.isoformat(), "latest_snapshot_at": latest,
            "tracking_started_day": tracking_started,
            "freshness_hours": round((reference-metrics.parse_ts(latest)).total_seconds()/3600, 2) if latest else None,
            "privacy": {"minimum_group": MIN_GROUP, "count_rounding": ROUND_TO, "geography": "FI",
                        "minimum_price_sellers": 20, "minimum_known_seller_share": .8,
                        "maximum_price_seller_share": .1},
            "cohort_open_days": COHORT_OPEN_DAYS, "fresh_price_hours": config.PANEL_MAX_AGE_HOURS,
            "listing_retention_days": config.LISTING_RETENTION_DAYS,
            "days": calendar, "partitions": partitions, "note": NOTE}


def export(reference=None):
    result = report(reference)
    write_text(config.REPORTS_DIR / "historical_aggregates.json", _json(result) + "\n")
    return result
