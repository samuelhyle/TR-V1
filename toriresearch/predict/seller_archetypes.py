"""HDBSCAN clusters of observed behavior under pseudonymous seller keys.

Group across SCD-2 versions by (platform, seller_id). Membership scores describe
cluster fit; labels describe observed centroids and never imply seller intent.
"""

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .. import db, metrics
from . import _linkage as linkage
from ._common import finite_price, positive_int, save_bundle, version_id

MODEL_VERSION = "seller-hdbscan-v2"
MIN_SELLER_LISTINGS = 3
MAX_SELLERS = 5000
FEATURE_COLUMNS = ["log_n_listings","log_median_price","price_cv","promotion_rate",
                   "price_change_rate","avg_first_drop_days","avg_listing_age_days",
                   "sell_through_rate","diili_rate","relist_rate"]


def _seller_features(days=180,now=None):
    positive_int(days,"days")
    now=now or datetime.now(timezone.utc)
    since=now-timedelta(days=days)
    rows=db.query("SELECT s.platform,s.seller_id,l.listing_pk,l.listed_price_eur,l.first_seen_at,"
                  "l.last_seen_at,l.outcome_status,l.outcome_observed_at,l.promotion_active,l.subplatform "
                  "FROM listings l JOIN sellers s ON l.seller_version_id=s.seller_version_id")
    by_seller={}
    for r in rows:
        stamp=metrics.parse_ts(r["first_seen_at"])
        if stamp and since<=stamp<=now:
            by_seller.setdefault((r["platform"],r["seller_id"]),[]).append(r)
    events=db.query("SELECT listing_pk,event_type,event_at,observed_at,before_value,after_value FROM events "
                    "WHERE event_type IN ('price_changed','relisted')")
    changes, drops, relists={},{},{}
    for e in events:
        stamp, observed=metrics.parse_ts(e["event_at"]), metrics.parse_ts(e["observed_at"])
        if not stamp or not observed or not since<=stamp<=now or observed>now:
            continue
        pk=e["listing_pk"]
        if e["event_type"]=="relisted":
            relists[pk]=relists.get(pk,0)+1
        else:
            before,after=finite_price(e["before_value"]),finite_price(e["after_value"])
            if before and after and before!=after:
                changes[pk]=changes.get(pk,0)+1
                if after<before:
                    drops[pk]=min(stamp,drops.get(pk,stamp))
    records=[]
    for (platform,sid),listings in sorted(by_seller.items(),key=lambda item:(-len(item[1]),item[0]))[:MAX_SELLERS]:
        n=len(listings)
        if n<MIN_SELLER_LISTINGS:
            continue
        prices=[p for p in (finite_price(r["listed_price_eur"]) for r in listings) if p is not None]
        if not prices:
            continue
        delays, ages=[],[]
        n_sold=0
        for r in listings:
            first=metrics.parse_ts(r["first_seen_at"])
            end=metrics.parse_ts(r["last_seen_at"])
            if end and first<=end<=now:
                ages.append((end-first).total_seconds()/86400)
            stamp=drops.get(r["listing_pk"])
            if stamp and stamp>=first:
                delays.append((stamp-first).total_seconds()/86400)
            outcome_at=metrics.parse_ts(r["outcome_observed_at"])
            n_sold+=bool(r["outcome_status"] in ("sold_confirmed","sold_inferred") and outcome_at and outcome_at<=now)
        median=float(np.median(prices))
        records.append({"platform":platform,"seller_id":sid,"n_listings":n,
                        "log_n_listings":math.log1p(n),"median_price":median,"log_median_price":math.log(median),
                        "price_cv":float(np.std(prices)/np.mean(prices)),
                        "promotion_rate":sum(bool(r["promotion_active"]) for r in listings)/n,
                        "price_change_rate":sum(changes.get(r["listing_pk"],0) for r in listings)/n,
                        "avg_first_drop_days":float(np.mean(delays)) if delays else float(np.mean(ages)) if ages else 0.0,
                        "avg_listing_age_days":float(np.mean(ages)) if ages else 0.0,
                        "sell_through_rate":n_sold/n,  # observed sale-label fraction, subject to censoring
                        "diili_rate":sum(r["subplatform"]=="diili" for r in listings)/n,
                        "relist_rate":sum(relists.get(r["listing_pk"],0) for r in listings)/n})
    return pd.DataFrame(records)


def _label_clusters(df,labels,n_clusters=None):
    mapping={}
    for cid in sorted(set(labels)-{-1}):
        center=df.loc[labels==cid].mean(numeric_only=True)
        if center.price_change_rate>=.75 and center.avg_first_drop_days<=30:
            label="aggressive_repricer"
        elif center.promotion_rate>=.5:
            label="promoted_seller"
        elif center.n_listings>=20:
            label="volume_liquidator" if center.sell_through_rate>=.4 else "volume_seller"
        elif center.avg_first_drop_days>=30 and center.avg_listing_age_days>=30:
            label="patient_holder"
        else:
            label="occasional_seller" if center.n_listings<=5 else "standard_seller"
        mapping[int(cid)]=label
    return mapping


def train_seller_archetypes(persist=False,days=180,now=None):
    from sklearn.cluster import HDBSCAN
    from sklearn.preprocessing import StandardScaler
    now=now or datetime.now(timezone.utc)
    with db.read_snapshot():
        coverage=linkage.require()
        df=_seller_features(days,now)
        versions=db.query("SELECT seller_version_id,platform,seller_id,valid_from FROM sellers "
                          "ORDER BY valid_from DESC,seller_version_id DESC")
    if df.empty or len(df)<10:
        raise ValueError(f"Insufficient seller data: {len(df)} sellers with at least {MIN_SELLER_LISTINGS} "
                         "listings; need 10 sellers with repeated observed behavior")
    scaler=StandardScaler()
    X=scaler.fit_transform(df[FEATURE_COLUMNS])
    clusterer=HDBSCAN(min_cluster_size=max(5,min(25,len(df)//8)),min_samples=4,
                     metric="euclidean",cluster_selection_method="eom")
    labels=clusterer.fit_predict(X)
    mapping=_label_clusters(df,labels)
    latest={}
    for r in versions:
        stamp=metrics.parse_ts(r["valid_from"])
        if stamp and stamp<=now:
            latest.setdefault((r["platform"],r["seller_id"]),r["seller_version_id"])
    version=version_id(MODEL_VERSION,df.to_dict("records"),now)
    assignments=[]
    for i,(_,r) in enumerate(df.iterrows()):
        sv=latest.get((r.platform,r.seller_id))
        if sv is None:
            continue
        assignments.append({"seller_version_id":sv,"cluster_id":int(labels[i]),
                            "archetype_id":mapping.get(int(labels[i]),"unclassified"),
                            "confidence":float(clusterer.probabilities_[i]),
                            "features_json":json.dumps({"observed":{c:float(r[c]) for c in FEATURE_COLUMNS},
                                                        "standardized":dict(zip(FEATURE_COLUMNS,map(float,X[i])))}),
                            "model_version":version,"computed_at":now.isoformat()})
    counts={}
    for a in assignments:
        counts[a["archetype_id"]]=counts.get(a["archetype_id"],0)+1
    result={"model_version":version,"n_sellers":len(df),"n_clusters":len(set(labels)-{-1}),
            "n_noise":int(np.sum(labels==-1)),"archetype_counts":counts,"linkage":coverage,
            "n_persisted":0,"trained_at":now.isoformat(),"window_days":days}
    save_bundle("seller_archetypes",{"model_version":version,"method_version":MODEL_VERSION,
                "trained_at":now.isoformat(),"clusterer":clusterer,"scaler":scaler,"features":FEATURE_COLUMNS},
                {"n_observations":int(df.n_listings.sum()),"n_entities":len(df),
                 "train_start_at":(now-timedelta(days=days)).isoformat(),"train_end_at":now.isoformat(),
                 "metrics":result,"method":{"features":FEATURE_COLUMNS,"algorithm":"sklearn HDBSCAN",
                                              "confidence":"cluster membership strength","window_days":days}})
    if persist:
        result["n_persisted"]=persist_archetypes(assignments)
    return dict(result,assignments=assignments)


def persist_archetypes(rows):
    cols=("seller_version_id","archetype_id","cluster_id","confidence","features_json","model_version","computed_at")
    conn=db.connect()
    try:
        with conn.transaction():
            for r in rows:
                conn.execute("INSERT INTO seller_archetypes ("+",".join(cols)+") VALUES ("
                             +",".join("?" for _ in cols)+") ON CONFLICT (seller_version_id,model_version) "
                             "DO UPDATE SET "+",".join(f"{c}=excluded.{c}" for c in cols[1:] if c!="model_version"),
                             [r[c] for c in cols])
    finally:
        conn.close()
    return len(rows)


def summary():
    latest=db.query_one("SELECT model_version FROM seller_archetypes WHERE model_version LIKE ? "
                        "ORDER BY computed_at DESC,archetype_pk DESC LIMIT 1",(MODEL_VERSION+"-%",))
    if not latest:
        return {}
    rows=db.query("SELECT a.archetype_id,s.platform,s.seller_id,a.computed_at,a.archetype_pk "
                  "FROM seller_archetypes a JOIN sellers s ON s.seller_version_id=a.seller_version_id "
                  "WHERE a.model_version=? ORDER BY a.computed_at DESC,a.archetype_pk DESC",
                  (latest["model_version"],))
    seen,counts=set(),{}
    for r in rows:
        key=(r["platform"],r["seller_id"])
        if key not in seen:
            seen.add(key)
            counts[r["archetype_id"]]=counts.get(r["archetype_id"],0)+1
    return counts
