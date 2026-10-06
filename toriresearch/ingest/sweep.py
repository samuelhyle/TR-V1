"""Bounded discovery over the configured portion of Tori's published grid.

Fair rotation alternates with productive-search and continuation work. Page
observations and checkpoints commit together; moving results and depth caps
remain visible as partial coverage. Weekly sitemap reconciliation consumes the
same request budget. Actual revisit ages depend on budget and host availability.

Search membership is recorded separately from finer category evidence. Item
pages remain the source of precise condition and terminal state and have their
own budget.
"""

from .. import collection_control, collection_events

import logging
import re
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlparse
import html as html_mod

from .. import collection, config, db, discovery, parser_health, sitemap_refresh, product_attributes, listing_analysis
from ..http import AccessBlocked, AuthorityBlocked, looks_like_blocked_page, mark_collection_stop
from ..metrics import parse_ts
from . import category as catdim
from .parsing import extract_docs, extract_itemlist

log = logging.getLogger("toriresearch.sweep")

SITEMAP_INDEX = "https://www.tori.fi/sitemap.xml"
SEARCH_BASE = "https://www.tori.fi/recommerce/forsale/search"
NORMALIZATION_VERSION = "norm-2026-10-01"

def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _sitemap_urls(session):
    index = session.get_text(SITEMAP_INDEX)
    return re.findall(r"<loc>(.*?)</loc>", index)


def enumerate_grid(session, limit_locations=None):
    """Yield (location_id, category_path, url) for the whole country.

    Reads only the sitemap. No listing page is fetched to build the grid, and
    nothing personal is touched: these are category and geography identifiers
    that Tori publishes for third-party indexing.
    """
    locations = _load_location_labels()
    seen = set()
    for sitemap_url in _sitemap_urls(session):
        body = session.get_text(sitemap_url)
        session.save_raw(sitemap_url, body.encode("utf-8"))
        for raw in re.findall(r"<loc>(.*?)</loc>", body):
            url = unquote(html_mod.unescape(raw))
            params = dict(parse_qsl(urlparse(url).query))
            location_id = params.get("location")
            category = (params.get("product_category")
                        or params.get("sub_category")
                        or params.get("category"))
            if not location_id or not category:
                continue
            key = (location_id, category)
            if key in seen:
                continue
            seen.add(key)
            if limit_locations and len({k[0] for k in seen}) > limit_locations:
                continue
            yield location_id, category, build_search_url(location_id, category)


def synthesize_tier1(session, platform="tori"):
    """Add the (municipality x top-level category) layer the sitemap omits.

    Tori's sitemap enumerates locations and categories but never the two
    together at the top level: every location-scoped URL it publishes is a
    sub_category or a product_category. So the one layer that yields a national
    price panel in under an hour of requests -- 294 municipalities x 11 top
    categories -- is absent from the grid and has to be built.

    This is arithmetic over two lists Tori already publishes, not enumeration of
    anything it does not serve. The URLs resolve normally; `category=0.93` with
    a location is a valid public search.
    """
    labels = _load_location_labels()
    municipalities = [k for k in labels if k.startswith("1.")]
    top_categories = _top_level_categories(session)
    added = 0
    conn = db.connect()
    try:
        for location_id in municipalities:
            for category in top_categories:
                url = build_search_url(location_id, category)
                conn.execute(
                    """INSERT INTO sweep_targets
                           (platform, location_id, location_label, location_grain,
                            category_path, category_depth, search_url, crawl_count, active)
                       VALUES (?, ?, ?, 'municipality', ?, 2, ?, 0, 1)
                       ON CONFLICT (platform, location_id, category_path) DO UPDATE SET
                           search_url = excluded.search_url""",
                    (platform, location_id, labels.get(location_id), category, url),
                )
                added += 1
        conn.commit()
    finally:
        conn.close()
    return {"tier1_cells": added, "municipalities": len(municipalities),
            "top_categories": len(top_categories)}


def _top_level_categories(session):
    """The top-level category ids, from the sitemap or one facet tree.

    The sitemap lists them as bare `?category=0.93` URLs. If that is unavailable
    the facet tree of any search page names them, so the list is never hardcoded.
    """
    found = set()
    for sitemap_url in _sitemap_urls(session):
        body = session.get_text(sitemap_url)
        for raw in re.findall(r"<loc>(.*?)</loc>", body):
            params = dict(parse_qsl(urlparse(unquote(html_mod.unescape(raw))).query))
            if params.get("category"):
                found.add(params["category"])
        if found:
            break
    if found:
        return sorted(found)
    from . import category as catdim

    probe = build_search_url(sorted(_load_location_labels())[0],
                             config.FOCUS_CATEGORIES[0] if config.FOCUS_CATEGORIES else "0")
    body = session.get_text(probe)
    catdim.upsert_labels(catdim.category_labels(body))
    labels = catdim._label_map()
    return sorted(k for k in labels if k.count(".") == 1 and k.startswith("0."))


def build_search_url(location_id, category):
    """Compose a search URL for one grid cell.

    The sitemap uses three different parameter names at three depths and they
    are not interchangeable: `category=0.93` returns results while
    `product_category=0.93` returns none. Getting this wrong silently yields an
    empty cell that looks like a quiet market rather than a malformed query.
    """
    param = _category_param(category)
    location = f"location={location_id}&" if location_id else ''
    return f"{SEARCH_BASE}?{location}{param}={category}"


def _category_param(category_path):
    root = category_path.split(".")[0]
    return {"0": "category", "1": "sub_category", "2": "product_category"}.get(
        root, "product_category")


def _load_location_labels():
    path = config.DATA_DIR / "tori_location_labels.json"
    if not path.exists():
        return {}
    import json

    return {k: v for k, v in json.loads(path.read_text()).items() if v}


def build_targets(session, platform="tori", limit_locations=None, budget=None):
    """Advance a durable grid refresh inside the same manual request ceiling."""
    if platform != "tori":
        raise ValueError("Sitemap reconciliation supports Tori")
    budget = int(config.SWEEP_BUDGET if budget is None else budget)
    if budget < 1 or limit_locations is not None and limit_locations < 1:
        raise ValueError("Budget and location limit must be positive")
    lock, error = db.acquire_lock("sweep")
    if lock is None:
        return {"status": "skipped", "reason": error, "requests": 0}
    try:
        count, result = 0, {}
        for _slot in range(budget):
            retry = parse_ts(discovery.state().get("retry_at"))
            if retry and retry > discovery.now():
                result = {"status": "error", "completed": False, "reason": "metadata retry is not due"}
                break
            result = sitemap_refresh.step(session, limit_locations=limit_locations)
            count += 1
            if result.get("completed") or result["status"] != "ok":
                break
        return {**result, "requests": count, "targets": _count_targets(platform), "sitemap_refresh": sitemap_refresh.report()}
    finally:
        db.release_lock(lock)


def _count_targets(platform="tori"):
    return db.scalar("SELECT COUNT(*) FROM sweep_targets WHERE platform = ?", (platform,)) or 0


def stalest_targets(platform, budget, only_municipality=True, deepest_first=False):
    """Read-only preview of the fair/utility dispatch sequence."""
    rows = discovery.targets(platform, only_municipality)
    count = discovery.state(platform)["dispatch_count"]
    reference = discovery.now()
    selected = []
    for index in range(max(0, int(budget))):
        target = discovery.choose(rows, count+index, reference, deepest_first)
        if target is None:
            break
        selected.append(target)
        rows = [dict(r, last_attempt_at=reference.isoformat(), next_due_at=(reference+timedelta(days=365)).isoformat())
                if r["target_pk"] == target["target_pk"] else r for r in rows]
    return selected


def _items_from_docs(body, location_label, platform):
    """Records from the embedded search API response, or None.

    Preferred over the JSON-LD because it carries strictly more per listing for
    the same request: the platform's own `trade_type`, its private/business
    flag, its Diili marker, and the item's own location label. The giveaway test
    moves off the "title starts with Annetaan" heuristic and onto the platform's
    own field, which is the difference between excluding giveaways and guessing.

    Returns None rather than [] when there is no embedded response, so the
    caller can fall back to the JSON-LD path instead of recording a busy page as
    an empty one.
    """
    docs = extract_docs(body)
    if not docs:
        return None
    out = []
    for doc in docs:
        price = doc.get("price_amount")
        record = {
            "listing_id": doc["listing_id"],
            "title": doc.get("title"),
            "price": float(price) if price is not None else None,
            "currency": doc.get("currency") or "EUR",
            "raw_condition": None,
            "brand": doc.get("brand"),
            "is_giveaway": bool(doc.get("is_giveaway")),
            "seller_type_norm": doc.get("seller_type_norm"),
            "subplatform": doc.get("subplatform") or "standard",
            # The item's own municipality label beats the grid cell's: a
            # region-wide cell yields many municipalities and only the listing
            # knows which one it is in.
            "municipality_norm": doc.get("municipality_norm") or location_label,
            "status_raw": doc.get("trade_type_raw"),
            "image_count": doc.get("image_count"),
            "platform_touched_at": doc.get("platform_touched_at"),
            "has_buy_now": doc.get("has_buy_now"),
            "has_shipping": doc.get("has_shipping"),
            "_source": "search_docs",
            "_product_attributes": doc.get("_product_attributes"),
            "_listing_analysis_input": doc.get("_listing_analysis_input"),
        }
        out.append(record)
    return out or None


def _items_from_itemlist(items, location_label, platform):
    """Records from the JSON-LD ItemList. The original, thinner path."""
    out = []
    for item in items:
        url = item.get("url") or ""
        match = re.search(r"/item/(\d+)", url)
        if not match:
            continue
        offer = item.get("offers") or {}
        try:
            price = float(offer.get("price"))
        except (TypeError, ValueError):
            price = None
        currency = offer.get("priceCurrency") or "EUR"
        # A giveaway is not a EUR 0 sale. Price alone cannot tell them apart
        # and a zero in a price median is a fabricated observation.
        is_giveaway = (price is not None and price <= 0) or (item.get("name") or "").strip().lower().startswith("annetaan")
        out.append({
            "listing_id": match.group(1),
            "title": item.get("name") or item.get("description"),
            "price": price,
            "currency": currency,
            "raw_condition": (item.get("itemCondition") or "").rsplit("/", 1)[-1],
            "brand": ((item.get("brand") or {}).get("name") or None),
            "is_giveaway": is_giveaway,
            "seller_type_norm": None,
            "subplatform": "standard",
            "municipality_norm": location_label,
            "status_raw": offer.get("availability"),
            "image_count": 1 if item.get("image") else None,
            "platform_touched_at": None,
            "has_buy_now": False,
            "has_shipping": None,
            "_source": "search_itemlist",
            "_product_attributes": product_attributes.extract({"title": item.get("name"), "description": item.get("description")}),
            "_listing_analysis_input": listing_analysis.prepare({"title":item.get("name"),"description":item.get("description")}),
        })
    return out


def _ingest_items(items, location_label, category_path, observed, platform, conn=None, target=None):
    """Record the listings on one search page.

    Query membership supplies category evidence until a finer search or item
    page provides it. Parent membership is recorded independently and cannot
    erase a known leaf. Labels resolve against dim_category and stay NULL when
    unknown.
    """
    chain, root, confidence = catdim.resolve(category_path)
    stats = {"seen": 0, "new": 0, "new_in_scope": 0, "priced": 0, "giveaway": 0, "price_changed": 0, "page_overlap": 0,
             "diili": 0, "seller_typed": 0}
    scope = collection.Scope(platform)
    owns = conn is None
    conn = conn or db.connect()
    try:
        with conn.transaction():
            for item in {str(item['listing_id']): item for item in items}.values():
                listing_id = item["listing_id"]
                price = item["price"]
                currency = item["currency"]
                is_giveaway = item["is_giveaway"]
                if item.get("subplatform") == "diili":
                    stats["diili"] += 1
                if item.get("seller_type_norm"):
                    stats["seller_typed"] += 1

                record = {
                    "platform": platform,
                    "subplatform": item.get("subplatform") or "standard",
                    "listing_id": listing_id,
                    "title": item.get("title"),
                    "seller_type_norm": item.get("seller_type_norm"),
                    "category_raw_id": category_path,
                    "category_norm": chain,
                    "category_root_norm": root,
                    "category_label_confidence": confidence,
                    "brand_norm": item.get("brand"),
                    "condition_raw": item.get("raw_condition") or None,
                    "condition_norm": None,
                    "condition_label_raw": None,
                    "municipality_norm": item.get("municipality_norm"),
                    "region_norm": None,
                    "listed_price_original": price,
                    "currency_original": currency,
                    "listed_price_eur": price if currency == "EUR" and price is not None and price > 0 and not is_giveaway else None,
                    "is_giveaway": 1 if is_giveaway else 0,
                    "platform_edited_at": None,
                    "first_seen_at": observed,
                    "last_seen_at": observed,
                    "observation_coverage_end": None,
                    "status_raw": item.get("status_raw"),
                    "status_norm": "active",
                    "coverage": "collecting",
                    "outcome_status": "unknown",
                    "outcome_confidence": None,
                    "outcome_observed_at": None,
                    "outcome_label_raw": None,
                    "image_count": item.get("image_count"),
                    "normalization_version": NORMALIZATION_VERSION,
                    "source_observed_at": observed,
                    "last_live_at": observed,
                    "platform_touched_at": item.get("platform_touched_at"),
                    "has_buy_now": item.get("has_buy_now"),
                    "has_shipping": item.get("has_shipping"),
                    "_source": item.get("_source", "unknown"),
                    "_product_attributes": item.get("_product_attributes"),
                    "_listing_analysis_input": item.get("_listing_analysis_input"),
                }
                prior_row = conn.execute(
                    "SELECT listing_pk, listed_price_eur, platform, category_raw_id, "
                    "category_norm, category_root_norm, municipality_norm FROM listings "
                    "WHERE platform = ? AND listing_id = ?",
                    (platform, listing_id),
                ).fetchone()
                prior = dict(prior_row) if prior_row else None
                if prior and prior["category_raw_id"] and collection.is_below(
                        collection.category_key(prior["category_raw_id"]), collection.category_key(category_path)):
                    # A permitted parent search is evidence of membership, not
                    # a reason to erase a previously measured child category.
                    for field in ("category_raw_id", "category_norm", "category_root_norm"):
                        record[field] = prior[field]
                pk, is_new = db.record_observation(record, conn=conn)
                if target:
                    previous = conn.execute("SELECT last_traversal_id FROM listing_search_attributions "
                                            "WHERE listing_pk=? AND target_pk=?", (pk, target["target_pk"])).fetchone()
                    stats["page_overlap"] += bool(previous and previous[0] == target["traversal_id"])
                    conn.execute("INSERT INTO listing_search_attributions (listing_pk,target_pk,first_seen_at,last_seen_at,last_traversal_id) "
                                 "VALUES (?,?,?,?,?) ON CONFLICT(listing_pk,target_pk) DO UPDATE SET "
                                 "last_seen_at=excluded.last_seen_at,last_traversal_id=excluded.last_traversal_id",
                                 (pk, target["target_pk"], observed, observed, target["traversal_id"]))
                if prior is None or not scope.matches(prior):
                    merged = dict(conn.execute("SELECT platform, category_raw_id, category_norm, "
                                               "category_root_norm, municipality_norm FROM listings WHERE listing_pk=?",
                                               (pk,)).fetchone())
                    if scope.matches(merged):
                        stats["new_in_scope"] += 1
                stats["seen"] += 1
                if is_new:
                    stats["new"] += 1
                elif prior and prior["listed_price_eur"] is not None and record["listed_price_eur"] is not None and abs(float(prior["listed_price_eur"]) - record["listed_price_eur"]) > 0.005:
                    stats["price_changed"] += 1
                if record["listed_price_eur"] is not None:
                    stats["priced"] += 1
                if is_giveaway:
                    stats["giveaway"] += 1
    finally:
        if owns:
            conn.close()
    return stats


def _commit_page(target, records, observed, parser, empty, raw_count=None, missing_id=None):
    conn = db.connect()
    try:
        with conn.transaction():
            current = conn.execute("SELECT traversal_id,next_page FROM sweep_targets WHERE target_pk=?", (target["target_pk"],)).fetchone()
            if current is None or current[0] != target["traversal_id"] or current[1] != target["next_page"]:
                raise RuntimeError("Search checkpoint changed before observation")
            stats = _ingest_items(records, target["location_label"], target["category_path"], observed,
                                  target["platform"], conn=conn, target=target)
            result = discovery.commit(conn, target, observed, stats, parser, empty)
            parser_health.record(target["platform"], "search", parser,
                                 records=[dict(r,category_path=target["category_path"]) for r in records],
                                 raw_count=raw_count, missing_id=missing_id, conn=conn)
        return stats, result
    finally:
        conn.close()


def run(session, budget=None, pages=None, only_municipality=True, platform="tori",
        dry_run=False, deepest_first=False):
    """Spend one shared request budget, preserving committed page checkpoints."""
    from .parsing import search_result_shape
    budget = int(config.SWEEP_BUDGET if budget is None else budget)
    pages = int(config.SWEEP_PAGES if pages is None else pages)
    if budget < 1 or pages < 1:
        raise ValueError("budget and pages must be positive")
    lock, err = db.acquire_lock("sweep")
    if lock is None:
        return {"status": "skipped", "reason": err, "requests": 0}
    started = _utcnow()
    totals = dict.fromkeys(("targets", "requests", "seen", "new", "priced", "giveaway", "price_changed",
                          "throttled_429", "new_in_scope", "diili", "seller_typed", "pages_from_docs",
                          "pages_from_itemlist", "blocked_403", "errors", "page_overlap", "reobserved",
                          "discovery_pages", "sitemap_requests", "capped_traversals", "abandoned_traversals"), 0)
    verticals = collection.VerticalCounters(platform)
    totals["by_vertical"] = verticals.values
    status, error = "ok", None
    try:
        if not dry_run:
            discovery.sync_national_targets(platform)
        rows = discovery.targets(platform, only_municipality)
        count = discovery.state(platform)["dispatch_count"]
        if dry_run:
            preview = stalest_targets(platform, budget, only_municipality, deepest_first)
            return {**totals, "dry_run": True, "targets": len(preview), "rotation": discovery.report(platform),
                    "next": [{"url": t["search_url"], "page": t["next_page"], "location": t["location_label"],
                              "category": t["category_path"]} for t in preview[:10]]}
        for slot in range(budget):
            collection_control.checkpoint()
            reference = discovery.now()
            target = discovery.choose(rows, count, reference, deepest_first)
            metadata = platform == "tori" and sitemap_refresh.due(reference)
            # One in ten dispatch slots, or idle capacity, services metadata.
            if metadata and (count % 10 == 9 or target is None):
                totals["requests"] += 1
                totals["sitemap_requests"] += 1
                result = sitemap_refresh.step(session, reference)
                count += 1
                if result.get("status") in ("blocked", "throttled"):
                    status = result["status"]
                    totals["blocked_403" if status == "blocked" else "throttled_429"] += 1
                    break
                if result.get("status") == "error":
                    totals["errors"] += 1
                    # Metadata failure is reported and backed off; search and
                    # detail work can continue inside the existing budget.
                if result.get("completed"):
                    rows = discovery.targets(platform, only_municipality)
                continue
            if target is None:
                break
            depth = 1 if target.get('location_grain') == 'national' else pages
            target = discovery.prepare(target, depth, reference)
            count += 1
            totals["abandoned_traversals"] += int(target["abandoned_traversal"])
            page = target["next_page"]
            url = target["search_url"] + (f"&page={page}" if page > 1 else "")
            dispatch = {"platform": platform, "category_raw_id": target["category_path"],
                        "municipality_norm": target["location_label"]}
            totals["requests"] += 1
            verticals.add(dispatch, requests=1)
            stop = False
            parser, shape = "unknown", None
            try:
                body = session.get_text(url, retry_on_429=False, max_attempts=1)
                shape = search_result_shape(body)
                if shape is None:
                    if looks_like_blocked_page(body):
                        raise AccessBlocked("refusal page served as 200")
                    raise ValueError("unrecognized search page; coverage unchanged")
                parser = shape["parser"]
                records = [] if parser == "docs" and shape["raw_count"] == 0 else _items_from_docs(body, target["location_label"], platform)
                if records is None:
                    records = _items_from_itemlist(extract_itemlist(body), target["location_label"], platform)
                    parser = "itemlist"
                    shape = search_result_shape(body, parser="itemlist") or shape
                if not records and shape["raw_count"]:
                    raise ValueError("nonempty search payload yielded no valid listings")
                observed = _utcnow()
                catdim.upsert_labels(catdim.category_labels(body))
                for facet in ("condition", "trade_type", "dealer_segment"):
                    catdim.upsert_enum(facet, catdim.enum_counts(body, facet), observed=observed)
                stats, committed = _commit_page(target, records, observed, parser, not records and shape["raw_count"] == 0,
                                                raw_count=shape["raw_count"], missing_id=shape["missing_id"])
                totals["discovery_pages"] += 1
                totals["pages_from_docs" if parser == "docs" else "pages_from_itemlist"] += 1
                for key in ("seen", "new", "new_in_scope", "priced", "giveaway", "price_changed", "diili", "seller_typed", "page_overlap"):
                    totals[key] += stats[key]
                totals["reobserved"] += stats["seen"]-stats["new"]
                totals["targets"] += int(committed["finished"])
                totals["capped_traversals"] += int(committed["capped"])
                verticals.add(dispatch, **{key: stats[key] for key in ("seen", "new", "new_in_scope", "price_changed")},
                              targets=int(committed["finished"]))
                collection_events.checkpoint('sweep', totals)
            except collection_control.CollectionHalted:
                # The prepared traversal remains immediately resumable. Operator
                # control must not charge a source failure or impose retry backoff.
                raise
            except AuthorityBlocked:
                totals["authority_denied"] = totals.get("authority_denied", 0)+1
                totals["errors"] += 1
                status, stop = "blocked", True
                discovery.fail(target, "platform_stop")
            except AccessBlocked:
                parser_health.failure(platform, "search", parser, "refusal")
                mark_collection_stop(url, "blocked")
                totals["blocked_403"] += 1
                verticals.add(dispatch, blocked=1)
                status, stop = "blocked", True
                discovery.fail(target, "platform_stop")
            except HTTPError as exc:
                totals["errors"] += 1
                verticals.add(dispatch, errors=1)
                if exc.code in (403, 429):
                    blocked = exc.code == 403
                    mark_collection_stop(url, "blocked" if blocked else "throttled")
                    totals["blocked_403" if blocked else "throttled_429"] += 1
                    status, stop = ("blocked" if blocked else "throttled"), True
                    discovery.fail(target, "platform_stop")
                else:
                    status = "error"
                    discovery.fail(target, "http404" if exc.code == 404 else "http")
                error = "HTTP " + str(exc.code)
            except Exception as exc:
                if isinstance(exc, (ValueError, TypeError, KeyError)):
                    parser_health.failure(platform, "search", parser, raw_count=(shape or {}).get("raw_count", 0))
                totals["errors"] += 1
                verticals.add(dispatch, errors=1)
                status = "error"
                discovery.fail(target, "parse" if isinstance(exc, ValueError) else "storage_or_network")
                error = type(exc).__name__
                log.warning("search page failed: %s", error)
            updated = db.query_one("SELECT * FROM sweep_targets WHERE target_pk=?", (target["target_pk"],))
            rows = [updated if r["target_pk"] == target["target_pk"] else r for r in rows]
            rows = [r for r in rows if r["active"]]
            if stop:
                break
        totals["rotation"] = discovery.report(platform)
        return _finish(totals, started, budget, platform, status=status, error=error)
    finally:
        db.release_lock(lock)


def _record_target(target, observed, listing_count, status, platform, deactivate=False,
                   counted=True):
    """Record that we crawled a cell.

    A cell is only deactivated when the platform stops serving it. A cell that
    happens to be empty right now is retried on the next rotation: an empty
    municipality/category pair in October can hold listings in November.

    `counted=False` records the attempt without claiming we read the cell, so a
    refused request does not refresh `last_crawled_at`, does not bump
    `crawl_count`, and above all does not move grid coverage. Coverage that
    counts cells we were not allowed to look at is the one kind of coverage
    number this project must never publish.
    """
    if not counted:
        db.execute(
            """UPDATE sweep_targets SET last_status = ?, active = CASE WHEN ? THEN FALSE ELSE active END WHERE target_pk = ?""",
            (status, bool(deactivate), target["target_pk"]),
        )
        return
    db.execute(
        """UPDATE sweep_targets
           SET last_crawled_at = ?, crawl_count = crawl_count + 1,
               last_listing_count = ?, last_status = ?,
               active = CASE WHEN ? THEN FALSE ELSE active END
           WHERE target_pk = ?""",
        (observed, listing_count, status, bool(deactivate), target["target_pk"]),
    )


def _finish(totals, started, budget, platform, status, error=None):
    finished = _utcnow()
    total = _count_targets(platform)
    never = db.scalar(
        "SELECT COUNT(*) FROM sweep_targets WHERE platform = ? AND last_crawled_at IS NULL",
        (platform,)) or 0
    db.execute(
        """INSERT INTO sweep_runs (started_at, finished_at, budget, requests_made,
               listings_seen, listings_new, targets_crawled, targets_remaining_stale,
               throttled_429, blocked_403, status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (started, finished, budget, totals["requests"], totals["seen"], totals["new"],
         totals["targets"], never, totals["throttled_429"], totals["blocked_403"],
         status, error),
    )
    totals["targets_total"] = total
    totals["targets_never_crawled"] = never
    totals["grid_coverage_pct"] = round(100 * (1 - never / total), 3) if total else None
    totals["status"] = status
    if error:
        totals["error"] = error
    return totals


def coverage(platform="tori"):
    total = _count_targets(platform)
    by_depth = db.query(
        "SELECT category_depth, COUNT(*) AS cells, "
        "SUM(CASE WHEN last_crawled_at IS NULL THEN 1 ELSE 0 END) AS pending "
        "FROM sweep_targets WHERE platform = ? GROUP BY 1 ORDER BY 1", (platform,))
    never = db.scalar(
        "SELECT COUNT(*) FROM sweep_targets WHERE platform = ? AND last_crawled_at IS NULL",
        (platform,)) or 0
    municipalities = db.scalar(
        "SELECT COUNT(DISTINCT location_id) FROM sweep_targets "
        "WHERE platform = ? AND location_grain = 'municipality'", (platform,)) or 0
    tier1 = db.scalar(
        "SELECT COUNT(*) FROM sweep_targets WHERE platform = ? AND category_depth = 2",
        (platform,)) or 0
    tier1_pending = db.scalar(
        "SELECT COUNT(*) FROM sweep_targets WHERE platform = ? AND category_depth = 2 "
        "AND last_crawled_at IS NULL", (platform,)) or 0
    ages = [(datetime.now(timezone.utc) - parse_ts(row["last_crawled_at"])).total_seconds()/3600
            for row in db.query("SELECT last_crawled_at FROM sweep_targets WHERE platform=? AND last_crawled_at IS NOT NULL", (platform,))
            if parse_ts(row["last_crawled_at"])]
    import statistics
    focused, params = collection.Scope(platform).conditions(grid=True)
    focused_where = "platform=? AND active=TRUE" + (" AND " + " AND ".join(focused) if focused else "")
    configured = db.query_one(
        "SELECT COUNT(*) AS targets, COUNT(DISTINCT location_id) AS locations, "
        "SUM(CASE WHEN last_crawled_at IS NULL THEN 1 ELSE 0 END) AS pending "
        "FROM sweep_targets WHERE " + focused_where, [platform] + params)
    focused_total, focused_pending = configured["targets"], configured["pending"] or 0
    return {
        "rotation": discovery.report(platform),
        "sitemap_refresh": sitemap_refresh.report() if platform == "tori" else None,
        "focus_categories": list(config.FOCUS_CATEGORIES),
        "focus_municipalities": list(config.FOCUS_MUNICIPALITIES),
        "configured": {"targets": focused_total, "never_crawled": focused_pending,
                       "locations": configured["locations"],
                       "grid_coverage_pct": round(100*(1-focused_pending/focused_total), 3) if focused_total else None},
        "targets": total,
        "municipalities": municipalities,
        "by_depth": [{"depth": r["category_depth"], "cells": r["cells"],
                      "pending": r["pending"]} for r in by_depth],
        "tier1_national": {"cells": tier1, "pending": tier1_pending,
                           "national_coverage_pct": round(
                               100 * (1 - tier1_pending / tier1), 1) if tier1 else None},
        "never_crawled": never,
        "grid_coverage_pct": round(100 * (1 - never / total), 3) if total else None,
        "hours_for_one_full_pass_at_1rps": round(total / 3600, 1) if total else None,
        "median_hours_since_crawl": round(statistics.median(ages), 1) if ages else None,
        "mean_hours_since_crawl": round(statistics.mean(ages), 1) if ages else None,
    }
