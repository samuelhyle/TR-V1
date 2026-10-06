"""One private eBay seller connection per environment; account reads only.

Viewing status never requests eBay. OAuth codes are neither stored nor logged.
File locks serialize consent, refresh and disconnect across local processes.
"""

import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from contextlib import contextmanager
from urllib.parse import urlencode

from . import config, fileio
from .ebay_api import ACCOUNT_SCOPE, EbayAPIError, EbayTransport, basic_header, settings, token_payload


def _options(environment):
    if environment not in {"sandbox", "production"}:
        raise ValueError("Choose sandbox or production")
    return settings(environment == "sandbox")


def _fingerprint(options):
    # Configuration changes invalidate grants without exposing credentials.
    return hashlib.sha256(json.dumps([options[k] for k in
        ("environment", "client_id", "client_secret", "runame")]).encode()).hexdigest()


@contextmanager
def _store(environment):
    _options(environment)
    directory = config.DATA_DIR / "connections"
    if directory.is_symlink():
        raise ValueError("Connection storage must be a private local directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    target, lock = directory / f"ebay-{environment}.json", directory / f"ebay-{environment}.lock"
    if target.is_symlink() or lock.is_symlink():
        raise ValueError("Connection storage must not use symlinks")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            record = json.loads(target.read_text()) if target.exists() else {"version": 1, "state": "disconnected"}
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ValueError("Invalid connection store; review the private file")
            for key in ("pending_until", "access_until", "refresh_until", "verified_at", "retry_after_at"):
                value = record.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value)):
                    raise ValueError("Invalid connection store; review the private file")
            if not isinstance(record.get("scopes", []), list) or any(
                    not isinstance(scope, str) for scope in record.get("scopes", [])):
                raise ValueError("Invalid connection store; review the private file")
            if target.exists():
                target.chmod(0o600)
            yield record, target
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Invalid connection store; review the private file") from None


def _write(target, record):
    fileio.write_text(target, json.dumps(record, indent=2, allow_nan=False) + "\n")
    target.chmod(0o600)


def _summary(environment, record):
    options, now = _options(environment), time.time()
    missing = [name for name in ("client_id", "client_secret", "runame") if not options[name]]
    state = record.get("state", "disconnected")
    if missing:
        state = "setup_required"
    elif record.get("fingerprint") and record["fingerprint"] != _fingerprint(options):
        state = "configuration_changed"
    elif state == "authorization_pending" and record.get("pending_until", 0) <= now:
        state = "consent_expired"
    elif state in {"connected", "verified"}:
        if record.get("refresh_until", 0) <= now:
            state = "reauthorization_required"
        elif record.get("access_until", 0) <= now + 60:
            state = "refresh_required"
    can_verify = state in {"connected", "verified", "refresh_required"} and ACCOUNT_SCOPE in record.get("scopes", [])
    return {"platform": "ebay", "environment": environment, "state": state,
            "missing_configuration": missing, "scopes": record.get("scopes", []),
            "verified_at": record.get("verified_at"),
            "seller_registration_completed": record.get("seller_registration_completed"),
            "last_error": record.get("last_error"), "retry_after_at": record.get("retry_after_at"),
            "has_local_grant": bool(record.get("refresh_token") or record.get("pending_hash") or
                                    record.get("revocation_tokens")),
            "capabilities": {"account_status": can_verify, "publish": False, "orders": False,
                             "refund": False, "message": False},
            "note": "Account verification reads eBay registration status. Publishing, orders and refunds "
                    "are not implemented. Collection permission does not grant seller actions."}


def status(environment=None):
    environments = [environment] if environment else ["sandbox", "production"]
    result = []
    for env in environments:
        try:
            with _store(env) as (record, _target):
                result.append(_summary(env, record))
        except (OSError, ValueError):
            _options(env)  # Do not hide invalid user input as a storage problem.
            result.append({"platform": "ebay", "environment": env, "state": "storage_error",
                           "capabilities": {"account_status": False, "publish": False},
                           "last_error": "Private connection store needs review"})
    return {"connections": result}


def _ready(options):
    basic_header(options)
    if not options["runame"]:
        raise ValueError(f"eBay {options['environment']} RuName is missing")


def _error(record, target, exc):
    record["last_error"] = str(exc)
    if isinstance(exc, EbayAPIError) and exc.status in (400, 401, 403):
        record["state"] = "reauthorization_required"
    if isinstance(exc, EbayAPIError) and exc.status == 429:
        record["retry_after_at"] = time.time() + (exc.retry_after if exc.retry_after is not None else 60)
    _write(target, record)


def _cooldown(record):
    if record.get("retry_after_at", 0) > time.time():
        raise ValueError("eBay requested a cooldown; retry after the recorded time")


def begin(environment="sandbox"):
    options = _options(environment)
    _ready(options)
    state, now = secrets.token_urlsafe(32), time.time()
    with _store(environment) as (record, target):
        if record.get("revocation_tokens"):
            raise ValueError("Complete pending token revocation before connecting again")
        if record.get("refresh_token"):
            raise ValueError("Disconnect the existing grant before connecting again")
        _cooldown(record)
        record.clear()
        record.update({"version": 1, "state": "authorization_pending", "fingerprint": _fingerprint(options),
                       "pending_hash": hashlib.sha256(state.encode()).hexdigest(), "pending_until": now + 600,
                       "scopes": [ACCOUNT_SCOPE]})
        _write(target, record)
        summary = _summary(environment, record)
    return {"connection": summary, "authorization_url": options["authorize_url"] + "?" + urlencode({
        "client_id": options["client_id"], "redirect_uri": options["runame"], "response_type": "code",
        "scope": ACCOUNT_SCOPE, "state": state}), "expires_in": 600}


def complete(code, state, environment="sandbox"):
    if not isinstance(code, str) or not code or len(code) > 8192 or not isinstance(state, str) or len(state) > 256:
        raise ValueError("The returned authorization code and state are required")
    options = _options(environment)
    _ready(options)
    with _store(environment) as (record, target):
        if (_summary(environment, record)["state"] != "authorization_pending" or
                not hmac.compare_digest(record.get("pending_hash", ""), hashlib.sha256(state.encode()).hexdigest())):
            raise ValueError("Consent state is invalid, expired or already used; start a new connection")
        _cooldown(record)
        # A code is single-use, including when an exchange has an uncertain result.
        record.pop("pending_hash", None)
        record.pop("pending_until", None)
        record["state"] = "reauthorization_required"
        _write(target, record)
        try:
            data = token_payload(EbayTransport(environment == "sandbox").token(options, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": options["runame"]}), refresh=True)
            supplied = data.get("scope", ACCOUNT_SCOPE).split()
            if ACCOUNT_SCOPE not in supplied:
                raise EbayAPIError(403)
            now = time.time()
            record.update({"state": "connected", "access_token": data["access_token"],
                           "access_until": now + data["expires_in"], "refresh_token": data["refresh_token"],
                           "refresh_until": now + data["refresh_token_expires_in"], "scopes": [ACCOUNT_SCOPE],
                           "last_error": None})
            _write(target, record)
        except EbayAPIError as exc:
            _error(record, target, exc)
            raise
        return {"connection": _summary(environment, record)}


def verify(environment="sandbox"):
    options = _options(environment)
    _ready(options)
    with _store(environment) as (record, target):
        if not _summary(environment, record)["capabilities"]["account_status"]:
            raise ValueError("A current seller grant with account-read scope is required")
        _cooldown(record)
        transport = EbayTransport(environment == "sandbox")
        try:
            if record.get("access_until", 0) <= time.time() + 60:
                data = token_payload(transport.token(options, {"grant_type": "refresh_token",
                    "refresh_token": record["refresh_token"], "scope": ACCOUNT_SCOPE}))
                if ACCOUNT_SCOPE not in data.get("scope", ACCOUNT_SCOPE).split():
                    raise EbayAPIError(403)
                record.update(access_token=data["access_token"], access_until=time.time() + data["expires_in"])
                if "refresh_token" in data:
                    token_payload(data, refresh=True)
                    record.update(refresh_token=data["refresh_token"],
                                  refresh_until=time.time() + data["refresh_token_expires_in"])
                _write(target, record)
            data = transport.request("GET", options["base_url"] + "/sell/account/v1/privilege",
                                     headers={"Authorization": "Bearer " + record["access_token"]})
            registered = data.get("sellerRegistrationCompleted")
            if not isinstance(registered, bool):
                raise EbayAPIError()
            record.update(state="verified", verified_at=time.time(), seller_registration_completed=registered,
                          last_error=None)
            record.pop("retry_after_at", None)
            _write(target, record)
        except EbayAPIError as exc:
            _error(record, target, exc)
            raise
        return {"connection": _summary(environment, record)}


def disconnect(environment="sandbox"):
    options = _options(environment)
    with _store(environment) as (record, target):
        tokens = record.get("revocation_tokens", []) + [
            {"token": record[key], "token_type_hint": key} for key in ("refresh_token", "access_token") if record.get(key)]
        old_fingerprint = record.get("fingerprint")
        cooldown = record.get("retry_after_at")
        record.clear()
        record.update(version=1, state="revocation_pending" if tokens else "disconnected",
                      revocation_tokens=tokens, fingerprint=old_fingerprint)
        if cooldown:
            record["retry_after_at"] = cooldown
        # Local access is removed before the first remote request.
        _write(target, record)
        if tokens:
            if old_fingerprint != _fingerprint(options):
                raise ValueError("Local access removed; restore the original client settings to revoke its grant")
            _cooldown(record)
            transport = EbayTransport(environment == "sandbox")
            for entry in list(tokens):
                try:
                    transport.request("POST", options["base_url"] + "/identity/v1/oauth2/token/revoke",
                                      headers={"Authorization": basic_header(options)}, fields=entry)
                    tokens.remove(entry)
                    _write(target, record)
                except EbayAPIError as exc:
                    _error(record, target, exc)
                    record["state"] = "revocation_pending"
                    _write(target, record)
                    raise
        record.clear()
        record.update(version=1, state="disconnected")
        _write(target, record)
        return {"connection": _summary(environment, record)}


def action(payload):
    if not isinstance(payload, dict):
        raise ValueError("Connection action must be an object")
    environment = payload.get("environment", "sandbox")
    name = payload.get("action")
    if name == "begin":
        return begin(environment)
    if name == "complete":
        return complete(payload.get("code"), payload.get("state"), environment)
    if name == "verify":
        return verify(environment)
    if name == "disconnect":
        return disconnect(environment)
    raise ValueError("Unsupported connection action")
