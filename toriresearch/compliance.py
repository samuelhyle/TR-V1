"""Collection preconditions, and the two things the dashboard must never do.

`Innoweb v Wegener` (C-202/12) is the one genuinely dangerous authority in the
project: a dedicated, publicly *available* meta-search interface over a
marketplace database re-utilises a substantial part, because it offers the same
search functionality with real-time query translation and mirrored presentation
order. That is what a public Tori price-comparison frontend is, and it is why
this project is internal analytics only.

The second boundary is Bird & Bird's practitioner framing: bots that merely
direct traffic arguably cause no damage, whereas bots used to check and undercut
competitors' prices in real time can cause financial losses, and real-time
undercutting is the specific harm that generates injunctions.

So the rules are not "be careful with the dashboard". They are:

1. It binds to loopback only. Not configurable. There is no version of this
   that is useful to a competitor and lawful at the same time.
2. It reads our own observations. It never queries the platform on a viewer's
   behalf, so no dashboard refresh can become a real-time request against Tori.

Both are enforced here rather than in the UI, because a UI-only rule is one
copy-paste away from being gone.
"""

import ipaddress
import re
import socket
from datetime import date, datetime, timezone

from . import config

LOOPBACK_ONLY = "dashboard is bound to loopback and refuses any other interface"

# Actions the dashboard may trigger. Each one already exists as a CLI command, so
# this grants no new capability -- it only bounds how much of it can be fired
# from a browser. Budgets are capped well below the scheduled defaults. An int is
# a bounded quantity the run must take; the string "flag" is an on/off switch the
# CLI already has, omitted entirely when it is off. Only the budget is a volume
# control, so only the budget is capped against the scheduled default.
ALLOWED_ACTIONS = {
    "sweep":      {"budget": 60, "pages": 3, "include_regions": "flag",
                   "deepest_first": "flag", "dry_run": "flag"},
    "itempage":   {"budget": 30},
    "enrich":     {"budget": 20},
    "report":     {},
    "retention":  {},
}


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_bind_host(host):
    """Refuse anything that is not this machine."""
    if host in ("localhost", ""):
        return "127.0.0.1"
    if is_loopback(host):
        return host
    raise RuntimeError(
        f"{LOOPBACK_ONLY}: refusing to bind {host!r}. A network-reachable view of "
        "this data is a public comparison frontend, which is Innoweb."
    )


def assert_not_exposed(host):
    """Second check at bind time, independent of the argument parser."""
    resolved = resolve_bind_host(host)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # A zero-length connect to a loopback address cannot leave the host; it
        # exists so an accidentally-supplied 0.0.0.0 cannot pass unnoticed.
        ipaddress.ip_address(resolved)
    finally:
        probe.close()
    return resolved


def _document_field(text, label):
    match = re.search(r"^\*\*" + re.escape(label) + r":\*\*[ \t]*(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _confirmed_value(value):
    return bool(value) and not any(marker in value.lower() for marker in (
        "[", "]", "___", "pending", "unverified", "not recorded", "to confirm"))


def _lia_signed_off(text):
    """Recognise a recorded signature; missing draft text is not approval."""
    if _document_field(text, "Status").upper() not in {"SIGNED OFF", "APPROVED"}:
        return False
    if not all(_confirmed_value(_document_field(text, label)) for label in (
            "Controller", "Signed off by", "Balancing conclusion")):
        return False
    stamp = _document_field(text, "Sign-off date")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
        return False
    try:
        return date.fromisoformat(stamp) <= date.today()
    except ValueError:
        return False


def compliance_report():
    """Document checks, reported separately from technical runtime readiness."""
    legal = config.ROOT / "legal"
    lia = legal / "LIA.md"
    notice = legal / "ART14_NOTICE.md"
    lia_text = lia.read_text() if lia.exists() else ""
    notice_text = notice.read_text() if notice.exists() else ""

    checks = {
        "contact_email_deliverable": config.contact_email_ok(),
        "lia_exists": lia.exists(),
        "lia_controller_named": _confirmed_value(_document_field(lia_text, "Controller")),
        "lia_signed_off": _lia_signed_off(lia_text),
        "art14_notice_exists": notice.exists(),
        "art14_controller_named": _confirmed_value(_document_field(notice_text, "Controller")),
        "art14_optout_present": any(
            w in notice_text.lower() for w in ("opt-out", "opt out", "veto", "kielt")),
    }
    blockers = [k for k, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "blocking": blockers,
        "ready_for_collection": not blockers,
        "note": (
            "These document checks do not verify permission, approval, notice "
            "publication or contact deliverability; the contact check validates "
            "address syntax only. The collector reports these checks without "
            "gating runtime. Unfinished requirements still need operator review."
        ),
    }


def dashboard_posture():
    return {
        "bind": LOOPBACK_ONLY,
        "data_source": "our own stored observations only",
        "no_live_platform_queries": (
            "Refreshing this page reads the local database. It never requests the "
            "platform, so no view refresh can become a real-time request against "
            "a seller. Real-time competitor undercutting is the specific harm "
            "that generates injunctions and it is not built."
        ),
        "public_comparison_frontend": (
            "Not built and must not be. Innoweb v Wegener (C-202/12) is a "
            "dedicated publicly available meta-search interface over a marketplace "
            "database; that is this project's risk case, not its product."
        ),
        "allowed_actions": sorted(ALLOWED_ACTIONS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
