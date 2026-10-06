"""Monthly observed listing-arrival concentration, by stable seller identity.

HHI uses shares on 0..1. Count shares and initial asking-value shares are
reported separately, alongside linkage coverage. These are supply observations,
not realised revenue shares or causal evidence of price leadership.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import db, metrics
from . import _linkage as linkage
from ._common import finite_price, positive_int, entry_prices

MODEL_VERSION="mkt-conc-v2"
HELSINKI=ZoneInfo("Europe/Helsinki")


def _month_start(now,months_ago=0):
    local=now.astimezone(HELSINKI)
    index=local.year*12+local.month-1-months_ago
    return datetime(index//12,index%12+1,1,tzinfo=HELSINKI)


def _hhi(shares):
    return float(sum(s*s for s in shares))


def compute_category_concentration(months_back=6,max_categories=40,region_norm=None,now=None):
    positive_int(months_back,"months_back")
    positive_int(max_categories,"max_categories")
    now=now or datetime.now(timezone.utc)
    since=_month_start(now,months_back-1)
    where=["l.category_norm IS NOT NULL","l.category_norm!=''","COALESCE(l.is_giveaway,FALSE)=FALSE"]
    params=[]
    if region_norm is not None:
        where.append("l.region_norm=?")
        params.append(region_norm)
    with db.read_snapshot():
        linkage.require()
        initial_prices = entry_prices(now)
        rows=db.query("SELECT l.listing_pk,l.category_norm,l.first_seen_at,l.listed_price_eur,"
                      "l.price_observed_at,l.last_seen_at,s.platform,s.seller_id "
                      "FROM listings l LEFT JOIN sellers s ON s.seller_version_id=l.seller_version_id WHERE "
                      +" AND ".join(where),params)
    monthly={}
    category_counts={}
    for r in rows:
        stamp=metrics.parse_ts(r["first_seen_at"])
        if not stamp or not since<=stamp<=now:
            continue
        local=stamp.astimezone(HELSINKI)
        month=f"{local.year}-{local.month:02d}-01"
        cat=r["category_norm"]
        category_counts[cat]=category_counts.get(cat,0)+1
        monthly.setdefault((cat,month),[]).append(r)
    categories=set(sorted(category_counts,key=lambda c:(-category_counts[c],c))[:max_categories])
    results=[]
    for (cat,month),cohort in sorted(monthly.items()):
        if cat not in categories:
            continue
        sellers={}
        linked=priced=0
        for r in cohort:
            if not r["seller_id"]:
                continue
            linked+=1
            key=(r["platform"],r["seller_id"])
            data=sellers.setdefault(key,{"count":0,"value":0.0})
            data["count"]+=1
            entry=initial_prices.get(r["listing_pk"])
            stamp=metrics.parse_ts(r["first_seen_at"])
            price=entry[0] if entry and abs((entry[1]-stamp).total_seconds())<=1 else None
            if price is None:
                price_time=metrics.parse_ts(r["price_observed_at"] or r["last_seen_at"])
                if price_time and abs((price_time-metrics.parse_ts(r["first_seen_at"])).total_seconds())<=1:
                    price=finite_price(r["listed_price_eur"])
            if price is not None:
                data["value"]+=price
                priced+=1
        if not linked:
            continue
        counts=sorted((r["count"]/linked for r in sellers.values()),reverse=True)
        total_value=sum(r["value"] for r in sellers.values())
        values=sorted((r["value"]/total_value for r in sellers.values()),reverse=True) if total_value>0 else []
        results.append({"category_norm":cat,"region_norm":region_norm,"month_start":month,
                        "hhi":round(_hhi(values),6) if values else None,
                        "top3_share":round(sum(values[:3]),6) if values else None,
                        "hhi_count":round(_hhi(counts),6),"top3_count_share":round(sum(counts[:3]),6),
                        "n_active_sellers":len(sellers),"n_observed_listings":len(cohort),
                        "n_linked_listings":linked,"linkage_coverage":linked/len(cohort),
                        "price_coverage":priced/linked,
                        "is_complete_month":month!=_month_start(now).date().isoformat(),
                        "price_leader_seller_version_id":None,"model_version":MODEL_VERSION,"computed_at":now.isoformat()})
    if not results:
        raise ValueError(f"No resolved seller links in the requested {months_back}-month category/region window")
    return results


def persist_concentration(rows):
    cols=("category_norm","region_norm","month_start","hhi","top3_share","hhi_count","top3_count_share",
          "n_active_sellers","n_observed_listings","n_linked_listings","linkage_coverage","price_coverage",
          "is_complete_month","price_leader_seller_version_id","model_version","computed_at")
    conn=db.connect()
    try:
        with conn.transaction():
            for row in rows:
                r=dict(row,region_norm=row.get("region_norm") or "")
                conn.execute("INSERT INTO market_concentration ("+",".join(cols)+") VALUES ("
                             +",".join("?" for _ in cols)+") ON CONFLICT "
                             "(category_norm,region_norm,month_start,model_version) DO UPDATE SET "
                             +",".join(f"{c}=excluded.{c}" for c in cols[3:] if c!="model_version"),
                             [r.get(c) for c in cols])
    finally:
        conn.close()
    return len(rows)


def summary():
    rows=db.query("SELECT * FROM market_concentration WHERE model_version=? "
                  "ORDER BY month_start DESC,computed_at DESC,concentration_pk DESC",(MODEL_VERSION,))
    seen,results=set(),[]
    for r in rows:
        key=(r["category_norm"],r["region_norm"] or "")
        if key in seen:
            continue
        seen.add(key)
        # Sparse linkage must remain visible rather than imply market-wide coverage.
        level=("partial_linkage" if r["linkage_coverage"]<.8 else "high" if r["hhi_count"]>.25
               else "moderate" if r["hhi_count"]>.15 else "low")
        results.append({"category_norm":r["category_norm"],"region_norm":r["region_norm"] or None,
                        "month":str(r["month_start"]),"hhi":r["hhi"],"hhi_count":r["hhi_count"],
                        "top3_share":r["top3_share"],"n_sellers":r["n_active_sellers"],
                        "linkage_coverage":r["linkage_coverage"],"price_coverage":r["price_coverage"],
                        "is_complete_month":bool(r["is_complete_month"]),"concentration":level})
    return results
