/**
 * The dashboard's only network layer.
 *
 * Every read and write in the UI goes through `call`. Concentrating it here is
 * what makes two properties enforceable rather than aspirational: that a
 * failure always produces the same operator-readable message, and that the
 * page cannot accidentally issue the same read many times over.
 *
 * No third-party dependency. `fetch` is resolved at call time so tests can
 * supply their own.
 */

/** How long a single request may take before the operator is told to refresh. */
const DEFAULT_TIMEOUT_MS = 45000;
let csrfToken = '';
export function setSession(token) { csrfToken = token || ''; }

/**
 * Whether two options describe a shareable read.
 *
 * Only a plain GET qualifies. A caller-supplied `signal` disqualifies it
 * because that caller may abort to supersede its own earlier request, and a
 * shared request must not die when one of several waiters walks away. A body
 * disqualifies it for the obvious reason.
 */
const isShareableRead = options =>
  (options.method ?? 'GET') === 'GET' && !options.body && !options.signal;

/**
 * Build an API client bound to a `fetch` implementation.
 *
 * A factory rather than module-level state so each test gets an isolated
 * in-flight table; the page uses the single default export.
 */
export function createApi({ fetch: fetchImpl, timeoutMs = DEFAULT_TIMEOUT_MS,
                            jobTimeoutMs = 300000, retryDelayMs = 1000 } = {}) {
  /** In-flight shareable reads, keyed by URL. */
  const inFlight = new Map();

  const doFetch = (...args) => (fetchImpl ?? globalThis.fetch)(...args);

  /** Perform the actual request. Shared by coalesced and unshared callers alike. */
  async function requestOnce(url, options) {
    const controller = new AbortController();
    const parent = options.signal;
    const cancel = () => controller.abort();
    if (parent?.aborted) cancel();
    else parent?.addEventListener('abort', cancel, { once: true });

    const timer = setTimeout(cancel, timeoutMs);
    try {
      const response = await doFetch(url, {
        ...options,
        signal: controller.signal,
        headers: { Accept: 'application/json',
          ...((options.method || 'GET') !== 'GET' && csrfToken ? {'X-CSRF-Token':csrfToken} : {}),
          ...(options.headers || {}) },
      });

      let result;
      try {
        result = await response.json();
      } catch {
        throw new Error('The local server returned an unreadable response. Try refreshing.');
      }

      // A JSON `null` body parses successfully and is not an error, so the
      // envelope check has to tolerate it. Reading `.ok` off `null` would throw
      // and be reported as an unreachable server, which is a different fault
      // entirely and would send the operator looking in the wrong place.
      const envelopeFailed = result != null && typeof result === 'object' && result.ok === false;

      if (!response.ok || envelopeFailed) {
        const error = new Error(result?.error || `The request failed (${response.status}).`);
        error.status = response.status;
        if (response.status === 401 && !url.startsWith('/api/auth/') && globalThis.location?.pathname !== '/login') {
          setSession(''); globalThis.location?.replace('/login');
        }
        throw error;
      }
      return result;
    } catch (error) {
      if (error.name === 'AbortError' && !parent?.aborted) {
        throw new Error('The workspace took too long to respond. Try refreshing.');
      }
      // A TypeError here is the browser's signal that the request never reached
      // the server at all, as opposed to a response we disliked.
      if (error instanceof TypeError) {
        throw new Error('Cannot reach the workspace. Check your connection and try refreshing.');
      }
      throw error;
    } finally {
      clearTimeout(timer);
      parent?.removeEventListener('abort', cancel);
    }
  }

  async function request(url, options) {
    const deadline=Date.now()+jobTimeoutMs;
    while(true) {
      const result=await requestOnce(url,options);
      if((options.method||'GET')!=='GET'||result?.pending!==true||typeof result.job_id!=='string')return result;
      if(Date.now()>=deadline)throw new Error('The analysis is still running. Refresh to check its progress.');
      await new Promise((resolve,reject)=>{
        const signal=options.signal;
        const cancelled=()=>{clearTimeout(timer);signal?.removeEventListener('abort',cancelled);reject(new globalThis.DOMException('Request cancelled.','AbortError'));};
        const timer=setTimeout(()=>{signal?.removeEventListener('abort',cancelled);resolve();},retryDelayMs);
        if(signal?.aborted)cancelled();else signal?.addEventListener('abort',cancelled,{once:true});
      });
    }
  }

  /**
   * Issue a request, reusing an identical in-flight read where possible.
   *
   * Fifteen views each render their own panels and several of them need the same
   * endpoint, so a single refresh can otherwise fire the same query a dozen
   * times against the same local database. Sharing the promise collapses that to
   * one. Responses are deliberately *not* cached: the server already owns
   * freshness, and a client-side cache would silently contradict it.
   */
  function call(url, options = {}) {
    if (!isShareableRead(options)) return request(url, options);

    const existing = inFlight.get(url);
    if (existing) return existing;

    const pending = request(url, options).finally(() => {
      // Only clear the slot if it is still ours; a later request may have
      // replaced it if this one settled out of order.
      if (inFlight.get(url) === pending) inFlight.delete(url);
    });
    inFlight.set(url, pending);
    return pending;
  }

  return { call, inFlight, timeoutMs };
}

const api = createApi();

/**
 * The page's client.
 *
 * Declared as a function rather than `const call = api.call` so the binding
 * stays reassignable: the behavioural tests intercept `call` to observe requests
 * without a network. A `const` would make that impossible and push the tests
 * toward stubbing `fetch` globally instead, which is both broader and less
 * precise.
 */
export function call(url, options) {
  return api.call(url, options);
}

/** Diagnostics for the health panel; not used for control flow. */
export const inFlightCount = () => api.inFlight.size;
