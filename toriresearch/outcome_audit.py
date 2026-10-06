"""Enrollment-based outcome denominators and retained check timing.

Ended enrollments are censored at their exit. Later evidence cannot retroactively
turn a capacity reduction, operator trim or source gap into a completed sale.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import config, db, followup, metrics

VERSION = "outcome-cohorts-v1"
TERMINAL = {"sold_confirmed", "sold_inferred", "withdrawn", "expired"}


def _timing(values):
    return dict(n_known=len(values), median_hours=metrics.percentiles(values, (.5,))[.5] if values else None,
                p95_hours=metrics.percentiles(values, (.95,))[.95] if values else None)


def describe(listings, horizon_days=30, min_n=30, reference=None, limit=50):
    reference = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ids = sorted({r["listing_pk"] for r in listings if r.get("listing_pk") is not None})
    metadata, enrollments, observations = {}, [], defaultdict(list)
    for offset in range(0, len(ids), 400):
        keys = ids[offset:offset+400]
        marks = ",".join("?" for _ in keys)
        admitted = db.query("SELECT * FROM followup_enrollments WHERE listing_pk IN ("+marks+")", keys)
        enrollments += admitted
        keys = sorted({e["listing_pk"] for e in admitted})
        if not keys:
            continue
        marks = ",".join("?" for _ in keys)
        metadata.update({r["listing_pk"]: r for r in db.query(
            "SELECT listing_pk,first_seen_at,platform_published_at FROM listings WHERE listing_pk IN ("+marks+")", keys)})
        for row in db.query("SELECT listing_pk,observed_at,state_at,source,state_json FROM listing_observations "
                            "WHERE listing_pk IN ("+marks+")", keys):
            stamp = metrics.parse_ts(row["observed_at"])
            state_at = metrics.parse_ts(row["state_at"])
            if stamp and state_at and stamp <= reference and state_at <= reference:
                observations[row["listing_pk"]].append((stamp, state_at, row["source"], json.loads(row["state_json"])))
    for timeline in observations.values():
        timeline.sort(key=lambda item: (item[0], item[1]))
    groups = defaultdict(list)
    for enrollment in enrollments:
        start = metrics.parse_ts(enrollment["enrolled_at"])
        if not start or start > reference:
            continue
        end = min(metrics.parse_ts(enrollment["ended_at"]) or reference, reference)
        if end < start:
            continue
        timeline = [item for item in observations[enrollment["listing_pk"]]
                    if start <= item[0] <= end and item[1] <= end]
        checks = [item[0] for item in timeline if item[2] == "item_page"]
        state = timeline[-1][3] if timeline else {}
        outcome_stamp = metrics.parse_ts(state.get("outcome_observed_at"))
        outcome = state.get("outcome_status", "unknown")
        # A carried marker older than enrollment is not a prospective event.
        if outcome not in TERMINAL or not outcome_stamp or not start <= outcome_stamp <= end:
            outcome = "unknown"
        coverage = outcome_stamp if outcome in TERMINAL else max((item[0] for item in timeline), default=start)
        meta = metadata.get(enrollment["listing_pk"], {})
        discovered = metrics.parse_ts(meta.get("first_seen_at"))
        published = metrics.parse_ts(meta.get("platform_published_at"))
        discovery_age = (discovered-published).total_seconds()/3600 if discovered and published and discovered >= published else None
        gaps = [max(0, (b-a).total_seconds()/3600) for a, b in zip(checks, checks[1:])]
        # Successful intervals are separate from retry dispatch/backoff clocks.
        lateness = [max(0, gap-config.PROBE_INTERVAL_HOURS) for gap in gaps]
        if enrollment["ended_at"] is None and checks:
            lateness.append(max(0, (reference-checks[-1]).total_seconds()/3600-config.PROBE_INTERVAL_HOURS))
        key = (enrollment["policy_key"], followup.purpose(enrollment), enrollment["vertical"], start.date().isoformat())
        groups[key].append(dict(enrollment=enrollment, duration=max(0, (coverage-start).total_seconds()/86400),
                                outcome=outcome, checks=len(checks), discovery_age=discovery_age,
                                first_delay=(checks[0]-start).total_seconds()/3600 if checks else None,
                                lateness=lateness))
    horizons = sorted(set([7,14,30,60,90,float(horizon_days)]))
    if any(not math.isfinite(h) or h <= 0 for h in horizons):
        raise ValueError("Horizon must be finite and positive")
    reports = []
    for (policy, purpose, vertical, day), members in sorted(groups.items(), key=lambda item: (item[0][3], item[0][:3]), reverse=True):
        counts = {kind: sum(m["outcome"] == kind for m in members) for kind in sorted(TERMINAL)}
        probabilities = [m["enrollment"]["selection_probability"] for m in members if m["enrollment"]["selection_probability"] is not None]
        pools = defaultdict(int)
        for member in members:
            e = member["enrollment"]
            pools[(e["batch_id"],e["stratum"],e["population_size"],e["selection_probability"])] += 1
        pool_rows = [dict(batch_id=batch,stratum=stratum,eligible_stratum_pool=population,
                          conditional_probability=probability,admitted_in_this_slice=count)
                     for (batch,stratum,population,probability),count in sorted(pools.items())]
        reports.append(dict(conditional_admission_pools=pool_rows[:100],admission_pools_total=len(pool_rows),
            admission_pools_truncated=len(pool_rows)>100,
            policy_key=policy,purpose=purpose,vertical=vertical,cohort_date_utc=day,
            denominator_enrollments=len(members), evidence_counts=counts,
            censored_unknown=sum(m["outcome"] == "unknown" for m in members),
            ended_reasons=dict((reason, sum(m["enrollment"]["end_reason"] == reason for m in members))
                               for reason in sorted({m["enrollment"]["end_reason"] for m in members if m["enrollment"]["end_reason"]})),
            with_successful_detail_check=sum(m["checks"] > 0 for m in members),
            awaiting_or_missing_first_check=sum(m["checks"] == 0 for m in members),
            discovery_age=_timing([m["discovery_age"] for m in members if m["discovery_age"] is not None]),
            first_check_delay=_timing([m["first_delay"] for m in members if m["first_delay"] is not None]),
            repeat_lateness=_timing([v for m in members for v in m["lateness"]]),
            conditional_probability_range=[min(probabilities),max(probabilities)] if probabilities else None,
            admission_batches=len({m["enrollment"]["batch_id"] for m in members}),
            horizon_support={f"{h:g}d": sum(m["duration"] >= h for m in members) for h in horizons},
            evidence_status={kind: "DESCRIPTIVE SUPPORT" if counts[kind] >= min_n and
                             sum(m["duration"] >= horizon_days for m in members) >= min_n else "INSUFFICIENT DATA"
                             for kind in ("sold_confirmed","sold_inferred")},
            probability_estimate=None,
            uncertainty_note="Retained evidence only. Unknowns are censored at last observation; historical retention gaps and actual transaction time remain unknown. Conditional batches have no pooled market probability."))
    earliest = min((metrics.parse_ts(e["enrolled_at"]) for e in enrollments if metrics.parse_ts(e["enrolled_at"])), default=None)
    cycles = db.query("SELECT started_at,status FROM collection_cycles WHERE started_at>=? AND started_at<=?",
                      (earliest.isoformat(), reference.isoformat())) if earliest else []
    return dict(recorded_collection_cycles=len(cycles), recorded_paused_cycles=sum(c["status"] == "paused" for c in cycles),
                pause_note="Recorded cycles across the configured collector; unrecorded downtime and pause duration are unknown",
                version=VERSION, listing_slice_denominator=len(ids), eligible_listing_denominator=len(ids),
                eligibility_basis="Supplied report slice, including resolved listings. Actual conditional eligible admission denominators are reported separately per stratum/batch; slice totals are not the unresolved admission pool.",
                not_enrolled_listings=len(set(ids)-{e["listing_pk"] for e in enrollments}),
                enrollment_denominator=sum(r["denominator_enrollments"] for r in reports),
                cohort_groups_total=len(reports),cohorts=reports[:limit],truncated=len(reports)>limit,
                min_n_threshold=min_n, clock="Enrollment to retained observation or marker, censored at enrollment exit",
                note="Purposes, policy dates and verticals remain separate. Vertical scope is not audited canonical product identity. Re-enrollments are distinct measurement episodes, not additional units. Discovery age is known only when publication time was retained.")
