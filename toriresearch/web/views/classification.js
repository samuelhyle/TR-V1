/** Current classification states, kept separate from measured accuracy. */
import { esc, num } from '../format.js';
import { metric } from '../components.js';

export function classificationQuality(report, throughput = null) {
  if (!report?.counts) return '';
  const c = report.counts;
  let html = '<div class="metric-list">'
    + metric('Accepted / tracked', `${num(c.accepted)} / ${num(report.n_listings)}`)
    + metric('Stale inputs / versions', `${num(c.stale_input)} / ${num(c.stale_version)}`)
    + metric('Uncertain labels / no matching type', `${num(c.low_confidence)} / ${num(c.abstained)}`)
    + metric('Failed / not yet classified', `${num(c.failed)} / ${num(c.never_classified)}`)
    + metric('Other validation refusals', num(c.validation_refused)) + '</div>';
  if (throughput) {
    html += '<div class="metric-list">'
      + metric('Measured cycles · current version', num(throughput.current_identity_cycles))
      + metric('Usable labels per cycle', num(throughput.mean_usable_labels_per_cycle, 2))
      + '</div>';
  }
  return html + '<p class="note">' + esc(report.basis)
    + ' Stale labels need reclassification; uncertain facts need review.</p>';
}
