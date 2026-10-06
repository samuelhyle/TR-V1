"""Explicit reusable assumptions, using existing research-session storage."""
from datetime import datetime, timezone

from . import analysis, db, economics, inventory, item_workspace, product_profiles, research, research_desk

VERSION = 'scenario-workspace-v1'
LIBRARY_LIMIT = 100
ORIGINS = {'operator_assumption', 'observed_ask', 'observed_inbound', 'calculator_default', 'saved_assumption', 'legacy_assumption'}


class Conflict(ValueError):
    """The operator must review changed evidence before writing."""


def _hash(value):
    return inventory._hash(value)


def _fields(mode):
    if mode not in ('cash', 'operating'):
        raise ValueError('choose cash or operating scope')
    return research.SCENARIO_FIELDS | (research.OPERATING_FIELDS if mode == 'operating' else set())


def inputs(values, mode):
    fields = _fields(mode)
    if not isinstance(values, dict) or set(values) - fields:
        raise ValueError('unsupported assumption fields for this cost scope')
    result = {}
    # Quantities and rates retain precision; only results are cent-rounded.
    for key in sorted(fields):
        amount = economics.amount(values.get(key), key)
        result[key] = float(amount) if amount is not None else None
    if result['fee'] is not None and result['fee'] >= 100 or result['haircut'] is not None and result['haircut'] > 50:
        raise ValueError('fee must be below 100% and haircut at most 50%')
    for key in ('days', 'storage_units'):
        if key in result and result[key] is not None and (not result[key].is_integer() or key == 'days' and result[key] > 36500):
            raise ValueError(key+' must be a bounded whole number')
    return result


def library():
    entries = []
    for session in research_desk.sessions()['sessions']:
        for pk, saved in session['scenarios'].items():
            if not str(pk).isdigit() or not isinstance(saved, dict):
                continue
            envelope = saved.get('version') == VERSION
            metadata = saved if envelope else saved.get('_scenario') or {}
            if not isinstance(metadata, dict):
                continue
            raw = metadata.get('inputs', {}) if envelope else saved
            if not isinstance(raw, dict):
                continue
            mode = metadata.get('mode') if envelope else ('operating' if research.OPERATING_FIELDS & raw.keys() else 'cash')
            try:
                values = inputs({key: value for key, value in raw.items() if key in _fields(mode)}, mode)
            except (ValueError, TypeError):
                continue
            entries.append(dict(id=f"{session['session_pk']}:{pk}", name=session['name'], listing_pk=int(pk),
                                updated_at=session['updated_at'], revision=_hash(saved), mode=mode, inputs=values,
                                profile=metadata.get('profile'), origins=saved.get('_origins') or metadata.get('origins', {})))
    return dict(version=VERSION, entries=entries[:LIBRARY_LIMIT], total=len(entries), truncated=len(entries) > LIBRARY_LIMIT)


def context(seed):
    if not isinstance(seed, dict):
        raise ValueError('one explicit item reference is required')
    with db.read_snapshot():
        item = item_workspace.view(**seed)
        source = item['source']['context'] or {}
        identity = source.get('current_identity') or {}
        profile = product_profiles.summary(identity, identity.get('listing_analysis'), item['source']['listing'])
        selected = next((row for row in item['decisions'] if row['decision_pk'] == item['selected_decision_pk']), None)
        comparison = None
        if selected:
            row = research.journal(decision_pk=selected['decision_pk'])['items'][0]
            comparison = {key: row.get(key) for key in ('profit_comparison_status', 'profit_comparison_note', 'comparison_profit_eur', 'profit_vs_assumption_eur', 'explicit_acquisition_link', 'linked_decision_pk')}
        listing = item['source']['listing']
        costs = db.query_one('SELECT shipping_cost_buyer_eur,buyer_fee_eur FROM listings WHERE listing_pk=?', (listing['listing_pk'],)) if listing else {}
        inbound = [analysis.amount((costs or {}).get(key)) for key in ('shipping_cost_buyer_eur', 'buyer_fee_eur')]
        observed = dict(buy=listing.get('listed_price_eur') if listing else None,
                        inbound=sum(inbound) if all(value is not None and value >= 0 for value in inbound) else None)
        return dict(version=VERSION, seed=item['seed'], title=item['title'], source=item['source'], profile=profile,
                    observed_costs=observed,
                    revision=_hash(dict(item_revision=item['revision'], profile_sha256=profile['sha256'], observed_costs=observed)),
                    selected_scenario=selected['scenario'] if selected else None, actual_comparison=comparison,
                    can_save=bool(listing), library=library())


def evaluate(seed, revision, values, mode='cash', origins=None, reused_from=None):
    with db.read_snapshot():
        current = context(seed)
        if not isinstance(revision, str) or len(revision) != 64:
            raise ValueError('a current evidence revision is required')
        if current['revision'] != revision:
            raise Conflict('Item or profile evidence changed. Refresh evidence and evaluate again; entered assumptions are retained.')
        values = inputs(values, mode)
        if origins is not None and (not isinstance(origins, dict) or set(origins) - values.keys() or any(value not in ORIGINS for value in origins.values())):
            raise ValueError('unsupported assumption origins')
        origins = {key: (origins or {}).get(key, 'operator_assumption') for key in values}
        listing = current['source']['listing'] or {}
        for key in values:
            if origins[key] == 'observed_ask' and (key != 'buy' or values[key] != listing.get('listed_price_eur')):
                origins[key] = 'operator_assumption'
            if origins[key] == 'observed_inbound' and (key != 'inbound' or current['observed_costs']['inbound'] is None or values[key] != current['observed_costs']['inbound']):
                origins[key] = 'operator_assumption'
        if reused_from is not None:
            if not isinstance(reused_from, dict) or set(reused_from) != {'id', 'revision'}:
                raise ValueError('a saved scenario ID and revision are required')
            entry = next((row for row in current['library']['entries'] if row['id'] == reused_from['id']), None)
            if not entry or entry['revision'] != reused_from['revision']:
                raise Conflict('The saved scenario changed or was removed. Reopen it before evaluating this copy.')
            origins = {key: 'saved_assumption' if entry['inputs'].get(key) == value and value is not None
                       else 'operator_assumption' if origins[key] == 'saved_assumption' else origins[key]
                       for key, value in values.items()}
        else:
            retained = (current['selected_scenario'] or {}).get('inputs', {})
            origins = {key: 'legacy_assumption' if origin == 'saved_assumption' and retained.get(key) == values[key]
                       else 'operator_assumption' if origin == 'saved_assumption' else origin
                       for key, origin in origins.items()}
        missing = [key for key, value in values.items() if value is None]
        evaluated = research.scenario(values) if not missing else None
        return dict(version=VERSION, mode=mode, inputs=values, origins=origins, reused_from=reused_from,
                    source_revision=current['revision'], seed=current['seed'], profile=current['profile'],
                    status='INCOMPLETE' if missing else 'COMPLETE', unknown_inputs=missing,
                    calculation=evaluated, inputs_sha256=_hash(values),
                    note='Explicit assumptions, not a forecast. Cash and operating scopes differ; shared capacities cannot be added across products.')


def save(name, seed, revision, values, mode='cash', origins=None, reused_from=None):
    name = research._text(name, 'scenario name', 50, True)
    with db.read_snapshot():
        result = evaluate(seed, revision, values, mode, origins, reused_from)
        current = context(seed)
        listing = current['source']['listing']
        if not listing:
            raise ValueError('Save reusable scenarios from a retained market source. Retained decisions remain readable after source expiry.')
        pk = listing['listing_pk']
        filters = dict(platform=listing['platform'], subplatform=listing['subplatform'])
    body = research._json({str(pk): result})
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    try:
        with conn.transaction():
            # Recheck inside the write boundary. Names identify immutable copies.
            if context(seed)['revision'] != revision:
                raise Conflict('Evidence changed before saving. Refresh and evaluate again.')
            existing = conn.execute('SELECT scenarios_json FROM research_sessions WHERE name=?', ('Scenario: '+name,)).fetchone()
            if existing:
                if existing[0] != body:
                    raise Conflict('This scenario name is already saved. Use a new name for another revision.')
                return dict(ok=True, replayed=True, scenario=result, library=library())
            if conn.execute('SELECT COUNT(*) FROM research_sessions').fetchone()[0] >= research_desk.SESSION_LIMIT:
                raise ValueError('The saved-session limit is reached; remove an unwanted session before saving another scenario.')
            inserted = conn.execute('INSERT INTO research_sessions (name,filters_json,selected_pks_json,notes,scenarios_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (name) DO NOTHING',
                         ('Scenario: '+name, research._json(filters), research._json([pk]), 'Reusable explicit assumptions. Original decisions remain frozen.', body, now, now))
            retained = conn.execute('SELECT scenarios_json FROM research_sessions WHERE name=?', ('Scenario: '+name,)).fetchone()
            if retained[0] != body:
                raise Conflict('This scenario name is already saved. Use a new name for another revision.')
            replayed = inserted.rowcount == 0
    finally:
        conn.close()
    return dict(ok=True, replayed=replayed, scenario=result, library=library())


def freeze_decision(values, metadata, platform, source_listing_id):
    if not isinstance(metadata, dict) or set(metadata) != {'seed', 'revision', 'mode', 'origins', 'reused_from', 'inputs_sha256'}:
        raise ValueError('the evaluated scenario context is required')
    if _hash(inputs(values, metadata['mode'])) != metadata['inputs_sha256']:
        raise Conflict('Assumptions changed after evaluation. Evaluate again before attaching them.')
    with db.read_snapshot():
        result = evaluate(metadata['seed'], metadata['revision'], values, metadata['mode'], metadata['origins'], metadata['reused_from'])
        current = context(metadata['seed'])
    if (current['source']['platform'], current['source']['listing_id']) != (platform, source_listing_id):
        raise ValueError('scenario context belongs to a different source')
    if result['status'] != 'COMPLETE':
        raise ValueError('complete the assumptions before attaching this scenario')
    frozen = result['calculation']
    frozen['provenance'] = {key: result[key] for key in ('version', 'mode', 'origins', 'reused_from', 'source_revision', 'profile', 'inputs_sha256')}
    return frozen
