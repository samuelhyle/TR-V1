import {esc,eur,num,date} from '../format.js';
import {badge,metric} from '../components.js';

export const scenarioFields=[['resale','Assumed sale proceeds (€)'],['buy','Assumed purchase price (€)'],['inbound','Inbound shipping + buyer fees (€)'],['fee','Sale + payment fee (%)'],['outbound','Outbound costs (€)'],['repair','Repair costs (€)'],['other','Tax + other costs (€)'],['target','Target profit (€)'],['days','Holding / cash-cycle days'],['carrying','Holding cost per day (€)'],['haircut','Downside sale haircut (%)'],['labour_hours','Operator work hours'],['hourly_rate','Operator hourly value (€)'],['packaging','Packaging (€)'],['return_loss','Return loss reserve (€)'],['overhead','Overhead (€)'],['cash_budget','Shared cash budget (€)'],['monthly_hours','Shared monthly work hours'],['storage_units','Shared storage units'],['source_units_month','Assumed sourcing units / month'],['sale_units_month','Assumed sale units / month']];
const scOrigins={operator_assumption:'Your assumption',observed_ask:'Observed ask used as a purchase assumption',observed_inbound:'Observed inbound costs',calculator_default:'Existing calculator assumption',saved_assumption:'Copied saved assumption',legacy_assumption:'Retained user assumption'};
const scOperating=new Set(scenarioFields.slice(11).map(([key])=>key));

export function profileSummary(profile){
  if(!profile)return '';
  const value=field=>field.value==null?'Unknown':field.value===false?'No':field.value===true?'Yes':String(field.value);
  return `<section class="scenario-profile"><h3>Product profile · ${esc(profile.label)}</h3><p class="note">${esc(profile.selection_basis.replaceAll('_',' '))} · ${esc(profile.version)} · ${esc(profile.sha256.slice(0,12))}</p><p>${profile.missing_fields.length?'Missing profile fields: '+esc(profile.missing_fields.map(key=>key.replaceAll('_',' ')).join(', ')):profile.fields.some(field=>field.required)?'Required source fields retained.':'A complete comparison contract is not defined for this profile.'}</p>${profile.missing_reviewed_identity.length?`<p class="note">Reviewed identity still needs: ${esc(profile.missing_reviewed_identity.join(', '))}.</p>`:''}<details><summary>Profile fields and evidence</summary><div class="table-wrap"><table><thead><tr><th>Field</th><th>Value</th><th>Evidence</th></tr></thead><tbody>${profile.fields.map(field=>`<tr><td>${esc(field.label)}${field.required?' · required':''}</td><td>${esc(value(field))}${field.state==='conflict'?' · conflict':''}</td><td>${esc(field.basis.replaceAll('_',' '))}</td></tr>`).join('')}</tbody></table></div></details><p class="note">${esc(profile.note)}</p></section>`;
}

export function scenarioComparison(comparison){
  if(!comparison)return '';
  const ready=comparison.profit_comparison_status==='available'&&comparison.explicit_acquisition_link===true;
  return `<section><h3>Retained decision vs actual outcome</h3>${badge(ready?'Receipt-linked reviewed cash comparison':'Comparison withheld',ready?'green':'amber')}${ready?`<div class="metric-list">${metric('Reviewed actual cash contribution',eur(comparison.comparison_profit_eur))}${metric('Actual − saved base assumption',eur(comparison.profit_vs_assumption_eur))}</div>`:''}<p class="note">${esc(comparison.profit_comparison_note||'A validated receipt and reviewed cost scope are required.')} Editing the copy below does not change this retained decision.</p></section>`;
}

export function scenarioProvenance(scenario){
  const provenance=scenario?.provenance;if(!provenance)return '';
  return `<details><summary>Frozen profile and assumption origins</summary>${profileSummary(provenance.profile)}<ul>${Object.entries(provenance.origins||{}).map(([key,origin])=>`<li>${esc(key.replaceAll('_',' '))}: ${esc(scOrigins[origin]||origin)}</li>`).join('')}</ul><p class="note">${esc(provenance.version)} · ${esc(provenance.mode)} scope · evidence ${esc(provenance.source_revision?.slice(0,12))}</p></details>`;
}

export function initScenarioWorkspace({dialog,root,call,onReturn}){
  let context=null,evaluated=null,origins={},reusedFrom=null,apply=null,sequence=0,busy=false,dirty=false;
  const scEl=selector=>root.querySelector(selector);
  const message=(text,error=false)=>{const node=scEl('[data-sc-message]');if(node){node.textContent=text;node.hidden=!text;node.className='message '+(error?'error':'info');}};
  const values=()=>Object.fromEntries(scenarioFields.filter(([key])=>mode()==='operating'||!scOperating.has(key)).map(([key])=>[key,scEl('form').elements[key].value===''?null:Number(scEl('form').elements[key].value)]));
  const mode=()=>scEl('[name="scope"]').value;
  const scope=()=>{
    for(const node of root.querySelectorAll('[data-sc-operating]'))node.hidden=mode()!=='operating';
    scEl('[data-sc-scope]').textContent=mode()==='cash'?'Cash scope: outbound includes packaging; repair includes paid labour. Operator opportunity time and capacity are excluded.':'Operating scope: outbound excludes packaging; repair excludes operator time. Work, packaging, reserves, overhead and shared capacity are separate assumptions.';
  };
  const result=()=>{
    const calculation=evaluated?.calculation;
    scEl('[data-sc-apply]').disabled=!apply||evaluated?.status!=='COMPLETE';
    scEl('[data-sc-result]').innerHTML=calculation?`<div class="metric-list">${metric('Base assumption profit',eur(calculation.results.base_profit_eur))}${metric('Downside assumption profit',eur(calculation.results.downside_profit_eur))}${metric('Maximum buy for target',calculation.results.max_buy_eur<0?'No feasible price':eur(calculation.results.max_buy_eur))}${calculation.operating?metric('Assumed units / month',num(calculation.operating.units_per_month,2))+metric('Assumed monthly profit',eur(calculation.operating.monthly_profit_eur)):''}</div><p class="note">${esc(calculation.version)} · explicit assumptions. Shared capacity across products cannot be added.</p>`:`<p class="note">${evaluated?.unknown_inputs.length?'Unknown assumptions: '+esc(evaluated.unknown_inputs.join(', '))+'.':'Evaluate these inputs against the current evidence.'} No result is calculated from missing costs.</p>`;
    for(const [key] of scenarioFields){const note=scEl('[data-sc-origin="'+key+'"]');if(note)note.textContent=values()[key]==null?'Unknown':scOrigins[origins[key]]||'Your assumption';}
  };
  const library=()=>{
    const selected=scEl('[data-sc-library]').value;
    scEl('[data-sc-library]').innerHTML='<option value="">Choose a saved copy</option>'+context.library.entries.map(row=>`<option value="${esc(row.id)}">${esc(row.name)} · ${row.mode} · ${date(row.updated_at)}</option>`).join('');
    scEl('[data-sc-library]').value=selected;
    scEl('[data-sc-library-note]').textContent=context.library.truncated?'Only the first '+context.library.entries.length+' of '+context.library.total+' saved copies are shown.':'';
  };
  const fill=(input,selectedMode='cash',inputOrigins={})=>{
    scEl('[name="scope"]').value=selectedMode;
    for(const [key] of scenarioFields)scEl('form').elements[key].value=input[key]??'';
    origins={...inputOrigins};evaluated=null;scope();result();
  };
  const draw=()=>{
    const input=([key,label])=>`<label class="field"${scOperating.has(key)?' data-sc-operating hidden':''}>${esc(label)}<input name="${key}" type="number" min="0" max="${key==='fee'?'99.99':key==='haircut'?'50':key==='days'?'36500':'1000000000'}" step="${['days','storage_units'].includes(key)?'1':'any'}" placeholder="Unknown"><span class="note" data-sc-origin="${key}">Unknown</span></label>`;
    root.innerHTML=`<div class="card-heading"><div><h2>Profiles & reusable scenarios</h2><p>${esc(context.title)}</p></div><button class="btn small" type="button" data-sc-discard>Close and discard draft</button></div><div class="message" role="status" data-sc-message hidden></div><div data-sc-profile>${profileSummary(context.profile)}</div><div data-sc-comparison>${scenarioComparison(context.actual_comparison)}</div>${scenarioProvenance(context.selected_scenario)}<div class="unit-actions"><button class="btn small" type="button" data-sc-refresh>Refresh evidence</button><button class="btn small" type="button" data-sc-return>Return to item workspace</button></div><section><h3>Saved assumption copies</h3><label class="field">REOPEN<select data-sc-library></select></label><button class="btn small" type="button" data-sc-reuse>Copy selected assumptions</button><p class="note" data-sc-library-note></p><div data-sc-copy-profile></div></section><form data-sc-form><label class="field">COST SCOPE<select name="scope"><option value="cash">Whole-unit cash assumptions</option><option value="operating">Operating costs and shared capacity</option></select></label><p class="note" data-sc-scope></p><div class="form-grid">${scenarioFields.map(input).join('')}</div><div class="unit-actions"><button class="btn primary" type="submit">Evaluate current evidence</button><button class="btn small" type="button" data-sc-apply disabled>Use in originating calculator</button></div></form><div data-sc-result aria-live="polite"></div><section><h3>Save a reusable copy</h3><label class="field">NEW COPY NAME<input data-sc-name maxlength="50" placeholder="Name these assumptions"></label><button class="btn small" type="button" data-sc-save${context.can_save?'':' disabled'}>Save new named copy</button><p class="note">Copies are stored in saved research sessions. A used name cannot replace an earlier revision. ${context.can_save?'Blank inputs can be saved, but remain unknown.':'This source is no longer retained; its original decision remains readable.'}</p></section>`;
    library();scope();result();
  };
  const post=async(action)=>{
    const response=await call('/api/research/scenarios',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params:{seed:context.seed,revision:context.revision,values:values(),mode:mode(),origins:Object.fromEntries(Object.entries(origins).filter(([key])=>Object.hasOwn(values(),key))),reused_from:reusedFrom,...(action==='save'?{name:scEl('[data-sc-name]').value}: {})}})});
    return response;
  };
  const open=async(seed,options={})=>{
    if(busy)return false;
    const same=context&&JSON.stringify(Object.fromEntries(Object.entries(context.seed).map(([key,value])=>[key,String(value)])))===JSON.stringify(Object.fromEntries(Object.entries(seed).map(([key,value])=>[key,String(value)])));
    if(same){dialog.showModal();if(options.onApply){apply=options.onApply;if(!dirty&&options.inputs){fill(options.inputs,options.mode||'cash',options.origins||{});reusedFrom=options.reusedFrom||null;}result();}return true;}
    if(dirty){dialog.showModal();message('Save this copy or close and discard its draft before opening another item.',true);return false;}
    const ticket=++sequence;apply=options.onApply||null;
    dialog.showModal();root.innerHTML='<p class="note">Reading the product profile and saved assumptions…</p>';
    try{
      const next=await call('/api/research/scenarios?'+new URLSearchParams(seed));if(ticket!==sequence)return false;
      context=next;draw();
      const retained=context.selected_scenario;
      const initial=options.inputs||retained?.inputs||{buy:context.source.listing?.listed_price_eur??null};
      fill(initial,options.mode||(retained?.version==='cost-scenario-v2'?'operating':'cash'),options.origins||Object.fromEntries(Object.keys(initial).map(key=>[key,retained?'saved_assumption':key==='buy'&& !options.inputs?'observed_ask':'operator_assumption'])));
      dirty=false;reusedFrom=options.reusedFrom||null;return true;
    }catch(error){root.innerHTML='<p class="message error"></p>';root.firstElementChild.textContent=error.message;return false;}
  };
  root.addEventListener('input',event=>{if(!event.target.closest('[data-sc-form]')||event.target.name==='scope')return;dirty=true;evaluated=null;origins[event.target.name]='operator_assumption';result();});
  root.addEventListener('change',event=>{
    if(event.target.name!=='scope')return;
    // Aggregated cash costs cannot silently become separately scoped costs.
    for(const key of ['outbound','repair',...scOperating])scEl('form').elements[key].value='';
    origins=Object.fromEntries(Object.entries(origins).filter(([key])=>mode()==='operating'||!scOperating.has(key)));dirty=true;evaluated=null;scope();result();message('Cost scope changed. Re-enter outbound, repair and operating costs to avoid counting a cost twice.');
  });
  root.addEventListener('submit',async event=>{
    if(!event.target.matches('[data-sc-form]'))return;event.preventDefault();if(busy||!event.target.reportValidity())return;
    busy=true;root.inert=true;
    try{evaluated=await post('evaluate');origins=evaluated.origins;result();message(evaluated.status==='COMPLETE'?'Evaluated against current item and profile evidence.':'Inputs retained; incomplete costs withhold the result.');}
    catch(error){evaluated=null;result();message(error.message,true);}
    finally{busy=false;root.inert=false;}
  });
  root.addEventListener('click',async event=>{
    if(busy||!context)return;
    const target=event.target;
    if(target.closest('[data-sc-discard]')){sequence++;dirty=false;context=null;evaluated=null;dialog.close();return;}
    if(target.closest('[data-sc-return]')){dialog.close();onReturn?.(context.seed);return;}
    if(target.closest('[data-sc-reuse]')){
      const entry=context.library.entries.find(row=>row.id===scEl('[data-sc-library]').value);if(!entry)return;
      scEl('[data-sc-copy-profile]').innerHTML='<h4>Profile retained with this copy</h4>'+profileSummary(entry.profile);fill(entry.inputs,entry.mode,Object.fromEntries(Object.keys(entry.inputs).map(key=>[key,'saved_assumption'])));reusedFrom={id:entry.id,revision:entry.revision};dirty=true;
      message('Copied '+entry.name+'. Values remain assumptions for this item. Evaluate again before applying them.');return;
    }
    if(target.closest('[data-sc-apply]')){
      if(!evaluated||evaluated.status!=='COMPLETE'||!apply)return;
      busy=true;root.inert=true;try{await apply(evaluated);dirty=false;dialog.close();}catch(error){message(error.message,true);}finally{busy=false;root.inert=false;}return;
    }
    if(target.closest('[data-sc-save]')){
      if(!scEl('form').reportValidity())return;busy=true;root.inert=true;
      try{const saved=await post('save');context.library=saved.library;evaluated=saved.scenario;origins=evaluated.origins;dirty=false;library();result();message('Named scenario saved. The original decision was not changed.');}
      catch(error){message(error.message,true);}finally{busy=false;root.inert=false;}return;
    }
    if(target.closest('[data-sc-refresh]')){
      busy=true;root.inert=true;
      try{const next=await call('/api/research/scenarios?'+new URLSearchParams(context.seed));const changed=context.revision!==next.revision;context=next;scEl('[data-sc-profile]').innerHTML=profileSummary(context.profile);scEl('[data-sc-comparison]').innerHTML=scenarioComparison(context.actual_comparison);scEl('[data-sc-save]').disabled=!context.can_save;library();if(changed){evaluated=null;result();message('Evidence changed. Your inputs are kept; evaluate again before applying them.');}else message('Evidence is current. Inputs are retained.');}
      catch(error){message(error.message,true);}finally{busy=false;root.inert=false;}
    }
  });
  return {open,isBusy:()=>busy};
}
