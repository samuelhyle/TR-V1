/**
 * Where a shortlist listing's numbers came from.
 *
 * The shortlist answers "which ask sits far below its peers". It cannot show
 * that the ask was converted at a stale rate, that the category is an unresolved
 * id rather than a label, or that the peers were search-page rows rather than
 * item-page fetches. Each of those changes what the number is worth, and all of
 * them are already stored. This panel reads that stored evidence instead of
 * restating the shortlist row.
 *
 * The rule the whole file exists to hold: an unknown input is named, never
 * blanked. A price with no channel behind it renders as "unattributed" and a
 * field with no value renders as its stored missing reason, because a table that
 * omits a row reads as "nothing to report" when the truth is "nothing recorded,
 * and that is a different thing".
 */

import { DASH, date, esc, eur, num } from '../format.js';
import { badge, evidenceChip, medianOrSuppressed, metric } from '../components.js';

// ---------------------------------------------------------------------------
// Values
// ---------------------------------------------------------------------------

/**
 * Render a stored value by its declared kind.
 *
 * `null` is never formatted as a number. The kinds come from the server so the
 * client cannot disagree with the server about what a column holds.
 */
const VALUES = {
  money: value => eur(value),
  // Rates and confidences are printed at fixed precision. A confidence of 0.7
  // rounded to zero decimals would read as a rejection rather than the floor.
  rate: value => (value == null ? DASH : num(value, 4, 4)),
  flag: value => (value == null ? DASH : value ? 'Yes' : 'No'),
  text: value => (value == null ? DASH : esc(value)),
};

const value = (kind, raw) => (VALUES[kind] ?? VALUES.text)(raw);

/** The channel that wrote a field, or an explicit "we do not know". */
export function channelLabel(channel) {
  return channel?.label ? badge(channel.label) : badge('Unattributed', 'amber');
}

/**
 * Why a stored derivation was refused.
 *
 * Every refusal gets its own wording because each needs a different action. A
 * stale input digest is fixed by re-classifying; a category mismatch is a
 * classification error; a version change needs the model rerun. One generic
 * "rejected" string would hide all three.
 */
const DERIVATIONS = {
  accepted: { tone: 'green', text: 'Accepted. This label is what matched the peers.' },
  low_confidence: {
    tone: 'amber',
    text: 'Refused: confidence is below the 0.7 floor, so it was not used for matching.',
  },
  category_mismatch: {
    tone: 'amber',
    text: 'Refused: the label does not belong to this listing’s category.',
  },
  not_in_vocabulary: {
    tone: 'amber',
    text: 'Refused: the label is outside the accepted vocabulary.',
  },
  unreadable_output: {
    tone: 'red',
    text: 'Refused: the stored classification output could not be read.',
  },
  input_changed: {
    tone: 'amber',
    text: 'Not current: the listing or model inputs have changed since this label was derived.',
  },
  version_changed: {
    tone: 'amber',
    text: 'Not current: the prompt or vocabulary version has moved on since this was derived.',
  },
  model_changed: {
    tone: 'amber', text: 'Not current: the classification model has changed; this label needs reclassification.',
  },
  inference_failed: {
    tone: 'red', text: 'Classification failed to produce a usable result. Prices and outcomes remain available.',
  },
  abstained: {
    tone: 'amber', text: 'No allowed product type described this listing; the model declined to assign a label.',
  },
  no_declared_home: {
    tone: 'amber', text: 'Refused: this product type has no configured category for comparison.',
  },
  no_platform_category: {
    tone: 'amber', text: 'Refused: the listing category is missing, so the label cannot be checked.',
  },
  not_evaluated: { tone: '', text: 'Not evaluated.' },
};

const derivation = reason => DERIVATIONS[reason] ?? DERIVATIONS.not_evaluated;

const table = (headers, rows, caption) =>
  `<div class="table-wrap"><table class="provenance-table"><caption class="sr-only">${esc(caption)}</caption>` +
  `<thead><tr>${headers.map(h => `<th scope="col">${esc(h)}</th>`).join('')}</tr></thead>` +
  `<tbody>${rows.map(cells => `<tr>${cells.map((c, i) => (i === 0
    ? `<th scope="row">${c}</th>`
    : `<td>${c}</td>`)).join('')}</tr>`).join('')}</tbody></table></div>`;

const note = text => `<p class="note">${esc(text)}</p>`;

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

/**
 * The asking price and every step that produced the euro figure.
 *
 * The measurement table matters as much as the headline: a price seen once and
 * a price confirmed nine times are different evidence, and only repeat
 * sightings are stored at all.
 */
export function provenancePriceSection(price = {}, coverage = {}) {
  const conversion = price.currency_original && price.currency_original !== 'EUR'
    ? `${num(price.ask_original, 2, 2)} ${esc(price.currency_original)}`
    : eur(price.ask_original);
  const rate = price.fx_rate_to_eur == null
    ? (price.currency_original && price.currency_original !== 'EUR'
      ? 'No rate recorded'
      : 'Not applicable — already in euro')
    : `1 ${esc(price.currency_original ?? 'unit')} = ${num(price.fx_rate_to_eur, 6, 6)} EUR`;
  const measurements = price.measurements ?? [];
  const history = price.history ?? [];
  return (
    '<h3>The asking price</h3>' +
    note(`Asked for ${eur(price.ask_eur)}. The retained price record holds ` +
      `${num(price.n_intervals ?? 0)} interval(s)` +
      (price.n_measurements
        ? ` and ${num(price.n_measurements)} repeat sighting(s).`
        : ' and no repeat sighting, so this ask was seen once.') +
      (price.channel ? ` Last written by ${esc(price.channel.label)}.` : ' No channel is recorded for it.')) +
    '<div class="metric-list" style="margin-top:14px">' +
    metric('Price as published', conversion) +
    metric('Recorded by', channelLabel(price.channel)) +
    metric('Price last observed', date(price.price_observed_at)) +
    metric('Conversion to EUR', rate) +
    metric('Rate date and source', [price.fx_rate_date, price.fx_source]
      .filter(Boolean).map(esc).join(' · ') || DASH) +
    '</div>' +
    (price.currency_original && price.currency_original !== 'EUR' && price.fx_source == null
      ? '<p class="note note-warn">A currency conversion is applied without a recorded rate source. The euro figure cannot be reproduced.</p>'
      : '') +
    (history.length
      ? '<h4>Retained price record</h4>' +
        table(['Held from', 'Until', 'In EUR', 'As published', 'Rate source'],
          history.map(hit => [
            esc(date(hit.valid_from)),
            hit.valid_to ? esc(date(hit.valid_to)) : 'Current',
            eur(hit.ask_eur),
            hit.ask_original == null ? DASH : `${num(hit.ask_original, 2, 2)} ${esc(hit.currency_original ?? '')}`,
            hit.fx_source ? esc(hit.fx_source) : DASH,
          ]),
          'Asking-price intervals for this listing') +
        (price.n_intervals > history.length ? note(`${num(price.n_intervals)} intervals stored, showing the most recent.`) : '')
      : '<p class="note">No price interval is retained, so the ask cannot be traced to the observation that set it.</p>') +
    (measurements.length
      ? '<h4>Repeat sightings</h4>' +
        table(['Observed', 'Channel', 'As published', 'In EUR'],
          measurements.map(hit => [
            esc(date(hit.observed_at)),
            channelLabel(hit.channel),
            hit.ask_original == null ? DASH : `${num(hit.ask_original, 2, 2)} ${esc(hit.currency_original ?? '')}`,
            eur(hit.ask_eur),
          ]),
          'Repeat asking-price sightings for this listing') +
        (price.n_measurements > (price.measurements_shown ?? measurements.length)
          ? note(`Showing the ${num(price.measurements_shown)} most recent of ${num(price.n_measurements)} sightings.`)
          : '')
      : `<p class="note">No repeat sighting is retained, so the price is a single reading rather than a confirmed value. ` +
        `${num(coverage.price_intervals?.n ?? 0)} price interval(s) and ` +
        `${num(coverage.observations?.n ?? 0)} retained observation(s) are what stand behind it.</p>`)
  );
}

/**
 * One row per field the comparison leans on, with the channel that wrote it.
 *
 * A missing value carries its stored missing reason, because "the page does not
 * expose a buyer fee" and "we did not record one" call for different actions.
 *
 * `writers` is the part a winner column cannot show: every retained observation
 * that supplied this field, so agreement between channels is visible rather than
 * inferred from a single label.
 */
export function provenanceFieldsSection(fields = []) {
  const rows = fields.map(field => {
    const history = (field.writers ?? []).map(hit =>
      `<span class="provenance-writer">${esc(date(hit.observed_at))} ${hit.channel?.label ? esc(hit.channel.label) : 'unattributed'}` +
      ` → ${value(hit.kind ?? field.kind, hit.value)}</span>`).join('');
    return [
      `${esc(field.label)}<small class="metric-sub">${esc(field.field)}</small>`,
      field.value === null
        ? badge(field.missing_reason ? `Not recorded · ${field.missing_reason}` : 'Not recorded', 'amber')
        : value(field.kind, field.value),
      channelLabel(field.channel) +
        (field.n_writers > 1
          ? `<small class="metric-sub">${num(field.n_writers)} readings</small>` +
            (history ? `<div class="provenance-history">${history}</div>` : '')
          : field.n_writers === 1
            ? '<small class="metric-sub">1 retained reading</small>'
            : '<small class="metric-sub">no retained reading</small>'),
      esc(field.note ?? ''),
    ];
  });
  const unrecorded = fields.filter(field => field.value === null).length;
  const read = fields.filter(field => field.n_writers > 0).length;
  return (
    '<h3>Field by field</h3>' +
    note(`${num(fields.length - unrecorded)} of ${num(fields.length)} recorded fields have a value.` +
      (unrecorded ? ' A field with no value is never treated as zero.' : '') +
      ` ${num(read)} field(s) have a retained reading to check the winner against.`) +
    (rows.length ? table(['Field', 'Stored value', 'Recorded by', 'Why it matters'], rows,
      'Stored value and recording channel for each field') : '')
  );
}

/**
 * The classification that let this listing join a peer group at all.
 *
 * A shortlist row cannot exist without one, so when it is refused the gap
 * percentage behind it is not comparable with anything. That is worth saying in
 * the panel rather than leaving to be inferred from an empty field.
 */
export function provenanceDerivationSection(entry, identity = null) {
  if (identity) {
    const missing = [...(identity.missing_variant_fields ?? []), ...(identity.missing_comparison_fields ?? [])];
    const source = identity.review?.review_source === 'physical_inspection' ? 'Operator review with inspection evidence'
      : identity.review ? 'Operator listing review' : 'Current listing text and recorded fields';
    return '<h3>Product identity</h3><div class="metric-list">' +
      metric('Model', esc([identity.brand, identity.model].filter(Boolean).join(' ') || 'Unknown')) +
      metric('Comparison', identity.comparison_key ? 'Reviewed variant and single-unit offer' : 'Exact-product comparison withheld') +
      metric('Research level', esc({
        model_research: 'Model research',
        variant_asking_price_context: 'Variant asking-price context',
        reviewed_comparable_offer: 'Reviewed comparable offer',
        purchase_eligible: 'Purchase identity checks complete',
      }[identity.comparison_level] ?? 'Unsupported or incomplete')) +
      metric('Evidence', esc(source)) +
      metric('Missing facts', esc(missing.length ? missing.join(', ') : 'None reported')) +
      metric('Physical inspection', identity.requires_physical_inspection ? 'Still required' : 'Recorded') +
      (identity.missing_physical_checks?.length
        ? metric('Inspection checks remaining', esc(identity.missing_physical_checks.join(', '))) : '') +
      (identity.review?.source ? metric('Source checked', esc(date(identity.review.source.checked_at))) : '') +
      '</div>' +
      (identity.review && identity.evidence
        ? table(['Fact', 'Value', 'Verification'], Object.entries(identity.evidence).map(([field, item]) => [
          esc(field), esc(item.value ?? 'Unknown'), esc({
            physical_inspection: 'Recorded physical inspection',
            seller_statement: 'Seller statement reviewed',
          }[item.verification] ?? 'Verification not recorded'),
        ]), 'Product facts and their verification') : '') +
      note('Local identity rules and operator evidence are separate from LLM product-type classification. Seller statements are not physical inspection. Purchase identity checks do not establish resale proceeds, selling speed, full costs or profit.');
  }
  if (!entry) {
    return '<h3>Product type</h3><p class="note">No classification has been derived for this listing, so it cannot be matched to a peer group on product type. Anything comparing it is comparing product names.</p>';
  }
  const verdict = derivation(entry.reason);
  const digest = entry.input_sha256 ? String(entry.input_sha256).slice(0, 12) : null;
  return (
    '<h3>Product type</h3>' +
    `<div class="provenance-verdict">${badge(entry.product_type ?? 'No label', entry.accepted ? 'green' : 'amber')}` +
    `${badge(entry.accepted ? 'Used for matching' : 'Not used for matching', verdict.tone)}</div>` +
    note('Derived, never observed. The validation score checks schema and category consistency; semantic accuracy is unmeasured. A valid label alone does not establish comparable offers.') +
    '<div class="metric-list" style="margin-top:14px">' +
    metric('Decision', esc(verdict.text)) +
    metric('Model', entry.model ? `${esc(entry.model)}${entry.provider ? ' · ' + esc(entry.provider) : ''}` : DASH) +
    metric('Validation score', entry.confidence == null ? DASH : num(entry.confidence, 4, 4)) +
    metric('Validation basis', entry.confidence_basis ? esc(entry.confidence_basis) : DASH) +
    metric('Validation', entry.validation ? esc(entry.validation) : DASH) +
    metric('Prompt / vocabulary version', `${esc(entry.prompt_version ?? DASH)} · ${esc(entry.vocabulary_version ?? DASH)}`) +
    metric('Inputs still current', entry.input_sha256_matches === false
      ? 'No — the listing changed after this was derived'
      : entry.input_sha256_matches === true ? 'Yes' : 'Not checked') +
    metric('Input digest', digest ? esc(digest) : DASH) +
    metric('Derived at', date(entry.observed_at)) +
    '</div>'
  );
}

/** A count breakdown: how many prices came from each channel. */
const mixList = (title, items) =>
  `<div class="provenance-mix"><span>${esc(title)}</span>` +
  (items.length
    ? items.map(item => badge(`${esc(item.value)} · ${num(item.n)}`)).join('')
    : badge('Nothing recorded', 'amber')) +
  '</div>';

const STATE_TONE = { matched: 'green', unknown: 'amber', unavailable: 'red' };

/**
 * What the peer match rests on, field by field.
 *
 * The old panel listed the seven matching field *names* as identical badges on
 * every listing, which read as a constant while hiding the only thing that
 * varies: whether each one was actually recorded.
 */
export function provenanceMatchSection(peers = {}) {
  const matched = peers.matched_on ?? [];
  if (!matched.length) {
    return '<h4>What the peer match rests on</h4>' +
      '<p class="note">No peer match criteria are recorded.</p>';
  }
  return (
    '<h4>What the peer match rests on</h4>' +
    table(['Criterion', 'Recorded value', 'State'],
      matched.map(item => [
        esc(item.label),
        esc(item.subject ?? DASH),
        badge(item.state === 'matched' ? 'Recorded' : item.state === 'unknown' ? 'Not recorded' : 'Unavailable',
          STATE_TONE[item.state] ?? ''),
      ]),
      'Fields the peer comparison matched on') +
    ((peers.unknown_identity ?? []).length
      ? `<p class="note note-warn">Matching includes an unrecorded ${(peers.unknown_identity ?? []).join(' and an unrecorded ')}. ` +
        'Two listings that both lack a brand match on that absence, so this group can contain different products.</p>'
      : '')
  );
}

/**
 * The comparison set, described rather than asserted.
 *
 * This is the panel's most important half. A gap percentage is only meaningful
 * relative to peers that were priced the same way, converted the same way, and
 * observed inside the same freshness window, so all three are shown before the
 * median is.
 */
export function provenancePeersSection(peers = {}) {
  const stats = peers.stats ?? {};
  const samples = peers.samples ?? [];
  const unsupported = stats.status !== 'OK';
  return (
    '<h3>The comparison behind the gap</h3>' +
    note(peers.basis ?? 'No peer group is recorded for this listing.') +
    provenanceMatchSection(peers) +
    '<div class="metric-list" style="margin-top:14px">' +
    metric('Same category and condition', num(peers.n_group ?? 0)) +
    metric('Left after product type and identity match', num(peers.n_peers ?? 0)) +
    metric('Counted as fresh priced peers', `${num(peers.n_priced ?? 0)} / ${num(peers.min_n)} required`) +
    metric('Peer median ask', medianOrSuppressed(stats, peers.min_n)) +
    metric('Freshness window', `${num((peers.freshness_hours ?? 0) / 24, 1, 1)} days`) +
    metric('Peers priced between', peers.oldest_price_observed_at && peers.newest_price_observed_at
      ? `${esc(date(peers.oldest_price_observed_at))} – ${esc(date(peers.newest_price_observed_at))}`
      : DASH) +
    '</div>' +
    (unsupported
      ? '<p class="note note-warn">The peer median is not reportable. Any gap shown elsewhere for this listing rests on fewer peers than the minimum, not on a median of zero.</p>'
      : '') +
    mixList('Peer prices recorded by', peers.price_sources ?? []) +
    mixList('Currencies', peers.currencies ?? []) +
    mixList('Rate sources', peers.fx_sources ?? []) +
    (peers.variant_asking_price_context
      ? '<h4>Variant asking-price research</h4>' +
        metric('Fresh variant asks', num(peers.variant_asking_price_context.n_priced ?? 0)) +
        metric('Variant median ask', medianOrSuppressed(peers.variant_asking_price_context.stats ?? {},
          peers.variant_asking_price_context.min_n)) +
        note(peers.variant_asking_price_context.basis ?? 'Offer facts may differ; research context only.')
      : '') +
    (samples.length
      ? '<h4>Sample of counted peers</h4>' +
        table(['Listing', 'Ask', 'As published', 'Recorded by', 'Price observed'],
          samples.map(peer => [
            `<button class="row-link" type="button" data-provenance-peer="${esc(peer.listing_pk)}">${esc(peer.title || `Listing #${peer.listing_id}`)}</button>`,
            eur(peer.ask_eur),
            peer.ask_original == null ? DASH : `${num(peer.ask_original, 2, 2)} ${esc(peer.currency_original ?? '')}`,
            channelLabel(peer.channel),
            esc(date(peer.price_observed_at)),
          ]),
          'Peer listings behind the median') +
        note(`Showing ${num(samples.length)} of ${num(peers.n_priced ?? 0)} counted peers, ordered by asking price.`)
      : '<p class="note">No peer listing is stored for this comparison.</p>')
  );
}

/** Where the collector found this listing, and the public page it came from. */
export function provenanceDiscoverySection(payload = {}) {
  const listing = payload.listing ?? {};
  const found = payload.search_sources ?? [];
  return (
    '<h3>Where this listing came from</h3>' +
    '<div class="metric-list">' +
    metric('Platform and market', `${esc(listing.platform ?? DASH)} · ${esc(listing.subplatform ?? DASH)}`) +
    metric('Marketplace listing ID', esc(listing.listing_id ?? DASH)) +
    metric('Public listing page', payload.market_url
      ? `<a href="${esc(payload.market_url)}" target="_blank" rel="noopener noreferrer">Open on the marketplace</a>`
      : DASH) +
    '</div>' +
    (payload.market_url ? '' : `<p class="note">${esc(payload.market_url_note ??
      'No public listing page can be reconstructed for this platform. The stored listing id is the only marketplace identity recorded.')}</p>`) +
    (found.length
      ? '<h4>Search cells that returned it</h4>' +
        table(['Search cell', 'Category id', 'First seen', 'Last seen'],
          found.map(hit => [
            `<span class="provenance-url">${esc(hit.search_url ?? '')}</span>` +
              (hit.location_label ? `<small class="metric-sub">${esc(hit.location_label)} · ${esc(hit.location_grain ?? '')}</small>` : ''),
            esc(hit.category_path ?? DASH),
            esc(date(hit.first_seen_at)),
            esc(date(hit.last_seen_at)),
          ]),
          'Search cells that returned this listing') +
        note('Discovery decides which categories are collected at all. A category absent from this list was never searched, which is not the same as having no listings.')
      : '<p class="note">No search cell is recorded. The listing was not discovered through a stored search target.</p>')
  );
}

/**
 * The append-only record of what each channel saw, newest first.
 *
 * An empty table here means nothing was *retained*, not that nothing was
 * observed, and the two call for different reactions, so the wording says which.
 */
export function provenanceTimelineSection(timeline = [], coverage = {}) {
  const retained = coverage.observations?.n ?? 0;
  if (!timeline.length) {
    const since = coverage.observations?.retained_since;
    return '<h3>Observation history</h3>' +
      '<p class="note note-warn">No append-only observation is retained for this listing, so which channels have ' +
      'read it cannot be established. This is not the same as nothing having been observed: ' +
      (since
        ? `the retained record for this workspace begins ${esc(date(since))}. `
        : 'this listing has no retained reading at all. ') +
      'Only the channel that won each field is known, so agreement between channels cannot be checked.</p>';
  }
  return (
    '<h3>Observation history</h3>' +
    note(`Append-only. ${num(retained)} retained observation(s), showing ` +
      `${num(timeline.length)}. Each row is one channel reporting on the listing, with the price that channel actually supplied.`) +
    table(['Observed', 'Channel', 'Price reported', 'Fields supplied'],
      timeline.map(entry => [
        esc(date(entry.observed_at)),
        channelLabel(entry.channel),
        entry.ask_eur == null && entry.ask_original == null
          ? 'No price'
          : `${entry.ask_original == null ? DASH : `${num(entry.ask_original, 2, 2)} ${esc(entry.currency_original ?? '')}`} → ${eur(entry.ask_eur)}`,
        num(entry.n_fields_supplied ?? 0),
      ]),
      'Recorded observations for this listing')
  );
}

/** Identity and lifecycle timestamps. */
export function provenanceIdentitySection(identity = {}) {
  return (
    '<h3>Observation record</h3>' +
    '<div class="metric-list">' +
    metric('First seen', date(identity.first_seen_at)) +
    metric('Last seen', date(identity.last_seen_at)) +
    metric('Last confirmed not terminal', date(identity.last_live_at)) +
    metric('Last status-check attempt', date(identity.last_probed_at)) +
    metric('Platform published', date(identity.platform_published_at)) +
    metric('Terminal state last checked', date(identity.outcome_observed_at)) +
    metric('Coverage stopped', date(identity.observation_coverage_end)) +
    metric('Normalization version', esc(identity.normalization_version ?? DASH)) +
    '</div>'
  );
}

/**
 * What this panel cannot vouch for, before any figure is read.
 *
 * A provenance view that only lists what it found is a marketing page. Each
 * warning is a fact about the evidence, ordered by how much it should change the
 * reader's next action, and each one is a stored condition rather than a guess.
 */
export function provenanceWarnings(warnings = []) {
  if (!warnings.length) {
    return '<div class="provenance-clear">' + badge('No provenance gaps detected', 'green') + '</div>';
  }
  const rank = { high: 0, medium: 1, low: 2 };
  const ordered = [...warnings].sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3));
  return (
    '<details class="provenance-warnings" open>' +
    `<summary>${badge(`${ordered.length} thing${ordered.length === 1 ? '' : 's'} this panel cannot vouch for`)}</summary>` +
    '<ul>' +
    ordered.map(warning =>
      `<li class="provenance-warning ${esc(warning.severity)}">${badge(warning.severity, warning.severity === 'high' ? 'red' : warning.severity === 'medium' ? 'amber' : '')}` +
      `<span>${esc(warning.message)}</span></li>`).join('') +
    '</ul></details>'
  );
}

/**
 * The panel header: what this is, when it was read, and what it is keyed on.
 *
 * `revision` is the whole point of it being shown. The dialog polls while it is
 * open, and the operator has to be able to tell a panel that is being kept
 * current from one that is showing a number captured an hour ago.
 */
export function provenanceHeader(payload = {}) {
  const listing = payload.listing ?? {};
  const coverage = payload.coverage ?? {};
  return (
    '<div class="provenance-head">' + evidenceChip('observed') +
    `<span>${esc(listing.title ?? `Listing #${listing.listing_id ?? ''}`)}</span>` +
    badge(`#${esc(listing.listing_id ?? '')}`) +
    `<span class="provenance-stamp" data-provenance-revision="${esc(payload.revision ?? '')}" ` +
    `data-provenance-captured="${esc(payload.captured_at ?? '')}">` +
    `Read ${esc(date(payload.captured_at))} · revision ${esc(payload.revision ?? 'unknown')}</span>` +
    '</div>' +
    `<p class="note provenance-coverage">Retained for this listing: ` +
    `${num(coverage.observations?.n ?? 0)} observation(s) from ` +
    `${coverage.observations?.retained_since ? esc(date(coverage.observations.retained_since)) : 'no retained record'}, ` +
    `${num(coverage.price_intervals?.n ?? 0)} price interval(s), ` +
    `${num(coverage.price_measurements?.n ?? 0)} repeat measurement(s), ` +
    `${num(coverage.search_attributions?.n ?? 0)} search cell(s). ` +
    `${num(coverage.fields_with_channel ?? 0)} of ${num(coverage.fields_total ?? 0)} listed fields name a channel. ` +
    'A count of zero means nothing was written down, not that nothing happened.</p>'
  );
}

/**
 * The whole panel.
 *
 * Ordered so the reader meets the conclusion before the workings: what was
 * asked, then who wrote each field, then the comparison it is standing against.
 */
export function provenanceAttributesSection(evidence) {
  if (!evidence?.snapshots?.length) return '';
  const claims = evidence.assertions ?? [];
  const rows = claims.map(entry => '<tr>' +
    `<td>${esc(entry.attribute)}</td><td>${esc(String(entry.value))}</td>` +
    `<td>${esc(entry.source_field)} · ${esc(entry.source)}</td>` +
    `<td>${esc(entry.evidence_token)}</td><td><time datetime="${esc(entry.observed_at)}">${esc(date(entry.observed_at))}</time></td>` +
    '</tr>').join('');
  const conflicts = evidence.conflicts?.length
    ? `<p class="note">Conflicting source claims: ${esc(evidence.conflicts.join(', '))}. Review the source before comparing.</p>` : '';
  return '<section class="provenance-section"><h3>Product facts to verify</h3>' +
    '<p class="note">Seller claims extracted from collected responses. These facts have not been independently reviewed or physically inspected.</p>' +
    conflicts + (rows ? '<table><thead><tr><th>Attribute</th><th>Claimed value</th><th>Source field</th><th>Matched token</th><th>Observed</th></tr></thead>' +
      `<tbody>${rows}</tbody></table>` : '<p class="note">No supported attribute claims were found in these observations.</p>') +
    '</section>';
}

export function provenanceView(payload = {}) {
  const coverage = payload.coverage ?? {};
  return [
    provenanceHeader(payload),
    provenanceWarnings(payload.warnings),
    provenancePriceSection(payload.price, coverage),
    provenanceFieldsSection(payload.fields),
    provenanceAttributesSection(payload.product_attributes),
    provenanceDerivationSection(payload.peers?.classification, payload.peers?.product_identity),
    provenancePeersSection(payload.peers),
    provenanceDiscoverySection(payload),
    provenanceTimelineSection(payload.timeline, coverage),
    provenanceIdentitySection(payload.identity),
    `<p class="note provenance-note">${esc(payload.note ??
      'Every figure on this panel is a stored observation or a stored derivation, never an assumption.')}</p>`,
  ].filter(Boolean).join('');
}
