/**
 * Shared presentational components.
 *
 * Fifteen views previously each grew their own version of a badge, a metric
 * line and an empty state. That is how evidence classes end up rendered three
 * different ways on one screen, which is worse than a duplication bug: it makes
 * a declared sale look like a verified one.
 *
 * These functions return HTML strings and touch no globals, so they are tested
 * directly rather than through a browser.
 *
 * The rule the whole file exists to hold: a suppressed statistic must never be
 * rendered as a number. When support is missing we say what is missing, and the
 * estimate is omitted rather than printed as `0` or `null`.
 */

import { esc, eur, icon, num, percent } from './format.js';

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

export const badge = (label, tone = '') =>
  `<span class="badge ${esc(tone)}">${esc(label)}</span>`;

export const metric = (label, value) =>
  `<div class="metric-line"><span>${esc(label)}</span><strong>${value}</strong></div>`;

export const empty = (title, description, iconName = 'chart') =>
  `<div class="empty"><div class="empty-icon">${icon(iconName, 20)}</div>` +
  `<strong>${esc(title)}</strong><p>${esc(description)}</p></div>`;

// ---------------------------------------------------------------------------
// Evidence classes
// ---------------------------------------------------------------------------

/**
 * The four kinds of value this application can show, plus plain observation.
 *
 * Each carries a glyph as well as a colour. Colour alone would fail for a
 * colour-blind operator and would fail again in a screenshot printed in
 * greyscale, and "declared" versus "verified" is exactly the distinction the
 * whole liquidity panel exists to keep honest.
 */
export const EVIDENCE = {
  declared: { label: 'Declared', glyph: '◐', note: 'seller-marked sold' },
  verified: { label: 'Verified', glyph: '●', note: 'verified sale' },
  observed: { label: 'Observed', glyph: '■', note: 'measured' },
  derived: { label: 'Derived', glyph: '◇', note: 'model classification' },
  assumed: { label: 'Assumed', glyph: '○', note: 'your input' },
};

/**
 * Render an evidence class with its support.
 *
 * `support` is the sample size behind the figure. It is shown even when it is
 * small, because a number with n=3 in front of it is the difference between an
 * estimate and a rumour.
 */
export function evidenceChip(kind, support = null, extra = '') {
  const spec = EVIDENCE[kind];
  if (!spec) return badge(kind ?? 'Unknown', 'amber');
  const n = support == null ? '' : ` <span class="evidence-n">n=${num(support)}</span>`;
  return (
    `<span class="evidence evidence-${esc(kind)}" title="${esc(spec.note)}">` +
    `<span class="evidence-glyph" aria-hidden="true">${spec.glyph}</span>` +
    `<span class="evidence-label">${esc(spec.label)}</span>${n}${extra}</span>`
  );
}

// ---------------------------------------------------------------------------
// Support and suppression
// ---------------------------------------------------------------------------

/** Tone for a status string, matching the vocabulary the server already uses. */
export const statusTone = status =>
  status === 'OK' ? 'green' : status === 'INSUFFICIENT DATA' ? 'amber' : '';

/**
 * Name what is missing, rather than withholding a number silently.
 *
 * This is the single most important string in the dashboard. `INSUFFICIENT
 * DATA` with no explanation reads as a bug; naming the shortfall tells the
 * operator whether to collect more, probe more, or wait for the horizon.
 */
export function suppressionNotice(reasons, status = 'INSUFFICIENT DATA') {
  const list = (reasons ?? []).filter(Boolean);
  if (!list.length) return `<div class="suppression">${badge(status, statusTone(status))}</div>`;
  return (
    `<div class="suppression" role="note">${badge(status, statusTone(status))}` +
    `<ul class="suppression-reasons">${list.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div>`
  );
}

/**
 * The gates that decide whether a cell can support an estimate.
 *
 * Returns the reasons a cell is suppressed, so the same function drives both
 * the suppression notice and the readiness ranking. One source, so a vertical
 * cannot look better in one place than another.
 */
export function evidenceGates(panel = {}, minN) {
  const gates = [];
  gates.push(panel.status === 'OK' ? 'Prices supported' : `Prices ${num(panel.n_priced)}/${num(minN)}`);

  for (const [evidence, rate] of Object.entries(panel.sale_rates ?? {})) {
    const key = evidence === 'sold_confirmed' ? 'Verified' : 'Declared';
    gates.push(rate ? `${key} sale rate` : `No ${key.toLowerCase()} sale rate`);
  }
  if (panel.followup_coverage != null && panel.horizon_support === false) {
    gates.push('Follow-up has not reached the horizon');
  }
  return gates;
}

/**
 * A median with its interval, or a suppression notice.
 *
 * The important behaviour is the negative case: when `median_eur` is absent this
 * returns a notice and no digits. Returning `eur(null)` here would render a
 * dash inside a numeric column, which reads as a price of zero.
 */
export function medianOrSuppressed(panel = {}, minN) {
  if (panel.status !== 'OK' || panel.median_eur == null) {
    // When the status and the value disagree, the notice reports the
    // suppression rather than the status. Labelling a withheld figure "OK"
    // because of the status field would be the opposite of honest.
    return suppressionNotice(
      evidenceGates(panel, minN),
      panel.status === 'OK' ? 'INSUFFICIENT DATA' : panel.status ?? 'INSUFFICIENT DATA',
    );
  }
  // Guard the bounds, not just the container: a partially populated `ci95`
  // would otherwise render as "95% CI —–—", which looks like an interval of
  // zero width rather than an absent one.
  const ci = panel.median_ci95;
  const hasInterval = ci != null && ci.lo != null && ci.hi != null;
  const interval = hasInterval
    ? ` <small class="metric-sub">95% CI ${eur(ci.lo)}–${eur(ci.hi)}</small>`
    : '';
  return `<strong>${eur(panel.median_eur)}</strong>${interval}`;
}

/**
 * A sale incidence with its interval, or an explicit absence.
 *
 * Declared and verified rates are separate objects and must be rendered
 * separately; pooling them is the error the README warns about repeatedly.
 */
export function incidenceOrAbsent(rate) {
  if (!rate) return '<span class="muted">Not enough evidence</span>';
  const ci = rate.ci95 ?? {};
  const interval =
    ci.lo == null ? '' : ` <small class="metric-sub">95% CI ${percent(ci.lo * 100)}–${percent(ci.hi * 100)}</small>`;
  return `<strong>${percent(rate.share * 100)}</strong>${interval}`;
}

// ---------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------

/**
 * A sortable data table that keeps its identifying column visible.
 *
 * The first column stays pinned on narrow screens because a row of numbers with
 * no visible label is unreadable, and the wrapper is focusable so the overflow
 * can be reached from the keyboard rather than only by dragging.
 */
export function dataTable(headers, rows, { sortable = [], caption = '' } = {}) {
  const head = headers
    .map((h, i) => {
      const key = sortable[i];
      const aria = key ? ` aria-sort="none"` : '';
      const marker = key ? ` <span class="sort-hint" aria-hidden="true">↕</span>` : '';
      return `<th scope="col"${key ? ` data-sort="${esc(key)}"` : ''}${aria}>${esc(h)}${marker}</th>`;
    })
    .join('');
  const body = rows
    .map(cells => {
      const [first, ...rest] = cells;
      return `<tr><th scope="row" class="sticky-col">${first}</th>${rest.map(c => `<td>${c}</td>`).join('')}</tr>`;
    })
    .join('');
  const cap = caption ? `<caption class="sr-only">${esc(caption)}</caption>` : '';
  return (
    `<div class="table-scroll" tabindex="0" role="region" aria-label="${esc(caption || 'Data table')}">` +
    `<table>${cap}<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`
  );
}
