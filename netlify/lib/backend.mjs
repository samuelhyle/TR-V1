import {Denied, authenticate, csrfMatches, login, logout, cookie, changePassword, manageUser, publicUser, requireDeveloper} from './auth.mjs';
import {dataService, MAX_UPLOAD, publishSnapshot} from './data.mjs';
import {NEEDS_RUNTIME} from './workspace.mjs';

async function bodyBytes(request, limit) {
  if(Number(request.headers.get('content-length'))>limit)throw new Denied(413,'Request body is too large.');
  const reader=request.body?.getReader();if(!reader)return Buffer.alloc(0);
  const chunks=[];let length=0;
  try {while(true) {const {done,value}=await reader.read();if(done)break;length+=value.byteLength;if(length>limit)throw new Denied(413,'Request body is too large.');chunks.push(Buffer.from(value));}}
  finally {await reader.cancel();}
  return Buffer.concat(chunks);
}
async function bodyJson(request) {
  if(request.headers.get('content-type')?.split(';')[0]!=='application/json')throw new Denied(415,'Send application/json.');
  const bytes=await bodyBytes(request,32768);
  try {const value=JSON.parse(bytes.toString());if(!value||Array.isArray(value)||typeof value!=='object')throw new Error();return value;}
  catch {throw new Denied(400,'Invalid JSON request.');}
}

/** Every read and write this deployment answers, before a control is drawn. */
const READS = ['/api/cloud/overview','/api/cloud/listings','/api/cloud/research','/api/cloud/item',
               '/api/cloud/records','/api/cloud/status','/api/auth/me','/api/auth/session',
               '/api/auth/users','/api/auth/activity'];
const WRITES = ['/api/auth/login','/api/auth/logout','/api/auth/password','/api/auth/users',
                '/api/cloud/snapshot'];

/** The reads a read-only role may see. The whole research picture stays developer-only. */
const VIEWER_READS = ['/api/cloud/overview','/api/cloud/listings','/api/cloud/research',
                      '/api/cloud/item','/api/cloud/records'];

export function createBackend({authStore,dataStore,origin,workspace=null,release=null}) {
  const data=dataService(dataStore);
  return async (request,context={})=>{
    const respond=(status,value,extra={})=>new Response(request.method==='HEAD'?null:JSON.stringify(value),{status,headers:{'Content-Type':'application/json','Cache-Control':'no-store','CDN-Cache-Control':'no-store','Netlify-CDN-Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',...extra}});
    try {
      const url=new URL(request.url),path=url.pathname.replace(/^\/\.netlify\/functions\/backend(?=\/)/,'');
      const method=request.method==='HEAD'?'GET':request.method;
      if(!['GET','POST'].includes(method))throw new Denied(405,'Method not supported.');
      const suppliedOrigin=request.headers.get('origin');
      if((suppliedOrigin&&suppliedOrigin!==origin)||request.headers.get('sec-fetch-site')==='cross-site')throw new Denied(403,'Same-origin requests required.');
      if(path==='/healthz'&&method==='GET')return respond(200,{status:'ok'});
      if(path==='/api/release'&&method==='GET')return respond(200,release||{api_version:1,source_hash:null,revision:'unknown'});
      if(path==='/api/auth/login'&&method==='POST') {
        const body=await bodyJson(request),result=await login(authStore,body.username,body.password,context.ip||'unknown');
        return respond(200,{user:result.user,csrf_token:result.csrf_token},{'Set-Cookie':cookie(result.secret)});
      }
      if(path==='/api/auth/session'&&method==='GET') {
        // Never 401: the login page probes this to skip the form when a session
        // already exists, and a browser logs every 4xx at network level. Only the
        // caller's own session is ever described. `/api/auth/me` keeps its 401 so
        // a revoked session stays distinguishable to API clients.
        let principal=null;
        try {principal=await authenticate(authStore,request);} catch {}
        return respond(200,principal?{user:principal.user,csrf_token:principal.csrf_token}:{user:null});
      }
      const principal=await authenticate(authStore,request);
      if(method==='POST'&&!csrfMatches(request.headers.get('x-csrf-token'),principal.token_hash))throw new Denied(403,'Refresh the page before trying again.');
      const accountPath=['/api/auth/me','/api/auth/logout','/api/auth/password'].includes(path);
      if(principal.user.must_change_password&&!accountPath)throw new Denied(403,'Change your temporary password before opening the workspace.');
      if(path==='/api/auth/me'&&method==='GET')return respond(200,{user:principal.user,csrf_token:principal.csrf_token});
      if(path==='/api/auth/logout'&&method==='POST'){await logout(authStore,principal);return respond(200,{ok:true},{'Set-Cookie':cookie('',true)});}
      if(path==='/api/auth/password'&&method==='POST'){const body=await bodyJson(request);await changePassword(authStore,principal,body.current_password,body.new_password);return respond(200,{ok:true},{'Set-Cookie':cookie('',true)});}

      // What this deployment answers. Answered before the role split below so the
      // shell can hide a control it could never use, rather than discovering the
      // absence from a 404 after it has already drawn the button.
      if(path==='/api/capabilities'&&method==='GET') {
        const {reads}=workspace?workspace.manifest():{reads:[]};
        const full=[...new Set([...reads,...READS])].sort();
        const developer=principal.user.role==='developer';
        return respond(200,{version:1,deployment:workspace?'replay':'reduced',
          reads:(developer?full:VIEWER_READS).sort(),
          writes:(developer?WRITES:WRITES.filter(path=>path==='/api/auth/logout'||path==='/api/auth/password')).sort(),
          role:principal.user.role, account_roles:['developer','viewer'], release,
          detail:workspace
            ? 'This workspace replays the research the local runtime last measured. It never collects, trades, runs a model or trains one.'
            : 'This workspace serves the reduced read-only view.'});
      }
      if(path==='/api/auth/users'||path==='/api/auth/activity') {
        requireDeveloper(principal);
        if(method==='GET') {
          const state=await authStore.get('state',{type:'json'});
          return respond(200,path.endsWith('users')?{users:state.users.map(publicUser)}:{events:state.events});
        }
        if(path.endsWith('users'))return respond(200,await manageUser(authStore,principal,await bodyJson(request)));
      }
      if(method==='POST') {
        requireDeveloper(principal);
        if(path==='/api/cloud/snapshot') {
          if(request.headers.get('content-type')?.split(';')[0]!=='application/gzip')throw new Denied(415,'Upload application/gzip.');
          const bytes=await bodyBytes(request,MAX_UPLOAD);
          requireDeveloper(await authenticate(authStore,request));
          return respond(200,{snapshot:await publishSnapshot(dataStore,bytes,principal.user.user_id)});
        }
        // One refusal, one sentence. A write this deployment cannot perform has a
        // single cause -- the runtime that owns the database is not running here --
        // so every route answers with that rather than with a per-route message
        // that would read as a different fault each time.
        throw new Denied(404,NEEDS_RUNTIME);
      }
      if(['/api/cloud/overview','/api/cloud/listings','/api/cloud/research','/api/cloud/item','/api/cloud/records'].includes(path))return respond(200,await data.query(path,url.searchParams));
      if(path==='/api/cloud/status'){requireDeveloper(principal);return respond(200,await data.query(path,url.searchParams));}
      // The original dashboard's reads, answered from the exported payload. They
      // describe the whole research picture rather than a shared slice, so they stay
      // behind the developer role; sellers keep the reduced read-only interface.
      if(workspace&&workspace.serves(path)){requireDeveloper(principal);return respond(200,await workspace.query(path,url.searchParams));}
      // A workspace is configured, so an unserved route is a read the local research
      // runtime owns. Saying that is more useful than the generic refusal below.
      if(workspace)throw new Denied(404,NEEDS_RUNTIME);
      throw new Denied(404,'This endpoint is unavailable in the hosted basic workspace.');
    } catch(error) {
      if(error instanceof Denied)return respond(error.status,{error:error.message},error.status===429?{'Retry-After':'900'}:{});
      console.error('Workspace request failed:',error.name);
      return respond(503,{error:'The workspace is temporarily unavailable. Try again shortly.'});
    }
  };
}
