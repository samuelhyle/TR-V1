"""Production WSGI adapter for the existing Python application.

Only the Netlify gateway can reach application endpoints. The database holds one
organisation workspace per deployment. Cloud memberships are separate from the
local read-only role, so this migration never widens local viewer permissions.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.client import HTTPMessage
from types import SimpleNamespace
from urllib.parse import parse_qs

from . import access, auth, cloud_jobs, config, dashboard, db, release

log = logging.getLogger(__name__)
COOKIE = '__Host-tori_session'
LIMIT = 4 * 1024 * 1024


def allowed_origins():
    return {origin for origin in (os.environ.get('TORI_PUBLIC_ORIGIN', ''),
                                  *os.environ.get('TORI_PREVIEW_ORIGINS', '').split(',')) if origin}


def membership(principal):
    row = db.query_one('SELECT * FROM cloud_memberships WHERE user_id=?', (principal['user_id'],))
    if not row or row['workspace_id'] != os.environ.get('TORI_WORKSPACE_ID', 'default'):
        raise auth.Denied('This account has no access to this workspace.')
    return dict(principal, role=row['role'], workspace_id=row['workspace_id'])


def public_user(principal):
    return dict(auth.public_user(principal), workspace_path={
        'developer': '/developer', 'seller': '/seller', 'viewer': '/reader'}[principal['role']])


class CloudHandler(dashboard.Handler):
    def _local_host(self):
        return True  # The WSGI adapter has already verified the signed gateway request.

    def _same_origin(self):
        return self.headers.get('X-Tori-Origin') in allowed_origins()

    def _session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            return cookie[COOKIE].value if COOKIE in cookie else None
        except Exception:
            return None

    def _cookie(self, token='', clear=False):
        return f'{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={0 if clear else auth.SESSION_SECONDS}'

    def _send(self, code, body, ctype='application/json', extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.response = (code, body, ctype, extra or {})

    def authorize(self, method, path, payload):
        self.principal = None
        required = access.capability(method, path)
        if required == 'public':
            return
        self.principal = membership(auth.authenticate(self._session_token()))
        role = self.principal['role']
        allowed = required == 'account' or (not self.principal['must_change_password'] and (
            role == 'developer' and required is not None
            or role == 'seller' and access.seller_request(method, path, payload)
            or role == 'viewer' and required == 'read'))
        if not allowed:
            auth.record_denial(self.principal, method, path)
            raise auth.Denied('This action is unavailable for your account.')
        if method == 'POST' and not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), self.principal['csrf_token']):
            raise auth.Denied('Refresh your session before submitting this request.')
        if (method == 'POST' and os.environ.get('TORI_CLOUD_READ_ONLY') == '1'
                and (method, path) not in access.ACCOUNT):
            raise auth.Denied('This deployment is read-only while the migrated analysis is being verified.')

    def _get_me(self, query):
        principal = membership(auth.authenticate(self._session_token()))
        return 200, json.dumps({'user': public_user(principal), 'csrf_token': principal['csrf_token']}), None

    def _get_auth_session(self, query):
        try:
            principal = membership(auth.authenticate(self._session_token()))
        except auth.Denied:
            return 200, json.dumps({'user': None}), None
        return 200, json.dumps({'user': public_user(principal), 'csrf_token': principal['csrf_token']}), None

    def _get_capabilities(self, query):
        reads, writes = access.all_routes()
        reads.add('/api/readiness')
        role = self.principal['role']
        if role == 'viewer':
            reads &= access.VIEWER_GET
            writes = {path for method, path in access.ACCOUNT if method == 'POST'}
        elif role == 'seller':
            reads &= access.SELLER_GET
            writes &= access.SELLER_POST
            writes |= {path for method, path in access.ACCOUNT if method == 'POST'}
        if self.principal['must_change_password']:
            reads = {'/api/auth/me', '/api/auth/session'}
            writes = {'/api/auth/password', '/api/auth/logout'}
        read_only = os.environ.get('TORI_CLOUD_READ_ONLY') == '1'
        if read_only:
            writes &= {path for method, path in access.ACCOUNT if method == 'POST'}
        report = access.deployment_manifest(reads, writes, 'runtime')
        report.update(role=role, account_roles=['developer', 'seller', 'viewer'], release=release.describe(),
                      read_only=read_only,
                      analytical_jobs=os.environ.get('TORI_CLOUD_ANALYTICAL_JOBS') == '1',
                      detail='Live analytical reads; business writes and collection are disabled during verification.' if read_only else report['detail'])
        return 200, json.dumps(report), None

    def _get_readiness(self, query):
        connection = db.connect()
        try:
            version = db.recorded_version(connection)
            developers = connection.execute(
                "SELECT COUNT(*) FROM cloud_memberships m JOIN auth_users u ON u.user_id=m.user_id "
                "WHERE m.workspace_id=? AND m.role='developer' AND u.active=TRUE",
                (self.principal['workspace_id'],)).fetchone()[0]
        finally:
            connection.close()
        try:
            runtime = json.loads((config.DATA_DIR / 'runtime-heartbeat.json').read_text())
            workers = time.time() - runtime['updated_at'] < 15 and runtime['workers_ready'] is True
        except (OSError, ValueError, KeyError, TypeError):
            workers = False
        checks = {'schema': version == release.describe()['schema_version'],
                  'workspace': developers > 0, 'workers': workers}
        ready = all(checks.values())
        return (200 if ready else 503), json.dumps({'ready': ready, 'checks': checks,
                                                  'release': release.describe()}), None

    def _post_login(self, payload):
        result = super()._post_login(payload)
        if result[0] != 200:
            return result
        data = json.loads(result[1])
        try:
            data['user'] = public_user(membership(data['user']))
        except auth.Denied:
            # A local-only account must never obtain a cloud session.
            token = SimpleCookie(result[3]['Set-Cookie'])[COOKIE].value
            auth.logout(auth.authenticate(token))
            raise
        return result[0], json.dumps(data), *result[2:]

    def _get_users(self, query):
        users = []
        for user in auth.list_users():
            try:
                users.append(auth.public_user(membership(user)))
            except auth.Denied:
                pass
        return 200, json.dumps({'users': users}), None

    def _post_users(self, payload):
        if set(payload) != {'action', 'params'} or not isinstance(payload['params'], dict):
            raise ValueError('Account action and parameters are required.')
        params, action = dict(payload['params']), payload['action']
        role = params.get('role')
        if role is not None and role not in {'developer', 'seller', 'viewer'}:
            raise ValueError('Choose developer, seller or viewer.')
        if action == 'create' and set(params) <= {'username', 'password', 'role'} and {'username', 'password'} <= set(params):
            role = role or 'seller'
            user = auth.create_user(params['username'], params['password'], 'developer' if role == 'developer' else 'viewer', self.principal['user_id'])
            db.execute('INSERT INTO cloud_memberships (user_id,workspace_id,role) VALUES (?,?,?)',
                       (user['user_id'], self.principal['workspace_id'], role))
        elif action == 'update' and set(params) <= {'user_id', 'role', 'active', 'password'} and 'user_id' in params:
            target = membership({'user_id': params['user_id']})
            converted = {k: v for k, v in params.items() if k != 'user_id'}
            if role:
                converted['role'] = 'developer' if role == 'developer' else 'viewer'
            auth.update_user(params['user_id'], self.principal['user_id'], **converted)
            if role:
                db.execute('UPDATE cloud_memberships SET role=? WHERE user_id=? AND workspace_id=?',
                           (role, params['user_id'], target['workspace_id']))
        else:
            raise ValueError('Unsupported account action or fields.')
        return self._get_users({})

    def _get_snapshot(self, query):
        if self.principal['role'] == 'developer':
            result = json.loads(super()._get_snapshot(query)[1])
            active = [dict(job, started_at=job['started_at'] or job['created_at'])
                      for job in cloud_jobs.jobs(kind='operation') if job['status'] in {'queued', 'running'}]
            result['health']['running_actions'].extend(active)
            result['health']['collector_running'] = result['health']['collector_running'] or bool(active)
            return 200, json.dumps(result, default=str, allow_nan=False), None
        from . import aggregates, collection, metrics, purchases
        filters = dashboard.snapshot_filters(query)
        with db.read_snapshot():
            # Keep the UI response shape, but expose only business evidence.
            result = dict(generated_at=auth.now().isoformat(), filters=filters,
                          panels=dashboard.panels(filters), segments=dashboard.segment_options(),
                          liquidity=metrics.liquidity(**filters), economics=purchases.summary(),
                          buys=purchases.list_purchases(), inventory=purchases.realized_economics(),
                          quality=dashboard.data_quality(), history=aggregates.report(),
                          collection=collection.coverage(), coverage=dashboard.sweep.coverage(),
                          health={'observations': db.scalar('SELECT COUNT(*) FROM listings'), 'running_actions': []},
                          compliance={}, permissions={}, enrichment={}, parser_health={}, posture={}, recent_actions=[])
        return 200, json.dumps(access.seller_projection(result), default=str, allow_nan=False), None

    def _get_predict(self, query):
        cloud_jobs.sync_models()
        result = json.loads(super()._get_predict(query)[1])
        jobs = cloud_jobs.jobs(None if self.principal['role'] == 'developer' else self.principal['user_id'], 'model')
        represented = {job['native_job_id'] for job in jobs if job['native_job_id']}
        retained = [job for job in result['jobs'] if job['job_id'] not in represented]
        result['jobs'] = sorted(jobs + retained, key=lambda job: job.get('started_at') or job.get('created_at') or '', reverse=True)[:12]
        if self.principal['role'] == 'seller':
            result = access.seller_projection(result)
        return 200, json.dumps(result, default=str, allow_nan=False), None

    def _post_predict(self, payload):
        from . import model_workspace
        if payload.get('action') == 'cancel':
            if set(payload) != {'action', 'job_id'}:
                raise ValueError('A job identifier is required for cancellation.')
            actor = None if self.principal['role'] == 'developer' else self.principal['user_id']
            return 200, json.dumps(cloud_jobs.cancel(payload['job_id'], actor)), None
        family, action = payload.get('family'), payload.get('action')
        allowed = {'train', 'predict', 'preview', 'evaluate'} if family in model_workspace.FAMILIES[:2] else {'compute'}
        if family not in model_workspace.FAMILIES or action not in allowed or set(payload)-{'family','action','scope','window_days'}:
            raise ValueError('Unsupported model request.')
        selected = model_workspace.scope(dict(payload.get('scope') or {}, family=family))
        if selected['version']:
            raise ValueError('Clear the historical version selection before starting a job.')
        job = cloud_jobs.enqueue('model', payload, self.principal['user_id'])
        return 202, json.dumps({'ok': True, 'job': job}), None

    def _post_action(self, payload):
        # The production dispatcher never starts an untracked web subprocess.
        action = payload.get('action')
        if set(payload) - {'action', 'params'}:
            raise ValueError('Unsupported operation fields.')
        prepared = dashboard.validate_action(action, payload.get('params'))
        if not prepared['ok']:
            raise ValueError(prepared['error'])
        job = cloud_jobs.enqueue('operation', {'action': action, 'params': prepared['params']}, self.principal['user_id'])
        return 202, json.dumps({'ok': True, 'action': action, 'job': job}), None


def application(environ, start_response):
    method, path = environ.get('REQUEST_METHOD', ''), environ.get('PATH_INFO', '')
    def respond(code, body, ctype='application/json', extra=None):
        if isinstance(body, str):
            body = body.encode()
        headers = [('Content-Type', ctype), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'),
                   ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer')]
        headers.extend((extra or {}).items())
        start_response(f'{code} {HTTPStatus(code).phrase}', headers)
        return [b'' if method == 'HEAD' else body]
    try:
        # Render health probes receive no data and cannot perform actions.
        if path == '/healthz' and method in {'GET', 'HEAD'}:
            db.query_one('SELECT meta_value FROM schema_meta LIMIT 1')
            return respond(200, '{"status":"ok"}')
        if method not in {'GET', 'HEAD', 'POST'} or not path.startswith('/api/'):
            return respond(404, '{"error":"Endpoint unavailable."}')
        length = int(environ.get('CONTENT_LENGTH') or 0)
        limit = min(LIMIT, dashboard.POST_BODY_LIMITS.get(path, dashboard.DEFAULT_BODY_LIMIT))
        if not 0 <= length <= limit:
            return respond(413, '{"error":"Request too large."}')
        body = environ['wsgi.input'].read(length)
        if len(body) != length:
            return respond(400, '{"error":"Incomplete request."}')
        route = path + ('?' + environ['QUERY_STRING'] if environ.get('QUERY_STRING') else '')
        stamp = environ.get('HTTP_X_TORI_TIME', '')
        key, origin = os.environ.get('TORI_GATEWAY_KEY', ''), os.environ.get('TORI_PUBLIC_ORIGIN', '')
        valid_time = stamp.isdigit() and abs(time.time()-int(stamp)) <= 60
        expected = hmac.new(key.encode(), '\n'.join((stamp, method, route, hashlib.sha256(body).hexdigest())).encode(), hashlib.sha256).hexdigest()
        if len(key)<48 or not origin or not valid_time or environ.get('HTTP_X_TORI_ORIGIN') not in allowed_origins() or not hmac.compare_digest(expected, environ.get('HTTP_X_TORI_SIGNATURE','')):
            return respond(403, '{"error":"Gateway authentication required."}')
        headers = HTTPMessage()
        for name, value in environ.items():
            if name.startswith('HTTP_'):
                headers[name[5:].replace('_', '-')] = value
        headers['Content-Type'] = environ.get('CONTENT_TYPE', '')
        handler = CloudHandler.__new__(CloudHandler)
        handler.headers, handler.command, handler.path = headers, method, route
        handler.server = SimpleNamespace(server_port=0)
        handler.close_connection, handler.response = True, None
        normalized = 'GET' if method == 'HEAD' else method
        routes = dashboard.POST_ROUTES if normalized == 'POST' else dashboard.GET_ROUTES
        route_handler = CloudHandler._get_readiness if path == '/api/readiness' and normalized == 'GET' else routes.get(path)
        if route_handler is None:
            return respond(404, '{"error":"Endpoint unavailable."}')
        payload = {}
        if normalized == 'POST':
            if environ.get('CONTENT_TYPE','').split(';')[0]!='application/json':
                return respond(415, '{"error":"Send application/json."}')
            payload = json.loads(body or b'{}')
            if not isinstance(payload, dict):
                raise ValueError('A JSON object is required.')
        handler.authorize(normalized, path, payload)
        values = payload if normalized == 'POST' else parse_qs(environ.get('QUERY_STRING',''))
        from . import cloud_analysis
        if (normalized == 'GET' and path in cloud_analysis.ROUTES
                and os.environ.get('TORI_CLOUD_ANALYTICAL_JOBS') == '1'):
            code, value = cloud_analysis.request(path, values, handler.principal['user_id'])
            result = code, json.dumps(value, default=str, allow_nan=False), None
        elif normalized == 'GET' and handler.principal and handler.principal['role'] == 'viewer':
            with db.read_snapshot():
                result = getattr(handler, route_handler.__name__)(values)
        else:
            result = getattr(handler, route_handler.__name__)(values)
        if result is None:
            result = handler.response
        else:
            code, output, *extra = result
            if extra and isinstance(extra[0], str):
                ctype = extra.pop(0)
            else:
                if extra and extra[0] is None:
                    extra.pop(0)
                ctype = 'application/json'
            result = (code, output, ctype, *extra)
        code, output, ctype, *extra = result
        if handler.principal and handler.principal['role'] in {'seller','viewer'} and ctype.startswith('application/json'):
            project = access.seller_projection if handler.principal['role']=='seller' else access.viewer_projection
            output = json.dumps(project(json.loads(output)), default=str, allow_nan=False)
        if handler.principal and normalized == 'POST' and code < 400:
            conn = db.connect()
            try:
                with conn.transaction():
                    auth._event(conn, 'business:' + path, handler.principal['user_id'])
            finally:
                conn.close()
        return respond(code, output, ctype, extra[0] if extra else None)
    except auth.Denied as exc:
        return respond(exc.status, json.dumps({'error':str(exc)}))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return respond(400, json.dumps({'error': str(exc)[:300]}))
    except Exception:
        log.exception('Cloud endpoint failed: %s %s', method, path)
        return respond(503, '{"error":"The workspace is temporarily unavailable. Try again shortly."}')
