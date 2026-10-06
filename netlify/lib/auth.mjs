import {randomBytes, randomUUID, createHash, createHmac, scrypt, timingSafeEqual} from 'node:crypto';
import {promisify} from 'node:util';

const derive = promisify(scrypt);
export const COOKIE = '__Host-tori_session';
const hash = value => createHash('sha256').update(value).digest('hex');
const token = () => randomBytes(32).toString('base64url');

/**
 * The CSRF token of a session, derived from the session itself.
 *
 * It is not stored. A session already stores the SHA-256 of its bearer secret, and
 * that digest is enough to derive a token that is stable for the life of the
 * session, different for every session, and unknowable to anyone who does not
 * already hold the secret. Deriving rather than storing means no copy of the token
 * exists anywhere at all -- not in the clear in the store, not in a second field
 * to keep in step with the first -- and revoking the session revokes the token
 * with it.
 */
const deriveCsrf = tokenHash => createHmac('sha256', tokenHash).update('csrf-v1').digest('base64url');
export class Denied extends Error {
  constructor(status, message) {super(message); this.status = status;}
}
export function username(value) {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9._@+-]{3,150}$/.test(value)) throw new Denied(400, 'Use 3–150 letters, numbers, dots, or @ in the username.');
  return value.toLowerCase();
}

/**
 * Password hashing parameters, named once.
 *
 * They were previously written out at both the hashing and the verifying call
 * site, which meant a cost increase applied to one and not the other would
 * silently make every stored hash unverifiable. The parameters are part of the
 * encoded hash instead, so a stored credential always carries the cost it was
 * created with and a default change can never invalidate anything.
 */
const CURRENT = {N:131072, r:8, p:1};
const LEGACY = {N:32768, r:8, p:3};
const KEY_BYTES = 32;
const SALT_BYTES = 16;

/** `maxmem` has to admit `128 * N * r` plus room for the block buffers. */
function memoryFor({N, r}) {return 128 * N * r + 32 * 1024 * 1024;}

/**
 * The stored form of a new password: `scrypt-v2$N$r$p$salt$digest`.
 *
 * The scheme name says the encoding as well as the KDF. `scrypt-v1` below is
 * hex salt and hex digest; the Python runtime's own `scrypt-v1` is base64 for
 * both, and Python reads a Netlify hash under the name `scrypt-netlify-v1`
 * precisely because one name for two encodings fails silently rather than loudly.
 * A versioned name makes the two distinguishable.
 */
export async function passwordHash(value) {
  if (typeof value !== 'string' || value.length < 15 || value.length > 256) throw new Denied(400, 'Passwords must contain 15–256 characters.');
  const salt = randomBytes(SALT_BYTES).toString('hex');
  const result = await derive(value, salt, KEY_BYTES, {...CURRENT, maxmem:memoryFor(CURRENT)});
  return `scrypt-v2$${CURRENT.N}$${CURRENT.r}$${CURRENT.p}$${salt}$${result.toString('hex')}`;
}

const V2 = /^scrypt-v2\$(\d+)\$(\d+)\$(\d+)\$([a-f0-9]{32})\$([a-f0-9]{64})$/;
// Accepted so an account created before the versioned scheme still signs in. It
// carries no parameters, so the legacy cost is fixed by the name.
const V1 = /^scrypt-v1\$([a-f0-9]{32})\$([a-f0-9]{64})$/;

/** Whether a stored hash was made with weaker parameters than the current default. */
export function needsRehash(encoded) {
  const parsed = String(encoded || '').match(V2);
  if (!parsed) return true;
  const [, N, r, p] = parsed.map(Number);
  return N < CURRENT.N || r < CURRENT.r || p < CURRENT.p;
}

export async function verifyPassword(value, encoded) {
  if (typeof value !== 'string' || value.length > 256) return false;
  const text = String(encoded || '');
  const v2 = text.match(V2);
  const v1 = v2 ? null : text.match(V1);
  const parameters = v2 ? {N:Number(v2[1]), r:Number(v2[2]), p:Number(v2[3])} : LEGACY;
  // `V2` captures N, r and p before the salt; `V1` captures the salt first.
  const [salt, digest] = v2 ? v2.slice(4) : v1 ? v1.slice(1) : [];
  // A hash claiming an absurd cost would let one stored row demand gigabytes of
  // memory on every login attempt, so the parameters are bounded as well as parsed.
  if (!digest || !/^[a-f0-9]{32}$/.test(salt) || parameters.N < 1024 || parameters.N > 1048576
      || parameters.r < 1 || parameters.r > 32 || parameters.p < 1 || parameters.p > 16) return false;
  const result = await derive(value, salt, KEY_BYTES, {...parameters, maxmem:memoryFor(parameters)});
  return timingSafeEqual(result, Buffer.from(digest, 'hex'));
}

const dummy = `scrypt-v2$${CURRENT.N}$${CURRENT.r}$${CURRENT.p}$${'0'.repeat(32)}$${'0'.repeat(64)}`;
export const publicUser = user => Object.fromEntries(['user_id','username','role','active','must_change_password','created_at'].map(key => [key,user[key]]));

export async function newUser(name, password, role, mustChange = true) {
  if (!['developer','viewer'].includes(role)) throw new Denied(400, 'Choose developer or viewer.');
  return {user_id:randomUUID(), username:username(name), password_hash:await passwordHash(password), role, active:true, must_change_password:mustChange, created_at:new Date().toISOString()};
}
export function cookie(value = '', clear = false) {
  return `${COOKIE}=${value}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=${clear?0:43200}`;
}
export function cookieToken(request) {
  return (request.headers.get('cookie') || '').split(';').map(value=>value.trim()).find(value=>value.startsWith(COOKIE+'='))?.slice(COOKIE.length+1) || '';
}
function event(state, kind, actor, target) {
  state.events.unshift({event_type:kind, actor_id:actor || null, target_id:target || null, occurred_at:new Date().toISOString()});
  state.events = state.events.slice(0,100);
}
/**
 * Login throttles, and the memory they are allowed to occupy.
 *
 * Each bucket is keyed by something an unauthenticated caller controls, so the
 * number of live buckets is attacker-controlled too. The cap below is a memory
 * bound and nothing else: exceeding it evicts the oldest bucket rather than
 * refusing the request. Refusing it was a denial of service on itself -- a caller
 * who introduced more than `MAX_BUCKETS` distinct keys filled the store with
 * unexpired rows, every later login was refused for exceeding a cap the caller
 * had created, and because nothing prunes without traffic the store stayed full.
 *
 * The controls that actually stop guessing are the per-name and per-address
 * ceilings, which a rotation of keys does not avoid. Evicting keeps those
 * enforceable and leaves no way to wedge the store shut.
 */
// Every key takes both parts of the request so the loop can call them uniformly; a
// bucket uses the one it needs. Written this way rather than with mixed arities
// because a key that silently read the wrong argument would key the per-address
// throttle on the username -- which a rotation of usernames walks straight past.
const THROTTLES = [
  {key: (name, _address) => hash('name:' + name), max: 5, window: 900000},
  {key: (name, address) => hash('ip:' + address), max: 30, window: 900000},
  // A site-wide ceiling bounds the hashing cost an attacker can cause from many
  // addresses at once. It is deliberately higher than the expected login rate and
  // is given back on success, so ordinary use never spends it.
  {key: () => 'global', max: 300, window: 60000},
];
const MAX_BUCKETS = 1024;

function prune(state, now) {
  state.sessions = state.sessions.filter(row => row.expires > now && row.last_seen + 1800000 > now);
  for (const [key,row] of Object.entries(state.attempts)) if (row.start + row.window <= now) delete state.attempts[key];
  // A session written before the CSRF token was derived rather than stored carries
  // a cleartext copy that nothing reads any more. Dropping it here removes it the
  // next time anything writes the store at all.
  for (const row of state.sessions) delete row.csrf_token;
}

/** Drop the oldest buckets until the store is back inside its memory bound. */
function evict(state) {
  const keys = Object.keys(state.attempts);
  if (keys.length <= MAX_BUCKETS) return;
  keys.sort((a, b) => state.attempts[a].start - state.attempts[b].start);
  for (const key of keys.slice(0, keys.length - MAX_BUCKETS)) delete state.attempts[key];
}
// Every account/session mutation uses the storage service's conditional ETag
// write. Concurrent logins and role changes cannot overwrite each other.
export async function mutate(store, change) {
  for (let attempt=0; attempt<8; attempt++) {
    const existing = await store.getWithMetadata('state', {type:'json'});
    if (!existing) throw new Denied(503, 'Accounts are not provisioned yet.');
    const state = existing.data;
    prune(state, Date.now());
    const result = await change(state);
    const write = await store.setJSON('state', state, {onlyIfMatch:existing.etag});
    if (write.modified) return result;
  }
  throw new Denied(503, 'The workspace is busy. Try again shortly.');
}
export async function login(store, name, password, ip) {
  let normalized;
  try {normalized = username(name);} catch {normalized = '';}
  const user = await mutate(store, state => {
    const now = Date.now();
    for (const {key, max, window} of THROTTLES) {
      const bucket = key(normalized, ip);
      let row = state.attempts[bucket];
      if (!row || row.start+window<=now) row = state.attempts[bucket] = {count:0,start:now,window};
      if (row.count >= max) throw new Denied(429, 'Too many login attempts. Try again later.');
      row.count++;
    }
    evict(state);
    return state.users.find(row => row.username === normalized);
  });
  const valid = await verifyPassword(typeof password==='string'?password:'', user?.password_hash || dummy);
  if (!valid || !user?.active) throw new Denied(401, 'Incorrect username or password.');
  const secret = token();
  const upgraded = needsRehash(user.password_hash);
  const result = await mutate(store, async state => {
    const now = Date.now();
    const current = state.users.find(row => row.user_id === user.user_id);
    if (!current?.active || current.password_hash!==user.password_hash) throw new Denied(401, 'Incorrect username or password.');
    if (state.sessions.length>=256) throw new Denied(429, 'Too many active sessions.');
    // A successful sign-in spends one unit of each ceiling it touched, including
    // the site-wide one, so a workspace that logs people in normally cannot push
    // itself over the limit it sets for everyone else.
    for (const {key, window} of THROTTLES) {
      const row = state.attempts[key(normalized, ip)];
      if (row && row.start+window>now) row.count = Math.max(0, row.count-1);
    }
    delete state.attempts[hash('name:'+normalized)];
    // A credential made with weaker parameters than the current default is
    // rewritten now that the plaintext is available. The session it belongs to
    // stays valid; only the stored hash changes.
    if (upgraded) current.password_hash = await passwordHash(password);
    const tokenHash = hash(secret);
    state.sessions.push({token_hash:tokenHash, user_id:current.user_id, created:now, last_seen:now, expires:now+43200000});
    event(state,'login',current.user_id);
    return {user:publicUser(current), csrf_token:deriveCsrf(tokenHash)};
  });
  return {...result, secret};
}

export async function authenticate(store, request) {
  const secret = cookieToken(request);
  if (!/^[a-zA-Z0-9_-]{43}$/.test(secret)) throw new Denied(401, 'Sign in to access this workspace.');
  const state = await store.get('state', {type:'json'});
  const now = Date.now(), session = state?.sessions.find(row=>row.token_hash===hash(secret) && row.expires>now && row.last_seen+1800000>now);
  const user = state?.users.find(row=>row.user_id===session?.user_id && row.active);
  if (!session || !user) throw new Denied(401, 'Your session has expired. Sign in again.');
  if (session.last_seen+60000<now) await mutate(store, value=>{
    const row=value.sessions.find(row=>row.token_hash===session.token_hash);
    if (!row) throw new Denied(401,'Your session has expired. Sign in again.');
    row.last_seen=now;
  });
  // A fingerprint of the stored hash rather than the hash itself. It answers the
  // only question a caller asks of it -- "is this still the credential the session
  // was created against" -- without handing the password hash to every route that
  // authenticates.
  return {user:publicUser(user), token_hash:session.token_hash, csrf_token:deriveCsrf(session.token_hash),
          credential:hash(user.password_hash)};
}

/**
 * Constant-time check of a supplied CSRF secret against a session.
 *
 * Compares the derived token rather than a stored one, so there is no stored copy
 * to compare and no stored copy to leak. The length is fixed by the derivation, so
 * the equality case is the only case that reaches the comparison.
 */
export function csrfMatches(supplied, tokenHash) {
  const expected = Buffer.from(deriveCsrf(tokenHash));
  const provided = Buffer.from(String(supplied || ''));
  if (provided.length !== expected.length) return false;
  return timingSafeEqual(provided, expected);
}


export function requireDeveloper(principal) {
  if (principal.user.role!=='developer') throw new Denied(403, 'Developer access is required.');
}
export async function logout(store, principal) {
  await mutate(store, state=>{state.sessions=state.sessions.filter(row=>row.token_hash!==principal.token_hash);event(state,'logout',principal.user.user_id);});
}
export async function changePassword(store, principal, oldPassword, nextPassword) {
  const encoded = await passwordHash(nextPassword);
  if (oldPassword===nextPassword) throw new Denied(400,'Choose a different password.');
  await mutate(store, async state=>{
    const user=state.users.find(row=>row.user_id===principal.user.user_id);
    if (!user?.active || hash(user.password_hash)!==principal.credential) throw new Denied(401,'Sign in again.');
    // Verified inside the atomic write, so a password that was changed between
    // this request being authenticated and this line running cannot slip through.
    if (!await verifyPassword(oldPassword, user.password_hash)) throw new Denied(400,'The current password is incorrect.');
    user.password_hash=encoded;user.must_change_password=false;
    state.sessions=state.sessions.filter(row=>row.user_id!==user.user_id);
    event(state,'password_changed',user.user_id);
  });
}
export async function manageUser(store, principal, body) {
  requireDeveloper(principal);
  const params=body.params || {};
  const created=body.action==='create'?await newUser(params.username,params.password,params.role):null;
  const encoded=body.action==='update'&&params.password!==undefined?await passwordHash(params.password):null;
  return mutate(store,state=>{
    // Check the developer's session again inside the atomic write.
    const actor=state.users.find(row=>row.user_id===principal.user.user_id);
    if (!actor?.active || actor.role!=='developer' || !state.sessions.some(row=>row.token_hash===principal.token_hash)) throw new Denied(403,'Developer access is required.');
    if (created) {
      if (state.users.some(row=>row.username===created.username)) throw new Denied(409,'This username already exists.');
      if (state.users.length>=64) throw new Denied(400,'This workspace supports up to 64 accounts.');
      state.users.push(created);event(state,'user_created',actor.user_id,created.user_id);
      return {user:publicUser(created)};
    }
    if (body.action!=='update') throw new Denied(400,'Unknown account action.');
    const user=state.users.find(row=>row.user_id===params.user_id);
    if (!user) throw new Denied(404,'Account not found.');
    if (params.role!==undefined&&!['developer','viewer'].includes(params.role)) throw new Denied(400,'Invalid role.');
    if (params.active!==undefined&&typeof params.active!=='boolean') throw new Denied(400,'Invalid account state.');
    const next={...user,role:params.role??user.role,active:params.active??user.active};
    if (!(next.active&&next.role==='developer')&&!state.users.some(row=>row.user_id!==user.user_id&&row.active&&row.role==='developer')) throw new Denied(409,'Keep at least one active developer account.');
    user.role=next.role;user.active=next.active;
    if (encoded) {user.password_hash=encoded;user.must_change_password=true;}
    state.sessions=state.sessions.filter(row=>row.user_id!==user.user_id);
    event(state,'user_updated',actor.user_id,user.user_id);
    return {user:publicUser(user)};
  });
}
