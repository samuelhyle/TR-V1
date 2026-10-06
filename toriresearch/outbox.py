"""Durable publication contract; there is deliberately no live publisher yet.

Adapters must support durable idempotency by action_id and authoritative lookup
of that same request. A timeout or expired lease holds the SKU for reconciliation,
never immediate resubmission. Contract fixtures exercise this lifecycle offline.
"""

import json
import uuid
from datetime import datetime, timedelta

from . import drafts, inventory, permissions

LEASE_SECONDS = 120


class Rejected(Exception):
    """An adapter guarantees the request had no external effect."""


def _approval(conn, approval_id):
    approval_id = inventory._text(approval_id, "approval ID", 64, True)
    row = conn.execute("SELECT * FROM draft_approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if not row:
        raise ValueError("draft approval not found")
    return dict(row)


def _action(conn, action_id, lock=False):
    action_id = inventory._text(action_id, "action ID", 64, True)
    suffix = " FOR UPDATE" if lock and conn.dialect == "postgres" else ""
    row = conn.execute("SELECT * FROM action_outbox WHERE action_id=?" + suffix, (action_id,)).fetchone()
    if not row:
        raise ValueError("queued action not found")
    return dict(row)


def publication_blocks(snapshot, adapter=None):
    channel, environment = snapshot["channel"], snapshot["environment"]
    reasons = []
    if channel == "local_draft":
        return ["Local preparation has no publication channel."]
    if environment == "production":
        try:
            permissions.require(channel, "publish")
        except permissions.PermissionDenied:
            reasons.append("Recorded permission does not cover publishing on this channel.")
    binding = snapshot["binding"]["account_binding"]
    if adapter is None:
        reasons.append("A supported publishing adapter with reconciliation is not implemented.")
        reasons.append("The order and return workflow must be implemented before publication.")
        reasons.append("A supported seller connection for this channel is required.")
    else:
        # This internal contract is not configurable through HTTP or the CLI.
        # Future adapters must implement every guarantee, including P4 readiness.
        capabilities = getattr(adapter, "capabilities", {})
        if not all(capabilities.get(key) is True for key in ("publish", "reconcile", "idempotency", "orders", "returns")):
            reasons.append("Adapter lacks publishing, reconciliation, idempotency or order/return support.")
        if not binding or getattr(adapter, "account_binding", None) != binding:
            reasons.append("Adapter account differs from the reviewed connection.")
        if getattr(adapter, "channel", None) != channel or getattr(adapter, "environment", None) != environment:
            reasons.append("Adapter channel or environment differs from the reviewed target.")
        try:
            checked = adapter.check(snapshot)
            if not isinstance(checked, list) or any(not isinstance(reason, str) for reason in checked):
                raise ValueError("invalid adapter check")
            reasons.extend(checked)
        except Exception:
            reasons.append("Adapter prerequisites could not be checked.")
    return reasons


def _blocks(conn, approval, adapter=None):
    return drafts.approval_check(conn, approval)["reasons"] + publication_blocks(json.loads(approval["snapshot_json"]), adapter)


def _transition(conn, row, state, detail, lease_token=None, lease_until=None):
    stamp = inventory._now().isoformat()
    conn.execute("UPDATE action_outbox SET state=?, detail=?, lease_token=?, lease_until=?, updated_at=? WHERE action_id=?",
                 (state, detail, lease_token, lease_until, stamp, row["action_id"]))
    conn.execute("INSERT INTO action_events (action_id, state, recorded_at, detail) VALUES (?, ?, ?, ?)",
                 (row["action_id"], state, stamp, detail))


def _view(conn, row):
    row = dict(row)
    row.pop("lease_token", None)
    approval = _approval(conn, row["approval_id"])
    snapshot = json.loads(approval["snapshot_json"])
    row.update(channel=snapshot["channel"], environment=snapshot["environment"],
               draft_id=approval["draft_id"], draft_version=approval["draft_version"],
               blockers=_blocks(conn, approval),
               reserved=bool(conn.execute("SELECT 1 FROM inventory_reservations WHERE action_id=?", (row["action_id"],)).fetchone()),
               cancellable=row["state"] in ("blocked", "queued", "failed", "cancelled") and not bool(
                   conn.execute("SELECT 1 FROM inventory_reservations WHERE action_id=?", (row["action_id"],)).fetchone()))
    row["history"] = [dict(event) for event in conn.execute(
        "SELECT state, recorded_at, detail FROM action_events WHERE action_id=? ORDER BY event_pk DESC LIMIT 30", (row["action_id"],)).fetchall()]
    listing = conn.execute("SELECT external_id, state, confirmed_at FROM channel_listings WHERE action_id=?", (row["action_id"],)).fetchone()
    row["listing"] = dict(listing) if listing else None
    return row


def enqueue(approval_id):
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            approval = _approval(conn, approval_id)
            inventory._unit(conn, json.loads(approval["snapshot_json"])["sku"], lock=True)
            previous = conn.execute("SELECT * FROM action_outbox WHERE approval_id=? AND action='publish'", (approval_id,)).fetchone()
            if previous:
                return _view(conn, previous)
            reasons = _blocks(conn, approval)
            action_id, stamp = uuid.uuid4().hex, inventory._now().isoformat()
            state, detail = ("blocked", " ".join(reasons)) if reasons else ("queued", "Reviewed publication queued.")
            conn.execute("INSERT INTO action_outbox (action_id, approval_id, sku, action, state, detail, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (action_id, approval_id, json.loads(approval["snapshot_json"])["sku"], "publish", state, detail, stamp, stamp))
            row = _action(conn, action_id)
            _transition(conn, row, state, detail)
            return _view(conn, _action(conn, action_id))
    finally:
        conn.close()


def refresh(action_id):
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            row = _action(conn, action_id)
            inventory._unit(conn, row["sku"], lock=True)
            row = _action(conn, action_id, lock=True)
            if row["state"] in ("blocked", "queued"):
                reasons = _blocks(conn, _approval(conn, row["approval_id"]))
                state, detail = ("blocked", " ".join(reasons)) if reasons else ("queued", "Reviewed publication queued.")
                if (state, detail) != (row["state"], row["detail"]):
                    _transition(conn, row, state, detail)
            return _view(conn, _action(conn, action_id))
    finally:
        conn.close()


def cancel(action_id, reason):
    reason = inventory._text(reason, "cancellation reason", 1000, True)
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            row = _action(conn, action_id)
            inventory._unit(conn, row["sku"], lock=True)
            row = _action(conn, action_id, lock=True)
            if row["state"] == "cancelled":
                return _view(conn, row)
            if row["state"] not in ("blocked", "queued", "failed") or conn.execute(
                    "SELECT 1 FROM inventory_reservations WHERE action_id=?", (action_id,)).fetchone():
                raise drafts.Conflict("This request may have an external effect. Reconcile its channel state before releasing stock.")
            _transition(conn, row, "cancelled", reason)
            return _view(conn, _action(conn, action_id))
    finally:
        conn.close()


def _claim(adapter, action_id=None):
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            if action_id is None:
                row = conn.execute("SELECT * FROM action_outbox WHERE state IN ('queued','blocked','uncertain') OR "
                    "(state IN ('dispatching','reconciling') AND lease_until<=?) ORDER BY updated_at, action_id LIMIT 1", (inventory._now().isoformat(),)).fetchone()
                if not row:
                    return None
                action_id = row["action_id"]
            row = _action(conn, action_id)
            inventory._unit(conn, row["sku"], lock=True)
            row = _action(conn, action_id, lock=True)
            if row["state"] in ("cancelled", "succeeded", "failed"):
                return None
            if row["state"] in ("dispatching", "reconciling") and datetime.fromisoformat(str(row["lease_until"])) > inventory._now():
                return None
            approval = _approval(conn, row["approval_id"])
            snapshot = json.loads(approval["snapshot_json"])
            reconcile = row["state"] in ("uncertain", "dispatching", "reconciling")
            # Reconciliation reads the frozen request even after a local edit.
            reasons = publication_blocks(snapshot, adapter) if reconcile else _blocks(conn, approval, adapter)
            if reasons:
                _transition(conn, row, "uncertain" if reconcile else "blocked", " ".join(reasons))
                return None
            reservation = conn.execute("SELECT action_id FROM inventory_reservations WHERE sku=?", (row["sku"],)).fetchone()
            if reservation and reservation[0] != action_id:
                _transition(conn, row, "blocked", "This physical unit is reserved by another publication request.")
                return None
            if not reservation:
                conn.execute("INSERT INTO inventory_reservations (sku, action_id, created_at) VALUES (?, ?, ?)",
                             (row["sku"], action_id, inventory._now().isoformat()))
            lease = uuid.uuid4().hex
            _transition(conn, row, "reconciling" if reconcile else "dispatching",
                        "Checking the existing external request." if reconcile else "Dispatch reserved; quantity is one.",
                        lease, (inventory._now() + timedelta(seconds=LEASE_SECONDS)).isoformat())
            if not reconcile:
                conn.execute("UPDATE action_outbox SET attempt_count=attempt_count+1 WHERE action_id=?", (action_id,))
            return {"action_id": action_id, "lease": lease, "snapshot": snapshot, "reconcile": reconcile}
    finally:
        conn.close()


def _finish(claim, result):
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            row = _action(conn, claim["action_id"])
            inventory._unit(conn, row["sku"], lock=True)
            row = _action(conn, row["action_id"], lock=True)
            if row["lease_token"] != claim["lease"] or row["state"] not in ("dispatching", "reconciling"):
                return {"action_id": row["action_id"], "state": row["state"], "fenced": True}
            status = result.get("status") if isinstance(result, dict) else None
            external = result.get("external_id") if isinstance(result, dict) else None
            if status == "found" and isinstance(external, str) and external.strip() and len(external) <= 200:
                snapshot = claim["snapshot"]
                # An identity collision must not release the reservation or retry.
                collision = conn.execute("SELECT action_id FROM channel_listings WHERE channel=? AND environment=? AND account_binding=? AND external_id=?",
                    (snapshot["channel"], snapshot["environment"], snapshot["binding"]["account_binding"], external)).fetchone()
                if collision and collision[0] != row["action_id"]:
                    _transition(conn, row, "uncertain", "External identity already belongs to another request; manual reconciliation required.")
                else:
                    conn.execute("INSERT INTO channel_listings (sku, action_id, channel, environment, account_binding, external_id, confirmed_at) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                                 (row["sku"], row["action_id"], snapshot["channel"], snapshot["environment"],
                                  snapshot["binding"]["account_binding"], external, inventory._now().isoformat()))
                    _transition(conn, row, "succeeded", "External listing confirmed by its stable request identity.")
            elif status == "rejected" or status == "absent" and claim["reconcile"]:
                conn.execute("DELETE FROM inventory_reservations WHERE action_id=?", (row["action_id"],))
                _transition(conn, row, "failed" if status == "rejected" else "queued",
                            "Channel definitively rejected this request without an external effect." if status == "rejected" else
                            "Channel authoritatively confirmed no effect; the same request identity may be retried.")
            else:
                _transition(conn, row, "uncertain", "External outcome is uncertain. Stock is held until authoritative reconciliation.")
            return {"action_id": row["action_id"], "state": _action(conn, row["action_id"])["state"]}
    finally:
        conn.close()


def run_one(adapter=None, action_id=None):
    """Internal adapter contract, never invoked by a web request or a scheduler.

    Real operation has no adapter and fails closed. Offline contract tests supply
    a fake with publish/lookup, stable account binding and idempotency guarantees.
    Exceptions and malformed results are ambiguous, without storing secret bodies.
    """
    claim = _claim(adapter, action_id)
    if claim is None:
        return {"state": "idle_or_blocked"}
    try:
        result = (adapter.lookup if claim["reconcile"] else adapter.publish)(claim["action_id"], claim["snapshot"])
    except Rejected:
        result = {"status": "rejected"} if not claim["reconcile"] else {"status": "unknown"}
    except Exception:
        result = {"status": "unknown"}
    return _finish(claim, result)
