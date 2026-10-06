import {esc,date,eur,num} from '../format.js';
import {badge,empty,metric} from '../components.js';
import {profileSummary,scenarioProvenance} from './scenarios.js';
import {identitySummary} from './research.js';
import {outcomeSummary} from './outcomes.js';
import {sourceReviewContext,sourceReviewHistory} from './source-review.js';
import {forecastEvidence} from './forecasts.js';

const tabs={source:'Source evidence',decisions:'Decisions & scenarios',owned:'Owned item & outcomes',history:'Retained history'};
const name=value=>String(value||'Unknown').replaceAll('_',' ');
const action=(label,type,id='',panel='')=>`<button class="btn small" type="button" data-item-action="${esc(type)}" data-item-id="${esc(id)}" data-item-panel="${esc(panel)}">${esc(label)}</button>`;

export function workspaceNavigation(data,{research=true}={}){
  return `<nav class="unit-actions" aria-label="Continue this item">${research&&data.source.retained?action('Research offer','research'):''}${data.owned?action('Owned inventory','owned')+action('Orders & returns','sale'):''}${action('Pilot review','pilot')}${action('Capture sold aggregate','sold_capture')}</nav>`;
}

export function workspaceDecisions(data){
  return `<p class="note">Each decision retains its original evidence, checks and assumptions. Source overlap does not attribute an acquisition.</p>${data.decisions.length?data.decisions.map(row=>{
    const link=row.relation==='receipt_linked'?badge('Receipt linked','green'):badge(row.relation==='invalid_receipt'?'Receipt needs review':'Source context','amber');
    return `<article class="item-record${row.decision_pk===data.selected_decision_pk?' is-selected':''}"><div class="card-heading"><h3>Decision #${row.decision_pk} · ${esc(name(row.decision))}</h3>${link}</div><p class="note">Recorded ${date(row.recorded_at)} · retained snapshot</p><p>${esc(row.notes||'No notes recorded.')}</p>${row.snapshot_status==='unreadable'?'<p class="message error">The retained snapshot is unreadable. Current evidence cannot replace it.</p>':`<div class="metric-list">${metric('Ask at decision time',eur(row.evidence?.ask_eur))}${metric('Saved base scenario result',eur(row.scenario?.results?.base_profit_eur))}${metric('Scenario version',esc(row.scenario?.version||'No scenario attached'))}</div>`}${scenarioProvenance(row.scenario)}${row.acquisition_links.map(link=>`<p class="note">${esc(link.reason)}</p>`).join('')}<div class="unit-actions">${action('Open retained decision','decision',row.decision_pk)}${action('Reopen scenario copy','scenario',row.decision_pk)}${action('Record actual acquisition','acquire',row.decision_pk)}${action('Record declined / failed attempt','attempt',row.decision_pk)}</div></article>`;
  }).join(''):empty('No retained decisions','Review the source, inspect the offer and record your own decision.','search')}`;
}

export function workspaceOwned(data){
  if(!data.owned)return `<p class="note">${data.purchase?'A purchase is retained, but no physical SKU has been recorded.':'No acquisition is recorded for this source.'} Research assumptions are separate from amounts actually paid.</p>${data.purchase?action('Record the acquired physical item','acquire'):data.source.retained?action('Record actual acquired item','acquire'):''}`;
  const unit=data.owned;
  return `<div class="card-heading"><h3>${esc(unit.title)}</h3>${badge(name(unit.stock_phase||unit.state))}</div><p class="note">${esc(unit.sku)} · item revision ${unit.version} · facts revision ${unit.facts_version}</p><p class="note">${unit.latest_inspection?'Latest inspection recorded '+date(unit.latest_inspection.recorded_at):'No physical inspection recorded.'} Source reviews do not establish physical inspection.</p><div class="unit-actions">${action('Edit owned facts','owned')}${action('Record physical inspection','owned','','inspection')}${action('Outcome evidence','outcome','','review')}${action('Prepare listing draft','new_draft')}${action('Record actual sale / return','sale')}</div>${data.outcome?outcomeSummary(data.outcome):''}<h3>Missing evidence and next actions</h3>${unit.local_preview.status!=='allowed'?`<div class="item-record"><p>Physical item needs review</p><p class="note">${unit.local_preview.reasons.map(row=>esc(row.message)).join(' · ')}</p>${action('Review owned facts and inspection','owned','','inspection')}</div>`:''}${data.evidence_steps.map(row=>`<div class="item-record"><strong>${esc(row.label)}</strong><p class="note">${row.reasons.map(reason=>esc(name(reason))).join(' · ')}</p>${action(row.label,'outcome','',row.panel)}</div>`).join('')}<h3>Saved listing drafts</h3>${data.drafts.length?data.drafts.map(row=>`<div class="item-record"><p>${esc(name(row.channel))} · draft revision ${row.version} ${badge(row.binding_current?'Bound to this unit revision':'Earlier unit revision',row.binding_current?'':'amber')}</p>${action('Open saved draft','draft',row.draft_id)}</div>`).join(''):'<p class="note">No saved listing draft.</p>'}<h3>Actual sale and return cycles</h3>${data.orders.length?data.orders.map(row=>`<div class="item-record"><p>Cycle ${row.cycle} · ${esc(name(row.channel))} · ${esc(name(row.fulfilment))} · sale recorded for ${date(row.sold_at)}</p><p class="note">${row.resale_pairing?.listing_id?'Resale listing #'+esc(row.resale_pairing.listing_id):'Resale pairing is unavailable or unpaired; acquisition source is separate.'}</p>${action('Review order / return','order',row.order_id)}</div>`).join(''):'<p class="note">No managed sale cycle recorded.</p>'}`;
}

export function workspaceHistory(data){
  return `<p class="note">${esc(data.history.note)}</p>${data.history.items.length?`<ol class="item-timeline">${data.history.items.map(row=>`<li><div class="card-heading"><strong>${esc(row.label)}</strong>${badge(name(row.evidence_class))}</div><p class="note">Recorded ${date(row.recorded_at)}${row.occurred_at?' · observed '+date(row.occurred_at):''}</p>${row.evidence_ref?`<p class="note">Private reference: ${esc(row.evidence_ref)}</p>`:''}${row.action?action('Open evidence',row.action.type,row.action.id,row.action.panel):''}</li>`).join('')}</ol>`:empty('No retained operator history','Reading this workspace does not create evidence.','file')}${data.history.truncated?'<p class="note">Only the latest retained events are shown. Older evidence remains available in its original editor.</p>':''}`;
}

export function workspaceView(data,selected='source'){
  const source=data.source,context=source.context;
  const panels={
    source:context?`${profileSummary(data.profile)}${sourceReviewContext(context)}${identitySummary({identity:context.current_identity,listing:source.listing,listing_analysis:context.current_identity.listing_analysis})}<div class="unit-actions">${action(context.can_review?'Review source identity':'View protected source review','source_review')}${action('Research this offer','research')}${action('Profiles & reusable scenarios','scenario')}${action('Inspect retained observation','listing')}</div><details class="source-review-history"><summary>Earlier source reviews</summary>${sourceReviewHistory(context)}</details>`:`<p class="message info">${source.listing_id?'The market source is no longer retained locally. Saved decisions and owned receipts remain available.':'This owned item has no marketplace source ID.'} No current source facts have been reconstructed.</p>${action('Profiles & retained scenarios','scenario')}`,
    decisions:workspaceDecisions(data),owned:workspaceOwned(data),history:workspaceHistory(data)
  };
  panels.source+=workspaceNavigation(data,{research:false})+forecastEvidence(data.forecasts);
  return `<section class="card card-pad item-summary"><div class="card-heading"><div><div class="eyebrow">One item · connected evidence</div><h2>${esc(data.title)}</h2><p class="note">${source.listing_id?esc(name(source.platform))+' / #'+esc(source.listing_id):'Offline / no source ID'}${data.owned?' · '+esc(data.owned.sku):''}</p></div><button class="btn small" type="button" data-item-refresh>Refresh context</button></div><div class="metric-list">${metric('Source',source.retained?'Retained locally':'Unavailable locally')}${metric('Decisions',num(data.coverage.totals.decisions)+' retained')}${metric('Physical item',data.owned?esc(name(data.owned.stock_phase||data.owned.state)):'No owned SKU')}${metric('Outcome scope',data.outcome?.time_scope_complete?'Reviewed':'Not reviewed')}</div><p class="note">Read current source evidence alongside saved decisions and actual owned outcomes. Each editor keeps its own evidence and save checks.</p>${data.coverage.truncated.length?`<p class="note">History is bounded to ${data.coverage.limit} records per section. More ${esc(data.coverage.truncated.join(', '))} remain in their original views.</p>`:''}</section><div class="message info" data-item-notice role="status" hidden></div><section class="card card-pad"><div class="item-tabs" role="tablist" aria-label="Item evidence">${Object.entries(tabs).map(([key,label])=>`<button class="btn small" role="tab" type="button" id="item-tab-${key}" aria-controls="item-panel-${key}" aria-selected="${selected===key}" tabindex="${selected===key?0:-1}" data-item-tab="${key}">${label}</button>`).join('')}</div>${Object.entries(panels).map(([key,html])=>`<section class="item-panel" role="tabpanel" id="item-panel-${key}" aria-labelledby="item-tab-${key}"${selected===key?'':' hidden'}>${html}</section>`).join('')}</section>`;
}

/** This reader never mounts or replaces an editor's form. */
export function initItemWorkspace({root,call,onAction,onContext,onUpdated}){
  let data=null,seed=null,selected='source',sequence=0,controller=null,pending=null;
  const select=key=>{
    if(!tabs[key])return;selected=key;
    for(const node of root.querySelectorAll('[data-item-tab]')){const chosen=node.dataset.itemTab===key;node.setAttribute('aria-selected',String(chosen));node.tabIndex=chosen?0:-1;}
    for(const node of root.querySelectorAll('[role="tabpanel"]'))node.hidden=node.id!=='item-panel-'+key;
  };
  const feedback=(text,error=false)=>{let node=root.querySelector('[data-item-notice]');if(!node){node=document.createElement('p');node.dataset.itemNotice='';root.prepend(node);}node.textContent=text;node.hidden=!text;node.className='message '+(error?'error':'info');node.setAttribute('role',error?'alert':'status');};
  const read=async(next,opening)=>{
    const stamp=++sequence;controller?.abort();controller=new AbortController();
    const focused=document.activeElement,hadFocus=root.contains(focused);
    root.setAttribute('aria-busy','true');root.inert=true;
    try{
      const value=await call('/api/item-workspace?'+new URLSearchParams(next),{signal:controller.signal});
      if(stamp!==sequence)return false;
      const changed=data&&JSON.stringify(seed)===JSON.stringify(next)&&data.revision!==value.revision;
      const same=seed&&JSON.stringify(seed)===JSON.stringify(next);
      seed={...next};data=value;
      if(!same)selected=next.sku||next.draft_id||next.order_id?'owned':next.decision_pk?'decisions':'source';
      if(opening||changed||!root.querySelector('[data-item-tab]')){
        const expanded=[...root.querySelectorAll('details')].map(node=>node.open);
        root.innerHTML=workspaceView(data,selected);
        if(same)root.querySelectorAll('details').forEach((node,index)=>node.open=expanded[index]||false);
      }
      if(changed)feedback('Evidence changed since the previous reading. Open editors keep their displayed values and revisions; review changes before saving.');
      else if(!opening)feedback('');
      onContext?.(data);onUpdated?.(data);return true;
    }catch(error){if(error.name!=='AbortError'&&stamp===sequence)feedback('Could not refresh item context. The last successful reading remains displayed. '+error.message,true);return false;}
    finally{if(stamp===sequence){root.inert=false;root.setAttribute('aria-busy','false');if(hadFocus)(focused.isConnected?focused:root.querySelector('#item-tab-'+selected))?.focus();}}
  };
  const open=next=>{pending=read(next,true);return pending;};
  const refresh=()=>{if(!seed)return Promise.resolve(false);if(root.getAttribute('aria-busy')==='true')return pending||Promise.resolve(false);pending=read(seed,false);return pending;};
  root.addEventListener('click',event=>{
    const tab=event.target.closest('[data-item-tab]');if(tab){select(tab.dataset.itemTab);return;}
    if(event.target.closest('[data-item-refresh]')){refresh();return;}
    const button=event.target.closest('[data-item-action]');if(button&&data)Promise.resolve(onAction?.({type:button.dataset.itemAction,id:button.dataset.itemId,panel:button.dataset.itemPanel},data)).catch(error=>feedback(error.message,true));
  });
  root.addEventListener('keydown',event=>{
    const node=event.target.closest('[data-item-tab]');if(!node||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();const keys=Object.keys(tabs),index=keys.indexOf(selected),key=event.key==='Home'?keys[0]:event.key==='End'?keys.at(-1):keys[(index+(event.key==='ArrowRight'?1:keys.length-1))%keys.length];select(key);root.querySelector('#item-tab-'+key).focus();
  });
  root.innerHTML=empty('Choose an item to connect its evidence','Open Item workspace from Price Research, Observations, a decision or Owned inventory.','search');
  return {open,refresh,data:()=>data,seed:()=>seed,isBusy:()=>root.getAttribute('aria-busy')==='true'};
}
