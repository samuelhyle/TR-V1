"""Read-only model research views and bounded, explicit local CLI jobs.

The HTTP process never imports scientific packages or unpickles artifacts.
Forecasts keep their fitted version; seller summaries contain no identity keys.
Jobs run in a separate process, share one kernel lock, and retain their result
on disk so a browser refresh or a dashboard restart does not lose the outcome.
"""

import importlib.util
import json
import logging
import math
import os
import subprocess
import sys
import threading
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import config, db, fileio, schema
from .predict._common import ARRIVAL_HORIZONS, HORIZONS, LISTING_METHODS, forecast_sources, forecast_validation, guidance_acceptance

FAMILIES = ("sale_probability", "price_trajectory", "category_trends",
            "seller_archetypes", "market_concentration")
TABLES = ("predictive_models", "predictive_feature_snapshots", "sale_predictions", "price_trajectories",
          "category_trends", "category_trend_forecasts", "seller_archetypes", "market_concentration")
REQUIREMENTS = ("numpy", "pandas", "scipy", "lifelines", "sklearn", "statsmodels", "ruptures")
SUPPORTED_METHODS = {"sale_probability": (LISTING_METHODS["sale_probability"],),
                     "price_trajectory": (LISTING_METHODS["price_trajectory"],),
                     "category_trends": ("cat-trend-stl-pelt-v4", "cat-trend-pelt-v4", "cat-trend-early-v4"),
                     "seller_archetypes": ("seller-hdbscan-v2",), "market_concentration": ("mkt-conc-v2",)}
FORECAST_METHODS = ("cat-forecast-nb-v2",)
MAX_FORECAST_LISTINGS = 500
_LOCK = threading.Lock()
_PROCESSES = {}
log = logging.getLogger("toriresearch.model_workspace")


class Busy(ValueError):
    pass


def safe(value):
    if isinstance(value, (float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    return value


def _json(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def missing_dependencies():
    missing = []
    for name in REQUIREMENTS:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ValueError, ImportError, AttributeError):
            missing.append(name)
    return missing


def _integer(value, name, lo, hi):
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not lo <= number <= hi:
        raise ValueError(f"{name} must be {lo}..{hi}")
    return number


def scope(values=None):
    values = values or {}
    if not isinstance(values, dict):
        raise ValueError("scope must be an object")
    if set(values) - {"family", "category", "region", "horizon", "version", "page", "listing_pk", "search"}:
        raise ValueError("unknown research scope option")
    family = values.get("family", "sale_probability")
    if family not in FAMILIES:
        raise ValueError("unknown model family")
    result = {"family": family, "horizon": _integer(values.get("horizon", 30), "horizon", 1, 90),
              "page": _integer(values.get("page", 1), "page", 1, 1000000)}
    if result["horizon"] not in HORIZONS:
        raise ValueError("horizon must be one of " + ", ".join(map(str, HORIZONS)) + " days")
    for key, limit in (("category", 300), ("region", 150), ("version", 150), ("search", 100)):
        value = values.get(key, "")
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError(f"invalid {key}")
        result[key] = value.strip()
    if values.get("listing_pk") is not None:
        result["listing_pk"] = _integer(values["listing_pk"], "listing_pk", 1, 2**63 - 1)
    return result


def _listing_scope(selected, alias="l", include_search=False):
    clauses, params = [], []
    for key, column in (("category", "category_norm"), ("region", "region_norm")):
        if selected.get(key):
            clauses.append(f"{alias}.{column}=?")
            params.append(selected[key])
    if include_search and selected.get("search"):
        value = selected["search"].lower()
        pattern = "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append(f"(LOWER(COALESCE({alias}.title,'')) LIKE ? ESCAPE '\\' OR "
                       f"LOWER({alias}.listing_id) LIKE ? ESCAPE '\\' OR CAST({alias}.listing_pk AS TEXT)=?)")
        params.extend((pattern, pattern, value))
    return clauses, params


def _trend_forecasts(selected, read):
    """Projected arrivals from the newest stored origin, one series per category.

    Only the newest origin is returned: an older projection describes an arrival
    process that has since been observed, and showing it beside the current
    history would imply it is still the current view.
    """
    from .predict._common import ACCEPTANCE_VERSION
    clauses, params = ["COALESCE(t.region_norm,'')=?"], [selected["region"]]
    if selected["category"]:
        clauses.append("t.category_norm=?")
        params.append(selected["category"])
    if selected["family"] == "category_trends" and selected["version"]:
        clauses.append("t.model_version=?")
        params.append(selected["version"])
    else:
        clauses.append("t.model_version=(SELECT f.model_version FROM category_trend_forecasts f "
                       "WHERE f.category_norm=t.category_norm "
                       "AND COALESCE(f.region_norm,'')=COALESCE(t.region_norm,'') "
                       "ORDER BY f.origin_week_start DESC,f.computed_at DESC,f.model_version DESC LIMIT 1)")
    # 60 category series at most, five lead times each.
    rows = read("category_trend_forecasts",
                "SELECT t.category_norm,t.region_norm,t.origin_week_start,t.horizon_weeks,"
                "t.target_week_start,t.expected_arrivals,t.arrivals_lo,t.arrivals_hi,t.interval_level,"
                "t.overdispersion,t.method,t.evidence_weeks,t.model_version,t.computed_at,"
                "t.is_accepted,t.validation_json "
                "FROM category_trend_forecasts t WHERE " + " AND ".join(clauses) +
                " AND t.origin_week_start=(SELECT MAX(f.origin_week_start) FROM category_trend_forecasts f "
                "WHERE f.category_norm=t.category_norm AND COALESCE(f.region_norm,'')=COALESCE(t.region_norm,'') "
                "AND f.model_version=t.model_version)"
                " ORDER BY t.category_norm,t.horizon_weeks LIMIT 300", params)
    for row in rows:
        row["region_norm"] = row["region_norm"] or None
        validation = _json(row.pop("validation_json"))
        evaluation = validation.get("evaluation", {})
        evaluation = evaluation if isinstance(evaluation, dict) else {}
        scores = evaluation.get("by_horizon", {})
        scores = scores if isinstance(scores, dict) else {}
        score = scores.get(str(row["horizon_weeks"]), {})
        row["evaluation"] = score if isinstance(score, dict) else {}
        recorded = validation.get("acceptance", {})
        row["acceptance"] = recorded if isinstance(recorded, dict) else {}
        recorded_horizons = row["acceptance"].get("supported_horizons", [])
        recorded_horizons = recorded_horizons if isinstance(recorded_horizons, list) else []
        try:
            verified = guidance_acceptance(evaluation, horizons=ARRIVAL_HORIZONS)
        except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
            verified = {}
        row["unavailability"] = validation.get("unavailability", {})
        if row["arrivals_lo"] is None or row["arrivals_hi"] is None:
            row["unavailability"].setdefault("predictive_interval", {"reason":
                f"Predictive interval unrecorded; needs count quantiles and finite parameters, "
                f"currently {row['evidence_weeks'] if row['evidence_weeks'] is not None else 'unrecorded'} completed weeks"})
        row["is_accepted"] = bool(row.get("is_accepted") and row["model_version"] in FORECAST_METHODS
                                  and row["acceptance"].get("policy_version") == ACCEPTANCE_VERSION
                                  and row["acceptance"].get("accepted") is True
                                  and row["horizon_weeks"] in recorded_horizons
                                  and row["horizon_weeks"] in verified.get("supported_horizons", []))
    versions = [r["model_version"] for r in read("category_trend_forecasts",
                "SELECT model_version,MAX(computed_at) AS stamp FROM category_trend_forecasts "
                "GROUP BY model_version ORDER BY stamp DESC LIMIT 50")]
    return {"items": rows, "categories": len({r["category_norm"] for r in rows}),
            "versions": versions, "supported_methods": list(FORECAST_METHODS)}


def _model(row):
    return {"family": row["model_family"], "model_version": row["model_version"],
            "feature_version": row["feature_version"], "trained_at": row["trained_at"],
            "train_start_at": row["train_start_at"], "train_end_at": row["train_end_at"],
            "n_observations": row["n_observations"], "n_entities": row["n_entities"],
            "metrics": _json(row["metrics_json"]), "method": _json(row["method_json"]),
            "artifact_path": row["artifact_path"]}


def _arrival_comparison(latest, history):
    """Compare adjacent four-week windows only when every week is stored."""
    try:
        end = date.fromisoformat(str(latest["week_start"]))
        weeks = {str(r["week_start"]):r for r in history
                 if r["category_norm"] == latest["category_norm"]}
        window = [weeks.get((end-timedelta(weeks=i)).isoformat()) for i in range(8)]
        if any(row is None or row.get("arrivals") is None for row in window):
            return {"available":False, "reason":"Eight consecutive stored weeks are required"}
        counts = [int(row["arrivals"]) for row in window]
        if any(n < 0 for n in counts):
            return {"available":False, "reason":"Invalid arrival counts"}
    except (KeyError, TypeError, ValueError):
        return {"available":False, "reason":"Invalid weekly history"}
    recent, previous = sum(counts[:4]), sum(counts[4:])
    return {"available":True, "recent_arrivals":recent, "previous_arrivals":previous,
            "change_pct":round(100*(recent-previous)/previous,1) if previous else None,
            "zero_arrival_weeks":counts.count(0), "weeks":8}


def _active_versions(models, read):
    # Read only the safe JSON pointer and the registry; never load a pickle.
    active = {}
    for family in FAMILIES:
        try:
            name = _json((config.DATA_DIR / "models" / f"{family}_latest.json").read_text()).get("artifact")
        except OSError:
            continue
        if not isinstance(name, str) or os.path.basename(name) != name:
            continue
        match = next((m for m in models if m["family"] == family and m["artifact_path"] == name), None)
        if not match:
            rows = read("predictive_models", "SELECT * FROM predictive_models WHERE model_family=? AND artifact_path=? ORDER BY trained_at DESC LIMIT 1", (family, name))
            if rows:
                match = _model(rows[0])
                models.append(match)
        from .predict._common import ACCEPTANCE_VERSION
        acceptance = match["metrics"].get("acceptance", {}) if match else {}
        accepted = (family not in FAMILIES[:2] or
                    (acceptance.get("accepted") and acceptance.get("policy_version") == ACCEPTANCE_VERSION))
        if match and accepted and match["feature_version"] in SUPPORTED_METHODS[family] and (config.DATA_DIR / "models" / name).is_file():
            active[family] = match["model_version"]
    return active


def report(values=None):
    selected = scope(values)
    with db.read_snapshot():
        return safe(_report(selected))


def _report(selected):
    from .predict import _linkage
    from .predict._common import ACCEPTANCE_VERSION

    tables, errors = {}, []
    required_columns = {table["name"]: {column[0] for column in table["columns"]}
                        for table in schema.TABLES if table["name"] in TABLES}
    for table in TABLES:
        # Metadata reads do not abort a PostgreSQL read transaction when an
        # installation has not yet migrated its optional tables.
        if db.dialect() == "postgres":
            columns = {r["column_name"] for r in db.query("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,))}
        else:
            columns = {r["name"] for r in db.query("SELECT name FROM pragma_table_info(?)", (table,))}
        tables[table] = required_columns[table].issubset(columns)

    def read(table, sql, params=()):
        if not tables[table]:
            return []
        try:
            return [dict(r) for r in db.query(sql, params)]
        except Exception:
            if table not in errors:
                errors.append(table)
            return []

    models = []
    for row in read("predictive_models", "SELECT * FROM predictive_models ORDER BY trained_at DESC, model_version DESC LIMIT 200"):
        models.append(_model(row))
    registry_count = read("predictive_models", "SELECT COUNT(*) AS n FROM predictive_models")
    registry_total = registry_count[0]["n"] if registry_count else 0
    active = _active_versions(models, read)
    candidates = {}
    for family in FAMILIES[:2]:
        try:
            name = _json((config.DATA_DIR / "models" / f"{family}_candidate.json").read_text()).get("artifact")
        except OSError:
            continue
        match = next((m for m in models if m["family"] == family and m["artifact_path"] == name), None)
        if (isinstance(name, str) and os.path.basename(name) == name and match
                and match["feature_version"] in SUPPORTED_METHODS[family]
                and (config.DATA_DIR / "models" / name).is_file()
                and (family != "price_trajectory" or match["metrics"].get("diagnostics", {}).get("diagnostics_ok"))):
            candidates[family] = match["model_version"]
    versions, chosen = {}, {}
    for family, table in (("sale_probability", "sale_predictions"), ("price_trajectory", "price_trajectories"),
                          ("seller_archetypes", "seller_archetypes")):
        versions[family] = [r["model_version"] for r in read(table,
            f"SELECT model_version, MAX(computed_at) AS stamp FROM {table} GROUP BY model_version ORDER BY stamp DESC, model_version DESC LIMIT 50")]
        # Preference order: an accepted current-method fit, then the newest fit
        # trained with the current method, then any registered fit, and only then
        # a version that merely has stored output rows.
        #
        # Falling straight through to the output tables looked harmless but was
        # not: with no accepted model the newest row in `sale_predictions` was a
        # `cox-ph-v1` artifact from a superseded method that has no
        # `predictive_models` row at all. The model page then resolved to a fit
        # it could not find and reported "No fit is registered for this model"
        # immediately after a successful training run. A freshly trained
        # candidate is the thing the operator needs to see.
        registered = [m["model_version"] for m in models if m["family"] == family]
        current = [m["model_version"] for m in models
                   if m["family"] == family and m["feature_version"] in SUPPORTED_METHODS.get(family, ())]
        chosen[family] = active.get(family) or next(iter(current), None) or next(iter(registered), None) or next(iter(versions[family]), None)
    if selected["version"]:
        chosen[selected["family"]] = selected["version"]
    for family, version in chosen.items():
        if version and not any(m["model_version"] == version and m["family"] == family for m in models):
            rows = read("predictive_models", "SELECT * FROM predictive_models WHERE model_family=? AND model_version=?", (family, version))
            if rows:
                models.append(_model(rows[0]))
    for row in models:
        row["active"] = active.get(row["family"]) == row["model_version"]

    listing_where, listing_params = _listing_scope(selected)
    summaries = {}
    for family, table, horizon, mean_column in (
        ("sale_probability", "sale_predictions", "horizon_days", "p_sale"),
        ("price_trajectory", "price_trajectories", "day", "predicted_price_eur")):
        clauses = ["p.model_version=?"] + listing_where
        params = [chosen[family] or ""] + listing_params
        where = " AND ".join(clauses)
        join = f" FROM {table} p JOIN listings l ON l.listing_pk=p.listing_pk WHERE {where}"
        grouped = read(table, f"SELECT p.{horizon} AS horizon, COUNT(*) AS rows, COUNT(DISTINCT p.listing_pk) AS listings, "
                       f"AVG(p.{mean_column}) AS mean, MAX(p.computed_at) AS computed_at, "
                       "SUM(CASE WHEN p.is_extrapolated THEN 1 ELSE 0 END) AS extrapolated" +
                       (", SUM(CASE WHEN p.diagnostics_ok THEN 1 ELSE 0 END) AS diagnostics_ok" if family == "price_trajectory" else "") +
                       join + f" GROUP BY p.{horizon} ORDER BY p.{horizon}", params) if tables[table] else []
        # Totals must use exactly the same selected fit and scope as the rows.
        total_rows = read(table, "SELECT COUNT(*) AS rows, COUNT(DISTINCT p.listing_pk) AS listings, MAX(p.computed_at) AS computed_at" +
                          (", SUM(CASE WHEN p.diagnostics_ok THEN 1 ELSE 0 END) AS diagnostics_ok" if family == "price_trajectory" else "") + join, params)
        totals = total_rows[0] if total_rows else {}
        summaries[family] = {"rows": totals.get("rows") or 0, "listings": totals.get("listings") or 0,
                             "computed_at": totals.get("computed_at")}
        if family == "sale_probability":
            summaries[family]["by_horizon"] = [dict(horizon_days=r["horizon"], rows=r["rows"], listings=r["listings"],
                mean_p_sale=r["mean"], mean_expected_price_eur=None, extrapolated=r["extrapolated"], computed_at=r["computed_at"]) for r in grouped]
        else:
            summaries[family].update(diagnostics_ok=totals.get("diagnostics_ok") or 0,
                by_day=[dict(day=r["horizon"], rows=r["rows"], listings=r["listings"], mean_price_eur=r["mean"],
                    extrapolated=r["extrapolated"], diagnostics_ok=r["diagnostics_ok"], computed_at=r["computed_at"]) for r in grouped])

    aggregates = {}
    for family, period, pk in (("category_trends", "week_start", "trend_pk"), ("market_concentration", "month_start", "concentration_pk")):
        clauses, params = [], []
        if selected["category"]:
            clauses.append("t.category_norm=?")
            params.append(selected["category"])
        clauses.append("COALESCE(t.region_norm,'')=?")
        params.append(selected["region"])
        if selected["family"] == family and selected["version"]:
            clauses.append("t.model_version=?")
            params.append(selected["version"])
        else:
            clauses.append(f"NOT EXISTS (SELECT 1 FROM {family} newer WHERE newer.category_norm=t.category_norm "
                f"AND COALESCE(newer.region_norm,'')=COALESCE(t.region_norm,'') AND newer.{period}=t.{period} "
                f"AND (newer.computed_at>t.computed_at OR (newer.computed_at=t.computed_at AND newer.{pk}>t.{pk})))")
        # 104 weeks / 24 months at most, and at most 60 category series.
        histories = read(family, f"SELECT t.* FROM {family} t WHERE " + " AND ".join(clauses) + f" ORDER BY t.{period} DESC, t.category_norm LIMIT 6240", params)
        for r in histories:
            r.pop(pk, None)
            r.pop("price_leader_seller_version_id", None)
            r["region_norm"] = r["region_norm"] or None
            if family == "category_trends":
                from .predict._common import trend_unavailability
                evidence = _json(r.pop("evidence_json", None))
                r["entry_price_count"] = evidence.get("entry_price_count")
                r["unavailability"] = trend_unavailability(r)
                r["direction_evaluation"] = evidence.get("direction_evaluation", {})
                r["direction_evaluation"].pop("windows", None)
        latest = {}
        for row in histories:
            latest.setdefault(row["category_norm"], row)
        items = sorted(latest.values(), key=lambda r: (-(r.get("arrivals") or r.get("n_observed_listings") or 0), r["category_norm"]))
        aggregates[family] = {"rows": len(histories), "categories": len(items), "items": items,
                              "history": list(reversed(histories)), "history_limited": len(histories) == 6240}
        aggregates[family]["latest_week" if family == "category_trends" else "latest_month"] = histories[0][period] if histories else None
        if family == "category_trends":
            aggregates[family]["states"] = dict(Counter(r["trend_state"] for r in items))
            for row in items:
                row["arrival_comparison"] = _arrival_comparison(row, histories)
        versions[family] = [r["model_version"] for r in read(family, f"SELECT model_version, MAX(computed_at) AS stamp FROM {family} GROUP BY model_version ORDER BY stamp DESC LIMIT 50")]

    aggregates["category_trends"]["forecasts"] = _trend_forecasts(selected, read)

    # Collapse SCD2 seller versions to a single stable seller for the selected fit.
    assignments = read("seller_archetypes", "SELECT archetype_id, confidence, cluster_id FROM "
        "(SELECT a.archetype_id, a.confidence, a.cluster_id, ROW_NUMBER() OVER "
        "(PARTITION BY s.platform,s.seller_id ORDER BY a.computed_at DESC,a.archetype_pk DESC) AS seller_rank "
        "FROM seller_archetypes a JOIN sellers s ON s.seller_version_id=a.seller_version_id "
        "WHERE a.model_version=?) ranked WHERE seller_rank=1", (chosen["seller_archetypes"] or "",))
    groups = {}
    for r in assignments:
        groups.setdefault(r["archetype_id"], []).append(r)
    archetypes = [{"archetype_id": k, "sellers": len(rows),
                  "mean_confidence": sum(float(r["confidence"] or 0) for r in rows) / len(rows)} for k, rows in sorted(groups.items())]
    listing_forecasts = _forecast_page(selected, chosen, read)
    options = {key: [r["value"] for r in db.query(f"SELECT DISTINCT {column} AS value FROM listings WHERE {column} IS NOT NULL AND {column}!='' ORDER BY {column} LIMIT 400")]
               for key, column in (("categories", "category_norm"), ("regions", "region_norm"))}
    counts = dict(db.query_one("SELECT COUNT(*) AS listings, "
        "SUM(CASE WHEN outcome_status IN ('sold_inferred','sold_confirmed') THEN 1 ELSE 0 END) AS sale_labels, "
        "SUM(CASE WHEN status_norm='active' AND coverage='collecting' AND outcome_status='unknown' "
        "AND listed_price_eur>0 AND COALESCE(is_reserved,FALSE)=FALSE AND COALESCE(is_giveaway,FALSE)=FALSE THEN 1 ELSE 0 END) AS active_priced FROM listings"))
    recorded = db.query_one("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'")
    schema_version = int(recorded["meta_value"]) if recorded else None
    return dict(generated_at=datetime.now(timezone.utc).isoformat(), tables=tables, errors=errors,
                schema_version=schema_version, schema_current=schema_version == schema.SCHEMA_VERSION,
                models=models, registry_total=registry_total, registry_limited=registry_total>200,
                active_models=active, candidate_models=candidates, horizons=list(HORIZONS), selected_versions=chosen, versions=versions, supported_methods=SUPPORTED_METHODS,
                scope=selected, options=options, source_counts=counts, acceptance_policy_version=ACCEPTANCE_VERSION,
                missing_dependencies=missing_dependencies(), install_command="python3 -m pip install -r requirements-predict.txt",
                train_command="python3 -m toriresearch.cli predict --model <name> --train --persist",
                linkage=_linkage.status(), listing_forecasts=listing_forecasts,
                seller_archetypes={"rows": len(assignments), "sellers": len(assignments), "archetypes": archetypes,
                                  "noise": sum(r["cluster_id"] == -1 for r in assignments),
                                  "clusters": len({r["cluster_id"] for r in assignments if r["cluster_id"] is not None and r["cluster_id"] >= 0})},
                jobs=jobs(), job_limit=MAX_FORECAST_LISTINGS, max_age_hours=config.PANEL_MAX_AGE_HOURS, **summaries, **aggregates)


def _forecast_page(selected, chosen, read):
    family = selected["family"]
    if family not in ("sale_probability", "price_trajectory"):
        return {"items": [], "total": 0, "page": 1, "pages": 1}
    table, horizon = ("sale_predictions", "horizon_days") if family == "sale_probability" else ("price_trajectories", "day")
    clauses, params = _listing_scope(selected, include_search=True)
    clauses += ["p.model_version=?", f"p.{horizon}=?"]
    params += [chosen[family] or "", selected["horizon"]]
    join = f" FROM {table} p JOIN listings l ON l.listing_pk=p.listing_pk WHERE " + " AND ".join(clauses)
    totals = read(table, "SELECT COUNT(*) AS n" + join, params)
    total = totals[0]["n"] if totals else 0
    pages = max(1, math.ceil(total / 25))
    page = min(pages, selected["page"])
    columns = "p.is_accepted, p.features_json, " + ("p.p_sale, p.p_sale_ci_lo, p.p_sale_ci_hi" if family == "sale_probability" else "p.predicted_price_eur, p.price_ci_lo, p.price_ci_hi, p.observed_duration_days, p.diagnostics_ok, p.validation_available, p.validation_improves_baseline")
    items = read(table, f"SELECT p.listing_pk, p.model_version, p.{horizon} AS horizon_days, p.computed_at, p.is_extrapolated, "
                 f"{columns}, l.title, l.platform, l.listing_id, l.category_norm, l.municipality_norm, l.listed_price_eur, "
                 "l.status_norm, l.coverage, l.outcome_status, l.last_seen_at" + join +
                 " ORDER BY p.computed_at DESC, p.listing_pk DESC LIMIT 25 OFFSET ?", params + [(page - 1) * 25])
    detail = None
    if selected.get("listing_pk"):
        pk = selected["listing_pk"]
        # Detail uses the same fit and segment, but includes all stored horizons.
        detail_clauses, detail_params = _listing_scope(selected)
        detail_clauses += ["p.model_version=?", "p.listing_pk=?"]
        detail_params += [chosen[family] or "", pk]
        forecasts = read(table, f"SELECT p.listing_pk, p.model_version, p.{horizon} AS horizon_days, p.computed_at, p.is_extrapolated, {columns} "
            f"FROM {table} p JOIN listings l ON l.listing_pk=p.listing_pk WHERE " + " AND ".join(detail_clauses) + f" ORDER BY p.{horizon}", detail_params)
        if forecasts:
            listing = dict(db.query_one("SELECT listing_pk,title,listed_price_eur,last_seen_at,status_norm FROM listings WHERE listing_pk=?", (pk,)))
            history = [dict(r) for r in db.query("SELECT valid_from,listed_price_eur FROM price_history WHERE listing_pk=? ORDER BY valid_from DESC LIMIT 100", (pk,))]
            detail = {"listing": listing, "forecasts": forecasts, "price_history": list(reversed(history))}
    forecasts = items + (detail["forecasts"] if detail else [])
    models, entries = forecast_sources(forecasts, read)
    for row in forecasts:
        row.update(forecast_validation(family, row, models.get(row["model_version"]), entries.get(row["listing_pk"])))
        row["forecast_inputs"] = _json(row.pop("features_json", None))
    return {"items": items, "total": total, "page": page, "pages": pages, "detail": detail}


def jobs():
    rows = []
    directory = config.DATA_DIR / "model_jobs"
    for path in sorted(directory.glob("*.json"), reverse=True)[:12]:
        try:
            row = _json(path.read_text())
        except OSError:
            continue
        if row.get("status") in ("running", "cancelling") and row.get("job_id") not in _PROCESSES:
            # Another dashboard process may own the shared job lock. A crashed
            # owner releases it automatically; do not report endless progress.
            if "predict-workspace" not in db.active_locks():
                row = dict(row, status="interrupted", error="Dashboard stopped before this job reported its outcome.")
        if row:
            # Older job monitors treated scientific-library warnings as crashes
            # even when stdout records a completed, stored candidate. Reconcile
            # that evidence on read without rewriting the activity history.
            if (row.get("status") == "failed" and row.get("returncode") == 1
                    and _outcome({}, 1, row.get("result"), row.get("error")) == "experimental"):
                row = dict(row, status="experimental", warnings=row.get("error"), error=None)
            row["can_cancel"] = row.get("job_id") in _PROCESSES
            rows.append(row)
    return safe(rows)


def _save_job(path, job):
    fileio.write_text(path, json.dumps(safe(job), allow_nan=False))


def _outcome(previous, code, result, stderr):
    """Classify a finished child process.

    `toriresearch.cli predict` exits non-zero for two very different situations
    and the operator has to be able to tell them apart. A genuine failure -- a
    missing dependency, a corpus that cannot support a fit, a crash -- writes to
    stderr and produces no result. A fit that ran to completion but did not clear
    guidance acceptance writes its metrics as JSON on stdout. Scientific-library
    warnings on stderr do not turn a recorded candidate into a failed fit.

    Collapsing both onto "failed" was the original bug: every successful
    training run on a young corpus was reported in red as a crash, which hid
    the fit and taught the operator to ignore the job panel.
    """
    if previous.get("status") == "interrupted":
        return "interrupted"
    if previous.get("status") == "cancelling":
        return "cancelled"
    if code == 0:
        return "done"
    acceptance = result.get("acceptance", {}) if isinstance(result, dict) else {}
    stored_candidate = (code == 1 and isinstance(result, dict) and result.get("model_version")
                        and isinstance(acceptance, dict) and acceptance.get("accepted") is False
                        and acceptance.get("status") == "experimental")
    if stored_candidate or (result and not (stderr or "").strip()):
        return "experimental"
    return "failed"


def start(payload, on_finish=None):
    if not isinstance(payload, dict) or set(payload) - {"action", "family", "scope", "window_days"}:
        raise ValueError("invalid model job")
    action, family = payload.get("action"), payload.get("family")
    allowed = ("train", "predict", "preview", "evaluate") if family in FAMILIES[:2] else ("compute",)
    if family not in FAMILIES or action not in allowed:
        raise ValueError("unsupported model action")
    if payload.get("scope") is not None and not isinstance(payload["scope"], dict):
        raise ValueError("scope must be an object")
    selected = scope(dict(payload.get("scope") or {}, family=family))
    if selected["version"]:
        raise ValueError("Jobs use the active model. Clear the historical version selection first.")
    recorded = db.query_one("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'")
    if not recorded or recorded["meta_value"] != str(schema.SCHEMA_VERSION):
        raise ValueError("Model database needs migration. Run python3 -m toriresearch.cli init before starting a job.")
    missing = missing_dependencies()
    if missing:
        raise ValueError("Install optional modelling packages before running a job: " + ", ".join(missing))
    argv = [sys.executable, "-m", "toriresearch.cli", "predict", "--model", family]
    argv += ["--predict" if action in ("predict", "preview", "compute") else "--" + action]
    if action == "preview":
        argv += ["--experimental"]
    if action in ("predict", "preview", "compute"):
        argv += ["--persist"]
    target_count = None
    if family in FAMILIES[:2]:
        if action in ("predict", "preview", "evaluate"):
            argv += ["--horizon", str(selected["horizon"])]
        if action in ("predict", "preview"):
            clauses, params = _listing_scope(selected)
            now = datetime.now(timezone.utc)
            clauses += ["l.status_norm='active'", "l.outcome_status='unknown'", "l.coverage='collecting'", "l.listed_price_eur>0",
                        "COALESCE(l.is_reserved,FALSE)=FALSE", "COALESCE(l.is_giveaway,FALSE)=FALSE", "l.last_seen_at>=?", "l.last_seen_at<=?",
                        "COALESCE(l.price_observed_at,l.last_seen_at)>=?", "l.first_seen_at<=?"]
            cutoff = (now - timedelta(hours=config.PANEL_MAX_AGE_HOURS)).isoformat()
            params += [cutoff, now.isoformat(), cutoff, now.isoformat()]
            targets = db.query("SELECT l.listing_pk FROM listings l WHERE " + " AND ".join(clauses) + " ORDER BY l.last_seen_at DESC, l.listing_pk DESC LIMIT ?", params + [MAX_FORECAST_LISTINGS])
            if not targets:
                raise ValueError("No fresh, active, priced listings in this scope. Review collection and filters first.")
            target_count = len(targets)
            argv += ["--listing-pks", ",".join(str(r["listing_pk"]) for r in targets)]
    elif family in ("category_trends", "market_concentration") and selected["region"]:
        argv += ["--region", selected["region"]]
    elif family == "seller_archetypes":
        argv += ["--window-days", str(_integer(payload.get("window_days", 180), "window_days", 30, 365))]
    with _LOCK:
        handle, error = db.acquire_lock("predict-workspace")
        if handle is None:
            raise Busy("A model job is already running. Wait for it to finish. " + (error or ""))
        stamp = datetime.now(timezone.utc)
        job_id = stamp.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        path = config.DATA_DIR / "model_jobs" / (job_id + ".json")
        job = {"job_id": job_id, "family": family, "action": action, "status": "running", "started_at": stamp.isoformat(),
               "finished_at": None, "scope": selected, "target_count": target_count, "result": None, "error": None}
        env = dict(os.environ, TORI_DATA_DIR=str(config.DATA_DIR), TORI_REPORTS_DIR=str(config.REPORTS_DIR), TORI_DB=str(config.SQLITE_PATH))
        if config.POSTGRES_DSN:
            env["TORI_PG_DSN"] = config.POSTGRES_DSN
        try:
            _save_job(path, job)
            # The child shares the kernel lock. If its dashboard dies, another
            # dashboard cannot start a competing fit while the orphan finishes.
            proc = subprocess.Popen(argv, cwd=str(config.ROOT), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, pass_fds=(handle.fileno(),))
            _PROCESSES[job_id] = proc
        except Exception:
            db.release_lock(handle)
            try:
                _save_job(path, dict(job, status="failed", error="Could not start the model process.", finished_at=datetime.now(timezone.utc).isoformat()))
            except OSError:
                log.exception("Could not write the failed model job record")
            raise ValueError("Could not start the model process.") from None

    def finish():
        try:
            try:
                stdout, stderr = proc.communicate(timeout=1800)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                code, stderr = -1, "Model job timed out after 30 minutes."
            previous = _json(path.read_text())
            try:
                result = json.loads(stdout)
            except (ValueError, TypeError):
                result = None
            status = _outcome(previous, code, result, stderr)
            if action == "preview" and status == "done":
                status = "experimental"
            job.update(status=status, returncode=code, finished_at=datetime.now(timezone.utc).isoformat(),
                       result=safe(result), output=(stdout or "")[-12000:],
                       warnings=(stderr or "")[-2000:] if status in ("done", "experimental") else None,
                       error=previous.get("error") if status == "interrupted"
                       else (stderr or "")[-2000:] if code and status == "failed" else None)
            _save_job(path, job)
        except Exception:
            try:
                _save_job(path, dict(job, status="failed", error="Could not record the model result.", finished_at=datetime.now(timezone.utc).isoformat()))
            except OSError:
                log.exception("Could not write the completed model job record")
        finally:
            with _LOCK:
                _PROCESSES.pop(job_id, None)
                db.release_lock(handle)
            if on_finish:
                on_finish()
            # Keep bounded local history; no private seller assignments are emitted by the CLI.
            for old in sorted(path.parent.glob("*.json"), reverse=True)[100:]:
                old.unlink(missing_ok=True)

    threading.Thread(target=finish, daemon=True, name="tori-model-job").start()
    # The worker owns its mutable job object; the start response always describes
    # the accepted request even if a tiny job finishes before it is serialised.
    return {"ok": True, "job": dict(job)}


def cancel(payload):
    job_id = payload.get("job_id")
    with _LOCK:
        proc = _PROCESSES.get(job_id) if isinstance(job_id, str) else None
        if proc is None:
            raise ValueError("This dashboard has no running job with that ID.")
        path = config.DATA_DIR / "model_jobs" / (job_id + ".json")
        job = _json(path.read_text())
        _save_job(path, dict(job, status="cancelling"))
        proc.terminate()
    return {"ok": True, "job_id": job_id}


def shutdown():
    """Stop children on a graceful dashboard shutdown, preserving the outcome."""
    with _LOCK:
        processes = list(_PROCESSES.items())
        for job_id, proc in processes:
            path = config.DATA_DIR / "model_jobs" / (job_id + ".json")
            try:
                row = _json(path.read_text())
                _save_job(path, dict(row, status="interrupted", error="Dashboard stopped during this model job.",
                                     finished_at=datetime.now(timezone.utc).isoformat()))
            except OSError:
                log.exception("Could not record interrupted model job")
            proc.terminate()
    for _, proc in processes:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
