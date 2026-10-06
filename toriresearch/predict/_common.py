"""Runtime artifact paths, provenance and input validation for optional models."""

import hashlib
import json
import math
import os
import pickle
import tempfile
import uuid

from .. import config, db

HORIZONS = (1, 2, 3, 7, 14, 30, 60, 90)
ARRIVAL_HORIZONS = (1, 2, 4, 8, 13)
VALIDATION_AGES = (0, 7, 14, 30, 60)
TREND_SEASONAL_WEEKS = 104
TREND_BREAK_WEEKS = 8
ACCEPTANCE_VERSION = "guidance-v3"
ENTRY_FEATURE_VERSION = "entry-v1"
ENTRY_FIELDS = ("category_root_norm", "condition_norm", "municipality_norm", "subplatform")
LISTING_METHODS = {"sale_probability":"cox-causes-v6", "price_trajectory":"price-hier-bayes-v7"}


def capture_entry_features(conn, listing_pk, row):
    conn.execute("INSERT INTO predictive_feature_snapshots "
                 "(listing_pk,observed_at,feature_version,features_json) VALUES (?,?,?,?) "
                 "ON CONFLICT (listing_pk) DO NOTHING",
                 (listing_pk, row["source_observed_at"], ENTRY_FEATURE_VERSION,
                  json.dumps({c:row.get(c) for c in ENTRY_FIELDS})))


def entry_features(asof):
    from ..metrics import parse_ts
    snapshots = db.query("SELECT f.*,l.first_seen_at FROM predictive_feature_snapshots f "
                         "JOIN listings l ON l.listing_pk=f.listing_pk WHERE f.feature_version=?",
                         (ENTRY_FEATURE_VERSION,))
    out = {}
    for row in snapshots:
        stamp, first = parse_ts(row["observed_at"]), parse_ts(row["first_seen_at"])
        if stamp and first and stamp <= asof and abs((stamp-first).total_seconds()) <= 1:
            out[row["listing_pk"]] = json.loads(row["features_json"])
    return out


def entry_prices(asof):
    from ..metrics import parse_ts
    prices = {}
    rows = db.query("SELECT listing_pk,listed_price_eur,valid_from,observed_at "
                    "FROM price_history ORDER BY listing_pk,valid_from")
    for row in rows:
        stamp, observed = parse_ts(row["valid_from"]), parse_ts(row["observed_at"])
        price = finite_price(row["listed_price_eur"])
        if stamp and observed and stamp <= asof and observed <= asof and price is not None:
            prices.setdefault(row["listing_pk"], (price,stamp))
    return prices


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def horizons_checked(horizons):
    values = list(HORIZONS if horizons is None else horizons)
    if not values or any(isinstance(h, bool) or not isinstance(h, int) or not 1 <= h <= 90
                         for h in values):
        raise ValueError("horizons must contain integers between 1 and 90 days")
    return sorted(set(values))


def finite_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def trend_unavailability(row):
    """Explain absent trend fields using stored evidence, including legacy rows.

    This stays dependency-free so a read-only dashboard can disclose thresholds
    without importing the scientific models or recomputing their results.
    """
    missing, weeks = {}, row.get("evidence_weeks")
    for field, label, required in (("seasonality_idx", "Seasonality", TREND_SEASONAL_WEEKS),
                                   ("changepoint_prob", "Break detection", TREND_BREAK_WEEKS),
                                   ("trend_slope", "Trend slope", 3),
                                   ("state_confidence", "Direction confidence", 3)):
        if row.get(field) is not None:
            continue
        if field == "state_confidence" and row.get("trend_state") == "volatile":
            missing[field] = {"reason":"Reported state is volatile; volatility makes no directional claim",
                              "required_state":"directional", "observed_state":"volatile"}
            continue
        reached = str(weeks) if weeks is not None else "unrecorded"
        reason = f"{label} needs {required} completed weeks, currently {reached}"
        if weeks is None or weeks >= required:
            reason += "; estimate unrecorded, recompute with the current method"
        missing[field] = {"required":required, "observed":weeks, "unit":"completed weeks", "reason":reason}
    if row.get("median_ask_eur") is None:
        n = row.get("entry_price_count")
        reached = str(n) if n is not None else "unrecorded"
        missing["median_ask_eur"] = {"required":1, "observed":n, "unit":"entry prices",
            "reason":f"Median entry ask needs 1 observed entry price in this week, currently {reached}"}
    return missing


def model_dir():
    return config.DATA_DIR / "models"


def version_id(method, data, now):
    encoded = json.dumps(data, sort_keys=True, default=str, allow_nan=False).encode()
    digest = hashlib.sha256(encoded + now.isoformat().encode() + uuid.uuid4().bytes).hexdigest()[:16]
    return f"{method}-{digest}"


def _atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = handle.name
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_bundle(family, bundle, metadata, activate=True):
    path = model_dir() / f"{family}_{bundle['model_version']}.pkl"
    _atomic_bytes(path, pickle.dumps(bundle, protocol=pickle.HIGHEST_PROTOCOL))
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO predictive_models (model_version, model_family, feature_version, "
            "trained_at, train_start_at, train_end_at, n_observations, n_entities, "
            "metrics_json, method_json, artifact_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (bundle["model_version"], family, bundle["method_version"], bundle["trained_at"],
             metadata.get("train_start_at"), metadata.get("train_end_at"),
             metadata["n_observations"], metadata["n_entities"],
             json.dumps(metadata.get("metrics", {}), allow_nan=False),
             json.dumps(metadata.get("method", {}), allow_nan=False), path.name))
    finally:
        conn.close()
    _atomic_bytes(model_dir() / f"{family}_candidate.json",
                  json.dumps({"artifact": path.name}).encode())
    if activate:
        _atomic_bytes(model_dir() / f"{family}_latest.json",
                      json.dumps({"artifact": path.name}).encode())
    return path


def load_bundle(family, method_version, experimental=False):
    pointer = model_dir() / f"{family}_{'candidate' if experimental else 'latest'}.json"
    if not pointer.exists():
        raise ValueError(f"{family} model not trained with the current method. Run --train first.")
    try:
        name = json.loads(pointer.read_text())["artifact"]
        if os.path.basename(name) != name:
            raise ValueError("invalid artifact name")
        with (model_dir() / name).open("rb") as handle:
            bundle = pickle.load(handle)
    except (OSError, ValueError, KeyError, pickle.UnpicklingError, EOFError) as exc:
        raise ValueError(f"Cannot load {family} artifact; run --train again: {exc}") from exc
    if bundle.get("method_version") != method_version:
        raise ValueError(f"{family} artifact uses an older method. Run --train again.")
    return bundle


def guidance_acceptance(evaluation, diagnostics_ok=True, horizons=HORIZONS):
    """Acceptance is horizon-specific and never follows from fit alone."""
    supported, segments = [], {}
    def passes(score):
        return bool(diagnostics_ok and score.get("available") and score.get("improves_baseline")
                    and score.get("calibration_ok") and score.get("n_test", 0) >= 10
                    and score.get("n_horizon_observed", score.get("n_test", 0)) >= 10
                    and score.get("horizon_supported"))
    for horizon, score in evaluation.get("by_horizon", {}).items():
        valid_segments = [s for s in score.get("by_segment", []) if passes(s)
                          and s.get("category") not in (None, "unknown")
                          and s.get("condition") not in (None, "unknown")]
        if evaluation.get("requires_age_support"):
            age_supported = []
            for segment in valid_segments:
                # Support older listings only through a consecutive sequence
                # of validated conditional-age checkpoints, starting at entry.
                maximum = None
                for age in VALIDATION_AGES:
                    age_score = segment.get("by_age", {}).get(str(age), {})
                    if not passes(age_score):
                        break
                    maximum = float(age)
                if maximum is not None:
                    age_supported.append(dict(segment, max_age_days=maximum))
            valid_segments = age_supported
        if (passes(score) and int(horizon) in horizons and
                (not (evaluation.get("requires_segment_support") or evaluation.get("requires_age_support")) or valid_segments)):
            supported.append(int(horizon))
            segments[str(horizon)] = [{key:s[key] for key in ("category", "condition", "max_duration_days", "max_age_days") if key in s}
                                     for s in valid_segments]
    return {"policy_version": ACCEPTANCE_VERSION, "accepted": bool(supported),
            "requires_age_support":bool(evaluation.get("requires_age_support")),
            "supported_horizons": sorted(supported),
            "supported_segments":segments,
            "status": "accepted" if supported else "experimental",
            "reason": None if supported else "No horizon cleared chronological baseline, calibration and support checks"}


def accepted_horizon(acceptance, horizon):
    return bool(acceptance.get("policy_version") == ACCEPTANCE_VERSION
                and acceptance.get("accepted") and horizon in acceptance.get("supported_horizons", []))


def accepted_segment(acceptance, horizon, category, condition, end_age, age_days=None):
    if (not math.isfinite(end_age) or end_age < 0 or
            (acceptance.get("requires_age_support") and age_days is None) or
            (age_days is not None and (not math.isfinite(age_days) or age_days < 0))):
        return False
    return bool(accepted_horizon(acceptance, horizon) and any(
        row.get("category") == category and row.get("condition") == condition
        and end_age <= row.get("max_duration_days", 0)
        and (age_days is None or age_days <= row.get("max_age_days", -1))
        for row in acceptance.get("supported_segments", {}).get(str(horizon), [])))


def validated_segment_support(evaluation, horizon, category, condition, end_age, age_days=None):
    """Duration support requires this horizon's holdout in this segment.

    Baseline and calibration failures affect acceptance, not duration support.
    Missing holdouts conservatively count as extrapolation, including candidates.
    """
    def supports(score):
        return bool(score.get("available") and score.get("horizon_supported")
                    and score.get("n_horizon_observed", score.get("n_test", 0)) >= 10)
    if not math.isfinite(end_age) or end_age < 0:
        return False
    score = evaluation.get("by_horizon", {}).get(str(horizon), {})
    if not supports(score):
        return False
    for segment in score.get("by_segment", []):
        if (segment.get("category") != category or segment.get("condition") != condition
                or not supports(segment) or end_age > segment.get("max_duration_days", 0)):
            continue
        if evaluation.get("requires_age_support"):
            if age_days is None or not math.isfinite(age_days) or age_days < 0:
                return False
            maximum = -1
            for age in VALIDATION_AGES:
                if not supports(segment.get("by_age", {}).get(str(age), {})):
                    break
                maximum = age
            if age_days > maximum:
                continue
        return True
    return False


def _mapping(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def forecast_sources(rows, read):
    """Batch safe registry and entry reads through the caller's transaction."""
    models, entries = {}, {}
    for table, keys in (("predictive_models", sorted({r["model_version"] for r in rows})),
                        ("predictive_feature_snapshots", sorted({r["listing_pk"] for r in rows}))):
        for offset in range(0, len(keys), 400):
            batch = keys[offset:offset+400]
            marks = ",".join("?" for _ in batch)
            if table == "predictive_models":
                records = read(table, "SELECT * FROM predictive_models WHERE model_version IN ("+marks+")", batch)
                models.update({r["model_version"]:r for r in records})
            else:
                records = read(table, "SELECT f.*,l.first_seen_at FROM predictive_feature_snapshots f "
                               "JOIN listings l ON l.listing_pk=f.listing_pk WHERE f.listing_pk IN ("+marks+")", batch)
                entries.update({r["listing_pk"]:r for r in records})
    return models, entries


def forecast_validation(family, row, model=None, entry=None):
    """Verify stored listing support without trusting flags or loading a model.

    Both writers and read-only views use registered chronological evidence and
    the entry snapshot. Today's category/condition cannot repair missing inputs.
    """
    from ..metrics import parse_ts
    result = {"is_accepted":False, "is_extrapolated":None, "diagnostics_ok":None,
              "validation_available":False, "validation_improves_baseline":False,
              "validation_reason":"Registered chronological validation is unavailable"}
    if (not model or model.get("model_family") != family
            or model.get("model_version") != row.get("model_version")
            or model.get("feature_version") != LISTING_METHODS.get(family)):
        return result
    try:
        values = ([row.get("p_sale")] if family == "sale_probability" else
                  [row.get(k) for k in ("price_ci_lo", "predicted_price_eur", "price_ci_hi")])
        finite = all(not isinstance(v, bool) and math.isfinite(v) for v in values)
        ordered = 0 <= values[0] <= 1 if family == "sale_probability" else 0 <= values[0] <= values[1] <= values[2]
        if not finite or not ordered:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        result["validation_reason"] = "Forecast values must be finite, bounded and ordered"
        return result
    computed, trained = parse_ts(row.get("computed_at")), parse_ts(model.get("trained_at"))
    if not computed or not trained or computed < trained:
        result["validation_reason"] = "Forecast time must be at or after its registered training time"
        return result
    metrics = _mapping(model.get("metrics_json"))
    evaluation = _mapping(metrics.get("evaluation"))
    recorded = _mapping(metrics.get("acceptance"))
    fit_diagnostics = _mapping(metrics.get("diagnostics")).get("diagnostics_ok")
    diagnostics = (fit_diagnostics is True
                        and _mapping(evaluation.get("diagnostics")).get("diagnostics_ok") is True
                   if family == "price_trajectory" else True)
    result["diagnostics_ok"] = fit_diagnostics if family == "price_trajectory" else None
    entry = entry or {}
    observed, first = parse_ts(entry.get("observed_at")), parse_ts(entry.get("first_seen_at"))
    features, frozen = _mapping(row.get("features_json")), _mapping(entry.get("features_json"))
    if "cat_root" in features and "condition" in features:
        result["is_extrapolated"] = True
    if (not observed or not first or observed > computed or abs((observed-first).total_seconds()) > 1
            or entry.get("feature_version") != ENTRY_FEATURE_VERSION
            or "cat_root" not in features or "condition" not in features):
        result["validation_reason"] = "Frozen forecast inputs and an entry-time snapshot are required"
        return result
    category, condition = frozen.get("category_root_norm") or "unknown", frozen.get("condition_norm") or "unknown"
    if features["cat_root"] != category or features["condition"] != condition:
        result["validation_reason"] = "Forecast category and condition differ from the frozen entry inputs"
        return result
    horizon = row.get("horizon_days", row.get("day"))
    age = features.get("age_days") if family == "sale_probability" else None
    if family == "sale_probability" and (isinstance(age, bool) or not isinstance(age, (int, float))
            or not math.isfinite(age) or age < 0 or abs(age-(computed-first).total_seconds()/86400) > 1/86400):
        result["validation_reason"] = "Sale support needs the listing's recorded conditional age at forecast time"
        return result
    try:
        end_age = horizon+(age or 0)
        supported = validated_segment_support(evaluation, horizon, category, condition, end_age, age)
        acceptance = guidance_acceptance(evaluation, diagnostics)
        accepted = (recorded.get("policy_version") == ACCEPTANCE_VERSION and recorded.get("accepted") is True
                    and accepted_segment(recorded, horizon, category, condition, end_age, age)
                    and accepted_segment(acceptance, horizon, category, condition, end_age, age) and supported)
        score = evaluation.get("by_horizon", {}).get(str(horizon), {})
        segment = next((s for s in score.get("by_segment", [])
                        if s.get("category") == category and s.get("condition") == condition), {})
        result.update(is_extrapolated=not supported,
                      validation_available=bool(score.get("available") and segment.get("available")),
                      validation_improves_baseline=bool(score.get("improves_baseline") and segment.get("improves_baseline")))
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        result["validation_reason"] = "Registered horizon or segment evidence is malformed"
        return result
    claimed = row.get("is_accepted") is True or (type(row.get("is_accepted")) is int and row["is_accepted"] == 1)
    result["is_accepted"] = bool(claimed and accepted)
    result["validation_reason"] = (None if result["is_accepted"] else
        "Horizon, category/condition or conditional age lacks chronological holdout support" if not supported else
        "Registered baseline, calibration or diagnostic checks do not qualify this forecast" if not accepted else
        "Forecast was recorded without guidance approval")
    return result
