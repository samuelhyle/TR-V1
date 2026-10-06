"""Bounded, stratified follow-up with durable retries and advisory control.

Discovery retains all permitted observations. Admission is conditional sampling
of an eligible pool; neither priority selections nor pooled admission batches
provide an unbiased estimate of the whole marketplace.
"""

import argparse
import hashlib
import json
import math
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from . import collection, config, db
from .metrics import parse_ts

VERSION = "followup-stratified-v1"
SERVICE_VERSION = "protected-purpose-v1"
ACTIVE = ("ready", "leased", "quarantined")


def purpose(row):
    return row.get("purpose") or ("baseline" if row["mode"] == "sampled" else "opportunity")

# Half-width of the per-listing due-time spread, in hours. See `due_jitter`.
DUE_SPREAD_HOURS = 0.5


def due_jitter(listing_pk):
    """A stable per-listing offset applied to the next scheduled probe.

    The detail stage drains its whole budget in about two seconds, so every
    listing it touches would otherwise come due at the same instant a day later.
    The stage then finds nothing at all, the work reappears a minute afterwards,
    and the cycle is reported as having dropped detail writes it was never
    offered. Measured on 3 October 2026: the stage ran at 12:30:33 with nothing
    due, the first of 97 tasks became due at 12:31:08, and the queue was empty
    again within the hour.

    Derived from the listing key rather than drawn at random, so re-running
    `complete` for the same row cannot move its schedule -- the spread has to be
    reproducible or a retried write would silently re-postpone a listing.
    """
    window = DUE_SPREAD_HOURS * 3600
    digest = int(hashlib.sha256(str(listing_pk).encode()).hexdigest()[:8], 16)
    return timedelta(seconds=(digest / 0xFFFFFFFF) * 2 * window - window)


class CandidateBatch(list):
    def __init__(self, rows, admitted):
        super().__init__(rows)
        self.admitted = admitted


def utcnow():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def policy_key():
    return _hash([VERSION, sorted(config.FOCUS_CATEGORIES), sorted(config.FOCUS_MUNICIPALITIES)])


def bound():
    """Operating bound plus an advisory measured bound when service was busy.

    Sparse queues do not measure available capacity. Never recursively size a
    shrinking cohort from its own declining request consumption.
    """
    from . import collection_health
    ceiling = max(0, config.ITEM_BUDGET) * 86400 / config.COLLECTION_INTERVAL_SECONDS
    measured_cap, basis = None, "provisional_configured_budget"
    report = collection_health.read_json(config.REPORTS_DIR / "collection_capacity.json") or {}
    if report.get("estimate_ready") and report.get("context") == collection_health.context():
        measured = report.get("capacity", {}).get("measured_successes_per_day")
        attempts = report.get("capacity", {}).get("measured_attempts_per_day") or 0
        basis = "configured_repeat_reservation"
        if isinstance(measured, (int, float)) and math.isfinite(measured) and measured >= 0 and attempts >= ceiling*0.8:
            measured_cap = math.floor(min(ceiling, measured)*config.PROBE_REPEAT_SHARE*config.PROBE_INTERVAL_HOURS/24*0.8)
    cap = math.floor(ceiling * config.PROBE_REPEAT_SHARE * config.PROBE_INTERVAL_HOURS / 24 * 0.8)
    if config.FOLLOWUP_MAX_ACTIVE:
        cap = min(cap, config.FOLLOWUP_MAX_ACTIVE)
    return {"max_active": cap, "measured_max_active_recommendation": measured_cap,
            "basis": basis, "headroom_fraction": 0.2,
            "daily_attempt_ceiling": ceiling,
            "discretionary_max_active": min(cap, config.FOLLOWUP_DISCRETIONARY_MAX_ACTIVE)}


def _stratum(row, scopes):
    vertical = next((path for path, scope in scopes if scope.matches(row)), "all")
    price = row.get("listed_price_eur")
    band = "unknown" if price is None else "under50" if price < 50 else "50to199" if price < 200 else "200plus"
    return vertical, json.dumps([vertical, row.get("municipality_norm") or "unknown", band], ensure_ascii=False)


def _eligible(conn, platform):
    conditions, params = collection.Scope(platform).conditions()
    conditions += ["platform=?", "outcome_status='unknown'", "listing_id IS NOT NULL"]
    params.append(platform)
    fields = "listing_pk, listing_id, platform, category_raw_id, category_norm, category_root_norm, municipality_norm, listed_price_eur, first_seen_at"
    return [dict(row) for row in conn.execute("SELECT " + fields + " FROM listings WHERE " + " AND ".join(conditions), params).fetchall()]


def _end(conn, task, now, reason):
    conn.execute("UPDATE followup_tasks SET state=?, lease_token=NULL, lease_until=NULL WHERE listing_pk=?",
                 ("complete" if reason == "terminal" else "suspended", task["listing_pk"]))
    conn.execute("UPDATE followup_enrollments SET ended_at=?, end_reason=? WHERE enrollment_id=? AND ended_at IS NULL",
                 (now.isoformat(), reason, task["enrollment_id"]))


def _enroll(conn, row, now, key, batch, mode, vertical, stratum, population, probability, enrollment_purpose="baseline"):
    enrollment = uuid.uuid4().hex
    conn.execute("INSERT INTO followup_enrollments (enrollment_id, listing_pk, policy_key, selection_version, "
                 "batch_id, mode, vertical, stratum, population_size, selection_probability, enrolled_at, purpose) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (enrollment, row["listing_pk"], key, VERSION, batch, mode, vertical, stratum, population, probability, now.isoformat(), enrollment_purpose))
    # Prior failed attempts never become successes. New tasks require a first
    # committed check. Re-admission retains known success and confirms it now.
    conn.execute("INSERT INTO followup_tasks (listing_pk, enrollment_id, state, due_at) VALUES (?,?,'ready',?) "
                 "ON CONFLICT(listing_pk) DO UPDATE SET enrollment_id=excluded.enrollment_id, state='ready', "
                 "due_at=excluded.due_at, failure_count=0, error_class=NULL, lease_token=NULL, lease_until=NULL",
                 (row["listing_pk"], enrollment, now.isoformat()))


def reconcile(platform="tori", reference=None, priority_ids=(), exploration_ids=(), reason=None):
    """Idempotently admit spare capacity; retain existing memberships until exit.

    Hash ranking within each stratum is stable for one UTC admission day. Slots
    are spread across verticals, then municipality/price strata. Probabilities
    describe that batch's pool only, and are NULL for manual priority entries.
    """
    now, cap, key = (reference or utcnow()).astimezone(timezone.utc), bound()["max_active"], policy_key()
    lock, error = db.acquire_lock("followup_admission")
    if lock is None:
        raise RuntimeError(error)
    conn = db.connect()
    try:
        with conn.transaction():
            # Serialize admissions across PostgreSQL writers as well. This is
            # task ownership only, not distributed HTTP pacing or pause state.
            if conn.dialect == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(?)", (729814620,))
            eligible = {row["listing_pk"]: row for row in _eligible(conn, platform)}
            tasks = [dict(row) for row in conn.execute(
                "SELECT t.*, e.policy_key, e.mode, e.purpose, e.enrolled_at FROM followup_tasks t "
                "JOIN followup_enrollments e ON e.enrollment_id=t.enrollment_id "
                "JOIN listings l ON l.listing_pk=t.listing_pk WHERE l.platform=?", (platform,)).fetchall()]
            for task in tasks:
                if task["state"] not in ACTIVE:
                    continue
                leased = parse_ts(task["lease_until"])
                if task["state"] == "leased" and leased and leased > now:
                    continue
                if task["listing_pk"] not in eligible or task["policy_key"] != key:
                    _end(conn, task, now, "scope_or_terminal")
                    task["state"] = "suspended"
                elif task["state"] == "leased":
                    conn.execute("UPDATE followup_tasks SET state='ready', lease_token=NULL, lease_until=NULL WHERE listing_pk=?",
                                 (task["listing_pk"],))
                    task["state"] = "ready"
            active = {t["listing_pk"]: t for t in tasks if t["state"] in ACTIVE}
            # Capacity reductions retire discretionary work before the baseline. A valid in-flight lease
            # may temporarily exceed a reduced cap, but consumes admission slots.
            removable = sorted((t for t in active.values() if t["state"] != "leased"),
                               key=lambda t: (purpose(t) == "baseline", parse_ts(t["enrolled_at"]), t["listing_pk"]))
            while len(active) > cap and removable:
                task = removable.pop(0)
                _end(conn, task, now, "capacity_reduction")
                del active[task["listing_pk"]]
            scopes = [(path, collection.Scope(platform, (path,))) for path in sorted(config.FOCUS_CATEGORIES)]
            seed_key = _hash([key, now.date().isoformat()])
            batch = uuid.uuid4().hex
            # Intent is an annotation, never a promotion or a replacement. A
            # baseline nomination retains its enrollment, probability and lease.
            nominated = {"opportunity": set(map(str, priority_ids)),
                         "exploration": set(map(str, exploration_ids))}
            if exploration_ids and not str(reason or "").strip():
                raise ValueError("Exploration requires a decision-impact reason")
            why = str(reason or "Operator opportunity nomination").strip()
            if not why or len(why) > 500:
                raise ValueError("Nomination reason must contain 1 to 500 characters")
            for intent, requested in nominated.items():
                if requested - {r["listing_id"] for r in eligible.values()}:
                    raise ValueError("Nominated IDs must be existing unresolved listings in the configured scope")
                for row in eligible.values():
                    if row["listing_id"] in requested:
                        conn.execute("INSERT INTO followup_intents (listing_pk,purpose,reason,nominated_at) "
                                     "VALUES (?,?,?,?) ON CONFLICT(listing_pk,purpose) DO NOTHING",
                                     (row["listing_pk"], intent, why, now.isoformat()))
            intents = [dict(r) for r in conn.execute(
                "SELECT i.* FROM followup_intents i JOIN listings l ON l.listing_pk=i.listing_pk "
                "WHERE l.platform=? ORDER BY i.nominated_at, i.listing_pk, i.purpose", (platform,)).fetchall()]
            discretionary = sum(purpose(t) != "baseline" for t in active.values())
            allowance = bound()["discretionary_max_active"]
            for intent in intents:
                pk = intent["listing_pk"]
                if pk not in eligible or pk in active or len(active) >= cap or discretionary >= allowance:
                    continue
                row = eligible[pk]
                vertical, stratum = _stratum(row, scopes)
                _enroll(conn, row, now, key, batch, "priority", vertical, stratum, 1, None, intent["purpose"])
                active[pk] = {"state": "ready", "mode": "priority", "purpose": intent["purpose"]}
                discretionary += 1
            # Quarantined samples stay enrolled and cannot re-enter as fresh work.
            # Retired samples are eligible next cycle if capacity returns.
            groups = defaultdict(list)
            for pk, row in eligible.items():
                if pk not in active:
                    groups[_stratum(row, scopes)].append(row)
            rings = defaultdict(deque)
            for (vertical, stratum), rows in sorted(groups.items(), key=lambda pair: _hash([seed_key, pair[0]])):
                rows.sort(key=lambda r: _hash([seed_key, stratum, r["listing_id"]]))
                rings[vertical].append((stratum, deque(rows)))
            selected = defaultdict(list)
            slots = max(0, cap-len(active))
            vertical_order = sorted(rings, key=lambda v: _hash([seed_key, v]))
            while slots and any(rings.values()):
                for vertical in vertical_order:
                    if not slots or not rings[vertical]:
                        continue
                    stratum, rows = rings[vertical].popleft()
                    selected[(vertical, stratum)].append(rows.popleft())
                    slots -= 1
                    if rows:
                        rings[vertical].append((stratum, rows))
            for (vertical, stratum), rows in selected.items():
                population = len(groups[(vertical, stratum)])
                for row in rows:
                    _enroll(conn, row, now, key, batch, "sampled", vertical, stratum, population, len(rows)/population)
            return {"max_active": cap, "admitted": sum(map(len, selected.values())), "eligible": len(eligible)}
    finally:
        conn.close()
        db.release_lock(lock)


def _fair(rows, dispatch):
    groups = defaultdict(deque)
    for row in sorted(rows, key=lambda r: (parse_ts(r["due_at"]), r["listing_pk"])):
        groups[row["vertical"]].append(row)
    ordered = sorted(groups, key=lambda v: (parse_ts(dispatch.get(v)) or datetime.min.replace(tzinfo=timezone.utc), v))
    result = []
    while any(groups.values()):
        for vertical in ordered:
            if groups[vertical]:
                result.append(groups[vertical].popleft())
    return result


def candidates(limit, platform="tori", min_age_hours=0, reference=None):
    if limit <= 0:
        return []
    now = (reference or utcnow()).astimezone(timezone.utc)
    admission = reconcile(platform, now)
    rows = db.query("SELECT l.*, t.enrollment_id, t.due_at, t.last_success_at, t.failure_count, e.vertical, e.mode, e.purpose, e.enrolled_at "
                    "FROM followup_tasks t JOIN listings l ON l.listing_pk=t.listing_pk "
                    "JOIN followup_enrollments e ON e.enrollment_id=t.enrollment_id "
                    "WHERE l.platform=? AND l.outcome_status='unknown' AND t.state='ready' AND t.due_at<=?",
                    (platform, now.isoformat()))
    cutoff = now-timedelta(hours=min_age_hours)
    rows = [r for r in rows if not min_age_hours or (parse_ts(r["first_seen_at"]) or now) <= cutoff]
    dispatch = {r["vertical"]: r["last_dispatch"] for r in db.query(
        "SELECT e.vertical, MAX(t.last_attempt_at) AS last_dispatch FROM followup_tasks t "
        "JOIN followup_enrollments e ON e.enrollment_id=t.enrollment_id "
        "JOIN listings l ON l.listing_pk=t.listing_pk WHERE l.platform=? GROUP BY e.vertical", (platform,))}
    baseline = [r for r in rows if purpose(r) == "baseline"]
    discretionary = [r for r in rows if purpose(r) != "baseline"]
    retry_allowance = max(1, math.floor(limit*0.1))
    selected = _select(baseline, limit, dispatch, now, retry_allowance)
    retries_used = sum(bool(r["failure_count"]) for r in selected)
    selected += _select(discretionary, limit-len(selected), dispatch, now, retry_allowance-retries_used)
    return CandidateBatch(selected, admission["admitted"])


def _select(rows, limit, dispatch, now, retry_allowance):
    """Reserve repeat service within a purpose; retries share the global cap."""
    if limit <= 0:
        return []
    retry = _fair([r for r in rows if r["failure_count"]], dispatch)
    first = _fair([r for r in rows if not r["failure_count"] and not r["last_success_at"]], dispatch)
    repeat = _fair([r for r in rows if not r["failure_count"] and r["last_success_at"]], dispatch)
    retry = retry[:retry_allowance] if limit > 1 or not (first or repeat) else []
    remaining = limit-len(retry)
    nf, nr = collection.probe_allocation(remaining, len(first), len(repeat))
    overdue = sum(parse_ts(r["due_at"]) <= now-timedelta(hours=config.PROBE_INTERVAL_HOURS) for r in repeat)
    if overdue and remaining > 1:
        nr = min(len(repeat), max(nr, min(overdue, math.ceil(remaining*0.75))), remaining-(1 if first else 0))
        nf = min(len(first), remaining-nr)
        nr = min(len(repeat), remaining-nf)
    selected, f, r = [], 0, 0
    while f < nf or r < nr:
        if r < nr and (f >= nf or r*nf <= f*nr):
            selected.append(repeat[r]); r += 1
        else:
            selected.append(first[f]); f += 1
    if retry:
        selected[1:1] = retry[:1]
        selected.extend(retry[1:])
    return selected[:limit]


def claim(conn, row, reference=None):
    now, token = (reference or utcnow()).astimezone(timezone.utc), uuid.uuid4().hex
    result = conn.execute("UPDATE followup_tasks SET state='leased', lease_token=?, lease_until=?, last_attempt_at=?, attempt_count=attempt_count+1 "
                          "WHERE listing_pk=? AND enrollment_id=? AND state='ready' AND due_at<=?",
                          (token, (now+timedelta(minutes=30)).isoformat(), now.isoformat(), row["listing_pk"],
                           row["enrollment_id"], now.isoformat()))
    return token if result.rowcount == 1 else None


def complete(conn, row, token, observed, terminal=False):
    """Caller must hold the observation transaction; loss of ownership rolls it back."""
    observed = parse_ts(observed).astimezone(timezone.utc).isoformat()
    # The next probe is one interval out, spread per listing so a drained cohort
    # does not return as a single clump. See `due_jitter`.
    next_due = (parse_ts(observed) + timedelta(hours=config.PROBE_INTERVAL_HOURS)
                + due_jitter(row["listing_pk"])).isoformat()
    result = conn.execute("UPDATE followup_tasks SET state=?, due_at=?, last_success_at=?, failure_count=0, "
                          "error_class=NULL, lease_token=NULL, lease_until=NULL "
                          "WHERE listing_pk=? AND enrollment_id=? AND state='leased' AND lease_token=?",
                          ("complete" if terminal else "ready", next_due, observed, row["listing_pk"], row["enrollment_id"], token))
    if result.rowcount != 1:
        raise RuntimeError("Follow-up lease lost before observation commit")
    if terminal:
        conn.execute("UPDATE followup_enrollments SET ended_at=?, end_reason='terminal' WHERE enrollment_id=?",
                     (observed, row["enrollment_id"]))


def fail(conn, row, token, error_class, reference=None):
    now = (reference or utcnow()).astimezone(timezone.utc)
    with conn.transaction():
        task = conn.execute("SELECT failure_count FROM followup_tasks WHERE listing_pk=? AND lease_token=?",
                            (row["listing_pk"], token)).fetchone()
        if task is None:
            return
        failures = task[0]+(0 if error_class == "platform_stop" else 1)
        quarantine = failures >= (3 if error_class == "parse" else 5)
        jitter = int(_hash([row["listing_pk"], failures])[:8], 16) % 21 / 100
        delay = min(24, 2**max(0, failures-1)*(1+jitter))
        conn.execute("UPDATE followup_tasks SET state=?, due_at=?, failure_count=?, error_class=?, "
                     "lease_token=NULL, lease_until=NULL WHERE listing_pk=? AND lease_token=?",
                     ("quarantined" if quarantine else "ready", (now+timedelta(hours=delay)).isoformat(),
                      failures, error_class, row["listing_pk"], token))
        conn.execute("UPDATE listings SET last_probed_at=? WHERE listing_pk=?", (now.isoformat(), row["listing_pk"]))


def status(reference=None):
    now = (reference or utcnow()).astimezone(timezone.utc)
    result = {"enabled": config.FOLLOWUP_ENABLED, **bound(), "selection_version": VERSION, "service_version": SERVICE_VERSION}
    conn = db.connect()
    try:
        exists = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
                              "AND table_name='followup_tasks'" if config.POSTGRES_DSN else
                              "SELECT name FROM sqlite_master WHERE type='table' AND name='followup_tasks'").fetchone()
        if not exists:
            return {**result, "initialized": False, "active": 0}
        rows = [dict(r) for r in conn.execute(
            "SELECT t.*, e.mode, e.purpose, e.vertical, e.enrolled_at, l.first_seen_at, l.outcome_status FROM followup_tasks t "
            "JOIN followup_enrollments e ON e.enrollment_id=t.enrollment_id "
            "JOIN listings l ON l.listing_pk=t.listing_pk WHERE l.platform='tori' "
            "AND t.state IN ('ready','leased','quarantined') AND l.outcome_status='unknown'").fetchall()]
        conditions, params = collection.Scope().conditions()
        conditions += ["platform='tori'", "outcome_status='unknown'"]
        eligible = conn.execute("SELECT COUNT(*) FROM listings WHERE " + " AND ".join(conditions), params).fetchone()[0]
        intents = [dict(r) for r in conn.execute("SELECT i.* FROM followup_intents i WHERE i.listing_pk IN (SELECT listing_pk FROM listings WHERE " + " AND ".join(conditions) + ")", params).fetchall()]
    finally:
        conn.close()
    active = [r for r in rows if r["state"] in ACTIVE and r["outcome_status"] == "unknown"]
    due = [r for r in active if r["state"] == "ready" and parse_ts(r["due_at"]) <= now]
    # Dispatch backoff, leases and quarantine cannot reset the successful-check
    # clock or hide an overdue first check from the review.
    late = [max(0, (now-parse_ts(r["last_success_at"])).total_seconds()/3600-config.PROBE_INTERVAL_HOURS)
            for r in rows if r["last_success_at"] and
            now >= parse_ts(r["last_success_at"])+timedelta(hours=config.PROBE_INTERVAL_HOURS)]
    first_ages = [max(0, (now-parse_ts(r["enrolled_at"])).total_seconds()/3600)
                  for r in rows if not r["last_success_at"]]
    return {**result, "initialized": True, "active": len(active),
            "eligible_unresolved": eligible, "not_admitted": max(0, eligible-len(active)),
            "by_purpose": {name: sum(purpose(r) == name for r in active) for name in ("baseline", "opportunity", "exploration")},
            "pending_intents": sum(i["listing_pk"] not in {r["listing_pk"] for r in active} for i in intents),
            "baseline_with_discretionary_intent": len({i["listing_pk"] for i in intents} & {r["listing_pk"] for r in active if purpose(r) == "baseline"}),
            "sampled": sum(r["mode"] == "sampled" for r in active),
            "priority": sum(r["mode"] == "priority" for r in active),
            "quarantined": sum(r["state"] == "quarantined" for r in active),
            "due": len(due), "due_first": sum(not r["last_success_at"] for r in due),
            "due_repeat": sum(bool(r["last_success_at"]) for r in due),
            "overdue_repeat": len(late),
            "retry_due": sum(r["failure_count"] > 0 for r in due),
            "p95_first_check_age_hours": round(sorted(first_ages)[max(0, math.ceil(len(first_ages)*0.95)-1)], 1) if first_ages else None,
            "p95_repeat_lateness_hours": round(sorted(late)[max(0, math.ceil(len(late)*0.95)-1)], 1) if late else None,
            "note": "Sampling probabilities are conditional on each admission pool. Priority entries and pooled batches are not representative of the whole market."}


# Why an unresolved listing has no terminal state. These are kept apart because
# each needs a different action, and "unresolved" on its own hides which one it
# is: an unprobed listing needs admitting, a parse failure needs a parser fix,
# and a listing awaiting its first success just needs time.
UNRESOLVED_REASONS = ("never_probed", "awaiting_first_success", "parse_failure",
                      "platform_stop", "quarantined", "other_error")


def unresolved_reasons(reference=None):
    """Break the unresolved in-scope population down by why it has no outcome.

    Without this, `n_unresolved` is one number covering four different problems,
    and the operator cannot tell whether to widen the cohort, fix a parser, or
    wait. Every count here is a reason, not a status.

    The scope conditions filter `listings`, so the task columns have to come from
    a join rather than the task table alone -- one query, not one per listing.
    At 27k stored listings a per-row lookup would dominate the cost of opening
    the Collection view.
    """
    conditions, params = collection.Scope().conditions()
    conditions += ["l.platform='tori'", "l.outcome_status='unknown'"]
    where = " AND ".join(conditions)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT t.error_class, t.state FROM listings l "
            "LEFT JOIN followup_tasks t ON t.listing_pk=l.listing_pk "
            "AND t.state IN ('ready','leased','quarantined') "
            "WHERE " + where, params).fetchall()
    finally:
        conn.close()

    counts = dict.fromkeys(UNRESOLVED_REASONS, 0)
    for error_class, state in rows:
        if error_class is None and state is None:
            counts["never_probed"] += 1
            continue
        # Quarantine is a state, not a cause: a task can be quarantined for a
        # parse failure or for a transport error. Report both, and let the cause
        # column carry the reason.
        if state == "quarantined":
            counts["quarantined"] += 1
        if error_class == "parse":
            counts["parse_failure"] += 1
        elif error_class == "platform_stop":
            counts["platform_stop"] += 1
        elif error_class:
            counts["other_error"] += 1
        else:
            counts["awaiting_first_success"] += 1
    counts["total"] = len(rows)
    counts["untracked"] = sum(1 for error, state in rows if error is None and state is None)
    return counts


def capacity_verdict(reference=None):
    """One verdict on whether follow-up can keep up, instead of two readouts.

    `due` and `probe_runs_at_budget` are both true and neither is actionable on
    its own. This collapses them into the question the operator actually has:
    can one day of collection consume the backlog that exists right now?

    The sizing rule is the documented inequality -- daily first checks, repeat
    demand and expected retries must fit inside 80% of sustainable daily attempt
    capacity -- and the 20% headroom is left unspent deliberately, because a
    collector sized to exactly its own observed load has no room for the arrival
    it has not seen yet.

    `verdict` is one of:

    * `within_capacity` -- a day of collection clears the backlog, and the cohort
      has room to admit more.
    * `at_capacity`     -- the cohort is full; admissions would have to displace
      something already being followed.
    * `over_capacity`   -- the backlog exceeds a full day of collection, so it is
      growing. This is a scope decision for the operator, not something to fix by
      raising the request rate.
    * `unavailable`     -- follow-up is not initialised, so nothing can be said.
    """
    state = status(reference)
    if not state.get("initialized"):
        return {"verdict": "unavailable", "active": 0, "due": 0,
                "reasons": ["Follow-up is not initialised."],
                "note": "No capacity verdict until follow-up has run once."}

    ceiling = state.get("daily_attempt_ceiling") or 0
    sustainable = ceiling * (1 - state.get("headroom_fraction", 0.2))
    due = state.get("due", 0)
    max_active = state.get("max_active") or 0
    active = state.get("active", 0)

    days_to_clear = (due / sustainable) if sustainable > 0 else None
    headroom = max_active - active

    reasons = []
    if ceiling <= 0:
        verdict = "unavailable"
        reasons.append("No item budget is configured, so daily attempt capacity is zero.")
    elif due > sustainable:
        verdict = "over_capacity"
        reasons.append(f"{due} status checks are due and one day of collection can "
                       f"complete about {int(sustainable)}.")
    elif headroom <= 0:
        verdict = "at_capacity"
        reasons.append(f"The cohort holds {active} of a maximum {max_active}.")
    else:
        verdict = "within_capacity"

    if state.get("not_admitted"):
        reasons.append(f"{state['not_admitted']} unresolved in-scope listings are not "
                       "in the followed cohort.")
    if state.get("quarantined"):
        reasons.append(f"{state['quarantined']} tasks are quarantined and will not "
                       "dispatch until reviewed.")
    if state.get("retry_due"):
        reasons.append(f"{state['retry_due']} due checks are retries of earlier failures.")
    if not reasons:
        reasons.append("Backlog clears within a day and the cohort has room to admit more.")

    return {"verdict": verdict, "active": active, "max_active": max_active,
            "cohort_headroom": headroom, "due": due,
            "due_first": state.get("due_first", 0), "due_repeat": state.get("due_repeat", 0),
            "retry_due": state.get("retry_due", 0), "quarantined": state.get("quarantined", 0),
            "not_admitted": state.get("not_admitted", 0),
            "eligible_unresolved": state.get("eligible_unresolved", 0),
            "daily_attempt_ceiling": ceiling, "sustainable_attempts_per_day": int(sustainable),
            "days_to_clear": round(days_to_clear, 2) if days_to_clear is not None else None,
            "basis": state.get("basis"), "reasons": reasons,
            "unresolved": unresolved_reasons(reference),
            "note": "Capacity says how much follow-up is sustainable. It does not "
                    "establish that the followed listings sold, and raising the "
                    "request rate is not the remedy for a backlog."}


# Why a trim ended an enrollment. Distinct from the automatic reasons so an
# operator decision is never mistaken for the collector giving up on its own.
TRIM_REASON = "operator_trim"


def trim_cohort(target_active, reason, client_token, platform="tori", reference=None):
    """Deliberately narrow the followed cohort, and record that it happened.

    Automatic capacity reduction already retires samples when the cohort exceeds
    its cap. This is the other case: arrivals outrun probe capacity, so following
    everything means following none of it properly. A 400-listing cohort fully
    followed beats a 5,000-listing cohort followed at 4%, for every statistic
    that depends on a terminal state.

    Properties this holds, each of which is the reason it is not just a DELETE:

    * **Bounded and explicit.** `target_active` must be a non-negative integer
      below the current size. There is no "trim everything" call, because an
      accidental one silently ends follow-up on real listings.
    * **Never interrupts an in-flight check.** A leased task keeps its lease and
      consumes a slot, so a trim issued mid-probe cannot pull a request out from
      under a running collection cycle.
    * **Idempotent.** A retry with the same `client_token` replays the recorded
      result instead of trimming again; the same token with different contents
      is refused, so a retry cannot quietly become a second, larger trim.
    * **Audited.** The decision, its target and its outcome are written to
      `action_log` in the same transaction as the ends it caused.
    * **Reversible in principle.** Enrollments are ended, never deleted, and
      retired samples re-enter if capacity returns.
    """
    try:
        token = str(uuid.UUID(client_token))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("a UUID submission token is required") from None
    if isinstance(target_active, bool) or not isinstance(target_active, int):
        raise ValueError("target cohort size must be an integer")
    if target_active < 0:
        raise ValueError("target cohort size cannot be negative")
    reason = str(reason or "").strip()
    if not reason:
        # Without a recorded reason this is indistinguishable from a bug in a
        # month, and the ended enrollments have to be explained somehow.
        raise ValueError("a reason is required so the trim can be explained later")
    if len(reason) > 500:
        raise ValueError("reason is too long")
    now = (reference or utcnow()).astimezone(timezone.utc)
    fingerprint = _hash([target_active, reason, platform, TRIM_REASON])

    lock, error = db.acquire_lock("followup_admission")
    if lock is None:
        raise RuntimeError(error)
    conn = db.connect()
    try:
        with conn.transaction():
            if conn.dialect == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(?)", (729814620,))
            prior = conn.execute(
                "SELECT request_sha256, returncode FROM action_log WHERE client_token=?",
                (token,)).fetchone()
            if prior is not None:
                if prior["request_sha256"] != fingerprint:
                    raise ValueError("This retry token was already used with different contents.")
                return {"trimmed": 0, "replayed": True, "target_active": target_active,
                        "reason": reason, "active_after": prior["returncode"],
                        "note": "Replayed; no listings were ended a second time."}
            rows = [dict(row) for row in conn.execute(
                "SELECT t.*, e.mode, e.purpose, e.enrolled_at, l.listing_id FROM followup_tasks t "
                "JOIN followup_enrollments e ON e.enrollment_id=t.enrollment_id "
                "JOIN listings l ON l.listing_pk=t.listing_pk "
                "WHERE l.platform=? AND l.outcome_status='unknown' "
                "AND t.state IN ('ready','leased')", (platform,)).fetchall()]
            active = [row for row in rows if row["state"] in ACTIVE]
            leased = [row for row in active if row["state"] == "leased"]
            # Match automatic reductions: discretionary work first, retaining
            # the baseline wherever the requested smaller cap permits it.
            removable = sorted((row for row in active if row["state"] != "leased"),
                               key=lambda row: (purpose(row) == "baseline",
                                                 parse_ts(row["enrolled_at"]), row["listing_pk"]))
            surplus = len(active) - target_active
            if surplus <= 0:
                raise ValueError(f"The cohort holds {len(active)} listings, which is already "
                                 f"at or below the target of {target_active}.")
            if surplus > len(removable):
                raise ValueError(f"Only {len(removable)} of {len(active)} listings can be ended now; "
                                 f"{len(leased)} hold an in-flight check. Retry once they finish, "
                                 f"or raise the target.")
            ended = removable[:surplus]
            for task in ended:
                _end(conn, task, now, TRIM_REASON)
            remaining = len(active) - len(ended)
            _record_trim(conn, token, fingerprint, target_active, reason, len(ended), remaining, now)
            return {"trimmed": len(ended), "replayed": False, "target_active": target_active,
                    "reason": reason, "active_after": remaining,
                    "still_leased": len(leased),
                    "ended_listing_ids": [row["listing_id"] for row in ended][:50],
                    "ended_truncated": len(ended) > 50,
                    "note": "Ended enrollments are retained, not deleted; retired samples "
                            "re-enter if capacity returns."}
    finally:
        conn.close()


def _record_trim(conn, token, fingerprint, target, reason, trimmed, remaining, now):
    """Write the decision in the same transaction as the ends it caused.

    `returncode` carries the resulting cohort size, which is a misuse of the
    column name but keeps the audit row inside the existing table rather than
    adding a second one for a single action. The full decision is in
    `stdout_tail`, and `client_token`/`request_sha256` make the write idempotent
    and tamper-evident alongside it.
    """
    conn.execute(
        "INSERT INTO action_log (started_at, action, returncode, seconds, stdout_tail, "
        "stderr_tail, client_token, request_sha256) VALUES (?,?,?,?,?,?,?,?)",
        (now.isoformat(), f"followup_trim:{token}", remaining, 0.0,
         f"target={target} trimmed={trimmed} reason={reason}", fingerprint, token, fingerprint))


def shadow(capacity, cohort):
    """Recommendations only: never edit budgets, cadence, source limits or pauses."""
    measured = capacity.get("estimate_ready", False)
    reasons = []
    if not measured:
        reasons.append("Wait for seven complete days under this collection policy")
    evidence = capacity.get("capacity", {})
    if evidence.get("retry_fraction", 0) is not None and evidence.get("retry_fraction", 0) > 0.1:
        reasons.append("Resolve failed checks before expanding discovery")
    if (cohort.get("p95_repeat_lateness_hours") or 0) > config.PROBE_INTERVAL_HOURS:
        reasons.append("Hold admissions and review overdue follow-up")
    if cohort.get("quarantined", 0):
        reasons.append("Review quarantined tasks with sanitized parser fixtures")
    ceiling = evidence.get("daily_attempt_ceiling", 0)
    repeat_demand = cohort.get("active", 0)*24/config.PROBE_INTERVAL_HOURS
    minimum_runtime = (max(0, config.ITEM_BUDGET)/config.ITEM_RPS
                       + max(0, config.SWEEP_BUDGET)/config.RATE_LIMIT["www.tori.fi"][0])
    source_window = config.COLLECTION_RUNTIME_SECONDS-config.ENRICH_RUNTIME_SECONDS-15-3*config.COLLECTION_REPORT_SECONDS-30
    if minimum_runtime >= source_window:
        reasons.append("Configured source work cannot fit the remaining cycle window; review budgets")
    recommended = cohort.get("measured_max_active_recommendation")
    return {"mode": "shadow", "applied": False,
            "recommended_max_active": min(cohort["max_active"], recommended) if recommended is not None else cohort["max_active"],
            "minimum_source_runtime_seconds": round(minimum_runtime, 1),
            "source_window_seconds": source_window,
            "budget_action": "hold", "discovery_action": "hold" if reasons else "review_existing_scope_rotation",
            "repeat_requests_per_day": repeat_demand, "repeat_reservation_per_day": ceiling*config.PROBE_REPEAT_SHARE,
            "reasons": reasons, "note": "Request budgets, rates, source scope and platform pause remain operator controlled."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reconcile", action="store_true", help="Apply local cohort admission without fetching pages")
    mode.add_argument("--prioritize", nargs="+", metavar="LISTING_ID", help="Nominate opportunities without displacing baseline listings")
    mode.add_argument("--explore", nargs="+", metavar="LISTING_ID", help="Nominate missing-fact checks; requires --reason")
    parser.add_argument("--reason", help="Opportunity rationale or exploration decision impact")
    mode.add_argument("--retry", nargs="+", metavar="LISTING_ID", help="Release selected quarantined tasks after parser review")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.reconcile or args.prioritize or args.explore or args.retry:
        db.init_db()
    if args.reconcile or args.prioritize or args.explore:
        reconcile(priority_ids=args.prioritize or (), exploration_ids=args.explore or (), reason=args.reason)
    if args.retry:
        conn = db.connect()
        try:
            with conn.transaction():
                for listing_id in args.retry:
                    conn.execute("UPDATE followup_tasks SET state='ready', due_at=?, failure_count=0, error_class=NULL "
                                 "WHERE state='quarantined' AND listing_pk IN "
                                 "(SELECT listing_pk FROM listings WHERE platform='tori' AND listing_id=?)",
                                 (utcnow().isoformat(), listing_id))
        finally:
            conn.close()
    print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
