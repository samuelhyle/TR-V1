/**
 * The acquisition path: turning a stored listing into a filled-in form.
 *
 * The buy side is the only evidence source in this system with no rate limit, no
 * waiting and no external dependency, and it is still empty. Everything here
 * exists to make recording an acquisition the path of least resistance.
 *
 * The function that matters is `acquisitionFormPlan`. It decides what the form
 * should contain, and it is pure so the rule that matters can be tested rather
 * than reviewed: an asking price is an observation and may be shown as context,
 * but it must never be written into an acquisition cost field. Doing that would
 * fabricate the exact figure the owned ledger exists to measure, and it would do
 * so silently, in a field the operator is likely to accept without retyping.
 */

import { esc, eur, num } from '../format.js';
import { badge, metric } from '../components.js';

/** Form fields this planner is allowed to write. */
const WRITABLE = new Set([
  'title', 'kind', 'platform', 'subplatform', 'source_listing_id', 'purchase_pk',
]);

/**
 * Decide what the acquisition form should show for a prefill response.
 *
 * Returns plain data rather than touching the document, so the browser code that
 * applies it stays a two-line loop and the decision is testable on its own.
 *
 * `mode` is `link` when a purchase already exists and the unit should attach to
 * it rather than creating a second acquisition.
 */
export function acquisitionFormPlan(prefill) {
  const data = prefill ?? {};
  const source = data.prefill ?? {};
  const purchase = data.purchase ?? null;

  const fields = {};
  for (const [key, value] of Object.entries(source)) {
    // An allowlist, not a denylist. Adding a field to the backend must not
    // silently start writing it into the form.
    if (WRITABLE.has(key) && value != null && value !== '') fields[key] = value;
  }
  if (purchase) {
    fields.purchase_pk = purchase.purchase_pk;
    fields.title = purchase.title_raw ?? fields.title;
  }

  // The three costs are the operator's to supply. They are listed explicitly so
  // that a future edit which tries to prefill them is visibly wrong here.
  const costsEnteredByOperator = ['acquisition_cost_eur', 'shipping_cost_eur', 'buyer_fee_eur'];

  return {
    mode: purchase ? 'link' : 'create',
    fields,
    costsEnteredByOperator,
    // A known kind saves a dropdown choice; an unknown one leaves the default
    // rather than guessing at something wrong.
    focus: purchase ? 'kind' : 'acquisition_cost_eur',
    context: acquisitionContextRows(data),
    blocked: (data.blocks ?? []).length > 0,
    existingSku: data.owned_sku ?? null,
    recordedOutcome: !!data.recorded_outcome,
  };
}

/**
 * The context lines shown above the form.
 *
 * Every line is labelled for what it is. An ask is an ask, a decision is the
 * operator's own earlier reasoning, and a recorded cost is what was actually
 * paid. Collapsing those three into one number is how a research tool starts
 * lying about money.
 */
export function acquisitionContextRows(prefill) {
  const data = prefill ?? {};
  const rows = [];

  if (data.listing) {
    rows.push(metric('Observed asking price', `<strong>${eur(data.listing.ask_eur)}</strong>`)
      + '<p class="inventory-help">What the seller asked, not what you paid. '
      + 'Never copied into a cost field.</p>');
    if (data.listing.condition_norm) {
      rows.push(metric('Listed condition', esc(data.listing.condition_norm)));
    }
  }

  if (data.decision) {
    const decision = data.decision;
    const asked = decision.ask_eur?.amount ?? null;
    rows.push(metric(
      'Decision on record',
      badge(String(decision.decision ?? 'recorded'), 'purple')
      + (asked == null ? '' : ` <span class="muted">at ${eur(asked)}</span>`)
      + (decision.decision_pk ? ` <span class="muted">#${num(decision.decision_pk)}</span>` : ''),
    ));
    if (decision.notes) {
      rows.push(`<p class="inventory-help">${esc(decision.notes)}</p>`);
    }
  }

  if (data.purchase) {
    rows.push(metric('Already recorded', badge('Existing purchase', 'green')
      + ` <span class="muted">#${num(data.purchase.purchase_pk)}</span>`));
    const complete = data.cost_completeness ?? {};
    rows.push(metric('Acquisition cost', `<strong>${eur(data.purchase.acquisition_cost_eur)}</strong>`));
    rows.push(metric('Recorded costs', complete.complete
      ? badge('All known', 'green')
      : badge(`${(complete.unknown ?? []).length} unknown`, 'amber')));
  }

  for (const block of data.blocks ?? []) {
    rows.push(`<p class="inventory-help">${esc(block)}</p>`);
  }

  return rows.join('');
}

const REASON_LABELS = {
  no_owned_unit: { label: 'No owned unit', tone: 'amber' },
  unknown_costs: { label: 'Unknown costs', tone: 'amber' },
  no_outcome: { label: 'No outcome yet', tone: '' },
};

/**
 * Rows for the acquisition work queue.
 *
 * The three reasons are rendered as separate chips rather than a single "open"
 * badge, because each needs a different next action: inspect the unit, fill in a
 * cost, or wait for an outcome.
 */
export function acquisitionQueueRows(queue) {
  const items = queue?.items ?? [];
  if (!items.length) {
    return `<tr><td colspan="4">${emptyQueue()}</td></tr>`;
  }
  return items
    .map(item => {
      const reasons = (item.reasons ?? [])
        .map(reason => {
          const spec = REASON_LABELS[reason];
          return spec ? badge(spec.label, spec.tone) : badge(reason, 'amber');
        })
        .join(' ');
      const complete = item.cost_completeness ?? {};
      const unknown = (complete.unknown ?? []).length;
      return '<tr>'
        + `<td><strong>${esc(item.title ?? 'Untitled')}</strong>`
        + (item.owned_sku
          ? `<div class="observation-meta unit-sku">${esc(item.owned_sku)}</div>`
          : '<div class="observation-meta">No owned unit yet</div>')
        + '</td>'
        + `<td class="num">${eur(item.acquisition_cost_eur)}</td>`
        + `<td>${unknown ? badge(`${unknown} unknown`, 'amber') : badge('All known', 'green')}</td>`
        + `<td>${reasons || '<span class="muted">Complete</span>'}</td>`
        + '</tr>';
    })
    .join('');
}

/** The empty state, which is the normal state until the first real acquisition. */
function emptyQueue() {
  return '<div class="empty"><div class="empty-icon">'
    + '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="1.65" stroke-linecap="round" aria-hidden="true">'
    + '<path d="M20 7V5H4v14h16v-8h-6v4h6 M4 7h16"/></svg></div>'
    + '<strong>No acquisitions recorded yet</strong>'
    + '<p>Review a listing, save your decision, then record what you actually paid. '
    + 'Asking prices are never copied into a cost field.</p></div>';
}

/** Headline counts for the queue, worst first. */
export function acquisitionQueueSummary(queue) {
  const counts = queue?.counts ?? {};
  return [
    ['Needs an owned unit', counts.needs_unit ?? 0],
    ['Unknown costs', counts.unknown_costs ?? 0],
    ['No outcome yet', counts.no_outcome ?? 0],
  ]
    .map(([label, value]) => `<section class="card stat"><div class="stat-top">${esc(label)}</div>`
      + `<div class="stat-value">${num(value)}</div></section>`)
    .join('');
}
