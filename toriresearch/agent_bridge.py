"""A read-only evidence bridge for an external agent runtime.

This exists because the agent layer is a separate process, not a module. Two
things follow from that, and both are decisions rather than constraints:

The interface is read-mostly and one-directional. `GET /agent/context` returns
a frozen evidence bundle and nothing else. `POST /agent/propose` records a
proposal in an append-only quarantine table and returns its own id. There is no
route that writes an observation, an owned ledger row or a publication intent,
so an agent cannot become an author of a fact by calling this service. Nothing
here reaches the outbound network either: an agent that wants collection runs
the CLI, which passes `permissions.py` and `compliance.py` first.

The bundle is the one `analyst.evidence()` already builds. Reusing it is the
point. That catalog is assembled in pure Python inside a single read-only
repeatable-read snapshot, every figure in it is constructed from stored rows,
and it carries its own limitations. An agent handed this bundle is handed the
same evidence a human sees on the dashboard, not a second and more convenient
account of it.

Everything is refused rather than prompted. The bind is loopback-only for the
same reason the dashboard's is, checked by `compliance.assert_not_exposed`
rather than by argument parsing. Sending seller-authored text to a hosted model
is a transfer decision, so `cloud_llm` capability is unavailable unless
`agent_authorizations` holds a confirmed, operator-signed row; there is no flag
that turns the check off, because the check is the feature.

The response carries no figures of its own. It reports what evidence exists and
what state the bridge is in. An agent that wants numbers reads them out of the
bundle, which means the numbers it reasons over are the ones that were stored,
with the bundle's hash attached.
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import analyst, compliance, db

log = logging.getLogger(__name__)

VERSION = "agent-bridge-v1"
MAX_BODY = 262144
KINDS = ("listing", "ranking", "scenario", "audit", "analysis")
STATES = ("pending", "approved", "rejected", "superseded", "invalid")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
CSP = ("default-src 'none'; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")

# A submission names the evidence it was shown. Recording the agent's own claim
# of what it saw would let a stale bundle be approved against fresh evidence, so
# the digest is recomputed here from the bundle the bridge actually served and
# the two must agree before anything is stored.
_REQUIRED = ("agent", "kind", "evidence_sha256", "proposal")


class Rejected(ValueError):
    """A submission that cannot be recorded. Says why, and records nothing."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      default=lambda item: item.isoformat())


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, name, maximum, required=True):
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
        raise Rejected(f"{name} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def bundle(scope=None):
    """Build one frozen evidence bundle and its digest.

    `analyst.evidence` owns the shape and the honesty rules. This adds the
    digest and drops nothing, so a bundle served here is byte-identical to the
    catalog the dashboard analyst is given for the same scope.

    The digest deliberately excludes `captured_at`. That field is the reading
    time, so including it would make every call produce a different digest and
    a client could never submit against the bundle it was just served -- the
    check would be unpassable rather than strict. Excluding it makes the digest
    identify the evidence itself, which is what a proposal actually depends on.
    This is the same exclusion `analyst` applies to its own cache key.
    """
    frozen = analyst.evidence(dict(scope or analyst.options()))
    content = {key: value for key, value in frozen.items() if key != "captured_at"}
    digest = hashlib.sha256(_json(content).encode()).hexdigest()
    return frozen, digest


def served(frozen, digest):
    """The exact document a client receives, plus the envelope around it."""
    return {
        "version": VERSION,
        "evidence": frozen,
        "evidence_sha256": digest,
        "evidence_version": frozen.get("version"),
        "captured_at": frozen.get("captured_at"),
        "fact_ids": [row["id"] for row in frozen.get("facts", [])],
        "next_step_ids": [row["id"] for row in frozen.get("next_steps", [])],
        "contract": {
            "agents_read": True,
            "agents_write_effects": False,
            "proposals_are_claims": True,
            "note": ("Facts and next steps are stored evidence with a basis recorded for each one. "
                     "Asking prices are not realised sale proceeds. A proposal is a claim until an "
                     "operator disposes of it, and disposing of it never happens through this service."),
        },
        "limitations": frozen.get("limitations", []),
    }


def capabilities():
    """What this bridge will and will not do, so a caller need not guess."""
    return {
        "version": VERSION,
        "read": ["agent/context"],
        "write": ["agent/propose"],
        "absent_by_design": ["observation", "owned_ledger", "publication", "collection", "outbound_network"],
        "cloud_llm": cloud_llm_allowed(),
        "limits": {"max_body_bytes": MAX_BODY, "kinds": list(KINDS), "states": list(STATES)},
    }


def cloud_llm_allowed():
    """Whether an agent may be pointed at a hosted model.

    False unless `agent_authorizations` holds a confirmed row for the
    capability. Reading the table directly rather than through a helper keeps
    the refusal on the same path as every other authorization check in this
    project: an absent record is a refusal, and a refusal is a value.
    """
    try:
        conn = db.connect()
    except Exception:
        return False
    try:
        row = conn.execute(
            "SELECT status FROM agent_authorizations WHERE capability=? AND status='confirmed'",
            ("cloud_llm",)).fetchone()
        return bool(row)
    except Exception:
        # An unreadable authorization registry is not an authorization.
        log.warning("Agent authorization registry unreadable; treating cloud_llm as refused")
        return False
    finally:
        conn.close()


def _validate(payload, digest):
    """Check a submission against the bundle digest it claims to have read."""
    if not isinstance(payload, dict):
        raise Rejected("submission must be a JSON object")
    unknown = sorted(set(payload) - set(_REQUIRED) - {"mission_id", "model_identity", "validation"})
    if unknown:
        raise Rejected(f"unsupported submission fields: {unknown}")
    missing = [key for key in _REQUIRED if key not in payload]
    if missing:
        raise Rejected(f"missing submission fields: {missing}")
    agent = _text(payload["agent"], "agent", 64)
    kind = _text(payload["kind"], "kind", 32)
    if kind not in KINDS:
        raise Rejected(f"unsupported proposal kind: {kind}")
    claimed = _text(payload["evidence_sha256"], "evidence_sha256", 64).lower()
    if claimed != digest:
        # The agent is answering about a bundle this bridge did not serve. Either
        # it is working from a stale reading or from something else entirely.
        raise Rejected("evidence_sha256 does not match the bundle this bridge serves")
    proposal = payload["proposal"]
    if not isinstance(proposal, dict) or not proposal:
        raise Rejected("proposal must be a non-empty JSON object")
    mission_id = payload.get("mission_id")
    if mission_id is not None:
        mission_id = _text(mission_id, "mission_id", 128)
    identity = payload.get("model_identity") or {}
    if not isinstance(identity, dict):
        raise Rejected("model_identity must be a JSON object")
    validation = payload.get("validation") or {}
    if not isinstance(validation, dict):
        raise Rejected("validation must be a JSON object")
    # The caller's verdict and the bridge's own guarantees are stored together.
    # The bridge's keys are applied last so a caller cannot overwrite
    # `agent_wrote_no_effect` or `evidence_verified` with something softer.
    reserved = {"evidence_verified", "proposal_is_claim", "agent_wrote_no_effect", "accepted", "kind"}
    if reserved & set(validation):
        raise Rejected(f"validation may not restate bridge-owned keys: {sorted(reserved & set(validation))}")
    return agent, kind, mission_id, claimed, proposal, identity, validation


def record(payload, digest=None, state="pending"):
    """Store one proposal in the quarantine. Returns its id and disposition.

    Idempotent on `input_sha256`, so a retrying agent does not fill the table
    with duplicates of the same claim. Replay is reported rather than hidden,
    because an agent that cannot tell a replay from a new acceptance will keep
    retrying.

    `digest` is the bundle this submission is about. An HTTP caller inherits the
    digest of the bundle being served on the same request; an in-process caller
    passes the one it actually read. It is never taken on trust from the
    payload beyond the equality check below.

    `state` defaults to pending, which is the only state that means "waiting for
    a person". `invalid` is accepted so a failed reading can be recorded without
    pretending it is awaiting disposal -- and the digest is the real one either
    way, because doctoring it to force a store would defeat the check.
    """
    if state not in STATES:
        raise Rejected(f"unsupported submission state: {state}")
    expected = digest or context_digest()
    agent, kind, mission_id, claimed, proposal, identity, verdict = _validate(payload, expected)
    input_sha256 = _hash(dict(agent=agent, kind=kind, mission_id=mission_id,
                              evidence_sha256=claimed, proposal=proposal,
                              model_identity=identity))
    conn = db.connect()
    try:
        with conn.transaction():
            existing = conn.execute(
                "SELECT submission_id, state FROM agent_submissions WHERE input_sha256=?",
                (input_sha256,)).fetchone()
            if existing:
                return dict(submission_id=existing["submission_id"], state=existing["state"],
                            replayed=True, effect="none")
            submission_id = uuid.uuid4().hex
            validation = _json(dict(verdict, accepted=state != "invalid", evidence_verified=True,
                                    kind=kind, proposal_is_claim=True,
                                    agent_wrote_no_effect=True, submitted_as=state))
            conn.execute(
                "INSERT INTO agent_submissions (submission_id, agent, mission_id, kind, evidence_sha256, "
                "input_sha256, proposal_json, validation_json, state, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (submission_id, agent, mission_id, kind, claimed, input_sha256,
                 _json(proposal), validation, state, _now()))
        return dict(submission_id=submission_id, state=state, replayed=False, effect="none")
    finally:
        conn.close()


def pending(limit=50):
    """Proposals awaiting an operator, oldest first."""
    limit = max(1, min(int(limit), 500))
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT submission_id, agent, mission_id, kind, evidence_sha256, state, recorded_at "
            "FROM agent_submissions WHERE state='pending' ORDER BY recorded_at, submission_id LIMIT ?",
            (limit,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def stats():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT state, COUNT(*) AS n FROM agent_submissions GROUP BY state").fetchall()
        return {row["state"]: row["n"] for row in rows}
    finally:
        conn.close()


# The digest a submission is checked against. Set for the duration of one
# request so `record` can validate without rebuilding the bundle, and so a
# proposal can never be validated against a different reading than the one the
# agent was served on the same request.
_context = {"digest": None}


def context_digest():
    digest = _context.get("digest")
    if not digest:
        raise Rejected("no evidence bundle is being served on this request")
    return digest


class Handler(BaseHTTPRequestHandler):
    server_version = "toriresearch-agent-bridge"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # The bind is loopback-only and compliance.assert_not_exposed already
    # enforced that before the socket existed. This asserts it per request, and
    # uses is_loopback rather than comparing against "127.0.0.1" so that an IPv6
    # loopback client is judged as what it is instead of refused by a string
    # mismatch. There is no header that can wave this through.
    def _local(self):
        return bool(compliance.is_loopback(self.client_address[0]))

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else _json(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "loopback host required"})
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/agent/capabilities":
                return self._send(200, capabilities())
            if parsed.path == "/agent/context":
                scope = _scope(query)
                frozen, digest = bundle(scope)
                previous = _context.get("digest")
                _context["digest"] = digest
                try:
                    return self._send(200, served(frozen, digest))
                finally:
                    _context["digest"] = previous
            if parsed.path == "/agent/proposals":
                return self._send(200, dict(pending=int(query.get("limit", ["50"])[0]), stats=stats()))
            return self._send(404, {"error": "not found"})
        except Rejected as exc:
            return self._send(400, {"error": str(exc)})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})

    def do_HEAD(self):
        return self.do_GET()

    def do_POST(self):
        if not self._local():
            return self._send(403, {"error": "loopback host required"})
        parsed = urlparse(self.path)
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            return self._send(403, {"error": "same-origin requests required"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "invalid Content-Length"})
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "body too large"})
        if parsed.path != "/agent/propose":
            return self._send(404, {"error": "not found"})
        try:
            payload = json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "body must be UTF-8 JSON"})
        frozen, digest = bundle(None)
        previous = _context.get("digest")
        _context["digest"] = digest
        try:
            return self._send(201, record(payload))
        except Rejected as exc:
            return self._send(400, {"error": str(exc)})
        finally:
            _context["digest"] = previous


def _scope(query):
    """Build analyst options from a query string, rejecting anything unknown."""
    allowed = {"dimension", "days", "horizon"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameters: {unknown}")
    options = {"dimension": query.get("dimension", ["category_root_norm"])[0],
               "days": query.get("days", ["30"])[0],
               "horizon": query.get("horizon", ["30"])[0]}
    return analyst.options(**options)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Bind and run. Refuses any interface that is not loopback."""
    host = compliance.assert_not_exposed(host)
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("invalid agent bridge port")
    httpd = _Server((host, port), Handler)
    print(f"agent bridge on http://{host}:{port}/agent/capabilities", flush=True)
    print("  read-only evidence; proposals are quarantined for an operator", flush=True)
    print("  cloud_llm authorised: " + ("yes" if cloud_llm_allowed() else "no"), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="toriresearch-agent-bridge")
    parser.add_argument("--host", default=DEFAULT_HOST, help="loopback only; anything else is refused")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    return serve(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())