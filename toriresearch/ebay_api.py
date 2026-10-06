"""Pinned, credential-redacting eBay API transport, separate from web crawling.

Contracts checked 2 October 2026 against the eBay OAuth guide and OpenAPI specs.
Only OAuth lifecycle POSTs and the implemented read routes are supported here.
"""

import base64
import json
import math
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse

from . import config
from .http import PoliteSession, parse_retry_after

API_SCOPE = "https://api.ebay.com/oauth/api_scope"
ACCOUNT_SCOPE = API_SCOPE + "/sell.account.readonly"
SUPPORTED_MARKETS = frozenset("EBAY_AT EBAY_AU EBAY_BE EBAY_CA EBAY_CH EBAY_DE EBAY_ES EBAY_FR "
                              "EBAY_GB EBAY_HK EBAY_IE EBAY_IT EBAY_NL EBAY_PL EBAY_SG EBAY_US".split())


class EbayAPIError(RuntimeError):
    def __init__(self, status=None, retry_after=None):
        self.status = status
        self.retry_after = retry_after
        # Never echo response bodies, HTTP exception reasons or credentials.
        message = f"eBay API returned HTTP {status}" if status else "eBay API request failed"
        if status in (401, 403):
            message += "; account access must be reviewed"
        if status == 429:
            message += "; retry later within platform limits"
        super().__init__(message)


def settings(sandbox=None, client_id=None, client_secret=None):
    sandbox = config.EBAY_SANDBOX if sandbox is None else sandbox
    if not isinstance(sandbox, bool):
        raise ValueError("sandbox must be a boolean")
    if sandbox:
        identifier, secret, runame = (config.EBAY_SANDBOX_CLIENT_ID, config.EBAY_SANDBOX_CLIENT_SECRET,
                                      config.EBAY_SANDBOX_RUNAME)
    else:
        identifier, secret, runame = config.EBAY_CLIENT_ID, config.EBAY_CLIENT_SECRET, config.EBAY_RUNAME
    return {"environment": "sandbox" if sandbox else "production",
            "base_url": "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com",
            "authorize_url": "https://auth.sandbox.ebay.com/oauth2/authorize" if sandbox
                             else "https://auth.ebay.com/oauth2/authorize",
            "client_id": identifier if client_id is None else client_id,
            "client_secret": secret if client_secret is None else client_secret, "runame": runame}


def validate_market(marketplace):
    if not isinstance(marketplace, str) or marketplace not in SUPPORTED_MARKETS:
        raise ValueError("Set EBAY_MARKETPLACE_ID to an explicitly selected supported Browse marketplace")
    return marketplace


def basic_header(options):
    identifier, secret = options["client_id"], options["client_secret"]
    if not identifier or not secret:
        raise ValueError(f"eBay {options['environment']} client credentials are missing")
    return "Basic " + base64.b64encode(f"{identifier}:{secret}".encode()).decode()


def token_payload(data, *, refresh=False):
    if (not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]
            or any(c.isspace() for c in data["access_token"]) or
            ("scope" in data and not isinstance(data["scope"], str))):
        raise EbayAPIError()
    if str(data.get("token_type", "Bearer")).lower() != "bearer":
        raise EbayAPIError()
    for key in ("expires_in", "refresh_token_expires_in"):
        if key not in data and key == "refresh_token_expires_in":
            continue
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise EbayAPIError()
    if refresh and (not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]
                    or "refresh_token_expires_in" not in data):
        raise EbayAPIError()
    return data


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization must never follow an API redirect, even to another eBay host.
        raise EbayAPIError(code)


class EbayTransport(PoliteSession):
    def __init__(self, sandbox=False):
        self.options = settings(sandbox)
        super().__init__(user_agent="toriresearch/0.1 (official eBay API)", require_contact=False)
        self.opener = urllib.request.build_opener(_NoRedirects())

    def request(self, method, url, *, headers=None, fields=None):
        parsed, expected = urlparse(url), urlparse(self.options["base_url"])
        if (parsed.scheme != "https" or parsed.netloc != expected.netloc or parsed.username or parsed.fragment):
            raise ValueError("eBay request does not match the selected environment")
        path = parsed.path
        oauth = method == "POST" and path in {"/identity/v1/oauth2/token", "/identity/v1/oauth2/token/revoke"}
        read = method == "GET" and (path == "/buy/browse/v1/item_summary/search" or
                                   re.fullmatch(r"/buy/browse/v1/item/[^/]+", path) or
                                   path == "/sell/account/v1/privilege")
        if not oauth and not read:
            raise ValueError("This eBay adapter does not support the requested action")
        if oauth and parsed.query:
            raise ValueError("OAuth parameters belong in the request body")
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        request_headers.update(headers or {})
        if not request_headers.get("Authorization"):
            raise ValueError("An authorized API token is required")
        body = None
        if fields is not None:
            if not oauth:
                raise ValueError("Only OAuth requests accept form fields")
            body = urlencode(fields).encode()
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        # Authenticated API contracts govern these fixed routes. No scraper
        # robots request, response-body retention or implicit write retries.
        self._throttle(url, check_robots=False)
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                self._audit(url, response.status, len(raw), "official API; body not retained")
                if len(raw) > 2 * 1024 * 1024:
                    raise EbayAPIError()
                if path.endswith("/revoke"):
                    return {}
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise EbayAPIError()
                return result
        except urllib.error.HTTPError as exc:
            self._audit(url, exc.code, 0, "official API; error body redacted")
            raise EbayAPIError(exc.code, parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)) from None
        except (urllib.error.URLError, OSError, ValueError):
            self._audit(url, "NETWORK", 0, "official API; details redacted")
            raise EbayAPIError() from None

    def token(self, options, fields):
        return self.request("POST", options["base_url"] + "/identity/v1/oauth2/token",
                            headers={"Authorization": basic_header(options)}, fields=fields)
