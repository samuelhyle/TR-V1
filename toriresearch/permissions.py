"""Private source/action authority, checked again before outbound collection.

An operator confirmation is evidence of authority, not a verified contract.
Operating budgets are deliberately separate from contractual limits.
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, fileio

TORI_ACTIONS = ("metadata", "atom_sampler", "discovery", "detail", "enrichment")
ACCEPTED_STATUS = {"operator_confirmed", "verified"}


class PermissionDenied(RuntimeError):
    pass


def registry_path():
    return Path(config.PERMISSION_REGISTRY_PATH) if config.PERMISSION_REGISTRY_PATH else config.DATA_DIR / "authorizations" / "registry.json"


def signature():
    try:
        digest = hashlib.sha256(registry_path().read_bytes()).hexdigest()
    except OSError:
        return "unavailable"
    try:
        now = datetime.now(timezone.utc)
        flags = [(e["id"], _active(e, now), bool(e.get("review_at") and _stamp(e["review_at"]) <= now))
                 for e in load()["entries"]]
        return digest + ":" + json.dumps(flags, sort_keys=True)
    except PermissionDenied:
        return digest + ":invalid"


def _stamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("permission date must be an ISO timestamp or null")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("permission timestamps need a timezone")
    return result


def load():
    try:
        record = json.loads(registry_path().read_text())
        if record.get("version") != 1 or not isinstance(record.get("entries"), list):
            raise ValueError("unsupported permission registry")
        entries = record["entries"]
        identifiers = [e["id"] for e in entries]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate permission IDs")
        for entry in entries:
            if not isinstance(entry.get("actions"), list):
                raise ValueError("permission actions must be a list")
            scope = entry.get("scope")
            if not isinstance(scope, dict):
                raise ValueError("permission scope must be an object")
            for field in ("category_paths", "municipalities"):
                if not isinstance(scope.get(field), list) or any(
                        not isinstance(v, str) or not v.strip() for v in scope[field]):
                    raise ValueError("invalid permission scope")
            _stamp(entry.get("expires_at"))
            _stamp(entry.get("review_at"))
        return record
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Never silently fall back to broad authority on a corrupt/missing file.
        raise PermissionDenied("Source permission registry is missing or invalid") from None


def _active(entry, now):
    expires = _stamp(entry.get("expires_at"))
    return entry.get("status") in ACCEPTED_STATUS and (expires is None or expires > now)


def _below(path, parent):
    from .collection import category_key
    path, parent = category_key(path), category_key(parent)
    return path == parent or path.startswith(parent + ".")


def require(platform, action, *, category_paths=None, municipalities=None, row=None, now=None):
    now = now or datetime.now(timezone.utc)
    entries = [e for e in load()["entries"] if e.get("platform") == platform]
    # A recorded platform-wide refusal wins over another positive entry.
    if any(e.get("status") in {"denied", "refused", "revoked"} for e in entries):
        raise PermissionDenied(f"{platform} source authority is refused or revoked")
    for entry in entries:
        if not _active(entry, now) or action not in entry["actions"]:
            continue
        scope = entry["scope"]
        categories, places = scope["category_paths"], scope["municipalities"]
        if action in {"discovery", "detail", "enrichment"}:
            if row is not None:
                from .collection import Scope
                matcher = Scope(platform=platform, categories=categories)
                matcher.municipalities = tuple(places)
                if not matcher.matches(row):
                    continue
            else:
                if categories and (not category_paths or not all(
                        any(_below(p, allowed) for allowed in categories) for p in category_paths)):
                    continue
                if places and (not municipalities or not all(p in places for p in municipalities)):
                    continue
        return {"permission_id": entry["id"], "verification_basis": entry.get("verification_basis"),
                "status": entry["status"]}
    raise PermissionDenied(f"{platform} {action} is outside recorded authority or requires review")


def require_tori_url(url):
    parsed = urlparse(url)
    if parsed.hostname not in {"tori.fi", "www.tori.fi"}:
        raise PermissionDenied("Unrecognized Tori request origin")
    try:
        port = parsed.port
    except ValueError:
        raise PermissionDenied("Invalid Tori request origin") from None
    if parsed.scheme != "https" or port not in (None, 443) or parsed.username:
        raise PermissionDenied("Tori requests require the approved HTTPS origin")
    path = parsed.path
    if path == "/feed/recommerce/atom.xml":
        return require("tori", "atom_sampler")
    if path == "/robots.txt" or path == "/sitemap.xml" or path.startswith("/sitemap"):
        return require("tori", "metadata")
    if path == "/recommerce/forsale/search":
        query = parse_qs(parsed.query)
        categories = [p for key in ("category", "sub_category", "product_category")
                      for p in query.get(key, [])]
        places = []
        if query.get("location"):
            try:
                labels = json.loads((config.DATA_DIR / "tori_location_labels.json").read_text())
            except (OSError, ValueError):
                labels = {}
            places = [labels.get(p, "") for p in query["location"]]
        return require("tori", "discovery", category_paths=categories, municipalities=places)
    prefix = "/recommerce/forsale/item/"
    if path.startswith(prefix) and path[len(prefix):].isdigit():
        row = db.query_one("SELECT * FROM listings WHERE platform='tori' AND listing_id=?",
                           (path[len(prefix):],))
        return require("tori", "detail", row=row, category_paths=[])
    raise PermissionDenied("Tori request surface is outside recorded authority")


def report():
    now = datetime.now(timezone.utc)
    try:
        entries = load()["entries"]
    except PermissionDenied as exc:
        return {"valid": False, "entries": [], "error": str(exc), "actions": {}}
    actions = {}
    for action in TORI_ACTIONS:
        try:
            actions[action] = {"allowed": True, **require(
                "tori", action, category_paths=list(config.FOCUS_CATEGORIES),
                municipalities=list(config.FOCUS_MUNICIPALITIES), now=now)}
        except PermissionDenied as exc:
            actions[action] = {"allowed": False, "reason": str(exc)}
    return {"valid": True, "actions": actions, "entries": [
        {k: e.get(k) for k in ("id", "platform", "status", "verification_basis", "actions", "scope",
                              "expires_at", "review_at", "contractual_limits", "operating_controls")}
        | {"review_due": bool(e.get("review_at") and _stamp(e["review_at"]) <= now)} for e in entries],
        "note": "Scope authority is checked before requests. Operator confirmation does not verify a contract; "
                "the national Atom sampler has separate coverage. Collection authority does not grant seller actions."}


def import_confirmation():
    """One-time, explicit migration of the already captured owner confirmation."""
    target = registry_path()
    if target.exists():
        raise ValueError("Permission registry already exists; review it before changing authority")
    source = config.DATA_DIR / "authorizations" / "tori_collection.json"
    record = json.loads(source.read_text())
    if record.get("platform") != "tori" or record.get("status") != "operator_confirmed":
        raise ValueError("Existing Tori operator confirmation is required")
    scope = record["collection_scope"]
    entry = {"id": "tori-focused-collection", "platform": "tori", "status": "operator_confirmed",
             "verification_basis": "owner confirmation; letter not independently inspected",
             "evidence_ref": "tori_collection.json", "confirmed_at": record["confirmed_at"],
             "actions": list(TORI_ACTIONS),
             "scope": {"category_paths": scope["category_paths"], "municipalities": scope["municipalities"]},
             "expires_at": None, "review_at": None, "contractual_limits": None,
             "operating_controls": record.get("operational_controls", {}),
             "interpretation": "Preserves the existing focused pipeline, including its national Atom arrival "
                               "sampler and sitemap metadata. Atom rows outside focus do not authorize detail checks. "
                               "Approximate one-third is not a numeric contractual quota. No expiry was supplied."}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    fileio.write_text(target, json.dumps({"version": 1, "entries": [entry]}, indent=2) + "\n")
    target.chmod(0o600)
    return report()
