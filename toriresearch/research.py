"""Local analyst cohorts and append-only decisions, separate from observations."""

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

from . import analysis, config, db, economics, metrics, product_identity, purchases, product_attributes

FILTERS = {"platform", "subplatform", "category_root_norm", "category_norm",
           "municipality_norm", "condition_norm", "seller_type_norm", "focus_path"}
CHECKS = {"model", "condition", "completeness", "delivery"}
DECISIONS = {"watch", "inspect", "reject", "pursue"}
SCENARIO_FIELDS = {"resale", "buy", "inbound", "fee", "outbound", "repair",
                   "other", "target", "days", "carrying", "haircut"}
OPERATING_FIELDS = {"labour_hours", "hourly_rate", "packaging", "return_loss", "overhead",
                    "cash_budget", "monthly_hours", "storage_units", "source_units_month", "sale_units_month"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      default=lambda item: item.isoformat())


def _text(value, label, limit, required=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label} must be text of at most {limit} characters")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} is required")
    return value


def clean_filters(filters):
    if not isinstance(filters, dict) or set(filters)-FILTERS:
        raise ValueError("unsupported segment filters")
    clean = {key: _text(value, key, 300) for key, value in filters.items()}
    clean = {key: value for key, value in clean.items() if value}
    clean.setdefault("platform", "tori")
    clean.setdefault("subplatform", "standard")
    if clean["subplatform"] not in config.SUBPLATFORMS:
        raise ValueError("unsupported segment market")
    return clean


def segments():
    rows = db.query("SELECT * FROM research_segments ORDER BY LOWER(name), segment_pk")
    return [{"segment_pk": row["segment_pk"], "name": row["name"],
             "filters": json.loads(row["filters_json"]), "updated_at": row["updated_at"]}
            for row in rows]


def save_segment(name, filters):
    name = _text(name, "segment name", 60, required=True)
    filters = clean_filters(filters)
    conn = db.connect()
    try:
        with conn.transaction():
            if (conn.execute("SELECT COUNT(*) FROM research_segments").fetchone()[0] >= 50
                    and not conn.execute("SELECT 1 FROM research_segments WHERE name=?", (name,)).fetchone()):
                raise ValueError("at most 50 segments can be saved")
            conn.execute("INSERT INTO research_segments (name, filters_json, updated_at) VALUES (?,?,?) "
                         "ON CONFLICT (name) DO UPDATE SET filters_json=excluded.filters_json, updated_at=excluded.updated_at",
                         (name, _json(filters), datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()
    return {"ok": True, "segments": segments()}


def delete_segment(segment_pk):
    if isinstance(segment_pk, bool) or not isinstance(segment_pk, int) or segment_pk < 1:
        raise ValueError("segment ID must be a positive integer")
    db.execute("DELETE FROM research_segments WHERE segment_pk=?", (segment_pk,))
    return {"ok": True, "segments": segments()}


def scenario(values):
    """Freeze explicit assumptions and reproducible cent-rounded arithmetic."""
    if values is None:
        return None
    if not isinstance(values, dict) or set(values) not in (SCENARIO_FIELDS, SCENARIO_FIELDS | OPERATING_FIELDS):
        raise ValueError("a scenario requires every cost assumption and haircut")
    numbers = {}
    for key, value in values.items():
        numbers[key] = economics.amount(value, key)
        if numbers[key] is None:
            raise ValueError(f"{key} must be known")
    if numbers["fee"] >= 100 or numbers["haircut"] > 50:
        raise ValueError("fee must be below 100% and haircut at most 50%")
    if numbers["days"] != numbers["days"].to_integral_value() or numbers["days"] > 36500:
        raise ValueError("holding days must be an integer between 0 and 36500")
    rate = numbers["fee"]/100
    fixed = sum(numbers[key] for key in ("inbound", "outbound", "repair", "other")) + numbers["days"]*numbers["carrying"]
    operating = bool(set(values) & OPERATING_FIELDS)
    if operating:
        fixed += sum(numbers[key] for key in ("packaging", "return_loss", "overhead"))
        fixed += numbers["labour_hours"] * numbers["hourly_rate"]
    downside_sale = numbers["resale"]*(1-numbers["haircut"]/100)
    fields = ("fixed_costs_eur", "selling_fee_eur")
    base = economics.unit_economics(numbers["buy"], numbers["resale"],
        dict(fixed_costs_eur=fixed, selling_fee_eur=numbers["resale"]*rate), required_costs=fields)
    downside = economics.unit_economics(numbers["buy"], downside_sale,
        dict(fixed_costs_eur=fixed, selling_fee_eur=downside_sale*rate), required_costs=fields)
    result = {"version": "cost-scenario-v2" if operating else "cost-scenario-v1", "basis": "user_assumptions",
            "inputs": {key: float(value) for key, value in numbers.items()},
            "results": {"base_profit_eur": base["net_profit_eur"], "downside_profit_eur": downside["net_profit_eur"],
                        "max_buy_eur": economics.money(downside_sale*(1-rate)-fixed-numbers["target"]),
                        "break_even_eur": economics.money((numbers["buy"]+fixed)/(1-rate)),
                        "fixed_costs_eur": economics.money(fixed)}}
    if operating:
        capital = numbers["buy"]+fixed-numbers["labour_hours"]*numbers["hourly_rate"]
        result["operating"] = economics.monthly_capacity(
            capital, numbers["labour_hours"], numbers["days"], numbers["cash_budget"],
            numbers["monthly_hours"], numbers["storage_units"], numbers["source_units_month"],
            numbers["sale_units_month"], downside["net_profit_eur"])
        result["operating"].update(capital_per_unit_eur=economics.money(capital),
                                  downside_covers_target=downside["net_profit_eur"] >= float(numbers["target"]),
                                  profit_basis="downside economic profit after the stated labour cost",
                                  profit_per_labour_hour_eur=round(downside["net_profit_eur"]/float(numbers["labour_hours"]), 2)
                                  if numbers["labour_hours"] else None)
    return result


def _listing(platform, listing_id):
    row = db.query_one("SELECT " + analysis.FIELDS + ", has_shipping, region_norm FROM listings WHERE platform=? AND listing_id=?",
                       (platform, listing_id))
    if not row:
        raise ValueError("choose a listing already stored in this workspace")
    return row


def listing_evidence(platform, listing_id, filters=None, reference_at=None):
    """Current peer context for inspection, without treating broad categories as peers."""
    with db.read_snapshot():
        return _listing_evidence(platform, listing_id, filters, reference_at)


def _listing_evidence(platform, listing_id, filters=None, reference_at=None):
    row = _listing(platform, listing_id)
    row = product_attributes.attach([row])[0]
    reference = analysis.comparison_reference(reference_at)
    if filters is not None:
        filters = clean_filters({"platform": platform, "subplatform": row["subplatform"], **filters})
    cutoff = reference-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    category, condition = row["category_norm"], row["condition_norm"]
    resolved = analysis.resolve_peer_cohort(row, cutoff, filters)
    types = resolved["types"]
    identity = resolved["identities"].get(row["listing_pk"]) or product_identity.resolve_rows([row])[row["listing_pk"]]
    peers = [peer for peer in resolved["peers"] if peer["listing_pk"] != row["listing_pk"]]
    canonical = identity["pilot_scope"]
    canonical_group = resolved["rows"]
    if canonical and identity["product_type"]:
        types[row["listing_pk"]] = identity["product_type"]
        condition = identity["attributes"]["condition"]
    context = analysis.price_panel(peers, cutoff)
    fresh_peers = [peer for peer in peers if analysis.is_priced(peer, cutoff)]
    missing_identity = [field for field in ('brand_norm', 'model_norm') if not row.get(field)]
    evidence = {"captured_at": datetime.now(timezone.utc).isoformat(),
                "subplatform": row["subplatform"],
                "ask_eur": analysis.amount(row["listed_price_eur"]),
                "price_observed_at": row["price_observed_at"] or row["last_seen_at"],
                "fresh_available": analysis.is_fresh(row, cutoff),
                "category_norm": category, "condition_norm": condition,
                "product_type": types.get(row["listing_pk"]),
                "product_type_basis": "derived" if types.get(row["listing_pk"]) else "unknown",
                "brand_norm": row.get("brand_norm"), "model_norm": row.get("model_norm"),
                "requires_variant_verification": True,
                "missing_identity": missing_identity,
                "matching_fields": ['platform', 'subplatform', 'category_norm', 'condition_norm',
                                    'product_type', 'brand_norm', 'model_norm'],
                "peer_items": [{key: peer.get(key) for key in ('listing_pk', 'listing_id', 'platform', 'subplatform',
                               'title', 'listed_price_eur', 'condition_norm', 'brand_norm',
                               'model_norm', 'last_seen_at')}
                               for peer in sorted(fresh_peers, key=lambda peer: (float(peer['listed_price_eur']), peer['listing_pk']))[:8]],
                "peer_items_limit": 8,
                "peer_prices": context, "min_n": config.MIN_N_FOR_REPORT,
                "effective_filters": resolved["filters"], "reference_at": reference.isoformat(),
                "eligibility": analysis.comparison_admission(row, identity, resolved["relationship_admission"]),
                "basis": ("A reviewed product and offer comparison is required; broad labels supply descriptive context only"
                          if types.get(row["listing_pk"]) else
                          "Product identity is unknown; classify and inspect the item before comparing peers")}
    from . import listing_analysis
    evidence["listing_analysis"] = identity.get("listing_analysis") or listing_analysis.attach([row])[0]["listing_analysis"]
    if resolved["key"]:
        evidence.update(analysis.benchmark_context(row, resolved["key"], resolved["peers"], cutoff, resolved["filters"], resolved["identities"]))
    if canonical:
        evidence.update(product_identity=identity,
                        variant_asking_price_context=_variant_context(row, canonical_group, cutoff),
                        missing_identity=identity["missing_variant_fields"]+identity["missing_comparison_fields"],
                        matching_fields=["platform", "subplatform", "category_norm", "canonical_comparison_key"],
                        product_type_basis=identity["basis"],
                        basis="Other fresh asking prices with the same canonical variant and reviewed single-unit offer"
                        if identity["comparison_key"] else "Pilot identity or offer facts are incomplete; no exact-product peers are supplied")
    return {"listing": row, "evidence": evidence}


def _canonical_peers(row, columns):
    """One matching policy for inspection and provenance, independent of LLM labels."""
    identity = product_identity.resolve_rows([row])[row["listing_pk"]]
    if not identity["pilot_scope"]:
        return None
    category = row.get("category_norm")
    group = []
    if category and category != row.get("category_root_norm"):
        group = db.query("SELECT "+columns+" FROM listings WHERE platform=? AND subplatform=? AND category_norm=?",
                         (row["platform"],row["subplatform"],category))
    identities = product_identity.resolve_rows(group+[row])
    peers = [peer for peer in group if peer["listing_pk"] != row["listing_pk"] and identity["comparison_key"]
             and identities[peer["listing_pk"]]["comparison_key"] == identity["comparison_key"]]
    return identity, group, peers


def _variant_context(row, group, cutoff):
    """Variant asking-price research never supplies an exact-offer gap."""
    identities = product_identity.resolve_rows(group+[row])
    subject = identities[row["listing_pk"]]
    peers = [peer for peer in group if peer["listing_pk"] != row["listing_pk"]
             and subject["comparison_levels"]["variant_asking_price_context"]
             and identities[peer["listing_pk"]]["comparison_levels"]["variant_asking_price_context"]
             and identities[peer["listing_pk"]]["variant_key"] == subject["variant_key"]]
    priced = [peer for peer in peers if analysis.is_priced(peer, cutoff)]
    attrs = [identities[peer["listing_pk"]]["attributes"] for peer in priced]
    battery = [item["battery_health_pct"] for item in attrs if item["battery_health_pct"] is not None]
    return dict(level="variant_asking_price_context", n_priced=len(priced), stats=analysis.price_panel(peers, cutoff),
                min_n=config.MIN_N_FOR_REPORT,
                condition_counts={condition: sum((item["condition"] or "unknown") == condition for item in attrs)
                                  for condition in sorted({item["condition"] or "unknown" for item in attrs})},
                battery_health_pct=dict(known=len(battery), unknown=len(priced)-len(battery),
                                        minimum=min(battery) if battery else None, maximum=max(battery) if battery else None),
                supports_exact_offer_gap=False,
                basis="Research asking prices for the same explicit variant and market. Condition, battery health and offer facts may differ or be unknown; no condition adjustment, exact-offer discount, resale proceeds or buying decision follows from this context.")


# ---------------------------------------------------------------------------
# Where a listing's numbers came from
# ---------------------------------------------------------------------------
#
# A shortlist row is a comparison, and a comparison hides its own inputs. The ask
# may have been converted at last month's rate, the category may be an unresolved
# id rather than a label, and the "peers" may be nine search-page results rather
# than item-page fetches. Each of those changes what the number is worth, so the
# provenance panel reads the stored evidence instead of restating the row.

# Operator-facing names for the channels `db.record_observation` accepts.
# `rank` is `db._SOURCE_RANK`, which decides who wins a conflict on a descriptive
# field. It is shown because "which channel wrote this" only means something next
# to "and how far does it outrank the others". A higher rank wins: the feed is
# the weakest channel and your own recorded sale is the strongest.
SOURCE_CHANNELS = {
    "atom": ("Feed sample (Atom)", 0),
    "search_itemlist": ("Search result listing", 1),
    "search_docs": ("Search page JSON-LD", 2),
    "unknown": ("Unattributed", 2),
    "ebay_browse": ("eBay browse API", 2),
    "item_page": ("Item page fetch", 3),
    "own_sale": ("Your own recorded sale", 4),
}

# The columns a provenance read needs. Deliberately wider than `analysis.FIELDS`,
# which carries only what a price comparison touches.
PROVENANCE_COLUMNS = (
    "listing_pk", "platform", "subplatform", "listing_id", "title",
    "listed_price_original", "currency_original", "listed_price_eur", "price_observed_at",
    "fx_rate_to_eur", "fx_rate_date", "fx_source",
    "shipping_cost_buyer_eur", "buyer_fee_eur", "total_buyer_cost_eur", "buy_now_price_eur",
    "promotion_active", "promotion_type",
    "category_raw_id", "category_norm", "category_root_norm", "category_label_confidence",
    "condition_raw", "condition_label_raw", "condition_norm",
    "brand_norm", "model_norm", "municipality_norm", "region_norm",
    "first_seen_at", "last_seen_at", "last_live_at", "last_probed_at", "platform_published_at",
    "source_observed_at", "observation_coverage_end", "outcome_observed_at", "outcome_status",
    "status_norm", "coverage", "normalization_version",
    "field_sources_json", "field_missing_reasons_json",
)

# Enough of a peer to reproduce the comparison and to say where its price came
# from. `status_norm`, `outcome_status`, `coverage`, `is_reserved`, `is_giveaway`
# and `price_observed_at` are load-bearing: they are what `analysis.is_fresh` and
# `analysis.is_priced` read to decide whether a peer counts at all.
_PEER_COLUMNS = ("listing_pk, platform, listing_id, title, category_norm, brand_norm, model_norm, "
                 "listed_price_eur, listed_price_original, currency_original, fx_rate_to_eur, fx_source, "
                 "price_observed_at, last_seen_at, status_norm, outcome_status, coverage, "
                 "is_reserved, is_giveaway, field_sources_json, subplatform, category_root_norm, condition_norm, source_observed_at")

# One row per field the shortlist decision actually leans on, with the reason to
# read it. A provenance panel that lists every column is a schema dump nobody
# reads; this is the subset that can move the number.
PROVENANCE_FIELDS = (
    ("listed_price_eur", "Asking price compared", "money",
     "The figure the gap percentage is calculated from."),
    ("listed_price_original", "Asking price as published", "money",
     "What the seller asked for, in their own currency."),
    ("currency_original", "Published currency", "text",
     "The currency the seller quoted in."),
    ("fx_rate_to_eur", "Rate applied to reach EUR", "rate",
     "A conversion, not an observation. A different rate moves this price."),
    ("fx_rate_date", "Rate date", "text",
     "When the rate was dated."),
    ("fx_source", "Rate source", "text",
     "Which published rate was used."),
    ("shipping_cost_buyer_eur", "Buyer shipping", "money",
     "Not part of the comparison. You pay it on top of the ask."),
    ("buyer_fee_eur", "Buyer fee", "money",
     "Not part of the comparison. You pay it on top of the ask."),
    ("total_buyer_cost_eur", "Total buyer cost", "money",
     "Derived from ask, shipping and fee, and only when all three are known."),
    ("buy_now_price_eur", "Buy-now price", "money",
     "A separate ask that may be cheaper than the listing price."),
    ("promotion_active", "Promotion running", "flag",
     "A promoted listing may have had its ask lowered."),
    ("category_norm", "Category used to group peers", "text",
     "An id is not a label: this is NULL until a page resolved it."),
    ("category_label_confidence", "Category label confidence", "rate",
     "1.0 from an item page, 0.0 for an unresolved id."),
    ("condition_norm", "Condition used to group peers", "text",
     "An unknown condition disqualifies the peer group entirely."),
    ("condition_label_raw", "Condition as published", "text",
     "The platform's own word, untranslated."),
    ("brand_norm", "Brand matched across peers", "text",
     "Null here means peers may match on a missing value."),
    ("model_norm", "Model matched across peers", "text",
     "Null here means peers may match on a missing value."),
    ("municipality_norm", "Municipality", "text",
     "Delivery cost depends on this, and it is not in the comparison."),
)

_EBAY_ITEM_ID = re.compile(r"^(?:v\d+\|)?(\d+)(?:\|\d+)?$")


def _channel(key):
    """Name and precedence for an observation channel, or None if unattributed."""
    if key not in SOURCE_CHANNELS:
        return None
    label, rank = SOURCE_CHANNELS[key]
    return {"key": key, "label": label, "rank": rank}


def _json_map(value):
    """A stored JSON object, or an empty one. Never raises on a legacy row."""
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value):
    return analysis.amount(value)


def _mix(values, absent):
    """Counts per distinct value, most frequent first.

    `absent` names the bucket for a value that is not there, and it is a
    parameter rather than a constant because "not recorded" and "not
    applicable" are different facts. Reporting a euro price's missing rate
    source as `unknown` invents a provenance problem for a price that needed no
    conversion at all, and a panel that cries wolf is a panel nobody reads.
    """
    counts = {}
    for value in values:
        key = value if value not in (None, "") else absent
        counts[key] = counts.get(key, 0) + 1
    return [{"value": key, "n": count}
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))]


# The three buckets a stored value can fall into, named once so the panel and the
# warnings cannot disagree about what "missing" meant.
ABSENT_UNATTRIBUTED = "unattributed"
ABSENT_NOT_RECORDED = "not_recorded"
ABSENT_NO_CONVERSION = "no_conversion_applied"
ABSENT_UNKNOWN_SOURCE = "unknown_source"


def _fx_key(row):
    """Name a rate's provenance without inventing a missing one.

    A price already in euro has no rate and no rate source, and that is not a gap.
    A price converted without a recorded source is a real gap, and is reported as
    one.
    """
    if row.get("fx_source"):
        return row["fx_source"]
    return ABSENT_UNKNOWN_SOURCE if row.get("fx_rate_to_eur") is not None else ABSENT_NO_CONVERSION


def _field_value(kind, raw):
    """Coerce a stored value by its declared kind, once, for every reader.

    Append-only history is serialised with `default=str`, so a boolean arrives as
    `"False"`. That string is truthy, and a stored "no" rendered as "Yes" is the
    worst kind of wrong on a panel whose entire purpose is to be believed. Money
    and rates are coerced through `_amount`; text is left as the platform wrote
    it; an unparseable amount is None rather than an exception.
    """
    if raw is None or raw == "":
        return None
    if kind in ("money", "rate"):
        try:
            return _number(raw)
        except (TypeError, ValueError):
            return None
    if kind == "flag":
        if isinstance(raw, str):
            return raw.strip().lower() in ("true", "1", "yes")
        return bool(raw)
    return raw


def _field_writers(observations):
    """Which channels actually supplied each field, newest first.

    `field_sources_json` records only the channel that won the last conflict, so a
    field three channels have read shows one name. `listing_observations` keeps
    what each channel really supplied, which is the history the winner column
    cannot show and the only way to tell "read once" from "agreed by three".

    Built from rows already read for the timeline, so this costs no extra query.
    """
    kinds = {field: kind for field, _label, kind, _note in PROVENANCE_FIELDS}
    writers = {}
    for entry in observations:
        for field, kind in kinds.items():
            if field not in entry["supplied"]:
                continue
            writers.setdefault(field, []).append(
                {"channel": _channel(entry["source"]), "observed_at": entry["observed_at"],
                 "kind": kind, "value": _field_value(kind, entry["supplied"][field])})
    return writers




def market_url(platform, listing_id):
    """The public listing page, when the platform's own id is enough to build one.

    No URL is stored, so this is reconstructed from the published id format. When
    that format is not known for a platform the answer is None rather than a
    plausible-looking link that 404s, because a wrong provenance link is worse
    than a missing one.
    """
    value = str(listing_id or "")
    if platform == "tori" and value.isdigit():
        from .ingest.itempage import ITEM_URL
        return ITEM_URL.format(listing_id=value)
    if platform == "ebay":
        # Browse-API item ids are `v1|<numeric>|<batch>`; only the numeric part
        # is the public listing id that appears in a URL.
        match = _EBAY_ITEM_ID.match(value)
        if match:
            return f"https://www.ebay.com/itm/{match.group(1)}"
    return None


def _peer_provenance(row, cutoff, filters=None):
    """The comparison set behind a shortlist row, and each peer's own price source.

    The peer set is rebuilt with exactly the rules `_listing_evidence` uses, so the
    panel cannot describe a different group from the one that produced the gap
    percentage. The extra per-peer columns are the whole point: a median of
    twelve search-page prices and a median of twelve item-page prices are the same
    number and mean different things.
    """
    category, condition = row["category_norm"], row["condition_norm"]
    resolved = analysis.resolve_peer_cohort(row, cutoff, filters, fields=_PEER_COLUMNS)
    group = resolved["rows"]
    product = resolved["identities"].get(row["listing_pk"]) or product_identity.resolve_rows([row])[row["listing_pk"]]
    canonical = product["pilot_scope"]
    peers = [peer for peer in resolved["peers"] if peer["listing_pk"] != row["listing_pk"]]
    if canonical:
        condition = product["attributes"]["condition"]
    # The subject is always part of the derivation lookup: it is what decides the
    # label the peers have to share, and it is the one whose derivation is shown.
    evidence = analysis.product_type_evidence(group + [row])
    types = {pk: entry["product_type"] for pk, entry in evidence.items() if entry["accepted"]}
    label = types.get(row["listing_pk"])
    if canonical:
        label = product["product_type"]
    priced = [peer for peer in peers if analysis.is_priced(peer, cutoff)]
    observed = sorted(str(peer["price_observed_at"]) for peer in priced if peer["price_observed_at"])
    # What the peer match actually rests on, field by field. A group held together
    # by two recorded fields and two nulls is not the same claim as one held by
    # four recorded fields, and the panel has to be able to say which it is.
    identity = [
        {"field": "platform", "label": "Marketplace", "state": "matched",
         "subject": f"{row['platform']} · {row['subplatform']}"},
        {"field": "category_norm", "label": "Detailed category", "state": "matched",
         "subject": category or "not recorded"},
        {"field": "condition_norm", "label": "Condition", "state": "matched",
         "subject": condition or "not recorded"},
        {"field": "product_type", "label": "Derived product type",
         "state": "matched" if label else "unavailable", "subject": label or "no accepted classification"},
        {"field": "brand_norm", "label": "Brand",
         "state": "unknown" if not row.get("brand_norm") else "matched",
         "subject": row.get("brand_norm") or "not recorded"},
        {"field": "model_norm", "label": "Model",
         "state": "unknown" if not row.get("model_norm") else "matched",
         "subject": row.get("model_norm") or "not recorded"},
    ]
    unknowns = [item["label"] for item in identity if item["state"] == "unknown"]
    if not label:
        basis = ("No product type was accepted for this listing, so no peer group could be formed. "
                 "Classify and inspect the item before comparing it to anything.")
    elif not unknowns:
        basis = ("Other fresh asking prices in the same detailed category, condition and accepted "
                 "product type, matched on recorded brand and model. Variants require inspection; "
                 "asking prices are not sale proceeds.")
    else:
        basis = ("Other fresh asking prices in the same detailed category, condition and accepted "
                 f"product type. Matching rests on {len(identity) - len(unknowns)} recorded fields "
                 f"and an unrecorded {unknowns[0]}"
                 + "".join(f" and an unrecorded {name}" for name in unknowns[1:])
                 + ", so peers may share a missing value rather than the same product.")
    if canonical:
        identity = [dict(field="canonical_comparison_key", label="Canonical variant and reviewed offer",
                         state="matched" if product["comparison_key"] else "unavailable", subject=product["model"])]
        unknowns = product["missing_variant_fields"]+product["missing_comparison_fields"]
        basis = ("Other fresh asking prices with the same detailed category, market, canonical variant and reviewed offer. Asking prices are not sale proceeds."
                 if product["comparison_key"] else "Canonical variant or offer facts are incomplete. No exact-product peers are supplied.")
    return {
        "basis": basis,
        "product_identity": product if canonical else None,
        "variant_asking_price_context": _variant_context(row, group, cutoff) if canonical else None,
        "eligibility": analysis.comparison_admission(row, product, resolved["relationship_admission"]),
        "effective_filters": resolved["filters"],
        **(analysis.benchmark_context(row, resolved["key"], resolved["peers"], cutoff, resolved["filters"], resolved["identities"])
           if resolved["key"] else {}),
        "matched_on": identity,
        "unknown_identity": unknowns,
        "product_type": label,
        "n_group": len(group),
        "n_peers": len(peers),
        "n_priced": len(priced),
        "min_n": config.MIN_N_FOR_REPORT,
        "freshness_hours": config.PANEL_MAX_AGE_HOURS,
        "stats": analysis.price_panel(peers, cutoff),
        "price_sources": _mix((_json_map(peer.get("field_sources_json")).get("listed_price_eur")
                               for peer in priced), ABSENT_UNATTRIBUTED),
        "currencies": _mix((peer.get("currency_original") for peer in priced),
                           ABSENT_NOT_RECORDED),
        "fx_sources": _mix((_fx_key(peer) for peer in priced), ABSENT_UNKNOWN_SOURCE),
        "oldest_price_observed_at": observed[0] if observed else None,
        "newest_price_observed_at": observed[-1] if observed else None,
        "samples": [{"listing_pk": peer["listing_pk"], "listing_id": peer["listing_id"],
                     "title": peer["title"], "ask_eur": _number(peer["listed_price_eur"]),
                     "ask_original": _number(peer["listed_price_original"]),
                     "currency_original": peer["currency_original"],
                     "fx_rate_to_eur": _number(peer["fx_rate_to_eur"]),
                     "fx_source": peer["fx_source"],
                     "price_observed_at": peer["price_observed_at"],
                     "channel": _channel(_json_map(peer.get("field_sources_json")).get("listed_price_eur"))}
                    for peer in sorted(priced, key=lambda peer: (float(peer["listed_price_eur"]),
                                                                  peer["listing_pk"]))[:8]],
        "classification": evidence.get(row["listing_pk"]),
    }


def listing_provenance(platform, listing_id, filters=None, reference_at=None):
    """Every stored input behind one listing's price and its peer comparison."""
    platform = _text(platform, "platform", 40, required=True)
    listing_id = _text(listing_id, "listing ID", 100, required=True)
    with db.read_snapshot():
        return _listing_provenance(platform, listing_id, filters, reference_at)


def _listing_provenance(platform, listing_id, filters=None, reference_at=None):
    row = db.query_one("SELECT " + ", ".join(PROVENANCE_COLUMNS)
                       + " FROM listings WHERE platform=? AND listing_id=?", (platform, listing_id))
    if not row:
        raise ValueError("choose a listing already stored in this workspace")
    now = analysis.comparison_reference(reference_at)
    if filters is not None:
        filters = clean_filters({"platform": platform, "subplatform": row["subplatform"], **filters})
    cutoff = now-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    sources = _json_map(row.get("field_sources_json"))
    reasons = _json_map(row.get("field_missing_reasons_json"))

    # The retained price record. `price_history` is the durable one: an interval
    # is written whenever the ask or a cost component moves, and it is present for
    # nearly every listing that ever had a price. `price_observations` is the
    # repeat-sighting record and covers far fewer listings, so it is supplementary
    # evidence rather than the primary record.
    history = db.query(
        "SELECT valid_from, valid_to, listed_price_eur, listed_price_original, currency_original, "
        "fx_rate_to_eur, fx_source, fx_rate_date FROM price_history WHERE listing_pk=? "
        "ORDER BY valid_from DESC LIMIT 8", (row["listing_pk"],))
    n_history = db.scalar("SELECT COUNT(*) FROM price_history WHERE listing_pk=?",
                          (row["listing_pk"],)) or 0
    measurements = db.query(
        "SELECT observed_at, price_observed_at, source, listed_price_eur, listed_price_original, "
        "currency_original FROM price_observations WHERE listing_pk=? ORDER BY observed_at DESC LIMIT 12",
        (row["listing_pk"],))
    n_measurements = db.scalar("SELECT COUNT(*) FROM price_observations WHERE listing_pk=?",
                               (row["listing_pk"],)) or 0

    # Append-only observations, retained from the earliest one this listing has.
    # Ordered by the unique index's leading column, so the earliest is free.
    observations = [{"observed_at": entry["observed_at"], "source": entry["source"],
                     "normalization_version": entry["normalization_version"],
                     "supplied": _json_map(entry["fields_json"])}
                    for entry in db.query(
                        "SELECT observed_at, source, normalization_version, fields_json "
                        "FROM listing_observations WHERE listing_pk=? ORDER BY observed_at DESC LIMIT 12",
                        (row["listing_pk"],))]
    n_observations = db.scalar("SELECT COUNT(*) FROM listing_observations WHERE listing_pk=?",
                               (row["listing_pk"],)) or 0
    oldest_observation = (db.query_one("SELECT MIN(observed_at) AS earliest FROM listing_observations "
                                       "WHERE listing_pk=?", (row["listing_pk"],)) or {}).get("earliest")
    writers = _field_writers(observations)

    fields = []
    for column, label, kind, note in PROVENANCE_FIELDS:
        value = _field_value(kind, row.get(column))
        key = sources.get(column)
        history_for_field = writers.get(column, [])
        fields.append({"field": column, "label": label, "kind": kind, "note": note,
                       "value": value, "channel": _channel(key),
                       "missing_reason": None if value is not None else reasons.get(column),
                       # Every channel that supplied this value, not only the one
                       # that won the last conflict. Agreement between channels is
                       # evidence; a single reader is not.
                       "writers": [{"channel": hit["channel"], "observed_at": hit["observed_at"],
                                    "kind": hit["kind"], "value": hit["value"]}
                                   for hit in history_for_field[:6]],
                       "n_writers": len(history_for_field)})

    peers = _peer_provenance(row, cutoff, filters)
    by_field = {field["field"]: field for field in fields}
    timeline = [{"observed_at": hit["observed_at"],
                 "normalization_version": hit["normalization_version"],
                 "channel": _channel(hit["source"]),
                 "n_fields_supplied": len(hit["supplied"]),
                 "ask_original": _number(hit["supplied"].get("listed_price_original")),
                 "currency_original": hit["supplied"].get("currency_original"),
                 "ask_eur": _number(hit["supplied"].get("listed_price_eur")),
                 "fx_rate_to_eur": _number(hit["supplied"].get("fx_rate_to_eur")),
                 "fx_source": hit["supplied"].get("fx_source")}
                for hit in observations]
    search_sources = [{"search_url": hit["search_url"], "location_label": hit["location_label"],
                       "location_grain": hit["location_grain"], "category_path": hit["category_path"],
                       "first_seen_at": hit["first_seen_at"], "last_seen_at": hit["last_seen_at"]}
                      for hit in db.query(
                          "SELECT a.first_seen_at, a.last_seen_at, t.location_label, t.location_grain, "
                          "t.category_path, t.search_url FROM listing_search_attributions a "
                          "JOIN sweep_targets t ON t.target_pk=a.target_pk WHERE a.listing_pk=? "
                          "ORDER BY a.last_seen_at DESC LIMIT 6", (row["listing_pk"],))]

    coverage = {
        # What is retained, not what happened. Absence below `retained` is not
        # evidence that nothing was observed; it is evidence that nothing was
        # written down, and the panel has to say which of the two it is showing.
        "observations": {"n": n_observations, "shown": len(observations),
                         "retained_since": oldest_observation},
        "price_intervals": {"n": n_history, "shown": len(history)},
        "price_measurements": {"n": n_measurements, "shown": len(measurements)},
        "search_attributions": {"n": len(search_sources)},
        "fields_with_channel": sum(1 for field in fields if field["channel"]),
        "fields_total": len(fields),
        "fields_with_history": sum(1 for field in fields if field["n_writers"]),
    }

    url = market_url(row["platform"], row["listing_id"])
    price_block = {
        "ask_eur": _number(row["listed_price_eur"]),
        "ask_original": _number(row["listed_price_original"]),
        "currency_original": row["currency_original"],
        "fx_rate_to_eur": _number(row["fx_rate_to_eur"]),
        "fx_rate_date": str(row["fx_rate_date"]) if row["fx_rate_date"] is not None else None,
        "fx_source": row["fx_source"],
        "price_observed_at": row["price_observed_at"],
        "channel": _channel(sources.get("listed_price_eur")),
        "writers": by_field["listed_price_eur"]["writers"],
        "n_writers": by_field["listed_price_eur"]["n_writers"],
        "n_measurements": n_measurements, "measurements_shown": len(measurements),
        "measurements": [{"observed_at": hit["observed_at"],
                          "price_observed_at": hit["price_observed_at"],
                          "ask_eur": _number(hit["listed_price_eur"]),
                          "ask_original": _number(hit["listed_price_original"]),
                          "currency_original": hit["currency_original"],
                          "channel": _channel(hit["source"])} for hit in measurements],
        "history": [{"valid_from": hit["valid_from"], "valid_to": hit["valid_to"],
                     "ask_eur": _number(hit["listed_price_eur"]),
                     "ask_original": _number(hit["listed_price_original"]),
                     "currency_original": hit["currency_original"],
                     "fx_rate_to_eur": _number(hit["fx_rate_to_eur"]),
                     "fx_source": hit["fx_source"],
                     "fx_rate_date": str(hit["fx_rate_date"]) if hit["fx_rate_date"] is not None else None}
                    for hit in history],
        "n_intervals": n_history,
    }

    payload = {
        "captured_at": now.isoformat(),
        "listing": {key: row.get(key) for key in ("listing_pk", "platform", "subplatform",
                                                  "listing_id", "title")},
        "market_url": url,
        "market_url_note": (None if url else
                            "No public listing page is reconstructed for this platform. The stored "
                            "listing id is the only marketplace identity recorded."),
        "identity": {"first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
                     "source_observed_at": row["source_observed_at"],
                     "platform_published_at": row["platform_published_at"],
                     "last_live_at": row["last_live_at"], "last_probed_at": row["last_probed_at"],
                     "observation_coverage_end": row["observation_coverage_end"],
                     "outcome_observed_at": row["outcome_observed_at"],
                     "status_norm": row["status_norm"], "outcome_status": row["outcome_status"],
                     "coverage": row["coverage"],
                     "normalization_version": row["normalization_version"]},
        "price": price_block,
        "fields": fields,
        "product_attributes": product_attributes.attach([row])[0]["product_attribute_evidence"],
        "timeline": timeline,
        "search_sources": search_sources,
        "peers": peers,
        "coverage": coverage,
        "note": ("Asking prices are not sale proceeds. The peer median is only as comparable as the "
                 "recorded category, condition, product type and brand/model above, and no figure "
                 "on this panel is an assumption."),
    }
    payload["warnings"] = _provenance_warnings(row, payload, cutoff, config.MIN_N_FOR_REPORT)
    payload["revision"] = _provenance_revision(row, payload)
    return payload


def _provenance_warnings(row, payload, cutoff, min_n):
    """What this panel cannot vouch for, named before any figure is read.

    A provenance view that only lists what it found is a marketing page. Each
    warning is a fact about the evidence, phrased so the reader knows whether the
    fix is collecting more, waiting longer, or inspecting the item.
    """
    price, peers, coverage = payload["price"], payload["peers"], payload["coverage"]
    found = []

    def warn(code, severity, message):
        found.append({"code": code, "severity": severity, "message": message})

    if price["ask_eur"] is None:
        warn("no_price", "high", "No euro asking price is stored. Nothing on this panel is comparable.")
    if price["n_measurements"] == 0 and price["n_intervals"]:
        warn("price_not_resighted", "medium",
             "This ask was never re-sighted. The retained price record holds "
             f"{price['n_intervals']} interval(s) but no repeat measurement, so the price is a single "
             "sighting rather than a confirmed value.")
    if price["n_intervals"] == 0 and price["n_measurements"] == 0 and price["ask_eur"] is not None:
        warn("no_price_record", "high",
             "A price is stored but no price history or measurement backs it. The figure cannot be traced "
             "to the observation that set it.")
    if price["fx_rate_to_eur"] is not None and not price["fx_source"]:
        warn("conversion_without_source", "high",
             "A currency conversion was applied without a recorded rate source, so the euro figure cannot "
             "be reproduced.")
    if price["currency_original"] and price["currency_original"] != "EUR" and price["fx_rate_to_eur"] is None:
        warn("unconverted_price", "high",
             f"The seller quoted {price['currency_original']} and no euro conversion is stored. The price "
             "shown is not comparable with euro peer prices.")
    observed = metrics.parse_ts(price["price_observed_at"])
    if observed and observed < cutoff:
        warn("stale_price", "medium",
             "The asking price was last observed outside the current freshness window, so it may no longer "
             "be the price on sale.")
    if peers["unknown_identity"]:
        if peers.get("product_identity"):
            warn("unknown_identity_match", "high", "Exact-product comparison is withheld. Missing variant or offer facts: "
                 + ", ".join(peers["unknown_identity"])+".")
        else:
            warn("unknown_identity_match", "high",
                 "The peer group was matched with an unrecorded "
                 + (" and an unrecorded ".join(peers["unknown_identity"]))
                 + ", so peers may share a missing value rather than the same product.")
    if not peers["product_type"]:
        warn("no_product_type", "high",
             "No product type was accepted, so this listing has no peer group at all.")
    if peers["n_priced"] < min_n:
        warn("thin_peer_support", "high",
             f"Only {peers['n_priced']} priced peers support this comparison against a minimum of {min_n}. "
             "No median is reportable.")
    if len(peers["price_sources"]) > 1:
        warn("mixed_peer_sources", "low",
             "Peer prices come from more than one channel: "
             + ", ".join(f"{item['value']} ×{item['n']}" for item in peers["price_sources"])
             + ". The median mixes reading quality.")
    if any(item["value"] == ABSENT_UNKNOWN_SOURCE for item in peers["fx_sources"]):
        warn("peer_rate_source_unknown", "medium",
             "At least one counted peer was converted without a recorded rate source.")
    if coverage["observations"]["n"] == 0:
        warn("no_retained_observations", "low",
             "No append-only observation history is retained for this listing, so which channels have "
             "read it cannot be established. Only the winning channel per field is known.")
    elif coverage["fields_with_history"] == 0:
        warn("no_field_history", "low",
             "Observations are retained but none of them recorded the fields shown here, so the current "
             "values predate the retained history.")
    if coverage["search_attributions"]["n"] == 0:
        warn("no_search_attribution", "low",
             "No stored search cell returned this listing. How collection reached it is not recorded.")
    return found


def _provenance_revision(row, payload):
    """A stable fingerprint of everything this panel reads.

    The dialog polls while it is open, and the collector keeps writing. This is
    what lets the client re-render only when a number actually moved: it covers
    the subject's observation state, the price records, and the peer set's size
    and newest price sighting. `captured_at` is deliberately excluded, because a
    payload that changed only in its own timestamp is not a change.
    """
    parts = [str(row.get(key) or "") for key in ("last_seen_at", "price_observed_at",
                                                 "first_seen_at", "coverage", "outcome_status")]
    parts += [str(payload["coverage"]["price_intervals"]["n"]),
              str(payload["coverage"]["price_measurements"]["n"]),
              str(payload["coverage"]["observations"]["n"])]
    peers = payload["peers"]
    parts += [str(peers["n_group"]), str(peers["n_peers"]), str(peers["n_priced"]),
              str(peers["newest_price_observed_at"]), str(peers.get("product_type"))]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def record_decision(client_token, platform, source_listing_id, decision,
                    notes="", checks=None, assumptions=None, filters=None,
                    reference_at=None, benchmark_revision=None, scenario_context=None):
    try:
        token = str(uuid.UUID(client_token))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("a UUID submission token is required")
    platform = _text(platform, "platform", 40, required=True)
    source_listing_id = _text(source_listing_id, "source listing ID", 100, required=True)
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise ValueError("unsupported research decision")
    notes = _text(notes, "decision notes", 2000)
    if checks is None:
        checks = {}
    if not isinstance(checks, dict) or set(checks)-CHECKS or any(not isinstance(value, bool) for value in checks.values()):
        raise ValueError("inspection checks must be supported boolean fields")
    checks = {key: checks.get(key, False) for key in sorted(CHECKS)}
    frozen = scenario(assumptions)
    if benchmark_revision is not None:
        benchmark_revision = _text(benchmark_revision, "benchmark revision", 64, required=True)
    request = dict(platform=platform, source_listing_id=source_listing_id, decision=decision,
                   notes=notes, checks=checks, scenario=frozen)
    if scenario_context is not None:
        request['scenario_context'] = scenario_context
    if filters is not None or reference_at is not None or benchmark_revision is not None:
        filters = clean_filters({} if filters is None else filters)
        request["comparison"] = dict(filters=filters, reference_at=reference_at,
                                     benchmark_revision=benchmark_revision)
    digest = hashlib.sha256(_json(request).encode()).hexdigest()
    existing = db.query_one("SELECT decision_pk, request_sha256 FROM research_decisions WHERE client_token=?", (token,))
    if existing:
        if existing["request_sha256"] != digest:
            raise ValueError("submission token was already used for a different decision")
        return {"ok": True, "decision_pk": existing["decision_pk"], "replayed": True}
    if scenario_context is not None:
        from . import scenario_workspace
        frozen = scenario_workspace.freeze_decision(assumptions, scenario_context, platform, source_listing_id)
    context = listing_evidence(platform, source_listing_id, filters, reference_at)
    if benchmark_revision and context["evidence"].get("benchmark_revision") != benchmark_revision:
        raise ValueError("The benchmark changed; refresh the listing evidence before saving this decision")
    from . import opportunities
    context["evidence"]["decision_contract"] = {
        "acquisition_scope": opportunities.acquisition_scope(context["listing"]),
        "version": "owned-decision-v1", "identity": context["evidence"].get("product_identity"),
        "comparison_policy_version": product_identity.COMPARISON_POLICY_VERSION,
        "acquisition_ask_eur": context["evidence"].get("ask_eur"),
        "buy_ceiling_eur": frozen["results"]["max_buy_eur"] if frozen else None,
        "cost_assumptions": frozen, "forecast_version": None,
        "forecast_basis": "Explicit user scenario" if frozen else "No scenario supplied",
    }
    conn = db.connect()
    try:
        with conn.transaction():
            if scenario_context is not None:
                scenario_workspace.freeze_decision(assumptions, scenario_context, platform, source_listing_id)
            conn.execute("INSERT INTO research_decisions (client_token, request_sha256, recorded_at, listing_pk, "
                         "platform, source_listing_id, decision, notes, checks_json, scenario_json, evidence_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (client_token) DO NOTHING",
                         (token, digest, datetime.now(timezone.utc).isoformat(), context["listing"]["listing_pk"],
                          platform, source_listing_id, decision, notes, _json(checks),
                          _json(frozen) if frozen else None, _json(context["evidence"])))
            saved = conn.execute("SELECT decision_pk, request_sha256 FROM research_decisions WHERE client_token=?", (token,)).fetchone()
            if saved[1] != digest:
                raise ValueError("submission token was already used for a different decision")
            return {"ok": True, "decision_pk": saved[0], "replayed": False}
    finally:
        conn.close()


def journal(decision="", page=1, decision_pk=None):
    with db.read_snapshot():
        return _journal(decision, page, decision_pk)


def _profit_comparison(item, attribution, timing_known, now):
    """Compare one attributed, reviewed cash outcome to its frozen scenario."""
    scenario = item["scenario"]
    outcome = item["owned_outcome"] or {}
    linked = bool(attribution.get("status") == "explicit"
                  and attribution["purchase_pk"] == item["purchase_pk"]
                  and attribution.get("decision_pk") == item["decision_pk"])
    result = dict(explicit_acquisition_link=linked, linked_decision_pk=attribution.get("decision_pk"),
                  acquisition_attribution=attribution, comparison_profit_eur=None,
                  profit_vs_assumption_eur=None, profit_comparison_basis=None)
    def withheld(status, note):
        return dict(result, profit_comparison_status=status, profit_comparison_note=note)
    if not scenario:
        return withheld("no_scenario", "No saved cost scenario is available for comparison")
    if scenario.get("version") == "cost-scenario-v2":
        return withheld("scope_not_comparable", "Recorded ledger costs do not establish the operating scenario's labour and overhead scope. Review those costs before comparing economic profit.")
    if attribution.get("status") == "invalid":
        return withheld("invalid_acquisition_link", attribution["reason"])
    if not linked:
        if attribution.get("status") == "explicit":
            return withheld("other_decision", "The acquisition receipt attributes this purchase to another decision; its ledger result is context here")
        return withheld("unattributed_acquisition", "No matching acquisition receipt attributes this purchase to the decision")
    if not timing_known:
        return withheld("timing_unknown", "Decision, acquisition and final outcome chronology needs review")
    if outcome.get("stock_phase") not in ("sold", "written_off"):
        return withheld("outcome_pending", "The attributed physical unit has no final owned outcome")
    evidence = outcome.get("financial_entries", []) + outcome.get("events", [])
    for row in evidence:
        effective, recorded = metrics.parse_ts(row.get("effective_at")), metrics.parse_ts(row.get("recorded_at"))
        if not effective or not recorded or not effective <= recorded <= now:
            return withheld("timing_unknown", "Owned cost and scope evidence chronology is missing, inconsistent or future-dated")
    if outcome["stock_phase"] == "written_off":
        purchase = metrics.parse_ts(item["purchased_at"])
        write_offs = [metrics.parse_ts(row["effective_at"]) for row in outcome.get("financial_entries", [])
                      if row["kind"] == "write_off"]
        if not purchase or not write_offs or any(stamp < purchase for stamp in write_offs):
            return withheld("timing_unknown", "The final write-off chronology needs review")
    profit = outcome.get("cash_contribution_eur")
    if profit is None:
        return withheld("costs_incomplete", "Whole-unit cash costs or the sale/return lifecycle remain incomplete")
    if not outcome.get("time_scope_complete"):
        return withheld("cost_scope_unreviewed", "Review the current whole-unit cost and work scope before comparing cash profit")
    scores = scenario.get("results")
    base = scores.get("base_profit_eur") if isinstance(scores, dict) else None
    try:
        base = float(base) if isinstance(base, (int, float)) and not isinstance(base, bool) else None
    except (TypeError, ValueError, OverflowError):
        base = None
    if base is not None and not math.isfinite(base):
        base = None
    if scenario.get("version") != "cost-scenario-v1" or base is None:
        return withheld("scenario_unknown", "The saved cash scenario or its base profit is unrecorded or invalid")
    return dict(result, comparison_profit_eur=profit, profit_vs_assumption_eur=round(profit-base, 2),
                profit_comparison_status="available", profit_comparison_basis="reviewed_whole_unit_cash_vs_saved_scenario",
                profit_comparison_note="Observed cash comparison for this linked acquisition; forecast validation requires an independent forward holdout")


def _journal(decision="", page=1, decision_pk=None):
    if decision and decision not in DECISIONS:
        raise ValueError("unsupported journal filter")
    try:
        valid_page = not isinstance(page, bool) and str(page) == str(int(page)) and int(page) > 0
    except (ValueError, TypeError, OverflowError):
        valid_page = False
    if not valid_page:
        raise ValueError("journal page must be a positive integer")
    params, where = ([decision], "WHERE d.decision=?") if decision else ([], "")
    if decision_pk is not None:
        try:
            valid_pk = not isinstance(decision_pk, bool) and str(decision_pk) == str(int(decision_pk)) and int(decision_pk) > 0
        except (ValueError, TypeError, OverflowError):
            valid_pk = False
        if not valid_pk:
            raise ValueError("decision ID must be a positive integer")
        where += (" AND " if where else "WHERE ") + "d.decision_pk=?"
        params.append(int(decision_pk))
    total = db.scalar("SELECT COUNT(*) FROM research_decisions d " + where, params) or 0
    pages = max(1, (total+24)//25)
    page = min(int(page), pages)
    rows = db.query(
        "SELECT d.*, l.title, p.purchase_pk, p.purchased_at, p.acquisition_cost_eur, u.sku AS owned_sku, "
        "p.shipping_cost_eur AS inbound_shipping_cost_eur, p.buyer_fee_eur, t.outcome_status, "
        "t.sale_at, t.final_sale_price_eur, t.platform_fee_eur, t.payment_fee_eur, "
        "t.seller_shipping_cost_eur, t.packaging_cost_eur, t.other_variable_cost_eur, t.tax_cost_eur "
        "FROM research_decisions d LEFT JOIN listings l ON l.listing_pk=d.listing_pk "
        "LEFT JOIN purchases p ON p.platform=d.platform AND p.source_listing_id=d.source_listing_id "
        "LEFT JOIN inventory_units u ON u.purchase_pk=p.purchase_pk "
        "LEFT JOIN transactions t ON t.purchase_pk=p.purchase_pk " + where +
        " ORDER BY d.decision_pk DESC LIMIT 25 OFFSET ?", params+[(page-1)*25])
    managed_trades = {row["purchase_pk"]: row for row in purchases.ledger_rows() if row.get("managed_sku")}
    now = datetime.now(timezone.utc)
    from . import acquisition_evidence
    attribution = acquisition_evidence.load([row["purchase_pk"] for row in rows],
                                            decision_ids=[row["decision_pk"] for row in rows], now=now)
    items = []
    timing = {}
    for row in rows:
        if row.get("purchase_pk") in managed_trades:
            trade = managed_trades[row["purchase_pk"]]
            for key in ("outcome_status", "sale_at", "final_sale_price_eur", "managed_sku", "net_margin") + tuple(purchases.UNKNOWN_COST):
                row[key] = trade[key]
        margin, _ = purchases._margin(row) if row["outcome_status"] in analysis.SALE_OUTCOMES or row.get("managed_sku") and row["outcome_status"] == "written_off" else (None, None)
        purchased, recorded = metrics.parse_ts(row["purchased_at"]), metrics.parse_ts(row["recorded_at"])
        prior = bool(purchased and recorded and purchased < recorded)
        sale = metrics.parse_ts(row["sale_at"])
        timing_known = bool(purchased and recorded and recorded <= purchased <= now)
        if row["outcome_status"] in analysis.SALE_OUTCOMES:
            timing_known = bool(timing_known and sale and purchased <= sale <= now)
        timing[row["decision_pk"]] = timing_known
        assumed = _json_map(row["scenario_json"]) if row["scenario_json"] else None
        evaluation = ("pre_existing_trade" if prior else "not_acquired" if not row["purchase_pk"]
                      else "timing_unknown" if not timing_known else "written_off_complete" if margin and row["outcome_status"] == "written_off"
                      else "sold_complete" if margin
                      else "sold_costs_unknown" if row["outcome_status"] in analysis.SALE_OUTCOMES
                      else "other_outcome" if row["outcome_status"] else "acquired")
        items.append({"decision_pk": row["decision_pk"], "recorded_at": row["recorded_at"],
                      "listing_pk": row["listing_pk"], "platform": row["platform"],
                      "source_listing_id": row["source_listing_id"], "title": row["title"],
                      "decision": row["decision"], "notes": row["notes"],
                      "checks": json.loads(row["checks_json"]), "scenario": assumed,
                      "evidence": json.loads(row["evidence_json"]), "evaluation": evaluation,
                      "purchase_pk": row["purchase_pk"],
                      "purchased_at": row["purchased_at"],
                      "owned_sku": row["owned_sku"],
                      "current_order_id": trade["trade_economics"]["current_order"]["order_id"] if row.get("managed_sku") else None,
                      "actual_profit_eur": margin["gross_margin_eur"] if margin else None})
    from . import owned_outcomes
    for item in items:
        item["acquisition_attempts"] = db.query("SELECT attempt_id,status,reason,evidence_ref,occurred_at,purchase_pk FROM acquisition_attempts WHERE decision_pk=? ORDER BY recorded_at,attempt_id", (item["decision_pk"],))
        item["owned_outcome"] = owned_outcomes.report(item["purchase_pk"]) if item["purchase_pk"] else None
        link = attribution.get(item["purchase_pk"], {})
        if not link:
            receipt_purchase = next((attempt["purchase_pk"] for attempt in item["acquisition_attempts"]
                                     if attempt["purchase_pk"] is not None), None)
            link = attribution.get(receipt_purchase, {})
        item.update(_profit_comparison(item, link, timing[item["decision_pk"]], now))
    counts = {row["decision"]: row["n"] for row in db.query(
        "SELECT decision, COUNT(*) AS n FROM research_decisions GROUP BY decision")}
    return {"items": items, "total": total, "page": page, "pages": pages,
            "n_records": sum(counts.values()), "counts": counts,
            "n_unique_listings": db.scalar("SELECT COUNT(*) FROM (SELECT platform, source_listing_id FROM research_decisions GROUP BY platform, source_listing_id) cohort") or 0}


def acquisition_context(platform, source_listing_id):
    """Resolve real acquisition identity even if its market observation expired."""
    platform = _text(platform, "platform", 20, True)
    source_listing_id = _text(source_listing_id, "source listing ID", 100, True)
    if platform not in ("tori", "ebay", "tradera", "other"):
        raise ValueError("unsupported acquisition source")
    with db.read_snapshot():
        purchase = db.query_one("SELECT p.purchase_pk, p.title_raw, p.acquisition_cost_eur, u.sku AS owned_sku, "
            "EXISTS(SELECT 1 FROM transactions t WHERE t.purchase_pk=p.purchase_pk) AS recorded_outcome "
            "FROM purchases p LEFT JOIN inventory_units u ON u.purchase_pk=p.purchase_pk "
            "WHERE p.platform=? AND p.source_listing_id=?", (platform, source_listing_id))
    return {"platform": platform, "source_listing_id": source_listing_id, "purchase": purchase,
            "note": "Source identity only. Enter actual acquisition cost and inspect owned facts separately."}


# Acquisition costs that must be known before a margin is reportable. These are
# the `purchases` column names, which is what a purchase row actually carries.
# `purchases._margin` reads `inbound_shipping_cost_eur` instead, because the
# journal query aliases the column when it joins a transaction; the two names
# refer to the same cost and the completeness test below pins that down.
ACQUISITION_COST_FIELDS = ("acquisition_cost_eur", "shipping_cost_eur", "buyer_fee_eur")


def cost_completeness(purchase):
    """Which recorded costs are known, and which are still unknown.

    A missing cost is unknown, never zero -- that rule is why this exists rather
    than a simple total. `unknown` is empty only when every component is present.
    """
    if not purchase:
        return {"known": [], "unknown": list(ACQUISITION_COST_FIELDS), "complete": False,
                "note": "No purchase recorded yet."}
    known = [f for f in ACQUISITION_COST_FIELDS if purchase.get(f) is not None]
    unknown = [f for f in ACQUISITION_COST_FIELDS if purchase.get(f) is None]
    return {"known": known, "unknown": unknown, "complete": not unknown,
            "note": None if not unknown else "Unknown costs suppress a complete profit figure."}


def acquisition_prefill(platform, source_listing_id):
    """Everything needed to record this acquisition without retyping it.

    The buy side is the only evidence source in this system with no rate limit,
    no waiting and no external dependency, and it is still empty. This exists to
    remove the retyping that makes it a chore: the observed facts, the frozen
    asking-price context and the decision that justified the buy are read from
    storage and handed back ready to submit.

    Two boundaries are preserved deliberately.

    Observed and entered never mix. `listing.ask_eur` is what the seller asked,
    an observation. `purchase.acquisition_cost_eur` is what the operator actually
    paid, an entry. The prefill carries the first as context and leaves the
    second blank, because defaulting an acquisition cost from an asking price
    would fabricate the exact figure the whole ledger exists to measure.

    Reopening is not duplicating. When a purchase or SKU already exists this
    returns its identity and reports `already_acquired`, so the UI reopens the
    existing unit instead of creating a second one.
    """
    platform = _text(platform, "platform", 20, True)
    source_listing_id = _text(source_listing_id, "source listing ID", 100, True)
    if platform not in ("tori", "ebay", "tradera", "other"):
        raise ValueError("unsupported acquisition source")

    with db.read_snapshot():
        listing = db.query_one(
            "SELECT " + analysis.FIELDS + " FROM listings WHERE platform=? AND listing_id=?",
            (platform, source_listing_id))
        purchase = db.query_one(
            "SELECT p.purchase_pk, p.purchased_at, p.acquisition_cost_eur, p.shipping_cost_eur, "
            "p.buyer_fee_eur, p.title_raw, p.condition_norm, u.sku AS owned_sku, u.state AS unit_state, "
            "EXISTS(SELECT 1 FROM inventory_units i WHERE i.purchase_pk=p.purchase_pk) AS has_unit, "
            "EXISTS(SELECT 1 FROM transactions t WHERE t.purchase_pk=p.purchase_pk) AS recorded_outcome "
            "FROM purchases p LEFT JOIN inventory_units u ON u.purchase_pk=p.purchase_pk "
            "WHERE p.platform=? AND p.source_listing_id=?", (platform, source_listing_id))
        decision = db.query_one(
            "SELECT decision_pk, decision, notes, recorded_at, evidence_json, scenario_json "
            "FROM research_decisions WHERE platform=? AND source_listing_id=? "
            "ORDER BY decision_pk DESC LIMIT 1", (platform, source_listing_id))

    blocks = []
    listing_view = None
    if listing:
        listing_view = {
            "listing_pk": listing["listing_pk"], "title": listing["title"],
            "subplatform": listing["subplatform"],
            # An asking price, always labelled as one. It is never a sale price.
            "ask_eur": analysis.amount(listing["listed_price_eur"]),
            "price_observed_at": listing["price_observed_at"] or listing["last_seen_at"],
            "category_norm": listing["category_norm"],
            "category_root_norm": listing["category_root_norm"],
            "condition_norm": listing["condition_norm"],
            "brand_norm": listing["brand_norm"], "model_norm": listing["model_norm"],
            "status_norm": listing["status_norm"], "outcome_status": listing["outcome_status"],
            "last_seen_at": listing["last_seen_at"],
        }
    else:
        # Retention can remove the market observation while the owned record
        # survives. That is a legitimate state, not an error.
        blocks.append("No stored market observation for this source. An offline "
                      "acquisition can still be recorded without a listing ID.")

    decision_view = None
    if decision:
        # The asking-price context lives inside the frozen evidence snapshot,
        # not in a column of its own. Reading it from `evidence_json` is what
        # keeps "the price when you decided" distinct from "the price now".
        evidence = json.loads(decision["evidence_json"]) if decision["evidence_json"] else {}
        decision_view = {"decision_pk": decision["decision_pk"], "decision": decision["decision"],
                         "notes": decision["notes"], "recorded_at": decision["recorded_at"],
                         "ask_eur": evidence.get("ask_eur"),
                         "scenario": json.loads(decision["scenario_json"]) if decision["scenario_json"] else None}

    owned_sku = purchase["owned_sku"] if purchase else None
    status = "already_acquired" if purchase else ("ready" if listing else "offline_only")

    return {
        "platform": platform, "source_listing_id": source_listing_id, "status": status,
        "listing": listing_view,
        "decision": decision_view,
        "purchase": dict(purchase) if purchase else None,
        "owned_sku": owned_sku,
        "has_unit": bool(purchase["has_unit"]) if purchase else False,
        "recorded_outcome": bool(purchase["recorded_outcome"]) if purchase else False,
        "cost_completeness": cost_completeness(purchase),
        # Ready to submit. Deliberately carries no acquisition cost: that figure
        # is the operator's to supply and must never be inferred from an ask.
        "prefill": {
            "kind": _suggested_kind(listing),
            "title": (listing["title"] if listing else None) or (purchase["title_raw"] if purchase else None),
            "platform": platform,
            "subplatform": listing["subplatform"] if listing else "standard",
            "source_listing_id": source_listing_id,
            "purchased_on": None,
            "acquisition_cost_eur": None,
            "shipping_cost_eur": None,
            "buyer_fee_eur": None,
        },
        "blocks": blocks,
        "note": "Asking prices are context. Enter the amount you actually paid.",
    }


def _suggested_kind(listing):
    """Guess an inventory kind from the category, or `None` rather than a guess.

    A wrong kind silently mis-files an owned unit, and the operator can set it in
    one field. Returning nothing is cheaper than being confidently wrong.
    """
    if not listing:
        return None
    haystack = " ".join(str(listing.get(f) or "") for f in ("category_norm", "category_root_norm", "title")).lower()
    if any(word in haystack for word in ("pyörä", "polkupyör", "bike", "cycl")):
        return "bicycle"
    if any(word in haystack for word in ("potkulauta", "scooter", "skooter")):
        return "scooter"
    if listing.get("category_root_norm") == "1.93":
        return "technology"
    return None


def acquisition_queue(limit=50):
    """Owned acquisitions that still need attention, worst first.

    Three reasons a purchase is unfinished, kept separate because they need
    different actions: no physical unit yet, recorded costs that are unknown, or
    no outcome at all. Collapsing them into one "open" count would hide which is
    which, and the fix differs in each case.
    """
    with db.read_snapshot():
        rows = db.query(
            "SELECT p.purchase_pk, p.platform, p.source_listing_id, p.title_raw, p.purchased_at, "
            "p.acquisition_cost_eur, p.shipping_cost_eur, p.buyer_fee_eur, "
            "u.sku AS owned_sku, u.state AS unit_state, "
            "EXISTS(SELECT 1 FROM inventory_units i WHERE i.purchase_pk=p.purchase_pk) AS has_unit, "
            "EXISTS(SELECT 1 FROM transactions t WHERE t.purchase_pk=p.purchase_pk) AS recorded_outcome "
            "FROM purchases p LEFT JOIN inventory_units u ON u.purchase_pk=p.purchase_pk "
            "ORDER BY p.purchased_at DESC LIMIT ?", (int(limit),))

    items, needs_unit, unknown_costs, no_outcome = [], 0, 0, 0
    for row in rows:
        complete = cost_completeness(row)
        reasons = []
        if not row["has_unit"]:
            reasons.append("no_owned_unit")
            needs_unit += 1
        if not complete["complete"]:
            reasons.append("unknown_costs")
            unknown_costs += 1
        if not row["recorded_outcome"]:
            reasons.append("no_outcome")
            no_outcome += 1
        items.append({"purchase_pk": row["purchase_pk"], "platform": row["platform"],
                      "source_listing_id": row["source_listing_id"], "title": row["title_raw"],
                      "purchased_at": row["purchased_at"], "owned_sku": row["owned_sku"],
                      "unit_state": row["unit_state"], "reasons": reasons,
                      "cost_completeness": complete,
                      "acquisition_cost_eur": row["acquisition_cost_eur"]})
    return {"items": items, "counts": {"needs_unit": needs_unit, "unknown_costs": unknown_costs,
                                       "no_outcome": no_outcome, "total": len(items)}}



def compare(left_filters, right_filters, horizon=30):
    left, right = clean_filters(left_filters), clean_filters(right_filters)
    if str(horizon) not in ("7", "14", "30", "60", "90"):
        raise ValueError("unsupported comparison horizon")
    cutoff = datetime.now(timezone.utc)-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    sides, keys = [], []
    for filters in (left, right):
        where, params, _ = metrics.listing_filters(**filters)
        rows = db.query("SELECT " + analysis.FIELDS + " FROM listings WHERE " + " AND ".join(where), params)
        rows = [row for row in rows if not row["is_giveaway"] and row["listed_price_eur"] != 0]
        keys.append({row["listing_pk"] for row in rows})
        prices = analysis.price_panel(rows, cutoff)
        liq = metrics.liquidity_rows(rows, int(horizon), filters=filters)
        condition_groups = {}
        for row in rows:
            condition_groups.setdefault(row["condition_norm"] or "unknown", []).append(row)
        sides.append({"filters": filters, "n_tracked": len(rows), "prices": prices, "liquidity": liq,
                      "conditions": [{"label": label, **analysis.price_panel(group, cutoff)} for label, group in sorted(condition_groups.items())]})
    a, b = (side["prices"] for side in sides)
    difference = round(b["median_eur"]-a["median_eur"], 2) if a["status"] == b["status"] == "OK" else None
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "left": sides[0], "right": sides[1],
            "horizon": int(horizon), "min_n": config.MIN_N_FOR_REPORT, "overlap_listings": len(keys[0]&keys[1]),
            "median_difference_eur": difference,
            "note": "Descriptive asking-price comparison. Product mix and collection effort may differ; a difference does not establish an arbitrage margin or a condition effect."}


def action(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
        raise ValueError("action and params are required")
    name, params = payload.get("action"), payload["params"]
    writers = {"save_segment": (save_segment, {"name", "filters"}),
               "delete_segment": (delete_segment, {"segment_pk"}),
               "record_decision": (record_decision, {"client_token", "platform", "source_listing_id", "decision", "notes", "checks", "assumptions", "filters", "reference_at", "benchmark_revision", "scenario_context"})}
    if not isinstance(name, str) or name not in writers:
        raise ValueError("unsupported research action")
    writer, allowed = writers[name]
    if set(params)-allowed:
        raise ValueError("unsupported research fields")
    try:
        return writer(**params)
    except TypeError:
        raise ValueError("required research fields are missing")
