#!/usr/bin/env python3
"""Export the original dashboard's own payload from the runtime that owns the database.

The hosted workspace serves the original dashboard, so its shell needs exactly the
payload `dashboard.snapshot()` builds. Recomputing those aggregates inside the
Netlify function would mean publishing numbers this project never measured, so the
runtime that owns the research database computes them once and the function replays
them verbatim.

A segment filter selects a different analysis rather than a different page of one:
it changes which rows the aggregates are measured over, so an unfiltered payload
cannot answer a filtered read. The export therefore measures the market as a whole
once, then measures only the filter-dependent blocks for each requested
combination. `_snapshot` builds those three blocks -- `panels`, `liquidity` and
`filters` -- from the filter and from nothing else, so reusing the invariant blocks
is exact rather than an approximation. Everything else in the payload is measured
over the same evidence whatever the filter, and is carried once.

Example:
  python -m toriresearch.cli dashboard --host 127.0.0.1 --port 8787   # in the container
  docker exec -i toriresearch-app-1 python - < bin/export_netlify_workspace.py > data/netlify/workspace.json
  gzip -c data/netlify/workspace.json > data/netlify/workspace.json.gz
  node bin/publish_netlify_workspace.mjs

Pass `--segments` to also measure named filter combinations, and `--segment-limit`
to bound how many. Omit both to export the market as a whole only.

The output is private data and is never a static-site build input.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from toriresearch import (dashboard, drafts, inventory, metrics, research, research_desk,
                          scenario_workspace, trades)


# Secrets, credentials and filesystem locations never leave the runtime. The
# capability flags go too: the hosted function refuses every write, so a rendered
# edit affordance would be a lie about what this deployment can do.
PRIVATE_KEYS = {'password_hash', 'csrf_token', 'token_hash', 'client_token', 'token',
                'capabilities', 'can_edit', 'can_save', 'can_publish', 'input_path',
                'model_path', 'artifact_path', 'database_path'}

# The collected collection authority decides which buttons the dashboard offers. A
# hosted deployment performs no collection, so it must not claim an authority it
# cannot exercise. Everything else in the payload is reported exactly as measured.
HOSTED_PERMISSIONS = {
    'valid': False,
    'error': 'Collection actions run from the local research runtime, not from this hosted workspace.',
    'note': 'This dashboard is a read-only view of the last exported research snapshot.',
}

HOSTED_BIND = ('This hosted workspace reads stored research only. It never requests the '
               'platform, never runs a model and never trains one. Refreshing this page '
               'replays the last payload the local runtime exported.')

MARKETS = ('standard', 'diili')

# Base 36 rather than hex: the same value in fewer characters, and the function side
# is JavaScript, where `toString(36)` is the idiomatic way to write it.
DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'

# The blocks `_snapshot` derives from the filter. Everything else it computes is
# measured over the same evidence whatever the filter is set to.
FILTER_DEPENDENT = ('panels', 'liquidity', 'filters')


def project(value):
    if isinstance(value, dict):
        return {k: project(v) for k, v in value.items() if k not in PRIVATE_KEYS}
    if isinstance(value, (tuple, list)):
        return [project(item) for item in value]
    return value


def hosted_posture(payload):
    return {**payload.get('posture', {}), 'bind': HOSTED_BIND}


def hosted_health(health):
    """Report the collector as idle by construction rather than by observation.

    Keeping the measured values would claim a collection run this deployment cannot
    perform, and `running_actions` would name jobs no browser here can monitor.
    """
    return {**health, 'collector_running': False, 'blocked': None, 'running_actions': []}


def envelope(reader):
    """The non-item half of a paged read: counts, options and totals the browser draws.

    The research snapshot already stores every page of these rows as one array, so
    only the envelope has to travel here for the function to paginate them again.
    """
    first = reader(page=1)
    return {key: value for key, value in first.items() if key not in {'items', 'page', 'pages'}}


def filters_for(market_name):
    return {'platform': 'tori', 'subplatform': market_name}


def seed_payload(payload):
    """The filter-independent payload, with the hosted posture applied."""
    payload['posture'] = hosted_posture(payload)
    payload['health'] = hosted_health(payload.get('health') or {})
    payload['permissions'] = HOSTED_PERMISSIONS
    return payload


def measured(filters):
    """The full payload for one filter combination. Only used by tests."""
    return seed_payload(dashboard._snapshot(filters))


def measure(filters):
    """The blocks a filter changes, measured over the filter."""
    return {'panels': dashboard.panels(filters),
            'liquidity': metrics.liquidity(**filters),
            'filters': dict(filters)}


def scoped(base, filters, measure_blocks=measure):
    """A payload for one filter combination, reusing the invariant blocks.

    `_snapshot` walks the same evidence for `health`, `quality`, `coverage`,
    `compliance`, `segments` and the ledger blocks whatever the filter says, so
    those are copied from the base measurement rather than recomputed. Only the
    three filter-dependent blocks are asked for again.

    The measuring is injected rather than called directly so the reuse can be
    asserted without a research database -- that the invariant blocks really are
    carried across, and not quietly recomputed per segment, is the property that
    keeps the export's size and time bounded.
    """
    return {**base, **measure_blocks(filters)}


def segment_key(filters):
    """The same key the function derives from a query string.

    Kept byte-identical to `netlify/lib/workspace.mjs`;
    `tests/test_netlify_workspace_contract.py` runs both and compares, because a key
    that disagreed would silently serve the market as a whole in place of the filter
    the operator asked for -- a wrong number that looks like a right one, with
    nothing anywhere reporting a fault.
    """
    order = ('category_root_norm', 'category_norm', 'municipality_norm',
             'condition_norm', 'seller_type_norm', 'focus_path')
    pairs = [f'{key}={filters[key]}' for key in order if filters.get(key)]
    if not pairs:
        return 'all'
    material = '\n'.join(pairs)
    value = 0x811c9dc5
    for character in material:
        value ^= ord(character)
        value = (value * 0x01000193) & 0xFFFFFFFF
    digits = ''
    while value:
        value, remainder = divmod(value, 36)
        digits = DIGITS[remainder] + digits
    return 's' + (digits or '0')


def parse_segment(text):
    """Turn `--segments category_norm=kitchen` into the filter it names."""
    if '=' not in text:
        raise ValueError(f'Segment "{text}" must be key=value.')
    key, _, value = text.partition('=')
    allowed = ('category_root_norm', 'category_norm', 'municipality_norm',
               'condition_norm', 'seller_type_norm', 'focus_path')
    if key not in allowed:
        raise ValueError(f'Unknown segment dimension "{key}". Choose one of: {", ".join(allowed)}.')
    return {key: value}


def candidates_for(filters):
    """The desk shortlist at the settings the hosted interface is allowed to ask for.

    `peer_rows` is 0 because the hosted shell never asks for the peer table, and
    exporting it would multiply the payload by an order of magnitude for rows
    nothing reads.
    """
    return {market_name: research_desk.candidates(
        dict(filters or {}, platform='tori', subplatform=market_name),
        limit=200, min_gap_pct=20, horizon=30, peer_rows=0, peer_stats=False)
        for market_name in MARKETS
        if not filters or filters.get('subplatform') in (None, market_name)}


def export(segment_filters=(), segment_limit=64):
    payload = seed_payload(dashboard._snapshot(filters_for('standard')))
    records = {name: envelope(reader)
               for name, reader in (('inventory', inventory.listing), ('drafts', drafts.listing),
                                    ('trades', trades.listing), ('journal', research.journal))}
    records['scenarios'] = {key: value for key, value in scenario_workspace.library().items()
                            if key != 'entries'}
    # `/api/research` returns saved segments beside the journal, so both travel.
    records['segments'] = research.segments()

    segments = {'payloads': {}, 'candidates': {}}
    measured_keys = set()
    for combination in segment_filters:
        key = segment_key(combination)
        if key in measured_keys:
            continue
        if len(measured_keys) >= segment_limit:
            print(f'skipping {combination}: the segment limit of {segment_limit} was reached', file=sys.stderr)
            continue
        measured_keys.add(key)
        # Only the three filter-dependent blocks are re-measured. The rest of the
        # payload is evidence the filter does not select over, so it is the same
        # measurement for every combination and is carried once.
        segments['payloads'][key] = project(scoped(payload, combination))
        segments['candidates'][key] = project(candidates_for(combination))

    return {'version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'payload': project(payload), 'segments': segments, 'records': project(records),
            'candidates': project(candidates_for(None))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--segments', nargs='*', default=[],
                        help='Filter combinations to measure, as key=value.')
    parser.add_argument('--segment-limit', type=int, default=64,
                        help='Maximum combinations to measure beyond the market as a whole.')
    arguments = parser.parse_args()
    combinations = [parse_segment(text) for text in arguments.segments]
    if arguments.segment_limit < 0:
        parser.error('The segment limit cannot be negative.')
    json.dump(export(combinations, arguments.segment_limit), sys.stdout,
              ensure_ascii=False, separators=(',', ':'), default=str)
