"""Operational metrics, exported in Prometheus text format.

Why hand-rolled rather than `prometheus_client`: the project carries a hard
constraint of running on the Python standard library alone (README, "Setup").
Adding a metrics library to observe a system that collects under a legal
constraint is a poor trade, and the exposition format is a text format. This
module is the whole implementation: a thread-safe registry of counters and
gauges plus a renderer.

What is worth measuring here, and why it is this list:

* **Requests, by host and status.** The single number that tells you whether the
  platform is still tolerating our rate. A rising share of 403s is the early
  warning that matters, and it is visible here long before it is visible in the
  data.
* **429s specifically.** A throttle is the platform saying our rate is too
  high. We stop on it by design, so the counter is the record of how often that
  happened -- not a fault to be retried away.
* **Rows ingested per run.** Collection that silently writes nothing looks
  exactly like collection that has nothing to write.
* **Grid coverage and crawl age.** The staleness rotation is only as good as its
  staleness, and a rotation that has wedged is invisible until you check.
* **Retention.** A retention schedule that is not demonstrably executing is not a
  retention schedule, so what it last removed is exported rather than logged.

Deliberately not exported: anything identifying. No listing id, no seller, no
URL path beyond the host, no title. A metrics endpoint is scraped and stored for
longer than the rows it describes, and Article 14 covers this endpoint the same
as any other.
"""

import threading
import time
from datetime import datetime, timezone

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _now():
    return time.time()


class Registry:
    """Counters and gauges, keyed by (name, label tuple).

    Values are held in memory and reset when the process exits. That is a real
    limitation and it is stated here rather than hidden: these are process
    metrics, so a scheduled run that starts, does its work and exits leaves
    behind only what a scraper caught. Anything that must survive the process
    belongs in `sweep_runs` and `ingest_runs`, which are already persisted --
    the counters here are for watching a long-lived dashboard, and the tables
    are for the record.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {}
        self._gauges = {}
        self._help = {}

    # -- registration ----------------------------------------------------
    def declare(self, kind, name, help_text):
        with self._lock:
            self._help[name] = (kind, help_text)
            getattr(self, f"_{kind}s").setdefault(name, {})

    def counter(self, name, help_text=""):
        self.declare("counter", name, help_text)

    def gauge(self, name, help_text=""):
        self.declare("gauge", name, help_text)

    # -- mutation --------------------------------------------------------
    def inc(self, name, value=1, **labels):
        key = _label_key(labels)
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0) + value

    def set(self, name, value, **labels):
        key = _label_key(labels)
        with self._lock:
            self._gauges.setdefault(name, {})[key] = value

    def observe_duration(self, name, seconds, **labels):
        self.inc(name, seconds, **labels)
        self.inc(name + "_seconds_sum", seconds, **labels)
        self.set(name + "_seconds_max", max(
            _series_get(self._gauges, name + "_seconds_max", _label_key(labels), 0.0),
            float(seconds)), **labels)

    # -- reading ---------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return (
                {k: dict(v) for k, v in self._counters.items()},
                {k: dict(v) for k, v in self._gauges.items()},
                dict(self._help),
            )

    def reset(self):
        """Test affordance. Clears every series but keeps declarations."""
        with self._lock:
            self._counters = {name: {} for name in self._counters}
            self._gauges = {name: {} for name in self._gauges}

    # -- rendering -------------------------------------------------------
    def render(self):
        counters, gauges, help_map = self.snapshot()
        lines = []

        def emit(kind, series, name, values):
            kind_name, help_text = help_map.get(name, (kind, name))
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind_name}")
            if not values:
                return
            for labels, value in sorted(values.items()):
                if labels:
                    lines.append(f"{name}{{{labels}}} {_fmt(value)}")
                else:
                    lines.append(f"{name} {_fmt(value)}")

        for name in sorted(counters):
            emit("counter", counters, name, counters[name])
        for name in sorted(gauges):
            emit("gauge", gauges, name, gauges[name])

        return "\n".join(lines) + "\n"


def _label_key(labels):
    if not labels:
        return ""
    return ",".join(
        f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value):
    if isinstance(value, float):
        if value != value:            # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return "+Inf" if value > 0 else "-Inf"
        return repr(value)
    return str(value)


def _series_get(store, name, key, default):
    return store.get(name, {}).get(key, default)


REGISTRY = Registry()

# -- declared series -------------------------------------------------------
REGISTRY.counter(
    "toriresearch_http_requests_total",
    "Outbound requests made, by host and HTTP status.")
REGISTRY.counter(
    "toriresearch_http_errors_total",
    "Outbound requests that raised, by host and reason.")
REGISTRY.counter(
    "toriresearch_http_429_total",
    "Rate-limit responses received. Non-zero means the run ended early, by design.")
REGISTRY.counter(
    "toriresearch_robots_denied_total",
    "Requests refused locally because robots.txt disallowed the URL.")
REGISTRY.counter(
    "toriresearch_rows_ingested_total",
    "Listing rows written, by channel.")
REGISTRY.counter(
    "toriresearch_events_emitted_total",
    "Events appended, by event type.")
REGISTRY.counter(
    "toriresearch_retention_removed_total",
    "Records deleted by retention enforcement, by kind.")
REGISTRY.gauge(
    "toriresearch_grid_coverage_ratio",
    "Fraction of sweep targets crawled at least once.")
REGISTRY.gauge(
    "toriresearch_targets_never_crawled",
    "Sweep targets still awaiting a first crawl.")
REGISTRY.gauge(
    "toriresearch_mean_hours_since_crawl",
    "Mean age of a crawled grid target, in hours.")
REGISTRY.gauge(
    "toriresearch_listings_rows",
    "Rows in listings.")
REGISTRY.gauge(
    "toriresearch_listing_outcomes",
    "Listings by outcome_status. Evidence classes are never pooled here.")
REGISTRY.gauge(
    "toriresearch_listing_seller_type",
    "Listings by the platform's own private/business classification.")
REGISTRY.gauge(
    "toriresearch_listing_subplatform",
    "Listings by subplatform. ToriDiili is a structurally different market and "
    "is counted apart from an ordinary listing, never pooled with it.")
REGISTRY.gauge(
    "toriresearch_contact_email_deliverable",
    "1 when TORI_CONTACT_EMAIL is set to a deliverable address, else 0. "
    "At 0 the collector refuses to send.")
REGISTRY.gauge(
    "toriresearch_compliance_blockers",
    "Outstanding Article 14 / LIA preconditions, counted.")
REGISTRY.gauge(
    "toriresearch_build_info",
    "Always 1. Labels carry the normalisation and schema versions.")


# -- instrumentation -------------------------------------------------------
REGISTRY.gauge("toriresearch_collection_attention", "1 when the independent collection check requires attention.")
REGISTRY.gauge("toriresearch_backup_verified_recent", "1 when a local restore-tested backup is less than 24 hours old.")
REGISTRY.gauge("toriresearch_collection_capacity_days", "Complete current-policy tracking days, capped at seven.")
REGISTRY.gauge("toriresearch_collection_capacity_ready", "1 after seven complete usable tracking days for the current settings.")
REGISTRY.gauge("toriresearch_collection_daily_attempt_ceiling", "Configured daily detail-attempt ceiling, not observed throughput.")
REGISTRY.gauge("toriresearch_collection_daily_repeat_demand", "Repeat attempts required daily for the admitted follow-up cohort or legacy focused queue.")
REGISTRY.gauge("toriresearch_followup_active", "Active admitted follow-up tasks, including quarantine.")
REGISTRY.gauge("toriresearch_followup_due", "Ready admitted follow-up tasks due for dispatch.")
REGISTRY.gauge("toriresearch_followup_quarantined", "Admitted tasks requiring parser or error review.")


def record_request(host, status):
    REGISTRY.inc("toriresearch_http_requests_total", host=host, status=str(status))
    if str(status) == "429":
        REGISTRY.inc("toriresearch_http_429_total", host=host)


def record_error(host, reason):
    REGISTRY.inc("toriresearch_http_errors_total", host=host, reason=reason)


def record_robots_denied(host):
    REGISTRY.inc("toriresearch_robots_denied_total", host=host)


def record_rows(channel, n=1):
    REGISTRY.inc("toriresearch_rows_ingested_total", n, channel=channel)


def record_events(event_type, n=1):
    REGISTRY.inc("toriresearch_events_emitted_total", n, event_type=event_type)


def record_retention(kind, n):
    REGISTRY.inc("toriresearch_retention_removed_total", n, kind=kind)


def render():
    """Refresh the database-derived gauges, then render the exposition text.

    Gauges that require a query are computed here, at scrape time, rather than
    maintained incrementally: they are counts of tables this process does not
    own, and a cached value would be wrong the moment a scheduled run wrote to
    the database underneath the dashboard.
    """
    _refresh_db_gauges()
    REGISTRY.set("toriresearch_contact_email_deliverable",
                 1 if _contact_ok() else 0)
    _refresh_compliance()
    _refresh_collection_gauges()
    return REGISTRY.render()


def _refresh_collection_gauges():
    from . import collection_health
    result = collection_health.view()
    freshness, capacity = result["freshness"], result["capacity"]
    REGISTRY.set("toriresearch_collection_attention", int(freshness["status"] not in ("ok", "running")))
    REGISTRY.set("toriresearch_backup_verified_recent", int(freshness["backup"]["status"] == "ok"))
    REGISTRY.set("toriresearch_collection_capacity_days", result["complete_days"])
    REGISTRY.set("toriresearch_collection_capacity_ready", int(result["estimate_ready"]))
    REGISTRY.set("toriresearch_collection_daily_attempt_ceiling", capacity.get("daily_attempt_ceiling", 0))
    REGISTRY.set("toriresearch_collection_daily_repeat_demand", capacity.get("repeat_demand_per_day") or 0)
    cohort = result["followup"]
    REGISTRY.set("toriresearch_followup_active", cohort.get("active", 0))
    REGISTRY.set("toriresearch_followup_due", cohort.get("due", 0))
    REGISTRY.set("toriresearch_followup_quarantined", cohort.get("quarantined", 0))


def _contact_ok():
    try:
        from . import config
        return 1 if config.contact_email_ok() else 0
    except Exception:  # noqa: BLE001 - metrics must never break the scrape
        return 0


def _refresh_db_gauges():
    from . import db, schema
    try:
        for table in schema.TABLES:
            name = table["name"]
            if name in ("sweep_targets",):
                continue
            count = db.scalar(f"SELECT COUNT(*) FROM {name}")
            if count is None:
                continue
            if name == "listings":
                REGISTRY.set("toriresearch_listings_rows", count)
            if name == "sweep_runs":
                continue
    except Exception:  # noqa: BLE001 - a metrics scrape must not fail loudly
        return

    try:
        REGISTRY.set("toriresearch_targets_never_crawled",
                     db.scalar("SELECT COUNT(*) FROM sweep_targets "
                               "WHERE last_crawled_at IS NULL") or 0)
        total = db.scalar("SELECT COUNT(*) FROM sweep_targets") or 0
        never = db.scalar("SELECT COUNT(*) FROM sweep_targets "
                          "WHERE last_crawled_at IS NULL") or 0
        REGISTRY.set("toriresearch_grid_coverage_ratio",
                     (1 - never / total) if total else 0.0)
        from .ingest.sweep import coverage
        age = coverage().get("mean_hours_since_crawl")
        if age is not None:
            REGISTRY.set("toriresearch_mean_hours_since_crawl", float(age))
    except Exception:  # noqa: BLE001
        pass

    try:
        for row in db.query(
                "SELECT outcome_status, COUNT(*) AS n FROM listings "
                "GROUP BY outcome_status"):
            REGISTRY.set("toriresearch_listing_outcomes", row["n"],
                         outcome=str(row["outcome_status"]))
        for row in db.query(
                "SELECT seller_type_norm, COUNT(*) AS n FROM listings "
                "WHERE seller_type_norm IS NOT NULL GROUP BY 1"):
            REGISTRY.set("toriresearch_listing_seller_type", row["n"],
                         seller_type=str(row["seller_type_norm"]))
        for row in db.query(
                "SELECT subplatform, COUNT(*) AS n FROM listings GROUP BY 1"):
            REGISTRY.set("toriresearch_listing_subplatform", row["n"],
                         subplatform=str(row["subplatform"]))
    except Exception:  # noqa: BLE001
        pass


def _refresh_compliance():
    try:
        from . import compliance
        report = compliance.compliance_report()
        REGISTRY.set("toriresearch_compliance_blockers", len(report["blocking"]))
    except Exception:  # noqa: BLE001
        pass


def render_json():
    """Counters and gauges as a nested dict, for the dashboard's own UI.

    Label sets are rendered as a single joined string rather than as a dict
    keyed by a tuple. A tuple key is hashable and so fine in Python, but it is
    not a JSON type, and this output is handed straight to `json.dumps` -- which
    fails on it at runtime rather than at import, so the endpoint 500s on first
    use instead of never being written. Keys are `"host=tori.fi,status=200"`,
    which round-trips and stays greppable.
    """
    counters, gauges, _help = REGISTRY.snapshot()

    def shape(series):
        return {labels or "total": value for labels, value in series.items()}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counters": {name: shape(series) for name, series in counters.items()},
        "gauges": {name: shape(series) for name, series in gauges.items()},
    }


def snapshot_json():
    """Fresh gauges as a dict, for the operations panel.

    `render()` is what refreshes the database-derived gauges; the panel wants
    the same values as JSON rather than as exposition text, so it refreshes
    first and shapes afterwards. Refreshing matters here: a row count read at
    process start would be wrong the moment a scheduled run wrote underneath it.
    """
    render()
    return render_json()
