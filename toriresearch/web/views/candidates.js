/**
 * The price research shortlist card.
 *
 * Two claims on this card can be false while looking true, so both are stated
 * rather than implied.
 *
 * The peer match. The peer group is keyed on brand and model as well as category,
 * condition and product type, and a key holding a NULL matches another NULL: it
 * groups on the absence of a value, not on the value. "Matched on brand and
 * model" and "matched on product type, because nothing recorded a brand or model"
 * are different comparisons with different prices, and only one of them is about
 * the same product. The card names which one it did.
 *
 * The benchmark. The gap is a percentage against a peer median, and that median
 * is an estimate with an interval around it. On a thin or mixed group the
 * interval is wide enough to contain the ask itself, which makes "64% below the
 * median" a statement about sampling noise rather than about the item. So the
 * interval is printed next to the median, and a discount that does not clear it
 * is marked rather than quietly presented as a bargain.
 */

import { LABELS, esc, eur, num, percent } from '../format.js';
import { badge } from '../components.js';

export function comparisonCoverage(report = {}) {
  const coverage = report.comparison_coverage;
  if (!coverage) return '';
  const chips = [badge(num(coverage.fresh_priced) + ' fresh prices'),
    badge(num(coverage.current_type_labels) + ' current validated labels'),
    badge(num(coverage.reviewed_eligible_observations) + ' reviewed eligible observations'),
    badge(num(coverage.supported_cohorts) + ' groups with enough other peers')];
  for (const [reason, count] of Object.entries(coverage.blocked_by_reason || {})) {
    chips.push(badge(num(count) + ' blocked: ' + reason.replaceAll('_', ' '), 'amber'));
  }
  const groups = new Set((report.candidates || []).map(row => row.cohort_id).filter(Boolean));
  if (groups.size && groups.size < report.candidates.length) {
    chips.push(badge(num(report.candidates.length) + ' rows share ' + num(groups.size) + ' peer groups'));
  }
  return '<div class="desk-summary">' + chips.join('') + '</div><p class="note">' +
    (coverage.blocked_reasons_overlap ? 'A listing may have several blocking reasons. ' : '') +
    esc(coverage.peer_count_basis || coverage.note || '') + '</p>';
}

const IDENTITY_LABELS = {
  canonical_variant: 'reviewed canonical variant and single-unit offer',
  brand_and_model: 'brand and model',
  model: 'model, brand unrecorded',
  brand: 'brand, model unrecorded',
  product_type_only: 'product type only, no brand or model recorded',
};

/** How the peer group was matched, in words. Defaults to the weakest claim. */
export function identityLabel(row = {}) {
  return IDENTITY_LABELS[row.identity_basis] ?? IDENTITY_LABELS.product_type_only;
}

/** The peer median with the interval it was estimated over. */
export function peerMedian(row = {}) {
  const interval = row.peer_median_ci95;
  const median = 'median ' + eur(row.peer_median_eur);
  return interval ? `${median} (95% CI ${eur(interval.lo)}–${eur(interval.hi)})` : median;
}

/** Whether the gap is larger than the uncertainty in the benchmark it used. */
export function gapIsRobust(row = {}) {
  return Boolean(row.peer_median_ci95) &&
    ('min_gap_survives_ci' in row ? row.min_gap_survives_ci === true : row.gap_survives_ci === true);
}

export function candidateCard(row = {}, index = 0) {
  const robust = gapIsRobust(row);
  const intervalNote = !row.peer_median_ci95 ? ''
    : 'min_gap_survives_ci' in row
      ? robust ? ' · minimum discount survives that interval' : ' · minimum discount does not survive that interval'
      : robust ? ' · gap survives that interval' : ' · gap does not survive that interval';
  const category = String(row.category_norm ?? '').split(' > ').at(-1);
  const condition = LABELS[row.condition_norm] || row.condition_norm;
  return '<article class="candidate"><div class="candidate-main"><h3>' + esc(row.title || row.listing_id) + '</h3>' +
    badge(percent(row.gap_pct) + ' below peer median', robust ? 'green' : 'purple') +
    '<p>' + esc(category) + ' · ' + esc(condition) + ' · ' + esc(row.municipality_norm || 'Unknown location') + '</p>' +
    '<p>' + num(row.n_peers) + ' other fresh peers · ' + peerMedian(row) + ' · P25 ' + eur(row.peer_p25_eur) +
      (row.product_type ? ' · derived ' + esc(row.product_type) : '') + '</p>' +
    '<p class="note">Peers matched on ' + esc(identityLabel(row)) + esc(intervalNote) +
      '. A group key holding no brand or model matches on their absence, not on the product.</p>' +
    '<button class="row-link" data-provenance-candidate="' + index + '" type="button">Where this comes from →</button><br>' +
    '<button class="row-link" data-inspect-candidate="' + index + '" type="button">Inspect stored listing →</button></div>' +
    '<div class="candidate-amount"><strong>' + eur(row.ask_eur) + '</strong><span class="note">asking price</span><br>' +
    '<button class="btn small" data-model-candidate="' + index + '" type="button">Model costs</button></div></article>';
}

export function candidateCards(rows = []) {
  return rows.map((row, index) => candidateCard(row, index)).join('');
}
