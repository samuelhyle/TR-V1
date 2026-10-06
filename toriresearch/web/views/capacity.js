/**
 * The follow-up capacity panel.
 *
 * The point of this panel is to replace two true-but-inert readouts — `due` and
 * `probe_runs_at_budget` — with one answer to the question the operator
 * actually has: can a day of collection consume the backlog that exists now?
 *
 * The property the wording has to hold: an over-capacity verdict must never read
 * as "send more requests". Raising the rate is the documented wrong answer, and a
 * panel that reports a red number without saying what to do invites it. So the
 * remedy text is part of the component, not a footnote.
 */

import { esc, num } from '../format.js';
import { badge, metric } from '../components.js';

const VERDICTS = {
  within_capacity: { label: 'Keeping up', tone: 'green' },
  at_capacity: { label: 'Cohort full', tone: 'amber' },
  over_capacity: { label: 'Backlog growing', tone: 'amber' },
  unavailable: { label: 'No verdict', tone: '' },
};

/**
 * What to do about each verdict.
 *
 * Phrased as a scope decision rather than a throughput one, because throughput is
 * bounded by the platform and by the collection policy, both of which are
 * deliberate. Every one of these is a choice about *what* is followed, not how
 * hard.
 */
const REMEDIES = {
  within_capacity:
    'A day of collection clears the current backlog. Room remains to admit more listings into the followed cohort.',
  at_capacity:
    'The cohort is at its maximum. Admitting more would displace something already being followed — narrow the scope deliberately rather than growing the queue.',
  over_capacity:
    'The backlog is larger than one day of collection can consume, so it is growing. Narrow the followed cohort or reduce arrivals; a larger request budget makes each run longer, not the collector faster.',
  unavailable:
    'Nothing can be said about capacity until follow-up has run and an item budget is configured.',
};

/**
 * The unresolved-reason breakdown.
 *
 * Each reason gets its own row because each needs a different action. A single
 * "unresolved" number is the thing this replaces: it cannot distinguish a listing
 * that was never admitted from one whose page failed to parse.
 */
const REASON_ROWS = [
  ['never_probed', 'Never probed', 'Not in the followed cohort. Widen the cohort or accept that these stay unresolved.'],
  ['awaiting_first_success', 'Awaiting first check', 'Admitted and queued. This one only needs time.'],
  ['parse_failure', 'Page could not be parsed', 'The parser changed or the page shape is unknown. Needs a parser fix, not more requests.'],
  ['platform_stop', 'Stopped by the platform', 'A refusal or rate limit ended the run. Review before retrying.'],
  ['other_error', 'Other repeated failure', 'Transport or runtime failure. Check the error class before admitting more.'],
  ['quarantined', 'Quarantined', 'Held after repeated failures. Will not dispatch until a reviewed repair releases it.'],
];

/**
 * Render the capacity panel.
 *
 * Returns the badge row, the decision figures and the prose reasons. Everything
 * is derived from the verdict object, so the panel cannot disagree with the
 * endpoint about what state collection is in.
 */
export function capacityPanel(verdict) {
  const data = verdict ?? {};
  const spec = VERDICTS[data.verdict] ?? VERDICTS.unavailable;

  const figures = [
    metric('Verdict', badge(spec.label, spec.tone)),
    metric('Checks due now', num(data.due ?? 0)
      + (data.due ? ` <span class="metric-sub">${num(data.due_first)} first · ${num(data.due_repeat)} repeat</span>` : '')),
    metric('Followed cohort', `${num(data.active)} / ${num(data.max_active)}`
      + (data.cohort_headroom > 0
        ? `<span class="metric-sub">${num(data.cohort_headroom)} places free</span>`
        : '<span class="metric-sub">full</span>')),
    metric('Sustainable per day', num(data.sustainable_attempts_per_day)
      + `<span class="metric-sub">of a ${num(data.daily_attempt_ceiling)} ceiling</span>`),
  ];
  if (data.not_admitted) {
    figures.push(metric('Not admitted', num(data.not_admitted)
      + '<span class="metric-sub">unresolved, outside the cohort</span>'));
  }

  const reasons = (data.reasons ?? [])
    .map(reason => `<li>${esc(reason)}</li>`)
    .join('');

  return `<div class="capacity-verdict">`
    + `<div class="metric-list">${figures.join('')}</div>`
    + `<p class="capacity-remedy">${esc(REMEDIES[data.verdict] ?? REMEDIES.unavailable)}</p>`
    + (reasons ? `<ul class="suppression-reasons">${reasons}</ul>` : '')
    + (data.note ? `<p class="note">${esc(data.note)}</p>` : '')
    + `</div>`;
}

/** The unresolved-reason breakdown, with the action each reason needs. */
export function unresolvedPanel(counts) {
  const data = counts ?? {};
  const rows = REASON_ROWS
    .filter(([key]) => (data[key] ?? 0) > 0)
    .map(([key, label, action]) =>
      `<tr><td><strong>${num(data[key])}</strong></td><td>${esc(label)}</td>`
      + `<td class="muted">${esc(action)}</td></tr>`)
    .join('');

  const total = data.total ?? 0;
  if (!rows) {
    // An empty breakdown is good news and should read as such, not as a bug.
    return `<div class="empty"><strong>No unresolved listings in this scope</strong>`
      + `<p>Every followed listing has reached a terminal state, or none are being followed yet.</p></div>`;
  }

  const shown = REASON_ROWS.reduce((total, [key]) => total + (data[key] ?? 0), 0);
  const covered = shown === total;

  return `<div class="table-wrap"><table><thead><tr><th class="num">Count</th><th>Reason</th>`
    + `<th>What it needs</th></tr></thead><tbody>${rows}</tbody></table></div>`
    + (covered ? '' : `<p class="note">${num(total - shown)} of ${num(total)} unresolved `
      + 'listings fall outside these reasons; treat the total as the authoritative count.</p>');
}
