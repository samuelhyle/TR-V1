"""Durable, shared results for analytical reads that outlast an edge request."""
import hashlib
import json
import time

from . import auth, cloud_jobs, dashboard, db, metrics, release

ROUTES = {'/api/analysis': '_get_analysis', '/api/focus': '_get_focus',
          '/api/opportunities': '_get_opportunities',
          '/api/research/candidates': '_get_research_candidates'}


def request(path, query, actor):
    if path not in ROUTES:
        raise ValueError('Unsupported analytical read.')
    # Parse bounds before adding a job. Unknown dimensions/filters must not occupy
    # the worker, and equivalent query-string orderings should share one result.
    from . import analysis, research_desk
    filters = dashboard.snapshot_filters(query)
    if path == '/api/analysis':
        if query.get('dimension', ['category_root_norm'])[0] not in analysis.DIMENSIONS:
            raise ValueError('unsupported analysis dimension')
        if query.get('days', ['30'])[0] not in ('7', '30', '90'):
            raise ValueError('unsupported analysis window')
    if path == '/api/opportunities':
        if query.get('days', ['30'])[0] not in ('7', '30', '90'):
            raise ValueError('unsupported arrival window')
        research_desk._bounded_int(query.get('limit', ['100'])[0], 'board limit', 1, 200, 100)
    if path == '/api/research/candidates':
        for name, label, low, high, default in (
                ('min_gap_pct', 'minimum gap', 1, 99, 20),
                ('limit', 'candidate limit', 1, research_desk.MAX_LIMIT, research_desk.DEFAULT_LIMIT),
                ('peer_rows', 'peer rows', 0, research_desk.MAX_PEER_ROWS, 0)):
            research_desk._bounded_int(query.get(name, [str(default)])[0], label, low, high, default)
    research_desk._horizon(query.get('horizon', ['30'])[0])
    signature = hashlib.sha256(json.dumps(dashboard.analysis_freshness_key(), default=str,
                                          sort_keys=True).encode()).hexdigest()
    values = {key: list(value) for key, value in sorted(query.items())}
    payload = {'path': path, 'query': values, 'filters': filters, 'signature': signature,
               'source_hash': release.describe()['source_hash']}
    cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    payload['period'] = int(time.time() // 300)
    job_id = cache_key + '-' + str(payload['period'])
    existing = db.query_one("SELECT * FROM cloud_jobs WHERE kind='analysis' AND job_id LIKE ? ORDER BY created_at DESC LIMIT 1",
                            (cache_key + '-%',))
    if existing and existing['status'] in {'queued', 'running'}:
        return 202, {'pending': True, 'job_id': existing['job_id'], 'status': existing['status'], 'retry_after': 1}
    finished = metrics.parse_ts(existing['finished_at']) if existing else None
    if existing and existing['status'] == 'done' and finished and (auth.now()-finished).total_seconds() < 300:
        return 200, json.loads(existing['result_json'])
    if existing and existing['job_id'] == job_id and existing['status'] in {'failed', 'interrupted', 'cancelled'}:
        # Failed jobs are not silently replayed in a polling loop. A later cache
        # period or a changed dataset creates an explicit new request.
        raise ValueError('The analysis did not finish. Refresh after a few minutes or review the worker activity.')
    job = cloud_jobs.enqueue('analysis', payload, actor, job_id=job_id)
    return 202, {'pending': True, 'job_id': job['job_id'], 'status': job['status'], 'retry_after': 1}


def compute(payload):
    handler = dashboard.Handler.__new__(dashboard.Handler)
    with db.read_snapshot():
        response = getattr(handler, ROUTES[payload['path']])(payload['query'])
    if response[0] != 200:
        raise ValueError('The analytical read could not be completed.')
    return json.loads(response[1])
