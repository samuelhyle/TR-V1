"""Ingest Tori's public Atom feed into the listing spine.

The feed is published by Tori itself and carries listing id, title, published
and updated timestamps. It carries no price, no category and no location, so
this establishes identity and observation timing only. Because it is a feed
the platform chooses to publish, it is the lowest-exposure channel available
and it is the backbone for duration measurement.
"""

import logging
from datetime import datetime, timezone
from xml.etree import ElementTree

from .. import db

log = logging.getLogger("toriresearch.atom")

FEED_URL = "https://www.tori.fi/feed/recommerce/atom.xml"
NS = {"atom": "http://www.w3.org/2005/Atom"}
NORMALIZATION_VERSION = "norm-2026-10-01"


def parse_feed(payload):
    root = ElementTree.fromstring(payload)
    entries = []
    feed_updated = root.findtext("atom:updated", namespaces=NS)
    for entry in root.findall("atom:entry", namespaces=NS):
        link = entry.find("atom:link", namespaces=NS)
        urn = entry.findtext("atom:id", namespaces=NS) or ""
        entries.append(
            {
                "listing_id": urn.split(":")[-1].strip(),
                "title": (entry.findtext("atom:title", namespaces=NS) or "").strip(),
                "listing_url": link.get("href") if link is not None else None,
                "published_at": entry.findtext("atom:published", namespaces=NS),
                "updated_at": entry.findtext("atom:updated", namespaces=NS),
            }
        )
    return feed_updated, [e for e in entries if e["listing_id"]]


def _iso(value):
    if not value:
        return None
    return value.replace("Z", "+00:00")


def ingest(session, url=FEED_URL, platform="tori", subplatform="standard"):
    payload = session.get(url)
    session.save_raw(url, payload)
    feed_updated, entries = parse_feed(payload)
    now = datetime.now(timezone.utc).isoformat()

    new = 0
    reobserved = 0
    conn = db.connect()
    try:
        for e in entries:
            # first_seen_at is when *we* observed it. The feed's own `updated` is a
            # platform-side edit timestamp and belongs in platform_edited_at.
            # Conflating them made every duration in the system wrong at the root,
            # because an ad edited a month ago appeared to have been observed for a
            # month on the first sighting.
            observed = now
            edited = _iso(e["updated_at"])
            published = _iso(e["published_at"])
            record = {
                "platform": platform,
                "subplatform": subplatform,
                "listing_id": e["listing_id"],
                "title": e["title"] or None,
                "category_norm": None,
                "category_root_norm": None,
                "condition_norm": None,
                "condition_label_raw": None,
                "condition_raw": None,
                "municipality_norm": None,
                "region_norm": None,
                "listed_price_original": None,
                "currency_original": None,
                "listed_price_eur": None,
                "platform_edited_at": edited,
                "first_seen_at": observed,
                "last_seen_at": observed,
                "observation_coverage_end": None,
                "status_raw": None,
                "status_norm": "active",
                "coverage": "collecting",
                "outcome_status": "unknown",
                "outcome_confidence": None,
                "outcome_observed_at": None,
                "outcome_label_raw": None,
                "image_count": None,
                "normalization_version": NORMALIZATION_VERSION,
                "source_observed_at": observed,
                "last_live_at": observed,
                "_source": "atom",
            }
            listing_pk, is_new = db.record_observation(record, conn=conn)
            if is_new:
                new += 1
                db.emit_event(conn, listing_pk, "first_seen", observed, observed)
            else:
                reobserved += 1
        conn.commit()
    finally:
        conn.close()

    conn = db.connect()
    try:
        conn.execute(
            """INSERT INTO ingest_runs (started_at, finished_at, source, endpoint, status,
                   rows_ingested, requests_made)
               VALUES (?, ?, 'tori.atom', ?, 'ok', ?, 1)""",
            (now, datetime.now(timezone.utc).isoformat(), url, new),
        )
        conn.commit()
    finally:
        conn.close()

    log.info("feed updated %s; %d entries, %d new, %d re-observed",
             feed_updated, len(entries), new, reobserved)
    return {"entries": len(entries), "new": new, "reobserved": reobserved,
            "feed_updated": feed_updated,
            "note": "the feed is a rolling window of the newest listings, not an "
                    "id spine; it measures arrival rate, not duration"}
