"""Durable local classification with one current task per listing and task type."""

import argparse
import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone

from .. import collection, config, db, product_identity
from ..metrics import parse_ts
from . import tasks, vocabulary

VERSION = "enrichment-queue-v4"
FIELDS = ("listing_pk,platform,subplatform,listing_id,title,category_norm,category_raw_id,"
          "category_root_norm,condition_norm,municipality_norm")

_CELL_CACHE = {}


def now():
    return datetime.now(timezone.utc)


def digest(row):
    return tasks.input_digest(row["title"], row["category_norm"], vocabulary.load()["sha256"][:12])


def research_cells(conn, platform, cutoff):
    """Category/condition cells large enough that a price comparison could clear MIN_N.

    `research_candidates` needs more than MIN_N peers sharing category, condition,
    product type, brand and model. The last three are unknown until a title has been
    classified, so this is the pre-classification upper bound: a cell that cannot reach
    MIN_N on category and condition alone cannot produce a candidate however well it is
    labelled, so spending the local model on it buys nothing.

    Product type splits a cell further, so this over-selects. That is the safe direction
    to be wrong in -- it costs some classifications spent on a cell that ends up too
    thin, and the alternative is starving every cell that could have produced one.

    Freshness and price use `analysis.is_priced` rather than a second copy of the rule,
    because a drift between the two would silently change which cells count.
    """
    from .. import analysis
    hour = cutoff.replace(minute=0, second=0, microsecond=0)
    cached = _CELL_CACHE.get((platform, hour))
    if cached is not None:
        return cached
    # The app process outlives many collection cycles, so keep only the current hour
    # rather than one entry per hour forever.
    for key in [key for key in _CELL_CACHE if key[1] != hour]:
        del _CELL_CACHE[key]
    # Select the analysis column set rather than naming the fields the freshness rule
    # reads today: `is_priced` grows a term now and then, and a column list written here
    # would quietly drop out of date without failing anything visible.
    rows = conn.execute("SELECT " + analysis.FIELDS + " FROM listings "
                        "WHERE platform=? AND category_norm IS NOT NULL AND category_norm<>'' "
                        "AND category_norm<>category_root_norm AND condition_norm IS NOT NULL "
                        "AND condition_norm NOT IN ('','unknown')", (platform,)).fetchall()
    counts = {}
    for raw in rows:
        row = dict(raw)
        if not analysis.is_priced(row, cutoff):
            continue
        key = (row["category_norm"], row["condition_norm"])
        counts[key] = counts.get(key, 0) + 1
    cells = frozenset(key for key, count in counts.items() if count > config.MIN_N_FOR_REPORT)
    _CELL_CACHE[(platform, hour)] = cells
    return cells


def _sync(conn, rows, platform, task, scope, stamp, stats=None):
    """Load tasks/cache only for this batch; preserve unchanged retries and leases."""
    if not rows:
        return 0
    marks = ','.join('?' for _ in rows)
    keys = [row['listing_pk'] for row in rows]
    prior = {r['listing_pk']: dict(r) for r in conn.execute(
        f"SELECT * FROM enrichment_tasks WHERE task=? AND listing_pk IN ({marks})", [task, *keys])}
    local = [dict(r) for r in conn.execute(
        f"SELECT * FROM enrichment WHERE platform=? AND task=? AND listing_pk IN ({marks}) "
        "AND output_json IS NOT NULL AND output_json<>'null' ORDER BY observed_at DESC,enrichment_pk DESC",
        [platform, task, *keys])]
    cached = {(r['listing_pk'], r['input_sha256']) for r in local}
    vocab = vocabulary.load()
    versions = vocabulary.compatible_versions(vocab, conn=conn)
    pilot = product_identity.load()
    eligible = {r['listing_pk']:digest(r) for r in rows if r['title'] and scope.matches(dict(r))}
    inputs = {tasks.input_digest(r['title'], r['category_norm'], version): eligible[r['listing_pk']]
              for r in rows if r['listing_pk'] in eligible for version in versions}
    shared, local_sources = {}, {}

    def reusable(entry):
        try:
            label = json.loads(entry['output_json']).get('product_type')
        except (ValueError, AttributeError, TypeError):
            return False
        return (entry['prompt_version'] == tasks.PROMPT_VERSION and entry['schema_version'] in versions
                and tasks.same_model(entry['model']) and label in vocab['allowed'])

    for entry in local:
        value = inputs.get(entry['input_sha256'])
        if value and value == eligible.get(entry['listing_pk']) and reusable(entry):
            local_sources.setdefault(entry['listing_pk'], entry)
    if inputs:
        # The digest covers exact title/category, model revision, prompt and vocabulary.
        # Keep original inference time and provenance when copying an identical input.
        for raw in conn.execute(
                "SELECT * FROM enrichment WHERE platform=? AND task=? AND input_sha256 IN ("+
                ','.join('?' for _ in inputs)+") AND output_json IS NOT NULL AND output_json<>'null' "
                "ORDER BY observed_at DESC,enrichment_pk DESC", [platform, task, *inputs]):
            entry = dict(raw)
            if reusable(entry):
                shared.setdefault(inputs[entry['input_sha256']], entry)
    added = 0
    for raw in rows:
        row = dict(raw)
        pk, old = row['listing_pk'], prior.get(row['listing_pk'])
        if not row.get('title') or not scope.matches(row):
            if old:
                conn.execute("UPDATE enrichment_tasks SET state='suspended',lease_token=NULL,lease_until=NULL "
                             "WHERE listing_pk=? AND task=?", (pk, task))
            continue
        value = eligible[pk]
        # Supported pilot families get the fresh research lane. A large broad
        # category cannot justify comparison eligibility or displace supported
        # profile work. The ordinary lane still serves older/exploratory tasks.
        pilot_family = any(pattern.search(row['title'])
                           for family in pilot['families'].values() for pattern in family['_hints'])
        priority = int(pilot_family)
        complete = (pk, value) in cached
        source = local_sources.get(pk) or shared.get(value)
        if not complete and source:
            # Append provenance without changing validation or confidence gates.
            conn.execute("INSERT INTO enrichment (listing_pk,platform,listing_id,task,input_sha256,model,provider,"
                         "prompt_version,schema_version,output_json,model_confidence_raw,confidence,confidence_basis,"
                         "validation,observed_at,latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
                         "ON CONFLICT(platform,listing_id,task,input_sha256) DO UPDATE SET "
                         "output_json=excluded.output_json,model=excluded.model,provider=excluded.provider,"
                         "prompt_version=excluded.prompt_version,schema_version=excluded.schema_version,"
                         "model_confidence_raw=excluded.model_confidence_raw,confidence=excluded.confidence,"
                         "confidence_basis=excluded.confidence_basis,validation=excluded.validation,"
                         "observed_at=excluded.observed_at,latency_ms=0 "
                         "WHERE enrichment.output_json IS NULL OR enrichment.output_json='null'",
                         (pk,platform,row['listing_id'],task,value,source['model'],source['provider'],
                          source['prompt_version'],vocab['sha256'][:12],source['output_json'],
                          source['model_confidence_raw'],source['confidence'],source['confidence_basis'],
                          (source['validation'] or '')+',cache_reused='+str(source['enrichment_pk'])+
                          (',vocabulary_migrated_from='+source['schema_version']
                           if source['schema_version'] != vocab['sha256'][:12] else ''), source['observed_at']))
            from .. import collection_events
            collection_events.attribute(conn, pk, {**row, 'product_type': json.loads(source['output_json']).get('product_type'),
                'confidence': source['confidence'], 'validation': source['validation'], 'input_sha256': value, 'cached': True},
                kind='classification', commit_key=value)
            collection_events.emit('classification_completed', {'classified': 1, 'cached': 1}, conn=conn)
            complete = True
            if stats is not None:
                stats['cached_reused'] = stats.get('cached_reused',0)+1
                if source['schema_version'] != vocab['sha256'][:12]:
                    stats['vocabulary_migrated'] = stats.get('vocabulary_migrated',0)+1
        if old and old['input_sha256'] == value and old['state'] != 'suspended':
            if old['research_priority'] != priority:
                conn.execute("UPDATE enrichment_tasks SET research_priority=? WHERE listing_pk=? AND task=?", (priority, pk, task))
            if complete and old['state'] != 'complete':
                conn.execute("UPDATE enrichment_tasks SET state='complete',completed_at=?,lease_token=NULL, "
                             "lease_until=NULL,error_class=NULL WHERE listing_pk=? AND task=?", (stamp, pk, task))
            continue
        conn.execute("INSERT INTO enrichment_tasks (listing_pk,task,input_sha256,research_priority,state,due_at,queued_at,completed_at) "
                     "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(listing_pk,task) DO UPDATE SET "
                     "input_sha256=excluded.input_sha256,state=excluded.state,due_at=excluded.due_at, "
                     "research_priority=excluded.research_priority, "
                     "queued_at=excluded.queued_at,completed_at=excluded.completed_at,failure_count=0, "
                     "error_class=NULL,lease_token=NULL,lease_until=NULL,last_attempt_at=NULL",
                     (pk, task, value, priority, 'complete' if complete else 'ready', stamp, stamp, stamp if complete else None))
        added += not complete
    return added


def reconcile(platform="tori", task="product_type", deadline=None, full=False):
    """Consume atomic changes and advance a daily repair using short bounded batches."""
    import time
    if task not in tasks.TASKS:
        raise ValueError("Unsupported classification task")
    # Validate before touching markers. Bad vocabulary cannot lose source work.
    vocab = vocabulary.load()
    identity = hashlib.sha256(json.dumps({
        'digest': tasks.input_digest('', '', vocab['sha256'][:12]),
        'categories': config.FOCUS_CATEGORIES if platform == 'tori' else (),
        'municipalities': config.FOCUS_MUNICIPALITIES if platform == 'tori' else (),
        'taxonomy': db.scalar('SELECT MAX(valid_from) FROM dim_category WHERE platform=?', (platform,)),
        'pilot_catalog': product_identity.load()['sha256'],
    }, sort_keys=True, default=str).encode()).hexdigest()
    reference, scope = now(), collection.Scope(platform)
    stamp = reference.isoformat()
    limit = config.ENRICH_REPAIR_BATCH
    stats = {'queued': 0, 'recovered_leases': 0, 'changes_processed': 0, 'repair_scanned': 0,
             'cached_reused': 0, 'vocabulary_migrated': 0}
    conn = db.connect()
    try:
        with conn.transaction():
            vocabulary.remember(conn, vocab)
            repaired = conn.execute('SELECT * FROM classification_repair WHERE platform=? AND task=?', (platform, task)).fetchone()
            finished = parse_ts(repaired['completed_at']) if repaired else None
            if not repaired or repaired['identity_key'] != identity or finished and (reference-finished).total_seconds() >= 86400:
                conn.execute("INSERT INTO classification_repair (platform,task,identity_key,cursor_pk) VALUES (?,?,?,0) "
                             "ON CONFLICT(platform,task) DO UPDATE SET identity_key=excluded.identity_key,cursor_pk=0,completed_at=NULL",
                             (platform, task, identity))
            result = conn.execute("UPDATE enrichment_tasks SET state='ready',due_at=?,lease_token=NULL,lease_until=NULL, "
                                  "error_class='interrupted' WHERE task=? AND state='leased' AND lease_until<=? "
                                  "AND listing_pk IN (SELECT listing_pk FROM listings WHERE platform=?)", (stamp, task, stamp, platform))
            stats['recovered_leases'] = result.rowcount
        batches = 0
        while full or batches < config.ENRICH_REPAIR_BATCHES:
            if deadline is not None and time.monotonic() >= deadline:
                break
            with conn.transaction():
                fields = ','.join('l.'+field for field in FIELDS.split(','))
                dirty = conn.execute('SELECT '+fields+' FROM classification_changes c JOIN listings l ON l.listing_pk=c.listing_pk '
                                     'WHERE l.platform=? ORDER BY c.changed_at,l.listing_pk LIMIT ?', (platform, limit)).fetchall()
                stats['queued'] += _sync(conn, dirty, platform, task, scope, stamp, stats)
                for row in dirty:
                    conn.execute('DELETE FROM classification_changes WHERE listing_pk=?', (row['listing_pk'],))
                stats['changes_processed'] += len(dirty)
                repair = conn.execute('SELECT * FROM classification_repair WHERE platform=? AND task=?', (platform, task)).fetchone()
                rows = []
                if repair['completed_at'] is None:
                    rows = conn.execute('SELECT '+FIELDS+' FROM listings WHERE platform=? AND listing_pk>? ORDER BY listing_pk LIMIT ?',
                                        (platform, repair['cursor_pk'], limit)).fetchall()
                    stats['queued'] += _sync(conn, rows, platform, task, scope, stamp, stats)
                    stats['repair_scanned'] += len(rows)
                    conn.execute('UPDATE classification_repair SET cursor_pk=?,completed_at=? WHERE platform=? AND task=?',
                                 (rows[-1]['listing_pk'] if rows else repair['cursor_pk'], stamp if len(rows) < limit else None, platform, task))
            batches += 1
            if len(dirty) < limit and len(rows) < limit:
                break
        return stats
    finally:
        conn.close()


def claim(platform="tori", task="product_type", retry=False, scope=None, freshest=False, deadline=None, stats=None):
    conn = db.connect()
    reference = now()
    try:
        with conn.transaction():
            conditions, params = (scope or collection.Scope(platform)).conditions()
            for failed in (retry, not retry):
                # Cached prefixes must not make the runner mistake a nonempty queue for idle.
                while deadline is None or time.monotonic() < deadline:
                    ordering = "t.research_priority DESC,l.first_seen_at DESC,t.queued_at DESC,l.listing_pk DESC" if freshest and not failed else "t.due_at,t.queued_at,l.listing_pk"
                    row = conn.execute("SELECT l.*,t.input_sha256,t.task,t.failure_count FROM enrichment_tasks t "
                                   "JOIN listings l ON l.listing_pk=t.listing_pk WHERE l.platform=? AND t.task=? "
                                   "AND t.state='ready' AND t.due_at<=? AND t.failure_count"+(">0" if failed else "=0")+
                                   (" AND "+" AND ".join(conditions) if conditions else "")+
                                   " ORDER BY "+ordering+" LIMIT 1",
                                   [platform, task, reference.isoformat(), *params]).fetchone()
                    if row is None:
                        break
                    _sync(conn, [row], platform, task, scope or collection.Scope(platform), reference.isoformat(), stats)
                    current = conn.execute("SELECT state,input_sha256 FROM enrichment_tasks WHERE listing_pk=? AND task=?",
                                           (row['listing_pk'], task)).fetchone()
                    if current['state'] != 'ready':
                        continue
                    token = uuid.uuid4().hex
                    updated = conn.execute("UPDATE enrichment_tasks SET state='leased',lease_token=?,lease_until=?,last_attempt_at=? "
                                           "WHERE listing_pk=? AND task=? AND state='ready'",
                                           (token, (reference+timedelta(minutes=30)).isoformat(), reference.isoformat(), row["listing_pk"], task))
                    if updated.rowcount == 1:
                        return {**dict(row), "input_sha256": current['input_sha256'], "lease_token": token}
        return None
    finally:
        conn.close()


def finish(row, record, scope=None):
    """Store the derivation and task completion atomically, validating the lease."""
    from . import runner
    conn = db.connect()
    try:
        with conn.transaction():
            current = conn.execute("SELECT * FROM enrichment_tasks WHERE listing_pk=? AND task=?", (row["listing_pk"], row["task"])).fetchone()
            listing = conn.execute("SELECT "+FIELDS+" FROM listings WHERE listing_pk=?", (row["listing_pk"],)).fetchone()
            if not current or not listing or current["state"] != "leased" or current["lease_token"] != row["lease_token"]:
                return "lease_lost"
            if not (scope or collection.Scope(row["platform"])).matches(dict(listing)):
                conn.execute("UPDATE enrichment_tasks SET state='suspended',lease_token=NULL,lease_until=NULL WHERE listing_pk=? AND task=?", (row["listing_pk"], row["task"]))
                return "stale"
            if digest(dict(listing)) != current["input_sha256"] or record.get("input_sha256") != current["input_sha256"]:
                stamp = now().isoformat()
                conn.execute("UPDATE enrichment_tasks SET state='ready',input_sha256=?,due_at=?,queued_at=?,completed_at=NULL,failure_count=0, "
                             "lease_token=NULL,lease_until=NULL,error_class='input_changed' WHERE listing_pk=? AND task=?",
                             (digest(dict(listing)), stamp, stamp, row["listing_pk"], row["task"]))
                return "stale"
            runner.store(record, row["platform"], row["listing_id"], row["listing_pk"], conn=conn)
            from .. import collection_events
            if record.get('output_json') is not None:
                output = record['output_json']
                collection_events.attribute(conn, row['listing_pk'],
                    {**dict(listing), 'product_type': output.get('product_type'), 'confidence': record.get('confidence'),
                     'validation': record.get('validation'), 'input_sha256': record['input_sha256'], 'cached': False},
                    kind='classification', commit_key=record['input_sha256'])
                collection_events.emit('classification_completed', {'classified': 1}, conn=conn)
            failure = record.get("output_json") is None
            count = current["failure_count"]+1 if failure else 0
            error = record.get("error") or ""
            kind = "schema" if error == "schema_violation" else "provider" if error.startswith("provider_unavailable") else "classification"
            quarantine = failure and kind != "provider" and count >= (3 if kind == "schema" else 5)
            state = "quarantined" if quarantine else "ready" if failure else "complete"
            reference = now()
            due = reference+timedelta(minutes=min(1440, 10*2**min(count-1, 8))) if failure else reference
            conn.execute("UPDATE enrichment_tasks SET state=?,due_at=?,completed_at=?,failure_count=?,error_class=?, "
                         "lease_token=NULL,lease_until=NULL WHERE listing_pk=? AND task=?",
                         (state, due.isoformat(), None if failure else reference.isoformat(), count, kind if failure else None,
                          row["listing_pk"], row["task"]))
            return state
    finally:
        conn.close()


def defer(row):
    """Release a deadline-cancelled inference without charging a model failure."""
    conn = db.connect()
    try:
        with conn.transaction():
            result = conn.execute("UPDATE enrichment_tasks SET state='ready',due_at=?,lease_token=NULL,lease_until=NULL,"
                                  "error_class='runtime_budget' WHERE listing_pk=? AND task=? AND state='leased' AND lease_token=?",
                                  (now().isoformat(),row['listing_pk'],row['task'],row['lease_token']))
            return result.rowcount == 1
    finally:
        conn.close()


def report(platform="tori"):
    from .. import analysis
    conditions, params = collection.Scope(platform).conditions()
    reference = now()
    fields = {"pending": "t.state IN ('ready','leased','quarantined')", "due": "t.state='ready' AND t.due_at<=?",
              "retry": "t.state='ready' AND t.failure_count>0", "leased": "t.state='leased'",
              "quarantined": "t.state='quarantined'", "complete_cached": "t.state='complete'",
              "pilot_priority_pending": "t.research_priority>0 AND t.state IN ('ready','leased','quarantined')"}
    query = ','.join("COALESCE(SUM(CASE WHEN "+condition+" THEN 1 ELSE 0 END),0) AS "+name for name,condition in fields.items())
    row = db.query_one("SELECT "+query+",MIN(CASE WHEN t.state IN ('ready','leased','quarantined') THEN t.queued_at END) AS oldest "
                       "FROM enrichment_tasks t JOIN listings l ON l.listing_pk=t.listing_pk WHERE l.platform=? AND t.task='product_type'"+
                       (" AND "+" AND ".join(conditions) if conditions else ""), [reference.isoformat(), platform, *params])
    oldest = parse_ts(row['oldest'])
    repair = db.query_one('SELECT cursor_pk,completed_at FROM classification_repair WHERE platform=? AND task=?', (platform,'product_type'))
    if repair and repair['completed_at'] is not None:
        repair['completed_at'] = parse_ts(repair['completed_at']).isoformat()
    dirty = db.scalar('SELECT COUNT(*) FROM classification_changes c JOIN listings l ON l.listing_pk=c.listing_pk WHERE l.platform=?', (platform,))
    rows = db.query("SELECT "+FIELDS+" FROM listings l WHERE l.platform=?"+
                    (" AND "+" AND ".join(conditions) if conditions else ""), [platform, *params])
    return {"policy_version": VERSION, **{name:int(row[name]) for name in fields},
            "changes_pending": dirty, "repair": repair,
            "classification_quality": analysis.classification_quality(rows),
            "recent_throughput": throughput(platform, int(row['pending']), reference),
            "oldest_pending_hours": round(max(0,(reference-oldest).total_seconds()/3600),1) if oldest else None,
            "runtime_seconds": config.ENRICH_RUNTIME_SECONDS,
            "fresh_share": config.ENRICH_FRESH_SHARE,
            "note": "Cached completion includes abstentions and labels below acceptance gates. Accepted current labels are reported separately. Reconciliation uses committed source facts."}


def throughput(platform, pending, reference=None):
    """Bounded current-identity cycle evidence; historical incompatible runs stay separate."""
    reference = reference or now()
    identity = tasks.classification_identity()
    identity_sha256 = tasks.classification_digest(platform)
    entries = db.query("SELECT result_json FROM collection_job_runs WHERE job='enrich' AND finished_at>=? "
                       "AND finished_at<=? ORDER BY finished_at DESC LIMIT 513",
                       ((reference-timedelta(hours=24)).isoformat(), reference.isoformat()))
    matched, excluded, accepted, completed, attempted = 0, 0, 0, 0, 0
    for row in entries[:512]:
        try:
            envelope = json.loads(row['result_json'])
            result = envelope.get('stats', {})
            if result.get('classification_identity_sha256') != identity_sha256:
                excluded += 1
                continue
            counts = [result.get(key, 0) for key in ('accepted', 'classified', 'abstained', 'attempted')]
            if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
                    or counts[0] > counts[1] or counts[1]+counts[2] > counts[3]):
                excluded += 1
                continue
        except (ValueError, TypeError, AttributeError):
            excluded += 1
            continue
        matched += 1
        accepted += counts[0]
        completed += counts[1]+counts[2]
        attempted += counts[3]
    rate = completed/matched if matched else None
    return dict(window_hours=24, current_identity_cycles=matched, excluded_cycles=excluded,
                truncated=len(entries)>512, attempted=attempted, accepted=accepted, completed=completed,
                mean_usable_labels_per_cycle=round(accepted/matched, 2) if matched else None,
                mean_completed_tasks_per_cycle=round(rate, 2) if rate is not None else None,
                backlog_clearance_floor_cycles=math.ceil(pending/rate) if rate else None,
                recovery_eta_hours=None, classification_identity=identity,
                classification_identity_sha256=identity_sha256,
                note="Current-identity recorded cycles only. Backlog cycles are an optimistic lower bound without arrivals, unreconciled work, outages or review of quarantined tasks; accepted labels and completed tasks differ. Recovery ETA is unmeasured.")


def main():
    from .. import local_runtime
    delegated = local_runtime.delegate('toriresearch.enrich.queue')
    if delegated is not None:
        return delegated
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--reconcile", action="store_true")
    mode.add_argument("--retry", metavar="LISTING_ID")
    args = parser.parse_args()
    db.init_db()
    if not args.status:
        lock, error = db.acquire_lock("enrich")
        if lock is None:
            print(json.dumps({"status": "skipped", "reason": error}))
            return 1
        try:
            reconcile(full=args.reconcile)
            if args.retry:
                db.execute("UPDATE enrichment_tasks SET state='ready',failure_count=0,due_at=?,error_class=NULL "
                           "WHERE state='quarantined' AND listing_pk IN (SELECT listing_pk FROM listings WHERE platform='tori' AND listing_id=?)",
                           (now().isoformat(), args.retry))
        finally:
            db.release_lock(lock)
    print(json.dumps(report(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
