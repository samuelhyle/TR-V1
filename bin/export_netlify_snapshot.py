#!/usr/bin/env python3
"""Export approved business fields inside the runtime that owns the database.

Example: docker exec -i toriresearch-app-1 python - < bin/export_netlify_snapshot.py > data/netlify/snapshot.json
Then: gzip -k data/netlify/snapshot.json
The output is private data, never a static-site build input.
"""
import json
import sys
from datetime import datetime, timezone

from toriresearch import db, drafts, inventory, research, research_desk, scenario_workspace, trades


PRIVATE_KEYS = {'password_hash', 'csrf_token', 'token_hash', 'client_token', 'actions',
                'next_actions', 'binding', 'account_binding', 'capabilities', 'queue',
                'outbox', 'can_edit', 'can_save', 'can_publish', 'input_path',
                'model_path', 'artifact_path', 'database_path'}


def project(value):
    if isinstance(value, dict):
        return {k: project(v) for k, v in value.items() if k not in PRIVATE_KEYS}
    if isinstance(value, (tuple, list)):
        return [project(v) for v in value]
    return value


def all_pages(reader):
    first = reader(page=1)
    return first.get('items', []) + [row for page in range(2, first.get('pages', 1) + 1)
                                   for row in reader(page=page).get('items', [])]


def export():
    with db.read_snapshot():
        columns = ('listing_pk, platform, subplatform, listing_id, title, category_norm, '
                   'category_root_norm, municipality_norm, condition_norm, condition_label_raw, '
                   'seller_type_norm, listed_price_eur, is_giveaway, is_reserved, status_norm, '
                   'outcome_status, first_seen_at, last_seen_at, last_probed_at, price_observed_at')
        listings = [dict(row) for row in db.query('SELECT ' + columns + ' FROM listings ORDER BY last_seen_at DESC, listing_pk DESC')]
        reports = {market: research_desk.candidates({'platform': 'tori', 'subplatform': market}, limit=100)
                   for market in ('standard', 'diili')}
        return project({'version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
                        'listings': listings, 'research': reports,
                        'inventory': all_pages(inventory.listing),
                        'drafts': all_pages(drafts.listing), 'orders': all_pages(trades.listing),
                        'journal': all_pages(research.journal),
                        'scenarios': scenario_workspace.library().get('entries', [])})


if __name__ == '__main__':
    json.dump(export(), sys.stdout, ensure_ascii=False, separators=(',', ':'), default=str)
