"""Unattended collection, durable progress and a measured 24-hour handoff."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import collection_control, collection_events, aggregates, backups, collection, collection_health, collection_review, compliance, config, continuous, db, discovery, followup, parser_health, permissions, sitemap_refresh, storage_health
from .enrich import queue as enrichment_queue
from .http import collection_stop
from .fileio import write_text


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path, value):
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def measurements():
    with db.read_snapshot():
        coverage = collection.coverage()
        counts = {table: db.scalar(f"SELECT COUNT(*) FROM {table}") for table in
                  ("listings", "price_history", "events", "enrichment",
                   "ingest_runs", "sweep_runs", "purchases", "sold_observations")}
        discovery_health = {"rotation": discovery.report(), "sitemap_refresh": sitemap_refresh.report()}
        enrichment_health = enrichment_queue.report()
        parser_status = parser_health.report()
        history = aggregates.report()
        storage = storage_health.report()
    return {"measured_at": now(), "coverage": coverage, "counts": counts,
            "discovery": discovery_health, "enrichment_queue": enrichment_health,
            "parser_health": parser_status, "history": history, "storage": storage,
            "context": collection_health.context(), "jobs": continuous.jobs()}


def write_health(check_model=False, current=None):
    current = current or measurements()
    baseline = read_json(config.DATA_DIR / "collection_baseline.json")
    baseline_matches = bool(baseline and
                            baseline.get("database") == str(config.SQLITE_PATH) and
                            baseline["coverage"]["focus_categories"] == current["coverage"]["focus_categories"] and
                            baseline["coverage"]["focus_municipalities"] == current["coverage"]["focus_municipalities"] and
                            baseline.get("context") == current["context"])
    fields = ("n_tracked", "n_probe_attempted", "n_never_probed",
              "n_known_condition", "n_fresh_priced", "n_classified")
    health = {**current, "baseline": baseline, "baseline_matches": baseline_matches,
              "session": read_json(config.DATA_DIR / "collection_session.json"),
              "backup": read_json(config.DATA_DIR / "collection_backup_manifest.json"),
              "delta": {key: current["coverage"][key] - baseline["coverage"][key]
                        for key in fields} if baseline_matches else None,
              "pipeline": read_json(config.DATA_DIR / "collection_status.json"),
              "platform_pause": collection_stop(), "active_locks": db.active_locks(),
              "requirements": compliance.compliance_report(), "source_authority": permissions.report(),
              "limits": ["A probe timestamp records an attempt, including unsuccessful attempts.",
                         "Counts and deltas include manual collection alongside scheduled runs.",
                         "24 hours cannot establish 7/30-day follow-up or realised sale prices.",
                         "Model labels require human quality review; valid current labels are counted separately.",
                         "Sold captures and own-trade costs require operator input."]}
    if health["pipeline"] and health["pipeline"]["status"] == "running" and "collection" not in health["active_locks"]:
        health["pipeline"]["status"] = "interrupted"
    if check_model:
        from .enrich import provider
        model = provider.health()
        health["model"] = {key: model[key] for key in ("enabled", "reachable", "model")}
    capacity = collection_health.summary(current["coverage"])
    health["capacity"] = capacity
    review = collection_review.evaluate(health)
    health["review"] = review
    write_json(config.REPORTS_DIR / "collection_review.json", review)
    write_text(config.REPORTS_DIR / "collection_review.md", collection_review.markdown(review))
    health["followup_controller"] = followup.shadow(capacity, current["coverage"]["followup"])
    write_json(config.REPORTS_DIR / "followup_controller.json", health["followup_controller"])
    write_json(config.REPORTS_DIR / "discovery_health.json", {"measured_at": current["measured_at"],
               "context": current["context"], **current.get("discovery", {})})
    write_json(config.REPORTS_DIR / "enrichment_queue.json", {"measured_at": current["measured_at"], **current.get("enrichment_queue", {})})
    write_json(config.REPORTS_DIR / "parser_health.json", {"measured_at": current["measured_at"], **current.get("parser_health", {})})
    write_json(config.REPORTS_DIR / "historical_aggregates.json", current.get("history", {}))
    write_json(config.REPORTS_DIR / "storage_health.json", current.get("storage", {}))
    write_json(config.REPORTS_DIR / "collection_capacity.json", capacity)
    write_json(config.REPORTS_DIR / "collection_health.json", health)
    health["freshness"] = collection_health.freshness()
    write_json(config.REPORTS_DIR / "collection_health.json", health)
    coverage = current["coverage"]
    def local_time(stamp):
        return datetime.fromisoformat(stamp).astimezone(ZoneInfo("Europe/Helsinki")).strftime("%d %B %Y %H:%M:%S %Z")

    lines = ["# Collection handoff", "", f"Updated: {local_time(current['measured_at'])}", "",
             f"Pipeline: {(health['pipeline'] or {}).get('status', 'not recorded')}",
             f"Platform pause: {(health['platform_pause'] or {}).get('status', 'none')}", "",
             "| Measure | Current | Change from baseline |", "| --- | ---: | ---: |"]
    labels = {"n_tracked": "Focused listings", "n_probe_attempted": "Listings with a detail attempt",
              "n_never_probed": "Awaiting a first detail check", "n_known_condition": "Known condition",
              "n_fresh_priced": "Fresh priced listings", "n_classified": "Current valid product labels"}
    for key in fields:
        change = health["delta"][key] if health["delta"] is not None else "unavailable"
        lines.append(f"| {labels[key]} | {coverage[key]} | {change} |")
    if baseline:
        lines.extend(["", f"24-hour review due: {local_time(baseline['review_at'])}"])
    lines.extend(["", f"Seven-day review: {review['status']}; {review['complete_days']} / 7 complete days",
                  f"Review not before: {review['review_not_before'] or 'after the first completed current-policy cycle'}"])
    if health["session"]:
        lines.extend(["", f"Temporary idle-sleep protection expires: {local_time(health['session']['awake_until'])}",
                      "Keep the Mac plugged in, online and logged in; do not close the lid or shut down."])
    if health["backup"]:
        lines.extend(["", f"Database backup: `{health['backup'].get('backup_database', 'unavailable')}`",
                      f"Backup integrity: {health['backup'].get('integrity', 'unverified')}"])
    limits = capacity["capacity"]
    freshness = health["freshness"]
    lines.extend(["", f"Independent freshness: {freshness['status']}",
                  f"Recovery backup: {freshness['backup']['status']} (local copy; no off-host copy)",
                  "", "## Sustainable capacity", "",
                  f"Complete tracking days for current settings: {capacity['complete_days']} / 7",
                  f"Configured daily detail-attempt ceiling: {limits['daily_attempt_ceiling']:.0f}",
                  f"Daily repeat demand for admitted follow-up: {limits['repeat_demand_per_day']}",
                  f"Daily repeat demand if all unresolved listings were followed: {limits['all_unresolved_repeat_demand_per_day']}",
                  f"Measured daily attempts: {limits['measured_attempts_per_day']}",
                  f"Measured focused arrivals per day: {limits['focused_arrivals_per_day']}",
                  f"Seven-day estimate ready: {capacity['estimate_ready']}", "",
                  capacity["note"], "", "| Day (Helsinki) | Cycles | Search arrivals | Detail attempts | Stored first / repeat |",
                  "| --- | ---: | ---: | ---: | ---: |"])
    for day in capacity["daily"]:
        detail = day["sources"].get("itempage", {})
        label = day["date"] + ("" if day["complete_tracking_day"] else " (partial/untracked)")
        lines.append(f"| {label} | {day['cycles']} | {day['sources'].get('sweep', {}).get('new_in_scope', 0):.0f} | "
                     f"{detail.get('requests', 0):.0f} | {detail.get('first_probe_successes', 0):.0f} / "
                     f"{detail.get('repeat_probe_successes', 0):.0f} |")
    cohort = coverage["followup"]
    controller = health["followup_controller"]
    lines.extend(["", "## Bounded follow-up", "",
                  f"Active cohort: {cohort['active']} / {cohort['max_active']} ({cohort['basis']})",
                  f"Sampled / priority: {cohort.get('sampled', 0)} / {cohort.get('priority', 0)}",
                  f"Quarantined tasks: {cohort.get('quarantined', 0)}",
                  f"Controller: shadow, budget action {controller['budget_action']}; no changes applied.",
                  cohort.get("note", "Cohort admission is pending."),
                  *["- " + reason for reason in controller["reasons"]]])
    if freshness["issues"]:
        lines.extend(["", *["- " + issue["message"] for issue in freshness["issues"]]])
    rotation = current.get("discovery", {}).get("rotation", {})
    if rotation:
        lines.extend(["", "## Discovery rotation", "",
                      f"Pending continuations: {rotation['continuations']}; capped traversals: {rotation['capped_targets']}",
                      f"Desired interval: {rotation['desired_refresh_hours']} hours; minimum requests/hour: {rotation['minimum_requests_per_hour']}",
                      f"Reserved rotation requests/hour: {rotation['reserved_rotation_requests_per_hour']}",
                      f"Desired interval feasible at this share: {rotation['desired_interval_feasible_at_reserved_share']}",
                      rotation["coverage_note"]])
    queued = current.get("enrichment_queue", {})
    parsed = current.get("parser_health", {})
    if queued:
        lines.extend(["", "## Independent classification", "",
                      f"Pending: {queued['pending']}; retrying: {queued['retry']}; quarantined: {queued['quarantined']}",
                      f"Local runtime ceiling: {queued['runtime_seconds']} seconds", queued["note"]])
        quality = queued.get("classification_quality", {})
        if quality:
            counts = quality["counts"]
            lines.extend([f"Accepted: {counts['accepted']}; stale input/version: {counts['stale_input']}/{counts['stale_version']}",
                          f"Low confidence: {counts['low_confidence']}; abstained: {counts['abstained']}; failed: {counts['failed']}; never classified: {counts['never_classified']}"])
        throughput = queued.get("recent_throughput", {})
        if throughput:
            lines.extend([f"Current-version measured cycles: {throughput['current_identity_cycles']}; usable labels/cycle: {throughput['mean_usable_labels_per_cycle']}",
                          throughput["note"]])
    if parsed:
        lines.extend(["", "## Parser checks", "",
                      f"Parse failures: {parsed['parse_errors']}; refusal pages: {parsed['refusals']}",
                      f"Search fallback share: {parsed['search_fallback_share']}", parsed["note"]])
    historical = current.get("history", {})
    if historical:
        missing = sum(day["status"] == "missing" for day in historical["days"])
        lines.extend(["", "## Daily history", "",
                      f"Latest snapshot: {historical['latest_snapshot_at']}; age: {historical['freshness_hours']} hours",
                      f"Missing UTC days in the displayed window: {missing}", historical["note"]])
    storage = current.get("storage", {})
    if storage.get("days"):
        latest = storage["days"][-1]
        lines.extend(["", "## Storage measurements", "",
                      f"Read mean/max: {latest['mean_read_ms']}/{latest['read_ms_max']} ms",
                      f"Busy write probes: {latest['busy_checks']}/{latest['checks']}; database bytes: {latest['database_bytes']}",
                      storage["note"]])
    lines.extend(["", "Unfinished requirement checks: " +
                  (", ".join(health["requirements"]["blocking"]) or "none"), ""])
    authority = health["source_authority"]
    lines.append("Source/action registry: " + ("valid" if authority["valid"] else "requires review"))
    lines.extend(f"- {action}: {'covered' if value['allowed'] else value['reason']}"
                 for action, value in authority["actions"].items())
    lines.extend(f"- {limit}" for limit in health["limits"])
    path = config.REPORTS_DIR / "collection_health.md"
    write_text(path, "\n".join(lines) + "\n")
    return health


def baseline():
    lock, error = db.acquire_lock("collection")
    if error:
        raise RuntimeError("Cannot replace the baseline during a scheduled run")
    try:
        value = {**measurements(), "database": str(config.SQLITE_PATH),
                 "review_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()}
        write_json(config.DATA_DIR / "collection_baseline.json", value)
        write_health(check_model=True)
        return value
    finally:
        db.release_lock(lock)


# Which stat of each source stage means "this stage committed observations".
# Named rather than inlined so a test can hold every source stage's real stat
# output against it -- the failure this replaces was a key that no stage emitted,
# which is invisible until the stage it belonged to stops counting as evidence.
OBSERVATION_STAT = {"atom": "entries", "sweep": "seen", "itempage": "stored"}


def _stream_stage(arguments, environment, timeout):
    """Drain both pipes concurrently; stage results never depend on log tails."""
    process = subprocess.Popen([sys.executable, '-u', '-m', 'toriresearch.cli', *arguments],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    pieces, diagnostics = [], []
    def drain(pipe, sink):
        size = 0
        for line in pipe:
            if size < 1048576:
                sink.append(line[:1048576-size])
                size += len(line)
        pipe.close()
    readers = [threading.Thread(target=drain, args=(process.stdout, pieces), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, diagnostics), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for reader in readers:
        reader.join(timeout=2)
    if diagnostics:
        collection_events.emit('log', {'message': 'Stage diagnostics were emitted; inspect retained warning and error events.'}, severity='warning')
    return ''.join(pieces), 124 if timed_out else process.returncode, timed_out


def run_stage(arguments, timeout, log):
    started = now()
    log.write(f"--- {started} {' '.join(arguments)} ---\n")
    log.flush()
    result = {"stage": arguments[0], "started_at": started}
    environment = dict(os.environ)
    owner = read_json(config.DATA_DIR/'collection_status.json') or {}
    if owner.get('pid') == os.getpid() and owner.get('status') == 'running':
        environment['TORI_COLLECTION_RUN_ID'] = owner['run_id']
        if owner.get('current_job_id'):
            environment['TORI_COLLECTION_JOB_ID'] = owner['current_job_id']
    environment.update(TORI_DATA_DIR=str(config.DATA_DIR), TORI_REPORTS_DIR=str(config.REPORTS_DIR), TORI_DB=str(config.SQLITE_PATH))
    try:
        if environment.get('TORI_COLLECTION_JOB_ID'):
            output, code, timed_out = _stream_stage(arguments, environment, timeout)
            if timed_out:
                result['timed_out'] = True
        else:
            process = subprocess.run([sys.executable, "-m", "toriresearch.cli", *arguments],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, timeout=timeout, env=environment)
            output, code = process.stdout, process.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        code = 124
        result["timed_out"] = True
    except OSError as exc:
        output, code = f"{type(exc).__name__}: stage could not start\n", 1
    log.write(output)
    log.write(f"\n   exit={code}\n")
    log.flush()
    # Persist aggregate stage results, never listing text or command credentials.
    try:
        stats = json.loads(output[output.index("{"):])
    except (ValueError, TypeError):
        stats = {}
    if not isinstance(stats, dict):
        stats = {}
    if stats.get('reason') in ('operator_pause', 'operator_cancel'):
        result['reason'] = stats['reason']
    result.update(exit_code=code, finished_at=now(),
                  stats=collection_health.sanitize_stats(stats))
    # The stat that means "this stage committed observations". For Atom that is
    # `entries`, not `rows_ingested`: the feed's rows are all written, including
    # the ones already known, and each write bumps `source_observed_at`. Naming a
    # field the stage never emits meant Atom could never record an observation
    # timestamp at all, so the freshness check's `stale_observations` verdict was
    # computed from sweep and itempage alone -- and Atom, the one source that
    # always has something new, was invisible to it.
    observed_field = OBSERVATION_STAT.get(arguments[0])
    if observed_field and result["stats"].get(observed_field, 0) > 0:
        try:
            stamp = collection_health.parse_ts(db.scalar("SELECT MAX(source_observed_at) FROM listings WHERE platform='tori'"))
            if stamp and collection_health.parse_ts(started) <= stamp <= collection_health.parse_ts(result["finished_at"]):
                result["last_observation_at"] = stamp.isoformat()
        except Exception as exc:
            result["observation_measurement_error"] = type(exc).__name__
    return result


def cadence_ready(reference=None):
    """Guard calendar/wake/manual starts without overwriting the owner's status."""
    reference = reference or datetime.now(timezone.utc)
    key = collection_health.context()["source_key"]
    records, _malformed = collection_health.history()
    previous = read_json(config.DATA_DIR / "collection_status.json")
    if isinstance(previous, dict):
        records.append(previous)
    starts = [collection_health.parse_ts(row.get("started_at")) for row in records
              if isinstance(row.get("context") or {}, dict) and
              (row.get("context") or {}).get("source_key", key) == key]
    latest = max((stamp for stamp in starts if stamp is not None), default=None)
    durable = continuous.latest_start()
    latest = max((stamp for stamp in (latest,durable) if stamp is not None), default=None)
    # Calendar starts have small interpreter/launch jitter. Wake catch-up runs
    # must not be followed by another batch a minute later at the next boundary.
    return latest is None or (reference-latest).total_seconds() >= config.COLLECTION_INTERVAL_SECONDS-5


def run_pipeline():
    command_id = os.environ.get('TORI_COLLECTION_COMMAND_ID')
    def decline(reason):
        owner = collection_control.active() if command_id else None
        collection_control.finish_command(command_id, 'declined', reason, run_id=owner['run_id'] if owner else None)
    lock, error = db.acquire_lock("collection")
    if error:
        print(error, file=sys.stderr)
        decline('collector_busy')
        return 0  # An existing owner retains its progress/status file.
    if any(name in db.active_locks() for name in ('collection-source','sweep','itempage')) or not cadence_ready():
        db.release_lock(lock)
        decline('collector_busy_or_cadence')
        print("Collection cadence is not due or a child is still active; preserving the previous cycle status.")
        return 0
    operator = collection_control.initialize()
    effective = json.loads(os.environ.get('TORI_COLLECTION_PROFILE_JSON', 'null')) or operator['settings']
    revision = collection_control.command_result(command_id)['payload']['revision'] if command_id else operator['revision']
    initial_profile = collection_control.command_result(command_id)['payload'].get('profile_id') if command_id else operator.get('profile_id')
    path = config.DATA_DIR / "collection_status.json"
    state = {"started_at": now(), "pid": os.getpid(), "status": "running", "stages": [],
             "run_origin": "scheduled" if os.environ.get("TORI_RUN_ORIGIN") == "scheduled" else "manual",
             "measurement_version": collection_health.VERSION, "run_id": uuid.uuid4().hex,
             "context": collection_health.context(), 'settings': effective, 'settings_revision': revision,
             'command_id': command_id}
    code = 0
    degraded = False
    started = time.monotonic()
    complete_deadline = started + config.COLLECTION_RUNTIME_SECONDS
    deadline = complete_deadline-config.ENRICH_RUNTIME_SECONDS-15-3*config.COLLECTION_REPORT_SECONDS-30
    journaled = False
    heartbeat = None
    event_token = None
    telemetry = collection_events.available()
    try:
        if continuous.available():
            if not continuous.begin(state):
                decline('slot_already_consumed')
                print('This collection schedule slot is already recorded; preserving its evidence.')
                db.release_lock(lock)
                return 0
            journaled = True
    except BaseException:
        db.release_lock(lock)
        raise
    try:
        previous_revision = os.environ.get('TORI_COLLECTION_INITIAL_REVISION')
        previous_profile = os.environ.get('TORI_COLLECTION_INITIAL_PROFILE_ID')
        os.environ['TORI_COLLECTION_INITIAL_REVISION'] = str(revision)
        if initial_profile:
            os.environ['TORI_COLLECTION_INITIAL_PROFILE_ID'] = initial_profile
        if journaled and telemetry:
            event_token = collection_events.bind(state['run_id'])
            collection_events.install_logging()
            collection_control.finish_command(command_id, 'running', run_id=state['run_id'])
            collection_events.emit('run_started', {'revision': operator['revision']})
        write_json(path, state)
        if journaled:
            heartbeat = continuous.Heartbeat(state['run_id'])
            heartbeat.start()
        log_path = config.DATA_DIR / "collect.log"
        with log_path.open("a") as log:
            os.chmod(log_path, 0o600)

            def stage(arguments):
                nonlocal degraded
                state["current_stage"] = arguments[0]
                write_json(path, state)
                job = arguments[0]
                timeout = (min(config.COLLECTION_REPORT_SECONDS, complete_deadline-30-time.monotonic()) if job in {"report", "aggregate"} else
                           min(config.ENRICH_RUNTIME_SECONDS+15, complete_deadline-30-2*config.COLLECTION_REPORT_SECONDS-time.monotonic()) if job == 'enrich' else
                           min(2400, deadline-time.monotonic()))
                job_id = continuous.start_job(state, job, max(0,timeout)) if journaled else None
                state['current_job_id'] = job_id
                write_json(path, state)
                halt = None
                if telemetry and journaled:
                    token = collection_events.bind(state['run_id'], job_id)
                    try:
                        collection_control.checkpoint(source=job in continuous.SOURCE_JOBS)
                    except collection_control.CollectionHalted as exc:
                        halt = exc.reason
                    finally:
                        collection_events.reset(token)
                    collection_events.emit('stage_started', {'stage': job}, job_id=job_id)
                    continuous.progress(state)
                if halt:
                    result = {'stage': job, 'started_at': now(), 'finished_at': now(),
                              'exit_code': 76 if halt == 'operator_cancel' else 75, 'reason': halt, 'stats': {}}
                elif timeout <= 0:
                    result = {'stage': job, 'started_at': now(), 'finished_at': now(), 'exit_code': 124,
                              'status': 'partial', 'timed_out': True, 'error_class': 'deadline', 'stats': {}}
                elif ((job == 'sweep' and effective['sweep_budget'] == 0) or (job == 'itempage' and effective['item_budget'] == 0)
                      or (job == 'enrich' and effective['enrich_budget'] == 0)):
                    result = {'stage':job, 'started_at':now(), 'finished_at':now(), 'exit_code':0,
                              'status':'skipped', 'error_class':'disabled', 'stats':{}}
                elif job == 'enrich' and 'enrich' in db.active_locks():
                    result = {'stage':job, 'started_at':now(), 'finished_at':now(), 'exit_code':0,
                              'status':'skipped', 'error_class':'model_busy', 'stats':{'status':'skipped'}}
                else:
                    result = run_stage(arguments, timeout, log)
                    result.setdefault('started_at', now())
                    result.setdefault('finished_at', now())
                if result.get('reason'):
                    state['terminal_reason'] = result['reason']
                if telemetry and journaled:
                    collection_events.emit('stage_finished', {'stage': job, 'exit_code': result['exit_code'],
                                           'counts': result['stats'], 'reason': result.get('reason')}, job_id=job_id)
                if job_id:
                    continuous.finish_job(job_id, result)
                    db.execute('UPDATE collection_cycles SET heartbeat_at=? WHERE run_id=?', (now(), state['run_id']))
                if result["stats"].get("status") == "skipped" or result["stats"].get("errors"):
                    degraded = True
                state["stages"].append(result)
                if journaled:
                    continuous.progress(state)
                write_json(path, state)
                return result

            result = stage(["doctor"])
            code = result["exit_code"]
            if not code:
                # Retention captures history atomically before deleting rows.
                # The daily recovery snapshot includes that committed partition.
                code = stage(["retention"])["exit_code"]
            # Recovery maintenance stays local and runs even during a platform pause.
            if not code:
                job_id = None
                try:
                    remaining = deadline-time.monotonic()
                    job_id = continuous.start_job(state, 'backup', min(120,max(0,remaining))) if journaled else None
                    state["backup"] = backups.ensure(timeout=min(120,remaining))
                    degraded = degraded or state["backup"]["status"] != "ok"
                    if job_id:
                        continuous.finish_job(job_id, {'stats': state['backup'], 'finished_at':now()})
                except Exception as exc:
                    state["backup"] = {"status": "error", "error_type": type(exc).__name__}
                    degraded = True
                    if journaled and job_id:
                        continuous.finish_job(job_id, {'exit_code':1,'error_class':type(exc).__name__,'finished_at':now()})
                write_json(path, state)
            pause = collection_stop()
            if not code and pause:
                code = 75 if pause.get("status") == "throttled" else 69
            plan = [["atom"],
                    ["sweep", "--budget", str(effective["sweep_budget"]), "--pages", str(effective["sweep_pages"])],
                    ["itempage", "--budget", str(effective["item_budget"])]]
            for arguments in plan:
                if code:
                    break
                code = stage(arguments)["exit_code"]
            report_code = 0
            # Local processing requires database/runtime readiness. A source
            # refusal is independent of that readiness and permits local work.
            if result['exit_code'] == 0:
                report_code = stage(["report"])["exit_code"]
                enriched = stage(["enrich", "--budget", str(effective["enrich_budget"])])
                stats = enriched["stats"]
                degraded = degraded or bool(enriched["exit_code"] or stats.get("errors") or
                                            stats.get("skipped_no_provider") or stats.get("schema_violations"))
                history_code = stage(["aggregate"])["exit_code"]
                degraded = degraded or bool(history_code)
                report_code = stage(["report"])["exit_code"] or report_code
            code = code or report_code
        state.update(status="cancelled" if state.get("terminal_reason") == "operator_cancel" else "paused" if code in (69, 75) else "partial" if code == 124 else "error" if code else
                     "degraded" if degraded else "ok", exit_code=code)
        return code
    except Exception as exc:
        state.update(status="partial" if isinstance(exc,TimeoutError) else "error",
                     exit_code=124 if isinstance(exc,TimeoutError) else 1, error_type=type(exc).__name__)
        raise
    finally:
        if heartbeat:
            heartbeat.close()
        try:
            finalize_pipeline(state, path, journaled)
        finally:
            if event_token is not None:
                collection_events.reset(event_token)
            if previous_revision is None:
                os.environ.pop('TORI_COLLECTION_INITIAL_REVISION', None)
            else:
                os.environ['TORI_COLLECTION_INITIAL_REVISION'] = previous_revision
            if previous_profile is None:
                os.environ.pop('TORI_COLLECTION_INITIAL_PROFILE_ID', None)
            else:
                os.environ['TORI_COLLECTION_INITIAL_PROFILE_ID'] = previous_profile
            db.release_lock(lock)


def finalize_pipeline(state, path, journaled):
    state.update(finished_at=now(), current_stage=None)
    if journaled:
        state['mixed_pace'] = any(json.loads(row['result_json']).get('mixed_pace') for row in
                                 db.query('SELECT result_json FROM collection_job_runs WHERE run_id=?', (state['run_id'],)))
    if not state.get('terminal_reason'):
        state['terminal_reason'] = ('deadline' if state.get('exit_code') == 124 else
                                    'platform_throttle' if state.get('exit_code') == 75 else
                                    'platform_block' if state.get('exit_code') == 69 else
                                    'stage_error' if state.get('exit_code') else
                                    'degraded_processing' if state.get('status') == 'degraded' else 'completed')
    persisted_pause = collection_stop()
    if isinstance(persisted_pause, dict):
        state["platform_pause"] = {key: persisted_pause[key] for key in ("status", "at") if key in persisted_pause}
    current = None
    try:
        current = measurements()
        state["followup"] = {key: current["coverage"][key] for key in
                             ("n_tracked", "n_unresolved", "n_due_first_in_scope",
                              "n_due_repeat_in_scope", "p95_first_check_age_hours",
                              "p95_repeat_lateness_hours")}
        state["followup"]["cohort"] = {key: value for key, value in current["coverage"]["followup"].items()
                                       if key in ("active", "due", "due_first", "due_repeat", "retry_due",
                                                  "overdue_repeat", "quarantined", "p95_first_check_age_hours", "p95_repeat_lateness_hours", "max_active", "by_purpose", "pending_intents", "service_version")}
    except Exception as exc:
        state["measurement_error"] = type(exc).__name__
    write_json(path, state)
    if journaled:
        if collection_events.available():
            collection_events.emit('run_finished', {'status': state['status'], 'reason': state['terminal_reason'], 'exit_code': state.get('exit_code')})
            collection_control.finish_command(state.get('command_id'), state['status'], state['terminal_reason'])
        continuous.finish(state)
        if collection_events.available():
            db.execute("UPDATE collection_commands SET status='not_applied',reason='run_finished',applied_at=? WHERE run_id=? AND command='stop' AND status='pending'", (state['finished_at'],state['run_id']))
    history = config.DATA_DIR / "collection_runs.jsonl"
    with history.open("a") as handle:
        os.chmod(history, 0o600)
        handle.write(json.dumps(state) + "\n")
    try:
        write_health(check_model=True, current=current)
    except Exception as exc:
        state["health_report_error"] = type(exc).__name__
        write_json(path, state)


def main():
    from . import local_runtime
    delegated = local_runtime.delegate("toriresearch.operations")
    if delegated is not None:
        return delegated
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--baseline", action="store_true", help="Start a new measured 24-hour review window")
    mode.add_argument("--status", action="store_true", help="Refresh health without collecting data")
    mode.add_argument("--check", action="store_true", help="Read-only freshness check; exits nonzero when attention is needed")
    mode.add_argument("--review", action="store_true", help="Refresh the seven-day review without collecting data")
    mode.add_argument("--backup", action="store_true", help="Create a verified local recovery snapshot now")
    mode.add_argument("--verify-backup", action="store_true", help="Rehearse a restore into a temporary database")
    mode.add_argument('--plan', action='store_true', help='Preview effective jobs, budgets and freshness capacity without requests')
    mode.add_argument('--observe', action='store_true', help='Run the independent local freshness observer')
    mode.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.plan:
        result = continuous.plan()
        result['discovery'] = discovery.report()
        result['followup'] = followup.status()
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.observe:
        from . import collection_observer
        return collection_observer.run()
    if args.check:
        result = collection_health.freshness()
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in ("ok", "running") else 1
    if args.backup or args.verify_backup:
        try:
            result = backups.ensure(force=True) if args.backup else backups.verify()
        except Exception as exc:
            print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "ok" else 1
    if args.baseline:
        value = baseline()
        print(f"Baseline saved; review due {value['review_at']}")
        return 0
    if args.review:
        result = write_health()["review"]
        print(json.dumps({"status": result["status"], "complete_days": result["complete_days"],
                          "review_not_before": result["review_not_before"],
                          "eligible_for_expansion_review": result["eligible_for_expansion_review"],
                          "gates": {name: gate["status"] for name, gate in result["gates"].items()}}, indent=2))
        return 0
    if args.status:
        health = write_health(check_model=True)
        print(json.dumps({"pipeline": (health["pipeline"] or {}).get("status"),
                          "platform_pause": (health["platform_pause"] or {}).get("status"),
                          "coverage": health["coverage"], "delta": health["delta"],
                          "freshness": health["freshness"], "capacity": health["capacity"]["capacity"]}, indent=2))
        return 0
    if args.worker:
        def stop(_signal, _frame):
            raise TimeoutError('Collection supervisor stopped an overdue cycle')
        signal.signal(signal.SIGTERM, stop)
        return run_pipeline()
    return supervise()


def supervise():
    """The real entrypoint bounds the whole worker, including finally/maintenance."""
    environment = dict(os.environ)
    effective = json.loads(environment.get('TORI_COLLECTION_PROFILE_JSON', 'null')) or collection_control.current()['settings']
    environment.update(collection_control.environment(effective))
    environment.update(TORI_DATA_DIR=str(config.DATA_DIR), TORI_REPORTS_DIR=str(config.REPORTS_DIR), TORI_DB=str(config.SQLITE_PATH))
    worker = subprocess.Popen([sys.executable, '-m', 'toriresearch.operations', '--worker'], start_new_session=True, env=environment)
    try:
        return worker.wait(timeout=config.COLLECTION_RUNTIME_SECONDS-5)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        try:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait()
        except ProcessLookupError:
            worker.wait()
        return 124


if __name__ == "__main__":
    sys.exit(main())
