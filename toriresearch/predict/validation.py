"""Rehearse model fits and result persistence on a read-only source's private copy."""

import argparse
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config, db, local_runtime, metrics
from ..fileio import write_text


def _fit_status(evaluation):
    if not evaluation.get("available"):
        return "trained_validation_unavailable"
    return "trained_improves_baseline" if evaluation.get("improves_baseline") else "trained_below_baseline"


def _prediction_counts(rows, acceptance):
    return {"guidance_status":"accepted" if acceptance.get("accepted") else "withheld",
            "accepted_predictions":sum(bool(r.get("is_accepted")) for r in rows),
            "n_extrapolated":sum(bool(r.get("is_extrapolated")) for r in rows),
            "prediction_mode":"experimental rehearsal on private copy"}


def _latest_trend_evidence(rows):
    latest = {}
    for row in sorted(rows, key=lambda r: r["week_start"]):
        score = {k:v for k,v in row.get("direction_evaluation", {}).items() if k != "windows"}
        latest[(row["category_norm"], row.get("region_norm") or "")] = {
            k:row[k] for k in ("category_norm", "region_norm", "week_start", "model_version",
                              "trend_state", "evidence_weeks", "entry_price_count", "unavailability")}
        latest[(row["category_norm"], row.get("region_norm") or "")]["direction_evaluation"] = score
    return list(latest.values())


def validate_database(source, now=None):
    source = Path(source).resolve()
    record = local_runtime.owner()
    if record and source == Path(record["database"]).resolve():
        local_runtime.assert_database_owner()
    from . import sale_probability as sale, price_trajectory as price, category_trends as trends
    from . import seller_archetypes as sellers, market_concentration as concentration, _linkage
    now = now or datetime.now(timezone.utc)
    if not source.is_file():
        raise ValueError("Source SQLite database does not exist")
    original = {key:getattr(config,key) for key in ("SQLITE_PATH","POSTGRES_DSN","DATA_DIR","REPORTS_DIR")}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tori-predict-validation-") as directory:
        clone = Path(directory)/"research.sqlite"
        reader = sqlite3.connect(source.as_uri()+"?mode=ro",uri=True)
        writer = sqlite3.connect(clone)
        try:
            reader.backup(writer)
        finally:
            writer.close()
            reader.close()
        try:
            config.SQLITE_PATH, config.POSTGRES_DSN = clone,None
            config.DATA_DIR, config.REPORTS_DIR = Path(directory),Path(directory)/"reports"
            db.init_db()
            report = {"computed_at":now.isoformat(),"source_modified":False,"validation_backend":"isolated SQLite copy",
                      "listings":db.scalar("SELECT COUNT(*) FROM listings"),
                      "outcomes":db.query("SELECT outcome_status,COUNT(*) AS n FROM listings GROUP BY outcome_status"),
                      "entry_snapshots":db.scalar("SELECT COUNT(*) FROM predictive_feature_snapshots"),
                      "ledger_counts":{name:db.scalar("SELECT COUNT(*) FROM "+name) for name in
                          ("purchases", "transactions", "inventory_units", "order_lines",
                           "financial_entries", "research_decisions", "acquisition_attempts")},
                      "sold_aggregate_captures":db.scalar("SELECT COUNT(*) FROM sold_observations"),
                      "seller_linkage":_linkage.status(),"models":{}}
            # Bound the prediction rehearsal. Fits and trends still use the full copy.
            targets=[r["listing_pk"] for r in db.query("SELECT listing_pk FROM listings WHERE status_norm='active' "
                     "AND outcome_status='unknown' AND coverage='collecting' AND listed_price_eur>0 "
                     "ORDER BY last_seen_at DESC,listing_pk LIMIT 25")]
            def check(name,operation):
                try:
                    report["models"][name]=operation()
                except (ValueError,ImportError) as exc:
                    report["models"][name]={"status":"unavailable","reason":str(exc)}
            def cox():
                model=sale.train_sale_probability(now=now)
                # Candidate forecasts belong only to this disposable copy. A
                # withheld fit must retain its evaluation rather than vanish
                # when the normal guidance path correctly refuses to use it.
                rows=sale.predict_sale_probability(targets,model=model,now=now,experimental=True)
                persisted=sale.persist_predictions(rows)
                evaluation=model.training_metrics["evaluation"]
                return {"status":_fit_status(evaluation),"fit":model.training_metrics,
                        "evaluation":evaluation,"predictions_persisted":persisted,
                        **_prediction_counts(rows, model.acceptance)}
            def trajectory():
                readiness=price.price_trajectory_readiness(now)
                if not readiness["ready"]:
                    return {"status":"unavailable", "evidence_status":"insufficient_data",
                            "reason":readiness["reason"], "readiness":readiness,
                            "predictions_persisted":0}
                result=price.train_price_trajectory(now=now)
                if not result["diagnostics"]["diagnostics_ok"]:
                    return {"status":"failed_diagnostics","fit":result,"predictions_persisted":0,
                            **_prediction_counts([], result["acceptance"])}
                rows=price.predict_price_trajectories(targets,now=now,experimental=True)
                persisted=price.persist_trajectories(rows)
                evaluation=result["evaluation"]
                return {"status":_fit_status(evaluation), "evaluation":evaluation,
                        "fit":result,"predictions_persisted":persisted,
                        **_prediction_counts(rows, result["acceptance"])}
            def category():
                rows=trends.compute_category_trends(now=now)
                forecasts=trends.compute_trend_forecasts(now=now,experimental=True)
                n=trends.persist_trends(rows)
                f=trends.persist_forecasts(forecasts)
                result = {"status":"computed" if rows else "unavailable",
                        "rows_persisted":n,"latest_states":trends.summary(),
                        "latest_evidence":_latest_trend_evidence(rows),
                        "maximum_evidence_weeks":max((r["evidence_weeks"] for r in rows), default=0),
                        "rows_with_seasonality":sum(r["seasonality_idx"] is not None for r in rows),
                        "forecasts_persisted":f,
                        "accepted_forecasts":sum(bool(r["is_accepted"]) for r in forecasts),
                        "forecast_evaluations":{r["category_norm"]:r["evaluation"] for r in forecasts},
                        "forecasted_categories":len({r["category_norm"] for r in forecasts}),
                        "maximum_forecast_lead_weeks":max((r["horizon_weeks"] for r in forecasts),default=0)}
                if not rows:
                    result["reason"] = (f"Category trends need {trends.MIN_WEEKS} completed week(s) "
                                        "in a category, currently 0")
                return result
            def archetypes():
                result=sellers.train_seller_archetypes(persist=True,now=now)
                return {"status":"computed","fit":{k:v for k,v in result.items() if k!="assignments"}}
            def market():
                rows=concentration.compute_category_concentration(now=now)
                return {"status":"computed","rows_persisted":concentration.persist_concentration(rows),
                        "minimum_linkage_coverage":min(r["linkage_coverage"] for r in rows)}
            for name,operation in [("sale_probability",cox),("price_trajectory",trajectory),("category_trends",category),
                                   ("seller_archetypes",archetypes),("market_concentration",market)]:
                check(name,operation)
            report["elapsed_seconds"]=round(time.monotonic()-started,3)
            return report
        finally:
            for key,value in original.items():
                setattr(config,key,value)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=config.SQLITE_PATH,help="read-only source database")
    parser.add_argument("--as-of",help="ISO timestamp for historical evidence cutoff")
    parser.add_argument("--output",type=Path,help="aggregate validation report JSON")
    args=parser.parse_args(argv)
    now=metrics.parse_ts(args.as_of) if args.as_of else None
    if args.as_of and now is None:
        parser.error("--as-of must be an ISO timestamp")
    record = local_runtime.owner()
    if record and args.db.resolve() == Path(record["database"]).resolve():
        delegated = local_runtime.delegate("toriresearch.predict.validation", argv)
        if delegated is not None:
            return delegated
    report=validate_database(args.db,now)
    content=json.dumps(report,indent=2,allow_nan=False,default=str)+"\n"
    if args.output:
        write_text(args.output,content)
    print(content,end="")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
