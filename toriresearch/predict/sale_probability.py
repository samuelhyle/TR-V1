"""Static entry-feature Cox models for sale and competing exits.

Time starts at first observation, not the platform publication date. Sale means
an observed confirmed or inferred sale label; disappearance is right-censoring.
Cause-specific hazards are integrated into a cumulative incidence, conditional
on still being active at the listing's current observed age.
"""

import json
import math
import pickle
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .. import config, db, metrics
from ..analysis import is_priced, SALE_OUTCOMES
from ._common import (HORIZONS, VALIDATION_AGES, accepted_segment, guidance_acceptance, validated_segment_support, finite_price, horizons_checked, load_bundle,
                      save_bundle, version_id, _atomic_bytes, entry_features, entry_prices,
                      forecast_sources, forecast_validation, LISTING_METHODS)

MODEL_VERSION = LISTING_METHODS["sale_probability"]
MIN_TRAIN_N = 200
MIN_SALES = 20
CAT_COLUMNS = ("cat_root", "condition", "municipality", "platform", "subplatform")


def _sale_horizons(horizons):
    values = horizons_checked(horizons)
    if any(h not in HORIZONS for h in values):
        raise ValueError(f"sale horizons must be one of {list(HORIZONS)}")
    return values


def _initial_prices(asof):
    return entry_prices(asof)


def _cox_features(row, initial_price, snapshot=None):
    # Deliberately exclude end-of-followup probe, repricing and outcome features.
    snapshot = snapshot or {}
    return {"log_price": math.log(initial_price),
            "cat_root": snapshot.get("category_root_norm") or "unknown",
            "condition": snapshot.get("condition_norm") or "unknown",
            "municipality": snapshot.get("municipality_norm") or "unknown",
            "platform": row.get("platform") or "unknown",
            "subplatform": snapshot.get("subplatform") or "unknown"}


def _prepare_training_data(cutoff):
    rows = db.query("SELECT * FROM listings WHERE first_seen_at IS NOT NULL")
    initial = _initial_prices(cutoff)
    snapshots = entry_features(cutoff)
    # Only observations actually recorded by the historical training cutoff
    # can establish censoring; later follow-up never extends an earlier cohort.
    anchors = {}
    for r in db.query("SELECT listing_pk,observed_at FROM events UNION ALL "
                      "SELECT listing_pk,observed_at FROM price_history UNION ALL "
                      "SELECT listing_pk,observed_at FROM price_observations"):
        stamp = metrics.parse_ts(r["observed_at"])
        if stamp and stamp <= cutoff:
            anchors[r["listing_pk"]] = max(stamp, anchors.get(r["listing_pk"], stamp))
    records = []
    for row in rows:
        first = metrics.parse_ts(row["first_seen_at"])
        if not first or first >= cutoff or row.get("is_giveaway"):
            continue
        entry = initial.get(row["listing_pk"])
        if entry is None:
            price_time = metrics.parse_ts(row.get("price_observed_at") or row.get("last_seen_at"))
            price = finite_price(row.get("listed_price_eur"))
            if not price or not price_time or abs((price_time - first).total_seconds()) > 1:
                continue
            entry = (price, price_time)
        # A price collected later cannot be an entry-time covariate.
        if abs((entry[1] - first).total_seconds()) > 1:
            continue
        outcome = row.get("outcome_status")
        sale = outcome in SALE_OUTCOMES
        competing = outcome in ("withdrawn", "expired")
        outcome_at = metrics.parse_ts(row.get("outcome_observed_at"))
        end = metrics.parse_ts(row.get("observation_coverage_end")) or metrics.parse_ts(row.get("last_seen_at"))
        if (sale or competing) and outcome_at and first <= outcome_at <= cutoff:
            end = outcome_at
        else:
            sale = competing = False
            fields = (("last_live_at", "observation_coverage_end") if row.get("coverage") == "gone_uncertain"
                      else ("last_seen_at", "last_live_at", "observation_coverage_end"))
            evidence = [metrics.parse_ts(row.get(k)) for k in fields]
            evidence = [stamp for stamp in evidence if stamp and stamp <= cutoff]
            anchor = anchors.get(row["listing_pk"])
            if anchor and row.get("coverage") != "gone_uncertain":
                evidence.append(anchor)
            end = max(evidence) if evidence else None
        if not end or end <= first:
            continue
        feats = _cox_features(row, entry[0], snapshots.get(row["listing_pk"]))
        feats.update(duration=(end-first).total_seconds()/86400,
                     event_sale=int(sale), event_competing=int(competing),
                     listing_pk=row["listing_pk"], first_seen_at=first, end_at=end)
        records.append(feats)
    return pd.DataFrame(records)


def _encode(frame, columns=None):
    X = pd.get_dummies(frame[["log_price", *CAT_COLUMNS]], columns=list(CAT_COLUMNS), dtype=float)
    if columns is not None:
        return X.reindex(columns=columns, fill_value=0).astype(float)
    # Stable one-hot schema is stored in the artifact, never recoded per batch.
    return X.loc[:, X.nunique() > 1].astype(float)


def _baseline_competing(df):
    times = np.sort(df["duration"].unique())
    increments = [((df["duration"].eq(t) & df["event_competing"].eq(1)).sum()
                   / df["duration"].ge(t).sum()) for t in times]
    return pd.DataFrame({"baseline cumulative hazard": np.cumsum(increments)}, index=times)


class SaleProbabilityModel:
    def __init__(self, cph_model=None, baseline_hazard=None, feature_columns=None,
                 model_version=MODEL_VERSION, competing_model=None, competing_baseline=None):
        self.cph_model = cph_model
        self.baseline_hazard = baseline_hazard
        self.feature_columns = feature_columns or []
        self.model_version = model_version
        self.competing_model = competing_model
        self.competing_baseline = competing_baseline
        self.trained_at = None
        self.max_duration = 0.0
        self.training_metrics = {}

    def save(self, path):
        _atomic_bytes(path, pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL))

    @classmethod
    def load(cls, path):
        with open(path, "rb") as handle:
            model = pickle.load(handle)
        if not isinstance(model, cls):
            raise ValueError("Legacy Cox artifact; run --train again")
        return model

    def _cause_hazards(self, features):
        if self.cph_model is None:
            raise ValueError("Model not trained")
        X = features.reindex(columns=self.feature_columns, fill_value=0)
        sale = self.cph_model.baseline_cumulative_hazard_
        comp = (self.competing_model.baseline_cumulative_hazard_
                if self.competing_model is not None else self.competing_baseline)
        if comp is None:
            comp = pd.DataFrame({"hazard":np.zeros(len(sale))},index=sale.index)
        times = np.union1d(sale.index.to_numpy(float), comp.index.to_numpy(float))
        def cumulative(table):
            idx = np.searchsorted(table.index.to_numpy(float), times, side="right") - 1
            vals = table.iloc[:, 0].to_numpy(float)
            return np.where(idx >= 0, vals[np.maximum(idx, 0)], 0.0)
        sale_cum, comp_cum = cumulative(sale), cumulative(comp)
        sale_risk = self.cph_model.predict_partial_hazard(X).to_numpy(float)
        comp_risk = (self.competing_model.predict_partial_hazard(X).to_numpy(float)
                     if self.competing_model is not None else np.ones(len(X)))
        return times, sale_cum, comp_cum, sale_risk, comp_risk

    def predict_sale_probability(self, features, horizons, ages=None):
        horizons = horizons_checked(horizons)
        ages = np.zeros(len(features)) if ages is None else np.asarray(ages, dtype=float)
        if len(ages) != len(features) or np.any(~np.isfinite(ages)) or np.any(ages < 0):
            raise ValueError("listing ages must be finite, nonnegative, and match features")
        times, sale_cum, comp_cum, sale_risk, comp_risk = self._cause_hazards(features)
        sale_steps = np.diff(np.r_[0.0, sale_cum])
        comp_steps = np.diff(np.r_[0.0, comp_cum])
        out = np.zeros((len(features), len(horizons)))
        for i, age in enumerate(ages):
            eligible = (times > age) & (times <= age + max(horizons))
            ds = sale_steps[eligible] * sale_risk[i]
            dc = comp_steps[eligible] * comp_risk[i]
            total = ds + dc
            survival_before = np.exp(-np.r_[0.0, np.cumsum(total)[:-1]])
            cif = np.cumsum(survival_before * np.divide(ds, total, out=np.zeros_like(ds), where=total>0)
                            * (-np.expm1(-total)))
            for j, h in enumerate(horizons):
                if age+h > times.max():
                    out[i, j] = np.nan
                    continue
                k = np.searchsorted(times[eligible], age+h, side="right")-1
                out[i, j] = cif[k] if k >= 0 else 0.0
        return np.clip(out, 0, 1)

    def predict_survival(self, features, horizons, ages=None):
        """Probability of no exit of either cause, conditional on current age."""
        horizons = horizons_checked(horizons)
        ages = np.zeros(len(features)) if ages is None else np.asarray(ages, dtype=float)
        times, sc, cc, sr, cr = self._cause_hazards(features)
        def at(values, t):
            i = np.searchsorted(times, t, side="right")-1
            return values[i] if i >= 0 else 0.0
        return np.array([[math.exp(-sr[i]*(at(sc, a+h)-at(sc, a))
                                   -cr[i]*(at(cc, a+h)-at(cc, a))) if a+h <= times.max() else np.nan for h in horizons]
                         for i, a in enumerate(ages)])


def _fit(df):
    from lifelines import CoxPHFitter
    if len(df) < MIN_TRAIN_N or int(df.get("event_sale", pd.Series(dtype=int)).sum()) < MIN_SALES:
        raise ValueError(f"Insufficient Cox data: {len(df)} usable listings, "
                         f"{int(df.get('event_sale', pd.Series(dtype=int)).sum())} observed sales; "
                         f"need {MIN_TRAIN_N} listings and {MIN_SALES} sales")
    X = _encode(df)
    cph = CoxPHFitter(penalizer=0.1)
    cph.fit(X.assign(duration=df.duration, event=df.event_sale), "duration", "event")
    comp = None
    if df.event_competing.sum() >= 10:
        comp = CoxPHFitter(penalizer=0.1)
        comp.fit(X.assign(duration=df.duration, event=df.event_competing), "duration", "event")
    model = SaleProbabilityModel(cph, cph.baseline_hazard_, list(X.columns),
                                competing_model=comp, competing_baseline=_baseline_competing(df))
    model.max_duration = float(df.duration.max())
    try:
        concordance = float(cph.concordance_index_)
    except ZeroDivisionError:
        # Every listing in the cohort shares a duration, so no pair can be
        # ordered. lifelines raises ZeroDivisionError here, which is neither a
        # ValueError nor an ImportError, so it escaped both the evaluation
        # harness and the CLI's no-traceback handling and surfaced as a stack
        # trace in the job panel. A cohort that cannot rank its own listings
        # cannot support a survival fit.
        raise ValueError("Cox fit has no orderable listing pairs; observed durations "
                         "do not vary enough in this cohort to rank listings") from None
    model.training_metrics = {"n_listings": len(df), "n_sales": int(df.event_sale.sum()),
                              "n_competing": int(df.event_competing.sum()),
                              "training_concordance": concordance}
    return model


def train_sale_probability(cutoff_hours=None, now=None):
    now = now or datetime.now(timezone.utc)
    asof = now if cutoff_hours is None else now-timedelta(hours=cutoff_hours)
    with db.read_snapshot():
        df = _prepare_training_data(asof)
    model = _fit(df)
    evaluation = _chronological_evaluation(asof)
    model.acceptance = guidance_acceptance(evaluation)
    model.activated = model.acceptance["accepted"]
    model.training_metrics.update(evaluation=evaluation, acceptance=model.acceptance)
    model.trained_at = now.isoformat()
    model.model_version = version_id(MODEL_VERSION, df.to_dict("records"), now)
    save_bundle("sale_probability", {"model": model, "model_version": model.model_version,
                "method_version": MODEL_VERSION, "trained_at": model.trained_at},
                {"n_observations": len(df), "n_entities": len(df),
                 "train_start_at": df.first_seen_at.min().isoformat(), "train_end_at": asof.isoformat(),
                 "metrics": model.training_metrics,
                 "method": {"features": model.feature_columns, "time_origin": "first_seen",
                            "competing_hazard": "cox" if model.competing_model is not None else "nelson_aalen",
                            "intervals": "not estimated", "sale_labels": sorted(SALE_OUTCOMES)}},
                activate=model.activated)
    return model


def predict_sale_probability(listing_pks=None, horizons=None, model=None, now=None, experimental=False):
    horizons = _sale_horizons(horizons)
    now = now or datetime.now(timezone.utc)
    if listing_pks == []:
        return []
    model = model or (load_bundle("sale_probability", MODEL_VERSION, experimental=True) if experimental
                      else load_bundle("sale_probability", MODEL_VERSION))["model"]
    acceptance = getattr(model, "acceptance", {})
    if not experimental and not acceptance.get("accepted"):
        raise ValueError("Sale model has not cleared guidance acceptance; inspect the experimental candidate")
    where = ["status_norm='active'", "outcome_status='unknown'", "coverage='collecting'",
             "COALESCE(is_reserved,FALSE)=FALSE", "COALESCE(is_giveaway,FALSE)=FALSE", "listed_price_eur>0"]
    params = list(listing_pks or [])
    if params:
        where.append("listing_pk IN (" + ",".join("?" for _ in params) + ")")
    with db.read_snapshot():
        rows = db.query("SELECT * FROM listings WHERE " + " AND ".join(where), params)
        initial = _initial_prices(now)
        snapshots = entry_features(now)
    cutoff = now-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    rows = [r for r in rows if is_priced(r, cutoff) and metrics.parse_ts(r["last_seen_at"]) <= now
            and metrics.parse_ts(r["price_observed_at"] or r["last_seen_at"]) <= now
            and metrics.parse_ts(r["first_seen_at"]) and metrics.parse_ts(r["first_seen_at"]) <= now
            and r["listing_pk"] in initial
            and abs((initial[r["listing_pk"]][1]-metrics.parse_ts(r["first_seen_at"])).total_seconds()) <= 1]
    if not rows:
        return []
    feats = [_cox_features(r, initial[r["listing_pk"]][0], snapshots.get(r["listing_pk"])) for r in rows]
    ages = [(now-metrics.parse_ts(r["first_seen_at"])).total_seconds()/86400 for r in rows]
    probs = model.predict_sale_probability(_encode(pd.DataFrame(feats), model.feature_columns), horizons, ages)
    predictions = []
    for i, r in enumerate(rows):
        for j, h in enumerate(horizons):
            supported = validated_segment_support(model.training_metrics.get("evaluation", {}), h,
                feats[i]["cat_root"], feats[i]["condition"], ages[i]+h, ages[i])
            accepted = supported and accepted_segment(acceptance, h, feats[i]["cat_root"],
                                                       feats[i]["condition"], ages[i]+h, ages[i])
            if not np.isfinite(probs[i,j]) or (not experimental and not accepted):
                continue
            predictions.append({"listing_pk": r["listing_pk"], "model_version": model.model_version,
                "horizon_days": h, "p_sale": round(float(probs[i,j]), 6),
                "p_sale_ci_lo": None, "p_sale_ci_hi": None, "expected_price_eur": None,
                "price_ci_lo": None, "price_ci_hi": None,
                "features_json": json.dumps(dict(feats[i], age_days=ages[i])),
                "is_accepted": accepted, "is_extrapolated": not supported, "computed_at": now.isoformat()})
    return predictions


def persist_predictions(predictions):
    cols = ("listing_pk", "model_version", "horizon_days", "p_sale", "p_sale_ci_lo", "p_sale_ci_hi",
            "expected_price_eur", "price_ci_lo", "price_ci_hi", "features_json", "is_extrapolated", "is_accepted", "computed_at")
    conn = db.connect()
    try:
        with conn.transaction():
            sources = forecast_sources(predictions, lambda table, sql, params:
                                       [dict(r) for r in conn.execute(sql, params).fetchall()])
            for p in predictions:
                if not math.isfinite(p["p_sale"]) or not 0 <= p["p_sale"] <= 1:
                    raise ValueError("p_sale must be finite and between zero and one")
                _sale_horizons([p["horizon_days"]])
                validation = forecast_validation("sale_probability", p,
                    sources[0].get(p["model_version"]), sources[1].get(p["listing_pk"]))
                if p.get("is_accepted") and not validation["is_accepted"]:
                    raise ValueError("accepted sale forecasts require registered chronological validation: "
                                     + validation["validation_reason"])
                row = dict(p, **validation)
                conn.execute("INSERT INTO sale_predictions (" + ",".join(cols) + ") VALUES ("
                             + ",".join("?" for _ in cols) + ") ON CONFLICT (listing_pk,model_version,horizon_days) "
                             "DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols[3:]),
                             [row.get(c) for c in cols])
    finally:
        conn.close()
    return len(predictions)


def _ipcw_scores(train, test, probabilities, horizon):
    from lifelines import KaplanMeierFitter
    censor = ~(train.event_sale.astype(bool) | train.event_competing.astype(bool))
    km = KaplanMeierFitter().fit(train.duration, event_observed=censor)
    durations = test.duration.to_numpy(float)
    probabilities = np.asarray(probabilities, dtype=float)
    if probabilities.shape != durations.shape or np.any(~np.isfinite(probabilities)) or np.any((probabilities<0) | (probabilities>1)):
        raise ValueError("Evaluation probabilities must be finite, bounded, and match the holdout")
    terminal = test.event_sale.to_numpy(bool) | test.event_competing.to_numpy(bool)
    before = terminal & (durations <= horizon)
    survives = (durations >= horizon) & ~before
    labels = (test.event_sale.to_numpy(bool) & (durations <= horizon)).astype(float)
    weights = np.zeros(len(test))
    # Left limits include follow-up that reaches the horizon exactly, before
    # censoring at that clock removes a subject from the next instant's risk set.
    table = km.survival_function_
    times, values = table.index.to_numpy(float), table.iloc[:,0].to_numpy(float)
    idx = np.searchsorted(times, durations, side="left")-1
    g = np.where(idx>=0, values[np.maximum(idx,0)], 1.0)
    at_horizon = np.searchsorted(times, horizon, side="left")-1
    gh = float(values[at_horizon]) if at_horizon>=0 else 1.0
    if gh <= 1e-6 or np.any(g[before] <= 1e-6) or not (before | survives).any():
        raise ValueError("Insufficient follow-up to evaluate the requested horizon")
    weights[before] = 1/g[before]
    weights[survives] = 1/gh
    return {"brier_score": float(np.mean(weights*(probabilities-labels)**2)),
            "mae": float(np.mean(weights*np.abs(probabilities-labels))),
            "n_horizon_observed": int((before | survives).sum()),
            "n_horizon_sales": int(labels.sum()),
            "n_early_censored": int((~(before | survives)).sum()),
            "censor_survival_at_horizon": gh}


def _follow_up_reason(horizon, follow_up):
    """Name the evidence a horizon is actually short of, in days and counts."""
    longest = float(follow_up.max()) if len(follow_up) else 0.0
    if longest < horizon:
        return (f"No listing has {horizon} days of follow-up; the longest observed is "
                f"{longest:.1f} days. {horizon - longest:.1f} more days of collection are "
                f"needed before this horizon can be validated")
    counted = int((follow_up >= horizon).sum())
    return (f"{counted} listing{'s' if counted != 1 else ''} "
            f"{'have' if counted != 1 else 'has'} {horizon} days of follow-up; "
            f"{MIN_TRAIN_N + 10} are needed to train and hold out at this horizon")


def _horizon_split(full, now, horizon):
    """The latest chronological boundary whose holdout still reaches `horizon`.

    A single fixed 80/20 boundary spends the newest fifth of the corpus on a
    holdout that has hours rather than days of life. Nothing can then be scored
    at any horizon, and because the holdout is the newest slice it never gains
    follow-up until the corpus is five times the horizon in age -- so a fixed
    boundary cannot validate a 90-day forecast on a one-year-old corpus, let
    alone a 7-day one on a three-day-old corpus.

    Moving the boundary per horizon keeps the two sides disjoint and puts the
    boundary as late as the horizon's own follow-up allows. The most recent
    `horizon` days of entries are embargoed: they are neither trained on nor
    tested, because neither a censored nor a completed outcome can be attributed
    to them yet.
    """
    ordered = full.sort_values(["first_seen_at", "listing_pk"])
    scorable = ordered[ordered.first_seen_at <= now-timedelta(days=horizon)]
    if len(scorable) < MIN_TRAIN_N+10:
        return None, scorable
    return scorable.iloc[int(len(scorable)*0.8)].first_seen_at, scorable


def _chronological_evaluation(now):
    with db.read_snapshot():
        full = _prepare_training_data(now)
        if full.empty:
            return {"available":False, "reason":"No usable Cox training data"}
    follow_up = (now-full.first_seen_at).dt.total_seconds()/86400
    results, boundary = {}, None
    for horizon in HORIZONS:
        split_at, scorable = _horizon_split(full, now, horizon)
        if split_at is None:
            results[str(horizon)] = {"available":False, "horizon_days":horizon,
                                      "n_holdout":int(len(scorable)),
                                      "n_eligible_for_horizon":int(len(scorable)),
                                      "reason":_follow_up_reason(horizon, follow_up)}
            continue
        with db.read_snapshot():
            train = _prepare_training_data(split_at)
        test = full[(full.first_seen_at >= split_at)
                    & (full.first_seen_at <= now-timedelta(days=horizon))].copy()
        boundary = boundary or split_at
        try:
            if len(train) < MIN_TRAIN_N or len(test) < 10:
                raise ValueError(f"Insufficient chronological split: {len(train)} training and "
                                 f"{len(test)} holdout listings at {horizon} days")
            results[str(horizon)] = _evaluate_horizon(train, test, _fit(train), horizon, split_at)
        except ValueError as exc:
            results[str(horizon)] = {"available":False, "horizon_days":horizon,
                                      "n_holdout":int(len(scorable)), "n_eligible_for_horizon":int(len(test)),
                                      "reason":str(exc)}
    return {"available":any(r.get("available") for r in results.values()), "requires_segment_support":True,
            "requires_age_support":True, "validation_ages_days":list(VALIDATION_AGES), "by_horizon":results,
            "train_end_at":boundary.isoformat() if boundary else None,
            "test_start_at":results[str(HORIZONS[0])].get("test_start_at"),
            "corpus_span_days":float(follow_up.max()),
            "embargo_days":{str(h):int((follow_up < h).sum()) for h in HORIZONS},
            "sale_labels":sorted(SALE_OUTCOMES)}


def _evaluate_horizon(train, test, model, horizon, split_at, segments=True, age=0):
    if age < 0 or not math.isfinite(age):
        raise ValueError("Evaluation age must be finite and nonnegative")
    # Landmark cohorts contain only listings observed as still at risk at this
    # age. Their outcomes and censoring clocks become time since the landmark.
    train = train[train.duration > age].copy()
    test = test[test.duration > age].copy()
    if len(train)<30 or len(test)<10:
        raise ValueError(f"Insufficient listings still observed at age {age:g} days")
    train["duration"] -= age
    test["duration"] -= age
    if age+horizon > model.max_duration or horizon > float(train.duration.max()):
        raise ValueError(f"Requested {horizon}-day evaluation exceeds {model.max_duration:.1f} days of training follow-up")
    probs = model.predict_sale_probability(_encode(test, model.feature_columns), [horizon], ages=np.full(len(test),age))[:,0]
    scores = _ipcw_scores(train, test, probs, horizon)
    outcomes = np.where(train.event_sale, "sold_inferred", np.where(train.event_competing, "withdrawn", "unknown"))
    baseline = metrics.cumulative_incidence(train.duration.to_numpy(), outcomes, [horizon], "sold_inferred")[horizon]
    baseline_scores = _ipcw_scores(train, test, np.full(len(test), baseline), horizon)
    from lifelines.utils import concordance_index
    try:
        concordance = float(concordance_index(test.duration,
                           -model.cph_model.predict_partial_hazard(_encode(test, model.feature_columns)), test.event_sale))
    except ZeroDivisionError:
        concordance = None
    # IPCW MAE against zero equals the weighted observed sale incidence.
    observed = _ipcw_scores(train, test, np.zeros(len(test)), horizon)["mae"]
    calibration_error = abs(float(np.mean(probs))-observed)
    result = dict(scores, available=True, horizon_days=horizon, age_days=age, n_train=len(train), n_test=len(test),
                concordance=concordance, baseline_brier_score=baseline_scores["brier_score"],
                improves_baseline=bool(scores["brier_score"]<baseline_scores["brier_score"]),
                calibration_error=calibration_error, calibration_ok=bool(calibration_error<=.1),
                model_version=MODEL_VERSION, train_end_at=split_at.isoformat(),
                test_start_at=test.first_seen_at.min().isoformat(),
                evaluation="chronological IPCW; sale labels include inferred outcomes",
                horizon_supported=bool(age+horizon <= model.max_duration))
    if segments:
        result["by_segment"] = []
        for (category, condition), subgroup in test.groupby(["cat_root", "condition"]):
            train_group = train[(train.cat_root == category) & (train.condition == condition)]
            if len(subgroup) < 10 or len(train_group) < 30 or train_group.event_sale.sum() < 20:
                continue
            try:
                score = _evaluate_horizon(train_group, subgroup, model, horizon, split_at, segments=False)
            except ValueError:
                continue
            score.update(category=category, condition=condition, max_duration_days=float(train_group.duration.max()))
            score["by_age"] = {"0":dict(score)}
            for landmark in VALIDATION_AGES[1:]:
                try:
                    score["by_age"][str(landmark)] = _evaluate_horizon(
                        train_group, subgroup, model, horizon, split_at, segments=False, age=landmark)
                except ValueError as exc:
                    score["by_age"][str(landmark)] = {"available":False, "age_days":landmark, "reason":str(exc)}
            result["by_segment"].append(score)
    return result


def evaluate_model(horizon=30, now=None):
    _sale_horizons([horizon])
    result = _chronological_evaluation(now or datetime.now(timezone.utc))
    score = result.get("by_horizon", {}).get(str(horizon), result)
    if not score.get("available"):
        raise ValueError(score.get("reason", "Insufficient chronological validation"))
    return score
