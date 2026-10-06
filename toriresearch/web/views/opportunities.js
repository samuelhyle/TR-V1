import {date,esc,eur,num,percent,plural} from '../format.js';
import {badge,empty,metric} from '../components.js';
import {listingAnalysisSummary} from './research.js';

export const OPPORTUNITY_DRAFT_KEY = 'tori.opportunity-drafts.v1';
const CHANNELS = ['tori','offline','ebay','tradera','other'];
const opFields=[['resale','Resale proceeds assumption (€)'],['buy','Purchase price (€)'],['inbound','Inbound cost (€)'],['fee','Selling fee (%)'],['outbound','Outbound cost (€)'],['repair','Repair cost (€)'],['other','Other cost / tax (€)'],['target','Required downside profit (€)'],['days','Holding / cash-cycle assumption (days)'],['carrying','Carrying cost per day (€)'],['haircut','Downside resale haircut (%)'],['labour_hours','Total work per unit (hours)'],['hourly_rate','Operator time value (€ / hour)'],['packaging','Packaging cost (€)'],['return_loss','Return / loss reserve (€)'],['overhead','Allocated overhead (€)'],['cash_budget','Deployable cash (€)'],['monthly_hours','Available monthly work (hours)'],['storage_units','Storage capacity (units)'],['source_units_month','Assumed acquisitions per month'],['sale_units_month','Assumed sales per month']];

/** Only assumptions are persisted. Numerical results are re-evaluated by the server. */
export function readOpportunityDrafts(storage) {
  try {
    const saved=JSON.parse(storage.getItem(OPPORTUNITY_DRAFT_KEY));
    if(saved?.version!==1||!saved.drafts||typeof saved.drafts!=='object'||Array.isArray(saved.drafts))return {};
    return Object.fromEntries(Object.entries(saved.drafts).filter(([key,draft])=>
      key.length<=256&&draft&&typeof draft.revision==='string'&&CHANNELS.includes(draft.exit_channel)&&
      draft.inputs&&typeof draft.inputs==='object'&&opFields.every(([name])=>
        draft.inputs[name]===null||(typeof draft.inputs[name]==='number'&&Number.isFinite(draft.inputs[name])&&
        draft.inputs[name]>=0&&draft.inputs[name]<=1e9))).slice(-50).map(([key,draft])=>[key,{
      revision:draft.revision,exit_channel:draft.exit_channel,evaluated:draft.evaluated===true,
      inputs:Object.fromEntries(opFields.map(([name])=>[name,draft.inputs[name]])),
      ...(draft.origins&&typeof draft.origins==='object'?{origins:Object.fromEntries(opFields.filter(([name])=>typeof draft.origins[name]==='string').map(([name])=>[name,draft.origins[name]]))}:{}),
      ...(draft.reused_from&&typeof draft.reused_from.id==='string'&&typeof draft.reused_from.revision==='string'?{reused_from:{id:draft.reused_from.id,revision:draft.reused_from.revision}}:{})
    }]));
  } catch { return {}; }
}

export function writeOpportunityDrafts(storage,drafts) {
  try {storage.setItem(OPPORTUNITY_DRAFT_KEY,JSON.stringify({version:1,drafts:Object.fromEntries(Object.entries(drafts).slice(-50))}));return true;}
  catch {return false;}
}

export function currentOpportunityScenarios(report,drafts) {
  return Object.fromEntries((report.rows||[]).filter(row=>{
    const draft=drafts[row.row_id];
    return row.comparison_supported&&draft?.evaluated&&draft.revision===row.evidence_revision&&
      opFields.every(([name])=>draft.inputs[name]!=null);
  }).slice(0,50).map(row=>[row.row_id,{revision:row.evidence_revision,exit_channel:drafts[row.row_id].exit_channel,
    inputs:drafts[row.row_id].inputs}]));
}

export function opportunityResearchGroups(rows) {
  const groups=new Map();
  for(const row of rows){
    const key=JSON.stringify([row.product.model_key||row.product.label,row.market.platform,row.market.subplatform]);
    if(!groups.has(key))groups.set(key,{label:row.product.label,rows:[]});
    groups.get(key).rows.push(row);
  }
  return [...groups.values()];
}

function researchTable(rows) {
  const offerHtml=(row,ref)=>'<div class="op-research-offer"><strong>'+eur(ref.ask_eur)+'</strong> · '+
    esc(row.market.municipality_norm||'Location unknown')+' · '+esc(row.market.seller_type_norm||'Seller type unknown')+
    '<div class="note">'+esc(ref.title||row.product.label)+' · Price observed '+date(ref.price_observed_at)+'</div>'+
    (ref.listing_analysis ? '<details><summary>Automatic product details</summary>'+listingAnalysisSummary(ref.listing_analysis)+'</details>' : '')+
    '<a href="'+esc(ref.url)+'" target="_blank" rel="noopener noreferrer">Marketplace offer '+esc(ref.listing_id)+'</a> '+
    '<button class="btn small" type="button" data-op-research="'+esc(ref.listing_pk)+'">Research this offer</button> '+
    '<button class="btn small" type="button" data-forecast-sources="'+esc(ref.listing_pk)+'" data-forecast-label="'+esc(row.product.label)+'">Forecast evidence</button></div>';
  return '<p class="note">Research queue: individual observed offers grouped by model mention. Exact variants and offer contents still need review. Profit, cash recovery and work-hour earnings are unknown.</p>'+
    '<table><thead><tr><th>Product to investigate</th><th>Current offers and asking prices</th><th>Next step</th></tr></thead><tbody>'+
    opportunityResearchGroups(rows).map(group=>{
      const offers=group.rows.flatMap(row=>(row.source_refs||[]).map(ref=>offerHtml(row,ref)));
      return '<tr><td><strong>'+esc(group.label)+'</strong><p class="note">'+
        num(group.rows.length)+' individual research offers in these results</p></td><td>'+offers.slice(0,3).join('')+
        (offers.length>3?'<details class="op-more-offers"><summary>Show '+num(offers.length-3)+' more offers</summary>'+offers.slice(3).join('')+'</details>':'')+
        '</td><td>Verify the exact variant and offer contents.<p class="note">'+esc([...new Set(group.rows.flatMap(row=>
          row.product.missing_identity||[]))].map(key=>key.replaceAll('_',' ')).join(', '))+'</p></td></tr>';
    }).join('')+'</tbody></table>';
}


const ATTRIBUTE_LABELS={brand:'Brand',model:'Model',storage:'Storage',hardware_code:'Hardware code',
  condition:'Condition',quantity:'Units in the offer',battery_health_pct:'Battery health',
  battery_presence:'Battery',mains_powered:'Mains powered',lock_applicable:'Lock',
  functionality:'Functionality',completeness:'Completeness',defects:'Disclosed defects',
  offer_kind:'Offer kind',mount:'Mount',edition:'Edition',focal_length:'Focal length'};
const CHANNEL_LABELS={tori:'Tori',ebay:'eBay',tradera:'Tradera',offline:'Offline',other:'Other'};
const channel=name=>CHANNEL_LABELS[name]||name;
const attributeValue=(key,value)=>{
  if(key.endsWith('_pct'))return num(value)+'%';
  if(typeof value!=='string')return String(value);
  return value.replaceAll('_',' ').replace(/^\w/,letter=>letter.toUpperCase());
};

export function opportunityRows(report, objective='margin', basis='observed') {
  const ranks = basis==='scenario' ? report.scenario_rankings?.[objective] : report.rankings?.[objective];
  const positions = new Map((ranks||[]).map((row,index)=>[row.row_id,index]));
  // Ranks come first in rank order; everything unranked keeps the board's own
  // curated evidence order instead of falling back to a stable but meaningless
  // row-id sort, which made the table look randomly shuffled.
  return [...(report.rows||[])]
    .map((row,index)=>({row,index}))
    .sort((a,b)=>(positions.get(a.row.row_id)??Infinity)-(positions.get(b.row.row_id)??Infinity)
      || a.index-b.index
      || a.row.row_id.localeCompare(b.row.row_id))
    .map(({row})=>row);
}

const evidenceItem=(label,body)=>'<p><strong>'+esc(label)+'</strong>'+(body||'Not recorded for this row.')+'</p>';

export function opportunityTable(report, objective='margin', basis='observed', open=new Set()) {
  const rows=opportunityRows(report,objective,basis);
  if(!rows.length)return empty('No identifiable product','No identifiable product is available in this scope. Review product identity and offer contents in Price research.','search');
  const ranking=basis==='scenario'?report.scenario_rankings?.[objective]:report.rankings?.[objective];
  const notice=ranking?.length?'':'<p class="note">No supported '+esc(objective.replaceAll('_',' '))+' ranking on this basis. Missing values remain unknown.</p>';
  if(rows.every(row=>!row.comparison_supported&&!row.owned.n_units))return notice+researchTable(rows);
  const heads=['What to search for','Market and delivery','Observed asks','Buy ceiling','Profit / margin / work hour','Cash recovery','Quantity','Evidence'];
  const table=notice+'<table class="opportunity-table"><thead><tr>'+heads.map(head=>'<th scope="col">'+head+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>{
    const product=row.product,owned=row.owned,scenario=row.scenario;
    const margin=basis==='scenario'?scenario?.margin_on_proceeds_pct:owned.margin_on_proceeds_pct;
    const hourly=basis==='scenario'?scenario?.operating?.profit_per_labour_hour_eur:owned.economic_profit_per_work_hour_eur;
    const roi=basis==='scenario'?scenario?.roi_on_total_cost_pct:owned.roi_on_actual_cost_pct;
    const profit=basis==='scenario'?scenario?.results?.downside_profit_eur:
      margin!=null&&owned.n_units>0&&owned.complete_cash_contribution_total_eur!=null?owned.complete_cash_contribution_total_eur/owned.n_units:null;
    const attrs=Object.entries(product.attributes||{}).filter(([,value])=>value!=null).map(([key,value])=>esc(ATTRIBUTE_LABELS[key]||key.replaceAll('_',' '))+': '+esc(attributeValue(key,value))).join(' · ');
    const terms=(product.search_terms||[]).filter(term=>term&&term!==product.label);
    const market=row.market,shipping=market.has_shipping==null?'Shipping unknown':market.has_shipping?'Shipping offered':'Collection only';
    const cash=basis==='scenario'?(scenario?.inputs?.days!=null?num(scenario.inputs.days)+' days (assumed cycle)':'Cash-cycle assumption unknown'):
      owned.median_cash_recovery_days==null?'Unknown':num(owned.median_cash_recovery_days,1)+' days';
    const quantity=basis==='scenario'?(scenario?num(scenario.operating.units_per_month,2)+' / month (scenario)':'Unknown scenario'):plural(owned.recorded_sold_units,'recorded sale');
    const sources=row.source_refs.map(ref=>'<a href="'+esc(ref.url)+'" target="_blank" rel="noopener noreferrer">Listing '+esc(ref.listing_id)+'</a>'+
      (ref.listing_pk?' <button class="btn small" type="button" data-op-research="'+esc(ref.listing_pk)+'">Research</button>':'')).join(' · ');
    const asks=row.source_refs.filter(ref=>ref.ask_eur!=null).map(ref=>eur(ref.ask_eur)).join(' · ');
    const marker=row.public_sale_evidence?.sell_through_by_evidence?.sold_inferred?.estimates?.[report.horizon+'d'];
    const markerText=marker?'Seller-declared marker incidence: '+percent(marker.share*100)+' over '+num(report.horizon)+' days; '+num(marker.n_followed_to_horizon)+' followed; 95% interval '+percent(marker.ci95.lo*100)+'–'+percent(marker.ci95.hi*100):'Seller-declared marker incidence withheld: event or horizon support missing.';
    const missing=row.abstentions.map(reason=>esc(reason.replaceAll('_',' ')));
    const evidenceId='opp-evidence-'+row.row_id;
    const isOpen=open.has(row.row_id);
    const state=row.comparison_supported?badge('Reviewed offer','green'):badge('Needs identity / offer review','amber');
    const panel='<tr class="opp-evidence-row"'+(isOpen?'':' hidden')+'><td colspan="'+heads.length+'"><div class="opp-evidence" id="'+esc(evidenceId)+'">'+
      evidenceItem('Next step',esc(row.next_action))+
      evidenceItem('Why this row is not fully ranked',missing.length?missing.join('; '):'Nothing outstanding.')+
      evidenceItem('Individual source offers',sources||'No stored source offers.')+
      evidenceItem('Retained forecast evidence',row.source_refs.length?'<button class="btn small" type="button" data-forecast-sources="'+esc(row.source_refs.map(ref=>ref.listing_pk).join(','))+'" data-forecast-label="'+esc(product.label)+'">Inspect individual offer forecasts</button> '+num(row.source_refs.length)+' source references shown'+(row.source_ref_coverage?.truncated?' of '+num(row.source_ref_coverage.total):'')+'. Outputs are not pooled into product accuracy.':'No retained marketplace source.')+
      evidenceItem('Asking prices are supply context','Ask sample: '+num(row.asking_prices.n_priced)+'; median '+(row.asking_prices.status==='OK'?eur(row.asking_prices.median_eur):'withheld')+'.')+
      evidenceItem('Seller-declared sale markers',esc(markerText)+' This measures observed markers; actual transaction time is unknown.')+
      evidenceItem('Owned margin totals',eur(owned.complete_cash_contribution_total_eur)+(owned.totals_are_partial?' (partial)':'')+'; '+num(owned.n_complete_costs)+' complete / '+plural(owned.n_units,'linked unit')+'.')+
      evidenceItem('Evidence policy','Policy: '+esc(row.comparison_policy_version)+'; revision '+esc(row.evidence_revision))+
      '</div></td></tr>';
    return '<tr><td><strong class="opp-product">'+esc(product.label)+'</strong>'+(attrs?'<div class="observation-meta">'+attrs+'</div>':'')+(terms.length?'<div class="note">'+esc(terms.join(' · '))+'</div>':'')+'</td>'+
      '<td>'+esc(channel(market.platform))+' / '+esc(market.subplatform)+'<div class="observation-meta">'+esc(market.municipality_norm||'Location unknown')+' · '+esc(market.seller_type_norm||'Seller type unknown')+' · '+esc(shipping)+'</div><div class="note">Observed resale channels: '+esc(owned.resale_channels.map(channel).join(', ')||'Unknown')+'</div></td>'+
      '<td>'+(asks||'No fresh observed ask')+'<div class="note">Individual source asks</div></td>'+
      '<td>'+(row.buy_ceiling_eur==null?'Unknown':(row.buy_ceiling_eur<0?'No feasible price':eur(row.buy_ceiling_eur))+'<div class="note">Explicit downside scenario · '+esc(channel(scenario.exit_channel))+'</div>')+'</td>'+
      '<td>'+(profit==null?'Profit unknown':eur(profit)+(basis==='scenario'?' downside / unit':' mean / linked unit'))+
      '<div class="note">Margin '+(margin==null?'Unknown':percent(margin))+' · ROI '+(roi==null?'Unknown':percent(roi))+'</div><div class="note">'+(hourly==null?'Work-hour earnings unknown':eur(hourly)+' / hour')+'</div></td>'+
      '<td>'+cash+'<div class="note">'+num(owned.n_cash_recovered)+' recovered / '+plural(owned.n_units,'linked unit')+'</div></td>'+
      '<td>'+quantity+'<div class="note">'+plural(row.quantity.unique_retained_offer_arrivals,'retained offer arrival')+' / '+num(report.days)+' days; '+num(owned.n_pending)+' owned pending</div></td>'+
      '<td><div class="opp-evidence-cell">'+state+'<button type="button" class="btn small" data-op-toggle="'+esc(row.row_id)+'" aria-expanded="'+(isOpen?'true':'false')+'" aria-controls="'+esc(evidenceId)+'">'+(isOpen?'Hide evidence':'Show evidence')+'</button></div></td></tr>'+panel;
  }).join('')+'</tbody></table>';
  return table;
}

export const initOpportunityBoard = async ({container,filters,call,onResearch=()=>{},onScenario=()=>{},storage}) => {
  if(!storage){try{storage=globalThis.localStorage;}catch{storage=null;}}
  let drafts=readOpportunityDrafts(storage),report,sequence=0,activeRow='',storageAvailable=!!storage;
  const openEvidence=new Set();
  const opGroups=[['Costs and proceeds (per unit)',['resale','buy','inbound','fee','outbound','repair','other','packaging','return_loss','carrying','haircut']],
    ['Your time and overhead',['labour_hours','hourly_rate','overhead']],
    ['Targets and shared resources',['target','days','cash_budget','monthly_hours','storage_units','source_units_month','sale_units_month']]];
  const opLabels=new Map(opFields);
  const input=name=>'<label class="field">'+esc(opLabels.get(name))+'<input type="number" name="'+name+'" min="0" max="'+(name==='haircut'?'50':name==='fee'?'99.99':'1000000000')+'" step="'+(['days','storage_units'].includes(name)?'1':'any')+'" required></label>';
  const opInputs=opGroups.map(([title,names])=>'<fieldset class="opp-form-group"><legend>'+esc(title)+'</legend><div class="form-grid">'+names.map(input).join('')+'</div></fieldset>').join('');
  container.innerHTML='<div class="form-grid"><label class="field">RANK BY<select data-op-objective><option value="margin">Margin</option><option value="euros_per_hour">Economic euros / work hour</option><option value="speed">Cash recovery speed</option><option value="quantity">Recorded sold quantity</option><option value="monthly_profit">Monthly profit scenario</option></select></label><label class="field">BASIS<select data-op-basis><option value="observed">Observed owned results</option><option value="scenario">Explicit scenarios</option></select></label></div>'+
    '<div class="opp-counts" data-op-counts></div><p class="note" data-op-note></p>'+
    '<details class="opp-selection"><summary>How these rows are ordered</summary><p class="note" data-op-selection></p></details>'+
    '<div role="alert" data-op-error hidden></div><div class="table-wrap" data-op-table></div>'+
    '<details data-op-calculator data-op-scenario-panel><summary>Evaluate a reviewed product scenario</summary><p class="note">Supply every resale, cost and resource assumption. Drafts are saved in this browser; results are re-evaluated against current evidence. Budgets are shared across products.</p><p class="note" data-op-availability></p><form data-op-form><label class="field">PRODUCT<select name="row" required></select></label><label class="field">EXIT CHANNEL<select name="exit_channel"><option value="tori">Tori</option><option value="offline">Offline</option><option value="ebay">eBay</option><option value="tradera">Tradera</option><option value="other">Other</option></select></label>'+opInputs+'<button class="btn primary" type="submit">Evaluate scenario</button> <button class="btn small" type="button" data-op-shared>Profiles & reusable scenarios</button> <button class="btn small" type="button" data-op-clear-draft>Clear this draft</button><div role="alert" data-op-form-error hidden></div></form><p class="note" role="status" data-op-draft-note></p><div data-op-scenario></div></details>';
  const element=selector=>container.querySelector(selector),form=element('[data-op-form]');
  const error=value=>{element('[data-op-error]').textContent=value;element('[data-op-error]').hidden=!value;};
  const row=()=>report?.rows.find(item=>item.row_id===form.elements.row.value);
  const persist=()=>{storageAvailable=writeOpportunityDrafts(storage,drafts);return storageAvailable;};
  const renderTable=()=>{
    if(!report)return;
    element('[data-op-table]').innerHTML=opportunityTable(report,element('[data-op-objective]').value,element('[data-op-basis]').value,openEvidence);
    element('[data-op-table]').dispatchEvent(new globalThis.CustomEvent('tori:table-layout',{bubbles:true}));
  };
  const renderScenario=()=>{
    const selected=row(),evaluated=selected?.scenario,draft=drafts[selected?.row_id];
    element('[data-op-draft-note]').textContent=draft
      ?draft.revision!==selected.evidence_revision?'Evidence changed; your inputs are kept. Evaluate again against current evidence.':
        evaluated?'Saved assumptions evaluated against current evidence.':'Draft saved. Evaluate to update the results.'
      :'';
    if(draft&&!storageAvailable)element('[data-op-draft-note]').textContent+=' Browser storage is unavailable; this draft is kept in this tab.';
    const money=value=>value!=null&&value<0?'<span class="negative">'+eur(value)+'</span>':eur(value);
    element('[data-op-scenario]').innerHTML=evaluated?'<div class="opp-scenario-result">'+
      metric('Downside profit',money(evaluated.results.downside_profit_eur))+
      metric('Maximum buy price',evaluated.results.max_buy_eur<0?'No feasible price':eur(evaluated.results.max_buy_eur))+
      metric('Units / month',num(evaluated.operating.units_per_month,2))+
      metric('Monthly profit',money(evaluated.operating.monthly_profit_eur))+'</div>'+
      '<p class="note">Single-product user scenario on the selected exit channel. Shared budgets cannot be added across products.</p>':'';
  };
  const restoreForm=()=>{
    activeRow=form.elements.row.value;
    const draft=drafts[activeRow];
    for(const [name] of opFields)form.elements[name].value=draft?.inputs[name]??'';
    form.elements.exit_channel.value=draft?.exit_channel||'tori';renderScenario();
  };
  const draw=()=>{
    element('[data-op-counts]').innerHTML=badge(num(report.rows.length)+' of '+num(report.total_rows)+' rows')+badge(num(report.days)+'-day arrival window')+badge(num(report.min_n)+' observations per supported rank')+badge(num(report.horizon)+'-day sale horizon');
    element('[data-op-selection]').textContent=report.selection_basis||'Supported ranks come first; other rows retain their evidence order.';
    element('[data-op-note]').textContent=report.total_rows+' product / market rows; showing '+report.rows.length+
      '. '+report.note;
    const previous=form.elements.row.value||activeRow,eligible=report.rows.filter(item=>item.comparison_supported);
    form.elements.row.innerHTML=eligible.length?eligible.map(item=>'<option value="'+esc(item.row_id)+'">'+
      esc(item.product.label)+' · '+esc(item.market.municipality_norm||'Unknown location')+'</option>').join(''):
      '<option value="">Review a marketplace offer first</option>';
    if(eligible.some(item=>item.row_id===previous))form.elements.row.value=previous;
    for(const input of form.querySelectorAll('input,select,button'))input.disabled=!eligible.length;
    element('[data-op-availability]').textContent=eligible.length?'Choose a reviewed product to evaluate its complete scenario.':
      'No reviewed products are available in these results. Use “Research this offer” above to inspect an individual listing and calculate an exploratory trade.';
    const scenario=element('[data-op-basis]').value==='scenario';
    for(const option of element('[data-op-objective]').options)option.disabled=scenario?option.value==='speed':option.value==='monthly_profit';
    if(element('[data-op-objective]').selectedOptions[0].disabled)element('[data-op-objective]').value='margin';
    element('[data-op-objective]').querySelector('[value=quantity]').textContent=scenario?'Monthly quantity scenario':'Recorded sold quantity';
    renderTable();restoreForm();
  };
  const invalidateScenario=key=>{
    const selected=report.rows.find(item=>item.row_id===key);
    if(selected){selected.scenario=null;selected.buy_ceiling_eur=null;}
    for(const [objective,ranks] of Object.entries(report.scenario_rankings||{}))report.scenario_rankings[objective]=ranks.filter(rank=>rank.row_id!==key);
  };
  const reconcileResults=()=>{
    for(const selected of report.rows){
      const draft=drafts[selected.row_id];
      if(selected.scenario&&(!draft?.evaluated||draft.exit_channel!==selected.scenario.exit_channel||
        opFields.some(([name])=>draft.inputs[name]!==selected.scenario.inputs[name])))invalidateScenario(selected.row_id);
    }
  };
  const captureDraft=({changed,origins,reused_from}={})=>{
    const selected=row();if(!selected)return;
    const previous=drafts[selected.row_id]||{};
    const provenance=origins||previous.origins;
    drafts=Object.fromEntries(Object.entries({...drafts,[selected.row_id]:{...(provenance?{origins:{...provenance,...(changed?{[changed]:'operator_assumption'}:{})},reused_from:reused_from===undefined?previous.reused_from:reused_from}:{}),revision:selected.evidence_revision,exit_channel:form.elements.exit_channel.value,
      inputs:Object.fromEntries(opFields.map(([name])=>[name,form.elements[name].value.trim()===''?null:Number(form.elements[name].value)])),evaluated:false}}).slice(-50));
    invalidateScenario(selected.row_id);const saved=persist();renderTable();renderScenario();
    if(!saved)element('[data-op-draft-note]').textContent='Draft kept in this tab; browser storage is unavailable.';
  };
  const load=async()=>{
    const ticket=++sequence;container.setAttribute('aria-busy','true');error('');
    try{
      const scope=filters(),result=await call('/api/opportunities?'+new URLSearchParams({...scope,days:30,horizon:30,limit:100}));
      if(ticket!==sequence)return;
      report=result;draw();
      const scenarios=currentOpportunityScenarios(report,drafts);
      if(Object.keys(scenarios).length){
        const evaluated=await call('/api/opportunities',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({filters:report.filters,days:report.days,horizon:report.horizon,scenarios})});
        if(ticket!==sequence)return;
        report=evaluated;reconcileResults();draw();
      }
    }catch(exc){if(ticket===sequence)error(exc.message);}
    finally{if(ticket===sequence)container.setAttribute('aria-busy','false');}
  };
  container.addEventListener('click',event=>{
    const toggle=event.target.closest('[data-op-toggle]');
    if(toggle&&report){
      const id=toggle.dataset.opToggle,panel=document.getElementById('opp-evidence-'+id);
      if(panel){
        const evidenceRow=panel.closest('tr'),opening=evidenceRow.hidden;evidenceRow.hidden=!opening;
        toggle.setAttribute('aria-expanded',String(opening));toggle.textContent=opening?'Hide evidence':'Show evidence';
        if(opening)openEvidence.add(id);else openEvidence.delete(id);
      }
    }
    const button=event.target.closest('[data-op-research]');if(button)onResearch(Number(button.dataset.opResearch));
  });
  element('[data-op-objective]').onchange=renderTable;
  element('[data-op-basis]').onchange=()=>{if(report)draw();};
  form.oninput=event=>{if(event.target.name!=='row')captureDraft({changed:event.target.name});};
  form.elements.row.onchange=restoreForm;
  element('[data-op-shared]').onclick=async()=>{
    const selected=row(),source=selected?.source_refs[0];if(!source)return;
    const key=selected.row_id,revision=selected.evidence_revision;
    const inputs=Object.fromEntries(opFields.map(([name])=>[name,form.elements[name].value.trim()===''?null:Number(form.elements[name].value)]));
    try{await onScenario({listing_pk:source.listing_pk},{inputs,mode:'operating',origins:drafts[key]?.origins,reusedFrom:drafts[key]?.reused_from,onApply:evaluated=>{
      if(row()?.row_id!==key||row()?.evidence_revision!==revision)throw new Error('The selected product evidence changed. Reopen its calculator before applying this copy.');
      if(evaluated.mode!=='operating')throw new Error('Product opportunities requires operating scope and shared resource assumptions. Choose operating scope and evaluate again.');
      for(const [name] of opFields)form.elements[name].value=evaluated.inputs[name]??'';
      captureDraft({origins:evaluated.origins,reused_from:evaluated.reused_from});element('[data-op-draft-note]').textContent='Copied evaluated assumptions. Evaluate this product scenario to check its reviewed group evidence and rankings.';
    }});}catch(exc){error(exc.message);}
  };
  element('[data-op-clear-draft]').onclick=()=>{
    const selected=row();if(!selected)return;
    delete drafts[selected.row_id];persist();invalidateScenario(selected.row_id);restoreForm();renderTable();
  };
  form.onsubmit=async event=>{
    event.preventDefault();const selected=row();if(!selected||!form.reportValidity())return;
    captureDraft();
    const ticket=++sequence,button=form.querySelector('[type=submit]');button.disabled=true;error('');
    try{
      const request=drafts[selected.row_id],scenarios=currentOpportunityScenarios(report,drafts);
      scenarios[selected.row_id]={revision:selected.evidence_revision,exit_channel:request.exit_channel,inputs:request.inputs};
      const result=await call('/api/opportunities',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filters:report.filters,days:report.days,horizon:report.horizon,scenarios})});
      if(ticket!==sequence)return;
      report=result;
      if(drafts[selected.row_id]===request)drafts[selected.row_id]={...request,evaluated:true};
      reconcileResults();persist();draw();
    }catch(exc){if(ticket===sequence)error(exc.message);}
    finally{button.disabled=false;}
  };
  await load();return {load};
};
