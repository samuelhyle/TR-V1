"""Completed Helsinki-week arrival cohorts with STL and PELT evidence tiers.

These describe the collected population. Missing weeks are zero observed
arrivals, not proof of an empty market. PELT scores are descriptive break
strength/recency scores, not calibrated probabilities.

Weekly arrivals are counts, so projecting them is a Poisson-Gamma (negative
binomial) predictive problem rather than a regression through a smooth curve.
The point forecast applies a robust slope and, once two annual cycles have
been observed, the seasonal factor for the target week. The interval carries
the count noise, the overdispersion the cohort actually shows, and the spread
of the slope estimate itself, so it widens with horizon rather than pretending
a thirteen-week projection is as sharp as a one-week one. Nothing here
forecasts a market: it projects the arrival process this collector observes,
and a projection is withheld whenever the completed weeks cannot support one.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .. import db, metrics
from ._common import (finite_price, positive_int, entry_prices, guidance_acceptance, trend_unavailability,
                      TREND_SEASONAL_WEEKS, TREND_BREAK_WEEKS, ARRIVAL_HORIZONS)

MODEL_VERSION = "cat-trend-stl-pelt-v4"
PELT_VERSION = "cat-trend-pelt-v4"
EARLY_VERSION = "cat-trend-early-v4"
FORECAST_VERSION = "cat-forecast-nb-v2"
MIN_WEEKS = 1
PELT_MIN_WEEKS = TREND_BREAK_WEEKS
STL_MIN_WEEKS = TREND_SEASONAL_WEEKS  # two observed annual cycles, period 52
SEASON_PERIOD = 52
MAX_CATEGORIES = 60
HELSINKI = ZoneInfo("Europe/Helsinki")

# A direction is read from a window that scales with the series but never
# below this, so one catch-up collection week cannot establish one.
DIRECTION_MIN_WEEKS = 8
DIRECTION_MAX_WEEKS = 26
# The median pairwise slope must exceed this share of the level to be a
# direction at all.
DIRECTION_SLOPE = .04
DIRECTION_EVALUATION_WEEKS = 8
DIRECTION_MIN_EVALUATION_WINDOWS = 10
DIRECTION_MIN_EVALUATION_ARRIVALS = 30
# Weekly arrivals are counts: at an expected level of mu the sampling CV is
# 1/sqrt(mu), so dispersion around 1.1x that expectation is still sampling.
# Volatility is called only above the larger of a relative share of the level
# and that band, which keeps a thin but steady category from being labelled
# erratic for being small without hiding real week-to-week whiplash.
VOLATILITY_RELATIVE = .5
VOLATILITY_SAMPLING_MULTIPLE = 1.1

FORECAST_HORIZONS = ARRIVAL_HORIZONS
FORECAST_MIN_WEEKS = 8
FORECAST_MIN_ARRIVALS = 30
FORECAST_INTERVAL = .9
# A horizon is projected only when the cohort covers at least this many
# weeks per week of lead time, so an eight-week series projects one week out
# and not thirteen.
FORECAST_WEEKS_PER_HORIZON = 4
FORECAST_MAX_DISPERSION = 100.0
FORECAST_MIN_HOLDOUT_WEEKS = 10
# Leave room to validate every lead time after the seasonal method becomes
# available. A fixed 104-week window would only ever test nonseasonal prefixes.
FORECAST_DEFAULT_WEEKS = STL_MIN_WEEKS+FORECAST_MIN_HOLDOUT_WEEKS+max(FORECAST_HORIZONS)


def _listing_rows(region_norm=None, categories=None):
    conditions = ["COALESCE(l.is_giveaway,FALSE)=FALSE"]
    params = []
    if region_norm is not None:
        conditions.append("l.region_norm=?")
        params.append(region_norm)
    if categories:
        conditions.append("l.category_norm IN ("+",".join("?" for _ in categories)+")")
        params.extend(categories)
    return db.query(
        "SELECT l.listing_pk,l.category_norm,l.first_seen_at,l.listed_price_eur,"
        "l.price_observed_at,l.last_seen_at FROM listings l WHERE "+" AND ".join(conditions),
        params)


def _arrival_frames(rows, start, current_week, now, initial_prices, grouped=True):
    """Monday-aligned weekly arrival cohorts, one frame per requested category.

    Every category in the scope comes out of a single query. Asking the
    database once per category is what made a sixty-category run cost sixty
    scans of the same table.
    """
    buckets = defaultdict(list)
    for r in rows:
        stamp = metrics.parse_ts(r["first_seen_at"])
        if not stamp:
            continue
        date = stamp.astimezone(HELSINKI).date()
        week = date-timedelta(days=date.weekday())
        if not start <= week < current_week:
            continue
        entry = initial_prices.get(r["listing_pk"])
        price = entry[0] if entry and abs((entry[1]-stamp).total_seconds()) <= 1 else None
        if price is None:
            price_time = metrics.parse_ts(r["price_observed_at"] or r["last_seen_at"])
            if price_time and abs((price_time-stamp).total_seconds()) <= 1:
                price = finite_price(r["listed_price_eur"])
        buckets[r["category_norm"] if grouped else ""].append((pd.Timestamp(week), price))
    frames = {}
    for name, records in buckets.items():
        frame = pd.DataFrame(records, columns=["week", "price"])
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
        grouped_frame = frame.groupby("week").agg(arrivals=("price", "size"), median_ask=("price", "median"),
                                                 entry_price_count=("price", "count"))
        index = pd.date_range(grouped_frame.index.min(), pd.Timestamp(current_week)-pd.Timedelta(weeks=1),
                              freq="W-MON")
        grouped_frame = grouped_frame.reindex(index)
        grouped_frame["arrivals"] = grouped_frame["arrivals"].fillna(0).astype(int)
        grouped_frame["entry_price_count"] = grouped_frame["entry_price_count"].fillna(0).astype(int)
        grouped_frame["week"] = grouped_frame.index.strftime("%Y-%m-%d")
        frames[name] = grouped_frame.reset_index(drop=True)
    return frames


def _weekly_series(category_norm=None, region_norm=None, weeks=104, now=None, initial_prices=None):
    positive_int(weeks, "weeks")
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(HELSINKI)
    current_week = local.date()-timedelta(days=local.weekday())
    start = current_week-timedelta(weeks=weeks)
    initial_prices = entry_prices(now) if initial_prices is None else initial_prices
    rows = _listing_rows(region_norm, [category_norm] if category_norm is not None else None)
    frames = _arrival_frames(rows, start, current_week, now, initial_prices, grouped=False)
    return frames.get("", pd.DataFrame())


def _window(n):
    return min(n, max(DIRECTION_MIN_WEEKS, min(DIRECTION_MAX_WEEKS, n//4)))


def _direction(values):
    """Median pairwise slope plus the share of slopes that share its sign.

    The agreement share is descriptive, not a calibrated probability: it says
    how consistently the window moves in one direction, which is the reason a
    direction is reported or withheld. Half means no consistent direction, and
    a perfectly flat window reports exactly half because its slopes are zero.
    """
    y = np.asarray(values, dtype=float)
    if len(y) < 3:
        return None, None
    y = y[-_window(len(y)):]
    if not np.all(np.isfinite(y)):
        return None, None
    i, j = np.triu_indices(len(y), k=1)
    slopes = (y[j]-y[i])/(j-i)
    if not len(slopes):
        return None, None
    slope = float(np.median(slopes))
    if not math.isfinite(slope):
        return None, None
    # Ties are split rather than dropped: a perfectly flat window has no slopes
    # of either sign, and reporting agreement of zero there would read as
    # evidence against a direction instead of as no evidence at all.
    up, down = float(np.mean(slopes > 0)), float(np.mean(slopes < 0))
    return slope, max(up, down) + (1-up-down)/2


def _theil_sen(values):
    y = np.asarray(values, dtype=float)
    if len(y) < 3 or not np.all(np.isfinite(y)):
        return 0.0, 0.0
    i, j = np.triu_indices(len(y), k=1)
    slopes = (y[j]-y[i])/(j-i)
    slope = float(np.median(slopes))
    spread = float(np.std(slopes, ddof=1)) if len(slopes) > 1 else 0.0
    return (slope, spread) if math.isfinite(slope) else (0.0, 0.0)


def _rolling_median(values, window):
    """Centred rolling median.

    A mean is moved by one catch-up collection week or one holiday week; a
    median is not. This is the whole reason a series shorter than two annual
    cycles has a usable trend at all, where the raw counts did not.
    """
    y = np.asarray(values, dtype=float)
    if len(y) < 3 or window < 3:
        return y.copy()
    half = window//2
    # Reflect rather than repeat the endpoint: repeating it fills the boundary
    # window with copies of the very outlier the median exists to suppress.
    padded = np.pad(y, (half, half), mode="reflect")
    return np.array([np.median(padded[i:i+window]) for i in range(len(y))], dtype=float)


def _classify_trend(values, changepoints, has_enough_evidence=True):
    y = np.asarray(values, dtype=float)
    if len(y) < PELT_MIN_WEEKS or not has_enough_evidence:
        return "emerging"
    recent = y[-_window(len(y)):]
    if not np.all(np.isfinite(recent)) or np.any(recent < 0):
        return "emerging"
    if not np.any(recent > 0):
        return "declining" if np.any(y > 0) else "emerging"
    if np.count_nonzero(recent) < 4:
        return "emerging"
    # Median pairwise slopes prevent one catch-up collection week from
    # establishing a direction.
    slope, _ = _direction(y)
    if slope is None:
        return "emerging"
    level = max(1.0, float(np.median(recent)))
    residual = recent-slope*np.arange(len(recent))
    # Compare dispersion against what Poisson sampling alone produces at this
    # level, so steady growth in a thin category is not called erratic.
    floor = max(VOLATILITY_RELATIVE*level, VOLATILITY_SAMPLING_MULTIPLE*math.sqrt(level))
    if float(np.std(residual)) > floor:
        return "volatile"
    relative = slope/level
    if relative > DIRECTION_SLOPE:
        return "growing"
    if relative < -DIRECTION_SLOPE:
        return "declining"
    if any(len(y)-5 <= c < len(y) for c in changepoints):
        return "volatile"
    return "mature"


def _decompose_uncached(arrivals):
    n = len(arrivals)
    season = None
    signal = np.log1p(arrivals)
    version = EARLY_VERSION
    if n >= STL_MIN_WEEKS:
        from statsmodels.tsa.seasonal import STL
        result = STL(signal, period=SEASON_PERIOD, robust=True).fit()
        # Positive multiplicative index: 1 is the estimated average seasonal level.
        seasonal_factor = np.exp(result.seasonal)
        season = seasonal_factor/np.mean(seasonal_factor)
        signal = signal-result.seasonal
        trend = np.maximum(0, np.expm1(result.trend))
        version = MODEL_VERSION
    else:
        # Below two annual cycles the STL level is unavailable, so the trend is
        # a rolling median of the counts rather than the counts themselves.
        trend = _rolling_median(arrivals, max(1, min(9, n if n % 2 else n-1)))
    breaks = []
    score = None
    if n >= PELT_MIN_WEEKS:
        import ruptures
        if version == EARLY_VERSION:
            version = PELT_VERSION
        diffs = np.diff(signal)
        scale = max(.1, float(np.median(np.abs(diffs-np.median(diffs))))/.6745/math.sqrt(2))
        normalized = (signal-np.median(signal))/scale
        candidates = ruptures.Pelt(model="l2", min_size=4, jump=1).fit(normalized.reshape(-1, 1)).predict(pen=3*math.log(n))
        # ruptures always returns n, the terminal segment boundary, not a break.
        breaks = [int(c) for c in candidates if 0 < c < n]
        score = 0.0
        if breaks:
            last = breaks[-1]
            left = signal[max(0, last-8):last]
            right = signal[last:]
            strength = abs(float(np.mean(right)-np.mean(left)))/scale
            score = float(math.exp(-(n-1-last)/4)*strength/(1+strength))
    return trend, season, breaks, score, version


@lru_cache(maxsize=2048)
def _decompose_cached(payload, count):
    return _decompose_uncached(np.frombuffer(payload, dtype=float, count=count))


def _decompose(arrivals):
    # Each emitted prefix refits from scratch, so the same series is decomposed
    # repeatedly across categories, months_back settings and validation runs.
    # The decomposition is a pure function of the counts, so memoise it on the
    # exact bytes rather than on a rounded or truncated copy.
    y = np.ascontiguousarray(arrivals, dtype=float).ravel()
    trend, season, breaks, score, version = _decompose_cached(y.tobytes(), len(y))
    # Hand out copies: the cache outlives this call and a caller that edits a
    # returned array would silently corrupt every later decomposition.
    return (trend.copy(), None if season is None else season.copy(), list(breaks), score, version)


def _dispersion(arrivals):
    """Poisson-Gamma dispersion phi, estimated from the most recent year."""
    recent = arrivals[-SEASON_PERIOD:]
    level = float(np.mean(recent))
    if level <= 0:
        return 1.0
    variance = float(np.var(recent, ddof=1)) if len(recent) > 1 else level
    return min(FORECAST_MAX_DISPERSION, max(1.0, variance/level))


def _interval(mean, dispersion, level=FORECAST_INTERVAL):
    """Quantiles of the Poisson-Gamma predictive distribution, or None.

    phi is 1 for an exact Poisson and larger once the cohort shows
    overdispersion. scipy is optional for this package, and a missing interval
    is reported as absent rather than replaced by a normal approximation that
    would put a fractional lower bound on a count.
    """
    if not math.isfinite(mean) or not math.isfinite(dispersion) or dispersion < 1:
        return None
    try:
        from scipy.stats import nbinom, poisson
    except ImportError:
        return None
    tail = (1-level)/2
    try:
        if dispersion <= 1+1e-9:
            bounds = poisson.ppf([tail, 1-tail], max(0.0, mean))
        else:
            bounds = nbinom.ppf([tail, 1-tail], mean/(dispersion-1), 1/dispersion)
    except Exception:
        return None
    bounds = [float(v) for v in np.atleast_1d(bounds)]
    if len(bounds) != 2 or not all(math.isfinite(v) for v in bounds) or bounds[0] > bounds[1]:
        return None
    return (max(0.0, bounds[0]), max(0.0, bounds[1]))


def _forecast_arrivals(arrivals, trend, season, origin_week):
    """Projected arrivals for each supported lead time from one cohort.

    Withheld entirely when the completed weeks or the arrival volume cannot
    support a projection at all, and narrowed to lead times the cohort is long
    enough to justify.
    """
    n = len(arrivals)
    if n < FORECAST_MIN_WEEKS or float(np.sum(arrivals)) < FORECAST_MIN_ARRIVALS:
        return []
    level = max(0.0, float(trend[-1]))
    slope, spread = _theil_sen(trend)
    phi = _dispersion(arrivals)
    # Pairwise slopes share observations, so their spread overstates the
    # sampling error of one slope estimate. Discount by the window length
    # instead of treating thousands of dependent slopes as independent.
    slope_error = spread/math.sqrt(max(1.0, len(trend)/4))
    rows = []
    for horizon in FORECAST_HORIZONS:
        if horizon > max(1, n//FORECAST_WEEKS_PER_HORIZON):
            break
        expected = level+slope*horizon
        factor = 1.0
        if season is not None:
            factor = float(season[(n-1+horizon) % SEASON_PERIOD])
        expected = max(0.0, expected*factor)
        # Total predictive variance is count noise around the projected mean
        # plus the spread the slope itself contributes, which is what makes
        # the interval widen as the lead time grows. The seasonal factor scales
        # the slope's contribution for the same reason it scales the level.
        total = phi*expected+(horizon*slope_error*factor)**2
        spread_phi = max(1.0, total/expected) if expected > 0 else 1.0
        bounds = _interval(expected, min(FORECAST_MAX_DISPERSION, spread_phi))
        lo, hi = bounds if bounds else (None, None)
        rows.append({"origin_week_start": origin_week,
                     "horizon_weeks": horizon,
                     "target_week_start": (datetime.strptime(origin_week, "%Y-%m-%d").date()
                                           + timedelta(weeks=horizon)).isoformat(),
                     "expected_arrivals": round(expected, 3),
                     "arrivals_lo": None if lo is None else round(lo, 3),
                     "arrivals_hi": None if hi is None else round(hi, 3),
                     "unavailability": {} if bounds else {"predictive_interval": {
                         "reason":f"Predictive interval needs SciPy count quantiles and finite parameters; "
                                  f"currently unavailable with {n} completed weeks",
                         "observed_completed_weeks":n}},
                     "interval_level": FORECAST_INTERVAL,
                     "overdispersion": round(min(FORECAST_MAX_DISPERSION, spread_phi), 6),
                     "method": "seasonal-negative-binomial" if season is not None else "negative-binomial",
                     "evidence_weeks": n})
    return rows


def _chronological_evaluation(arrivals, weeks):
    """Refit only completed prefixes, scoring recent completed target weeks.

    Each horizon is tested against both the last-four-week mean and, when
    available, the same week a year earlier. Future counts never enter a fit,
    its interval, or either baseline. All horizons use the same target window.
    """
    y = np.asarray(arrivals, dtype=float)
    method = "seasonal-negative-binomial" if len(y) >= STL_MIN_WEEKS else "negative-binomial"
    split = max(FORECAST_MIN_WEEKS, len(y)-max(FORECAST_MIN_HOLDOUT_WEEKS,
                                            math.ceil(len(y)*.2)))
    scores, forecasts_by_origin = {}, {}
    for horizon in FORECAST_HORIZONS:
        errors, baseline_errors, recent_errors, covered, origins = [], [], [], [], []
        seasonal_tests = 0
        for target in range(split, len(y)):
            origin = target-horizon
            if origin < FORECAST_MIN_WEEKS-1:
                continue
            prefix = y[:origin+1]
            if origin not in forecasts_by_origin:
                trend, season, _breaks, _score, _version = _decompose(prefix)
                forecasts_by_origin[origin] = {r["horizon_weeks"]: r for r in
                    _forecast_arrivals(prefix, trend, season, weeks[origin])}
            forecast = forecasts_by_origin[origin].get(horizon)
            if forecast is None or forecast["method"] != method:
                continue
            recent = float(np.mean(prefix[-4:]))
            seasonal = target-SEASON_PERIOD <= origin and target-SEASON_PERIOD >= 0
            baseline = float(y[target-SEASON_PERIOD]) if seasonal else recent
            seasonal_tests += int(seasonal)
            errors.append(abs(float(y[target])-forecast["expected_arrivals"]))
            baseline_errors.append(abs(float(y[target])-baseline))
            recent_errors.append(abs(float(y[target])-recent))
            if forecast["arrivals_lo"] is not None and forecast["arrivals_hi"] is not None:
                covered.append(forecast["arrivals_lo"] <= y[target] <= forecast["arrivals_hi"])
            origins.append(weeks[origin])
        enough = len(errors) >= FORECAST_MIN_HOLDOUT_WEEKS
        coverage = float(np.mean(covered)) if covered else None
        scores[str(horizon)] = {
            "available": enough, "n_test": len(errors), "horizon_weeks": horizon, "method": method,
            "horizon_supported": enough,
            "mae_arrivals": float(np.mean(errors)) if errors else None,
            "baseline_mae_arrivals": float(np.mean(baseline_errors)) if errors else None,
            "last_four_week_mae_arrivals": float(np.mean(recent_errors)) if errors else None,
            "baseline": "seasonal-naive / last-4-week-mean" if seasonal_tests else "last-4-week-mean",
            "seasonal_baseline_weeks": seasonal_tests,
            "improves_baseline": bool(enough and np.mean(errors) < np.mean(baseline_errors)
                                      and np.mean(errors) < np.mean(recent_errors)),
            "coverage_90": coverage,
            "calibration_ok": bool(len(covered) == len(errors) and enough and .8 <= coverage <= 1),
            "first_origin_week": origins[0] if origins else None,
            "last_origin_week": origins[-1] if origins else None,
            "reason": None if enough else
                f"Needs {FORECAST_MIN_HOLDOUT_WEEKS} held-out completed weeks at {horizon} weeks under {method}; currently {len(errors)}",
        }
    return {"available": any(s["available"] for s in scores.values()), "by_horizon": scores,
            "evaluation": "chronological rolling origins; completed weeks only",
            "test_start_week": weeks[split] if split < len(y) else None,
            "test_end_week": weeks[-1] if split < len(y) else None,
            "evidence_weeks": len(y), "method": method}


def _direction_evaluation(arrivals, weeks):
    """Compare fixed direction rules with disjoint following eight-week blocks.

    Training prefixes never include their outcome blocks. Agreement describes
    the collector's arrivals; it neither calibrates state_confidence nor tunes
    the inherited thresholds. A flat-direction baseline is retained for review.
    """
    y = np.asarray(arrivals, dtype=float)
    confusion, windows = defaultdict(lambda: defaultdict(int)), []
    thin = 0
    for origin in range(DIRECTION_MIN_WEEKS-1, len(y)-DIRECTION_EVALUATION_WEEKS,
                        DIRECTION_EVALUATION_WEEKS):
        prefix, future = y[:origin+1], y[origin+1:origin+1+DIRECTION_EVALUATION_WEEKS]
        if (float(np.sum(prefix[-DIRECTION_MIN_WEEKS:])) < DIRECTION_MIN_EVALUATION_ARRIVALS
                or float(np.sum(future)) < DIRECTION_MIN_EVALUATION_ARRIVALS):
            thin += 1
            continue
        trend, _season, breaks, _score, _version = _decompose(prefix)
        predicted = _classify_trend(trend, breaks)
        # The target is the robust slope of the actually observed future
        # counts, without smoothing them or fitting a future seasonal model.
        slope, _spread = _theil_sen(future)
        relative = slope/max(1., float(np.median(future)))
        observed = ("growing" if relative > DIRECTION_SLOPE else
                    "declining" if relative < -DIRECTION_SLOPE else "mature")
        confusion[predicted][observed] += 1
        windows.append({"origin_week_start":weeks[origin], "target_start_week":weeks[origin+1],
                        "target_end_week":weeks[origin+DIRECTION_EVALUATION_WEEKS],
                        "predicted":predicted, "observed":observed, "observed_relative_slope":float(relative)})
    n = len(windows)
    enough = n >= DIRECTION_MIN_EVALUATION_WINDOWS
    reason = None if enough else (
        f"Direction validation needs {DIRECTION_MIN_EVALUATION_WINDOWS} completed non-overlapping "
        f"{DIRECTION_EVALUATION_WEEKS}-week windows with at least {DIRECTION_MIN_EVALUATION_ARRIVALS} "
        f"arrivals in training and outcome blocks, currently {n}")
    return {"available":enough, "reason":reason, "n_test_windows":n, "policy_version":"direction-8w-v1",
            "horizon_weeks":DIRECTION_EVALUATION_WEEKS, "non_overlapping_targets":True,
            "confusion":{state:dict(counts) for state, counts in confusion.items()},
            "agreement_rate":sum(w["predicted"] == w["observed"] for w in windows)/n if enough else None,
            "flat_baseline_agreement_rate":sum(w["observed"] == "mature" for w in windows)/n if enough else None,
            "n_thin_windows":thin, "windows":windows, "thresholds_tuned":False,
            "thresholds":{"relative_slope":DIRECTION_SLOPE, "minimum_direction_weeks":DIRECTION_MIN_WEEKS,
                          "minimum_test_windows":DIRECTION_MIN_EVALUATION_WINDOWS,
                          "minimum_arrivals_per_block":DIRECTION_MIN_EVALUATION_ARRIVALS},
            "evaluation":"chronological direction agreement; collector arrivals, not calibrated probabilities"}


def compute_category_trends(min_weeks=MIN_WEEKS, max_categories=MAX_CATEGORIES, months_back=None,
                            region_norm=None, now=None):
    return _compute(min_weeks, max_categories, months_back, region_norm, now, include_forecasts=False)[0]


def compute_trend_forecasts(min_weeks=MIN_WEEKS, max_categories=MAX_CATEGORIES, months_back=None,
                            region_norm=None, now=None, experimental=False):
    """Validated arrival projections; candidates require explicit opt-in."""
    return _compute(min_weeks, max_categories, months_back, region_norm, now,
                    experimental=experimental)[1]


def _compute(min_weeks, max_categories, months_back, region_norm, now, experimental=False,
             include_forecasts=True):
    positive_int(min_weeks, "min_weeks")
    positive_int(max_categories, "max_categories")
    if months_back is not None:
        positive_int(months_back, "months_back")
    # Descriptive trends need two annual cycles; forecasting also needs later
    # completed targets on which that same seasonal method can be evaluated.
    weeks = ((FORECAST_DEFAULT_WEEKS if include_forecasts else STL_MIN_WEEKS) if months_back is None
             else max(1, math.ceil(months_back*365.25/12/7)))
    now = now or datetime.now(timezone.utc)
    results, projections = [], []
    with db.read_snapshot():
        initial_prices = entry_prices(now)
        conditions = ["category_norm IS NOT NULL", "category_norm!=''",
                      "COALESCE(is_giveaway,FALSE)=FALSE"]
        params = []
        if region_norm is not None:
            conditions.append("region_norm=?")
            params.append(region_norm)
        # Rank within the requested cohort. A busy national category must not
        # consume the cap when it has no observations in this region.
        categories = db.query("SELECT category_norm,COUNT(*) AS n FROM listings WHERE "
                              + " AND ".join(conditions) +
                              " GROUP BY category_norm ORDER BY n DESC,category_norm LIMIT ?",
                              params+[max_categories])
        names = [c["category_norm"] for c in categories]
        if not names:
            return results, projections
        local = now.astimezone(HELSINKI)
        current_week = local.date()-timedelta(days=local.weekday())
        frames = _arrival_frames(_listing_rows(region_norm, names), current_week-timedelta(weeks=weeks),
                                 current_week, now, initial_prices)
    for name in names:
        series = frames.get(name)
        if series is None or series.empty or len(series) < min_weeks:
            continue
        # Refit each emitted prefix so historical rows never use future weeks.
        for idx in range(max(min_weeks-1, len(series)-26), len(series)):
            arrivals = series.arrivals.iloc[:idx+1].to_numpy(float)
            trend, season, breaks, score, version = _decompose(arrivals)
            price = series.iloc[idx].median_ask
            slope, confidence = _direction(trend)
            state = _classify_trend(trend, breaks, len(arrivals) >= PELT_MIN_WEEKS)
            row = {"category_norm": name, "region_norm": region_norm,
                            "week_start": series.iloc[idx].week, "arrivals": int(arrivals[-1]),
                            "median_ask_eur": None if pd.isna(price) else round(float(price), 2),
                            "trend_state": state,
                            "changepoint_prob": None if score is None else round(score, 4),
                            "seasonality_idx": None if season is None else round(float(season[-1]), 4),
                            "trend_slope": None if slope is None else round(slope, 6),
                            # Volatility makes no directional claim, so a
                            # confidence figure for one would be meaningless.
                            "state_confidence": None if (state == "volatile" or confidence is None)
                            else round(confidence, 4),
                            "evidence_weeks": len(arrivals), "model_version": version,
                            "computed_at": now.isoformat(),
                            "entry_price_count":int(series.iloc[idx].entry_price_count)}
            row["unavailability"] = trend_unavailability(row)
            row["direction_evaluation"] = _direction_evaluation(arrivals, series.week.iloc[:idx+1].tolist())
            results.append(row)
        if not include_forecasts:
            continue
        arrivals = series.arrivals.to_numpy(float)
        trend, season, _breaks, _score, version = _decompose(arrivals)
        candidates = _forecast_arrivals(arrivals, trend, season, series.iloc[-1].week)
        if not candidates:
            continue
        evaluation = _chronological_evaluation(arrivals, series.week.tolist())
        acceptance = guidance_acceptance(evaluation, horizons=FORECAST_HORIZONS)
        for row in candidates:
            accepted = row["horizon_weeks"] in acceptance["supported_horizons"]
            if not experimental and not accepted:
                continue
            projections.append(dict(row, category_norm=name, region_norm=region_norm,
                                    is_accepted=accepted, evaluation=evaluation, acceptance=acceptance,
                                    model_version=FORECAST_VERSION, computed_at=now.isoformat()))
    return results, projections


def persist_trends(rows):
    cols = ("category_norm", "region_norm", "week_start", "arrivals", "median_ask_eur", "trend_state",
            "changepoint_prob", "seasonality_idx", "trend_slope", "state_confidence",
            "evidence_weeks", "model_version", "computed_at", "evidence_json")
    conn = db.connect()
    try:
        with conn.transaction():
            for row in rows:
                r = dict(row, region_norm=row.get("region_norm") or "")
                r["evidence_json"] = json.dumps({"unavailability":trend_unavailability(r),
                    "entry_price_count":r.get("entry_price_count"),
                    "direction_evaluation":r.get("direction_evaluation", {})}, allow_nan=False)
                conn.execute("INSERT INTO category_trends ("+",".join(cols)+") VALUES ("
                             + ",".join("?" for _ in cols)+") ON CONFLICT "
                             "(category_norm,region_norm,week_start,model_version) DO UPDATE SET "
                             + ",".join(f"{c}=excluded.{c}" for c in cols[3:] if c != "model_version"),
                             [r.get(c) for c in cols])
    finally:
        conn.close()
    return len(rows)


def persist_forecasts(rows):
    cols = ("category_norm", "region_norm", "origin_week_start", "horizon_weeks", "target_week_start",
            "expected_arrivals", "arrivals_lo", "arrivals_hi", "interval_level", "overdispersion",
            "method", "evidence_weeks", "model_version", "computed_at", "is_accepted", "validation_json")
    conn = db.connect()
    try:
        with conn.transaction():
            if db.dialect() == "postgres":
                # Serialize versioned-origin replacement across independent CLI
                # writers; otherwise an older insert could commit after cleanup.
                conn.execute("LOCK TABLE category_trend_forecasts IN SHARE ROW EXCLUSIVE MODE")
            for row in rows:
                r = dict(row, region_norm=row.get("region_norm") or "")
                if r.get("expected_arrivals") is None or r.get("interval_level") is None:
                    raise ValueError("expected arrivals and interval_level are required")
                expected, level = r["expected_arrivals"], r["interval_level"]
                lo, hi = r.get("arrivals_lo"), r.get("arrivals_hi")
                if not math.isfinite(expected) or expected < 0:
                    raise ValueError("expected arrivals must be finite and nonnegative")
                if not 0 <= level < 1:
                    raise ValueError("interval_level must be between zero and one")
                if (lo is None) != (hi is None):
                    raise ValueError("a predictive interval needs both bounds or neither")
                if lo is not None and (not math.isfinite(lo) or not math.isfinite(hi) or not 0 <= lo <= hi):
                    raise ValueError("predictive interval must be finite, nonnegative and ordered")
                positive_int(r.get("horizon_weeks"), "horizon_weeks")
                evaluation = r.get("evaluation", {})
                acceptance = guidance_acceptance(evaluation, horizons=FORECAST_HORIZONS)
                accepted = (r.get("model_version") == FORECAST_VERSION and
                            r["horizon_weeks"] in acceptance["supported_horizons"])
                if r.get("is_accepted") and not accepted:
                    raise ValueError("accepted arrival forecasts require chronological validation")
                r["is_accepted"] = accepted
                r["validation_json"] = json.dumps({"evaluation": evaluation, "acceptance": acceptance,
                                                    "unavailability":r.get("unavailability", {})},
                                                  allow_nan=False)
                conn.execute("INSERT INTO category_trend_forecasts ("+",".join(cols)+") VALUES ("
                             + ",".join("?" for _ in cols)+") ON CONFLICT "
                             "(category_norm,region_norm,origin_week_start,horizon_weeks,model_version) "
                             "DO UPDATE SET "+",".join(f"{c}=excluded.{c}" for c in cols[5:]),
                             [r.get(c) for c in cols])
            # Keep all horizons at the newest origin in each versioned cohort.
            # Prune after the whole batch validates so a bad row rolls back both
            # writes and retention. An older recomputation cannot replace newer evidence.
            conn.execute("DELETE FROM category_trend_forecasts WHERE EXISTS ("
                         "SELECT 1 FROM category_trend_forecasts newer WHERE "
                         "newer.category_norm=category_trend_forecasts.category_norm "
                         "AND COALESCE(newer.region_norm,'')=COALESCE(category_trend_forecasts.region_norm,'') "
                         "AND newer.model_version=category_trend_forecasts.model_version "
                         "AND newer.origin_week_start>category_trend_forecasts.origin_week_start)")
    finally:
        conn.close()
    return len(rows)


def summary(rows=None):
    if rows is None:
        rows = db.query("SELECT * FROM category_trends WHERE model_version IN (?,?,?) "
                        "ORDER BY week_start DESC,computed_at DESC,model_version DESC",
                        (MODEL_VERSION, PELT_VERSION, EARLY_VERSION))
        by_version = True
    else:
        rows = sorted(rows, key=lambda r: (r["week_start"], r["computed_at"]), reverse=True)
        by_version = False
    seen = set()
    out = {}
    for r in rows:
        key = (r["category_norm"], r.get("region_norm") or "")
        if key in seen:
            continue
        seen.add(key)
        states = out.setdefault(r["model_version"], {}) if by_version else out
        states[r["trend_state"]] = states.get(r["trend_state"], 0)+1
    return out
