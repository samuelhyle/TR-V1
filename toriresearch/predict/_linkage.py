"""Whether seller-level models can run, and why not when they cannot.

Every model in this package that groups listings under a seller needs the same
thing: listings that resolve to a seller version. An empty result would read as
"the market has no measurable concentration", which is the opposite of what an
absent key means -- so both entry points ask this module first and refuse with
the actual reason. Coverage stays partial for as long as item-page collection
has not reached every listing, and the reason has to name which of the two
reasons that is, because the fixes are not interchangeable: re-running
collection helps when pages were never parsed and does nothing when the parser
is reading a field the platform does not serve.
"""

from .. import db, privacy


def status():
    """Non-identifying summary of seller linkage as it stands."""
    listings = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN l.seller_id IS NOT NULL THEN 1 ELSE 0 END) AS linked, "
        "SUM(CASE WHEN s.seller_version_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved "
        "FROM listings l LEFT JOIN sellers s ON s.seller_version_id=l.seller_version_id")
    sellers = db.query_one("SELECT COUNT(*) AS c FROM sellers")["c"]
    parsed = db.query_one(
        "SELECT day, parsed_rows FROM parser_checks "
        "WHERE source='itempage' AND parser='hydration' AND result='ok' "
        "ORDER BY day DESC LIMIT 1")
    total = listings["total"] or 0
    linked = int(listings["linked"] or 0)
    resolved = int(listings["resolved"] or 0)
    probed = db.scalar("SELECT COUNT(*) FROM listings WHERE last_probed_at IS NOT NULL") or 0
    info = {
        "key_enabled": privacy.seller_key_enabled(),
        "listings_total": total,
        "listings_with_key": linked,
        "listings_with_version": resolved,
        "resolved_coverage_pct": round(100.0 * resolved / total, 2) if total else 0.0,
        "coverage_pct": round(100.0 * linked / total, 2) if total else 0.0,
        "sellers": sellers,
        # Whether the item-page parser has run at all, and how recently. Without
        # this the reason below cannot tell "no page has been parsed" apart from
        # "pages are being parsed and every one of them yields no owner", which
        # are opposite problems with opposite fixes. The probe count is the
        # fallback because parser diagnostics expire on a retention window.
        "listings_probed": probed,
        "item_page_parse_day": parsed["day"] if parsed else None,
        "item_page_parses": int(parsed["parsed_rows"] or 0) if parsed else 0,
    }
    # Carried in the payload so the dashboard and `doctor` quote the same reason
    # the models refuse with, instead of each keeping its own guess at the cause.
    info["reason"] = blocked_reason(info)
    return info


def blocked_reason(info=None, min_listings=1):
    """Why seller-level models cannot run, or None when they can.

    Order matters: each branch names a cause the operator can act on, and the
    parser branch comes before the historical one because "re-run collection"
    is only useful advice when re-running it would change the outcome.
    """
    info = info or status()
    if info["listings_with_version"] >= min_listings:
        return None
    if info["listings_with_key"]:
        return ("seller keys exist but have no resolved seller versions; re-run "
                "item-page collection to resolve linkage.")
    if not info["key_enabled"]:
        return (
            "no TORI_SELLER_SALT configured, so listings.seller_id is NULL. "
            "Set the salt in config/local.env to enable pseudonymous seller "
            "linkage (see toriresearch/privacy.py), then re-run item-page "
            "collection so the key can be derived at parse time."
        )
    if info["listings_total"] == 0:
        return "no listings collected yet."
    if info["item_page_parses"]:
        return (
            f"item-page collection parsed {info['item_page_parses']} pages on "
            f"{info['item_page_parse_day']} and derived no seller key from any of "
            "them, so re-running collection reproduces the same NULL: the parser "
            "is not reading the owner field the platform serves (see "
            "_seller_key_from in toriresearch/ingest/itempage.py). If the salt was "
            "configured after that day, the next parse settles it."
        )
    if info["listings_probed"]:
        return (
            f"{info['listings_probed']} listings have been probed and none carries a "
            "seller key, but the parse diagnostics for those days have expired, so "
            "whether the parser found an owner on any of them is no longer recorded. "
            "Re-run item-page collection to find out."
        )
    return (
        f"{info['listings_with_key']} of {info['listings_total']} listings carry a "
        "seller key. Keys are derived when an item page is parsed, and no item "
        "page has been parsed yet, so seller-level models stay empty until "
        "item-page collection runs under a configured salt."
    )


def require(min_listings=1):
    """Raise with a readable reason unless enough listings carry a seller key.

    The threshold is a floor, not a statistical one: models that need several
    listings per seller pass their own higher bar and report it in their own
    output. This only catches the case where the key does not exist yet.
    """
    info = status()
    reason = blocked_reason(info, min_listings)
    if reason is None:
        return info
    raise ValueError(
        f"Seller-level model unavailable: {reason} "
        f"({info['listings_with_key']} linked of {info['listings_total']} listings, "
        f"{info['sellers']} seller versions).")
