#!/usr/bin/env python3
"""Deploy the complete UI after authenticated release and worker checks pass."""
import argparse
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from toriresearch import release

ANALYTICAL_READS = {'/api/snapshot', '/api/analysis', '/api/focus', '/api/opportunities',
                    '/api/listings', '/api/item-workspace', '/api/predict',
                    '/api/research/candidates', '/api/forecast-evidence'}


def cli():
    path = ROOT / 'node_modules/netlify-cli/bin/run.js'
    node = shutil.which('node')
    if path.is_file() and node:
        return [node, str(path)]
    raise RuntimeError('Run npm ci to install the pinned Netlify CLI.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _signed_request_once(settings, origin, path, *, payload=None, cookie=None, csrf=None):
    method = 'POST' if payload is not None else 'GET'
    body = json.dumps(payload).encode() if payload is not None else b''
    stamp = str(int(time.time()))
    signature = hmac.new(settings['gateway_key'].encode(),
                         '\n'.join((stamp, method, path, hashlib.sha256(body).hexdigest())).encode(),
                         hashlib.sha256).hexdigest()
    headers = {'X-Tori-Time': stamp, 'X-Tori-Signature': signature, 'X-Tori-Origin': origin}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    if cookie:
        headers['Cookie'] = cookie
    if csrf:
        headers['X-CSRF-Token'] = csrf
    request = urllib.request.Request(settings['backend_url'].rstrip('/') + path,
                                     data=body if payload is not None else None, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            return json.loads(response.read(2 * 1024 * 1024)), response.headers
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Backend verification failed for {path}: HTTP {exc.code}.') from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError(f'Backend verification failed for {path}.') from None


def signed_request(settings, origin, path, *, payload=None, cookie=None, csrf=None):
    deadline = time.monotonic() + 300
    while True:
        value, headers = _signed_request_once(settings, origin, path, payload=payload, cookie=cookie, csrf=csrf)
        if payload is not None or not isinstance(value, dict) or value.get('pending') is not True or not isinstance(value.get('job_id'), str):
            return value, headers
        if time.monotonic() >= deadline:
            raise RuntimeError('Backend analysis did not finish within five minutes: ' + path)
        time.sleep(1)


def preflight(settings, origin):
    backend = urlparse(settings['backend_url'])
    if backend.scheme != 'https' or not backend.hostname or backend.username or backend.password or backend.path not in {'', '/'} or backend.query or backend.fragment:
        raise ValueError('Configure an HTTPS backend origin without embedded credentials.')
    key = settings['gateway_key']
    if len(key) < 48:
        raise ValueError('Use a gateway key of at least 48 characters.')
    account = settings.get('smoke_account')
    if not isinstance(account, dict) or set(account) != {'username', 'password'}:
        raise ValueError('Configure a dedicated developer smoke_account with username and password.')
    from http.cookies import SimpleCookie
    data, headers = signed_request(settings, origin, '/api/auth/login', payload=account)
    cookies = SimpleCookie(headers.get('Set-Cookie', ''))
    if '__Host-tori_session' not in cookies:
        raise RuntimeError('The smoke account login did not establish a session.')
    session = {'cookie': '__Host-tori_session=' + cookies['__Host-tori_session'].value,
               'csrf': data.get('csrf_token')}
    try:
        if data.get('user', {}).get('role') != 'developer' or data['user'].get('must_change_password'):
            raise RuntimeError('The smoke account must be an active developer with a personal password.')
        ready, _ = signed_request(settings, origin, '/api/readiness', **session)
        if ready.get('ready') is not True or not all(ready.get('checks', {}).get(k) is True for k in ('schema', 'workspace', 'workers')):
            raise RuntimeError('The schema, workspace and workers must all be ready.')
        expected = release.describe()
        current = ready.get('release', {})
        if any(current.get(k) != expected[k] for k in ('api_version', 'schema_version', 'source_hash')):
            raise RuntimeError('The frontend and backend must be built from the same source.')
        capabilities, _ = signed_request(settings, origin, '/api/capabilities', **session)
        if capabilities.get('deployment') != 'runtime' or not ANALYTICAL_READS <= set(capabilities.get('reads', [])):
            raise RuntimeError('The backend must offer the complete analytical workspace.')
        read_only = settings.get('read_only', False)
        if capabilities.get('read_only', False) != read_only:
            raise RuntimeError('The backend read-only mode differs from the deployment configuration.')
        if capabilities.get('analytical_jobs', False) != settings.get('analytical_jobs', False):
            raise RuntimeError('The backend analytical worker mode differs from the deployment configuration.')
        if not read_only and '/api/inventory' not in capabilities.get('writes', []):
            raise RuntimeError('The backend must offer the live developer workspace.')
        listings, _ = signed_request(settings, origin, '/api/listings?page_size=1', **session)
        if not isinstance(listings.get('items'), list):
            raise RuntimeError('The authenticated research read returned an unexpected response.')
        for path, key in (('/api/analysis', 'matrix'), ('/api/focus', 'panels'),
                          ('/api/opportunities', 'rows')):
            report, _ = signed_request(settings, origin, path, **session)
            if not isinstance(report.get(key), list):
                raise RuntimeError('The analytical read returned an unexpected response: ' + path)
        from toriresearch.model_workspace import FAMILIES
        for family in FAMILIES:
            report, _ = signed_request(settings, origin, '/api/predict?family=' + family, **session)
            if report.get('scope', {}).get('family') != family or report.get('errors'):
                raise RuntimeError('The stored model results could not be verified: ' + family)
        return current
    finally:
        signed_request(settings, origin, '/api/auth/logout', payload={}, **session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='Private JSON with backend_url and gateway_key')
    parser.add_argument('--prod', action='store_true')
    parser.add_argument('--check', action='store_true', help='Verify the backend without deploying.')
    args = parser.parse_args()
    if args.config.stat().st_mode & 0o077:
        parser.error('Deployment configuration must be private (chmod 600).')
    settings = json.loads(args.config.read_text())
    origin = settings.get('production_origin' if args.prod else 'preview_origin')
    public = urlparse(origin or '')
    if public.scheme != 'https' or not public.hostname or public.username or public.password or public.path not in {'', '/'} or public.query or public.fragment:
        parser.error('Configure an explicit HTTPS production_origin or preview_origin.')
    if not settings.get('site_id'):
        parser.error('Configure the intended Netlify site_id explicitly.')
    verified = preflight(settings, origin)
    if args.check:
        print(json.dumps({'ready': True, 'release': verified}, indent=2))
        return
    from build_netlify import build
    build('full', strict=True)
    command = cli() + ['deploy', '--no-build', '--skip-functions-cache', '--dir', 'dist/netlify', '--functions', 'netlify/functions',
                       '--site', settings['site_id'],
                       '--env', 'TORI_DEPLOY_MODE=full', '--env', 'TORI_PUBLIC_ORIGIN=' + origin,
                       '--env', 'TORI_BACKEND=gateway',
                       '--env', 'TORI_RELEASE_SOURCE_HASH=' + verified['source_hash'],
                       '--env', 'TORI_BACKEND_URL=' + settings['backend_url'],
                       '--secret-env', 'TORI_GATEWAY_KEY=' + settings['gateway_key'], '--json']
    command += ['--prod'] if args.prod else ['--alias', 'full-app-preview']
    # Secret values never enter shell interpolation or output. The CLI masks
    # --secret-env values in Netlify's stored deployment metadata.
    result = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Netlify deployment failed. Review the Netlify deployment logs.')
    value = json.loads(result.stdout)
    print(json.dumps({key: value[key] for key in ('deploy_id', 'deploy_url', 'deploy_ssl_url', 'url') if key in value}, indent=2))


if __name__ == '__main__':
    main()
