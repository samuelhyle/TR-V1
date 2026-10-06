import {createHash, createHmac} from 'node:crypto';

const LIMIT = 4 * 1024 * 1024;
/**
 * How long the backend may take to send its response *headers*.
 *
 * A whole-request timeout is the wrong shape for a proxy. The fetch the runtime uses
 * keeps an abort signal armed for the entire life of the response body, not only
 * until the headers arrive, so one timeout value silently becomes "the response is
 * cut off here" rather than "the backend took too long to start answering". Anything
 * long-lived -- a progress stream, a slow report -- would be truncated at exactly
 * this point, with a 200 and its headers already sent, so the client would see a
 * connection that ends mid-body rather than an error it could act on.
 *
 * The budget therefore bounds the wait for the response to begin, and nothing bounds
 * how long the body may take beyond that. A caller who needs a deadline for the whole
 * exchange applies one itself, where a clean status is still possible.
 */
const HEADER_TIMEOUT_MS = 20000;

export function createGateway({backend, key, origin, fetchImpl=fetch, now=()=>Date.now(), headerTimeoutMs=HEADER_TIMEOUT_MS}) {
  const target = new URL(backend);
  if(target.protocol!=='https:' || target.username || target.password || target.search || target.hash || target.pathname!=='/')throw new Error('An HTTPS backend origin is required.');
  if(!key || key.length<48)throw new Error('A strong gateway key is required.');
  return async request=>{
    const respond=(status,error)=>new Response(JSON.stringify({error}),{status,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
    const url=new URL(request.url),path=url.pathname.replace(/^\/\.netlify\/functions\/backend(?=\/)/,'');
    if(!['GET','HEAD','POST'].includes(request.method))return respond(405,'Method unavailable.');
    if((request.headers.get('origin') && request.headers.get('origin')!==origin)||request.headers.get('sec-fetch-site')==='cross-site')return respond(403,'Same-origin requests required.');
    if(!path.startsWith('/api/') && path!=='/healthz')return respond(404,'Endpoint unavailable.');
    if(Number(request.headers.get('content-length'))>LIMIT)return respond(413,'Request too large.');
    const parts=[];let size=0;const reader=request.body?.getReader();
    if(reader){try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>LIMIT)return respond(413,'Request too large.');parts.push(value);}}finally{await reader.cancel();}}
    const bytes=Buffer.concat(parts),route=path+url.search,timestamp=String(Math.floor(now()/1000));
    const hash=createHash('sha256').update(bytes).digest('hex');
    const signature=createHmac('sha256',key).update([timestamp,request.method,route,hash].join('\n')).digest('hex');
    const headers=new Headers({'X-Tori-Time':timestamp,'X-Tori-Signature':signature,'X-Tori-Origin':origin});
    for(const name of ['cookie','content-type','x-csrf-token'])if(request.headers.has(name))headers.set(name,request.headers.get(name));
    // The deadline is armed on a controller rather than an `AbortSignal.timeout`,
    // because the timer has to be *cleared* once the response begins. A signal from
    // `AbortSignal.timeout` cannot be disarmed -- it has no `abort` method to call and
    // no handle to its timer -- so it would stay armed for the life of the body and
    // truncate the response at exactly this many milliseconds.
    const controller=new AbortController();
    const deadline=setTimeout(()=>controller.abort(),headerTimeoutMs);
    try {
      const upstream=await fetchImpl(new URL(route,target),{method:request.method,headers,body:request.method==='POST'?bytes:undefined,redirect:'manual',signal:controller.signal});
      if(upstream.status>=300 && upstream.status<400)return respond(502,'Unexpected backend response.');
      // The backend has begun answering. Clear the deadline so the body is not cut
      // short, and hand it on by reference so it streams rather than being buffered.
      clearTimeout(deadline);
      const out=new Headers({'Cache-Control':'no-store','CDN-Cache-Control':'no-store','Netlify-CDN-Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'});
      for(const name of ['content-type','set-cookie','retry-after'])if(upstream.headers.has(name))out.set(name,upstream.headers.get(name));
      return new Response(request.method==='HEAD'?null:upstream.body,{status:upstream.status,headers:out});
    } catch {return respond(503,'The workspace is temporarily unavailable. Try again shortly.');}
    finally {clearTimeout(deadline);}
  };
}
