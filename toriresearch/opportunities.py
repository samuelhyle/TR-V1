"""Canonical product research with separate observed and assumption rankings.

This reads one local snapshot. It never promotes asking prices into proceeds,
fills missing costs, or interprets observed listing arrivals as buyer demand.
"""

import copy
import hashlib
import json
import statistics
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from datetime import date, datetime, timezone

from . import analysis, config, db, economics, inventory, metrics, offer_evidence, owned_outcomes, product_identity, research, trades

VERSION = "product-opportunities-v1"
MARKET_FIELDS = ("platform", "subplatform", "category_root_norm", "category_norm",
                 "municipality_norm", "region_norm", "seller_type_norm", "has_shipping")
RANKS = {"margin": ("margin_on_proceeds_pct", False),
         "euros_per_hour": ("economic_profit_per_work_hour_eur", False),
         "speed": ("median_cash_recovery_days", True),
         "quantity": ("recorded_sold_units", False)}
MAX_OWNED = 2000


def _hash(value):
    # SQLite returns integer booleans and text dates; PostgreSQL returns native
    # values. Evidence and row identifiers must survive a database migration.
    def portable(item):
        if isinstance(item, bool):
            return int(item)
        if isinstance(item, datetime):
            if item.tzinfo is None:
                item = item.replace(tzinfo=timezone.utc)
            return item.astimezone(timezone.utc).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Decimal):
            return float(item)
        if isinstance(item, dict):
            result = {}
            for key, value in item.items():
                if isinstance(value, str) and (key.endswith('_at') or key == 'observation_coverage_end'):
                    value = metrics.parse_ts(value) or value
                result[key] = portable(value)
            return result
        if isinstance(item, (list, tuple)):
            return [portable(value) for value in item]
        return item
    return hashlib.sha256(json.dumps(portable(value), sort_keys=True, allow_nan=False).encode()).hexdigest()


def acquisition_scope(row):
    """Freeze actual acquisition geography/delivery, without consulting later rows."""
    return {key: row.get(key) for key in MARKET_FIELDS}


def _key(identity, scope, listing_pk=None):
    return _hash(dict(identity={"unresolved_listing_pk": listing_pk} if listing_pk is not None else identity.get("comparison_key"),
                      policy=identity.get("comparison_policy_version"), market=scope))


def _matches(scope, filters):
    for key, value in filters.items():
        if key == "focus_path":
            # An old frozen decision contains no proof of configured focus membership.
            return False
        if key == "condition_norm":
            continue  # checked against the frozen reviewed offer below
        if scope.get(key) != value:
            return False
    return True


def _amounts(outcome, purchase_pk):
    entries = outcome["financial_entries"]
    if outcome["orders"] or outcome["stock_phase"] == "written_off":
        income = sum(row["amount_minor"] or 0 for row in entries if row["kind"] == "sale_proceeds")
        refunds = sum(row["amount_minor"] or 0 for row in entries if row["kind"] == "refund")
        costs = sum(row["amount_minor"] or 0 for row in entries if row["kind"] in trades.EXPENSES)
        channels = db.query("SELECT DISTINCT channel FROM order_lines WHERE sku=?", (outcome["sku"],))
        return (income-refunds)/100, costs/100, [row["channel"] for row in channels]
    sale = db.query_one("SELECT final_sale_price_eur, platform FROM transactions WHERE purchase_pk=? ORDER BY transaction_pk DESC LIMIT 1", (purchase_pk,))
    if not sale or outcome["cash_contribution_eur"] is None:
        return None, None, []
    proceeds = sale["final_sale_price_eur"]
    return proceeds, proceeds-outcome["cash_contribution_eur"], [sale["platform"]]


def _unit_verified(outcome, identity, scope, cutoff):
    """Use retained pre-sale unit facts/inspection, never today's marketplace label."""
    event = db.query_one("SELECT snapshot_json FROM inventory_events WHERE sku=? AND recorded_at<=? ORDER BY event_pk DESC LIMIT 1",
                         (outcome["sku"], cutoff.isoformat()))
    if not event:
        return False
    unit = json.loads(event["snapshot_json"])
    facts = unit["facts"]
    observed = product_identity.derive(dict(platform=scope["platform"], category_root_norm=scope["category_root_norm"],
                       title=facts.get("model"), model_norm=facts.get("model"), brand_norm=facts.get("brand"),
                       condition_norm=facts.get("condition")))
    if observed["variant_key"] != identity["variant_key"] or facts.get("condition") != identity["attributes"]["condition"]:
        return False
    inspection = db.query_one("SELECT * FROM inspections WHERE sku=? AND facts_version=? AND recorded_at<=? ORDER BY inspection_pk DESC LIMIT 1",
                              (outcome["sku"], unit["facts_version"], cutoff.isoformat()))
    if not inspection or not inspection["competent"] or not inspection["evidence_ref"]:
        return False
    inspected_at = metrics.parse_ts(inspection["recorded_at"])
    if not inspected_at or cutoff-inspected_at > timedelta(days=inventory.INSPECTION_MAX_DAYS):
        return False
    checks = json.loads(inspection["checks_json"])
    required = ["identity", "condition", "completeness"]
    if identity["attributes"].get("battery_health_pct") is not None:
        if facts.get("battery_presence") != "present":
            return False
        required.append("battery")
        if observed["attributes"].get("battery_health_pct") != identity["attributes"]["battery_health_pct"]:
            return False
    return all(checks.get(key, {}).get("outcome") == "pass" for key in required)


def _owned(units, min_n, window_start, reference):
    closed = [u for u in units if u["outcome"]["stock_phase"] in ("sold", "written_off")]
    complete = [u for u in closed if u["outcome"]["cash_contribution_eur"] is not None]
    economic = [u for u in complete if u["outcome"]["economic_profit_eur"] is not None]
    recovered = [u["outcome"]["cash"]["recovery_days"] for u in units
                 if u["outcome"]["cash"]["status"] == "recovered"]
    channels = sorted({channel for u in units for channel in u["channels"]})
    all_complete = bool(units) and len(complete) == len(units)
    verified = all(u["unit_verified"] for u in units)
    supported = all_complete and len(units) >= min_n and len(channels) == 1 and verified
    cash_profit = round(sum(u["outcome"]["cash_contribution_eur"] for u in complete), 2)
    proceeds = sum(u["proceeds"] or 0 for u in complete)
    costs = sum(u["costs"] or 0 for u in complete)
    hours = sum(u["outcome"]["total_work_hours"] for u in economic)
    total_economic = round(sum(u["outcome"]["economic_profit_eur"] for u in economic), 2)
    sales = sum(u["outcome"]["stock_phase"] == "sold" and u["sold_at"] is not None
                and window_start <= u["sold_at"] <= reference for u in units)
    return dict(basis="explicitly_linked_frozen_acquisitions; whole physical units across all cycles",
                n_units=len(units), n_terminal=len(closed), n_pending=len(units)-len(closed),
                n_verified_owned_identity=sum(u["unit_verified"] for u in units),
                n_complete_costs=len(complete), n_complete_economics=len(economic),
                n_cash_recovered=len(recovered), resale_channels=channels,
                complete_cash_contribution_total_eur=cash_profit if complete else None,
                totals_are_partial=not all_complete, actual_cost_total_eur=round(costs, 2) if all_complete else None,
                net_proceeds_total_eur=round(proceeds, 2) if all_complete else None,
                economic_profit_total_eur=total_economic if economic else None,
                margin_on_proceeds_pct=round(cash_profit/proceeds*100, 2) if supported and proceeds > 0 else None,
                roi_on_actual_cost_pct=round(cash_profit/costs*100, 2) if supported and costs > 0 else None,
                economic_profit_per_work_hour_eur=round(total_economic/hours, 2)
                if supported and len(economic) == len(units) and hours > 0 else None,
                median_cash_recovery_days=statistics.median(recovered)
                if len(units) >= min_n and len(recovered) == len(units) and len(channels) == 1 and verified else None,
                recorded_sold_units=sales, quantity_rank_supported=sales >= min_n and len(channels) == 1 and verified,
                unit_refs=[dict(purchase_pk=u["purchase_pk"], decision_pk=u["decision_pk"],
                                attempt_id=u["attempt_id"], stock_phase=u["outcome"]["stock_phase"],
                                cash_status=u["outcome"]["cash"]["status"]) for u in units],
                note="Profit totals cover all linked lifetime units. Partial totals are accounting history. Margin/hour ranks require inspected matching owned variants, every linked unit terminal and complete, minimum support and one observed resale channel. Cash speed includes every unit; unrecovered stock cannot disappear from its denominator. Recorded sold counts do not forecast demand.")


def select_rows(rows, limit=8, exploration=2):
    """A documented evidence order, followed by a reserved research allowance."""
    def order(row):
        owned = row["owned"]
        tier = (0 if owned["margin_on_proceeds_pct"] is not None else
                1 if row["scenario"] is not None else 2 if row["asking_prices"]["status"] == "OK" else
                3 if row["comparison_supported"] else 4)
        return tier, row["row_id"]
    ordered = sorted(rows, key=order)
    supported = [row for row in ordered if row["comparison_supported"]]
    research_rows = [row for row in ordered if not row["comparison_supported"]]
    leaders = []
    for name, (field, ascending) in RANKS.items():
        scored = [r for r in supported if r["owned"][field] is not None and
                  (name != "quantity" or r["owned"]["quantity_rank_supported"])]
        if scored:
            best = min(scored, key=lambda r:(r["owned"][field] if ascending else -r["owned"][field], r["row_id"]))
            if best not in leaders:
                leaders.append(best)
    reserved = min(exploration, len(research_rows), max(0, limit-len(leaders)))
    chosen = leaders[:limit]
    chosen_ids = {r["row_id"] for r in chosen}
    chosen += [r for r in supported if r["row_id"] not in chosen_ids][:max(0, limit-reserved-len(chosen))]
    chosen += research_rows[:reserved]
    chosen_ids = {row["row_id"] for row in chosen}
    chosen += [row for row in ordered if row["row_id"] not in chosen_ids][:limit-len(chosen)]
    return chosen


def board(filters=None, days=30, horizon=30, limit=100, scenarios=None, reference_at=None):
    from .research_desk import _bounded_int, _horizon
    if not isinstance(filters, (dict, type(None))):
        raise ValueError("unsupported board filters")
    supplied = dict(filters or {})
    region = supplied.pop("region_norm", None)
    filters = research.clean_filters(supplied)
    if region is not None:
        region = research._text(region, "region_norm", 300)
        if region:
            filters["region_norm"] = region
    days = _bounded_int(days, "arrival window", 1, 90, 30)
    if days not in (7, 30, 90):
        raise ValueError("arrival window must be 7, 30 or 90 days")
    horizon = _horizon(horizon)
    limit = _bounded_int(limit, "board limit", 1, 200, 100)
    if scenarios is not None and (not isinstance(scenarios, dict) or len(scenarios) > 50):
        raise ValueError("at most 50 explicit row scenarios are supported")
    reference = analysis.comparison_reference(reference_at)
    with db.read_snapshot():
        return _board(filters, days, horizon, limit, scenarios or {}, reference)


def _board(filters, days, horizon, limit, scenarios, reference):
    cutoff = reference-timedelta(hours=config.PANEL_MAX_AGE_HOURS)
    window_start = reference-timedelta(days=days)
    where, params, _ = metrics.listing_filters(**filters)
    rows = db.query("SELECT "+analysis.FIELDS+", has_shipping, region_norm FROM listings WHERE "+" AND ".join(where)+" ORDER BY listing_pk", params)
    types = analysis.accepted_product_types(rows)
    identities = product_identity.resolve_rows(rows, product_types=types)
    admissions = {}
    cohorts, _ = analysis.peer_cohorts(rows, cutoff, types, identities, admissions, priced_only=False)
    eligible = {row["listing_pk"] for members in cohorts.values() for row in members}
    groups = {}

    def ensure(identity, scope, pk=None):
        key = _key(identity, scope, pk)
        groups.setdefault(key, dict(row_id=key, identity=identity, market=scope, members=[], units=[], attempts=[], failed_decisions=set()))
        return groups[key]

    blockers = Counter()
    for row in rows:
        identity = identities[row["listing_pk"]]
        automatic = identity.get("listing_analysis") or {}
        if automatic.get("status") == "current" and not identity.get("review"):
            fields = automatic["fields"]
            manufacturer = fields.get("manufacturer",{}).get("value")
            model = fields.get("model",{}).get("value")
            # Source mentions supply navigation only; they never create a catalog or reviewed key.
            identity = dict(identity,brand=manufacturer,model=model or automatic["profile_label"],
                family=automatic["family"],model_key="source-"+automatic["input_sha256"][:20],
                comparison_key=None,variant_key=None)
        admission = analysis.comparison_admission(row, identity, admissions.get(row["listing_pk"]))
        blockers.update(admission["reasons"])
        if not identity["model_key"] or identity["conflicts"]:
            continue
        accepted = row["listing_pk"] in eligible
        if row["listing_pk"] in admissions:
            continue
        if accepted or analysis.is_fresh(row, cutoff):
            item = ensure(identity, acquisition_scope(row), None if accepted else row["listing_pk"])
            item["members"].append(row)
            item.setdefault("admitted", accepted)

    attempts = db.query("SELECT a.*,d.evidence_json FROM acquisition_attempts a JOIN research_decisions d ON d.decision_pk=a.decision_pk ORDER BY a.attempt_id")
    unattributed = Counter()
    linked = 0
    acquired_decisions = {a["decision_pk"] for a in attempts if a["status"] == "acquired"}
    catalog = product_identity.load()
    for attempt in attempts:
        evidence = json.loads(attempt["evidence_json"])
        contract = evidence.get("decision_contract") or {}
        identity = contract.get("identity") or {}
        scope = contract.get("acquisition_scope")
        if (not scope or not identity.get("comparison_key") or not identity.get("review")
                or contract.get("comparison_policy_version") != product_identity.COMPARISON_POLICY_VERSION):
            unattributed["missing_frozen_scope_or_current_comparison_policy"] += 1
            continue
        current = product_identity._finish(copy.deepcopy(identity), catalog)
        if current["comparison_key"] != identity["comparison_key"]:
            unattributed["changed_identity_policy_semantics"] += 1
            continue
        if not _matches(scope, filters) or (filters.get("condition_norm") and
                                          identity["attributes"]["condition"] != filters["condition_norm"]):
            continue
        item = ensure(identity, scope)
        item["admitted"] = True
        occurred = metrics.parse_ts(attempt["occurred_at"])
        if occurred and window_start <= occurred <= reference:
            item["attempts"].append(attempt)
        if attempt["status"] != "acquired":
            if attempt["decision_pk"] not in acquired_decisions:
                item["failed_decisions"].add(attempt["decision_pk"])
            continue
        linked += 1
        if linked > MAX_OWNED:
            raise ValueError("owned board exceeds 2000 linked units; narrow the market scope")
        outcome = owned_outcomes.report(attempt["purchase_pk"])
        proceeds, costs, channels = _amounts(outcome, attempt["purchase_pk"])
        sold_at = max((metrics.parse_ts(order["sold_at"]) for order in outcome["orders"]), default=None)
        if not outcome["orders"]:
            sale = db.query_one("SELECT sale_at FROM transactions WHERE purchase_pk=? ORDER BY transaction_pk DESC LIMIT 1", (attempt["purchase_pk"],))
            sold_at = metrics.parse_ts(sale["sale_at"]) if sale else None
        item["units"].append(dict(outcome=outcome, proceeds=proceeds, costs=costs, channels=channels,
                                  purchase_pk=attempt["purchase_pk"], decision_pk=attempt["decision_pk"],
                                  attempt_id=attempt["attempt_id"], sold_at=sold_at,
                                  unit_verified=_unit_verified(outcome, identity, scope,
                                      min((metrics.parse_ts(o["sold_at"]) for o in outcome["orders"]), default=sold_at or reference))))
    result_rows = []
    for key, group in groups.items():
        members, identity = group["members"], group["identity"]
        accepted = group.get("admitted", False)
        prices = analysis.price_panel(members if accepted else [], cutoff)
        prices["basis"] = "current asking prices of reviewed offers in this product/market; not realised proceeds"
        liq = metrics.liquidity_rows(members, horizon, filters=group["market"]) if accepted and members else None
        units = _owned(group["units"], config.MIN_N_FOR_REPORT, window_start, reference)
        units["unallocated_failed_decisions"] = len(group["failed_decisions"])
        if group["failed_decisions"]:
            units["economic_profit_per_work_hour_eur"] = None
        reasons = []
        if units["n_verified_owned_identity"] != units["n_units"]:
            reasons.append("owned_variant_or_presale_inspection_missing")
        if group["failed_decisions"]:
            reasons.append("failed_attempt_work_scope_unreviewed")
        if not units["quantity_rank_supported"]:
            reasons.append("owned_sales_quantity_support_missing")
        if not accepted:
            reasons.append("reviewed_identity_or_offer_missing")
        if prices["status"] != "OK":
            reasons.append("insufficient_fresh_asking_prices")
        if units["margin_on_proceeds_pct"] is None:
            reasons.append("complete_owned_margin_support_missing")
        if units["economic_profit_per_work_hour_eur"] is None:
            reasons.append("complete_work_and_cost_support_missing")
        if units["median_cash_recovery_days"] is None:
            reasons.append("all_unit_cash_recovery_support_missing")
        if len(units["resale_channels"]) > 1:
            reasons.append("mixed_resale_channels")
        inputs = dict(market=group["market"], identity=identity,
                      members=[dict(row=row, identity=identities[row["listing_pk"]]) for row in sorted(members, key=lambda r:r["listing_pk"])],
                      attempts=group["attempts"], units=group["units"], min_n=config.MIN_N_FOR_REPORT,
                      days=days, horizon=horizon, max_age_hours=config.PANEL_MAX_AGE_HOURS,
                      freshness_cutoff=cutoff.isoformat(), window_start=window_start.isoformat())
        # Bind time buckets rather than each polling millisecond. Scenarios still
        # recompute admission at the new clock; expiry changes the price sample.
        inputs["freshness_cutoff"] = cutoff.replace(minute=0, second=0, microsecond=0).isoformat()
        inputs["window_start"] = window_start.replace(minute=0, second=0, microsecond=0).isoformat()
        inputs["fresh_priced_pks"] = sorted(row["listing_pk"] for row in members if analysis.is_priced(row, cutoff))
        inputs["arrived_pks"] = sorted(r["listing_pk"] for r in members
                                      if (stamp := metrics.parse_ts(r["first_seen_at"])) and window_start <= stamp <= reference)
        inputs["owned_summary"] = units
        revision = _hash(inputs)
        scenario = None
        if key in scenarios:
            request = scenarios[key]
            if not isinstance(request, dict) or set(request) != {"revision", "inputs", "exit_channel"}:
                raise ValueError("each row scenario needs revision, complete inputs and exit_channel")
            if request["revision"] != revision:
                raise ValueError("opportunity evidence changed; refresh before evaluating the scenario")
            if not accepted:
                raise ValueError("a reviewed comparable product and offer is required for a row scenario")
            if request["exit_channel"] not in trades.CHANNELS:
                raise ValueError("unsupported scenario exit channel")
            if not isinstance(request["inputs"], dict) or set(request["inputs"]) != research.SCENARIO_FIELDS | research.OPERATING_FIELDS:
                raise ValueError("a board scenario requires every cost and operating assumption")
            scenario = research.scenario(request["inputs"])
            sale = economics.amount(request["inputs"]["resale"], "resale")
            downside = sale*(1-economics.amount(request["inputs"]["haircut"], "haircut")/100)
            cost = economics.amount(request["inputs"]["buy"], "buy")+Decimal(str(scenario["results"]["fixed_costs_eur"]))+downside*economics.amount(request["inputs"]["fee"], "fee")/100
            scenario.update(exit_channel=request["exit_channel"], evidence_revision=revision,
                            margin_on_proceeds_pct=round(scenario["results"]["downside_profit_eur"]/float(downside)*100, 2) if downside else None,
                            roi_on_total_cost_pct=round(scenario["results"]["downside_profit_eur"]/float(cost)*100, 2) if cost else None)
        arrivals = [r for r in members if (stamp := metrics.parse_ts(r["first_seen_at"])) and window_start <= stamp <= reference]
        result_rows.append(dict(row_id=key, evidence_revision=revision, product=dict(family=identity["family"],
                 model_key=identity["model_key"], label=(identity["model"] if identity["brand"] and identity["model"]
                    and identity["model"].casefold().startswith(identity["brand"].casefold()+" ") else
                    " ".join(filter(None, [identity["brand"], identity["model"]]))),
                 variant_key=identity["variant_key"], comparison_key=identity["comparison_key"], attributes=identity["attributes"],
                 search_terms=[" ".join(filter(None, [identity["brand"], identity["model"]]))],
                 missing_identity=identity["missing_variant_fields"]+identity["missing_comparison_fields"],
                 missing_physical_checks=identity["missing_physical_checks"]),
                 market=group["market"], comparison_supported=accepted,
                 comparison_policy_version=product_identity.COMPARISON_POLICY_VERSION,
                 offer_policy_version=offer_evidence.VERSION, asking_prices=prices,
                 public_sale_evidence=liq, owned=units, scenario=scenario, buy_ceiling_eur=scenario["results"]["max_buy_eur"] if scenario else None,
                 quantity=dict(window_days=days, unique_retained_offer_arrivals=len(arrivals),
                               attempt_counts=dict(Counter(a["status"] for a in group["attempts"])),
                               available_observations=sum(analysis.is_fresh(r, cutoff) for r in members),
                               observed_physical_sales=units["recorded_sold_units"], observed_stock=units["n_pending"],
                               basis="Retained deduplicated listing observations and explicitly linked owned units; public physical quantities and buyer demand unknown"),
                 source_refs=[dict(listing_pk=r["listing_pk"], platform=r["platform"], listing_id=r["listing_id"],
                                   title=r["title"], ask_eur=analysis.amount(r["listed_price_eur"]) if analysis.is_priced(r, cutoff) else None,
                                   price_observed_at=r["price_observed_at"],
                                   listing_analysis=identities[r["listing_pk"]].get("listing_analysis"),
                                   url=research.market_url(r["platform"], r["listing_id"])) for r in sorted(members, key=lambda r:r["listing_pk"])[:8]],
                 source_ref_coverage=dict(total=len(members), limit=8, truncated=len(members)>8),
                 abstentions=reasons, next_action="Record full resale/cost assumptions and inspect the unit" if accepted else "Review the exact variant and single-unit offer"))
    if set(scenarios)-{row["row_id"] for row in result_rows}:
        raise ValueError("scenario product is outside the current board scope")
    ranked = {}
    for name, (field, ascending) in RANKS.items():
        supported = [r for r in result_rows if r["owned"][field] is not None
                     and (name != "quantity" or r["owned"]["quantity_rank_supported"])]
        supported.sort(key=lambda r: (r["owned"][field] if ascending else -r["owned"][field], r["row_id"]))
        ranked[name] = [dict(row_id=r["row_id"], value=r["owned"][field], n_units=r["owned"]["n_units"],
                             evidence_revision=r["evidence_revision"], basis="observed_owned") for r in supported]
    scenario_fields = {"margin":"margin_on_proceeds_pct", "euros_per_hour":"profit_per_labour_hour_eur",
                       "quantity":"units_per_month", "monthly_profit":"monthly_profit_eur"}
    scenario_ranks = {}
    for name, field in scenario_fields.items():
        scored = [(r, r["scenario"].get(field, r["scenario"]["operating"].get(field))) for r in result_rows if r["scenario"]]
        scored = [(r, v) for r,v in scored if v is not None]
        scenario_ranks[name] = [dict(row_id=r["row_id"], value=v, basis="user_assumptions", evidence_revision=r["evidence_revision"])
                                for r,v in sorted(scored, key=lambda pair:(-pair[1], pair[0]["row_id"]))]
    ordered = select_rows(result_rows, limit, exploration=min(2, limit))
    visible = {r["row_id"] for r in ordered}
    return dict(version=VERSION, generated_at=reference.isoformat(), filters=filters, days=days, horizon=horizon,
                min_n=config.MIN_N_FOR_REPORT, max_age_hours=config.PANEL_MAX_AGE_HOURS,
                total_rows=len(result_rows), rows=ordered, truncated=len(result_rows)>limit,
                rankings={k:[r for r in ranks if r["row_id"] in visible] for k,ranks in ranked.items()},
                scenario_rankings={k:[r for r in ranks if r["row_id"] in visible] for k,ranks in scenario_ranks.items()},
                coverage=dict(selected_observations=len(rows), comparable_observations=len(eligible),
                              blockers=dict(blockers), unattributed_attempts=dict(unattributed),
                              unlinked_owned_units=db.scalar("SELECT COUNT(*) FROM purchases WHERE purchase_pk NOT IN (SELECT purchase_pk FROM acquisition_attempts WHERE purchase_pk IS NOT NULL)")),
                selection_basis="Observed objective leaders, then complete owned evidence, explicit scenarios, supported asks and reviewed thin groups; up to two research rows reserved. Stable row IDs break ties. Ranks compare separate objectives; no weighted score.",
                note="Asks are supply context. Marker incidence is separate from actual cash recovery. Scenario quantities and monthly profits are single-product steady-state assumptions with shared resources; they cannot be added across products.")


def render(report):
    lines = ["Product opportunity board", report["note"], f"{report['total_rows']} product/market rows; {report['min_n']} observations required for supported ranks"]
    for row in report["rows"]:
        lines += ["", row["product"]["label"], "row_id: "+row["row_id"], "revision: "+row["evidence_revision"],
                  "market: "+json.dumps(row["market"], ensure_ascii=False),
                  "buy ceiling: "+(str(row["buy_ceiling_eur"])+" EUR (user scenario)" if row["scenario"] else "unknown; explicit complete scenario required"),
                  "next: "+row["next_action"], "missing: "+", ".join(row["abstentions"])]
    return "\n".join(lines)
