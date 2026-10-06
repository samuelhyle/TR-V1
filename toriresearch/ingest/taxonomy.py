"""Build the category and location dimensions from Tori's public sitemap.

The sitemap enumerates every category x location combination Tori serves
(~86k URLs). That is the whole taxonomy and the whole geography, for the cost
of a few HTTP requests. No listing pages are fetched and nothing personal is
touched.

Location identifiers are opaque Tori ids. They are resolved to their published
label once and cached on disk, so the ~314 resolution requests happen exactly
once and never again.
"""

import html
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import unquote

from .. import config, db

log = logging.getLogger("toriresearch.taxonomy")

SITEMAP_INDEX = "https://www.tori.fi/sitemap.xml"
TAXONOMY_VERSION = "tori-sitemap-2026-10-01"
NORMALIZATION_VERSION = "norm-2026-10-01"


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def fetch_sitemap_documents(session):
    index = session.get_text(SITEMAP_INDEX)
    children = re.findall(r"<loc>(.*?)</loc>", index)
    docs = {}
    for url in children:
        body = session.get_text(url)
        session.save_raw(url, body.encode("utf-8"))
        docs[url] = body
    return docs


def _params(url):
    if "?" not in url:
        return {}
    query = unquote(html.unescape(url.split("?", 1)[1]))
    out = {}
    for kv in query.split("&"):
        if "=" in kv:
            key, value = kv.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def parse_dimensions(docs):
    categories = {}
    locations = {}

    for text in docs.values():
        for url in re.findall(r"<loc>(.*?)</loc>", text):
            p = _params(url)
            for key in ("category", "product_category", "sub_category"):
                raw = p.get(key)
                if raw and "." in raw:
                    categories[raw] = url
            if p.get("location"):
                locations.setdefault(p["location"], url)

    return categories, locations


def category_depth(path):
    return len(path.split("."))


def category_ancestors(path):
    parts = path.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def write_categories(categories):
    now = _utcnow()
    conn = db.connect()
    inserted = 0
    try:
        for path, sample_url in sorted(categories.items()):
            depth = category_depth(path)
            parent = category_ancestors(path)[-1] if depth > 1 else None
            conn.execute(
                """INSERT OR IGNORE INTO dim_category
                       (platform, platform_category_id, platform_category_path, taxonomy_version,
                        canonical_category_id, mapping_version, mapping_confidence,
                        valid_from, valid_to)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    "tori",
                    path,
                    f"depth={depth};parent={parent or 'root'}",
                    TAXONOMY_VERSION,
                    None,
                    NORMALIZATION_VERSION,
                    None,
                    now,
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def resolve_location_labels(session, locations, cache_path=None, limit=None):
    """Resolve opaque Tori location ids to their published label via page title.

    Tori publishes the label as the page title, so this reads Tori's own
    labelling rather than inferring geography. Mixes municipalities and regions;
    that is intentional, it is the coarsest grain Tori itself publishes.
    """
    cache_path = cache_path or (config.DATA_DIR / "tori_location_labels.json")
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    todo = {k: v for k, v in locations.items() if k not in cache}
    if limit:
        todo = dict(list(todo.items())[:limit])

    log.info("resolving %d location labels (%d already cached)", len(todo), len(cache))
    failed = []
    for i, (loc_id, url) in enumerate(sorted(todo.items()), 1):
        try:
            if config.FOCUS_CATEGORIES:
                from .sweep import build_search_url
                url = build_search_url(loc_id, config.FOCUS_CATEGORIES[0])
            html_text = session.get_text(url)
            m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S)
            label = html.unescape(m.group(1)).split("|")[0].strip() if m else None
            cache[loc_id] = label
        except Exception as exc:
            # Deliberately NOT cached. A failed fetch is not evidence that a
            # location has no label, and writing one made the failure permanent:
            # the municipality then failed `_in_scope()` on every later run and
            # never got another attempt. A page that genuinely has no <title>
            # still caches None, because that is a real observation. Leaving the
            # key absent simply means "ask again next time".
            log.warning("location %s failed, will retry next run: %s", loc_id, exc)
            failed.append(loc_id)
        if i % 25 == 0:
            cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True))
            log.info("  %d/%d", i, len(todo))

    cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True))
    return cache


def write_locations(cache):
    conn = db.connect()
    now = _utcnow()
    written = 0
    try:
        for loc_id, label in sorted(cache.items()):
            if not label:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO dim_location
                       (platform, municipality_norm, region_norm, country_code)
                   VALUES (?, ?, '', 'FI')""",
                ("tori", label),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written
