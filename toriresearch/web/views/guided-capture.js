import {esc,date,eur,num} from '../format.js';

/** A lost response keeps the exact request frozen until its receipt is recovered. */
export function createCaptureSaver({call,url,onState}){
  let state={status:'idle',result:null,error:''},request=null;
  const publish=next=>{state={...state,...next};onState?.(state);};
  const save=async build=>{
    if(['saving','saved'].includes(state.status))return state.result;
    try{
      request??=JSON.stringify(build());
      publish({status:'saving',error:''});
      const response=await call(url,{method:'POST',headers:{'Content-Type':'application/json'},body:request});
      const result=response?.result||response;
      if(!result?.receipt)throw new Error('The save response did not contain a receipt.');
      publish({status:'saved',result,error:''});return result;
    }catch(error){
      const rejected=[400,401,403,409,422].includes(error.status);
      // A builder error happened before a request was made.
      const uncertain=!!request&&!rejected;
      if(!uncertain)request=null;
      publish({status:uncertain?'uncertain':'rejected',error:error.message});return null;
    }
  };
  return {save,state:()=>state,reset:()=>{
    if(['saving','uncertain'].includes(state.status))return false;
    request=null;publish({status:'idle',result:null,error:''});return true;
  }};
}

export function captureReceipt(result,kind='sold'){
  const row=result?.receipt;if(!row)return '';
  const heading=result.replayed?'Recovered existing receipt':'Saved receipt';
  if(kind==='attempt')return `<article class="message success capture-receipt" role="status"><strong>${heading} · ${esc(row.attempt_id)}</strong><p>Decision #${esc(row.decision_pk)} · ${esc(row.status)}</p><p>Observed ${date(row.occurred_at)} · recorded ${date(row.recorded_at)}</p><p>${esc(row.reason)}</p><p>Evidence reference: ${esc(row.evidence_ref)}</p></article>`;
  return `<article class="message success capture-receipt" role="status"><strong>${heading} · sold capture #${esc(row.sold_obs_pk)}</strong><p>${esc(row.source)} · ${esc(row.platform)} · ${esc(row.keyword)}</p><p>Captured ${date(row.captured_at)} by ${esc(row.captured_by)}</p><p>Filters: ${esc(row.category_norm)} / ${esc(row.condition_norm)} · window ${esc(row.window_start)} → ${esc(row.window_end)}</p><p>Sample: ${num(row.n_sold)} sold transactions · observed average ${eur(row.avg_sold_price_eur)}</p><p>Source URL: ${esc(row.query_url)}</p>${row.notes?`<p>${esc(row.notes)}</p>`:''}</article>`;
}

export function recentCaptureReceipts(rows=[]){
  return rows.length?`<details><summary>Recent retained capture receipts (${rows.length}, at most 20)</summary>${rows.map(receipt=>captureReceipt({receipt})).join('')}</details>`:'<p class="note">No retained sold capture receipt.</p>';
}

const gcInput=(name,label,type='text',attrs='')=>`<label class="field">${label}<input name="${name}" type="${type}" ${attrs}></label>`;
export function soldCaptureForm(){
  return `<div class="card-heading"><div><h2>Capture one sold aggregate</h2><p>Enter figures you actually observed in eBay Product Research, in EUR.</p></div></div><p class="note" data-capture-context>No item selected. Captures describe the named query, independently of an owned item.</p><form data-sold-capture><div class="form-grid">${gcInput('keyword','Captured keyword','text','required maxlength="120"')}${gcInput('captured_by','Captured by','text','required maxlength="64"')}${gcInput('query_url','Source query URL','url','required maxlength="2000"')}${gcInput('captured_at','Actually captured at','datetime-local','required step="1"')}${gcInput('category_norm','Captured category filter','text','required maxlength="120"')}<label class="field">Captured condition filter<select name="condition_norm" required><option value="">Choose the recorded filter</option>${['new_unused','like_new','very_good','good','fair','poor','for_parts','refurbished','unknown'].map(value=>`<option value="${value}">${value==='unknown'?'All / unspecified':value.replaceAll('_',' ')}</option>`).join('')}</select></label>${gcInput('window_start','Observation window starts','date','required')}${gcInput('window_end','Observation window ends','date','required')}${gcInput('n_sold','Observed sold sample size','number','required min="1" max="10000000" step="1"')}${gcInput('avg_sold_price_eur','Observed average sold price (€)','number','required min="0" max="1000000000" step="0.01"')}${gcInput('sold_price_min_eur','Observed sold minimum (€)','number','min="0" max="1000000000" step="0.01"')}${gcInput('sold_price_max_eur','Observed sold maximum (€)','number','min="0" max="1000000000" step="0.01"')}${gcInput('sell_through_rate','Observed sell-through fraction (0–1)','number','min="0" max="1" step="0.0001"')}${gcInput('avg_shipping_eur','Observed average shipping (€)','number','min="0" max="1000000000" step="0.01"')}<label class="field wide">Other recorded filters / currency conversion evidence<textarea name="notes" maxlength="2000" rows="3"></textarea></label></div><p class="note">Blank optional amounts remain unknown. Category, condition, query URL and observation window identify the captured filters; record other filters in the notes. This aggregate does not create a listing sale label, an owned outcome or a matched ask-to-sale ratio.</p><div class="message" data-capture-message role="status" hidden></div><div data-capture-receipt></div><div class="form-footer"><button class="btn quiet" type="button" data-capture-new>Start another capture</button><button class="btn primary" type="submit">Save observed aggregate</button></div></form>`;
}

function gcPaint(form,state,kind){
  const locked=['saving','saved','uncertain'].includes(state.status);
  for(const node of form.querySelectorAll('input,select,textarea'))node.disabled=locked;
  const submit=form.querySelector('[type="submit"]');submit.disabled=['saving','saved'].includes(state.status);
  submit.textContent=state.status==='uncertain'?'Retry the same capture':kind==='attempt'?'Record attempt':'Save observed aggregate';
  form.querySelector('[data-capture-new]').disabled=['saving','uncertain'].includes(state.status);
  const message=form.querySelector('[data-capture-message]');
  message.hidden=!state.error;message.className='message error';
  message.textContent=state.error+(state.status==='uncertain'?' The result is unconfirmed. Retry the same capture to recover its receipt.':'');
  form.querySelector('[data-capture-receipt]').innerHTML=captureReceipt(state.result,kind);
  form.setAttribute('aria-busy',String(state.status==='saving'));
}

export function initSoldCapture({root,call,onSaved}){
  root.innerHTML=soldCaptureForm();const form=root.querySelector('form');
  const saver=createCaptureSaver({call,url:'/api/soldextract',onState:state=>gcPaint(form,state,'sold')});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(saver.state().status!=='uncertain'&&!form.reportValidity())return;
    const result=await saver.save(()=>{
      const params=Object.fromEntries(new FormData(form));
      params.captured_at=new Date(params.captured_at).toISOString();params.n_sold=Number(params.n_sold);
      return {action:'capture',params:{...params,source:'ebay.product_research.ui',platform:'ebay'}};
    });
    if(result)try{await onSaved?.(result);}catch{ /* The confirmed receipt remains visible. */ }
  });
  form.querySelector('[data-capture-new]').onclick=()=>{if(saver.reset())form.reset();};
  return {isBusy:()=>saver.state().status==='saving',context:title=>{
    root.querySelector('[data-capture-context]').textContent=title?'Returning item: '+title+'. Record the actual captured query; this is separate aggregate evidence.':'No item selected. Captures describe the named query, independently of an owned item.';
  },focus:()=>form.querySelector('input:not(:disabled),[type="submit"]:not(:disabled),[data-capture-new]:not(:disabled)')?.focus()};
}

export function initAttemptCapture({dialog,form,context,call,onSaved}){
  const drafts=new Map();let chosen=null;
  const remember=()=>{if(chosen&&['idle','rejected'].includes(chosen.saver.state().status))chosen.values=Object.fromEntries(new FormData(form));};
  for(const event of ['input','change'])form.addEventListener(event,remember);
  dialog.addEventListener('close',remember);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!chosen||chosen.saver.state().status!=='uncertain'&&!form.reportValidity())return;
    remember();const current=chosen;
    const result=await current.saver.save(()=>{
      const values=Object.fromEntries(new FormData(form)),params={decision_pk:current.decision.decision_pk,status:values.status,reason:values.reason,evidence_ref:values.evidence_ref,client_token:'attempt-'+crypto.randomUUID()};
      if(values.occurred_on)params.occurred_on=new Date(values.occurred_on).toISOString();
      return {action:'attempt',params};
    });
    if(result)try{await onSaved?.(result);}catch{ /* The confirmed receipt remains visible. */ }
  });
  form.querySelector('[data-capture-new]').onclick=()=>{if(chosen?.saver.reset()){form.reset();remember();}};
  const editor={isBusy:()=>chosen?.saver.state().status==='saving',open:decision=>{
    if(editor.isBusy())return false;remember();
    const key=String(decision.decision_pk);
    if(!drafts.has(key)){
      const entry={decision,values:{}};
      entry.saver=createCaptureSaver({call,url:'/api/outcomes',onState:state=>{if(chosen===entry)gcPaint(form,state,'attempt');}});drafts.set(key,entry);
    }
    chosen=drafts.get(key);form.reset();
    for(const [key,value] of Object.entries(chosen.values))if(form.elements[key])form.elements[key].value=value;
    context.textContent='Decision #'+decision.decision_pk+' · '+(decision.title||'Retained decision')+' · '+date(decision.recorded_at);
    gcPaint(form,chosen.saver.state(),'attempt');dialog.showModal();return true;
  }};return editor;
}
