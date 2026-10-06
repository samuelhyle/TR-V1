/**
 * The price research desk.
 *
 * The analyst view answers "is anything cheap in my market" with twelve rows. The
 * desk answers "what do I know about this item" and is built around the idea that
 * a discount is a reason to look, not a reason to buy. Three claims on this page
 * can each be false while looking true, so all three are stated on the row rather
 * than implied by its position.
 *
 * **The peer match.** A group key holding no brand or model matches another no
 * brand or model: it groups on the absence of a value. "Matched on brand and
 * model" and "matched on product type, because nothing recorded either" are
 * different comparisons with different prices, and only one of them is about the
 * same product. Each row names which it did.
 *
 * **The benchmark.** The gap is a percentage against a median that is itself an
 * estimate with an interval. On a thin or mixed cohort that interval is wide
 * enough to contain the ask itself, which makes "26% below the median" a
 * statement about sampling noise. So the interval is printed beside the median
 * and a discount that does not clear it is marked rather than presented.
 *
 * **What is absent.** An item with no cohort, a cohort below the reporting
 * threshold, or an untested gap gets a named reason. "Insufficient data" as a
 * blank panel reads as nothing to see; "its cohort holds 6 priced listings and
 * needs 30" reads as work still to do.
 *
 * `calculateScenario` is exported because the analyst workspace's own calculator
 * runs the identical arithmetic, and two copies of a margin formula is how a
 * reader ends up comparing two different numbers. This module owns it; the
 * analyst view imports it rather than keeping its own.
 */

import { DASH, LABELS, date, esc, eur, num, percent } from '../format.js';
import { badge, empty, metric } from '../components.js';
import { identityLabel as peerMatchLabel, peerMedian as peerMedianLabel } from './candidates.js';

/**
 * How a cohort was matched, in words. Defaults to the weakest claim.
 *
 * Borrowed from the analyst view's shortlist card rather than restated. Two
 * copies of this table is how the two views would come to describe the same
 * cohort differently, and the disagreement would look like a data problem
 * instead of a duplicated string. `candidates.js` owns the wording because it is
 * where the shortlist was first rendered; the desk is a second reader of it.
 */
export const matchBasis = (basis = 'product_type_only') =>
  peerMatchLabel({ identity_basis: basis });

/** What a gap means relative to the uncertainty in the benchmark it used. */
export function gapVerdict(row = {}) {
  if (!row.peer_median_ci95) {
    // An untested gap is not a failed one. Saying "gap unproven" where the
    // benchmark could not be pinned down at all would report a measurement that
    // was never made.
    return { tone: '', label: 'gap not testable', detail: 'no interval on this cohort median' };
  }
  if ('min_gap_survives_ci' in row) {
    if (row.min_gap_survives_ci === null) return { tone: '', label: 'gap not testable', detail: 'no interval on this cohort median' };
    return row.min_gap_survives_ci === true
      ? { tone: 'green', label: 'minimum gap survives its interval', detail: 'chosen discount holds at the lower median bound' }
      : { tone: 'amber', label: 'minimum gap not established', detail: 'chosen discount does not hold at the lower median bound' };
  }
  return row.gap_survives_ci === true
    ? { tone: 'green', label: 'gap survives its interval', detail: 'ask is below the median interval' }
    : { tone: 'amber', label: 'gap does not survive its interval', detail: 'ask sits inside the median interval' };
}

/**
 * The peer median with the interval it was estimated over.
 *
 * The same wording as the analyst view's shortlist card, for the same reason: a
 * median printed in two places must be read identically in both, or a reader will
 * conclude one of them is using a different benchmark.
 */
export const benchmark = row => peerMedianLabel(row);

/**
 * The bar above the table.
 *
 * `truncated` is stated whenever the page shows fewer candidates than the
 * threshold admits. A capped list that does not say it is capped reads as a
 * complete one, and a reader would conclude the market holds three candidates.
 */
export function deskSummary(report = {}) {
  const summary = report.summary || {};
  const shown = summary.returned ?? 0;
  const basis = Object.entries(summary.by_identity_basis || {});
  const chips = [
    badge(num(summary.total_candidates) + ' at least ' + percent(report.min_gap_pct) + ' below peers',
      summary.total_candidates ? 'purple' : ''),
  ];
  if (summary.truncated) chips.push(badge('showing ' + num(shown) + ' of ' + num(summary.total_candidates), 'amber'));
  const robust = summary.min_gap_survives_ci ?? summary.gap_survives_ci;
  if (robust) chips.push(badge(num(robust) + ('min_gap_survives_ci' in summary
    ? ' meet the minimum gap across their interval' : ' clear their interval'), 'green'));
  if (summary.gap_untested) chips.push(badge(num(summary.gap_untested) + ' untestable', 'amber'));
  if (basis.length) {
    chips.push(badge(basis.map(([name, count]) => num(count) + ' ' + matchBasis(name)).join(' · ')));
  }
  if (summary.distinct_cohorts != null && shown > summary.distinct_cohorts) {
    chips.push(badge(num(shown) + ' rows share ' + num(summary.distinct_cohorts) + ' peer groups'));
  }
  for (const [reason, count] of Object.entries(summary.blocked_by_reason || {})) {
    chips.push(badge(num(count) + ' blocked: ' + reason.replaceAll('_', ' '), 'amber'));
  }
  if (summary.blocked_reasons_overlap) chips.push(badge('A listing may have several blocking reasons'));
  return '<div class="desk-summary">' + chips.join('') + '</div>';
}

/** One row of the candidate table. */
export function candidateRow(row = {}, index = 0, selected = false) {
  const verdict = gapVerdict(row);
  const category = String(row.category_norm ?? '').split(' > ').at(-1);
  const condition = LABELS[row.condition_norm] || row.condition_norm || 'condition unknown';
  const rank = row.peer_sample ? ' of ' + eur(row.peer_sample.min_eur) + '–' + eur(row.peer_sample.max_eur) : '';
  return '<tr' + (selected ? ' class="is-selected"' : '') + '>' +
    '<td class="num"><input type="checkbox" data-desk-select="' + index + '" aria-label="Select ' +
      esc(row.title || row.listing_id) + '"' + (selected ? ' checked' : '') + '></td>' +
    '<td><button class="row-link desk-title" data-desk-open="' + index + '" type="button">' +
      esc(row.title || row.listing_id) + '</button>' +
      '<div class="observation-meta">' + esc(category) + ' · ' + esc(condition) + ' · ' +
      esc(row.municipality_norm || 'unknown location') + '</div>' +
      '<div class="observation-meta">' + esc(matchBasis(row.identity_basis)) + '</div></td>' +
    '<td class="num desk-ask">' + eur(row.ask_eur) + '</td>' +
    '<td class="num">' + benchmark(row) + (rank ? '<div class="range-text">peers ' + rank + '</div>' : '') + '</td>' +
    '<td class="num">' + percent(row.gap_pct) + '<div class="range-text">' + esc(verdict.detail) + '</div></td>' +
    '<td class="num">' + badge(verdict.label, verdict.tone) + '</td>' +
    '<td class="num">' + num(row.n_peers) + '</td>' +
    '<td><button class="btn quiet small" data-desk-detail="' + index + '" type="button">Inspect' +
      (row.requires_variant_verification ? ' · verify variant' : '') + '</button></td></tr>';
}

export const candidateTable = (rows = [], selected = new Set(), sort = 'supported') =>
  '<div class="table-wrap"><table class="desk-table"><thead><tr>' +
  '<th scope="col"><span class="sr-only">Select</span></th>' +
  '<th scope="col">Listing</th><th scope="col" class="num">Ask</th>' +
  '<th scope="col" class="num">Peer benchmark</th><th scope="col" class="num">Gap</th>' +
  '<th scope="col">Verdict</th><th scope="col" class="num">Peers</th><th scope="col">Action</th>' +
  '</tr></thead><tbody>' +
  (rows.length ? rows.map((row, index) => ({row, index})).sort((a, b) => {
    const key = {ask:'ask_eur', gap:'gap_pct', peers:'n_peers'}[sort];
    return key ? (sort === 'ask' ? 1 : -1) * (a.row[key] - b.row[key]) || a.index - b.index : a.index - b.index;
  }).map(({row, index}) => candidateRow(row, index, selected.has(row.listing_pk))).join('')
    : '<tr><td colspan="8">' + empty(
        'Nothing meets this threshold',
        'A candidate needs at least ' + num(rows.peersNeeded ?? 30) + ' other fresh priced peers in the same ' +
        'detailed category, condition and accepted product type, priced below their lower quartile and at ' +
        'least this far below their median. Lower the minimum gap, or collect more peers in one cohort.',
        'search') + '</td></tr>') +
  '</tbody></table></div>';

/** Keep choices attached to listing identities when a refresh changes ordering. */
export const reconcileSelection = (rows = [], selected = new Set()) =>
  new Set(rows.map(row => row.listing_pk).filter(pk => selected.has(pk)));

/** The empty state for a slice that has not been searched yet. */
export const deskIdle = (minN = 30) =>
  empty('Read the current shortlist',
    'Candidates require a supported product variant, a reviewed single-unit offer and at least ' + num(minN) +
    ' other fresh priced peers. Category asking-price context remains available.', 'search');

/** Stored offers remain useful research while comparison evidence is incomplete. */
export function researchInventory(inventory = {}, context = 'desk', selected = new Set()) {
  const action = context === 'analysis' ? 'data-analysis-research-open' : 'data-desk-research-open';
  const selectable = context === 'desk';
  const reasons = {incomplete_reviewed_offer:'Review variant and offer',
    unsupported_comparison_policy:'Comparison policy unavailable', unreviewed_offer:'Offer needs review',
    accessory_offer:'Accessory offer', bundle_offer:'Bundle offer', multiple_units_offer:'Multiple units',
    incompatible_offer_condition:'Condition needs a separate comparison',
    conflicting_offer_evidence:'Resolve conflicting evidence'};
  return '<div class="table-wrap"><table class="desk-table desk-inventory-table' + (selectable ? ' is-selectable' : '') + '"><thead><tr>' +
    (selectable ? '<th scope="col"><span class="sr-only">Select for research session</span></th>' : '') +
    '<th scope="col">Listing</th><th scope="col" class="num">Observed ask</th>' +
    '<th scope="col">Price observed</th><th scope="col">Review needed</th><th scope="col">Action</th>' +
    '</tr></thead><tbody>' + (inventory.items || []).map((row, index) =>
    '<tr' + (selected.has(row.listing_pk) ? ' class="is-selected"' : '') + '>' +
    (selectable ? '<td><input type="checkbox" data-desk-select-pk="' + esc(row.listing_pk) +
      '" aria-label="Select ' + esc(row.title || row.listing_id) + ' for research"' +
      (selected.has(row.listing_pk) ? ' checked' : '') + '></td>' : '') +
    '<td><button class="row-link desk-title" ' + action + '="' + index + '" type="button">' +
    esc(row.title || row.listing_id) + '</button><div class="observation-meta">' +
    esc(row.category_norm) + ' · ' + esc(row.municipality_norm || 'Unknown location') + '</div>' +
    '<div class="observation-meta">#' + esc(row.listing_id) + ' · ' +
    esc(LABELS[row.condition_norm] || row.condition_norm || 'Unknown condition') + '</div></td>' +
    '<td class="num">' + eur(row.ask_eur) + '</td><td>' + date(row.price_observed_at || row.last_seen_at) + '</td>' +
    '<td>' + (row.review_reasons || []).map(reason => badge(reasons[reason] || reason.replaceAll('_', ' '), 'amber')).join(' ') +
    '</td><td><button class="btn quiet small" ' + action + '="' + index +
    '" type="button">Inspect listing</button></td></tr>').join('') + '</tbody></table></div>';
}

/** Recognized source facts and the work still needed, without inventing peers. */
export function listingAnalysisSummary(automatic) {
  if (!automatic) return '';
  const current = automatic.status === 'current';
  const states = {known:'Found',explicit_no:'Explicitly absent',unknown:'Unknown',conflict:'Conflicting statements',not_applicable:'Not applicable'};
  const basis = {platform_field:'Platform field',seller_statement:'Seller statement',catalog_specification:'Catalog specification'};
  const facts = Object.entries(automatic.fields || {});
  const fieldRows = facts.map(([key, field]) => {
    const resolved = ['known','explicit_no'].includes(field.state);
    const value = resolved ? (typeof field.value === 'boolean' ? (field.value ? 'Yes' : 'No') : String(field.value) + (field.unit ? ' ' + field.unit : '')) : states[field.state] || 'Unknown';
    const evidence = (field.evidence || []).map(item => '<div class="note">' + esc(item.source_field) + ': “' + esc(item.token) + '”</div>').join('');
    return '<tr><td>' + esc(field.label || key.replaceAll('_',' ')) + '</td><td>' +
      '<input type="checkbox" disabled aria-label="Source evidence found for ' + esc(field.label || key) + '"' + (resolved ? ' checked' : '') + '> ' + esc(value) +
      '</td><td>' + esc(basis[field.basis] || '') + evidence + '</td></tr>';
  }).join('');
  const accompanying = ['included','excluded'].map(role => {
    const items = (automatic.accompanying || {})[role] || [];
    return items.length ? '<p class="note">Other objects ' + role + ': ' + items.map(item =>
      esc(String(item.value)) + ' (“' + esc(item.token) + '”)').join('; ') + '.</p>' : '';
  }).join('');
  const reviewMessages = (automatic.blockers || []).map(value => {
    if (value.startsWith('unverified_proposal:')) {
      const key = value.slice('unverified_proposal:'.length);
      const label = (automatic.fields || {})[key]?.label || (key === 'object_cue' ? 'product identification' : key.replaceAll('_',' '));
      return 'Unsupported ' + label.toLowerCase() + ' claim omitted';
    }
    if (value === 'ambiguous_memory_type') return 'Ambiguous memory capacity omitted';
    return value.replaceAll('_',' ');
  });
  return '<section class="listing-analysis"><h3 class="desk-subhead">Automatic product analysis</h3>' +
    badge(current ? 'Source analyzed' : automatic.status === 'stale' ? 'Update pending' : 'Analysis pending',current ? 'green' : 'amber') +
    '<p>' + esc(automatic.description || 'Automatic analysis is awaiting source input.') + '</p>' +
    (automatic.offer_role ? '<p class="note">Offer: ' + esc(automatic.offer_role.replaceAll('_',' ')) + '</p>' : '') +
    accompanying +
    (facts.length ? '<details open><summary>Product details and source checklist (' + facts.filter(([,field]) => ['known','explicit_no'].includes(field.state)).length + ' of ' + facts.length + ' fields found)</summary><div class="table-wrap"><table><thead><tr><th>Detail</th><th>Value</th><th>Source evidence</th></tr></thead><tbody>' + fieldRows + '</tbody></table></div></details>' : '') +
    (reviewMessages.length ? '<p class="note">Needs review: ' + esc(reviewMessages.join(', ')) + '.</p>' : '') +
    '<p class="note">The source checklist records listing statements. Physical inspection checks are recorded separately.</p></section>';
}

export function identitySummary(detail = {}) {
  const identity = detail.identity || {}, listing = detail.listing || {};
  const label = [identity.brand, identity.model].filter(Boolean).join(' ') ||
    [listing.brand_norm, listing.model_norm].filter(Boolean).join(' ') || 'Exact product unresolved';
  const missing = [...new Set([...(identity.missing_variant_fields || []),
    ...(identity.missing_comparison_fields || [])])];
  const known = Object.entries(identity.attributes || {}).filter(([, value]) => value != null);
  const facts = known.map(([key, value]) => metric(key.replaceAll('_', ' '), esc(String(value)))).join('');
  return listingAnalysisSummary(detail.listing_analysis) + '<h3 class="desk-subhead">Product and offer evidence</h3><p><strong>' +
    (identity.review ? '' : 'Model mention: ') + esc(label) +
    '</strong> ' + badge(identity.review ? 'Source reviewed' : 'Needs source review', identity.review ? 'green' : 'amber') +
    '</p>' + (facts ? '<div class="metric-list">' + facts + '</div>' : '') +
    (missing.length ? '<p class="note">Still needed: ' + esc(missing.map(key => key.replaceAll('_', ' ')).join(', ')) + '.</p>' : '') +
    '<p class="note">Recognized and seller-reported facts guide research. Confirm the actual unit during inspection.</p>';
}

export function cohortDetails(cohort = {}) {
  if (!cohort.key) return '<div class="desk-gate"><strong>Peer comparison unavailable</strong>' +
    '<p>Review the product variant and offer contents before a supported peer comparison can be made.</p></div>';
  return '<h3 class="desk-subhead">Comparable asking-price evidence</h3><p class="note">' +
    esc(cohort.identity_basis_detail || '') + '</p>' + cohortKey(cohort) + cohortShape(cohort) +
    '<h3 class="desk-subhead">Who the peers are</h3>' + breakdown('Condition', cohort.conditions) +
    breakdown('Seller type', cohort.seller_types) + breakdown('Municipality', cohort.municipalities) +
    '<h3 class="desk-subhead">Observed sale markers in this cohort</h3>' + saleMarkers(cohort);
}

/** One inspection item. `required` is what makes it a checklist rather than a tip. */
export const inspectionItem = item =>
  '<li class="desk-check' + (item.required ? ' is-required' : '') + '">' +
  '<div class="desk-check-head"><strong>' + esc(item.label) + '</strong>' +
  (item.required ? badge('Required', 'amber') : badge('Optional')) + '</div>' +
  '<p>' + esc(item.detail) + '</p><div class="observation-meta">' + esc(item.source) + '</div></li>';

export const inspectionList = (inspection = {}, checks = {}) =>
  ((inspection.items || []).length ? '<p class="note">Checks are your inspection notes. Save the session to keep them.</p>' : '') +
  '<ul class="desk-checks">' + (inspection.items || []).map(item =>
    '<li class="desk-check' + (item.required ? ' is-required' : '') + '"><label class="desk-check-head">' +
    '<input type="checkbox" data-desk-check="' + esc(item.key) + '"' +
    (checks[item.key] === true ? ' checked' : '') + '><strong>' + esc(item.label) + '</strong>' +
    (item.required ? badge('Required', 'amber') : badge('Optional')) + '</label><p>' +
    esc(item.detail) + '</p><div class="observation-meta">' + esc(item.source) + '</div></li>').join('') + '</ul>';

/** The cohort's price shape, or an explicit refusal to draw one. */
export function cohortShape(cohort = {}) {
  const sample = cohort.price_sample;
  if (!sample) return '<p class="note">No supported cohort price distribution for this item.</p>';
  const bars = ['min', 'p25', 'median', 'p75', 'max'].map((key) => {
    const value = sample[key + '_eur'];
    return '<div class="range-row"><div class="range-label">' + key.replace('_eur', '').toUpperCase() +
      '</div><div class="range-track"><i style="width:' +
      Math.max(2, Math.round(100 * (value - sample.min_eur) / Math.max(1e-9, sample.max_eur - sample.min_eur))) +
      '%"></i></div><div class="range-price">' + eur(value) + '</div></div>';
  }).join('');
  const histogram = cohort.histogram
    ? '<div class="desk-histogram" role="img" aria-label="Asking-price distribution of ' +
      num(cohort.histogram.n) + ' peers">' + cohort.histogram.counts.map((count, index) =>
        '<div class="desk-bin"><div class="desk-bin-bar" style="height:' +
        Math.max(2, Math.round(100 * count / Math.max(1, ...cohort.histogram.counts))) + '%" title="' +
        num(count) + ' peers"></div><span>' +
        eur(cohort.histogram.bins[index]) + '</span></div>').join('') + '</div>'
    : '<p class="note">Too few peers to draw a distribution; the quartile figures above are the whole ' +
      'evidence.</p>';
  return '<div class="metric-list">' + bars + '</div>' + histogram;
}

/** A named breakdown of the cohort, e.g. condition or seller type. */
export const breakdown = (label, rows = []) =>
  '<h4 class="desk-subhead">' + esc(label) + '</h4><div class="desk-chips">' +
  (rows.length ? rows.map(row => badge(row.label + ' · ' + num(row.n))).join('')
    : '<span class="note">Not recorded on these peers.</span>') + '</div>';

/** Observed sale-marker incidence for the cohort, with each class kept apart. */
export function saleMarkers(cohort = {}) {
  const markers = cohort.sale_markers || {};
  const rate = value => (value?.share == null ? badge('Insufficient data', 'amber')
    : badge(percent(value.share * 100) + ' within ' + num(markers.horizon_days) + 'd', 'green'));
  return '<div class="metric-list">' +
    metric('Declared sale markers', num(markers.sold_declared)) +
    metric('Verified sale markers', num(markers.sold_verified)) +
    metric('Declared incidence', rate(markers.estimates?.sold_inferred)) +
    metric('Verified incidence', rate(markers.estimates?.sold_confirmed)) +
    metric('Followed to the horizon', num(markers.horizon_support)) +
    '</div><p class="note">' + esc(cohort.basis || '') + '</p>';
}

/** Our own completed trades in the same category, with unknowns left unknown. */
export function ownedComparables(owned = {}) {
  if (owned.status !== 'OK') {
    return '<p class="note">' + esc(owned.note || 'No comparable owned trades.') + '</p>';
  }
  return '<div class="table-wrap"><table class="ledger-table"><thead><tr><th>Owned trade</th>' +
    '<th class="num">Paid</th><th class="num">Cleared</th><th class="num">Margin</th>' +
    '<th class="num">Days held</th></tr></thead><tbody>' +
    owned.trades.map(trade => '<tr><td>' + esc(trade.title || 'Trade #' + trade.purchase_pk) + '</td>' +
      '<td class="num">' + eur(trade.acquisition_cost_eur) + '</td>' +
      '<td class="num">' + (trade.final_sale_price_eur == null ? DASH : eur(trade.final_sale_price_eur)) + '</td>' +
      '<td class="num">' + (trade.margin_known ? eur(trade.gross_margin_eur)
        : badge('Unknown cost', 'amber')) + '</td>' +
      '<td class="num">' + (trade.days_held == null ? DASH : num(trade.days_held, 1)) + '</td></tr>').join('') +
    '</tbody></table></div><p class="note">' + esc(owned.note || '') + '</p>';
}

/** The reason an item is or is not a candidate, stated rather than left blank. */
export function verdictPanel(detail = {}) {
  const verdict = detail.verdict || {};
  if (verdict.qualifies) {
    return '<div class="desk-verdict is-yes">' + badge('Qualifies at ' + num(verdict.min_gap_pct) + '% below peers', 'green') +
      '<p>It is below its cohort\u2019s lower quartile and at least ' + num(verdict.min_gap_pct) +
      '% under its median, measured against ' + num(verdict.min_n) + ' peers or more.</p></div>';
  }
  return '<div class="desk-verdict is-no">' + badge('Not a candidate', 'amber') +
    '<p>' + esc(verdict.detail || 'This item does not meet the current threshold.') + '</p></div>';
}

/** The named cohort fields, so a reader can see exactly what was matched. */
export const cohortKey = (cohort = {}) =>
  '<dl class="desk-key">' + Object.entries(cohort.key || {}).map(([field, value]) =>
    '<div><dt>' + esc(field.replace(/_/g, ' ')) + '</dt><dd>' +
    (value == null || value === '' ? '<span class="muted">unrecorded</span>' : esc(value)) +
    '</dd></div>').join('') + '</dl>';

/**
 * Scenario arithmetic, identical to the analyst workspace's calculator.
 *
 * A shared formula is the point: two copies of a margin calculation is how a
 * reader ends up comparing a base profit from one page against a downside profit
 * from another and concluding the tool is inconsistent. `null` means "not
 * computable from these inputs", and the caller must say so rather than render a
 * zero, because zero profit is a measured claim and an unknown is not.
 */
export function calculateScenario(values, haircut) {
  const keys = ['resale', 'buy', 'inbound', 'fee', 'outbound', 'repair', 'other', 'target', 'days', 'carrying'];
  if (!values || keys.some(key => !Number.isFinite(values[key]) || values[key] < 0)) return null;
  if (!Number.isFinite(haircut) || haircut < 0 || haircut > 100 || values.fee >= 100) return null;
  const rate = values.fee / 100;
  const fixed = values.inbound + values.outbound + values.repair + values.other +
    values.days * values.carrying;
  const downsideSale = values.resale * (1 - haircut / 100);
  const base = values.resale * (1 - rate) - values.buy - fixed;
  const downside = downsideSale * (1 - rate) - values.buy - fixed;
  const totalCost = values.buy + fixed + values.resale * rate;
  if (![fixed, downsideSale, base, downside, totalCost,
    downsideSale * (1 - rate) - fixed - values.target,
    (values.buy + fixed) / (1 - rate)].every(Number.isFinite)) return null;
  return {
    base, downside, fixed,
    maxBuy: downsideSale * (1 - rate) - fixed - values.target,
    breakEven: (values.buy + fixed) / (1 - rate),
    roi: totalCost ? base / totalCost : null,
    holding: values.days * values.carrying,
    target: values.target,
    downsideSale,
  };
}

/** The calculator result. An uncomputable scenario prints why, not a number. */
export function scenarioResult(values, haircut) {
  const scenario = calculateScenario(values, haircut);
  if (!scenario) {
    return '<p class="note">Enter finite nonnegative assumptions and a fee rate below 100%. Blank costs stay ' +
      'unknown rather than becoming zero.</p>';
  }
  const tone = value => (value >= 0 ? 'positive' : 'negative');
  return '<div class="scenario-stats">' +
    '<div class="scenario-stat"><span>Base profit</span><strong class="' + tone(scenario.base) + '">' +
      eur(scenario.base) + '</strong></div>' +
    '<div class="scenario-stat"><span>Downside profit</span><strong class="' + tone(scenario.downside) + '">' +
      eur(scenario.downside) + '</strong></div>' +
    '<div class="scenario-stat"><span>Max buy for target</span><strong class="' + tone(scenario.maxBuy) + '">' +
      (scenario.maxBuy >= 0 ? eur(scenario.maxBuy) : 'No feasible price') + '</strong></div></div>' +
    '<div class="scenario-detail">' +
    badge(scenario.downside >= scenario.target ? 'Downside covers target'
      : scenario.downside >= 0 ? 'Downside below target' : 'Loss in downside case',
      scenario.downside >= scenario.target ? 'green' : scenario.downside >= 0 ? 'amber' : 'red') +
    '<p>Break-even proceeds: <strong>' + eur(scenario.breakEven) + '</strong> · Base ROI on all costs: <strong>' +
      percent(scenario.roi == null ? null : scenario.roi * 100) + '</strong></p>' +
    '<p>Downside proceeds ' + eur(scenario.downsideSale) + ' · Fixed costs ' + eur(scenario.fixed) +
      ' (including ' + eur(scenario.holding) + ' holding) · Target ' + eur(scenario.target) + '.</p></div>' +
    '<p class="note">These are your assumptions, not observations. Sale proceeds are what you expect to ' +
      'receive, which is not the peer median above: no asking price has been shown to convert into a ' +
      'clearing price for this segment.</p>';
}

/** Saved sessions, with each choice's live status rather than a stored price. */
export function sessionList(report = {}) {
  const sessions = report.sessions || [];
  if (!sessions.length) {
    return '<p class="note">No saved sessions. Saving one keeps your filters, your chosen listings and your ' +
      'notes. It stores no prices: reopening it reads the current evidence again.</p>';
  }
  return '<ul class="desk-sessions">' + sessions.map(session => {
    const status = session.status || {};
    return '<li><div class="desk-check-head"><strong>' + esc(session.name) + '</strong>' +
      badge(num(session.n_selected) + ' selected', session.n_selected ? 'purple' : '') + '</div>' +
      (session.notes ? '<p>' + esc(session.notes) + '</p>' : '') +
      '<div class="observation-meta">' + esc(
        Object.entries(session.filters || {}).map(([key, value]) => key + '=' + value).join(' · ') ||
        'all stored markets') +
      ' · ' + esc(agoText(session.updated_at)) + '</div>' +
      (session.n_selected
        ? '<div class="desk-chips">' +
          badge(num(status.n_qualifying) + ' still qualify', status.n_qualifying ? 'green' : 'amber') +
          (status.n_gone ? badge(num(status.n_gone) + ' no longer stored', 'red') : '') +
          (status.n_live - status.n_qualifying
            ? badge(num(status.n_live - status.n_qualifying) + ' research listings', 'amber') : '') +
          '</div>'
        : '<p class="note">No listings chosen yet.</p>') +
      ((status.listings || []).length ? '<ul class="desk-session-choices">' + status.listings.map(row =>
        '<li><button class="row-link" type="button" data-desk-saved-open="' + esc(row.listing_pk) +
        '" data-desk-saved-session="' + esc(session.name) + '">' + esc(row.title || 'Listing ' + row.listing_pk) +
        '</button> · Stored ask ' + eur(row.ask_eur) + ' · ' + date(row.price_observed_at) + ' · ' + esc(row.verdict?.qualifies ? 'Supported candidate' :
          String(row.verdict?.code || 'Needs review').replaceAll('_', ' ')) + '</li>').join('') + '</ul>' : '') +
      '<button class="btn quiet small" data-desk-open-session="' + esc(session.name) + '" type="button">Open' +
      '</button> <button class="btn quiet small" data-desk-delete-session="' + esc(session.name) +
      '" type="button">Delete</button></li>';
  }).join('') + '</ul>';
}

function agoText(value) {
  const stamp = Date.parse(value);
  if (!Number.isFinite(stamp)) return '';
  const minutes = Math.round((Date.now() - stamp) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return num(minutes) + ' min ago';
  const hours = Math.round(minutes / 60);
  if (hours < 48) return num(hours) + ' h ago';
  return num(Math.round(hours / 24)) + ' d ago';
}

/** The CSV export header and rows for the visible shortlist. */
export const exportColumns = ['listing_pk', 'listing_id', 'platform', 'subplatform', 'title',
  'category_norm', 'condition_norm', 'municipality_norm', 'brand_norm', 'model_norm',
  'product_type', 'identity_basis', 'ask_eur', 'peer_median_eur', 'peer_median_ci95_lo',
  'peer_median_ci95_hi', 'peer_p25_eur', 'peer_min_eur', 'peer_max_eur', 'gap_pct',
  'gap_survives_ci', 'n_peers', 'min_gap_pct', 'min_n', 'horizon_days', 'last_seen_at',
  'generated_at', 'filters', 'basis', 'ask_below_median_ci', 'min_gap_survives_ci',
  'conservative_gap_pct', 'cohort_id', 'benchmark_revision', 'comparison_policy_version', 'reference_at'];

export const exportCells = (row, report = {}) => [
  row.listing_pk, row.listing_id, row.platform, row.subplatform, row.title,
  row.category_norm, row.condition_norm, row.municipality_norm, row.brand_norm, row.model_norm,
  row.product_type, row.identity_basis, row.ask_eur, row.peer_median_eur,
  row.peer_median_ci95?.lo ?? null, row.peer_median_ci95?.hi ?? null, row.peer_p25_eur,
  row.peer_sample?.min_eur ?? null, row.peer_sample?.max_eur ?? null, row.gap_pct,
  row.gap_survives_ci ?? 'untested', row.n_peers, report.min_gap_pct, report.min_n,
  report.horizon_days, row.last_seen_at, report.generated_at,
  JSON.stringify(report.filters || {}), row.basis, row.ask_below_median_ci ?? 'untested',
  row.min_gap_survives_ci ?? 'untested', row.conservative_gap_pct ?? null,
  row.cohort_id ?? null, row.benchmark_revision ?? null, row.comparison_policy_version ?? null,
  report.reference_at ?? null,
];

/**
 * Side-by-side comparison of the selected candidates.
 *
 * A comparison invites a conclusion the columns do not support, so the framing is
 * deliberate. Two rows differing by 30% may be two different products, two
 * different condition grades, or two different days of collection -- and nothing
 * in this table separates those. So no "winner" is marked, no spread is called a
 * margin, and each row keeps its own identity basis visible: comparing a
 * brand-and-model row against a product-type row is comparing different things,
 * and the table says which rather than ranking them.
 */
export function comparisonTable(rows = []) {
  if (!rows.length) return '';
  const basis = new Set(rows.map(row => matchBasis(row.identity_basis)));
  const cohorts = new Set(rows.map(row => row.cohort_id).filter(Boolean));
  const body = rows.map(row => {
    const verdict = gapVerdict(row);
    return '<tr><td><strong>' + esc(row.title || row.listing_id) + '</strong>' +
      '<div class="observation-meta">' + esc(String(row.category_norm ?? '').split(' > ').at(-1)) +
      ' · ' + esc(LABELS[row.condition_norm] || row.condition_norm || 'condition unknown') + '</div></td>' +
      '<td class="num desk-ask">' + eur(row.ask_eur) + '</td>' +
      '<td class="num">' + benchmark(row) + '</td>' +
      '<td class="num">' + percent(row.gap_pct) + '</td>' +
      '<td>' + badge(verdict.label, verdict.tone) + '</td>' +
      '<td class="num">' + num(row.n_peers) + '</td>' +
      '<td>' + esc(matchBasis(row.identity_basis)) + '</td>' +
      '<td>' + esc(row.municipality_norm || 'unknown location') + '</td>' +
      '<td class="num">' + esc(String(row.seller_type_norm ?? 'unrecorded')) + '</td></tr>';
  }).join('');
  const caveat = basis.size > 1
    ? ' These rows were matched on different identities (' +
      [...basis].map(matchBasis).join('; ') + '), so the differences below are between ' +
      'comparisons of different strength rather than between the same product.'
    : ' All rows were matched on the same basis, so the differences below are within one ' +
      'comparison — but product mix, condition mix and collection effort may still differ.';
  const cohortNote = cohorts.size > 1
    ? ` These rows come from ${num(cohorts.size)} different peer cohorts, so their medians are not the same benchmark.`
    : cohorts.size === 1 && rows.every(row => row.cohort_id)
      ? ' All rows share one peer cohort.' : ' Cohort identifiers are missing, so a shared benchmark is not established.';
  return '<div class="desk-compare">' +
    '<p class="note"><strong>Descriptive comparison.</strong> A difference in asking price is not a ' +
    'margin, a saving or an opportunity: no sale price, fee or repair cost appears anywhere in this ' +
    'table.' + caveat + cohortNote + '</p>' +
    '<div class="table-wrap"><table class="desk-table"><thead><tr><th>Listing</th>' +
    '<th scope="col" class="num">Ask</th><th scope="col" class="num">Peer benchmark</th>' +
    '<th scope="col" class="num">Gap</th><th scope="col">Verdict</th><th scope="col" class="num">Peers</th>' +
    '<th scope="col">Peers matched on</th><th scope="col">Municipality</th>' +
    '<th scope="col">Seller</th></tr></thead><tbody>' + body + '</tbody></table></div>' +
    '<p class="note">Read each row\'s own interval before comparing two gaps. Two candidates can show ' +
    'similar percentages against benchmarks that are each several hundred euros apart.</p></div>';
}

/**
 * A Markdown research report for the current shortlist.
 *
 * Carries the same evidence as the CSV and the same refusals. The inspection
 * checklist is included unchecked on purpose: the report is a record of what was
 * known when it was written, and pre-ticking a box would turn an open question
 * into a closed one on paper.
 */
export function researchReport(rows = [], report = {}, detail = null) {
  const scope = Object.entries(report.filters || {}).filter(([, value]) => value)
    .map(([key, value]) => `${reportText(key)} = ${reportText(value)}`).join('; ') || 'all stored markets';
  const out = [];
  out.push(`# Price research — ${rows.length} candidate${rows.length === 1 ? '' : 's'}`);
  out.push('');
  out.push(`Generated ${report.generated_at || '—'} · minimum gap ${num(report.min_gap_pct)}% below peers · ` +
    `minimum ${num(report.min_n)} peers · sale horizon ${num(report.horizon_days)} days`);
  out.push('');
  out.push('## Scope');
  out.push('');
  out.push(`- Filters: ${scope}`);
  out.push(`- Freshness window: ${num(report.max_age_hours)} hours`);
  if (report.reference_at) out.push(`- Evidence pinned to: ${report.reference_at}`);
  out.push(`- Selected ${num(summaryOf(report).total_candidates)} candidates at this threshold; ` +
    `showing ${num(rows.length)}`);
  out.push('');
  out.push('## What this report is');
  out.push('');
  out.push('> Asking prices, not clearing prices. Each gap is measured against the median asking');
  out.push('> price of other fresh listings in the same cohort, with that median\'s own interval.');
  out.push('> A gap is a reason to inspect an item. It is not a margin, and no figure here is a');
  out.push('> predicted sale price.');
  out.push('');
  rows.forEach((row, index) => {
    const verdict = gapVerdict(row);
    const sample = row.peer_sample;
    out.push(`### ${index + 1}. ${reportText(row.title || row.listing_id)}`);
    out.push('');
    out.push(`- **Ask:** ${eur(row.ask_eur)} · ${num(row.n_peers)} peers · ${verdict.label}`);
    out.push(`- **Gap:** ${percent(row.gap_pct)} below the peer median`);
    out.push(`- **Peer benchmark:** ${benchmark(row)}`);
    if (sample) {
      out.push(`- **Peer range:** ${eur(sample.min_eur)} – ${eur(sample.max_eur)} ` +
        `(P25 ${eur(sample.p25_eur)}, P75 ${eur(sample.p75_eur)})`);
    }
    out.push(`- **Peers matched on:** ${matchBasis(row.identity_basis)}`);
    out.push(`- **Category / condition:** ${reportText(row.category_norm)} · ${reportText(row.condition_norm)}`);
    out.push(`- **Location / seller:** ${reportText(row.municipality_norm || 'unknown')} · ${reportText(row.seller_type_norm || 'unrecorded')}`);
    out.push(`- **Listing:** ${reportText(row.platform)} / ${reportText(row.listing_id)} · workspace key ${row.listing_pk}`);
    if (row.cohort_id) out.push(`- **Cohort / benchmark revision:** ${reportText(row.cohort_id)} / ${reportText(row.benchmark_revision)}`);
    if (row.last_seen_at) out.push(`- **Last observed:** ${row.last_seen_at}`);
    out.push('');
  });
  if (detail?.inspection?.items?.length && rows.some(row => row.listing_pk === detail.listing_pk || row.listing_pk === detail.listing?.listing_pk)) {
    out.push(`## Inspection outstanding — ${reportText(detail.listing?.title || 'opened item')}`);
    out.push('');
    detail.inspection.items.forEach(item => {
      out.push(`- [ ] **${reportText(item.label)}**${item.required ? ' (required)' : ''} — ${reportText(item.detail)}`);
    });
    out.push('');
  }
  out.push('## Limitations');
  out.push('');
  out.push('- Asking prices only. No realized market price is published by any source this workspace reads.');
  out.push('- A sale marker is a seller declaring an outcome, not an observed clearing price.');
  out.push('- Variant, completeness, defects and delivery cost are not represented in any figure above.');
  out.push('- Differences between rows may reflect product mix, condition mix or collection effort.');
  out.push('- Cost assumptions and margins are yours to enter; none are recorded in this report.');
  out.push('');
  return out.join('\n');
}

function summaryOf(report) {
  return report.summary || {};
}

function reportText(value) {
  return esc(String(value ?? 'unrecorded').replace(/[\r\n]+/g, ' '))
    .replace(/([\\`*_[\]#~])/g, '\\$1');
}

/**
 * Decisions already recorded for this listing.
 *
 * More than one row here is a history, not a duplicate: reassessment is the
 * intended workflow. What must not happen is the newest entry being read as
 * *the* verdict while an older one says the opposite, so a disagreement is
 * counted and stated. The recorded assumptions are not re-rendered — they were
 * frozen when written and the journal owns them.
 */
export function recordedDecisions(decisions = {}) {
  if (decisions.status !== 'RECORDED') {
    return '<p class="note">' + esc(decisions.note || 'No decision recorded for this listing yet.') + '</p>';
  }
  const rows = (decisions.items || []).map(item =>
    '<tr><td>' + badge(String(item.decision || '').replace(/^./, c => c.toUpperCase()),
      item.decision === 'reject' ? 'red' : item.decision === 'pursue' ? 'green' : 'purple') + '</td>' +
    '<td class="muted">' + esc(String(item.recorded_at || '').slice(0, 10)) + '</td>' +
    '<td>' + (item.notes ? esc(String(item.notes).slice(0, 160)) : '<span class="muted">No notes</span>') +
    ' <button class="row-link" type="button" data-desk-decision="' + esc(item.decision_pk) + '">Open journal entry</button></td></tr>').join('');
  return '<div class="table-wrap"><table class="ledger-table"><thead><tr><th>Decision</th>' +
    '<th scope="col">Recorded</th><th scope="col">Notes</th></tr></thead><tbody>' + rows +
    '</tbody></table></div><p class="note">' + esc(decisions.note || '') + '</p>' +
    (decisions.disagreeing_with_latest
      ? '<p class="note">' + badge(num(decisions.disagreeing_with_latest) + ' earlier entries decided differently', 'amber') +
        '</p>'
      : '');
}

/**
 * Why there is no price trend here.
 *
 * A sparkline over two adjacent days is not a trend, it is a line, and a reader
 * who cannot tell the difference will read "prices are stable" off it. That is
 * the specific failure this gate exists to prevent, so it is rendered in place of
 * the chart: how much history actually exists, when tracking began, and whether
 * the asking-price cells are suppressed under the aggregate's own privacy floor.
 *
 * Everything here comes from the aggregate report the snapshot already carries, so
 * no second history query is made and nothing can disagree with the market-state
 * report written by the same module.
 */
export function trendGate(history = {}) {
  const days = history.days || [];
  const available = days.filter(day => day.status === 'available');
  const privacy = history.privacy || {};
  const minimum = privacy.minimum_group;
  const hasSuppressedAsks = cells => (cells || []).some(cell => cell.asking_prices?.status === 'suppressed');
  const suppressedDays = new Set((history.partitions || [])
    .filter(partition => partition.kind === 'market' && hasSuppressedAsks(partition.cells))
    .map(partition => partition.period_day));
  const suppressed = available.filter(day => suppressedDays.has(day.day) || hasSuppressedAsks(day.cells)).length;
  const withheld = suppressed ? ' ' + num(suppressed) +
    ' of these days have asking prices suppressed under the aggregate’s reporting policy (minimum group of ' +
    num(minimum) + '), so those medians are withheld rather than published.' : '';
  const scopeNote = ' These are recording days across the collection scope; a candidate-specific price series is not established.';
  const started = history.tracking_started_day;

  if (!available.length) {
    return '<div class="desk-gate">' + badge('No price history yet', 'amber') +
      '<p>' + (started
        ? 'Daily aggregates begin on ' + esc(started) + ', and no day has been recorded yet.'
        : 'No daily aggregate has been recorded, so no price history exists to draw.') +
      ' Until days accumulate, nothing on this page can say whether asking prices are rising or falling, ' +
      'and none of it does.</p></div>';
  }

  // One or two days is a line, not a direction. Say so rather than plotting it.
  if (available.length < 3) {
    return '<div class="desk-gate">' + badge('Not enough days for a trend', 'amber') +
      '<p>' + num(available.length) + ' of ' + num(days.length) + ' days recorded since ' +
      esc(started || 'tracking began') + '. ' +
      (available.length < 2
        ? 'A single day cannot show a direction.'
        : 'Two adjacent days cannot show a direction either: a line through them is not a trend, and drawing one would read as "prices are stable" when nothing establishes that.') +
      ' No trend is drawn until at least three days are available.' + withheld + scopeNote + '</p></div>';
  }

  return '<div class="desk-gate">' + badge(num(available.length) + ' days recorded', 'green') +
    '<p>Daily aggregates since ' + esc(started || 'tracking began') + '. ' +
    withheld + scopeNote +
    '</p></div>';
}
