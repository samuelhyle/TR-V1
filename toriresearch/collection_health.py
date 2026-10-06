"""Durable cycle measurements and a clock-driven, read-only freshness check."""

import hashlib
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import backups, config, db
from .metrics import parse_ts

LOCAL = ZoneInfo("Europe/Helsinki")
VERSION = 1
NUMERIC_STATS = frozenset((
    "requests_made", "rows_ingested", "listings_new", "probed", "requests", "entries",
    "seen", "new", "new_in_scope", "reobserved", "page_overlap", "discovery_pages", "sitemap_requests",
    "capped_traversals", "abandoned_traversals", "pages_from_docs", "pages_from_itemlist", "targets", "live", "stored", "condition_filled",
    "category_filled", "sold_inferred", "withdrawn", "expired", "blocked", "throttled", "cohort_admitted",
    "first_probe_attempts", "repeat_probe_attempts", "first_probe_successes",
    "repeat_probe_successes", "attempted", "classified", "abstained", "errors",
    "skipped_no_provider", "schema_violations", "accepted", "price_changed", "terminal_events", "raw", "audit", "listings",
    "queued", "recovered_leases", "runtime_limited", "stale_results", "parser_checks",
    "changes_processed", "repair_scanned", "cached_reused", "model_requests", "runtime_deferred", "vocabulary_migrated"))


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def context():
    from . import collection_control
    settings = json.loads(os.environ['TORI_COLLECTION_PROFILE_JSON']) if os.environ.get('TORI_COLLECTION_PROFILE_JSON') else collection_control.current()['settings']
    scope = {"categories": sorted(settings['categories']),
             "municipalities": sorted(settings['municipalities'])}
    policy = {"item_budget": settings['item_budget'], "sweep_budget": settings['sweep_budget'],
              "sweep_pages": settings['sweep_pages'], "enrich_budget": settings['enrich_budget'],
              "source_rps": settings['source_rps'],
              "repeat_share": config.PROBE_REPEAT_SHARE,
              "probe_interval_hours": config.PROBE_INTERVAL_HOURS,
              "interval_seconds": config.COLLECTION_INTERVAL_SECONDS,
              "item_rps": settings['item_rps'],
              "followup_enabled": config.FOLLOWUP_ENABLED,
              "followup_max_active": config.FOLLOWUP_MAX_ACTIVE,
              "followup_version": "followup-stratified-v1", "discovery_version": "discovery-rotation-v2",
              "national_focus_discovery":config.FOCUS_NATIONAL_DISCOVERY,"national_discovery_share":config.SWEEP_NATIONAL_SHARE,
              "enrichment_version": "enrichment-queue-v3", "classification_fresh_share":config.ENRICH_FRESH_SHARE,
              "parser_version": "parser-checks-v1",
              "aggregate_version": "daily-history-v1",
              "coordinator_cadence_version": "continuous-jobs-v1",
              "collection_runtime_seconds": config.COLLECTION_RUNTIME_SECONDS,
              "collection_report_seconds": config.COLLECTION_REPORT_SECONDS,
              "enrichment_repair_batch": config.ENRICH_REPAIR_BATCH,
              "enrichment_repair_batches": config.ENRICH_REPAIR_BATCHES,
              "latency_version": "probe-latency-hours-v1",
              "review_version": "collection-review-v1",
              "run_origin_version": "scheduled-origin-v1",
              "enrichment_runtime_seconds": config.ENRICH_RUNTIME_SECONDS,
              "sweep_refresh_hours": config.SWEEP_REFRESH_HOURS,
              "sweep_productive_refresh_hours": config.SWEEP_PRODUCTIVE_REFRESH_HOURS,
              "sweep_max_refresh_hours": config.SWEEP_MAX_REFRESH_HOURS,
              "sweep_traversal_max_age_hours": config.SWEEP_TRAVERSAL_MAX_AGE_HOURS}
    return {"source_key": backups.source_key(), "scope_key": _hash(scope),
            "policy_key": _hash(policy), "scope": scope, "policy": policy}


def sanitize_stats(stats, include_verticals=True):
    if not isinstance(stats, dict):
        return {}
    result = {key: value for key, value in stats.items() if key in NUMERIC_STATS and
              isinstance(value, (int, float)) and not isinstance(value, bool) and
              math.isfinite(value) and value >= 0}
    if stats.get("status") in ("ok", "error", "blocked", "throttled", "skipped"):
        result["status"] = stats["status"]
    identity = stats.get("classification_identity_sha256")
    if isinstance(identity, str) and re.fullmatch(r"[a-f0-9]{64}", identity):
        result["classification_identity_sha256"] = identity
    for name in ("first_delay_hours", "repeat_lateness_hours"):
        histogram = stats.get(name)
        if isinstance(histogram, dict):
            result[name] = {str(hour): count for hour, count in histogram.items()
                            if str(hour) in {str(i) for i in range(169)} and
                            isinstance(count, int) and not isinstance(count, bool) and count > 0}
    if include_verticals and isinstance(stats.get("by_vertical"), dict):
        result["by_vertical"] = {path: sanitize_stats(values, False) for path, values in
                                 stats["by_vertical"].items() if path in config.FOCUS_CATEGORIES}
    return result


def record_latency(stats, row, observed_at):
    """Bounded success histograms, never listing timestamps or identities."""
    observed = parse_ts(observed_at)
    repeat = parse_ts(row.get("last_success_at"))
    enrolled = parse_ts(row.get("enrolled_at"))
    due = repeat + timedelta(hours=config.PROBE_INTERVAL_HOURS) if repeat else enrolled
    if not observed or not due or observed < (repeat or enrolled):
        return
    hour = min(168, math.floor(max(0, (observed-due).total_seconds()/3600)))
    name = "repeat_lateness_hours" if repeat else "first_delay_hours"
    stats[name][str(hour)] = stats[name].get(str(hour), 0)+1


def history():
    records, malformed = {}, 0
    path = config.DATA_DIR / "collection_runs.jsonl"
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or not isinstance(record.get("stages"), list):
                        raise ValueError("invalid cycle record")
                    if not parse_ts(record.get("finished_at")):
                        raise ValueError("incomplete cycle record")
                    key = record.get("run_id") or (record.get("started_at"), record.get("pid"))
                    records[key] = record  # An accidentally replayed cycle is counted once.
                except (ValueError, TypeError):
                    malformed += 1
    except FileNotFoundError:
        pass
    except OSError:
        malformed += 1
    # Durable slots also retain interrupted owners that never appended JSONL.
    # Prefer their canonical record when both publication paths contain a run.
    from . import continuous
    if continuous.available():
        for row in db.query("SELECT record_json FROM collection_cycles WHERE status<>'running'"):
            try:
                record = json.loads(row['record_json'])
                if parse_ts(record.get('finished_at')):
                    records[record['run_id']] = record
            except (ValueError, KeyError, TypeError):
                malformed += 1
    return list(records.values()), malformed


def _midnight(day):
    return datetime.combine(day, datetime.min.time(), tzinfo=LOCAL)


def _sources(run):
    result = defaultdict(lambda: defaultdict(float))
    for stage in run["stages"]:
        if not isinstance(stage, dict):
            continue
        name = stage.get("stage")
        if name not in ("atom", "sweep", "itempage", "enrich", "retention"):
            continue
        stats = sanitize_stats(stage.get("stats"))
        row = result[name]
        row["runs"] += 1
        row["failed_runs"] += bool(stage.get("exit_code"))
        row["skipped_runs"] += stats.get("status") == "skipped"
        for key, value in stats.items():
            if key in NUMERIC_STATS:
                row[key] += value
        start, end = parse_ts(stage.get("started_at")), parse_ts(stage.get("finished_at"))
        if start and end and end >= start:
            row["seconds"] += (end - start).total_seconds()
    return {name: dict(values) for name, values in result.items()}


def summary(coverage, reference=None, records=None):
    reference = reference or datetime.now(timezone.utc)
    current = context()
    if records is None:
        records, malformed = history()
    else:
        malformed = 0
    # Legacy runs remain in the audit history. They cannot invent measurements
    # they did not record or establish capacity for another scope/budget/DB.
    matching = [run for run in records if not run.get('mixed_pace') and run.get("measurement_version") == VERSION
                and run.get("context") == current
                and parse_ts(run.get("started_at"))
                and parse_ts(run["finished_at"]) <= reference]
    today = reference.astimezone(LOCAL).date()
    window_start = today - timedelta(days=7)
    first = min((parse_ts(run["started_at"]).astimezone(LOCAL).date() for run in matching), default=today)
    complete_start = max(window_start, first + timedelta(days=1))
    daily = {str(window_start + timedelta(days=i)): {
        "date": str(window_start + timedelta(days=i)), "cycles": 0,
        "completed_ok": 0, "paused": 0, "errors": 0, "degraded": 0,
        "sources": {}, "by_vertical": {},
        "complete_tracking_day": complete_start <= window_start + timedelta(days=i) < today}
        for i in range(8)}
    for run in matching:
        # Attribute each cycle once by its completion date, including midnight runs.
        day = str(parse_ts(run["finished_at"]).astimezone(LOCAL).date())
        if day not in daily:
            continue
        row = daily[day]
        row["cycles"] += 1
        row["completed_ok"] += run.get("status") in ("ok", "degraded")
        field = {"paused": "paused", "error": "errors", "partial": "errors", "interrupted": "errors", "degraded": "degraded"}.get(run.get("status"))
        if field:
            row[field] += 1
        row["followup"] = run.get("followup")
        for name, values in _sources(run).items():
            dest = row["sources"].setdefault(name, {})
            for key, value in values.items():
                dest[key] = dest.get(key, 0) + value
        for stage in run["stages"]:
            if not isinstance(stage, dict):
                continue
            for path, stats in sanitize_stats(stage.get("stats")).get("by_vertical", {}).items():
                dest = row["by_vertical"].setdefault(path, {}).setdefault(stage["stage"], {})
                for key, value in stats.items():
                    if key in NUMERIC_STATS:
                        dest[key] = dest.get(key, 0) + value
    complete = [row for row in daily.values() if row["complete_tracking_day"]]
    attempts = sum(row["sources"].get("itempage", {}).get("requests", 0) for row in complete)
    successes = sum(row["sources"].get("itempage", {}).get("stored", 0) for row in complete)
    new = sum(row["sources"].get("sweep", {}).get("new_in_scope", 0) for row in complete)
    first_success = sum(row["sources"].get("itempage", {}).get("first_probe_successes", 0) for row in complete)
    repeat_success = sum(row["sources"].get("itempage", {}).get("repeat_probe_successes", 0) for row in complete)
    expected = sum((_midnight(datetime.fromisoformat(row["date"]).date() + timedelta(days=1)).astimezone(timezone.utc)
                    - _midnight(datetime.fromisoformat(row["date"]).date()).astimezone(timezone.utc)).total_seconds()
                   / config.COLLECTION_INTERVAL_SECONDS for row in complete)
    cycles = sum(row["cycles"] for row in complete)
    observed = attempts / len(complete) if complete else None
    ceiling = config.ITEM_BUDGET * 86400 / config.COLLECTION_INTERVAL_SECONDS
    interval = config.PROBE_INTERVAL_HOURS
    cohort = coverage.get("followup", {})
    followed = cohort.get("active", 0) if cohort.get("enabled") else coverage.get("n_unresolved", 0)
    repeat_demand = followed * 24 / interval if interval > 0 else None
    errors = max(0, attempts - successes)
    retry_share = errors / attempts if attempts else None
    arrivals = new / len(complete) if complete else None
    admissions = sum(row["sources"].get("itempage", {}).get("cohort_admitted", 0) for row in complete)/len(complete) if complete else None
    first_demand = admissions if cohort.get("enabled") else arrivals
    demand = repeat_demand + first_demand if repeat_demand is not None and first_demand is not None else None
    ready = len(complete) == 7 and not malformed
    return {"generated_at": reference.isoformat(), "context": current,
            "daily": list(daily.values()), "complete_days": len(complete),
            "estimate_ready": ready, "malformed_history_records": malformed,
            "legacy_or_other_context_runs": len(records) - len(matching),
            "capacity": {"daily_attempt_ceiling": ceiling,
                         "measured_attempts_per_day": observed,
                         "measured_successes_per_day": successes / len(complete) if complete else None,
                         "first_successes": first_success, "repeat_successes": repeat_success,
                         "focused_arrivals_per_day": arrivals, "retry_fraction": retry_share,
                         "cohort_admissions_per_day": admissions,
                         "repeat_demand_per_day": repeat_demand,
                         "all_unresolved_repeat_demand_per_day": coverage.get("n_unresolved", 0)*24/interval,
                         "minimum_demand_per_day": demand,
                         "safe_attempts_per_day": observed * 0.8 if ready else None,
                         "repeat_demand_exceeds_ceiling": repeat_demand is not None and repeat_demand > ceiling,
                         "scheduled_cycle_coverage": min(1, cycles / expected) if expected else None},
            "note": "Full Helsinki calendar days only; the first tracking day and today are partial. "
                    "Capacity needs seven complete days for the current database, scope and policy. "
                    "Missing scheduled cycles reduce measured capacity. Arrivals count entries into focus through search, "
                    "including listings first seen in Atom; "
                    "the uncategorized Atom sample is separate. Demand excludes additional retry work. "
                    "Per-vertical totals overlap if focus paths overlap. Manual CLI stages are not cycle totals."}


def detail_due_at(moment, platform="tori"):
    """Follow-up work that was due at `moment`, on the terms the detail stage uses.

    The same predicate `followup.candidates` selects on, so "was there work?" is
    answered by the queue itself rather than by a summary written later.

    Returns None when the queue cannot be read, so a caller falls back to the
    end-of-cycle report rather than reporting a detail failure on the strength of
    a query that did not run.
    """
    if moment is None:
        return None
    try:
        row = db.scalar(
            "SELECT COUNT(*) FROM followup_tasks t "
            "JOIN listings l ON l.listing_pk = t.listing_pk "
            "JOIN followup_enrollments e ON e.enrollment_id = t.enrollment_id "
            "WHERE l.platform = ? AND l.outcome_status = 'unknown' "
            "AND t.state = 'ready' AND t.due_at <= ?",
            (platform, moment.isoformat()))
    except Exception:  # noqa: BLE001 - an unreadable queue is not a detail failure
        return None
    return int(row or 0)


def freshness(reference=None):
    """Read persisted evidence using the caller's clock; no report writes or source requests."""
    reference = reference or datetime.now(timezone.utc)
    current = context()
    pipeline = read_json(config.DATA_DIR / "collection_status.json") or {}
    report = read_json(config.REPORTS_DIR / "collection_health.json") or {}
    from .http import collection_stop
    pause = collection_stop()
    issues = []
    locks = db.active_locks()
    running = pipeline.get("status") == "running" and "collection" in locks
    records, _malformed = history()
    completed = [parse_ts(run["finished_at"]) for run in records
                 if run.get("context") == current and parse_ts(run["finished_at"]) <= reference]
    stamp = max(completed, default=None)
    if not running and pipeline.get("context") == current:
        stamp = parse_ts(pipeline.get("finished_at")) or stamp
    if stamp is None:
        # A policy rollout invalidates capacity evidence, but a recent cycle
        # still demonstrates host liveness for this database. Keep those facts
        # separate instead of declaring an outage at every settings change.
        prior = [parse_ts(run.get("finished_at")) for run in [*records, pipeline]
                 if (run.get("context") or {}).get("source_key") == current["source_key"]
                 and parse_ts(run.get("finished_at")) is not None
                 and parse_ts(run.get("finished_at")) <= reference]
        stamp = max(prior, default=None)
    age = (reference - stamp).total_seconds() / 3600 if stamp else None
    threshold = 2 * config.COLLECTION_INTERVAL_SECONDS / 3600
    if pipeline.get("context") != current:
        issues.append({"code": "unmeasured_configuration", "message": "No completed measurement for the current collection settings."})
    if not pause and (age is None and not running or age is not None and (age < 0 or age >= threshold)):
        issues.append({"code": "stale_cycle", "message": "No recent completed collection cycle."})
    if pipeline.get("status") == "running" and not running:
        issues.append({"code": "interrupted_cycle", "message": "The last running cycle no longer owns its lock."})
    if pipeline.get("status") in ("error", "degraded"):
        issues.append({"code": "cycle_" + pipeline["status"], "message": "The latest collection cycle needs review: " + pipeline["status"] + "."})
    if running:
        start = parse_ts(pipeline.get("started_at"))
        if not start or (reference - start).total_seconds() > config.COLLECTION_RUNTIME_SECONDS:
            issues.append({"code": "overdue_cycle", "message": "The active cycle has exceeded its work window."})
    coverage = report.get("coverage", {}) if report.get("context") == current else {}
    cohort = coverage.get("followup", {})
    stages = [stage for stage in pipeline.get("stages", []) if stage.get("stage") == "itempage"]
    detail_finished = parse_ts(stages[-1].get("finished_at")) if stages else None
    due_work = cohort.get("due", 0) if cohort.get("enabled") else coverage.get("n_due_in_scope", 0)
    if not pause and not running and pipeline.get("context") == current and due_work > 0:
        # Ask whether work was due when the detail stage decided, not when this
        # report was written. The two differ by the length of a cycle, and the
        # difference is enough to invent a contradiction: measured on 3 October
        # 2026 the stage ran at 12:30:33 and found nothing due, the first task
        # became due at 12:31:08, and the report written at 12:34:54 counted 97
        # due -- so a stage that had correctly idled was reported as having
        # dropped detail work it was never offered.
        due_at_decision = detail_due_at(detail_finished) if detail_finished is not None else None
        if due_at_decision is not None:
            due_work = due_at_decision
        if due_work > 0 and stages and not stages[-1].get("stats", {}).get("stored", 0):
            issues.append({"code": "no_usable_detail_writes", "message": "Detail work was due when the last cycle's detail stage ran, and it stored nothing."})
    capacity = report.get("capacity", {}) if report.get("context") == current else {}
    completed_days = [day for day in capacity.get("daily", []) if day.get("complete_tracking_day") and day.get("followup")]
    if len(completed_days) >= 4:
        backlogs = [day["followup"].get("cohort", {}).get("overdue_repeat", day["followup"].get("cohort", {}).get("due_repeat", 0)) if config.FOLLOWUP_ENABLED else
                    day["followup"].get("n_due_repeat_in_scope", 0) for day in completed_days[-4:]]
        if all(b > a for a, b in zip(backlogs, backlogs[1:])):
            issues.append({"code": "repeat_backlog_growing", "message": "Repeat backlog grew in three consecutive daily reviews."})
    backup = backups.status(reference)
    dated = report.get("history", {}) if report.get("context") == current else {}
    if report.get("context") == current and "history" in report:
        snapshot_at = parse_ts(dated.get("latest_snapshot_at"))
        if snapshot_at is None:
            issues.append({"code": "history_missing", "message": "No daily research snapshot has been recorded for these settings."})
        elif (reference-snapshot_at).total_seconds() >= 26*3600:
            issues.append({"code": "history_stale", "message": "The daily research snapshot is more than 26 hours old."})
        tracked_since = dated.get("tracking_started_day")
        recent_cutoff = (reference.date()-timedelta(days=7)).isoformat()
        missing_days = [day["day"] for day in dated.get("days", []) if day.get("status") == "missing"
                        and tracked_since and max(tracked_since, recent_cutoff) <= day["day"] < reference.date().isoformat()]
        if missing_days:
            issues.append({"code": "history_missing_days", "message": f"Daily research history has {len(missing_days)} missing days in the past week."})
    if backup["status"] != "ok":
        issues.append({"code": "backup_" + backup["status"], "message": "A recent verified recovery backup is unavailable."})
    from . import continuous
    job_status = continuous.jobs(reference)
    jobs = job_status['jobs']
    cycle = job_status.get('latest_cycle') or {}
    heartbeat = parse_ts(cycle.get('heartbeat_at'))
    if running and heartbeat and (reference-heartbeat).total_seconds()>120:
        issues.append({'code':'stale_heartbeat','message':'The active collector has stopped reporting progress.'})
    observations = [parse_ts(item.get('last_observation_at')) for name,item in jobs.items() if name in continuous.SOURCE_JOBS]
    observed = max((stamp for stamp in observations if stamp),default=None)
    if not pause and observed and (reference-observed).total_seconds() >= threshold*3600:
        issues.append({'code':'stale_observations','message':'No recent committed marketplace observations.'})
    from . import collection_control
    operator_paused = not collection_control.current()['collection_enabled']
    return {"checked_at": reference.isoformat(),
            "status": "paused" if pause or operator_paused else "stale" if any(issue["code"] in
                      ("stale_cycle", "interrupted_cycle", "overdue_cycle", "stale_observations", "stale_heartbeat") for issue in issues)
                      else "warning" if issues else "running" if running else "ok",
            "cycle_age_hours": round(age, 2) if age is not None else None,
            "platform_pause": (pause or {}).get("status"), "operator_paused": operator_paused, "backup": backup, "issues": issues,
            "jobs": jobs, 'elapsed_slots_since_start':job_status.get('elapsed_slots_since_start')}


def view():
    """Small live monitor response, independent of snapshot caching and the pipeline."""
    saved = read_json(config.REPORTS_DIR / "collection_capacity.json") or {}
    if saved.get("context") != context():
        saved = {}
    from . import followup
    cohort = followup.status()
    review = read_json(config.REPORTS_DIR / "collection_review.json") or {}
    stamp = parse_ts(review.get("generated_at"))
    if (review.get("context") != context() or not stamp or
            not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() < 2*config.COLLECTION_INTERVAL_SECONDS):
        review = {}
    review_summary = {key: review.get(key) for key in
                      ("status", "complete_days", "review_not_before", "eligible_for_expansion_review")}
    review_summary["gates"] = {name: {"name": gate["name"], "status": gate["status"]}
                               for name, gate in review.get("gates", {}).items()}
    fresh = freshness()
    if fresh["status"] in {"stale", "paused"}:
        review_summary["eligible_for_expansion_review"] = False
        if review_summary.get("status") == "ready_for_review":
            review_summary["status"] = "needs_attention"
    return {"freshness": fresh, "followup": cohort, "controller": followup.shadow(saved, cohort),
            "review": review_summary,
            "generated_at": saved.get("generated_at"),
            "complete_days": saved.get("complete_days", 0),
            "estimate_ready": saved.get("estimate_ready", False),
            "capacity": saved.get("capacity", {}), "note": saved.get("note")}
