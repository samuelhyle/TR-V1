"""Persistence and the batch runner.

Two rules hold here.

Derived values live in `enrichment`, never in `listings`. Nothing in this module
writes to a column a reported statistic reads. That separation is the reason a
panel can honestly say whether a figure was measured or inferred.

Results are keyed on prompt/model identity, vocabulary meaning, category and
title. Formatting edits preserve results; semantic edits invalidate them.
Recorded byte hashes keep the original vocabulary traceable, and reconciliation
migrates only proven compatible legacy hashes without another inference.
"""

import json
import logging
import math
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from .. import collection, config, db
from . import provider, queue, tasks, vocabulary

log = logging.getLogger("toriresearch.enrich")


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def already_enriched(platform, listing_id, task, digest):
    return db.query_one(
        "SELECT 1 FROM enrichment WHERE platform=? AND listing_id=? AND task=? "
        "AND input_sha256=? AND output_json IS NOT NULL AND output_json<>'null'",
        (platform, listing_id, task, digest)) is not None


def store(record, platform, listing_id, listing_pk=None, notes=None, conn=None):
    """Persist one derivation. A model failure is recorded, not silently dropped."""
    owns = conn is None
    conn = conn or db.connect()
    validation = record.get("validation")
    if record.get("output_json") is None:
        error = record.get("error") or ""
        validation = "schema_violation" if error == "schema_violation" else "provider_unavailable" if error.startswith("provider_unavailable") else "inference_failure"
    try:
        conn.execute(
            """INSERT INTO enrichment
                   (listing_pk, platform, listing_id, task, input_sha256, model,
                    provider, prompt_version, schema_version, output_json,
                    model_confidence_raw, confidence, confidence_basis, validation,
                    observed_at, latency_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (platform, listing_id, task, input_sha256) DO UPDATE SET
                   model=excluded.model, provider=excluded.provider,
                   prompt_version=excluded.prompt_version, schema_version=excluded.schema_version,
                   output_json=excluded.output_json, confidence=excluded.confidence,
                   model_confidence_raw=excluded.model_confidence_raw,
                   confidence_basis=excluded.confidence_basis, validation=excluded.validation,
                   observed_at=excluded.observed_at, latency_ms=excluded.latency_ms
               WHERE enrichment.output_json IS NULL OR enrichment.output_json='null'""",
            (listing_pk, platform, listing_id, record["task"], record["input_sha256"],
             record.get("model"), record.get("provider"), record.get("prompt_version"),
             record.get("schema_version"),
             json.dumps(record.get("output_json"), sort_keys=True, ensure_ascii=False),
             record.get("model_confidence_raw"), record.get("confidence"),
             record.get("confidence_basis"),
             validation, _utcnow(),
             record.get("latency_ms")),
        )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def candidates(platform="tori", limit=60, task="product_type"):
    """Listings not yet classified for the current model, prompt and vocabulary.

    Ordered by discovery for the same reason the item-page prober is: an
    unresolved listing is the one closest to losing the information a classifier
    would have extracted from it.
    """
    vocab = vocabulary.load()
    versions = vocabulary.compatible_versions(vocab)
    limit = int(limit)
    if limit <= 0:
        return []
    out = []
    focused, values = collection.Scope(platform).conditions()
    clause = " AND " + " AND ".join(focused) if focused else ""
    cached = {(row["listing_id"], row["input_sha256"]) for row in db.query(
        "SELECT listing_id, input_sha256, model FROM enrichment "
        "WHERE platform=? AND task=? AND prompt_version=? "
        "AND schema_version IN ("+','.join('?' for _ in versions)+") "
        "AND output_json IS NOT NULL AND output_json<>'null'",
        (platform, task, tasks.PROMPT_VERSION, *versions)) if tasks.same_model(row["model"])}
    after, batch_size = 0, max(100, limit)
    while len(out) < limit:
        rows = db.query(
            "SELECT listing_pk, listing_id, platform, title, category_norm, category_raw_id, "
            "category_root_norm, municipality_norm FROM listings "
            "WHERE platform=? AND title IS NOT NULL AND title<>'' AND listing_pk>? " + clause +
            " ORDER BY listing_pk LIMIT ?", [platform, after] + values + [batch_size])
        if not rows:
            break
        for row in rows:
            if not any((row["listing_id"], tasks.input_digest(row["title"], row["category_norm"], version)) in cached
                       for version in versions):
                out.append(row)
                if len(out) == limit:
                    break
        after = rows[-1]["listing_pk"]
    return out


def run(platform="tori", budget=None, task="product_type", concurrency=None,
        dry_run=False, scope=None):
    """Consume durable local tasks within a separate runtime and item budget."""
    if task == "listing_analysis":
        from . import listing_queue
        return listing_queue.run(platform,budget,concurrency,dry_run)
    budget = int(config.ENRICH_BUDGET if budget is None else budget)
    concurrency = int(config.LLM_CONCURRENCY if concurrency is None else concurrency)
    if budget < 1 or concurrency < 1 or task not in tasks.TASKS:
        raise ValueError("Classification requires positive budgets and a supported task")
    stats = {"requested": budget, "attempted": 0, "classified": 0, "abstained": 0,
             "cached": 0, "errors": 0, "skipped_no_provider": 0,
             "schema_violations": 0, "prompt_tokens": 0, "accepted": 0,
             "stale_results": 0, "runtime_limited": 0, "elapsed_s": None,
             "cached_reused": 0, "model_requests": 0, "runtime_deferred": 0}
    verticals = collection.VerticalCounters(platform)
    stats["by_vertical"] = verticals.values
    if dry_run:
        rows = candidates(platform=platform, limit=budget, task=task)
        stats.update(candidates=len(rows), next=[{"listing_id":r["listing_id"], "title":r["title"],
                                                "platform_category":r["category_norm"]} for r in rows[:15]])
        return stats
    if platform == "tori":
        from .. import permissions
        permissions.require("tori", "enrichment", category_paths=list(config.FOCUS_CATEGORIES),
                            municipalities=list(config.FOCUS_MUNICIPALITIES))
    lock, error = db.acquire_lock("enrich")
    if lock is None:
        return {**stats, "status":"skipped", "reason":error}
    started = time.monotonic()
    deadline = started+config.ENRICH_RUNTIME_SECONDS
    completed = []
    try:
        try:
            stats.update(queue.reconcile(platform, task, deadline=deadline))
        except (ValueError, OSError) as exc:
            stats.update(errors=1, configuration_error=type(exc).__name__)
            return stats
        stats["classification_identity"] = tasks.classification_identity()
        stats["classification_identity_sha256"] = tasks.classification_digest(platform)
        stats["platform"] = platform
        stats["queue"] = queue.report(platform)
        health = provider.health()
        if not health["reachable"]:
            stats["skipped_no_provider"] = 1
            return stats
        abstain = vocabulary.load()["abstain"]
        scope = scope or collection.Scope(platform)
        stop = False
        def work(row):
            timeout = min(config.LLM_TIMEOUT, max(0.1, deadline-time.monotonic()))
            record = tasks.classify_title(row["title"], row["category_norm"],timeout=timeout,max_retries=1)
            error = record.get('error') or ''
            if (record.get('output_json') is None and error.startswith('provider_unavailable')
                    and 'timed out' in error.lower() and timeout < config.LLM_TIMEOUT and time.monotonic() >= deadline):
                record['runtime_expired'] = True
            return record
        # Workers only infer. The owning thread serializes all task/result writes.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            dispatched = 0
            futures = {}
            exhausted = False
            while futures or dispatched < budget and not stop and not exhausted:
                while len(futures) < concurrency and dispatched < budget and not stop and not exhausted:
                    from .. import collection_control
                    try:
                        collection_control.checkpoint(source=False)
                    except collection_control.CollectionHalted:
                        stop = True
                        break
                    if time.monotonic() >= deadline:
                        stats["runtime_limited"] = 1
                        break
                    fresh = math.floor((dispatched+1)*config.ENRICH_FRESH_SHARE) > math.floor(dispatched*config.ENRICH_FRESH_SHARE)
                    row = queue.claim(platform, task, retry=dispatched % 10 == 9, scope=scope,
                                      freshest=fresh, deadline=deadline, stats=stats)
                    if row is None:
                        exhausted = True
                        break
                    futures[pool.submit(work, row)] = row
                    dispatched += 1
                    stats['model_requests'] += 1
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    row = futures.pop(future)
                    stats["attempted"] += 1
                    verticals.add(row, attempted=1)
                    try:
                        record = future.result()
                    except Exception as exc:
                        record = {"task":task, "input_sha256":row["input_sha256"],
                                  "output_json":None, "error":"classification_exception"}
                        log.warning("local inference raised %s", type(exc).__name__)
                    try:
                        if record.get('runtime_expired'):
                            if queue.defer(row):
                                stats['runtime_deferred'] += 1
                                verticals.add(row,runtime_deferred=1)
                            else:
                                stats['stale_results'] += 1
                            stats['runtime_limited'] = 1
                            stop = True
                            continue
                        state = queue.finish(row, record, scope=scope)
                    except Exception as exc:
                        stats["errors"] += 1
                        verticals.add(row, errors=1)
                        log.warning("classification commit raised %s", type(exc).__name__)
                        stop = True
                        continue
                    if state in ("lease_lost", "stale"):
                        stats["stale_results"] += 1
                        continue
                    if record.get("error") == "schema_violation":
                        stats["schema_violations"] += 1
                        verticals.add(row, schema_violations=1)
                    if record.get("output_json") is None:
                        stats["errors"] += 1
                        verticals.add(row, errors=1)
                        if (record.get("error") or "").startswith("provider_unavailable"):
                            stop = True
                            stats["skipped_no_provider"] = 1
                    else:
                        completed.append(row)
                        key = "abstained" if record["output_json"].get("product_type") == abstain else "classified"
                        stats[key] += 1
                        verticals.add(row, **{key:1})
        from .. import analysis
        accepted = analysis.accepted_product_types(completed)
        stats["accepted"] = len(accepted)
        for row in completed:
            if row["listing_pk"] in accepted:
                verticals.add(row, accepted=1)
        if time.monotonic() >= deadline:
            stats["runtime_limited"] = 1
        stats["queue"] = queue.report(platform)
        return stats
    finally:
        stats["elapsed_s"] = round(time.monotonic()-started, 1)
        db.release_lock(lock)


def summary(platform="tori"):
    """Enrichment coverage, reported separately from observation coverage.

    Kept apart on purpose: "we have classified 400 listings" is not the same
    claim as "we have observed 400 listings", and merging them would let a
    derived figure stand in for a measured one.
    """
    from .. import analysis
    rows = db.query("SELECT " + analysis.FIELDS + " FROM listings WHERE platform=?", (platform,))
    total = len(rows)
    enriched = db.scalar(
        "SELECT COUNT(DISTINCT listing_id) FROM enrichment WHERE platform=? "
        "AND task='product_type' AND output_json IS NOT NULL AND output_json<>'null'",
        (platform,)) or 0
    by_type = db.query(
        """SELECT json_extract(output_json, '$.product_type') AS product_type,
                  COUNT(*) AS n, ROUND(CAST(AVG(confidence) AS NUMERIC), 3) AS mean_confidence
           FROM enrichment WHERE platform=? AND task='product_type' AND output_json IS NOT NULL AND output_json<>'null'
           GROUP BY 1 ORDER BY n DESC""", (platform,))
    violations = db.scalar(
        "SELECT COUNT(*) FROM enrichment WHERE platform=? AND task='product_type' AND validation='schema_violation'",
        (platform,)) or 0
    abstained = db.scalar(
        """SELECT COUNT(*) FROM enrichment
           WHERE platform=? AND task='product_type' AND json_extract(output_json, '$.product_type') = ?""",
        (platform, vocabulary.load()["abstain"])) or 0

    confidence_groups = [row for row in by_type if row["mean_confidence"] is not None]

    return {
        "observations": total,
        "enriched_listings": enriched,
        "enrichment_coverage_pct": round(100 * enriched / total, 2) if total else None,
        "distinct_product_types": len([r for r in by_type
                                       if r["product_type"] not in (None, vocabulary.load()["abstain"])]),
        "abstained": abstained,
        "schema_violations": violations,
        "mean_confidence": (round(sum(float(r["mean_confidence"])*r["n"] for r in confidence_groups)
                                   / sum(r["n"] for r in confidence_groups), 3)
                            if confidence_groups else None),
        "vocabulary": vocabulary.coverage(),
        "classification_quality": analysis.classification_quality(rows),
        "by_product_type": [dict(r) for r in by_type[:20]],
    }


def product_type_panel(min_n=None, filters=None):
    """Ask-price distribution by *derived* product type.

    The label on every row says derived. It is a legitimate panel and it is not
    the same kind of claim as a panel keyed on the platform's own category, and
    a reader should never be able to confuse the two.
    """
    from .. import analysis, metrics

    min_n = config.MIN_N_FOR_REPORT if min_n is None else int(min_n)
    where, params, applied = metrics.listing_filters(**(filters or {}))
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = db.query("SELECT " + analysis.FIELDS + " FROM listings" + clause, params)
    types = analysis.accepted_product_types(rows)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    grouped = {}
    for r in rows:
        label = types.get(r["listing_pk"])
        if label and analysis.is_priced(r, cutoff):
            grouped.setdefault(label, []).append(float(r["listed_price_eur"]))

    out = []
    for product_type, prices in sorted(grouped.items(),
                                       key=lambda kv: -len(kv[1])):
        n = len(prices)
        entry = {"product_type": product_type, "n": n, "basis": "derived"}
        if metrics.suppress(n, min_n):
            entry["status"] = "INSUFFICIENT DATA"
            out.append(entry)
            continue
        pcts = metrics.percentiles(prices)
        ci = metrics.bootstrap_median_ci(prices)
        entry.update({
            "status": "OK",
            "median_eur": round(pcts[0.50], 2),
            "p25_eur": round(pcts[0.25], 2),
            "p75_eur": round(pcts[0.75], 2),
            "median_ci95": ([round(ci["lo"], 2), round(ci["hi"], 2)]
                            if ci else None),
        })
        out.append(entry)
    return {
        "basis": "derived",
        "caveat": ("Item type is model-derived, not a platform label. Every row "
                   "here carries n and a bootstrap CI, and cells below the "
                   "threshold report no figure at all."),
        "min_n_threshold": min_n,
        "filters": applied,
        "max_age_hours": config.PANEL_MAX_AGE_HOURS,
        "cells": out,
    }
