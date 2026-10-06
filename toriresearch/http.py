"""HTTP access layer with compliance guarantees enforced in code, not policy.

Every outbound request in this project goes through PoliteSession. It refuses
disallowed URLs, rate limits per host, backs off on 429/5xx, and appends an
audit record. There is deliberately no "ignore robots" escape hatch.
"""

import gzip
import hashlib
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from . import config, observability, collection_control, collection_events

log = logging.getLogger("toriresearch.http")


def mark_collection_stop(url, status, retry_after=None):
    """Persist Tori refusals and cooldowns across scheduler invocations."""
    host = urlparse(url).hostname
    if host not in ("tori.fi", "www.tori.fi"):
        return
    record = {"host": host, "status": status,
              "at": datetime.now(timezone.utc).isoformat()}
    if status == "throttled":
        record["resume_after"] = time.time() + max(3600, retry_after or 0)
    path = config.DATA_DIR / "collection_stop.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)


def collection_stop():
    path = config.DATA_DIR / "collection_stop.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    if record.get("status") == "throttled" and record.get("resume_after", 0) <= time.time():
        return None
    return record

# Statuses that mean the platform is refusing us rather than failing us. None of
# them is transient, so none of them is retried.
#   401 -- we never authenticate to a marketplace surface, so this is a refusal
#   403 -- the explicit block
#   451 -- a legal takedown, which we are required to stop on
BLOCK_STATUS_CODES = (401, 403, 451)

# How long to wait before re-reading a robots.txt that could not be read. Long
# enough not to hammer a host that is already failing us, short enough that one
# blip does not cost a whole scheduled run.
ROBOTS_RETRY_SECONDS = 60.0


class RobotsDenied(RuntimeError):
    pass


class MissingContactEmail(RuntimeError):
    pass


class AccessBlocked(RuntimeError):
    """The platform is refusing us: a 403, or an interstitial served as a 200.

    Raised, never worked around. A block is the platform exercising an access
    control, and `PROJECT_PLAN.md` treats defeating one as the line between a
    low-risk project and a criminal-risk one under ryzlain 35 luku. The correct
    response is to stop, say so, and let a human decide -- which is Tier 0
    licensing, not a technique.
    """


class AuthorityBlocked(AccessBlocked):
    """Local scope refusal, distinct from a platform HTTP refusal."""


class _ToriRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, session):
        self.session = session

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).hostname not in ("tori.fi", "www.tori.fi"):
            raise AuthorityBlocked("Tori redirect leaves the approved source")
        self.session._check_tori_authority(newurl)
        robots = urlparse(req.full_url).path == '/robots.txt'
        if robots and urlparse(newurl).path != '/robots.txt':
            raise AuthorityBlocked('Robots redirect leaves the metadata path')
        if not robots and not self.session.allowed(newurl):
            raise RobotsDenied("Tori redirect target is not allowed by robots")
        self.session._audit(req.full_url, code, 0, 'redirect')
        self.session._throttle(newurl, check_robots=not robots)
        self.session._check_tori_authority(newurl)
        self.session._begin_request(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Markers of an interstitial or bot-check page. Deliberately specific: a false
# positive here would reclassify a real observation, so this only ever *raises*,
# and only when the caller has already found the page unusable.
_BLOCK_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cloudflare",
    "cf-browser-verification",
    "just a moment",
    "unusual traffic",
    "access denied",
    "you have been blocked",
    "enable javascript and cookies",
    "tarkistamme",
    "oletko robotti",
    "access to this page has been denied",
)


def looks_like_blocked_page(text):
    """True if a 200 response is actually a refusal page rather than content.

    Some protections answer 200 with an interstitial. For a search page that is
    merely an empty cell; for an item page it is much worse, because "no item on
    this page" is how this project spells `withdrawn` -- so an undetected
    interstitial writes fabricated terminations into the survival analysis, which
    is the one series the whole liquidity estimate rests on.
    """
    if not text:
        return False
    lowered = text[:20000].lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def parse_retry_after(value, now=None):
    """Seconds to wait, from a `Retry-After` header. None if absent or unusable.

    RFC 7231 permits either delta-seconds or an HTTP-date, and servers send both.
    Coercing the header with `float()` raises ValueError on a date, and an
    exception raised inside an `except HTTPError` block is not caught by that
    try's sibling handlers -- so a server that dates its own backoff instruction
    would crash the run at exactly the moment it asked us to be patient. The
    politeness code is the last place allowed to fail that way.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (when - reference).total_seconds())


class PoliteSession:
    def __init__(self, user_agent=None, contact_email=None, require_contact=True):
        self.user_agent = user_agent or config.USER_AGENT
        resolved = config.CONTACT_EMAIL if contact_email is None else contact_email
        if resolved:
            self.user_agent = f"{self.user_agent} contact:{resolved}"
        # A request whose User-Agent cannot be answered is a request we cannot
        # honour an Article 14 access request against. Refuse before it is sent
        # rather than logging a placeholder we would have to apologise for.
        if require_contact and not config.contact_email_ok(resolved):
            raise MissingContactEmail(
                "TORI_CONTACT_EMAIL is not set to a deliverable address. Every outbound "
                "request must carry a working contact so a data subject can reach us. "
                "Set TORI_CONTACT_EMAIL, or pass require_contact=False for an offline test."
            )
        self._robots = {}
        self._lock = threading.Lock()
        self._robots_lock = threading.Lock()
        self._request_started = None
        self._last_request = {}
        self.audit_path = config.DATA_DIR / "request_audit.jsonl"

    def _robots_for(self, url):
        """Return `(parser, unreadable)` for this URL's origin.

        Failing closed is right: a URL we cannot verify as permitted must not be
        fetched. What was wrong was the *duration* and the *label*. A transient
        read failure set `disallow_all` and cached that parser for the life of
        the process, so one network blip turned every later request in that run
        into a local refusal with no request sent, and `sweep` then aborted with
        `status: error` having crawled nothing. Measured on this checkout: 12
        consecutive denials at 15:00:21, the same URL returning 200 at 15:00:52
        from the next process.

        It also mislabelled the refusal. An unreadable robots.txt is *unknown*,
        not *disallowed*, and the audit log is the Article 5(2) accountability
        artefact -- recording "robots.txt disallow" for a URL the site's own
        robots.txt permits is a false statement in the record we would show a
        regulator. So an unreadable read is never cached, is retried after a
        short cooldown, and is reported under its own reason.
        """
        parsed = urlparse(url)
        if parsed.hostname in ("tori.fi", "www.tori.fi"):
            self._check_tori_authority(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        root = f"{parsed.scheme}://{parsed.netloc}"
        now = time.time()
        with self._robots_lock:
            cached = self._robots.get(root)
            if cached is not None:
                parser, unreadable, failed_at = cached
                # A real verdict is cached for the process. An unreadable one is
                # only honoured until the cooldown expires, so recovery does not
                # require a restart but a failing host is not re-read per request.
                if not unreadable or now - failed_at < ROBOTS_RETRY_SECONDS:
                    return cached
            rp = RobotFileParser()
            rp.set_url(f"{root}/robots.txt")
            try:
                self._read_robots(rp)
            except (collection_control.CollectionHalted, AccessBlocked):
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
                log.warning("robots.txt unreadable for %s (%s); refusing this request "
                            "and retrying in %ss", root, exc, ROBOTS_RETRY_SECONDS)
                # Deny everything, but flag it as unreadable and do not cache it
                # as a verdict: the next request re-reads robots.txt.
                rp.disallow_all = True
                entry = (rp, True, now)
                self._robots[root] = entry
                return entry
            entry = (rp, False, now)
            self._robots[root] = entry
            return entry

    def _read_robots(self, parser):
        """Metadata fetch with authority, pacing, audit, and no recursive robots read."""
        url = parser.url
        self._throttle(url, check_robots=False)
        self._check_tori_authority(url)
        self._begin_request(url)
        req = urllib.request.Request(url, headers={'User-Agent': self.user_agent})
        try:
            opener = urllib.request.build_opener(_ToriRedirects(self))
            with opener.open(req, timeout=45) as response:
                payload = response.read()
                self._audit(url, response.status, len(payload))
                text = payload.decode('utf-8', 'replace')
                if looks_like_blocked_page(text):
                    mark_collection_stop(url, 'blocked')
                    raise AccessBlocked('Source refused robots metadata')
                if text.lstrip().lower().startswith(('<!doctype html', '<html')):
                    raise ValueError('robots metadata is HTML')
                parser.parse(text.splitlines())
        except urllib.error.HTTPError as exc:
            self._audit(url, exc.code, 0)
            if exc.code in BLOCK_STATUS_CODES:
                mark_collection_stop(url, 'blocked')
                raise AccessBlocked('Source refused robots metadata') from exc
            if exc.code == 429:
                mark_collection_stop(url, 'throttled', parse_retry_after(exc.headers.get('Retry-After') if exc.headers else None))
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            self._audit(url, 'NETWORK', 0)
            raise

    def allowed(self, url):
        parser, _unreadable, _at = self._robots_for(url)
        return parser.can_fetch(self.user_agent, url)

    def _robots_reason(self, url):
        """Why this URL is refused: our rules, or our inability to read them.

        Kept distinct because the audit log is the accountability record. An
        unreadable robots.txt means "we could not check", which is a different
        claim from "the site forbids this", and only one of the two is a fact
        about the site.
        """
        _parser, unreadable, _at = self._robots_for(url)
        return ("robots.txt unreadable, failing closed"
                if unreadable else "robots.txt disallow")

    def _crawl_delay(self, url):
        """The host's own requested minimum interval between requests, in seconds.

        `Crawl-delay` is a request the site owner makes explicit, so it outranks
        our configured rate: we slow down, never speed up past it. Returns None
        when the host does not ask. Tori's robots.txt does not set one, so this
        is currently inert against it -- it exists so that a host which does ask
        is obeyed without anyone having to remember to come and edit the rate.
        """
        if not config.RESPECT_CRAWL_DELAY:
            return None
        try:
            delay = self._robots_for(url)[0].crawl_delay(self.user_agent)
        except Exception as exc:  # noqa: BLE001 - a bad header must not stop collection
            log.warning("crawl-delay unreadable for %s (%s)", url, exc)
            return None
        try:
            delay = float(delay)
        except (TypeError, ValueError):
            return None
        return delay if delay > 0 else None

    def _throttle(self, url, rps=None, check_robots=True):
        host = urlparse(url).netloc
        state = collection_control.checkpoint()
        authority = 'www.tori.fi' if host in ('tori.fi', 'www.tori.fi') else host
        caps = collection_control.ceilings()
        ceiling = caps['source_rps']
        if rps is not None:
            ceiling = min(ceiling, caps['item_rps'])
            if rps != config.ITEM_RPS:
                ceiling = min(ceiling, rps)
        limit = min(ceiling, state['settings']['source_rps'], state['settings']['item_rps'] if rps is not None else ceiling)
        min_gap = 1.0 / limit
        # Read robots before taking the lock: _robots_for acquires the same
        # non-reentrant lock, so calling it inside would deadlock.
        crawl_delay = self._crawl_delay(url) if check_robots else None
        if crawl_delay:
            if crawl_delay > min_gap:
                log.info("honouring Crawl-delay %.2fs for %s (configured gap %.2fs)",
                         crawl_delay, host, min_gap)
            min_gap = max(min_gap, crawl_delay)
        with self._lock:
            # Share request start times between scheduler and dashboard
            # processes. A per-session dictionary alone cannot enforce a host
            # rate when two differently named collectors are running.
            import fcntl
            rate_dir = config.DATA_DIR / "rates"
            rate_dir.mkdir(parents=True, exist_ok=True)
            path = rate_dir / (hashlib.sha256(authority.encode()).hexdigest() + ".time")
            with path.open("a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.seek(0)
                try:
                    last = float(handle.read())
                except ValueError:
                    last = None
                if last is not None:
                    gap = time.time() - last
                    if gap < min_gap:
                        controlled = state['revision'] > 0 or collection_events.context()[0]
                        if controlled:
                            while gap < min_gap:
                                collection_control.wait(min(0.5, min_gap-gap))
                                state = collection_control.checkpoint()
                                effective = min(ceiling, state['settings']['source_rps'], state['settings']['item_rps'] if rps is not None else ceiling)
                                min_gap = max(1.0/effective, crawl_delay or 0)
                                gap = time.time()-last
                        else:
                            time.sleep(min_gap-gap)
                collection_control.checkpoint()
                now = time.time()
                handle.seek(0)
                handle.truncate()
                handle.write(str(now))
                handle.flush()
                self._last_request[host] = now

    def _begin_request(self, url, attempt=1):
        self._request_started = time.monotonic()
        collection_events.emit('request_started', {'host': urlparse(url).hostname, 'attempt': attempt})

    def _audit(self, url, status, bytes_len, note=""):
        host = urlparse(url).netloc
        if status not in ("NETWORK", "DENIED"):
            observability.record_request(host, status)
        else:
            if status == "DENIED":
                observability.record_robots_denied(host)
            else:
                observability.record_error(host, "network")
        run_id, job_id = collection_events.context()
        if run_id:
            collection_events.emit('request_completed' if status != 'DENIED' else 'request_denied',
                {'host': host, 'http_status': status, 'bytes': bytes_len,
                 'duration_ms': round((time.monotonic()-self._request_started)*1000, 1) if self._request_started else None},
                severity='warning' if status in ('NETWORK', 'DENIED') or isinstance(status, int) and status >= 400 else 'info')
        record = {
            'run_id': run_id, 'job_run_id': job_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "url": url,
            "status": status,
            "bytes": bytes_len,
            "user_agent": self.user_agent,
            "note": note,
        }
        with open(self.audit_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")

    def get(self, url, headers=None, max_attempts=4, check_robots=True, rps=None,
            retry_on_429=True):
        collection_control.checkpoint()
        if urlparse(url).hostname not in ("tori.fi", "www.tori.fi"):
            raise AuthorityBlocked("Unregistered collection source; use a reviewed source-specific adapter")
        if urlparse(url).hostname in ("tori.fi", "www.tori.fi"):
            stopped = collection_stop()
            if stopped:
                raise AccessBlocked(f"Tori collection {stopped['status']}; inspect collection_stop.json before resuming")
            self._check_tori_authority(url)
        if check_robots and not self.allowed(url):
            reason = self._robots_reason(url)
            self._audit(url, "DENIED", 0, reason)
            raise RobotsDenied(f"{reason}: {url}")

        request_headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip",
        }
        if headers:
            request_headers.update(headers)

        delay = 2.0
        for attempt in range(1, max_attempts + 1):
            self._throttle(url, rps=rps)
            if urlparse(url).hostname in ("tori.fi", "www.tori.fi"):
                self._check_tori_authority(url)
            collection_control.checkpoint()
            self._begin_request(url, attempt)
            req = urllib.request.Request(url, headers=request_headers)
            try:
                opener = (urllib.request.build_opener(_ToriRedirects(self))
                          if urlparse(url).hostname in ("tori.fi", "www.tori.fi") else None)
                with (opener.open(req, timeout=45) if opener else urllib.request.urlopen(req, timeout=45)) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    self._audit(url, resp.status, len(raw))
                    return raw
            except urllib.error.HTTPError as exc:
                self._audit(url, exc.code, 0, f"attempt {attempt}")
                if exc.code in BLOCK_STATUS_CODES:
                    mark_collection_stop(url, "blocked")
                    # Not transient, and not something to retry around. A refusal
                    # ends the request; stopping the run is the caller's job.
                    raise AccessBlocked(
                        f"{exc.code} from {urlparse(url).netloc} on {url}"
                    ) from exc
                if exc.code == 429 and not retry_on_429:
                    mark_collection_stop(url, "throttled", parse_retry_after(
                        exc.headers.get("Retry-After") if exc.headers else None))
                    # A 429 here is the platform saying the current rate is too
                    # high. It is a signal to stop, not to retry harder.
                    raise
                if exc.code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                    sleep_for = parse_retry_after(
                        exc.headers.get("Retry-After") if exc.headers else None,
                        now=datetime.now(timezone.utc),
                    )
                    if sleep_for is None:
                        sleep_for = delay
                    elif sleep_for > config.MAX_RETRY_AFTER_SLEEP:
                        # Sitting here for longer than a run should is not an
                        # option, and retrying early is the impolite choice.
                        # Ending the request lets the rotation resume later.
                        log.warning("Retry-After %ss on %s exceeds cap %ss; "
                                    "ending request", sleep_for, url,
                                    config.MAX_RETRY_AFTER_SLEEP)
                        raise
                    log.warning("HTTP %s on %s; sleeping %.1fs", exc.code, url, sleep_for)
                    if collection_events.context()[0] or collection_control.current()['revision']:
                        collection_control.wait(sleep_for)
                    else:
                        time.sleep(sleep_for)
                    delay *= 2
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                self._audit(url, "NETWORK", 0, str(exc)[:120])
                if attempt < max_attempts:
                    if collection_events.context()[0] or collection_control.current()['revision']:
                        collection_control.wait(delay)
                    else:
                        time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError(f"exhausted attempts for {url}")

    def _check_tori_authority(self, url):
        from . import permissions
        try:
            permissions.require_tori_url(url)
        except permissions.PermissionDenied as exc:
            self._audit(url, "DENIED", 0, str(exc))
            raise AuthorityBlocked(str(exc)) from None

    def get_status(self, url, max_attempts=3, retry_on_429=True):
        """HTTP status without raising on 404.

        A 404 is a data point, not an error: it is how a withdrawn or expired
        listing announces itself. Callers need the code, not an exception.
        """
        try:
            self.get(url, max_attempts=max_attempts, retry_on_429=retry_on_429)
            return 200
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404
            raise

    def get_text(self, url, **kwargs):
        return self.get(url, **kwargs).decode("utf-8", errors="replace")

    def get_json(self, url, **kwargs):
        return json.loads(self.get(url, **kwargs).decode("utf-8"))

    def save_raw(self, url, data, bucket=None):
        """Retain a response body for audit, under the configured retention.

        Item pages embed seller identity and precise coordinates. They are not
        retained: the parsed, redacted record is the artefact we keep, and a
        raw copy on disk would quietly undo the redaction.
        """
        if _looks_like_item_page(url) or "/recommerce/forsale/search" in url:
            return None
        digest = hashlib.sha256(data).hexdigest()[:16]
        target = (bucket or config.DATA_DIR / "raw") / digest
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{digest}.bin"
        if not path.exists():
            path.write_bytes(data)
        return path

    def enforce_retention(self):
        """Delete retained bodies and audit lines past their retention period.

        A retention period that is written down but never executed is not a
        retention period; this is the part that makes it true.
        """
        removed = {"raw": 0, "audit": 0, "listings": 0, "audit_unreadable": 0}
        from . import listing_analysis
        listing_analysis.purge()
        cutoff = time.time() - config.RAW_RETENTION_DAYS * 86400
        raw_root = config.DATA_DIR / "raw"
        if raw_root.exists():
            for path in raw_root.rglob("*.bin"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed["raw"] += 1
                except OSError:
                    continue
        if self.audit_path.exists():
            cutoff = time.time() - config.AUDIT_RETENTION_DAYS * 86400
            kept = []
            for line in self.audit_path.read_text().splitlines():
                try:
                    stamp = parse_ts(json.loads(line).get("at"))
                    if stamp and stamp.timestamp() < cutoff:
                        removed["audit"] += 1
                        continue
                except Exception:
                    # An unreadable audit line is not the same event as an expired
                    # one. Dropping it here used to shrink the Article 5(2)
                    # record silently: `removed["audit"]` counted only the lines
                    # that were deliberately expired, so a corrupted or truncated
                    # tail left no trace of how much evidence went missing. It is
                    # counted and surfaced instead, because a retention policy
                    # that quietly discards its own audit trail is not one.
                    removed["audit_unreadable"] += 1
                kept.append(line)
            if removed["audit"]:
                self.audit_path.write_text("\n".join(kept) + ("\n" if kept else ""))
        for kind, n in removed.items():
            if n:
                observability.record_retention(kind, n)
        # Owned purchase/sale records and their linked listings are preserved.
        # Children are deleted in one transaction before their parent listing.
        from . import db
        conn = db.connect()
        try:
            cutoff = datetime.fromtimestamp(
                time.time() - config.LISTING_RETENTION_DAYS * 86400,
                tz=timezone.utc).isoformat()
            with conn.transaction():
                removed["listings"] = db.purge_expired_listings(conn, cutoff)
                from . import parser_health
                removed["parser_checks"] = parser_health.expire(conn)
        finally:
            conn.close()
        if removed["listings"]:
            observability.record_retention("listings", removed["listings"])
        return removed


def _looks_like_item_page(url):
    return "/recommerce/forsale/item/" in url


def parse_ts(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
