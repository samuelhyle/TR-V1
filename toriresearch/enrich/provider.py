"""Client for a local llama.cpp server.

Local only, and enforced rather than documented. `resolve_base_url` refuses any
host that is not loopback, so a stray environment variable cannot turn
enrichment into a transfer of seller-authored text to a third party. There is no
flag to disable the check because the check is the feature.

Output is constrained with `response_format: json_schema` and `strict: true`.
That is decoder-level enforcement: the model cannot emit a token sequence
outside the enum, so a prompt injection in a listing title cannot produce an
out-of-vocabulary label. Measured on the local model with an adversarial title:

    json_object    -> {"product_type":"free_money"}     obeyed the injection
    json_schema    -> {"product_type":"astiasto"}       containment held

The prompt is a performance and formatting aid, not a control. Everything that
actually protects this system is in the schema.
"""

import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from ipaddress import ip_address
from urllib.parse import urlparse

from .. import config

log = logging.getLogger("toriresearch.enrich.provider")

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderUnavailable("local model redirects are refused")


# Local requests must not inherit a proxy or send authorization to a redirect.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class ProviderUnavailable(RuntimeError):
    pass


def resolve_base_url(base_url=None):
    """Return the base URL, or refuse if it is not local."""
    url = (base_url or config.LLM_BASE_URL or "").strip().rstrip("/")
    if not url:
        raise ProviderUnavailable("no enrichment base url configured")

    # urlparse, not string splitting: a bracketed IPv6 literal contains colons,
    # so splitting on ':' yields '[' as the host and refuses a loopback address.
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise ProviderUnavailable("invalid local model URL") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port == 0):
        raise ProviderUnavailable("local model requires an HTTP(S) loopback URL without embedded credentials")
    host = parsed.hostname or ""
    if host in _LOOPBACK_NAMES:
        return url
    try:
        if ip_address(host).is_loopback:
            return url
    except ValueError:
        pass
    raise ProviderUnavailable(
        f"enrichment is local-only by decision; refusing {host!r}. Seller-authored "
        "text is not transferred off this machine. Set TORI_LLM_URL to a loopback "
        "address, or set TORI_LLM_ENABLED=0 to run with no model at all."
    )


def _get(path, timeout=5, base_url=None):
    url = f"{resolve_base_url(base_url)}/{path.lstrip('/')}"
    headers = {}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def available(base_url=None):
    """True when a model is reachable. Never raises."""
    if not config.LLM_ENABLED:
        return False
    try:
        resolve_base_url(base_url)
    except ProviderUnavailable:
        return False
    try:
        return _get("models", base_url=base_url).get("data") is not None
    except Exception:
        return False


def health(base_url=None):
    out = {"enabled": config.LLM_ENABLED, "reachable": False, "url": None,
           "model": config.LLM_MODEL, "error": None}
    try:
        out["url"] = resolve_base_url(base_url)
    except ProviderUnavailable as exc:
        out["error"] = str(exc)
        return out
    if not config.LLM_ENABLED:
        out["error"] = "disabled by TORI_LLM_ENABLED"
        return out
    try:
        data = _get("models", base_url=base_url).get("data") or []
        out["reachable"] = True
        if data:
            out["model"] = data[0].get("id") or config.LLM_MODEL
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def _post(path, payload, timeout, max_retries=3, base_url=None):
    url = f"{resolve_base_url(base_url)}/{path.lstrip('/')}"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"

    last = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = (exc.read(200) or b"").decode("utf-8", "replace")
            last = f"HTTP {exc.code}: {detail}"
            # A schema violation is our prompt's fault and retrying cannot help.
            if exc.code in (400, 401, 403, 404, 422):
                break
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 8))
    raise ProviderUnavailable(last or "enrichment provider unreachable")


def complete_json(system, user, schema, max_tokens=200, temperature=0.0,
                  base_url=None, model=None, timeout=None, max_retries=3):
    """One schema-constrained completion. Returns (parsed_or_None, meta).

    A response that does not satisfy the schema is returned as None with the raw
    text in meta, never coerced. Repairing malformed model output by guessing is
    how derived data silently becomes fiction.
    """
    payload = {
        "model": model or config.LLM_MODEL,
        "temperature": temperature,
        "top_k": 1,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "t", "strict": True,
                                            "schema": schema}},
    }
    started = time.time()
    resp = _post("chat/completions", payload, timeout=timeout or config.LLM_TIMEOUT,
                 max_retries=max_retries,
                 base_url=base_url)
    latency = int((time.time() - started) * 1000)
    text = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = resp.get("usage") or {}
    meta = {"latency_ms": latency, "raw": text,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "model": resp.get("model") or config.LLM_MODEL}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, meta
    if not validate(parsed, schema):
        return None, meta
    return parsed, meta


def validate(value, schema, _depth=0):
    """Recursively validate the schema subset used by our local model tasks."""
    if _depth > 20 or not isinstance(schema, dict):
        return False
    kinds = schema.get("type", "object" if "properties" in schema else None)
    kinds = kinds if isinstance(kinds, list) else [kinds]
    checks = {"object": lambda: isinstance(value, dict),
              "array": lambda: isinstance(value, list),
              "string": lambda: isinstance(value, str),
              "boolean": lambda: isinstance(value, bool),
              "null": lambda: value is None,
              "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
              "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool)
                        and (isinstance(value, int) or math.isfinite(value))}
    if kinds != [None] and not any(kind in checks and checks[kind]() for kind in kinds):
        return False
    if "enum" in schema and not any(value == item and isinstance(value, bool) == isinstance(item, bool)
                                    for item in schema["enum"]):
        return False
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                if not validate(item, props[key], _depth+1):
                    return False
            elif extra is False or isinstance(extra, dict) and not validate(item, extra, _depth+1):
                return False
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            return False
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            return False
        if "items" in schema and any(not validate(item, schema["items"], _depth+1) for item in value):
            return False
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
            return False
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return False
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            return False
    return True
