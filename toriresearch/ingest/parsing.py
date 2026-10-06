"""Parsing of Tori's public listing surfaces.

Kept separate from both ingesters because it is the one piece of logic that has
to survive a frontend migration. Tori has changed its URL scheme at least once
since 2020, and every scraper written against the old scheme is broken. The
parsers here are small, dependency-free, and independently testable against a
saved page, so a format change is a localised failure rather than a rewrite.

Two listing sources exist on a search page, and the richer one is not the one
this project was built against:

* the **JSON-LD ItemList**, which is what `extract_itemlist` reads. It carries
  id, title, price and an `itemCondition` token that is `UsedCondition` for
  essentially everything -- by this module's own admission not a measurement.
* the **dehydrated search API response** the page embeds for its own hydration,
  which carries the same listings as `docs[]` and adds, per listing:
  `trade_type`, `flags`, `labels`, `timestamp`, `location`, `price.price_unit`.

`extract_docs` reads the second. It is the same HTTP request and the same
`~50 listings per request` economics, so taking it costs nothing, and it is
where `seller_type_norm` comes from -- the platform's own Yksityinen/Yritys
distinction, which it publishes for every listing and which nothing else on the
page exposes. Before this, that column was null on every row in the database.

One field is deliberately not carried out of this structure, and it is worth
naming rather than leaving as an unexplained gap: `coords` is dropped. The
response includes precise `coordinates` per listing, and they are accurate to
about five metres. A seller's listing is not consent to their home address
being stored, and a coordinate is a far sharper identifier than the coarse
municipality label Tori actually displays. `extract_docs` reads the key and
never returns it -- there is no code path that could persist it.
"""

import base64
import json
import re

from .. import product_attributes, listing_analysis

ITEMLIST_SCRIPT = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
ITEM_URL = re.compile(r"/item/(\d+)")


def _walk_itemlists(node):
    if isinstance(node, dict):
        if "itemListElement" in node:
            yield node
        for value in node.values():
            yield from _walk_itemlists(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_itemlists(value)


def extract_itemlist(html_text):
    """Pull the JSON-LD ItemList Products out of a search page.

    Roughly fifty fully structured listings per HTTP request, which is what
    makes national coverage affordable: one request buys a category cell for a
    municipality, complete with price, brand and a platform condition label.
    """
    items = []
    for block in ITEMLIST_SCRIPT.findall(html_text):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk_itemlists(data):
            for element in node.get("itemListElement") or []:
                item = element.get("item") if isinstance(element, dict) else None
                if isinstance(item, dict) and item.get("offers"):
                    items.append(item)
    return items


def listing_id_from(item):
    match = ITEM_URL.search(item.get("url") or "")
    return match.group(1) if match else None


def search_item_condition(item):
    """The schema.org condition token, untranslated and uninterpreted.

    Tori reports `UsedCondition` for essentially every listing, so this is not
    a condition measurement and is never mapped into the condition enum. The
    platform's real condition word is on the item page and is handled there.
    """
    return (item.get("itemCondition") or "").strip() or None


# ---------------------------------------------------------------------------
# The dehydrated search API response
# ---------------------------------------------------------------------------

PAYLOAD_SCRIPT = re.compile(
    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', re.S | re.I)

# `trade_type` is the platform's own wording. These two are the whole
# distinction: a giveaway is not a EUR 0 sale, and only this field separates
# them reliably. The old heuristic -- a title starting with "annetaan" -- misses
# every giveaway not titled that way, which puts fabricated zeros into a price
# median.
TRADE_TYPE_SALE = ("myydään", "myydaan")
TRADE_TYPE_GIVEAWAY = ("annetaan",)

# `flags`/`labels` carry the platform's commercial distinction. `retailer` is
# its business flag; `private` is its non-business one. Both are published
# classifications about how an account is set up, not about a person, which is
# why reading them needs no seller identifier at all.
FLAG_SELLER_TYPE = {"retailer": "business", "private": "private"}

# ToriDiili is a structurally different market: negotiated prices, a different
# fee structure, different buyer psychology. Pooling it with an ordinary
# listing distorts both, which is why `subplatform` exists at all.
FLAG_SUBPLATFORM = {"fiks_ferdig": "diili"}


def _decode_payloads(html_text):
    """Yield each `application/json` script block as a Python object.

    Two encodings are in use and a page can contain both. The hydration payload
    that carries the facet tree and the search response is base64; smaller
    config blocks are plain JSON. Only decoding base64 silently drops the plain
    ones -- no error, just an absent block -- so both are tried, plain first
    because `json.loads` is unambiguous about rejecting base64.
    """
    for block in PAYLOAD_SCRIPT.findall(html_text):
        block = block.strip()
        if not block or len(block) < 16:
            continue
        try:
            yield json.loads(block)
            continue
        except json.JSONDecodeError:
            pass
        try:
            yield json.loads(base64.b64decode(
                block + "=" * (-len(block) % 4)).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - an undecodable block is not a failure
            continue


_SEARCH_DOC_PREFIX = 5


def _looks_like_search(docs):
    """Whether a prefix of `docs[]` carries the identity marker."""
    for entry in docs[:_SEARCH_DOC_PREFIX]:
        if isinstance(entry, dict) and ("ad_id" in entry or "canonical_url" in entry):
            return True
    return False


def _search_docs(node):
    """Find the dehydrated search response's `docs[]` array.

    Located by shape rather than by path: the hydration envelope's keys are the
    frontend's business and have already changed once. What is stable is that a
    search response has a list of dicts each carrying an `ad_id` and a `price`.

    The shape test scans a bounded prefix of the array rather than `docs[0]`
    alone. Reading only the first element meant one malformed leading row made
    the entire page invisible -- roughly fifty valid listings dropped with no
    error and no row written -- which is the opposite of the localised failure
    this module exists to produce. The prefix is bounded because the array can be
    large and the marker appears on every real row, so a handful is enough to
    tell a search response from an unrelated list.
    """
    if isinstance(node, dict):
        data = node.get("state")
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            docs = data["data"].get("docs")
            if isinstance(docs, list) and (not docs or _looks_like_search(docs)):
                yield docs
        for value in node.values():
            yield from _search_docs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _search_docs(value)


def _flag_map(doc):
    """`flag name -> label text`, merged from `flags` and `labels`.

    `flags` is a bare list of ids; `labels` is the same information with the
    platform's own Finnish wording attached. Both are read because `flags` is
    the more stable of the two and `labels` is the more informative, and neither
    alone covers every flag.
    """
    out = {}
    for label in doc.get("labels") or []:
        if isinstance(label, dict) and label.get("id"):
            out[str(label["id"])] = label.get("text")
    for flag in doc.get("flags") or []:
        if isinstance(flag, str):
            out.setdefault(flag, out.get(flag))
    return out


def _doc_brand(doc):
    """The brand the search response publishes, when the seller supplied one.

    Carried in `extras` as `{"id": "brand", "values": ["Mercedes-Benz"]}` --
    the plural form, unlike the item page's singular `value`. Only 12 of 53
    listings on a saved page had one, because most sellers leave brand blank and
    the platform does not infer it, so this is a genuine observation rather than
    a guess: it is NULL when the seller did not say, not a guess from the title.
    """
    for extra in doc.get("extras") or []:
        if not isinstance(extra, dict) or extra.get("id") != "brand":
            continue
        values = extra.get("values") or ([extra["value"]] if extra.get("value") else [])
        if not isinstance(values, list):
            values = [values]
        parts = [str(v).strip() for v in values if str(v).strip()]
        if parts:
            return ", ".join(parts)
    return None


def _doc_id(doc):
    if not isinstance(doc, dict):
        return None
    value = doc.get("ad_id") or doc.get("id")
    if value is not None and not isinstance(value, bool) and str(value).isdigit():
        return str(value)
    match = ITEM_URL.search(doc.get("canonical_url") or "")
    return match.group(1) if match else None


def doc_record(doc):
    """Project one `docs[]` entry onto the fields the schema already has.

    Allowlist in the same sense `ingest/itempage.py` uses it: this builds a new
    dict from named keys, so a field that is not named here is not stored. That
    is what makes `coordinates` a non-issue -- it is read by nobody, and there
    is no path that could write it.

    **`timestamp` is deliberately NOT read as a publish date.** Measured against
    a saved page: 53 listings spanning ids `2424570` to `47887314` -- roughly six
    years of posting -- all carried timestamps within a few minutes of each
    other and of the fetch itself. A 2020 listing cannot have been published
    seconds ago, so the field is a last-modified / feed-refresh marker that the
    platform bumps on every touch, not the moment the seller posted.

    Writing it into `platform_published_at` would be worse than leaving it NULL.
    The project's own rule is that `first_seen_at` is when *we* observed a
    listing and a platform timestamp must never be confused with it; mapping this
    field to publish time would make every listing look seconds old and collapse
    every duration in the survival analysis to zero. It is returned under its own
    name, `platform_touched_at`, and callers must decide what it is good for.
    """
    flags = _flag_map(doc)
    price = doc.get("price") or {}
    amount = price.get("amount")

    listing_id = _doc_id(doc)
    if listing_id is None:
        return None

    touched_at = None
    timestamp = doc.get("timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        from datetime import datetime, timezone
        touched_at = datetime.fromtimestamp(
            timestamp / 1000.0, tz=timezone.utc).isoformat()

    trade_raw = (doc.get("trade_type") or "").strip()
    trade_key = trade_raw.lower()
    is_giveaway = trade_key in TRADE_TYPE_GIVEAWAY
    # A zero price is corroboration, not a substitute: a giveaway legitimately
    # costs nothing and must still be excluded from a price median.
    zero_price = isinstance(amount, (int, float)) and amount == 0

    seller_type = None
    for flag, mapped in FLAG_SELLER_TYPE.items():
        if flag in flags:
            seller_type = mapped
            break

    subplatform = "standard"
    for flag, mapped in FLAG_SUBPLATFORM.items():
        if flag in flags:
            subplatform = mapped
            break

    location = doc.get("location") or ""
    municipality = location.split(",")[-1].strip() if "," in location else (
        location.strip() or None)

    return {
        "listing_id": str(listing_id),
        "title": doc.get("heading") or None,
        "price_amount": amount if isinstance(amount, (int, float)) else None,
        "currency": price.get("currency_code") or None,
        "platform_touched_at": touched_at,
        "trade_type_raw": trade_raw or None,
        "is_giveaway": bool(is_giveaway or zero_price),
        "seller_type_norm": seller_type,
        "subplatform": subplatform,
        "municipality_norm": municipality,
        "has_buy_now": "buy_now" in flags,
        "has_shipping": "shipping_exists" in flags,
        "image_count": len(doc.get("image_urls") or []) or None,
        "brand": _doc_brand(doc),
        "ad_type": doc.get("ad_type"),
        "_product_attributes": product_attributes.extract(doc, title_field="heading"),
        "_listing_analysis_input": listing_analysis.prepare(doc, title_field="heading"),
    }


def extract_docs(html_text):
    """Every listing on a search page, from the embedded search API response.

    Returns a list of records shaped by `doc_record`. An empty list means this
    page had no embedded response -- a genuinely empty grid cell, or a frontend
    change. Callers fall back to `extract_itemlist`, which is why this returning
    nothing is not fatal.
    """
    out = []
    seen = set()
    for payload in _decode_payloads(html_text):
        for docs in _search_docs(payload):
            for doc in docs:
                record = doc_record(doc) if isinstance(doc, dict) else None
                if not record or record["listing_id"] in seen:
                    continue
                seen.add(record["listing_id"])
                out.append(record)
            if out:
                return out
    return out


def recognised_search_page(html_text):
    """Recognize an empty result using its schema, rather than absence of rows."""
    for payload in _decode_payloads(html_text):
        if any(True for _ in _search_docs(payload)):
            return True
    for block in ITEMLIST_SCRIPT.findall(html_text):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if any(True for _ in _walk_itemlists(payload)):
            return True
    return False


def search_result_shape(html_text, parser=None):
    """Recognized raw result count, so invalid nonempty rows cannot become an empty end."""
    empty, invalid = False, None
    for payload in _decode_payloads(html_text) if parser != "itemlist" else ():
        for docs in _search_docs(payload):
            missing = sum(_doc_id(doc) is None for doc in docs)
            shape = {"parser": "docs", "raw_count": len(docs), "missing_id": missing}
            if missing < len(docs):
                return shape
            if docs:
                invalid = invalid or shape
            else:
                empty = True
    if invalid:
        return invalid
    if empty:
        return {"parser": "docs", "raw_count": 0, "missing_id": 0}
    for block in ITEMLIST_SCRIPT.findall(html_text):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        lists = list(_walk_itemlists(payload))
        if lists:
            if any(not isinstance(node.get("itemListElement"), list) for node in lists):
                raise ValueError("Invalid ItemList result structure")
            elements = [element for node in lists for element in node["itemListElement"]]
            missing = sum(not isinstance(element, dict) or not isinstance(element.get("item"), dict)
                          or listing_id_from(element["item"]) is None for element in elements)
            return {"parser": "itemlist", "raw_count": len(elements), "missing_id": missing}
    return None
