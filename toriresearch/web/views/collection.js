/** Pipeline controls consume durable state; browser lifetime never owns work. */
import {call} from '../api.js';
import {date,esc,num,eur} from '../format.js';
import {metric} from '../components.js';

export function runSummary(run) {
  if (!run) return '<p class="note">No pipeline runs have been recorded.</p>';
  const counts=run.counts||{},stage=run.record?.current_stage;
  const progress=(run.jobs?.find(job=>job.status==='running')||run.jobs?.slice().reverse().find(job=>job.result?.applied_revision!=null))?.result||{};
  const elapsed=(new Date(run.finished_at||Date.now())-new Date(run.started_at))/1000;
  return '<div class="metric-list">'
    +metric('Run status',esc(run.status)+(stage?' · '+esc(stage):''))
    +metric('Started / finished',date(run.started_at)+' / '+(run.finished_at?date(run.finished_at):'In progress'))
    +metric('Elapsed',Number.isFinite(elapsed)?num(Math.max(0,elapsed)/60,1)+' minutes':'—')
    +metric('Cycle budgets',esc('Discovery '+(run.record?.settings?.sweep_budget??'—')+' · detail '+(run.record?.settings?.item_budget??'—')+' · classification '+(run.record?.settings?.enrich_budget??'—')))
    +metric('HTTP attempts / completed',num(run.http_attempts)+' / '+num(run.http_completed))
    +metric('Measured pace · last 100 attempts',run.measured_rps==null?'Awaiting request sample':num(run.measured_rps,3)+' requests / second')
    +metric('Requests in the last 30 seconds',num(run.recent_attempts||0))
    +metric('HTTP statuses',esc(JSON.stringify(run.recent_http_statuses||{})))
    +metric('New / repeated / changed observations',run.attribution_available===false?'Attribution unavailable for this historical run':[counts.new,counts.reobserved,counts.changed].map(v=>num(v||0)).join(' / '))
    +metric('Derived classifications',run.attribution_available===false?'See historical stage results':num(counts.classification||0))
    +metric('Requested / worker-applied settings revision',num(run.record?.settings_revision)+' / '+num(progress.applied_revision))
    +metric('Worker-applied pace',progress.effective_source_rps==null?'Awaiting checkpoint':num(progress.effective_source_rps,3)+' source / '+num(progress.effective_item_rps,3)+' detail requests / second')
    +metric('Heartbeat / deadline',date(run.heartbeat_at)+' / '+date(run.deadline_at))
    +metric('Completion reason',esc(run.record?.terminal_reason||'Processing'))+'</div>';
}

export function eventRows(events) {
  return events.map(event=>'<tr><td>'+date(event.at)+'</td><td>'+esc(event.payload?.stage||'—')
    +'</td><td>'+esc(event.severity)+'</td><td>'+esc(event.event_type)
    +'</td><td><code>'+esc(JSON.stringify(event.payload||{}))+'</code></td></tr>').join('');
}

export function dataRows(rows) {
  return rows.map(row=>{const s=row.snapshot||{};return '<tr><td>'+date(row.observed_at)+'</td><td>'+esc(row.kind)
    +'</td><td><button class="btn quiet small" type="button" data-crawl-listing="'+esc(row.listing_pk)+'">'
    +esc(s.title||s.listing_id||'Open current listing')+'</button></td><td>'+eur(s.listed_price_eur)
    +'</td><td>'+esc(row.kind==='classification'?(s.product_type||'Unknown')+' · derived':s.outcome_status||s.status_norm||'Unknown')+'</td></tr>';}).join('');
}

export function initCollectionPanel({onListing=()=>{}}={}) {
  const $=id=>document.getElementById(id),form=$('crawl-settings-form');
  if (!form) return {open(){},close(){}};
  const numeric=['source_rps','item_rps','sweep_budget','sweep_pages','item_budget','enrich_budget'];
  const scopes=['categories','municipalities'];
  let visible=false,timer=null,stream=null,control=null,selected='',run=null,dirty=false,busy=false;
  let historyRows=[],historyCursor=0,logCursor=0,seen=new Set(),logRows=[],dataCursor=0,dataNext=0,dataStack=[];
  let pendingCommand=null,startToken=null,sequence=0,dataSequence=0,editingRevision=null;
  const message=(text,error=false)=>{const node=$('crawl-message');node.textContent=text;node.hidden=false;node.className='message '+(error?'error':'info');};
  const settings=()=>Object.fromEntries([...numeric.map(k=>[k,Number(form.elements[k].value)]),...scopes.map(k=>[k,form.elements[k].value.split(',').map(v=>v.trim()).filter(Boolean)])]);
  const post=(url,payload)=>call(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  function buttons() {
    for(const k of [...numeric,...scopes])form.elements[k].disabled=!control?.available;
    $('crawl-preview').disabled=busy||!control?.available;
    $('crawl-start').disabled=busy||!control?.can_start;
    $('crawl-pause').disabled=busy||!control?.available;
    $('crawl-pause').textContent=control?.collection_enabled?'Pause collection':'Resume collection';
    $('crawl-stop').disabled=busy||run?.status!=='running';
    form.querySelector('[type="submit"]').disabled=busy||!control?.available;
    $('crawl-export-data').disabled=busy||!selected;
    $('crawl-export-events').disabled=busy||!selected;
  }
  function drawControl(value) {
    control=value;
    if (!dirty) for (const k of [...numeric,...scopes]) form.elements[k].value=Array.isArray(value.settings?.[k])?value.settings[k].join(', '):value.settings?.[k]??'';
    for (const k of numeric) form.elements[k].max=String(value.ceilings?.[k]??'');
    $('crawl-mode').textContent=value.collection_enabled?'Collection enabled':'Collection paused';
    $('crawl-readiness').textContent=(value.can_start?'Ready to start. ':('Start unavailable: '+(value.start_reason||'Checking readiness')+'. '))
      +'Schedule: every '+num((value.interval_seconds||0)/60)+' minutes. Next eligible: '+date(value.next_eligible_at)+'. '
      +(value.platform_pause?'Platform pause: '+value.platform_pause.status+'. ':'')+'Retained history: '+num(value.retention_days)+' days.';
    buttons();
  }
  function closeStream() {if(stream){stream.close();stream=null;}}
  function appendEvents(events) {
    for (const event of events) {if(!seen.has(event.id)){seen.add(event.id);logRows.push(event);}logCursor=Math.max(logCursor,event.id);}
    logRows=logRows.slice(-200);seen=new Set(logRows.map(row=>row.id));
    $('crawl-log-rows').innerHTML=eventRows(logRows);
  }
  async function logs(reset=false) {
    if(!selected)return;
    if(reset){logCursor=0;seen=new Set();logRows=[];}
    const id=selected,version=sequence;
    const query=new URLSearchParams({run_id:id,after:String(logCursor),severity:$('crawl-log-severity').value,stage:$('crawl-log-stage').value});
    const page=await call('/api/collection/events?'+query);
    if(id!==selected||version!==sequence)return;
    appendEvents(page.events);$('crawl-log-more').hidden=!page.has_more;
  }
  async function records() {
    if(!selected)return;
    const id=selected,version=sequence,cursor=dataCursor,ticket=++dataSequence;
    const page=await call('/api/collection/data?'+new URLSearchParams({run_id:id,after:String(cursor),direction:'desc',kind:$('crawl-data-kind').value}));
    if(id!==selected||version!==sequence||cursor!==dataCursor||ticket!==dataSequence)return;
    $('crawl-data-rows').innerHTML=dataRows(page.rows);dataNext=page.next_cursor;
    $('crawl-data-next').disabled=!page.has_more;$('crawl-data-prev').disabled=!dataStack.length;
  }
  function drawRun(value) {
    run=value;$('crawl-run-summary').innerHTML=runSummary(value);
    $('crawl-run-settings').textContent=JSON.stringify({settings:value.record?.settings,stages:value.jobs},null,2);
    buttons();
  }
  function connect() {
    closeStream();
    if(!visible||!selected||run?.status!=='running'||typeof EventSource==='undefined'||document.documentElement.dataset.liveStreams==='0')return;
    const id=selected,version=sequence;
    const source=new EventSource('/api/collection/events?'+new URLSearchParams({run_id:id,stream:'1',after:String(logCursor),severity:$('crawl-log-severity').value,stage:$('crawl-log-stage').value}));
    stream=source;
    source.addEventListener('collection',event=>{if(selected===id&&version===sequence)appendEvents([JSON.parse(event.data)]);});
    source.addEventListener('progress',event=>{
      if(selected!==id||version!==sequence)return;
      const value=JSON.parse(event.data);drawRun(value);
      $('crawl-connection').textContent='Live progress · '+date(new Date().toISOString());
      if(!dataCursor)records().catch(error=>message(error.message,true));
      if(value.status!=='running'){closeStream();refresh().catch(error=>message(error.message,true));}
    });
    source.onerror=()=>{$('crawl-connection').textContent='Reconnecting live updates; polling retained state.';};
  }
  async function choose(id) {
    sequence++;selected=id;closeStream();dataCursor=0;dataStack=[];logCursor=0;seen=new Set();logRows=[];
    $('crawl-run-select').value=id;
    if(!id){run=null;$('crawl-run-summary').innerHTML=runSummary(null);buttons();return;}
    const version=sequence,value=await call('/api/collection/run?'+new URLSearchParams({run_id:id}));
    if(version!==sequence)return;
    drawRun(value);await Promise.all([logs(true),records()]);connect();
  }
  function historyOptions() {
    $('crawl-run-select').innerHTML=historyRows.length?historyRows.map(row=>'<option value="'+esc(row.run_id)+'">'+esc(date(row.started_at)+' · '+row.origin+' · '+row.status)+'</option>').join(''):'<option value="">No runs recorded</option>';
    $('crawl-run-select').value=selected;
  }
  async function refresh() {
    const [state,history]=await Promise.all([call('/api/collection/control'),call('/api/collection/runs')]);
    if(!visible)return;
    drawControl(state);
    const latestIds=new Set(history.runs.map(row=>row.run_id));
    historyRows=[...history.runs,...historyRows.filter(row=>!latestIds.has(row.run_id))];historyCursor=Math.max(historyCursor,historyRows.length);historyOptions();$('crawl-history-more').hidden=!history.has_more;
    if(pendingCommand){const command=await call('/api/collection/command?'+new URLSearchParams({command_id:pendingCommand}));
      if(command.run_id){pendingCommand=null;await choose(command.run_id);if(command.status==='declined')message('Start declined: '+command.reason+'. Showing the recorded owner.',true);}
      else if(command.status!=='pending'){pendingCommand=null;message('Start '+command.status+': '+(command.reason||''),true);}}
    if(!selected&&historyRows.length)await choose(state.active_run?.run_id||historyRows[0].run_id);
    else if(selected){const value=await call('/api/collection/run?'+new URLSearchParams({run_id:selected}));if(value.run_id===selected){drawRun(value);await logs();if(!dataCursor)await records();if(!stream)connect();}}
  }
  async function action(work) {
    if(busy)return;busy=true;buttons();
    try{await work();}catch(error){message(error.message,true);}finally{busy=false;buttons();}
  }
  async function exportAll(resource) {
    const id=selected,chunks=[];let cursor=0,more=true;
    while(more){const page=await call('/api/collection/export?'+new URLSearchParams({run_id:id,resource,after:String(cursor),format:'json'}));
      chunks.push(chunks.length?page.csv.slice(page.csv.indexOf('\n')+1):page.csv);
      if(page.has_more&&page.next_cursor<=cursor)throw new Error('Export cursor did not advance.');
      cursor=page.next_cursor;more=page.has_more;}
    const url=URL.createObjectURL(new Blob(chunks,{type:'text/csv;charset=utf-8'})),link=document.createElement('a');
    link.href=url;link.download='collection-'+resource+'-'+id+'.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    message('Exported retained '+resource+' for this run.');
  }
  form.addEventListener('input',()=>{if(!dirty)editingRevision=control?.revision;dirty=true;});
  form.addEventListener('submit',event=>{event.preventDefault();if(!form.reportValidity())return;action(async()=>{
    const value=await post('/api/collection/settings',{settings:settings(),expected_revision:editingRevision??control.revision});dirty=false;editingRevision=null;drawControl(value);message('Settings saved. Workers acknowledge live pace changes at their next checkpoint.');await refresh();});});
  $('crawl-preview').onclick=()=>action(async()=>{if(!form.reportValidity())return;
    const value=await post('/api/collection/preview',{settings:settings()});$('crawl-preview-details').hidden=false;
    $('crawl-preview-content').textContent=JSON.stringify({settings:value.settings,can_start:value.can_start,reason:value.start_reason,next_eligible_at:value.next_eligible_at,runtime_seconds:value.runtime_seconds},null,2);});
  $('crawl-start').onclick=()=>action(async()=>{if(!form.reportValidity())return;
    startToken=startToken||crypto.randomUUID();const result=await post('/api/collection/start',{client_token:startToken,settings:settings()});startToken=null;
    pendingCommand=result.command_id;message('Start request '+result.status+'. Waiting for worker ownership.');await refresh();});
  $('crawl-pause').onclick=()=>action(async()=>{const command=control.collection_enabled?'pause':'resume';
    const result=await post('/api/collection/control',{command,client_token:crypto.randomUUID()});message(command==='pause'?'Pause '+result.status+'. The current source operation finishes first.':'Collection enabled for the next eligible cycle.');await refresh();});
  $('crawl-stop').onclick=()=>action(async()=>{const result=await post('/api/collection/control',{command:'stop',run_id:selected,client_token:crypto.randomUUID()});message('Stop '+result.status+'. Future scheduled cycles remain enabled.');await refresh();});
  $('crawl-run-select').onchange=()=>choose($('crawl-run-select').value).catch(error=>message(error.message,true));
  for(const id of ['crawl-log-severity','crawl-log-stage'])$(id).onchange=()=>{sequence++;closeStream();logs(true).then(connect).catch(error=>message(error.message,true));};
  $('crawl-log-more').onclick=()=>logs().catch(error=>message(error.message,true));
  $('crawl-data-kind').onchange=()=>{dataCursor=0;dataStack=[];records().catch(error=>message(error.message,true));};
  $('crawl-data-next').onclick=()=>{dataStack.push(dataCursor);dataCursor=dataNext;records().catch(error=>message(error.message,true));};
  $('crawl-data-prev').onclick=()=>{dataCursor=dataStack.pop()||0;records().catch(error=>message(error.message,true));};
  $('crawl-history-more').onclick=()=>action(async()=>{const page=await call('/api/collection/runs?after='+historyCursor);historyRows.push(...page.runs);historyCursor=page.next_cursor;historyOptions();$('crawl-history-more').hidden=!page.has_more;});
  for(const resource of ['events','data'])$('crawl-export-'+resource).onclick=()=>action(()=>exportAll(resource));
  $('crawl-data-rows').addEventListener('click',event=>{const button=event.target.closest('[data-crawl-listing]');if(button)onListing(Number(button.dataset.crawlListing));});
  async function tick(){if(!visible)return;try{await refresh();}catch(error){message(error.message,true);}finally{if(visible)timer=setTimeout(tick,stream?5000:2000);}}
  return {open(){if(visible)return;visible=true;tick();},close(){visible=false;sequence++;clearTimeout(timer);closeStream();}};
}
