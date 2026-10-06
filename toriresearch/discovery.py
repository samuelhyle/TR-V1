"""Fair search rotation and transactional page checkpoints for one collector host."""

import math
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import collection, config, db
from .metrics import parse_ts

VERSION = "discovery-rotation-v2"


def sync_national_targets(platform='tori'):
    """Add only configured categories, without widening geographic authority."""
    if platform != 'tori':
        return 0
    from .ingest.sweep import build_search_url
    conn = db.connect()
    try:
        with conn.transaction():
            conn.execute("UPDATE sweep_targets SET active=FALSE WHERE platform=? AND grid_origin='focused-national'", (platform,))
            if not config.FOCUS_NATIONAL_DISCOVERY or config.FOCUS_MUNICIPALITIES:
                return 0
            for path in sorted(set(config.FOCUS_CATEGORIES)):
                conn.execute("INSERT INTO sweep_targets (platform,location_id,location_grain,category_path,category_depth,search_url,grid_origin) "
                             "VALUES (?,'','national',?,?,?,'focused-national') ON CONFLICT(platform,location_id,category_path) "
                             "DO UPDATE SET active=TRUE,search_url=excluded.search_url",
                             (platform,path,len(path.split('.')),build_search_url('',path)))
        return len(set(config.FOCUS_CATEGORIES))
    finally:
        conn.close()


def now():
    return datetime.now(timezone.utc)


def state(platform="tori", conn=None):
    owns = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute("SELECT * FROM discovery_state WHERE platform=?", (platform,)).fetchone()
        return dict(row) if row else {"platform": platform, "dispatch_count": 0}
    finally:
        if owns:
            conn.close()


def tick(conn, platform):
    conn.execute("INSERT INTO discovery_state (platform, dispatch_count) VALUES (?,1) "
                 "ON CONFLICT(platform) DO UPDATE SET dispatch_count=discovery_state.dispatch_count+1", (platform,))


def targets(platform="tori", only_municipality=True):
    conditions, params = collection.Scope(platform).conditions(grid=True)
    conditions += ["platform=?", "active=TRUE"]
    params.append(platform)
    if only_municipality:
        conditions.append("location_grain IN ('municipality','national')" if config.FOCUS_NATIONAL_DISCOVERY and not config.FOCUS_MUNICIPALITIES
                          else "location_grain='municipality'")
    return db.query("SELECT * FROM sweep_targets WHERE " + " AND ".join(conditions), params)


def _vertical(row):
    key = collection.category_key(row["category_path"])
    return next((path for path in sorted(config.FOCUS_CATEGORIES)
                 if collection.is_below(key, collection.category_key(path))), key.split(".")[0])


def _front(row):
    return parse_ts(row.get("last_page1_at") or row.get("last_crawled_at"))


def _due(row, reference):
    due = parse_ts(row.get("next_due_at"))
    return due is None or due <= reference


def _order(row, reference, deepest_first):
    # Pending failures need backoff, but they also rotate after an attempt.
    stamp = parse_ts(row.get("last_attempt_at")) or _front(row)
    return (stamp or datetime.min.replace(tzinfo=timezone.utc),
            -(row.get("category_depth") or 0) if deepest_first else row.get("category_depth") or 0,
            row["target_pk"])


def choose(rows, count=0, reference=None, deepest_first=False):
    """Alternate a fair rotation slot and a yield/continuation slot.

    Fair slots rotate verticals and municipalities using persistent dispatch
    times. Within each group the oldest attempted target wins. Utility slots
    favor pending pages and productive searches; allowed parents get a small
    breadth bonus. No search expands beyond configured scope.
    """
    reference = reference or now()
    due = [r for r in rows if _due(r, reference)]
    if not due:
        return None
    national = [r for r in due if r.get('location_grain') == 'national']
    local = [r for r in rows if r.get('location_grain') != 'national']
    if national:
        reserved = math.floor((count+1)*config.SWEEP_NATIONAL_SHARE) > math.floor(count*config.SWEEP_NATIONAL_SHARE)
        if reserved or not any(_due(r,reference) for r in local):
            return min(national,key=lambda r:_order(r,reference,deepest_first))
    if not local:
        return min(due,key=lambda r:_order(r,reference,deepest_first))
    rows = local
    due = [r for r in due if r.get('location_grain') != 'national']
    if count % 2 == 0:
        verticals, municipalities = defaultdict(list), defaultdict(list)
        for row in rows:
            vertical = _vertical(row)
            verticals[vertical].append(parse_ts(row.get("last_attempt_at")))
            municipalities[(vertical, row["location_id"])].append(parse_ts(row.get("last_attempt_at")))
        floor = datetime.min.replace(tzinfo=timezone.utc)
        def served(values):
            return max((v for v in values if v), default=floor)
        vertical = min({_vertical(r) for r in due}, key=lambda v: (served(verticals[v]), v))
        options = [r for r in due if _vertical(r) == vertical]
        place = min({r["location_id"] for r in options},
                    key=lambda p: (served(municipalities[(vertical, p)]), p))
        return min((r for r in options if r["location_id"] == place), key=lambda r: _order(r, reference, deepest_first))
    # Compute descendant counts in O(targets x path depth), not a pairwise scan.
    homes, descendants = {}, defaultdict(int)
    for row in rows:
        homes[(row["location_id"], collection.category_key(row["category_path"]))] = row["target_pk"]
    for (_place, key), _pk in homes.items():
        parts = key.split(".")
        for length in range(1, len(parts)):
            parent = homes.get((_place, ".".join(parts[:length])))
            if parent is not None:
                descendants[parent] += 1
    def utility(row):
        pending = row.get("next_page", 1) > 1
        broad = math.log2(1+descendants[row["target_pk"]])
        pages = row.get("traversal_pages", 0)
        yield_score = row.get("traversal_new", 0)/pages if pages else 5
        return (0 if pending else 1,
                _order(row, reference, deepest_first) if pending else (datetime.min.replace(tzinfo=timezone.utc), 0, 0),
                -(yield_score+broad), _order(row, reference, deepest_first))
    return min(due, key=utility)


def prepare(target, depth, reference=None):
    """Claim a dispatch locally; a killed process leaves its same page pending."""
    reference = (reference or now()).astimezone(timezone.utc)
    conn = db.connect()
    try:
        with conn.transaction():
            row = dict(conn.execute("SELECT * FROM sweep_targets WHERE target_pk=?", (target["target_pk"],)).fetchone())
            started = parse_ts(row.get("traversal_started_at"))
            expired = started and reference-started >= timedelta(hours=config.SWEEP_TRAVERSAL_MAX_AGE_HOURS)
            restart = (not row.get("traversal_id") or row.get("traversal_depth") != depth or expired
                       or row.get("next_page", 1) == 1 and row.get("traversal_pages", 0) > 0)
            abandoned = bool(restart and row.get("next_page", 1) > 1)
            if restart:
                conn.execute("UPDATE sweep_targets SET traversal_id=?, traversal_started_at=?, traversal_depth=?, "
                             "next_page=1, traversal_pages=0, traversal_seen=0, traversal_new=0 WHERE target_pk=?",
                             (uuid.uuid4().hex, reference.isoformat(), depth, row["target_pk"]))
            conn.execute("UPDATE sweep_targets SET last_attempt_at=? WHERE target_pk=?", (reference.isoformat(), row["target_pk"]))
            tick(conn, row["platform"])
            row = dict(conn.execute("SELECT * FROM sweep_targets WHERE target_pk=?", (row["target_pk"],)).fetchone())
            row["abandoned_traversal"] = abandoned
            return row
    finally:
        conn.close()


def commit(conn, target, observed, stats, parser, empty=False):
    """Advance exactly one page inside the caller's observation transaction."""
    page = target["next_page"]
    unique = stats["seen"]-stats.get("page_overlap", 0)
    final = empty or page >= target["traversal_depth"]
    reason = "observed_empty_end" if empty else "depth_cap" if final else None
    stamp = parse_ts(observed).astimezone(timezone.utc)
    streak = (target["empty_streak"]+1 if empty else 0) if page == 1 else target["empty_streak"]
    interval = min(config.SWEEP_MAX_REFRESH_HOURS, config.SWEEP_REFRESH_HOURS*(2 if streak >= 2 else 1))
    if target["traversal_seen"]+unique >= 5 and target["traversal_new"]+stats["new"] > 0:
        interval = config.SWEEP_PRODUCTIVE_REFRESH_HOURS
    first = stamp if page == 1 else _front(target) or stamp
    due = first+timedelta(hours=interval) if final else stamp
    if target.get('location_grain') == 'national' and final:
        seconds = config.COLLECTION_INTERVAL_SECONDS
        due = datetime.fromtimestamp((math.floor(stamp.timestamp()/seconds)+(2 if streak >= 2 else 1))*seconds, timezone.utc)
    inserted = conn.execute("INSERT INTO sweep_page_commits (target_pk, traversal_id, page_number, observed_at, "
                            "seen, new, overlap, parser, end_reason) VALUES (?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(target_pk,traversal_id,page_number) DO NOTHING",
                            (target["target_pk"], target["traversal_id"], page, observed,
                             stats["seen"], stats["new"], stats.get("page_overlap", 0), parser, reason))
    if inserted.rowcount != 1:
        raise RuntimeError("Search page was already committed")
    updated = conn.execute("UPDATE sweep_targets SET next_page=?, next_due_at=?, "
                           "traversal_pages=traversal_pages+1, traversal_seen=traversal_seen+?, traversal_new=traversal_new+?, "
                           "last_page1_at=CASE WHEN ? THEN ? ELSE last_page1_at END, first_crawled_at=COALESCE(first_crawled_at,?), "
                           "last_crawled_at=CASE WHEN ? THEN ? ELSE last_crawled_at END, "
                           "crawl_count=crawl_count+?, last_listing_count=traversal_seen+?, last_status=?, coverage_state=?, "
                           "last_page_new=?, last_page_seen=?, error_count=0, http404_count=0, empty_streak=? "
                           "WHERE target_pk=? AND traversal_id=? AND next_page=?",
                           (1 if final else page+1, due.isoformat(), unique, stats["new"],
                            page == 1, observed, observed, final, observed, int(final), unique,
                            "ok" if final else "partial", "observed_empty_end" if empty else "capped" if final else "partial",
                            stats["new"], stats["seen"], streak, target["target_pk"], target["traversal_id"], page))
    if updated.rowcount != 1:
        raise RuntimeError("Search checkpoint changed before page commit")
    return {"finished": final, "capped": final and not empty, "end_reason": reason}


def fail(target, error_class, reference=None):
    reference = reference or now()
    count = target["error_count"]+(error_class != "platform_stop")
    delay = min(24, 2**max(0, count-1))
    misses = target["http404_count"]+1 if error_class == "http404" and target["next_page"] == 1 else 0
    db.execute("UPDATE sweep_targets SET last_status=?, error_count=?, http404_count=?, next_due_at=?, "
               "active=CASE WHEN ? THEN FALSE ELSE active END WHERE target_pk=? AND traversal_id=?",
               ("retired_404" if misses >= 2 else error_class, count, misses,
                (reference+timedelta(hours=delay)).isoformat(), misses >= 2, target["target_pk"], target["traversal_id"]))


def report(platform="tori", reference=None):
    reference = reference or now()
    rows = targets(platform)
    count = len(rows)
    per_hour = max(0, config.SWEEP_BUDGET)*3600/config.COLLECTION_INTERVAL_SECONDS
    # A tenth of dispatch slots is available for metadata while refresh is due.
    national = [r for r in rows if r.get('location_grain') == 'national']
    local = [r for r in rows if r.get('location_grain') != 'national']
    national_required = len(national)*3600/config.COLLECTION_INTERVAL_SECONDS
    national_reserved = min(per_hour*config.SWEEP_NATIONAL_SHARE, national_required)
    rotation = max(0,per_hour*0.9-national_reserved)*0.5
    required = len(local)/config.SWEEP_REFRESH_HOURS
    ages = [(reference-_front(r)).total_seconds()/3600 for r in rows if _front(r)]
    pending = sum(r.get("next_page", 1) > 1 for r in rows)
    nation_ages = [(reference-_front(r)).total_seconds()/60 for r in national if _front(r)]
    return {"policy_version": VERSION, "active_targets": count, "continuations": pending,
            "capped_targets": sum(r.get("coverage_state") == "capped" for r in rows),
            "observed_empty_ends": sum(r.get("coverage_state") == "observed_empty_end" for r in rows),
            "first_page_unmeasured": sum(not _front(r) for r in rows),
            "first_page_overdue": sum(age >= config.SWEEP_MAX_REFRESH_HOURS for age in ages),
            "oldest_first_page_age_hours": round(max(ages), 1) if ages else None,
            "desired_refresh_hours": config.SWEEP_REFRESH_HOURS, "maximum_refresh_hours": config.SWEEP_MAX_REFRESH_HOURS,
            "configured_requests_per_hour": per_hour, "minimum_requests_per_hour": round(required, 2),
            "reserved_rotation_requests_per_hour": round(rotation, 2),
            "rotation_lower_bound_hours": round(len(local)/rotation, 1) if rotation else None,
            "desired_interval_feasible_at_reserved_share": required <= rotation,
            "national_focus": {'enabled':config.FOCUS_NATIONAL_DISCOVERY,'targets':len(national),
                               'requested_targets':len(set(config.FOCUS_CATEGORIES)) if not config.FOCUS_MUNICIPALITIES else 0,
                               'observed_targets':len(nation_ages),'overdue_targets':sum(_due(r,reference) for r in national),
                               'oldest_front_page_age_minutes':round(max(nation_ages),1) if nation_ages else None,
                               'refresh_seconds':config.COLLECTION_INTERVAL_SECONDS,'request_share':config.SWEEP_NATIONAL_SHARE,
                               'minimum_requests_per_hour':national_required,
                               'reserved_requests_per_hour':round(national_reserved,2),
                               'interval_feasible_at_reserved_share':national_required<=per_hour*config.SWEEP_NATIONAL_SHARE*0.9},
            "scope_note": "Parent search membership is separate from item-confirmed leaf categories. Only configured paths and places are eligible.",
            "coverage_note": "A depth cap is partial inventory coverage. An observed empty end describes one moving traversal; it cannot prove an exhaustive market census. Rotation guarantees depend on sufficient budget and host availability."}
