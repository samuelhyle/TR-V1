"""Bounded public-page adapter for operator-selected research sources.

This is separate from marketplace collection. It never follows page links or
executes scripts, and an Offer is retained as a published ask, never a sale.
"""

import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import time
import queue
import threading
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import config
from .http import AccessBlocked, PoliteSession, RobotsDenied

MAX_BYTES = 1_048_576
MAX_REQUESTS = 12  # Includes robots metadata and redirects, across the whole run.
MAX_SECONDS = 90
KINDS = ("asking_price", "product_specs", "fee_terms", "research")


def clean_url(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("Source URL must be an HTTPS URL of at most 2048 characters")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Source URL has an invalid port") from None
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        raise ValueError("Sources require HTTPS on port 443 without credentials")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host:
        raise ValueError("Source must name a public website")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Source must name a public website, not an IP address")
    if host.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".example")) or host == "localhost":
        raise ValueError("Local and reserved source hosts are refused")
    if host == "tori.fi" or host.endswith(".tori.fi") or host == "ebay.com" or host.endswith(".ebay.com"):
        raise ValueError("Use the marketplace collector or manual sold-evidence import for this source")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def public_addresses(host):
    """Validate every DNS answer; the connection below pins a checked address."""
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Source resolves to a private or reserved network")
    return addresses


def _resolve(host, timeout):
    """A slow system resolver cannot hold the research job past its budget."""
    result = queue.Queue(maxsize=1)

    def work():
        try:
            result.put((public_addresses(host), None))
        except Exception as exc:
            result.put((None, exc))

    threading.Thread(target=work, daemon=True).start()
    try:
        addresses, error = result.get(timeout=min(8, timeout))
    except queue.Empty:
        raise TimeoutError("Source DNS lookup exceeded its research budget") from None
    if error:
        raise error
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        connection = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


class PublicSourceSession(PoliteSession):
    """Robots, contact, host pacing, audit, TLS, public DNS and response limits."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.deadline = time.monotonic() + MAX_SECONDS
        self.requests = 0

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Internet research reached its runtime budget")
        return remaining

    def _throttle(self, url, check_robots=True):
        import fcntl
        delay = self._crawl_delay(url) if check_robots else None
        gap = max(1.0, delay or 0)
        host = urlsplit(url).hostname
        rate_dir = config.DATA_DIR / "rates"
        rate_dir.mkdir(parents=True, exist_ok=True)
        with (rate_dir / (hashlib.sha256(host.encode()).hexdigest() + ".time")).open("a+") as handle:
            # A concurrent request must not make an otherwise bounded job wait
            # indefinitely for the shared host lock.
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(.1, self._remaining()))
            handle.seek(0)
            try:
                last = float(handle.read())
            except ValueError:
                last = 0
            wait = max(0, last + gap - time.time())
            if wait >= self._remaining():
                raise TimeoutError("Source crawl delay exceeds the research runtime budget")
            if wait:
                time.sleep(wait)
            handle.seek(0)
            handle.truncate()
            handle.write(str(time.time()))
            handle.flush()

    def _raw(self, url, metadata=False):
        origin = urlsplit(clean_url(url)).hostname
        for _ in range(4):
            url = clean_url(url)
            if urlsplit(url).hostname != origin:
                raise ValueError("Source redirects must remain on the same host")
            if not metadata and not self.allowed(url):
                self._audit(url, "DENIED", 0, self._robots_reason(url))
                raise RobotsDenied(self._robots_reason(url))
            self._throttle(url, check_robots=not metadata)
            if self.requests >= MAX_REQUESTS:
                raise ValueError("Internet research reached its request budget")
            address = _resolve(origin, self._remaining())[0]
            connection = _PinnedHTTPS(origin, address, min(12, self._remaining()))
            self.requests += 1
            self._begin_request(url)
            try:
                parsed = urlsplit(url)
                connection.request("GET", urlunsplit(("", "", parsed.path, parsed.query, "")),
                                   headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity",
                                            "Accept": "text/html,application/ld+json,application/json,text/plain"})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    self._audit(url, response.status, 0)
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("Source redirect has no location")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    self._audit(url, response.status, 0)
                    raise AccessBlocked(f"Source returned HTTP {response.status}; no retry was sent")
                content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type not in ("text/html", "application/xhtml+xml", "application/json", "application/ld+json", "text/plain"):
                    raise ValueError("Source is not an HTML, JSON or text page")
                chunks, size = [], 0
                while size <= MAX_BYTES:
                    self._remaining()
                    connection.sock and connection.sock.settimeout(min(12, self._remaining()))
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                raw = b"".join(chunks)
                if len(raw) > MAX_BYTES:
                    raise ValueError("Source exceeds the one MiB response limit")
                if response.getheader("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Expanded source exceeds the one MiB response limit")
                self._audit(url, response.status, len(raw))
                return {"url": url, "body": raw, "content_type": content_type}
            except (OSError, TimeoutError):
                self._audit(url, "NETWORK", 0, "Public source request failed")
                raise
            finally:
                connection.close()
        raise ValueError("Source exceeded its redirect budget")

    def _read_robots(self, parser):
        result = self._raw(parser.url, metadata=True)
        text = result["body"].decode("utf-8", "replace")
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise ValueError("Source robots metadata is HTML")
        parser.parse(text.splitlines())

    def page(self, url):
        return self._raw(url)


def _text(value, limit=240):
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.title, self.text = [], [], []
        self.script = None
        self.in_title = False
        self.hidden = 0
        self.text_size = 0
        self.preferred_text = []
        self.preferred_size = 0
        self.preferred_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.script = []
        if tag == "title":
            self.in_title = True
        if tag in ("main", "article"):
            self.preferred_depth += 1

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            if len(self.scripts) < 20:
                self.scripts.append("".join(self.script))
            self.script = None
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False
        if tag in ("main", "article"):
            self.preferred_depth = max(0, self.preferred_depth - 1)

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.in_title:
            self.title.append(data)
        normalized = re.sub(r"\s+", " ", data).strip()
        if not self.hidden and normalized and self.text_size < 6000:
            self.text.append(normalized[:6000 - self.text_size])
            self.text_size += len(self.text[-1])
        if not self.hidden and normalized and self.preferred_depth and self.preferred_size < 6000:
            self.preferred_text.append(normalized[:6000 - self.preferred_size])
            self.preferred_size += len(self.preferred_text[-1])


def _nodes(document):
    """Bound traversal; no remote JSON-LD contexts, links or script execution."""
    stack = [(document, 0)]
    count = 0
    while stack and count < 1000:
        value, depth = stack.pop()
        count += 1
        if depth > 12:
            continue
        if isinstance(value, list):
            stack.extend((child, depth + 1) for child in reversed(value[:100]))
        if isinstance(value, dict):
            yield value
            stack.extend((child, depth + 1) for child in reversed(list(value.values())[:100]))


def extract(page, kind):
    if kind not in KINDS:
        raise ValueError("Unknown research source kind")
    text = page["body"].decode("utf-8", "replace")
    parser = _Page()
    documents = []
    if page["content_type"] in ("application/json", "application/ld+json"):
        try:
            documents.append(json.loads(text))
        except (ValueError, RecursionError):
            pass
    elif page["content_type"] == "text/plain":
        parser.text = [text[:2400]]
    else:
        parser.feed(text)
        for script in parser.scripts:
            try:
                documents.append(json.loads(script))
            except (ValueError, RecursionError):
                continue
    title = _text(" ".join(parser.title))
    if any(marker in title.lower() for marker in ("access denied", "just a moment", "verify you are human", "captcha", "robot check")):
        raise AccessBlocked("Source returned a verification or refusal page; no evidence was extracted")
    products = []
    published_at, publisher_updated_at = "", ""
    for document in documents:
        for node in _nodes(document):
            published_at = published_at or _text(node.get("datePublished"),60)
            publisher_updated_at = publisher_updated_at or _text(node.get("dateModified"),60)
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if not isinstance(types, list) or not any(t in ("Product", "IndividualProduct", "ProductModel") for t in types):
                continue
            product = {key: _text(node.get(key)) for key in ("name", "model", "sku", "gtin", "gtin13", "brand", "color", "size")}
            properties = node.get("additionalProperty") or []
            properties = [properties] if isinstance(properties, dict) else properties
            product["properties"] = [{"name": _text(p.get("name")), "value": _text(p.get("value"))}
                                     for p in properties[:20] if isinstance(p, dict)] if isinstance(properties, list) else []
            offers = node.get("offers") or []
            offers = [offers] if isinstance(offers, dict) else offers
            product["offers"] = []
            for offer in offers[:10] if isinstance(offers, list) else []:
                if not isinstance(offer, dict) or offer.get("@type") == "AggregateOffer":
                    continue
                raw = offer.get("price")
                if isinstance(raw, bool) or not re.fullmatch(r"\d+(?:\.\d{1,4})?", str(raw)):
                    continue
                amount = float(raw)
                currency = _text(offer.get("priceCurrency"), 3).upper()
                if amount > 10_000_000 or not re.fullmatch("[A-Z]{3}", currency):
                    continue
                product["offers"].append({"amount": amount, "currency": currency, "basis": "published_asking_price",
                                          "condition": _text(offer.get("itemCondition") or node.get("itemCondition")),
                                          "availability": _text(offer.get("availability")),
                                          "valid_until": _text(offer.get("priceValidUntil")),
                                          "shipping_and_fees": "not_established"})
            products.append(product)
            if len(products) == 8:
                break
        if len(products) == 8:
            break
    return {"url": page["url"], "body_sha256": hashlib.sha256(page["body"]).hexdigest(),
            "title": title, "products": products,
            "published_at": published_at or None, "publisher_updated_at": publisher_updated_at or None,
            "excerpt": _text(" ".join(parser.preferred_text or parser.text), 2400), "kind": kind,
            "basis": "published_asking_price" if kind == "asking_price" else "reference_material",
            "warnings": ["Publisher claims are unverified; check variant, condition, region, stock, shipping and fees.",
                         "An advertised Offer does not establish a completed sale or achievable resale proceeds."]}
