/**
 * Focus-category view helpers.
 *
 * The first view module extracted out of `dashboard.html`. Only the parts with
 * no DOM access live here; `focusCard`, `focusRow` and `drawFocus` still need
 * the live snapshot and the document, and move when that state is lifted into a
 * store.
 *
 * `focusGates` is re-exported from `components.js` rather than reimplemented.
 * It used to be a second copy of the same gate logic, which is precisely how a
 * vertical ends up looking better in one view than another.
 */

import { esc, num, percent } from '../format.js';
import { evidenceGates } from '../components.js';

export { evidenceGates as focusGates };

/**
 * Normalise follow-up counts for one vertical.
 *
 * Two shapes reach here: the current admitted-task queue, and the older
 * attempt counters kept for historical rows. The fallback exists so a report
 * generated before a queue existed still renders instead of showing blanks.
 */
export function focusQueue(follow = {}) {
  return (
    follow.queue ?? {
      enabled: false,
      due: follow.n_due_in_scope ?? 0,
      due_first: follow.n_due_first_in_scope ?? 0,
      due_repeat: follow.n_due_repeat_in_scope ?? 0,
      minimum_runs: follow.probe_runs_at_budget,
    }
  );
}

/**
 * Sort keys for the vertical list.
 *
 * Readiness ranks on support first and volume second: the vertical with more
 * evidence is the one worth opening, and among equally supported verticals the
 * larger sample is the more reliable read. Ranking on `n` alone would put a
 * thin, well-covered cell above a thick, poorly-covered one.
 *
 * `price` uses -1 for an absent median so a suppressed cell sorts below every
 * real price rather than tying with the cheapest one.
 */
export const FOCUS_SORTS = {
  readiness: p => (p.status === 'OK' ? 2e9 : 0) + (p.sale_status === 'OK' ? 1e9 : 0) + (p.n_priced ?? 0),
  supply: p => p.n_available ?? 0,
  price: p => p.median_eur ?? -1,
  due: p => focusQueue(p.followup).due,
  grid: p => p.grid?.crawled ?? 0,
};

/** Order panels for display. `name` keeps the server's own ordering. */
export function sortFocusPanels(panels = [], sort = 'readiness') {
  if (sort === 'name') return panels;
  const key = FOCUS_SORTS[sort] ?? (() => 0);
  return [...panels].sort((a, b) => key(b) - key(a));
}

/**
 * Sale incidence at focus density: the figure plus its interval on a second
 * line, because a vertical card has room for it and a table cell does not.
 *
 * Declared and verified are passed separately by the caller and must stay that
 * way; pooling them is the error the README warns about repeatedly.
 */
export function focusIncidence(rate) {
  if (!rate) return '<span class="muted">Not enough evidence</span>';
  const ci = rate.ci95 ?? {};
  const hasInterval = ci.lo != null && ci.hi != null;

  const shown = hasInterval
    ? `<small class="metric-sub">95% CI ${percent(ci.lo * 100)}–${percent(ci.hi * 100)}</small>`
    : '';
  const followed =
    rate.n_followed_to_horizon == null ? '' : `${num(rate.n_followed_to_horizon)} followed to horizon`;

  // The tooltip has to be built from the same guarded values as the body.
  // Formatting an absent bound would put "NaN%" in the title, which reads as a
  // real interval to anyone inspecting the element.
  const parts = [hasInterval ? `95% CI ${percent(ci.lo * 100)}–${percent(ci.hi * 100)}` : null];
  if (followed) parts.push(followed);
  const title = parts.filter(Boolean).join('; ');

  return `<span${title ? ` title="${esc(title)}"` : ''}>${percent(rate.share * 100)}</span>${shown}`;
}
