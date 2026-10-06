"""Application accounts and revocable sessions, separate from source authority."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from . import db

COOKIE = 'tori_session'
ROLES = {'developer', 'viewer'}
IDLE_SECONDS = 1800
SESSION_SECONDS = 43200
_PASSWORD_SLOTS = threading.BoundedSemaphore(2)
_SCRYPT = (32768, 8, 3)


class Denied(ValueError):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9._@+\-]{3,150}', value.strip()):
        raise ValueError('Username must contain 3–150 letters, numbers, or . _ @ + -')
    return value.strip().lower()


def _algorithm():
    return 'scrypt-v1' if hasattr(hashlib, 'scrypt') else 'pbkdf2-sha256-v1'


# The cost a new credential is made with. Written into the hash rather than assumed
# by the verifier, so raising it can never invalidate a credential that already
# exists -- each stored hash names the cost it was created with.
_SCRYPT = (131072, 8, 1)
_LEGACY_SCRYPT = (32768, 8, 3)


def _maxmem(n, r):
    """scrypt needs `128 * n * r` for its blocks, plus room for the buffers."""
    return 128 * n * r + 32 * 1024 * 1024


def _derive(password, salt, version=None, params=None):
    if not _PASSWORD_SLOTS.acquire(blocking=False):
        raise Denied('Login is busy. Try again shortly.', 429)
    try:
        version = version or _algorithm()
        if version == 'pbkdf2-sha256-v1':
            return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000, dklen=32)
        if not hasattr(hashlib, 'scrypt'):
            raise Denied('Password verification requires the owning Docker runtime.', 503)
        n, r, p = params or _SCRYPT
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=_maxmem(n, r))
    finally:
        _PASSWORD_SLOTS.release()


def password_hash(password):
    """`scrypt-v2$N$r$p$hexsalt$hexdigest`.

    The same encoding the Netlify function writes, deliberately. One scheme name for
    one set of parameters and one field encoding is what makes a credential portable
    in both directions; the earlier arrangement had two different meanings behind the
    single name `scrypt-v1`, one hex and one base64, which failed silently -- a
    verifier handed the wrong one returned false rather than complaining -- and forced
    a second name (`scrypt-netlify-v1`) to stand in for the difference.
    """
    if not isinstance(password, str) or not 15 <= len(password) <= 256:
        raise ValueError('Use a password of 15–256 characters.')
    if not hasattr(hashlib, 'scrypt'):
        # A host without scrypt cannot produce a v2 hash; fall back rather than refuse
        # to create an account at all.
        salt = secrets.token_bytes(16)
        digest = _derive(password, salt, 'pbkdf2-sha256-v1')
        return f'pbkdf2-sha256-v1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}'
    n, r, p = _SCRYPT
    salt = secrets.token_bytes(16).hex()
    # The salt fed to the KDF is the *ASCII text* of the hex, not the sixteen bytes it
    # encodes. That is not the usual choice, and it is deliberate: Node's scrypt takes
    # the salt as a string and hashes its UTF-8 bytes, so the hosted function has
    # always salted with thirty-two ASCII characters while this runtime salted with
    # sixteen raw ones. Same random value, different salt, completely different digest
    # -- and the mismatch is silent in both directions, because each side verifies its
    # own convention perfectly and neither can read the other's.
    #
    # Matching the function here is what makes one credential portable, and matching
    # it in ASCII also keeps every account already provisioned at the hosted cost
    # readable, since those were salted the same way.
    digest = _derive(password, salt.encode(), 'scrypt-v1', _SCRYPT)
    return f'scrypt-v2${n}${r}${p}${salt}${digest.hex()}'


_V2 = re.compile(r'^scrypt-v2\$(\d+)\$(\d+)\$(\d+)\$([0-9a-f]{32})\$([0-9a-f]{64})$')


def _needs_rehash(encoded):
    """True when a stored credential predates the current cost or encoding."""
    match = _V2.match(encoded or '')
    if not match:
        return True
    n, r, p = (int(match.group(index)) for index in (1, 2, 3))
    return (n, r, p) != tuple(_SCRYPT)


def verify(password, encoded):
    if not isinstance(password, str) or len(password) > 256:
        return False
    # A host whose Python was built without scrypt cannot check a scrypt credential.
    # That is a property of the host, not of the password, so it is reported rather
    # than folded into a plain "wrong password" -- which would send an operator with a
    # perfectly good credential looking for a typo that does not exist.
    needs_scrypt = str(encoded or '').startswith(('scrypt-v2$', 'scrypt-v1$', 'scrypt-netlify-v1$'))
    if needs_scrypt and not hasattr(hashlib, 'scrypt'):
        raise Denied('This host cannot verify scrypt credentials. Sign in through the '
                     'workspace host, which can.', 503)
    try:
        versioned = _V2.match(encoded or '')
        if versioned:
            n, r, p = (int(versioned.group(index)) for index in (1, 2, 3))
            # A stored hash claiming an absurd cost would demand gigabytes of memory
            # on every attempt from anyone who can reach the login form.
            if not 1024 <= n <= 1048576 or not 1 <= r <= 32 or not 1 <= p <= 16:
                return False
            salt, expected = bytes.fromhex(versioned.group(4)), bytes.fromhex(versioned.group(5))
            # The salt is the ASCII text of the stored hex, matching how it was created
            # and how the hosted function reads it. Hashing the sixteen decoded bytes
            # instead would produce a different digest and report a correct password as
            # wrong.
            return hmac.compare_digest(
                _derive(password, versioned.group(4).encode(), 'scrypt-v1', (n, r, p)), expected)
        version, salt, expected = encoded.split('$')
        if version == 'scrypt-netlify-v1':
            # The earliest Netlify login: hex fields, and a cost fixed by the name.
            if not re.fullmatch(r'[0-9a-f]{32}', salt) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                return False
            return hmac.compare_digest(
                _derive(password, salt.encode(), 'scrypt-v1', _LEGACY_SCRYPT), bytes.fromhex(expected))
        if version not in {'scrypt-v1', 'pbkdf2-sha256-v1'}:
            return False
        salt, expected = base64.b64decode(salt, validate=True), base64.b64decode(expected, validate=True)
        if len(salt) != 16 or len(expected) != 32:
            return False
        params = _LEGACY_SCRYPT if version == 'scrypt-v1' else None
    except (ValueError, AttributeError):
        return False
    # A hash with no parameters in it carries the cost it was made with under its own
    # name. Reading it at the current default would silently stop every existing
    # account from verifying, and would look exactly like every account having the
    # wrong password at once.
    return hmac.compare_digest(_derive(password, salt, version, params), expected)


def public_user(row):
    return {key: bool(row[key]) if key in {'active', 'must_change_password'} else row[key]
            for key in ('user_id', 'username', 'role', 'active', 'must_change_password')}


def _event(conn, kind, actor=None, target=None):
    conn.execute('INSERT INTO auth_events (event_id,actor_id,target_id,event_type,occurred_at) VALUES (?,?,?,?,?)',
                 (secrets.token_hex(16), actor, target, kind, now().isoformat()))


def create_user(name, password, role='viewer', actor=None, must_change=True):
    name = username(name)
    if role not in ROLES or type(must_change) is not bool:
        raise ValueError('Choose developer or viewer and a valid password-change setting.')
    encoded, uid, stamp = password_hash(password), secrets.token_hex(16), now().isoformat()
    conn = db.connect()
    try:
        with conn.transaction():
            # Serialise account changes in both supported database dialects.
            conn.execute("UPDATE auth_users SET updated_at=updated_at WHERE role='developer'")
            if conn.execute('SELECT user_id FROM auth_users WHERE username=?', (name,)).fetchone():
                raise ValueError('That username already exists.')
            conn.execute('INSERT INTO auth_users (user_id,username,password_hash,role,active,must_change_password,created_at,updated_at) '
                         'VALUES (?,?,?,?,?,?,?,?)', (uid, name, encoded, role, True, must_change, stamp, stamp))
            _event(conn, 'user_created', actor, uid)
        return public_user(dict(user_id=uid, username=name, role=role, active=True, must_change_password=must_change))
    finally:
        conn.close()


def list_users():
    return [public_user(row) for row in db.query('SELECT * FROM auth_users ORDER BY created_at,username')]


def login(name, password):
    try:
        name = username(name)
    except ValueError:
        name = ''
    bucket, current = _digest(name), now()
    attempt = db.query_one('SELECT * FROM auth_login_attempts WHERE bucket=?', (bucket,))
    if attempt and attempt['failures'] >= 5 and (current - _stamp(attempt['window_start'])).total_seconds() < 900:
        raise Denied('Too many login attempts. Try again later.', 429)
    row = db.query_one('SELECT * FROM auth_users WHERE username=?', (name,))
    # Missing users pay the same password verification cost.
    dummy = _algorithm() + '$' + base64.b64encode(b'\0' * 16).decode() + '$' + base64.b64encode(b'\0' * 32).decode()
    valid = verify(password, row['password_hash'] if row else dummy)
    conn = db.connect()
    try:
        with conn.transaction():
            if not valid or not row or not row['active']:
                # Lock/re-read the throttle counter so concurrent failures cannot reset it.
                conn.execute('INSERT INTO auth_login_attempts (bucket,failures,window_start) VALUES (?,?,?) '
                             'ON CONFLICT (bucket) DO NOTHING', (bucket, 0, current.isoformat()))
                conn.execute('UPDATE auth_login_attempts SET failures=failures WHERE bucket=?', (bucket,))
                old = conn.execute('SELECT * FROM auth_login_attempts WHERE bucket=?', (bucket,)).fetchone()
                within = (current - _stamp(old['window_start'])).total_seconds() < 900
                conn.execute('UPDATE auth_login_attempts SET failures=?,window_start=? WHERE bucket=?',
                             (old['failures'] + 1 if within else 1, old['window_start'] if within else current.isoformat(), bucket))
                _event(conn, 'login_failed')
            else:
                # Account changes during password verification must invalidate this login.
                current_user = conn.execute('SELECT * FROM auth_users WHERE user_id=?', (row['user_id'],)).fetchone()
                if not current_user['active'] or current_user['password_hash'] != row['password_hash']:
                    raise Denied('Invalid username or password.', 401)
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                conn.execute('DELETE FROM auth_login_attempts WHERE bucket=?', (bucket,))
                conn.execute('INSERT INTO auth_sessions (token_hash,user_id,csrf_token,created_at,last_seen_at,expires_at) '
                             'VALUES (?,?,?,?,?,?)', (_digest(token), row['user_id'], csrf, current.isoformat(),
                                                     current.isoformat(), (current + timedelta(seconds=SESSION_SECONDS)).isoformat()))
                _event(conn, 'login_succeeded', row['user_id'])
                return token, dict(user=public_user(current_user), csrf_token=csrf)
    finally:
        conn.close()
    raise Denied('Invalid username or password.', 401)


def authenticate(token, touch=True):
    if not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9_\-]{40,64}', token):
        raise Denied('Sign in to continue.', 401)
    row = db.query_one('SELECT u.*,s.token_hash,s.csrf_token,s.last_seen_at,s.expires_at,s.revoked_at '
                       'FROM auth_sessions s JOIN auth_users u ON u.user_id=s.user_id WHERE s.token_hash=?', (_digest(token),))
    current = now()
    if (not row or not row['active'] or row['revoked_at'] or _stamp(row['expires_at']) <= current
            or (current - _stamp(row['last_seen_at'])).total_seconds() >= IDLE_SECONDS):
        raise Denied('Your session ended. Sign in again.', 401)
    if touch and (current - _stamp(row['last_seen_at'])).total_seconds() >= 60:
        db.execute('UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=? AND revoked_at IS NULL',
                   (current.isoformat(), row['token_hash']))
    return dict(row)


def logout(principal):
    conn = db.connect()
    try:
        with conn.transaction():
            conn.execute('UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?',
                         (now().isoformat(), principal['token_hash']))
            _event(conn, 'logout', principal['user_id'])
    finally:
        conn.close()


def change_password(principal, current_password, new_password):
    row = db.query_one('SELECT * FROM auth_users WHERE user_id=?', (principal['user_id'],))
    if not verify(current_password, row['password_hash']):
        raise Denied('Current password is incorrect.')
    encoded = password_hash(new_password)
    conn = db.connect()
    try:
        with conn.transaction():
            current = conn.execute('SELECT * FROM auth_users WHERE user_id=?', (row['user_id'],)).fetchone()
            if current['password_hash'] != row['password_hash']:
                raise Denied('Account changed. Sign in again.', 401)
            conn.execute('UPDATE auth_users SET password_hash=?,must_change_password=?,updated_at=? WHERE user_id=?',
                         (encoded, False, now().isoformat(), row['user_id']))
            conn.execute('UPDATE auth_sessions SET revoked_at=? WHERE user_id=?', (now().isoformat(), row['user_id']))
            _event(conn, 'password_changed', row['user_id'])
    finally:
        conn.close()


def update_user(uid, actor, role=None, active=None, password=None):
    if role is not None and role not in ROLES or active is not None and type(active) is not bool:
        raise ValueError('Invalid role or account state.')
    encoded = password_hash(password) if password is not None else None
    conn = db.connect()
    try:
        with conn.transaction():
            conn.execute("UPDATE auth_users SET updated_at=updated_at WHERE role='developer'")
            row = conn.execute('SELECT * FROM auth_users WHERE user_id=?', (uid,)).fetchone()
            if row is None:
                raise ValueError('Account does not exist.')
            next_role, next_active = role or row['role'], row['active'] if active is None else active
            if row['role'] == 'developer' and row['active'] and (next_role != 'developer' or not next_active):
                count = conn.execute("SELECT COUNT(*) AS n FROM auth_users WHERE role='developer' AND active=TRUE").fetchone()['n']
                if count <= 1:
                    raise ValueError('Keep at least one active developer account.')
            conn.execute('UPDATE auth_users SET role=?,active=?,password_hash=?,must_change_password=?,updated_at=? WHERE user_id=?',
                         (next_role, bool(next_active), encoded or row['password_hash'], True if encoded else bool(row['must_change_password']),
                          now().isoformat(), uid))
            conn.execute('UPDATE auth_sessions SET revoked_at=? WHERE user_id=?', (now().isoformat(), uid))
            _event(conn, 'user_updated', actor, uid)
        return list_users()
    finally:
        conn.close()


def activity():
    return [dict(row) for row in db.query('SELECT * FROM auth_events ORDER BY occurred_at DESC LIMIT 100')]


def record_denial(principal, method, path):
    # Log only the registered path, never a query string or submitted payload.
    conn = db.connect()
    try:
        _event(conn, 'access_denied:' + method + ':' + path, principal['user_id'])
    finally:
        conn.close()


def cli(args):
    import getpass
    from pathlib import Path
    try:
        if args.action == 'list':
            value = list_users()
        else:
            if args.password_file:
                path = Path(args.password_file)
                if path.stat().st_mode & 0o077:
                    raise ValueError('Password file must be private (chmod 600).')
                password = path.read_text().rstrip('\r\n')
            else:
                password = getpass.getpass('Password (15+ characters): ')
                if password != getpass.getpass('Confirm password: '):
                    raise ValueError('Passwords do not match.')
            if args.action == 'create':
                value = create_user(args.username, password, args.role)
            else:
                row = db.query_one('SELECT user_id FROM auth_users WHERE username=?', (username(args.username),))
                if not row:
                    raise ValueError('Account does not exist.')
                value = update_user(row['user_id'], 'local_cli', password=password)
        print(json.dumps(value, default=str, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        import sys
        print(str(exc), file=sys.stderr)
        return 2
