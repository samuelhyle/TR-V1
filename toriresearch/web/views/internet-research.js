import {esc, eur} from '../format.js';

const labels = {asking_price: 'Published asking price', product_specs: 'Product specifications', fee_terms: 'Fees and terms', research: 'Reference material'};
const stamp = value => value ? esc(new Date(value).toLocaleString()) : 'Not checked';
const sourceLink = source => '<a href="' + esc(source.url) + '" target="_blank" rel="noopener noreferrer">' + esc(source.label || source.evidence?.title || new URL(source.url).hostname) + '</a>';

export function sourceCard(source) {
  const evidence = source.evidence;
  const prices = (evidence?.products || []).flatMap(product => (product.offers || []).map(offer =>
    '<li>' + esc(product.name || product.model || 'Product') + ': <strong>' +
    (offer.currency === 'EUR' ? eur(offer.amount) : esc(offer.amount + ' ' + offer.currency)) + '</strong> asking price' +
    ' · ' + esc(offer.condition?.split('/').pop() || 'condition unknown') +
    ' · ' + esc(offer.availability?.split('/').pop() || 'stock unknown') +
    (offer.valid_until ? ' · valid until ' + esc(offer.valid_until) : '') +
    (offer.validity === 'expired' ? ' · <strong>Published offer expired</strong>' : '') + '</li>'));
  return '<article class="internet-source"><div class="desk-check-head"><strong>' + sourceLink(source) + '</strong>' +
    '<span class="badge ' + (source.fresh ? '' : 'amber') + '">' + (source.fresh ? 'Source checked recently' : evidence ? 'Needs a fresh check' : 'Awaiting evidence') + '</span></div>' +
    '<p class="note">' + esc(labels[source.kind] || source.kind) + ' · ' + esc(source.match?.status?.replaceAll('_', ' ') || 'match unverified') +
    ' · exact offer unverified</p>' +
    (source.match?.conflicts?.length ? '<p class="message error">Variant conflicts: ' + esc(source.match.conflicts.join(', ')) + '</p>' : '') +
    '<p class="observation-meta">Retrieved: ' + stamp(source.retrieved_at) + ' · last attempt: ' + stamp(source.last_attempt_at) +
    ' · fresh for ' + esc(source.max_age_hours) + ' hours</p>' +
    (evidence ? '<p class="observation-meta">Publisher publication date: ' + esc(evidence.published_at || 'not supplied') +
      ' · publisher update date: ' + esc(evidence.publisher_updated_at || 'not supplied') + '</p>' : '') +
    (source.last_error ? '<p class="message error">Last check failed: ' + esc(source.last_error) + '. Previous evidence is retained.</p>' : '') +
    (prices.length ? '<ul>' + prices.join('') + '</ul><p class="note">Shipping and fees are not established. Published asks do not establish completed sales or resale proceeds.</p>' :
      evidence ? '<p class="note">No single structured asking price was extracted.</p>' : '') +
    (evidence?.excerpt ? '<details><summary>Publisher excerpt</summary><p>' + esc(evidence.excerpt) + '</p></details>' : '') +
    '<div class="desk-links"><button class="btn small" data-internet-refresh="' + esc(source.source_id) + '" type="button">Refresh this source</button>' +
    '<button class="btn small" data-internet-watch="' + esc(source.source_id) + '" data-enabled="' + !source.watch_enabled + '" type="button">' +
    (source.watch_enabled ? 'Stop watching' : 'Watch every ' + esc(source.interval_hours) + ' hours') + '</button>' +
    '<button class="btn quiet small" data-internet-remove="' + esc(source.source_id) + '" type="button">Remove source</button></div>' +
    (source.watch_enabled ? '<p class="observation-meta">Next scheduled check: ' + stamp(source.next_due_at) +
    (source.review_updates ? ' · review changed evidence with local LLM' : ' · collect source evidence') + '</p>' : '') + '</article>';
}

const references = (ids, facts) => '<span class="observation-meta"> Evidence: ' + ids.map(id => {
  const fact = facts.find(item => item.id === id);
  if (id.startsWith('source:') && fact?.data?.url) return sourceLink({url: fact.data.url, label: fact.data.label});
  return esc({listing: 'Stored listing', identity: 'Product identity', economics: 'Economic evidence limits'}[id] || id);
}).join(' · ') + '</span>';

export function reviewPanel(report = {}) {
  const review = report.review;
  if (!review) return '<p class="note">Run a local LLM review to get a source-cited interpretation, risks and next checks.</p>';
  if (!review.current || !review.review) return '<p class="message info">This interpretation is out of date. Review the current evidence again.</p>';
  const value = review.review, facts = report.facts || [];
  const section = item => '<p>' + esc(item.text) + references(item.references, facts) + '</p>';
  return '<p class="note">LLM interpretation · ' + esc(value.stance.replaceAll('_', ' ')) + ' · ' + esc(review.model?.name || 'local model') +
    ' · ' + stamp(review.completed_at) + '</p>' + section(value.interpretation) +
    '<h4>Risks and uncertainty</h4>' + value.risks.map(section).join('') +
    '<h4>Next checks</h4>' + value.next_checks.map(section).join('') +
    '<p class="note">Interpretations can be wrong. They do not verify identity, establish profit or authorize a purchase.</p>';
}

export function jobPanel(runs = []) {
  if (!runs.length) return '<p class="note">No internet research jobs have run for this offer.</p>';
  return '<ul class="desk-sessions">' + runs.slice(0, 5).map(run => '<li><strong>' + esc(run.operation.replaceAll('_', ' ')) +
    ' · ' + esc(run.status) + '</strong><span class="observation-meta"> · ' + stamp(run.created_at) + ' · ' + esc(run.origin) +
    ' · ' + esc(run.result?.requests || 0) + ' requests</span>' + (run.error ? '<p class="message error">' + esc(run.error) + '</p>' : '') +
    (run.result?.sources || []).filter(source => source.error).map(source => '<p class="message error">' + esc(source.error) + '</p>').join('') + '</li>').join('') + '</ul>';
}

export function internetMonitor(report = {}) {
  return '<p class="note">' + esc(report.watched_sources || 0) + ' watched sources · ' + esc(report.due_sources || 0) +
    ' due · ' + esc(report.active?.length || 0) + ' active jobs. Registered URLs refresh while this app runs.</p>' +
    '<ul class="desk-sessions">' + (report.recent || []).slice(0, 8).map(run => '<li><button class="row-link" type="button" data-internet-monitor-listing="' +
    esc(run.listing_pk) + '">Offer ' + esc(run.listing_pk) + '</button> · ' + esc(run.operation.replaceAll('_', ' ')) +
    ' · ' + esc(run.status) + ' · ' + esc(run.origin) + ' · ' + stamp(run.created_at) +
    (run.error ? '<p class="message error">' + esc(run.error) + '</p>' : '') + '</li>').join('') + '</ul>';
}

let cleanup = () => {};
const drafts = new Map();

export function closeInternetResearch() {
  cleanup();
  cleanup = () => {};
}

export function mountInternetResearch(container, listing, call) {
  closeInternetResearch();
  if (!container || !listing?.listing_pk) return;
  const element = document.createElement('section');
  element.className = 'internet-research';
  const query = encodeURIComponent(listing.title || 'product model');
  element.innerHTML = '<h3 class="desk-subhead">Internet evidence and LLM review</h3>' +
    '<p class="note">Add public product, price or fee pages. Sources keep their URL and retrieval time; model mentions require verification. Watches refresh registered URLs while this app runs.</p>' +
    '<a href="https://www.google.com/search?q=' + query + '" target="_blank" rel="noopener noreferrer">Find source pages on the web</a>' +
    '<form data-internet-add class="internet-source-form"><label>Public HTTPS source URL<input name="url" type="url" required maxlength="2048" placeholder="https://…"></label>' +
    '<label>Source type<select name="kind">' + Object.entries(labels).map(([value, label]) => '<option value="' + value + '">' + label + '</option>').join('') + '</select></label>' +
    '<label>Label<input name="label" maxlength="120" placeholder="Retailer, manufacturer or guide"></label>' +
    '<label>Watch interval (hours)<input name="interval" type="number" min="6" max="168" step="1" value="24" required></label>' +
    '<label><input name="watch" type="checkbox"> Watch this source automatically</label>' +
    '<label><input name="review_updates" type="checkbox"> Review changed evidence with local LLM</label><button class="btn small" type="submit">Add source</button></form>' +
    '<p class="message" data-internet-message role="status" hidden></p>' +
    '<div class="desk-links"><button class="btn small" data-internet-run="refresh" type="button">Refresh sources</button>' +
    '<button class="btn small" data-internet-run="review" type="button">Review stored evidence with LLM</button>' +
    '<button class="btn primary small" data-internet-run="refresh_review" type="button">Refresh and review</button></div>' +
    '<p class="note">Each job checks up to three sources, including at most twelve HTTP requests. Sources rotate across jobs. Refreshes never populate the proceeds calculator.</p>' +
    '<div data-internet-sources><p class="note">Reading stored internet evidence…</p></div>' +
    '<h3 class="desk-subhead">Local LLM interpretation</h3><div data-internet-review></div>' +
    '<details><summary>Research job monitor</summary><div data-internet-jobs></div></details>';
  container.append(element);
  const form = element.querySelector('[data-internet-add]');
  const draft = drafts.get(listing.listing_pk);
  if (draft) for (const [name, value] of Object.entries(draft)) {
    const field = form.elements.namedItem(name);
    if (field.type === 'checkbox') field.checked = value;
    else field.value = value;
  }
  const controller = new AbortController();
  let timer = null, stopped = false, busy = false, active = false;
  const message = (text, error = false) => {
    const node = element.querySelector('[data-internet-message]');
    node.textContent = text;
    node.className = 'message ' + (error ? 'error' : 'info');
    node.hidden = !text;
  };
  const read = async () => {
    clearTimeout(timer);
    if (stopped) return;
    try {
      if (element.closest('[hidden]')) {timer = setTimeout(read, 15000); return;}
      const report = await call('/api/research/internet?listing_pk=' + listing.listing_pk, {signal: controller.signal});
      if (stopped) return;
      element.querySelector('[data-internet-sources]').innerHTML = report.sources.length ? report.sources.map(sourceCard).join('') : '<p class="note">No sources registered for this offer.</p>';
      element.querySelector('[data-internet-review]').innerHTML = reviewPanel(report);
      element.querySelector('[data-internet-jobs]').innerHTML = jobPanel(report.runs);
      active = report.runs.some(run => ['queued', 'running'].includes(run.status));
      element.querySelectorAll('[data-internet-run]').forEach(button => {button.disabled = active || busy;});
      timer = setTimeout(read, active ? 2000 : 30000);
    } catch (error) {
      if (!stopped && error.name !== 'AbortError') {message(String(error.message || error), true); timer = setTimeout(read, 30000);}
    }
  };
  const act = async payload => {
    if (busy || stopped) return;
    busy = true;
    try {
      const response = await call('/api/research/internet/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...payload, listing_pk: listing.listing_pk}), signal: controller.signal});
      if (stopped) return;
      message(response.run_id ? 'Research job queued. The monitor will show its result.' : 'Source settings saved.');
      await read();
    } catch (error) {
      if (!stopped && error.name !== 'AbortError') message(String(error.message || error), true);
    } finally {busy = false; if (!stopped) element.querySelectorAll('[data-internet-run]').forEach(button => {button.disabled = active;});}
  };
  element.addEventListener('submit', event => {
    if (!event.target.matches('[data-internet-add]')) return;
    event.preventDefault();
    const fields = event.target.elements;
    act({action: 'add', url: fields.url.value.trim(), kind: fields.kind.value, label: fields.label.value, interval_hours: Number(fields.interval.value), watch: fields.watch.checked, review_updates: fields.review_updates.checked});
  });
  element.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.internetRun) act({action: 'run', operation: button.dataset.internetRun});
    if (button.dataset.internetRefresh) act({action: 'run', operation: 'refresh', source_ids: [button.dataset.internetRefresh]});
    if (button.dataset.internetWatch) act({action: 'watch', source_id: button.dataset.internetWatch, watch: button.dataset.enabled === 'true'});
    if (button.dataset.internetRemove) act({action: 'remove', source_id: button.dataset.internetRemove});
  });
  cleanup = () => {
    drafts.delete(listing.listing_pk);
    drafts.set(listing.listing_pk, Object.fromEntries([...form.elements].filter(field => field.name)
      .map(field => [field.name, field.type === 'checkbox' ? field.checked : field.value])));
    if (drafts.size > 50) drafts.delete(drafts.keys().next().value);
    stopped = true; clearTimeout(timer); controller.abort(); element.remove();
  };
  read();
}
