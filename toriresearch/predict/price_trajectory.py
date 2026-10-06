"""Hierarchical Bayesian asking-price paths, with Gaussian log-price increments.

Daily drifts partially pool through global -> category -> category/condition ->
listing levels. Variances have inverse-gamma priors and use collapsed slice updates.
Gaussian effects are drawn jointly through the hierarchy. Posterior predictive bands include drift uncertainty and future price
noise. They concern asking prices conditional on a listing remaining available.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .. import config, db, metrics
from ..analysis import is_priced
from ._common import (HORIZONS, accepted_segment, guidance_acceptance, validated_segment_support, finite_price,
                      horizons_checked, load_bundle, positive_int, save_bundle, version_id, entry_features,
                      forecast_sources, forecast_validation, LISTING_METHODS)

MODEL_VERSION = LISTING_METHODS["price_trajectory"]
MIN_TRAIN_ROWS = 30
MIN_TRAJECTORIES = 10
MIN_INTERVAL_DAYS = 1.0


def _observed_trajectories(now=None, listing_pks=None):
    now = now or datetime.now(timezone.utc)
    if listing_pks == []:
        return pd.DataFrame()
    params = list(listing_pks or [])
    selected = " AND l.listing_pk IN ("+",".join("?" for _ in params)+")" if params else ""
    rows = db.query(
        "SELECT ph.listing_pk, ph.valid_from, ph.observed_at, ph.listed_price_eur, "
        "l.first_seen_at, l.category_root_norm, l.condition_norm FROM "
        "(SELECT listing_pk,COALESCE(price_observed_at,valid_from) AS valid_from,observed_at,listed_price_eur FROM price_history UNION ALL "
        "SELECT listing_pk,price_observed_at AS valid_from,observed_at,listed_price_eur FROM price_observations) ph "
        "JOIN listings l ON l.listing_pk=ph.listing_pk "
        "WHERE COALESCE(l.is_giveaway,FALSE)=FALSE"+selected+
        " ORDER BY ph.listing_pk, ph.valid_from", params)
    paths = {}
    snapshots = entry_features(now)
    for r in rows:
        first, stamp = metrics.parse_ts(r["first_seen_at"]), metrics.parse_ts(r["valid_from"])
        observed = metrics.parse_ts(r["observed_at"])
        price = finite_price(r["listed_price_eur"])
        if not first or not stamp or not observed or stamp < first or stamp > now or observed > now or not price:
            continue
        path = paths.setdefault(r["listing_pk"], {"row": r, "points": {}})
        # Repeated exports of a measurement cannot backdate when its value
        # became known. Preserve the earliest recorded measurement per clock.
        point = path["points"].get(stamp)
        if point is None or observed < point[1]:
            path["points"][stamp] = (price, observed)
    records, subday_observations = [], 0
    for pk, path in paths.items():
        r = path["row"]
        points = sorted(path["points"].items())
        # Only two measured endpoints establish an interval. last_seen_at is a
        # sighting clock and cannot establish price stability.
        snapshot = snapshots.get(pk, {})
        category = snapshot.get("category_root_norm") or "unknown"
        condition = snapshot.get("condition_norm") or "unknown"
        start, (p0, seen0) = points[0]
        for stop, (p1, seen1) in points[1:]:
            dt = (stop-start).total_seconds()/86400
            if dt < MIN_INTERVAL_DAYS:
                subday_observations += 1
                continue
            records.append({"listing_pk": pk, "category": category, "condition": condition,
                            "start_at": start, "end_at": stop, "first_seen_at": metrics.parse_ts(r["first_seen_at"]),
                            "start_observed_at":seen0, "available_at":max(seen0,seen1),
                            "duration": dt, "log_change": math.log(p1/p0),
                            "initial_price": p0, "final_price": p1})
            start, p0, seen0 = stop, p1, seen1
    frame = pd.DataFrame(records)
    frame.attrs["price_readiness"] = {"n_price_measured_listings": len(paths),
                                    "n_subday_observations_excluded": subday_observations}
    return frame


def _trajectory_readiness(frame):
    n, listings = len(frame), int(frame.listing_pk.nunique()) if not frame.empty else 0
    observed = frame.attrs.get("price_readiness", {})
    measured = observed.get("n_price_measured_listings", listings)
    per_listing = n/listings if listings else None
    minimum = float(frame.duration.min()) if not frame.empty else None
    minimum = minimum if minimum is not None and math.isfinite(minimum) else None
    ready = bool(n >= MIN_TRAIN_ROWS and listings >= MIN_TRAJECTORIES
                 and minimum is not None and minimum >= MIN_INTERVAL_DAYS
                 and np.all(np.isfinite(frame.duration.to_numpy(float))))
    ratio = f"{per_listing:.2f}" if per_listing is not None else "unavailable (no eligible listings)"
    prefix = "Insufficient trajectory data" if n else "No observed price intervals meet the separation floor"
    reason = None if ready else (
        f"{prefix}: {n} eligible intervals across {listings} eligible listings; "
        f"intervals per listing: {ratio}. Need {MIN_TRAIN_ROWS} intervals across {MIN_TRAJECTORIES} listings "
        f"with measured endpoints at least {MIN_INTERVAL_DAYS:g} day apart. "
        f"{measured} listings have measured prices; "
        f"{observed.get('n_subday_observations_excluded', 0)} sub-day observations excluded")
    return dict(observed, ready=ready, reason=reason, n_intervals=n, n_eligible_listings=listings,
                n_price_measured_listings=measured, intervals_per_listing=per_listing,
                maximum_observed_path_days=float(frame.groupby("listing_pk").duration.sum().max()) if listings else None,
                intervals_per_measured_listing=n/measured if measured else None,
                minimum_interval_days=MIN_INTERVAL_DAYS, minimum_observed_interval_days=minimum,
                required_intervals=MIN_TRAIN_ROWS, required_listings=MIN_TRAJECTORIES)


def price_trajectory_readiness(now=None):
    """Read-only evidence shape check, without sampling or registering a fit."""
    with db.read_snapshot():
        return _trajectory_readiness(_observed_trajectories(now))


def _rhat_ess(samples):
    """Split-chain R-hat and an initial-positive-sequence ESS for monitored parameters."""
    half = samples.shape[1]//2
    split = np.concatenate([samples[:, :half], samples[:, -half:]], axis=0)
    m, n, _ = split.shape
    within = split.var(axis=1, ddof=1).mean(axis=0)
    between = n*split.mean(axis=1).var(axis=0, ddof=1)
    var_hat = (n-1)/n*within + between/n
    rhat = np.sqrt(var_hat/np.maximum(within, 1e-30))
    ess = []
    for k in range(split.shape[2]):
        centered = split[:,:,k]-split[:,:,k].mean(axis=1, keepdims=True)
        autocov = np.mean([np.correlate(c,c,mode="full")[n-1:]/np.arange(n,0,-1) for c in centered], axis=0)
        rho = 1-(within[k]-autocov)/max(var_hat[k],1e-30)
        total = 0.0
        for lag in range(1,n-1,2):
            pair = rho[lag]+rho[lag+1]
            if pair <= 0:
                break
            total += pair
        ess.append(min(m*n, m*n/max(1.0,1+2*total)))
    return {"max_split_rhat": float(np.max(rhat)), "min_effective_samples": float(min(ess)),
            "chains": samples.shape[0], "draws_per_chain": samples.shape[1],
            "diagnostics_ok": bool(np.max(rhat) < 1.1 and min(ess) >= 100)}


def _sample_posterior(df, draws=400, warmup=400, chains=4, seed=17):
    positive_int(draws, "draws")
    positive_int(warmup, "warmup")
    positive_int(chains, "chains")
    if draws < 40 or chains < 2:
        raise ValueError("Use at least 40 draws and two chains for posterior diagnostics")
    readiness = _trajectory_readiness(df)
    if not readiness["ready"]:
        raise ValueError(readiness["reason"])
    stats = df.groupby("listing_pk", sort=True).agg(
        duration=("duration","sum"), log_change=("log_change","sum"),
        category=("category","first"), condition=("condition","first"))
    roots = sorted(stats.category.unique())
    groups = sorted(set(zip(stats.category, stats.condition)))
    root_index = {c:i for i,c in enumerate(roots)}
    group_index = {g:i for i,g in enumerate(groups)}
    group_root = np.array([root_index[g[0]] for g in groups])
    listing_group = np.array([group_index[g] for g in zip(stats.category, stats.condition)])
    exposure, change = stats.duration.to_numpy(), stats.log_change.to_numpy()
    dt, dy = df.duration.to_numpy(), df.log_change.to_numpy()
    ng, nr, nl = len(groups), len(roots), len(stats)
    observed_slopes = change/exposure
    within_ss = max(0.0,float(np.sum(dy**2/dt)-np.sum(change**2/exposure)))
    prior_scales = np.array([.0008,.00002,.00002,.00005])

    def summaries(variances):
        sigma2,tau_l2,tau_g2,tau_r2 = variances
        weights = 1/(tau_l2+sigma2/exposure)
        wg = np.bincount(listing_group,weights=weights,minlength=ng)
        bg = np.bincount(listing_group,weights=weights*observed_slopes,minlength=ng)
        mg = bg/wg
        zg = wg/(1+tau_g2*wg)
        wr = np.bincount(group_root,weights=zg,minlength=nr)
        br = np.bincount(group_root,weights=zg*mg,minlength=nr)
        mr = br/wr
        zr = wr/(1+tau_r2*wr)
        return weights,wg,bg,mg,zg,wr,br,mr,zr

    def log_posterior(log_variances):
        if np.any(log_variances < -50) or np.any(log_variances > 5):
            return -np.inf
        v = np.exp(log_variances)
        sigma2,tau_l2,tau_g2,tau_r2 = v
        w,wg,bg,mg,zg,wr,br,mr,zr = summaries(v)
        # Integrate all listing, condition, category and global Gaussian effects.
        # The residual factor accounts for multiple intervals on one listing.
        group_ss = max(0.0,float(np.sum(w*observed_slopes**2)-np.sum(bg**2/wg)))
        root_ss = max(0.0,float(np.sum(zg*mg**2)-np.sum(br**2/wr)))
        global_ss = float(np.sum(zr*mr**2)-.02**2*np.sum(zr*mr)**2/(1+.02**2*np.sum(zr)))
        log_like = -.5*((len(df)-nl)*log_variances[0]+within_ss/sigma2
                        -np.log(w).sum()+group_ss+np.log1p(tau_g2*wg).sum()
                        +root_ss+np.log1p(tau_r2*wr).sum()+global_ss
                        +np.log1p(.02**2*np.sum(zr)))
        # IG shape 3, plus the Jacobian from variance to log variance.
        return float(log_like-3*log_variances.sum()-np.sum(prior_scales/v))

    def slice_step(state,coordinate,rng):
        current = state[coordinate]
        level = log_posterior(state)+math.log(rng.uniform())
        left = current-rng.uniform()
        right = left+1.0
        trial = state.copy()
        # Randomly split a finite stepping-out budget (Neal's univariate sampler).
        j = int(rng.integers(0,20))
        k = 19-j
        while j>0:
            trial[coordinate]=left
            if log_posterior(trial)<=level:
                break
            left-=1.0
            j-=1
        while k>0:
            trial[coordinate]=right
            if log_posterior(trial)<=level:
                break
            right+=1.0
            k-=1
        for _ in range(200):
            proposed = rng.uniform(left,right)
            trial[coordinate]=proposed
            if log_posterior(trial)>=level:
                return trial
            if proposed<current:
                left=proposed
            else:
                right=proposed
        raise ValueError("Bayesian variance sampler failed to find a finite slice")

    chains_out = []
    for chain in range(chains):
        rng = np.random.default_rng(seed+chain)
        state = np.log([.0004,.00001,.00001,.000025])+rng.normal(0,.4,4)
        kept = []
        for iteration in range(warmup+draws):
            for coordinate in rng.permutation(4):
                state = slice_step(state,int(coordinate),rng)
            v = np.exp(state)
            if iteration >= warmup:
                sigma2,tau_l2,tau_g2,tau_r2 = v
                _,wg,bg,mg,_,wr,br,mr,zr = summaries(v)
                # Draw the full Gaussian block after marginalising child effects.
                var_mu = 1/(1/.02**2+zr.sum())
                mu = rng.normal(var_mu*np.sum(zr*mr),math.sqrt(var_mu))
                var_r = 1/(wr+1/tau_r2)
                root_b = rng.normal(var_r*(br+mu/tau_r2),np.sqrt(var_r))
                var_g = 1/(wg+1/tau_g2)
                group_b = rng.normal(var_g*(bg+root_b[group_root]/tau_g2),np.sqrt(var_g))
                kept.append(np.r_[mu,root_b,group_b,v])
        chains_out.append(kept)
    samples = np.array(chains_out)
    # Variances are monitored on the log scale for diagnostics.
    monitored = samples.copy()
    monitored[:,:,-4:] = np.log(monitored[:,:,-4:])
    diagnostics = _rhat_ess(monitored)
    flat = samples.reshape(-1,samples.shape[-1])
    return {"global_draws":flat[:,0], "category_draws":flat[:,1:1+nr],
            "group_draws":flat[:,1+nr:1+nr+ng], "variance_draws":flat[:,-4:],
            "categories":roots, "groups":groups,
            "listing_stats":{int(pk):{"duration":float(r.duration), "log_change":float(r.log_change),
                                     "group":group_index[(r.category,r.condition)]}
                             for pk,r in stats.iterrows()},
            "diagnostics":diagnostics, "seed":seed}


def _price_samples(bundle, category, condition, horizon, listing_pk=None, listing_evidence=None):
    # Seed per listing/group, independent of prediction batch order.
    rng = np.random.default_rng(bundle["seed"] + int(listing_pk or 0) % 2147483647)
    sigma2, tau_l2, tau_g2, tau_r2 = bundle["variance_draws"].T
    key = (category or "unknown", condition or "unknown")
    stat = bundle["listing_stats"].get(listing_pk)
    if listing_evidence:
        stat = dict(stat or {}, **listing_evidence)
    if stat and "group" in stat:
        group = bundle["group_draws"][:,stat["group"]]
    elif key in bundle["groups"]:
        group = bundle["group_draws"][:, bundle["groups"].index(key)]
    elif key[0] in bundle["categories"]:
        root = bundle["category_draws"][:,bundle["categories"].index(key[0])]
        group = rng.normal(root,np.sqrt(tau_g2))
    else:
        root = rng.normal(bundle["global_draws"],np.sqrt(tau_r2))
        group = rng.normal(root,np.sqrt(tau_g2))
    if stat:
        var = 1/(stat["duration"]/sigma2 + 1/tau_l2)
        drift = rng.normal(var*(stat["log_change"]/sigma2+group/tau_l2),np.sqrt(var))
    else:
        drift = rng.normal(group,np.sqrt(tau_l2))
    log_ratio = rng.normal(drift*horizon,np.sqrt(sigma2*horizon))
    # Finite numerical bounds only; wide intervals remain visible.
    return np.exp(np.clip(log_ratio,-30,30))


def _chronological_split(df):
    """Keep listings disjoint and train only on measurements known before test origins.

    Entry times precede the first daily price endpoints on a young corpus.
    Move that boundary to a held-out measured origin when necessary, rather
    than permanently asking for intervals completed before collection began.
    Stable positional cohorts also keep simultaneous arrivals disjoint.
    """
    starts = df.groupby("listing_pk").first_seen_at.min().sort_values(kind="stable")
    test_ids = starts.iloc[int(len(starts)*.8):].index
    split_at = starts.loc[test_ids].min()
    earlier = df[~df.listing_pk.isin(test_ids)].copy()
    test = df[df.listing_pk.isin(test_ids)]
    earlier["known_at"] = earlier[["end_at", "available_at"]].max(axis=1)
    if len(earlier) >= MIN_TRAIN_ROWS and earlier.listing_pk.nunique() >= MIN_TRAJECTORIES:
        ready_at = max(earlier.known_at.sort_values().iloc[MIN_TRAIN_ROWS-1],
                       earlier.groupby("listing_pk").known_at.min().sort_values().iloc[MIN_TRAJECTORIES-1])
        if ready_at >= split_at:
            origins = test[(test.start_at > ready_at) & (test.start_observed_at <= test.start_at)]
            if not origins.empty:
                split_at = origins.start_at.min()
    train = earlier[(earlier.end_at < split_at) & (earlier.available_at < split_at)].drop(columns="known_at")
    return train, test, split_at


def _chronological_evaluation(df, draws, warmup, chains, seed):
    train, test, split_at = _chronological_split(df)
    test_ids = test.listing_pk.unique()
    if len(train) < MIN_TRAIN_ROWS or train.listing_pk.nunique() < MIN_TRAJECTORIES or len(test_ids) < 3:
        # Name the shortfall. "Insufficient disjoint chronological trajectories"
        # told the operator nothing about which of the three requirements failed,
        # and the usual cause -- every measured price path being shorter than the
        # boundary that would leave a train and a holdout on opposite sides --
        # reads as a bug rather than as a young corpus.
        return {"available":False, "reason":(
            f"Only {len(train)} measured intervals across {train.listing_pk.nunique()} "
            f"listings end before the {split_at.date()} holdout boundary; "
            f"{MIN_TRAIN_ROWS} intervals across {MIN_TRAJECTORIES} listings are needed to "
            f"train. There are {len(test_ids)} held-out listings; at least 3 are needed to test")}
    posterior = _sample_posterior(train,draws,warmup,chains,seed)
    actual, median, covered, baseline, durations = [],[],[],[],[]
    for pk, path in test.groupby("listing_pk"):
        path = path[(path.start_at >= split_at) & (path.start_observed_at <= path.start_at)].sort_values("start_at")
        if path.empty:
            continue
        h = (path.end_at.max()-path.start_at.min()).total_seconds()/86400
        if not 0 < h <= 90:
            continue
        ratios = _price_samples(posterior,path.iloc[0].category,path.iloc[0].condition,h)
        lo, mid, hi = np.quantile(ratios,[.05,.5,.95])
        observed = math.exp(path.log_change.sum())
        actual.append(observed)
        median.append(mid)
        baseline.append(1.0)
        covered.append(lo <= observed <= hi)
        durations.append(h)
    if not actual:
        return {"available":False, "reason":"No held-out trajectories within 90 days"}
    mae=float(np.mean(np.abs(np.array(actual)-median)))
    baseline_mae=float(np.mean(np.abs(np.array(actual)-baseline)))
    by_horizon = {}
    for horizon in HORIZONS:
        errors, baseline_errors, coverage = [], [], []
        segment_errors = defaultdict(list)
        segment_durations = defaultdict(list)
        conditioned = 0
        evaluated_durations = []
        for pk, path in test.groupby("listing_pk"):
            path = path.sort_values("start_at")
            # One endpoint per held-out listing avoids treating many repeated
            # observations of one item as independent validation subjects.
            # Use the latest origin with a measured endpoint near this horizon.
            selected = None
            for _, origin in path.iloc[::-1].iterrows():
                if origin.start_at < split_at or origin.start_observed_at > origin.start_at:
                    continue
                elapsed = (path.end_at-origin.start_at).dt.total_seconds()/86400
                eligible = path[abs(elapsed-horizon) <= horizon*.1]
                if not eligible.empty:
                    endpoint = eligible.loc[(abs(elapsed.loc[eligible.index]-horizon)).idxmin()]
                    selected = (origin, endpoint)
                    break
            if selected is None:
                continue
            origin, endpoint = selected
            actual_h = (endpoint.end_at-origin.start_at).total_seconds()/86400
            history = path[(path.end_at <= origin.start_at) & (path.available_at <= origin.start_at)]
            evidence = {"duration":float(history.duration.sum()), "log_change":float(history.log_change.sum())} if not history.empty else None
            conditioned += int(evidence is not None)
            samples = _price_samples(posterior, origin.category, origin.condition, actual_h,
                                     listing_pk=int(pk), listing_evidence=evidence)
            lo, mid, hi = np.quantile(samples, [.05,.5,.95])
            observed = endpoint.final_price/origin.initial_price
            errors.append(abs(observed-mid))
            baseline_errors.append(abs(observed-1))
            coverage.append(lo <= observed <= hi)
            evaluated_durations.append(actual_h)
            segment_errors[(origin.category, origin.condition)].append(
                (abs(observed-mid), abs(observed-1), lo <= observed <= hi))
            segment_durations[(origin.category, origin.condition)].append(actual_h)
        enough = len(errors) >= 10
        # The holdout evidence for this horizon is only evidence about this
        # horizon if the endpoints actually reached it. An interval measured
        # over a fraction of a day cannot support a seven-day claim however
        # long the training series happens to be, so support is decided on the
        # durations the validation actually used.
        achieved_max = max(evaluated_durations) if evaluated_durations else 0.
        by_horizon[str(horizon)] = {
            "available": enough, "n_test": len(errors),
            "horizon_supported": bool(enough and horizon <= achieved_max
                                      and horizon <= float(train.groupby("listing_pk").duration.sum().max())),
            "mae_price_ratio": float(np.mean(errors)) if errors else None,
            "baseline_mae_price_ratio": float(np.mean(baseline_errors)) if errors else None,
            "improves_baseline": bool(enough and np.mean(errors)<np.mean(baseline_errors)),
            "coverage_90": float(np.mean(coverage)) if coverage else None,
            "calibration_ok": bool(enough and .8 <= np.mean(coverage) <= 1),
            "observation_window_fraction": .1,
            "n_conditioned_on_history": conditioned,
            "evaluated_duration_days_min":min(evaluated_durations) if evaluated_durations else None,
            "evaluated_duration_days_max":max(evaluated_durations) if evaluated_durations else None,
            "training_span_days": float(train.groupby("listing_pk").duration.sum().max()),
            "forecast_origin":"latest measured origin with a held-out endpoint",
        }
        if not enough:
            by_horizon[str(horizon)]["reason"] = (
                f"{len(errors)} held-out listings have measured endpoints near {horizon} days; "
                f"at least 10 are required. Held-out paths reach {max(durations):.2f} days")
        by_horizon[str(horizon)]["by_segment"] = []
        for (category, condition), values in segment_errors.items():
            segment_train = train[(train.category == category) & (train.condition == condition)]
            span = float(segment_train.groupby("listing_pk").duration.sum().max()) if not segment_train.empty else 0
            achieved = max(segment_durations.get((category, condition), [0.]), default=0.)
            enough_segment = len(values) >= 10
            a, b, c = np.array(values).T
            by_horizon[str(horizon)]["by_segment"].append(dict(
                category=category, condition=condition, max_duration_days=min(span, achieved),
                available=enough_segment, n_test=len(values),
                # Same rule as the horizon: what the validation reached, not
                # what the training rows for this segment add up to.
                horizon_supported=bool(horizon <= achieved and horizon <= span),
                evaluated_duration_days_max=achieved,
                improves_baseline=bool(np.mean(a)<np.mean(b)),
                calibration_ok=bool(.8 <= np.mean(c) <= 1)))
    return {"available":True, "requires_segment_support":True, "by_horizon": by_horizon, "n_train_listings":int(train.listing_pk.nunique()),
            "n_test_listings":len(actual), "train_end_at":train.end_at.max().isoformat(),
            "test_start_at":test.loc[test.start_at >= split_at, "start_at"].min().isoformat(),
            "mae_price_ratio":mae, "baseline_mae_price_ratio":baseline_mae,
            "improves_baseline":bool(mae<baseline_mae),
            "evaluated_duration_days_min":float(min(durations)),
            "evaluated_duration_days_max":float(max(durations)),
            "coverage_90":float(np.mean(covered)), "split_at":split_at.isoformat(),
            "diagnostics":posterior["diagnostics"], "evaluation":"chronological, disjoint listings"}


def train_price_trajectory(now=None, draws=400, warmup=400, chains=4, seed=17):
    now = now or datetime.now(timezone.utc)
    with db.read_snapshot():
        df = _observed_trajectories(now)
    readiness = _trajectory_readiness(df)
    if not readiness["ready"]:
        raise ValueError(readiness["reason"])
    evaluation = _chronological_evaluation(df,draws,warmup,chains,seed)
    bundle = _sample_posterior(df,draws,warmup,chains,seed)
    span = df.groupby("listing_pk").duration.sum().max()
    bundle.update(model_version=version_id(MODEL_VERSION,df.to_dict("records"),now),
                  method_version=MODEL_VERSION, trained_at=now.isoformat(),
                  observed_duration_days=float(span), evaluation=evaluation,
                  n_trajectories=int(df.listing_pk.nunique()),n_intervals=len(df), readiness=readiness)
    bundle["acceptance"] = guidance_acceptance(evaluation,
        bundle["diagnostics"]["diagnostics_ok"] and evaluation.get("diagnostics", {}).get("diagnostics_ok", False))
    bundle["activated"] = bundle["acceptance"]["accepted"]
    save_bundle("price_trajectory",bundle,
                {"n_observations":len(df),"n_entities":bundle["n_trajectories"],
                 "train_start_at":df.start_at.min().isoformat(),"train_end_at":df.end_at.max().isoformat(),
                 "metrics":{"evaluation":evaluation,"diagnostics":bundle["diagnostics"],
                            "acceptance":bundle["acceptance"], "readiness": readiness},
                 "method":{"hierarchy":["global","category","condition","listing"],
                           "likelihood":"log_change ~ Normal(drift * days, sigma^2 * days)",
                           "priors":{"global_sd":.02,"sigma2_ig":[3,.0008],
                                     "listing_variance_ig":[3,.00002],"condition_variance_ig":[3,.00002],
                                     "category_variance_ig":[3,.00005]},
                           "listing_evidence":"measured log-price increments available at forecast time",
                           "validation_origin":"latest measured origin per held-out listing and horizon",
                           "interval":"90% posterior predictive","seed":seed}},activate=bundle["activated"])
    return {k:bundle[k] for k in ("model_version","trained_at","n_trajectories","n_intervals",
                                  "observed_duration_days","evaluation","diagnostics","activated","acceptance", "readiness")}


def evaluate_price_trajectory():
    bundle = load_bundle("price_trajectory",MODEL_VERSION, experimental=True)
    if not bundle["diagnostics"]["diagnostics_ok"]:
        raise ValueError("Price posterior failed convergence diagnostics; retrain before forecasting")
    return dict(bundle["evaluation"],model_version=bundle["model_version"],trained_at=bundle["trained_at"],
                posterior_diagnostics=bundle["diagnostics"], readiness=bundle.get("readiness", {}))


def predict_price_trajectories(listing_pks=None,horizons=None,now=None,experimental=False):
    horizons = horizons_checked(horizons)
    now = now or datetime.now(timezone.utc)
    if listing_pks == []:
        return []
    bundle = (load_bundle("price_trajectory",MODEL_VERSION,experimental=True) if experimental
              else load_bundle("price_trajectory",MODEL_VERSION))
    if not bundle["diagnostics"]["diagnostics_ok"]:
        raise ValueError("Price posterior failed convergence diagnostics; retrain before forecasting")
    acceptance = bundle.get("acceptance", {})
    if not experimental and not acceptance.get("accepted"):
        raise ValueError("Price model has not cleared guidance acceptance; inspect the experimental candidate")
    where = ["status_norm='active'","outcome_status='unknown'","coverage='collecting'",
             "COALESCE(is_reserved,FALSE)=FALSE","COALESCE(is_giveaway,FALSE)=FALSE","listed_price_eur>0"]
    params = list(listing_pks or [])
    if params:
        where.append("listing_pk IN ("+",".join("?" for _ in params)+")")
    with db.read_snapshot():
        rows = db.query("SELECT * FROM listings WHERE "+" AND ".join(where),params)
        snapshots = entry_features(now)
        paths = _observed_trajectories(now, [r["listing_pk"] for r in rows])
    observed_stats = {} if paths.empty else {
        int(pk):{"duration":float(path.duration.sum()), "log_change":float(path.log_change.sum())}
        for pk, path in paths.groupby("listing_pk")}
    cutoff = now-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    predictions = []
    for r in rows:
        first = metrics.parse_ts(r["first_seen_at"])
        price_time = metrics.parse_ts(r["price_observed_at"] or r["last_seen_at"])
        if not is_priced(r,cutoff) or not first or first>now or metrics.parse_ts(r["last_seen_at"])>now or not price_time or price_time>now:
            continue
        for h in horizons:
            snapshot = snapshots.get(r["listing_pk"], {})
            supported = validated_segment_support(bundle.get("evaluation", {}), h,
                snapshot.get("category_root_norm"), snapshot.get("condition_norm"), h)
            accepted = accepted_segment(acceptance, h, snapshot.get("category_root_norm"),
                                        snapshot.get("condition_norm"), h) and supported
            if not experimental and not accepted:
                continue
            ratios = _price_samples(bundle,snapshot.get("category_root_norm"),snapshot.get("condition_norm"),h,r["listing_pk"],observed_stats.get(r["listing_pk"]))
            prices = np.quantile(ratios,[.05,.5,.95])*float(r["listed_price_eur"])
            predictions.append({"listing_pk":r["listing_pk"],"model_version":bundle["model_version"],
                                "horizon_days":h,"predicted_price_eur":round(float(prices[1]),2),
                                "features_json":json.dumps({"cat_root":snapshot.get("category_root_norm") or "unknown",
                                                            "condition":snapshot.get("condition_norm") or "unknown"}),
                                "price_ci_lo":round(float(prices[0]),2),"price_ci_hi":round(float(prices[2]),2),
                                "is_extrapolated":not supported,
                                "observed_duration_days":bundle["observed_duration_days"],
                                "diagnostics_ok":bundle["diagnostics"]["diagnostics_ok"],
                                "validation_available":bundle["evaluation"].get("by_horizon", {}).get(str(h), {}).get("available",False),
                                "validation_improves_baseline":bundle["evaluation"].get("by_horizon", {}).get(str(h), {}).get("improves_baseline",False),
                                "is_accepted":accepted,
                                "computed_at":now.isoformat()})
    return predictions


def persist_trajectories(predictions):
    cols = ("listing_pk","model_version","day","predicted_price_eur","price_ci_lo","price_ci_hi",
            "is_extrapolated","observed_duration_days","diagnostics_ok", "validation_available",
            "validation_improves_baseline","is_accepted","computed_at","features_json")
    conn = db.connect()
    try:
        with conn.transaction():
            sources = forecast_sources(predictions, lambda table, sql, params:
                                       [dict(r) for r in conn.execute(sql, params).fetchall()])
            for p in predictions:
                horizons_checked([p["horizon_days"]])
                lo,mid,hi = (p[k] for k in ("price_ci_lo","predicted_price_eur","price_ci_hi"))
                if not all(math.isfinite(v) for v in (lo,mid,hi)) or not 0 <= lo <= mid <= hi:
                    raise ValueError("price interval must be finite, nonnegative and ordered")
                row = dict(p,day=p["horizon_days"])
                validation = forecast_validation("price_trajectory", row,
                    sources[0].get(p["model_version"]), sources[1].get(p["listing_pk"]))
                if p.get("is_accepted") and not validation["is_accepted"]:
                    raise ValueError("accepted price forecasts require registered chronological validation: "
                                     + validation["validation_reason"])
                row.update(validation)
                conn.execute("INSERT INTO price_trajectories ("+",".join(cols)+") VALUES ("
                             +",".join("?" for _ in cols)+") ON CONFLICT (listing_pk,model_version,day) "
                             "DO UPDATE SET "+",".join(f"{c}=excluded.{c}" for c in cols[3:]),
                             [row.get(c) for c in cols])
    finally:
        conn.close()
    return len(predictions)
