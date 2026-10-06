"""Bounded, read-only evidence cards for retained listing forecasts.

No artifact is loaded and no model runs here. A chronological score is research
evidence; it does not establish the separate forward-validation gate for buying.
"""

import hashlib
import json
from datetime import timedelta

from . import db, metrics, model_workspace, schema
from .predict._common import ACCEPTANCE_VERSION, HORIZONS, LISTING_METHODS, forecast_sources, forecast_validation

VERSION = "forecast-evidence-v1"
MAX_LISTINGS = 8
SCORE_FIELDS = {"available", "reason", "n_test", "n_horizon_observed", "n_train", "horizon_supported", "improves_baseline",
                "calibration_ok", "calibration_error", "coverage_90", "brier_score", "baseline_brier_score",
                "mae_price_ratio", "baseline_mae_price_ratio", "train_end_at", "test_start_at", "max_duration_days",
                "evaluated_duration_days_min", "evaluated_duration_days_max"}
TARGETS = {
    "sale_probability": "Seller sale-label probability conditional on recorded listing age; labels include inferred outcomes",
    "price_trajectory": "Future asking price; transaction proceeds and resale profit are unknown",
}


def _mapping(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(model_workspace.safe(value), sort_keys=True, allow_nan=False).encode()).hexdigest()


def view(listing_pks):
    if not isinstance(listing_pks, (list, tuple)) or not 1 <= len(listing_pks) <= MAX_LISTINGS:
        raise ValueError(f"provide 1..{MAX_LISTINGS} listing identifiers")
    from .item_workspace import _key
    keys = list(dict.fromkeys(_key(value, "listing_pk") for value in listing_pks))
    with db.read_snapshot():
        return for_listings(keys)


def for_listings(keys):
    """Read in the caller's snapshot; callers supply bounded, retained references."""
    if not keys:
        return dict(version=VERSION, items=[], note="No retained marketplace source; owned outcomes cannot supply a forecast.")
    if len(keys) > MAX_LISTINGS:
        raise ValueError("forecast source limit exceeded")
    tables = ("sale_predictions", "price_trajectories", "predictive_models", "predictive_feature_snapshots")
    present = {}
    for table in tables:
        columns = {row["column_name"] for row in db.query(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,))} if db.dialect() == "postgres" else {
                row["name"] for row in db.query("SELECT name FROM pragma_table_info(?)", (table,))}
        required = next({column[0] for column in spec["columns"]} for spec in schema.TABLES if spec["name"] == table)
        present[table] = required.issubset(columns)

    def read(table, sql, params=()):
        return db.query(sql, params) if present[table] else []

    marks = ",".join("?" for _ in keys)
    listings = db.query("SELECT listing_pk,listing_id,title,platform,subplatform,category_root_norm,condition_norm,"
                        "first_seen_at,last_seen_at,status_norm,outcome_status,listed_price_eur FROM listings "
                        f"WHERE listing_pk IN ({marks})", keys)
    found = {row["listing_pk"]:row for row in listings}
    outputs = []
    for family, table, horizon, pk in (("sale_probability", "sale_predictions", "horizon_days", "prediction_pk"),
                                      ("price_trajectory", "price_trajectories", "day", "trajectory_pk")):
        # Each horizon retains its actual version and computation time. Never
        # substitute another listing's output or average product-group values.
        rows = read(table, "SELECT * FROM (SELECT p.*, ROW_NUMBER() OVER "
                    f"(PARTITION BY listing_pk,{horizon} ORDER BY computed_at DESC,model_version DESC,{pk} DESC) "
                    f"AS retained_rank FROM {table} p WHERE listing_pk IN ({marks}) "
                    f"AND {horizon} IN ({','.join(str(value) for value in HORIZONS)})) ranked "
                    "WHERE retained_rank=1 ORDER BY listing_pk,"+horizon, keys)
        for row in rows:
            if row[horizon] in HORIZONS:
                outputs.append(dict(row, family=family, horizon_days=row[horizon]))
    models, entries = forecast_sources(outputs, read)
    items = []
    for key in keys:
        listing = found.get(key)
        if not listing:
            items.append(dict(listing_pk=key, listing=None, cards=[], reason="Marketplace source is no longer retained locally"))
            continue
        cards = [_card(row, models.get(row["model_version"]), entries.get(key), listing) for row in outputs if row["listing_pk"] == key]
        items.append(dict(listing_pk=key, listing=model_workspace.safe(listing), cards=cards,
                          missing_families=[dict(family=family, target=TARGETS[family],
                              reason="No retained forecast at a declared horizon" if present[table] else "Forecast schema is unavailable")
                              for family, table in (("sale_probability", "sale_predictions"), ("price_trajectory", "price_trajectories"))
                              if not any(card["family"] == family for card in cards)]))
    result = dict(version=VERSION, items=items, selection="Newest retained output per listing, family and declared horizon",
                  note="Listing forecasts are research context. They do not measure owned selling speed, actual proceeds or profit. Opening evidence never trains, approves or records a purchase.")
    result["revision"] = _fingerprint(result)
    return result


def _card(row, model, entry, listing):
    family, horizon = row["family"], row["horizon_days"]
    validation = forecast_validation(family, row, model, entry)
    model = model or {}
    stored = _mapping(model.get("metrics_json"))
    evaluation = _mapping(stored.get("evaluation"))
    acceptance = _mapping(stored.get("acceptance"))
    inputs = _mapping(row.get("features_json"))
    score = _mapping(_mapping(evaluation.get("by_horizon")).get(str(horizon)))
    segments = score.get("by_segment")
    segment = next((item for item in segments if isinstance(item, dict)
                    and item.get("category") == inputs.get("cat_root")
                    and item.get("condition") == inputs.get("condition")), {}) if isinstance(segments, list) else {}
    # Age holdouts are checkpoints, not interpolation of accuracy. Show both
    # surrounding checkpoints used by the existing consecutive-support policy.
    age_scores = _mapping(segment.get("by_age"))
    computed = metrics.parse_ts(row.get("computed_at"))
    target_end = computed+timedelta(days=horizon) if computed else None
    reasons = [validation["validation_reason"]] if validation["validation_reason"] else []
    if acceptance and acceptance.get("policy_version") != ACCEPTANCE_VERSION:
        reasons.append("Recorded approval uses a superseded or unrecorded policy")
    current_method = model.get("feature_version") == LISTING_METHODS[family] and model.get("model_family") == family
    if not current_method:
        reasons.append("Original method or time-origin definition is unverified")
    reason = "Forward validation and its current approval fingerprint are not established by retained chronological model scores"
    card = dict(family=family, target=TARGETS[family], listing_pk=row["listing_pk"],
                model_version=row["model_version"], method_version=model.get("feature_version"), current_method=current_method,
                computed_at=row["computed_at"], horizon_days=horizon, target_end_at=target_end if current_method else None,
                horizon_basis="From computation time" if current_method else "Original time-origin unverified",
                population=dict(category=inputs.get("cat_root"), condition=inputs.get("condition"),
                                age_days=inputs.get("age_days") if family == "sale_probability" else None,
                                max_duration_days=segment.get("max_duration_days"),
                                basis="Frozen listing inputs; category/condition support does not establish exact-product accuracy",
                                current_identity_differs=inputs.get("cat_root") != (listing.get("category_root_norm") or "unknown")
                                    or inputs.get("condition") != (listing.get("condition_norm") or "unknown")),
                training={key:model.get(key) for key in ("trained_at", "train_start_at", "train_end_at", "n_observations", "n_entities")},
                evaluation=dict(horizon={key:value for key, value in score.items() if key in SCORE_FIELDS},
                                segment={key:value for key, value in segment.items() if key in SCORE_FIELDS},
                                age_checkpoints={key:{field:value for field, value in checkpoint.items() if field in SCORE_FIELDS}
                                                 for key, checkpoint in age_scores.items() if isinstance(checkpoint, dict)},
                                fingerprint=_fingerprint(dict(model_version=row["model_version"], method=model.get("feature_version"),
                                                             evaluation=evaluation, acceptance=acceptance))),
                validation=dict(validation, policy_version=ACCEPTANCE_VERSION,
                                recorded_policy_version=acceptance.get("policy_version"), withholding_reasons=reasons),
                guidance=dict(status="withheld", reason=reason),
                output={key:row.get(key) for key in (("p_sale", "p_sale_ci_lo", "p_sale_ci_hi") if family == "sale_probability"
                        else ("predicted_price_eur", "price_ci_lo", "price_ci_hi"))},
                inspect=dict(family=family, version=row["model_version"], horizon=horizon, listing_pk=row["listing_pk"]))
    return model_workspace.safe(card)
