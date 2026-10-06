"""Observed acquisition, work and cash evidence on the existing owned ledger.

Sale proceeds, acquisition costs and refunds remain in purchases/trades. Cash
receipts are informational financial entries, never additional profit. Operator
time is an economic cost; paid labour is a separately evidenced cash expense.
"""
import inspect
import json
import uuid
from decimal import Decimal

from . import acquisition_evidence, db, inventory, metrics, purchases, trades

ACTIVITIES = {"search", "travel", "inspect", "repair", "photo", "list", "message", "fulfil"}
VERSION = "owned-outcomes-v1"


def _purchase(conn, purchase_pk, lock=False):
    inventory._integer(purchase_pk, "purchase ID")
    row = conn.execute("SELECT p.*, u.sku FROM purchases p LEFT JOIN inventory_units u ON u.purchase_pk=p.purchase_pk WHERE p.purchase_pk=?", (purchase_pk,)).fetchone()
    if not row:
        raise ValueError("purchase not found")
    result = dict(row)
    if lock and result["sku"]:
        inventory._unit(conn, result["sku"], lock=True)
    return result


def _decision(conn, decision_pk):
    inventory._integer(decision_pk, "decision ID")
    row = conn.execute("SELECT * FROM research_decisions WHERE decision_pk=?", (decision_pk,)).fetchone()
    if not row:
        raise ValueError("research decision not found")
    return dict(row)


def _mutate(client_token, name, payload, evidence_ref, occurred_on, apply):
    token = inventory._token(client_token)
    evidence = inventory._text(evidence_ref, "owned evidence reference", 500, True)
    effective = trades._stamp(occurred_on)
    digest = inventory._hash(dict(action=name, payload=payload, evidence=evidence, occurred_on=occurred_on))
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "owned-outcome:" + token)
            previous = conn.execute("SELECT request_sha256, payload_json, event_pk FROM owned_outcome_events WHERE client_token=?", (token,)).fetchone()
            if previous:
                if previous["request_sha256"] != digest:
                    raise inventory.Conflict("This receipt token was used for a different owned event.")
                return {**json.loads(previous["payload_json"])["result"], "event_pk": previous["event_pk"], "replayed": True}
            result = apply(conn, effective)
            data = dict(payload, result=result)
            row = conn.execute("INSERT INTO owned_outcome_events (client_token, request_sha256, purchase_pk, decision_pk, event_type, payload_json, evidence_ref, effective_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING event_pk",
                (token, digest, payload.get("purchase_pk"), payload.get("decision_pk"), name, inventory._json(data), evidence, effective, inventory._now().isoformat())).fetchone()
            return {**result, "event_pk": row[0], "replayed": False}
    finally:
        conn.close()


def attempt(client_token, decision_pk, status, reason, evidence_ref, purchase_pk=None, occurred_on=None):
    """One completed attempt per receipt. Multiple failed attempts stay separate."""
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            return _attempt(conn, client_token, decision_pk, status, reason, evidence_ref, purchase_pk, occurred_on)
    finally:
        conn.close()


def _attempt_receipt(row):
    return {key: row[key] for key in ("attempt_id", "decision_pk", "purchase_pk", "status", "reason",
                                     "evidence_ref", "occurred_at", "recorded_at")}


def _attempt(conn, client_token, decision_pk, status, reason, evidence_ref, purchase_pk=None, occurred_on=None):
    """Connection-bound writer, also used by the atomic inventory acquisition."""
    token = inventory._token(client_token)
    reason = inventory._text(reason, "attempt reason", 1000, True)
    evidence_ref = inventory._text(evidence_ref, "attempt evidence", 500, True)
    if status not in ("acquired", "declined", "unavailable", "failed"):
        raise ValueError("unsupported acquisition attempt status")
    if (status == "acquired") != (purchase_pk is not None):
        raise ValueError("only an acquired attempt requires an actual purchase ID")
    effective = trades._stamp(occurred_on)
    digest = inventory._hash(dict(decision_pk=decision_pk, status=status, reason=reason, evidence_ref=evidence_ref, purchase_pk=purchase_pk, occurred_on=occurred_on))
    inventory._token_lock(conn, "acquisition-attempt:" + token)
    previous = conn.execute("SELECT * FROM acquisition_attempts WHERE client_token=?", (token,)).fetchone()
    if previous:
        if previous["request_sha256"] != digest:
            raise inventory.Conflict("This attempt token was used for a different attempt.")
        return {"attempt_id": previous["attempt_id"], "receipt": _attempt_receipt(previous), "replayed": True}
    decision = _decision(conn, decision_pk)
    trades._after(effective, decision["recorded_at"], "acquisition attempt")
    if purchase_pk is not None:
        purchase = _purchase(conn, purchase_pk, lock=True)
        if not purchase["sku"] or (purchase["platform"], purchase["source_listing_id"]) != (decision["platform"], decision["source_listing_id"]):
            raise ValueError("successful attempt requires the matching received purchase and owned SKU")
        # Attribution requires precise forward chronology, even on the same day.
        if not trades._dt(decision["recorded_at"]) <= trades._dt(purchase["purchased_at"]) <= trades._dt(effective):
            raise ValueError("received acquisition must follow its decision and purchase timestamps")
        if conn.execute("SELECT 1 FROM acquisition_attempts WHERE purchase_pk=?", (purchase_pk,)).fetchone():
            raise inventory.Conflict("This physical acquisition already has a successful attempt.")
    attempt_id = uuid.uuid4().hex
    conn.execute("INSERT INTO acquisition_attempts (attempt_id,client_token,request_sha256,decision_pk,purchase_pk,status,reason,evidence_ref,occurred_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, token, digest, decision_pk, purchase_pk, status, reason, evidence_ref, effective, inventory._now().isoformat()))
    row = conn.execute("SELECT * FROM acquisition_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    return {"attempt_id": attempt_id, "receipt": _attempt_receipt(row), "replayed": False}


def record_time(client_token, activity, minutes, evidence_ref, purchase_pk=None, decision_pk=None,
                worker="operator", hourly_value_eur=None, occurred_on=None, supersedes_event_pk=None):
    if activity not in ACTIVITIES or worker not in ("operator", "paid"):
        raise ValueError("unsupported work activity or worker")
    if type(minutes) is not int or not 0 <= minutes <= 1000000:
        raise ValueError("minutes must be a nonnegative integer")
    if (purchase_pk is None) == (decision_pk is None):
        raise ValueError("work needs exactly one purchase or research decision")
    rate = trades._money(hourly_value_eur, "hourly value")
    if worker == "paid" and rate is not None:
        raise ValueError("paid worker time is tracked here; record actual cash wages with operating_cost")
    payload = dict(purchase_pk=purchase_pk, decision_pk=decision_pk, activity=activity, minutes=minutes,
                   worker=worker, hourly_value_minor=rate, supersedes_event_pk=supersedes_event_pk)
    def apply(conn, effective):
        _purchase(conn, purchase_pk, lock=True) if purchase_pk is not None else _decision(conn, decision_pk)
        if decision_pk is not None:
            inventory._token_lock(conn, "work-decision:" + str(decision_pk))
        if supersedes_event_pk is not None:
            inventory._integer(supersedes_event_pk, "superseded work event")
            previous = conn.execute("SELECT * FROM owned_outcome_events WHERE event_pk=? AND event_type='work'", (supersedes_event_pk,)).fetchone()
            if not previous or (previous["purchase_pk"], previous["decision_pk"]) != (purchase_pk, decision_pk):
                raise ValueError("work correction must match its original owner")
            prior_payload = json.loads(previous["payload_json"])
            if prior_payload["activity"] != activity or prior_payload["worker"] != worker:
                raise ValueError("work correction must match its activity and worker")
            for row in conn.execute("SELECT payload_json FROM owned_outcome_events WHERE event_type='work'").fetchall():
                if json.loads(row[0]).get("supersedes_event_pk") == supersedes_event_pk:
                    raise inventory.Conflict("This work event was already corrected.")
        return {"activity": activity, "minutes": minutes}
    return _mutate(client_token, "work", payload, evidence_ref, occurred_on, apply)


def operating_cost(client_token, purchase_pk, kind, amount_eur, reason, evidence_ref,
                   occurred_on=None, supersedes_id=None):
    """Whole-unit totals outside existing sale costs, including all return cycles.

    Correct the current total with supersedes_id. Never record costs here that
    are already included in sale-cycle other_variable_cost_eur or another entry.
    """
    if kind not in trades.OPERATING_COSTS:
        raise ValueError("unsupported operating cost")
    amount = trades._money(amount_eur, kind)
    reason = inventory._text(reason, "cost scope and allocation", 1000, True)
    payload = dict(purchase_pk=purchase_pk, kind=kind, amount_minor=amount, reason=reason, supersedes_id=supersedes_id)
    def apply(conn, effective):
        purchase = _purchase(conn, purchase_pk, lock=True)
        if not purchase["sku"]:
            raise ValueError("link this purchase to an owned SKU before recording operating costs")
        active = [row for row in trades._tips(conn, purchase["sku"]) if row["kind"] == kind]
        if active and (len(active) != 1 or active[0]["entry_id"] != supersedes_id) or not active and supersedes_id:
            raise inventory.Conflict("Correct the current whole-unit cost entry using its supersedes_id.")
        entry_id = trades._entry(conn, purchase["sku"], kind, amount, effective, evidence_ref, reason, supersedes=supersedes_id)
        return {"entry_id": entry_id}
    return _mutate(client_token, "operating_cost", payload, evidence_ref, occurred_on, apply)


def cash(client_token, purchase_pk, kind, amount_eur, evidence_ref, external_event_ref,
         occurred_on, order_id=None, supersedes_id=None):
    if kind not in trades.CASH_RECEIPTS:
        raise ValueError("cash kind must be cash_committed, usable_payout or payout_reversal")
    amount = trades._money(amount_eur, kind, False)
    external_event_ref = inventory._text(external_event_ref, "cash receipt reference", 100, True)
    payload = dict(purchase_pk=purchase_pk, kind=kind, amount_minor=amount, order_id=order_id,
                   external_event_ref=external_event_ref, supersedes_id=supersedes_id)
    def apply(conn, effective):
        purchase = _purchase(conn, purchase_pk, lock=True)
        if not purchase["sku"]:
            raise ValueError("cash evidence requires an owned SKU")
        entries = trades._tips(conn, purchase["sku"])
        history = [dict(row) for row in conn.execute("SELECT * FROM financial_entries WHERE sku=?", (purchase["sku"],)).fetchall()]
        ancestry, ancestor = set(), supersedes_id
        while ancestor:
            ancestry.add(ancestor)
            parent = next((row for row in history if row["entry_id"] == ancestor), None)
            ancestor = parent["supersedes_id"] if parent else None
        references = [row for row in history if row["external_event_ref"] == external_event_ref]
        if any(row["entry_id"] not in ancestry for row in references):
            raise inventory.Conflict("This cash receipt reference is already recorded for this unit.")
        if supersedes_id:
            previous = next((row for row in entries if row["entry_id"] == supersedes_id), None)
            if not previous or previous["kind"] != kind or previous["order_id"] != order_id:
                raise ValueError("cash correction must match the current entry, kind and order")
        if kind != "cash_committed":
            if order_id:
                order = trades._order(conn, order_id)
                if order["sku"] != purchase["sku"]:
                    raise ValueError("payout order belongs to another unit")
                trades._after(effective, order["sold_at"], "usable payout or reversal")
            else:
                legacy = conn.execute("SELECT sale_at FROM transactions WHERE purchase_pk=? AND outcome_status='sold_confirmed'", (purchase_pk,)).fetchone()
                if not legacy or conn.execute("SELECT 1 FROM order_lines WHERE sku=?", (purchase["sku"],)).fetchone():
                    raise ValueError("payout requires its owned order or a verified legacy sale")
                trades._after(effective, legacy[0], "usable payout or reversal")
        if kind == "payout_reversal":
            balance = sum((row["amount_minor"] or 0) * (1 if row["kind"] == "usable_payout" else -1) for row in entries if row["kind"] in {"usable_payout", "payout_reversal"} and row["order_id"] == order_id and row["entry_id"] != supersedes_id and trades._dt(row["effective_at"]) <= trades._dt(effective))
            if amount > balance:
                raise ValueError("cash reversal cannot exceed this order's usable payouts")
        if kind == "usable_payout":
            projected = amount + sum((row["amount_minor"] or 0) * (1 if row["kind"] == "usable_payout" else -1) for row in entries if row["kind"] in {"usable_payout", "payout_reversal"} and row["order_id"] == order_id and row["entry_id"] != supersedes_id)
            if projected < 0:
                raise ValueError("payout correction would leave reversals above usable payouts")
        entry_id = trades._entry(conn, purchase["sku"], kind, amount, effective, evidence_ref,
                                 "Observed cash movement; excluded from profit arithmetic", order_id=order_id,
                                 supersedes=supersedes_id, external_ref=None if references else external_event_ref)
        return {"entry_id": entry_id}
    return _mutate(client_token, "cash", payload, evidence_ref, occurred_on, apply)


def _events(conn, purchase_pk):
    return [dict(row, payload=json.loads(row["payload_json"])) for row in conn.execute(
        "SELECT * FROM owned_outcome_events WHERE purchase_pk=? OR decision_pk IN (SELECT decision_pk FROM acquisition_attempts WHERE purchase_pk=?) ORDER BY event_pk", (purchase_pk, purchase_pk)).fetchall()]


def correct_cost(client_token, purchase_pk, supersedes_id, amount_eur, reason, evidence_ref, occurred_on=None):
    """Correct an original cost on an unsold write-off; no fabricated order."""
    amount = trades._money(amount_eur, "corrected cost")
    reason = inventory._text(reason, "cost correction reason", 1000, True)
    payload = dict(purchase_pk=purchase_pk, supersedes_id=supersedes_id, amount_minor=amount, reason=reason)
    def apply(conn, effective):
        purchase = _purchase(conn, purchase_pk, lock=True)
        if not purchase["sku"]:
            raise ValueError("cost correction requires an owned SKU")
        entries = trades._tips(conn, purchase["sku"])
        if not any(row["kind"] == "write_off" and row["order_id"] is None for row in entries):
            raise ValueError("use the existing trade financial journal for other settled units")
        previous = next((row for row in entries if row["entry_id"] == supersedes_id), None)
        if not previous or previous["kind"] not in set(trades.ACQUISITION_COSTS) | {"disposal"} or previous["order_id"] is not None:
            raise ValueError("correction requires the current original acquisition or disposal entry")
        trades._after(effective, purchase["purchased_at"], "cost correction")
        entry_id = trades._entry(conn, purchase["sku"], previous["kind"], amount, effective, evidence_ref, reason, supersedes=supersedes_id)
        return {"entry_id": entry_id}
    return _mutate(client_token, "cost_correction", payload, evidence_ref, occurred_on, apply)


def _work(events):
    rows = [row for row in events if row["event_type"] == "work"]
    replaced = {row["payload"].get("supersedes_event_pk") for row in rows}
    return [row for row in rows if row["event_pk"] not in replaced]


def _fingerprint(entries, work):
    return inventory._hash(dict(entries=entries, work=[row["event_pk"] for row in work]))


def _scope_hash(conn, purchase, entries, work):
    # Legacy corrections and purchase corrections also invalidate completeness.
    ledger = dict(purchase={key: str(purchase[key]) for key in ("purchased_at", "acquisition_cost_eur", "shipping_cost_eur", "buyer_fee_eur")},
                  transactions=[dict(row) for row in conn.execute("SELECT * FROM transactions WHERE purchase_pk=? ORDER BY transaction_pk", (purchase["purchase_pk"],)).fetchall()],
                  orders=[dict(row) for row in conn.execute("SELECT order_id,version,state,fulfilment FROM order_lines WHERE sku=? ORDER BY cycle", (purchase["sku"],)).fetchall()])
    return inventory._hash(dict(ledger=ledger, activity=_fingerprint(entries, work)))


def review_scope(client_token, purchase_pk, evidence_ref, confirm_time_complete, confirm_cash_complete=False, expected_scope_sha256=None):
    if confirm_time_complete is not True or type(confirm_cash_complete) is not bool:
        raise ValueError("explicit complete work-time review is required")
    payload = dict(purchase_pk=purchase_pk, confirm_time_complete=True, confirm_cash_complete=confirm_cash_complete)
    if expected_scope_sha256 is not None:
        payload["expected_scope_sha256"] = inventory._text(expected_scope_sha256, "reviewed scope fingerprint", 64, True)
    def apply(conn, effective):
        purchase = _purchase(conn, purchase_pk, lock=True)
        entries = trades._tips(conn, purchase["sku"]) if purchase["sku"] else []
        current = _scope_hash(conn, purchase, entries, _work(_events(conn, purchase_pk)))
        if expected_scope_sha256 is not None and expected_scope_sha256 != current:
            raise inventory.Conflict("Costs, work or cash changed since this preview. Reload and review the current evidence.")
        return {"scope_sha256": current}
    return _mutate(client_token, "scope_review", payload, evidence_ref, None, apply)


def write_off_unsold(client_token, purchase_pk, expected_unit_version, reason, evidence_ref,
                     disposal_cost_eur=None, occurred_on=None):
    reason = inventory._text(reason, "write-off evidence and reason", 1000, True)
    disposal = trades._money(disposal_cost_eur, "actual disposal cost")
    payload = dict(purchase_pk=purchase_pk, expected_unit_version=expected_unit_version,
                   reason=reason, disposal_cost_minor=disposal)
    def apply(conn, effective):
        purchase = _purchase(conn, purchase_pk, lock=True)
        if not purchase["sku"]:
            raise ValueError("write-off requires an owned SKU")
        unit = inventory._unit(conn, purchase["sku"], lock=True)
        inventory._version(unit, expected_unit_version)
        inventory._unreserved(conn, unit["sku"])
        if unit["has_recorded_outcome"] or conn.execute("SELECT 1 FROM order_lines WHERE sku=?", (unit["sku"],)).fetchone():
            raise inventory.Conflict("Use the existing order's return/write-off workflow for a sold unit.")
        trades._after(effective, purchase["purchased_at"], "write-off")
        for kind, field in (("acquisition_cost_eur", "acquisition_cost_eur"), ("inbound_shipping_cost_eur", "shipping_cost_eur"), ("buyer_fee_eur", "buyer_fee_eur")):
            trades._entry(conn, unit["sku"], kind, trades._money(purchase[field]), purchase["purchased_at"], evidence_ref, "Original acquisition on unsold write-off")
        trades._entry(conn, unit["sku"], "write_off", 0, effective, evidence_ref, reason)
        trades._entry(conn, unit["sku"], "disposal", disposal, effective, evidence_ref, "Actual disposal expense; no forecast reserve")
        conn.execute("UPDATE inventory_units SET state='retired',version=version+1,updated_at=? WHERE sku=?", (inventory._now().isoformat(), unit["sku"]))
        inventory._event(conn, inventory._unit(conn, unit["sku"]), "written_off", reason)
        return {"sku": unit["sku"], "version": expected_unit_version + 1}
    return _mutate(client_token, "unsold_write_off", payload, evidence_ref, occurred_on, apply)


def _report(conn, purchase_pk):
    purchase = _purchase(conn, purchase_pk)
    events = _events(conn, purchase_pk)
    work = _work(events)
    entries = trades._tips(conn, purchase["sku"]) if purchase["sku"] else []
    managed = bool(purchase["sku"] and conn.execute("SELECT 1 FROM order_lines WHERE sku=?", (purchase["sku"],)).fetchone())
    op_entries = [row for row in entries if row["kind"] in trades.OPERATING_COSTS]
    missing = sorted(set(trades.OPERATING_COSTS) - {row["kind"] for row in op_entries} | {row["kind"] for row in op_entries if row["amount_minor"] is None})
    if managed:
        base = trades.economics(purchase["sku"], conn=conn)
        cash_profit, known_cash = base["net_profit_eur"], base["known_net_cash_eur"]
        phase, base_missing = base["stock_phase"], base["unknown_costs"]
        n_cycles = base["n_sale_cycles"]
    else:
        rows = conn.execute("SELECT * FROM transactions WHERE purchase_pk=? ORDER BY transaction_pk DESC", (purchase_pk,)).fetchall()
        sale = dict(rows[0]) if rows else None
        values = dict(purchase, inbound_shipping_cost_eur=purchase["shipping_cost_eur"], **(sale or {}))
        margin, _ = purchases._margin(values) if sale and sale["outcome_status"] == "sold_confirmed" else (None, None)
        extra = sum(row["amount_minor"] or 0 for row in op_entries) / 100
        cash_profit = round(margin["gross_margin_eur"] - extra, 2) if margin else None
        known_cash = cash_profit if cash_profit is not None else -sum(float(purchase[key] or 0) for key in ("acquisition_cost_eur", "shipping_cost_eur", "buyer_fee_eur")) - extra
        phase = "sold" if margin or sale and sale["outcome_status"] == "sold_confirmed" else "available"
        base_missing = [key for key in ("acquisition_cost_eur", "shipping_cost_eur", "buyer_fee_eur") if purchase[key] is None]
        if sale:
            base_missing += [key for key in purchases.UNKNOWN_COST if sale[key] is None]
            # Show the known contribution even when a remaining cost is unknown.
            known_cash = float(sale["final_sale_price_eur"] or 0) - sum(float(purchase[key] or 0) for key in ("acquisition_cost_eur", "shipping_cost_eur", "buyer_fee_eur")) - sum(float(sale[key] or 0) for key in purchases.UNKNOWN_COST) - extra
        if any(row["kind"] == "write_off" for row in entries):
            phase = "written_off"
            relevant = [row for row in entries if row["kind"] in trades.EXPENSES]
            base_missing = [row["kind"] for row in relevant if row["amount_minor"] is None]
            known_cash = -sum(row["amount_minor"] or 0 for row in relevant) / 100
            cash_profit = known_cash if not base_missing else None
        n_cycles = 1 if sale else 0
    if missing:
        cash_profit = None
    scopes = [row for row in events if row["event_type"] == "scope_review"]
    review = scopes[-1]["payload"] if scopes else None
    fresh = bool(review and review["result"]["scope_sha256"] == _scope_hash(conn, purchase, entries, work))
    total_minutes = sum(row["payload"]["minutes"] for row in work)
    unvalued = [row["event_pk"] for row in work if row["payload"]["worker"] == "operator" and row["payload"]["minutes"] and row["payload"]["hourly_value_minor"] is None]
    value = sum((Decimal(row["payload"]["minutes"]) * Decimal(row["payload"]["hourly_value_minor"] or 0) / Decimal(6000) for row in work if row["payload"]["worker"] == "operator"), Decimal(0))
    labour = float(value.quantize(Decimal(".01"))) if fresh and not unvalued else None
    economic = round(cash_profit - labour, 2) if cash_profit is not None and labour is not None else None
    movements = sorted([row for row in entries if row["kind"] in trades.CASH_RECEIPTS], key=lambda row: (trades._dt(row["effective_at"]), row["entry_id"]))
    committed = sum(row["amount_minor"] for row in movements if row["kind"] == "cash_committed")
    payouts = sum(row["amount_minor"] * (1 if row["kind"] == "usable_payout" else -1) for row in movements if row["kind"] != "cash_committed")
    reconciled = False
    # Contributions may be losses, while individual observed money entries cannot.
    if fresh and review["confirm_cash_complete"] and cash_profit is not None and movements:
        reconciled = payouts - committed == int(Decimal(str(cash_profit)) * 100)
    first = min((trades._dt(row["effective_at"]) for row in movements if row["kind"] == "cash_committed" and row["amount_minor"] > 0), default=None)
    recovery = None
    balance = 0
    for row in movements:
        if row["kind"] == "cash_committed":
            continue
        balance += row["amount_minor"] * (1 if row["kind"] == "usable_payout" else -1)
        if balance < committed:
            recovery = None
        elif recovery is None and first and trades._dt(row["effective_at"]) >= first:
            recovery = trades._dt(row["effective_at"])
    cash_status = "unknown" if not movements else "unreconciled" if not reconciled else "recovered" if recovery else "not_recovered"
    attempts = [dict(row) for row in conn.execute("SELECT * FROM acquisition_attempts WHERE purchase_pk=?", (purchase_pk,)).fetchall()]
    orders = [dict(row) for row in conn.execute("SELECT order_id,cycle,state,sold_at,resale_pairing_json FROM order_lines WHERE sku=? ORDER BY cycle", (purchase["sku"],)).fetchall()] if purchase["sku"] else []
    for order in orders:
        order["resale_pairing"] = json.loads(order.pop("resale_pairing_json")) if order["resale_pairing_json"] else None
    attribution = acquisition_evidence.load([purchase_pk]).get(purchase_pk)
    decision_rows = conn.execute("SELECT decision_pk,decision,recorded_at,notes FROM research_decisions WHERE platform=? AND source_listing_id=? ORDER BY recorded_at DESC,decision_pk DESC",
                                 (purchase["platform"], purchase["source_listing_id"])).fetchall()
    start = metrics.parse_ts(purchase["purchased_at"])
    prior = [dict(row) for row in decision_rows if start and metrics.parse_ts(row["recorded_at"])
             and metrics.parse_ts(row["recorded_at"]) <= start <= inventory._now()]
    return dict(version=VERSION, purchase_pk=purchase_pk, sku=purchase["sku"], stock_phase=phase,
                n_sale_cycles=n_cycles, acquisition_attempts=attempts, orders=orders,
                acquisition_attribution=attribution, acquisition_decisions=prior,
                scope_sha256=_scope_hash(conn, purchase, entries, work),
                active_work=[{key: row[key] for key in ("event_pk", "evidence_ref", "effective_at", "recorded_at", "payload")} for row in work],
                acquisition_costs={key: purchase[key] for key in ("acquisition_cost_eur", "shipping_cost_eur", "buyer_fee_eur")},
                cash_contribution_eur=cash_profit, known_contribution_eur=known_cash,
                unknown_costs=sorted(set(base_missing + missing)), time_scope_complete=fresh,
                total_work_hours=round(total_minutes / 60, 4), operator_time_value_eur=labour,
                unvalued_work_events=unvalued, economic_profit_eur=economic,
                economic_profit_per_work_hour_eur=round(economic / (total_minutes / 60), 2) if economic is not None and total_minutes else None,
                cash=dict(status=cash_status, committed_eur=committed / 100 if movements else None,
                          usable_net_payout_eur=payouts / 100 if movements else None,
                          first_committed_at=first.isoformat() if first else None,
                          recovered_at=recovery.isoformat() if reconciled and recovery else None,
                          recovery_days=round((recovery - first).total_seconds() / 86400, 4) if reconciled and recovery and first else None),
                events=[{key: row[key] for key in ("event_pk", "event_type", "effective_at", "recorded_at", "evidence_ref", "payload")} for row in events],
                financial_entries=entries,
                basis="Whole physical unit across returns and resales; observed costs only. Forecast holding and return reserves are not charged again.")


def evidence_steps(result):
    """Typed UI remedies. A prior decision is offered, never selected implicitly."""
    steps = []
    attribution = result["acquisition_attribution"]
    if not attribution and result["acquisition_decisions"]:
        steps.append(dict(panel="receipt", label="Link the acquisition receipt", reasons=["Choose the prior decision that led to this purchase and record its receipt."]))
    elif attribution and attribution["status"] != "explicit":
        steps.append(dict(panel="receipt", label="Review acquisition evidence", reasons=[attribution["reason"]]))
    if result["unknown_costs"]:
        steps.append(dict(panel="costs", label="Enter missing actual costs", reasons=result["unknown_costs"]))
    if result["unvalued_work_events"]:
        steps.append(dict(panel="work", label="Value recorded operator time", reasons=["Some recorded operator minutes have no evidenced hourly value."]))
    if not result["time_scope_complete"]:
        steps.append(dict(panel="review", label="Review current costs and work scope", reasons=["Review the current evidence after corrections; previous reviews may be stale."]))
    if result["cash"]["status"] in ("unknown", "unreconciled"):
        steps.append(dict(panel="cash", label="Reconcile actual cash movements", reasons=["Record committed cash, usable payouts and reversals, then review completeness."]))
    return steps


def report(purchase_pk):
    with db.read_snapshot():
        return _report(db.connect(), purchase_pk)


def overview(page=1):
    page = int(page)
    if not 1 <= page <= 100000:
        raise ValueError("page must be positive")
    with db.read_snapshot():
        conn = db.connect()
        rows = conn.execute("SELECT purchase_pk FROM purchases ORDER BY purchase_pk DESC LIMIT 25 OFFSET ?", ((page - 1) * 25,)).fetchall()
        counts = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) AS n FROM acquisition_attempts GROUP BY status").fetchall()}
        return dict(version=VERSION, page=page, total=db.scalar("SELECT COUNT(*) FROM purchases"),
                    attempt_counts=counts, items=[_report(conn, row[0]) for row in rows])


def action(payload):
    writers = dict(attempt=attempt, record_time=record_time, operating_cost=operating_cost, cash=cash, review_scope=review_scope, write_off_unsold=write_off_unsold, correct_cost=correct_cost)
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict) or not isinstance(payload.get("action"), str) or payload["action"] not in writers:
        raise ValueError("supported owned outcome action and params are required")
    writer = writers[payload["action"]]
    try:
        inspect.signature(writer).bind(**payload["params"])
    except TypeError:
        raise ValueError("unsupported or missing owned outcome fields") from None
    return {"ok": True, "result": writer(**payload["params"])}
