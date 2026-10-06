import {getStore} from '@netlify/blobs';
import {createBackend} from '../lib/backend.mjs';
import {createGateway} from '../lib/gateway.mjs';
import {workspaceService} from '../lib/workspace.mjs';
import buildMetadata from '../../dist/netlify/build-manifest.json' with {type: 'json'};

let handler;

/**
 * Choose where the dashboard's reads are answered from.
 *
 * `gateway` forwards every request to a persistent research backend over signed
 * requests, which is the only way to get live numbers. `blobs` serves the same
 * original dashboard from the payload the local runtime exported, so the interface
 * deploys and reads real data with nothing else running. The two are chosen by
 * configuration rather than inferred, so a deployment never silently changes where
 * its numbers come from.
 */
function chosen() {
  const backend = process.env.TORI_BACKEND || 'blobs';
  if (!['blobs', 'gateway'].includes(backend)) {
    throw new Error('TORI_BACKEND must be blobs or gateway.');
  }
  return backend;
}

export default async function backend(request, context) {
  if (chosen() === 'gateway') {
    if (!process.env.TORI_BACKEND_URL || !process.env.TORI_GATEWAY_KEY) {
      return new Response(JSON.stringify({error: 'The workspace is being configured.'}),
        {status: 503, headers: {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}});
    }
    handler ||= createGateway({backend: process.env.TORI_BACKEND_URL, key: process.env.TORI_GATEWAY_KEY,
                               origin: process.env.TORI_PUBLIC_ORIGIN || 'https://tori-research-workspace.netlify.app'});
    return handler(request);
  }
  handler ||= createBackend({
    authStore: getStore({name: 'tori-auth', consistency: 'strong', region: 'eu-central-1'}),
    dataStore: getStore({name: 'tori-research', consistency: 'strong', region: 'eu-central-1'}),
    origin: process.env.TORI_PUBLIC_ORIGIN || 'https://tori-research-workspace.netlify.app',
    release: {...buildMetadata.release, build_id:buildMetadata.build_id},
    // The original dashboard's shell reads these. A deployment that serves the
    // reduced interface never asks for them, so they cost nothing when unused.
    workspace: workspaceService(getStore({name: 'tori-research', consistency: 'strong', region: 'eu-central-1'}))
  });
  return handler(request, context);
}

export const config = {region: 'fra'};
