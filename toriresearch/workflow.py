"""Read-only pilot review of owned evidence, independent of market estimates.

Counts refer to physical SKUs. Returns and repeated decisions never multiply
the denominator. Duration is acquisition to final observed outcome, not bank
capital duration or human review effort. No external actions are performed.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median

from . import acquisition_evidence, config, db, drafts, inventory, metrics, owned_outcomes, trades


def _decisions(purchase, rows, now, attribution=None):
    start = metrics.parse_ts(purchase["purchased_at"])
    eligible, retrospective, invalid = [], [], []
    for row in rows:
        recorded = metrics.parse_ts(row["recorded_at"])
        if not start or start > now or not recorded or recorded > now:
            invalid.append(row)
        elif recorded <= start:
            eligible.append(row)
        else:
            retrospective.append(row)
    # A prior decision is context until a matching receipt attributes it.
    eligible.sort(key=lambda row: (metrics.parse_ts(row["recorded_at"]), row["decision_pk"]), reverse=True)
    context = eligible[0] if eligible else None
    attribution = attribution or {}
    explicit = attribution.get("status") == "explicit"
    return {"status": "pre_acquisition" if explicit else "invalid_acquisition_link" if attribution else
            "pre_acquisition_context" if context else "timing_unknown" if invalid else
            "retrospective_only" if retrospective else "not_recorded",
            "anchor": attribution["decision"] if explicit else None,
            "explicit_acquisition_link": explicit,
            "attempt_id": attribution.get("attempt_id"),
            "reason": attribution.get("reason") or ("Prior decisions are context; no explicit acquisition receipt is recorded" if context else None),
            "context": {key: context[key] for key in ("decision_pk", "decision", "recorded_at")} if context else None,
            "n_entries": len(rows), "n_retrospective": len(retrospective), "n_invalid_timing": len(invalid)}


def _duration(purchase, economics, events, now):
    if not economics["complete"]:
        return None
    order = economics["current_order"]
    end = order.get("delivered_at") if economics["stock_phase"] == "sold" else next(
        (event["effective_at"] for event in reversed(events) if event["event_type"] == "stock_written_off"), None)
    # A later return settlement is part of this physical unit's outcome.
    dates = [metrics.parse_ts(end)]
    dates.extend(metrics.parse_ts(event["effective_at"]) for event in events if event["event_type"] == "return_closed")
    start = metrics.parse_ts(purchase["purchased_at"])
    if not start or any(value is None for value in dates):
        return None
    end = max(dates)
    return round((end - start).total_seconds() / 86400, 2) if start <= end <= now else None


def _next(unit, economics, tori_draft, active_cases, reserved, outcome=None):
    order_id = economics["current_order"]["order_id"] if economics and economics["current_order"] else None
    def step(label, view, reasons=(), panel=None):
        return {"label": label, "view": view, "sku": unit["sku"], "order_id": order_id,
                "draft_id": tori_draft["draft_id"] if tori_draft else None, "reasons": list(reasons), "panel": panel}
    evidence = owned_outcomes.evidence_steps(outcome) if outcome else []
    if reserved:
        return step("Reconcile reserved channel stock", "drafts", ["An existing publication reservation holds this unit."])
    if active_cases:
        return step("Resolve the recorded return / claim", "orders", ["Open cases keep final profit pending."])
    if economics and economics["stock_phase"] == "returned_review":
        preview = inventory.preview({**unit, "state": "in_stock", "has_recorded_outcome": False}, unit["latest_inspection"], "local_draft")
        if preview["status"] != "allowed":
            return step("Inspect the returned physical unit", "inventory", [row["message"] for row in preview["reasons"]])
        return step("Release the inspected return for resale", "orders")
    if (economics and economics["stock_phase"] in ("sold", "written_off")) or (outcome and outcome["stock_phase"] == "written_off"):
        if economics and economics["unknown_costs"] and any(kind not in trades.OPERATING_COSTS for kind in economics["unknown_costs"]):
            return step("Resolve unknown trade costs", "orders", economics["reasons"])
        if economics and not economics["complete"] and not economics["unknown_costs"]:
            return step("Record delivery and settle the lifecycle", "orders", economics["reasons"])
        if evidence:
            remedy = evidence[0]
            return step(remedy["label"], "inventory", remedy["reasons"], remedy["panel"])
        return step("Review the completed net result", "inventory", panel="review")
    if unit["has_recorded_outcome"]:
        return step("Adopt and review the existing ledger outcome", "orders")
    if unit["local_preview"]["status"] != "allowed":
        return step("Review owned facts and physical inspection", "inventory",
                    [row["message"] for row in unit["local_preview"]["reasons"]])
    if evidence and evidence[0]["panel"] == "receipt":
        return step(evidence[0]["label"], "inventory", evidence[0]["reasons"], "receipt")
    if not tori_draft:
        return step("Prepare a Tori listing draft", "drafts")
    if not tori_draft["review"]["ready"] or not any(row["current"] for row in tori_draft["approvals"]):
        return step("Review the saved Tori draft", "drafts", tori_draft["review"]["reasons"])
    return step("Download reviewed text for manual Tori entry", "drafts",
                ["Publication and buyer payment still need actual evidence."])


def report(page=1, sku=""):
    try:
        valid_page = not isinstance(page, bool) and str(page) == str(int(page)) and int(page) > 0
    except (ValueError, TypeError, OverflowError):
        valid_page = False
    if not valid_page:
        raise ValueError("page must be a positive integer")
    sku = inventory._text(sku, "SKU", 64)
    now = datetime.now(timezone.utc)
    with db.read_snapshot():
        conn = db.connect()
        units = db.query("SELECT u.sku, u.title, u.kind, u.state, u.purchase_pk, p.platform, p.source_listing_id, p.purchased_at "
                         "FROM inventory_units u JOIN purchases p ON p.purchase_pk=u.purchase_pk ORDER BY u.updated_at DESC, u.sku")
        if sku and not any(unit["sku"] == sku for unit in units):
            raise ValueError("owned SKU not found")
        decisions = defaultdict(list)
        for row in db.query("SELECT d.decision_pk, d.platform, d.source_listing_id, d.decision, d.recorded_at FROM research_decisions d "
                            "WHERE EXISTS (SELECT 1 FROM purchases p JOIN inventory_units u ON u.purchase_pk=p.purchase_pk "
                            "WHERE p.platform=d.platform AND p.source_listing_id=d.source_listing_id)"):
            decisions[(row["platform"], row["source_listing_id"])].append(row)
        orders = defaultdict(list)
        for row in db.query("SELECT order_id, sku, cycle, channel FROM order_lines ORDER BY cycle"):
            orders[row["sku"]].append(row)
        cases = defaultdict(list)
        for row in db.query("SELECT c.*, o.sku FROM return_cases c JOIN order_lines o ON o.order_id=c.order_id"):
            cases[row["sku"]].append(row)
        events = defaultdict(list)
        for row in db.query("SELECT e.event_type, e.effective_at, o.sku FROM order_events e JOIN order_lines o ON o.order_id=e.order_id ORDER BY e.event_pk"):
            events[row["sku"]].append(row)
        reserved = {row["sku"] for row in db.query("SELECT sku FROM inventory_reservations")}
        draft_ids = {}
        for row in db.query("SELECT draft_id, sku FROM listing_drafts WHERE channel='tori' ORDER BY updated_at, draft_id"):
            draft_ids[row["sku"]] = row["draft_id"]
        anchors, economics, durations, settled, reason_counts = {}, {}, [], [], Counter()
        attribution = acquisition_evidence.load([unit["purchase_pk"] for unit in units], now=now)
        for unit in units:
            anchors[unit["sku"]] = _decisions(unit, decisions[(unit["platform"], unit["source_listing_id"])], now,
                                             attribution.get(unit["purchase_pk"]))
            reason_counts[anchors[unit["sku"]]["status"]] += 1
            if orders[unit["sku"]]:
                e = trades.economics(unit["sku"], conn=conn)
                economics[unit["sku"]] = e
                if e["complete"]:
                    settled.append(e)
                    duration = _duration(unit, e, events[unit["sku"]], now)
                    if duration is not None:
                        durations.append(duration)
        losses = sum(e["net_profit_eur"] < 0 for e in settled)
        claim_types = Counter(case["case_type"] for group in cases.values() for case in group)
        case_units = sum(bool(group) for group in cases.values())
        threshold = config.MIN_N_FOR_REPORT
        eligible = [unit for unit in units if not sku or unit["sku"] == sku]
        pages = max(1, (len(eligible) + 24) // 25)
        page = min(int(page), pages)
        items = []
        for row in eligible[(page - 1) * 25:page * 25]:
            unit = inventory.get(row["sku"])
            draft = drafts.get(draft_ids[row["sku"]]) if row["sku"] in draft_ids else None
            e = economics.get(row["sku"])
            active = [case for case in cases[row["sku"]] if case["state"] != "closed"]
            outcome = owned_outcomes._report(conn, row["purchase_pk"])
            items.append({"sku": row["sku"], "title": row["title"], "kind": row["kind"],
                          "source": {"platform": row["platform"], "listing_id": row["source_listing_id"]},
                          "purchase_pk": row["purchase_pk"], "research": anchors[row["sku"]],
                          "inspection": unit["local_preview"]["status"], "stock_phase": unit["stock_phase"],
                          "n_sale_cycles": len(orders[row["sku"]]), "n_open_cases": len(active),
                          "net_profit_eur": e["net_profit_eur"] if e else None,
                          "owned_cash_contribution_eur": outcome["cash_contribution_eur"],
                          "outcome_scope_reviewed": outcome["time_scope_complete"],
                          "duration_days": _duration(row, e, events[row["sku"]], now) if e else None,
                          "evidence_steps": owned_outcomes.evidence_steps(outcome),
                          "next_step": _next(unit, e, draft, active, row["sku"] in reserved, outcome)})
        summary = {"owned_units": len(units), "research_links": dict(reason_counts),
                   "units_with_sku_sales": len(economics), "sale_cycles": sum(len(group) for group in orders.values()),
                   "legacy_outcome_units": db.scalar("SELECT COUNT(*) FROM inventory_units u WHERE EXISTS "
                       "(SELECT 1 FROM transactions t WHERE t.purchase_pk=u.purchase_pk) AND NOT EXISTS "
                       "(SELECT 1 FROM order_lines o WHERE o.sku=u.sku)"),
                   "settled_units": len(settled), "unsettled_traded_units": len(economics)-len(settled),
                   "unknown_cost_units": sum(bool(e["unknown_costs"]) for e in economics.values()),
                   "open_cases": sum(case["state"] != "closed" for group in cases.values() for case in group),
                   "claim_cases": dict(claim_types), "units_with_claims": case_units,
                   "loss_units": losses, "net_profit_eur": round(sum(e["net_profit_eur"] for e in settled), 2) if settled else None,
                   "profit_is_partial": len(settled) < len(economics),
                   "loss_rate": losses/len(settled) if len(settled) >= threshold else None,
                   "duration_n": len(durations), "median_duration_days": round(median(durations), 2) if len(durations) >= threshold else None,
                   "min_n": threshold, "tori_draft_units": len(draft_ids), "reserved_units": len(reserved)}
    return {"version": "owned-pilot-review-v2", "generated_at": now.isoformat(), "summary": summary,
            "items": items, "page": page, "pages": pages, "total": len(eligible),
            "scope": "Entire owned inventory; financial totals cover the managed SKU journal. Filters affect the table only.",
            "limits": ["One physical SKU contributes once to loss and duration denominators, including all its sale cycles.",
                       "An acquisition receipt selects its decision after source and chronology checks. Other prior decisions remain context; late decisions cannot supply forward evidence.",
                       "Outcome duration includes delivery/write-off and recorded claim settlement; it is not bank capital duration.",
                       "Elapsed workflow time is not human review effort. Draft factual errors and unsafe approvals require labelled human evaluation.",
                       "Unknown costs and unsettled outcomes are excluded from complete profit, never treated as zero.",
                       "Loss rates and duration medians need the stated support threshold and are descriptions of this owned sample.",
                       "This review does not validate predictions or authorize unattended trading."],
            "automation": {"status": "not_validated", "external_actions_enabled": False}}
