"""Run source-authored development challenges under the shared enrichment lock."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from toriresearch import db, listing_analysis as analysis, listing_analysis_audit as audit, product_profiles
from toriresearch.enrich import provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--timeout",type=float,default=60,help="Per-request timeout in seconds (1–180)")
    args = parser.parse_args()
    partial = args.out.with_suffix(".partial.json")
    if args.out.exists() or partial.exists():
        raise SystemExit("Choose a new output file")
    if not 1<=args.timeout<=180:
        raise SystemExit("Timeout must be between 1 and 180 seconds")
    document_source = json.loads(args.fixtures.read_text())
    if document_source.get("purpose")!="synthetic_development":
        raise SystemExit("This runner only accepts synthetic development fixtures")
    lock,error = db.acquire_lock("enrich")
    if not lock:
        print(json.dumps(dict(status="busy",reason=error)),flush=True)
        return 75
    cases, records = [], []
    started = time.monotonic()
    failure_detail = {}
    original_post = provider._post
    def diagnostic_post(*args,**kwargs):
        try:
            return original_post(*args,**kwargs)
        except provider.ProviderUnavailable as exc:
            # Fixed categories only: provider errors may contain response text.
            message = str(exc).lower()
            failure_detail["provider_failure_class"] = ("timeout" if "timeout" in message or "timed out" in message else
                "context_limit" if "context" in message else "http_client_error" if message.startswith("http 4") else
                "http_server_error" if message.startswith("http 5") else "unavailable")
            raise
    def snapshot(complete):
        return dict(purpose="synthetic_development",independent_holdout=False,identity=analysis.identity(),
                    analysis_source_sha256=hashlib.sha256(Path(analysis.__file__).read_bytes()).hexdigest(),
                    source_fixture_sha256=analysis._hash(document_source),elapsed_seconds=round(time.monotonic()-started,3),
                    request_timeout_seconds=args.timeout,complete=complete,cases=cases,summary=records,
                    automatic_comparison_promoted=False)
    provider._post = diagnostic_post
    try:
        for case in document_source["cases"]:
            failure_detail.clear()
            capsule = analysis.prepare(case)
            row = {key:case.get(key) for key in ("title","category_norm","brand_norm","model_norm","condition_norm")}
            record = analysis.analyze(row,capsule,[],timeout=args.timeout)
            result = record.get("output_json") or {}
            expected = case["expected"]
            checks = {"profile":result.get("profile")==expected["profile"],"offer_role":result.get("offer_role")==expected["offer_role"]}
            for key,value in expected["fields"].items():
                field = result.get("fields",{}).get(key,{})
                checks[key] = field.get("state") in {"known","explicit_no"} and audit._equal(field.get("value"),value)
            for key in expected.get("unknown",[]):
                checks["unknown:"+key] = result.get("fields",{}).get(key,{}).get("state","unknown")=="unknown"
            for key in expected.get("conflict",[]):
                checks["conflict:"+key] = result.get("fields",{}).get(key,{}).get("state")=="conflict"
            item = dict(id=case["id"],expected=expected,prediction=result,checks=checks,error=record.get("error"),
                        validation_detail=record.get("validation_detail"),latency_ms=record.get("latency_ms"),**failure_detail)
            cases.append(item)
            # A diagnostic record, never imported as a human label.
            records.append(dict(id=case["id"],family=product_profiles.load()["profiles"][expected["profile"]]["family"],
                                passed=sum(checks.values()),checks=len(checks),failed=[key for key,value in checks.items() if not value]))
            print(json.dumps(dict(id=case["id"],profile=result.get("profile"),role=result.get("offer_role"),error=item["error"],
                                  failed=records[-1]["failed"],latency_ms=item["latency_ms"],**failure_detail)),flush=True)
            temporary = partial.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshot(False),ensure_ascii=False,indent=2)+"\n")
            temporary.replace(partial)
    finally:
        provider._post = original_post
        db.release_lock(lock)
    output = snapshot(True)
    args.out.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n")
    partial.unlink(missing_ok=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
