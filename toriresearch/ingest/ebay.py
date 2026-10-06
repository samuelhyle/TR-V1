"""Read-only eBay Browse ingestion of asking-price observations.

Verify application access, permitted use, marketplace support and current quota
before production use. Seller authorization is a separate integration. The
adapter uses environment-specific endpoints and separate sandbox credentials.
Production Browse use also requires a recorded source/action review.

This API returns ASKING prices only. eBay sold-completion data is not available
here; see PROJECT_PLAN.md 1.2.
"""

import logging
import math
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from .. import config, db
from ..ebay_api import API_SCOPE, EbayTransport, settings, token_payload, validate_market

log = logging.getLogger("toriresearch.ebay")

NORMALIZATION_VERSION = "norm-2026-10-01"


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class EbayClient:
    def __init__(self, client_id=None, client_secret=None, marketplace_id=None, sandbox=None):
        self.options = settings(sandbox, client_id, client_secret)
        self.client_id = self.options["client_id"]
        self.client_secret = self.options["client_secret"]
        self.marketplace_id = validate_market(config.EBAY_MARKETPLACE_ID if marketplace_id is None else marketplace_id)
        self.sandbox = self.options["environment"] == "sandbox"
        self.token_url = self.options["base_url"] + "/identity/v1/oauth2/token"
        self.search_url = self.options["base_url"] + "/buy/browse/v1/item_summary/search"
        self.token = None
        self.token_expires = 0.0
        self.session = EbayTransport(self.sandbox)

    def _authority(self):
        if not self.sandbox:
            from .. import permissions
            permissions.require("ebay", "browse")

    def access_token(self):
        self._authority()
        now = datetime.now(timezone.utc).timestamp()
        if self.token and now < self.token_expires:
            return self.token
        data = token_payload(self.session.token(self.options, {
            "grant_type": "client_credentials", "scope": API_SCOPE}))
        self.token = data["access_token"]
        self.token_expires = now + float(data.get("expires_in", 7200)) - 60
        return self.token

    def search(self, keyword, limit=50):
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 512:
            raise ValueError("Search needs a keyword of at most 512 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Search limit must be between 1 and 200")
        params = {"q": keyword, "limit": limit}
        url = f"{self.search_url}?{urlencode(params)}"
        data = self.session.request("GET", url, headers={
            "Authorization": f"Bearer {self.access_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
        })
        return data.get("itemSummaries") or []

    def browse(self, item_ids):
        if not isinstance(item_ids, (list, tuple)) or len(item_ids) > 20:
            raise ValueError("Retrieve at most 20 explicit REST item IDs")
        if not item_ids:
            return []
        if any(not isinstance(i, str) or not i or len(i) > 256 or any(c.isspace() for c in i)
               for i in item_ids):
            raise ValueError("Invalid REST item ID")
        # getItems is restricted to selected partners. Use documented getItem
        # instead of the former fabricated item_summary/get_items route.
        out = []
        for item_id in dict.fromkeys(item_ids):
            out.append(self.session.request("GET", self.options["base_url"] +
                                            "/buy/browse/v1/item/" + quote(item_id, safe=""), headers={
                "Authorization": f"Bearer {self.access_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id}))
        return out


def _normalise_condition(ebay_condition):
    if not ebay_condition:
        return None, None
    mapping = {
        "NEW": "new_unused", "LIKE_NEW": "like_new", "NEW_OTHER": "new_unused",
        "NEW_WITH_DEFECTS": "fair", "CERTIFIED_REFURBISHED": "refurbished",
        "EXCELLENT_REFURBISHED": "refurbished", "VERY_GOOD_REFURBISHED": "refurbished",
        "GOOD_REFURBISHED": "refurbished", "SELLER_REFURBISHED": "refurbished",
        "USED_EXCELLENT": "very_good", "USED_VERY_GOOD": "very_good",
        "USED_GOOD": "good", "USED_ACCEPTABLE": "fair",
        "FOR_PARTS_OR_NOT_WORKING": "for_parts",
    }
    return ebay_condition, mapping.get(ebay_condition.upper().replace(" ", "_"), "unknown")


def run_search(marketplace_id, keyword, limit=50, store=True):
    client = EbayClient(marketplace_id=marketplace_id)
    summaries = client.search(keyword, limit=limit)
    if not store:
        return {"keyword": keyword, "returned": len(summaries), "stored": 0}

    observed = _utcnow()
    stored = 0
    for item in summaries:
        item_id = item.get("itemId")
        price = (item.get("price") or {})
        try:
            amount = float(price.get("value"))
        except (TypeError, ValueError):
            continue
        if not isinstance(item_id, str) or not item_id or not math.isfinite(amount) or amount < 0:
            continue
        currency = price.get("currency") or "EUR"
        cond_raw, cond_norm = _normalise_condition(item.get("condition"))
        created = item.get("itemCreationDate")
        pk, is_new = db.record_observation({
            "platform":"ebay", "subplatform":"standard", "listing_id":str(item_id),
            "title":item.get("title"),
            "category_raw_id":(item.get("categories") or [{}])[-1].get("categoryId"),
            "condition_raw":cond_raw, "condition_norm":cond_norm,
            "listed_price_original":amount, "currency_original":currency,
            "listed_price_eur":amount if currency=="EUR" and amount>0 else None,
            "platform_published_at":created, "first_seen_at":observed, "last_seen_at":observed,
            "last_live_at":observed, "status_norm":"active", "coverage":"collecting",
            "outcome_status":"unknown", "normalization_version":NORMALIZATION_VERSION,
            "source_observed_at":observed, "_source":"ebay_browse"})
        stored += is_new

    return {"keyword": keyword, "returned": len(summaries), "stored": stored,
            "note": "Ask prices only. eBay sold data is not in this API."}
