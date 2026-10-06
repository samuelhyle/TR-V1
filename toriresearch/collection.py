"""One collection scope and explicit measures of follow-up capacity."""

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db, metrics


def category_key(path):
    # Tori encodes depth in the first component: 0.93 -> 1.93.3210 ->
    # 2.93.3210.43. Descendants therefore do not share the literal ID prefix.
    parts = str(path).strip().split(".")
    return ".".join(parts[1:] if len(parts) > 1 and parts[0] in {"0", "1", "2"} else parts)


def is_below(path, parent):
    return path == parent or path.startswith(parent + ".")


def _like_prefix(value):
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


class Scope:
    """Match search paths, refined item IDs and historical category labels.

    Focus settings apply to Tori. Other platform collectors retain their scope.
    Unlabelled listings need a matching category ID to enter a focused queue.
    """

    def __init__(self, platform="tori", categories=None):
        # `categories` narrows the scope to named paths instead of reading the
        # configuration, so one configured focus path can be measured on its
        # own without mutating process-wide state.
        self.categories = (tuple(categories) if categories is not None
                           else tuple(config.FOCUS_CATEGORIES) if platform == "tori" else ())
        self.municipalities = tuple(config.FOCUS_MUNICIPALITIES) if platform == "tori" else ()
        self.paths, self.roots, self.chains = set(), set(), set()
        self.category_names = []
        self.bare_ids = defaultdict(set)
        if not self.categories:
            return
        from .ingest import category
        labels = category._label_map(platform)
        keyed = {category_key(path): label.strip() for path, label in labels.items()}
        id_paths = defaultdict(set)
        for key in keyed:
            id_paths[key.rsplit(".", 1)[-1]].add(key)
        for path in self.categories:
            parent = category_key(path)
            parts = parent.split(".")
            self.category_names.append(" > ".join(keyed[".".join(parts[:n])]
                                       for n in range(1, len(parts)+1)
                                       if ".".join(parts[:n]) in keyed) or path)
            self.paths.update(f"{depth}.{parent}" for depth in range(3))
            # Also retain literal prefixes for nonstandard/older cached paths.
            self.paths.add(path)
            root = keyed.get(parent.split(".")[0])
            if root and "." not in parent:
                self.roots.add(root)
            descendants = {key: label for key, label in keyed.items() if is_below(key, parent)}
            if "." in parent:
                descendants.setdefault(parent, keyed.get(parent))
            for key, leaf in descendants.items():
                bare_id = key.rsplit(".", 1)[-1]
                # A shortened ID with multiple homes needs its measured label
                # chain; the numeric suffix alone cannot resolve membership.
                if len(id_paths[bare_id]) <= 1:
                    self.bare_ids[root].add(bare_id)
                if leaf and root:
                    # Item pages retain root and leaf; search records may retain
                    # all intermediate labels. Accept both measured shapes.
                    self.chains.add(root + " > " + leaf if leaf != root else root)
                    parts = key.split(".")
                    chain = [keyed[".".join(parts[:n])] for n in range(1, len(parts)+1)
                             if ".".join(parts[:n]) in keyed]
                    self.chains.add(" > ".join(chain))

    def _category_sql(self, field, listings=False):
        clauses, params = [], []
        for path in sorted(self.paths):
            clauses.append(f"({field}=? OR {field} LIKE ? ESCAPE '!')")
            params.extend((path, _like_prefix(path) + ".%"))
        if listings:
            for root, ids in sorted(self.bare_ids.items(), key=lambda pair: pair[0] or ""):
                marks = ",".join("?" for _ in ids)
                clause = f"{field} IN ({marks})"
                params.extend(sorted(ids))
                if root:
                    clause += " AND (category_root_norm IS NULL OR TRIM(category_root_norm)=?)"
                    params.append(root)
                clauses.append("(" + clause + ")")
            for label_field, labels in (("category_root_norm", self.roots), ("category_norm", self.chains)):
                if labels:
                    clauses.append(f"TRIM({label_field}) IN (" + ",".join("?" for _ in labels) + ")")
                    params.extend(sorted(labels))
        return "(" + " OR ".join(clauses) + ")", params

    def conditions(self, grid=False):
        conditions, params = [], []
        if self.categories:
            clause, values = self._category_sql("category_path" if grid else "category_raw_id", not grid)
            conditions.append(clause)
            params.extend(values)
        if self.municipalities:
            field = "location_label" if grid else "municipality_norm"
            conditions.append(f"TRIM({field}) IN (" + ",".join("?" for _ in self.municipalities) + ")")
            params.extend(self.municipalities)
        return conditions, params

    def matches(self, row):
        if row.get("platform", "tori") != "tori":
            return True
        if self.municipalities and (row.get("municipality_norm") or "").strip() not in self.municipalities:
            return False
        if not self.categories:
            return True
        raw = str(row.get("category_raw_id") or "").strip()
        root = (row.get("category_root_norm") or "").strip()
        chain = (row.get("category_norm") or "").strip()
        return (any(is_below(raw, path) for path in self.paths)
                or any(raw in ids and (not home or not root or root == home)
                       for home, ids in self.bare_ids.items())
                or root in self.roots or chain in self.chains)


class VerticalCounters:
    """Aggregate dispatch-time scope membership without retaining listing identity."""

    def __init__(self, platform="tori"):
        self.scopes = [(path, Scope(platform, categories=(path,)))
                       for path in config.FOCUS_CATEGORIES] if platform == "tori" else []
        self.values = {path: {} for path, _scope in self.scopes}

    def add(self, row, **counts):
        for path, scope in self.scopes:
            if scope.matches(row):
                dest = self.values[path]
                for key, value in counts.items():
                    dest[key] = dest.get(key, 0) + value


def task_snapshot():
    """Read current admission tasks without admitting or claiming any work."""
    if not config.FOLLOWUP_ENABLED:
        return []
    from . import followup
    return db.query(
        "SELECT t.*, e.mode, e.purpose FROM followup_tasks t "
        "JOIN followup_enrollments e ON e.enrollment_id=t.enrollment_id "
        "WHERE t.state IN ('ready','leased','quarantined') AND e.ended_at IS NULL "
        "AND e.policy_key=?", (followup.policy_key(),))


def queue_summary(rows, now=None, scope=None, tasks=None):
    """Actual due tasks for this slice, distinct from historical probe attempts."""
    now = now or datetime.now(timezone.utc)
    scope = scope or Scope()
    eligible = {row['listing_pk']: row for row in rows
                if row['platform'] == 'tori' and row['outcome_status'] == 'unknown'
                and row['listing_id'] is not None and scope.matches(row)}
    if not config.FOLLOWUP_ENABLED:
        historical = summarize(list(eligible.values()), now, scope)
        due = historical['n_due_in_scope']
        return {'enabled': False, 'eligible': len(eligible), 'admitted': None,
                'not_admitted': None, 'successful': None, 'awaiting_first': None,
                'due': due, 'due_first': historical['n_due_first_in_scope'],
                'due_repeat': historical['n_due_repeat_in_scope'],
                'retry_due': None, 'leased': None, 'quarantined': None,
                'overdue_repeat': None, 'minimum_runs': historical['probe_runs_at_budget']}
    members = [task for task in (task_snapshot() if tasks is None else tasks)
               if task['listing_pk'] in eligible]
    due = [task for task in members if task['state'] == 'ready'
           and (stamp := metrics.parse_ts(task['due_at'])) is not None and stamp <= now]
    successful = sum(bool(task['last_success_at']) for task in members)
    from . import followup
    return {'enabled': True, 'eligible': len(eligible), 'admitted': len(members),
            'by_purpose': {name: sum(followup.purpose(task) == name for task in members) for name in ('baseline','opportunity','exploration')},
            'not_admitted': max(0, len(eligible)-len(members)),
            'successful': successful, 'awaiting_first': len(members)-successful,
            'due': len(due), 'due_first': sum(not task['last_success_at'] for task in due),
            'due_repeat': sum(bool(task['last_success_at']) for task in due),
            'retry_due': sum(task['failure_count'] > 0 for task in due),
            'leased': sum(task['state'] == 'leased' for task in members),
            'quarantined': sum(task['state'] == 'quarantined' for task in members),
            'overdue_repeat': sum(bool((stamp := metrics.parse_ts(task['last_success_at']))
                                      and (now-stamp).total_seconds() > config.PROBE_INTERVAL_HOURS*3600)
                                  for task in members),
            'minimum_runs': math.ceil(len(due)/config.ITEM_BUDGET) if config.ITEM_BUDGET > 0 else None}


def probe_conditions(now=None):
    """The default probe queue, shared with backlog reporting."""
    now = now or datetime.now(timezone.utc)
    field = "last_probed_at" if config.POSTGRES_DSN else "julianday(last_probed_at)"
    cutoff = "?" if config.POSTGRES_DSN else "julianday(?)"
    return ["listing_id IS NOT NULL", "outcome_status='unknown'",
            f"(last_probed_at IS NULL OR {field} <= {cutoff})"], [
                (now - timedelta(hours=config.PROBE_INTERVAL_HOURS)).isoformat()]


def repeat_probe_reserve(budget):
    """Reserve repeat capacity while keeping a first-probe slot when possible.

    A one-item run prioritizes a due repeat; an empty queue lends its capacity.
    """
    if budget <= 0:
        return 0
    return min(max(1, budget-1), max(1, math.ceil(budget*config.PROBE_REPEAT_SHARE)))


def probe_allocation(budget, first_count, repeat_count):
    """Bounded selection with both directions of capacity borrowing."""
    repeat = min(repeat_count, repeat_probe_reserve(budget))
    first = min(first_count, max(0, budget-repeat))
    repeat = min(repeat_count, max(0, budget-first))
    return first, repeat


def summarize(rows, now=None, scope=None, product_types=None):
    """Exact counts; capacity is bounded runs, with no assumed daily cadence."""
    from . import analysis
    now = now or datetime.now(timezone.utc)
    scope = scope or Scope()
    cutoff = now - timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    due_cutoff = now - timedelta(hours=config.PROBE_INTERVAL_HOURS)
    unresolved = [row for row in rows if row["outcome_status"] == "unknown" and row["listing_id"] is not None]
    never = [row for row in unresolved if row["last_probed_at"] is None]
    due = []
    for row in unresolved:
        probed_at = metrics.parse_ts(row["last_probed_at"])
        if row["last_probed_at"] is None or (probed_at is not None and probed_at <= due_cutoff):
            due.append(row)
    in_scope = [row for row in rows if scope.matches(row)]
    due_in_scope = [row for row in due if scope.matches(row)]
    repeat_in_scope = [row for row in due_in_scope if row["last_probed_at"] is not None]
    probed = sum(row["last_probed_at"] is not None for row in rows)
    known = sum(bool(row["condition_norm"] and row["condition_norm"] != "unknown") for row in rows)
    fresh = [row for row in rows if analysis.is_fresh(row, cutoff)]
    classified = sum(row["listing_pk"] in (product_types or {}) for row in rows)
    ages = [(now - stamp).total_seconds()/3600 for row in due_in_scope
            if (stamp := metrics.parse_ts(row["last_probed_at"] or row["first_seen_at"])) is not None]
    repeat_ages = [(now-metrics.parse_ts(row["last_probed_at"])).total_seconds()/3600
                   for row in repeat_in_scope]
    first_ages = [(now-stamp).total_seconds()/3600 for row in due_in_scope
                  if row["last_probed_at"] is None
                  and (stamp := metrics.parse_ts(row["first_seen_at"])) is not None]
    def p95(values):
        return round(sorted(values)[max(0, math.ceil(len(values)*0.95)-1)], 1) if values else None
    budget = config.ITEM_BUDGET
    return {"n_tracked": len(rows), "n_unresolved": len(unresolved),
            "n_probe_attempted": probed, "probe_attempt_coverage": probed/len(rows) if rows else None,
            "n_never_probed": len(never), "n_due": len(due), "n_due_repeat": len(due)-len(never),
            "n_in_collection_scope": len(in_scope), "n_due_in_scope": len(due_in_scope),
            "n_due_first_in_scope": len(due_in_scope)-len(repeat_in_scope),
            "n_due_repeat_in_scope": len(repeat_in_scope),
            "n_known_condition": known, "condition_coverage": known/len(rows) if rows else None,
            "n_fresh_available": len(fresh), "n_fresh_priced": sum(analysis.is_priced(row, cutoff) for row in fresh),
            "n_classified": classified, "classification_coverage": classified/len(rows) if rows else None,
            "oldest_due_hours": round(max(ages), 1) if ages else None,
            "oldest_due_repeat_hours": round(max(repeat_ages), 1) if repeat_ages else None,
            "p95_first_check_age_hours": p95(first_ages),
            "p95_repeat_lateness_hours": p95([max(0, age-config.PROBE_INTERVAL_HOURS) for age in repeat_ages]),
            "item_budget": budget, "probe_interval_hours": config.PROBE_INTERVAL_HOURS,
            "probe_repeat_share": config.PROBE_REPEAT_SHARE,
            "repeat_probe_reserve": repeat_probe_reserve(budget),
            "probe_runs_at_budget": math.ceil(len(due_in_scope)/budget) if budget > 0 else None,
            "capacity_note": "Minimum full-budget runs for the current in-scope backlog; new arrivals, repeat checks and errors add work. Repeat reservations apply across the entire configured scope; unused slots are borrowed by the other queue. Probe timestamps include failed attempts."}


def _sweep_policy():
    """The scheduled sweep, and the ceiling a browser may ask for instead of it."""
    from . import compliance
    return {"budget": config.SWEEP_BUDGET, "pages": config.SWEEP_PAGES,
            "refresh_hours": config.SWEEP_REFRESH_HOURS,
            "productive_refresh_hours": config.SWEEP_PRODUCTIVE_REFRESH_HOURS,
            "max_refresh_hours": config.SWEEP_MAX_REFRESH_HOURS,
            "traversal_max_age_hours": config.SWEEP_TRAVERSAL_MAX_AGE_HOURS,
            "caps": dict(compliance.ALLOWED_ACTIONS["sweep"])}


def coverage(filters=None):
    """Configured collection cohort or an explicitly selected stored segment."""
    from . import analysis, followup
    scope = Scope()
    where, params, applied = metrics.listing_filters(**(
        {"platform": "tori", "subplatform": None} if filters is None else filters))
    if filters is None:
        focused, values = scope.conditions()
        where.extend(focused)
        params.extend(values)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = db.query("SELECT " + analysis.FIELDS + " FROM listings" + clause, params)
    cohort = followup.status()
    summary = summarize(rows, scope=scope, product_types=analysis.accepted_product_types(rows))
    if filters is None and cohort.get("enabled"):
        summary["all_listing_probe_runs_at_budget"] = summary["probe_runs_at_budget"]
        summary["probe_runs_at_budget"] = math.ceil(cohort.get("due", 0)/config.ITEM_BUDGET) if config.ITEM_BUDGET > 0 else None
        summary["capacity_note"] = (
            "Listing first/repeat counts describe historical attempts across all stored focused listings. "
            "Minimum runs counts only ready, due tasks in the admitted cohort. Unadmitted discoveries are retained "
            "outside automatic follow-up. Task repeats use successful checks; failed attempts retry separately. "
            "Reservations and per-vertical fairness apply within the unchanged total budget.")
    return {"filters": applied, "followup": cohort, "focus_categories": list(config.FOCUS_CATEGORIES),
            "focus_category_names": scope.category_names,
            "focus_municipalities": list(config.FOCUS_MUNICIPALITIES),
            "run_budgets": {"sweep": config.SWEEP_BUDGET, "itempage": config.ITEM_BUDGET,
                            "enrich": config.ENRICH_BUDGET},
            "sweep_policy": _sweep_policy(),
            "classification_quality": analysis.classification_quality(rows),
            **summary}
