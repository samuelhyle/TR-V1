"""Weekly sitemap reconciliation, one request per durable frontier step."""

import hashlib
import json
import uuid
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse

from . import collection, config, db, discovery
from .http import AccessBlocked, AuthorityBlocked, looks_like_blocked_page, mark_collection_stop
from .metrics import parse_ts

INDEX = "https://www.tori.fi/sitemap.xml"


def due(reference=None):
    reference = reference or discovery.now()
    value = discovery.state()
    retry = parse_ts(value.get("retry_at"))
    if retry and retry > reference:
        return False
    last = parse_ts(value.get("last_refresh_at"))
    return bool(value.get("refresh_id")) or not last or reference-last >= timedelta(days=7)


def _document(body):
    root = ET.fromstring(body)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError("Unrecognized sitemap root")
    urls = [node.text.strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "loc" and node.text]
    if kind == "sitemapindex" and not urls:
        raise ValueError("Empty sitemap index is not a complete grid")
    return kind, urls


def _approved_sitemap(url):
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in ("www.tori.fi", "tori.fi")
            and not parsed.username and parsed.port in (None, 443) and parsed.path.startswith("/sitemap"))


def _cell(url):
    parsed = urlparse(unquote(url))
    if parsed.scheme != "https" or parsed.hostname not in ("www.tori.fi", "tori.fi") or parsed.path != "/recommerce/forsale/search":
        return None
    params = parse_qs(parsed.query)
    category = next((params[k][0] for k in ("product_category", "sub_category", "category") if params.get(k)), None)
    if not category or not all(part.isdigit() for part in category.split(".")):
        return None
    return (params.get("location", [""])[0], category)


def _in_scope(location, category, labels):
    return ((not config.FOCUS_MUNICIPALITIES or labels.get(location) in config.FOCUS_MUNICIPALITIES)
            and (not config.FOCUS_CATEGORIES or any(collection.is_below(collection.category_key(category), collection.category_key(parent))
                                                    for parent in config.FOCUS_CATEGORIES)))


def _finish(conn, value, reference):
    from .ingest.sweep import build_search_url, _load_location_labels
    cells = [tuple(row) for row in conn.execute("SELECT location_id,category_path FROM sitemap_cells "
                                              "WHERE refresh_id=? ORDER BY location_id,category_path", (value["refresh_id"],)).fetchall()]
    if not any(location for location, _category in cells):
        raise ValueError("No search cells in the completed sitemap set; retirement requires review")
    version = "sitemap-"+hashlib.sha256(json.dumps(cells).encode()).hexdigest()[:20]
    labels = _load_location_labels()
    limit = value.get("refresh_location_limit")
    allowed_places = set(sorted({location for location, _category in cells if location})[:limit]) if limit else None
    stamp = reference.isoformat()
    added = 0
    for location, category in cells:
        if not location or allowed_places is not None and location not in allowed_places or not _in_scope(location, category, labels):
            continue
        inserted = conn.execute("INSERT INTO sweep_targets (platform,location_id,location_label,location_grain,category_path, "
                                "category_depth,search_url,grid_origin,grid_version) VALUES ('tori',?,?,?,?,?,?,'sitemap',?) "
                                "ON CONFLICT(platform,location_id,category_path) DO UPDATE SET "
                                "search_url=excluded.search_url,location_label=COALESCE(excluded.location_label,sweep_targets.location_label), "
                                "grid_version=excluded.grid_version,missing_snapshots=0,active=TRUE",
                                (location, labels.get(location), "municipality" if location.startswith("1.") else "region",
                                 category, category.count(".")+1, build_search_url(location, category), version))
        added += inserted.rowcount
    # Preserve the existing synthetic top-level layer. It is not an omitted
    # sitemap cell and must never be retired because it is absent from the XML.
    roots = {category for _location, category in cells if category.startswith("0.")}
    for location, label in labels.items():
        if not location.startswith("1.") or allowed_places is not None and location not in allowed_places:
            continue
        for category in roots:
            if _in_scope(location, category, labels):
                conn.execute("INSERT INTO sweep_targets (platform,location_id,location_label,location_grain,category_path,category_depth, "
                             "search_url,grid_origin,grid_version) VALUES ('tori',?,?,'municipality',?,2,?,'synthesized',?) "
                             "ON CONFLICT(platform,location_id,category_path) DO NOTHING",
                             (location, label, category, build_search_url(location, category), version))
    # Legacy child targets came from the published sitemap. Two complete
    # snapshots must agree on absence before one is retired.
    conn.execute("UPDATE sweep_targets SET grid_origin='sitemap' WHERE platform='tori' AND grid_origin IS NULL "
                 "AND (category_path LIKE '1.%' OR category_path LIKE '2.%')")
    conn.execute("UPDATE sweep_targets SET missing_snapshots=0, grid_version=? WHERE platform='tori' AND grid_origin='sitemap' "
                 "AND EXISTS (SELECT 1 FROM sitemap_cells c WHERE c.refresh_id=? "
                 "AND c.location_id=sweep_targets.location_id AND c.category_path=sweep_targets.category_path)",
                 (version, value["refresh_id"]))
    conn.execute("UPDATE sweep_targets SET missing_snapshots=missing_snapshots+1, "
                 "active=CASE WHEN missing_snapshots>=1 THEN FALSE ELSE active END, "
                 "last_status=CASE WHEN missing_snapshots>=1 THEN 'retired_sitemap' ELSE last_status END "
                 "WHERE platform='tori' AND grid_origin='sitemap' AND NOT EXISTS "
                 "(SELECT 1 FROM sitemap_cells c WHERE c.refresh_id=? AND c.location_id=sweep_targets.location_id "
                 "AND c.category_path=sweep_targets.category_path)", (value["refresh_id"],))
    categories = {category for _location, category in cells}
    for category in sorted(categories):
        prior = conn.execute("SELECT platform_label,label_source FROM dim_category WHERE platform='tori' "
                             "AND platform_category_id=? AND platform_label IS NOT NULL ORDER BY valid_from DESC, category_version_pk DESC LIMIT 1",
                             (category,)).fetchone()
        conn.execute("INSERT INTO dim_category (platform,platform_category_id,platform_category_path,taxonomy_version, "
                     "platform_label,label_source,valid_from) VALUES ('tori',?,?,?,?,?,?) ON CONFLICT "
                     "(platform,platform_category_id,taxonomy_version) DO NOTHING",
                     (category, category, version, prior[0] if prior else None, prior[1] if prior else None, stamp))
    conn.execute("DELETE FROM sitemap_cells WHERE refresh_id=?", (value["refresh_id"],))
    conn.execute("UPDATE discovery_state SET refresh_id=NULL, frontier_json=NULL, visited_json=NULL, refresh_started_at=NULL, refresh_location_limit=NULL, "
                 "last_refresh_at=?,last_refresh_version=?,retry_at=NULL,last_error_class=NULL WHERE platform='tori'", (stamp, version))
    return {"completed": True, "version": version, "reconciled_in_scope": added}


def step(session, reference=None, limit_locations=None):
    """One HTTP attempt including retry/backoff and atomic frontier advancement."""
    reference = (reference or discovery.now()).astimezone(timezone.utc)
    value = discovery.state()
    limit = value.get("refresh_location_limit") if value.get("refresh_id") else limit_locations
    refresh_id = value.get("refresh_id") or uuid.uuid4().hex
    frontier = json.loads(value.get("frontier_json") or "[]") or [INDEX]
    visited = set(json.loads(value.get("visited_json") or "[]"))
    url = frontier[0]
    conn = db.connect()
    try:
        with conn.transaction():
            discovery.tick(conn, "tori")
            conn.execute("UPDATE discovery_state SET refresh_id=?,refresh_started_at=COALESCE(refresh_started_at,?), "
                         "frontier_json=?,refresh_location_limit=? WHERE platform='tori'", (refresh_id, reference.isoformat(), json.dumps(frontier), limit))
        if not _approved_sitemap(url):
            raise ValueError("Sitemap frontier leaves approved metadata paths")
        body = session.get_text(url, max_attempts=1, retry_on_429=False)
        if looks_like_blocked_page(body):
            mark_collection_stop(url, "blocked")
            raise AccessBlocked("Sitemap refusal page")
        kind, urls = _document(body)
        remaining = frontier[1:]
        with conn.transaction():
            if kind == "sitemapindex":
                if not all(_approved_sitemap(child) for child in urls):
                    raise ValueError("Sitemap index contains an unapproved metadata URL")
                # Reject cycles instead of exhausting the source budget on an
                # index that keeps referring back to itself.
                if url in urls or visited.intersection(urls) or INDEX in urls or len(visited)+len(urls) > 1000:
                    raise ValueError("Cyclic sitemap index")
                remaining = list(dict.fromkeys([*remaining, *urls]))
            else:
                for candidate in urls:
                    cell = _cell(candidate)
                    if cell:
                        conn.execute("INSERT INTO sitemap_cells (refresh_id,location_id,category_path) VALUES (?,?,?) "
                                     "ON CONFLICT(refresh_id,location_id,category_path) DO NOTHING", (refresh_id, *cell))
            visited.add(url)
            conn.execute("UPDATE discovery_state SET frontier_json=?,visited_json=?,retry_at=NULL,last_error_class=NULL WHERE platform='tori'",
                         (json.dumps(remaining), json.dumps(sorted(visited))))
            if not remaining:
                return {"status": "ok", **_finish(conn, {"refresh_id": refresh_id, "refresh_location_limit": limit}, reference)}
        return {"status": "ok", "completed": False, "documents_pending": len(remaining)}
    except (AuthorityBlocked, AccessBlocked):
        # Authority failures do not manufacture a platform refusal. The HTTP
        # adapter records source-specific stops when the platform refused.
        conn.execute("UPDATE discovery_state SET retry_at=?,last_error_class='platform_stop' WHERE platform='tori'",
                     ((reference+timedelta(hours=1)).isoformat(),))
        return {"status": "blocked", "completed": False}
    except HTTPError as exc:
        status = "throttled" if exc.code == 429 else "blocked" if exc.code == 403 else "error"
        if exc.code in (403, 429):
            mark_collection_stop(url, "throttled" if exc.code == 429 else "blocked")
        conn.execute("UPDATE discovery_state SET retry_at=?,last_error_class=? WHERE platform='tori'",
                     ((reference+timedelta(hours=1)).isoformat(), "http"))
        return {"status": status, "completed": False}
    except Exception as exc:
        conn.execute("UPDATE discovery_state SET retry_at=?,last_error_class=? WHERE platform='tori'",
                     ((reference+timedelta(hours=24)).isoformat(), "parse" if isinstance(exc, (ValueError, ET.ParseError)) else "network_or_storage"))
        return {"status": "error", "completed": False}
    finally:
        conn.close()


def report():
    value = discovery.state()
    frontier = json.loads(value.get("frontier_json") or "[]")
    def stamp(key):
        parsed = parse_ts(value.get(key))
        return parsed.isoformat() if parsed else None
    return {"last_completed_at": stamp("last_refresh_at"), "version": value.get("last_refresh_version"),
            "in_progress": bool(value.get("refresh_id")), "documents_pending": len(frontier),
            "retry_at": stamp("retry_at"), "error_class": value.get("last_error_class"),
            "request_share": 0.1, "note": "Metadata shares the sweep budget. Only complete recognized snapshots can confirm missing targets; retirement requires two snapshots."}
