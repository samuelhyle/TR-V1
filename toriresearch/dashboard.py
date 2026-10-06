"""Local control and monitoring dashboard.

Stored observations and our own trade ledger, loopback-only, stdlib only.

Two boundaries are enforced in code rather than in the interface, because a
rule that lives in the UI is one copy-paste away from being gone. See
`compliance.py`: this binds to loopback and refuses anything else, and it never
requests the platform on a viewer's behalf. Refreshing this page reads the local
database, so watching it cannot become real-time traffic against a seller.

"Realtime" here means the panel tracks our collection as it runs -- new rows,
new terminal states, new coverage -- at the cadence of the collector. It does
not mean live queries, and it should not, because real-time competitor price
undercutting is the specific harm that generates injunctions.

Collection actions reuse the CLI. Owned inventory and inspection writes stay
local; the dashboard bounds budgets, serialises runs behind a lock so a
dashboard trigger cannot race the launchd job into the same grid cell, and shows
what the collector is actually doing.
"""

import json
import hashlib
import hmac
import logging
import math
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import collection_control, collection_events, aggregates, collection, compliance, config, connections, db, drafts, inventory, observability, parser_health, permissions, purchases, soldextract, trades, workflow
from . import auth, access
from .ebay_api import EbayAPIError
from .ingest import category as catdim
from .ingest import sweep

log = logging.getLogger("toriresearch.dashboard")

ROOT = config.ROOT
PAGE = Path(__file__).with_name("dashboard.html")
WEB_ROOT = Path(__file__).with_name("web")

# Static asset types. Nothing here may execute, and nothing may reference an
# external origin -- the page is required to work with the network unplugged.
WEB_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}

# No external resources, no framing, no inline event handlers. The page must work
# with the network unplugged, so nothing may be fetched from another origin.
#
# `script-src 'self'` replaces the former `'unsafe-inline'`: the dashboard's
# JavaScript now lives in same-origin ES modules under `web/`, so inline script
# execution is no longer needed and is no longer permitted. `style-src` still
# allows inline styles because a handful of layout values are set per element;
# those are candidates for extraction, not for a security claim either way.
#
# `frame-ancestors 'none'` is a separate directive that `default-src` does not
# cover. This page triggers real collection runs on a click, so a page that
# framed it and overlaid its own controls would be a clickjacking path into
# those actions.
CSP = ("default-src 'none'; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")


def web_asset(relpath):
    """Resolve a `/static/...` request to a file inside `web/`, or None.

    Resolving and then checking containment is deliberate: `Path.resolve()`
    collapses `..` and follows symlinks, so the check below is against the
    *real* path and a request cannot escape the asset directory.
    """
    if not relpath:
        return None
    try:
        target = (WEB_ROOT / relpath).resolve()
        target.relative_to(WEB_ROOT.resolve())
    except (ValueError, OSError):
        # ValueError covers both a `..` escape and an absolute path that lands
        # outside the asset root.
        return None
    if not target.is_file():
        return None
    if target.suffix.lower() not in WEB_TYPES:
        # The allowlist is what stops an unexpected file type being served with
        # a content type a browser would treat as active content.
        return None
    return target


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def _ago(iso):
    if not iso:
        return None
    from .metrics import parse_ts
    stamp = parse_ts(iso)
    if stamp is None:
        return None
    return round((datetime.now(timezone.utc) - stamp).total_seconds() / 60, 1)


def health():
    """Operational health, including whether we are being throttled or blocked."""
    from . import collection_health
    last_sweep = db.query_one(
        "SELECT * FROM sweep_runs ORDER BY sweep_run_pk DESC LIMIT 1")
    last_probe = db.query_one(
        "SELECT * FROM ingest_runs ORDER BY run_pk DESC LIMIT 1")
    throttled = db.scalar(
        "SELECT COUNT(*) FROM sweep_runs WHERE throttled_429 > 0") or 0
    recent_429 = db.scalar(
        "SELECT COUNT(*) FROM sweep_runs WHERE throttled_429 > 0 "
        "AND started_at > ?", ((datetime.now(timezone.utc)
                                - timedelta(hours=24)).isoformat(),)) or 0
    # A block is not a metric, it is a stop sign: it means the platform has
    # refused us and the only legitimate response is a human and Tier 0
    # licensing. It gets its own count so it cannot hide inside `errors`.
    blocked = db.scalar(
        "SELECT COUNT(*) FROM sweep_runs WHERE blocked_403 > 0") or 0
    recent_blocked = db.scalar(
        "SELECT COUNT(*) FROM sweep_runs WHERE blocked_403 > 0 "
        "AND started_at > ?", ((datetime.now(timezone.utc)
                                - timedelta(hours=24)).isoformat(),)) or 0
    from .http import collection_stop
    stop = collection_stop()

    audit = config.DATA_DIR / "request_audit.jsonl"
    audit_lines = 0
    last_status = None
    if audit.exists():
        try:
            tail = audit.read_text(errors="replace").splitlines()[-2000:]
            audit_lines = sum(1 for _ in audit.open())
            for line in reversed(tail):
                try:
                    last_status = json.loads(line).get("status")
                    break
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    raw_bytes = 0
    raw_root = config.DATA_DIR / "raw"
    if raw_root.exists():
        raw_bytes = sum(f.stat().st_size for f in raw_root.rglob("*.bin")
                        if f.exists())

    locks = db.active_locks()
    with _RUNNING_LOCK:
        running_actions = list(_running.values())

    observations = db.scalar("SELECT COUNT(*) FROM listings") or 0
    oldest_unseen_min = _ago(db.scalar(
        "SELECT MIN(first_seen_at) FROM listings WHERE outcome_status='unknown'"))

    return {
        "observations": observations,
        "oldest_unseen_minutes": oldest_unseen_min,
        "last_sweep": dict(last_sweep) if last_sweep else None,
        "last_sweep_minutes_ago": _ago(last_sweep["started_at"]) if last_sweep else None,
        "last_atom_feed": dict(last_probe) if last_probe else None,
        "sweeps_throttled_total": throttled,
        "sweeps_throttled_24h": recent_429,
        "sweeps_blocked_total": blocked,
        "sweeps_blocked_24h": recent_blocked,
        "blocked": bool(stop),
        "collection_stop": stop,
        "blocked_note": (
            "The platform refused us. Nothing was retried and no listing was "
            "guessed at, and no cell was counted as covered. Do not work around "
            "a refusal -- see PROJECT_PLAN.md on ryzlain 35 luku. Fix the "
            "credentials, or send the Tier 0 licensing email."
        ) if stop else None,
        "audit_lines": audit_lines,
        "collection_monitor": collection_health.view(),
        "last_request_status": last_status,
        "raw_cache_mb": round(raw_bytes / 1e6, 1),
        "active_locks": locks,
        "collector_running": bool(locks or running_actions),
        "running_actions": running_actions,
        "retention": {
            "raw_days": config.RAW_RETENTION_DAYS,
            "audit_days": config.AUDIT_RETENTION_DAYS,
        },
    }


def data_quality():
    """How much of what we hold is actually usable."""
    def frac(sql):
        total = db.scalar("SELECT COUNT(*) FROM listings") or 0
        return round(100 * (db.scalar(sql) or 0) / total, 1) if total else None

    return {
        "with_price": frac("SELECT COUNT(*) FROM listings WHERE listed_price_eur>0"),
        "with_category_label": frac(
            "SELECT COUNT(*) FROM listings WHERE category_norm IS NOT NULL"),
        "with_condition": frac(
            "SELECT COUNT(*) FROM listings WHERE condition_label_raw IS NOT NULL"),
        "with_terminal_state": frac(
            "SELECT COUNT(*) FROM listings WHERE outcome_status<>'unknown'"),
        # Free from the search page since the embedded search response was
        # adopted: the platform publishes this per listing, so anything missing
        # here is a row collected before that, or one that fell back to JSON-LD.
        "with_seller_type": frac(
            "SELECT COUNT(*) FROM listings WHERE seller_type_norm IS NOT NULL"),
        "diili_split_out": db.scalar(
            "SELECT COUNT(*) FROM listings WHERE subplatform='diili'") or 0,
        "seller_id_stored": db.scalar(
            "SELECT COUNT(*) FROM listings WHERE seller_id IS NOT NULL") or 0,
        "coordinates_stored": 0,
        "giveaways_flagged": db.scalar(
            "SELECT COUNT(*) FROM listings WHERE is_giveaway=TRUE") or 0,
        "category_labels": catdim.coverage(),
    }


def enrichment():
    """Derived-value health, kept visually separate from observation health.

    Shown in its own panel because the two are different claims. "We have
    classified 400 listings" is not "we have observed 400 listings", and showing
    them in one coverage figure would let a derived number stand in for a
    measured one.
    """
    from .enrich import provider, queue, runner, listing_queue
    from . import analysis

    stats = runner.summary()
    health = provider.health()
    conditions, params = collection.Scope("tori").conditions()
    rows = db.query("SELECT "+queue.FIELDS+" FROM listings WHERE platform='tori'"+
                    (" AND "+" AND ".join(conditions) if conditions else ""), params)
    accept = len(analysis.accepted_product_types(rows))
    mismatched = db.scalar(
        "SELECT COUNT(*) FROM enrichment WHERE validation LIKE '%home=mismatch%'"
    ) or 0
    home_checked = db.scalar(
        "SELECT COUNT(*) FROM enrichment WHERE validation LIKE '%home=%'") or 0
    stats.update({
        "provider_reachable": health["reachable"],
        "provider_error": health.get("error"),
        "provider_model": health.get("model"),
        "accepted_confident": accept,
        "queue": queue.report(),
        "listing_analysis": listing_queue.report(),
        "home_mismatch": mismatched,
        "home_check_rate_pct": round(100 * mismatched / home_checked, 1)
        if home_checked else None,
        "note": (
            "home_mismatch is ambiguous: it means either the vocabulary has no term "
            "for the item, or the seller filed it under the wrong category. Its rate "
            "is the signal for when to extend config/vocabulary.yaml."
        ),
    })
    return stats


def panels(filters=None):
    """All views use the same slice, availability, freshness and threshold rules."""
    from . import metrics
    filters = filters or {}
    def panel(label, **slice_filters):
        report = metrics.ask_price_panel(**slice_filters)
        report.update(label=label, n=report["n_with_price"],
                      median=report.get("median_eur"), p25=report.get("p25_eur"),
                      p75=report.get("p75_eur"))
        ci = report.get("median_ci95")
        report["ci"] = [ci["lo"], ci["hi"]] if ci else None
        return report
    where, params, _ = metrics.listing_filters(**filters)
    base = " WHERE " + " AND ".join(where) if where else ""
    roots = db.query("SELECT category_root_norm AS k, COUNT(*) AS n FROM listings" +
                     base + (" AND " if base else " WHERE ") +
                     "category_root_norm IS NOT NULL GROUP BY category_root_norm ORDER BY n DESC LIMIT 12", params)
    municipalities = db.query("SELECT municipality_norm AS k, COUNT(*) AS n FROM listings" +
                              base + (" AND " if base else " WHERE ") +
                              "municipality_norm IS NOT NULL GROUP BY municipality_norm ORDER BY n DESC LIMIT 12", params)
    return {"national": metrics.ask_price_panel(**filters),
            "by_category": [panel("category: " + row["k"], **dict(filters, category_root_norm=row["k"])) for row in roots],
            "by_municipality": [panel("municipality: " + row["k"], **dict(filters, municipality_norm=row["k"])) for row in municipalities]}


def segment_options():
    return {"category_norm": [r["k"] for r in db.query(
                "SELECT DISTINCT category_norm AS k FROM listings WHERE category_norm IS NOT NULL ORDER BY 1")],
            "category_root_norm": [r["k"] for r in db.query(
                "SELECT DISTINCT category_root_norm AS k FROM listings WHERE category_root_norm IS NOT NULL ORDER BY 1")],
            "municipality_norm": [r["k"] for r in db.query(
                "SELECT DISTINCT municipality_norm AS k FROM listings WHERE municipality_norm IS NOT NULL ORDER BY 1")],
            "platform": [r["k"] for r in db.query("SELECT DISTINCT platform AS k FROM listings ORDER BY 1")],
            "subplatform": list(config.SUBPLATFORMS),
            "condition_norm": [r["k"] for r in db.query(
                "SELECT DISTINCT condition_norm AS k FROM listings WHERE condition_norm IS NOT NULL ORDER BY 1")],
            "seller_type_norm": [r["k"] for r in db.query(
                "SELECT DISTINCT seller_type_norm AS k FROM listings WHERE seller_type_norm IS NOT NULL ORDER BY 1")]}


def observations(filters=None, search="", sort="newest", page=1, page_size=25,
                 availability="all"):
    """Browse local observations without making any marketplace requests."""
    from . import metrics
    orders = {
        "newest": "last_seen_at DESC, listing_pk DESC",
        "oldest": "last_seen_at ASC, listing_pk ASC",
        "price_asc": "listed_price_eur IS NULL, listed_price_eur ASC, listing_pk DESC",
        "price_desc": "listed_price_eur IS NULL, listed_price_eur DESC, listing_pk DESC",
        "title": "LOWER(COALESCE(title, '')) ASC, listing_pk DESC",
    }
    if sort not in orders:
        raise ValueError("unsupported listing sort")
    if not isinstance(search, str) or len(search) > 200:
        raise ValueError("search must be at most 200 characters")
    if isinstance(page, bool) or isinstance(page_size, bool):
        raise ValueError("page and page size must be integers")
    try:
        page, page_size = int(page), int(page_size)
    except (ValueError, TypeError):
        raise ValueError("page and page size must be integers")
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("page must be positive and page size must be 1..100")
    where, params, applied = metrics.listing_filters(**(filters or {}))
    search = search.strip()
    if search:
        # Treat wildcard characters as text in an operator's search.
        pattern = "%" + search.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        where.append("(LOWER(COALESCE(title, '')) LIKE ? ESCAPE '!' OR LOWER(listing_id) LIKE ? ESCAPE '!')")
        params.extend([pattern, pattern])
    if availability == "available":
        where.extend(["status_norm='active'", "outcome_status='unknown'", "COALESCE(is_reserved,FALSE)=FALSE"])
    elif availability == "reserved":
        where.append("is_reserved=TRUE")
    elif availability == "sold":
        where.append("outcome_status IN ('sold_confirmed','sold_inferred')")
    elif availability == "giveaway":
        where.append("is_giveaway=TRUE")
    elif availability != "all":
        raise ValueError("unsupported availability filter")
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = db.scalar("SELECT COUNT(*) FROM listings" + clause, params) or 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    columns = ("listing_pk, platform, subplatform, listing_id, title, category_norm, "
               "category_root_norm, municipality_norm, condition_norm, condition_label_raw, "
               "seller_type_norm, listed_price_eur, is_giveaway, is_reserved, status_norm, "
               "outcome_status, first_seen_at, last_seen_at, last_probed_at, price_observed_at")
    items = db.query("SELECT " + columns + " FROM listings" + clause +
                     " ORDER BY " + orders[sort] + " LIMIT ? OFFSET ?",
                     params + [page_size, (page - 1) * page_size])
    return {"items": [dict(row) for row in items], "total": total, "page": page,
            "pages": pages, "page_size": page_size, "filters": applied}


def action_history(limit=20):
    return [dict(r) for r in db.query(
        "SELECT action_log_pk, started_at, action, returncode, seconds, stdout_tail, stderr_tail FROM action_log "
        "ORDER BY action_log_pk DESC LIMIT ?", (limit,))] if _has_table("action_log") else []


def _has_table(name):
    try:
        db.scalar(f"SELECT 1 FROM {name} LIMIT 1")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# predictive models
# --------------------------------------------------------------------------

def _json_safe(value):
    """Drop non-finite floats and stringify what JSON cannot carry.

    NaN is not JSON and `JSON.parse` rejects it, so a non-finite gauge would
    fail the whole page rather than one cell. Timestamps become ISO 8601 rather
    than `str(datetime)`, which some engines will not parse back.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def predictions(values=None):
    from . import model_workspace
    return model_workspace.report(values)


# --------------------------------------------------------------------------
# local operations: recovery backup, storage, the seven-day review
# --------------------------------------------------------------------------

OPERATION_ACTIONS = ("backup", "verify")
_OPERATION_LOCK = threading.Lock()
_OPERATION_JOB = {"action": None, "status": "idle", "started_at": None,
                  "finished_at": None, "result": None, "error": None}


def _collection_review_report():
    """The seven-day operating review as the coordinator last wrote it.

    Recomputing it here would need a full measurement pass; reading the report
    keeps this endpoint cheap, so its age is reported instead of hidden.
    """
    path = config.REPORTS_DIR / "collection_review.json"
    try:
        review = json.loads(path.read_text())
    except (OSError, ValueError):
        review = None
    if not isinstance(review, dict):
        return {"available": False,
                "reason": "The seven-day collection review has not been written yet. "
                          "It is produced by the scheduled coordinator run, not by this page."}
    from .metrics import parse_ts
    stamp = parse_ts(review.get("generated_at"))
    age_hours = round((datetime.now(timezone.utc) - stamp).total_seconds() / 3600, 2) if stamp else None
    fresh = bool(stamp and age_hours is not None and 0 <= age_hours
                 <= 2 * config.COLLECTION_INTERVAL_SECONDS / 3600)
    from . import collection_health
    return {"available": True, "fresh": fresh, "age_hours": age_hours,
            "context_matches": review.get("context") == collection_health.context(),
            "generated_at": review.get("generated_at"), "status": review.get("status"),
            "complete_days": review.get("complete_days"), "required_days": review.get("required_days", 7),
            "eligible_for_expansion_review": review.get("eligible_for_expansion_review"),
            "review_not_before": review.get("review_not_before"),
            "gates": list((review.get("gates") or {}).values()),
            "schedule": review.get("schedule") or {}, "note": review.get("note")}


def operations_report():
    """Recovery, storage and process state, all read from this machine."""
    from . import backups, storage_health

    storage = storage_health.report()
    with _OPERATION_LOCK:
        job = dict(_OPERATION_JOB)
    return _json_safe({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "storage": storage,
        "backup": storage.get("backup") or backups.status(),
        "review": _collection_review_report(),
        "locks": db.active_locks(),
        "metrics": observability.snapshot_json(),
        "job": job,
        "actions": list(OPERATION_ACTIONS),
    })


def operations_action(payload):
    """Start one local recovery operation in the background.

    Both operations are local file work: a copy and a restore rehearsal. Neither
    contacts a platform, so neither needs a compliance allowlist entry, but only
    one may run at a time and its outcome must survive the request that started
    it -- hence a job record the GET endpoint keeps reporting.
    """
    action = payload.get("action")
    if action not in OPERATION_ACTIONS:
        raise ValueError("unknown operation: " + repr(action)[:80])
    with _OPERATION_LOCK:
        if _OPERATION_JOB["status"] == "running":
            raise ValueError("An operation is already running. Wait for it to finish.")
        started = datetime.now(timezone.utc).isoformat()
        _OPERATION_JOB.update({"action": action, "status": "running", "started_at": started,
                               "finished_at": None, "result": None, "error": None})
        job = dict(_OPERATION_JOB)

    def run():
        from . import backups
        try:
            result = backups.ensure(force=True) if action == "backup" else backups.verify()
            status, result, error = "done", result, None
        except Exception as exc:  # noqa: BLE001 - a failed rehearsal must be reported, not raised in a thread
            log.exception("operation %s failed", action)
            status, result, error = "failed", None, str(exc)[:300]
        with _OPERATION_LOCK:
            if _OPERATION_JOB.get("started_at") == started:
                _OPERATION_JOB.update({"status": status, "result": _json_safe(result),
                                       "error": error,
                                       "finished_at": datetime.now(timezone.utc).isoformat()})

    threading.Thread(target=run, daemon=True, name="tori-" + action).start()
    return {"ok": True, "job": job}


# --------------------------------------------------------------------------
# manual imports
# --------------------------------------------------------------------------

# One hand-captured file per request. The template is 15 columns and captures
# are a bounded keyword list, so this is generous rather than permissive: the
# body is a JSON string of the CSV, which costs more bytes than the file does.
IMPORT_MAX_BODY = 65536


def soldextract_report(keyword=None, min_obs=3):
    """What is stored from hand-captured eBay Product Research aggregates.

    The template travels with the report so the download button and the import
    contract come from the same place the parser reads them.
    """
    try:
        min_obs = int(min_obs)
    except (TypeError, ValueError):
        raise ValueError("min_obs must be an integer") from None
    if not 1 <= min_obs <= 1000:
        raise ValueError("min_obs must be 1..1000")
    keyword = (keyword or "").strip() or None
    from . import guided_capture
    return _json_safe({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": soldextract.SOURCE,
        "fields": list(soldextract.CAPTURE_TEMPLATE_FIELDS),
        "template": soldextract.template_csv(),
        "summary": soldextract.summary(),
        "ratio": soldextract.realization_ratio(keyword=keyword, min_obs=min_obs),
        "keyword": keyword,
        "min_obs": min_obs,
        "recent_captures": guided_capture.recent(),
    })


def soldextract_action(payload):
    """Import one hand-captured CSV. Every row is validated before the write."""
    action = payload.get("action")
    if action == "capture":
        from . import guided_capture
        if set(payload) != {"action", "params"}:
            raise ValueError("provide action and sold capture params")
        return guided_capture.capture(payload["params"])
    if action != "import":
        raise ValueError("unknown import action: " + repr(action)[:80])
    csv_text = payload.get("csv")
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise ValueError("csv text is required")
    captured_by = payload.get("captured_by") or "operator"
    if not isinstance(captured_by, str) or not captured_by.strip() or len(captured_by) > 64:
        raise ValueError("captured_by must be 1..64 characters")
    result = soldextract.import_capture(csv_text, captured_by=captured_by.strip())
    return {"ok": True, **result}


_CACHE = {"key": None, "value": None, "at": 0.0, "build": 0.0}
_CACHE_LOCK = threading.Lock()
SNAPSHOT_TTL = 8.0
_SNAPSHOT_BUILD_LOCK = threading.Lock()


def _effective_ttl(base, built):
    """A floor on the cache window derived from what a rebuild actually costs.

    The fixed TTLs were tuned on a small database and then outlived it. On this
    one a snapshot rebuild takes seconds, so an eight-second window was already
    expiring by the time a build finished: the cache could never report a hit and
    every poll paid the full cost. Deriving the window from the last measured
    build keeps the cache honest as the database grows, without a magic number
    that has to be re-guessed at every scale.
    """
    return max(base, built * 2.0)


def _ledger_freshness_key():
    # The ledger is small, and reading its actual contents is what makes this
    # correct: a correction that leaves row counts and purchase dates unchanged
    # still changes the cached totals, so a cheaper key -- count plus max pk --
    # would be wrong in exactly the case this function exists for.
    #
    # Measured cost, since it runs inside the freshness key on every snapshot:
    # 0.01ms empty, 1.0ms at 500 rows, 14.9ms at 5000. The owned ledger grows
    # with the operator's own trades, not with collection volume, so it reaches
    # the expensive end only after years of trading. Revisit if it ever does.
    return json.dumps([
        db.query("SELECT * FROM purchases ORDER BY purchase_pk"),
        db.query("SELECT * FROM transactions ORDER BY transaction_pk")
    ], sort_keys=True, default=str)


def _storage_key():
    return (hashlib.sha256(config.POSTGRES_DSN.encode()).hexdigest()
            if config.POSTGRES_DSN else str(config.SQLITE_PATH))


def _listings_signature():
    """One aggregate over `listings`, shared by both cache keys.

    `_freshness_key` and `analysis_freshness_key` each want to know how many
    listings exist, how recently each was seen, and how many have been probed.
    They used to ask separately, which meant two full table scans per snapshot
    build -- 78ms on this database, growing with it. The aggregate is
    unqualified, so no index changes the scan; computing it once and sharing it
    halves the cost for free, and keeps the two keys from drifting apart as
    fields are added to one and not the other.

    `resolved` is included because the analyst view is driven by outcome
    resolution rather than row arrival: a status check that turns 400 listings
    terminal writes no new listing row and no new `first_seen_at`.
    """
    return db.query_one(
        "SELECT COUNT(*) AS n, MAX(last_seen_at) AS newest, "
        "MAX(outcome_observed_at) AS newest_outcome, MAX(last_probed_at) AS newest_probe, "
        "SUM(CASE WHEN outcome_status <> 'unknown' THEN 1 ELSE 0 END) AS resolved, "
        "SUM(CASE WHEN last_probed_at IS NOT NULL THEN 1 ELSE 0 END) AS probed "
        "FROM listings")


def _freshness_key():
    """A cheap signature of the data, used to invalidate the snapshot cache.

    Counting rows and taking the newest observation timestamp is enough to tell
    whether anything changed, and costs two indexed queries. Without this, a page
    polling every ten seconds recomputed two dozen bootstrap intervals per poll
    and burned a fifth of a core doing arithmetic that had not changed.
    """
    try:
        row = _listings_signature()
        extra = db.query_one("SELECT COUNT(*) AS n, MAX(observed_at) AS newest FROM enrichment")
        owned_events = db.query_one("SELECT COUNT(*) AS n, MAX(event_pk) AS latest FROM inventory_events")
        owned_previews = db.query_one("SELECT COUNT(*) AS n, MAX(evaluation_pk) AS latest FROM platform_eligibility")
        draft_events = db.query_one("SELECT COUNT(*) AS n, MAX(event_pk) AS latest FROM draft_events")
        approvals = db.query_one("SELECT COUNT(*) AS n, MAX(recorded_at) AS latest FROM draft_approvals")
        queue_events = db.query_one("SELECT COUNT(*) AS n, MAX(event_pk) AS latest FROM action_events")
        trade_events = db.query_one("SELECT COUNT(*) AS n, MAX(event_pk) AS latest FROM order_events")
        classification_tasks = db.query_one("SELECT COUNT(*) AS n,MAX(queued_at) AS queued,MAX(last_attempt_at) AS attempted, "
                                            "SUM(CASE WHEN state='leased' THEN 1 ELSE 0 END) AS leased, "
                                            "SUM(CASE WHEN state='quarantined' THEN 1 ELSE 0 END) AS quarantined, "
                                            "SUM(failure_count) AS failures FROM enrichment_tasks")
        parser_counts = db.query_one("SELECT SUM(checks) AS checks,SUM(parsed_rows) AS parsed FROM parser_checks")
        history_state = db.query_one("SELECT COUNT(*) AS n,MAX(captured_at) AS latest, "
                                    "SUM(CASE WHEN state='frozen' THEN 1 ELSE 0 END) AS frozen FROM aggregate_partitions")
        from .enrich import vocabulary
        from . import listing_analysis
        return (row["n"], row["newest"], row["newest_outcome"], row["newest_probe"], row["probed"], extra,
                _ledger_freshness_key(), owned_events, owned_previews, draft_events, approvals, queue_events, trade_events,
                classification_tasks, parser_counts, history_state, _storage_key(), config.FOCUS_CATEGORIES,
                config.FOCUS_MUNICIPALITIES, config.ITEM_BUDGET, config.PROBE_INTERVAL_HOURS,
                config.PROBE_REPEAT_SHARE, config.SWEEP_BUDGET, config.ENRICH_BUDGET,
                config.PANEL_MAX_AGE_HOURS, config.LLM_MODEL, config.LLM_MODEL_REVISION,
                config.ENRICH_PROMPT_VERSION, vocabulary.load()["sha256"], listing_analysis.identity(), permissions.signature())
    except Exception:
        return None


def cached_snapshot(max_age=None, filters=None):
    """Snapshot, recomputed only when the data changed or the TTL expired."""
    # Page polling and SSE can arrive together. Only one cold build runs, and
    # a waiting caller checks the cache again after that build completes.
    with _SNAPSHOT_BUILD_LOCK:
        # Omitted platform/market filters mean the same slice as the browser's
        # explicit defaults. Treating them as separate keys makes checks and
        # open tabs repeatedly rebuild the same expensive snapshot.
        scope = {"platform": "tori", "subplatform": "standard", **(filters or {})}
        key = (_freshness_key(), json.dumps(scope, sort_keys=True))
        with _CACHE_LOCK:
            window = max_age if max_age is not None else _effective_ttl(SNAPSHOT_TTL, _CACHE.get("build", 0.0))
            fresh_enough = time.monotonic() - _CACHE["at"] < window
            if (fresh_enough and key[0] is not None and _CACHE["key"] == key
                    and _CACHE["value"] is not None):
                return _CACHE["value"]
        started = time.monotonic()
        value = snapshot(filters)
        with _CACHE_LOCK:
            _CACHE.update({"key": key, "value": value, "at": time.monotonic(),
                           "build": time.monotonic() - started})
        return value


def snapshot(filters=None):
    # Reuse one connection and one committed view across the whole response.
    with db.read_snapshot():
        return _snapshot(filters)


def _snapshot(filters=None):
    # The analyst view used to be computed here as `market_intel` and embedded in
    # this payload, which meant every snapshot build paid a second full
    # `analysis.snapshot()` -- 4.7s of the 9.4s total on this database -- to
    # return keys the browser never reads, and that `/api/analysis` already
    # returns as `matrix` / `candidates` / `liquidity` / `portfolio`. Building it
    # once, on the endpoint that serves it, is the whole fix. `market_intel`
    # itself is unchanged and still reachable from the CLI.
    from . import metrics

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "posture": compliance.dashboard_posture(),
        "health": health(),
        "compliance": compliance.compliance_report(),
        "permissions": permissions.report(),
        "coverage": sweep.coverage(),
        "collection": collection.coverage(),
        "quality": data_quality(),
        "enrichment": enrichment(),
        "parser_health": parser_health.report(),
        "history": aggregates.report(),
        "panels": panels(filters),
        "segments": segment_options(),
        "filters": filters or {"platform":"tori", "subplatform":"standard"},
        "liquidity": metrics.liquidity(**(filters or {})),
        "economics": purchases.summary(),
        "buys": purchases.list_purchases(),
        "inventory": purchases.realized_economics(),
        "recent_actions": action_history(),
        "protection": {
            "boundary": compliance.LOOPBACK_ONLY,
            "reason": compliance.dashboard_posture()["public_comparison_frontend"],
        },
    }


_FOCUS_CACHE = {"key": None, "value": None, "at": 0.0, "build": 0.0}
_FOCUS_TTL = 30.0


def cached_focus(filters=None, horizon=30, max_age=None):
    """The per-vertical focus breakdown, cached on the analysis signature.

    Kept out of `snapshot()` on purpose. This walks one cohort and a price panel
    per configured vertical, so it costs seconds rather than milliseconds, and
    the snapshot cache would otherwise rebuild it on every poll from every open
    tab. It reads the same evidence as the analyst view, so the analysis
    freshness key is the correct thing to key it on.
    """
    from . import analysis
    key = (analysis_freshness_key(), json.dumps(filters or {}, sort_keys=True), str(horizon))
    now = time.monotonic()
    with _CACHE_LOCK:
        window = max_age if max_age is not None else _effective_ttl(_FOCUS_TTL, _FOCUS_CACHE.get("build", 0.0))
        if (now - _FOCUS_CACHE["at"] < window and key is not None
                and _FOCUS_CACHE["key"] == key and _FOCUS_CACHE["value"] is not None):
            return _FOCUS_CACHE["value"]
    started = time.monotonic()
    with db.read_snapshot():
        value = analysis.focus_breakdown(filters, horizon=horizon)
    with _CACHE_LOCK:
        _FOCUS_CACHE.update({"key": key, "value": value, "at": now,
                             "build": time.monotonic() - started})
    return value


_ANALYSIS_CACHE = {"key": None, "value": None, "at": 0.0, "build": 0.0}
_ANALYSIS_TTL = 20.0
_ANALYSIS_BUILD_LOCK = threading.Lock()
_DESK_CACHE = {"key": None, "value": None, "at": 0.0, "build": 0.0}
_DESK_BUILD_LOCK = threading.Lock()


def cached_desk(filters=None, max_age=None, **options):
    """Coalesce shortlist reads and expire them on evidence or time changes."""
    from . import research_desk
    with _DESK_BUILD_LOCK:
        with db.read_snapshot():
            key = (analysis_freshness_key(), json.dumps(filters or {}, sort_keys=True),
                   json.dumps(options, sort_keys=True))
            with _CACHE_LOCK:
                window = max_age if max_age is not None else _effective_ttl(_ANALYSIS_TTL, _DESK_CACHE["build"])
                if (key[0] is not None and _DESK_CACHE["key"] == key
                        and _DESK_CACHE["value"] is not None
                        and time.monotonic() - _DESK_CACHE["at"] < window):
                    return _DESK_CACHE["value"]
            started = time.monotonic()
            value = research_desk.candidates(filters, **options)
        with _CACHE_LOCK:
            _DESK_CACHE.update(key=key, value=value, at=time.monotonic(),
                               build=time.monotonic() - started)
        return value


def analysis_freshness_key():
    """Cheap signature of everything the analyst view reads.

    `snapshot()`'s key is not enough here. The analyst view is driven by outcome
    resolution, not by row arrival: a status check that turns 400 listings into
    terminal ones writes no new listing row and no new `first_seen_at`, so a
    key built from those alone would keep serving a cache that says "sale
    evidence: none" long after the evidence exists. The counts that actually move
    are included, and so is the price-event total, which is what the activity
    chart's price-cut series is built from.
    """
    try:
        row = _listings_signature()
        events = db.scalar("SELECT COUNT(*) FROM events") or 0
        enrich = db.query_one("SELECT COUNT(*) AS n, MAX(observed_at) AS newest FROM enrichment")
        relationships = db.query_one("SELECT COUNT(*) AS n, MAX(recorded_at) AS newest FROM listing_relationship_events")
        tasks = db.query_one("SELECT COUNT(*) AS n, MAX(last_attempt_at) AS attempted, "
                             "MAX(last_success_at) AS succeeded, MAX(due_at) AS due, "
                             "SUM(failure_count) AS failures, "
                             "SUM(CASE WHEN state='ready' THEN 1 ELSE 0 END) AS ready, "
                             "SUM(CASE WHEN state='leased' THEN 1 ELSE 0 END) AS leased, "
                             "SUM(CASE WHEN state='quarantined' THEN 1 ELSE 0 END) AS quarantined "
                             "FROM followup_tasks")
        from .enrich import vocabulary
        from . import product_identity, offer_evidence, listing_analysis
        settings = (config.MIN_N_FOR_REPORT, config.PANEL_MAX_AGE_HOURS,
                    config.LLM_MODEL, config.LLM_MODEL_REVISION,
                    config.ENRICH_PROMPT_VERSION, vocabulary.load()["sha256"],
                    config.FOCUS_CATEGORIES, config.FOCUS_MUNICIPALITIES,
                    config.ITEM_BUDGET, config.PROBE_INTERVAL_HOURS, config.PROBE_REPEAT_SHARE,
                    config.FOLLOWUP_ENABLED, config.FOLLOWUP_MAX_ACTIVE, product_identity.load()["sha256"],
                    product_identity.RULE_VERSION, product_identity.COMPARISON_POLICY_VERSION,
                    product_identity.REVIEW_VERSION, offer_evidence.VERSION, listing_analysis.identity())
        return (row["n"], row["newest"], row["newest_outcome"], row["resolved"],
                row["probed"], row["newest_probe"], events, enrich, relationships, tasks, _ledger_freshness_key(), settings, _storage_key())
    except Exception:
        return None


def cached_analysis(filters=None, dimension="category_root_norm", days=30,
                    horizon=30, max_age=None):
    """Analysis, recomputed only when the underlying evidence changed.

    The analyst view is the most expensive thing this process serves. It groups
    every selected listing by a dimension, runs a price panel per group, and runs
    the full survival machinery per group -- and `liquidity_rows` bootstraps 400
    replicates per evidence class the moment a group has enough sale events to
    support an estimate. That is fine once an hour and ruinous once every thirty
    seconds, which is what a refreshing dashboard does.

    Measured on this database: 6.4s for 11 groups over 9.4k listings, and it
    grows with both the row count and the group count. The browser abandons the
    request at 45s, so without this the analyst view starts failing on real data
    while working perfectly on an empty one.
    """
    from . import analysis
    with _ANALYSIS_BUILD_LOCK:
        with db.read_snapshot():
            scope = {"platform": "tori", "subplatform": "standard", **(filters or {})}
            key = (analysis_freshness_key(), json.dumps(scope, sort_keys=True),
                   str(dimension), str(days), str(horizon))
            with _CACHE_LOCK:
                window = max_age if max_age is not None else _effective_ttl(_ANALYSIS_TTL, _ANALYSIS_CACHE["build"])
                if (key[0] is not None and _ANALYSIS_CACHE["key"] == key
                        and _ANALYSIS_CACHE["value"] is not None
                        and time.monotonic() - _ANALYSIS_CACHE["at"] < window):
                    return _ANALYSIS_CACHE["value"]
            started = time.monotonic()
            value = analysis.snapshot(filters, dimension=dimension, days=days, horizon=horizon)
        with _CACHE_LOCK:
            _ANALYSIS_CACHE.update(key=key, value=value, at=time.monotonic(),
                                   build=time.monotonic() - started)
        return value


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

_running = {}
_RUNNING_LOCK = threading.Lock()


def validate_action(action, params=None):
    """Validate a bounded CLI request before either local or cloud execution.

    Subprocess rather than in-process so a 60-second sweep cannot block the HTTP
    thread, and so the dashboard can only ever do what the CLI can do. Quantities
    are clamped to `compliance.ALLOWED_ACTIONS`; anything larger is refused
    rather than silently trimmed. Switches are refused unless they are real
    booleans, and an off switch is dropped instead of becoming `--flag 0`.
    """
    if action not in compliance.ALLOWED_ACTIONS:
        return {"ok": False, "error": f"action {action!r} not permitted"}
    caps = compliance.ALLOWED_ACTIONS[action]
    if params is not None and not isinstance(params, dict):
        return {"ok": False, "error": "params must be an object"}
    params = dict(params or {})

    for key in list(params):
        if key not in caps:
            return {"ok": False, "error": f"{action}: {key} not permitted"}
    for key, cap in caps.items():
        if cap == "flag":
            if key not in params:
                continue
            value = params[key]
            if not isinstance(value, bool) and value not in (0, 1):
                return {"ok": False, "error": f"{key} must be true or false"}
            if not bool(value):
                del params[key]
            else:
                params[key] = True
            continue
        if isinstance(params.get(key), (bool, float)):
            return {"ok": False, "error": f"{key} must be an integer"}
        try:
            budget = int(params.get(key, cap))
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{key} must be an integer"}
        if budget < 1 or budget > cap:
            return {"ok": False,
                    "error": f"{key} must be 1..{cap} from the dashboard"}
        params[key] = budget

    return {"ok": True, "params": params}


def run_action(action, params=None):
    """Trigger a validated CLI action in the persistent local runtime."""
    prepared = validate_action(action, params)
    if not prepared['ok']:
        return prepared
    params = prepared['params']
    started = time.time()
    started_at = datetime.fromtimestamp(started, timezone.utc).isoformat()
    with _RUNNING_LOCK:
        if action in _running:
            return {"ok": False, "error": f"{action} already running here"}
        _running[action] = {"action": action, "started_at": started_at, "params": params}

    argv = [sys.executable, "-m", "toriresearch.cli", action]
    for key, value in params.items():
        flag = f"--{key.replace('_', '-')}"
        argv += [flag] if value is True else [flag, str(value)]

    env = dict(os.environ)
    env.update(collection_control.environment(collection_control.current()['settings']))
    env["TORI_DATA_DIR"] = str(config.DATA_DIR)
    env["TORI_REPORTS_DIR"] = str(config.REPORTS_DIR)
    env['TORI_DB'] = str(config.SQLITE_PATH)

    def target():
        code, tail, err = -1, "", ""
        try:
            proc = subprocess.run(argv, cwd=str(ROOT), env=env, timeout=1800,
                                  capture_output=True, text=True)
            code, tail = proc.returncode, (proc.stdout or "")[-1500:]
            err = (proc.stderr or "")[-600:]
        except subprocess.TimeoutExpired:
            code, tail, err = -1, "", "timed out after 1800s"
        except OSError as exc:
            code, tail, err = 126, "", str(exc)
        except Exception as exc:
            err = str(exc)[:600]
        finally:
            _log(action, code, round(time.time() - started, 1), started_at, tail, err)
            with _RUNNING_LOCK:
                _running.pop(action, None)
            invalidate()


    threading.Thread(target=target, daemon=True).start()
    return {"ok": True, "started": argv[2:], "action": action, "started_at": started_at}


def _log(action, code, seconds, started_at=None, stdout_tail="", stderr_tail=""):
    try:
        db.execute(
            """INSERT INTO action_log (started_at, action, returncode, seconds, stdout_tail, stderr_tail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (started_at or datetime.now(timezone.utc).isoformat(), action, code, seconds,
             stdout_tail[-1500:], stderr_tail[-600:]),
        )
    except Exception as exc:
        log.warning("action log failed: %s", exc)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

def snapshot_filters(query):
    # `focus_path` is a configured vertical id, resolved by `metrics.listing_filters`
    # through the collector's own scope. It is allowlisted rather than arbitrary
    # because an unrecognised value selects nothing, which would look like an
    # empty segment instead of a bad request.
    allowed = ("platform", "subplatform", "category_root_norm", "category_norm", "municipality_norm",
               "condition_norm", "seller_type_norm", "focus_path")
    return {key: query[key][0] for key in allowed if query.get(key) and query[key][0]}



def invalidate():
    """Drop every cached view. Called when a write may have changed the data.

    Two caches, one reason: a completed collection run or a ledger write makes
    the stored evidence newer, and serving a pre-action analysis after it would
    show the operator a conclusion that was already out of date the moment they
    clicked the button. The focus breakdown is keyed on the same signature, so
    it is dropped with them.
    """
    with _CACHE_LOCK:
        _CACHE["value"] = None
        _ANALYSIS_CACHE["value"] = None
        _FOCUS_CACHE["value"] = None
        _DESK_CACHE["value"] = None


def ledger_action(payload):
    """Validate a small local ledger request and reuse the purchase writers."""
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
        raise ValueError("action and params are required")
    action, params = payload.get("action"), dict(payload["params"])
    common = {"platform", "source_listing_id"}
    if action == "buy":
        allowed = common | {"title", "acquisition_cost_eur", "subplatform",
                            "shipping_cost_eur", "buyer_fee_eur", "category_norm"}
        writer = purchases.record
    elif action == "sell":
        allowed = common | {"final_sale_price_eur", "platform_fee_eur", "payment_fee_eur",
                            "seller_shipping_cost_eur", "packaging_cost_eur",
                            "other_variable_cost_eur", "tax_cost_eur"}
        writer = purchases.record_sale
    else:
        raise ValueError("unsupported ledger action")
    if set(params) - allowed:
        raise ValueError("unsupported ledger fields")
    if action == "buy" and not {"title", "acquisition_cost_eur", "source_listing_id"} <= set(params):
        raise ValueError("title, source listing ID and acquisition cost are required")
    if action == "sell" and not {"source_listing_id", "final_sale_price_eur"} <= set(params):
        raise ValueError("source listing ID and realized price are required")
    result = writer(**params)
    invalidate()
    return {"ok": True, "result": result}


def collection_reply(function, payload):
    try:
        value = function(payload)
        return 202 if function in (collection_control.start, collection_control.control) else 200, json.dumps(value, default=str, allow_nan=False)
    except collection_control.Conflict as exc:
        return 409, json.dumps({'error': str(exc)})
    except permissions.PermissionDenied as exc:
        return 400, json.dumps({'error': str(exc)})


class Handler(BaseHTTPRequestHandler):
    server_version = "toriresearch"
    protocol_version = "HTTP/1.1"
    # Bound every socket read and write. Without this, `protocol_version =
    # "HTTP/1.1"` means a client that opens a connection and then stops talking
    # parks its worker thread forever: `readline()` on the request line blocks
    # with no timeout, and ThreadingHTTPServer spawns a thread per connection.
    # Loopback-only binding limits who can reach it, but a stalled socket from
    # anything on the host -- a stray local agent, a half-open browser tab --
    # would still accumulate until the dashboard stopped responding.
    timeout = 30

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            # Changing filters can cancel an in-flight browser request, and a
            # timed-out read is the same thing from the server's side.
            pass

    def _local_host(self):
        try:
            authority = urlparse("http://" + self.headers.get("Host", ""))
            host, port = authority.hostname, authority.port or 80
            return (host == "localhost" or bool(host and compliance.is_loopback(host))) and port == self.server.server_port
        except ValueError:
            return False

    def _same_origin(self):
        expected = 'http://' + self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        return not origin or origin == expected

    def _session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(getattr(self, 'headers', {}).get('Cookie', ''))
            return cookie[auth.COOKIE].value if auth.COOKIE in cookie else None
        except CookieError:
            return None

    def _require_access(self, method, path):
        required = access.capability(method, path)
        self.principal = None
        if required == 'public':
            return True
        try:
            self.principal = auth.authenticate(self._session_token())
        except auth.Denied as exc:
            if method in {'GET', 'HEAD'} and path in {'/', '/index.html', '/developer', '/viewer'}:
                self._send(303, '', 'text/plain', {'Location': '/login'})
            else:
                self._send(exc.status, json.dumps({'error': str(exc)}))
            return False
        if not access.permits(self.principal, required):
            if path in {'/', '/index.html'} and self.principal['role'] == 'viewer' and not self.principal['must_change_password']:
                self._send(303, '', 'text/plain', {'Location': '/viewer'})
                return False
            auth.record_denial(self.principal, method, path)
            self._send(403, json.dumps({'error': 'Read-only access: this action is unavailable.'
                                      if self.principal['role'] == 'viewer' else 'This action is unavailable.'}))
            return False
        if method == 'POST' and not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), self.principal['csrf_token']):
            self._send(403, json.dumps({'error': 'Refresh your session before submitting this request.'}))
            return False
        return True

    def _cookie(self, token='', clear=False):
        cookie = SimpleCookie()
        cookie[auth.COOKIE] = token
        cookie[auth.COOKIE]['path'] = '/'
        cookie[auth.COOKIE]['httponly'] = True
        cookie[auth.COOKIE]['samesite'] = 'Strict'
        if clear:
            cookie[auth.COOKIE]['max-age'] = 0
        return cookie.output(header='').strip()

    def _stream_authorized(self):
        try:
            current = auth.authenticate(self._session_token(), touch=False)
            return access.permits(current, 'developer')
        except auth.Denied:
            return False

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if getattr(self, 'command', 'GET') != 'HEAD':
            self.wfile.write(body)

    def _send_sse(self, filters=None):
        """Stream the selected local snapshot; disconnects are detected on write.

        A frame is only sent when the evidence actually moved. The obvious
        implementation -- re-serialise the snapshot on a timer -- costs a full
        document every interval whether or not the collector did anything, and
        this snapshot measured 40KB: one idle browser tab cost ~1.3MB/min at the
        default cadence, which is local loopback traffic but still real work on a
        laptop that also has a 30B model running on it.

        So the freshness key from the cache decides whether a frame is owed. When
        nothing changed the stream emits a tiny named `keepalive` event instead,
        which the client uses to prove the stream is alive without re-rendering.
        It has to be a real event rather than an SSE comment, because the client's
        staleness guard can only be refreshed by something it can observe, and a
        comment is invisible to EventSource by design.
        """
        current = cached_snapshot(filters=filters)
        key = (_freshness_key(), json.dumps(filters or {}, sort_keys=True))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.close_connection = True
        stop = getattr(self.server, "sse_stop", None) or threading.Event()
        interval = max(1, int(getattr(self.server, "refresh_seconds", 30)))
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.write(
                f"event: update\ndata: {json.dumps(current, default=str, allow_nan=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                if stop.wait(interval):
                    break
                if not self._stream_authorized():
                    break
                fresh_key = (_freshness_key(),
                             json.dumps(filters or {}, sort_keys=True))
                if fresh_key == key:
                    # Nothing observed changed. Stay observably alive for a few
                    # bytes rather than a re-render.
                    self.wfile.write(b"event: keepalive\ndata: 1\n\n")
                    self.wfile.flush()
                    continue
                key = fresh_key
                current = cached_snapshot(filters=filters)
                body = json.dumps(current, default=str, allow_nan=False)
                self.wfile.write(f"event: update\ndata: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception:
            # Headers have already been sent; sending a JSON error response
            # here would corrupt the stream. EventSource will reconnect.
            log.exception("dashboard event stream failed")

    def do_HEAD(self):
        if not self._local_host():
            return self._send(403, b"", "text/plain")
        """Health checkers and `curl -I` expect this. No body, same headers."""
        parsed = urlparse(self.path)
        if not parsed.path.startswith('/static/') and not self._require_access('HEAD', parsed.path):
            return
        code, ctype = (404, "application/json")
        if parsed.path in ("/", "/index.html"):
            code, ctype = 200, "text/html; charset=utf-8"
        elif parsed.path.startswith("/static/"):
            asset = web_asset(parsed.path[len("/static/"):])
            code, ctype = (200, WEB_TYPES[asset.suffix.lower()]) if asset else (404, None)
            if asset is None:
                ctype = "application/json"
        elif parsed.path == "/events":
            code, ctype = 200, "text/event-stream; charset=utf-8"
        elif parsed.path in ("/api/snapshot", "/api/opportunities", "/api/analysis", "/api/product-identity", "/api/focus", "/api/research",
                             "/api/research/candidates", "/api/research/candidate", "/api/research/proceeds",
                             "/api/research/sessions",
                             "/api/analyst", "/api/research/listing", "/api/research/provenance",
                             "/api/research/acquisition", "/api/compare", "/api/health",
                             "/api/listings", "/api/compliance", "/api/metrics", "/api/inventory",
                             "/api/permissions", "/api/connections", "/api/drafts", "/api/trades", "/api/outcomes", "/api/workflow", "/api/collection-health",
                             "/api/collection/capacity",
                             "/api/predict", "/api/operations", "/api/soldextract"):
            code = 200
        elif parsed.path == "/metrics":
            code, ctype = 200, observability.CONTENT_TYPE
        elif parsed.path in GET_ROUTES:
            code = 200
        body = b"" if self.command == "HEAD" else b"{}"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if not self._local_host():
            return self._send(403, json.dumps({"error": "loopback host required"}))
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not parsed.path.startswith('/static/') and not self._require_access('GET', parsed.path):
            return
        try:
            # `/static/` is a prefix, everything else is exact. One dict lookup
            # instead of a chain of `if path == ...`, so adding a route cannot
            # accidentally shadow an existing one by being spelled similarly.
            if parsed.path.startswith("/static/"):
                return self._route_static(parsed.path[len("/static/"):])
            handler = GET_ROUTES.get(parsed.path)
            if handler is None:
                return self._send(404, json.dumps({"error": "not found"}))
            if self.principal and self.principal['role'] == 'viewer':
                with db.read_snapshot():
                    result = handler(self, query)
            else:
                result = handler(self, query)
            if result is None:
                # The handler wrote its own response (the SSE stream).
                return
            if self.principal and self.principal['role'] == 'viewer' and parsed.path.startswith('/api/'):
                code, body, *rest = result
                result = (code, json.dumps(access.viewer_projection(json.loads(body)), default=str, allow_nan=False), *rest)
            return self._send(*_with_content_type(result))
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)[:200]}))
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s failed", parsed.path)
            return self._send(500, json.dumps({"error": 'The server could not complete this request.'}))

    def _route_static(self, name):
        asset = web_asset(name)
        if asset is None:
            return self._send(404, json.dumps({"error": "not found"}))
        return self._send(200, asset.read_bytes(), WEB_TYPES[asset.suffix.lower()])

    # -- GET routes -------------------------------------------------------
    # Each returns (status, body, content_type) or (status, body, ctype, extra).
    # `default=str` and `allow_nan=False` are the two conventions here: the
    # first because rows carry Decimals and datetimes, the second because JSON
    # has no NaN and a silently emitted one produces a document no browser
    # parser will accept.

    def _get_collection_control(self, query):
        try:
            result = collection_control.preview()
        except permissions.PermissionDenied as exc:
            result = {**collection_control.current(), 'can_start': False, 'start_reason': str(exc), 'active_run': collection_control.active()}
        return 200, json.dumps(result, default=str, allow_nan=False)

    def _get_collection_command(self, query):
        command_id = query.get('command_id', [''])[0]
        row = collection_control.command_result(command_id)
        if row['collector_key'] != collection_control.key():
            raise ValueError('command not found')
        return 200, json.dumps(row, default=str, allow_nan=False)

    def _get_collection_runs(self, query):
        return 200, json.dumps(collection_control.runs(query.get('after', [0])[0], query.get('limit', [20])[0]), default=str)

    def _get_collection_run(self, query):
        return 200, json.dumps(collection_control.detail(query.get('run_id', [''])[0]), default=str, allow_nan=False)

    def _get_collection_data(self, query):
        run_id = query.get('run_id', [''])[0]
        collection_control.detail(run_id)
        return 200, json.dumps(collection_events.data(run_id, query.get('after', [0])[0], query.get('limit', [50])[0], query.get('kind', [None])[0], query.get('direction', ['asc'])[0]), default=str, allow_nan=False)

    def _get_collection_export(self, query):
        run_id = query.get('run_id', [''])[0]
        run = collection_control.detail(run_id)
        resource = query.get('resource', ['events'])[0]
        output, cursor, more = collection_events.export(run_id, resource, query.get('after', [0])[0])
        if query.get('format', ['csv'])[0] == 'json':
            return 200, json.dumps({'csv': output, 'next_cursor': cursor, 'has_more': more,
                'manifest': {'run_id': run_id, 'settings': run['record'].get('settings'), 'scope': run['record'].get('context', {}).get('scope'),
                             'generated_at': collection_events.now(), 'basis': 'retained normalized evidence'}})
        return 200, output, 'text/csv; charset=utf-8', {
            'Content-Disposition': 'attachment; filename="collection-'+resource+'.csv"',
            'X-Next-Cursor': str(cursor), 'X-Has-More': '1' if more else '0',
            'X-Collection-Basis': 'normalized-evidence', 'X-Collection-Revision': str(run['record'].get('settings_revision', 0))}

    def _get_collection_events(self, query):
        run_id = query.get('run_id', [''])[0]
        collection_control.detail(run_id)
        options = {'severity': query.get('severity', [None])[0], 'stage': query.get('stage', [None])[0],
                   'event_type': query.get('event_type', [None])[0]}
        after = query.get('after', [0])[0]
        if query.get('stream', ['0'])[0] != '1':
            return 200, json.dumps(collection_events.read(run_id, after, query.get('limit', [100])[0], **options), default=str)
        if not self._same_origin():
            return 403, json.dumps({'error': 'same-origin requests required'})
        after = collection_events.bounded_int(self.headers.get('Last-Event-ID') or after, 0, 2**63-1)
        # Validate filters before sending stream headers.
        collection_events.read(run_id, after, 1, **options)
        if not self.server.acquire_stream_slot():
            return 503, json.dumps({'error': 'too many open event streams'})
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b'retry: 2000\n\n')
            stop = getattr(self.server, 'sse_stop', None) or threading.Event()
            while not stop.is_set():
                if not self._stream_authorized():
                    break
                page = collection_events.read(run_id, after, 100, **options)
                for event in page['events']:
                    body = json.dumps(event, default=str, allow_nan=False)
                    self.wfile.write(f"id: {event['id']}\nevent: collection\ndata: {body}\n\n".encode())
                after = page['next_cursor']
                run = collection_control.detail(run_id)
                self.wfile.write(('event: progress\ndata: '+json.dumps(run, default=str, allow_nan=False)+'\n\n').encode())
                self.wfile.write(b'event: keepalive\ndata: 1\n\n')
                self.wfile.flush()
                if not page['has_more'] and run['status'] != 'running':
                    break
                if not page['has_more']:
                    stop.wait(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception:
            log.exception('collection event stream failed')
        finally:
            self.server.release_stream_slot()
        return None

    def _post_collection_start(self, payload):
        return collection_reply(collection_control.start, payload)

    def _post_collection_preview(self, payload):
        return collection_reply(collection_control.preview, payload)

    def _post_collection_settings(self, payload):
        return collection_reply(collection_control.settings, payload)

    def _post_collection_control(self, payload):
        return collection_reply(collection_control.control, payload)

    def _get_index(self, query):
        html = PAGE.read_text()
        # Let the page poll at the cadence the operator chose.
        html = html.replace('data-refresh-seconds="30"',
                            f'data-refresh-seconds="{int(getattr(self.server, "refresh_seconds", 30))}"')
        html = html.replace('src="/static/dashboard.js"', 'src="/static/developer.js"')
        return 200, html, "text/html; charset=utf-8"

    def _get_login(self, query):
        return 200, PAGE.with_name('login.html').read_text(), 'text/html; charset=utf-8'

    def _get_viewer(self, query):
        return 200, PAGE.with_name('viewer.html').read_text(), 'text/html; charset=utf-8'

    def _get_healthz(self, query):
        return 200, json.dumps({'status': 'ok'}), None

    def _get_release(self, query):
        from . import release
        return 200, json.dumps(release.describe()), None

    def _get_me(self, query):
        try:
            principal = auth.authenticate(self._session_token())
            return 200, json.dumps({'user': auth.public_user(principal), 'csrf_token': principal['csrf_token']}), None
        except auth.Denied as exc:
            return exc.status, json.dumps({'error': str(exc)}), None

    def _get_auth_session(self, query):
        # Never 401. The login page probes this to skip the form when a session
        # already exists, and a browser logs every 4xx at network level, so an
        # honest "signed out" answer has to be a 200. `/api/auth/me` keeps its
        # 401 so a revoked session is still distinguishable to API clients.
        try:
            principal = auth.authenticate(self._session_token())
        except auth.Denied:
            return 200, json.dumps({'user': None}), None
        return 200, json.dumps({'user': auth.public_user(principal), 'csrf_token': principal['csrf_token']}), None

    def _get_users(self, query):
        return 200, json.dumps({'users': auth.list_users()}), None

    def _get_auth_activity(self, query):
        return 200, json.dumps({'events': auth.activity()}, default=str), None

    def _get_viewer_snapshot(self, query):
        filters = snapshot_filters(query)
        from . import metrics
        result = {'generated_at': datetime.now(timezone.utc).isoformat(), 'filters': filters,
                  'segments': segment_options(), 'panels': panels(filters),
                  'liquidity': metrics.liquidity(**filters), 'economics': purchases.summary(),
                  'data_quality': {'with_price': data_quality()['with_price']}}
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_viewer_forecasts(self, query):
        from . import forecast_evidence, metrics
        where, values, _ = metrics.listing_filters(**snapshot_filters(query))
        where.append('(EXISTS (SELECT 1 FROM sale_predictions s WHERE s.listing_pk=listings.listing_pk) '
                     'OR EXISTS (SELECT 1 FROM price_trajectories p WHERE p.listing_pk=listings.listing_pk))')
        rows = db.query('SELECT listing_pk FROM listings WHERE ' + ' AND '.join(where) +
                        ' ORDER BY listing_pk DESC LIMIT 8', values)
        result = forecast_evidence.view([row['listing_pk'] for row in rows]) if rows else {'items': [], 'note': 'No retained listing forecasts in this slice.'}
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _post_login(self, payload):
        if set(payload) != {'username', 'password'}:
            raise ValueError('Username and password are required.')
        try:
            token, result = auth.login(payload['username'], payload['password'])
            return 200, json.dumps(result), 'application/json', {'Set-Cookie': self._cookie(token)}
        except auth.Denied as exc:
            return exc.status, json.dumps({'error': str(exc)}), None

    def _post_logout(self, payload):
        auth.logout(self.principal)
        return 200, json.dumps({'ok': True}), 'application/json', {'Set-Cookie': self._cookie(clear=True)}

    def _post_password(self, payload):
        if set(payload) != {'current_password', 'new_password'}:
            raise ValueError('Current and new passwords are required.')
        auth.change_password(self.principal, **payload)
        return 200, json.dumps({'ok': True}), 'application/json', {'Set-Cookie': self._cookie(clear=True)}

    def _post_users(self, payload):
        if set(payload) != {'action', 'params'} or not isinstance(payload['params'], dict):
            raise ValueError('Account action and parameters are required.')
        params, action = payload['params'], payload['action']
        if action == 'create' and set(params) <= {'username', 'password', 'role'} and {'username', 'password'} <= set(params):
            auth.create_user(params['username'], params['password'], params.get('role', 'viewer'), self.principal['user_id'])
        elif action == 'update' and set(params) <= {'user_id', 'role', 'active', 'password'} and 'user_id' in params:
            auth.update_user(params['user_id'], self.principal['user_id'], **{k: v for k, v in params.items() if k != 'user_id'})
        else:
            raise ValueError('Unsupported account action or fields.')
        return 200, json.dumps({'users': auth.list_users()}), None

    def _get_events(self, query):
        if not self._same_origin():
            return self._send(403, json.dumps({"error": "same-origin requests required"}))
        if not self.server.acquire_stream_slot():
            # Each stream pins a thread for the lifetime of the connection; a
            # browser that reconnects faster than the old socket is reaped would
            # otherwise accumulate them without limit. EventSource retries on a
            # non-2xx, so refusing is self-correcting rather than a dead end.
            self._send(503, json.dumps({"error": "too many open event streams"}),
                       extra={"Retry-After": "10"})
            return None
        try:
            self._send_sse(snapshot_filters(query))
        finally:
            self.server.release_stream_slot()
        return None

    def _get_snapshot(self, query):
        return 200, json.dumps(cached_snapshot(filters=snapshot_filters(query)), default=str)

    def _get_opportunities(self, query):
        from . import opportunities
        result = opportunities.board(snapshot_filters(query), days=query.get("days", ["30"])[0],
                                     horizon=query.get("horizon", ["30"])[0], limit=query.get("limit", ["100"])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _post_opportunities(self, payload):
        from . import opportunities
        if not isinstance(payload, dict) or set(payload) != {"filters", "days", "horizon", "scenarios"}:
            raise ValueError("board scenario needs filters, days, horizon and scenarios")
        result = opportunities.board(**payload)
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_analysis(self, query):
        return 200, json.dumps(
            cached_analysis(snapshot_filters(query),
                            dimension=query.get("dimension", ["category_root_norm"])[0],
                            days=query.get("days", ["30"])[0],
                            horizon=query.get("horizon", ["30"])[0]),
            default=str, allow_nan=False), None

    def _get_focus(self, query):
        return 200, json.dumps(
            cached_focus(snapshot_filters(query), horizon=query.get("horizon", ["30"])[0]),
            default=str, allow_nan=False), None

    def _get_research(self, query):
        from . import research
        result = {"segments": research.segments(),
                  "journal": research.journal(query.get("decision", [""])[0],
                                              query.get("page", ["1"])[0],
                                              query.get("decision_pk", [None])[0])}
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_source_review(self, query):
        from . import source_review
        raw = query.get("listing_pk", [""])[0]
        if not raw.isdecimal():
            raise ValueError("a positive listing key is required")
        return 200, json.dumps(source_review.view(int(raw)), default=str, allow_nan=False), None

    def _get_item_workspace(self, query):
        from . import item_workspace
        identifiers = {key: values[0] for key, values in query.items() if key in item_workspace.SEEDS}
        result = item_workspace.view(**identifiers)
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_forecast_evidence(self, query):
        from . import forecast_evidence
        if set(query) != {'listing_pks'} or len(query['listing_pks']) != 1:
            raise ValueError('provide listing_pks only')
        result = forecast_evidence.view(query['listing_pks'][0].split(','))
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_scenario_workspace(self, query):
        from . import item_workspace, scenario_workspace
        seed = {key: values[0] for key, values in query.items() if key in item_workspace.SEEDS}
        result = scenario_workspace.context(seed) if seed else scenario_workspace.library()
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _post_scenario_workspace(self, payload):
        from . import scenario_workspace
        action = payload.get('action')
        if action not in ('evaluate', 'save') or set(payload) != {'action', 'params'} or not isinstance(payload['params'], dict):
            raise ValueError('scenario action and params are required')
        allowed = {'seed', 'revision', 'values', 'mode', 'origins', 'reused_from'} | ({'name'} if action == 'save' else set())
        if set(payload['params']) - allowed:
            raise ValueError('unsupported scenario fields')
        try:
            result = getattr(scenario_workspace, action)(**payload['params'])
        except scenario_workspace.Conflict as exc:
            return 409, json.dumps({'ok': False, 'error': str(exc)}), None
        except TypeError:
            return 400, json.dumps({'ok': False, 'error': 'scenario parameters are incomplete'}), None
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_candidates(self, query):
        """The research desk's shortlist: every qualifying item in the slice.

        Separate from `/api/analysis` because it answers a different question with
        a different budget. The analysis snapshot is the analyst view's own twelve
        best-supported comparisons inside a payload that also carries the market
        matrix, activity and portfolio; this is the queue a person works through,
        which is why it takes its own threshold, its own width and its own peer
        sample instead of being a field of somebody else's report.
        """
        from . import research_desk
        def value(name, default):
            return query.get(name, [default])[0]
        result = cached_desk(snapshot_filters(query),
                                          min_gap_pct=value("min_gap_pct", "20"),
                                          limit=value("limit", str(research_desk.DEFAULT_LIMIT)),
                                          peer_stats=value("peer_stats", "1") not in ("0", "false", "no"),
                                          peer_rows=value("peer_rows", "0"),
                                          horizon=value("horizon", "30"))
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_candidate(self, query):
        """One listing's cohort, its gaps and what still needs physical inspection.

        `min_gap_pct` is accepted here for the same reason the list takes it: an
        item opened from a 5%-gap list has to be judged by the 5% the reader
        chose, or the page will disagree with the list that linked to it.
        """
        from . import research_desk
        result = research_desk.candidate(query.get("listing_pk", [None])[0],
                                         peer_rows=query.get("peer_rows", [str(research_desk.DEFAULT_PEER_ROWS)])[0],
                                         horizon=query.get("horizon", ["30"])[0],
                                         min_gap_pct=query.get("min_gap_pct", ["20"])[0],
                                         filters=snapshot_filters(query),
                                         reference_at=query.get("reference_at", [None])[0],
                                         expected_revision=query.get("benchmark_revision", [None])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_proceeds(self, query):
        """Comparable owned resale evidence for one listing, if any exists.

        Split from the candidate page because it is the only read here that can
        take seconds rather than milliseconds: it counts paired owned trades, and
        on a young ledger that count is usually zero. A caller that wants the
        cheap view does not have to wait to discover there is no benchmark.
        """
        from . import research_desk
        result = research_desk.expected_proceeds(query.get("listing_pk", [None])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_sessions(self, query):
        from . import research_desk
        result = research_desk.session_status(query.get("name", [None])[0],
                                              min_gap_pct=query.get("min_gap_pct", ["20"])[0],
                                              horizon=query.get("horizon", ["30"])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_analyst_evaluation(self, query):
        from . import analyst_quality
        return 200, json.dumps(analyst_quality.report(), default=str, allow_nan=False), None

    def _get_product_identity(self, query):
        from . import product_identity
        return 200, json.dumps(product_identity.quality_report(snapshot_filters(query)),
                               default=str, allow_nan=False), None

    def _get_analyst(self, query):
        from . import analyst
        run_id = query.get("run_id", [""])[0]
        result = analyst.get(run_id) if run_id else {"runs": analyst.history(),
                                                      "enabled": config.ANALYST_ENABLED}
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_listing(self, query):
        from . import research
        result = research.listing_evidence(query.get("platform", ["tori"])[0],
                                           query.get("listing_id", [""])[0],
                                           filters=snapshot_filters(query),
                                           reference_at=query.get("reference_at", [None])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_provenance(self, query):
        from . import research
        result = research.listing_provenance(query.get("platform", ["tori"])[0],
                                             query.get("listing_id", [""])[0],
                                             filters=snapshot_filters(query),
                                             reference_at=query.get("reference_at", [None])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_acquisition(self, query):
        from . import research
        listing_id = query.get("listing_id", [""])[0]
        # `prefill=1` asks for the ready-to-submit record. The original shape
        # stays the default so an existing caller is unaffected.
        if query.get("prefill", ["0"])[0] in ("1", "true", "yes"):
            result = research.acquisition_prefill(query.get("platform", ["tori"])[0], listing_id)
        else:
            result = research.acquisition_context(query.get("platform", ["tori"])[0], listing_id)
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_research_acquisition_queue(self, query):
        from . import research
        limit = query.get("limit", ["50"])[0]
        if not str(limit).isdigit() or not 1 <= int(limit) <= 200:
            return self._send(400, json.dumps({"error": "limit must be 1..200"}))
        return self._send(200, json.dumps(research.acquisition_queue(int(limit)),
                                          default=str, allow_nan=False))

    def _get_compare(self, query):
        from . import research
        left = json.loads(query.get("left", ["{}"])[0])
        right = json.loads(query.get("right", ["{}"])[0])
        result = research.compare(left, right, query.get("horizon", ["30"])[0])
        return self._send(200, json.dumps(result, default=str, allow_nan=False))

    def _get_health(self, query):
        return 200, json.dumps(health(), default=str), None

    def _get_collection_health(self, query):
        from . import collection_health
        return 200, json.dumps(collection_health.view(), allow_nan=False), None

    def _get_collection_capacity(self, query):
        from . import followup
        return 200, json.dumps(followup.capacity_verdict(), default=str, allow_nan=False), None

    def _get_permissions(self, query):
        return 200, json.dumps(permissions.report()), None

    def _get_capabilities(self, query):
        # What this process answers. A reader role sees the reduced set, because
        # the seller projection has never carried the operational surface.
        reads, writes = access.all_routes()
        if self.principal and self.principal['role'] == 'viewer':
            reads &= access.VIEWER_GET | {path for path in reads if path in access.SELLER_GET}
            writes = set()
        return 200, json.dumps(
            access.deployment_manifest(reads, writes, 'runtime',
                                       'This workspace runs the research engine: collection, models, '
                                       'trades and every read are answered live.')), None

    def _get_connections(self, query):
        return 200, json.dumps(connections.status(query.get("environment", [None])[0])), None

    def _get_outcomes(self, query):
        from . import owned_outcomes
        purchase = query.get("purchase_pk", [""])[0]
        result = owned_outcomes.report(int(purchase)) if purchase else owned_outcomes.overview(query.get("page", [1])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_resale_preview(self, query):
        from . import realization
        listing_id = inventory._text(query.get("listing_id", [""])[0], "resale listing ID", 100)
        sold_at = trades._stamp(query.get("sold_on", [None])[0], "sale date")
        with db.read_snapshot():
            result = realization.freeze_resale_pairing(db.connect(), "tori", listing_id or None, sold_at)
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_trades(self, query):
        order_id = query.get("order_id", [""])[0]
        sku = query.get("sku", [""])[0]
        as_of = query.get("as_of", [None])[0]
        if order_id:
            result = trades.get(order_id)
        elif as_of and sku:
            result = trades.economics(sku, as_of)
        else:
            result = trades.listing(sku=sku, page=query.get("page", ["1"])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_workflow(self, query):
        return 200, json.dumps(workflow.report(query.get("page", ["1"])[0],
                                               query.get("sku", [""])[0]),
                                  default=str, allow_nan=False), None

    def _get_predict(self, query):
        return 200, json.dumps(predictions({k: v[0] for k, v in query.items()}),
                                  default=str, allow_nan=False), None

    def _get_operations(self, query):
        return 200, json.dumps(operations_report(), default=str, allow_nan=False), None

    def _get_soldextract(self, query):
        return 200, json.dumps(soldextract_report(query.get("keyword", [""])[0],
                                                  query.get("min_obs", ["3"])[0]),
                                  default=str, allow_nan=False), None

    def _get_drafts(self, query):
        draft_id = query.get("draft_id", [""])[0]
        if draft_id:
            result = drafts.get(draft_id)
        else:
            result = drafts.listing(sku=query.get("sku", [""])[0],
                                    page=query.get("page", ["1"])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_inventory(self, query):
        sku = query.get("sku", [""])[0]
        if sku:
            result = inventory.get(sku)
        else:
            result = inventory.listing(search=query.get("search", [""])[0],
                                       state=query.get("state", [""])[0],
                                       page=query.get("page", ["1"])[0])
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _get_listings(self, query):
        def value(name, default):
            return query.get(name, [default])[0]
        result = observations(snapshot_filters(query), search=value("search", ""),
                              sort=value("sort", "newest"), page=value("page", "1"),
                              page_size=value("page_size", "25"),
                              availability=value("availability", "all"))
        return 200, json.dumps(result, default=str), None

    def _get_compliance(self, query):
        return 200, json.dumps(compliance.compliance_report(), default=str), None

    def _get_metrics(self, query):
        # Prometheus text exposition. Served from the same loopback-only listener
        # as everything else, so it inherits the Innoweb boundary rather than
        # needing its own argument for why it is safe to expose.
        return 200, observability.render(), observability.CONTENT_TYPE

    def _get_metrics_json(self, query):
        return 200, json.dumps(observability.render_json(), default=str), None

    def do_POST(self):
        # Rejected requests may still have unread bytes in their request body.
        self.close_connection = True
        if not self._local_host():
            return self._send(403, json.dumps({"error": "loopback host required"}))
        parsed = urlparse(self.path)
        handler = POST_ROUTES.get(parsed.path)
        if handler is None:
            return self._send(404, json.dumps({"error": "not found"}))
        if not self._require_access('POST', parsed.path):
            return
        if not self._same_origin():
            return self._send(403, json.dumps({"error": "same-origin requests required"}))
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, json.dumps({"error": "application/json required"}))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, json.dumps({"error": "invalid body length"}))
        limit = POST_BODY_LIMITS.get(parsed.path, DEFAULT_BODY_LIMIT)
        if not 0 <= length <= limit:
            return self._send(413, json.dumps({"error": "body too large"}))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send(400, json.dumps({"error": "invalid json"}))
        if not isinstance(payload, dict):
            return self._send(400, json.dumps({"error": "JSON object required"}))
        try:
            result = handler(self, payload)
            if result is None:
                return
            return self._send(*_with_content_type(result))
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError as exc:
            return self._send(getattr(exc, 'status', 400), json.dumps({"ok": False, "error": str(exc)}))
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s failed", parsed.path)
            return self._send(500, json.dumps({"ok": False, "error": str(exc)[:200]}))

    # -- POST routes ------------------------------------------------------
    # Each takes (payload) and returns (status, body) or (status, body, ctype).

    def _post_collection_trim(self, payload):
        from . import followup
        params = payload.get("params", payload)
        if not isinstance(params, dict) or set(params) - {"target_active", "reason", "client_token", "platform"}:
            return self._send(400, json.dumps({"error": "unsupported or missing trim fields"}))
        try:
            result = followup.trim_cohort(params.get("target_active"), params.get("reason"),
                                          params.get("client_token"),
                                          platform=params.get("platform", "tori"))
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}))
        except RuntimeError as exc:
            return self._send(409, json.dumps({"error": str(exc)}))
        return 200, json.dumps({"ok": True, "result": result}, default=str, allow_nan=False), None

    def _post_predict(self, payload):
        from . import model_workspace
        try:
            result = (model_workspace.cancel(payload) if payload.get("action") == "cancel"
                      else model_workspace.start(payload, on_finish=invalidate))
        except model_workspace.Busy as exc:
            return 409, json.dumps({"ok": False, "error": str(exc)}), None
        return 202, json.dumps(result, allow_nan=False), None

    def _post_connections(self, payload):
        try:
            return 200, json.dumps(connections.action(payload)), None
        except EbayAPIError as exc:
            return 502, json.dumps({"ok": False, "error": str(exc)}), None
        except OSError:
            return 500, json.dumps({"ok": False, "error": "Private connection storage unavailable"}), None

    def _post_analyst(self, payload):
        from . import analyst
        try:
            result = analyst.action(payload)
        except analyst.Busy as exc:
            return 409, json.dumps({"ok": False, "error": str(exc)}), None
        return (202 if payload["action"] == "explain" else 200), json.dumps(result), None

    def _get_internet_research(self, query):
        from . import internet_research
        listing_pk = query.get("listing_pk", [None])[0]
        result = internet_research.report(listing_pk) if listing_pk is not None else internet_research.monitor()
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _post_internet_research(self, payload):
        from . import internet_research
        try:
            result = internet_research.action(payload)
        except internet_research.Busy as exc:
            return 409, json.dumps({"ok": False, "error": str(exc)}), None
        return (202 if payload["action"] == "run" else 200), json.dumps(result), None

    def _post_research(self, payload):
        from . import research, scenario_workspace
        try:
            result = research.action(payload)
        except scenario_workspace.Conflict as exc:
            return 409, json.dumps({"ok": False, "error": str(exc)}), None
        except ValueError as exc:
            return 400, json.dumps({"ok": False, "error": str(exc)}), None
        return 200, json.dumps(result, allow_nan=False), None

    def _post_research_session(self, payload):
        """Save or delete a research session. Never writes an observation or a price.

        A session is the operator's own working state: which slice they were
        looking at, which items they picked out of it, and what they assumed. It
        is kept in a separate route from `/api/research` so the journal's
        append-only decision record cannot be confused with a mutable note, and so
        the two do not share a body ceiling they were not sized for.
        """
        from . import research_desk
        params = payload.get("params", payload)
        if not isinstance(params, dict):
            return 400, json.dumps({"ok": False, "error": "params are required"}), None
        action = params.get("action")
        writers = {"save": (research_desk.save_session,
                            {"name", "filters", "selected_pks", "notes", "scenarios"}),
                   "delete": (research_desk.delete_session, {"session_pk"})}
        if not isinstance(action, str) or action not in writers:
            return 400, json.dumps({"ok": False, "error": "unsupported session action"}), None
        writer, allowed = writers[action]
        if set(params) - allowed - {"action"}:
            return 400, json.dumps({"ok": False, "error": "unsupported session fields"}), None
        try:
            return 200, json.dumps(writer(**{k: v for k, v in params.items() if k != "action"}),
                                   default=str, allow_nan=False), None
        except TypeError:
            # A required field the caller did not send. `research.action` maps the
            # same case to a 400 for the same reason: a write endpoint that answers
            # 500 for a malformed request tells the reader the server broke, when
            # the request did.
            return 400, json.dumps({"ok": False, "error": "required session fields are missing"}), None
        except ValueError as exc:
            return 400, json.dumps({"ok": False, "error": str(exc)[:300]}), None

    def _post_ledger(self, payload):
        try:
            result = ledger_action(payload)
        except (ValueError, TypeError) as exc:
            return 400, json.dumps({"ok": False, "error": str(exc)}), None
        return 200, json.dumps(result), None

    def _post_operations(self, payload):
        try:
            result = operations_action(payload)
        except ValueError as exc:
            return 400, json.dumps({"ok": False, "error": str(exc)}), None
        return 200, json.dumps(result, default=str), None

    def _post_soldextract(self, payload):
        try:
            result = soldextract_action(payload)
            invalidate()
        except inventory.Conflict as exc:
            return 409, json.dumps({"ok": False, "error": str(exc)[:300]}), None
        except ValueError as exc:
            return 400, json.dumps({"ok": False, "error": str(exc)[:300]}), None
        return 200, json.dumps(result), None

    def _post_action(self, payload):
        action = payload.get("action")
        if not isinstance(action, str):
            return 400, json.dumps({"error": "action required"}), None
        result = run_action(action, payload.get("params", {}))
        return (200 if result.get("ok") else 400), json.dumps(result), None

    def _owned_action(self, action, payload, *, invalidate_on_success=True):
        """The shared shape of the three owned-ledger write endpoints.

        `trades`, `drafts` and `inventory` differ only in which module they call
        and have identical error semantics: a `Conflict` is a 409 and everything
        else the caller got wrong is a 400. Spelling that out once means a
        fourth ledger cannot accidentally get a different mapping.
        """
        try:
            result = action(payload)
            if invalidate_on_success:
                invalidate()
        except inventory.Conflict as exc:
            return 409, json.dumps({"ok": False, "error": str(exc)}), None
        except (ValueError, TypeError) as exc:
            return 400, json.dumps({"ok": False, "error": str(exc)}), None
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _post_outcomes(self, payload):
        from . import owned_outcomes
        return self._owned_action(owned_outcomes.action, payload)

    def _post_source_review(self, payload):
        from . import source_review
        preview = isinstance(payload, dict) and payload.get("action") == "preview"
        return self._owned_action(source_review.action, payload, invalidate_on_success=not preview)

    def _post_trades(self, payload):
        return self._owned_action(trades.action, payload)

    def _post_drafts(self, payload):
        return self._owned_action(drafts.action, payload)

    def _post_inventory(self, payload):
        return self._owned_action(inventory.action, payload)


# Route tables, declared after `Handler` so the methods they name exist. Kept as
# data rather than as `if path == ...` chains: a route is now findable by grep,
# a mistyped path is a KeyError at startup rather than a 404 in production, and
# `tests/test_dashboard_routes.py` walks these dicts to assert every entry
# resolves to a real method.
GET_ROUTES = {
    '/login': Handler._get_login,
    '/developer': Handler._get_index,
    '/viewer': Handler._get_viewer,
    '/healthz': Handler._get_healthz,
    '/api/auth/me': Handler._get_me,
    '/api/release': Handler._get_release,
    '/api/auth/session': Handler._get_auth_session,
    '/api/auth/users': Handler._get_users,
    '/api/auth/activity': Handler._get_auth_activity,
    '/api/viewer/snapshot': Handler._get_viewer_snapshot,
    '/api/viewer/forecasts': Handler._get_viewer_forecasts,
    "/api/research/internet": Handler._get_internet_research,
    '/api/collection/control': Handler._get_collection_control,
    '/api/collection/command': Handler._get_collection_command,
    '/api/collection/runs': Handler._get_collection_runs,
    '/api/collection/run': Handler._get_collection_run,
    '/api/collection/events': Handler._get_collection_events,
    '/api/collection/data': Handler._get_collection_data,
    '/api/collection/export': Handler._get_collection_export,
    "/": Handler._get_index,
    "/index.html": Handler._get_index,
    "/events": Handler._get_events,
    "/api/snapshot": Handler._get_snapshot,
    "/api/opportunities": Handler._get_opportunities,
    "/api/analysis": Handler._get_analysis,
    "/api/product-identity": Handler._get_product_identity,
    "/api/focus": Handler._get_focus,
    "/api/research": Handler._get_research,
    "/api/research/source-review": Handler._get_source_review,
    "/api/item-workspace": Handler._get_item_workspace,
    "/api/forecast-evidence": Handler._get_forecast_evidence,
    "/api/research/scenarios": Handler._get_scenario_workspace,
    "/api/research/candidates": Handler._get_research_candidates,
    "/api/research/candidate": Handler._get_research_candidate,
    "/api/research/proceeds": Handler._get_research_proceeds,
    "/api/research/sessions": Handler._get_research_sessions,
    "/api/analyst/evaluation": Handler._get_analyst_evaluation,
    "/api/analyst": Handler._get_analyst,
    "/api/research/listing": Handler._get_research_listing,
    "/api/research/provenance": Handler._get_research_provenance,
    "/api/research/acquisition": Handler._get_research_acquisition,
    "/api/research/acquisition-queue": Handler._get_research_acquisition_queue,
    "/api/compare": Handler._get_compare,
    "/api/health": Handler._get_health,
    "/api/collection-health": Handler._get_collection_health,
    "/api/collection/capacity": Handler._get_collection_capacity,
    "/api/permissions": Handler._get_permissions,
    "/api/capabilities": Handler._get_capabilities,
    "/api/connections": Handler._get_connections,
    "/api/outcomes": Handler._get_outcomes,
    "/api/trades/resale-preview": Handler._get_resale_preview,
    "/api/trades": Handler._get_trades,
    "/api/workflow": Handler._get_workflow,
    "/api/predict": Handler._get_predict,
    "/api/operations": Handler._get_operations,
    "/api/soldextract": Handler._get_soldextract,
    "/api/drafts": Handler._get_drafts,
    "/api/inventory": Handler._get_inventory,
    "/api/listings": Handler._get_listings,
    "/api/compliance": Handler._get_compliance,
    "/metrics": Handler._get_metrics,
    "/api/metrics": Handler._get_metrics_json,
}

POST_ROUTES = {
    '/api/auth/login': Handler._post_login,
    '/api/auth/logout': Handler._post_logout,
    '/api/auth/password': Handler._post_password,
    '/api/auth/users': Handler._post_users,
    "/api/research/internet/action": Handler._post_internet_research,
    '/api/collection/start': Handler._post_collection_start,
    '/api/collection/preview': Handler._post_collection_preview,
    '/api/collection/settings': Handler._post_collection_settings,
    '/api/collection/control': Handler._post_collection_control,
    "/api/action": Handler._post_action,
    "/api/ledger": Handler._post_ledger,
    "/api/research": Handler._post_research,
    "/api/research/session": Handler._post_research_session,
    "/api/research/scenarios": Handler._post_scenario_workspace,
    "/api/opportunities": Handler._post_opportunities,
    "/api/analyst": Handler._post_analyst,
    "/api/inventory": Handler._post_inventory,
    "/api/connections": Handler._post_connections,
    "/api/drafts": Handler._post_drafts,
    "/api/outcomes": Handler._post_outcomes,
    "/api/research/source-review": Handler._post_source_review,
    "/api/trades": Handler._post_trades,
    "/api/operations": Handler._post_operations,
    "/api/predict": Handler._post_predict,
    "/api/soldextract": Handler._post_soldextract,
    "/api/collection/trim": Handler._post_collection_trim,
}

# A body that can carry a CSV capture is a different order of magnitude from one
# that carries an action name, so the ceiling is per-route rather than global.
# Every POST route must appear here: `tests/test_dashboard_routes.py` fails if
# one does not, because a route that silently inherits the smallest ceiling is
# restrictive by accident rather than by decision.
DEFAULT_BODY_LIMIT = 4096
POST_BODY_LIMITS = {
    '/api/auth/login': 4096,
    '/api/auth/logout': 4096,
    '/api/auth/password': 4096,
    '/api/auth/users': 4096,
    "/api/research/internet/action": 8192,
    '/api/collection/start': 8192,
    '/api/collection/preview': 8192,
    '/api/collection/settings': 8192,
    '/api/collection/control': DEFAULT_BODY_LIMIT,
    "/api/soldextract": IMPORT_MAX_BODY,
    "/api/inventory": 32768,
    "/api/drafts": 32768,
    "/api/opportunities": 65536,
    "/api/outcomes": 32768,
    "/api/trades": 32768,
    "/api/connections": 16384,
    "/api/research": 8192,
    "/api/research/scenarios": 8192,
    "/api/research/source-review": 16384,
    "/api/research/session": 16384,
    "/api/collection/trim": 8192,
    # Action dispatch, journal writes and model-cancellation requests are all
    # small named payloads; 8KB leaves room for a note without being an
    # invitation.
    "/api/action": DEFAULT_BODY_LIMIT,
    "/api/analyst": DEFAULT_BODY_LIMIT,
    "/api/ledger": DEFAULT_BODY_LIMIT,
    "/api/operations": DEFAULT_BODY_LIMIT,
    "/api/predict": DEFAULT_BODY_LIMIT,
}


def _with_content_type(result):
    """Normalise a route return to what `_send` expects.

    Routes return `(code, body)` or `(code, body, ctype)`. The trailing `None`
    that a route writes when it means "the default JSON content type" is
    dropped here rather than passed through, because `_send`'s default is
    `"application/json"` and passing `None` explicitly would send it verbatim.
    """
    code, body, *rest = result
    ctype = rest[0] if rest else None
    return (code, body) if ctype is None else (code, body, *rest)


class _BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a backlog and a ceiling on long-lived streams.

    Two limits the stdlib leaves open. `request_queue_size` defaults to 5, so a
    burst of parallel requests -- the dashboard issues five on load -- can be
    refused at the connect() stage rather than queued. And each `/events` stream
    occupies a thread until the process exits, so those are counted separately
    and refused with 503 once the cap is reached.
    """

    daemon_threads = True
    request_queue_size = 64
    max_streams = 32

    def __init__(self, *args, **kwargs):
        self._streams = 0
        self._streams_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def acquire_stream_slot(self):
        with self._streams_lock:
            if self._streams >= self.max_streams:
                return False
            self._streams += 1
            return True

    def release_stream_slot(self):
        with self._streams_lock:
            if self._streams > 0:
                self._streams -= 1


def serve(host="127.0.0.1", port=8787, open_browser=False, refresh_seconds=30):
    """Bind and run. Refuses any non-loopback interface."""
    host = compliance.assert_not_exposed(host)
    collection_control.recover()
    httpd = _BoundedHTTPServer((host, port), Handler)
    httpd.refresh_seconds = max(1, int(refresh_seconds))
    httpd.sse_stop = threading.Event()
    from . import internet_research
    threading.Thread(target=internet_research.watch_worker, args=(httpd.sse_stop,), daemon=True).start()
    url = f"http://{host}:{port}/"
    if refresh_seconds != 30:
        url += f"?refresh={refresh_seconds}"
    # flush on every line: this is commonly started detached, and a buffered
    # banner is a banner nobody sees.
    for line in (
            f"toriresearch dashboard on {url}",
            "  bound to loopback only; refusing any other interface",
            f"  auto-refresh every {refresh_seconds}s",
            "  reads stored observations; never queries the platform for you",
            "  SSE events: /events (Server-Sent Events for real-time updates)",
            "  compliance: "
            + ("OK" if compliance.compliance_report()["ready_for_collection"]
               else "BLOCKED — see /api/compliance")):
        print(line, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.sse_stop.set()
        httpd.server_close()
        from . import model_workspace
        model_workspace.shutdown()
    return 0
