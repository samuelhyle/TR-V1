"""Explicit method/path capabilities. Unclassified requests are denied."""

PUBLIC = {
    ('GET', '/login'), ('GET', '/healthz'), ('GET', '/api/auth/me'), ('GET', '/api/auth/session'),
    ('POST', '/api/auth/login'), ('GET', '/api/release'),
}
ACCOUNT = {
    ('POST', '/api/auth/logout'), ('POST', '/api/auth/password'),
}
VIEWER_GET = {
    '/viewer', '/api/viewer/snapshot', '/api/viewer/forecasts', '/api/analysis', '/api/focus', '/api/listings',
    '/api/research/candidates', '/api/research/candidate', '/api/research/listing', '/api/research/provenance',
    '/api/item-workspace', '/api/forecast-evidence', '/api/research/scenarios', '/api/research/sessions',
    '/api/research', '/api/opportunities', '/api/inventory', '/api/drafts', '/api/trades', '/api/outcomes',
    '/api/workflow', '/api/research/proceeds', '/api/compare', '/api/capabilities',
}
DEVELOPER_GET = {
    '/', '/index.html', '/developer', '/events', '/api/snapshot', '/api/product-identity',
    '/api/research/internet', '/api/collection/control', '/api/collection/command', '/api/collection/runs',
    '/api/collection/run', '/api/collection/events', '/api/collection/data', '/api/collection/export',
    '/api/research/source-review', '/api/analyst/evaluation', '/api/analyst', '/api/research/acquisition',
    '/api/research/acquisition-queue', '/api/health', '/api/collection-health', '/api/collection/capacity',
    '/api/permissions', '/api/connections', '/api/trades/resale-preview', '/api/predict', '/api/operations',
    '/api/soldextract', '/api/compliance', '/metrics', '/api/metrics', '/api/auth/users', '/api/auth/activity',
    '/api/readiness',
}
DEVELOPER_POST = {
    '/api/research/internet/action', '/api/collection/start', '/api/collection/preview',
    '/api/collection/settings', '/api/collection/control', '/api/action', '/api/ledger',
    '/api/research', '/api/research/session', '/api/research/scenarios', '/api/opportunities',
    '/api/analyst', '/api/inventory', '/api/connections', '/api/drafts', '/api/outcomes',
    '/api/research/source-review', '/api/trades', '/api/operations', '/api/predict', '/api/soldextract',
    '/api/collection/trim', '/api/auth/users',
}

# Seller access is a business capability, independent of the read-only viewer.
# Operational routes remain developer-only even when called directly.
SELLER_GET = VIEWER_GET | {
    '/seller', '/api/snapshot', '/api/product-identity', '/api/research/source-review',
    '/api/research/acquisition', '/api/research/acquisition-queue', '/api/analyst',
    '/api/analyst/evaluation', '/api/research/internet', '/api/trades/resale-preview',
    '/api/predict', '/api/soldextract', '/api/connections',
}
SELLER_POST = {
    '/api/ledger', '/api/research', '/api/research/session', '/api/research/scenarios',
    '/api/opportunities', '/api/inventory', '/api/drafts', '/api/outcomes',
    '/api/research/source-review', '/api/trades', '/api/soldextract', '/api/connections',
    '/api/predict',
}


def deployment_manifest(reads, writes, deployment, detail=None):
    """Describe what this deployment can actually answer.

    Authorisation (`capability`) and availability are different questions: a
    route can be permitted to a developer and still be refused because the thing
    that computes it is not running. The interface needs the second answer before
    it draws a control, not after it fails, so each deployment reports its own
    truth rather than the browser inferring one from a 404.

    `reads` and `writes` are the paths this process answers. A read-only replay
    of an exported workspace therefore reports reads without writes, which is the
    honest description of what a reader can do.
    """
    return {'version': 1, 'deployment': deployment,
            'reads': sorted(reads), 'writes': sorted(writes),
            'detail': detail or ''}


def all_routes():
    """Every method/path pair the research runtime answers.

    A deployment that owns the runtime has no partial surface, so this is what it
    reports. Imported lazily inside the caller to keep `access` free of a
    dependency on the route tables that declare it.
    """
    from . import dashboard
    return ({path for path in dashboard.GET_ROUTES if path.startswith('/api/')},
            {path for path in dashboard.POST_ROUTES if path.startswith('/api/')})


def seller_request(method, path, payload=None):
    """Check the exact route and action; never delegate the generic CLI dispatcher."""
    method = 'GET' if method == 'HEAD' else method
    if method == 'GET':
        return path in SELLER_GET
    if method != 'POST' or path not in SELLER_POST:
        return False
    if path == '/api/predict':
        return isinstance(payload, dict) and payload.get('action') in {'predict', 'compute'}
    return True


def seller_projection(value):
    """Preserve editable business evidence while excluding credentials and runtime paths."""
    private = {'client_secret', 'access_token', 'refresh_token', 'password_hash', 'token_hash',
               'csrf_token', 'artifact_path', 'model_path', 'database_path', 'input_path',
               'install_command', 'train_command', 'provider_url'}
    if isinstance(value, dict):
        return {key: seller_projection(item) for key, item in value.items() if key not in private}
    if isinstance(value, list):
        return [seller_projection(item) for item in value]
    return value


def capability(method, path):
    method = 'GET' if method == 'HEAD' else method
    pair = (method, path)
    if pair in PUBLIC:
        return 'public'
    if pair in ACCOUNT:
        return 'account'
    if method == 'GET' and path in VIEWER_GET:
        return 'read'
    if method == 'GET' and path in DEVELOPER_GET or method == 'POST' and path in DEVELOPER_POST:
        return 'developer'
    return None


def permits(principal, required):
    if required == 'public':
        return True
    if not principal or principal.get('role') not in {'developer', 'viewer'}:
        return False
    if required == 'account':
        return True
    if principal.get('must_change_password'):
        return False
    return required == 'read' or required == 'developer' and principal['role'] == 'developer'


def viewer_projection(value):
    """Remove action scaffolding and account/runtime bindings from shared readers."""
    private = {'queue', 'outbox', 'can_save', 'can_edit', 'can_publish', 'actions', 'next_actions',
               'capabilities', 'binding', 'account_binding', 'client_token', 'artifact_path', 'model_path',
               'database_path', 'input_path', 'password_hash', 'csrf_token', 'token_hash'}
    if isinstance(value, dict):
        return {key: viewer_projection(item) for key, item in value.items() if key not in private}
    if isinstance(value, list):
        return [viewer_projection(item) for item in value]
    return value
