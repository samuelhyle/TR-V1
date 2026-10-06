"""Item-page ingestion: category, condition, and the terminal state.

A search page yields ~52 listings per request and tells you almost nothing about
them beyond id, title and ask price. An item page yields one listing and tells
you nearly everything: the platform's own category chain, its own condition
label, its own edit timestamp, and whether the seller marked it sold.

That last one matters more than the rest. Tori publishes a sold marker on the
item itself -- `disposed: true` with `disposedText` such as "Myyty" -- which the
project previously assumed did not exist. It is a *seller-declared* outcome, so
it undercounts and lags real sales, and it is recorded here as
`sold_inferred` with a confidence below 1. It is not a transaction and is never
reported as one. What it buys is the first honest denominator in the project:
a survival curve over listings that actually reached a terminal state.

Data minimisation is structural, not procedural. The page also carries exact
`lat`/`lng` and `postalCode`; those are read by nobody, because this module
builds its record from an explicit allowlist and the payload object is
discarded when the function returns. There is no code path that could store
them, which is the only version of this rule that survives contact with a
deadline.

`ownerId` is the one exception, and it is an exception for a reason that has
to be written down rather than implied. Nothing about seller concentration or
seller behaviour can be computed from listings that cannot be grouped into
sellers, so the alternative to reading it is not "the same analysis with less
precision" -- it is no seller analysis at all. The identifier is therefore
read, reduced to a keyed HMAC by `privacy.seller_key`, and dropped before the
record exists: what reaches the database is 32 hex characters, and the raw
value appears in no record, log line, exception or export. The posture is
configurable and off by default; see `privacy.py` for what the salt means for
the longevity of every seller-level series in the project.
"""

from .. import collection_control, collection_events

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError

from .. import collection, config, db, followup, privacy, product_attributes, listing_analysis
from ..http import AccessBlocked, AuthorityBlocked, looks_like_blocked_page, mark_collection_stop

log = logging.getLogger("toriresearch.itempage")


class ParseFailure(ValueError):
    """A page could not be interpreted; it says nothing about the outcome."""

ITEM_URL = "https://www.tori.fi/recommerce/forsale/item/{listing_id}"
NORMALIZATION_VERSION = "norm-2026-10-01"

_HYDRATION = re.compile(
    r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);?\s*</script>",
    re.S,
)

# The platform's own condition words, mapped to the project's enum. Anything
# unrecognised becomes "unknown" rather than being forced into a bucket: a wrong
# condition label is worse than an absent one, because it survives into a panel
# median as if it were measured.
CONDITION_MAP = {
    "uusi": "new_unused",
    "kuin uusi": "like_new",
    "erinomainen": "like_new",
    "hyvä": "very_good",
    "hyvakuntoinen": "very_good",
    "kohtalainen": "good",
    "tyydyttävä": "fair",
    "kohtalainenkuntoinen": "good",
    "huono": "poor",
    "huonokuntoinen": "poor",
    "rikkinäinen": "poor",
    "vika": "poor",
    "osia": "for_parts",
    "varaosiksi": "for_parts",
}

OUTCOME_MAP = {
    "myyty": "sold_inferred",
    "myytynä": "sold_inferred",
    "sold": "sold_inferred",
    "ei enää saatavilla": "expired",
    "poistettu": "withdrawn",
}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def parse_hydration(html_text):
    """Return the item-recommerce payload, or None if the page has no item.

    An absent or malformed payload is ambiguous and is never terminal evidence.
    """
    match = _HYDRATION.search(html_text)
    if not match:
        return None
    try:
        # First json.loads yields the JS string literal; second yields the object.
        payload = json.loads(json.loads(match.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    loaders = payload.get("loaderData")
    route = loaders.get("item-recommerce") if isinstance(loaders, dict) else None
    item = route.get("itemData") if isinstance(route, dict) else None
    return item if isinstance(item, dict) and item else None


def _category_chain(node):
    """Walk the platform's category tree into a top>leaf label chain.

    Returns (root_label, leaf_label, numeric_leaf_id). Only labels are kept:
    Tori's numeric ids are an internal taxonomy with no stability guarantee.
    """
    labels = []
    numeric_id = None
    cursor = node or {}
    while isinstance(cursor, dict) and cursor.get("value"):
        labels.append(cursor["value"])
        if numeric_id is None and cursor.get("id") is not None:
            numeric_id = str(cursor["id"])
        cursor = cursor.get("parent") or {}
    if not labels:
        return None, None, None
    labels.reverse()
    return labels[0], labels[-1], numeric_id


def normalise_condition(label):
    if not label:
        return None, None
    key = label.strip().lower()
    return label, CONDITION_MAP.get(key, "unknown")


def _extra_scalar(item, extra_id):
    """One of the platform's `extras`, as a single string.

    Two shapes exist in the same payload and they are not interchangeable. The
    item page carries `"value": "Hyvä"`; the search response carries
    `"values": ["Mercedes-Benz"]` for brand. Reading only `value` reads only the
    item page, and reading only `values` reads nothing -- so both are tried, and
    the plural form is joined rather than taken raw, because
    `["A", "B"]` stringified is `"['A', 'B']"`, which then matches no
    condition and silently stores NULL.
    """
    for extra in item.get("extras") or []:
        if not isinstance(extra, dict) or extra.get("id") != extra_id:
            continue
        value = extra.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
        values = extra.get("values")
        if isinstance(values, list) and values:
            parts = [str(v).strip() for v in values if str(v).strip()]
            if len(parts) == 1:
                return parts[0]
            if parts:
                # Multi-valued: keep them distinguishable rather than fusing
                # them into a string that matches nothing.
                return ", ".join(parts)
    return None


def _seller_key_from(item):
    """Pseudonymous key for the item's seller, or None.

    The identifier lives at `meta.ownerId` in the item-recommerce payload --
    an integer, nested under `meta` beside `userOwner`, not at the top level
    beside `title` and `price`. Only the top level was read here for a while,
    which is a shape the platform never serves: every parse derived no key,
    every seller-level model reported itself blocked, and item-page collection
    looked healthy the entire time because a missing owner is not a parse
    error. Both nestings are read for that reason -- a seller key that is
    absent reads as "no seller", and a seller-level model computed over an
    absent key is a real number computed from nothing.

    The value has shipped both as a scalar and as an object carrying an id,
    so both shapes are accepted; anything else is treated as absent rather
    than coerced, because a key derived from the wrong value would merge
    unrelated sellers into one archetyped, concentrated seller and the error
    would be invisible.

    The raw identifier is read into a local, handed to `privacy.seller_key`,
    and never stored.
    """
    meta = item.get("meta")
    candidates = [meta.get("ownerId")] if isinstance(meta, dict) else []
    candidates += [item.get("ownerId"), item.get("owner_id")]
    for owner in candidates:
        if isinstance(owner, dict):
            owner = owner.get("id")
        if isinstance(owner, bool) or not isinstance(owner, (str, int)):
            continue
        key = privacy.seller_key(owner)
        if key is not None:
            return key
    return None


def build_record(item, listing_id, observed, municipality_norm=None,
                 platform="tori", subplatform="standard"):
    """Project an item-page payload onto a listing record. Allowlist only.

    Everything read here is public and published by the platform for
    third-party consumption. Coordinates and postal codes are absent by
    construction rather than by deletion. `ownerId` is the single identifier
    this module touches, and it is converted to a pseudonymous key before it
    can be stored -- see `privacy.seller_key`.
    """
    for key, kind in (("meta", dict), ("category", dict), ("extras", list), ("images", list),
                      ("title", str), ("disposedText", str), ("disposed", bool)):
        if item.get(key) is not None and not isinstance(item[key], kind):
            raise ParseFailure("invalid item field structure")
    meta = item.get("meta") or {}
    if meta.get("isInactive") is not None and not isinstance(meta["isInactive"], bool):
        raise ParseFailure("invalid item inactivity marker")
    if isinstance(item.get("price"), bool):
        raise ParseFailure("invalid item price")
    root_label, leaf_label, numeric_id = _category_chain(item.get("category"))

    condition_label = _extra_scalar(item, "condition")
    condition_raw, condition_norm = normalise_condition(condition_label)

    meta = item.get("meta") or {}
    disposed = bool(item.get("disposed"))
    disposed_text = item.get("disposedText")
    inactive = bool(meta.get("isInactive"))
    label = (disposed_text or "").strip().lower()
    reserved = label in ("varattu", "reserved")

    outcome_status = "unknown"
    if disposed and not reserved:
        outcome_status = OUTCOME_MAP.get(label, "unknown")
    elif inactive and not reserved:
        outcome_status = "withdrawn"

    price = item.get("price")
    if price is None:
        listed_price_eur = listed_price_original = None
    else:
        listed_price_original = float(price)
        import math
        if not math.isfinite(listed_price_original) or listed_price_original < 0:
            raise ParseFailure("invalid item price")
        listed_price_eur = listed_price_original if listed_price_original > 0 else None

    return {
        "platform": platform,
        "subplatform": subplatform,
        "listing_id": str(listing_id),
        "title": item.get("title"),
        "seller_type_norm": None,          # deliberately not derived from seller pages
        "seller_id": _seller_key_from(item),
        "category_raw_id": numeric_id,
        "category_norm": f"{root_label} > {leaf_label}" if root_label else None,
        "category_root_norm": root_label,
        "category_label_confidence": 1.0 if root_label else 0.0,
        "brand_norm": _extra_scalar(item, "brand"),
        "model_norm": _extra_scalar(item, "model"),
        "field_missing_reasons_json": json.dumps({field:"not_observed" for field in
            ("model_norm", "promotion_active", "shipping_cost_buyer_eur", "buyer_fee_eur")}),
        "condition_raw": condition_raw,
        "condition_norm": condition_norm,
        "condition_label_raw": condition_label,
        "municipality_norm": municipality_norm,
        "region_norm": None,
        "listed_price_original": listed_price_original,
        "currency_original": "EUR",
        "listed_price_eur": listed_price_eur,
        "platform_edited_at": meta.get("edited"),
        "first_seen_at": observed,
        "last_seen_at": observed,
        "observation_coverage_end": observed if outcome_status != "unknown" else None,
        "status_raw": item.get("adViewTypeLabel"),
        "status_norm": "active" if reserved or not (disposed or inactive) else "inactive",
        "coverage": "collecting" if reserved or not (disposed or inactive) else "gone_uncertain",
        "outcome_status": outcome_status,
        # A seller clicking "Myyty" is evidence, not a verified transaction.
        "outcome_confidence": 0.6 if outcome_status == "sold_inferred" else (
            0.5 if outcome_status in ("withdrawn", "expired") else None),
        "outcome_observed_at": observed if outcome_status != "unknown" else None,
        "outcome_label_raw": disposed_text,
        "image_count": len(item.get("images") or []) or None,
        "normalization_version": NORMALIZATION_VERSION,
        "source_observed_at": observed,
        "last_live_at": observed if reserved or not (disposed or inactive) else None,
        "last_probed_at": observed,
        "is_reserved": reserved,
        "is_giveaway": True if price is not None and float(price) == 0 else None,
        "_product_attributes": product_attributes.extract(item),
        "_listing_analysis_input": listing_analysis.prepare(item),
        "_source": "item_page",
    }


def probe(session, listing_id, municipality_norm=None, platform="tori",
          subplatform="standard", max_attempts=None):
    """Fetch one listing and return its record, or None if it has vanished."""
    url = ITEM_URL.format(listing_id=listing_id)
    observed = _utcnow()
    try:
        html_text = session.get_text(
            url, max_attempts=config.ITEM_MAX_ATTEMPTS if max_attempts is None else max_attempts,
            rps=config.ITEM_RPS, retry_on_429=False,
        )
    except HTTPError as exc:
        if exc.code == 404:
            return {"_terminal": "unknown", "_observed": observed, "_parser": "http404"}
        raise
    item = parse_hydration(html_text)
    if item is None:
        # A 200 with no item is normally a listing taken down while its URL still
        # resolves. It is also what a refusal page looks like, and this function
        # returns "withdrawn" for it -- which is a fabricated terminal state
        # feeding straight into the survival analysis. So the two are told apart
        # here, and the ambiguous case stops the run instead of guessing.
        if looks_like_blocked_page(html_text):
            mark_collection_stop(url, "blocked")
            raise AccessBlocked(
                f"item page {listing_id} served a refusal page, not a listing")
        raise ParseFailure(f"unrecognized item page for {listing_id}")
    payload_id = item.get("id") or item.get("adId")
    if payload_id is None and not isinstance(item.get("title"), str):
        raise ParseFailure("item payload lacks recognizable listing fields")
    if payload_id is not None and str(payload_id) != str(listing_id):
        raise ParseFailure("item payload identity mismatch")
    record = build_record(item, listing_id, observed, municipality_norm=municipality_norm,
                          platform=platform, subplatform=subplatform)
    record["_parser"] = "hydration"
    return record


def _terminal_record(probe, row, terminal, platform, subplatform):
    """Keep missing-page evidence separate from a source-labeled exit.

    A 404 supplies no outcome, price or attributes. The merge retains earlier
    descriptive facts and censors unknown disappearance at the last live check.
    """
    observed = probe["_observed"]
    record = {c: None for c in db.OBSERVATION_COLUMNS}
    record.update({
        "platform": row.get("platform", platform),
        "subplatform": row.get("subplatform", subplatform),
        "listing_id": row["listing_id"],
        "first_seen_at": observed,
        "last_seen_at": observed,
        "source_observed_at": observed,
        "status_norm": "inactive",
        "coverage": "gone_uncertain",
        "outcome_status": terminal,
        # Unknown disappearance has no terminal-cause confidence or timestamp.
        "outcome_observed_at": observed if terminal != "unknown" else None,
        "outcome_confidence": 0.5 if terminal != "unknown" else None,
        "normalization_version": NORMALIZATION_VERSION,
        "observation_coverage_end": observed if terminal != "unknown" else None,
        "last_probed_at": observed,
        "is_reserved": False,
        "_source": "item_page",
        "_parser": probe.get("_parser"),
    })
    return record


def record_probe(conn, record, platform="tori", subplatform="standard"):
    """Persist a probe result and emit the state-transition event.

    Terminal states are the whole point of this module, so they are the only
    place it writes events: a listing that moves from active to sold produces
    exactly one `sold_inferred` event no matter how many times it is re-probed.
    """
    listing_id = record.get("listing_id")
    if listing_id is None:
        terminal = record.get("_terminal")
        return None, terminal
    with conn.transaction():
        prior_row = conn.execute("SELECT * FROM listings WHERE platform=? AND listing_id=?",
                                 (record["platform"], listing_id)).fetchone()
        prior = dict(prior_row) if prior_row else None
        pk, _is_new = db.record_observation(record, conn=conn)
        current = conn.execute("SELECT * FROM listings WHERE listing_pk=?", (pk,)).fetchone()
        before = prior["outcome_status"] if prior else "unknown"
        after = current["outcome_status"]
        changed = after != before or (prior is not None and any(
            current[field] != prior[field] for field in ("status_norm", "coverage")))
        if changed:
            db.emit_event(conn, pk, "status_changed",
                          event_at=current["outcome_observed_at"] or record["last_seen_at"],
                          observed_at=record["last_seen_at"], before=before,
                          after=after, confidence=current["outcome_confidence"],
                          payload={"source": record.get("_parser") or "hydration",
                                   "status_norm": current["status_norm"], "coverage": current["coverage"],
                                   "prior_status_norm": prior["status_norm"] if prior else None,
                                   "prior_coverage": prior["coverage"] if prior else None})
        task = record.get("_followup_task")
        if task:
            followup.complete(conn, task["row"], task["token"], record["last_probed_at"],
                              terminal=after != "unknown")
        if record.get("_parser"):
            from .. import parser_health
            parser_health.record(record["platform"], "itempage", record["_parser"],
                                 records=() if record["_parser"] == "http404" else [record], conn=conn)
    record["_status_changed"] = after if after != before and after != "unknown" else None
    return pk, None


def _candidate_ids(limit, only_stale=True, min_age_hours=0.0, platform="tori"):
    """Reserve due repeats, borrow spare slots, and interleave both queues.

    Each queue rotates oldest first. Completeness breaks equal-time ties.
    Explicit all-listings runs retain the operator's staleness override.
    """
    limit = int(limit)
    if limit <= 0:
        return []
    if config.FOLLOWUP_ENABLED and only_stale and platform == "tori":
        return followup.candidates(limit, platform, min_age_hours)
    now = datetime.now(timezone.utc)
    conditions = ["listing_id IS NOT NULL", "platform = ?"]
    params = [platform]
    focused, values = collection.Scope(platform).conditions()
    conditions.extend(focused)
    params.extend(values)
    if only_stale:
        due, values = collection.probe_conditions(now)
        conditions.extend(due)
        params.extend(values)
    if min_age_hours:
        age_field = "first_seen_at" if config.POSTGRES_DSN else "julianday(first_seen_at)"
        age_cutoff = "?" if config.POSTGRES_DSN else "julianday(?)"
        conditions.append(f"{age_field} <= {age_cutoff}")
        params.append((now - timedelta(hours=min_age_hours)).isoformat())
    probe_order = "last_probed_at" if config.POSTGRES_DSN else "julianday(last_probed_at)"
    discovery_order = "first_seen_at" if config.POSTGRES_DSN else "julianday(first_seen_at)"
    query = ("SELECT listing_pk, listing_id, municipality_norm, platform, subplatform, last_probed_at, "
             "category_raw_id, category_norm, category_root_norm "
             "FROM listings WHERE " + " AND ".join(conditions))
    complete = "CASE WHEN listed_price_eur > 0 AND outcome_status='unknown' THEN 0 ELSE 1 END"
    if not only_stale:
        return db.query(query + " ORDER BY CASE WHEN last_probed_at IS NULL THEN 0 ELSE 1 END, "
                        f"{probe_order}, {complete}, {discovery_order}, listing_pk LIMIT ?", params+[limit])
    # Read a bounded prefix of each queue from one snapshot. Arrivals cannot
    # displace a due repeat between the two selections, and no listing is twice.
    with db.read_snapshot():
        repeats = db.query(query + " AND last_probed_at IS NOT NULL "
                           f"ORDER BY {probe_order}, {complete}, {discovery_order}, listing_pk LIMIT ?", params+[limit])
        first = db.query(query + " AND last_probed_at IS NULL "
                         f"ORDER BY {discovery_order}, {complete}, listing_pk LIMIT ?", params+[limit])
    first_count, repeat_count = collection.probe_allocation(limit, len(first), len(repeats))
    first, repeats = first[:first_count], repeats[:repeat_count]
    selected, f, r = [], 0, 0
    while f < first_count or r < repeat_count:
        # Proportional interleaving starts with an overdue repeat and preserves
        # each queue's order, even if collection stops before spending its budget.
        if r < repeat_count and (f >= first_count or r*first_count <= f*repeat_count):
            selected.append(repeats[r])
            r += 1
        else:
            selected.append(first[f])
            f += 1
    return selected


def run(session, budget=None, only_stale=True, min_age_hours=0.0,
        platform="tori", subplatform="standard"):
    """Probe a bounded balanced batch, recording terminal states."""
    budget = int(config.ITEM_BUDGET if budget is None else budget)
    if budget < 1:
        raise ValueError("budget must be positive")
    lock, err = db.acquire_lock("itempage")
    if lock is None:
        log.warning("itempage skipped: %s", err)
        return {"status": "skipped", "reason": err, "probed": 0}
    stats = {"requested": budget, "probed": 0, "live": 0, "sold_inferred": 0,
             "withdrawn": 0, "expired": 0, "errors": 0, "throttled": 0,
             "blocked": 0, "category_filled": 0, "condition_filled": 0}
    stats.update(requests=0, stored=0, first_probe_successes=0, repeat_probe_successes=0,
                 terminal_events=0)
    stats.update(first_delay_hours={}, repeat_lateness_hours={})
    verticals = collection.VerticalCounters(platform)
    stats["by_vertical"] = verticals.values
    stats.update(first_probes_scheduled=0, repeat_probes_scheduled=0,
                 first_probe_attempts=0, repeat_probe_attempts=0,
                 probe_repeat_share=config.PROBE_REPEAT_SHARE)
    started = _utcnow()
    conn = None
    try:
        candidates = _candidate_ids(budget, only_stale=only_stale,
                                    min_age_hours=min_age_hours, platform=platform)
        stats["cohort_admitted"] = getattr(candidates, "admitted", 0)
        def is_repeat(row):
            return bool(row.get("last_success_at")) if "enrollment_id" in row else row["last_probed_at"] is not None
        stats["repeat_probes_scheduled"] = sum(is_repeat(row) for row in candidates)
        stats["first_probes_scheduled"] = len(candidates)-stats["repeat_probes_scheduled"]
        conn = db.connect()
        for row in candidates:
            collection_control.checkpoint()
            token = followup.claim(conn, row) if "enrollment_id" in row else None
            if "enrollment_id" in row and token is None:
                continue
            attempted = _utcnow()
            stats["requests"] += 1
            kind = "repeat" if is_repeat(row) else "first"
            stats[kind + "_probe_attempts"] += 1
            verticals.add(row, requests=1, **{kind + "_probe_attempts": 1})
            try:
                record = probe(session, row["listing_id"],
                               municipality_norm=row.get("municipality_norm"),
                               platform=row.get("platform", platform),
                               subplatform=row.get("subplatform", subplatform),
                               max_attempts=1 if token else config.ITEM_MAX_ATTEMPTS)
            except collection_control.CollectionHalted:
                if token:
                    conn.execute("UPDATE followup_tasks SET state='ready',lease_token=NULL,lease_until=NULL WHERE lease_token=?", (token,))
                raise
            except AuthorityBlocked as exc:
                stats["authority_denied"] = stats.get("authority_denied", 0) + 1
                stats["errors"] += 1
                if token:
                    followup.fail(conn, row, token, "authority")
                verticals.add(row, errors=1)
                log.error("source authority needs review; stopping this run: %s", exc)
                break
            except AccessBlocked as exc:
                from .. import parser_health
                parser_health.failure(platform, "itempage", result="refusal")
                # Refused, not throttled. Nothing is retried and no listing is
                # guessed at; the next run resumes from the database.
                stats["blocked"] += 1
                if token:
                    followup.fail(conn, row, token, "platform_stop")
                verticals.add(row, blocked=1)
                log.error("blocked after %d probes; stopping this run: %s",
                          stats["probed"], exc)
                break
            except HTTPError as exc:
                if token:
                    followup.fail(conn, row, token, "platform_stop" if exc.code in (403, 429) else "http")
                else:
                    db.execute("UPDATE listings SET last_probed_at=? WHERE listing_pk=?",
                               (attempted, row["listing_pk"]))
                stats["errors"] += 1
                verticals.add(row, errors=1)
                if exc.code == 403:
                    mark_collection_stop(ITEM_URL.format(listing_id=row["listing_id"]), "blocked")
                    stats["blocked"] += 1
                    verticals.add(row, blocked=1)
                    break
                if exc.code == 429:
                    # The platform has told us the rate is too high. Stopping is
                    # the compliant response; the next run resumes from here.
                    stats["throttled"] += 1
                    verticals.add(row, throttled=1)
                    log.warning("429 after %d probes; stopping this run", stats["probed"])
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - one bad page must not kill the run
                if isinstance(exc, (ParseFailure, ValueError, TypeError, KeyError)):
                    from .. import parser_health
                    parser_health.failure(platform, "itempage")
                if token:
                    followup.fail(conn, row, token, "parse" if isinstance(exc, (ParseFailure, ValueError)) else "transient")
                else:
                    db.execute("UPDATE listings SET last_probed_at=? WHERE listing_pk=?",
                               (attempted, row["listing_pk"]))
                stats["errors"] += 1
                verticals.add(row, errors=1)
                log.warning("probe %s failed: %s", row["listing_id"], exc)
                continue

            stats["probed"] += 1
            if record.get("_terminal"):
                terminal = record["_terminal"]
                record = _terminal_record(record, row, terminal, platform,
                                         subplatform)

            try:
                if token:
                    record["_followup_task"] = {"row": row, "token": token}
                record_probe(conn, record)
                stats["stored"] += 1
                collection_events.checkpoint("itempage", stats)
                if record.get("_product_attributes") is not None:
                    assertions = record["_product_attributes"]["assertions"]
                    stats["attribute_detail_observations"] = stats.get("attribute_detail_observations", 0)+1
                    stats["attribute_detail_with_assertions"] = stats.get("attribute_detail_with_assertions", 0)+bool(assertions)
                    stats["attribute_assertions"] = stats.get("attribute_assertions", 0)+len(assertions)
                    counts = stats.setdefault("attributes_by_field", {})
                    for assertion in assertions:
                        key = assertion["attribute"]
                        counts[key] = counts.get(key, 0)+1
                stats[kind + "_probe_successes"] += 1
                if token:
                    from .. import collection_health
                    collection_health.record_latency(stats, row, record["last_probed_at"])
                committed = {"stored": 1, kind + "_probe_successes": 1}
                outcome = record.get("outcome_status")
                if outcome in ("sold_inferred", "withdrawn", "expired"):
                    committed[outcome] = 1
                else:
                    committed["live"] = 1
                    if record.get("category_norm"):
                        committed["category_filled"] = 1
                    if record.get("condition_norm"):
                        committed["condition_filled"] = 1
                if record.get("_status_changed"):
                    committed["terminal_events"] = 1
                for key, value in committed.items():
                    if key not in ("stored", kind + "_probe_successes"):
                        stats[key] = stats.get(key, 0) + value
                verticals.add(row, **committed)
            except Exception as exc:  # noqa: BLE001
                if token:
                    followup.fail(conn, row, token, "storage")
                stats["errors"] += 1
                verticals.add(row, errors=1)
                log.warning("record %s failed: %s", row["listing_id"], exc)
    finally:
        if conn is not None:
            conn.close()
        db.release_lock(lock)
    stats["status"] = ("blocked" if stats["blocked"] or stats.get("authority_denied") else "throttled" if stats["throttled"]
                       else "error" if stats["errors"] else "ok")
    db.execute("INSERT INTO ingest_runs (started_at, finished_at, source, status, "
               "rows_ingested, requests_made, error) VALUES (?,?,?,?,?,?,?)",
               (started, _utcnow(), "tori.itempage", stats["status"], stats["stored"],
                stats["requests"], json.dumps(stats) if stats["errors"] else None))
    return stats
