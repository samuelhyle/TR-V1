/**
 * What this deployment can actually do.
 *
 * The interface used to draw every control and discover the answer from a 404. On
 * a read-only deployment that meant fifteen views whose every panel refused, plus
 * live controls -- "Record a purchase", the collection controls, the seller
 * connections -- that were drawn enabled and only discovered to be dead on click.
 * A refusal is the correct answer to a request; it is the wrong thing to build a
 * page out of.
 *
 * So the deployment is asked once, before anything is drawn, and the answer is used
 * to leave out what is not there. The server is the authority: the local runtime
 * reports the routes it owns, the Netlify function reports the subset it can answer
 * from the workspace it was given, and this module only compares the two. Nothing
 * about a deployment is hard-coded here.
 */

/**
 * The reads each view needs before it can render.
 *
 * A view is offered only when every one of these is answerable, so a view is never
 * reachable in a state where its first panel refuses. A view with no entry is
 * always offered -- it needs no read to draw its shell.
 */
export const VIEW_READS = {
  analyst: ['/api/analysis'],
  opportunities: ['/api/opportunities'],
  research: ['/api/research', '/api/research/candidates', '/api/research/scenarios'],
  item: ['/api/item-workspace'],
  focus: ['/api/focus'],
  overview: ['/api/snapshot'],
  observations: ['/api/listings'],
  predict: ['/api/predict'],
  journal: ['/api/research'],
  inventory: ['/api/inventory'],
  drafts: ['/api/drafts'],
  orders: ['/api/trades'],
  workflow: ['/api/workflow'],
  ledger: ['/api/snapshot'],
  collection: ['/api/collection/control'],
  operations: ['/api/operations'],
  imports: ['/api/soldextract'],
  connections: ['/api/connections'],
};

/** The writes the interface can issue, so each control can be disabled by name. */
export const CONTROL_WRITES = {
  'ledger-record': '/api/ledger',
  'decision-save': '/api/research',
  'segment-save': '/api/research',
  'desk-session-save': '/api/research/session',
  'inventory-write': '/api/inventory',
  'draft-write': '/api/drafts',
  'trade-write': '/api/trades',
  'scenario-write': '/api/research/scenarios',
  'outcomes-write': '/api/outcomes',
  'analyst-run': '/api/analyst',
  'model-run': '/api/predict',
  'capture-import': '/api/soldextract',
  'source-review-write': '/api/research/source-review',
  'connection-write': '/api/connections',
  'collection-control': '/api/collection/control',
  'collection-start': '/api/collection/start',
  'operation-run': '/api/operations',
};

/**
 * A blank capability set: nothing readable, nothing writable.
 *
 * The default matters. Until the server has answered, the interface knows nothing,
 * and knowing nothing is not the same as knowing everything -- so nothing is drawn
 * as usable and the read-only banner is already up rather than appearing a moment
 * later over controls that were briefly live.
 */
export function emptyCapabilities() {
  return {version: 1, deployment: 'unknown', detail: '', role: null, reads: new Set(), writes: new Set()};
}

/** Read a capability report defensively. A malformed answer is treated as no answer. */
export function parseCapabilities(body) {
  const blank = emptyCapabilities();
  if (!body || body.version !== 1 || !Array.isArray(body.reads) || !Array.isArray(body.writes)) return blank;
  return {version: 1, deployment: String(body.deployment || 'unknown'),
          detail: String(body.detail || ''), role: body.role ?? null,
          readOnly: body.read_only === true,
          reads: new Set(body.reads.filter(path => typeof path === 'string')),
          writes: new Set(body.writes.filter(path => typeof path === 'string'))};
}

/** True when every read a view needs is answerable here. */
export function canReadView(capabilities, view) {
  const needed = VIEW_READS[view];
  if (!needed) return true;
  return needed.every(path => capabilities.reads.has(path));
}

/** The first read a view is missing, or null. Names the reason a view is withheld. */
export function missingRead(capabilities, view) {
  return (VIEW_READS[view] || []).find(path => !capabilities.reads.has(path)) || null;
}

export function canWrite(capabilities, write) {
  return capabilities.writes.has(write);
}

/** Views that cannot be rendered at all, in navigation order. */
export function unavailableViews(capabilities, views) {
  return views.filter(view => !canReadView(capabilities, view));
}

/**
 * Controls this deployment cannot use.
 *
 * A `disabled` assignment made by a render pass would otherwise undo the block: the
 * collection panel recomputes its buttons from `control?.can_start`, the connections
 * panel recomputes from the last account state, and each of those runs after the
 * capability answer and would set `disabled` back to false on a control that has to
 * stay unusable. The blocked set is kept here so a render pass can consult it rather
 * than needing to know that capabilities exist.
 */
const blockedWrites = new WeakSet();

/** True when this deployment cannot perform the write this control performs. */
export function isWriteBlocked(node) {
  return !!node && blockedWrites.has(node);
}

/**
 * Combine a render pass's own reason for disabling with the deployment's.
 *
 * `panelSaysDisabled` is what the panel decided; the block wins if either holds.
 * Returns the value to assign, so a call site reads as one expression rather than as
 * two rules that have to be remembered together.
 */
export function writeDisabled(node, panelSaysDisabled = false) {
  return isWriteBlocked(node) || panelSaysDisabled;
}

/**
 * Apply the answer to the page.
 *
 * Three things, in order of how much they matter. Views that cannot render are
 * removed from the navigation, because a link that leads to a refusal is worse than
 * no link: it costs the reader a click to learn nothing. Controls that would write
 * are disabled, because a control that looks live and fails on click is a lie about
 * what the deployment can do. And one banner states what this deployment is, so the
 * numbers on screen are never mistaken for live ones.
 *
 * Returns a summary for tests and for the record the operator sees.
 */
export function applyCapabilities(capabilities, {document: doc, views, fallbackView, reason}) {
  const withheld = unavailableViews(capabilities, views);
  for (const view of withheld) {
    const button = doc.querySelector(`[data-view="${view}"]`);
    if (button) button.remove();
    doc.querySelectorAll(`[data-go="${view}"]`).forEach(node => {
      if (node.tagName === 'BUTTON') {blockedWrites.add(node); node.disabled = true;}
      else node.setAttribute('aria-disabled', 'true');
    });
  }
  const blocked = [];
  for (const [control, write] of Object.entries(CONTROL_WRITES)) {
    if (canWrite(capabilities, write)) continue;
    for (const node of doc.querySelectorAll(`[data-write="${control}"]`)) {
      blockedWrites.add(node);
      node.disabled = true;
      node.dataset.writeUnavailable = write;
      blocked.push(node.id || control);
    }
  }
  const summary = {withheld, blocked, total: views.length, fallback: withheld.includes(fallbackView) ? null : fallbackView};
  if (reason) reason(summary, capabilities);
  return summary;
}
