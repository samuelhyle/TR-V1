"""Bounded listing-analysis tasks using the existing durable queue and journal."""

import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta

from .. import collection, config, db, listing_analysis as analysis
from . import provider


def reconcile(platform="tori", limit=500):
    analysis.purge()
    conn = db.connect()
    scanned = 0
    try:
        with conn.transaction():
            conn.execute("UPDATE enrichment_tasks SET state='ready',lease_token=NULL,lease_until=NULL "
                         "WHERE task=? AND state='leased' AND lease_until<=?",(analysis.TASK,analysis.now().isoformat()))
            current_identity = analysis._hash(analysis.identity())
            cursor = conn.execute("SELECT * FROM classification_repair WHERE platform=? AND task=?",(platform,analysis.TASK)).fetchone()
            if (cursor and cursor["identity_key"]==current_identity and cursor["completed_at"]
                    and analysis.metrics.parse_ts(cursor["completed_at"])+timedelta(hours=24)>analysis.now()):
                return 0
            # Do not turn the legacy archive into thousands of expiring capsules.
            # New observations enqueue directly and bypass this bounded backfill.
            ready = conn.execute("SELECT COUNT(*) AS n FROM enrichment_tasks t JOIN listings l ON l.listing_pk=t.listing_pk "
                                 "WHERE t.task=? AND l.platform=? AND t.state IN ('ready','leased')",(analysis.TASK,platform)).fetchone()["n"]
            if ready>=max(100,config.ENRICH_BUDGET*10):
                return 0
            after = cursor["cursor_pk"] if cursor and not cursor["completed_at"] and cursor["identity_key"]==current_identity else 0
            rows = conn.execute("SELECT * FROM listings WHERE platform=? AND listing_pk>? ORDER BY listing_pk LIMIT ?",(platform,after,limit)).fetchall()
            scope = collection.Scope(platform)
            for raw in rows:
                row = dict(raw)
                if not scope.matches(row) or not row["title"]:
                    continue
                if not analysis.sources_for(conn,row["listing_pk"]):
                    analysis.capture(conn,row,{},row["last_seen_at"],"legacy_title")
                analysis.sync_task(conn,row)
                scanned += 1
            conn.execute("INSERT INTO classification_repair (platform,task,identity_key,cursor_pk,completed_at) "
                         "VALUES (?,?,?,?,?) ON CONFLICT(platform,task) DO UPDATE SET cursor_pk=excluded.cursor_pk,"
                         "identity_key=excluded.identity_key,completed_at=excluded.completed_at",
                         (platform,analysis.TASK,current_identity,rows[-1]["listing_pk"] if rows else after,
                          analysis.now().isoformat() if len(rows)<limit else None))
    finally:
        conn.close()
    return scanned


def next_lane(platform):
    """Continue round-robin scheduling across short worker runs and restarts."""
    lanes = config.FOCUS_CATEGORIES if platform=="tori" else ()
    key = analysis._hash(list(lanes))
    conn = db.connect()
    try:
        with conn.transaction():
            prior = conn.execute("SELECT * FROM classification_repair WHERE platform=? AND task=?",
                                 (platform,"listing_analysis_dispatch")).fetchone()
            index = prior["cursor_pk"] if prior and prior["identity_key"]==key else 0
            conn.execute("INSERT INTO classification_repair (platform,task,identity_key,cursor_pk) VALUES (?,?,?,?) "
                         "ON CONFLICT(platform,task) DO UPDATE SET identity_key=excluded.identity_key,cursor_pk=excluded.cursor_pk",
                         (platform,"listing_analysis_dispatch",key,(index+1)%max(4,len(lanes)*4)))
            return (lanes[index%len(lanes)] if lanes else None),index%4==0
    finally:
        conn.close()


def claim(platform, lane=None, freshest=False, rich_first=True):
    conn = db.connect()
    reference = analysis.now()
    try:
        with conn.transaction():
            conditions, values = collection.Scope(platform,categories=(lane,) if lane else None).conditions()
            rows = conn.execute("SELECT l.*,t.input_sha256,t.task,t.failure_count FROM enrichment_tasks t JOIN listings l ON l.listing_pk=t.listing_pk "
                "WHERE l.platform=? AND t.task=? AND t.state='ready' AND t.due_at<=?"+
                (" AND "+" AND ".join(conditions) if conditions else "")+
                " ORDER BY t.failure_count,"+("t.research_priority DESC," if rich_first else "")+"t.queued_at"+
                (" DESC" if freshest else "")+",l.listing_pk LIMIT 50",[platform,analysis.TASK,reference.isoformat(),*values]).fetchall()
            for raw in rows:
                row = dict(raw)
                analysis.sync_task(conn,row)
                task = conn.execute("SELECT * FROM enrichment_tasks WHERE listing_pk=? AND task=?",(row["listing_pk"],analysis.TASK)).fetchone()
                if task["state"] != "ready":
                    continue
                sources = analysis.sources_for(conn,row["listing_pk"])
                capsule = analysis.capsule_for(sources)
                if not capsule["fields"]:
                    continue
                token = uuid.uuid4().hex
                updated = conn.execute("UPDATE enrichment_tasks SET state='leased',lease_token=?,lease_until=?,last_attempt_at=? "
                    "WHERE listing_pk=? AND task=? AND state='ready'",(token,(reference+timedelta(minutes=30)).isoformat(),reference.isoformat(),row["listing_pk"],analysis.TASK))
                if updated.rowcount:
                    return dict(row,input_sha256=task["input_sha256"],lease_token=token,sources=sources,capsule=capsule)
    finally:
        conn.close()
    return None


def finish(row, record):
    from . import runner
    conn = db.connect()
    try:
        with conn.transaction():
            task = conn.execute("SELECT * FROM enrichment_tasks WHERE listing_pk=? AND task=?",(row["listing_pk"],analysis.TASK)).fetchone()
            current = conn.execute("SELECT * FROM listings WHERE listing_pk=?",(row["listing_pk"],)).fetchone()
            if not task or not current or task["state"]!="leased" or task["lease_token"]!=row["lease_token"]:
                return "lease_lost"
            sources = analysis.sources_for(conn,row["listing_pk"])
            digest = analysis.input_digest(dict(current),sources)
            if (not collection.Scope(row["platform"]).matches(dict(current)) or digest!=record["input_sha256"]
                    or digest!=task["input_sha256"]):
                analysis.sync_task(conn,dict(current))
                return "stale"
            runner.store(record,row["platform"],row["listing_id"],row["listing_pk"],conn=conn)
            failure = record.get("output_json") is None
            count = task["failure_count"]+1 if failure else 0
            state = "quarantined" if failure and count>=3 else "ready" if failure else "complete"
            stamp = analysis.now()
            conn.execute("UPDATE enrichment_tasks SET state=?,failure_count=?,error_class=?,due_at=?,completed_at=?,"
                         "lease_token=NULL,lease_until=NULL WHERE listing_pk=? AND task=?",
                         (state,count,record.get("error_code",record.get("error")),(stamp+timedelta(minutes=min(1440,10*2**min(count,7)))).isoformat(),
                          None if failure else stamp.isoformat(),row["listing_pk"],analysis.TASK))
            if not failure:
                # Persist a classification projection without a second model call.
                from . import tasks, vocabulary
                vocab = vocabulary.load()
                result = record["output_json"]
                label = result["product_type"] if result["product_type"] in vocab["allowed"] else vocab["abstain"]
                home = vocab["homes"].get(label)
                output = {"product_type":label,"agrees_with_platform":"yes" if home and tasks._same_home(home,tasks._root_of(current["category_norm"]) or "") else "unclear"}
                confidence,basis,output = tasks.derive_confidence(output,True,current["category_norm"])
                projection = dict(task="product_type",input_sha256=tasks.input_digest(current["title"],current["category_norm"],vocab["sha256"][:12]),
                    prompt_version=tasks.PROMPT_VERSION,schema_version=vocab["sha256"][:12],model=record["model"],provider="llama.cpp",
                    output_json=output,confidence=confidence,confidence_basis=basis+",listing_analysis_projection",
                    validation="enum_validated,home="+output.get("home_check","?")+",listing_analysis_projection",latency_ms=0)
                runner.store(projection,row["platform"],row["listing_id"],row["listing_pk"],conn=conn)
                from .. import collection_events
                collection_events.emit("classification_completed",{"classified":1},conn=conn)
                # Keep revision hashes; remove the now-unneeded prose from local storage.
                conn.execute("UPDATE listing_analysis_inputs SET input_json=NULL WHERE listing_pk=?",(row["listing_pk"],))
            return state
    finally:
        conn.close()


def report(platform="tori"):
    values = db.query("SELECT t.state,COUNT(*) AS n,MIN(t.queued_at) AS oldest FROM enrichment_tasks t JOIN listings l "
                      "ON l.listing_pk=t.listing_pk WHERE t.task=? AND l.platform=? GROUP BY t.state",(analysis.TASK,platform))
    reference = analysis.now()
    recent = db.query("SELECT validation,COUNT(*) AS n,AVG(latency_ms) AS mean_latency_ms FROM enrichment "
                      "WHERE task=? AND platform=? AND observed_at>=? GROUP BY validation",
                      (analysis.TASK,platform,(reference-timedelta(hours=1)).isoformat()))
    lanes = db.query("SELECT l.category_norm,t.state,COUNT(*) AS n,MIN(t.queued_at) AS oldest "
                     "FROM enrichment_tasks t JOIN listings l ON l.listing_pk=t.listing_pk "
                     "WHERE t.task=? AND l.platform=? GROUP BY l.category_norm,t.state",(analysis.TASK,platform))
    for item in lanes:
        item["oldest_age_seconds"] = max(0,round((reference-analysis.metrics.parse_ts(item.pop("oldest"))).total_seconds()))
    errors = db.query("SELECT t.error_class,COUNT(*) AS n FROM enrichment_tasks t JOIN listings l "
                      "ON l.listing_pk=t.listing_pk WHERE t.task=? AND l.platform=? AND t.error_class IS NOT NULL "
                      "GROUP BY t.error_class",(analysis.TASK,platform))
    return dict(task=analysis.TASK,states={item["state"]:item["n"] for item in values},lanes=lanes,
                oldest_ready_age_seconds=next((max(0,round((reference-analysis.metrics.parse_ts(item["oldest"])).total_seconds()))
                                              for item in values if item["state"]=="ready"),None),
                recent_results=dict(window_minutes=60,distinct_listing_revisions=recent,
                                    note="Latest journal record per revision; retries may overwrite failures. This is throughput, not semantic accuracy."),
                errors=errors,
                source_wait=db.scalar("SELECT COUNT(*) FROM enrichment_tasks t JOIN listings l ON l.listing_pk=t.listing_pk "
                    "WHERE t.task=? AND l.platform=? AND t.error_class='source_expired'",(analysis.TASK,platform)) or 0,
                semantic_accuracy_probability=None,identity=analysis.identity())


def run(platform="tori",budget=None,concurrency=None,dry_run=False,runtime_seconds=None):
    """Bound dispatch time; finish in-flight calls within their provider timeout."""
    budget = config.ENRICH_BUDGET if budget is None else int(budget)
    concurrency = config.LLM_CONCURRENCY if concurrency is None else int(concurrency)
    if not 1 <= budget <= 10000 or not 1 <= concurrency <= 32:
        raise ValueError("listing analysis needs positive bounded budgets")
    if dry_run:
        return dict(status="dry_run",requested=budget,queue=report(platform))
    if platform=="tori":
        from .. import permissions
        permissions.require("tori","enrichment",category_paths=list(config.FOCUS_CATEGORIES),municipalities=list(config.FOCUS_MUNICIPALITIES))
    lock,error = db.acquire_lock("enrich")
    if lock is None:
        return dict(status="skipped",reason=error)
    started = time.monotonic()
    dispatch_window = config.ENRICH_RUNTIME_SECONDS if runtime_seconds is None else runtime_seconds
    deadline = started+dispatch_window
    stats = dict(task=analysis.TASK,requested=budget,attempted=0,classified=0,accepted=0,abstained=0,
                 errors=0,stale_results=0,model_requests=0,skipped_no_provider=0,runtime_limited=0,
                 dispatch_window_seconds=dispatch_window,request_timeout_seconds=config.LLM_TIMEOUT)
    try:
        stats["reconciled"] = reconcile(platform)
        if not provider.health()["reachable"]:
            stats["skipped_no_provider"] = 1
            return stats
        def work(row):
            return analysis.analyze(row,row["capsule"],row["sources"],timeout=config.LLM_TIMEOUT)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {}
            dispatched = 0
            exhausted = False
            while futures or dispatched<budget and not exhausted and time.monotonic()<deadline:
                while len(futures)<concurrency and dispatched<budget and not exhausted and time.monotonic()<deadline:
                    from .. import collection_control
                    collection_control.checkpoint(source=False)
                    lane, oldest_first = next_lane(platform)
                    row = claim(platform,lane=lane,rich_first=not oldest_first)
                    if row is None and lane:
                        row = claim(platform,rich_first=not oldest_first)
                    if not row:
                        exhausted = True
                        break
                    futures[pool.submit(work,row)] = row
                    dispatched += 1
                    stats["model_requests"] += 1
                if not futures:
                    break
                done,_ = wait(futures,return_when=FIRST_COMPLETED)
                for future in done:
                    row = futures.pop(future)
                    stats["attempted"] += 1
                    try:
                        record = future.result()
                    except Exception:
                        record = dict(task=analysis.TASK,input_sha256=row["input_sha256"],output_json=None,error="inference_failure")
                    state = finish(row,record)
                    if state in {"stale","lease_lost"}:
                        stats["stale_results"] += 1
                    elif record.get("output_json"):
                        stats["classified"] += 1
                        from . import vocabulary
                        stats["abstained" if record["output_json"]["product_type"]==vocabulary.load()["abstain"] else "accepted"] += 1
                    else:
                        stats["errors"] += 1
                        if record.get("error")=="provider_unavailable":
                            exhausted = True
                            stats["skipped_no_provider"] = 1
        stats["runtime_limited"] = int(time.monotonic()>=deadline)
        return stats
    finally:
        stats["queue"] = report(platform)
        stats["elapsed_s"] = round(time.monotonic()-started,3)
        db.release_lock(lock)
