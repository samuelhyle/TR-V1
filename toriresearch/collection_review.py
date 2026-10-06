"""Evidence gates for a seven-day collection review, without changing budgets.

Missing cycles remain in the denominator. Only slots with a recorded platform
pause are excluded, and those exclusions remain visible. No host-offline time
is inferred from missing records. Probe latency uses bounded success histograms,
not averages of per-run percentiles or historical attempt timestamps.
"""

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import collection_health as health, config
from .metrics import parse_ts

VERSION = "collection-review-v1"
MIN_LATENCY_SAMPLES = 30


def _gate(name, value, target, ready, violated=False):
    return {"name": name, "status": "fail" if violated else "pass" if ready else "pending",
            "value": value, "target": target}


def _p95(histogram):
    n = sum(histogram.values())
    if not n:
        return None
    rank, total = math.ceil(n*.95), 0
    for hour, count in sorted(histogram.items()):
        total += count
        if total >= rank:
            return {"lower_hours": hour, "upper_hours_exclusive": hour+1 if hour < 168 else None}


def evaluate(current, reference=None, records=None):
    reference = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    context = health.context()
    if records is None:
        records, malformed = health.history()
    else:
        malformed = 0
    unique = {}
    for row in records:
        if not isinstance(row, dict):
            malformed += 1
            continue
        start, end = parse_ts(row.get("started_at")), parse_ts(row.get("finished_at"))
        if not start or not end or end < start or not isinstance(row.get("stages"), list):
            malformed += 1
            continue
        if any(row.get(key) is not None and not isinstance(row[key], dict) for key in ("followup", "backup")):
            malformed += 1
            continue
        if row.get("measurement_version") != health.VERSION or row.get("context") != context or end > reference:
            continue
        key = row.get("run_id") or (row["started_at"], row["finished_at"])
        if not isinstance(key, (str, tuple)):
            malformed += 1
            continue
        unique[key] = row
    matching = sorted(unique.values(), key=lambda row: parse_ts(row["finished_at"]))
    coverage = current.get("coverage", {})
    capacity = health.summary(coverage, reference, matching)
    complete_days = capacity["complete_days"]
    full = complete_days == 7 and not malformed
    today = reference.astimezone(health.LOCAL).date()
    end = health._midnight(today).astimezone(timezone.utc)
    first = min((parse_ts(row["started_at"]) for row in matching), default=None)
    first_day = first.astimezone(health.LOCAL).date() if first else None
    start_day = max(today-timedelta(days=7), first_day+timedelta(days=1)) if first_day else today
    start = health._midnight(min(start_day, today)).astimezone(timezone.utc)
    interval = config.COLLECTION_INTERVAL_SECONDS
    expected = math.ceil((end-start).total_seconds()/interval)
    window_runs = [row for row in matching if start <= parse_ts(row["started_at"]) < end]
    slots = defaultdict(list)
    for row in window_runs:
        slot = int((parse_ts(row["started_at"])-start).total_seconds()//interval)
        slots[slot].append(row)
    completed, paused = set(), set()
    observations = [start]
    histograms = {"first_delay_hours": Counter(), "repeat_lateness_hours": Counter()}
    successes = Counter()
    missing_watermarks = 0
    backlog = {}
    backup_stamps = set()
    for slot, runs in slots.items():
        for row in runs:
            sources = {stage.get("stage"): stage for stage in row["stages"] if isinstance(stage, dict)}
            if row.get("run_origin") == "scheduled" and row.get("status") in {"ok", "degraded"} and all(
                    name in sources and sources[name].get("exit_code") == 0 for name in ("atom", "sweep", "itempage")):
                completed.add(slot)
            platform_pause = row.get("platform_pause") or {}
            pause_at = parse_ts(platform_pause.get("at")) if isinstance(platform_pause, dict) else None
            if (row.get("run_origin") == "scheduled" and row.get("status") == "paused" and pause_at and
                    platform_pause.get("status") in {"blocked", "throttled"} and
                    pause_at <= parse_ts(row["finished_at"])):
                paused.add(slot)
            for name, count_key in (("itempage", "stored"), ("sweep", "seen"), ("atom", "rows_ingested")):
                stage = sources.get(name, {})
                stats = health.sanitize_stats(stage.get("stats"))
                if stats.get(count_key, 0) > 0:
                    observed = parse_ts(stage.get("last_observation_at"))
                    if observed and parse_ts(row["started_at"]) <= observed <= parse_ts(row["finished_at"]):
                        observations.append(max(start, min(end, observed)))
                    else:
                        missing_watermarks += 1
            detail = health.sanitize_stats(sources.get("itempage", {}).get("stats"))
            for field, success_field in (("first_delay_hours", "first_probe_successes"),
                                         ("repeat_lateness_hours", "repeat_probe_successes")):
                histograms[field].update({int(hour): count for hour, count in detail.get(field, {}).items()})
                successes[field] += detail.get(success_field, 0)
            followup = row.get("followup") or {}
            cohort_state = followup.get("cohort") or {}
            due = cohort_state.get("overdue_repeat") if config.FOLLOWUP_ENABLED else followup.get("n_due_repeat_in_scope")
            if isinstance(due, (int, float)) and not isinstance(due, bool) and math.isfinite(due) and due >= 0:
                day = parse_ts(row["started_at"]).astimezone(health.LOCAL).date().isoformat()
                backlog[day] = due  # Records are ordered by actual completion.
            proof = row.get("backup") or {}
            stamp = parse_ts(proof.get("created_at"))
            if proof.get("status") == "ok" and proof.get("restore_verified") is True and stamp:
                backup_stamps.add(stamp)
    paused -= completed
    available = expected-len(paused)
    fraction = len(completed)/available if available else None
    gates = {"cycle_completion": _gate("Scheduled cycle completion", fraction, ">=95% of available slots",
                                    full and fraction is not None and fraction >= .95,
                                    expected > 0 and fraction is not None and fraction < .95)}

    # Subtract only recorded paused slots. Gaps between missing runs remain
    # unexplained; a sleeping host is never assumed to excuse a missing slot.
    observations.append(end)
    observations = sorted(set(observations))
    max_gap = 0.
    for left, right in zip(observations, observations[1:]):
        gap = (right-left).total_seconds()
        for slot in paused:
            a = start+timedelta(seconds=slot*interval)
            b = min(end, a+timedelta(seconds=interval))
            gap -= max(0., (min(right, b)-max(left, a)).total_seconds())
        max_gap = max(max_gap, gap/3600)
    gates["ingestion_gap"] = _gate("Longest unexplained ingestion gap",
                                  {"hours": round(max_gap, 2), "stages_missing_watermarks": missing_watermarks}, "<2 hours",
                                  full and not missing_watermarks and max_gap < 2,
                                  expected > 0 and not missing_watermarks and max_gap >= 2)
    cohort = coverage.get("followup", {}) if config.FOLLOWUP_ENABLED else coverage
    for field, label, target, pending_key in (
            ("first_delay_hours", "First successful check delay", 48, "p95_first_check_age_hours"),
            ("repeat_lateness_hours", "Repeat successful check lateness", 24, "p95_repeat_lateness_hours")):
        hist = histograms[field]
        band = _p95(hist)
        pending = cohort.get(pending_key)
        n = sum(hist.values())
        measured = n == successes[field] and n >= MIN_LATENCY_SAMPLES and bool(band)
        # Bins are [lower, upper); a repeat in bin 23 is strictly below 24.
        satisfied = measured and band["upper_hours_exclusive"] is not None and band["upper_hours_exclusive"] <= target
        failed = bool(band and band["lower_hours"] >= target) if field == "repeat_lateness_hours" else bool(band and band["lower_hours"] > target)
        failed = measured and failed
        pending_bad = pending is not None and (pending >= target if field == "repeat_lateness_hours" else pending > target)
        gates[field] = _gate(label, {"successful_samples": n, "recorded_successes": successes[field],
                                   "p95_band": band, "pending_p95_hours": pending},
                                   f"{'<' if target == 24 else '<='}{target} hours; >=30 success samples",
                                   full and satisfied and not pending_bad, failed or pending_bad)
    daily_backlog = [{"day": day["date"], "overdue_repeat": backlog.get(day["date"])}
                     for day in capacity["daily"] if day["complete_tracking_day"]]
    values = [row["overdue_repeat"] for row in daily_backlog if row["overdue_repeat"] is not None]
    slope = None
    if len(values) == 7:
        mean = sum(values)/7
        slope = sum((i-3)*(value-mean) for i, value in enumerate(values))/28
    growing = bool(slope is not None and slope > 0 and values[-1] > values[0])
    gates["repeat_backlog"] = _gate("Repeat backlog trend", {"daily": daily_backlog, "slope_per_day": slope},
                                    "Seven daily snapshots without a positive trend and net increase",
                                    full and slope is not None and not growing, growing)

    current_backup = (current.get("storage") or {}).get("backup", {})
    latest_proof = current.get("backup") or {}
    latest = parse_ts(latest_proof.get("created_at"))
    if (latest and latest_proof.get("source_key") == context["source_key"] and
            latest_proof.get("restore_verified") is True and latest_proof.get("integrity") == "ok"):
        backup_stamps.add(latest)
    daily_backup = []
    for day in capacity["daily"]:
        if not day["complete_tracking_day"]:
            continue
        cutoff = health._midnight(datetime.fromisoformat(day["date"]).date()+timedelta(days=1)).astimezone(timezone.utc)
        stamp = max((stamp for stamp in backup_stamps if stamp <= cutoff), default=None)
        daily_backup.append({"day": day["date"], "verified": bool(stamp and (cutoff-stamp).total_seconds() <= 86400)})
    backup_good = len(daily_backup) == 7 and all(day["verified"] for day in daily_backup) and current_backup.get("status") == "ok"
    gates["daily_backup"] = _gate("Daily verified recovery", daily_backup, "Verified evidence every day, with a fresh backup now",
                                 full and backup_good, full and not backup_good)
    saved = {row["day"]: row["status"] for row in (current.get("history") or {}).get("days", [])}
    utc_days = [(end.date()-timedelta(days=offset)).isoformat() for offset in reversed(range(1, 8))]
    partitions = [{"day": day, "status": saved.get(day, "missing")} for day in utc_days]
    history_good = all(day["status"] in {"available", "suppressed"} for day in partitions)
    gates["daily_history"] = _gate("Daily UTC aggregate partitions", partitions, "Seven saved days; suppressed groups still count as saved",
                                  full and history_good, full and not history_good)
    passed = all(gate["status"] == "pass" for gate in gates.values())
    failed = any(gate["status"] == "fail" for gate in gates.values())
    latest_cycle = max((parse_ts(row["finished_at"]) for row in matching), default=None)
    cycle_age = (reference-latest_cycle).total_seconds()/3600 if latest_cycle else None
    live = cycle_age is not None and 0 <= cycle_age < max(2, 2*interval/3600)
    return {"version": VERSION, "generated_at": reference.isoformat(), "context": context,
            "status": "needs_attention" if failed or malformed or (passed and (not live or current.get("platform_pause"))) else "ready_for_review" if passed else "collecting",
            "eligible_for_expansion_review": passed and not malformed and not current.get("platform_pause") and live,
            "current_cycle_age_hours": round(cycle_age, 2) if cycle_age is not None else None,
            "complete_days": complete_days, "required_days": 7,
            "tracking_started_at": first.isoformat() if first else None,
            "review_not_before": health._midnight(first_day+timedelta(days=8)).isoformat() if first_day else None,
            "outcome_review_not_before": health._midnight(first_day+timedelta(days=31)).isoformat() if first_day else None,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "Europe/Helsinki"},
            "schedule": {"expected_slots": expected, "completed_slots": len(completed),
                         "manual_pipeline_runs": sum(row.get("run_origin") == "manual" for row in window_runs),
                         "unverified_origin_runs": sum(row.get("run_origin") not in {"manual", "scheduled"} for row in window_runs),
                         "recorded_paused_slots": len(paused), "paused_hours": len(paused)*interval/3600,
                         "paused_hours_note": "Nominal excluded slot duration, not an exact platform outage measurement.",
                         "unobserved_slots": expected-len(slots), "host_offline_hours": None,
                         "host_offline_note": "Host uptime history is unavailable; missing slots remain in the denominator."},
            "malformed_history_records": malformed, "gates": gates,
            "note": "Proposed operating targets, not market estimates. The first local day and today are partial. "
                    "Success latency is an hourly histogram with a 168+ hour tail; attempts are not success samples. "
                    "Only scheduled cycles fill completion slots; manual pipelines and unknown origins remain separate. "
                    "Cycle completions count once per scheduled slot. No budgets, scope, source permissions or pauses are changed. "
                    "Seven-day readiness does not establish 30-day outcomes or unpublished realized prices."}


def markdown(report):
    lines = ["# Collection review", "", f"Updated: {report['generated_at']}",
             f"Status: {report['status']}; complete days: {report['complete_days']} / 7",
             f"Seven-day review not before: {report['review_not_before'] or 'after the first measured cycle'}", "",
             "| Gate | Status | Target |", "| --- | --- | --- |"]
    lines.extend(f"| {gate['name']} | {gate['status']} | {gate['target']} |" for gate in report["gates"].values())
    schedule = report["schedule"]
    lines.extend(["", f"Scheduled slots: {schedule['expected_slots']}; completed: {schedule['completed_slots']}; "
                  f"recorded paused: {schedule['recorded_paused_slots']}; unobserved: {schedule['unobserved_slots']}.",
                  schedule["host_offline_note"], "", report["note"], ""])
    return "\n".join(lines)
