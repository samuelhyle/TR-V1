import {esc,eur,date,num} from '../format.js';
import {badge,metric} from '../components.js';

const OUTCOME_COSTS={paid_labour:'Paid labour',carrying_cost:'Actual carrying cost',overhead_cost:'Allocated overhead'};
const OUTCOME_CASH={cash_committed:'Cash committed',usable_payout:'Usable payout',payout_reversal:'Payout reversal'};
const outcomeOptions=(values,selected)=>Object.entries(values).map(([key,label])=>`<option value="${esc(key)}"${key===selected?' selected':''}>${esc(label)}</option>`).join('');
const outcomeInput=(name,label,type='text',extra='')=>`<label class="field">${esc(label)}<input name="${name}" type="${type}" ${extra}></label>`;
const outcomeEvidence=()=>outcomeInput('evidence_ref','Evidence reference','text','required maxlength="500"')+outcomeInput('occurred_on','Observed at (blank = now)','datetime-local','step="0.001"');
const outcomeSubmit=label=>`<div class="form-footer"><button class="btn primary" type="submit">${label}</button></div>`;
const outcomeAmount=(value)=>value==null?'':String(value/100);
const outcomeLocalTime=value=>{const stamp=new Date(value);return new Date(stamp.getTime()-stamp.getTimezoneOffset()*60000).toISOString().slice(0,23);};

export function outcomeSummary(result){
  const link=result.acquisition_attribution;
  return `<div class="metric-list">${metric('Acquisition decision',link?.status==='explicit'?`Receipt-linked #${link.decision.decision_pk}`:badge(link?'Evidence needs review':'Unlinked','amber'))}${metric('Whole-unit cash contribution',eur(result.cash_contribution_eur))}${metric('Economic profit after operator time',eur(result.economic_profit_eur))}${metric('Recorded work',num(result.total_work_hours,2)+' hours')}${metric('Cost and work scope',badge(result.time_scope_complete?'Reviewed current evidence':'Review required',result.time_scope_complete?'green':'amber'))}${metric('Cash recovery',esc(result.cash.status.replaceAll('_',' ')))}</div><p class="inventory-help">${esc(result.basis)} Blank costs and hourly values remain unknown. Bank movements measure recovery and do not add profit.</p>`;
}

export function outcomeRemedies(row){
  if(!row.owned_sku)return '';
  const panel={unattributed_acquisition:'receipt',invalid_acquisition_link:'receipt',costs_incomplete:'costs',cost_scope_unreviewed:'review',available:'review'}[row.profit_comparison_status]||'review';
  const label={receipt:'Link / review acquisition receipt',costs:'Enter missing actual costs',review:'Review owned outcome evidence'}[panel];
  return `<button class="btn small" type="button" data-owned-outcomes="${esc(row.owned_sku)}" data-outcome-panel="${panel}" data-outcome-decision="${row.decision_pk}">${label}</button>`;
}

export function decisionAttempts(row){
  const rows=row.acquisition_attempts||[];
  return `<h3>Completed acquisition attempts</h3>${rows.length?'<div class="unit-history">'+rows.map(attempt=>`<article><strong>${esc(attempt.status)} · ${date(attempt.occurred_at)}</strong><p>${esc(attempt.reason)}</p><p class="note">${esc(attempt.evidence_ref)}</p></article>`).join('')+'</div>':'<p class="inventory-help">No completed acquisition attempt recorded for this decision.</p>'}`;
}

export function resalePreview(pair){
  if(!pair?.listing_id)return '<p class="inventory-help">No resale listing selected. This sale will be explicitly unpaired.</p>';
  const complete=pair.ask&&pair.identity&&pair.market;
  return `<p class="inventory-help">${badge(complete?'Retained pre-sale evidence':'Incomplete resale evidence',complete?'green':'amber')} Your resale listing #${esc(pair.listing_id)} · ask ${eur(pair.ask?.ask_eur)} · observed ${date(pair.ask?.price_observed_at)} · recorded ${date(pair.ask?.price_recorded_at)} · identity state ${date(pair.identity_state_at)} · market ${esc(pair.market?.subplatform||'unknown')}.</p><p class="note">${complete?'Canonical comparability still requires accepted identity and a matching market.':'Missing historical ask, identity or market stays unknown.'} The sale freezes retained evidence again when saved.</p>`;
}

export function outcomeForms(result,unitVersion,decisionPk=null){
  const attempts=result.acquisition_attempts.map(row=>`<article><strong>Decision #${row.decision_pk} · ${esc(row.status)}</strong><p>${esc(row.reason)} · ${esc(row.evidence_ref)}</p><p class="note">Occurred ${date(row.occurred_at)} · recorded ${date(row.recorded_at)}</p></article>`).join('');
  const receipt=result.acquisition_attribution?`<p class="inventory-help">${esc(result.acquisition_attribution.reason)}. The recorded receipt is retained; another decision cannot replace it.</p>`:result.acquisition_decisions.length?`<form data-outcome-action="attempt"><div class="form-grid"><label class="field wide">Decision that led to this acquisition<select name="decision_pk" required><option value="">Choose explicitly</option>${result.acquisition_decisions.map(row=>`<option value="${row.decision_pk}"${row.decision_pk===decisionPk?' selected':''}>#${row.decision_pk} · ${esc(row.decision)} · ${date(row.recorded_at)} · ${esc(row.notes)}</option>`).join('')}</select></label>${outcomeInput('reason','Receipt reason','text','required maxlength="1000"')}${outcomeEvidence()}</div>${outcomeSubmit('Link acquisition receipt')}</form>`:'<p class="inventory-help">No matching decision was recorded before this purchase. A new decision cannot create earlier acquisition evidence.</p>';
  const costs=Object.entries(OUTCOME_COSTS).map(([kind,label])=>{
    const entry=result.financial_entries.find(row=>row.kind===kind);
    return `<form data-outcome-action="operating_cost" data-cost-kind="${kind}"${entry?` data-supersedes="${esc(entry.entry_id)}"`:''}><h4>${label}${entry?' · current '+eur(entry.amount_minor==null?null:entry.amount_minor/100):' · unknown'}</h4><div class="form-grid">${outcomeInput('amount_eur','Whole-unit total (€)','number',`min="0" step="0.01" placeholder="Unknown" value="${outcomeAmount(entry?.amount_minor)}"`)}${outcomeInput('reason','Scope / allocation reason','text','required maxlength="1000"')}${outcomeEvidence()}</div>${outcomeSubmit(entry?'Correct '+label.toLowerCase():'Record '+label.toLowerCase())}</form>`;
  }).join('');
  const workRows=result.active_work.map(row=>`<article><strong>${esc(row.payload.activity)} · ${esc(row.payload.worker)} · ${row.payload.minutes} min</strong><p>${eur(row.payload.hourly_value_minor==null?null:row.payload.hourly_value_minor/100)} / hour · ${esc(row.evidence_ref)}</p><button class="btn quiet small" type="button" data-correct-work="${row.event_pk}">Correct work entry</button></article>`).join('');
  const cashRows=result.financial_entries.filter(row=>OUTCOME_CASH[row.kind]).map(row=>`<article><strong>${OUTCOME_CASH[row.kind]} · ${eur(row.amount_minor/100)}</strong><p>${esc(row.evidence_ref)} · ${date(row.effective_at)}</p><button class="btn quiet small" type="button" data-correct-cash="${esc(row.entry_id)}">Correct cash entry</button></article>`).join('');
  const orderOptions='<option value="">No order (committed cash / legacy sale)</option>'+result.orders.map(row=>`<option value="${esc(row.order_id)}">Cycle ${row.cycle} · ${esc(row.state)} · ${esc(row.order_id)}</option>`).join('');
  const orders=result.orders.map(row=>`<article><button class="btn small" type="button" data-ledger-order="${esc(row.order_id)}">Review sale cycle ${row.cycle}</button>${resalePreview(row.resale_pairing)}</article>`).join('');
  const writeoff=['available','quarantined'].includes(result.stock_phase)&&!result.orders.length?`<details id="outcome-writeoff"><summary>Write off this unsold unit</summary><form data-outcome-action="write_off_unsold" data-unit-version="${unitVersion}"><p class="inventory-help">Record an observed disposal of this physical item. It preserves the acquisition loss without creating a sale.</p><div class="form-grid">${outcomeInput('disposal_cost_eur','Actual disposal cost (€)','number','min="0" step="0.01" placeholder="Unknown"')}${outcomeInput('reason','Disposal reason','text','required maxlength="1000"')}${outcomeEvidence()}</div>${outcomeSubmit('Record unsold write-off')}</form></details>`:'';
  const corrections=result.stock_phase==='written_off'&&!result.orders.length?result.financial_entries.filter(row=>['acquisition_cost_eur','inbound_shipping_cost_eur','buyer_fee_eur','disposal'].includes(row.kind)).map(row=>`<form data-outcome-action="correct_cost" data-supersedes="${esc(row.entry_id)}"><h4>Correct ${esc(row.kind.replaceAll('_',' '))}</h4><div class="form-grid">${outcomeInput('amount_eur','Corrected total (€)','number',`min="0" step="0.01" value="${outcomeAmount(row.amount_minor)}" placeholder="Unknown"`)}${outcomeInput('reason','Correction reason','text','required maxlength="1000"')}${outcomeEvidence()}</div>${outcomeSubmit('Correct original cost')}</form>`).join(''):'';
  const acquisition=Object.entries(result.acquisition_costs||{}).map(([key,value])=>metric(key.replaceAll('_',' '),eur(value))).join('');
  const ledger=result.financial_entries.filter(row=>!OUTCOME_CASH[row.kind]).map(row=>`<article><strong>${esc(row.kind.replaceAll('_',' '))} · ${eur(row.amount_minor==null?null:row.amount_minor/100)}</strong><p>${esc(row.evidence_ref)} · ${date(row.effective_at)}${row.order_id?' · owned sale cycle':''}</p></article>`).join('');
  return `<section id="outcome-receipt"><h3>Acquisition receipt</h3>${receipt}<div class="unit-history">${attempts}</div></section>
  <section id="outcome-costs"><h3>Actual operating costs</h3><div class="metric-list">${acquisition}</div><details><summary>Current financial ledger entries</summary><div class="unit-history">${ledger}</div></details><p class="inventory-help">Enter evidenced totals across all return and resale cycles. Exclude costs already in the sale journal. Enter zero only when confirmed; leave unmeasured costs blank. Missing costs: ${result.unknown_costs.length?esc(result.unknown_costs.join(', ')):'none'}.</p>${orders}${costs}${corrections}</section>
  <section id="outcome-work"><h3>Work time</h3><div class="unit-history">${workRows}</div><form data-outcome-action="record_time"><div class="form-grid"><label class="field">Activity<select name="activity">${outcomeOptions(Object.fromEntries(['search','travel','inspect','repair','photo','list','message','fulfil'].map(key=>[key,key])))}</select></label><label class="field">Worker<select name="worker"><option value="operator">Operator</option><option value="paid">Paid worker</option></select></label>${outcomeInput('minutes','Actual minutes','number','required min="0" max="1000000" step="1"')}${outcomeInput('hourly_value_eur','Operator hourly value (€)','number','min="0" step="0.01" placeholder="Unknown"')}${outcomeEvidence()}</div><p class="inventory-help">Paid worker time has no operator rate; record actual wages in paid labour above.</p>${outcomeSubmit('Record work time')}<button class="btn quiet small" type="button" data-cancel-correction hidden>Cancel correction</button></form></section>
  <section id="outcome-cash"><h3>Cash movements</h3><div class="metric-list">${metric('Committed cash',eur(result.cash.committed_eur))}${metric('Net usable payouts',eur(result.cash.usable_net_payout_eur))}${metric('Recovery days',num(result.cash.recovery_days,2))}</div><div class="unit-history">${cashRows}</div><form data-outcome-action="cash"><div class="form-grid"><label class="field">Movement<select name="kind">${outcomeOptions(OUTCOME_CASH)}</select></label><label class="field">Owned order<select name="order_id">${orderOptions}</select></label>${outcomeInput('amount_eur','Actual amount (€)','number','required min="0" step="0.01"')}${outcomeInput('external_event_ref','Unique cash receipt reference','text','required maxlength="100"')}${outcomeEvidence()}</div><p class="inventory-help">Record cash when committed or available for use. Payouts and reversals need their order. These amounts do not add to profit.</p>${outcomeSubmit('Record cash movement')}<button class="btn quiet small" type="button" data-cancel-correction hidden>Cancel correction</button></form></section>
  <section id="outcome-review"><h3>Review current evidence</h3><p class="inventory-help">Check acquisition and all sale-cycle costs, operating totals, work time and cash movements above. A later correction requires a fresh review. Unknown values still withhold profit.</p><form data-outcome-action="review_scope">${outcomeInput('evidence_ref','Review evidence reference','text','required maxlength="500"')}<label class="unit-attestation"><input name="confirm_time_complete" type="checkbox" required>I reviewed the current cost scope and recorded all work, including work on the linked decision.</label><label class="unit-attestation"><input name="confirm_cash_complete" type="checkbox">I reconciled all committed cash, payouts and reversals.</label>${outcomeSubmit('Confirm current evidence review')}</form></section>${writeoff}<details><summary>Evidence history</summary><div class="unit-history">${result.events.map(row=>`<article><strong>${esc(row.event_type)} · #${row.event_pk}</strong><p>${esc(row.evidence_ref)} · observed ${date(row.effective_at)} · recorded ${date(row.recorded_at)}</p></article>`).join('')}</div></details>`;
}

/** Item-scoped editor. Polling never replaces a form; retries retain their token. */
export function initOwnedOutcomes({root,call,onSaved}){
  let data=null,sequence=0,busy=false,owner=null,unitVersion=null,selectedDecision=null;
  let baseline='';
  const retries=new Map();
  const signature=()=>JSON.stringify([...root.querySelectorAll('form')].map(form=>({dataset:{...form.dataset},fields:[...form.querySelectorAll('[name]')].map(node=>[node.name,node.value,node.checked,node.disabled])})));
  const hasDraft=()=>!!data&&signature()!==baseline;
  const feedback=(text,error=false)=>{const node=root.querySelector('[data-outcome-message]');node.textContent=text;node.hidden=false;node.className='message '+(error?'error':'success');};
  const draw=()=>{root.innerHTML=`<div data-outcome-message class="message" role="status" hidden></div><button class="btn quiet small" type="button" data-outcome-reload>Reload evidence and discard drafts</button><div data-outcome-summary>${outcomeSummary(data)}</div>${outcomeForms(data,unitVersion,selectedDecision)}`;baseline=signature();};
  const load=async(purchasePk,options={})=>{
    if(busy)return false;
    if(owner!==purchasePk&&hasDraft()&&!options.force){feedback('Save this item’s unfinished outcome evidence or reload it and discard drafts before opening another item.',true);return false;}
    if(data&&owner===purchasePk&&!options.force&&root.querySelector('form')){
      const stamp=++sequence;
      try{
        const current=await call('/api/outcomes?'+new URLSearchParams({purchase_pk:purchasePk}));
        if(owner!==purchasePk||stamp!==sequence)return false;
        if(JSON.stringify(current)!==JSON.stringify(data))feedback('Evidence changed. Your entered values and displayed review scope are retained. Reload evidence and discard drafts to review the current scope.');
        if(options.panel)root.querySelector('#outcome-'+options.panel)?.scrollIntoView({block:'start',behavior:'smooth'});
        return true;
      }catch(error){if(owner===purchasePk)feedback(error.message,true);return false;}
    }
    const stamp=++sequence;owner=purchasePk;unitVersion=options.unitVersion;selectedDecision=options.decisionPk??null;
    root.innerHTML='<p class="note" role="status">Reading owned outcome evidence…</p>';
    try{const next=await call('/api/outcomes?'+new URLSearchParams({purchase_pk:purchasePk}));if(stamp!==sequence)return false;data=next;draw();if(options.panel)root.querySelector('#outcome-'+options.panel)?.scrollIntoView({block:'start',behavior:'smooth'});return true;}
    catch(error){if(stamp===sequence){root.innerHTML='<p class="message error" role="alert"></p>';root.firstElementChild.textContent=error.message;}return false;}
  };
  root.addEventListener('submit',async event=>{
    const form=event.target.closest('[data-outcome-action]');if(!form)return;event.preventDefault();if(busy||!data||!form.reportValidity())return;
    const action=form.dataset.outcomeAction,values=Object.fromEntries(new FormData(form));
    const params={purchase_pk:owner,evidence_ref:values.evidence_ref};
    if(values.occurred_on)params.occurred_on=new Date(values.occurred_on).toISOString();
    if(action==='attempt')Object.assign(params,{decision_pk:Number(values.decision_pk),status:'acquired',reason:values.reason});
    if(action==='operating_cost'||action==='correct_cost')Object.assign(params,{amount_eur:values.amount_eur||null,reason:values.reason});
    if(action==='operating_cost')params.kind=form.dataset.costKind;
    if(form.dataset.supersedes)params.supersedes_id=form.dataset.supersedes;
    if(action==='record_time'){
      Object.assign(params,{activity:form.elements.activity.value,worker:form.elements.worker.value,minutes:Number(values.minutes),hourly_value_eur:form.elements.worker.value==='paid'?null:values.hourly_value_eur||null});
      if(form.dataset.workCorrection)params.supersedes_event_pk=Number(form.dataset.workCorrection);
      if(form.dataset.decisionOwner){delete params.purchase_pk;params.decision_pk=Number(form.dataset.decisionOwner);}
    }
    if(action==='cash'){
      if(!values.occurred_on){feedback('Enter the observed time of this cash movement.',true);return;}
      Object.assign(params,{kind:form.elements.kind.value,amount_eur:values.amount_eur,external_event_ref:values.external_event_ref,order_id:form.elements.order_id.value||null});
    }
    if(action==='review_scope')Object.assign(params,{confirm_time_complete:form.elements.confirm_time_complete.checked,confirm_cash_complete:form.elements.confirm_cash_complete.checked,expected_scope_sha256:data.scope_sha256});
    if(action==='write_off_unsold')Object.assign(params,{expected_unit_version:Number(form.dataset.unitVersion),reason:values.reason,disposal_cost_eur:values.disposal_cost_eur||null});
    const key=action+':'+(form.dataset.costKind||form.dataset.supersedes||''),fingerprint=JSON.stringify(params),prior=retries.get(key);
    const token=prior?.fingerprint===fingerprint?prior.token:crypto.randomUUID();retries.set(key,{fingerprint,token});params.client_token=token;
    // Keep unfinished sibling forms. Completeness attestations are always reset.
    const formKey=node=>node.dataset.outcomeAction+':'+(node.dataset.costKind||node.dataset.supersedes||'');
    const unfinished=[...root.querySelectorAll('form')].filter(node=>node!==form&&node.dataset.outcomeAction!=='review_scope').map(node=>({key:formKey(node),dataset:{...node.dataset},fields:[...node.querySelectorAll('[name]')].map(input=>({name:input.name,value:input.value,checked:input.checked,disabled:input.disabled}))}));
    const controls=[...root.querySelectorAll('input,select,button')],disabled=controls.map(node=>node.disabled);busy=true;root.setAttribute('aria-busy','true');controls.forEach(node=>node.disabled=true);
    let saved=false;
    try{
      await call('/api/outcomes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});
      saved=true;
      const next=await call('/api/outcomes?'+new URLSearchParams({purchase_pk:owner}));data=next;retries.delete(key);draw();
      for(const draft of unfinished){const sibling=[...root.querySelectorAll('form')].find(node=>formKey(node)===draft.key);if(!sibling)continue;Object.assign(sibling.dataset,draft.dataset);for(const field of draft.fields){const input=sibling.elements[field.name];if(input){input.value=field.value;input.checked=field.checked;input.disabled=field.disabled;}}if(sibling.dataset.workCorrection||sibling.dataset.supersedes&&sibling.dataset.outcomeAction==='cash')sibling.querySelector('[data-cancel-correction]').hidden=false;}
      feedback(action==='review_scope'?'Evidence saved. Current scope review confirmed.':'Evidence saved. Review the current scope after changes.');
      await onSaved?.(data);
    }catch(error){feedback((saved?'Evidence was saved; refreshing it failed. ':'')+error.message+' Reload evidence before retrying a stale correction or review.',true);}
    finally{busy=false;root.setAttribute('aria-busy','false');controls.forEach((node,i)=>node.disabled=disabled[i]);}
  });
  root.addEventListener('change',event=>{
    if(event.target.name!=='worker')return;const form=event.target.closest('[data-outcome-action="record_time"]');if(!form)return;
    form.elements.hourly_value_eur.disabled=event.target.value==='paid';if(event.target.value==='paid')form.elements.hourly_value_eur.value='';
  });
  root.addEventListener('click',event=>{
    if(busy||!data)return;
    if(event.target.closest('[data-outcome-reload]')){load(owner,{unitVersion,decisionPk:selectedDecision,force:true});return;}
    const work=event.target.closest('[data-correct-work]'),cash=event.target.closest('[data-correct-cash]'),cancel=event.target.closest('[data-cancel-correction]');
    if(cancel){const form=cancel.closest('form');form.reset();delete form.dataset.workCorrection;delete form.dataset.decisionOwner;delete form.dataset.supersedes;form.querySelectorAll('select').forEach(node=>node.disabled=false);form.elements.external_event_ref?.removeAttribute('readonly');if(form.elements.hourly_value_eur)form.elements.hourly_value_eur.disabled=false;cancel.hidden=true;return;}
    if(work){
      const row=data.active_work.find(row=>row.event_pk===Number(work.dataset.correctWork)),form=root.querySelector('[data-outcome-action="record_time"]');
      form.dataset.workCorrection=row.event_pk;for(const key of ['activity','worker','minutes'])form.elements[key].value=row.payload[key];form.elements.hourly_value_eur.value=outcomeAmount(row.payload.hourly_value_minor);
      form.elements.occurred_on.value=outcomeLocalTime(row.effective_at);
      if(row.payload.decision_pk)form.dataset.decisionOwner=row.payload.decision_pk;else delete form.dataset.decisionOwner;
      form.elements.activity.disabled=true;form.elements.worker.disabled=true;form.elements.hourly_value_eur.disabled=row.payload.worker==='paid';
      form.querySelector('[data-cancel-correction]').hidden=false;form.scrollIntoView({block:'center'});
    }
    if(cash){
      const row=data.financial_entries.find(row=>row.entry_id===cash.dataset.correctCash),form=root.querySelector('[data-outcome-action="cash"]');
      form.dataset.supersedes=row.entry_id;form.elements.kind.value=row.kind;form.elements.order_id.value=row.order_id||'';form.elements.amount_eur.value=outcomeAmount(row.amount_minor);
      form.elements.occurred_on.value=outcomeLocalTime(row.effective_at);
      const receipt=data.events.find(event=>event.payload.result?.entry_id===row.entry_id);form.elements.external_event_ref.value=receipt?.payload.external_event_ref||row.external_event_ref||'';
      form.elements.kind.disabled=true;form.elements.order_id.disabled=true;form.elements.external_event_ref.readOnly=true;
      form.querySelector('[data-cancel-correction]').hidden=false;form.scrollIntoView({block:'center'});
    }
  });
  return {load,hasDraft,discard:()=>{if(busy)return false;sequence++;data=null;owner=null;baseline='';root.replaceChildren();return true;},isBusy:()=>busy};
}
