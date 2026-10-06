"""Facet and category resolution: platform ids in, human labels out.

Tori's sitemap enumerates 71,377 category ids and not one label. The labels are
in the facet tree embedded in every search page, and the tree is *recursive*:
each node carries its own `filter_items`, so one already-fetched page yields the
entire Finnish category taxonomy with labels, plus the platform's own enums for
condition, trade type and dealer segment.

That last part matters more than the categories. The condition enum is the
platform's real condition vocabulary -- Tori reports schema.org `UsedCondition`
for nearly everything on a search page, which is not a measurement. The
trade_type enum separates a giveaway from a sale, which keeps free items out of
a price median. The dealer_segment enum separates private from business without
touching a single seller identifier.

Two disciplines hold throughout. An id is never written into a label column: a
category whose label is unknown stays NULL rather than becoming a group called
"2". And nothing here identifies a seller -- dealer_segment is the platform's own
published distinction between "Yksityinen" and "Yritys", not a person.
"""

import base64
import json
import logging
import re
from datetime import datetime, timezone

from .. import db

log = logging.getLogger("toriresearch.category")

PAYLOAD_SCRIPT = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)
STANDARD_FILTER = "STANDARD_FILTER"
TAXONOMY_VERSION = "tori-2026-10-01"

CATEGORY_FACETS = ("category", "sub_category", "product_category")

# The platform's own enums, keyed by facet name. `trade_type` is what separates
# a giveaway ("Annetaan", price 0) from something actually for sale, and
# `dealer_segment` is a published commercial distinction that identifies nobody.
TRADE_TYPE = {"1": "for_sale", "2": "giveaway", "3": "wanted"}
DEALER_SEGMENT = {"1": "private", "3": "business"}


def _decode_payloads(html_text):
    for block in PAYLOAD_SCRIPT.findall(html_text):
        block = block.strip()
        if not block or len(block) < 32:
            continue
        try:
            yield json.loads(base64.b64decode(
                block + "=" * (-len(block) % 4)).decode("utf-8", "replace"))
        except Exception:
            continue


def _walk_filters(node):
    if isinstance(node, dict):
        if node.get("type") == STANDARD_FILTER and "filter_items" in node:
            yield node
        for value in node.values():
            yield from _walk_filters(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_filters(value)


def _walk_items(items):
    """Yield every node in the nested facet tree, at any depth."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        yield from _walk_items(item.get("filter_items"))


def facets(html_text):
    """Extract `facet name -> {value: (label, hits)}` from a search page.

    Recursive, because the category tree nests. One page therefore labels the
    whole taxonomy rather than the eleven top-level nodes.
    """
    out = {}
    for payload in _decode_payloads(html_text):
        for node in _walk_filters(payload):
            for item in _walk_items(node.get("filter_items")):
                name = item.get("name")
                value = item.get("value")
                label = item.get("display_name")
                if not (name and value is not None and label):
                    continue
                out.setdefault(str(name), {})[str(value)] = (
                    label, item.get("hits"))
    return out


def category_labels(html_text):
    """`category id -> label` across all three taxonomy depths."""
    found = {}
    for name in CATEGORY_FACETS:
        for value, (label, _hits) in facets(html_text).get(name, {}).items():
            if "." in value:          # a taxonomy id, not an enum value
                found[value] = label
    return found


def enum_counts(html_text, facet_name):
    """The platform's own value distribution for one facet, e.g. condition.

    This is a measured distribution of how sellers label their own listings,
    not an inference. Useful on its own, and it is how the giveaway share is
    quantified rather than guessed.
    """
    table = facets(html_text).get(facet_name, {})
    return {value: {"label": label, "hits": hits} for value, (label, hits) in table.items()}


def upsert_labels(mapping, platform="tori", source="facet_tree", observed=None):
    """Append changed labels; repeated identical observations do not rewrite history."""
    import hashlib
    from ..metrics import parse_ts
    if not mapping:
        return 0
    observed = observed or datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    try:
        with conn.transaction():
            previous = {row["platform_category_id"]: dict(row) for row in conn.execute(
                "SELECT * FROM dim_category WHERE platform=? AND platform_label IS NOT NULL "
                "ORDER BY valid_from ASC,category_version_pk ASC", (platform,)).fetchall()}
            for cat_id, label in mapping.items():
                prior = previous.get(cat_id)
                if prior and (prior["platform_label"] == label and prior["label_source"] == source
                              or parse_ts(prior["valid_from"]) > parse_ts(observed)):
                    continue
                suffix = hashlib.sha256(json.dumps([cat_id,label,source,observed]).encode()).hexdigest()[:20]
                version = TAXONOMY_VERSION+"-"+suffix
                if prior:
                    conn.execute("UPDATE dim_category SET valid_to=? WHERE category_version_pk=?", (observed,prior["category_version_pk"]))
                conn.execute("INSERT INTO dim_category (platform,platform_category_id,platform_category_path,taxonomy_version, "
                             "platform_label,platform_label_depth,label_source,first_seen_at,valid_from,valid_to) "
                             "VALUES (?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(platform,platform_category_id,taxonomy_version) DO NOTHING",
                             (platform,cat_id,cat_id,version,label,cat_id.count(".")+1,source,observed,observed))
    finally:
        conn.close()
    return len(mapping)


def upsert_enum(table_name, counts, platform="tori", observed=None):
    """Store a platform enum's value distribution per facet row."""
    if not counts:
        return 0
    observed = observed or datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    try:
        for value, info in counts.items():
            conn.execute(
                """INSERT INTO platform_enums
                       (platform, facet, value, label, hits, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (platform, facet, value, observed_at) DO UPDATE SET
                       label = excluded.label, hits = excluded.hits""",
                (platform, table_name, str(value), info["label"], info["hits"], observed),
            )
        conn.commit()
    finally:
        conn.close()
    return len(counts)


def _label_map(platform="tori"):
    rows = db.query(
        "SELECT platform_category_id, platform_label FROM dim_category "
        "WHERE platform = ? AND platform_label IS NOT NULL ORDER BY valid_from ASC, category_version_pk ASC", (platform,))
    return {r["platform_category_id"]: r["platform_label"] for r in rows}


def resolve(category_path, labels=None):
    """Turn a category path into (label_chain, top_label, confidence).

    The first component encodes depth. Resolve ancestry across `0.93`,
    `1.93.3215` and `2.93.3215.43`, rather than treating depth as a parent.
    A partially-known path never invents a label it lacks.

    Returns (None, None, 0.0) when nothing is known, which is the honest answer
    rather than a category called "2".
    """
    from ..collection import category_key
    labels = labels if labels is not None else _label_map()
    keyed = {category_key(path): label for path, label in labels.items()}
    key = category_key(category_path)
    parts = key.split(".")
    chain = [keyed[".".join(parts[:depth])] for depth in range(1, len(parts)+1)
             if ".".join(parts[:depth]) in keyed]
    if not chain:
        return None, None, 0.0
    # Confidence is about the leaf, not the arity: `0.93` is a complete
    # top-level id even though it has two segments and one label.
    exact = key in keyed
    return " > ".join(chain), chain[0], 1.0 if exact else 0.0


def coverage(platform="tori"):
    total = db.scalar("SELECT COUNT(*) FROM dim_category WHERE platform = ?",
                      (platform,)) or 0
    labelled = db.scalar(
        "SELECT COUNT(*) FROM dim_category WHERE platform = ? AND platform_label IS NOT NULL",
        (platform,)) or 0
    return {"categories_seen": total, "with_label": labelled,
            "label_coverage_pct": round(100 * labelled / total, 2) if total else None}
