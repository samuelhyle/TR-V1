"""Manual owned-SKU sale cycles, return evidence and immutable financial entries.

This module records observed events. It performs no marketplace, payment,
refund, messaging or fulfilment action. Money is integer EUR cents. Unknown
costs and unresolved return/stock states suppress complete profit.
"""

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from . import db, inventory

Conflict = inventory.Conflict
CHANNELS = ("tori", "offline", "ebay", "tradera", "other")
CASE_TYPES = {"withdrawal": "Consumer withdrawal", "defect": "Defect claim",
              "platform_protection": "Platform protection", "voluntary": "Voluntary return"}
SALE_COSTS = ("platform_fee_eur", "payment_fee_eur", "seller_shipping_cost_eur", "packaging_cost_eur",
              "other_variable_cost_eur", "tax_cost_eur")
ACQUISITION_COSTS = ("acquisition_cost_eur", "inbound_shipping_cost_eur", "buyer_fee_eur")
OPERATING_COSTS = ("paid_labour", "carrying_cost", "overhead_cost")
CASH_RECEIPTS = {"cash_committed", "usable_payout", "payout_reversal"}
EXPENSES = set(SALE_COSTS + ACQUISITION_COSTS + OPERATING_COSTS + ("reverse_shipping", "repair", "disposal"))
INCOME = {"sale_proceeds", "fee_recovery"}
KINDS = EXPENSES | INCOME | {"refund", "write_off"} | CASH_RECEIPTS
LOCAL = ZoneInfo("Europe/Helsinki")
SOURCES = {"returns": "https://www.kkv.fi/kuluttaja-asiat/tavarat/vaihto-ja-palautusoikeus/",
           "used_goods": "https://www.kkv.fi/kuluttaja-asiat/tavarat/vastuu-kaytetyn-tavaran-virheesta/"}


def _money(value, name="amount", unknown=True):
    if unknown and (value is None or isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(name + " must be nonnegative EUR with at most two decimal places")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1000000000 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError()
        return int(amount * 100)
    except (ValueError, InvalidOperation, OverflowError):
        raise ValueError(name + " must be nonnegative EUR with at most two decimal places") from None


def _stamp(value=None, name="event date"):
    if value is None:
        return inventory._now().isoformat()
    value = inventory._text(value, name, 100, True)
    try:
        if len(value) == 10:
            parsed = datetime.combine(date.fromisoformat(value), time.min, LOCAL)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed > inventory._now():
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError(name + " must be an observed ISO date or timezone-aware timestamp, never future") from None


def _dt(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _after(value, earlier, name):
    # Date-only evidence has day precision. Avoid inventing an intra-day order.
    later, first = _dt(value).astimezone(LOCAL), _dt(earlier).astimezone(LOCAL)
    if later.date() < first.date() or later.date() == first.date() and later.time() != time.min and first.time() != time.min and later < first:
        raise ValueError(name + " cannot precede its earlier event")


def _asof(value=None):
    if value is None:
        return inventory._now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("as-of timestamp needs a timezone")
        return min(value, inventory._now())
    if isinstance(value, str) and len(value) == 10:
        day = date.fromisoformat(value)
        if day > inventory._now().astimezone(LOCAL).date():
            raise ValueError("as-of day cannot be future")
        return min(datetime.combine(day + timedelta(days=1), time.min, LOCAL) - timedelta(microseconds=1), inventory._now())
    return _dt(_stamp(value, "as-of timestamp"))


def _deadline(value):
    if value is None:
        value = {"status": "requires_review"}
    if not isinstance(value, dict) or set(value) - {"status", "kind", "on", "reason", "evidence_ref"}:
        raise ValueError("unsupported deadline assessment")
    status = value.get("status", "requires_review")
    if status not in ("requires_review", "applicable", "not_applicable"):
        raise ValueError("unsupported deadline status")
    result = {"status": status, "kind": inventory._text(value.get("kind", ""), "deadline purpose", 100),
              "reason": inventory._text(value.get("reason", ""), "deadline basis", 1000, status != "requires_review"),
              "evidence_ref": inventory._text(value.get("evidence_ref", ""), "deadline evidence", 500, status != "requires_review"),
              "on": value.get("on") or None}
    if status == "applicable":
        try:
            if not result["kind"] or date.fromisoformat(result["on"]).isoformat() != result["on"]:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("an applicable deadline needs a purpose and YYYY-MM-DD date") from None
    elif result["on"]:
        raise ValueError("a date requires an applicable deadline assessment")
    return result


def _order(conn, order_id, lock=False):
    order_id = inventory._text(order_id, "order ID", 64, True)
    suffix = " FOR UPDATE" if lock and conn.dialect == "postgres" else ""
    row = conn.execute("SELECT * FROM order_lines WHERE order_id=?" + suffix, (order_id,)).fetchone()
    if not row:
        raise ValueError("owned order not found")
    return dict(row)


def _case(conn, case_id, order_id):
    case_id = inventory._text(case_id, "case ID", 64, True)
    row = conn.execute("SELECT * FROM return_cases WHERE case_id=? AND order_id=?", (case_id, order_id)).fetchone()
    if not row:
        raise ValueError("return case does not belong to this order")
    return dict(row)


def _stock(conn, sku):
    row = conn.execute("SELECT * FROM stock_lifecycle WHERE sku=?", (sku,)).fetchone()
    return dict(row) if row else None


def _snapshot(conn, order_id):
    order = _order(conn, order_id)
    return {"order": order, "stock": _stock(conn, order["sku"]),
            "cases": [dict(row) for row in conn.execute("SELECT * FROM return_cases WHERE order_id=? ORDER BY created_at, case_id", (order_id,)).fetchall()]}


def _receipt(conn, token, fingerprint):
    previous = inventory._replay(conn, "order_events", token, fingerprint)
    if previous:
        return {**json.loads(previous["result_json"]), "replayed": True}
    return None


def _event(conn, token, fingerprint, order_id, name, evidence, effective, result):
    conn.execute("INSERT INTO order_events (order_id, client_token, request_sha256, event_type, recorded_at, effective_at, evidence_ref, result_json, snapshot_json) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (order_id, token, fingerprint, name, inventory._now().isoformat(), effective,
                 evidence, inventory._json(result), inventory._json(_snapshot(conn, order_id))))


def _entry(conn, sku, kind, amount, effective, evidence, reason, order_id=None, case_id=None, supersedes=None, external_ref=None):
    entry_id = uuid.uuid4().hex
    conn.execute("INSERT INTO financial_entries (entry_id, sku, order_id, case_id, supersedes_id, external_event_ref, kind, amount_minor, effective_at, recorded_at, evidence_ref, reason) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (entry_id, sku, order_id, case_id, supersedes, external_ref, kind, amount,
                 effective, inventory._now().isoformat(), evidence, reason))
    return entry_id


def _tips(conn, sku, asof=None):
    rows = [dict(row) for row in conn.execute("SELECT * FROM financial_entries WHERE sku=? ORDER BY recorded_at, entry_id", (sku,)).fetchall()]
    if asof is not None:
        rows = [row for row in rows if _dt(row["recorded_at"]) <= asof and _dt(row["effective_at"]) <= asof]
    replaced = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
    return [row for row in rows if row["entry_id"] not in replaced]


def _bump(conn, order):
    conn.execute("UPDATE order_lines SET version=version+1, updated_at=? WHERE order_id=?",
                 (inventory._now().isoformat(), order["order_id"]))


def _physical(conn, sku, phase, state, reason, received=None, invalidate=False):
    inventory._unreserved(conn, sku)
    conn.execute("UPDATE stock_lifecycle SET phase=?, received_at=COALESCE(?,received_at), updated_at=? WHERE sku=?",
                 (phase, received, inventory._now().isoformat(), sku))
    conn.execute("UPDATE inventory_units SET state=?, version=version+1, facts_version=facts_version+?, updated_at=? WHERE sku=?",
                 (state, int(invalidate), inventory._now().isoformat(), sku))
    inventory._event(conn, inventory._unit(conn, sku), "trade_" + phase, reason)


def _context(value, choices, name):
    if value not in choices:
        raise ValueError("unsupported " + name)
    return value


def record_sale(client_token, sku, expected_unit_version, proceeds_eur, evidence_ref, costs=None,
                channel="tori", external_order_ref=None, sold_on=None, handed_over=False,
                seller_context="unknown", buyer_context="unknown", sale_method="unknown", resale_listing_id=None):
    token = inventory._token(client_token)
    evidence_ref = inventory._text(evidence_ref, "sale/payment evidence", 500, True)
    _context(channel, CHANNELS, "sale channel")
    _context(seller_context, ("unknown", "private", "business"), "seller context")
    _context(buyer_context, ("unknown", "consumer", "business"), "buyer context")
    _context(sale_method, ("unknown", "distance", "in_person"), "sale method")
    if not isinstance(handed_over, bool):
        raise ValueError("handover confirmation must be true or false")
    if external_order_ref is not None:
        external_order_ref = inventory._text(external_order_ref, "external order reference", 100, True)
    if costs is None:
        costs = {}
    if not isinstance(costs, dict) or set(costs) - set(SALE_COSTS):
        raise ValueError("unsupported sale costs")
    resale_listing_id = inventory._text(resale_listing_id, "resale listing ID", 100, True) if resale_listing_id is not None else None
    amount = _money(proceeds_eur, "observed sale proceeds", False)
    cleaned = {key: _money(costs.get(key), key) for key in SALE_COSTS}
    # Hash the supplied date, not an auto-generated now: ambiguous retries keep
    # their original observation time and cannot create a second sale cycle.
    fingerprint = inventory._hash({"sku": sku, "version": expected_unit_version, "amount": amount,
        "evidence": evidence_ref, "costs": cleaned, "channel": channel, "external": external_order_ref,
        "sold_on": sold_on, "handed_over": handed_over, "seller": seller_context, "buyer": buyer_context, "method": sale_method, "resale_listing_id": resale_listing_id})
    sold_at = _stamp(sold_on, "sale date")
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "trade:" + token)
            previous = _receipt(conn, token, fingerprint)
            if previous:
                return previous
            unit = inventory._unit(conn, sku, lock=True)
            inventory._version(unit, expected_unit_version)
            inventory._unreserved(conn, sku)
            stock = _stock(conn, sku)
            if unit["state"] != "in_stock" or unit["has_recorded_outcome"] or stock and stock["phase"] != "available":
                raise Conflict("This physical unit is unavailable. Reconcile its sale or returned-stock inspection first.")
            if conn.execute("SELECT 1 FROM return_cases c JOIN order_lines o ON o.order_id=c.order_id WHERE o.sku=? AND c.state!='closed'", (sku,)).fetchone():
                raise Conflict("Resolve this unit's open return cases before another sale.")
            purchase = dict(conn.execute("SELECT * FROM purchases WHERE purchase_pk=?", (unit["purchase_pk"],)).fetchone())
            _after(sold_at, purchase["purchased_at"], "sale")
            if external_order_ref and conn.execute("SELECT 1 FROM order_lines WHERE channel=? AND external_order_ref=? AND sku=?", (channel, external_order_ref, sku)).fetchone():
                raise Conflict("This external order and physical unit are already recorded.")
            cycle = conn.execute("SELECT COALESCE(MAX(cycle),0)+1 FROM order_lines WHERE sku=?", (sku,)).fetchone()[0]
            if stock:
                earlier = _order(conn, stock["current_order_id"])
                _after(sold_at, stock["received_at"] or earlier["sold_at"], "resale")
            order_id, stamp = uuid.uuid4().hex, inventory._now().isoformat()
            conn.execute("INSERT INTO order_lines (order_id, sku, cycle, channel, external_order_ref, origin, state, fulfilment, sold_at, delivered_at, seller_context, buyer_context, sale_method, evidence_ref, source_snapshot_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'manual', 'sold', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, sku, cycle, channel, external_order_ref, "delivered" if handed_over else "pending", sold_at,
                 sold_at if handed_over else None, seller_context, buyer_context, sale_method, evidence_ref, inventory._json(unit), stamp, stamp))
            from . import realization
            pairing = realization.freeze_resale_pairing(conn, channel, resale_listing_id, sold_at)
            conn.execute("UPDATE order_lines SET resale_pairing_json=? WHERE order_id=?", (inventory._json(pairing), order_id))
            if not stock:
                for key, source in (("acquisition_cost_eur", "acquisition_cost_eur"), ("inbound_shipping_cost_eur", "shipping_cost_eur"), ("buyer_fee_eur", "buyer_fee_eur")):
                    _entry(conn, sku, key, _money(purchase[source], key), str(purchase["purchased_at"]), evidence_ref, "Original acquisition ledger snapshot")
            _entry(conn, sku, "sale_proceeds", amount, sold_at, evidence_ref, "Observed gross sale/payment proceeds", order_id)
            for key, value in cleaned.items():
                _entry(conn, sku, key, value, sold_at, evidence_ref, "Sale-cycle cost; blank is unknown", order_id)
            conn.execute("INSERT INTO stock_lifecycle (sku, phase, current_order_id, updated_at) VALUES (?, 'sold', ?, ?) "
                         "ON CONFLICT(sku) DO UPDATE SET phase='sold', current_order_id=excluded.current_order_id, received_at=NULL, release_inspection_pk=NULL, updated_at=excluded.updated_at",
                         (sku, order_id, stamp))
            _physical(conn, sku, "sold", "retired", "Actual manual sale recorded: " + order_id)
            result = {"order_id": order_id, "sku": sku, "version": 1, "replayed": False}
            _event(conn, token, fingerprint, order_id, "sale_recorded", evidence_ref, sold_at, result)
            return result
    finally:
        conn.close()


def _mutate(client_token, order_id, expected_version, name, params, apply):
    token = inventory._token(client_token)
    inventory._integer(expected_version, "order version")
    fingerprint = inventory._hash({"action": name, "order_id": order_id, "version": expected_version, "params": params})
    evidence = inventory._text(params.get("evidence_ref", ""), "event evidence", 500, True)
    effective = _stamp(params.get("occurred_on"), "event date")
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "trade:" + token)
            previous = _receipt(conn, token, fingerprint)
            if previous:
                return previous
            order = _order(conn, order_id)
            unit = inventory._unit(conn, order["sku"], lock=True)
            order = _order(conn, order_id, lock=True)
            if order["version"] != expected_version:
                raise Conflict("This order changed. Reload it before saving.")
            if name != "financial_entry":
                _after(effective, order["sold_at"], "order event")
            result = apply(conn, order, unit, effective) or {}
            _bump(conn, order)
            result = {**result, "order_id": order_id, "sku": order["sku"], "version": expected_version + 1, "replayed": False}
            _event(conn, token, fingerprint, order_id, name, evidence, effective, result)
            return result
    finally:
        conn.close()


def fulfil(client_token, order_id, expected_version, stage, evidence_ref, occurred_on=None):
    if stage not in ("shipped", "delivered"):
        raise ValueError("unsupported fulfilment stage")
    def apply(conn, order, unit, effective):
        if order["state"] != "sold" or order["fulfilment"] == "delivered" or stage == "shipped" and order["fulfilment"] != "pending":
            raise Conflict("This fulfilment transition is unavailable.")
        if order["shipped_at"]:
            _after(effective, order["shipped_at"], "delivery")
        field = "shipped_at" if stage == "shipped" else "delivered_at"
        conn.execute("UPDATE order_lines SET fulfilment=?, " + field + "=? WHERE order_id=?", (stage, effective, order_id))
    return _mutate(client_token, order_id, expected_version, "fulfilment", locals_payload(stage=stage, evidence_ref=evidence_ref, occurred_on=occurred_on), apply)


def locals_payload(**kwargs):
    return kwargs


def open_case(client_token, order_id, expected_version, case_type, reason, evidence_ref, occurred_on=None, deadline=None):
    if case_type not in CASE_TYPES:
        raise ValueError("unsupported return case type")
    reason = inventory._text(reason, "return reason", 1000, True)
    deadline = _deadline(deadline)
    def apply(conn, order, unit, effective):
        if conn.execute("SELECT 1 FROM return_cases WHERE order_id=? AND state!='closed'", (order_id,)).fetchone():
            raise Conflict("Resolve this order's existing open return case first.")
        case_id = uuid.uuid4().hex
        conn.execute("INSERT INTO return_cases (case_id, order_id, case_type, state, reason, evidence_ref, notified_at, deadline_json, created_at) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                     (case_id, order_id, case_type, reason, evidence_ref, effective, inventory._json(deadline), inventory._now().isoformat()))
        for kind in ("reverse_shipping", "repair"):
            _entry(conn, unit["sku"], kind, None, effective, evidence_ref, "Return-case cost to reconcile", order_id, case_id)
        stock = _stock(conn, unit["sku"])
        if stock["current_order_id"] == order_id and stock["phase"] == "sold":
            _physical(conn, unit["sku"], "return_pending", "retired", "Return claim opened: " + case_id)
        return {"case_id": case_id}
    return _mutate(client_token, order_id, expected_version, "return_opened", locals_payload(case_type=case_type, reason=reason, evidence_ref=evidence_ref, occurred_on=occurred_on, deadline=deadline), apply)


def receive_return(client_token, order_id, expected_version, case_id, evidence_ref, occurred_on=None):
    def apply(conn, order, unit, effective):
        case, stock = _case(conn, case_id, order_id), _stock(conn, unit["sku"])
        if case["state"] != "open" or stock["current_order_id"] != order_id or stock["phase"] != "return_pending" or order["state"] != "sold":
            raise Conflict("This case cannot receive the physical unit again.")
        _after(effective, case["notified_at"], "return receipt")
        conn.execute("UPDATE return_cases SET state='received', received_at=? WHERE case_id=?", (effective, case_id))
        conn.execute("UPDATE order_lines SET state='returned', fulfilment='returned' WHERE order_id=?", (order_id,))
        _physical(conn, unit["sku"], "returned_review", "quarantined", "Returned item requires a fresh inspection", effective, invalidate=True)
    return _mutate(client_token, order_id, expected_version, "return_received", locals_payload(case_id=case_id, evidence_ref=evidence_ref, occurred_on=occurred_on), apply)


def finance(client_token, order_id, expected_version, kind, amount_eur, reason, evidence_ref,
            occurred_on=None, case_id=None, supersedes_id=None, external_event_ref=None):
    if kind not in KINDS - {"write_off"} - CASH_RECEIPTS - set(OPERATING_COSTS):
        raise ValueError("unsupported financial entry kind")
    reason = inventory._text(reason, "financial reason", 1000, True)
    amount = _money(amount_eur, kind, unknown=kind in EXPENSES)
    if external_event_ref is not None:
        external_event_ref = inventory._text(external_event_ref, "payment/refund reference", 100, True)
    def apply(conn, order, unit, effective):
        if external_event_ref and conn.execute("SELECT 1 FROM financial_entries WHERE order_id=? AND external_event_ref=?", (order_id, external_event_ref)).fetchone():
            raise Conflict("This payment/refund reference is already recorded for the order.")
        purchased = conn.execute("SELECT purchased_at FROM purchases WHERE purchase_pk=?", (unit["purchase_pk"],)).fetchone()[0]
        _after(effective, order["sold_at"] if kind in INCOME | {"refund"} else purchased, "financial entry")
        previous = None
        if case_id:
            _case(conn, case_id, order_id)
        if supersedes_id:
            previous = conn.execute("SELECT * FROM financial_entries WHERE entry_id=? AND sku=?", (supersedes_id, unit["sku"])).fetchone()
            if not previous or previous["kind"] != kind or previous["order_id"] not in (None, order_id) or previous["case_id"] != case_id:
                raise ValueError("correction does not match this entry's SKU, order, case and kind")
            if conn.execute("SELECT 1 FROM financial_entries WHERE supersedes_id=?", (supersedes_id,)).fetchone():
                raise Conflict("This financial entry was already corrected. Reload its current revision.")
        elif kind in {"sale_proceeds"} | set(ACQUISITION_COSTS):
            raise ValueError("proceeds and original acquisition costs require a superseding correction")
        if kind == "refund" and not case_id:
            raise ValueError("an actual refund requires a linked return/claim case")
        if kind in ("reverse_shipping", "repair") and not case_id:
            raise ValueError("return costs require a linked return/claim case")
        tips = [row for row in _tips(conn, unit["sku"]) if row["order_id"] == order_id and row["entry_id"] != supersedes_id]
        proceeds = sum(row["amount_minor"] or 0 for row in tips if row["kind"] == "sale_proceeds") + (amount if kind == "sale_proceeds" else 0)
        refunds = sum(row["amount_minor"] or 0 for row in tips if row["kind"] == "refund") + (amount if kind == "refund" else 0)
        if refunds > proceeds:
            raise ValueError("total refunds cannot exceed this sale's recorded proceeds")
        fee_kinds = ("platform_fee_eur", "payment_fee_eur")
        fees = [row["amount_minor"] for row in tips if row["kind"] in fee_kinds]
        if kind in fee_kinds:
            fees.append(amount)
        recovered = sum(row["amount_minor"] or 0 for row in tips if row["kind"] == "fee_recovery") + (amount if kind == "fee_recovery" else 0)
        if (kind == "fee_recovery" and any(value is None for value in fees)
                or not any(value is None for value in fees) and recovered > sum(fees)):
            raise ValueError("recovered fees require known fees and cannot exceed them")
        entry_order = previous["order_id"] if previous else order_id
        entry_id = _entry(conn, unit["sku"], kind, amount, effective, evidence_ref, reason, entry_order, case_id, supersedes_id, external_event_ref)
        return {"entry_id": entry_id}
    return _mutate(client_token, order_id, expected_version, "financial_entry", locals_payload(kind=kind, amount_minor=amount, reason=reason,
        evidence_ref=evidence_ref, occurred_on=occurred_on, case_id=case_id, supersedes_id=supersedes_id, external_event_ref=external_event_ref), apply)


def assess_deadline(client_token, order_id, expected_version, case_id, deadline, evidence_ref, occurred_on=None):
    deadline = _deadline(deadline)
    def apply(conn, order, unit, effective):
        _case(conn, case_id, order_id)
        conn.execute("UPDATE return_cases SET deadline_json=? WHERE case_id=?", (inventory._json(deadline), case_id))
    return _mutate(client_token, order_id, expected_version, "deadline_reviewed", locals_payload(case_id=case_id, deadline=deadline, evidence_ref=evidence_ref, occurred_on=occurred_on), apply)


def review_context(client_token, order_id, expected_version, seller_context, buyer_context,
                   sale_method, evidence_ref, occurred_on=None):
    _context(seller_context, ("unknown", "private", "business"), "seller context")
    _context(buyer_context, ("unknown", "consumer", "business"), "buyer context")
    _context(sale_method, ("unknown", "distance", "in_person"), "sale method")
    def apply(conn, order, unit, effective):
        conn.execute("UPDATE order_lines SET seller_context=?, buyer_context=?, sale_method=? WHERE order_id=?",
                     (seller_context, buyer_context, sale_method, order_id))
        # Applicability reviewed under different participant facts needs a new
        # evidenced assessment. Previous decisions remain in event snapshots.
        if any(order[key] != value for key, value in (("seller_context", seller_context),
                ("buyer_context", buyer_context), ("sale_method", sale_method))):
            conn.execute("UPDATE return_cases SET deadline_json=? WHERE order_id=?",
                         (inventory._json(_deadline(None)), order_id))
    return _mutate(client_token, order_id, expected_version, "context_reviewed",
        locals_payload(seller_context=seller_context, buyer_context=buyer_context, sale_method=sale_method,
                       evidence_ref=evidence_ref, occurred_on=occurred_on), apply)


def close_case(client_token, order_id, expected_version, case_id, resolution, evidence_ref,
               settlement_confirmed, occurred_on=None):
    resolution = inventory._text(resolution, "case resolution", 1000, True)
    if settlement_confirmed is not True:
        raise ValueError("confirm recorded refunds, retained/recovered fees and return costs before closing")
    def apply(conn, order, unit, effective):
        case = _case(conn, case_id, order_id)
        if case["state"] == "closed":
            raise Conflict("This case is already closed.")
        _after(effective, case["received_at"] or case["notified_at"], "case closure")
        if any(row["amount_minor"] is None for row in _tips(conn, unit["sku"]) if row["case_id"] == case_id):
            raise Conflict("Return costs are unknown. Record or explicitly confirm zero before closing.")
        conn.execute("UPDATE return_cases SET state='closed', resolution=?, closed_at=? WHERE case_id=?", (resolution, effective, case_id))
        stock = _stock(conn, unit["sku"])
        if stock["current_order_id"] == order_id and stock["phase"] == "return_pending":
            _physical(conn, unit["sku"], "sold", "retired", "Claim closed; item remains with buyer")
    return _mutate(client_token, order_id, expected_version, "return_closed", locals_payload(case_id=case_id, resolution=resolution, evidence_ref=evidence_ref, settlement_confirmed=True, occurred_on=occurred_on), apply)


def release_stock(client_token, order_id, expected_version, evidence_ref, occurred_on=None):
    def apply(conn, order, unit, effective):
        stock = _stock(conn, unit["sku"])
        if stock["current_order_id"] != order_id or stock["phase"] != "returned_review":
            raise Conflict("This unit is not awaiting returned-stock release.")
        if conn.execute("SELECT 1 FROM return_cases c JOIN order_lines o ON o.order_id=c.order_id WHERE o.sku=? AND c.state!='closed'", (unit["sku"],)).fetchone():
            raise Conflict("Resolve open claims before releasing returned stock.")
        inspection = inventory._inspection(conn.execute("SELECT * FROM inspections WHERE sku=? ORDER BY inspection_pk DESC LIMIT 1", (unit["sku"],)).fetchone())
        preview = inventory.preview({**unit, "state": "in_stock", "has_recorded_outcome": False}, inspection, "local_draft")
        if not inspection or _dt(inspection["recorded_at"]) < _dt(stock["received_at"]) or preview["status"] != "allowed":
            raise Conflict("Returned stock needs a current, passed competent inspection before release.")
        _after(effective, stock["received_at"], "stock release")
        _after(effective, inspection["recorded_at"], "stock release")
        conn.execute("UPDATE stock_lifecycle SET release_inspection_pk=? WHERE sku=?", (inspection["inspection_pk"], unit["sku"]))
        _physical(conn, unit["sku"], "available", "in_stock", "Returned-stock inspection verified before resale")
    return _mutate(client_token, order_id, expected_version, "stock_released", locals_payload(evidence_ref=evidence_ref, occurred_on=occurred_on), apply)


def write_off(client_token, order_id, expected_version, disposal_cost_eur, reason, evidence_ref, occurred_on=None):
    reason = inventory._text(reason, "write-off reason", 1000, True)
    amount = _money(disposal_cost_eur, "disposal cost")
    def apply(conn, order, unit, effective):
        stock = _stock(conn, unit["sku"])
        if stock["current_order_id"] != order_id or stock["phase"] not in ("returned_review", "available"):
            raise Conflict("Only returned owned stock can be written off in this workflow.")
        _after(effective, stock["received_at"], "write-off")
        _entry(conn, unit["sku"], "write_off", 0, effective, evidence_ref, reason, order_id)
        _entry(conn, unit["sku"], "disposal", amount, effective, evidence_ref, "Observed disposal cost; acquisition is counted once", order_id)
        _physical(conn, unit["sku"], "written_off", "retired", reason)
    return _mutate(client_token, order_id, expected_version, "stock_written_off", locals_payload(disposal_minor=amount, reason=reason, evidence_ref=evidence_ref, occurred_on=occurred_on), apply)


def economics(sku, as_of=None, conn=None):
    cutoff = _asof(as_of)
    if conn is None:
        with db.read_snapshot():
            return economics(sku, cutoff, db.connect())
    unit = inventory._unit(conn, sku)
    tips = [entry for entry in _tips(conn, sku, cutoff) if entry["kind"] not in CASH_RECEIPTS]
    events = [dict(row) for row in conn.execute("SELECT * FROM order_events WHERE order_id IN (SELECT order_id FROM order_lines WHERE sku=?) ORDER BY event_pk", (sku,)).fetchall()
              if _dt(row["recorded_at"]) <= cutoff and _dt(row["effective_at"]) <= cutoff]
    latest, cases, orders = None, {}, {}
    for event in events:
        snapshot = json.loads(event["snapshot_json"])
        latest = snapshot["stock"]
        orders[snapshot["order"]["order_id"]] = snapshot["order"]
        for case in snapshot["cases"]:
            cases[case["case_id"]] = case
    unknown = [row for row in tips if row["amount_minor"] is None]
    known = sum((row["amount_minor"] or 0) * (1 if row["kind"] in INCOME else 0 if row["kind"] == "write_off" else -1) for row in tips)
    proceeds = sum(row["amount_minor"] or 0 for row in tips if row["kind"] == "sale_proceeds")
    refunds = sum(row["amount_minor"] or 0 for row in tips if row["kind"] == "refund")
    recovery = sum(row["amount_minor"] or 0 for row in tips if row["kind"] == "fee_recovery")
    open_cases = [case for case in cases.values() if case["state"] != "closed"]
    phase = latest["phase"] if latest else "available"
    current = orders.get(latest["current_order_id"]) if latest else None
    settled = phase == "written_off" or phase == "sold" and current and current["fulfilment"] == "delivered"
    complete = bool(tips and settled and not unknown and not open_cases)
    amounts = {key: sum(row["amount_minor"] or 0 for row in tips if row["kind"] == key) for key in KINDS}
    missing = sorted({row["kind"] for row in unknown})
    reasons = (["Unknown costs: " + ", ".join(missing)] if missing else [])
    if not settled:
        reasons.append("Stock or fulfilment lifecycle is not settled.")
    if open_cases:
        reasons.append("Return/claim cases remain open.")
    return {"sku": sku, "purchase_pk": unit["purchase_pk"], "as_of": cutoff.isoformat(), "stock_phase": phase,
            "n_sale_cycles": len(orders), "gross_proceeds_eur": proceeds / 100, "refunds_eur": refunds / 100,
            "fee_recovery_eur": recovery / 100, "net_sale_receipts_eur": (proceeds - refunds) / 100,
            "known_net_cash_eur": known / 100 if tips else None, "net_profit_eur": known / 100 if complete else None,
            "complete": complete, "unknown_costs": missing, "open_cases": len(open_cases), "reasons": reasons,
            "amounts_minor": amounts, "current_order": current, "active_entries": tips,
            "note": "Observed gross receipts and costs; unknown entries suppress complete profit. Not a bank payout reconciliation."}


def get(order_id):
    with db.read_snapshot():
        conn = db.connect()
        order = _order(conn, order_id)
        order["unit"] = inventory.get(order["sku"])
        order["economics"] = economics(order["sku"], conn=conn)
        order["cases"] = [dict(row) for row in conn.execute("SELECT * FROM return_cases WHERE order_id=? ORDER BY created_at DESC, case_id", (order_id,)).fetchall()]
        for case in order["cases"]:
            case["deadline"] = json.loads(case.pop("deadline_json"))
        order["entries"] = [dict(row) for row in conn.execute("SELECT * FROM financial_entries WHERE sku=? ORDER BY recorded_at, entry_id", (order["sku"],)).fetchall()]
        order["history"] = [dict(row) for row in conn.execute("SELECT event_pk, event_type, recorded_at, effective_at, evidence_ref, snapshot_json FROM order_events WHERE order_id=? ORDER BY event_pk DESC LIMIT 100", (order_id,)).fetchall()]
        for event in order["history"]:
            event["snapshot"] = json.loads(event.pop("snapshot_json"))
        order["sources"] = SOURCES
        order["external_actions_enabled"] = False
        return order


def ledger_rows():
    """One aggregate per purchase for compatibility, never one margin per resale."""
    result = []
    with db.read_snapshot():
        conn = db.connect()
        units = conn.execute("SELECT u.sku, p.* FROM inventory_units u JOIN purchases p ON p.purchase_pk=u.purchase_pk WHERE EXISTS (SELECT 1 FROM order_lines o WHERE o.sku=u.sku)").fetchall()
        for raw in units:
            purchase, e = dict(raw), economics(raw["sku"], conn=conn)
            current, amounts = e["current_order"], e["amounts_minor"]
            costs = {key: None if key in e["unknown_costs"] else amounts[key] / 100 for key in SALE_COSTS + ACQUISITION_COSTS}
            extra = (amounts["reverse_shipping"] + amounts["repair"] + amounts["disposal"] + sum(amounts[key] for key in OPERATING_COSTS)) / 100
            if not any(key in e["unknown_costs"] for key in ("other_variable_cost_eur", "reverse_shipping", "repair", "disposal") + OPERATING_COSTS):
                costs["other_variable_cost_eur"] += extra
            else:
                costs["other_variable_cost_eur"] = None
            if costs["platform_fee_eur"] is not None and costs["payment_fee_eur"] is not None:
                platform_credit = min(costs["platform_fee_eur"], e["fee_recovery_eur"])
                costs["platform_fee_eur"] -= platform_credit
                costs["payment_fee_eur"] -= e["fee_recovery_eur"] - platform_credit
            phase = e["stock_phase"]
            status = None if phase == "available" else "sold_confirmed" if phase == "sold" else phase
            if status == "sold_confirmed" and current and current["origin"] == "legacy":
                status = json.loads(current["source_snapshot_json"])["transaction"]["outcome_status"]
            net = e["net_sale_receipts_eur"]
            margin = {"gross_margin_eur": e["net_profit_eur"], "gross_margin_pct": e["net_profit_eur"] / net if net else None} if e["complete"] else None
            result.append({**purchase, **costs, "managed_sku": purchase["sku"], "outcome_status": status,
                           "sale_at": current["sold_at"] if current else None, "final_sale_price_eur": net,
                           "net_margin": margin, "trade_economics": e})
    return result


def listing(sku="", page=1):
    sku = inventory._text(sku, "SKU", 64)
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        raise ValueError("page must be a positive integer") from None
    clause, params = (" WHERE o.sku=?", [sku]) if sku else ("", [])
    with db.read_snapshot():
        total = db.scalar("SELECT COUNT(*) FROM order_lines o" + clause, params)
        pages = max(1, (total + 24) // 25)
        page = min(page, pages)
        items = db.query("SELECT o.*, u.title FROM order_lines o JOIN inventory_units u ON u.sku=o.sku" + clause + " ORDER BY o.created_at DESC, o.order_id LIMIT 25 OFFSET ?", params + [(page - 1) * 25])
        for row in items:
            row["economics"] = economics(row["sku"], conn=db.connect())
            row.pop("source_snapshot_json", None)
        units = db.query("SELECT u.sku, u.title, u.version FROM inventory_units u LEFT JOIN stock_lifecycle s ON s.sku=u.sku "
            "WHERE u.state='in_stock' AND (s.phase IS NULL OR s.phase='available') "
            "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.purchase_pk=u.purchase_pk AND s.phase IS NULL) "
            "AND NOT EXISTS (SELECT 1 FROM inventory_reservations r WHERE r.sku=u.sku) ORDER BY u.updated_at DESC LIMIT 200")
        counts = {row["phase"]: row["n"] for row in db.query("SELECT phase, COUNT(*) AS n FROM stock_lifecycle GROUP BY phase")}
        legacy = db.query("SELECT t.transaction_pk, p.purchase_pk, p.title_raw, t.platform, t.sale_at FROM transactions t JOIN purchases p ON p.purchase_pk=t.purchase_pk "
            "WHERE t.outcome_status IN ('sold_confirmed','sold_inferred') AND NOT EXISTS (SELECT 1 FROM order_lines o WHERE o.legacy_transaction_pk=t.transaction_pk) ORDER BY t.transaction_pk DESC LIMIT 200")
    return {"items": items, "total": total, "page": page, "pages": pages, "units": units,
            "counts": counts, "legacy_candidates": legacy, "case_types": CASE_TYPES, "sources": SOURCES,
            "external_actions_enabled": False}


def import_legacy(client_token, transaction_pk, kind, evidence_ref):
    """Explicitly adopt a real legacy transaction; never create a duplicate sale."""
    token = inventory._token(client_token)
    inventory._integer(transaction_pk, "transaction ID")
    _context(kind, inventory.KINDS, "inventory kind")
    evidence_ref = inventory._text(evidence_ref, "migration evidence", 500, True)
    fingerprint = inventory._hash({"transaction_pk": transaction_pk, "kind": kind, "evidence": evidence_ref})
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "trade:" + token)
            previous = _receipt(conn, token, fingerprint)
            if previous:
                return previous
            suffix = " FOR UPDATE" if conn.dialect == "postgres" else ""
            raw = conn.execute("SELECT * FROM transactions WHERE transaction_pk=?" + suffix, (transaction_pk,)).fetchone()
            if not raw or raw["outcome_status"] not in ("sold_confirmed", "sold_inferred") or not raw["purchase_pk"]:
                raise ValueError("choose a real legacy sale with an acquisition purchase")
            legacy = dict(raw)
            sold_at = _stamp(str(legacy["sale_at"]) if legacy["sale_at"] else "", "legacy sale date")
            proceeds = _money(legacy["final_sale_price_eur"], "legacy proceeds", False)
            purchase = dict(conn.execute("SELECT * FROM purchases WHERE purchase_pk=?", (legacy["purchase_pk"],)).fetchone())
            _after(sold_at, purchase["purchased_at"], "legacy sale")
            unit_row = conn.execute("SELECT sku FROM inventory_units WHERE purchase_pk=?", (legacy["purchase_pk"],)).fetchone()
            if unit_row:
                unit = inventory._unit(conn, unit_row[0], lock=True)
                sku = unit["sku"]
                if unit["kind"] != kind:
                    raise Conflict("Existing SKU category differs; review its owned facts first.")
                inventory._unreserved(conn, sku)
            else:
                sku, stamp = "TR-" + uuid.uuid4().hex[:16].upper(), inventory._now().isoformat()
                conn.execute("INSERT INTO inventory_units (sku, purchase_pk, client_token, request_sha256, title, kind, state, facts_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'retired', ?, ?, ?)", (sku, legacy["purchase_pk"], "legacy-" + str(legacy["purchase_pk"]), fingerprint,
                    purchase["title_raw"], kind, inventory._json(inventory._facts({})), stamp, stamp))
                inventory._event(conn, inventory._unit(conn, sku), "legacy_adopted", "Historical unit from acquisition and real legacy sale; physical facts remain unknown")
            if conn.execute("SELECT 1 FROM order_lines WHERE sku=?", (sku,)).fetchone():
                raise Conflict("This unit already has SKU-linked sale history.")
            if conn.execute("SELECT COUNT(*) FROM transactions WHERE purchase_pk=?", (legacy["purchase_pk"],)).fetchone()[0] != 1:
                raise Conflict("Multiple legacy transactions need explicit lifecycle reconciliation before adoption.")
            order_id, stamp = uuid.uuid4().hex, inventory._now().isoformat()
            conn.execute("INSERT INTO order_lines (order_id, sku, cycle, channel, external_order_ref, legacy_transaction_pk, origin, state, fulfilment, sold_at, delivered_at, seller_context, buyer_context, sale_method, evidence_ref, source_snapshot_json, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?, 'legacy', 'sold', ?, ?, ?, 'unknown', 'unknown', 'unknown', ?, ?, ?, ?)",
                (order_id, sku, legacy["platform"], legacy["platform_transaction_id"], transaction_pk,
                 "pending", sold_at, None,
                 evidence_ref, inventory._json({"transaction": legacy, "purchase": purchase}), stamp, stamp))
            for key, source in (("acquisition_cost_eur", "acquisition_cost_eur"), ("inbound_shipping_cost_eur", "shipping_cost_eur"), ("buyer_fee_eur", "buyer_fee_eur")):
                _entry(conn, sku, key, _money(purchase[source], key), str(purchase["purchased_at"]), evidence_ref, "Original acquisition; explicit legacy adoption")
            _entry(conn, sku, "sale_proceeds", proceeds, sold_at, evidence_ref, "Original legacy gross proceeds", order_id)
            for key in SALE_COSTS:
                _entry(conn, sku, key, _money(legacy[key], key), sold_at, evidence_ref, "Original legacy sale cost", order_id)
            conn.execute("INSERT INTO stock_lifecycle (sku, phase, current_order_id, updated_at) VALUES (?, 'sold', ?, ?)", (sku, order_id, stamp))
            _physical(conn, sku, "sold", "retired", "Legacy sale adopted; old rows preserved")
            conn.execute("UPDATE order_lines SET resale_pairing_json=? WHERE order_id=?", (legacy.get("resale_pairing_json"), order_id))
            result = {"order_id": order_id, "sku": sku, "version": 1, "replayed": False}
            _event(conn, token, fingerprint, order_id, "legacy_imported", evidence_ref, sold_at, result)
            return result
    finally:
        conn.close()


def action(payload):
    writers = {
        "record_sale": record_sale, "fulfil": fulfil, "open_case": open_case, "receive_return": receive_return,
        "finance": finance, "assess_deadline": assess_deadline, "review_context": review_context, "close_case": close_case,
        "release_stock": release_stock, "write_off": write_off, "import_legacy": import_legacy,
    }
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
        raise ValueError("action and params are required")
    name, params = payload.get("action"), payload["params"]
    if not isinstance(name, str) or name not in writers:
        raise ValueError("unsupported owned trade action")
    # Explicit Python signatures are the bounded action contract. Binding first
    # avoids exposing internal TypeError details or allowing positional injection.
    import inspect
    try:
        inspect.signature(writers[name]).bind(**params)
    except TypeError:
        raise ValueError("unsupported or missing trade fields") from None
    return {"ok": True, "result": writers[name](**params)}
