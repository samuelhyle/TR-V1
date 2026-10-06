"""Runtime configuration. No secrets in source; credentials come from env vars."""

import os
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_ENV_PREFIXES = ("TORI_", "EBAY_")
# Set TORI_NO_ENV_FILE=1 to skip this. Tests that need a hermetic environment
# should use it rather than relying on the checkout having no local.env.
_ENV_FILE = Path(
    os.environ.get("TORI_ENV_FILE", ROOT / "config" / "local.env"))


def _load_env_file(path=None):
    """Read `config/local.env` into the environment before anything reads it.

    Credentials live in a gitignored file, and only some launchers were loading
    it: `collect.sh` sources it explicitly, but a bare `python3 -m
    toriresearch.cli` did not, and neither did the dashboard's action
    subprocesses, which inherit `os.environ` from whatever shell launched the
    server. The result was a command that works when you remember to export the
    email and fails with an opaque traceback when you do not -- and a dashboard
    whose Collect button silently does nothing.

    Only the two prefixes this project uses are honoured, and an already-set
    variable always wins, so an explicit environment or a test monkeypatch is
    never overridden by the file.
    """
    target = Path(path) if path is not None else _ENV_FILE
    if os.environ.get("TORI_NO_ENV_FILE") == "1" or not target.is_file():
        return 0
    loaded = 0
    try:
        text = target.read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key.startswith(_ENV_PREFIXES) or key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


_load_env_file()

DATA_DIR = Path(os.environ.get("TORI_DATA_DIR", ROOT / "data"))
REPORTS_DIR = Path(os.environ.get("TORI_REPORTS_DIR", ROOT / "reports"))
PERMISSION_REGISTRY_PATH = os.environ.get("TORI_PERMISSION_REGISTRY")

SQLITE_PATH = Path(os.environ.get("TORI_DB", DATA_DIR / "research.sqlite"))
POSTGRES_DSN = os.environ.get("TORI_PG_DSN")
SQLITE_MMAP_BYTES = int(os.environ.get("TORI_SQLITE_MMAP_BYTES", "0"))
if not 0 <= SQLITE_MMAP_BYTES <= 536870912:
    raise ValueError("SQLite read mapping must be between 0 and 512 MiB")
# How long a writer waits for the write lock before giving up. The default of 0
# made every query fail the instant the collector held a transaction, which the
# dashboard reported as a request failure rather than as the contention it is.
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("TORI_SQLITE_BUSY_TIMEOUT_MS", "15000"))
if not 0 <= SQLITE_BUSY_TIMEOUT_MS <= 600000:
    raise ValueError("SQLite busy timeout must be between 0 and 600000 ms")
# Attempts per connection open. One is the old behaviour; the extra attempts
# exist for the lock and header races that clear on their own within
# milliseconds.
SQLITE_CONNECT_ATTEMPTS = int(os.environ.get("TORI_SQLITE_CONNECT_ATTEMPTS", "3"))
if not 1 <= SQLITE_CONNECT_ATTEMPTS <= 10:
    raise ValueError("SQLite connect attempts must be between 1 and 10")

USER_AGENT = os.environ.get(
    "TORI_USER_AGENT",
    "toriresearch/0.1 (+market research)",
)

CONTACT_EMAIL = os.environ.get("TORI_CONTACT_EMAIL", "")

# A contact address in the User-Agent is the Article 14 notice's only delivery
# mechanism to a data subject who finds us. Sending requests without a real one
# is worse than not sending them, so this is refused rather than defaulted.
_PLACEHOLDER_EMAILS = {"", "set-me", "set-me@example.com", "set tori_contact_email",
                       "you@example.com"}


def contact_email_ok(address=None):
    """Reject placeholders and malformed addresses; delivery needs operator verification."""
    raw = CONTACT_EMAIL if address is None else address
    if not isinstance(raw, str):
        return False
    candidate = raw.strip()
    if candidate.lower() in _PLACEHOLDER_EMAILS:
        return False
    return bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+", candidate))


def _request_rate(name, default):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return value


RATE_LIMIT = {
    "tori.fi": (_request_rate("TORI_RPS", "1"), 1.0),
    "www.tori.fi": (_request_rate("TORI_RPS", "1"), 1.0),
    "api.ebay.com": (_request_rate("EBAY_RPS", "4"), 1.0),
    "api.sandbox.ebay.com": (_request_rate("EBAY_RPS", "4"), 1.0),
    "identity.ebay.com": (_request_rate("EBAY_RPS", "4"), 1.0),
    "default": (1, 1.0),
}

# A `Crawl-delay` in a site's robots.txt is that owner naming their own limit,
# so it outranks whatever rate is configured here: we may only ever slow down to
# meet it. Off by default only so the behaviour can be disproved in a test.
RESPECT_CRAWL_DELAY = os.environ.get("TORI_RESPECT_CRAWL_DELAY", "1") == "1"

# The longest a single request will sit waiting for a server that told us to
# wait. Beyond this the request ends instead of sleeping: a stale or absurd
# Retry-After must not wedge a scheduled run, and retrying earlier than asked is
# the one option that is definitely wrong.
MAX_RETRY_AFTER_SLEEP = float(os.environ.get("TORI_MAX_RETRY_AFTER_SLEEP", "900"))

# Item pages are a different cost class from search pages: one request buys one
# listing instead of fifty. They are also the only channel that yields a
# terminal state, so they get their own budget rather than sharing the sweep's.
ITEM_RPS = _request_rate("TORI_ITEM_RPS", "0.5")
ITEM_MAX_ATTEMPTS = int(os.environ.get("TORI_ITEM_ATTEMPTS", "3"))
COLLECTION_INTERVAL_SECONDS = int(os.environ.get("TORI_COLLECTION_INTERVAL_SECONDS", "3600"))

SWEEP_BUDGET = int(os.environ.get("TORI_SWEEP_BUDGET", "120"))
SWEEP_PAGES = int(os.environ.get("TORI_SWEEP_PAGES", "1"))
SWEEP_REFRESH_HOURS = float(os.environ.get("TORI_SWEEP_REFRESH_HOURS", "168"))
SWEEP_PRODUCTIVE_REFRESH_HOURS = float(os.environ.get("TORI_SWEEP_PRODUCTIVE_REFRESH_HOURS", "24"))
SWEEP_MAX_REFRESH_HOURS = float(os.environ.get("TORI_SWEEP_MAX_REFRESH_HOURS", "336"))
SWEEP_TRAVERSAL_MAX_AGE_HOURS = float(os.environ.get("TORI_SWEEP_TRAVERSAL_MAX_AGE_HOURS", "24"))
FOCUS_NATIONAL_DISCOVERY = os.environ.get("TORI_FOCUS_NATIONAL_DISCOVERY", "0") == "1"
SWEEP_NATIONAL_SHARE = float(os.environ.get("TORI_SWEEP_NATIONAL_SHARE", "0.6"))
if not math.isfinite(SWEEP_NATIONAL_SHARE) or not 0 < SWEEP_NATIONAL_SHARE < 1:
    raise ValueError("National discovery share must be between zero and one")
if (not all(math.isfinite(value) and value > 0 for value in
            (SWEEP_REFRESH_HOURS, SWEEP_PRODUCTIVE_REFRESH_HOURS, SWEEP_MAX_REFRESH_HOURS, SWEEP_TRAVERSAL_MAX_AGE_HOURS))
        or SWEEP_MAX_REFRESH_HOURS < SWEEP_REFRESH_HOURS or SWEEP_PRODUCTIVE_REFRESH_HOURS > SWEEP_REFRESH_HOURS):
    raise ValueError("Sweep intervals must be finite, positive, and maximum refresh at least the desired refresh")
ITEM_BUDGET = int(os.environ.get("TORI_ITEM_BUDGET", "120"))
PROBE_INTERVAL_HOURS = float(os.environ.get("TORI_PROBE_INTERVAL_HOURS", "24"))
# Both queues retain capacity when a run can hold at least two items.
PROBE_REPEAT_SHARE = float(os.environ.get("TORI_PROBE_REPEAT_SHARE", "0.5"))
if not math.isfinite(PROBE_REPEAT_SHARE) or not 0 < PROBE_REPEAT_SHARE < 1:
    raise ValueError("TORI_PROBE_REPEAT_SHARE must be finite and between 0 and 1 (exclusive)")
FOLLOWUP_ENABLED = os.environ.get("TORI_FOLLOWUP_ENABLED", "1") == "1"
# Zero derives a conservative bound from the existing repeat reservation.
FOLLOWUP_MAX_ACTIVE = int(os.environ.get("TORI_FOLLOWUP_MAX_ACTIVE", "0"))
FOLLOWUP_DISCRETIONARY_MAX_ACTIVE = int(os.environ.get("TORI_FOLLOWUP_DISCRETIONARY_MAX_ACTIVE", "0"))
if FOLLOWUP_DISCRETIONARY_MAX_ACTIVE < 0:
    raise ValueError("Discretionary follow-up cap must be nonnegative")
if FOLLOWUP_MAX_ACTIVE < 0 or not math.isfinite(PROBE_INTERVAL_HOURS) or PROBE_INTERVAL_HOURS <= 0:
    raise ValueError("Follow-up cap must be nonnegative and probe interval finite and positive")
PANEL_MAX_AGE_HOURS = float(os.environ.get("TORI_PANEL_MAX_AGE_HOURS", "168"))
# Optional paths and municipality labels, shared by discovery, probes and enrichment.
FOCUS_CATEGORIES = tuple(p.strip() for p in os.environ.get(
    "TORI_FOCUS_CATEGORIES", "").split(",") if p.strip())
FOCUS_MUNICIPALITIES = tuple(p.strip() for p in os.environ.get(
    "TORI_FOCUS_MUNICIPALITIES", "").split(",") if p.strip())

# Pseudonymous seller linkage. The platform publishes an ownerId on item pages;
# the parser reduces it to an HMAC and the raw value is never stored, logged or
# exported. Seller-level rows are only written while a salt is configured, so
# the default posture is unchanged from the minimisation rule.
#
# This salt is durable: rotating or losing it relabels every seller and resets
# the archetype, concentration and relist series built on top of them. Set it
# once in config/local.env and leave it alone. TORI_SELLER_HASH=0 turns the
# feature off without discarding the salt.
SELLER_KEY_SALT = os.environ.get("TORI_SELLER_SALT", "")
SELLER_KEY_ENABLED = (
    os.environ.get("TORI_SELLER_HASH", "1") == "1" and bool(SELLER_KEY_SALT.strip())
)

EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")
EBAY_MARKETPLACE_ID = os.environ.get("EBAY_MARKETPLACE_ID", "")
EBAY_SANDBOX = os.environ.get("EBAY_SANDBOX", "0") == "1"
EBAY_RUNAME = os.environ.get("EBAY_RUNAME", "")
EBAY_SANDBOX_CLIENT_ID = os.environ.get("EBAY_SANDBOX_CLIENT_ID", "")
EBAY_SANDBOX_CLIENT_SECRET = os.environ.get("EBAY_SANDBOX_CLIENT_SECRET", "")
EBAY_SANDBOX_RUNAME = os.environ.get("EBAY_SANDBOX_RUNAME", "")

SUBPLATFORMS = ("standard", "diili")

COVERAGE_COLLECTING = "collecting"
COVERAGE_GONE = "gone_uncertain"

MIN_N_FOR_REPORT = int(os.environ.get("TORI_MIN_N", "30"))

# ---------------------------------------------------------------------------
# Local enrichment model
# ---------------------------------------------------------------------------
# Local only, by decision and not by omission. Nothing here may point at a
# network host: enrichment reads seller-authored free text, and sending that off
# the machine is a transfer decision, not a default. `enrich/provider.py`
# resolves the URL and refuses anything that is not loopback.
LLM_BASE_URL = os.environ.get("TORI_LLM_URL", "http://127.0.0.1:8080/v1")
LLM_API_KEY = os.environ.get("TORI_LLM_KEY", "")
LLM_MODEL = os.environ.get("TORI_LLM_MODEL", "local")
LLM_MODEL_REVISION = os.environ.get("TORI_LLM_MODEL_REVISION", "unspecified")
LLM_TIMEOUT = int(os.environ.get("TORI_LLM_TIMEOUT", "180"))
LLM_CONCURRENCY = int(os.environ.get("TORI_LLM_CONCURRENCY", "4"))
LLM_ENABLED = os.environ.get("TORI_LLM_ENABLED", "1") == "1"
LISTING_ANALYSIS_ENABLED = os.environ.get("TORI_LISTING_ANALYSIS_ENABLED", "1") == "1"
ANALYST_ENABLED = os.environ.get("TORI_ANALYST_ENABLED", "1") == "1"
ANALYST_TIMEOUT = max(5, min(180, int(os.environ.get("TORI_ANALYST_TIMEOUT", "90"))))

# Enrichment runs off the collection path. The collector must be identical with
# this at 0, so a model outage can never stall or alter collection.
ENRICH_BUDGET = int(os.environ.get("TORI_ENRICH_BUDGET", "60"))
ENRICH_RUNTIME_SECONDS = float(os.environ.get("TORI_ENRICH_RUNTIME_SECONDS", str(min(600, max(1, COLLECTION_INTERVAL_SECONDS // 3)))))
ENRICH_FRESH_SHARE = float(os.environ.get("TORI_ENRICH_FRESH_SHARE", "0.25"))
if not math.isfinite(ENRICH_FRESH_SHARE) or not 0 <= ENRICH_FRESH_SHARE < 1:
    raise ValueError("Fresh classification share must be at least zero and below one")
PARSER_DIAGNOSTIC_DAYS = int(os.environ.get("TORI_PARSER_DIAGNOSTIC_DAYS", "7"))
if not math.isfinite(ENRICH_RUNTIME_SECONDS) or not 1 <= ENRICH_RUNTIME_SECONDS <= 1800:
    raise ValueError("Classification runtime must be between 1 and 1800 seconds")
if not 1 <= PARSER_DIAGNOSTIC_DAYS <= 30:
    raise ValueError("Parser diagnostic retention must be between 1 and 30 days")

# The bounded vocabulary the classifier is allowed to emit. This is the single
# most important file in the enrichment layer: an open vocabulary is both the
# prompt-injection vector and the reason statistical power per cell collapses.
ENRICH_VOCAB_PATH = Path(
    os.environ.get("TORI_ENRICH_VOCAB", ROOT / "config" / "vocabulary.yaml"))

# Prompts are versioned and the version is hashed into the cache key, so a
# prompt change never silently reuses stale classifications.
ENRICH_PROMPT_VERSION = "v2-offer-object"

# Retention. Article 5(1)(e) requires a documented, enforced limit; indefinite
# row-level retention of marketplace data was found unlawful in TSV 26/2020.
RAW_RETENTION_DAYS = int(os.environ.get("TORI_RAW_RETENTION_DAYS", "14"))
LISTING_RETENTION_DAYS = int(os.environ.get("TORI_LISTING_RETENTION_DAYS", "400"))
AUDIT_RETENTION_DAYS = int(os.environ.get("TORI_AUDIT_RETENTION_DAYS", "400"))

# Local recovery snapshots. Only automatically managed backups are rotated.
BACKUP_RETENTION_DAYS = int(os.environ.get("TORI_BACKUP_RETENTION_DAYS", "7"))
COLLECTION_RUNTIME_SECONDS = float(os.environ.get("TORI_COLLECTION_RUNTIME_SECONDS", str(min(3300, COLLECTION_INTERVAL_SECONDS - 60))))
COLLECTION_REPORT_SECONDS = float(os.environ.get("TORI_COLLECTION_REPORT_SECONDS", str(min(120, max(15, COLLECTION_INTERVAL_SECONDS // 20)))))
COLLECTION_OBSERVER_SECONDS = int(os.environ.get("TORI_COLLECTION_OBSERVER_SECONDS", "300"))
COLLECTION_RUN_RETENTION_DAYS = int(os.environ.get("TORI_COLLECTION_RUN_RETENTION_DAYS", "90"))
ENRICH_REPAIR_BATCH = int(os.environ.get("TORI_ENRICH_REPAIR_BATCH", "500"))
ENRICH_REPAIR_BATCHES = int(os.environ.get("TORI_ENRICH_REPAIR_BATCHES", "4"))
if BACKUP_RETENTION_DAYS < 1 or COLLECTION_INTERVAL_SECONDS < 900:
    raise ValueError("Backup retention must be positive and collection interval at least 900 seconds")
if (not math.isfinite(COLLECTION_RUNTIME_SECONDS) or not 60 <= COLLECTION_RUNTIME_SECONDS <= 3300
        or COLLECTION_RUNTIME_SECONDS >= COLLECTION_INTERVAL_SECONDS
        or not math.isfinite(COLLECTION_REPORT_SECONDS) or not 1 <= COLLECTION_REPORT_SECONDS <= 120
        or ENRICH_RUNTIME_SECONDS + 15 + 3 * COLLECTION_REPORT_SECONDS + 30 >= COLLECTION_RUNTIME_SECONDS):
    raise ValueError("Collection runtime must fit the cadence and reserve classification, reports and shutdown")
if (not 30 <= COLLECTION_OBSERVER_SECONDS <= 3600 or not 7 <= COLLECTION_RUN_RETENTION_DAYS <= 400
        or not 1 <= ENRICH_REPAIR_BATCH <= 500 or not 1 <= ENRICH_REPAIR_BATCHES <= 20
        or min(SWEEP_BUDGET, ITEM_BUDGET, ENRICH_BUDGET) < 0 or SWEEP_PAGES < 1):
    raise ValueError("Collection budgets, observer, repair batches and operational retention are invalid")

DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
