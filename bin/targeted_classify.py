"""Preview current classification gaps and recover one bounded durable batch.

Broad category counts are an upper bound for research supply, not proof of
comparison eligibility. Recovery uses the ordinary leased queue and its runtime,
retry, input freshness and permission checks. Re-run to resume pending work.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from toriresearch import analysis, collection, config, db, metrics
from toriresearch.enrich import runner, tasks, vocabulary


def qualifying_rows(platform, subplatform, min_n, max_age_hours):
    """Fresh priced listings in a leaf-category/condition group that can clear min_n."""
    where, params, _ = metrics.listing_filters(platform=platform, subplatform=subplatform)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = db.query("SELECT " + analysis.FIELDS + " FROM listings" + clause, params)
    market = [r for r in rows if not r["is_giveaway"] and r["listed_price_eur"] != 0]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    eligible = [r for r in market
                if r["category_norm"] and r["category_norm"] != r["category_root_norm"]
                and r["condition_norm"] and r["condition_norm"] != "unknown"
                and analysis.is_priced(r, cutoff)]
    groups = defaultdict(list)
    for row in eligible:
        groups[(row["platform"], row["subplatform"],
                row["category_norm"], row["condition_norm"])].append(row)
    return {r["listing_pk"]: r for rows_group in groups.values() if len(rows_group) > min_n
            for r in rows_group}


def outstanding(targets, version):
    """Skip only completed labels bound to all current classification inputs."""
    digests = {pk: tasks.input_digest(row["title"], row["category_norm"], version)
               for pk, row in targets.items()}
    allowed = vocabulary.load()["allowed"]
    stored = set()
    for entry in db.query(
            "SELECT listing_pk,platform,listing_id,input_sha256,prompt_version,model,output_json "
            "FROM enrichment WHERE task='product_type' AND schema_version=? "
            "AND output_json IS NOT NULL AND output_json<>'null'", (version,)):
        row = targets.get(entry["listing_pk"])
        if (not row or entry["platform"] != row["platform"] or entry["listing_id"] != row["listing_id"]
                or entry["input_sha256"] != digests[entry["listing_pk"]]
                or entry["prompt_version"] != tasks.PROMPT_VERSION or not tasks.same_model(entry["model"])):
            continue
        try:
            label = json.loads(entry["output_json"]).get("product_type")
        except (ValueError, AttributeError, TypeError):
            continue
        if label in allowed:
            stored.add(entry["listing_pk"])
    return [(pk, targets[pk], digests[pk]) for pk in sorted(targets) if pk not in stored]


class TargetScope(collection.Scope):
    def __init__(self, platform, subplatform, keys):
        super().__init__(platform)
        self.subplatform = subplatform
        self.keys = frozenset(keys)

    def conditions(self, grid=False):
        conditions, params = super().conditions(grid)
        conditions += ["l.subplatform=?", "l.listing_pk IN (" + ",".join("?" for _ in self.keys) + ")"]
        params += [self.subplatform, *sorted(self.keys)]
        return conditions, params

    def matches(self, row):
        return (row.get("listing_pk") in self.keys and row.get("subplatform") == self.subplatform
                and super().matches(row))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="tori")
    parser.add_argument("--subplatform", default="standard")
    parser.add_argument("--min-n", type=int, default=config.MIN_N_FOR_REPORT)
    parser.add_argument("--max-age-hours", type=float, default=config.PANEL_MAX_AGE_HOURS)
    parser.add_argument("--concurrency", type=int, default=config.LLM_CONCURRENCY)
    parser.add_argument("--limit", type=int, default=config.ENRICH_BUDGET,
                        help="maximum items in this batch (1-1000); 0 uses the configured bounded budget")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    limit = args.limit or config.ENRICH_BUDGET
    if not 1 <= limit <= 1000 or args.concurrency < 1 or args.min_n < 1 or args.max_age_hours <= 0:
        parser.error("positive scope and concurrency, and a batch limit of 1-1000, are required")
    targets = qualifying_rows(args.platform, args.subplatform, args.min_n, args.max_age_hours)
    version = str(vocabulary.load()["sha256"][:12])
    pending = outstanding(targets, version)
    todo = pending[:limit]
    print(json.dumps(dict(qualifying_listings=len(targets), already_current=len(targets)-len(pending),
                          pending=len(pending), batch_size=len(todo), vocabulary=version,
                          comparison_eligibility="Requires supported identity and reviewed offer evidence")))
    if args.dry_run or not todo:
        return 0
    scope = TargetScope(args.platform, args.subplatform, [pk for pk, _, _ in todo])
    result = runner.run(platform=args.platform, budget=len(todo), concurrency=args.concurrency, scope=scope)
    print(json.dumps(result, default=str))
    return 1 if result.get("errors") or result.get("skipped_no_provider") or result.get("status") == "skipped" else 0


if __name__ == "__main__":
    sys.exit(main())
