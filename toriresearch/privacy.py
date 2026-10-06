"""Pseudonymous seller keys.

Tori publishes an `ownerId` on every item page. Linking listings to a seller
is what makes seller concentration, archetype and relist analysis possible at
all, and there is no aggregate that can be computed without it -- so the choice
is never between seller analytics and no seller analytics, it is between doing
the linkage under a key we cannot reverse and not doing it.

So the identifier is read, reduced to an HMAC, and dropped. The raw value
never reaches a record, a log line, an exception message or an export, and the
salt that keyed the HMAC is held in `config/local.env`, outside the repository.
What persists is 32 hex characters that name a seller to this project and to
nothing else: it cannot be turned back into an ownerId, and it is not
correlatable with any other dataset that hashes the same accounts without our
salt.

Two consequences are worth stating plainly, because both are failures someone
will hit eventually:

* The salt is durable. Rotating it relabels every seller, which silently
  resets the archetype, concentration and relist series that depend on seller
  identity. Losing it does the same thing with no way back. Configure it once.
* Absence is the default. With no salt configured the parser returns `None`,
  `listings.seller_id` stays NULL, and the seller-level models report BLOCKED
  rather than producing numbers over an empty key.
"""

import hashlib
import hmac

from . import config

KEY_LENGTH = 32


def seller_key_enabled():
    """Whether seller linkage is configured for this run."""
    return bool(config.SELLER_KEY_ENABLED)


def seller_key(raw_id):
    """Reduce a platform owner identifier to a pseudonymous key.

    Returns `None` when linkage is disabled or the identifier is absent or
    empty, which is the only failure mode: a caller that receives a key knows
    it is usable, and a caller that receives `None` stores NULL.

    `raw_id` is a local argument and is deliberately never formatted into any
    message this function raises or logs.
    """
    if raw_id is None or not seller_key_enabled():
        return None
    if not isinstance(raw_id, str):
        raw_id = str(raw_id)
    raw_id = raw_id.strip()
    if not raw_id:
        return None
    digest = hmac.new(
        config.SELLER_KEY_SALT.encode("utf-8"),
        raw_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:KEY_LENGTH]


def posture():
    """Non-identifying description of the current configuration.

    Safe for `doctor`, status output and dashboards: it reports whether a salt
    is present and its length, never its value.
    """
    salt = config.SELLER_KEY_SALT
    if not seller_key_enabled():
        if not salt.strip():
            reason = "no TORI_SELLER_SALT configured; seller_id stays NULL"
        else:
            reason = "TORI_SELLER_HASH=0; linkage switched off"
        return {"enabled": False, "key_length": KEY_LENGTH, "reason": reason}
    return {
        "enabled": True,
        "key_length": KEY_LENGTH,
        "salt_length": len(salt),
        "reason": "ownerId reduced to HMAC at parse time; raw value not stored",
    }
