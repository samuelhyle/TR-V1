"""Read saved integrity checks without running fixtures or local inference."""

from datetime import datetime, timezone

from . import analyst, analyst_eval, config
from .metrics import parse_ts


def report():
    identity = analyst.model_identity()
    expected = {name: analyst_eval.fixture_digest(name, case, golden)
                for name, case, golden in analyst_eval.CASES}
    runs = []
    for mode, filename in (("offline", "analyst_evaluation.json"),
                           ("live", "analyst_evaluation_live.json")):
        row = {"mode": mode, "filename": filename, "status": "missing"}
        try:
            saved = analyst_eval.read_report(config.REPORTS_DIR / filename)
            if saved["mode"] != mode:
                raise ValueError("report mode does not match its destination")
            generated = parse_ts(saved.get("generated_at"))
            if generated is None or generated > datetime.now(timezone.utc):
                raise ValueError("invalid evaluation timestamp")
            matching = [case for case in saved["results"]
                        if case.get("fixture_sha256") == expected.get(case["case"]) and case["case"] in expected]
            contract = all(saved.get("model", {}).get(key) == identity[key]
                           for key in ("prompt_version", "evidence_version"))
            model_match = (saved.get("model", {}).get("model") == identity["model"] and
                           saved.get("model", {}).get("revision") == identity["revision"])
            current = contract and len(matching) == len(saved["results"]) and (mode == "offline" or model_match)
            complete = {case["case"] for case in matching} == set(expected)
            status = "stale" if not current else "failed" if saved["n_passed"] < saved["n_cases"] else "passed" if complete else "partial"
            row.update(status=status, generated_at=saved["generated_at"], n_cases=saved["n_cases"],
                       n_passed=saved["n_passed"], expected_cases=len(expected), current=current,
                       complete=complete, model_match=model_match, model=saved.get("model", {}).get("model"),
                       age_hours=round((datetime.now(timezone.utc)-generated).total_seconds()/3600, 1),
                       missing_cases=sorted(set(expected)-{case["case"] for case in matching}),
                       fixture_policy=saved.get("fixture_policy"),
                       cases=[{"case": case["case"], "passed": analyst_eval.passed(case),
                               "evidence_passed": case["evidence_checks_passed"],
                               "model_status": case.get("model_status"), "latency_ms": case.get("latency_ms"),
                               "failures": case.get("failures", []),
                               "failed_checks": [check for check in case.get("checks", []) if check.get("passed") is False]}
                              for case in saved["results"]],
                       comparison=saved.get("comparison"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            row.update(status="invalid", error="Saved evaluation could not be validated. Run the checks again.")
        runs.append(row)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "evaluations": runs,
            "expected_cases": len(expected), "fixture_policy": analyst_eval.FIXTURE_POLICY,
            "limits": "Synthetic integrity checks measure evidence and output validation. Model priorities and financial performance remain unmeasured."}
