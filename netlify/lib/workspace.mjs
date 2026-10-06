import {gunzipSync, gzipSync} from 'node:zlib';
import {Denied} from './auth.mjs';
import {dataService} from './data.mjs';

export const WORKSPACE_KEY = 'workspace';
export const MAX_PAYLOAD = 40 * 1024 * 1024;

/**
 * What a hosted deployment cannot do, said once.
 *
 * Every write the original dashboard offers -- collecting, checking, classifying,
 * drafting, listing, trading, training a model -- needs the runtime that owns the
 * research database. Netlify Functions cannot run it, so the function refuses
 * rather than answering, and every panel that depends on one renders this reason
 * in place of a number nobody measured.
 */
export const NEEDS_RUNTIME = 'This view is produced by the local research runtime. The hosted workspace replays exported data and never runs collection, trading or models.';

/** Item selectors return one fully materialised record, which a list row is not. */
const ITEM_SELECTORS = ['sku', 'draft_id', 'order_id', 'listing_pk', 'decision_pk'];

const LISTING_SORTS = {
  newest: (a, b) => String(b.last_seen_at || '').localeCompare(String(a.last_seen_at || '')) || b.listing_pk - a.listing_pk,
  oldest: (a, b) => String(a.last_seen_at || '').localeCompare(String(b.last_seen_at || '')) || a.listing_pk - b.listing_pk,
  price_asc: (a, b) => (a.listed_price_eur == null) - (b.listed_price_eur == null) || (a.listed_price_eur || 0) - (b.listed_price_eur || 0) || b.listing_pk - a.listing_pk,
  price_desc: (a, b) => (a.listed_price_eur == null) - (b.listed_price_eur == null) || (b.listed_price_eur || 0) - (a.listed_price_eur || 0) || b.listing_pk - a.listing_pk,
  title: (a, b) => String(a.title || '').toLowerCase().localeCompare(String(b.title || '').toLowerCase()) || b.listing_pk - a.listing_pk,
};
// Retain the former hosted-only name for existing bookmarked requests.
LISTING_SORTS.last_seen = LISTING_SORTS.newest;

/**
 * Segment filters select a different analysis, not a different page of one.
 *
 * A filter changes which rows the aggregates are measured over, so an export
 * cannot answer a filtered read from the unfiltered numbers. The runtime that
 * owns the database therefore measures one payload per filter combination the
 * interface offers, and the function picks between them. A combination nobody
 * exported is still refused, because recomputing an aggregate here would publish
 * a number this project never measured.
 *
 * The dimensions are the six the shell's filter bar can set. The values are not
 * enumerated here on purpose: the export owns that, and the key is a stable
 * digest of whichever combination arrived, so a new focus category needs no
 * change to this file.
 */
export const SEGMENT_KEYS = ['category_root_norm', 'category_norm', 'municipality_norm',
                             'condition_norm', 'seller_type_norm', 'focus_path'];

/** The markets the export covers. `standard` is the default the shell asks for. */
export const MARKETS = ['standard', 'diili'];

/** The desk settings the export was built at. A narrower request truncates it. */
export const DESK = {min_gap_pct: 20, horizon_days: 30};

/** Largest page any hosted read will serve, so one request cannot ask for everything. */
const MAX_PAGE_SIZE = 200;
const DEFAULT_PAGE_SIZE = 25;

const RECORD_ROUTES = {
  '/api/inventory': 'inventory',
  '/api/drafts': 'drafts',
  '/api/trades': 'trades',
};

/**
 * A stable key for a filter combination.
 *
 * Dimensions are ordered and only non-empty ones contribute, so `a=1` and `b=2`
 * produce different keys while a filter applied in a different order produces
 * the same one. The digest keeps the key short and free of characters that would
 * have to be escaped in a URL or a JSON pointer.
 */
export function segmentKey(params) {
  const set = SEGMENT_KEYS.filter(key => params.get(key));
  if (!set.length) return 'all';
  const material = set.map(key => `${key}=${params.get(key)}`).join('\n');
  let hash = 0x811c9dc5;
  for (let i = 0; i < material.length; i++) {
    hash ^= material.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `s${hash.toString(36)}`;
}

/** The filter values behind a key, so a refusal can name what to export. */
export function segmentLabel(params) {
  const set = SEGMENT_KEYS.filter(key => params.get(key));
  if (!set.length) return 'no filter';
  return set.map(key => `${key}=${params.get(key)}`).join(', ');
}

function page(items, params, size) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const requested = Number(params.get('page') || 1);
  if (!Number.isSafeInteger(requested) || requested < 1) throw new Denied(400, 'Invalid page.');
  const current = Math.min(requested, pages);
  return {items: items.slice((current - 1) * size, current * size), total: items.length,
          pages, page: current, page_size: size};
}

function pageSize(params) {
  const size = Number(params.get('page_size') || DEFAULT_PAGE_SIZE);
  if (!Number.isSafeInteger(size) || size < 1 || size > MAX_PAGE_SIZE) throw new Denied(400, 'Invalid page size.');
  return size;
}

function market(params) {
  const value = params.get('subplatform') || 'standard';
  if (!MARKETS.includes(value)) throw new Denied(400, 'Invalid market.');
  return value;
}

function search(value, limit, label) {
  const text = (value || '').trim().toLowerCase();
  if (text.length > limit) throw new Denied(400, `Filter text is too long. ${label}`);
  return text;
}

export function validateWorkspace(data) {
  if (data?.version !== 1 || !Number.isFinite(Date.parse(data.generated_at))
      || !data.payload?.panels?.national || !data.payload?.health || !data.payload?.quality
      || !data.payload?.liquidity || !data.payload?.coverage || !data.payload?.posture
      || !data.payload?.compliance || !data.records || !data.candidates) {
    throw new Denied(400, 'Use a workspace created by export_netlify_workspace.py.');
  }
  // Every desk report the export offers, whether for the market as a whole or for
  // one filter combination, has to carry its rows. A report without them would be
  // served as an empty shortlist, which reads as a measured zero rather than as a
  // broken export.
  for (const scope of [data.candidates, ...Object.values(data.segments?.candidates || {})]) {
    for (const report of Object.values(scope || {})) {
      if (!Array.isArray(report?.candidates)) throw new Denied(400, 'A desk report is missing its candidate rows.');
    }
  }
  return data;
}

export async function publishWorkspace(store, bytes) {
  let data;
  try {data = JSON.parse(gunzipSync(bytes, {maxOutputLength: MAX_PAYLOAD}).toString());}
  catch {throw new Denied(400, 'Upload a valid compressed JSON workspace (.json.gz).');}
  validateWorkspace(data);
  await store.set(WORKSPACE_KEY, gzipSync(Buffer.from(JSON.stringify(data))));
  return {generated_at: data.generated_at, uploaded_at: new Date().toISOString(),
          bytes: bytes.length, markets: Object.keys(data.candidates),
          segments: Object.keys(data.segments?.payloads || {}).length};
}

/**
 * Serve the original dashboard from the exported payload.
 *
 * The payload is the object `dashboard.snapshot()` built, so the shell renders the
 * same numbers the local runtime measured. It is immutable between publishes, which
 * is what lets one decompressed copy be held for the life of a warm function.
 */
export function workspaceService(store) {
  const data = dataService(store);
  let cachedEtag, cached;

  async function read() {
    const existing = await store.getWithMetadata(WORKSPACE_KEY, {type: 'arrayBuffer'});
    if (!existing) throw new Denied(503, 'A developer needs to publish the dashboard workspace first.');
    if (existing.etag !== cachedEtag) {
      let value;
      try {value = JSON.parse(gunzipSync(Buffer.from(existing.data), {maxOutputLength: MAX_PAYLOAD}).toString());}
      catch {throw new Denied(503, 'The published dashboard workspace is unreadable. Republish it from the local runtime.');}
      validateWorkspace(value);
      cached = value;
      cachedEtag = existing.etag;
    }
    return cached;
  }

  /** The measured payload for a filter combination, or the reason there is none. */
  function scoped(workspace, params) {
    const key = segmentKey(params);
    if (key === 'all') return workspace.payload;
    const payloads = workspace.segments?.payloads;
    if (!payloads || !payloads[key]) {
      throw new Denied(403, `This workspace exported the market as a whole but not the filter ${segmentLabel(params)}. Publish one that includes it, or clear the filters.`);
    }
    return payloads[key];
  }

  function desk(workspace, params) {
    const key = segmentKey(params);
    const report = key === 'all'
      ? workspace.candidates[market(params)]
      : workspace.segments?.candidates?.[key]?.[market(params)];
    if (!report) {
      const where = key === 'all'
        ? `the ${market(params)} market`
        : `the filter ${segmentLabel(params)}`;
      throw new Denied(403, `The exported shortlist was measured for ${where} at a ${DESK.min_gap_pct}% gap over ${DESK.horizon_days} days without peer rows. Publish a workspace that covers it to read it here.`);
    }
    return report;
  }

  async function snapshot(params) {
    const workspace = await read();
    return scoped(workspace, params);
  }

  async function health(params) {
    const workspace = await read();
    return scoped(workspace, params).health;
  }

  async function listings(params) {
    const {data: research} = await data.read();
    const place = market(params);
    const term = search(params.get('search'), 200, 'Search for up to 200 characters.');
    const category = params.get('category_norm') || '';
    const municipality = params.get('municipality_norm') || '';
    const state = params.get('availability') || 'all';
    let rows = research.listings.filter(row => row.subplatform === place);
    if (category) rows = rows.filter(row => row.category_norm === category);
    if (municipality) rows = rows.filter(row => row.municipality_norm === municipality);
    if (term) rows = rows.filter(row => (row.title || '').toLowerCase().includes(term) || String(row.listing_id || '').includes(term));
    if (state === 'available') rows = rows.filter(row => row.status_norm === 'active' && !row.is_reserved && row.outcome_status === 'unknown');
    else if (state === 'reserved') rows = rows.filter(row => row.is_reserved);
    else if (state === 'sold') rows = rows.filter(row => ['sold_confirmed', 'sold_inferred'].includes(row.outcome_status));
    else if (state === 'giveaway') rows = rows.filter(row => row.is_giveaway);
    else if (state === 'priced') rows = rows.filter(row => !row.is_giveaway && row.listed_price_eur > 0);
    else if (state !== 'all') throw new Denied(400, 'Invalid availability filter.');
    const sort = Object.hasOwn(LISTING_SORTS, params.get('sort') || 'newest') && LISTING_SORTS[params.get('sort') || 'newest'];
    if (!sort) throw new Denied(400, 'Invalid sort.');
    rows = [...rows].sort(sort);
    const result = page(rows, params, pageSize(params));
    // The row a peer comparison was measured against. The hosted function refuses
    // that comparison, so the value only has to be truthful, not resolvable.
    return {...result, items: result.items.map(row => ({...row, effective_filters: {}, reference_at: research.generated_at}))};
  }

  async function record(path, params) {
    if (ITEM_SELECTORS.some(key => params.get(key))) {
      throw new Denied(403, 'Opening a single record needs the local research runtime. The exported workspace serves the stored lists only.');
    }
    const {records} = await read();
    const {data: research} = await data.read();
    const key = RECORD_ROUTES[path];
    const rows = key === 'inventory' ? research.inventory
      : key === 'drafts' ? research.drafts : research.orders;
    const term = search(params.get('search'), 200, 'Search for up to 200 characters.');
    let items = Array.isArray(rows) ? rows : [];
    if (term) items = items.filter(row => JSON.stringify(row).toLowerCase().includes(term));
    if (path === '/api/inventory' && params.get('state')) items = items.filter(row => row.state === params.get('state'));
    return {...records[key], ...page(items, params, pageSize(params))};
  }

  /** `/api/research` pairs saved segments with the decision journal. */
  async function research(params) {
    if (ITEM_SELECTORS.some(key => params.get(key))) {
      throw new Denied(403, 'Opening a single decision needs the local research runtime. The exported workspace serves the stored journal only.');
    }
    const {records} = await read();
    const {data: snapshot} = await data.read();
    const items = Array.isArray(snapshot.journal) ? snapshot.journal : [];
    const term = search(params.get('search'), 200, 'Search for up to 200 characters.');
    const filtered = term ? items.filter(row => JSON.stringify(row).toLowerCase().includes(term)) : items;
    return {segments: records.segments || [],
            journal: {...records.journal, ...page(filtered, params, pageSize(params))}};
  }

  async function scenarios() {
    const {records} = await read();
    const {data: snapshot} = await data.read();
    return {...records.scenarios, entries: Array.isArray(snapshot.scenarios) ? snapshot.scenarios : []};
  }

  /**
   * The desk shortlist was exported once per filter combination.
   *
   * `limit` only truncates an ordered queue, so the exported list is sliced to
   * the requested width and says so. The threshold, horizon and peer rows decide
   * *which* items qualify and what evidence each row carries, so a request that
   * differs from the export is refused instead of answered with rows the operator
   * did not ask for.
   */
  async function candidates(params) {
    const workspace = await read();
    // Peer rows were excluded from the export because the hosted shell never asks
    // for them and each one is a comparable table per candidate. Asking is a
    // refusal rather than a silently shorter answer, so the caller knows the
    // evidence it is missing is absent rather than merely unrequested.
    if (Number(params.get('peer_rows') || 0) !== 0) {
      throw new Denied(403, 'The exported shortlist carries no peer comparison rows. Publish a workspace with peer evidence to read them here.');
    }
    const report = desk(workspace, params);
    const limit = Number(params.get('limit') || DEFAULT_PAGE_SIZE);
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_PAGE_SIZE) throw new Denied(400, 'Invalid candidate limit.');
    const rows = Array.isArray(report.candidates) ? report.candidates : [];
    return {...report, candidates: rows.slice(0, limit),
            summary: {...report.summary, truncated: limit < rows.length}};
  }

  /**
   * Every read this deployment answers, as data rather than as a hand-kept list.
   *
   * The table is the routing table, so a route cannot be added to one and
   * forgotten in the other. The interface reads this before it draws a control
   * rather than discovering the absence from a 404.
   */
  const ROUTES = {
    '/api/snapshot': snapshot,
    '/api/health': health,
    '/api/listings': listings,
    '/api/research': research,
    '/api/research/candidates': candidates,
    '/api/research/scenarios': scenarios,
    ...Object.fromEntries(Object.keys(RECORD_ROUTES).map(path => [path, params => record(path, params)])),
  };

  /** True for a route this deployment serves; false means the honest refusal. */
  function serves(path) {
    return Object.hasOwn(ROUTES, path);
  }

  function query(path, params) {
    const handler = ROUTES[path];
    if (!handler) throw new Denied(404, NEEDS_RUNTIME);
    return handler(params);
  }

  return {query, read, serves, manifest: () => ({reads: Object.keys(ROUTES), writes: []})};
}
