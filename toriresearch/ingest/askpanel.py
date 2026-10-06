"""Single-cell collection. A convenience wrapper over the sweep.

This module used to hold its own crawler. It was superseded by
`ingest/sweep.py`, which knows the national grid and records category
attribution from the query. It is kept for two reasons: the JSON-LD item-list
parser lives here, and `cli askpanel` is still the fastest way to look at one
municipality by hand.

The previous implementation has been removed rather than fixed because it had
three defects that could not be patched locally: it re-scraped a rolling window
of the newest listings instead of re-measuring a fixed set, it carried
SQLite-only SQL that broke under `TORI_PG_DSN`, and it never marked a listing
as gone, so no duration could ever be derived from it.
"""

import json
import logging

from .. import config, db
from . import sweep

log = logging.getLogger("toriresearch.askpanel")

from .parsing import extract_itemlist, listing_id_from, search_item_condition

__all__ = [
    "extract_itemlist",
    "listing_id_from",
    "normalise_condition",
    "run",
    "SEARCH_BASE",
]




def normalise_condition(item):
    """Search-page condition label, uninterpreted. See parsing.search_item_condition."""
    return search_item_condition(item), None


def load_location_ids():
    path = config.DATA_DIR / "tori_location_labels.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def location_id_for(municipality_norm):
    cache = load_location_ids()
    return next((k for k, v in cache.items()
                 if v and v.lower() == (municipality_norm or "").lower()), None)


def run(session, municipality_norm, location_id=None, category=None,
        page_limit=None, platform="tori"):
    """Crawl one (municipality, category) cell now, bypassing the rotation.

    Registers the cell in the sweep grid if it is not already there, so an
    ad-hoc look does not create a second, untracked crawl path.
    """
    location_id = location_id or location_id_for(municipality_norm)
    if not location_id:
        raise ValueError(
            f"unknown municipality {municipality_norm!r}; run 'cli sweep --build' first"
        )
    if not category:
        raise ValueError(
            "a category is required. 'category=0.93' is Elektroniikka ja kodinkoneet; "
            "the full list is in sweep_targets."
        )
    url = sweep.build_search_url(location_id, category)
    existing = db.query_one(
        "SELECT target_pk FROM sweep_targets WHERE platform = ? AND location_id = ? "
        "AND category_path = ?", (platform, location_id, category))
    if existing is None:
        labels = load_location_ids()
        db.execute(
            """INSERT INTO sweep_targets (platform, location_id, location_label,
                   location_grain, category_path, category_depth, search_url, crawl_count, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)
               ON CONFLICT (platform, location_id, category_path) DO NOTHING""",
            (platform, location_id, labels.get(location_id),
             "municipality" if location_id.startswith("1.") else "region",
             category, category.count(".") + 1, url),
        )

    lock, error = db.acquire_lock("sweep")
    if lock is None:
        return {"status": "skipped", "reason": error, "requests": 0}
    observed = sweep._utcnow()
    totals = {"seen": 0, "new": 0, "priced": 0, "giveaway": 0, "price_changed": 0,
              "requests": 0, "url": url}
    try:
        for page in range(1, int(page_limit or 1) + 1):
            body = session.get_text(url + (f"&page={page}" if page > 1 else ""),
                                    retry_on_429=False, max_attempts=1)
            totals["requests"] += 1
            items = sweep._items_from_docs(body, municipality_norm, platform)
            if items is None:
                items = sweep._items_from_itemlist(extract_itemlist(body), municipality_norm, platform)
            if not items:
                from ..http import AccessBlocked, looks_like_blocked_page
                from .parsing import recognised_search_page
                if looks_like_blocked_page(body):
                    raise AccessBlocked("refusal page served as 200")
                if not recognised_search_page(body):
                    raise ValueError("unrecognized search page")
                break
            stats = sweep._ingest_items(items, municipality_norm, category, observed, platform)
            for key in ("seen", "new", "priced", "giveaway", "price_changed"):
                totals[key] += stats[key]
    finally:
        db.release_lock(lock)
    return totals


# Retained for callers that imported these names before the sweep existed.
SEARCH_BASE = sweep.SEARCH_BASE
MAX_PAGES = 3
NORMALIZATION_VERSION = "norm-2026-10-01"
