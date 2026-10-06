import {initCollectionPanel} from '/static/views/collection.js';
import {FILTER_KEYS,LABELS,ago,csvDocument,date,esc,eur,filtersToQuery,icon,num,percent,plural,refreshMs} from '/static/format.js';
import {call} from '/static/api.js';
import {applyCapabilities, canReadView, canWrite, writeDisabled, emptyCapabilities, parseCapabilities, VIEW_READS} from '/static/hosted.js';
import {badge,empty,metric} from '/static/components.js';
import {focusGates,focusIncidence,focusQueue,sortFocusPanels} from '/static/views/focus.js';
import {acquisitionContextRows,acquisitionFormPlan,acquisitionQueueRows,acquisitionQueueSummary} from '/static/views/acquisition.js';
import {capacityPanel,unresolvedPanel} from '/static/views/capacity.js';
import {provenanceView} from '/static/views/provenance.js';
import {candidateCards,comparisonCoverage} from '/static/views/candidates.js';
import {calculateScenario,candidateTable,cohortDetails,comparisonTable,deskIdle,deskSummary,exportCells,exportColumns,identitySummary,inspectionList,ownedComparables,recordedDecisions,researchInventory,researchReport,scenarioResult,sessionList,trendGate,verdictPanel} from '/static/views/research.js';
import {closeInternetResearch,internetMonitor,mountInternetResearch} from '/static/views/internet-research.js';
import {initOpportunityBoard} from '/static/views/opportunities.js';
import {classificationQuality} from '/static/views/classification.js';
import {decisionAttempts,initOwnedOutcomes,outcomeRemedies,resalePreview} from '/static/views/outcomes.js';
import {initSourceReview} from '/static/views/source-review.js';
import {initScenarioWorkspace,profileSummary,scenarioProvenance} from '/static/views/scenarios.js';
import {initItemWorkspace,workspaceNavigation} from '/static/views/item-workspace.js';
import {initForecastEvidence} from '/static/views/forecasts.js';
import {initSoldCapture,initAttemptCapture,recentCaptureReceipts} from '/static/views/guided-capture.js';
const $ = id => document.getElementById(id);
let collectionPanel;
document.querySelectorAll('[data-icon]').forEach(node=>node.innerHTML=icon(node.dataset.icon));
const query = new URLSearchParams(location.search);
const FILTERS = {platform:query.get('platform')||'tori',subplatform:query.get('subplatform')||'standard'};
for(const key of FILTER_KEYS.slice(2)) if(query.get(key)) FILTERS[key]=query.get(key);
const REFRESH_MS = refreshMs(query.get('refresh'),document.documentElement.dataset.refreshSeconds);
const VIEWS = {analyst:['Analyst workspace','Your decision workspace','Research your next trade.','Compare asking prices, review sale evidence, and check the costs before you buy.'],focus:['Focus categories','Your configured categories','Your focus, in detail.','Compare each configured category by supply, asking prices, and sale evidence.'],overview:['Market overview','Market intelligence','Market overview','A clearer view of the listings you have collected.'],observations:['Observations','Your collected sample','Browse observations.','Explore the items behind your market data.'],collection:['Collection','Your local dataset','Keep your data current.','Collect listings, follow up on availability, and review your coverage.'],ledger:['Trade ledger','Your actual transactions','Your trades, accounted for.','Record what you paid, what you received, and what it cost.']};
VIEWS.opportunities=['Product opportunities','Your product evidence','What should you search for?','Compare separate margin, work-hour, cash-speed and quantity evidence.'];
let opportunityPanel,opportunityPanelPromise;
async function loadOpportunities(){
  if(opportunityPanel)return opportunityPanel.load();
  if(opportunityPanelPromise)return opportunityPanelPromise;
  opportunityPanelPromise=initOpportunityBoard({container:$('opportunity-board'),filters:()=>({...FILTERS}),call,onResearch:async listingPk=>{
    await showView('research');await openDeskItem({listing_pk:listingPk});
  },onScenario:(seed,options)=>scenarioWorkspace.open(seed,options)}).then(panel=>{opportunityPanel=panel;return panel;}).finally(()=>{opportunityPanelPromise=null;});
  return opportunityPanelPromise;
}
VIEWS.research=['Price research','Your research desk','Price research','Work through the shortlist, then inspect the evidence and test your costs.'];
VIEWS.item=['Item workspace','Connected item evidence','One item, connected.','Read source evidence, saved decisions and actual owned outcomes together.'];
VIEWS.journal=['Decision journal','Your research evidence','Your decision history.','Keep your inspection notes and assumptions alongside actual trade results.'];
VIEWS.inventory=['Owned inventory','Your physical stock','Know what you own.','Record product facts, inspect each unit, and review where it can be sold.'];
VIEWS.drafts=['Tori drafts','Your reviewed listing preparation','Prepare your next Tori listing.','Turn inspected inventory into a clear, reviewed listing with traceable history.'];
VIEWS.orders=['Orders & returns','Your actual Tori trades','Follow each sale through.','Keep delivery, claims and net results linked to the same physical item.'];
VIEWS.workflow=['Pilot review','Your owned evidence','See what the pilot establishes.','Review the next step for each SKU, recorded losses and gaps in actual trade evidence.'];
VIEWS.connections=['Connections','Your seller accounts','Know your account access.','Connect one account, check its status, and see which actions are available.'];
VIEWS.predict=['Forecasts & trends','Predictive research','Understand the next move.','Explore sale likelihood, future asks, category momentum, and seller structure with their supporting evidence.'];
VIEWS.operations=['Operations','Local storage and recovery','Keep this workspace recoverable.','Backups, storage measurements, the seven-day review and this process, all read from this machine.'];
VIEWS.imports=['Imports','Manual capture','Bring in what you observed by hand.','Download the capture template, import your filled file, and read what is stored before you rely on it.'];
const ACTION_LABELS = {sweep:'Collect listings',itempage:'Check listing status',enrich:'Classify items',report:'Generate report',retention:'Apply retention policy'};
let currentView='overview', lastSnapshot=null, ledgerBuys=[], listingRows=[], selectedListing=null;
// What this deployment answers, as the server reported it. Read by the controls
// before they are drawn rather than discovered from a 404 after they are clicked.
let capabilities=emptyCapabilities();
let ownedOutcomeEditor=null,acquisitionDecision=null,attemptCapture=null,soldCapture=null,resalePreviewSequence=0;
let sourceReviewEditor=null;
let itemWorkspace=null,scenarioWorkspace=null,forecastReader=null;
const itemSeedKeys=['listing_pk','decision_pk','sku','draft_id','order_id'];
let itemSeed=itemSeedKeys.includes(query.get('item_kind'))?{[query.get('item_kind')]:query.get('item_id')}:null;
let snapshotController=null, snapshotSequence=0, refreshTimer=null, listingsController=null, listingsSequence=0;
let liveSource=null,liveFilterKey='',liveConnected=false,liveLastUpdateAt=0;
let listingPage=1, listingPages=1, searchTimer=null, monitorTimer=null, toastTimer=null;
const localRuns=new Map(), openRunDetails=new Set();
let lastPurchaseOptions='';
let inventoryData=null,inventoryPage=1,inventorySequence=0,inventorySearchTimer=null,selectedUnit=null,unitSequence=0,unitStep='facts',unitBusy=false;
let unitDirty=false,orderDirty=false;
const inventoryRetries=new Map();
const inventoryDisabled=new Map();
let draftData=null,draftPage=1,draftSequence=0,draftDetailSequence=0,selectedDraft=null,draftBusy=false,draftDirty=false;
let draftUnitVersions=new Map(),draftChanged=false;
const draftRetries=new Map(),draftDisabled=new Map();
let ordersData=null,ordersPage=1,ordersSequence=0,orderDetailSequence=0,selectedOrder=null,orderBusy=false,orderChanged=false,saleUnits=[];
const orderRetries=new Map();
let workflowData=null,workflowPage=1,workflowSequence=0,acquisitionBusy=false;
let lastAnalysis=null,analysisController=null,analysisSequence=0,analysisRequestKey='',activitySeries='new_observations';
let tableLayoutPending=false;
let analystRun=null,analystTimer=null,analystSequence=0,analystStarting=false;
let savedSegments=[];
let decisionAssumptions,decisionScenarioContext=null;
let researchReady=false,researchSequence=0,journalPage=1,journalPages=1,journalData=null,decisionListing=null,decisionToken=null,scenarioListing=null,comparisonChoices=new Map(),comparisonSequence=0;
let provenanceSequence=0,provenanceController=null,provenanceListing=null,provenancePeers=[],provenanceRevision='',provenanceCheckedAt=0,provenanceChangedAt=0;
try{const stored=JSON.parse(localStorage.getItem('tori.analyst.segments.v1')||'[]');if(Array.isArray(stored))savedSegments=stored.filter(row=>row&&typeof row.name==='string'&&row.filters&&typeof row.filters==='object').slice(0,12);}catch{}
const analysisDefaults={'analysis-dimension':['dimension',['category_root_norm','category_norm','municipality_norm','condition_norm','product_type']],'analysis-days':['days',['7','30','90']],'analysis-horizon':['horizon',['7','14','30','60','90']]};
for(const [id,[key,allowed]] of Object.entries(analysisDefaults))if(allowed.includes(query.get(key)))$(id).value=query.get(key);
function setHTML(id,html){if($(id).innerHTML!==html){$(id).innerHTML=html;scheduleTableScroll();}}
function toast(text,error=false){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,6000);}
function message(id,text,tone='info'){const node=$(id);node.textContent=text;node.className='message '+tone;node.hidden=!text;}
function syncURL(){
  const params=new URLSearchParams(location.search);
  FILTER_KEYS.forEach(key=>FILTERS[key]?params.set(key,FILTERS[key]):params.delete(key));
  history.replaceState(null,'',location.pathname+'?'+params.toString()+'#'+currentView);
  drawFilterContext();
}
const mobileViewport=window.matchMedia('(max-width:900px)');
function closeNavigation(restoreFocus=false){
  const wasOpen=$('sidebar').classList.contains('is-open');
  $('sidebar').classList.remove('is-open');$('scrim').classList.remove('is-open');
  $('mobile-menu').setAttribute('aria-expanded','false');$('mobile-menu').setAttribute('aria-label','Open navigation');
  $('sidebar').inert=mobileViewport.matches;$('main-content').inert=false;
  $('sidebar').removeAttribute('role');$('sidebar').removeAttribute('aria-modal');document.body.classList.remove('nav-open');
  if(restoreFocus===true&&wasOpen&&mobileViewport.matches)$('mobile-menu').focus();
}
function openNavigation(){
  $('sidebar').classList.add('is-open');$('scrim').classList.add('is-open');$('sidebar').inert=false;
  $('mobile-menu').setAttribute('aria-expanded','true');$('mobile-menu').setAttribute('aria-label','Close navigation');
  if(mobileViewport.matches){$('sidebar').setAttribute('role','dialog');$('sidebar').setAttribute('aria-modal','true');$('main-content').inert=true;document.body.classList.add('nav-open');}
  $('sidebar').querySelector('[aria-current="page"]')?.focus();
}
function navigationKeydown(event){
  if(!$('sidebar').classList.contains('is-open')||!mobileViewport.matches)return;
  if(event.key==='Escape'){event.preventDefault();closeNavigation(true);return;}
  if(event.key!=='Tab')return;
  const items=[...$('sidebar').querySelectorAll('a[href],button:not(:disabled)')].filter(node=>node.offsetParent!==null);
  const first=items[0],last=items.at(-1);
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
}
mobileViewport.addEventListener('change',closeNavigation);
/**
 * Ask the deployment what it can answer, then draw only that.
 *
 * Authorisation and availability are different questions. A route can be permitted
 * to this account and still be refused because the thing that computes it is not
 * running here -- the Netlify workspace answers from an exported payload and never
 * runs collection, trading or models. Asking once, up front, is what keeps the
 * interface from offering fifteen views whose every panel refuses and a set of
 * buttons that look live until they are clicked.
 *
 * A failure here is not fatal and not silently ignored: the capability set stays
 * empty, which withholds every view that needs a read and leaves the reader on the
 * one view that does not, with the banner explaining why. Rendering the full shell
 * and hoping was the behaviour that made this worth fixing.
 */
async function establishCapabilities(){
  capabilities=parseCapabilities(await call('/api/capabilities'));
  const views=[...document.querySelectorAll('[data-view]')].map(node=>node.dataset.view);
  const summary=applyCapabilities(capabilities,{document,views,fallbackView:'overview',reason:drawDeploymentNote});
  if(summary.withheld.length){
    // A view named in the URL that this deployment cannot answer is a bookmark
    // from a fuller workspace. Fall back rather than leaving the reader on an
    // empty page with no explanation.
    if(!document.querySelector('[data-view="'+currentView+'"]'))showView(summary.fallback||'overview');
  }
  // Views that render their own shell still need their writes switched off, which
  // `applyCapabilities` did; and the shell's own wording has to stop claiming a
  // local database this deployment has never been near.
  lockDeskControls();
  drawDeploymentNote(summary,capabilities);
  return capabilities;
}
const DEPLOYMENT_TITLES={replay:'Read-only replay of the last exported research',reduced:'Reduced read-only workspace',runtime:'Live research workspace'};
function drawDeploymentNote(summary,current=capabilities){
  const node=$('deployment-note');
  // A deployment that owns the runtime needs no banner: it is what the page claims
  // to be, so saying so would be noise on every load.
  if(current.deployment==='runtime'&&!current.readOnly){node.hidden=true;return;}
  const generated=lastSnapshot?.generated_at;
  const title=current.readOnly?'Live analysis · read-only verification':DEPLOYMENT_TITLES[current.deployment]||'Read-only workspace';
  $('deployment-note-title').textContent=title;
  $('deployment-note-body').textContent=[
    current.detail,
    generated?'Numbers were measured '+ago(new Date(generated).toISOString())+'.':null,
    summary.withheld.length?summary.withheld.length+' of '+summary.total+' views need the local research runtime and are not shown.':null,
    'Nothing here collects, trades, runs a model or trains one.'
  ].filter(Boolean).join(' ');
  node.hidden=false;
}
// Guarded rather than assumed. Everything else in this file reaches for the
// elements it was written against, but this one runs before the capability answer
// arrives, so a shell that lacks the banner must still load the dashboard rather
// than throw on the way in.
if($('deployment-note-dismiss'))$('deployment-note-dismiss').addEventListener('click',()=>{$('deployment-note').hidden=true;});

function showView(view,updateHash=true){
  if(!VIEWS[view]) view='analyst';
  if(capabilities.deployment!=='unknown'&&!canReadView(capabilities,view))view='overview';
  let ready;
  if(view==='collection')collectionPanel?.open();else collectionPanel?.close();
  if(currentView==='predict'&&view!=='predict'){closeModelDetail();pendingModelListing=null;clearTimeout(predictSearchTimer);}
  if(view!=='predict'){clearTimeout(predictPoll);predictDetailSequence++;}
  currentView=view;
  updateItemRail();
  document.querySelectorAll('.view').forEach(node=>node.hidden=node.id!=='view-'+view);
  document.querySelectorAll('[data-view]').forEach(node=>{if(node.dataset.view===view)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
  const [breadcrumb,eyebrow,title,subtitle]=VIEWS[view];
  $('breadcrumb').textContent=breadcrumb;$('page-eyebrow').textContent=eyebrow;$('page-title').textContent=title;$('page-subtitle').textContent=subtitle;
  $('filter-card').hidden=['item','ledger','collection','journal','focus','inventory','connections','drafts','orders','workflow','predict','operations','imports'].includes(view);
  $('heading-action').hidden=['item','collection','connections','drafts','orders','workflow','predict','operations','imports'].includes(view);
  const headingWrite={inventory:'/api/inventory',ledger:'/api/ledger',journal:'/api/research'}[view];
  $('heading-action').disabled=!!headingWrite&&!canWrite(capabilities,headingWrite);
  if(!['inventory','ledger','journal','research','analyst','focus'].includes(view)&&!capabilities.reads.has('/api/collection/control'))$('heading-action').hidden=true;
  $('heading-action-label').textContent=view==='inventory'?'Add owned item':view==='ledger'?'New purchase':view==='journal'?'New decision':view==='research'?'Find candidates':view==='analyst'||view==='focus'?'Inspect observations':'Collect data';
  if(updateHash)history.replaceState(null,'',location.pathname+location.search+'#'+view);
  closeNavigation();
  if(updateHash){window.scrollTo({top:0,behavior:'auto'});$('page-title').focus({preventScroll:true});}
  scheduleTableScroll();
if(view==='observations')loadListings();
if(view==='research'){ready=loadDesk({preserveDetail:true});loadDeskSessions();}
if(view==='item'&&itemWorkspace&&!itemWorkspace.isBusy()){ready=itemWorkspace.data()?itemWorkspace.refresh():itemSeed?itemWorkspace.open(itemSeed):undefined;}
if(view==='analyst')loadAnalysis();
if(view==='opportunities')loadOpportunities();
  if(view==='journal')loadResearch();
  if(view==='focus')loadFocus();
  if(view==='inventory')loadInventory();
  if(view==='drafts')loadDrafts();
  if(view==='orders')loadOrders();
  if(view==='workflow')loadWorkflow();
  if(view==='connections')loadConnections();
  if(view==='predict')loadPredict();
  if(view==='operations')loadOperations();
  if(view==='imports')loadImports();
  return ready;
}
function applyFilter(key,value){
  if(value)FILTERS[key]=value;else delete FILTERS[key];
  if(key==='category_root_norm'||key==='focus_path')delete FILTERS.category_norm;
  if(key==='focus_path')delete FILTERS.category_root_norm;
  if(key==='category_root_norm'||key==='platform')delete FILTERS.focus_path;
  listingPage=1;if(analysisController)analysisController.abort();
  $('export-analysis').disabled=true;$('analysis-content').setAttribute('aria-busy','true');$('market-content').setAttribute('aria-busy','true');$('stats').setAttribute('aria-busy','true');
  drawSegments(lastSnapshot?.segments||{});syncURL();refresh();if(currentView==='analyst')loadAnalysis();if(currentView==='observations')loadListings();
}
function focusLabel(path){
  const scope=lastSnapshot?.collection||{},index=(scope.focus_categories||[]).indexOf(path);
  return index>=0?scope.focus_category_names?.[index]?.split(' > ').at(-1)||path:path;
}
function drawFilterContext(){
  const names={platform:'Platform',subplatform:'Market',focus_path:'Focus',category_root_norm:'Category',category_norm:'Detailed category',municipality_norm:'Municipality',condition_norm:'Condition',seller_type_norm:'Seller'};
  const active=FILTER_KEYS.filter(key=>FILTERS[key]&&!(key==='platform'&&FILTERS[key]==='tori')&&!(key==='subplatform'&&FILTERS[key]==='standard'));
  setHTML('filter-chips',active.map(key=>{
    const value=key==='focus_path'?focusLabel(FILTERS[key]):LABELS[FILTERS[key]]||FILTERS[key];
    return '<button type="button" class="filter-chip" data-clear-filter="'+key+'" aria-label="Remove '+esc(names[key])+': '+esc(value)+'"><span>'+esc(names[key])+': '+esc(value)+'</span><span aria-hidden="true">×</span></button>';
  }).join(''));
  $('filter-chips').hidden=!active.length;
  $('reset-filters').disabled=!active.length&&!$('listing-search').value&&$('listing-availability').value==='all';
  const advanced=active.filter(key=>['platform','category_root_norm','category_norm','condition_norm','seller_type_norm'].includes(key)).length;
  $('more-filters').innerHTML=icon('filter')+'More filters'+(advanced?' ('+num(advanced)+')':'');
}
function resetResearchFilters(){
  for(const key of FILTER_KEYS.slice(2))delete FILTERS[key];FILTERS.platform='tori';FILTERS.subplatform='standard';listingPage=1;
  $('listing-search').value='';$('listing-availability').value='all';
  if(analysisController)analysisController.abort();$('export-analysis').disabled=true;$('analysis-content').setAttribute('aria-busy','true');
  drawSegments(lastSnapshot?.segments||{});syncURL();refresh();if(currentView==='analyst')loadAnalysis();if(currentView==='observations')loadListings();
}
function drawSegments(options){
  const allLabels={focus_path:'All stored categories',category_root_norm:'All categories',category_norm:'All detailed categories',municipality_norm:'All municipalities',condition_norm:'All conditions',seller_type_norm:'All seller types'};
  for(const select of document.querySelectorAll('[data-filter]')){
    const key=select.dataset.filter,source=key==='focus_path'?lastSnapshot?.collection?.focus_categories||[]:options[key]||[];
    const values=[...new Set([...source,...(FILTERS[key]?[FILTERS[key]]:[])])];
    const html=(allLabels[key]?'<option value="">'+allLabels[key]+'</option>':'')+values.map(value=>'<option value="'+esc(value)+'">'+esc(key==='focus_path'?focusLabel(value):LABELS[value]||value)+'</option>').join('');
    if(select.dataset.options!==html){select.innerHTML=html;select.dataset.options=html;}
    select.value=FILTERS[key]||'';
    if(key==='focus_path')select.disabled=FILTERS.platform!=='tori'||!values.length;
    // A hosted deployment replays one exported payload, so a segment filter would
    // select an analysis it never computed. Disabling it says so; leaving it
    // enabled would answer with a refusal and read as a broken workspace.
    if(document.documentElement.dataset.hosted==='1'){select.disabled=true;select.title='Segment filters are resolved by the local research runtime.';}
  }
  const platformHTML=[...new Set(['tori',...(options.platform||[])])].map(value=>'<option value="'+esc(value)+'">'+esc(LABELS[value]||value)+'</option>').join('');
  if($('buy-platform').dataset.options!==platformHTML){const value=$('buy-platform').value;$('buy-platform').innerHTML=platformHTML;$('buy-platform').value=value||'tori';$('buy-platform').dataset.options=platformHTML;}
  drawFilterContext();
}
function drawSummary(snapshot){
  const national=snapshot.panels.national,tier=snapshot.coverage.tier1_national||{};
  $('stat-total').textContent=num(snapshot.health.observations);$('nav-count').textContent=num(snapshot.health.observations);
  $('stat-median').textContent=national.status==='OK'?eur(national.median_eur):'—';
  $('stat-ci').textContent=national.median_ci95?'95% CI '+eur(national.median_ci95.lo)+'–'+eur(national.median_ci95.hi):'Needs '+num(national.min_n_threshold)+' priced observations';
  $('stat-priced').textContent=num(national.n_with_price);
  $('stat-priced-note').textContent=percent(national.coverage_with_price==null?null:national.coverage_with_price*100)+' of fresh, available items';
  $('stat-coverage').textContent=percent(tier.national_coverage_pct);
  $('stat-coverage-note').textContent=num((tier.cells||0)-(tier.pending||0))+' of '+num(tier.cells)+' top-level cells';
  $('segment-note').textContent=num(national.n_with_price)+' priced / '+num(national.n_listings_total)+' fresh, available listings'+(national.n_stale_excluded?' · '+num(national.n_stale_excluded)+' stale excluded':'');
  const focus=snapshot.collection||{};
  $('nav-focus-summary').textContent=(focus.focus_category_names?.length?plural(focus.focus_category_names.length,'category','categories'):'All categories')+' · '+(focus.focus_municipalities?.length?plural(focus.focus_municipalities.length,'municipality','municipalities'):'Nationwide');
  const badgeNode=$('focus-badge');
  if(focus.focus_category_names&&focus.focus_category_names.length){
    badgeNode.textContent='Focus · '+num(focus.focus_category_names.length)+' categor'+(focus.focus_category_names.length===1?'y':'ies');
    badgeNode.className='badge amber link-badge';
    badgeNode.title=focus.focus_category_names.join(' · ');
    badgeNode.hidden=false;
  }else{
    badgeNode.hidden=true;
  }
  if(FILTERS.focus_path)$('segment-note').textContent=num(national.n_with_price)+' priced / '+num(national.n_listings_total)+' fresh, available listings in '+focusLabel(FILTERS.focus_path)+(national.n_stale_excluded?' · '+num(national.n_stale_excluded)+' stale excluded':'');
  $('foot').textContent='Asking prices from items observed within '+num(national.max_age_hours/24)+' days. Minimum '+num(national.min_n_threshold)+' priced observations per estimate. Giveaways and reserved items are excluded. Actual sale prices may differ.';
}
let lastFocus=null,focusController=null,focusSequence=0;
function focusPanels(){
  return sortFocusPanels(lastFocus?.panels||[],$('focus-sort').value);
}
function focusEvidenceHTML(panel,min_n){
  const gates=panel.evidence_gates||{},sales=gates.sales||{},horizon=lastFocus?.horizon||30;
  const count=(label,n,ready,positive='Supported')=>metric(label,num(n)+' / '+num(min_n)+' '+badge(ready?positive:'Needs evidence',ready?'green':'amber'));
  return '<div class="focus-evidence"><h4>Evidence at '+horizon+' days</h4><div class="metric-list">'
    +count('Fresh priced items',panel.n_priced,panel.status==='OK')
    +count('Followed to '+horizon+' days',panel.horizon_support,gates.horizon?.ready??panel.horizon_support>=min_n,'Threshold met')
    +count('Declared sale markers',sales.sold_inferred?.count??panel.sold_declared,(sales.sold_inferred?.count??panel.sold_declared)>=min_n,'Threshold met')
    +count('Verified sale markers',sales.sold_confirmed?.count??panel.sold_verified,(sales.sold_confirmed?.count??panel.sold_verified)>=min_n,'Threshold met')
    +'</div><p class="note" style="margin-top:9px">Each sale class needs both marker and horizon support.</p></div>';
}
function focusCard(panel,min_n){
  const follow=panel.followup||{},queue=focusQueue(follow),grid=panel.grid||{},rates=panel.sale_rates||{},step=panel.next_step;
  const declared=rates.sold_inferred,verified=rates.sold_confirmed;
  const ready=panel.status==='OK'&&(declared||verified);
  return '<article class="focus-card'+(ready?' ready':'')+'"><div class="focus-card-head"><div><h3>'+esc(panel.name.split(' > ').at(-1))+'</h3><small>'+esc(panel.name.includes(' > ')?panel.name.split(' > ').slice(0,-1).join(' > '):panel.path)+'</small></div>'+badge(ready?'Evidence supported':panel.status==='OK'?'Prices only':'Building',ready?'green':panel.status==='OK'?'purple':'')+'</div>'
    +(panel.status==='OK'?'<div class="focus-price"><strong>'+eur(panel.median_eur)+'</strong><span class="note">median ask</span></div>':'<div class="focus-price"><strong class="muted">—</strong><span class="note">Needs '+num(min_n)+' fresh prices · has '+num(panel.n_priced)+'</span></div>')
    +(panel.status==='OK'?'<p class="note">95% CI '+(panel.median_ci95?eur(panel.median_ci95.lo)+'–'+eur(panel.median_ci95.hi):'unavailable')+' · P25–P75 '+eur(panel.p25_eur)+'–'+eur(panel.p75_eur)+'</p>':'')
    +focusEvidenceHTML(panel,min_n)
    +'<div class="metric-list">'+metric('Known condition',percent(follow.condition_coverage==null?null:follow.condition_coverage*100))+metric('Accepted product classification',percent(follow.classification_coverage==null?null:follow.classification_coverage*100))+metric('Status checks queued',num(queue.due))+(queue.enabled?metric('Discovered, not admitted',num(queue.not_admitted)):'')+'</div>'
    +(step?'<div class="focus-next"><strong>'+esc(step.title)+'</strong><p>'+esc(step.detail)+'</p></div>':'')
    +'<div class="focus-detail"><details><summary>Sale estimates and collection details</summary><div class="metric-list">'+metric('Tracked / available / priced',num(panel.n_tracked)+' / '+num(panel.n_available)+' / '+num(panel.n_priced))
    +metric('Declared sale incidence · '+(lastFocus?.horizon||30)+'d',focusIncidence(declared))+metric('Verified sale incidence · '+(lastFocus?.horizon||30)+'d',focusIncidence(verified))
    +(queue.enabled?metric('Admitted / with a successful check',num(queue.admitted)+' / '+num(queue.successful))+metric('Awaiting first successful check',num(queue.awaiting_first))+metric('Due first / repeat checks',num(queue.due_first)+' / '+num(queue.due_repeat))+metric('Due retries / quarantined',num(queue.retry_due)+' / '+num(queue.quarantined)):'')
    +metric('Minimum full-budget runs',queue.minimum_runs==null?'—':num(queue.minimum_runs)+' at '+num(follow.item_budget)+' items')+metric('Grid cells read / owned',num(grid.crawled)+' / '+num(grid.cells)+' · '+percent(grid.coverage_pct))+'</div></details></div>'
    +'<div class="focus-actions"><button class="btn small" type="button" data-focus-drill="'+esc(panel.path)+'">Browse category</button><button class="btn small quiet" type="button" data-focus-goto="'+esc(panel.path)+'">Compare category</button>'+(step?.view==='collection'?'<button class="btn small quiet" type="button" data-go="collection">Review follow-up</button>':'')+'</div></article>';
}
function focusRow(panel,min_n){
  const follow=panel.followup||{},queue=focusQueue(follow),grid=panel.grid||{},ci=panel.median_ci95;
  return '<tr><td class="focus-cell"><button class="row-link" type="button" data-focus-drill="'+esc(panel.path)+'">'+esc(panel.name)+'</button><div class="range-text">'+esc(panel.path)+'</div></td>'
    +'<td class="num">'+num(panel.n_available)+' <span class="muted">/ '+num(panel.n_priced)+'</span><div class="range-text">'+num(panel.n_tracked)+' tracked</div></td>'
    +'<td class="num">'+(panel.status==='OK'?eur(panel.median_eur):'<span class="muted">Needs '+num(min_n)+'</span>')+'<div class="range-text">'+(ci?eur(ci.lo)+'–'+eur(ci.hi):'No interval')+'</div></td>'
    +'<td class="num muted">'+(panel.status==='OK'?eur(panel.p25_eur)+'–'+eur(panel.p75_eur):'—')+'</td>'
    +'<td class="num">'+num(panel.sold_declared)+' <span class="muted">/ '+num(panel.sold_verified)+'</span><div class="range-text">'+num(panel.horizon_support)+' followed to horizon</div></td>'
    +'<td class="num"><div class="focus-gate">'+focusGates(panel,min_n).map(label=>badge(label)).join('')+'</div><div class="range-text">'+num(panel.horizon_support)+' / '+num(min_n)+' followed to '+lastFocus.horizon+'d</div>'+(panel.next_step?'<div class="range-text">'+esc(panel.next_step.title)+'</div>':'')+'</td>'
    +'<td class="num">'+num(queue.due)+'<div class="range-text">'+(queue.minimum_runs==null?'—':num(queue.minimum_runs))+' runs at '+num(follow.item_budget)+'</div>'+(queue.enabled?'<div class="range-text">'+num(queue.not_admitted)+' not admitted</div>':'')+'</td>'
    +'<td class="num">'+percent(grid.coverage_pct)+'<div class="range-text">'+num(grid.crawled)+' / '+num(grid.cells)+' cells</div></td>'
    +'<td><button class="btn quiet small" type="button" data-focus-goto="'+esc(panel.path)+'" aria-label="Open '+esc(panel.name)+' in analyst mode">'+icon('chart',14)+'</button></td></tr>';
}
function drawFocus(){
  if(!lastFocus)return;
  const report=lastFocus,panels=focusPanels(),min_n=report.min_n;
  if(!panels.length){
    setHTML('focus-cards',empty('No focus categories configured','Your collection covers all categories. Browse observations or compare category groups in the analyst workspace.','collect'));
    $('focus-rows').innerHTML='';$('export-focus').disabled=true;
    $('focus-note').textContent='No verticals are configured, so there is nothing to rank.';
    $('focus-configured').textContent='0';$('focus-prices').textContent='0';$('focus-sale').textContent='0';$('focus-due').textContent='0';
    setHTML('focus-followup','');setHTML('focus-grid','');return;
  }
  $('export-focus').disabled=false;
  const totals=report.totals||{};
  $('focus-configured').textContent=num(report.n_configured);
  $('focus-configured-note').textContent=(report.focus_municipalities||[]).length?num(report.focus_municipalities.length)+' municipalities configured':'All municipalities';
  $('focus-prices').textContent=num(report.prices_ready)+' / '+num(report.n_configured);
  $('focus-prices-note').textContent='Need '+num(min_n)+' fresh prices each';
  $('focus-sale').textContent=num(report.sale_ready)+' / '+num(report.n_configured);
  $('focus-sale-note').textContent=report.horizon+'d horizon · events + follow-up';
  const queue=focusQueue(totals);
  $('focus-due').textContent=num(queue.due);
  $('focus-due-note').textContent=(queue.minimum_runs==null?'—':num(queue.minimum_runs))+' minimum full-budget run'+(queue.minimum_runs===1?'':'s')+(queue.enabled?' · admitted tasks':'');
  const municipalities=(report.focus_municipalities||[]);
  const market=report.filters?.subplatform;
  $('focus-scope').textContent=(market?(LABELS[market]||market)+' · ':'')+num(report.n_market)+' market listings in view'+(municipalities.length?' · '+municipalities.length+' municipalities':'');
  const cards=$('focus-layout').value==='cards';
  $('focus-cards').hidden=!cards;$('focus-table').hidden=cards;
  if(cards)setHTML('focus-cards',panels.map(panel=>focusCard(panel,min_n)).join(''));
  else $('focus-rows').innerHTML=panels.map(panel=>focusRow(panel,min_n)).join('');
  const ready=panels.filter(panel=>panel.sale_status==='OK').length;
  $('focus-note').textContent='Showing '+num(panels.length)+' configured verticals · generated '+date(report.generated_at)+' · '+num(ready)+' with a supported sale rate at '+report.horizon+'d. '+(report.note||'');
  setHTML('focus-followup',followupHTML(totals));
  const debt=[...panels].sort((a,b)=>(b.grid?.pending??0)-(a.grid?.pending??0));
  setHTML('focus-grid','<div class="metric-list">'+debt.map(panel=>metric(panel.name,num(panel.grid?.pending)+' unvisited of '+num(panel.grid?.cells)+' cells')).join('')+'</div><p class="note" style="margin-top:16px">An unvisited cell has never been read, so its supply is unknown rather than absent. One search page is one request; an item page is one listing.</p>');
}
async function loadFocus(){
  if(focusController)focusController.abort();
  focusController=new AbortController();const sequence=++focusSequence;
  $('focus-panel').setAttribute('aria-busy','true');$('focus-cards').setAttribute('aria-busy','true');$('export-focus').disabled=true;
  try{
    const result=await call('/api/focus?'+new URLSearchParams({platform:'tori',subplatform:FILTERS.subplatform||'standard',horizon:$('focus-horizon').value||30}),{signal:focusController.signal});
    if(sequence!==focusSequence)return;
    lastFocus=result;drawFocus();$('focus-error').hidden=true;
  }catch(error){if(error.name==='AbortError'||sequence!==focusSequence)return;message('focus-error','Could not read your verticals. '+error.message+(lastFocus?' Previous results remain at '+lastFocus.horizon+' days.':''),'error');}
  finally{if(sequence===focusSequence){$('focus-cards').setAttribute('aria-busy','false');$('focus-panel').setAttribute('aria-busy','false');}}
}
function exportFocus(){
  if(!lastFocus||$('export-focus').disabled)return;
  const r=lastFocus,head=['path','name','tracked','available','priced','price_coverage','median_ask_eur','median_ci95_lo','median_ci95_hi','p25_eur','p75_eur','sold_declared','sold_verified','declared_incidence','declared_ci95_lo','declared_ci95_hi','verified_incidence','verified_ci95_lo','verified_ci95_hi','horizon_days','horizon_support','sale_status','price_status','never_probed','probes_due_in_scope','minimum_probe_runs','item_budget','grid_cells','grid_crawled','grid_pending','grid_coverage_pct','filters','generated_at','queue_basis','admitted','not_admitted','successful_checks','due_first','due_repeat','due_retries','quarantined','historical_probes_due_in_scope','condition_coverage','classification_coverage','next_step'];
  const cells=focusPanels().map(panel=>{const d=panel.sale_rates?.sold_inferred,v=panel.sale_rates?.sold_confirmed,f=panel.followup||{},q=focusQueue(f),g=panel.grid||{};return [panel.path,panel.name,panel.n_tracked,panel.n_available,panel.n_priced,panel.price_coverage,panel.median_eur,panel.median_ci95?.lo,panel.median_ci95?.hi,panel.p25_eur,panel.p75_eur,panel.sold_declared,panel.sold_verified,d?.share,d?.ci95.lo,d?.ci95.hi,v?.share,v?.ci95.lo,v?.ci95.hi,r.horizon,panel.horizon_support,panel.sale_status,panel.status,f.n_never_probed,q.due,q.minimum_runs,f.item_budget,g.cells,g.crawled,g.pending,g.coverage_pct,JSON.stringify(r.filters),r.generated_at,q.enabled?'admitted_tasks':'legacy_attempt_queue',q.admitted,q.not_admitted,q.successful,q.due_first,q.due_repeat,q.retry_due,q.quarantined,f.n_due_in_scope,f.condition_coverage,f.classification_coverage,panel.next_step?.code];});
  downloadCSV('tori-focus-verticals-'+r.generated_at.slice(0,10)+'.csv',head,cells);
}
// A vertical drill keeps `focus_path` as the only narrowing filter. Adding the
// root category as well would double-count nothing but would make the selection
// depend on how deep the facet tree happens to be, and the root is strictly
// wider than the vertical the operator chose.
function focusApply(path){
  for(const key of FILTER_KEYS.slice(2))delete FILTERS[key];
  if(path)FILTERS.focus_path=path;
  listingPage=1;
  if(analysisController)analysisController.abort();
  $('export-analysis').disabled=true;
  $('listing-search').value='';$('listing-availability').value='all';
  syncURL();refresh();
}
function focusDrill(path){focusApply(path);showView('observations');}
function focusGoto(path){focusApply(path);$('analysis-dimension').value='category_norm';showView('analyst');}
function drawPanels(panels){
  const categories=panels.by_category||[],supported=categories.filter(row=>row.status==='OK'),scale=Math.max(1,...supported.map(row=>Number(row.p75)));
  $('category-count').textContent=plural(categories.length,'category');
  setHTML('bycat',categories.length?categories.map(row=>{
    const label=row.filters.category_root_norm;
    if(row.status!=='OK')return '<button class="range-row" type="button" data-category="'+esc(label)+'"><span class="range-label">'+esc(label)+'<small>'+num(row.n)+' priced observations</small></span><span class="note">Needs '+num(row.min_n_threshold)+' items</span><span class="range-price">—</span></button>';
    const left=Math.min(100,row.p25/scale*100),right=Math.min(100,row.p75/scale*100),median=Math.min(100,row.median/scale*100);
    const title='P25 '+eur(row.p25)+' · Median '+eur(row.median)+' · P75 '+eur(row.p75)+' · 95% CI '+(row.ci?eur(row.ci[0])+'–'+eur(row.ci[1]):'unavailable');
    return '<button class="range-row" type="button" data-category="'+esc(label)+'" title="'+esc(title)+'"><span class="range-label">'+esc(label)+'<small>'+num(row.n)+' priced observations</small></span><span class="range-track" aria-hidden="true"><i style="left:'+left+'%;width:'+(right-left)+'%"></i><b style="left:calc('+median+'% - 2px)"></b></span><span class="range-price">'+eur(row.median)+'</span></button>';
  }).join(''):empty('No prices in this segment','Choose a broader category or collect more observations.'));
  setHTML('bymuni',(panels.by_municipality||[]).length?panels.by_municipality.map(row=>'<tr><td><button class="row-link" type="button" data-municipality="'+esc(row.filters.municipality_norm)+'">'+esc(row.filters.municipality_norm)+'</button></td><td class="num muted">'+num(row.n)+'</td><td class="num">'+(row.status==='OK'?eur(row.median):'—')+'</td><td class="num muted">'+(row.ci?num(row.ci[0])+'–'+num(row.ci[1]):'Needs '+num(row.min_n_threshold))+'</td></tr>').join(''):'<tr><td colspan="4">'+empty('No municipality data','Try resetting the market filters.')+'</td></tr>');
}
function progress(label,value){return '<div class="progress-item"><div class="progress-label"><span>'+esc(label)+'</span><strong>'+percent(value)+'</strong></div><div class="progress-track" role="progressbar" aria-label="'+esc(label)+'" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+Math.max(0,Math.min(100,value||0))+'"><i style="width:'+Math.max(0,Math.min(100,value||0))+'%"></i></div></div>';}
function drawQuality(quality){setHTML('quality',progress('Has an asking price',quality.with_price)+progress('Has a category label',quality.with_category_label)+progress('Has a condition label',quality.with_condition)+'<div class="mini-grid"><div><div class="mini-value">'+num(quality.giveaways_flagged)+'</div><div class="mini-label">Giveaways identified</div></div><div><div class="mini-value">'+num(quality.diili_split_out)+'</div><div class="mini-label">Diili listings separated</div></div></div>');}
function drawLiquidity(liquidity){
  $('liquidity-badge').textContent=liquidity.status==='OK'?'Estimates available':'Building history';$('liquidity-badge').className='badge '+(liquidity.status==='OK'?'green':'');
  let html='<div class="metric-list">'+metric('Listings tracked',num(liquidity.listings_tracked))+metric('Seller-declared sales',num(liquidity.sold_inferred_seller_declared))+metric('Verified sales',num(liquidity.sold_confirmed))+metric('Withdrawn / expired',num((liquidity.withdrawn||0)+(liquidity.expired??liquidity.expired_or_404??0)))+metric('Follow-up coverage',percent(liquidity.followup_coverage==null?null:liquidity.followup_coverage*100))+'</div>';
  if(liquidity.selection_note)html+='<p class="note" style="margin-top:12px">'+esc(liquidity.selection_note)+'</p>';
  const estimates=Object.entries(liquidity.sell_through_by_evidence||{}).filter(([,report])=>report.estimates);
  if(estimates.length){
    for(const [evidence,report] of estimates)html+='<div class="section-divider"></div><h3>'+(evidence==='sold_confirmed'?'Verified sales':'Seller-declared sales')+'</h3><div class="table-wrap"><table><thead><tr><th>Follow-up</th><th class="num">Share sold</th><th class="num">95% CI</th></tr></thead><tbody>'+Object.entries(report.estimates).map(([horizon,value])=>'<tr><td>'+esc(horizon)+'</td><td class="num">'+percent(value.share*100)+'</td><td class="num muted">'+num(value.ci95.lo*100,1)+'–'+num(value.ci95.hi*100,1)+'%</td></tr>').join('')+'</tbody></table></div>';
  }else{html+='<div class="section-divider"></div><p class="note">'+(liquidity.resolved_terminal?'More events and longer follow-up are needed for a supported sell-through estimate.':'No resolved outcomes yet. Check listing status over time to build the follow-up history.')+'</p><button class="btn small" data-go="collection" type="button" style="margin-top:12px">Check collected listings '+icon('refresh',13)+'</button>';}
  const units=liquidity.physical_unit_evidence;
  if(units)html+='<p class="note">Physical quantity remains unknown for public listings. '+num(units.verified_owned_units)+' owned units have explicit links; relist hints and seller quantity claims do not establish sales.</p>';
  const audit=liquidity.measurement_cohorts;
  if(audit?.cohorts?.length){
    html+='<div class="section-divider"></div><h3>Follow-up cohorts</h3><p class="note">Purposes and admission dates stay separate. Counts are listing enrollments; re-enrollment does not add a physical unit.</p><div class="table-wrap"><table><thead><tr><th>Cohort</th><th class="num">Enrolled</th><th class="num">Declared / verified</th><th class="num">Withdrawn</th><th class="num">Censored</th><th class="num">First check · h</th><th class="num">Repeat lateness · P95 h</th><th class="num">7d support</th></tr></thead><tbody>';
    html+=audit.cohorts.slice(0,12).map(c=>'<tr><td>'+esc(c.purpose)+' · '+esc(c.vertical)+'<div class="cell-sub">'+esc(c.cohort_date_utc)+'</div></td><td class="num">'+num(c.denominator_enrollments)+'</td><td class="num">'+num(c.evidence_counts?.sold_inferred)+' / '+num(c.evidence_counts?.sold_confirmed)+'</td><td class="num">'+num(c.evidence_counts?.withdrawn)+'</td><td class="num">'+num(c.censored_unknown)+'</td><td class="num">'+num(c.first_check_delay?.median_hours,1)+'</td><td class="num">'+num(c.repeat_lateness?.p95_hours,1)+'</td><td class="num">'+num(c.horizon_support?.['7d'])+'</td></tr>').join('');
    html+='</tbody></table></div><p class="note">'+num(audit.not_enrolled_listings)+' listings in this slice have no enrollment. '+num(audit.recorded_paused_cycles)+' paused collection cycles recorded. Marker time, missed checks and selection bias limit comparisons.'+(audit.cohort_groups_total>12?' Showing 12 of '+num(audit.cohort_groups_total)+' cohort groups.':'')+'</p>';
  }
  setHTML('liquidity',html);
}
function drawHealth(health){
  const running=health.running_actions||[];
  $('collector-badge').textContent=health.blocked?'Paused':health.collector_running?'Running':'Ready';$('collector-badge').className='badge '+(health.blocked?'red':health.collector_running?'purple':'green');
  setHTML('health',metric('Stored listings',num(health.observations))+metric('Last collection',ago(health.last_sweep_minutes_ago))+metric('Latest collection result',badge(health.last_sweep?.status||'No runs',health.last_sweep?.status==='ok'?'green':''))+metric('Requests in last run',num(health.last_sweep?.requests_made))+metric('New listings in last run',num(health.last_sweep?.listings_new))+metric('Rate limits in 24 hours',num(health.sweeps_throttled_24h))+metric('Collector',esc(running.map(row=>ACTION_LABELS[row.action]||row.action).join(', ')||health.active_locks.join(', ')||'Idle')));
  message('collection-stop',health.blocked?'Collection is paused after a platform '+(health.collection_stop?.status==='throttled'?'rate limit':'refusal')+'. Existing observations remain available. Review the collector status before resuming.':'','error');
  $('retention-note').textContent='Retention: raw responses '+health.retention.raw_days+' days · request audit '+health.retention.audit_days+' days.';
  if(health.collection_monitor)drawCollectionMonitor(health.collection_monitor);
}
function drawCollectionMonitor(monitor){
  const freshness=monitor.freshness||{},capacity=monitor.capacity||{},backup=freshness.backup||{},issues=freshness.issues||[],cohort=monitor.followup||{},controller=monitor.controller||{};
  const paused=freshness.status==='paused',stale=freshness.status==='stale';
  message('collection-freshness',issues.map(row=>row.message).join(' ')+(paused?' Collection remains paused for operator review.':''),stale?'error':'info');
  if(stale&&!lastSnapshot?.health?.collector_running){$('collector-badge').textContent='Stale';$('collector-badge').className='badge red';}
  $('capacity-badge').textContent=monitor.estimate_ready?'Seven-day measurement':num(monitor.complete_days)+' / 7 complete days';
  $('capacity-badge').className='badge '+(monitor.estimate_ready?'green':'amber');
  setHTML('collection-capacity',metric('Freshness',esc(freshness.status||'Unknown'))+metric('Configured detail attempts / day',num(capacity.daily_attempt_ceiling))+metric('Follow-up cohort / cap',num(cohort.active)+' / '+num(cohort.max_active))+metric('Baseline / opportunity / exploration',num(cohort.by_purpose?.baseline??cohort.sampled)+' / '+num(cohort.by_purpose?.opportunity??cohort.priority)+' / '+num(cohort.by_purpose?.exploration??0))+metric('Pending nominations',num(cohort.pending_intents??0))+metric('Quarantined checks',num(cohort.quarantined))+metric('Repeat demand / day · admitted cohort',num(capacity.repeat_demand_per_day))+metric('Repeat demand / day · all unresolved',num(capacity.all_unresolved_repeat_demand_per_day))+metric('Measured detail attempts / day',num(capacity.measured_attempts_per_day))+metric('Measured focused arrivals / day',num(capacity.focused_arrivals_per_day))+metric('Controller recommendation · cap',num(controller.recommended_max_active))+metric('Recovery backup',esc(backup.status||'Unknown')+(backup.age_hours==null?'':' · '+num(backup.age_hours,1)+' h old')));
  $('capacity-note').textContent=(capacity.repeat_demand_exceeds_ceiling?'Daily repeat demand exceeds the configured attempt ceiling. ':'')+(monitor.estimate_ready?'Capacity reflects seven complete tracking days. ':'Capacity is provisional until seven complete days under the current settings. ')+(cohort.note||'')+' Controller recommendations are advisory. The backup is local; separate storage is still needed.';
  const review=monitor.review||{},labels={collecting:'Collecting evidence',needs_attention:'Needs attention',ready_for_review:'Targets met'};
  $('collection-capacity').innerHTML+=metric('Seven-day operating review',esc(labels[review.status]||'Awaiting current evidence'));
  const jobNames={sweep:'New listing searches',itempage:'Listing status checks',enrich:'Item classification'};
  const jobStates={succeeded:'Completed',running:'Running',partial:'Partial',failed:'Needs attention',interrupted:'Interrupted',paused:'Paused',skipped:'Skipped'};
  for(const [key,label] of Object.entries(jobNames)){
    const job=(freshness.jobs||{})[key];
    if(job)$('collection-capacity').innerHTML+=metric(label,esc(jobStates[job.status]||job.status)+(job.success_age_hours==null?' · no completed run recorded':' · last completed '+num(job.success_age_hours,1)+' h ago'));
  }
  const issuesText=Object.values(review.gates||{}).filter(gate=>gate.status==='fail').map(gate=>gate.name).join(', ');
  if(issuesText)$('capacity-note').textContent+=' Review targets needing attention: '+issuesText+'.';
}
let collectionMonitorLoading=false;
async function refreshCollectionMonitor(){
  if(collectionMonitorLoading)return;collectionMonitorLoading=true;
  try{const monitor=await call('/api/collection-health');if(lastSnapshot)lastSnapshot.health.collection_monitor=monitor;drawCollectionMonitor(monitor);loadCapacityVerdict();}
  catch(error){message('collection-freshness','Collection freshness could not be checked. '+error.message,'error');}
  finally{collectionMonitorLoading=false;}
}
function drawCoverage(coverage){
  const tier=coverage.tier1_national||{},focused=coverage.configured||{},rotation=coverage.rotation||{},metadata=coverage.sitemap_refresh||{};
  setHTML('coverage',progress('Configured active grid',focused.grid_coverage_pct)+'<div class="metric-list">'+metric('Configured cells collected',num((focused.targets||0)-(focused.never_crawled||0))+' / '+num(focused.targets))+metric('Configured cells still to visit',num(focused.never_crawled))+metric('First pages still unmeasured',num(rotation.first_page_unmeasured))+metric('First pages beyond maximum target age',num(rotation.first_page_overdue))+metric('Oldest first-page observation',rotation.oldest_first_page_age_hours==null?'—':num(rotation.oldest_first_page_age_hours,1)+' h ago')+metric('Pending page continuations',num(rotation.continuations))+metric('Traversals capped at configured depth',num(rotation.capped_targets))+metric('Desired refresh interval',num(rotation.desired_refresh_hours)+' h')+metric('Minimum requests / hour for this interval',num(rotation.minimum_requests_per_hour,2))+metric('Reserved rotation requests / hour',num(rotation.reserved_rotation_requests_per_hour,2))+metric('Sitemap refresh',metadata.retry_at?'Retry after '+date(metadata.retry_at):metadata.in_progress?'In progress · '+num(metadata.documents_pending)+' documents':metadata.last_completed_at?date(metadata.last_completed_at):'Awaiting first refresh')+metric('Top-level national coverage',percent(tier.national_coverage_pct))+metric('Full national grid coverage',percent(coverage.grid_coverage_pct))+metric('Municipalities in national grid',num(coverage.municipalities))+metric('Median time since collection',coverage.median_hours_since_crawl==null?'—':num(coverage.median_hours_since_crawl,1)+' h')+'</div><p class="note" style="margin-top:12px">'+esc(rotation.coverage_note||'Coverage counts observed grid cells, not every listing in each cell.')+(rotation.desired_interval_feasible_at_reserved_share===false?' The current budget cannot meet the desired refresh interval.':'')+'</p>');
}
function followupHTML(row){
  if(!row)return empty('Coverage is loading','Refresh to read the local collection backlog.');
  if(row.queue?.enabled){
    const queue=row.queue;
    return progress('Has a known condition',row.condition_coverage==null?null:row.condition_coverage*100)+progress('Current accepted classification',row.classification_coverage==null?null:row.classification_coverage*100)
      +'<div class="metric-list">'+metric('Eligible unresolved discoveries',num(queue.eligible))+metric('Admitted to follow-up',num(queue.admitted))+metric('Discovered, not admitted',num(queue.not_admitted))+metric('Admitted with a successful check',num(queue.successful))+metric('Awaiting first successful check',num(queue.awaiting_first))+metric('Due first / repeat checks',num(queue.due_first)+' / '+num(queue.due_repeat))+metric('Due retries (included above)',num(queue.retry_due))+metric('In progress / quarantined',num(queue.leased)+' / '+num(queue.quarantined))+metric('Overdue repeat checks',num(queue.overdue_repeat))+metric('Minimum full-budget runs',queue.minimum_runs==null?'—':num(queue.minimum_runs)+' at '+num(row.item_budget)+' items/run')+'</div><p class="note" style="margin-top:16px">Queued counts include only admitted tasks ready and due now. Failed attempts do not establish successful checks. Minimum runs is a lower bound at the shared budget; retry limits, other categories and new work add time. Discoveries outside admission remain available to browse.</p>';
  }
  return progress('Has a known condition',row.condition_coverage==null?null:row.condition_coverage*100)+progress('Current accepted classification',row.classification_coverage==null?null:row.classification_coverage*100)+'<div class="metric-list">'+metric('Tracked / within collection focus',num(row.n_tracked)+' / '+num(row.n_in_collection_scope))+metric('Status-check attempts',num(row.n_probe_attempted)+' · '+percent(row.probe_attempt_coverage==null?null:row.probe_attempt_coverage*100))+metric('Never probed · unresolved',num(row.n_never_probed))+metric('Probes due / repeat checks due',num(row.n_due)+' / '+num(row.n_due_repeat))+metric('Due within collection focus',num(row.n_due_in_scope))+metric('Repeat slots reserved · entire focus',num(row.repeat_probe_reserve)+' / '+num(row.item_budget))+metric('Oldest due repeat in focus',row.oldest_due_repeat_hours==null?'—':num(row.oldest_due_repeat_hours,1)+' h')+metric('Fresh available / fresh priced',num(row.n_fresh_available)+' / '+num(row.n_fresh_priced))+metric('Oldest due in focus',row.oldest_due_hours==null?'—':num(row.oldest_due_hours,1)+' h')+metric('Minimum full-budget runs',row.probe_runs_at_budget==null?'—':num(row.probe_runs_at_budget)+' at '+num(row.item_budget)+' items/run')+'</div><p class="note" style="margin-top:16px">'+esc(row.capacity_note)+'</p>'+(row.n_due>row.n_due_in_scope?'<p class="note" style="margin-top:8px">'+num(row.n_due-row.n_due_in_scope)+' due items are outside collection focus. Viewing a segment does not change collector targeting.</p>':'');
}
function applyRunBudgets(row){
  // The scheduled policy and the ceiling a browser may ask for come from the
  // server, so the form cannot drift away from what the run will actually do.
  const policy=row?.sweep_policy,caps=policy?.caps||{},budget=$('budget-sweep'),pages=$('pages-sweep');
  if(budget&&Number.isFinite(caps.budget)){
    budget.max=String(caps.budget);
    const cap=$('sweep-budget-cap');if(cap)cap.textContent=num(caps.budget);
  }
  if(pages&&Number.isFinite(caps.pages))pages.max=String(caps.pages);
  for(const [action,value] of Object.entries(row?.run_budgets||{})){
    const input=$('budget-'+action);
    if(!input||input.dataset.configured||input.dataset.edited||!Number.isFinite(value))continue;
    input.value=String(Math.max(Number(input.min)||1,Math.min(Number(input.max)||Infinity,Math.floor(value))));
    input.dataset.configured='1';
  }
  if(pages&&!pages.dataset.configured&&!pages.dataset.edited&&Number.isFinite(policy?.pages)){
    pages.value=String(Math.max(Number(pages.min)||1,Math.min(Number(pages.max)||Infinity,Math.floor(policy.pages))));
    pages.dataset.configured='1';
  }
  const note=$('sweep-policy-note');
  if(note&&policy)note.textContent='Scheduled run: '+num(policy.budget)+' search pages at depth '+num(policy.pages)+', refreshed every '+num(Math.round(policy.refresh_hours))+'h ('+num(Math.round(policy.productive_refresh_hours))+'h when productive, '+num(Math.round(policy.max_refresh_hours))+'h maximum). Settings above override the schedule for this run only.';
}
async function loadCapacityVerdict(){
  const badge=$('capacity-verdict-badge');
  try{
    const data=await call('/api/collection/capacity');
    badge.textContent=data.verdict.replaceAll('_',' ');
    badge.className='badge '+(data.verdict==='within_capacity'?'green':data.verdict==='unavailable'?'':'amber');
    setHTML('collection-capacity-verdict',capacityPanel(data));
    setHTML('collection-unresolved',unresolvedPanel(data.unresolved));
  }catch(error){
    // Capacity is advisory. A failure here must not take Collection down.
    badge.textContent='Unavailable';badge.className='badge';
    setHTML('collection-capacity-verdict','<p class="note">'+esc(error.message)+'</p>');
    setHTML('collection-unresolved','');
  }
}
function drawCollection(row){applyRunBudgets(row);setHTML('collection-followup',followupHTML(row));$('collection-focus').textContent='Categories: '+((row?.focus_category_names||[]).join(', ')||'All categories')+' · Municipalities: '+((row?.focus_municipalities||[]).join(', ')||'All municipalities');}
function drawHistory(history){
  const days=history?.days||[],selector=$('history-day');
  const selected=selector.value||days.at(-1)?.day;
  selector.innerHTML=days.map(row=>'<option value="'+esc(row.day)+'">'+esc(row.day)+' · '+esc(row.status)+'</option>').join('');
  selector.value=days.some(row=>row.day===selected)?selected:(days.at(-1)?.day||'');
  selector.onchange=()=>drawHistory(lastSnapshot?.history);
  const partition=(history?.partitions||[]).find(row=>row.kind==='market'&&row.period_day===selector.value);
  const status=days.find(row=>row.day===selector.value)?.status;
  // "This day updates as observations arrive" is a statement about a collector that
  // is running. On a deployment replaying an export it is simply false, and it is
  // the kind of false that a reader acts on -- waiting for a change that cannot come.
  const collecting=canWrite(capabilities,'/api/action');
  $('history-status').textContent=!history?.tracking_started_day?'History has not started.':status==='missing'?(selector.value<history.tracking_started_day?'This day predates the start of this history series.':'No snapshot was recorded for this day.'):status==='suppressed'?'A snapshot was saved; its groups are too small to release.':'Snapshot saved '+date(partition?.captured_at)+'. '+(partition?.state==='frozen'?'This day is closed.':collecting?'This day updates as observations arrive.':'This day is frozen at the last export.');
  setHTML('history-rows',(partition?.cells||[]).map(cell=>'<tr><td>'+esc(cell.vertical)+'</td><td>'+esc(cell.market_segment)+' / '+esc(cell.condition)+'</td><td class="num">'+num(cell.counts?.retained_listings)+'</td><td class="num">'+num(cell.counts?.fresh_available_listings)+'</td><td class="num">'+num(cell.counts?.price_change_events_today)+'</td><td>'+esc(cell.asking_prices?.median_band_eur?('€'+cell.asking_prices.median_band_eur):'Insufficient support')+'</td></tr>').join('')||'<tr><td colspan="6" class="muted">No released groups for this day.</td></tr>');
}
function drawDiagnostics(snapshot){
  const unmet=snapshot.compliance.blocking||[];
  $('setup-badge').textContent=unmet.length?num(unmet.length)+' setup items':'Setup complete';$('setup-badge').className='badge '+(unmet.length?'amber':'green');
  const names={lia_signed_off:'Legitimate-interest assessment needs sign-off',lia_controller_named:'Assessment needs the controller name',art14_controller_named:'Privacy notice needs the controller name',contact_email_deliverable:'Collector contact address is missing'};
  setHTML('comp','<h3>Collection setup</h3>'+(unmet.length?'<p>'+unmet.map(key=>esc(names[key]||key.replaceAll('_',' '))).join('<br>')+'</p><p>Confirm the controller in legal/LIA.md and legal/ART14_NOTICE.md. After reviewing the assessment, record SIGNED OFF, the signer, sign-off date and balancing conclusion in legal/LIA.md.</p>':'<p>All configured setup checks are complete.</p>')+'<p>'+esc(snapshot.compliance.note||'')+'</p>');
  setHTML('posture','<h3>Private observation workspace</h3><p>'+esc(snapshot.posture.bind)+'. Refreshing this page reads stored data. Only an explicit collection action requests the platform.</p>');
  const authority=snapshot.permissions;
  if(authority){$('comp').innerHTML+='<h3>Recorded collection authority</h3>'+(authority.valid?Object.entries(authority.actions||{}).map(([name,row])=>'<p>'+esc(name.replaceAll('_',' '))+': '+badge(row.allowed?'Covered':'Requires review',row.allowed?'green':'amber')+(row.reason?' '+esc(row.reason):'')+'</p>').join(''):'<p>'+esc(authority.error||'Registry needs review')+'</p>')+'<p>'+esc(authority.note||'')+'</p>';}
  const enrichment=snapshot.enrichment||{};
  const classificationDetails=classificationQuality(enrichment.queue?.classification_quality,enrichment.queue?.recent_throughput);
  setHTML('enrich','<div class="metric-list">'+metric('Local model',badge(enrichment.provider_reachable?'Available':'Offline',enrichment.provider_reachable?'green':''))+metric('Items classified',num(enrichment.enriched_listings))+metric('Current accepted labels in focus',num(enrichment.accepted_confident))+metric('Classification tasks pending',num(enrichment.queue?.pending))+metric('Classification retries / needs review',num(enrichment.queue?.retry)+' / '+num(enrichment.queue?.quarantined))+metric('Vocabulary terms',num(enrichment.vocabulary?.product_types))+metric('Schema violations',num(enrichment.schema_violations))+'</div>'+(enrichment.provider_error?'<p>'+esc(enrichment.provider_error)+'</p>':'')+'<p>Classifications are derived values. Prices and outcomes come from observations.</p>'+classificationDetails+'<div class="metric-list">'+metric('Parser failures · recent requests',num(snapshot.parser_health?.parse_errors))+metric('Search fallback share',snapshot.parser_health?.search_fallback_share==null?'—':percent(snapshot.parser_health.search_fallback_share*100))+'</div>');
}
function runResult(row){if(row.returncode===0)return badge('Completed','green');if(row.returncode===69)return badge('Blocked','red');if(row.returncode===75)return badge('Rate limited','amber');return badge('Failed','red');}
function drawActions(rows,health){
  let html=(health.running_actions||[]).map(row=>'<tr><td>'+date(row.started_at)+'</td><td>'+esc(ACTION_LABELS[row.action]||row.action)+'</td><td>'+badge('Running','purple')+'</td><td class="num">'+num((Date.now()-new Date(row.started_at))/1000,1)+' s</td><td></td></tr>').join('');
  html+=rows.map(row=>{
    const id=String(row.action_log_pk),output=[row.stdout_tail,row.stderr_tail].filter(Boolean).join('\n');
    return '<tr><td class="muted">'+date(row.started_at)+'</td><td>'+esc(ACTION_LABELS[row.action]||row.action)+'</td><td>'+runResult(row)+'</td><td class="num muted">'+num(row.seconds,1)+' s</td><td class="num">'+(output?'<button class="btn quiet small" data-run="'+esc(id)+'" type="button" aria-expanded="'+openRunDetails.has(id)+'">View output</button>':'—')+'</td></tr>'+(output?'<tr class="run-detail" data-run-detail="'+esc(id)+'" '+(openRunDetails.has(id)?'':'hidden')+'><td colspan="5"><pre>'+esc(output)+'</pre></td></tr>':'');
  }).join('');
  setHTML('actlog',html||'<tr><td colspan="5">'+empty('Your runs will appear here','Start a small collection or generate a report.','collect')+'</td></tr>');
}
function drawLedger(snapshot){
  ledgerBuys=snapshot.buys||[];
  const inventory=snapshot.inventory||[],economics=snapshot.economics||{};
  setHTML('ledger-summary',[['Purchases recorded',economics.purchases_recorded,'Your own acquisition history'],['Completed sales',(economics.sold_with_complete_costs||0)+(economics.sold_with_unknown_costs||0),'Recorded sale prices'],['Median trade margin',economics.median_gross_margin_eur==null?'—':eur(economics.median_gross_margin_eur),economics.status==='OK'?'Fully known costs only':'Needs '+num(economics.min_n_threshold)+' sales with known costs']].map(([label,value,note])=>'<section class="card stat"><div class="stat-top">'+esc(label)+'</div><div class="stat-value">'+(typeof value==='number'?num(value):value)+'</div><div class="stat-note">'+esc(note)+'</div></section>').join(''));
  const byPurchase=new Map(inventory.map(row=>[row.purchase_pk,row]));
  setHTML('econ',ledgerBuys.length?'<div class="table-wrap"><table class="ledger-table"><thead><tr><th>Item / source</th><th>Purchased</th><th class="num">Cost</th><th class="num">Sale price</th><th>Status</th><th class="num">Margin</th><th></th></tr></thead><tbody>'+ledgerBuys.map(purchase=>{
    const trade=byPurchase.get(purchase.purchase_pk),sold=trade?.status&&trade.status!=='in_stock',managed=trade?.trade_economics;
    const label=managed?(STOCK_PHASES[managed.stock_phase]||managed.stock_phase):sold?'Sold':'In stock';
    const action=managed?'<button class="btn quiet small" type="button" data-ledger-order="'+esc(managed.current_order.order_id)+'">Review order</button>':purchase.inventory_sku?'<button class="btn quiet small" type="button" data-go="orders">Record SKU sale</button>':purchase.source_listing_id?'<button class="btn quiet small" type="button" data-sell="'+purchase.purchase_pk+'">'+(sold?'Update sale':'Record sale')+'</button>':'<button class="btn quiet small" type="button" data-go="inventory">Review owned item</button>';
    return '<tr><td><strong>'+esc(purchase.title_raw)+'</strong><div class="observation-meta">'+esc(LABELS[purchase.platform]||purchase.platform)+(purchase.source_listing_id?' · #'+esc(purchase.source_listing_id):' · offline/source ID unavailable')+'</div></td><td class="muted">'+date(purchase.purchased_at)+'</td><td class="num">'+eur(purchase.acquisition_cost_eur)+'</td><td class="num">'+eur(trade?.final_sale_price_eur??managed?.net_sale_receipts_eur)+'</td><td>'+badge(label,sold?'green':'purple')+'</td><td class="num">'+(trade?.margin?eur(trade.margin.gross_margin_eur):managed?'<span class="muted" title="'+esc(managed.reasons.join(' '))+'">Pending</span>':sold?'<span class="muted" title="One or more costs are unknown">Costs unknown</span>':'—')+'</td><td class="num">'+action+'</td></tr>';
  }).join('')+'</tbody></table></div>':empty('Your first trade starts here','Record a purchase, then add its sale to track the actual result.','wallet'));
  const saleBuys=ledgerBuys.filter(purchase=>purchase.source_listing_id&&!purchase.inventory_sku);
  const options=saleBuys.map(purchase=>'<option value="'+purchase.purchase_pk+'">'+esc(purchase.title_raw)+' · '+eur(purchase.acquisition_cost_eur)+(byPurchase.get(purchase.purchase_pk)?.status==='in_stock'?'':' · recorded sale')+'</option>').join('');
  if(options!==lastPurchaseOptions){const selected=$('purchase-options').value;$('purchase-options').innerHTML='<option value="">Choose a purchase</option>'+options;$('purchase-options').value=selected;lastPurchaseOptions=options;}
  $('sale-fields').disabled=!saleBuys.length;$('sale-empty-note').hidden=!!saleBuys.length;
}
function selectSale(pk){
  $('purchase-options').value=String(pk||'');
  const trade=(lastSnapshot?.inventory||[]).find(row=>String(row.purchase_pk)===String(pk));
  const form=$('sale-form');
  form.elements.final_sale_price_eur.value=trade?.final_sale_price_eur??'';
  form.querySelectorAll('.optional-cost').forEach(input=>input.value=trade?.sale_costs?.[input.name]??'');
}
const UNIT_OUTCOMES={unknown:'Unknown',pass:'Pass',fail:'Fail',not_applicable:'Not applicable'};
const UNIT_STATUS={allowed:'Ready for preparation',blocked:'Blocked',requires_review:'Needs review'};
const UNIT_STATES={in_stock:'In stock',quarantined:'Quarantined',retired:'Retired'};
const UNIT_KINDS={technology:'Technology',bicycle:'Bicycle',scooter:'Scooter'};
function unitOptions(values,selected){return Object.entries(values).map(([key,label])=>'<option value="'+esc(key)+'"'+(selected===key?' selected':'')+'>'+esc(label)+'</option>').join('');}
function unitStepShow(step){
  if(ownedOutcomeEditor?.isBusy()&&step!==unitStep)return;
  if(!['facts','inspection','eligibility','history','outcomes'].includes(step))step='facts';unitStep=step;
  for(const name of ['facts','inspection','eligibility','history','outcomes'])if($('unit-panel-'+name))$('unit-panel-'+name).hidden=name!==step;
  document.querySelectorAll('[data-unit-step]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.unitStep===step)));
  if(step==='outcomes'&&selectedUnit)loadOwnedOutcomes();
}
function loadOwnedOutcomes(options={}){
  if(!selectedUnit||!$('owned-outcome-editor'))return;
  if(!ownedOutcomeEditor)ownedOutcomeEditor=initOwnedOutcomes({root:$('owned-outcome-editor'),call,onSaved:async()=>{
    const unit=await call('/api/inventory?'+new URLSearchParams({sku:selectedUnit.sku}));
    // Keep the revision that the facts/inspection forms actually display.
    if(unit.version!==selectedUnit.version)message('unit-changed','The item revision changed. Reload the item before editing its facts, inspection or stock state.');
    else selectedUnit=unit;
    if(currentView==='workflow')await loadWorkflow();if(currentView==='journal')await loadResearch();
  }});
  return ownedOutcomeEditor.load(selectedUnit.purchase_pk,{unitVersion:selectedUnit.version,...options});
}
async function openOwnedOutcome(sku,panel='review',decisionPk=null){
  if(ownedOutcomeEditor?.isBusy()){toast('Finish saving this evidence first.',true);return;}
  for(const id of ['journal-detail-dialog','listing-dialog'])if($(id).open)$(id).close();
  unitStep='facts';showView('inventory');await openUnit(sku);
  if(selectedUnit?.sku!==sku)return;
  unitStepShow('outcomes');await loadOwnedOutcomes({panel,decisionPk});
}
async function loadInventory(){
  const sequence=++inventorySequence;$('inventory-content').setAttribute('aria-busy','true');
  try{
    const data=await call('/api/inventory?'+new URLSearchParams({search:$('inventory-search').value,state:$('inventory-state').value,page:inventoryPage}));
    if(sequence!==inventorySequence)return;inventoryData=data;inventoryPage=data.page;
    setHTML('inventory-summary',Object.entries(UNIT_STATES).map(([state,label])=>'<section class="card stat"><div class="stat-top">'+esc(label)+'</div><div class="stat-value">'+num(data.counts[state]||0)+'</div><div class="stat-note">'+(state==='in_stock'?'Physical inspection required':state==='quarantined'?'Held for review':'Removed from sale preparation')+'</div></section>').join(''));
    setHTML('inventory-rows',data.items.length?data.items.map(row=>'<tr><td><button class="row-link" type="button" data-owned-sku="'+esc(row.sku)+'">'+esc(row.title)+'</button><div class="observation-meta unit-sku">'+esc(row.sku)+'</div></td><td>'+esc(UNIT_KINDS[row.kind])+'</td><td>'+badge(UNIT_STATES[row.state],row.state==='quarantined'?'amber':row.state==='retired'?'':'purple')+'</td><td class="num">'+eur(row.acquisition_cost_eur)+'</td><td>'+esc(LABELS[row.platform]||row.platform)+(row.source_listing_id?'<div class="observation-meta">#'+esc(row.source_listing_id)+'</div>':'')+'</td><td><button class="btn quiet small" data-owned-sku="'+esc(row.sku)+'" type="button">Review item</button></td></tr>').join(''):'<tr><td colspan="6">'+empty('Your owned stock starts here','Add an item you acquired, or link an unsold purchase from the trade ledger.','wallet')+'</td></tr>');
    $('inventory-count').textContent=num(data.total)+' matching owned items';$('inventory-page').textContent='Page '+num(data.page)+' / '+num(data.pages);
    $('inventory-prev').disabled=data.page<=1;$('inventory-next').disabled=data.page>=data.pages;
    // Preserve an open acquisition form and every unsaved inspection during polling.
    if(!$('inventory-dialog').open)drawUnitPurchaseOptions();
    const listed=selectedUnit&&data.items.find(row=>row.sku===selectedUnit.sku);
    if(listed&&listed.version!==selectedUnit.version){message('unit-changed','This item changed in another session. Reload it before saving.');drawEligibility({...selectedUnit.local_preview,status:'requires_review',stale:true});}
    if($('inventory-message').classList.contains('error'))$('inventory-message').hidden=true;
    loadAcquisitionQueue();
  }catch(error){if(sequence===inventorySequence)message('inventory-message',error.message,'error');}
  finally{if(sequence===inventorySequence)$('inventory-content').setAttribute('aria-busy','false');}
}
async function loadAcquisitionQueue(){
  const rows=$('acquisition-queue-rows');if(!rows)return;
  if(!capabilities.reads.has('/api/research/acquisition-queue')){const section=rows.closest?.('section');if(section)section.hidden=true;return;}
  try{
    const data=await call('/api/research/acquisition-queue');
    setHTML('acquisition-queue-summary',acquisitionQueueSummary(data));
    setHTML('acquisition-queue-rows',acquisitionQueueRows(data));
  }catch(error){
    // A failed queue must not take the inventory view down with it.
    setHTML('acquisition-queue-rows','<tr><td colspan="4">'+empty('The work queue is unavailable',
      error.message,'chart')+'</td></tr>');
  }
}
function drawUnitPurchaseOptions(){
  const select=$('unit-purchase'),value=select.value;
  setHTML('unit-purchase','<option value="">Record a new acquisition</option>'+(inventoryData?.unlinked_purchases||[]).map(row=>'<option value="'+row.purchase_pk+'">'+esc(row.title_raw)+' · '+eur(row.acquisition_cost_eur)+'</option>').join(''));select.value=value;
}
function acquisitionMode(){
  const form=$('unit-create-form'),linked=!!$('unit-purchase').value;$('new-acquisition-fields').disabled=linked;
  const row=inventoryData?.unlinked_purchases.find(row=>String(row.purchase_pk)===$('unit-purchase').value);
  if(row)form.elements.title.value=row.title_raw;
  $('unit-source-id').disabled=$('unit-source').value==='offline';$('unit-market').disabled=linked||$('unit-source').value!=='tori';
}
function newOwnedUnit(purchasePk=null){
  if(unitBusy||ownedOutcomeEditor?.isBusy())return;
  $('unit-create-form').reset();drawUnitPurchaseOptions();if(purchasePk)$('unit-purchase').value=String(purchasePk);acquisitionMode();
  acquisitionDecision=null;if($('unit-receipt-fields')){$('unit-receipt-fields').hidden=true;$('unit-receipt-fields').disabled=true;}
  setHTML('unit-acquisition-context','');
  $('inventory-create-message').hidden=true;$('inventory-dialog').showModal();$('unit-create-form').elements.title.focus();
}
async function openUnit(sku,afterSave=false){
  if(unitBusy&&!afterSave)return;
  if(ownedOutcomeEditor?.isBusy()&&sku!==selectedUnit?.sku)return;
  if(!afterSave&&selectedUnit&&sku!==selectedUnit.sku&&(unitDirty||ownedOutcomeEditor?.hasDraft())){message('inventory-message','Save the current item’s edits, or close the item and discard them, before opening another SKU.','error');return;}
  const sequence=++unitSequence;
  try{const unit=await call('/api/inventory?'+new URLSearchParams({sku}));if(sequence!==unitSequence)return;selectedUnit=unit;drawUnit();$('unit-detail').hidden=false;}
  catch(error){if(sequence===unitSequence)message('inventory-message',error.message,'error');}
}
function drawUnit(){
  const unit=selectedUnit;if(!unit)return;
  unitDirty=false;
  $('unit-title').textContent=unit.title;$('unit-sku').textContent=unit.sku;$('unit-version').textContent='Revision '+unit.version+' · facts '+unit.facts_version;
  $('unit-changed').hidden=true;$('unit-purchase-context').textContent='Purchase #'+unit.purchase_pk+' · '+(LABELS[unit.purchase.platform]||unit.purchase.platform)+' · '+eur(unit.purchase.acquisition_cost_eur)+' paid. Inbound shipping: '+eur(unit.purchase.shipping_cost_eur)+'; buyer fee: '+eur(unit.purchase.buyer_fee_eur)+'. Blank costs stay unknown.';
  const form=$('unit-facts-form');form.elements.title.value=unit.title;form.elements.kind.value=unit.kind;form.elements.state.value=unit.state;form.elements.reason.value='';
  const facts=unit.facts,fields=[['brand','Brand'],['model','Model']];
  let html=fields.map(([key,label])=>'<label class="field">'+esc(label)+'<input name="'+key+'" maxlength="100" value="'+esc(facts[key]||'')+'"></label>').join('');
  for(const [key,label,values] of [['condition','Physical condition',Object.fromEntries(['unknown','new_unused','like_new','very_good','good','fair','poor','for_parts','refurbished'].map(key=>[key,LABELS[key]]))],['battery_presence','Battery present',{unknown:'Unknown',present:'Present',absent:'Absent'}],['mains_powered','Uses mains power',{unknown:'Unknown',yes:'Yes',no:'No'}],['lock_applicable','Device restrictions apply',{unknown:'Unknown',yes:'Yes',no:'No'}]])html+='<label class="field">'+esc(label)+'<select name="'+key+'">'+unitOptions(values,facts[key])+'</select></label>';
  html+='<label class="field">Battery capacity (Wh)<input name="battery_wh" type="number" min="0" max="5000" step="0.01" value="'+esc(facts.battery_wh??'')+'" placeholder="Unknown"></label><label class="field">Manufacture date (if evidenced)<input name="manufactured_on" type="date" value="'+esc(facts.manufactured_on||'')+'"></label>';setHTML('unit-facts-fields',html);
  const latest=unit.latest_inspection,current=latest?.facts_version===unit.facts_version,checks=current?latest.checks:{};
  $('unit-inspection-form').reset();if(current){$('unit-inspection-form').elements.inspector.value=latest.inspector;$('unit-inspection-form').elements.evidence_ref.value=latest.evidence_ref;$('unit-inspection-form').elements.competent.checked=latest.competent;}
  message('inspection-context',latest?(current?'Latest inspection: '+date(latest.recorded_at)+'. Saving creates a new snapshot.':'The previous inspection covers older facts. Inspect this revision before preparing a listing.'):'No inspection recorded yet.');
  setHTML('owned-checks',unit.checks_template.map(check=>{const value=checks[check.key]||{outcome:'unknown',reason:''};return '<section class="owned-check"><div class="owned-check-head"><strong>'+esc(check.label)+'</strong><small>'+esc(check.applicability.replaceAll('_',' '))+'</small></div><div class="form-grid"><label class="field">Result<select data-inspection-outcome="'+check.key+'">'+unitOptions(UNIT_OUTCOMES,value.outcome)+'</select></label><label class="field">Reason / measured evidence<input data-inspection-reason="'+check.key+'" maxlength="1000" value="'+esc(value.reason)+'" placeholder="Explain observed results or why it does not apply"></label></div></section>';}).join(''));
  setHTML('channel-review-fields',['permission','account','product_policy'].map(key=>'<section class="owned-check"><div class="owned-check-head"><strong>'+esc({permission:'Seller action permission',account:'Appropriate seller account',product_policy:'Current product and shipping rules'}[key])+'</strong></div><div class="form-grid"><label class="field">Review<select data-channel-outcome="'+key+'">'+unitOptions({unknown:'Unknown',pass:'Confirmed',fail:'Denied'},'unknown')+'</select></label><label class="field">Evidence reference<input data-channel-evidence="'+key+'" maxlength="500" placeholder="Scope/account/rule evidence"></label><label class="field wide">Reason<input data-channel-reason="'+key+'" maxlength="1000" placeholder="What does this evidence establish?"></label></div></section>').join(''));
  $('channel-review-fields').hidden=$('eligibility-channel').value==='local_draft';
  eligibilityChannelChoice();
  setHTML('unit-history',unit.history.map(row=>'<article><strong>'+esc(row.event_type)+' · revision '+num(row.unit_version)+'</strong><p>'+esc(row.reason)+'</p><p class="note">'+date(row.recorded_at)+'</p></article>').join('')+unit.inspections.map(row=>'<article><strong>Inspection #'+row.inspection_pk+' · facts '+row.facts_version+'</strong><p>'+esc(row.inspector)+' · '+date(row.recorded_at)+'</p><p class="note">Evidence: '+esc(row.evidence_ref||'Unknown')+'</p><details><summary>Recorded results</summary>'+Object.entries(row.checks).map(([key,check])=>'<p>'+esc(key.replaceAll('_',' '))+': '+esc(UNIT_OUTCOMES[check.outcome])+' — '+esc(check.reason||'Unverified')+'</p>').join('')+'</details></article>').join('')+unit.evaluations.map(row=>'<article><strong>'+esc(CHANNEL_NAME(row.channel))+' · '+esc(UNIT_STATUS[row.status])+'</strong><p>'+date(row.recorded_at)+(row.stale?' · stale; review required':'')+'</p><p class="note">'+esc(row.rule_version)+' · item revision '+row.unit_version+'</p></article>').join(''));
  unitStepShow(unitStep);
  if(unitBusy)unitSaving(true);
}
function CHANNEL_NAME(channel){return {local_draft:'Local preparation',ebay:'eBay',tradera:'Tradera',tori:'Tori',etsy:'Etsy vintage'}[channel]||channel;}
function eligibilityChannelChoice(){
  if(!selectedUnit)return;
  const channel=$('eligibility-channel').value,result=selectedUnit.evaluations.find(row=>row.channel===channel);
  $('channel-review-fields').hidden=channel==='local_draft';
  for(const input of $('channel-review-fields').querySelectorAll('[data-channel-outcome]')){
    const key=input.dataset.channelOutcome,review=result&&!result.stale?result.reviews[key]:null;
    input.value=review?.outcome||'unknown';
    $('channel-review-fields').querySelector('[data-channel-reason="'+key+'"]').value=review?.reason||'';
    $('channel-review-fields').querySelector('[data-channel-evidence="'+key+'"]').value=review?.evidence_ref||'';
  }
  drawEligibility(result||(channel==='local_draft'?selectedUnit.local_preview:{...selectedUnit.local_preview,channel,status:'requires_review',reasons:[{message:'Save a preview to evaluate this channel with your recorded review evidence.'}],sources:[]}));
}
function drawEligibility(result){
  const tone=result.status==='blocked'?'red':result.status==='allowed'?'green':'amber';
  setHTML('eligibility-result','<section class="eligibility-result"><div class="card-heading"><h3>'+esc(CHANNEL_NAME(result.channel))+'</h3>'+badge(UNIT_STATUS[result.status],tone)+'</div>'+(result.recorded_at?'<p class="note">Saved preview · '+date(result.recorded_at)+'</p>':'')+(result.stale?'<p class="inventory-help">This historical preview is stale. Its earlier result does not apply to the current item.</p>':'')+(result.reasons.length?'<ul>'+result.reasons.map(reason=>'<li>'+esc(reason.message)+'</li>').join('')+'</ul>':'<p class="inventory-help">The recorded facts and checks support local listing preparation.</p>')+'<p class="inventory-help">'+esc(result.note)+'</p><p class="note">Rule '+esc(result.rule_version)+' · item revision '+num(result.unit_version)+' · '+(result.inspection_pk?'inspection #'+num(result.inspection_pk):'no inspection')+'</p>'+(result.sources?.length?'<p class="inventory-help">Official reference: '+result.sources.map(url=>'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(CHANNEL_NAME(result.channel))+' guidance</a>').join(', ')+'</p>':'')+'</section>');
}
function inventoryToken(action,params){
  const fingerprint=JSON.stringify(params),key=action+':'+(params.sku||'new'),previous=inventoryRetries.get(key);
  if(previous?.fingerprint===fingerprint)return previous.token;
  const token=crypto.randomUUID();inventoryRetries.set(key,{fingerprint,token});return token;
}
function unitSaving(on){
  if(on){
    const controls=[...$('unit-detail').querySelectorAll('input,select,textarea,button'),...$('unit-create-form').querySelectorAll('input,select,textarea,button,fieldset'),$('new-unit')];
    if(currentView==='inventory')controls.push($('heading-action'));
    for(const node of controls){if(!inventoryDisabled.has(node))inventoryDisabled.set(node,node.disabled);node.disabled=true;}
  }else{for(const [node,disabled] of inventoryDisabled)node.disabled=disabled;inventoryDisabled.clear();}
  $('unit-detail').setAttribute('aria-busy',String(on));$('unit-create-form').setAttribute('aria-busy',String(on));
}
async function saveInventoryForm(event,action){
  event.preventDefault();if(unitBusy)return;
  const form=event.target;if(!form.reportValidity())return;
  const values=Object.fromEntries(new FormData(form));let params={};
  if(action==='create'){
    params={kind:values.kind,title:values.title};if(values.sku)params.sku=values.sku;
    if(values.purchase_pk)params.purchase_pk=Number(values.purchase_pk);
    else for(const key of ['platform','subplatform','source_listing_id','acquisition_cost_eur','shipping_cost_eur','buyer_fee_eur','purchased_on'])if(values[key])params[key]=key.endsWith('_eur')?Number(values[key]):key==='purchased_on'?new Date(values[key]).toISOString():values[key];
    if(acquisitionDecision){params.acquisition_receipt={decision_pk:acquisitionDecision.decision_pk,reason:values.receipt_reason,evidence_ref:values.receipt_evidence_ref};if(values.receipt_occurred_on)params.acquisition_receipt.occurred_on=new Date(values.receipt_occurred_on).toISOString();}
  }else{
    if(!selectedUnit)return;params={sku:selectedUnit.sku,expected_version:selectedUnit.version};
    if(action==='update'){
      params={...params,title:values.title,kind:values.kind,state:values.state,reason:values.reason,facts:{}};
      for(const key of ['brand','model','condition','battery_presence','mains_powered','lock_applicable','battery_wh','manufactured_on'])if(values[key]!=='')params.facts[key]=key==='battery_wh'?Number(values[key]):values[key];
    }else if(action==='inspect'){
      params={...params,inspector:values.inspector,competent:form.elements.competent.checked,evidence_ref:values.evidence_ref,checks:{}};
      for(const input of form.querySelectorAll('[data-inspection-outcome]')){const key=input.dataset.inspectionOutcome;params.checks[key]={outcome:input.value,reason:form.querySelector('[data-inspection-reason="'+key+'"]').value};}
    }else{
      params={...params,channel:values.channel,reviews:{}};
      if(values.channel!=='local_draft')for(const input of form.querySelectorAll('[data-channel-outcome]')){const key=input.dataset.channelOutcome;params.reviews[key]={outcome:input.value,reason:form.querySelector('[data-channel-reason="'+key+'"]').value,evidence_ref:form.querySelector('[data-channel-evidence="'+key+'"]').value};}
    }
  }
  if(action!=='update')params.client_token=inventoryToken(action,params);
  const button=form.querySelector('button[type="submit"]'),buttonLabel=button.textContent;unitBusy=true;button.disabled=true;button.textContent='Saving…';
  unitSaving(true);
  form.setAttribute('aria-busy','true');
  try{
    const result=await call('/api/inventory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});
    inventoryRetries.delete(action+':'+(params.sku||'new'));
    if(action==='create'){$('inventory-dialog').close();acquisitionDecision=null;unitStep='facts';}
    message('inventory-message',action==='create'?'Owned item added. Record its physical facts next.':action==='inspect'?'Inspection snapshot saved.':action==='evaluate'?'Eligibility preview saved.':'Product revision saved. Inspect changed facts before listing preparation.','success');
    await openUnit(result.result.sku,true);await loadInventory();
    if(action==='evaluate')drawEligibility(result.result);
    await refresh();
  }catch(error){message(action==='create'?'inventory-create-message':'inventory-message',error.message,'error');}
  finally{unitBusy=false;unitSaving(false);button.disabled=false;button.textContent=buttonLabel;form.setAttribute('aria-busy','false');}
}
const QUEUE_LABELS={blocked:'Blocked',queued:'Queued',dispatching:'Sending',uncertain:'Needs reconciliation',reconciling:'Checking channel',succeeded:'Confirmed',cancelled:'Cancelled',failed:'Rejected'};
async function loadDrafts(){
  const sequence=++draftSequence;$('draft-list').setAttribute('aria-busy','true');
  try{
    const data=await call('/api/drafts?'+new URLSearchParams({page:draftPage}));if(sequence!==draftSequence)return;
    draftData=data;draftPage=data.page;
    setHTML('draft-rows',data.items.length?data.items.map(row=>{
      const approved=row.approvals.some(item=>item.current),label=row.review.stale?'Revision needed':approved?'Reviewed':row.review.ready?'Ready for review':'Needs evidence';
      return '<tr><td><button type="button" class="row-link" data-open-draft="'+esc(row.draft_id)+'">'+esc(row.content.title)+'</button><div class="observation-meta unit-sku">'+esc(row.sku)+' · '+esc(CHANNEL_NAME(row.channel))+'</div></td><td>'+badge(label,approved?'green':'amber')+'</td><td class="num">'+eur(row.content.price_minor/100)+'</td><td>'+date(row.updated_at)+'</td><td><button class="btn quiet small" type="button" data-open-draft="'+esc(row.draft_id)+'">Review draft</button></td></tr>';
    }).join(''):'<tr><td colspan="5">'+empty('Your Tori drafts start with owned stock','Record the real item and its inspection in Owned inventory, then prepare its listing here.','file')+'</td></tr>');
    $('new-draft').disabled=writeDisabled($('new-draft'),draftBusy||!data.units.length);
    $('draft-count').textContent=num(data.total)+' local drafts';$('draft-page').textContent='Page '+num(data.page)+' / '+num(data.pages);
    $('draft-prev').disabled=data.page<=1;$('draft-next').disabled=data.page>=data.pages;
    drawDraftQueue(data.queue);
    if(selectedDraft){
      const current=data.items.find(row=>row.draft_id===selectedDraft.draft_id);
      if(current&&current.version!==selectedDraft.version){draftChanged=true;message('draft-changed','This draft changed in another session. Reload it before saving or reviewing.');drawDraftReview();}
      else if(current){selectedDraft.review=current.review;selectedDraft.approvals=current.approvals;drawDraftReview();}
    }
  }catch(error){if(sequence===draftSequence)message('draft-message',error.message,'error');}
  finally{if(sequence===draftSequence)$('draft-list').setAttribute('aria-busy','false');}
}
function drawDraftQueue(rows){
  setHTML('draft-queue',rows.length?rows.map(row=>'<article class="eligibility-result" style="margin-top:14px"><div class="card-heading"><h3>'+esc(CHANNEL_NAME(row.channel))+' · '+esc(row.sku)+'</h3>'+badge(QUEUE_LABELS[row.state]||row.state,row.state==='succeeded'?'green':row.state==='cancelled'?'':'amber')+'</div><p>'+esc(row.detail)+'</p>'+(row.blockers.length?'<details><summary>Current publication prerequisites</summary><ul>'+row.blockers.map(reason=>'<li>'+esc(reason)+'</li>').join('')+'</ul></details>':'')+'<p class="note">Action '+esc(row.action_id)+' · draft revision '+num(row.draft_version)+' · '+num(row.attempt_count)+' send attempts · '+(row.reserved?'stock reserved':'stock not reserved')+'</p><div class="unit-actions"><button class="btn small" type="button" data-open-draft="'+esc(row.draft_id)+'">Open draft</button><button class="btn quiet small" type="button" data-check-action="'+esc(row.action_id)+'"'+(draftBusy?' disabled':'')+'>Check prerequisites</button>'+(row.cancellable&&row.state!=='cancelled'?'<button class="btn quiet small" type="button" data-cancel-action="'+esc(row.action_id)+'"'+(draftBusy?' disabled':'')+'>Cancel local request</button>':'')+'</div><details><summary>Request history</summary>'+row.history.map(event=>'<p class="note">'+date(event.recorded_at)+' · '+esc(QUEUE_LABELS[event.state]||event.state)+' · '+esc(event.detail)+'</p>').join('')+'</details></article>').join(''):empty('No publishing requests','A reviewed draft can be downloaded for manual Tori entry. Recording a request shows the prerequisites for future automation.','file'));
}
function draftOptions(sku=''){
  const rows=[...(draftData?.units||[])];
  if(sku&&!rows.some(row=>row.sku===sku)&&selectedDraft)rows.unshift({sku,title:selectedDraft.content.title,version:selectedDraft.current_unit_version});
  draftUnitVersions=new Map(rows.map(row=>[row.sku,row.version]));
  setHTML('draft-sku','<option value="">Select an owned item</option>'+rows.map(row=>'<option value="'+esc(row.sku)+'">'+esc(row.title)+' · '+esc(row.sku)+'</option>').join(''));
  $('draft-sku').value=sku;
}
function newDraft(sku=''){
  if(draftBusy)return;
  if(draftDirty){message('draft-message','Save your edits or close the editor before preparing another draft.');return;}
  draftDetailSequence++;selectedDraft=null;draftDirty=false;draftChanged=false;$('draft-form').reset();$('draft-approval-form').reset();
  draftOptions(sku);$('draft-sku').disabled=false;$('draft-channel').disabled=false;$('draft-channel').value='tori';
  $('draft-title').textContent='Prepare Tori draft';$('draft-context').textContent='New listing preparation';
  $('reload-draft').hidden=true;$('draft-review-area').hidden=true;$('draft-detail').hidden=false;message('draft-changed','');
  $('draft-sku').focus();
}
async function openDraft(draftId,afterSave=false){
  if(draftBusy&&!afterSave)return;
  if(draftDirty&&!afterSave&&selectedDraft?.draft_id!==draftId){message('draft-message','Save your edits or close the editor before opening another draft.');return;}
  const sequence=++draftDetailSequence;
  try{
    const row=await call('/api/drafts?'+new URLSearchParams({draft_id:draftId}));if(sequence!==draftDetailSequence)return;
    selectedDraft=row;draftDirty=false;draftChanged=false;draftOptions(row.sku);
    $('draft-sku').disabled=true;$('draft-channel').value=row.channel;$('draft-channel').disabled=true;
    const form=$('draft-form');form.elements.price_eur.value=(row.content.price_minor/100).toFixed(2);
    for(const key of ['included_items','defects','notes'])form.elements[key].value=row.content.disclosures[key];
    $('draft-title').textContent=row.content.title;$('draft-context').textContent=row.sku+' · revision '+row.version+' · '+CHANNEL_NAME(row.channel);
    $('reload-draft').hidden=false;$('draft-review-area').hidden=false;$('draft-detail').hidden=false;message('draft-changed','');drawDraftReview();
    setHTML('draft-history',row.approvals.map(item=>'<article><strong>'+esc(item.current?'Current preparation review':'Historical review')+' · revision '+num(item.draft_version)+'</strong><p>'+esc(item.reviewer)+' · '+date(item.recorded_at)+'</p><p class="note">Evidence: '+esc(item.evidence_ref)+'</p></article>').join('')+row.history.map(item=>'<article><strong>Draft revision '+num(item.version)+'</strong><p>'+date(item.recorded_at)+' · '+eur(item.snapshot.content.price_minor/100)+'</p><details><summary>Saved text</summary><p style="white-space:pre-wrap">'+esc(item.snapshot.content.description)+'</p></details></article>').join(''));
  }catch(error){if(sequence===draftDetailSequence)message('draft-message',error.message,'error');}
}
function drawDraftReview(){
  if(!selectedDraft)return;
  const row=selectedDraft,approval=row.approvals.find(item=>item.current),ready=row.review.ready&&!draftDirty&&!draftBusy&&!draftChanged;
  $('draft-review-status').textContent=draftDirty?'Unsaved changes':draftChanged?'Draft changed':approval&&ready?'Reviewed preparation':row.review.stale?'Revision needed':ready?'Ready for review':'Needs evidence';
  $('draft-preview-title').textContent=row.content.title;$('draft-preview-description').textContent=row.content.description;
  $('draft-preview-binding').textContent=eur(row.content.price_minor/100)+' · one unit · inspected item revision '+row.unit_version+' · saved text only';
  setHTML('draft-review-reasons',row.review.reasons.length?'<ul>'+row.review.reasons.map(reason=>'<li>'+esc(reason)+'</li>').join('')+'</ul>':'<p>The saved facts and disclosures support local preparation. Review every factual statement before use on Tori.</p>');
  $('approve-draft').disabled=!ready;$('export-draft').disabled=!ready||!approval;$('queue-draft').disabled=!ready||!approval;
  $('draft-approval-note').textContent=approval?'Reviewed by '+approval.reviewer+' · '+date(approval.recorded_at):'No current preparation approval';
}
function markDraftDirty(){
  draftDirty=true;message('draft-changed','You have unsaved edits. The preview shows the saved revision; save before review or export.');drawDraftReview();
}
function draftSaving(on){
  if(on){for(const node of [...$('draft-detail').querySelectorAll('input,textarea,select,button'),...$('draft-queue').querySelectorAll('button'),$('new-draft')]){if(!draftDisabled.has(node))draftDisabled.set(node,node.disabled);node.disabled=true;}}
  else{for(const [node,disabled] of draftDisabled)node.disabled=disabled;draftDisabled.clear();}
  $('draft-detail').setAttribute('aria-busy',String(on));
}
function draftToken(action,params){
  const key=action+':'+(params.draft_id||params.sku),fingerprint=JSON.stringify(params),previous=draftRetries.get(key);
  if(previous?.fingerprint===fingerprint)return previous.token;
  const token=crypto.randomUUID();draftRetries.set(key,{fingerprint,token});return token;
}
async function draftAction(action,params){
  if(draftBusy)return;
  if(['save','approve'].includes(action))params={...params,client_token:draftToken(action,params)};
  draftBusy=true;draftSaving(true);
  try{
    const result=await call('/api/drafts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});
    if(['save','approve'].includes(action))draftRetries.delete(action+':'+(params.draft_id||params.sku));
    if(action==='save'||action==='approve'){await loadDrafts();await openDraft(result.result.draft_id,true);}
    else await loadDrafts();
    message('draft-message',action==='save'?'Draft revision saved. Review the saved text and price.':action==='approve'?'Preparation review saved. Reviewed text is ready for manual Tori entry.':action==='enqueue'?'Publishing request recorded locally. Review its blockers below.':action==='cancel'?'Local request cancelled.':'Publication prerequisites checked.','success');
  }catch(error){message('draft-message',error.message,'error');}
  finally{draftBusy=false;draftSaving(false);if(selectedDraft){$('draft-sku').disabled=true;$('draft-channel').disabled=true;}$('new-draft').disabled=!draftData?.units.length;drawDraftReview();if(draftData)drawDraftQueue(draftData.queue);}
}
async function exportDraft(){
  if(draftBusy||draftDirty||draftChanged||!selectedDraft?.review.ready||!selectedDraft.approvals.some(row=>row.current))return;
  const saved=selectedDraft;draftBusy=true;draftSaving(true);
  try{
    const row=await call('/api/drafts?'+new URLSearchParams({draft_id:saved.draft_id}));
    if(row.version!==saved.version||!row.review.ready||!row.approvals.some(item=>item.current)){draftChanged=true;throw new Error('The preparation review changed. Reload and review this draft before export.');}
    const text=row.content.title+'\n\n'+(row.content.price_minor/100).toFixed(2)+' EUR\n\n'+row.content.description+'\n';
    const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})),link=document.createElement('a');
    link.href=url;link.download='tori-'+row.sku+'-revision-'+row.version+'.txt';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    message('draft-message','Reviewed text downloaded. Add your owned photos and review Tori’s category and submission fields manually. No publication has been recorded.','success');
  }catch(error){message('draft-message',error.message,'error');}
  finally{draftBusy=false;draftSaving(false);drawDraftReview();}
}
function bindDrafts(){
  $('new-draft').addEventListener('click',()=>newDraft());
  $('prepare-unit-draft').addEventListener('click',async()=>{if(unitBusy||!selectedUnit)return;const sku=selectedUnit.sku;showView('drafts');await loadDrafts();newDraft(sku);});
  $('draft-form').addEventListener('input',markDraftDirty);
  $('draft-form').addEventListener('submit',event=>{
    event.preventDefault();if(draftBusy||!event.target.reportValidity())return;
    const form=event.target,sku=$('draft-sku').value,channel=$('draft-channel').value;
    const params={sku,expected_unit_version:selectedDraft?.current_unit_version||draftUnitVersions.get(sku),channel,environment:channel==='tori'?'production':'local',price_eur:form.elements.price_eur.value,disclosures:{}};
    for(const key of ['included_items','defects','notes'])params.disclosures[key]=form.elements[key].value;
    if(selectedDraft){params.draft_id=selectedDraft.draft_id;params.expected_version=selectedDraft.version;}
    draftAction('save',params);
  });
  $('draft-approval-form').addEventListener('submit',event=>{event.preventDefault();if(!selectedDraft||draftBusy||draftDirty||!event.target.reportValidity())return;const form=event.target;draftAction('approve',{draft_id:selectedDraft.draft_id,expected_version:selectedDraft.version,reviewer:form.elements.reviewer.value,evidence_ref:form.elements.evidence_ref.value,facts_confirmed:form.elements.facts_confirmed.checked});});
  $('close-draft').addEventListener('click',()=>{if(draftBusy)return;draftDetailSequence++;selectedDraft=null;draftDirty=false;$('draft-detail').hidden=true;});
  $('reload-draft').addEventListener('click',()=>{if(selectedDraft)openDraft(selectedDraft.draft_id);});
  $('draft-open-unit').addEventListener('click',()=>{const sku=$('draft-sku').value;if(sku){showView('inventory');openUnit(sku);}});
  $('export-draft').addEventListener('click',exportDraft);
  $('queue-draft').addEventListener('click',()=>{const approval=selectedDraft?.approvals.find(row=>row.current);if(approval&&!draftDirty&&!draftChanged)draftAction('enqueue',{approval_id:approval.approval_id});});
  $('draft-prev').addEventListener('click',()=>{if(draftPage>1){draftPage--;loadDrafts();}});
  $('draft-next').addEventListener('click',()=>{if(draftData&&draftPage<draftData.pages){draftPage++;loadDrafts();}});
  $('view-drafts').addEventListener('click',event=>{
    const open=event.target.closest('[data-open-draft]');if(open){openDraft(open.dataset.openDraft);return;}
    const check=event.target.closest('[data-check-action]');if(check){draftAction('check',{action_id:check.dataset.checkAction});return;}
    const cancel=event.target.closest('[data-cancel-action]');if(cancel)draftAction('cancel',{action_id:cancel.dataset.cancelAction,reason:'Operator cancelled this local publishing request in the dashboard.'});
  });
}
const STOCK_PHASES={available:'Available for resale',sold:'With buyer',return_pending:'Claim open',returned_review:'Returned · inspection needed',written_off:'Written off'};
const TRADE_KINDS={sale_proceeds:'Gross sale proceeds',refund:'Actual refund',fee_recovery:'Recovered fees',acquisition_cost_eur:'Acquisition',inbound_shipping_cost_eur:'Inbound shipping',buyer_fee_eur:'Buyer fee',platform_fee_eur:'Platform fee',payment_fee_eur:'Payment fee',seller_shipping_cost_eur:'Outbound shipping',packaging_cost_eur:'Packaging',other_variable_cost_eur:'Other cost',tax_cost_eur:'Tax',reverse_shipping:'Return shipping',repair:'Repair',disposal:'Disposal',write_off:'Write-off marker'};
const RETURN_TYPES={withdrawal:'Withdrawal',defect:'Defect claim',platform_protection:'Platform protection',voluntary:'Voluntary return'};
function tradeField(label,name,type='text',attrs=''){
  return '<label class="field">'+esc(label)+'<input name="'+name+'" type="'+type+'" '+attrs+'></label>';
}
function tradeSelect(label,name,options,selected='',attrs=''){
  return '<label class="field">'+esc(label)+'<select name="'+name+'" '+attrs+'>'+unitOptions(options,selected)+'</select></label>';
}
function tradeCaseOptions(){return Object.fromEntries((selectedOrder?.cases||[]).map(row=>[row.case_id,RETURN_TYPES[row.case_type]+' · '+row.state+' · '+row.case_id.slice(0,8)]));}
async function loadOrders(){
  const sequence=++ordersSequence;
  try{
    const data=await call('/api/trades?'+new URLSearchParams({page:ordersPage}));
    if(sequence!==ordersSequence)return;ordersData=data;ordersPage=data.page;
    setHTML('orders-rows',data.items.length?data.items.map(row=>'<tr><td><strong>'+esc(row.title)+'</strong><div class="observation-meta unit-sku">'+esc(row.sku)+' · cycle '+num(row.cycle)+'</div></td><td>'+esc(CHANNEL_NAME(row.channel))+'</td><td>'+esc(row.fulfilment)+'</td><td>'+badge(STOCK_PHASES[row.economics.stock_phase]||row.economics.stock_phase,row.economics.stock_phase==='sold'?'green':'amber')+'</td><td class="num">'+(row.economics.complete?eur(row.economics.net_profit_eur):'<span class="muted">Pending</span>')+'</td><td><button class="btn quiet small" type="button" data-order-id="'+esc(row.order_id)+'">Review</button></td></tr>').join(''):'<tr><td colspan="6">'+empty('No recorded SKU sales yet','Add your acquired item in Owned inventory, then record its actual Tori sale.','wallet')+'</td></tr>');
    $('orders-page').textContent=num(data.total)+' sale cycles · Page '+num(data.page)+' / '+num(data.pages);
    $('orders-prev').disabled=data.page<=1;$('orders-next').disabled=data.page>=data.pages;$('new-order').disabled=writeDisabled($('new-order'),orderBusy||!data.units.length);
    if(!$('order-dialog').open){const selected=$('order-unit-options').value;setHTML('order-unit-options','<option value="">Choose available stock</option>'+data.units.map(row=>'<option value="'+esc(row.sku)+'">'+esc(row.title)+' · '+esc(row.sku)+'</option>').join(''));$('order-unit-options').value=selected;}
    // Polling updates the list, never replaces entered costs, claim notes or dates.
    if(!document.activeElement?.closest?.('#legacy-order-form')){
      const previous=$('legacy-order-options').value;
      setHTML('legacy-order-options',data.legacy_candidates.map(row=>'<option value="'+row.transaction_pk+'">'+esc(row.title_raw)+' · '+esc(row.platform)+'</option>').join(''));
      if(previous)$('legacy-order-options').value=previous;
    }
    $('legacy-order-panel').hidden=!data.legacy_candidates.length;
    if(selectedOrder&&!orderBusy){
      const id=selectedOrder.order_id,version=selectedOrder.version,unitVersion=selectedOrder.unit.version;
      const saved=await call('/api/trades?'+new URLSearchParams({order_id:id}));
      if(sequence===ordersSequence&&selectedOrder?.order_id===id&&selectedOrder.version===version&&selectedOrder.unit.version===unitVersion&&(saved.version!==version||saved.unit.version!==unitVersion)){
        orderChanged=true;message('order-changed','This order or its physical item changed. Your entered fields are preserved; reload saved data before submitting.','info');$('order-action-fields').disabled=true;
      }
    }
  }catch(error){if(sequence===ordersSequence)message('orders-message',error.message,'error');}
}
function drawOrderForm(){
  if(!selectedOrder)return;
  const action=$('order-action-type').value,cases=tradeCaseOptions();let fields='',help='Records a completed action locally.';
  const caseField=()=>tradeSelect('CASE','case_id',cases,'','required');
  if(action==='fulfil'){fields=tradeSelect('FULFILMENT','stage',{shipped:'Shipped',delivered:'Delivered / handed over'});help='Record delivery only with evidence the buyer received the unit.';}
  if(action==='open_case'){fields=tradeSelect('CASE TYPE','case_type',RETURN_TYPES)+tradeField('OBSERVED REASON','reason','text','required maxlength="1000"');help='Withdrawal, defects, platform protection and voluntary returns have separate evidence. Deadline applicability starts as needs review.';}
  if(action==='finance'){
    const choices=Object.fromEntries(selectedOrder.economics.active_entries.filter(row=>(!row.order_id||row.order_id===selectedOrder.order_id)&&row.kind!=='write_off').map(row=>[row.entry_id,(TRADE_KINDS[row.kind]||row.kind)+' · '+(row.amount_minor==null?'unknown':eur(row.amount_minor/100))]));
    fields=tradeSelect('ENTRY TO CORRECT','supersedes_id',{'':'New financial entry',...choices})+tradeSelect('ENTRY TYPE','kind',Object.fromEntries(Object.entries(TRADE_KINDS).filter(([key])=>key!=='write_off')),'refund')+tradeField('AMOUNT (€)','amount_eur','number','min="0" step="0.01" placeholder="Unknown cost"')+tradeSelect('LINKED CASE','case_id',{'':'No case',...cases})+tradeField('RECEIPT / REFUND REFERENCE','external_event_ref','text','maxlength="100"')+tradeField('REASON','reason','text','required maxlength="1000"');
    help='An actual refund needs its case and receipt reference. Correct an existing cost to replace its unknown value; adding an extra cost keeps the unknown entry. Acquisition is counted once across resales.';
  }
  if(action==='assess_deadline'){
    fields=caseField()+tradeSelect('APPLICABILITY','deadline_status',{requires_review:'Needs review',applicable:'Applicable',not_applicable:'Not applicable'})+tradeField('DEADLINE PURPOSE','deadline_kind','text','maxlength="100"')+tradeField('DUE DATE','deadline_on','date')+tradeField('APPLICABILITY REASON','deadline_reason','text','maxlength="1000"')+tradeField('RIGHTS / TERMS EVIDENCE','deadline_evidence','text','maxlength="500"');
    help='Review the seller, buyer, sale method, delivery and applicable rights or platform terms. The workspace does not calculate a universal return deadline.';
  }
  if(action==='review_context'){
    fields=tradeSelect('SELLER','seller_context',{unknown:'Needs review',private:'Private person',business:'Business'},selectedOrder.seller_context)+tradeSelect('BUYER','buyer_context',{unknown:'Needs review',consumer:'Consumer',business:'Business'},selectedOrder.buyer_context)+tradeSelect('SALE METHOD','sale_method',{unknown:'Needs review',distance:'Distance sale',in_person:'In person'},selectedOrder.sale_method);
    help='Changed participant facts invalidate existing case deadline assessments. Review them again using evidence.';
  }
  if(action==='receive_return'){fields=caseField();help='Confirm physical receipt. The unit is quarantined and its earlier inspection becomes stale.';}
  if(action==='close_case'){fields=caseField()+tradeField('RESOLUTION','resolution','text','required maxlength="1000"')+'<label class="unit-attestation"><input type="checkbox" name="settlement_confirmed" required>Refunds, retained or recovered fees, and return costs are reconciled.</label>';help='Unknown return costs block closure. Confirm €0 explicitly when no cost occurred.';}
  if(action==='release_stock')help='Resolve claims and complete a fresh passed inspection in Owned inventory. Release makes this same SKU available for another sale cycle.';
  if(action==='write_off'){fields=tradeField('ACTUAL DISPOSAL COST (€)','disposal_cost_eur','number','min="0" step="0.01" placeholder="Unknown"')+tradeField('WRITE-OFF REASON','reason','text','required maxlength="1000"');help='Records disposal of returned owned stock. Its original acquisition remains counted once.';}
  setHTML('order-action-inputs',fields);$('order-action-help').textContent=help;
  if(action==='finance')syncFinanceForm();
}
function syncFinanceForm(){
  const form=$('order-action-form'),previous=selectedOrder.economics.active_entries.find(row=>row.entry_id===form.elements.supersedes_id.value);
  if(previous){form.elements.kind.value=previous.kind;form.elements.case_id.value=previous.case_id||'';form.elements.amount_eur.value=previous.amount_minor==null?'':(previous.amount_minor/100).toFixed(2);}
  form.elements.kind.disabled=!!previous;
  form.elements.external_event_ref.required=form.elements.kind.value==='refund'&&!previous;
  form.elements.amount_eur.required=['refund','fee_recovery','sale_proceeds'].includes(form.elements.kind.value);
}
async function openOrder(id,allowWhileBusy=false){
  if(!allowWhileBusy&&selectedOrder&&id!==selectedOrder.order_id&&orderDirty){message('orders-message','Save the current order action, or close the order and discard it, before opening another order.','error');return;}
  if(orderBusy&&!allowWhileBusy)return;const sequence=++orderDetailSequence;
  try{
    const row=await call('/api/trades?'+new URLSearchParams({order_id:id}));if(sequence!==orderDetailSequence)return;
    selectedOrder=row;orderDirty=false;orderChanged=false;$('order-detail').hidden=false;$('order-changed').hidden=true;$('order-action-fields').disabled=orderBusy;
    $('order-title').textContent=row.unit.title;$('order-sku').textContent=row.sku+' · order '+row.order_id;
    $('order-context').textContent=CHANNEL_NAME(row.channel)+' · cycle '+row.cycle+' · '+row.fulfilment+' · revision '+row.version;
    const e=row.economics;
    setHTML('order-economics',[['Gross receipts',e.gross_proceeds_eur],['Actual refunds',e.refunds_eur],['Net SKU profit',e.net_profit_eur]].map(([label,value])=>'<section class="card stat"><div class="stat-top">'+esc(label)+'</div><div class="stat-value">'+(value==null?'Pending':eur(value))+'</div></section>').join(''));
    $('order-economics-note').textContent=(STOCK_PHASES[e.stock_phase]||e.stock_phase)+' · '+e.n_sale_cycles+' sale cycles. '+(e.reasons.length?e.reasons.join(' '):'All recorded costs are known and the lifecycle is settled.')+' Gross receipts and costs across this SKU; bank payouts require separate reconciliation.';
    setHTML('order-cases',row.cases.length?row.cases.map(c=>'<article><div class="card-heading"><h3>'+esc(RETURN_TYPES[c.case_type])+'</h3>'+badge(c.state,c.state==='closed'?'green':'amber')+'</div><p>'+esc(c.reason)+'</p><p class="note">'+esc(c.evidence_ref)+' · '+date(c.notified_at)+'</p><p>Deadline: '+esc(c.deadline.status)+(c.deadline.on?' · '+esc(c.deadline.on):'')+(c.deadline.kind?' · '+esc(c.deadline.kind):'')+'</p>'+(c.deadline.reason?'<p class="note">'+esc(c.deadline.reason)+' · '+esc(c.deadline.evidence_ref)+'</p>':'')+(c.resolution?'<p>'+esc(c.resolution)+'</p>':'')+'</article>').join(''):empty('No return cases','Record a claim when you have actual evidence.','file'));
    const active=new Set(e.active_entries.map(entry=>entry.entry_id));
    setHTML('order-finances',row.entries.map(entry=>'<tr><td>'+esc(TRADE_KINDS[entry.kind]||entry.kind)+'<div class="observation-meta">'+(!entry.order_id?'Acquisition · once per SKU':entry.order_id===row.order_id?'This sale cycle':'Earlier sale cycle')+' · '+date(entry.effective_at)+(active.has(entry.entry_id)?'':' · superseded')+'</div></td><td class="num">'+(entry.amount_minor==null?'Unknown':eur(entry.amount_minor/100))+'</td><td>'+esc(entry.evidence_ref)+'<div class="observation-meta">'+esc(entry.reason)+'</div></td><td>'+(active.has(entry.entry_id)&&entry.kind!=='write_off'&&(!entry.order_id||entry.order_id===row.order_id)?'<button class="btn quiet small" type="button" data-correct-entry="'+esc(entry.entry_id)+'">Correct</button>':'')+'</td></tr>').join(''));
    setHTML('order-history',row.history.map(event=>'<article><strong>'+esc(event.event_type.replaceAll('_',' '))+'</strong><p>'+date(event.effective_at)+' · recorded '+date(event.recorded_at)+'</p><p class="note">'+esc(event.evidence_ref)+'</p></article>').join(''));
    $('order-action-form').reset();drawOrderForm();$('order-asof-result').textContent='';
  }catch(error){if(sequence===orderDetailSequence)message('orders-message',error.message,'error');}
}
async function tradeAction(action,params){
  if(orderBusy||orderChanged&&params.order_id)return;
  const key=action+':'+(params.order_id||params.sku||params.transaction_pk),fingerprint=JSON.stringify(params),old=orderRetries.get(key);
  const token=old?.fingerprint===fingerprint?old.token:crypto.randomUUID();orderRetries.set(key,{fingerprint,token});
  orderBusy=true;$('order-action-fields').disabled=true;$('new-order-fields').disabled=true;$('new-order').disabled=true;
  try{
    const result=await call('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params:{...params,client_token:token}})});
    orderRetries.delete(key);if(action==='record_sale'){$('order-dialog').close();$('new-order-form').reset();}
    await openOrder(result.result.order_id,true);await loadOrders();
    message('orders-message','Actual trade action recorded. Review the SKU totals and remaining lifecycle steps.','success');await refresh();
  }catch(error){message('orders-message',error.message,'error');if($('order-dialog').open)message('order-dialog-message',error.message,'error');}
  finally{orderBusy=false;$('order-action-fields').disabled=orderChanged;$('new-order-fields').disabled=false;$('new-order').disabled=writeDisabled($('new-order'),!ordersData?.units.length);}
}
function bindOrders(){
  $('new-order').addEventListener('click',()=>{if(orderBusy)return;saleUnits=(ordersData?.units||[]).map(row=>({...row}));message('order-dialog-message','');resalePreviewSequence++;setHTML('resale-preview','<p class="inventory-help">Preview your selected resale listing before saving. A sale without a reference will be explicitly unpaired.</p>');$('order-dialog').showModal();});
  $('cancel-order').addEventListener('click',()=>{if(!orderBusy)$('order-dialog').close();});
  $('order-dialog').addEventListener('cancel',event=>{if(orderBusy)event.preventDefault();});
  $('new-order-form').addEventListener('submit',event=>{
    event.preventDefault();if(orderBusy||!event.target.reportValidity())return;const form=event.target,unit=saleUnits.find(row=>row.sku===form.elements.sku.value);if(!unit)return;
    const params={sku:unit.sku,expected_unit_version:unit.version,channel:'tori',proceeds_eur:form.elements.proceeds_eur.value,evidence_ref:form.elements.evidence_ref.value,handed_over:form.elements.handed_over.checked,costs:{}};
    for(const key of ['seller_context','buyer_context','sale_method'])params[key]=form.elements[key].value;
    for(const key of ['sold_on','external_order_ref','resale_listing_id'])if(form.elements[key]?.value)params[key]=key==='sold_on'?new Date(form.elements[key].value).toISOString():form.elements[key].value;
    for(const input of form.querySelectorAll('.optional-cost'))params.costs[input.name]=input.value;
    tradeAction('record_sale',params);
  });
  $('legacy-order-form').addEventListener('submit',event=>{event.preventDefault();if(orderBusy||!event.target.reportValidity())return;const form=event.target;tradeAction('import_legacy',{transaction_pk:Number(form.elements.transaction_pk.value),kind:form.elements.kind.value,evidence_ref:form.elements.evidence_ref.value});});
  $('orders-prev').addEventListener('click',()=>{if(ordersPage>1){ordersPage--;loadOrders();}});$('orders-next').addEventListener('click',()=>{if(ordersPage<ordersData?.pages){ordersPage++;loadOrders();}});
  $('reload-order').addEventListener('click',()=>{if(selectedOrder)openOrder(selectedOrder.order_id);});
  $('close-order').addEventListener('click',()=>{if(orderBusy)return;orderDetailSequence++;selectedOrder=null;$('order-detail').hidden=true;});
  $('order-open-unit').addEventListener('click',()=>{if(!selectedOrder||orderBusy)return;const sku=selectedOrder.sku;showView('inventory');openUnit(sku);});
  $('order-action-type').addEventListener('change',drawOrderForm);
  $('order-action-inputs').addEventListener('change',event=>{if(['supersedes_id','kind'].includes(event.target.name))syncFinanceForm();});
  $('order-action-form').addEventListener('submit',event=>{
    event.preventDefault();if(!selectedOrder||orderBusy||orderChanged||!event.target.reportValidity())return;
    const form=event.target,action=form.elements.action.value,params={order_id:selectedOrder.order_id,expected_version:selectedOrder.version,evidence_ref:form.elements.evidence_ref.value};
    if(form.elements.occurred_on.value)params.occurred_on=form.elements.occurred_on.value;
    for(const input of $('order-action-inputs').querySelectorAll('input,select')){
      if(input.name.startsWith('deadline_'))continue;
      if(input.type==='checkbox')params[input.name]=input.checked;else if(input.value||['amount_eur','disposal_cost_eur'].includes(input.name))params[input.name]=input.value;
    }
    if(action==='assess_deadline')params.deadline={status:form.elements.deadline_status.value,kind:form.elements.deadline_kind.value,on:form.elements.deadline_on.value,reason:form.elements.deadline_reason.value,evidence_ref:form.elements.deadline_evidence.value};
    tradeAction(action,params);
  });
  $('view-orders').addEventListener('click',event=>{
    const open=event.target.closest('[data-order-id]');if(open){openOrder(open.dataset.orderId);return;}
    const correct=event.target.closest('[data-correct-entry]');if(correct&&!orderBusy&&!orderChanged){$('order-action-type').value='finance';drawOrderForm();$('order-action-form').elements.supersedes_id.value=correct.dataset.correctEntry;syncFinanceForm();$('order-action-form').scrollIntoView({block:'center'});}
  });
  $('order-asof-form').addEventListener('submit',async event=>{event.preventDefault();if(!selectedOrder||!event.target.reportValidity())return;const sku=selectedOrder.sku;try{const e=await call('/api/trades?'+new URLSearchParams({sku,as_of:event.target.elements.as_of.value}));if(selectedOrder?.sku===sku)$('order-asof-result').textContent=e.as_of+' · gross '+eur(e.gross_proceeds_eur)+' · refunds '+eur(e.refunds_eur)+' · net profit '+(e.complete?eur(e.net_profit_eur):'pending')+'. '+e.reasons.join(' ');}catch(error){$('order-asof-result').textContent=error.message;}});
}
const RESEARCH_LINK_LABELS={pre_acquisition:'Receipt-linked acquisition decision',pre_acquisition_context:'Prior decision · acquisition unlinked',invalid_acquisition_link:'Acquisition link needs review',retrospective_only:'Retrospective decisions only',timing_unknown:'Timing needs review',not_recorded:'No prior decision recorded'};
function workflowResearch(research){
  const decision=research.anchor||research.context,explicit=research.explicit_acquisition_link===true&&!!research.anchor;
  const status=research.status==='pre_acquisition'&&!explicit?'pre_acquisition_context':research.status;
  return badge(RESEARCH_LINK_LABELS[status]||'Acquisition link needs review',explicit?'green':'amber')+(decision?'<div><button class="row-link" type="button" data-workflow-decision="'+esc(decision.decision_pk)+'">Decision #'+esc(decision.decision_pk)+' · '+esc(DECISION_LABELS[decision.decision])+'</button></div>':'')+(research.reason?'<div class="observation-meta">'+esc(research.reason)+'</div>':'');
}
async function loadWorkflow(){
  const sequence=++workflowSequence;
  try{
    const data=await call('/api/workflow?'+new URLSearchParams({page:workflowPage}));if(sequence!==workflowSequence)return;
    message('workflow-message','');workflowData=data;workflowPage=data.page;const s=data.summary;
    setHTML('workflow-summary',[[s.owned_units,'Owned physical units','One SKU per acquisition'],[s.settled_units,'Settled SKU outcomes',s.sale_cycles+' sale cycles across '+s.units_with_sku_sales+' SKUs'],[s.net_profit_eur==null?'Pending':eur(s.net_profit_eur),'Recorded net result',s.profit_is_partial?'Partial: unsettled or unknown-cost units excluded':'Managed SKU journal; known costs only']].map(([value,label,note])=>'<section class="card stat"><div class="stat-top">'+esc(label)+'</div><div class="stat-value">'+(typeof value==='number'?num(value):value)+'</div><div class="stat-note">'+esc(note)+'</div></section>').join(''));
    setHTML('workflow-measures',metric('SKUs with receipt-linked decisions',num(s.research_links.pre_acquisition||0))+metric('Prior context / invalid acquisition links',num(s.research_links.pre_acquisition_context||0)+' / '+num(s.research_links.invalid_acquisition_link||0))+metric('Retrospective / timing unknown / no decision',num(s.research_links.retrospective_only||0)+' / '+num(s.research_links.timing_unknown||0)+' / '+num(s.research_links.not_recorded||0))+metric('Unsettled traded SKUs / unknown-cost SKUs',num(s.unsettled_traded_units)+' / '+num(s.unknown_cost_units))+metric('Loss-making settled SKUs',num(s.loss_units))+metric('Observed loss rate',s.loss_rate==null?'Needs '+num(s.min_n)+' settled SKUs':percent(s.loss_rate*100))+metric('Claim cases / distinct SKUs with claims',num(Object.values(s.claim_cases).reduce((total,value)=>total+value,0))+' / '+num(s.units_with_claims))+metric('Open cases',num(s.open_cases))+Object.entries(RETURN_TYPES).map(([key,label])=>metric(label+' cases',num(s.claim_cases[key]||0))).join('')+metric('Median acquisition-to-final-outcome duration',s.median_duration_days==null?'Needs '+num(s.min_n)+' valid durations':num(s.median_duration_days,2)+' days')+metric('Valid completed-outcome durations',num(s.duration_n))+metric('Legacy outcomes awaiting SKU adoption',num(s.legacy_outcome_units))+metric('Tori draft SKUs / reserved SKUs',num(s.tori_draft_units)+' / '+num(s.reserved_units))+metric('Unattended trading evaluation',esc(data.automation.status.replaceAll('_',' '))));
    $('workflow-scope').textContent=data.scope;setHTML('workflow-limits',data.limits.map(note=>'<li>'+esc(note)+'</li>').join(''));
    setHTML('workflow-rows',data.items.length?data.items.map(row=>{const step=row.next_step;return '<tr><td><button class="row-link" type="button" data-workflow-unit="'+esc(row.sku)+'">'+esc(row.title)+'</button><div class="observation-meta unit-sku">'+esc(row.sku)+'</div></td><td>'+workflowResearch(row.research)+'</td><td>'+esc(STOCK_PHASES[row.stock_phase]||row.stock_phase)+'<div class="observation-meta">'+num(row.n_sale_cycles)+' sale cycles · '+num(row.n_open_cases)+' open cases</div></td><td class="num">'+(row.owned_cash_contribution_eur==null?'Pending':eur(row.owned_cash_contribution_eur))+'<div class="observation-meta">'+(row.outcome_scope_reviewed?'Current scope reviewed':'Scope review required')+'</div>'+'</td><td><button class="btn quiet small" type="button" data-workflow-step="'+esc(row.sku)+'">'+esc(step.label)+'</button>'+(step.reasons.length?'<details><summary>Evidence needed</summary><ul class="inventory-help" style="padding-left:16px">'+step.reasons.map(reason=>'<li>'+esc(reason)+'</li>').join('')+'</ul></details>':'')+'</td></tr>';}).join(''):'<tr><td colspan="5">'+empty('The pilot needs actual owned evidence','Review a Tori observation, save your decision, then record an item after you acquire it.','wallet')+'</td></tr>');
    $('workflow-page').textContent=num(data.total)+' physical SKUs · Page '+num(data.page)+' / '+num(data.pages);$('workflow-prev').disabled=data.page<=1;$('workflow-next').disabled=data.page>=data.pages;
  }catch(error){if(sequence===workflowSequence)message('workflow-message',error.message,'error');}
}
async function openWorkflowDecision(pk){
  try{const data=await call('/api/research?'+new URLSearchParams({decision_pk:pk}));const row=data.journal.items[0];if(row)showDecisionDetail(row);else toast('This decision is unavailable.',true);}catch(error){toast(error.message,true);}
}
async function continueWorkflow(sku){
  const row=workflowData?.items.find(item=>item.sku===sku);if(!row)return;const step=row.next_step;
  try{
    if(!await openItemWorkspace({sku}))return;
    const data=itemWorkspace.data();
    if(step.view==='inventory')await itemAction({type:step.panel?'outcome':'owned',panel:step.panel},data);
    else if(step.view==='orders')await itemAction({type:step.order_id?'order':'sale',id:step.order_id},data);
    else if(step.view==='drafts')await itemAction({type:step.draft_id?'draft':'new_draft',id:step.draft_id},data);
  }catch(error){toast(error.message,true);}
}
function decisionOwnedButton(row){return '<button class="btn small" type="button" data-decision-owned="'+row.decision_pk+'">'+(row.owned_sku?'Open owned item':row.purchase_pk?'Link acquired item':'Record acquired item')+'</button>';}
async function beginOwnedAcquisition(row){
  if(!row||acquisitionBusy||unitBusy)return;
  if($('inventory-dialog').open){toast('Finish the open acquisition form first.',true);return;}
  acquisitionBusy=true;
  try{
    const source=row.source_listing_id||row.listing_id;
    // `prefill=1` asks for the ready-to-submit record: observed facts, the
    // decision that justified the buy, and cost completeness. The operator's
    // three cost fields stay empty by construction -- see acquisitionFormPlan.
    const context=await call('/api/research/acquisition?'+new URLSearchParams({platform:row.platform,listing_id:source,prefill:'1'})),purchase=context.purchase;
    for(const id of ['listing-dialog','journal-detail-dialog'])if($(id).open)$(id).close();
    if(purchase?.owned_sku){await openOwnedOutcome(purchase.owned_sku,'receipt',row.decision_pk||null);return;}
    if(purchase?.recorded_outcome){showView('orders');toast('This purchase has a ledger outcome. Adopt the existing sale in Orders & returns.');return;}
    showView('inventory');await loadInventory();
    if($('inventory-dialog').open)return;
    newOwnedUnit();const form=$('unit-create-form');
    if(row.decision_pk){acquisitionDecision=row;$('unit-receipt-fields').hidden=false;$('unit-receipt-fields').disabled=false;$('unit-receipt-decision').textContent='Selected decision #'+row.decision_pk+' · '+date(row.recorded_at)+' · '+DECISION_LABELS[row.decision]+'. This exact decision will be linked by your receipt.';}
    if(purchase){
      form.elements.title.value=purchase.title_raw;
      if(![...$('unit-purchase').options].some(option=>option.value===String(purchase.purchase_pk))){const option=document.createElement('option');option.value=String(purchase.purchase_pk);option.textContent=purchase.title_raw+' · '+eur(purchase.acquisition_cost_eur);$('unit-purchase').appendChild(option);}
      $('unit-purchase').value=String(purchase.purchase_pk);acquisitionMode();
    }else{
      const plan=acquisitionFormPlan(context);
      for(const [name,value] of Object.entries(plan.fields)){
        const field=form.elements[name];
        if(field&&field.value!==undefined)field.value=value;
      }
      acquisitionMode();
      setHTML('unit-acquisition-context',acquisitionContextRows({...context,decision:acquisitionDecision||context.decision}));
      message('inventory-create-message',purchase?'Linking your existing actual purchase. Physical facts still need inspection.':'Source selected. Record this only after acquisition: enter the actual amount paid. Asking prices and calculator assumptions have not been copied.','info');
      form.elements[plan.focus]?.focus();
      return;
    }
    setHTML('unit-acquisition-context',purchase?acquisitionContextRows({...context,decision:acquisitionDecision||context.decision}):'');
    message('inventory-create-message',purchase?'Linking your existing actual purchase. Physical facts still need inspection.':'Source selected. Record this only after acquisition: enter the actual amount paid. Asking prices and calculator assumptions have not been copied.','info');
    (purchase?form.elements.kind:form.elements.acquisition_cost_eur).focus();
  }catch(error){toast(error.message,true);}
  finally{acquisitionBusy=false;}
}
function bindWorkflow(){
  $('workflow-prev').addEventListener('click',()=>{if(workflowPage>1){workflowPage--;loadWorkflow();}});$('workflow-next').addEventListener('click',()=>{if(workflowPage<workflowData?.pages){workflowPage++;loadWorkflow();}});
  document.addEventListener('click',async event=>{const button=event.target.closest('[data-decision-owned]');if(!button)return;button.disabled=true;try{const data=await call('/api/research?'+new URLSearchParams({decision_pk:button.dataset.decisionOwned}));await beginOwnedAcquisition(data.journal.items[0]);}catch(error){toast(error.message,true);}finally{button.disabled=false;}});
  document.addEventListener('click',event=>{const button=event.target.closest('[data-owned-outcomes]');if(button)openOwnedOutcome(button.dataset.ownedOutcomes,button.dataset.outcomePanel,Number(button.dataset.outcomeDecision)||null);});
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-decision-attempt]');if(!button||attemptCapture?.isBusy())return;button.disabled=true;
    try{const row=await itemDecision(button.dataset.decisionAttempt);if(await openItemWorkspace({decision_pk:row.decision_pk}))attemptCapture.open(row);}
    catch(error){toast(error.message,true);}finally{button.disabled=false;}
  });
  // The workflow table renders three buttons per row. Delegated on document
  // rather than bound per row because `setHTML` replaces the tbody on every
  // load, so a listener attached to a row would not survive the next refresh --
  // which is why these were inert rather than occasionally working.
  document.addEventListener('click',event=>{const unit=event.target.closest('[data-workflow-unit]');if(unit){openItemWorkspace({sku:unit.dataset.workflowUnit});return;}
    const decision=event.target.closest('[data-workflow-decision]');if(decision){openWorkflowDecision(decision.dataset.workflowDecision);return;}
    const step=event.target.closest('[data-workflow-step]');if(step){continueWorkflow(step.dataset.workflowStep);}});
}
async function previewResaleEvidence(){
  const form=$('new-order-form'),sequence=++resalePreviewSequence,params=new URLSearchParams({listing_id:form.elements.resale_listing_id.value});
  if(form.elements.sold_on.value)params.set('sold_on',new Date(form.elements.sold_on.value).toISOString());
  setHTML('resale-preview','<p class="note">Reading retained pre-sale observations…</p>');
  try{const result=await call('/api/trades/resale-preview?'+params);if(sequence===resalePreviewSequence)setHTML('resale-preview',resalePreview(result));}
  catch(error){if(sequence===resalePreviewSequence){setHTML('resale-preview','<p class="message error" role="alert"></p>');$('resale-preview').firstElementChild.textContent=error.message;}}
}
const sizeLabel=value=>value==null?'—':value<1e3?num(value)+' B':value<1e6?num(value/1e3,1)+' kB':value<1e9?num(value/1e6,1)+' MB':num(value/1e9,2)+' GB';
function metricValue(report,name){
  const series=(report.gauges&&report.gauges[name])||(report.counters&&report.counters[name]);
  if(!series)return null;
  const values=Object.values(series).filter(value=>typeof value==='number'&&Number.isFinite(value));
  return values.length?values.reduce((total,value)=>total+value,0):null;
}
function metricDisplay(name,value){
  return value==null?'—':name.endsWith('_ratio')?percent(value*100):num(value,Number.isInteger(value)?0:3);
}
const MODEL_INFO = {
  sale_probability:{title:'Sale probability',method:'Cox proportional hazards',description:'Probability of an observed sale label within the next horizon, conditional on current age and competing exits.',note:'A sale label is observed evidence. Forecast probabilities do not establish a selling price or realised profit.',requirements:[['Training listings','At least 200 usable entry cohorts'],['Observed sale labels','At least 20 usable sale events'],['Validation','Later cohorts and enough follow-up']],actions:[['predict','Generate forecasts'],['train','Train sale model'],['evaluate','Evaluate chronological holdout']]},
  price_trajectory:{title:'Asking-price paths',method:'Hierarchical Bayesian model',description:'Future asking prices conditional on the listing staying available, with 90% posterior predictive intervals.',note:'Forecasts start from the ask at computation time. Intervals describe future asking-price uncertainty; sale proceeds require separate evidence.',requirements:[['Price paths','At least 10 listings / 30 intervals'],['Measured endpoint spacing','At least 1 day'],['Sampling','Split R-hat < 1.1 / ESS ≥ 100'],['Validation','Disjoint later listing cohorts']],actions:[['predict','Generate price paths'],['train','Train price model'],['evaluate','Review holdout evaluation']]},
  category_trends:{title:'Category trends',method:'STL seasonality + PELT changepoints + negative-binomial projection',description:'Completed Helsinki weeks of collected arrivals, with prefix estimates that use only evidence available that week, and a projected arrival count for the weeks ahead.',note:'Arrivals reflect the collected population and collection effort. The break score describes strength and recency, not a calibrated probability. Arrival guidance requires its own chronological baseline and interval checks at each horizon.',requirements:[['Early trend','At least one completed week'],['Changepoints','At least 8 completed weeks'],['Annual seasonality','At least 104 completed weeks'],['Arrival candidates','At least 8 completed weeks and 30 arrivals'],['Arrival guidance','10 held-out weeks / beats baselines / interval coverage']],actions:[['compute','Compute weekly trends and projections']]},
  seller_archetypes:{title:'Seller behaviour groups',method:'HDBSCAN clustering',description:'Groups of similar observed listing behaviour under stable pseudonymous seller linkage.',note:'Membership is clustering strength, not confidence about a seller’s intent. Noise remains unclassified; seller identities are omitted.',requirements:[['Seller sample','At least 10 linked sellers'],['Per seller','At least 3 observed listings'],['Features','Listing, price and repricing history']],actions:[['compute','Compute seller groups']]},
  market_concentration:{title:'Market concentration',method:'Monthly count & asking-value shares',description:'Concentration among linked sellers in observed calendar-month listing-arrival cohorts.',note:'HHI is on a 0–1 scale. Count shares and initial asking-value shares describe observed supply. Partial linkage and current months limit comparisons.',requirements:[['Seller linkage','Resolved seller versions'],['Count shares','Linked arrivals in each cohort'],['Asking-value shares','Known positive entry prices']],actions:[['compute','Compute concentration']]}
};
const PREDICT_STATES={emerging:'Emerging',growing:'Growing',mature:'Mature',declining:'Declining',volatile:'Volatile'};
let predictSequence=0,predictData=null,predictController=null,predictPoll=null,predictStarting=false,predictDetailSequence=0,predictSearchTimer=null;
let pendingModelListing=/^[1-9]\d{0,15}$/.test(query.get('model_listing')||'')?Number(query.get('model_listing')):null;
const MODEL_HORIZONS=[1,2,3,7,14,30,60,90];
const predictScope={family:MODEL_INFO[query.get('model')]?query.get('model'):'sale_probability',horizon:MODEL_HORIZONS.includes(Number(query.get('forecast')))?Number(query.get('forecast')):1,category:query.get('model_category')||'',region:query.get('model_region')||'',version:query.get('model_version')||'',search:(query.get('model_search')||'').slice(0,100),page:1};
const modelPercent=value=>percent(value==null?null:value*100);
const modelDate=value=>value?String(value).slice(0,10):'—';
function predictURL(){
  const params=new URLSearchParams(location.search);
  for(const [key,value] of Object.entries({model:predictScope.family,forecast:predictScope.horizon,model_category:predictScope.category,model_region:predictScope.region,model_version:predictScope.version,model_search:predictScope.search}))value?params.set(key,value):params.delete(key);
  if(pendingModelListing)params.set('model_listing',pendingModelListing);
  history.replaceState(null,'',location.pathname+'?'+params+'#predict');
}
function selectModel(family){
  if(!MODEL_INFO[family])return;
  pendingModelListing=null;
  closeModelDetail();clearTimeout(predictSearchTimer);
  predictScope.family=family;predictScope.version='';predictScope.page=1;
  predictURL();drawModelTabs();loadPredict();
}
function drawModelTabs(){
  document.querySelectorAll('[data-model-family]').forEach(button=>{const selected=button.dataset.modelFamily===predictScope.family;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});
  $('model-panel').setAttribute('aria-labelledby','model-tab-'+predictScope.family);
}
async function loadPredict(){
  const sequence=++predictSequence;
  if(predictController)predictController.abort();
  const controller=new AbortController();predictController=controller;
  $('model-panel').setAttribute('aria-busy','true');$('model-export').disabled=true;document.querySelectorAll('[data-model-run]').forEach(button=>button.disabled=true);
  drawModelTabs();
  try{
    const data=await call('/api/predict?'+new URLSearchParams(predictScope),{signal:controller.signal});
    if(sequence!==predictSequence)return;
    predictData=data;predictScope.page=data.listing_forecasts.page;drawPredict(data);
    const issues=[];
    if(data.missing_dependencies.length)issues.push('Training packages are missing: '+data.missing_dependencies.join(', ')+'. Stored results remain available. Install with '+data.install_command+'.');
    if(data.schema_current===false)issues.push('The model database needs migration before new jobs can run. Run python3 -m toriresearch.cli init. Stored results remain available.');
    const unavailable=Object.entries(data.tables).filter(([,available])=>!available).map(([name])=>name).concat(data.errors||[]);
    if(unavailable.length)issues.push('Some model tables could not be read: '+unavailable.join(', ')+'. Run the database schema migration before using these models.');
    message('predict-message',issues.join(' '),'info');
    if(pendingModelListing&&['sale_probability','price_trajectory'].includes(predictScope.family)){
      const pk=pendingModelListing;pendingModelListing=null;openModelListing(pk);
    }
  }catch(error){if(sequence===predictSequence&&error.name!=='AbortError')message('predict-message','Model results could not be refreshed. '+error.message,'error');}
  finally{if(sequence===predictSequence){predictController=null;$('model-panel').setAttribute('aria-busy','false');}}
}
function modelSelect(id,values,placeholder,selected){
  const options='<option value="">'+esc(placeholder)+'</option>'+values.map(value=>'<option value="'+esc(value)+'">'+esc(value)+'</option>').join('');
  if($(id).dataset.options!==options){$(id).innerHTML=options;$(id).dataset.options=options;}
  if(selected&&!values.includes(selected))$(id).insertAdjacentHTML('beforeend','<option value="'+esc(selected)+'">'+esc(selected)+'</option>');
  $(id).value=selected||'';
}
function modelTable(headers,rows){
  return '<div class="table-wrap"><table class="model-table"><thead><tr>'+headers.map((label,i)=>'<th'+(i?' class="num"':'')+'>'+esc(label)+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
}
function modelCells(first,values){return '<tr><td>'+first+'</td>'+values.map(value=>'<td class="num">'+value+'</td>').join('')+'</tr>';}
function modelCategory(row){return esc(row.category_norm)+(row.region_norm?'<div class="observation-meta">'+esc(row.region_norm)+'</div>':'');}
function modelFit(data,family=predictScope.family){const selected=data.selected_versions[family];return selected?data.models.find(row=>row.family===family&&row.model_version===selected):data.models.find(row=>row.family===family);}
function currentModelMethod(data,version,fit=null,family=predictScope.family){const methods=data.supported_methods?.[family]||[];return fit?methods.includes(fit.feature_version):methods.some(method=>version===method||String(version||'').startsWith(method+'-'));}
function modelAcceptedHorizon(data,fit,horizon,family=predictScope.family){
  const acceptance=fit?.metrics?.acceptance;
  return Boolean(fit&&currentModelMethod(data,fit.model_version,fit,family)&&acceptance?.accepted&&
    (!data.acceptance_policy_version||acceptance.policy_version===data.acceptance_policy_version)&&acceptance.supported_horizons?.includes(Number(horizon)));
}
function modelForecastSupport(row,validated){
  return badge(validated?'Validated horizon':'Experimental / unverified',validated?'green':'amber')+
    badge(row.is_extrapolated==null?'Support unrecorded':row.is_extrapolated?'Extrapolated':validated?'Within validated duration':'Support unverified',row.is_extrapolated==null||row.is_extrapolated||!validated?'amber':'')+
    (row.validation_reason?'<div class="observation-meta">'+esc(row.validation_reason)+'</div>':'');
}
function modelArrivalComparison(row){
  const comparison=row.arrival_comparison;
  if(!comparison?.available)return '<span class="muted">Needs 8 stored weeks</span>';
  const change=comparison.change_pct;
  return num(comparison.recent_arrivals)+' / '+num(comparison.previous_arrivals)+
    '<div class="observation-meta">'+(change==null?'No arrivals in prior window':(change>0?'+':'')+num(change,1)+'%')+
    (comparison.zero_arrival_weeks?' · '+num(comparison.zero_arrival_weeks)+' zero-arrival weeks':'')+'</div>';
}
function modelTrendEvidence(row){
  // Volatility makes no directional claim, so a confidence figure for one would
  // describe something the state never asserted.
  if(row.trend_state==='volatile')return '<div class="observation-meta">Volatility makes no directional claim</div>';
  if(row.state_confidence==null)return '<div class="observation-meta">'+esc(row.unavailability?.state_confidence?.reason||'Direction confidence needs 3 completed weeks, currently '+num(row.evidence_weeks))+'</div>';
  return '<div class="observation-meta">'+modelPercent(row.state_confidence)+' of slopes agree'+
    (row.trend_slope==null?'':' · '+num(row.trend_slope,2)+'/week')+'</div>';
}
function modelTrendValue(row,key,digits=3){
  if(row[key]!=null)return key==='median_ask_eur'?eur(row[key]):num(row[key],digits);
  const required={seasonality_idx:['Seasonality',104],changepoint_prob:['Break detection',8],trend_slope:['Trend slope',3]};
  const reason=row.unavailability?.[key]?.reason||(required[key]?required[key][0]+' needs '+required[key][1]+' completed weeks, currently '+num(row.evidence_weeks):'Median entry ask needs 1 observed entry price, currently unrecorded');
  return '<span class="muted">—</span><div class="observation-meta">'+esc(reason)+'</div>';
}
function modelArrivalProjection(data,row){
  const items=(data.category_trends?.forecasts?.items||[]).filter(forecast=>forecast.category_norm===row.category_norm);
  if(!items.length)return '<span class="muted">No supported projection</span>';
  const furthest=items.reduce((a,b)=>Number(b.horizon_weeks)>Number(a.horizon_weeks)?b:a);
  const interval=furthest.arrivals_lo==null||furthest.arrivals_hi==null?
    '<div class="observation-meta">'+esc(furthest.unavailability?.predictive_interval?.reason||'Predictive interval unrecorded; count quantiles are required')+'</div>':
    '<div class="observation-meta">90% '+num(furthest.arrivals_lo)+'–'+num(furthest.arrivals_hi)+' at '+num(furthest.horizon_weeks)+' weeks</div>';
  return modelArrivalValidation(furthest)+'<div>'+num(furthest.expected_arrivals,1)+' in '+num(furthest.horizon_weeks)+' weeks</div>'+interval;
}
function modelArrivalValidation(row){
  const score=row.evaluation||{};
  const status=row.is_accepted?'Validated horizon':!score.available?(score.n_test?'Unvalidated — insufficient out-of-time evidence':'Unvalidated — no out-of-time test'):'Experimental / unverified';
  const reason=score.reason||(!row.is_accepted&&score.available?(!score.improves_baseline?'Does not beat the arrival baseline':!score.calibration_ok?'Interval coverage check failed':'Horizon support check failed'):null);
  const evidence=score.n_test!=null?num(score.n_test)+' held-out weeks · MAE '+num(score.mae_arrivals,2)+' / baseline '+num(score.baseline_mae_arrivals,2):null;
  return badge(status,row.is_accepted?'green':'amber')+(reason?'<div class="observation-meta">'+esc(reason)+'</div>':'')+(evidence?'<div class="observation-meta">'+esc(evidence)+'</div>':'');
}
function modelHorizonEvidence(data,fit){
  const evaluation=fit.metrics?.evaluation||{},acceptance=fit.metrics?.acceptance||{},family=predictScope.family;
  if(!evaluation.by_horizon)return '<div class="validation-callout amber"><strong>No horizon-specific validation</strong><p>Train the current model to check baseline error, calibration and category support at each horizon.</p></div>';
  const price=family==='price_trajectory';
  const rows=(data.horizons||MODEL_HORIZONS).map(horizon=>{
    const score=evaluation.by_horizon[String(horizon)]||{},accepted=modelAcceptedHorizon(data,fit,horizon);
    const segments=acceptance.supported_segments?.[String(horizon)]||[];
    let reason=score.reason;
    if(!reason){
      if(!score.available)reason='Validation unavailable';
      else if((score.n_horizon_observed??score.n_test??0)<10)reason='Too little observed follow-up';
      else if(!score.horizon_supported)reason=price&&score.evaluated_duration_days_max!=null?'Validated only over '+num(score.evaluated_duration_days_max,2)+' days':'Beyond training follow-up';
      else if(!score.improves_baseline)reason='Does not beat the baseline';
      else if(!score.calibration_ok)reason='Calibration check failed';
      else if(!accepted)reason='Category, age or fit checks have not passed';
    }
    const observed=score.n_horizon_observed??score.n_test;
    const error=price?score.mae_price_ratio:score.brier_score,baseline=price?score.baseline_mae_price_ratio:score.baseline_brier_score;
    const age=segments.filter(row=>row.max_age_days!=null).map(row=>Number(row.max_age_days));
    return modelCells(num(horizon)+' days'+(horizon===predictScope.horizon?'<div class="observation-meta">Selected horizon</div>':''),[
      badge(accepted?'Validated':'Experimental',accepted?'green':'amber')+(reason?'<div class="observation-meta">'+esc(reason)+'</div>':''),
      num(observed),num(error,5)+' / '+num(baseline,5),price?modelPercent(score.coverage_90):modelPercent(score.calibration_error),
      num(segments.length)+(age.length?'<div class="observation-meta">Age support up to '+num(Math.max(...age))+' days, varies by category / condition</div>':'')]);
  });
  return '<div class="section-divider"></div><h3>Validation by future horizon</h3><p class="note">'+(price?'Error is asking-price ratio MAE; the baseline keeps the current ask.':'Error is IPCW Brier score; the baseline uses the training cohort’s sale incidence. Age checkpoints test listings still observed at risk.')+' Lower error is better. Only validated category and condition groups qualify for guidance.</p>'+modelTable(['Horizon','Guidance status','Observed holdout',price?'MAE / baseline':'Brier / baseline',price?'90% interval coverage':'Calibration error','Supported groups'],rows);
}
function drawPredict(data){
  const family=predictScope.family,info=MODEL_INFO[family],listingFamily=['sale_probability','price_trajectory'].includes(family),fit=modelFit(data);
  $('predict-models').textContent=num(data.registry_total??data.models.length);$('predict-models-note').textContent=data.registry_limited?'Latest 200 fits, plus selected and active fits shown':data.models.length?'Latest fit '+date(data.models[0].trained_at):'No training fit registered yet';
  $('model-search-field').hidden=!listingFamily;if(document.activeElement!==$('model-search'))$('model-search').value=predictScope.search;
  $('predict-sales').textContent=num(data.sale_probability.listings);$('predict-sales-note').textContent=data.sale_probability.computed_at?'Computed '+date(data.sale_probability.computed_at):'No stored forecasts in this scope';
  $('predict-paths').textContent=num(data.price_trajectory.listings);$('predict-paths-note').textContent=data.price_trajectory.computed_at?'Computed '+date(data.price_trajectory.computed_at):'No stored paths in this scope';
  $('predict-trend-states').textContent=num(data.category_trends.categories);$('predict-trend-states-note').textContent=data.category_trends.latest_week?'Stored week '+modelDate(data.category_trends.latest_week):'Waiting for completed weekly evidence';
  $('model-method').textContent=info.method;$('model-title').textContent=info.title;$('model-description').textContent=info.description;$('model-result-note').textContent=info.note;
  modelSelect('model-category',data.options.categories,'All categories',predictScope.category);
  modelSelect('model-region',data.options.regions,'Nationwide',predictScope.region);
  modelSelect('model-version',[...new Set([...(data.versions[family]||[]),...data.models.filter(row=>row.family===family).map(row=>row.model_version)])],'Current results',predictScope.version);
  $('model-horizon').value=String(predictScope.horizon);$('model-horizon-field').hidden=!listingFamily;$('model-window-field').hidden=family!=='seller_archetypes';
  $('model-category').disabled=family==='seller_archetypes';$('model-region').disabled=family==='seller_archetypes';
  $('model-scope-note').textContent=family==='seller_archetypes'?'Seller groups cover their complete fitted observation window.':listingFamily?'Stored forecasts keep their computation time and fitted version.':'Nationwide selects national aggregate rows. Regions are separate cohorts.';
  const rows=listingFamily?data.listing_forecasts.items:data[family].items||data.seller_archetypes.archetypes;
  const validatedRows=listingFamily&&rows.length&&rows.every(row=>row.is_accepted&&modelAcceptedHorizon(data,fit,row.horizon_days));
  $('model-status').textContent=predictScope.version?'Selected fit':rows.length?listingFamily?(validatedRows?'Validated forecasts':'Experimental forecasts'):'Stored results':listingFamily&&fit?'Trained · no forecasts':'No results in scope';$('model-status').className='badge '+(predictScope.version||listingFamily&&!validatedRows?'amber':rows.length?'green':'');
  const version=data.selected_versions[family];
  const storedMethods=listingFamily||family==='seller_archetypes'?(version?[version]:[]):(data[family].items||[]).map(row=>row.model_version);
  const legacy=storedMethods.some(method=>!currentModelMethod(data,method,listingFamily?fit:null));
  if(legacy){$('model-description').textContent='These stored outputs use an older or unverified method. The current implementation uses '+info.method+'. Train or compute new results to use it.';$('model-result-note').textContent='Older outputs retain their original interval and time-origin definitions. Fit and validate the current implementation before interpreting them as current forecasts.';}
  const extrapolated=listingFamily?data.listing_forecasts.items.filter(row=>row.is_extrapolated).length:0;
  const evaluation=fit?.metrics?.evaluation;
  setHTML('model-evidence',(version?badge((data.active_models[family]===version?'Active fit · ':'Stored fit · ')+version,'purple'):'')+(listingFamily?badge(predictScope.horizon+(legacy?' · stored horizon/day':' days ahead')):badge(legacy?'Stored model snapshots':family==='category_trends'?'Completed Helsinki weeks':family==='market_concentration'?'Calendar arrival cohorts':'Stable seller linkage'))+(legacy?badge('Legacy or unverified method · recompute','amber'):'')+(extrapolated?badge(num(extrapolated)+' visible forecasts extrapolated','amber'):'')+(family==='price_trajectory'&&evaluation?.available&&!evaluation.improves_baseline?badge('Holdout error exceeds no-change baseline','amber'):'')+(family==='price_trajectory'&&!evaluation?.available?badge('Holdout quality unverified','amber'):'')+(family==='market_concentration'&&data.market_concentration.items.some(row=>row.linkage_coverage!=null&&row.linkage_coverage<.8)?badge('Partial seller linkage','amber'):'')+(family==='market_concentration'&&data.market_concentration.items.some(row=>row.is_complete_month===false||row.is_complete_month===0)?badge('Partial month cohorts','amber'):''));
  drawModelResults(data);drawModelValidation(data,fit);drawModelControls(data);drawModelHistory(data);drawModelJobs(data.jobs||[]);
  $('predict-registry-badge').textContent=data.registry_limited?num(data.models.length)+' shown / '+num(data.registry_total)+' fits':num(data.models.length)+' registered fits';
  setHTML('predict-registry',data.models.length?modelTable(['Family / version','Fit status','Trained / window','Observations / entities','Method & evidence'],data.models.map(row=>modelCells(esc(MODEL_INFO[row.family]?.title||row.family)+'<div class="model-version">'+esc(row.model_version)+'</div>',[
    badge(row.active?'Active':'Recorded',row.active?'green':''),date(row.trained_at)+'<div class="observation-meta">'+modelDate(row.train_start_at)+' → '+modelDate(row.train_end_at)+'</div>',num(row.n_observations)+' / '+num(row.n_entities),'<details class="model-fit-details"><summary>'+esc(row.feature_version)+'</summary><pre>'+esc(JSON.stringify({metrics:row.metrics,method:row.method},null,2))+'</pre></details>'
  ]))):empty('No registered fits yet','Training records its window, sample size, method and diagnostics.','spark'));
  scheduleTableScroll();
}
function drawModelResults(data){
  const family=predictScope.family,listingFamily=['sale_probability','price_trajectory'].includes(family);
  let headers=[],rows=[];
  if(listingFamily){
    const current=currentModelMethod(data,data.selected_versions[family],modelFit(data));
    headers=family==='sale_probability'?['Listing / current state','Current ask','Sale probability','Support','Computed']:['Listing / current state','Current ask',current?'Future median ask':'Stored model ask',current?'90% predictive interval':'Stored interval','Support','Computed'];
    rows=data.listing_forecasts.items.map(row=>{
      const first='<button class="row-link" type="button" data-model-listing="'+row.listing_pk+'">'+esc(row.title||row.listing_id)+'</button><div class="observation-meta">'+esc(row.platform)+' · '+esc(row.municipality_norm||'Unknown location')+' · '+esc(row.status_norm)+' / '+esc(row.outcome_status)+'</div>';
      const support=modelForecastSupport(row,row.is_accepted&&modelAcceptedHorizon(data,modelFit(data),row.horizon_days));
      return modelCells(first,family==='sale_probability'?[eur(row.listed_price_eur),modelPercent(row.p_sale),support,date(row.computed_at)]:[eur(row.listed_price_eur),eur(row.predicted_price_eur),row.price_ci_lo==null||row.price_ci_hi==null?'—':eur(row.price_ci_lo)+'–'+eur(row.price_ci_hi),support,date(row.computed_at)]);
    });
    const page=data.listing_forecasts;$('model-pagination').hidden=!page.total;$('model-page-note').textContent=num(page.total)+' listings · page '+num(page.page)+' of '+num(page.pages);$('model-prev').disabled=page.page<=1;$('model-next').disabled=page.page>=page.pages;
  }else{
    $('model-pagination').hidden=true;
    if(family==='category_trends'){
      headers=['Category / stored week','State / evidence','Arrivals','Latest 4 weeks / prior 4','Projected arrivals','Median entry ask','Break score','Seasonal factor','Evidence / method'];
      rows=data.category_trends.items.map(row=>modelCells(modelCategory(row)+'<div class="observation-meta">'+modelDate(row.week_start)+'</div>',[badge(PREDICT_STATES[row.trend_state]||row.trend_state,row.trend_state==='declining'||row.trend_state==='volatile'?'amber':'purple')+modelTrendEvidence(row),num(row.arrivals),modelArrivalComparison(row),modelArrivalProjection(data,row),modelTrendValue(row,'median_ask_eur'),modelTrendValue(row,'changepoint_prob'),modelTrendValue(row,'seasonality_idx'),num(row.evidence_weeks)+' weeks<div class="observation-meta">'+esc(row.model_version)+'</div>']));
    }else if(family==='market_concentration'){
      headers=['Category / month','Count HHI','Asking-value HHI','Top 3 · count / value','Linked sellers / arrivals','Linkage / price coverage'];
      rows=data.market_concentration.items.map(row=>modelCells(modelCategory(row)+'<div class="observation-meta">'+modelDate(row.month_start)+' · '+(row.is_complete_month==null?'Month coverage unrecorded':row.is_complete_month?'Complete month':'Partial month')+'</div>',[num(row.hhi_count,3),num(row.hhi,3),modelPercent(row.top3_count_share)+' / '+modelPercent(row.top3_share),num(row.n_active_sellers)+' / '+num(row.n_observed_listings),badge(modelPercent(row.linkage_coverage),row.linkage_coverage<.8?'amber':'')+' / '+modelPercent(row.price_coverage)]));
    }else{
      headers=['Observed behaviour','Distinct sellers',currentModelMethod(data,data.selected_versions[family],modelFit(data))?'Mean membership strength':'Stored membership strength'];
      rows=data.seller_archetypes.archetypes.map(row=>modelCells(esc(row.archetype_id.replaceAll('_',' ')),[num(row.sellers),num(row.mean_confidence,3)]));
    }
  }
  const fit=modelFit(data),score=fit?.metrics?.evaluation?.by_horizon?.[String(predictScope.horizon)];
  const accepted=listingFamily&&modelAcceptedHorizon(data,fit,predictScope.horizon);
  const waiting=listingFamily&&fit?'<div class="validation-callout '+(accepted?'':'amber')+'"><strong>'+(accepted?'Model ready; generate forecasts for this scope':'Model trained; '+num(predictScope.horizon)+'-day guidance is awaiting validation')+'</strong><p>'+esc(accepted?'Use Generate forecasts to save results at this horizon.':score?.reason||fit.metrics?.evaluation?.reason||fit.metrics?.acceptance?.reason||'This horizon has not passed its holdout checks.')+'</p>'+(accepted?'':'<p>Preview experimental forecasts to inspect this trained model. Experimental estimates remain unverified.</p>')+'</div>':'';
  setHTML('model-results',rows.length?modelTable(headers,rows):listingFamily&&predictScope.search?empty('No stored forecasts match your search','Try another title or listing ID, or review the selected fit, scope and horizon.','search'):empty('No stored results in this scope','Review the evidence requirements, choose a scope, and run this model to produce results.','chart')+waiting);
  $('model-export').disabled=!rows.length;
}
function drawModelControls(data){
  const family=predictScope.family,info=MODEL_INFO[family],busy=predictStarting||(data.jobs||[]).some(job=>['queued','running','cancelling'].includes(job.status));
  const listingFamily=['sale_probability','price_trajectory'].includes(family);
  $('model-run-note').textContent=listingFamily?'Train on the collected data, then preview experimental forecasts. Validated forecasts require a fit and horizon that pass chronological checks.':'Compute and persist summaries using collected observations.';
  $('model-run-scope').textContent=family==='sale_probability'||family==='price_trajectory'?'Training and evaluation use the full research history. Forecasts use this category and region, capped at '+num(data.job_limit)+' recent eligible listings per run.':family==='seller_archetypes'?'Computes all eligible linked sellers over the selected observation window.':'Computes all eligible categories in the selected region. The category filter controls which stored results are shown.';
  const historical=Boolean(predictScope.version),missing=data.missing_dependencies.length>0||data.schema_current===false;
  const activeFit=data.models.find(row=>row.family===family&&row.model_version===data.active_models[family]);
  const supported=modelAcceptedHorizon(data,activeFit,predictScope.horizon);
  const candidate=Boolean(data.candidate_models?.[family]);
  const actions=listingFamily?[info.actions[1],['preview','Preview experimental forecasts'],info.actions[0],info.actions[2]]:info.actions;
  setHTML('model-run-actions',actions.map(([action,label],i)=>'<button type="button" class="btn '+(i===0?'primary':'')+'" data-model-run="'+action+'" '+(busy||missing||historical||(action==='predict'&&!supported)||(action==='preview'&&!candidate)?'disabled':'')+'>'+esc(busy?'A model job is running':label)+'</button>').join('')+(historical?'<p class="note">Select Current results to run a new job.</p>':listingFamily&&!supported?'<p class="note">The selected '+predictScope.horizon+'-day horizon has not cleared guidance checks. '+(candidate?'The trained candidate is available for an experimental preview.':'Train the current model to create a preview candidate.')+'</p>':''));
  const counts=data.source_counts,link=data.linkage;
  const weeks=(data.category_trends?.items||[]).map(row=>row.evidence_weeks).filter(value=>value!=null&&Number.isFinite(Number(value))).map(Number);
  const seasonality=family==='category_trends'?'<p class="note">Seasonality needs 104 completed weeks, currently '+(weeks.length?num(Math.min(...weeks))+(Math.min(...weeks)===Math.max(...weeks)?'':'–'+num(Math.max(...weeks)))+' in the displayed cohorts':'unrecorded in this scope')+'.</p>':'';
  const fitted=modelFit(data),readiness=fitted?.metrics?.readiness;
  const measurements=family==='price_trajectory'&&readiness?'<div class="section-divider"></div>'+metric('Measured intervals at training',num(readiness.n_intervals))+metric('Listings with daily paths',num(readiness.n_eligible_listings))+metric('Intervals per listing',num(readiness.intervals_per_listing,2))+metric('Longest measured path',num(readiness.maximum_observed_path_days,2)+' days')+'<p class="note">Row volume supports fitting. Each forecast horizon also needs later measured endpoints for validation.</p>':'';
  setHTML('model-requirements',info.requirements.map(([key,value])=>metric(key,esc(value))).join('')+'<div class="section-divider"></div>'+(
    ['seller_archetypes','market_concentration'].includes(family)?metric('Resolved seller coverage',percent(link.resolved_coverage_pct))+metric('Seller key enabled',link.key_enabled?'Yes':'No')+metric('Seller versions observed',num(link.sellers)):
    family==='sale_probability'?metric('Stored sale labels',num(counts.sale_labels))+metric('Stored listings',num(counts.listings)):family==='price_trajectory'?metric('Active priced candidates',num(counts.active_priced)):metric('Categories with stored trends',num(data.category_trends.categories)))+measurements+seasonality+'<p class="note">Source counts are preliminary. The model applies its evidence and timing checks when it runs.</p>');
}
function drawModelValidation(data,fit){
  const family=predictScope.family;
  if(family==='category_trends'){
    const rows=data.category_trends.items.map(row=>{
      const score=row.direction_evaluation||{};
      const reason=score.reason||(!score.available?'Direction validation needs 10 completed non-overlapping 8-week windows with at least 30 arrivals in each training and outcome block; currently unrecorded':null);
      return modelCells(modelCategory(row),[badge(score.available?'Descriptive check':'Not enough completed evidence',score.available?'purple':'amber')+(reason?'<div class="observation-meta">'+esc(reason)+'</div>':''),num(score.n_test_windows)+(score.n_thin_windows==null?'':'<div class="observation-meta">'+num(score.n_thin_windows)+' thin windows excluded</div>'),score.available?modelPercent(score.agreement_rate)+' / '+modelPercent(score.flat_baseline_agreement_rate):'—',score.confusion?'<details><summary>State confusion</summary><pre>'+esc(JSON.stringify(score.confusion,null,2))+'</pre></details>':'Unrecorded']);
    });
    setHTML('model-validation','<h3>Direction checks against following 8 weeks</h3><p class="note">Fixed thresholds: 4% of level per week and at least 8 direction weeks. Thresholds remain unchanged. Agreement covers blocks with at least 30 arrivals in both training and outcome windows; thinner blocks are excluded. It describes collected arrivals and does not calibrate direction confidence.</p>'+modelTable(['Category','Evidence','Completed 8-week windows','Agreement / flat baseline','Confusion'],rows));return;
  }
  if(!fit){
    // "Seller key enabled: Yes" next to "0% resolved coverage" reads as a
    // contradiction, and "no fit is registered" invites the operator to just
    // press Train again. These two families cannot produce anything until
    // listings resolve to a seller version, so say that here rather than
    // leaving it to be rediscovered from a failed job.
    const link=data.linkage||{};
    if(['seller_archetypes','market_concentration'].includes(family)&&!link.listings_with_version){
      setHTML('model-validation','<div class="validation-callout amber"><strong>Seller linkage is unresolved</strong><p>This model groups listings under a seller, and '
        +num(link.listings_total)+' collected listings currently resolve to '+num(link.listings_with_version)+' seller versions. '
        +(link.reason?esc(link.reason)+' ':'')+'</p><p>An empty result here would read as "this market has no measurable seller concentration", which is the opposite of what a missing key means.</p></div>');
      return;
    }
    setHTML('model-validation','<p class="note">No fit is registered for this model. Completed computations appear in Model activity below.</p>');return;
  }
  let content='<div class="model-version">'+esc(fit.model_version)+'</div><div class="metric-list">'+metric('Trained',date(fit.trained_at))+metric('Training window',modelDate(fit.train_start_at)+' → '+modelDate(fit.train_end_at))+metric('Observations / entities',num(fit.n_observations)+' / '+num(fit.n_entities))+'</div>';
  if(family==='price_trajectory'){
    const diagnostics=fit.metrics.diagnostics||{},evaluation=fit.metrics.evaluation||{};
    const readiness=fit.metrics.readiness;
    if(readiness)content+='<div class="metric-list">'+metric('Eligible measured intervals',num(readiness.n_intervals))+metric('Eligible listings',num(readiness.n_eligible_listings))+metric('Intervals per listing',num(readiness.intervals_per_listing,2))+metric('Minimum endpoint spacing',num(readiness.minimum_interval_days)+' day')+'</div>';
    content+='<div class="section-divider"></div><div class="validation-callout '+(diagnostics.diagnostics_ok?'':'red')+'"><strong>'+esc(diagnostics.diagnostics_ok?'Sampling converged':'Sampling is unverified or failed')+'</strong><p>Convergence checks the posterior sampler. Predictive accuracy is measured on held-out listings.</p></div><div class="metric-list">'+metric('Maximum split R-hat',num(diagnostics.max_split_rhat,4))+metric('Minimum effective sample size',num(diagnostics.min_effective_samples))+'</div>';
    content+='<div class="section-divider"></div><div class="validation-callout '+(evaluation.available&&evaluation.improves_baseline?'':'amber')+'"><strong>'+esc(!evaluation.available?'Holdout validation unavailable':evaluation.improves_baseline?'Lower holdout error than no change':'Does not beat the no-change baseline')+'</strong><p>'+esc(evaluation.reason||'A converged fit can still make worse predictions than retaining the current ask.')+'</p></div>';
    if(evaluation.available)content+='<div class="metric-list">'+metric('Ratio MAE / baseline',num(evaluation.mae_price_ratio,5)+' / '+num(evaluation.baseline_mae_price_ratio,5))+metric('90% interval coverage',modelPercent(evaluation.coverage_90))+metric('Holdout listings',num(evaluation.n_test_listings))+metric('Evaluated duration',num(evaluation.evaluated_duration_days_min,3)+'–'+num(evaluation.evaluated_duration_days_max,3)+' days')+(evaluation.training_span_days==null?'':'<p class="note">Training follow-up reaches '+num(evaluation.training_span_days,3)+' days, but a horizon is only counted as supported when the held-out endpoints actually reached it.</p>')+'</div><p class="note" style="margin-top:15px">Validation at shorter durations does not validate the selected '+predictScope.horizon+'-day forecast.</p>';
  }else if(family==='sale_probability'){
    const evaluation=fit.metrics.evaluation||{};
    content+='<div class="section-divider"></div><div class="validation-callout '+(evaluation.available?'':'amber')+'"><strong>'+esc(evaluation.available?'Chronological holdout stored with this fit':'Holdout validation unavailable')+'</strong><p>'+esc(evaluation.reason||'Earlier cohorts train the model; later listings test future sale incidence. Validation at entry alone cannot support forecasts for older listings.')+'</p></div>';
  }else content+='<div class="section-divider"></div><p class="note">Group membership measures HDBSCAN assignment strength. Unclassified noise is retained in the sample.</p>';
  if(['sale_probability','price_trajectory'].includes(family))content+=modelHorizonEvidence(data,fit);
  setHTML('model-validation',content);
}
function modelJobOutcome(job){
  if(job.status!=='experimental')return '';
  if(job.action==='preview')return '<p class="note">Experimental forecasts were generated and saved. Inspect their horizon and support labels before interpreting the estimates. This preview does not activate a model or qualify it for guidance.</p>';
  // A fit that ran and stored a candidate but did not clear acceptance is not a
  // crash. Say what it produced and what is missing, so the panel is read as
  // "waiting for evidence" rather than "the job broke".
  const result=job.result||{},acceptance=result.acceptance||{};
  const blockers=Object.entries(result.evaluation?.by_horizon||{})
    .filter(([,score])=>score&&score.reason).slice(0,3)
    .map(([horizon,score])=>num(horizon)+' days: '+score.reason);
  return '<p class="note">The fit completed and was stored as an experimental candidate. It is not activated, so it cannot produce guidance.'
    +(acceptance.reason?'<br>'+esc(acceptance.reason)+'.':'')
    +(blockers.length?'<br>'+esc(blockers.join(' · ')):'')+'</p>';
}
function drawModelJobs(jobs){
  const active=jobs.find(job=>['queued','running','cancelling'].includes(job.status));
  $('model-job-status').textContent=active?active.status==='cancelling'?'Cancelling job':active.status==='queued'?'Job queued':'Job running':'No active job';$('model-job-status').className='badge '+(active?'purple':'');
  setHTML('model-jobs',jobs.length?jobs.slice(0,6).map(job=>'<article class="model-job"><div class="model-job-heading"><div><strong>'+esc((MODEL_INFO[job.family]?.title||job.family)+' · '+job.action)+'</strong><p class="note">Started '+date(job.started_at)+(job.finished_at?' · finished '+date(job.finished_at):['queued','running','cancelling'].includes(job.status)?' · runs in the background':' · completion time unrecorded')+(job.target_count!=null?' · '+num(job.target_count)+' candidate listings':'')+'</p></div><div class="unit-actions">'+badge(job.status,job.status==='done'?'green':job.status==='failed'||job.status==='interrupted'?'red':job.status==='running'?'purple':'amber')+(['queued','running'].includes(job.status)&&job.can_cancel?'<button class="btn small" type="button" data-model-cancel="'+esc(job.job_id)+'">Cancel</button>':'')+'</div></div>'+modelJobOutcome(job)+(job.error?'<p class="note negative">'+esc(job.error)+'</p>':'')+(job.warnings?'<details><summary>Model warnings</summary><pre>'+esc(job.warnings)+'</pre></details>':'')+(job.status==='cancelled'?'<p class="note">Work completed before cancellation may already have been saved.</p>':'')+(job.output||job.result?'<details><summary>Inspect result</summary><pre>'+esc(job.result?JSON.stringify(job.result,null,2):job.output)+'</pre></details>':'')+'</article>').join(''):'<p class="note">No model jobs have run from this workspace yet. Choose a model and run an explicit job above.</p>');
  clearTimeout(predictPoll);
  if(active&&currentView==='predict'&&!document.hidden)predictPoll=setTimeout(()=>loadPredict(),1800);
}
async function runModel(action){
  if(predictStarting||predictController)return;
  predictStarting=true;if(predictData)drawModelControls(predictData);
  try{await call('/api/predict',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,family:predictScope.family,scope:{...predictScope},window_days:Number($('model-window').value)})});toast('Model job started. Progress is shown in Model activity.');await loadPredict();}
  catch(error){message('predict-message',error.message,'error');}
  finally{predictStarting=false;if(predictData)drawModelControls(predictData);}
}
async function cancelModel(jobId){
  try{await call('/api/predict',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel',job_id:jobId})});await loadPredict();}
  catch(error){message('predict-message',error.message,'error');}
}
function researchChart(points,series,{label,unit='',band=false,ymax=null,bandLabel='Shaded band · 90% posterior predictive interval'}={}){
  if(!points.length)return empty('No chart history yet','Stored observations will appear here when they are available.','chart');
  const width=680,height=250,left=52,right=24,top=22,bottom=43;
  const finite=value=>typeof value==='number'&&Number.isFinite(value);
  const values=points.flatMap(row=>series.map(s=>row[s.key])).concat(band?points.flatMap(row=>[row.lo,row.hi]):[]).filter(finite);
  if(!values.length)return empty('No measured values','Missing estimates are left blank.','chart');
  const xvalues=points.map((row,i)=>finite(row.x)?row.x:i),xmin=Math.min(...xvalues),xmax=Math.max(...xvalues);
  const minimum=0,maximum=ymax||Math.max(1,...values)*1.1,x=i=>left+(xmax===xmin?.5:(xvalues[i]-xmin)/(xmax-xmin))*(width-left-right),y=value=>height-bottom-(value-minimum)/(maximum-minimum)*(height-top-bottom);
  const ticks=Array.from({length:4},(_,i)=>maximum*i/3);
  let svg='<svg class="research-chart" viewBox="0 0 '+width+' '+height+'" role="img" aria-label="'+esc(label||'Observed research history')+'"><title>'+esc(label||'Observed research history')+'</title>'+ticks.map(value=>'<line class="grid" x1="'+left+'" x2="'+(width-right)+'" y1="'+y(value)+'" y2="'+y(value)+'"/><text x="'+(left-10)+'" y="'+(y(value)+4)+'" text-anchor="end">'+esc(num(value,maximum<=1?2:0))+esc(unit)+'</text>').join('');
  if(band){const segments=[];let segment=[];points.forEach((row,i)=>{if(finite(row.lo)&&finite(row.hi))segment.push([row,i]);else{if(segment.length)segments.push(segment);segment=[];}});if(segment.length)segments.push(segment);svg+=segments.map(part=>'<polygon class="band" points="'+part.map(([row,i])=>x(i)+','+y(row.hi)).concat([...part].reverse().map(([row,i])=>x(i)+','+y(row.lo))).join(' ')+'"/>').join('');}
  for(const [index,s] of series.entries()){
    let path='',connected=false;
    points.forEach((row,i)=>{if(finite(row[s.key])){path+=(connected?' L':' M')+x(i)+','+y(row[s.key]);connected=true;}else connected=false;});
    svg+='<path class="series '+(index?'secondary':'')+'" d="'+path+'"/>';
    svg+=points.map((row,i)=>finite(row[s.key])?'<circle class="'+(index?'secondary':'')+'" tabindex="0" cx="'+x(i)+'" cy="'+y(row[s.key])+'" r="3.5" aria-label="'+esc(row.label+' · '+s.label+' '+num(row[s.key],3)+unit)+'"><title>'+esc(row.label+' · '+s.label+' '+num(row[s.key],3)+unit)+'</title></circle>':'').join('');
  }
  const labels=[...new Set([0,Math.floor((points.length-1)/2),points.length-1])];
  svg+=labels.map(i=>'<text x="'+x(i)+'" y="'+(height-12)+'" text-anchor="'+(i===0?'start':i===points.length-1?'end':'middle')+'">'+esc(points[i].label)+'</text>').join('')+'</svg>';
  return svg+'<div class="chart-key">'+series.map((s,i)=>'<span><i class="'+(i?'secondary':'')+'"></i>'+esc(s.label)+'</span>').join('')+(band?'<span>'+esc(bandLabel)+'</span>':'')+'</div>';
}
function drawModelHistory(data){
  const family=predictScope.family,visible=['category_trends','market_concentration','seller_archetypes'].includes(family);
  $('model-history-card').hidden=!visible;if(!visible)return;
  $('model-series').hidden=family==='seller_archetypes';
  if(family==='seller_archetypes'){
    const arch=data.seller_archetypes;$('model-history-title').textContent='Observed seller mix';$('model-history-caption').textContent=num(arch.sellers)+' distinct sellers · '+num(arch.clusters)+' clusters · '+num(arch.noise)+' unclassified';
    setHTML('model-history-chart',arch.archetypes.length?arch.archetypes.map(row=>'<div class="seller-bar-row"><span>'+esc(row.archetype_id.replaceAll('_',' '))+'</span><div class="progress-track"><i style="width:'+Math.max(0,Math.min(100,row.sellers/arch.sellers*100))+'%"></i></div><strong class="num">'+num(row.sellers)+'</strong></div>').join(''):empty('No linked seller groups yet','Revisit item pages under the configured seller key, then compute groups once the sample is sufficient.','list'));
    $('model-history-note').textContent='Each stable seller counts once within this fitted version, including sellers classified as noise.';return;
  }
  const aggregate=data[family],categories=aggregate.items.map(row=>row.category_norm),old=$('model-series').value;
  setHTML('model-series',categories.map(category=>'<option value="'+esc(category)+'">'+esc(category)+'</option>').join(''));$('model-series').value=categories.includes(old)?old:categories[0]||'';
  const history=aggregate.history.filter(row=>row.category_norm===$('model-series').value);
  const projection=(aggregate.forecasts?.items||[]).filter(row=>row.category_norm===$('model-series').value);
  $('model-history-title').textContent=family==='category_trends'?(projection.length?'Weekly arrivals and projection':'Stored weekly arrivals'):'Monthly concentration history';$('model-history-caption').textContent=$('model-series').value||'Choose a category with stored results';
  const points=family==='category_trends'?history.map(row=>({label:modelDate(row.week_start),x:Date.parse(row.week_start),arrivals:Number(row.arrivals),expected:null}))
    .concat(projection.map(row=>({label:modelDate(row.target_week_start),x:Date.parse(row.target_week_start),arrivals:null,
      expected:row.expected_arrivals==null?null:Number(row.expected_arrivals),lo:row.arrivals_lo==null?null:Number(row.arrivals_lo),hi:row.arrivals_hi==null?null:Number(row.arrivals_hi)})))
    :history.map(row=>({label:modelDate(row.month_start),count:row.hhi_count==null?null:Number(row.hhi_count),value:row.hhi==null?null:Number(row.hhi)}));
  const projectionEvidence=projection.map(row=>'<div>'+num(row.horizon_weeks)+' weeks · '+modelArrivalValidation(row)+'</div>').join('');
  setHTML('model-history-chart',projectionEvidence+researchChart(points,family==='category_trends'?[{key:'arrivals',label:'Collected arrivals'},{key:'expected',label:'Projected arrivals'}]:[{key:'count',label:'Count HHI'},{key:'value',label:'Initial asking-value HHI'}],{label:$('model-history-title').textContent+' for '+$('model-series').value,ymax:family==='market_concentration'?1:null,band:projection.some(row=>row.arrivals_lo!=null&&row.arrivals_hi!=null),bandLabel:'Shaded band · 90% predictive interval for projected weekly arrivals'}));
  const legacy=history.some(row=>!currentModelMethod(data,row.model_version));
  const projected=projection.some(row=>!data.category_trends?.forecasts?.supported_methods?.includes(row.model_version));
  $('model-history-note').textContent=(legacy?'This history includes older or unverified methods. Recompute with the current implementation before interpreting interval, cohort or completed-period definitions. ':family==='category_trends'?(projection.length?'Projected weeks are unobserved. The projection carries a 90% interval that widens with lead time and projects this collector’s arrivals, not the market. ':'No accepted projection is stored for this category. Each horizon needs at least ten held-out completed weeks, lower error than the arrival baselines and adequate interval coverage. ')+'Only completed weeks are included. Seasonality needs 104 weeks; break detection starts at 8 weeks. Collection changes can affect arrival counts.':'Each point describes the linked part of a monthly arrival cohort. The current month is partial, and price coverage affects asking-value HHI.')+(projected?' This projection uses an unverified method. Recompute it before relying on it. ':'')+(aggregate.history_limited?' History is limited to 6,240 stored category periods. Narrow the category scope for more detail.':'');
}
function closeModelDetail(){
  predictDetailSequence++;
  if($('model-detail-dialog').open)$('model-detail-dialog').close();
  const url=new URL(location.href);url.searchParams.delete('model_listing');history.replaceState(null,'',url);
}
async function openModelListing(pk){
  const sequence=++predictDetailSequence;
  const selected={...predictScope},family=selected.family;
  const url=new URL(location.href);url.searchParams.set('model_listing',pk);history.replaceState(null,'',url);
  $('model-detail-title').textContent='Listing forecast';setHTML('model-detail-content','<p class="note">Reading stored forecast and price observations…</p>');$('model-detail-dialog').showModal();
  try{
    const data=await call('/api/predict?'+new URLSearchParams({...selected,listing_pk:pk}));if(sequence!==predictDetailSequence)return;
    const detail=data.listing_forecasts.detail;if(!detail){setHTML('model-detail-content',empty('Forecast unavailable','No forecasts remain for this listing, fit and scope.','chart'));return;}
    $('model-detail-title').textContent=detail.listing.title||'Listing forecast';
    const price=family==='price_trajectory',forecasts=detail.forecasts,points=forecasts.map(row=>({label:row.horizon_days+'d',x:Number(row.horizon_days),value:price?(row.predicted_price_eur==null?null:Number(row.predicted_price_eur)):(row.p_sale==null?null:Number(row.p_sale)*100),lo:row.price_ci_lo==null?null:Number(row.price_ci_lo),hi:row.price_ci_hi==null?null:Number(row.price_ci_hi)}));
    const extrapolated=forecasts.some(row=>row.is_extrapolated);
    const dates=[...new Set(forecasts.map(row=>row.computed_at))],current=currentModelMethod(data,data.selected_versions[family],modelFit(data,family),family);
    const chart=!current?'<div class="message info">These outputs use an older or unverified method. Their original interval and time-origin definitions are unverified. Train the current model before interpreting them as future forecasts.</div>':dates.length===1?researchChart(points,[{key:'value',label:price?'Future median ask':'Conditional sale probability'}],{label:price?'Asking-price forecast':'Conditional probability by future horizon',unit:price?' €':'%',band:price,ymax:price?null:100}):'<p class="note">These horizons were computed at different times. Inspect their timestamps in the table before comparing them.</p>';
    const headers=price?[current?'Future horizon':'Stored day','Median ask',current?'90% predictive interval':'Stored interval','Support','Computed']:[current?'Future horizon':'Stored horizon','Sale probability','Support','Computed'];
    const rows=forecasts.map(row=>modelCells(num(row.horizon_days)+' days',(price?[eur(row.predicted_price_eur),row.price_ci_lo==null||row.price_ci_hi==null?'—':eur(row.price_ci_lo)+'–'+eur(row.price_ci_hi)]:[modelPercent(row.p_sale)]).concat([modelForecastSupport(row,row.is_accepted&&modelAcceptedHorizon(data,modelFit(data,family),row.horizon_days,family)),date(row.computed_at)])));
    setHTML('model-detail-content','<div class="metric-list">'+metric('Current stored ask',eur(detail.listing.listed_price_eur))+metric('Current state',esc(detail.listing.status_norm))+metric('Last observed',date(detail.listing.last_seen_at))+'</div>'+(extrapolated?'<div class="message info">Some horizons lack holdout support for this category, condition or listing age. Predictions at these horizons are extrapolations.</div>':'')+'<h3>'+(current?'Forecast from its computation time':'Stored model outputs')+'</h3>'+chart+modelTable(headers,rows)+'<p class="note" style="margin-top:15px">'+esc(current?MODEL_INFO[family].note:'Older forecasts retain their original interval and time-origin definitions. Train and evaluate the current model to refresh these outputs.')+'</p><h3>Observed asking-price history</h3>'+researchChart(detail.price_history.map(row=>({label:modelDate(row.valid_from),x:Date.parse(row.valid_from),ask:row.listed_price_eur==null?null:Number(row.listed_price_eur)})),[{key:'ask',label:'Observed ask'}],{label:'Observed asking-price history',unit:' €'}));
    scheduleTableScroll();
  }catch(error){if(sequence===predictDetailSequence)setHTML('model-detail-content','<div class="message error">'+esc(error.message)+'</div>');}
}
function exportModel(){
  if(!predictData||$('model-export').disabled)return;
  const family=predictScope.family,rows=['sale_probability','price_trajectory'].includes(family)?predictData.listing_forecasts.items:family==='seller_archetypes'?predictData.seller_archetypes.archetypes:predictData[family].items;
  if(!rows.length)return;
  const headers=[...new Set(rows.flatMap(row=>Object.keys(row)))];
  downloadCSV('tori-'+family+'-results.csv',headers,rows.map(row=>headers.map(key=>row[key]&&typeof row[key]==='object'?JSON.stringify(row[key]):row[key])));
}
function bindPredict(){
  for(const [id,key] of [['model-category','category'],['model-region','region'],['model-horizon','horizon'],['model-version','version']])$(id).onchange=()=>{closeModelDetail();pendingModelListing=null;predictScope[key]=key==='horizon'?Number($(id).value):$(id).value;predictScope.page=1;predictURL();loadPredict();};
  $('model-series').onchange=()=>{if(predictData)drawModelHistory(predictData);};
  $('model-prev').onclick=()=>{predictScope.page=Math.max(1,predictScope.page-1);loadPredict();};$('model-next').onclick=()=>{predictScope.page++;loadPredict();};$('model-export').onclick=exportModel;
  $('model-search').oninput=()=>{clearTimeout(predictSearchTimer);predictSearchTimer=setTimeout(()=>{closeModelDetail();predictScope.search=$('model-search').value.trim();predictScope.page=1;predictURL();loadPredict();},300);};
  $('close-model-detail').onclick=closeModelDetail;
  $('model-detail-dialog').addEventListener('close',()=>{predictDetailSequence++;const url=new URL(location.href);url.searchParams.delete('model_listing');history.replaceState(null,'',url);});
  document.querySelector('.model-tabs').addEventListener('keydown',event=>{
    const tabs=[...document.querySelectorAll('[data-model-family]')],current=tabs.indexOf(document.activeElement);let next=current;
    if(event.key==='ArrowRight')next=(current+1)%tabs.length;else if(event.key==='ArrowLeft')next=(current-1+tabs.length)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;
    event.preventDefault();tabs[next].focus();selectModel(tabs[next].dataset.modelFamily);
  });
  document.addEventListener('click',event=>{
    const tab=event.target.closest('[data-model-family]');if(tab){selectModel(tab.dataset.modelFamily);return;}
    const run=event.target.closest('[data-model-run]');if(run&&!run.disabled){runModel(run.dataset.modelRun);return;}
    const cancel=event.target.closest('[data-model-cancel]');if(cancel){cancel.disabled=true;cancelModel(cancel.dataset.modelCancel);return;}
    const listing=event.target.closest('[data-model-listing]');if(listing){openModelListing(Number(listing.dataset.modelListing));return;}
    const forecast=event.target.closest('[data-open-model]');if(forecast){if($('listing-dialog').open)$('listing-dialog').close();predictScope.category=forecast.dataset.modelCategory||'';predictScope.region='';predictScope.search='';selectModel(forecast.dataset.openModel);pendingModelListing=Number(forecast.dataset.openListing)||null;predictURL();showView('predict');}
  });
}
const REVIEW_LABELS={collecting:'Collecting evidence',needs_attention:'Needs attention',ready_for_review:'Targets met'};
const OPS_BACKUP_TONE={ok:'green',stale:'amber',invalid:'red',missing:'amber',unsupported:''};
const OPS_METRICS=[['toriresearch_listings_rows','Stored listing rows'],['toriresearch_collection_attention','Collection needs attention'],['toriresearch_backup_verified_recent','Backup verified recently'],['toriresearch_compliance_blockers','Compliance blockers'],['toriresearch_followup_due','Follow-up checks due'],['toriresearch_grid_coverage_ratio','Grid coverage'],['toriresearch_http_requests_total','HTTP requests'],['toriresearch_http_errors_total','HTTP errors'],['toriresearch_rows_ingested_total','Rows ingested'],['toriresearch_events_emitted_total','Streamed updates']];
function gateValue(value){
  if(value==null)return '—';
  if(typeof value==='number')return num(value,2);
  if(typeof value==='boolean')return value?'Yes':'No';
  if(Array.isArray(value))return value.map(gateValue).join(' · ');
  if(typeof value==='object')return Object.entries(value).map(([key,inner])=>key.replaceAll('_',' ')+': '+gateValue(inner)).join(' · ');
  return String(value);
}
let operationsSequence=0,operationsPoll=null;
let importsSequence=0,importsData=null;
async function loadOperations(){
  const sequence=++operationsSequence;
  try{
    const data=await call('/api/operations');if(sequence!==operationsSequence)return;
    drawOperations(data);
  }catch(error){if(sequence===operationsSequence)message('operations-message',error.message,'error');}
}
function drawOperations(data){
  const storage=data.storage||{},backup=data.backup||{},review=data.review||{},job=data.job||{},days=storage.days||[],latest=days.at(-1);
  const backupTone=OPS_BACKUP_TONE[backup.status]??'';
  $('ops-backend').textContent=String(storage.backend||'—').replace(/^./,letter=>letter.toUpperCase());
  $('ops-backend-note').textContent=storage.backend==='postgres'?'PostgreSQL · native backups configured separately':'SQLite file on this machine';
  $('ops-backup').textContent=String(backup.status||'—').replace(/^./,letter=>letter.toUpperCase());
  $('ops-backup').className='stat-value';
  $('ops-backup-note').textContent=backup.age_hours==null?(backup.note||'No verified snapshot recorded yet'):num(backup.age_hours,1)+' h old · restore rehearsal '+(backup.restore_verified?'verified':'not verified');
  $('ops-size').textContent=latest&&latest.database_bytes!=null?sizeLabel(latest.database_bytes):backup.database_bytes!=null?sizeLabel(backup.database_bytes):'—';
  $('ops-size-note').textContent=latest?'WAL '+sizeLabel(latest.wal_bytes||0)+' · sampled '+date(latest.day):'Not sampled yet';
  const locks=data.locks||[];
  $('ops-locks').textContent=num(locks.length);
  $('ops-locks-note').textContent=locks.length?locks.join(', '):'Nothing running against the database';
  $('ops-backup-badge').textContent=backup.status||'Unknown';$('ops-backup-badge').className='badge '+backupTone;
  const timings=backup.timings||{};
  setHTML('ops-backup-detail',metric('Status',badge(backup.status||'Unknown',backupTone))+metric('Created',date(backup.created_at))+metric('Age',backup.age_hours==null?'—':num(backup.age_hours,1)+' h')+metric('Restore rehearsal',backup.restore_verified?badge('Verified','green'):badge('Not verified','amber'))+metric('Database size',sizeLabel(backup.database_bytes))+metric('Copy / restore / total',timings.copy_seconds==null?'—':num(timings.copy_seconds,2)+' s / '+num(timings.restore_seconds,2)+' s / '+num(timings.total_seconds,2)+' s')+metric('Off-host copy',backup.off_host_copy?badge('Yes','green'):'No · same machine')+(backup.note?metric('Note',esc(backup.note)):''));
  const jobLabel=job.action==='verify'?'Restore rehearsal':job.action==='backup'?'Backup':'Operation';
  $('ops-job').textContent=job.status==='running'?jobLabel+' running · started '+date(job.started_at)
    :job.status==='done'?jobLabel+' finished '+date(job.finished_at)+(job.result&&job.result.status?' · '+String(job.result.status):'')
    :job.status==='failed'?jobLabel+' failed: '+(job.error||'unknown error')
    :'No operation running.';
  $('ops-job').className='note'+(job.status==='failed'?' message error':'');
  $('ops-run-backup').disabled=job.status==='running';$('ops-run-verify').disabled=job.status==='running';
  if(job.status==='running'&&!operationsPoll)operationsPoll=setTimeout(()=>{operationsPoll=null;loadOperations();},2500);
  setHTML('ops-storage',metric('Backend',esc(String(storage.backend||'—')))+metric('Latest read / lock probe',latest?num(latest.read_ms,1)+' ms / '+num(latest.lock_ms,1)+' ms':'—')+metric('Contention in latest sample',latest?(latest.busy_checks?'Recorded':'None'):'—')+metric('Days sampled',num(days.length))+metric('Slowest read / lock',days.length?num(Math.max(...days.map(row=>row.read_ms_max||0)),1)+' ms / '+num(Math.max(...days.map(row=>row.lock_ms_max||0)),1)+' ms':'—')+metric('Busy samples',num(days.reduce((total,row)=>total+(row.busy_checks||0),0))));
  $('ops-storage-note').textContent=storage.note||'';
  if(!review.available){
    $('ops-review-badge').textContent='Not available';$('ops-review-badge').className='badge';
    setHTML('ops-review',empty('No seven-day review yet',review.reason||'Run the scheduled coordinator to produce it.','refresh'));
  }else{
    const gates=review.gates||[],failed=gates.filter(gate=>gate.status==='fail');
    $('ops-review-badge').textContent=(REVIEW_LABELS[review.status]||String(review.status||'Unknown'))+' · '+num(review.complete_days)+' / '+num(review.required_days||7)+' days';
    $('ops-review-badge').className='badge '+(review.status==='ready_for_review'?'green':review.status==='needs_attention'?'red':'amber');
    setHTML('ops-review',(gates.length?'<div class="table-wrap"><table><thead><tr><th>Gate</th><th>Status</th><th>Value</th><th>Target</th></tr></thead><tbody>'+
      gates.map(gate=>'<tr><td>'+esc(gate.name||'')+'</td><td>'+badge(gate.status,gate.status==='pass'?'green':gate.status==='fail'?'red':'amber')+'</td><td>'+esc(gateValue(gate.value))+'</td><td class="muted">'+esc(gateValue(gate.target))+'</td></tr>').join('')+'</tbody></table></div>':'')+
      '<p class="note" style="margin-top:14px">'+(review.fresh?'Written '+date(review.generated_at)+'. ':'Written '+date(review.generated_at)+' · '+num(review.age_hours,1)+' h old'+(review.context_matches===false?' and under different collection settings':'')+'. ')+(failed.length?esc(failed.map(gate=>gate.name).join(', '))+' need attention. ':'')+(review.eligible_for_expansion_review?'Eligible for an expansion review. ':'Expansion review not yet eligible. ')+'</p>'+(review.note?'<p class="note" style="margin-top:8px">'+esc(review.note)+'</p>':''));
  }
  const metrics=data.metrics||{};
  const curated=OPS_METRICS.map(([name,label])=>{const value=metricValue(metrics,name);return value==null?null:metric(label,metricDisplay(name,value));}).filter(Boolean).join('');
  setHTML('ops-metrics',curated||'<p class="note">No metrics recorded yet.</p>');
  const rows=[];
  for(const [name,series] of Object.entries({...metrics.counters,...metrics.gauges})){
    if(rows.length>=80)break;
    rows.push(metric(name,esc(Object.entries(series).map(([label,value])=>label==='total'?String(value):label+' = '+value).join(' · '))));
  }
  setHTML('ops-metrics-all',rows.join('')||'<p class="note">Nothing recorded yet.</p>');
  $('ops-metrics-badge').textContent=num(Object.keys(metrics.counters||{}).length+Object.keys(metrics.gauges||{}).length)+' series';
}
async function startOperation(action){
  const button=$(action==='backup'?'ops-run-backup':'ops-run-verify');
  button.disabled=true;
  try{
    await call('/api/operations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    message('operations-message',action==='backup'?'Copying this database and rehearsing the restore.':'Rehearsing a restore into a temporary database.','info');
    if(!operationsPoll)operationsPoll=setTimeout(()=>{operationsPoll=null;loadOperations();},600);
  }catch(error){button.disabled=false;message('operations-message',error.message,'error');toast(error.message,true);}
}
function bindOperations(){
  $('ops-run-backup').onclick=()=>startOperation('backup');
  $('ops-run-verify').onclick=()=>startOperation('verify');
}
function importsQuery(){
  const keyword=($('sold-keyword').value||'').trim(),params=new URLSearchParams();
  if(keyword)params.set('keyword',keyword);
  params.set('min_obs',String(Number($('sold-minobs').value)||3));
  return params;
}
async function loadImports(){
  const sequence=++importsSequence;
  try{
    const data=await call('/api/soldextract?'+importsQuery());if(sequence!==importsSequence)return;
    importsData=data;drawImports(data);
  }catch(error){if(sequence===importsSequence)message('imports-message',error.message,'error');}
}
function drawSoldRatio(ratio){
  const status=ratio.status||'NO SOLD DATA',tone=status==='OK'?'green':status==='INSUFFICIENT DATA'?'amber':'';
  const range=metric('Observed sold range',(ratio.sold_price_min_eur==null&&ratio.sold_price_max_eur==null)?'—':eur(ratio.sold_price_min_eur)+' – '+eur(ratio.sold_price_max_eur));
  setHTML('sold-ratio',metric('Status',badge(status,tone))+
    (ratio.sold_observations==null?'':metric('Captures',num(ratio.sold_observations)))+
    (ratio.underlying_transactions_n_sold==null?'':metric('Transactions represented',num(ratio.underlying_transactions_n_sold)))+
    (ratio.median_avg_sold_price_eur==null?'':metric('Median average sold price',eur(ratio.median_avg_sold_price_eur)))+
    (ratio.median_avg_sold_price_eur==null?'':range)+
    (ratio.median_sell_through_rate==null?'':metric('Median sell-through',percent(ratio.median_sell_through_rate*100)))+
    metric('Realization ratio',ratio.realization_ratio==null?badge(ratio.ratio_status||'Not available','amber'):num(ratio.realization_ratio,3)));
  $('sold-ratio-note').textContent=ratio.note||ratio.confidence||'A ratio needs a matched asking-price cohort and time window. It is not derived from sold averages alone.';
}
function drawImports(data){
  const summary=data.summary||{},status=summary.status||'EMPTY',tone=status==='OK'?'green':'amber';
  $('imports-captures').textContent=num(summary.captures);
  $('imports-keywords').textContent=num(summary.keywords);
  $('imports-transactions').textContent=num(summary.transactions_represented);
  $('imports-sellthrough').textContent=num(summary.keywords_with_sell_through);
  $('imports-captures-note').textContent=status==='OK'?'Rows accepted from your capture files':'No capture file imported yet';
  $('imports-keywords-note').textContent=status==='OK'?'Bounded keyword list you captured':'Download the template to start';
  $('imports-transactions-note').textContent=status==='OK'?'eBay reports these as sold':'Nothing stored yet';
  $('imports-sellthrough-note').textContent=status==='OK'?'Keywords with a recorded rate':'No sell-through recorded';
  $('sold-source-badge').textContent=data.source||'ebay.product_research.ui';
  $('sold-summary-badge').textContent=status;$('sold-summary-badge').className='badge '+tone;
  setHTML('sold-summary',metric('Captures',num(summary.captures))+metric('Keywords',num(summary.keywords))+metric('Transactions represented',num(summary.transactions_represented))+metric('Keywords with sell-through',num(summary.keywords_with_sell_through))+metric('Status',badge(status,tone))+(data.keyword?metric('Inspected keyword',esc(data.keyword)):''));
  drawSoldRatio(data.ratio||{});
  setHTML('sold-recent-receipts',recentCaptureReceipts(data.recent_captures));
}
async function importSoldCapture(file){
  const button=$('sold-import');button.disabled=true;
  try{
    const csv=await file.text();
    const result=await call('/api/soldextract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'import',csv,captured_by:'dashboard'})});
    const text=num(result.imported)+' row'+(result.imported===1?'':'s')+' imported'+(result.duplicates?' · '+num(result.duplicates)+' duplicate'+(result.duplicates===1?'':'s'):'')+'.';
    $('sold-file').value='';$('sold-import').disabled=true;
    $('sold-import-result').textContent='Last import: '+text;
    message('imports-message',text,'success');toast(text);
    await loadImports();
  }catch(error){
    message('imports-message',error.message,'error');toast(error.message,true);
    button.disabled=!(($('sold-file').files||[]).length);
  }
}
function bindImports(){
  $('sold-template').onclick=async()=>{
    try{
      const data=importsData||await call('/api/soldextract'),text=data.template||'';
      if(!text)throw new Error('The server returned no template.');
      const url=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'})),link=document.createElement('a');
      link.href=url;link.download='sold_capture_template.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      message('imports-message','Template downloaded. Fill it out, then import it here.','success');
    }catch(error){message('imports-message',error.message,'error');}
  };
  $('sold-file').onchange=()=>{
    const file=($('sold-file').files||[])[0];
    $('sold-import').disabled=!file;
    $('sold-import-result').textContent=file?'Selected '+file.name+' · '+num(file.size)+' bytes. One file per import.':'The template and the stored captures are read from this machine.';
  };
  $('sold-import').onclick=()=>{const file=($('sold-file').files||[])[0];if(file)importSoldCapture(file);};
  $('sold-ratio-run').onclick=()=>loadImports();
  $('sold-keyword').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();loadImports();}};
}
function drawSavedSegments(){
  setHTML('saved-segments',savedSegments.length?savedSegments.map((row,index)=>'<span class="saved-segment"><button type="button" data-saved="'+index+'">'+esc(row.name)+'</button><button type="button" data-remove-saved="'+index+'" aria-label="Remove '+esc(row.name)+'">×</button></span>').join(''):'<span class="note">Save a segment to revisit it.</span>');
  drawComparisonOptions();
}
async function researchAction(action,params){return call('/api/research',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});}
async function loadResearch(){
  const sequence=++researchSequence;$('journal-content').setAttribute('aria-busy','true');$('export-journal').disabled=true;
  try{
    let result=await call('/api/research?'+new URLSearchParams({decision:$('journal-filter').value,page:journalPage}));if(sequence!==researchSequence)return;
    if(!researchReady){
      // Move existing browser cohorts once; existing database names take precedence.
      const legacy=savedSegments.filter(row=>!row.segment_pk&&!result.segments.some(saved=>saved.name===row.name));
      let migrationComplete=true;
      for(const row of legacy){try{const imported=await researchAction('save_segment',{name:row.name,filters:Object.fromEntries(Object.entries(row.filters).filter(([key,value])=>FILTER_KEYS.includes(key)&&typeof value==='string'))});result.segments=imported.segments;}catch(error){migrationComplete=false;toast('A browser segment could not be migrated: '+error.message,true);}}
      if(migrationComplete)try{localStorage.removeItem('tori.analyst.segments.v1');}catch{}
    }
    if(sequence!==researchSequence)return;
    savedSegments=result.segments;researchReady=true;journalData=result.journal;journalPage=journalData.page;journalPages=journalData.pages;
    drawSavedSegments();drawJournal();$('save-segment').disabled=false;$('export-journal').disabled=!journalData.items.length;$('journal-error').hidden=true;
    // The note named a database this deployment may not have. On the hosted
    // workspace the segments shown are the ones the last export carried, and
    // nothing can be written, so saying where they "live" and implying they can be
    // was two claims and only the first was true.
    $('research-storage-note').textContent=canWrite(capabilities,'/api/research')
      ?'Saved segments and decision history live in your local research database.'
      :'Saved segments and decision history are replayed from the last research export. They cannot be changed from here.';
  }catch(error){if(sequence!==researchSequence)return;message('journal-error',error.message,'error');$('research-storage-note').textContent='Cannot load saved research: '+error.message;}
  finally{if(sequence===researchSequence)$('journal-content').setAttribute('aria-busy','false');}
}
function drawComparisonOptions(){
  comparisonChoices=new Map([['current',{label:'Current segment',filters:{...FILTERS}}]]);
  for(const row of savedSegments)if(row.segment_pk)comparisonChoices.set('saved:'+row.segment_pk,{label:row.name,filters:row.filters});
  if(lastAnalysis&&lastAnalysis.dimension!=='product_type')for(const row of lastAnalysis.matrix){const filters={...FILTERS,[lastAnalysis.dimension]:row.label};if(lastAnalysis.dimension==='category_root_norm')delete filters.category_norm;comparisonChoices.set('group:'+lastAnalysis.dimension+':'+row.label,{label:LABELS[row.label]||row.label,filters});}
  const options=[...comparisonChoices].map(([key,row])=>'<option value="'+esc(key)+'">'+esc(row.label)+'</option>').join('');
  for(const id of ['compare-left','compare-right']){const value=$(id).value;setHTML(id,options);$(id).value=comparisonChoices.has(value)?value:'current';}
  if($('compare-right').value==='current'&&comparisonChoices.size>1)$('compare-right').value=[...comparisonChoices.keys()][1];
}
function comparisonCard(side,label){
  const p=side.prices,l=side.liquidity,ci=p.median_ci95;
  const rate=evidence=>l.sell_through_by_evidence[evidence]?.estimates?.[l.horizon_days_requested+'d'];
  const context=Object.entries(side.filters).map(([,value])=>LABELS[value]||value).join(' · ');
  return '<section class="comparison-card"><h3>'+esc(label)+'</h3><p class="note">'+esc(context)+'</p><div class="comparison-price">'+eur(p.median_eur)+'</div><p class="note">Median ask '+(ci?'· 95% CI '+eur(ci.lo)+'–'+eur(ci.hi):'· needs '+num(configuredMin())+' fresh prices')+'</p><div class="metric-list">'+metric('Fresh available / priced',num(p.n_available)+' / '+num(p.n_priced))+metric('Price completeness',percent(p.price_coverage==null?null:p.price_coverage*100))+metric('P25–P75 asking range',p.status==='OK'?eur(p.p25_eur)+'–'+eur(p.p75_eur):'—')+metric('Declared / verified markers',num(l.sold_inferred_seller_declared)+' / '+num(l.sold_confirmed))+metric('Declared sale incidence',incidence(rate('sold_inferred')))+metric('Verified sale incidence',incidence(rate('sold_confirmed')))+metric('Follow-up coverage',percent(l.followup_coverage==null?null:l.followup_coverage*100))+'</div><div class="condition-comparison"><h3>Prices within condition</h3><div class="metric-list">'+side.conditions.map(row=>metric((LABELS[row.label]||row.label)+' · '+num(row.n_priced)+' priced',row.status==='OK'?eur(row.median_eur):'Insufficient data')).join('')+'</div></div></section>';
}
function configuredMin(){return lastAnalysis?.min_n||lastSnapshot?.panels.national.min_n_threshold||30;}
async function compareSegments(){
  const left=comparisonChoices.get($('compare-left').value),right=comparisonChoices.get($('compare-right').value);if(!left||!right)return;
  const sequence=++comparisonSequence;$('compare-button').disabled=true;
  try{
    const result=await call('/api/compare?'+new URLSearchParams({left:JSON.stringify(left.filters),right:JSON.stringify(right.filters),horizon:$('analysis-horizon').value}));if(sequence!==comparisonSequence)return;
    setHTML('comparison-result','<div class="comparison-grid">'+comparisonCard(result.left,'A · '+left.label)+comparisonCard(result.right,'B · '+right.label)+'</div><div class="comparison-footer"><p>'+badge(result.median_difference_eur==null?'Difference unsupported':'B − A median ask: '+eur(result.median_difference_eur),'purple')+' '+badge(num(result.overlap_listings)+' listings overlap',result.overlap_listings?'amber':'')+'</p><p class="note" style="margin-top:9px">'+esc(result.note)+' Sale horizon: '+num(result.horizon)+' days. Generated '+date(result.generated_at)+'.</p></div>');
    $('comparison-error').hidden=true;
  }catch(error){if(sequence===comparisonSequence)message('comparison-error',error.message,'error');}
  finally{if(sequence===comparisonSequence)$('compare-button').disabled=false;}
}
const DECISION_LABELS={watch:'Watch',inspect:'Inspect',reject:'Reject',pursue:'Pursue'};
const EVALUATION_LABELS={not_acquired:'No purchase recorded',acquired:'Acquired · awaiting sale',sold_complete:'Sold · ledger costs recorded',sold_costs_unknown:'Sold · costs unknown',pre_existing_trade:'Trade predates decision',timing_unknown:'Ledger timing needs review',other_outcome:'Other recorded ledger outcome',written_off_complete:'Written off · ledger loss recorded'};
function journalComparable(row){return row.profit_comparison_status==='available'&&row.explicit_acquisition_link===true&&Number.isFinite(row.comparison_profit_eur)&&Number.isFinite(row.profit_vs_assumption_eur);}
function journalComparison(row){
  const available=journalComparable(row);
  return '<div class="range-text">'+badge(available?'Reviewed cash − assumed base: '+eur(row.profit_vs_assumption_eur):'Profit comparison withheld',available?'green':'amber')+'</div><div class="observation-meta">'+esc(row.profit_comparison_note||'Acquisition attribution and whole-unit cost review are required.')+'</div>';
}
function journalExportRow(row){
  const available=journalComparable(row);
  return [row.decision_pk,row.recorded_at,row.platform,row.source_listing_id,row.decision,row.notes,JSON.stringify(row.checks),row.scenario?.version,JSON.stringify(row.scenario?.inputs||null),row.scenario?.results?.base_profit_eur,row.scenario?.results?.downside_profit_eur,row.evidence.ask_eur,row.evidence.peer_prices.n_priced,row.evaluation,row.actual_profit_eur,available?row.profit_vs_assumption_eur:null,row.explicit_acquisition_link===true,row.linked_decision_pk,row.acquisition_attribution?.attempt_id,available?row.comparison_profit_eur:null,row.profit_comparison_status||'unverified',row.profit_comparison_note||'Acquisition attribution and whole-unit cost review are required.',row.profit_comparison_basis];
}
function drawJournal(){
  if(!journalData)return;
  const j=journalData,c=j.counts;
  setHTML('journal-stats',[[num(j.n_unique_listings),'Listings reviewed',num(j.n_records)+' decision entries'],[num((c.watch||0)+(c.inspect||0)),'Watch / inspect','Research in progress'],[num(c.reject||0),'Rejections','Reasons preserved'],[num(c.pursue||0),'Pursue decisions','Purchases recorded separately']].map(([value,label,note])=>'<section class="card stat"><div class="stat-top">'+esc(label)+'</div><div class="stat-value">'+value+'</div><div class="stat-note">'+esc(note)+'</div></section>').join(''));
  setHTML('journal-rows',j.items.length?j.items.map(row=>{const results=row.scenario?.results,checks=Object.values(row.checks).filter(Boolean).length;return '<tr><td><button class="row-link journal-title" data-decision-detail="'+row.decision_pk+'" type="button">'+esc(row.title||'Listing #'+row.source_listing_id)+'</button><div class="observation-meta">'+esc(LABELS[row.platform]||row.platform)+' · #'+esc(row.source_listing_id)+'</div></td><td class="muted">'+date(row.recorded_at)+'</td><td>'+badge(DECISION_LABELS[row.decision],row.decision==='reject'?'red':row.decision==='pursue'?'green':'purple')+'</td><td>'+badge(checks+' / 4 checked',checks===4?'green':'amber')+'</td><td class="num">'+(results?eur(results.base_profit_eur)+' / '+eur(results.downside_profit_eur):'<span class="muted">No scenario</span>')+'</td><td>'+badge(EVALUATION_LABELS[row.evaluation],row.evaluation==='sold_complete'?'green':row.evaluation==='sold_costs_unknown'?'amber':'')+(row.actual_profit_eur!=null?'<div class="range-text">Related ledger margin '+eur(row.actual_profit_eur)+'</div>':'')+journalComparison(row)+'</td><td><button class="btn quiet small" data-decision-detail="'+row.decision_pk+'" type="button">Details</button> '+decisionOwnedButton(row)+'</td></tr>';}).join(''):'<tr><td colspan="7">'+empty('Capture your first research decision','Inspect a stored listing or model its costs, then save your reasoning here.','file')+'</td></tr>');
  $('journal-count').textContent=num(j.total)+' matching entries · '+num(j.n_unique_listings)+' unique listings overall';$('journal-page-label').textContent='Page '+num(j.page)+' / '+num(j.pages);$('journal-prev').disabled=j.page<=1;$('journal-next').disabled=j.page>=j.pages;
}
function calculatorAssumptions(){
  const fields=[...$('calculator-form').querySelectorAll('input[name]')];
  if(fields.some(input=>input.value===''||!input.checkValidity()))return null;
  return {...Object.fromEntries(fields.map(input=>[input.name,Number(input.value)])),haircut:Number($('scenario-discount').value)};
}
function decisionScenario(){
  // An explicit handover from the research desk wins over the analyst card's
  // inputs: the decision must record the assumptions the operator typed where
  // they were deciding, not whatever another card happens to hold.
  const assumptions=decisionAssumptions===undefined?calculatorAssumptions():decisionAssumptions,selected=$('decision-include-scenario');
  selected.disabled=!assumptions;
  if(!assumptions){selected.checked=false;$('decision-scenario').textContent='Calculator inputs are incomplete. Save inspection notes now, or complete the calculator before attaching a scenario.';return;}
  const result=calculateScenario(assumptions,assumptions.haircut);
  if(!result){selected.checked=false;$('decision-scenario').textContent='These assumptions cannot be evaluated. Use finite nonnegative values and a fee rate below 100%.';return;}
  $('decision-scenario').textContent='Current assumptions: sale '+eur(assumptions.resale)+' · buy '+eur(assumptions.buy)+' · base profit '+eur(result.base)+' · downside '+eur(result.downside)+'. Your supplied values will be frozen when saved.';
}
function openDecision(row=null,attach=false,assumptions=undefined,scenarioContext=null){
  decisionAssumptions=assumptions;decisionScenarioContext=scenarioContext;
  if($('listing-dialog').open)$('listing-dialog').close();
  $('decision-form').reset();decisionListing=null;decisionToken=crypto.randomUUID();$('decision-error').hidden=true;
  $('decision-platform').value=row?.platform||FILTERS.platform||'tori';$('decision-listing-id').value=row?.listing_id||'';
  setHTML('decision-context','<p>Load a stored listing to capture its current asking-price evidence.</p>');
  decisionScenario();$('decision-include-scenario').checked=attach&&!$('decision-include-scenario').disabled;
  $('decision-dialog').showModal();if(row)loadDecisionListing();else $('decision-listing-id').focus();
}
async function loadDecisionListing(){
  const platform=$('decision-platform').value,id=$('decision-listing-id').value.trim();if(!id){message('decision-error','Enter a stored source listing ID.','error');return;}
  decisionListing=null;$('load-decision-listing').disabled=true;
  try{
    const result=await call('/api/research/listing?'+researchReadParams({platform,listing_id:id}));
    if(platform!==$('decision-platform').value||id!==$('decision-listing-id').value.trim())return;
    decisionListing={...result.listing,comparison_evidence:result.evidence};const e=result.evidence,p=e.peer_prices;
    setHTML('decision-context','<strong>'+esc(decisionListing.title||id)+'</strong><p>'+esc(e.category_norm||'Category unknown')+' · '+esc(LABELS[e.condition_norm]||'Condition unknown')+'</p><p>Observed ask <strong>'+eur(e.ask_eur)+'</strong> · '+(e.fresh_available?'fresh and available':'availability or freshness needs checking')+'</p><p>'+num(p.n_priced)+' other fresh priced peers · '+(p.status==='OK'?'median ask '+eur(p.median_eur)+' · P25–P75 '+eur(p.p25_eur)+'–'+eur(p.p75_eur):'insufficient data for a peer estimate')+'</p><p>Peer asking prices are context; your assumed sale proceeds are recorded separately.</p>');$('decision-error').hidden=true;
  }catch(error){message('decision-error',error.message,'error');}
  finally{$('load-decision-listing').disabled=false;}
}
async function saveDecision(event){
  event.preventDefault();const form=$('decision-form');
  if(!decisionListing||decisionListing.platform!==$('decision-platform').value||decisionListing.listing_id!==$('decision-listing-id').value.trim()){message('decision-error','Load the stored evidence for this listing before saving.','error');return;}
  const assumptions=$('decision-include-scenario').checked?(decisionAssumptions===undefined?calculatorAssumptions():decisionAssumptions):null;
  if($('decision-include-scenario').checked&&!assumptions){message('decision-error','Complete every calculator assumption before attaching it.','error');return;}
  const params={client_token:decisionToken,platform:decisionListing.platform,source_listing_id:decisionListing.listing_id,decision:form.elements.decision.value,notes:form.elements.notes.value,checks:Object.fromEntries(['model','condition','completeness','delivery'].map(key=>[key,form.elements[key].checked])),assumptions};
  if(assumptions&&decisionScenarioContext)params.scenario_context=decisionScenarioContext;
  const evidence=decisionListing.comparison_evidence;
  if(evidence){params.filters=evidence.effective_filters;params.reference_at=evidence.reference_at;params.benchmark_revision=evidence.benchmark_revision;}
  $('save-decision').disabled=true;
  try{await researchAction('record_decision',params);$('decision-dialog').close();toast('Research decision saved with its original evidence.');journalPage=1;await loadResearch();showView('journal');}
  catch(error){message('decision-error',error.message,'error');}
  finally{$('save-decision').disabled=false;}
}
function journalDetail(pk){
  const row=journalData?.items.find(item=>item.decision_pk===Number(pk));if(!row)return;
  showDecisionDetail(row);
}
function showDecisionDetail(row){
  $('journal-detail-title').textContent=row.title||'Listing #'+row.source_listing_id;
  const e=row.evidence,p=e.peer_prices,s=row.scenario;
  setHTML('journal-detail',scenarioProvenance(s)+badge(DECISION_LABELS[row.decision],'purple')+' <span class="note">'+date(row.recorded_at)+' · #'+esc(row.source_listing_id)+'</span><h3>Your reasoning</h3><p>'+esc(row.notes||'No notes recorded.')+'</p><h3>Inspection at decision time</h3><div class="metric-list">'+Object.entries(row.checks).map(([key,value])=>metric(key[0].toUpperCase()+key.slice(1),badge(value?'Checked':'Unchecked',value?'green':'amber'))).join('')+'</div><h3>Asking-price evidence at decision time</h3><div class="metric-list">'+metric('Observed ask',eur(e.ask_eur))+metric('Other fresh priced peers',num(p.n_priced))+metric('Peer median asking price',eur(p.median_eur))+metric('Peer median 95% CI',p.median_ci95?eur(p.median_ci95.lo)+'–'+eur(p.median_ci95.hi):'Insufficient data')+'</div><h3>Saved assumptions</h3>'+(s?'<div class="metric-list">'+Object.entries(s.inputs||{}).map(([key,value])=>metric(key, key==='fee'||key==='haircut'?percent(value):key==='days'?num(value)+' days':eur(value))).join('')+metric('Base / downside profit',eur(s.results?.base_profit_eur)+' / '+eur(s.results?.downside_profit_eur))+'</div><p class="note">'+esc(s.version)+' · user-supplied assumptions</p>':'<p class="note">No scenario attached.</p>')+'<h3>Actual ledger result</h3><div class="metric-list">'+metric('Evaluation',esc(EVALUATION_LABELS[row.evaluation]))+metric('Related ledger margin',eur(row.actual_profit_eur))+metric('Reviewed whole-unit cash contribution',eur(journalComparable(row)?row.comparison_profit_eur:null))+metric('Reviewed cash − assumed base profit',eur(journalComparable(row)?row.profit_vs_assumption_eur:null))+'</div>'+journalComparison(row)+'<p class="note" style="margin-top:15px">An unbought item has no observed realised-profit result. This record does not establish performance on rejected opportunities.</p>');
  setHTML('journal-detail',$('journal-detail').innerHTML+decisionAttempts(row)+'<h3>Continue the owned workflow</h3><div class="unit-actions"><button class="btn small" type="button" data-item-decision="'+row.decision_pk+'">Item workspace</button>'+decisionOwnedButton(row)+outcomeRemedies(row)+'<button class="btn small" type="button" data-decision-attempt="'+row.decision_pk+'">Record declined / failed attempt</button>'+(row.current_order_id?'<button class="btn small" type="button" data-ledger-order="'+esc(row.current_order_id)+'">Review SKU order</button>':'')+'</div><p class="note">Record acquired stock only after purchase. Research checks do not replace a physical inspection.</p>');
  $('journal-detail-dialog').showModal();
}
function exportJournal(){
  if(!journalData?.items.length)return;
  const header=['decision_pk','recorded_at','platform','source_listing_id','decision','notes','inspection_checks','scenario_version','scenario_inputs','assumed_base_profit_eur','assumed_downside_profit_eur','observed_ask_at_decision_eur','peer_n','evaluation','actual_profit_eur','actual_minus_assumed_eur','explicit_acquisition_link','linked_decision_pk','acquisition_attempt_id','comparison_profit_eur','profit_comparison_status','profit_comparison_note','profit_comparison_basis'];
  const rows=journalData.items.map(journalExportRow);
  downloadCSV('tori-decisions-page-'+journalPage+'.csv',header,rows);
}
function analysisParams(){return new URLSearchParams({...FILTERS,dimension:$('analysis-dimension').value,days:$('analysis-days').value,horizon:$('analysis-horizon').value});}
function updateAnalysisURL(){const params=new URLSearchParams(location.search);for(const [id,[key]] of Object.entries(analysisDefaults))params.set(key,$(id).value);history.replaceState(null,'',location.pathname+'?'+params.toString()+'#'+currentView);}
async function loadAnalysis(){
  const params=analysisParams().toString();
  if(analysisController&&!analysisController.signal.aborted&&analysisRequestKey===params)return;
  if(analysisController)analysisController.abort();analysisController=new AbortController();const sequence=++analysisSequence;
  analysisRequestKey=params;
  $('analysis-content').setAttribute('aria-busy','true');$('analysis-kpis').setAttribute('aria-busy','true');$('export-analysis').disabled=true;
  if(!lastAnalysis)setHTML('analysis-candidates','<p class="note" role="status">Reading current listings and comparison evidence…</p>');
  try{
    const result=await call('/api/analysis?'+params,{signal:analysisController.signal});
    if(sequence!==analysisSequence)return;
    lastAnalysis=result;drawAnalysis(result);$('analysis-error').hidden=true;$('export-analysis').disabled=false;
  }catch(error){if(error.name==='AbortError'||sequence!==analysisSequence)return;message('analysis-error',(lastAnalysis?'Showing the last successful analysis. ':'')+error.message,'error');}
  finally{if(sequence===analysisSequence){analysisController=null;$('analysis-content').setAttribute('aria-busy','false');$('analysis-kpis').setAttribute('aria-busy','false');}}
}
function matrixRows(){
  const search=$('matrix-search').value.toLocaleLowerCase(),sort=$('matrix-sort').value;
  const score=row=>sort==='sales'?row.sold_declared+row.sold_verified:sort==='price'?row.median_eur??-1:sort==='followup'?row.followup_coverage??-1:row.n_available;
  return (lastAnalysis?.matrix||[]).filter(row=>(LABELS[row.label]||row.label).toLocaleLowerCase().includes(search)).sort((a,b)=>score(b)-score(a)||a.label.localeCompare(b.label));
}
function incidence(value){return value?'<span title="95% CI '+percent(value.ci95.lo*100)+'–'+percent(value.ci95.hi*100)+'; '+num(value.n_followed_to_horizon)+' followed to horizon">'+percent(value.share*100)+'</span>':'<span class="muted" title="Needs enough sale events and follow-up at this horizon">—</span>';}
function drawMatrix(){
  if(!lastAnalysis)return;
  const rows=matrixRows(),report=lastAnalysis;
  setHTML('analysis-matrix',rows.length?rows.map(row=>{
    const drill=report.dimension==='product_type'?'':' data-drill="'+esc(row.label)+'"';
    const label=LABELS[row.label]||row.label;
    const segment=drill?'<button class="row-link" type="button"'+drill+'>'+esc(label)+'</button>':'<strong>'+esc(label.replaceAll('_',' '))+'</strong>';
    const ci=row.median_ci95;
    const followup=row.followup||{};
    return '<tr><td class="segment-cell">'+segment+'<div class="range-text">'+num(row.n_tracked)+' tracked'+(row.basis==='derived'?' · model-derived':'')+(row.n_stale?' · '+num(row.n_stale)+' stale':'')+'</div></td><td class="num">'+num(row.n_available)+' <span class="muted">/ '+num(row.n_priced)+'</span><div class="range-text">'+percent(row.price_coverage==null?null:row.price_coverage*100)+' priced</div></td><td class="num">'+(row.status==='OK'?eur(row.median_eur):'<span class="muted">Needs '+num(report.min_n)+' prices</span>')+'<div class="range-text">'+(ci?eur(ci.lo)+'–'+eur(ci.hi):'Insufficient data')+'</div></td><td class="num muted">'+(row.status==='OK'?eur(row.p25_eur)+'–'+eur(row.p75_eur):'—')+'</td><td class="num">'+num(row.sold_declared)+' <span class="muted">/ '+num(row.sold_verified)+'</span></td><td class="num">'+incidence(row.sale_rates.sold_inferred)+' / '+incidence(row.sale_rates.sold_confirmed)+'<div class="range-text">'+num(row.horizon_support)+' followed ≥ '+report.horizon+'d</div></td><td class="num">'+percent(row.followup_coverage==null?null:row.followup_coverage*100)+'<div class="range-text">'+num(followup.n_due_in_scope)+' due in focus / '+num(followup.n_due)+' due</div><div class="range-text">'+num(followup.n_known_condition)+' condition known · '+num(followup.n_classified)+' classified</div></td></tr>';
  }).join(''):'<tr><td colspan="7">'+empty(report.dimension==='product_type'?'No accepted product classifications':'No matching segments',report.dimension==='product_type'?'Classify collected items with the local model. Only current validated results at confidence ≥ 0.7 appear.':'Broaden the filters or clear your segment search.')+'</td></tr>');
  $('matrix-note').textContent='Showing '+num(rows.length)+' of '+num(report.n_groups)+' groups'+(report.n_groups>60?' · largest 60 tracked groups loaded':'')+' · '+num(report.n_ungrouped)+' items without this grouping. Price estimates need '+num(report.min_n)+' fresh prices. Sale incidence needs '+num(report.min_n)+' events per evidence class and follow-up support at '+report.horizon+' days. Hover rates for 95% CIs. Click observed segments to drill down.';
}
let analystQuality=null,analystQualityLoading=false;
const QUALITY_LABELS={passed:'Full suite passed',partial:'Partial suite passed',failed:'Checks failed',stale:'Earlier evaluation',invalid:'Invalid report',missing:'No saved report'};
function drawAnalystQuality(data){
  analystQuality=data;
  const offline=data.evaluations.find(row=>row.mode==='offline');
  $('analyst-checks-status').textContent=QUALITY_LABELS[offline?.status]||'Checks unavailable';
  $('analyst-checks-status').className='badge '+(offline?.status==='passed'?'green':['failed','invalid'].includes(offline?.status)?'red':'amber');
  $('analyst-checks-note').textContent=data.limits+' Fixtures use a minimum of '+data.fixture_policy.min_n+' prices and a '+data.fixture_policy.max_age_hours+'-hour freshness window.';
  setHTML('analyst-checks-results',data.evaluations.map(row=>{
    const title=row.mode==='offline'?'Evidence checks':'Local model checks';
    if(['missing','invalid'].includes(row.status))return '<div class="research-limitations"><strong>'+title+' · '+esc(QUALITY_LABELS[row.status])+'</strong><p>'+esc(row.error||'No evaluation has been saved for this mode.')+'</p></div>';
    const summary=num(row.n_passed)+' / '+num(row.n_cases)+' cases passed · '+date(row.generated_at);
    const caution=!row.current?'This report uses earlier fixtures, evidence rules or a different model. Re-run the current suite before treating it as current validation.':!row.complete?'This report covers a subset of the '+num(row.expected_cases)+' current cases.':'';
    const failures=(row.cases||[]).filter(item=>!item.passed).map(item=>'<div class="message error"><strong>'+esc(item.case.replaceAll('_',' '))+'</strong><p>'+esc((item.failures||[]).join(', ')||item.model_status||'Check failed')+'</p>'+(item.failed_checks||[]).map(check=>'<p>'+esc(check.check)+': expected '+esc(JSON.stringify(check.expected))+', observed '+esc(JSON.stringify(check.actual))+'</p>').join('')+'</div>').join('');
    const rows=(row.cases||[]).map(item=>modelCells(esc(item.case.replaceAll('_',' ')),[badge(item.passed?'Passed':'Failed',item.passed?'green':'red'),item.model_status?esc(item.model_status.replaceAll('_',' ')):'Not called',item.latency_ms==null?'—':num(item.latency_ms/1000,1)+' s']));
    const comparison=row.comparison?'<p class="note">Baseline comparison: '+num(row.comparison.compared_cases?.length||0)+' matching cases · '+num(row.comparison.regressions?.length||0)+' regressions · '+num(row.comparison.recoveries?.length||0)+' recoveries.</p>':'';
    return '<details class="research-details"><summary>'+title+' · '+esc(summary)+' · '+esc(QUALITY_LABELS[row.status])+'</summary>'+(caution?'<p class="note">'+esc(caution)+'</p>':'')+(row.mode==='live'?'<p class="note">Model: '+esc(row.model||'Unrecorded')+'</p>':'')+failures+modelTable(['Known-answer case','Result','Local inference','Latency'],rows)+comparison+'</details>';
  }).join(''));
  $('export-analyst-checks').disabled=false;scheduleTableScroll();
}
async function loadAnalystQuality(){
  if(analystQualityLoading)return;analystQualityLoading=true;$('refresh-analyst-checks').disabled=true;
  try{drawAnalystQuality(await call('/api/analyst/evaluation'));}
  catch(error){$('analyst-checks-status').textContent='Checks unavailable';$('analyst-checks-status').className='badge amber';$('analyst-checks-note').textContent='Saved checks could not be refreshed. '+error.message;$('export-analyst-checks').disabled=true;}
  finally{analystQualityLoading=false;$('refresh-analyst-checks').disabled=false;}
}
function exportAnalystQuality(){
  if(!analystQuality||$('export-analyst-checks').disabled)return;
  const blob=new Blob([JSON.stringify(analystQuality,null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=url;link.download='tori-research-integrity.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function analystOptions(){return {filters:{...FILTERS},dimension:$('analysis-dimension').value,days:Number($('analysis-days').value),horizon:Number($('analysis-horizon').value)};}
function analystBusy(){return analystStarting||analystRun&&['queued','building','running'].includes(analystRun.status);}
function analystButtons(){const busy=Boolean(analystBusy());$('explain-segment').disabled=busy||!lastAnalysis;$('cancel-explanation').hidden=!busy;$('cancel-explanation').disabled=analystStarting;}
function analystScope(options){const f=options.filters;return Object.entries(f).map(([,value])=>LABELS[value]||value).join(' · ')+' · grouped by '+options.dimension.replaceAll('_norm','').replaceAll('_',' ')+' · '+options.horizon+'d sale horizon · '+options.days+'d activity';}
async function loadAnalystHistory(){
  try{const result=await call('/api/analyst');setHTML('analyst-history','<option value="">Saved explanations</option>'+result.runs.map(row=>{const scope=JSON.parse(row.options_json);return '<option value="'+esc(row.run_id)+'">'+esc(date(row.created_at)+' · '+(scope.filters.category_norm||scope.filters.municipality_norm||scope.filters.category_root_norm||'Selected market')+' · '+row.status)+'</option>';}).join(''));if(analystRun)$('analyst-history').value=analystRun.run_id;
    const remembered=new URLSearchParams(location.search).get('research_run');if(!analystRun&&remembered&&/^[0-9a-f-]{36}$/i.test(remembered))await loadAnalystRun(remembered);
  }catch(error){message('analyst-message','Saved explanations are unavailable. '+error.message,'error');}
}
async function startExplanation(){
  if(analystBusy())return;const options=analystOptions();clearTimeout(analystTimer);analystSequence++;analystStarting=true;analystButtons();message('analyst-message','Preparing a frozen evidence snapshot…');
  try{const result=await call('/api/analyst',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'explain',params:options})});analystRun={run_id:result.run_id,status:'queued',options};setHTML('analyst-result','');const url=new URL(location.href);url.searchParams.set('research_run',result.run_id);history.replaceState(null,'',url);await loadAnalystRun(result.run_id);}
  catch(error){message('analyst-message',error.message,'error');}
  finally{analystStarting=false;analystButtons();}
}
async function loadAnalystRun(runId){
  clearTimeout(analystTimer);const sequence=++analystSequence;
  try{const run=await call('/api/analyst?'+new URLSearchParams({run_id:runId}));if(sequence!==analystSequence)return;analystRun=run;drawExplanation(run);if(['queued','building','running'].includes(run.status))analystTimer=setTimeout(()=>loadAnalystRun(runId),1200);else loadAnalystHistory();}
  catch(error){if(sequence===analystSequence){message('analyst-message','Could not check research status. '+error.message,'error');if(analystRun&&['queued','building','running'].includes(analystRun.status))analystTimer=setTimeout(()=>loadAnalystRun(runId),4000);}}
  finally{if(sequence===analystSequence)analystButtons();}
}
async function cancelExplanation(){
  if(!analystRun)return;$('cancel-explanation').disabled=true;
  try{await call('/api/analyst',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel',params:{run_id:analystRun.run_id}})});await loadAnalystRun(analystRun.run_id);}
  catch(error){message('analyst-message',error.message,'error');$('cancel-explanation').disabled=false;}
}
function drawExplanation(run){
  if(run)$('local-research').open=true;
  const labels={queued:'Waiting',building:'Capturing evidence',running:'Local model researching',complete:run.cached_from_run_id?'Validated cached result':'Research complete',unavailable:'Evidence only',invalid:'Response rejected',error:'Research interrupted',cancelled:'Cancelled'};
  $('analyst-status').textContent=labels[run.status]||run.status;$('analyst-status').className='badge '+(run.status==='complete'?'green':['unavailable','invalid','error'].includes(run.status)?'amber':'purple');
  $('analyst-scope').textContent='Frozen scope: '+analystScope(run.options)+(run.evidence?' · captured '+date(run.evidence.captured_at):'');
  const progress={queued:'Your research request is waiting.',building:'Reading the local database into one consistent snapshot.',running:'The local model is prioritizing supported findings. You can keep using the dashboard.'};
  message('analyst-message',run.error||progress[run.status]||'',run.status==='invalid'||run.status==='error'?'error':'info');
  if(!run.evidence){setHTML('analyst-result','');return;}
  const e=run.evidence,byId=new Map(e.facts.map(row=>[row.id,row]));
  const selected=run.selection?run.selection.findings.map(id=>byId.get(id)):[];
  const findings=selected.length?'<div class="research-findings">'+selected.map(row=>'<article class="research-finding"><h3>'+esc(row.title)+'</h3><p>'+esc(row.detail)+'</p><button class="row-link" type="button" data-analyst-evidence="'+esc(row.id)+'">Open evidence →</button></article>').join('')+'</div>':'';
  const next=run.selection?run.selection.next_steps.map(id=>e.next_steps.find(row=>row.id===id)):[];
  const steps=next.length?'<div class="research-next"><h3>Next research steps</h3>'+next.map(row=>'<div class="research-step"><div><strong>'+esc(row.title)+'</strong><p>'+esc(row.detail)+'</p><button class="row-link" type="button" data-analyst-evidence="'+esc(row.evidence_ids[0])+'">Why this step →</button></div><button class="btn small" data-go="'+esc(row.view)+'" type="button">Review</button></div>').join('')+'</div>':'';
  setHTML('analyst-result',findings+steps+'<div class="research-limitations"><strong>Limits of this evidence</strong>'+e.limitations.map(text=>'<p>'+esc(text)+'</p>').join('')+'</div><details class="research-details"><summary>All captured evidence · '+e.facts.length+' findings</summary><div class="research-facts">'+e.facts.map(row=>'<button class="btn small" type="button" data-analyst-evidence="'+esc(row.id)+'">'+esc(row.title)+(row.values.label?' · '+esc(row.values.label):'')+'</button>').join('')+'</div></details><p class="research-provenance">'+(run.selection?'The local model prioritized these evidence-backed findings.':'Captured measurements remain available without a model explanation.')+' Model: '+esc(run.model.model)+' · '+esc(run.model.prompt_version)+' · '+(run.latency_ms!=null?num(run.latency_ms/1000,1)+'s inference · ':'')+'Snapshot '+esc(run.evidence_sha256.slice(0,12))+'. '+(run.model.revision==='unspecified'?'Model artifact revision is not pinned.':'Model artifact revision is pinned.')+'</p>');
}
function openAnalystEvidence(id){
  const run=analystRun,row=run?.evidence?.facts.find(item=>item.id===id);if(!row)return;
  $('analyst-evidence-title').textContent=row.title;setHTML('analyst-evidence-body','<p>'+esc(row.detail)+'</p><div class="research-evidence-values">'+Object.entries(row.values).map(([key,value])=>'<div class="evidence-value"><strong>'+esc(key.replaceAll('_',' '))+'</strong><pre>'+esc(value===null?'Unknown / unsupported':typeof value==='object'?JSON.stringify(value,null,2):String(value))+'</pre></div>').join('')+'</div><h3>Measurement scope</h3><p class="note">'+esc(row.source.scope||analystScope(run.options))+'</p><p class="note" style="margin-top:8px">Captured '+esc(date(run.evidence.captured_at))+' · '+esc(row.basis.replaceAll('_',' '))+' · '+esc(row.source.function)+'</p><p class="research-provenance">Evidence ID '+esc(row.id)+' · SHA-256 '+esc(run.evidence_sha256)+'<br>Model artifact '+esc(run.model.revision)+'</p>');$('analyst-evidence-dialog').showModal();
}
function drawActivity(){
  if(!lastAnalysis)return;
  const timeline=lastAnalysis.timeline,maximum=Math.max(1,...timeline.map(row=>row[activitySeries]));
  const seriesLabels={new_observations:'First discoveries',sold_declared:'Seller-declared sale detections',sold_verified:'Verified sale detections',price_reductions:'Observed price reductions'};
  const total=timeline.reduce((sum,row)=>sum+row[activitySeries],0);
  document.querySelectorAll('[data-series]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.series===activitySeries)));
  setHTML('activity-chart','<div class="activity-chart" role="group" aria-label="'+esc(seriesLabels[activitySeries])+' by day">'+timeline.map(row=>'<button type="button" class="activity-day" data-day="'+row.date+'" aria-label="'+row.date+': '+num(row[activitySeries])+' '+esc(seriesLabels[activitySeries].toLowerCase())+'" title="'+row.date+' · '+num(row[activitySeries])+'" style="border:0;padding:0"><i style="height:'+Math.max(row[activitySeries]?1:0,row[activitySeries]/maximum*100)+'%;'+(row[activitySeries]?'':'opacity:0')+'"></i></button>').join('')+'</div><div class="chart-axis"><span>'+esc(timeline[0]?.date)+'</span><span>Daily · Helsinki time</span><span>'+esc(timeline.at(-1)?.date)+'</span></div><div class="activity-summary"><span><strong>'+num(total)+'</strong>'+esc(seriesLabels[activitySeries])+'</span><span><strong>'+num(timeline.filter(row=>row[activitySeries]>0).length)+' / '+num(timeline.length)+'</strong>Days with recorded events</span></div>');
  $('activity-caption').textContent=seriesLabels[activitySeries]+' over the selected window.';
}
function drawPortfolio(portfolio){
  const p=portfolio;
  setHTML('portfolio-evidence','<div class="metric-list">'+metric('Recorded purchases',num(p.n_purchases))+metric('Sales / complete-cost sales',num(p.n_sold)+' / '+num(p.n_complete_margins))+metric('Revenue from recorded sales',eur(p.revenue_eur))+metric('Profit from complete-cost sales',eur(p.realized_profit_eur))+metric('Loss-making complete-cost trades',num(p.n_losses))+metric('Sales missing cost details',num(p.n_unknown_cost_sales))+metric('Median days from purchase to sale',p.median_days_to_sale==null?'Needs '+num(lastAnalysis.min_n)+' valid durations':num(p.median_days_to_sale,1)+'d')+'</div><p class="note portfolio-note">'+(p.profit_is_partial?'Profit is a partial total: incomplete-cost sales are excluded. ':'')+'Exact ledger totals describe recorded trades; medians require the minimum sample.</p>'+(p.by_category.length?'<div class="section-divider"></div><h3>Profit by your trade category</h3><div class="metric-list" style="margin-top:12px">'+p.by_category.slice(0,8).map(row=>metric(row.label+' · '+num(row.n)+' complete trades',eur(row.total_profit_eur))).join('')+'</div>':''));
  const stock=p.n_in_stock;
  setHTML('portfolio-aging','<h3>Inventory age · '+num(stock)+' items</h3><div class="age-bar" aria-hidden="true">'+Object.entries(p.age_buckets).filter(([,value])=>value).map(([label,value],index)=>'<i title="'+esc(label)+' · '+num(value)+'" style="flex:'+value+';opacity:'+(.35+index*.15)+'"></i>').join('')+'</div><div class="metric-list">'+Object.entries(p.age_buckets).map(([label,value])=>metric(label,num(value))).join('')+metric('Acquisition capital in stock',eur(p.acquisition_capital_eur))+metric('Known landed costs in stock',eur(p.known_landed_capital_eur))+'</div><p class="note portfolio-note">'+(p.n_unknown_landed_costs?num(p.n_unknown_landed_costs)+' items have unknown inbound costs. Known landed costs are a partial total.':'Landed costs include acquisition, inbound shipping, and buyer fees.')+'</p>'+(p.oldest_stock.length?'<div class="section-divider"></div><h3>Oldest stock</h3><div class="metric-list" style="margin-top:12px">'+p.oldest_stock.slice(0,4).map(row=>metric(row.title,row.age_days==null?'Unknown age':num(row.age_days,1)+'d')).join('')+'</div>':''));
}
function drawEvidenceGate(report){
  const l=report.liquidity, min_n=report.min_n, horizon=report.horizon;
  const declared=l.sold_inferred_seller_declared, verified=l.sold_confirmed;
  const followup=l.followup_coverage||0;
  const horizonSupport=l.horizon_support?.[horizon+'d']||0;
  const declaredReport=l.sell_through_by_evidence?.sold_inferred||{};
  const verifiedReport=l.sell_through_by_evidence?.sold_confirmed||{};
  const declaredOK=declaredReport.status==='OK';
  const verifiedOK=verifiedReport.status==='OK';
  const gateOpen=declaredOK||verifiedOK;
  const missing=[]; if(!declaredOK) missing.push('declared: '+num(declared)+' events (need '+num(min_n)+')'); if(!verifiedOK) missing.push('verified: '+num(verified)+' events (need '+num(min_n)+')'); if(followup<0.5) missing.push('follow-up coverage '+percent(followup*100)); if(horizonSupport<min_n) missing.push(num(horizonSupport)+' followed to '+horizon+'d (need '+num(min_n)+')');
  $('gate-status').textContent=gateOpen?'Estimates available':'Needs more evidence';
  $('gate-status').className='badge '+(gateOpen?'green':'amber');
  let html='<div class="metric-list">';
  html+=metric('Seller-declared sale markers',num(declared)+' events'+(declaredOK?' · estimates OK':' · needs '+num(min_n)+' events'));
  html+=metric('Verified sale markers',num(verified)+' events'+(verifiedOK?' · estimates OK':' · needs '+num(min_n)+' events'));
  html+=metric('Follow-up coverage',percent(followup*100));
  html+=metric('Listings followed to '+horizon+'d',num(horizonSupport)+' of '+min_n+' minimum');
  html+='</div>';
  if(missing.length){
    html+='<div class="section-divider"></div><h3>Missing for supported estimates</h3><div class="metric-list">'+missing.map(m=>metric('—',m)).join('')+'</div>';
    html+='<p class="note" style="margin-top:12px">Run <strong>Check listing status</strong> from the Collection view. Each probe that finds a terminal state adds evidence. Breadth does not clear this gate; follow-up does.</p>';
  }else{
    html+='<p class="note" style="margin-top:12px"><strong>Evidence gate is open.</strong> Sell-through estimates are reported for supported evidence classes at '+horizon+'d horizon.</p>';
  }
  setHTML('evidence-gate-detail',html);
}
function drawProductIdentity(report){
  if(!report){setHTML('product-identity-quality',empty('Product identity coverage is unavailable','Review model, variant and offer details before comparing prices.'));return;}
  const fields={model_key:'model',storage:'storage',hardware_code:'hardware code',edition:'disc or digital edition',mount:'lens mount',condition:'condition',functionality:'functionality',completeness:'completeness',defects:'defects',offer_kind:'offer contents',quantity:'quantity',battery_health_pct:'battery health'};
  const rows=report.families.map(family=>{
    const measured=family.reviewed_model_precision;
    const accuracy=measured.precision===null?'Not measured':percent(100*measured.precision)+' on '+num(measured.n)+' reviewed examples';
    const missing=Object.entries(family.missing_fields).slice(0,3).map(([key,n])=>esc(fields[key]||key)+' ('+num(n)+')').join(', ');
    return '<tr><td><strong>'+esc(family.label)+'</strong><br><span class="note">'+(missing?'Needs '+missing:'No missing fields reported')+'</span></td><td class="num">'+num(family.n_mentions)+'</td><td class="num">'+num(family.n_variant_identified)+'</td><td class="num">'+num(family.n_comparable)+'</td><td>'+esc(accuracy)+'</td></tr>';
  }).join('');
  const audit=report.audit_evaluation;
  const holdout=audit?'<p class="note">'+num(audit.n_eligible_holdout_reviews)+' current holdout reviews · '+num(Object.values(audit.families).reduce((total,family)=>total+family.n_unit_groups,0))+' family/unit groups. Holdout predictions were frozen before labels, and relists score once per verified physical unit. Automatic comparison remains provisional.</p>':'';
  setHTML('product-identity-quality','<p class="note">'+num(report.n_comparable)+' comparable single units · '+num(report.n_current_reviews)+' current operator reviews. A model name alone does not establish comparability.</p><div class="table-wrap"><table class="analyst-table"><thead><tr><th>Product family · missing facts</th><th class="num">Mentions</th><th class="num">Known variants</th><th class="num">Comparable</th><th>Reviewed model precision</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+holdout+'<p class="note">Precision describes scored reviewed examples only. Unreviewed offer facts stay unknown; asking prices do not establish resale profit or selling speed.</p>');
}
function drawAnalysis(report){
  const p=report.portfolio,l=report.liquidity;
  setHTML('analysis-followup',followupHTML(report.followup)+classificationQuality(report.classification_quality));
  drawEvidenceGate(report);
  drawProductIdentity(report.product_identity);
  $('analysis-verdict').textContent=report.readiness.prices==='OK'?(report.readiness.demand==='OK'?'Price and sale evidence are available. Compare the segments, inspect the item, then test your cost assumptions.':'Asking-price research is ready. Sale evidence is still developing; repeated status checks will build the history.'):'This segment needs more fresh priced observations. Broaden your filters or collect more listings before comparing prices.';
  setHTML('analysis-readiness',badge('Prices · '+(report.readiness.prices==='OK'?'ready':'building'),report.readiness.prices==='OK'?'green':'')+badge('Demand · '+(report.readiness.demand==='OK'?'ready':'building'),report.readiness.demand==='OK'?'green':'')+badge('Profit history · '+(report.readiness.profit==='OK'?'ready':'building'),report.readiness.profit==='OK'?'green':''));
  $('analysis-supply').textContent=num(report.n_available);$('analysis-supply-note').textContent=num(report.n_market)+' tracked for-sale items · '+num(report.max_age_hours/24)+'d freshness';
  $('analysis-sales').textContent=num(l.sold_inferred_seller_declared+l.sold_confirmed);$('analysis-sales-note').textContent=num(l.sold_inferred_seller_declared)+' declared · '+num(l.sold_confirmed)+' verified · all follow-up';
  $('analysis-profit').textContent=eur(p.realized_profit_eur);$('analysis-profit').className='stat-value'+(p.realized_profit_eur<0?' negative':'');$('analysis-profit-note').textContent=p.n_complete_margins?num(p.n_complete_margins)+' complete-cost trades'+(p.profit_is_partial?' · partial total':' · all your trades'):'Record sales and all costs to establish profit';
  $('analysis-capital').textContent=eur(p.acquisition_capital_eur);$('analysis-capital-note').textContent=num(p.n_in_stock)+' owned items · acquisition costs only';
  $('risk-count').textContent=num(report.risks.length)+' priorities';
  setHTML('analysis-risks',report.risks.length?report.risks.map(row=>'<div class="risk-item"><span class="risk-count '+row.severity+'">'+num(row.count)+'</span><div><h3>'+esc(row.title)+'</h3><p>'+esc(row.detail)+'</p><button class="row-link" data-go="'+esc(row.view)+'" type="button">Review in '+esc(VIEWS[row.view][0].toLowerCase())+' →</button></div></div>').join(''):empty('No flagged gaps in this segment','Keep reviewing condition, model, costs, and follow-up as new evidence arrives.'));
  const inventory=report.research_inventory||{},hasInventory=!!inventory.items?.length;
  $('analysis-shortlist-description').textContent=hasInventory
    ?'Current collected listings awaiting product and offer review. Inspect their observed asking prices and source evidence.'
    :'At least 20% below peer median and below P25. Each row states what the peer match actually used and whether the gap survives the median’s own interval. Inspect before deciding.';
  $('analysis-shortlist-badge').textContent=hasInventory?'Awaiting review':'Ask comparison';
  $('analysis-shortlist-badge').className='badge '+(hasInventory?'amber':'purple');
  setHTML('analysis-candidates',hasInventory
    ?'<p class="note">Showing '+num(inventory.items.length)+' of '+num(inventory.total)+' current listings. No supported price comparisons yet; these are observed asks awaiting review.</p><p><button class="btn small" data-go="research" type="button">Open research desk →</button></p>'+researchInventory(inventory,'analysis')+comparisonCoverage(report)
    :comparisonCoverage(report)+(report.candidates.length?candidateCards(report.candidates):empty('No supported price candidates yet','A candidate needs a supported product variant, a reviewed working single-unit offer, and at least '+num(report.min_n)+' other eligible fresh prices. Category and product-type distributions remain market context.','search')));
  drawMatrix();drawActivity();drawPortfolio(p);drawComparisonOptions();
  analystButtons();
}
// --------------------------------------------------------------------------
// price research desk
// --------------------------------------------------------------------------
//
// A work queue rather than a summary. Three pieces of state drive it: the slice
// (the shared workspace filters, so the desk never quietly reads a different
// market than the rest of the page), the threshold the reader chose, and which
// candidates they have picked out. Choices use stable listing keys so a refresh
// cannot transfer a selection to another row.

let deskReport=null,deskSessions=null,deskDetail=null,deskListing=null,deskSequence=0,deskDetailSequence=0,deskSessionsSequence=0;
let deskController=null,deskSessionsController=null,deskDetailController=null,deskProceedsSequence=0;
let deskLoadPromise=null,deskLoadKey='';
let deskRequestKey=null;
let deskSelection=new Set();
let deskScenarios={};
const deskControls={'desk-min-gap':'desk_gap','desk-limit':'desk_limit','desk-horizon':'desk_horizon','desk-peer-rows':'desk_peers'};

/**
 * Lock the desk settings a deployment cannot honour.
 *
 * The question is not "is this a write" but "can this deployment measure a different
 * shortlist". Only the runtime can: the exported shortlist was computed once, at one
 * threshold, over one horizon, without peer rows. So a replay deployment locks those
 * three, and the runtime locks nothing.
 *
 * `limit` is deliberately left alone everywhere. It only truncates an
 * already-ordered queue, so honouring it changes no row's inclusion or evidence.
 *
 * Leaving them editable on a deployment that must refuse them meant a view that
 * loaded perfectly at the defaults and then failed the moment anyone touched a
 * control -- which reads as the data being broken rather than as the control being
 * unavailable.
 */
function lockDeskControls(){
  const canMeasure=capabilities.deployment==='runtime';
  for(const [id,reason] of [['desk-min-gap','the exported shortlist was measured at one threshold'],
                            ['desk-horizon','the exported shortlist was measured at one horizon'],
                            ['desk-peer-rows','the export carries no peer comparison rows']]){
    const node=$(id);
    if(!node)continue;
    node.disabled=!canMeasure;
    node.title=node.disabled?reason+', so only the local research runtime can change it.':'';
  }
}
for(const [id,key] of Object.entries(deskControls)){
  const value=query.get(key),input=$(id);
  if(value!==null&&(input.tagName==='SELECT'?[...input.options].some(option=>option.value===value):/^\d+$/.test(value)&&Number(value)>=1&&Number(value)<=99))input.value=value;
}

function syncDeskURL(){
  const params=new URLSearchParams(location.search);
  for(const [id,key] of Object.entries(deskControls))params.set(key,$(id).value);
  history.replaceState(null,'',location.pathname+'?'+params.toString()+'#'+currentView);
}

function rememberDeskScenario(){
  if(!deskDetail)return;
  deskScenarios[String(deskDetail.listing_pk)]={
    ...Object.fromEntries([...$('desk-calculator-form').querySelectorAll('input[name]')].map(input=>[input.name,input.value===''?null:Number(input.value)])),
    haircut:Number($('desk-discount').value),
    checks:deskScenarios[String(deskDetail.listing_pk)]?.checks||{},
    _scenario:deskScenarios[String(deskDetail.listing_pk)]?._scenario||null,
    _origins:deskScenarios[String(deskDetail.listing_pk)]?._origins||{},
  };
}

function closeDeskDetail(){
  closeInternetResearch();
  rememberDeskScenario();
  deskDetailSequence++;deskDetailController?.abort();deskProceedsSequence++;
  deskDetail=null;deskListing=null;
  $('desk-detail').hidden=true;$('desk-calculator').hidden=true;
  setHTML('desk-proceeds-result','');
}

const deskParams=()=>{const params=new URLSearchParams(filtersToQuery(FILTERS));params.set('min_gap_pct',$('desk-min-gap').value);params.set('limit',$('desk-limit').value);params.set('horizon',$('desk-horizon').value);params.set('peer_rows',$('desk-peer-rows').value);return params;};

// The aggregate report already travels in the snapshot, so the history gate reads
// that rather than making a second query it could disagree with.
function drawDeskTrend(){
  setHTML('desk-trend',trendGate(lastSnapshot?.history||{}));
}

// Comparing is a reading task, so it is capped. Eight side-by-side rows is already
// wider than most screens; a 200-row table is a spreadsheet, and the CSV export is
// what a spreadsheet is for.
const DESK_COMPARE_MAX=8;

function deskSelectedRows(){
  return(deskReport?.candidates||[]).filter(row=>deskSelection.has(row.listing_pk));
}
function toggleDeskSelection(pk){
  if(!Number.isSafeInteger(pk)||pk<=0)return;
  if(deskSelection.has(pk))deskSelection.delete(pk);
  else if(deskSelection.size<50)deskSelection.add(pk);
  else toast('A research session can hold at most 50 listings.');
  drawDesk();
}

function loadDesk(options={}){
  const requestKey=deskParams().toString();
  // Background snapshots can arrive while navigation is still loading this scope.
  // Share that read so its caller can finish opening the selected offer.
  if(deskLoadPromise&&deskLoadKey===requestKey)return deskLoadPromise;
  deskLoadKey=requestKey;
  const task=readDesk(options,requestKey);deskLoadPromise=task;
  const finished=()=>{if(deskLoadPromise===task){deskLoadPromise=null;deskLoadKey='';}};
  task.then(finished,finished);return task;
}

async function readDesk({preserveDetail=false},requestKey){
  const inspected=preserveDetail&&deskRequestKey===requestKey?deskListing:null;
  const detailSequence=deskDetailSequence;
  if(!inspected)closeDeskDetail();
  syncDeskURL();
  if(deskController)deskController.abort();
  const sequence=++deskSequence;
  $('desk-refresh').disabled=true;
  $('desk-table').setAttribute('aria-busy','true');
  setHTML('desk-error','');
  deskController=new AbortController();
  if(capabilities.reads.has('/api/research/internet'))call('/api/research/internet').then(monitor=>{if(sequence===deskSequence)setHTML('desk-internet-monitor',internetMonitor(monitor));})
    .catch(error=>{if(sequence===deskSequence)setHTML('desk-internet-monitor','<p class="note">Could not read the research monitor: '+esc(String(error.message||error))+'</p>');});
  try{
    const report=await call('/api/research/candidates?'+requestKey,{signal:deskController.signal});
    if(sequence!==deskSequence)return;
    deskReport=report;
    deskRequestKey=requestKey;
    // Chosen listing identities survive limits and changes in eligibility.
    // Saved choices are re-read through session status rather than stored prices.
    drawDesk();
    if(inspected&&detailSequence===deskDetailSequence&&deskListing?.listing_pk===inspected.listing_pk){
      const current=[...(report.candidates||[]),...(report.research_inventory?.items||[])]
        .find(row=>row.listing_pk===inspected.listing_pk)||{listing_pk:inspected.listing_pk,title:inspected.title};
      await openDeskItem(current,{scroll:false});
    }
  }catch(error){
    if(sequence===deskSequence&&error.name!=='AbortError')message('desk-error',String(error.message||error).slice(0,300),'error');
  }finally{
    if(sequence===deskSequence){$('desk-refresh').disabled=false;$('desk-table').setAttribute('aria-busy','false');}
  }
}

function drawDeskComparison(){
  const candidatePks=new Set((deskReport?.candidates||[]).map(row=>row.listing_pk));
  const chosen=deskSelectedRows().filter(row=>candidatePks.has(row.listing_pk));
  const usable=chosen.slice(0,DESK_COMPARE_MAX);
  $('desk-compare-selected').disabled=chosen.length<2;
  if(chosen.length<2){$('desk-compare-panel').hidden=true;return;}
  $('desk-compare-panel').hidden=false;
  $('desk-compare-note').textContent=chosen.length>DESK_COMPARE_MAX
    ?'Showing the first '+num(DESK_COMPARE_MAX)+' of '+num(chosen.length)+' selected. Use Export CSV for the rest.'
    :num(chosen.length)+' selected, each against its own peer cohort.';
  setHTML('desk-compare',comparisonTable(usable));
}

function downloadDeskReport(){
  const chosen=deskSelectedRows(),rows=chosen.length?chosen:deskReport?.candidates||[];
  if(!rows.length)return;
  const markdown=researchReport(rows,deskReport||{},deskDetail);
  const url=URL.createObjectURL(new Blob([markdown],{type:'text/markdown;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;
  link.download='price-research-'+(deskReport?.min_gap_pct??20)+'pct.md';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast('Downloaded a report for '+num(rows.length)+' candidate'+(rows.length===1?'':'s')+'.');
}

function drawDesk(){
  const report=deskReport;
  if(!report){setHTML('desk-table',deskIdle(30));return;}
  const rows=report.candidates||[];
  const summary=report.summary||{};
  setHTML('desk-summary',deskSummary(report));
  $('desk-count').textContent=rows.length
    ?num(rows.length)+(summary.truncated?' of '+num(summary.total_candidates):'')+' candidate'+(rows.length===1?'':'s')+' · '+num(report.min_n)+' peers required'
    :'No candidates at this threshold.';
  $('desk-export').disabled=!rows.length;
  $('desk-export-markdown').disabled=!rows.length;
  $('desk-save-session').disabled=!deskSelection.size;
  $('desk-inventory-save').disabled=!deskSelection.size;
  setHTML('desk-table',rows.length?candidateTable(rows,deskSelection,$('desk-sort').value):deskIdle(report.min_n));
  const inventory=report.research_inventory||{},hasInventory=!!inventory.items?.length;
  $('desk-research-list').hidden=!hasInventory;
  $('desk-hero').hidden=hasInventory;
  $('desk-list').hidden=!rows.length&&hasInventory;
  $('desk-research-count').textContent=num(inventory.items?.length||0)+' of '+num(inventory.total||0)+' current listings';
  setHTML('desk-research-table',hasInventory?researchInventory(inventory,'desk',deskSelection):'');
  const visibleSelected=[...rows,...(inventory.items||[])].filter(row=>deskSelection.has(row.listing_pk)).length;
  $('desk-selection-note').textContent=num(deskSelection.size)+' selected for this research pass'+
    (deskSelection.size>visibleSelected?' · '+num(deskSelection.size-visibleSelected)+' outside the displayed results':'')+'.';
  const requirements=report.requirements||{};
  $('desk-basis').textContent=[
    'At least '+(requirements.min_peers??report.min_n)+' other fresh priced peers',
    requirements.detailed_category,requirements.known_condition,requirements.accepted_product_type,requirements.reviewed_offer,
    requirements.both_filters].filter(Boolean).join(' · ')+'. '+report.note;
  drawDeskComparison();
  drawDeskTrend();
  setHTML('desk-readiness',
    badge(num(summary.total_candidates||0)+' candidates','purple')+
    badge(num(summary.min_gap_survives_ci||0)+' meet the minimum gap across their interval',(summary.min_gap_survives_ci?'green':''))+
    badge(num(summary.gap_untested||0)+' untestable',(summary.gap_untested?'amber':'')));
}

async function openDeskDetail(index){
  const row=deskReport?.candidates?.[index];
  return openDeskItem(row);
}

async function openDeskItem(row,{scroll=true}={}){
  if(!row)return;
  closeDeskDetail();deskListing=row;
  const sequence=++deskDetailSequence;
  deskDetailController=new AbortController();
  $('desk-detail').hidden=false;
  $('desk-detail-title').textContent=row.title||'Offer '+row.listing_pk;
  $('desk-detail-sub').textContent='';
  setHTML('desk-detail-body','<div id="desk-candidate-loading"><p class="note">Reading this item\u2019s cohort…</p></div>');
  mountInternetResearch($('desk-detail-body'),row,call);
  try{
    const report=deskReport||{};
    const params=new URLSearchParams(filtersToQuery(report.filters||FILTERS));
    params.set('listing_pk',row.listing_pk);params.set('min_gap_pct',report.min_gap_pct??$('desk-min-gap').value);params.set('horizon',report.horizon_days??$('desk-horizon').value);
    if(report.reference_at)params.set('reference_at',report.reference_at);
    if(row.benchmark_revision)params.set('benchmark_revision',row.benchmark_revision);
    const detail=await call('/api/research/candidate?'+params.toString(),{signal:deskDetailController.signal});
    if(sequence!==deskDetailSequence)return;
    deskDetail=detail;
    drawDeskDetail(detail);
    prefillDeskCalculator(detail,scroll?row:null);
  }catch(error){
    if(sequence===deskDetailSequence&&error.name!=='AbortError')setHTML('desk-candidate-loading','<div class="message error">Could not read this item: '+esc(String(error.message||error).slice(0,200))+'</div>');
  }
}

function drawDeskDetail(detail){
  const listing=detail.listing||{},cohort=detail.cohort||{};
  $('desk-detail-title').textContent=listing.title||listing.listing_id||'Inspect this item';
  $('desk-detail-sub').textContent=[listing.category_norm,listing.condition_norm,listing.municipality_norm]
    .filter(Boolean).join(' · ')+' · '+num(listing.ask_eur,2,2)+' € asking price';
  setHTML('desk-detail-body',profileSummary(detail.profile)+
    verdictPanel(detail)+identitySummary(detail)+cohortDetails(cohort)+
    '<h3 class="desk-subhead">What you still have to check</h3>'+inspectionList(detail.inspection,deskScenarios[String(detail.listing_pk)]?.checks||{})+
    '<h3 class="desk-subhead">Your own completed trades in this category</h3>'+ownedComparables(detail.owned_comparables||{})+
    '<h3 class="desk-subhead">Decisions you have already recorded</h3>'+recordedDecisions(detail.decisions||{})+
    '<div class="desk-links">'+
    '<button class="btn primary small" data-desk-keep type="button">'+
    (deskSelection.has(listing.listing_pk)?'Remove from research session':'Add to research session')+'</button>'+
    (detail.links?.source?.url?'<a class="btn small" href="'+esc(detail.links.source.url)+'" target="_blank" rel="noopener noreferrer">Open marketplace offer</a>':'')+
    '<button class="btn small" data-desk-provenance type="button">Where these numbers come from</button>'+
    '<button class="btn small" data-desk-evidence type="button">Stored listing evidence</button>'+
    '<button class="btn small" data-source-review="'+listing.listing_pk+'" type="button">Review source identity</button>'+
    '<button class="btn small" data-desk-item-workspace type="button">Item workspace</button>'+
    '<button class="btn small" data-desk-open-listing type="button">Open in observations</button></div>');
  mountInternetResearch($('desk-detail-body'),listing,call);
}

function prefillDeskCalculator(detail,row){
  const listing=detail.listing||{};
  const form=$('desk-calculator-form').elements;
  $('desk-calculator-form').reset();
  // The peer median is deliberately NOT written into sale proceeds. An asking
  // price is not a clearing price, and pre-filling proceeds with a peer median
  // would manufacture the exact conclusion this page exists to withhold. The
  // field is left blank with a placeholder that says where the number must come
  // from, and only the costs we actually observed are carried over.
  form.resale.value='';
  form.resale.placeholder='Not derivable from peer asks — your researched estimate';
  form.buy.value=listing.ask_eur??'';
  form.inbound.value=[listing.buyer_shipping_eur,listing.buyer_fee_eur].every(v=>v!=null)
    ?listing.buyer_shipping_eur+listing.buyer_fee_eur:'';
  $('desk-discount').value='20';
  const retained=deskScenarios[String(listing.listing_pk)];
  const saved=retained?.version==='scenario-workspace-v1'?retained.inputs:retained;
  if(retained?.version==='scenario-workspace-v1')deskScenarios[String(listing.listing_pk)]={...retained.inputs,_scenario:retained,checks:{}};
  if(saved&&(!retained.mode||retained.mode==='cash')){
    for(const input of $('desk-calculator-form').querySelectorAll('input[name]'))if(Object.hasOwn(saved,input.name))input.value=saved[input.name]??'';
    if(Number.isFinite(saved.haircut))$('desk-discount').value=saved.haircut;
  }
  const defaults=Object.fromEntries([...$('desk-calculator-form').querySelectorAll('input[name]')].map(input=>[input.name,input.name==='buy'?'observed_ask':input.name==='inbound'?'observed_inbound':'calculator_default']));defaults.haircut='calculator_default';
  const draft=deskScenarios[String(listing.listing_pk)]||(deskScenarios[String(listing.listing_pk)]={});
  draft._origins=retained?{...defaults,...Object.fromEntries(Object.keys(saved||{}).filter(key=>!key.startsWith('_')&&key!=='checks').map(key=>[key,'legacy_assumption'])),...retained._origins,...retained._scenario?.origins,...retained.origins}:defaults;
  $('desk-calculator').hidden=false;
  drawDeskCalculator();
  row&&$('desk-calculator').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});
}

function deskAssumptions(){
  const fields=[...$('desk-calculator-form').querySelectorAll('input[name]')];
  if(fields.some(input=>input.value===''||!input.checkValidity()))return null;
  return Object.fromEntries(fields.map(input=>[input.name,Number(input.value)]));
}

function drawDeskCalculator(){
  const haircut=Number($('desk-discount').value);
  $('desk-discount-label').textContent=percent(haircut);
  const values=deskAssumptions();
  setHTML('desk-calculator-result',scenarioResult(values,haircut));
}

async function loadDeskProceeds(){
  if(!deskListing)return;
  const sequence=++deskProceedsSequence;
  setHTML('desk-proceeds-result','<p class="note">Checking your recorded resale pairings…</p>');
  try{
    const result=await call('/api/research/proceeds?listing_pk='+deskListing.listing_pk);
    if(sequence!==deskProceedsSequence)return;
    if(result.status!=='ESTIMATE'){
      setHTML('desk-proceeds-result','<div class="message info"><strong>No expected resale price.</strong><p style="margin-top:6px">'+esc(result.note||'')+'</p></div>');
      return;
    }
    const estimate=result.estimate||{};
    setHTML('desk-proceeds-result','<div class="message info"><strong>Expected realised '+eur(estimate.expected_realized_eur)+'</strong>'+
      '<p style="margin-top:6px">Realization ratio '+num(estimate.realization_ratio,2)+' applied to a peer ask median of '+eur(estimate.median_ask_eur)+'. '+
      (estimate.interval_eur?'Interval '+eur(estimate.interval_eur[0])+'–'+eur(estimate.interval_eur[1])+'. ':'')+esc(estimate.caveat||'')+'</p></div>');
  }catch(error){
    if(sequence===deskProceedsSequence)setHTML('desk-proceeds-result','<p class="note">Could not read comparable resales: '+esc(String(error.message||error).slice(0,200))+'</p>');
  }
}

async function loadDeskSessions(){
  if(!capabilities.reads.has('/api/research/sessions')){setHTML('desk-sessions','<p class="note">Saved sessions are available with the live backend.</p>');return;}
  if(deskSessionsController)deskSessionsController.abort();
  const sequence=++deskSessionsSequence;
  deskSessionsController=new AbortController();
  try{
    const result=await call('/api/research/sessions?min_gap_pct='+$('desk-min-gap').value+'&horizon='+$('desk-horizon').value,{signal:deskSessionsController.signal});
    if(sequence!==deskSessionsSequence)return;
    deskSessions=result;
    setHTML('desk-sessions',sessionList(result));
  }catch(error){
    if(sequence===deskSessionsSequence&&error.name!=='AbortError')setHTML('desk-sessions','<p class="note">Could not read saved sessions.</p>');
  }
}

function exportDesk(){
  if(!deskReport?.candidates?.length)return;
  const chosen=deskSelectedRows(),rows=chosen.length?chosen:deskReport.candidates;
  downloadCSV('price-research-'+deskReport.min_gap_pct+'pct.csv',exportColumns,rows.map(row=>exportCells(row,deskReport)));
  toast('Exported '+num(rows.length)+(chosen.length?' selected':' visible')+' candidate'+(rows.length===1?'':'s')+'.');
}

const deskWrite=(params)=>call('/api/research/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)});

// Reopening a session applies its filters to the shared workspace and re-reads
// the desk against current evidence. It deliberately restores no price: the saved
// rows carry only which listings were chosen, so anything shown after this call
// was measured now.
async function openDeskSession(name){
  const session=(deskSessions?.sessions||[]).find(row=>row.name===name);
  if(!session)return;
  closeDeskDetail();
  for(const key of FILTER_KEYS)delete FILTERS[key];
  for(const [key,value] of Object.entries(session.filters||{})){
    if(FILTER_KEYS.includes(key)&&typeof value==='string'&&value.length<=300)FILTERS[key]=value;
  }
  FILTERS.platform=FILTERS.platform||'tori';
  FILTERS.subplatform=FILTERS.subplatform||'standard';
  listingPage=1;
  if(analysisController)analysisController.abort();
  deskSelection=new Set(session.status?(session.status.listings||[]).map(row=>row.listing_pk):(session.selected_pks||[]));
  deskScenarios={...(session.scenarios||{})};
  $('desk-session-name').value=session.name;$('desk-session-notes').value=session.notes||'';
  syncURL();drawSegments(lastSnapshot?.segments||{});refresh();
  await loadDesk();
  const missing=(session.selected_pks||[]).length-deskSelection.size;
  toast('Opened "'+name+'" with current evidence.'+(missing?' '+num(missing)+' choices are no longer stored.':''));
}

async function saveDeskSession(){
  const name=$('desk-session-name').value.trim();
  if(!name)return;
  rememberDeskScenario();
  const selected=[...deskSelection];
  const scenarios=Object.fromEntries(selected.filter(pk=>Object.hasOwn(deskScenarios,String(pk))).map(pk=>[pk,deskScenarios[String(pk)]]));
  try{
    await deskWrite({action:'save',name,filters:{...(deskReport?.filters||FILTERS)},selected_pks:selected,notes:$('desk-session-notes').value,scenarios});
    toast('Saved session "'+name+'".');
    await loadDeskSessions();
  }catch(error){toast(String(error.message||error).slice(0,200),true);}
}

async function deleteDeskSession(name){
  const session=(deskSessions?.sessions||[]).find(row=>row.name===name);
  if(!session)return;
  try{
    await deskWrite({action:'delete',session_pk:session.session_pk});
    toast('Deleted "'+name+'".');
    await loadDeskSessions();
  }catch(error){toast(String(error.message||error).slice(0,200),true);}
}

async function openDeskDecision(pk){
  try{
    const result=await call('/api/research?decision_pk='+encodeURIComponent(pk));
    const entry=result.journal?.items?.[0];
    if(entry)showDecisionDetail(entry);else toast('This journal entry is no longer stored.',true);
  }catch(error){toast(error.message,true);}
}

// `calculateScenario` is imported from views/research.js. The desk and the analyst
// calculator must agree on every figure, and two copies of a margin formula is how a
// reader ends up comparing a base profit from one page against a downside profit
// from another and concluding the tool contradicts itself.
function drawCalculator(){
  const fields=[...$('calculator-form').querySelectorAll('input[name]')],haircut=Number($('scenario-discount').value);
  $('scenario-discount-label').textContent=percent(haircut);
  if(fields.some(input=>input.value===''||!input.checkValidity())){setHTML('calculator-result','<p class="note">Enter valid sale proceeds, purchase price, and all cost assumptions. Blank costs remain unknown.</p>');return;}
  const values=Object.fromEntries(fields.map(input=>[input.name,Number(input.value)])),s=calculateScenario(values,haircut);
  if(!s){setHTML('calculator-result','<p class="note">Use finite nonnegative assumptions and a fee rate below 100%.</p>');return;}
  const tone=value=>value>=0?'positive':'negative';
  setHTML('calculator-result','<div class="scenario-stats"><div class="scenario-stat"><span>Base profit</span><strong class="'+tone(s.base)+'">'+eur(s.base)+'</strong></div><div class="scenario-stat"><span>Downside profit</span><strong class="'+tone(s.downside)+'">'+eur(s.downside)+'</strong></div><div class="scenario-stat"><span>Max buy for downside target</span><strong class="'+tone(s.maxBuy)+'">'+(s.maxBuy>=0?eur(s.maxBuy):'No feasible price')+'</strong></div></div><div class="scenario-detail">'+badge(s.downside>=s.target?'Downside covers target':s.downside>=0?'Downside below target':'Loss in downside case',s.downside>=s.target?'green':s.downside>=0?'amber':'red')+'<p>Break-even sale proceeds: <strong>'+eur(s.breakEven)+'</strong> · Base ROI on all costs: <strong>'+percent(s.roi==null?null:s.roi*100)+'</strong></p><p>Downside proceeds '+eur(s.downsideSale)+' · Fixed costs '+eur(s.fixed)+' (including '+eur(s.holding)+' holding costs) · Target '+eur(s.target)+'.</p></div>');
}
function exportAnalysis(){
  if(!lastAnalysis||$('export-analysis').disabled)return;
  const r=lastAnalysis,head=['segment','basis','available','priced','price_coverage','median_ask_eur','median_ci95_lo','median_ci95_hi','p25_eur','p75_eur','sold_declared','sold_verified','horizon_days','declared_incidence','declared_ci95_lo','declared_ci95_hi','verified_incidence','verified_ci95_lo','verified_ci95_hi','followup_coverage','horizon_support','min_n','filters','generated_at','known_condition','current_classified','never_probed','probes_due','probes_due_in_focus','minimum_probe_runs'];
  const cells=matrixRows().map(row=>{const d=row.sale_rates.sold_inferred,v=row.sale_rates.sold_confirmed,f=row.followup||{};return [row.label,row.basis,row.n_available,row.n_priced,row.price_coverage,row.median_eur,row.median_ci95?.lo,row.median_ci95?.hi,row.p25_eur,row.p75_eur,row.sold_declared,row.sold_verified,r.horizon,d?.share,d?.ci95.lo,d?.ci95.hi,v?.share,v?.ci95.lo,v?.ci95.hi,row.followup_coverage,row.horizon_support,r.min_n,JSON.stringify(r.filters),r.generated_at,f.n_known_condition,f.n_classified,f.n_never_probed,f.n_due,f.n_due_in_scope,f.probe_runs_at_budget];});
  downloadCSV('tori-analysis-'+r.dimension+'-'+r.generated_at.slice(0,10)+'.csv',head,cells);
}
function downloadCSV(filename,header,rows){
  const csv=csvDocument(header,rows),url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function actionButtons(){
  document.querySelectorAll('[data-action]').forEach(button=>{
    const action=button.dataset.action,health=lastSnapshot?.health,running=(health?.running_actions||[]).some(row=>row.action===action)||localRuns.has(action),collection=['sweep','itempage'].includes(action);
    const authority=lastSnapshot?.permissions,sourceAction={sweep:'discovery',itempage:'detail',enrich:'enrichment'}[action],uncovered=sourceAction&&authority&&(!authority.valid||!authority.actions?.[sourceAction]?.allowed);
    button.disabled=!lastSnapshot||running||uncovered||((collection||action==='enrich')&&(health?.collector_running||localRuns.size>0))||(collection&&health?.blocked)||(action==='enrich'&&!lastSnapshot?.enrichment?.provider_reachable);
    button.textContent=running?'Running…':ACTION_LABELS[action];
    if(action==='enrich')button.title=lastSnapshot?.enrichment?.provider_reachable?'':'Start the local classification model to use this action.';
  });
}
function drawOverviewBanner(snapshot){
  const c=snapshot.compliance||{}, blockers=c.blocking||[];
  const names={lia_signed_off:'Legitimate-interest assessment needs sign-off',lia_controller_named:'Assessment needs the controller name',art14_controller_named:'Privacy notice needs the controller name',contact_email_deliverable:'Collector contact address is missing'};
  if(blockers.length){
    const text=blockers.map(key=>names[key]||key.replaceAll('_',' ')).join(' · ');
    message('overview-banner','Setup incomplete: '+text+'. See Collection → Workspace details.','error');
  }else{
    message('overview-banner','Configured document text checks are complete. Publication, sign-off and source authority require their own evidence.','success');
  }
  const focus=snapshot.collection||{};
  if(focus.focus_category_names&&focus.focus_category_names.length){
    const count=focus.focus_category_names.length;
    message('overview-banner',($('overview-banner').textContent?$('overview-banner').textContent+' | ':'')+'Focused collection: '+count+' vertical'+(count===1?'':'s')+' ('+focus.focus_category_names.map(name=>name.split(' > ').at(-1)).join(', ')+')','info');
  }
}
function drawMoatIndicator(snapshot){
  const econ=snapshot.economics||{}, purchases=econ.purchases_recorded||0, complete=econ.sold_with_complete_costs||0, unknown=econ.sold_with_unknown_costs||0;
  // This panel counted nothing as an invitation to act. On a deployment that cannot
  // record a trade, "No purchases recorded — start here" with a button pointing at
  // a form that refuses is a promise the deployment cannot keep, and it reads as a
  // measurement of the reader's own inactivity rather than of their trades.
  const writable=canWrite(capabilities,'/api/ledger');
  $('moat-indicator').hidden=false;
  if(purchases===0){
    $('moat-badge').textContent=writable?'No purchases recorded — start here':'No purchases recorded in this workspace';
    $('moat-badge').className='badge amber';
    setHTML('moat-detail','<div class="metric-list">'+metric('Purchases recorded','0')+metric('Sales with complete costs','0')+'</div>'+
      (writable
        ? '<p class="note"><strong>The buy-side moat is empty.</strong> Every trade you record compounds into the only verified realized-price dataset in this market. Use the Trade ledger to record your first purchase.</p><button class="btn primary" data-go="ledger" type="button" style="margin-top:12px">Record a purchase →</button>'
        : '<p class="note"><strong>The buy-side moat is empty.</strong> Trades are recorded from the local research runtime, which this workspace does not run. The figure above is the last runtime export, not a measure of trades you have not made.</p>'));
  }else if(complete===0){
    $('moat-badge').textContent=num(purchases)+' purchases · 0 complete-cost sales';
    $('moat-badge').className='badge amber';
    setHTML('moat-detail','<div class="metric-list">'+metric('Purchases recorded',num(purchases))+metric('Sales with complete costs','0')+'</div>'+
      (writable
        ? '<p class="note">You have purchases but no sales with all costs known. Record a sale with every cost to establish your first margin.</p><button class="btn" data-go="ledger" type="button" style="margin-top:12px">Record a sale →</button>'
        : '<p class="note">You have purchases but no sales with all costs known. Recording a sale needs the local research runtime.</p>'));
  }else{
    $('moat-badge').textContent=num(purchases)+' purchases · '+num(complete)+' complete-cost sales';
    $('moat-badge').className='badge green';
    const margin=econ.median_gross_margin_eur;
    setHTML('moat-detail','<div class="metric-list">'+metric('Purchases recorded',num(purchases))+metric('Sales with complete costs',num(complete))+metric('Median gross margin',margin==null?'—':eur(margin))+metric('Sales missing costs',num(unknown))+'</div><p class="note">Your moat is building. '+num(complete)+' complete-cost trades establish realized profit. Exact totals remain visible; medians need '+num(lastSnapshot?.panels?.national?.min_n_threshold||30)+' complete sales.</p>');
  }
}
function renderSnapshot(snapshot){
  lastSnapshot=snapshot;
  drawSegments(snapshot.segments||{});drawSummary(snapshot);drawPanels(snapshot.panels);
  if(lastFocus)drawFocus();drawQuality(snapshot.quality);drawLiquidity(snapshot.liquidity);drawHealth(snapshot.health);drawCoverage(snapshot.coverage);drawCollection(snapshot.collection);drawHistory(snapshot.history);drawDiagnostics(snapshot);drawActions(snapshot.recent_actions||[],snapshot.health);drawLedger(snapshot);drawOverviewBanner(snapshot);drawMoatIndicator(snapshot);actionButtons();
  $('stats').setAttribute('aria-busy','false');$('market-content').setAttribute('aria-busy','false');
  $('net').className='connection';$('net').innerHTML='<span class="local-dot"></span>Updated '+new Date(snapshot.generated_at).toLocaleTimeString('fi-FI',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Helsinki'});
  $('updated-at').textContent='Last updated '+date(snapshot.generated_at);$('connection-error').hidden=true;
  if(snapshot.health.collector_running&&!monitorTimer)monitorTimer=setTimeout(monitorActions,1500);
}
let connectionsData=null,connectionBusy=false,connectionsSequence=0;
const CONNECTION_STATES={setup_required:'Configuration needed',disconnected:'Disconnected',authorization_pending:'Waiting for seller consent',consent_expired:'Consent request expired',connected:'Connected · account check pending',verified:'Account API verified',refresh_required:'Access token needs refresh',reauthorization_required:'Seller consent required again',revocation_pending:'Disconnected · remote revocation pending',configuration_changed:'Client configuration changed',storage_error:'Private storage needs review'};
function drawConnections(){
  const environment=$('connection-environment').value,row=connectionsData?.connections?.find(item=>item.environment===environment);
  if(!row)return;
  const missing=row.missing_configuration||[];
  setHTML('connection-summary',metric('Environment',esc(environment))+metric('Connection',badge(CONNECTION_STATES[row.state]||row.state,row.state==='verified'?'green':'amber'))+metric('Seller registration',row.seller_registration_completed===true?'Complete':row.seller_registration_completed===false?'Incomplete':'Not checked')+metric('Account checked',row.verified_at?date(new Date(row.verified_at*1000).toISOString()):'Not checked')+metric('Publishing',badge('Unavailable'))+(missing.length?'<p>Set '+missing.map(name=>'<code>'+esc('EBAY_'+(environment==='sandbox'?'SANDBOX_':'')+name.toUpperCase())+'</code>').join(', ')+' in your private configuration.</p>':'')+(row.last_error?'<p>'+esc(row.last_error)+'</p>':'')+(row.retry_after_at>Date.now()/1000?'<p>Platform cooldown until '+date(new Date(row.retry_after_at*1000).toISOString())+'</p>':''));
  // Every one of these goes through `writeDisabled` so the deployment's answer is
  // OR-ed in with the panel's own reason. Without it, a panel that computed its
  // controls from the last account state would hand back a live "Start account
  // connection" button on a deployment whose writes are refused -- which is exactly
  // the state a read-only workspace is in.
  $('connection-begin').disabled=writeDisabled($('connection-begin'),connectionBusy||missing.length>0||['connected','verified','refresh_required','revocation_pending','configuration_changed','storage_error'].includes(row.state));
  $('connection-verify').disabled=writeDisabled($('connection-verify'),connectionBusy||!row.capabilities?.account_status||row.retry_after_at>Date.now()/1000);
  $('connection-disconnect').disabled=writeDisabled($('connection-disconnect'),connectionBusy||!row.has_local_grant);
  $('connection-environment').disabled=writeDisabled($('connection-environment'),connectionBusy);
  $('connection-complete').disabled=writeDisabled($('connection-complete'),connectionBusy);
  $('connection-return-url').disabled=writeDisabled($('connection-return-url'),connectionBusy);
  $('connection-complete-form').hidden=row.state!=='authorization_pending';
}
async function loadConnections(){
  const sequence=++connectionsSequence;
  try{const result=await call('/api/connections');if(sequence!==connectionsSequence)return;connectionsData=result;drawConnections();}
  catch(error){if(sequence===connectionsSequence)message('connection-action-message',error.message,'error');}
}
async function connectionAction(action,params={}){
  if(connectionBusy)return;
  connectionBusy=true;drawConnections();message('connection-action-message','Processing account action…','info');
  try{
    const result=await call('/api/connections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,environment:$('connection-environment').value,...params})});
    if(result.authorization_url){
      const url=new URL(result.authorization_url);
      if(!['https://auth.sandbox.ebay.com','https://auth.ebay.com'].includes(url.origin))throw new Error('Unexpected account consent origin');
      $('connection-return-url').value='';
      $('connection-consent-link').href=url.href;$('connection-authorization').hidden=false;
    }else if(action==='complete'||action==='disconnect'){
      $('connection-return-url').value='';$('connection-authorization').hidden=true;$('connection-consent-link').removeAttribute('href');
    }
    message('connection-action-message',action==='begin'?'Consent link ready. Continue on eBay, then paste the returned URL.':action==='verify'?'Account registration status checked.':action==='disconnect'?'Local access removed and remote token revocation completed.':'Seller grant saved. Check account registration next.','success');
  }catch(error){message('connection-action-message',error.message,'error');}
  finally{connectionBusy=false;await loadConnections();drawConnections();}
}
function bindConnectionControls(){
$('connection-start-form').addEventListener('submit',event=>{event.preventDefault();connectionAction('begin');});
$('connection-complete-form').addEventListener('submit',event=>{
  event.preventDefault();
  try{const url=new URL($('connection-return-url').value);if(!['https:','http:'].includes(url.protocol))throw new Error();const code=url.searchParams.get('code'),state=url.searchParams.get('state');if(!code||!state)throw new Error();connectionAction('complete',{code,state});}
  catch{message('connection-action-message','Paste the eBay return URL containing both code and state.','error');}
});
$('connection-verify').addEventListener('click',()=>connectionAction('verify'));
$('connection-disconnect').addEventListener('click',()=>connectionAction('disconnect'));
$('connection-environment').addEventListener('change',()=>{$('connection-return-url').value='';$('connection-authorization').hidden=true;$('connection-consent-link').removeAttribute('href');message('connection-action-message','');drawConnections();});
}
function scenarioMatches(scenario,inputs){return scenario?.mode==='cash'&&scenario.status==='COMPLETE'&&inputs&&Object.entries(scenario.inputs).every(([key,value])=>inputs[key]===value);}
function scenarioContext(scenario){return {seed:scenario.seed,revision:scenario.source_revision,mode:scenario.mode,origins:scenario.origins,reused_from:scenario.reused_from,inputs_sha256:scenario.inputs_sha256};}
async function openDeskScenarios(){
  if(!deskDetail)return;
  rememberDeskScenario();const pk=deskDetail.listing_pk,saved=deskScenarios[String(pk)];
  const operating=saved._scenario?.mode==='operating';
  const inputs=operating?saved._scenario.inputs:Object.fromEntries(Object.entries(saved).filter(([key])=>!['checks','_scenario','_origins'].includes(key)));
  return scenarioWorkspace.open({listing_pk:pk},{inputs,mode:operating?'operating':'cash',origins:saved._origins||saved._scenario?.origins,reusedFrom:saved._scenario?.reused_from,onApply:evaluated=>{
    if(deskDetail?.listing_pk!==pk)throw new Error('The selected research item changed. Reopen its calculator.');
    if(evaluated.mode!=='cash')throw new Error('Price Research uses cash costs. Choose cash scope and re-enter its aggregated costs before applying. Operating copies can be used on Product opportunities.');
    for(const input of $('desk-calculator-form').querySelectorAll('input[name]'))input.value=evaluated.inputs[input.name]??'';
    $('desk-discount').value=evaluated.inputs.haircut;deskScenarios[String(pk)]={...evaluated.inputs,checks:saved.checks||{},_scenario:evaluated,_origins:evaluated.origins};drawDeskCalculator();
  }});
}

function itemEditorBusy(){return unitBusy||draftBusy||orderBusy||attemptCapture?.isBusy()||soldCapture?.isBusy()||acquisitionBusy||ownedOutcomeEditor?.isBusy()||sourceReviewEditor?.isBusy()||scenarioWorkspace?.isBusy()||$('decision-dialog').open&&$('save-decision').disabled;}
function updateItemRail(){
  const data=itemWorkspace?.data();$('item-context').hidden=!data||!['item','research','observations','journal','inventory','drafts','orders','workflow','predict','imports','opportunities'].includes(currentView);
  if(data){$('item-context-title').textContent=data.title+(data.owned?' · '+data.owned.sku:'');setHTML('item-context-actions',workspaceNavigation(data));}
  soldCapture?.context(data?.title);
}
function itemFallbackSeed(){
  if($('source-review-dialog').open&&sourceReviewEditor?.listingPk())return {listing_pk:sourceReviewEditor.listingPk()};
  if($('listing-dialog').open&&selectedListing)return {listing_pk:selectedListing.listing_pk};
  if(acquisitionDecision&&$('inventory-dialog').open)return {decision_pk:acquisitionDecision.decision_pk};
  if(selectedUnit&&currentView==='inventory')return {sku:selectedUnit.sku};
  if(selectedOrder&&currentView==='orders')return {order_id:selectedOrder.order_id};
  if(selectedDraft&&currentView==='drafts')return {draft_id:selectedDraft.draft_id};
  if(deskListing)return {listing_pk:deskListing.listing_pk};
  return null;
}
async function openItemWorkspace(seed=itemWorkspace?.seed()||itemFallbackSeed()){
  if(itemEditorBusy()){toast('Finish saving the current evidence before switching context.',true);return false;}
  if(!seed){toast('Choose a stored listing, decision or owned item first.');return false;}
  for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
  itemSeed={...seed};const ready=itemWorkspace.open(itemSeed);showView('item');return ready;
}
function itemContextUpdated(data){
  if(selectedUnit&&data.owned&&selectedUnit.sku===data.owned.sku&&selectedUnit.version!==data.owned.version)message('unit-changed','This item changed. Entered values are retained; reload the saved item before editing its facts or inspection.');
  const draft=data.drafts.find(row=>row.draft_id===selectedDraft?.draft_id);
  if(draft&&draft.version!==selectedDraft.version){draftChanged=true;message('draft-changed','This draft changed. Entered values are retained; reload the saved draft before saving or reviewing.');}
  const order=data.orders.find(row=>row.order_id===selectedOrder?.order_id);
  if(order&&order.version!==selectedOrder.version){orderChanged=true;message('order-changed','This order changed. Entered values are retained; reload the saved order before recording an action.');$('order-action-fields').disabled=true;}
}
async function itemDecision(pk){
  const report=await call('/api/research?'+new URLSearchParams({decision_pk:pk}));
  const row=report.journal.items.find(row=>row.decision_pk===Number(pk));
  if(!row)throw new Error('The selected decision is unavailable.');return row;
}
async function itemAction(action,data){
  if(itemEditorBusy())throw new Error('Finish saving the current evidence before switching editors.');
  const pk=data.source.listing?.listing_pk,sku=data.owned?.sku;
  if(action.type==='sold_capture'){showView('imports');soldCapture.context(data.title);soldCapture.focus();return;}
  if(action.type==='pilot'){showView('workflow');return;}
  if(action.type==='source_review'){if(!pk)throw new Error('The source is no longer retained.');return openSourceReview(pk,{resume:true});}
  if(action.type==='listing'){if(pk)return openListing(pk,data.source.listing);return;}
  if(action.type==='research'){
    if(!pk)throw new Error('The source is no longer retained.');
    const preserved=deskDetail?.listing_pk===pk&&deskListing?.listing_pk===pk;
    await showView('research');
    if(!preserved)await openDeskItem({listing_pk:pk,title:data.title});return;
  }
  if(action.type==='scenario')return scenarioWorkspace.open(action.id?{decision_pk:action.id}:data.seed);
  if(action.type==='decision'){showDecisionDetail(await itemDecision(action.id));return;}
  if(action.type==='attempt'){
    attemptCapture.open(await itemDecision(action.id));return;
  }
  if(action.type==='acquire'){
    if(action.id&&!data.owned&&acquisitionDecision?.decision_pk===Number(action.id)){showView('inventory');$('inventory-dialog').showModal();return;}
    const row=action.id?await itemDecision(action.id):{...data.source.listing,platform:data.source.platform,source_listing_id:data.source.listing_id,title:data.title};
    return beginOwnedAcquisition(row);
  }
  if(['owned','outcome'].includes(action.type)){
    if(!sku)throw new Error('No physical SKU is recorded for this item.');
    showView('inventory');
    if(selectedUnit?.sku!==sku||$('unit-detail').hidden)await openUnit(sku);
    if(selectedUnit?.sku!==sku)return;
    unitStepShow(action.type==='outcome'?'outcomes':action.panel||'facts');
    if(action.type==='outcome')await loadOwnedOutcomes({panel:action.panel||'review'});return;
  }
  if(action.type==='draft'){
    showView('drafts');if(selectedDraft?.draft_id!==action.id||$('draft-detail').hidden)await openDraft(action.id);return;
  }
  if(action.type==='new_draft'){
    if(!sku)throw new Error('No physical SKU is recorded for this item.');
    showView('drafts');await loadDrafts();
    if(!selectedDraft&&$('draft-sku').value===sku&&!$('draft-detail').hidden)return;
    if(!draftData.units.some(row=>row.sku===sku))throw new Error('This SKU is unavailable for new listing preparation. Review its owned state first.');
    newDraft(sku);return;
  }
  if(action.type==='order'){
    showView('orders');if(selectedOrder?.order_id!==action.id||$('order-detail').hidden)await openOrder(action.id);return;
  }
  if(action.type==='sale'){
    if(!sku)throw new Error('No physical SKU is recorded for this item.');
    showView('orders');await loadOrders();
    if(!ordersData.units.some(row=>row.sku===sku)){
      if(data.orders[0]){if(selectedOrder?.order_id!==data.orders[0].order_id)await openOrder(data.orders[0].order_id);return;}
      throw new Error('This SKU is unavailable for a sale. Review its inspection and stock state first.');
    }
    if($('new-order-form').elements.sku.value!==sku)$('new-order-form').reset();
    $('new-order').click();$('new-order-form').elements.sku.value=sku;return;
  }
}

function refreshVisibleData(){
if(currentView==='item')itemWorkspace?.refresh();
if(currentView==='observations')loadListings();
if(currentView==='research'){loadDesk({preserveDetail:true});loadDeskSessions();}
if(currentView==='analyst')loadAnalysis();
if(currentView==='opportunities')loadOpportunities();
  if(currentView==='journal')loadResearch();
  if(currentView==='focus')loadFocus();
  if(currentView==='inventory')loadInventory();
  if(currentView==='drafts')loadDrafts();
  if(currentView==='orders')loadOrders();
  if(currentView==='workflow')loadWorkflow();
  if(currentView==='connections')loadConnections();
  if(currentView==='predict')loadPredict();
  if(currentView==='operations')loadOperations();
  if(currentView==='imports')loadImports();
  // The provenance panel is a reader of stored evidence that the collector keeps
  // rewriting, so it re-reads on the same pushes that redraw the views. The
  // server sends an update only when something actually moved, and the panel
  // compares revisions, so an idle workspace costs one cheap request, not a
  // re-render.
  revalidateProvenance();
}
function connectLiveUpdates(){
  if(!window.EventSource||document.hidden||document.documentElement.dataset.liveStreams==='0')return;
  const key=new URLSearchParams(FILTERS).toString();
  if(liveSource&&liveFilterKey===key)return;
  if(liveSource)liveSource.close();
  liveConnected=false;liveFilterKey=key;$('sse-indicator').hidden=true;
  const source=new EventSource('/events?'+key);liveSource=source;
  source.addEventListener('update',event=>{
    if(source!==liveSource||key!==new URLSearchParams(FILTERS).toString()||document.hidden)return;
    try{
      const snapshot=JSON.parse(event.data);
      liveConnected=true;liveLastUpdateAt=Date.now();$('sse-indicator').hidden=false;
      if(!snapshotController){renderSnapshot(snapshot);refreshVisibleData();}
    }catch{liveConnected=false;$('sse-indicator').hidden=true;}
  });
  source.onerror=()=>{if(source===liveSource){liveConnected=false;$('sse-indicator').hidden=true;}};
  // The server only sends a full snapshot when the evidence actually moved, and
  // a bare `keepalive` while nothing has. Without this the staleness guard in
  // scheduleRefresh would read a healthy idle stream as a stalled one and fall
  // back to polling -- reintroducing the full snapshot every 30s by the back
  // door, which is exactly what the keepalive exists to avoid.
  source.addEventListener('keepalive',()=>{
    if(source!==liveSource||key!==new URLSearchParams(FILTERS).toString()||document.hidden)return;
    liveConnected=true;liveLastUpdateAt=Date.now();$('sse-indicator').hidden=false;
  });
}
function scheduleRefresh(){
  clearTimeout(refreshTimer);
  refreshTimer=setTimeout(()=>{
    // Each of these is a request that can only ever succeed on a deployment that
    // runs the runtime. Issuing them against one that cannot is not a harmless
    // retry: the collection panel polls twice a second, so an idle tab pointed at
    // a view it cannot render spends a function invocation per poll to be told no.
    if(!document.hidden&&canWrite(capabilities,'/api/action'))refreshCollectionMonitor();
    if(!document.hidden&&currentView==='connections'&&!connectionBusy
      &&VIEW_READS.connections.every(path=>capabilities.reads.has(path)))loadConnections();
    if(liveConnected&&Date.now()-liveLastUpdateAt>Math.max(REFRESH_MS*2,60000)){
      liveConnected=false;$('sse-indicator').hidden=true;
    }
    // The provenance panel's "checked X ago" is drawn from this cycle rather
    // than from a second timer, so it cannot outlive the dialog it belongs to.
    if($('provenance-dialog').open)drawProvenanceStamp();
    if(!document.hidden&&!liveConnected)refresh();else scheduleRefresh();
  },REFRESH_MS);
}
async function refresh(){
  clearTimeout(refreshTimer);if(snapshotController)snapshotController.abort();
  snapshotController=new AbortController();const sequence=++snapshotSequence;
  connectLiveUpdates();
  $('net').className='connection busy';$('net').textContent=lastSnapshot?'Updating local data':'Loading local data';$('refresh-button').disabled=true;
  try{
    const snapshot=await call('/api/snapshot?'+new URLSearchParams(FILTERS),{signal:snapshotController.signal});
    if(sequence!==snapshotSequence)return;
    renderSnapshot(snapshot);refreshVisibleData();
  }catch(error){
    if(error.name==='AbortError'||sequence!==snapshotSequence)return;
    $('net').className='connection error';$('net').textContent='Connection interrupted';
    message('connection-error',(lastSnapshot?'Showing the last successful snapshot. ':'')+error.message+' Use Refresh to try again.','error');
  }finally{if(sequence===snapshotSequence){snapshotController=null;$('refresh-button').disabled=false;scheduleRefresh();}}
}
function availability(row){
  if(row.outcome_status==='sold_confirmed')return badge('Sold · verified','green');
  if(row.outcome_status==='sold_inferred')return badge('Sold · declared','green');
  if(row.is_reserved)return badge('Reserved','amber');
  if(row.outcome_status==='expired')return badge('Expired');
  if(row.outcome_status==='withdrawn'||row.status_norm==='inactive')return badge('Unavailable');
  if(row.is_giveaway)return badge('Giveaway','purple');
  return badge(row.status_norm==='active'?'Available':'Unknown',row.status_norm==='active'?'green':'');
}
async function loadListings(){
  if(currentView!=='observations')return;
  if(listingsController)listingsController.abort();listingsController=new AbortController();
  const sequence=++listingsSequence;
  $('listings-content').setAttribute('aria-busy','true');$('prev-page').disabled=true;$('next-page').disabled=true;
  const params=new URLSearchParams({...FILTERS,search:$('listing-search').value,sort:$('listing-sort').value,availability:$('listing-availability').value,page:listingPage,page_size:25});
  try{
    const result=await call('/api/listings?'+params,{signal:listingsController.signal});if(sequence!==listingsSequence)return;
    listingRows=result.items;listingPage=result.page;listingPages=result.pages;
    setHTML('listings',listingRows.length?listingRows.map(row=>'<tr><td><button class="row-link observation-title" data-listing="'+row.listing_pk+'" type="button" title="'+esc(row.title||'Untitled listing')+'">'+esc(row.title||'Untitled listing')+'</button><div class="observation-meta">#'+esc(row.listing_id)+' · '+esc(LABELS[row.subplatform]||row.subplatform)+'</div></td><td style="max-width:180px">'+esc(row.category_root_norm||'Unlabelled')+'</td><td class="muted">'+esc(row.municipality_norm||'Unknown')+'</td><td class="num">'+(row.is_giveaway?'<span class="muted">Free</span>':eur(row.listed_price_eur))+'</td><td>'+availability(row)+'</td><td class="num muted">'+date(row.last_seen_at)+'</td><td><button class="btn quiet small" data-buy="'+row.listing_pk+'" type="button" aria-label="Record purchase of '+esc(row.title||row.listing_id)+'">'+icon('plus',15)+'</button></td></tr>').join(''):'<tr><td colspan="7">'+empty('No matching observations','Try another search, reset the filters, or collect more listings.','search')+'</td></tr>');
    $('listings').querySelectorAll('[data-buy]').forEach(button=>{button.disabled=!canWrite(capabilities,'/api/ledger');});
    $('listings').querySelectorAll('[data-listing]').forEach(button=>{if(!capabilities.reads.has('/api/peers')){button.disabled=true;button.title='Detailed analysis is available with the live backend.';}});
    $('listing-count').textContent=result.total?num((result.page-1)*result.page_size+1)+'–'+num(Math.min(result.page*result.page_size,result.total))+' of '+num(result.total)+' observations':'0 observations';
    $('page-number').textContent='Page '+num(result.page)+' / '+num(result.pages);
    $('prev-page').disabled=result.page<=1;$('next-page').disabled=result.page>=result.pages;$('listing-error').hidden=true;
  }catch(error){if(error.name==='AbortError'||sequence!==listingsSequence)return;message('listing-error',error.message,'error');}
  finally{if(sequence===listingsSequence){$('listings-content').setAttribute('aria-busy','false');drawFilterContext();scheduleTableScroll();}}
}
let listingDetailSequence=0,listingDetailController=null,listingDetailPeers=[];
function listingSummaryHTML(row){
  return '<div class="stat-value">'+(row.is_giveaway?'Free':eur(row.listed_price_eur))+'</div><div style="margin:10px 0 22px">'+availability(row)+'</div><div class="metric-list">'+metric('Source ID',esc(row.listing_id))+metric('Market',esc(LABELS[row.subplatform]||row.subplatform||'Unknown'))+metric('Category',esc(row.category_norm||row.category_root_norm||'Unlabelled'))+metric('Municipality',esc(row.municipality_norm||'Unknown'))+metric('Condition',esc(row.condition_label_raw||LABELS[row.condition_norm]||'Unknown'))+metric('First observed',date(row.first_seen_at))+metric('Last observed',date(row.last_seen_at))+metric('Last status-check attempt',date(row.last_probed_at))+'</div><div class="section-divider"></div><div class="unit-actions"><button type="button" class="btn small" data-open-model="sale_probability" data-open-listing="'+esc(row.listing_pk)+'" data-model-category="'+esc(row.category_norm||'')+'">Sale forecasts</button><button type="button" class="btn small" data-open-model="price_trajectory" data-open-listing="'+esc(row.listing_pk)+'" data-model-category="'+esc(row.category_norm||'')+'">Price paths</button></div>';
}
function listingPeersHTML(e){
  const p=e.peer_prices,missing=(e.missing_identity||[]).map(key=>key==='brand_norm'?'brand':'model');
  return '<h3>Comparable asking prices</h3><p class="note">'+esc(e.basis)+'</p><div class="metric-list" style="margin-top:14px">'+metric('Product type',esc(e.product_type||'Unknown')+(e.product_type?' · derived classification':''))+metric('Recorded brand / model',esc(e.brand_norm||'Unknown')+' / '+esc(e.model_norm||'Unknown'))+metric('Other fresh priced peers',num(p.n_priced)+' / '+num(e.min_n)+' required')+metric('Peer median ask',p.status==='OK'?eur(p.median_eur):'Insufficient evidence')+metric('95% CI',p.status==='OK'&&p.median_ci95?eur(p.median_ci95.lo)+'–'+eur(p.median_ci95.hi):'—')+metric('P25–P75',p.status==='OK'?eur(p.p25_eur)+'–'+eur(p.p75_eur):'—')+metric('Price last observed',date(e.price_observed_at))+'</div>'
    +'<p class="note" style="margin-top:14px">'+(missing.length?'Missing '+esc(missing.join(' and '))+': peers may match unknown identity values. ':'')+'Verify the exact variant, defects, included items and delivery. The selected listing is excluded from its peers. Asking prices do not establish sale proceeds or profit.</p>'
    +(!e.fresh_available?'<p class="note">This listing’s current availability or freshness needs checking.</p>':'')
    +(e.peer_items?.length?'<div class="listing-peer-list">'+e.peer_items.map(peer=>'<div class="listing-peer-item"><button type="button" class="row-link" data-peer-listing="'+esc(peer.listing_pk)+'">'+esc(peer.title||'Listing #'+peer.listing_id)+'</button><strong>'+eur(peer.listed_price_eur)+'</strong></div>').join('')+'</div><p class="note" style="margin-top:10px">Showing '+num(e.peer_items.length)+' of '+num(p.n_priced)+' fresh priced peers, ordered by asking price.</p>':'');
}
function researchReadParams(row){
  const params=new URLSearchParams(filtersToQuery(row.effective_filters||FILTERS));
  params.set('platform',row.platform);params.set('listing_id',row.listing_id);
  if(row.subplatform)params.set('subplatform',row.subplatform);
  if(row.reference_at)params.set('reference_at',row.reference_at);
  return params;
}
async function openListing(pk,listing=null){
  const row=listing||listingRows.find(row=>row.listing_pk===Number(pk));if(!row)return;
  selectedListing=row;listingDetailPeers=[];
  if(listingDetailController)listingDetailController.abort();listingDetailController=new AbortController();
  const sequence=++listingDetailSequence;
  $('listing-dialog-title').textContent=row.title||'Untitled listing';
  setHTML('listing-dialog-content','<div id="listing-summary">'+listingSummaryHTML(row)+'</div><section id="listing-peers" class="listing-peers" aria-live="polite" aria-busy="true"><p class="note">Reading comparable-item evidence…</p></section>');
  if(!$('listing-dialog').open)$('listing-dialog').showModal();
  try{
    const result=await call('/api/research/listing?'+researchReadParams(row),{signal:listingDetailController.signal});
    if(sequence!==listingDetailSequence||!$('listing-dialog').open)return;
    selectedListing={...row,...result.listing};listingDetailPeers=result.evidence.peer_items||[];
    $('listing-dialog-title').textContent=selectedListing.title||'Untitled listing';
    setHTML('listing-summary',listingSummaryHTML(selectedListing));setHTML('listing-peers',listingPeersHTML(result.evidence));
  }catch(error){if(error.name!=='AbortError'&&sequence===listingDetailSequence&&$('listing-dialog').open)setHTML('listing-peers','<p class="note">Comparable-item evidence could not be loaded. '+esc(error.message)+'</p>');}
  finally{if(sequence===listingDetailSequence)$('listing-peers').setAttribute('aria-busy','false');}
}
async function openProvenance(row,revalidate=false){
  if(!row)return;
  if(!revalidate)provenanceListing=row;
  provenancePeers=revalidate?provenancePeers:[];
  if(provenanceController)provenanceController.abort();provenanceController=new AbortController();
  const sequence=++provenanceSequence;
  if(!revalidate){
    $('provenance-title').textContent=row.title||'Listing #'+row.listing_id;
    setHTML('provenance-content','<p class="note">Reading stored observations, price measurements and classification records…</p>');
    $('provenance-content').setAttribute('aria-busy','true');
    if(!$('provenance-dialog').open)$('provenance-dialog').showModal();
    drawProvenanceStamp();
  }
  try{
    const result=await call('/api/research/provenance?'+researchReadParams(row),{signal:provenanceController.signal});
    if(sequence!==provenanceSequence||!$('provenance-dialog').open)return;
    // The panel is re-read on every server push while it is open, because the
    // collector keeps writing. A payload whose revision is unchanged moved no
    // number, so re-rendering it would only throw away the reader's scroll and
    // selection for markup that is byte-identical.
    const unchanged=provenanceRevision===result.revision;
    provenanceRevision=result.revision;
    provenanceListing={...row,...result.listing};provenancePeers=result.peers?.samples||[];
    $('provenance-title').textContent=provenanceListing.title||'Listing #'+provenanceListing.listing_id;
    if(unchanged){provenanceCheckedAt=Date.now();drawProvenanceStamp();return;}
    provenanceChangedAt=Date.now();provenanceCheckedAt=Date.now();
    setHTML('provenance-content',provenanceView(result));
  }catch(error){if(error.name!=='AbortError'&&sequence===provenanceSequence&&$('provenance-dialog').open)setHTML('provenance-content','<p class="note">Provenance could not be read. '+esc(error.message)+'</p>');}
  finally{if(sequence===provenanceSequence)$('provenance-content').setAttribute('aria-busy','false');}
}
function drawProvenanceStamp(){
  const stamp=$('provenance-content').querySelector('[data-provenance-revision]');
  if(!stamp)return;
  const age=Math.round((Date.now()-provenanceCheckedAt)/1000);
  const live=age<=Math.max(REFRESH_MS,15000)*2/1000;
  stamp.textContent='Checked '+ago(Math.round(age/60))+' · revision '+stamp.dataset.provenanceRevision+
    (provenanceChangedAt&&Date.now()-provenanceChangedAt<12000?' · updated just now':'');
  // When the evidence itself was captured, as distinct from when this panel was
  // last read. Both are shown because they answer different questions: "checked
  // 4 min ago" is about this page, "read 3 h ago" is about the data behind it,
  // and a panel that conflates them reads as current when the evidence is not.
  const captured=Date.parse(stamp.dataset.provenanceCaptured||'');
  const capturedStale=Number.isFinite(captured)&&Math.max(0,Date.now()-captured)>3600000;
  if(Number.isFinite(captured))stamp.textContent+=' · evidence '+ago(Math.round(Math.max(0,Date.now()-captured)/60000));
  stamp.classList.toggle('is-stale',!live||capturedStale);
  stamp.title=live?'Re-read whenever stored evidence changes':'No stored evidence has changed for a while; this panel is current as of the last check.';
}
function revalidateProvenance(){
  if(!$('provenance-dialog').open||!provenanceListing)return;
  openProvenance(provenanceListing,true);
}
function openProvenanceCandidate(index){
  const row=lastAnalysis?.candidates[Number(index)];
  if(row)openProvenance(row);
}
function openProvenanceCandidatePeer(pk){
  const peer=provenancePeers.find(row=>row.listing_pk===Number(pk));
  if(peer){$('provenance-dialog').close();openListing(peer.listing_pk,peer);}
}
// Recording an acquisition goes through `beginOwnedAcquisition`, which asks the
// server for a prefill rather than copying fields into the form here. The
// client-side version that used to sit at this point set six fields by name and
// so silently drifted whenever the ledger form grew one; the server owns the
// shape now and the form is populated from a response instead.
async function startAction(action){
  const budget=$('budget-'+action),params={};
  if(budget){if(!budget.reportValidity())return;params.budget=Number(budget.value);}
  if(action==='sweep'){
    const pages=$('pages-sweep');
    if(pages&&pages.value!==''&&pages.reportValidity())params.pages=Number(pages.value);
    if($('opt-sweep-regions')&&$('opt-sweep-regions').checked)params.include_regions=true;
    if($('opt-sweep-deepest')&&$('opt-sweep-deepest').checked)params.deepest_first=true;
    if($('opt-sweep-dry')&&$('opt-sweep-dry').checked)params.dry_run=true;
  }
  const dry=!!params.dry_run;
  localRuns.set(action,{started_at:null});actionButtons();
  message('action-message',dry?'Preparing a dry run…':'Starting '+ACTION_LABELS[action].toLowerCase()+'…');
  try{
    const result=await call('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});
    localRuns.set(action,{started_at:result.started_at});
    message('action-message',dry?'Dry run is running. It prints the queue and fetches nothing. ':ACTION_LABELS[action]+' is running. '+(dry?'':'You can continue exploring stored data.'));
    if(!monitorTimer)monitorTimer=setTimeout(monitorActions,700);
  }catch(error){localRuns.delete(action);actionButtons();message('action-message',error.message,'error');toast(error.message,true);}
}
// stdout_tail keeps only the last 1500 bytes, so a large run report is not
// parseable JSON. Parse what parses; a dry run is still recognisable in a tail.
function runOutput(text){try{return JSON.parse(text);}catch{return null;}}
function sweepOutcome(output,text){
  const dry=output?!!output.dry_run:/"dry_run":\s*true/.test(text||'');
  if(dry)return 'Dry run finished'+(output&&output.targets!=null?': '+num(output.targets)+' targets queued and '+num((output.next||[]).length)+' next URLs previewed':'')+'. Nothing was fetched.';
  if(!output)return null;
  return num(output.new)+' new listings collected from '+num(output.requests)+' search pages.';
}
async function monitorActions(){
  monitorTimer=null;
  try{
    const health=await call('/api/health');
    if(lastSnapshot){lastSnapshot.health=health;drawHealth(health);drawActions(lastSnapshot.recent_actions||[],health);}
    const active=new Set((health.running_actions||[]).map(row=>row.action)),finished=[...localRuns.entries()].filter(([action,run])=>run.started_at&&!active.has(action));
    finished.forEach(([action])=>localRuns.delete(action));actionButtons();
    if(finished.length){
      await refresh();
      for(const [action,run] of finished){
        const result=lastSnapshot?.recent_actions.find(row=>row.action===action&&row.started_at===run.started_at);
        if(result){
          let description=ACTION_LABELS[action]+(result.returncode===0?' completed.':' failed.');
          const output=runOutput(result.stdout_tail||'');
          if(action==='sweep'&&result.returncode===0)description=sweepOutcome(output,result.stdout_tail||'')||description;
          else if(output&&output.error)description=output.error;
          else if(output&&output.status==='skipped')description=output.reason||'No work was needed.';
          if(result.returncode!==0&&result.stderr_tail)description+=' '+result.stderr_tail.slice(-200);
          message('action-message',description,result.returncode===0?'success':'error');toast(description,result.returncode!==0);
        }
      }
    }else if(!health.collector_running)refresh();
    if(health.collector_running||localRuns.size)monitorTimer=setTimeout(monitorActions,1500);
  }catch(error){message('action-message','Could not check the run status. '+error.message,'error');if(localRuns.size)monitorTimer=setTimeout(monitorActions,4000);}
}
async function saveLedger(event,action){
  event.preventDefault();
  const form=event.target,button=form.querySelector('button[type="submit"]'),values=Object.fromEntries(new FormData(form)),params={};
  if(action==='sell'){
    const purchase=ledgerBuys.find(row=>String(row.purchase_pk)===values.purchase_pk);if(!purchase){message('ledger-message','Choose a recorded purchase first.','error');return;}
    params.platform=purchase.platform;params.source_listing_id=purchase.source_listing_id;delete values.purchase_pk;
  }
  for(const [key,value] of Object.entries(values))if(value!=='')params[key]=key.endsWith('_eur')?Number(value):value;
  button.disabled=true;
  try{
    const result=await call('/api/ledger',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});
    const text=action==='buy'?'Purchase saved.':result.result.margin?'Sale saved. Gross margin: '+eur(result.result.margin.gross_margin_eur)+'.':'Sale saved. Margin is pending until all costs are known.';
    form.reset();message('ledger-message',text,'success');toast(text);await refresh();
  }catch(error){message('ledger-message',error.message,'error');toast(error.message,true);}
  finally{button.disabled=false;}
}
function updateTableScroll(){
  document.querySelectorAll('.table-wrap').forEach(wrap=>{
    const overflow=wrap.clientWidth>0&&wrap.scrollWidth>wrap.clientWidth+2;
    wrap.classList.toggle('is-scrollable',overflow);
    if(overflow){
      const card=wrap.closest('.card'),name=card?.querySelector('h2')?.textContent||card?.getAttribute('aria-label')||'Data table';
      wrap.setAttribute('tabindex','0');wrap.setAttribute('role','region');wrap.setAttribute('aria-label',name+'. Scroll horizontally for more columns.');
    }else{wrap.removeAttribute('tabindex');wrap.removeAttribute('role');wrap.removeAttribute('aria-label');}
  });
}
function scheduleTableScroll(){
  if(tableLayoutPending||typeof requestAnimationFrame!=='function')return;
  tableLayoutPending=true;requestAnimationFrame(()=>{tableLayoutPending=false;updateTableScroll();});
}
// Views rebuild their table markup after this module has already run its first
// layout pass, so they ask for one instead of waiting for the next route change.
document.addEventListener?.('tori:table-layout',scheduleTableScroll);
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>showView(button.dataset.view));
collectionPanel=initCollectionPanel({onListing:pk=>openListing(pk)});
document.addEventListener('click',event=>{const button=event.target.closest('[data-peer-listing]');if(button){const peer=listingDetailPeers.find(row=>row.listing_pk===Number(button.dataset.peerListing));if(peer)openListing(peer.listing_pk,peer);}});
document.addEventListener('click',event=>{const button=event.target.closest('[data-provenance-peer]');if(button)openProvenanceCandidatePeer(button.dataset.provenancePeer);});
document.addEventListener('click',event=>{const button=event.target.closest('[data-ledger-order]');if(button){if($('journal-detail-dialog').open)$('journal-detail-dialog').close();showView('orders');openOrder(button.dataset.ledgerOrder);}});
bindConnectionControls();
bindDrafts();
bindOrders();
bindWorkflow();
bindOperations();
bindImports();
bindPredict();
document.querySelectorAll('[data-filter]').forEach(select=>select.onchange=()=>{$('market-content').setAttribute('aria-busy','true');$('stats').setAttribute('aria-busy','true');applyFilter(select.dataset.filter,select.value);});
document.querySelectorAll('.budget-row input').forEach(input=>input.addEventListener('input',()=>{input.dataset.edited='1';}));
$('more-filters').onclick=()=>{const open=$('advanced-filters').hidden;$('advanced-filters').hidden=!open;$('more-filters').setAttribute('aria-expanded',String(open));};
$('reset-filters').onclick=resetResearchFilters;
$('refresh-button').onclick=refresh;
$('focus-badge').onclick=()=>showView('focus');
$('heading-action').onclick=()=>{if(currentView==='inventory')newOwnedUnit();else if(currentView==='ledger')$('buy-form').elements.title.focus();else if(currentView==='journal')openDecision();else if(currentView==='research'){loadDesk();loadDeskSessions();}else showView(currentView==='analyst'||currentView==='focus'?'observations':'collection');};
$('save-segment').onclick=async()=>{const name=($('segment-name').value.trim()||FILTERS.category_norm||FILTERS.category_root_norm||FILTERS.municipality_norm||'All '+(LABELS[FILTERS.subplatform]||'markets')).slice(0,60);$('save-segment').disabled=true;try{const result=await researchAction('save_segment',{name,filters:{...FILTERS}});savedSegments=result.segments;drawSavedSegments();$('segment-name').value='';toast('Segment saved in your local research database.');}catch(error){toast(error.message,true);}finally{$('save-segment').disabled=!researchReady;}};
$('compare-button').onclick=compareSegments;
$('explain-segment').onclick=startExplanation;
$('cancel-explanation').onclick=cancelExplanation;
$('refresh-analyst-checks').onclick=loadAnalystQuality;
$('export-analyst-checks').onclick=exportAnalystQuality;
$('close-analyst-evidence').onclick=()=>$('analyst-evidence-dialog').close();
$('analyst-history').onchange=()=>{const id=$('analyst-history').value;if(id){const url=new URL(location.href);url.searchParams.set('research_run',id);history.replaceState(null,'',url);loadAnalystRun(id);}};
$('journal-filter').onchange=()=>{journalPage=1;loadResearch();};
$('journal-prev').onclick=()=>{if(journalPage>1){journalPage--;loadResearch();}};
$('journal-next').onclick=()=>{if(journalPage<journalPages){journalPage++;loadResearch();}};
$('export-journal').onclick=exportJournal;
$('close-decision').onclick=()=>$('decision-dialog').close();
$('close-journal-detail').onclick=()=>$('journal-detail-dialog').close();
$('load-decision-listing').onclick=loadDecisionListing;
$('decision-include-scenario').onchange=decisionScenario;
$('decision-form').onsubmit=saveDecision;
$('decision-platform').onchange=$('decision-listing-id').oninput=()=>{decisionListing=null;};
for(const id of Object.keys(analysisDefaults))$(id).onchange=()=>{updateAnalysisURL();loadAnalysis();};
$('matrix-sort').onchange=drawMatrix;$('matrix-search').oninput=drawMatrix;
$('export-analysis').onclick=exportAnalysis;
$('export-focus').onclick=exportFocus;
$('focus-layout').onchange=drawFocus;
$('focus-sort').onchange=drawFocus;
const rememberedFocusHorizon=new URLSearchParams(location.search).get('focus_horizon');
if(['7','14','30','60','90'].includes(rememberedFocusHorizon))$('focus-horizon').value=rememberedFocusHorizon;
$('focus-horizon').onchange=()=>{const params=new URLSearchParams(location.search);params.set('focus_horizon',$('focus-horizon').value);history.replaceState(null,'',location.pathname+'?'+params.toString()+'#'+currentView);loadFocus();};
$('calculator-form').oninput=drawCalculator;$('calculator-form').onsubmit=event=>event.preventDefault();
$('mobile-menu').onclick=()=>{$('sidebar').classList.contains('is-open')?closeNavigation(true):openNavigation();};
$('close-navigation').onclick=$('scrim').onclick=()=>closeNavigation(true);
$('listing-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{listingPage=1;loadListings();},250);};
$('desk-refresh').onclick=()=>loadDesk();
for(const id of Object.keys(deskControls))$(id).onchange=()=>{syncDeskURL();loadDesk();loadDeskSessions();};
$('desk-export').onclick=exportDesk;
$('desk-export-markdown').onclick=downloadDeskReport;
$('desk-sort').onchange=drawDesk;
$('desk-compare-selected').onclick=()=>{$('desk-compare-panel').scrollIntoView({behavior:'smooth',block:'nearest'});};
$('desk-compare-markdown').onclick=downloadDeskReport;
$('desk-compare-close').onclick=()=>$('desk-compare-panel').hidden=true;
$('desk-save-session').onclick=saveDeskSession;
$('desk-select-all').onclick=$('desk-inventory-select-all').onclick=()=>{const rows=[...(deskReport?.candidates||[]),...(deskReport?.research_inventory?.items||[])];deskSelection=new Set(rows.slice(0,50).map(row=>row.listing_pk));if(rows.length>50)toast('Selected the first 50 listings, the saved session limit.');drawDesk();};
$('desk-inventory-save').onclick=()=>{$('desk-session-library').open=true;$('desk-session-name').focus();$('desk-session-library').scrollIntoView({behavior:'smooth',block:'start'});};
$('desk-clear-selection').onclick=()=>{deskSelection=new Set();drawDesk();};
$('desk-close-detail').onclick=closeDeskDetail;
$('desk-new-session').onclick=()=>{closeDeskDetail();deskSelection=new Set();deskScenarios={};$('desk-session-name').value='';$('desk-session-notes').value='';drawDesk();};
$('desk-calculator-form').oninput=event=>{if(deskDetail){const key=String(deskDetail.listing_pk),draft=deskScenarios[key]||(deskScenarios[key]={});draft._origins={...draft._origins,[event.target.name]:'operator_assumption'};rememberDeskScenario();}drawDeskCalculator();};
$('desk-calculator-form').onsubmit=event=>event.preventDefault();
$('desk-discount').oninput=()=>{if(deskDetail){const key=String(deskDetail.listing_pk),draft=deskScenarios[key]||(deskScenarios[key]={});draft._origins={...draft._origins,haircut:'operator_assumption'};rememberDeskScenario();}drawDeskCalculator();};
$('desk-proceeds').onclick=loadDeskProceeds;
$('desk-scenarios').onclick=openDeskScenarios;
$('desk-decide').onclick=()=>{
  if(!deskDetail)return;
  const prior=deskDetail.decisions;
  if(prior?.n)toast(prior.disagreeing_with_latest
    ?'You recorded '+num(prior.n)+' decisions on this item, '+num(prior.disagreeing_with_latest)+' with a different outcome. This adds a new one.'
    :'You recorded '+num(prior.n)+' decision'+(prior.n===1?'':'s')+' on this item. This adds a new one.');
  const haircut=Number($('desk-discount').value),values=deskAssumptions();
  // The desk's scenario is handed over explicitly rather than through the
  // analyst form, so a decision records the assumptions the operator actually
  // typed on this page rather than whatever is left in another card's inputs.
  const evaluated=deskScenarios[String(deskDetail.listing_pk)]?._scenario;
  const inputs=values?{...values,haircut}:null;
  if(evaluated&&!scenarioMatches(evaluated,inputs)){toast('These saved assumptions changed. Evaluate them in Profiles & reusable scenarios before attaching a new decision.',true);return;}
  openDecision(deskDetail.listing,true,inputs,evaluated?scenarioContext(evaluated):null);
  const checks=deskScenarios[String(deskDetail.listing_pk)]?.checks||{};
  for(const [name,key] of Object.entries({model:'identity',condition:'condition',completeness:'completeness',delivery:'delivery'}))$('decision-form').elements[name].checked=checks[key]===true;
};
$('listing-sort').onchange=$('listing-availability').onchange=()=>{listingPage=1;loadListings();};
$('prev-page').onclick=()=>{if(listingPage>1){listingPage--;loadListings();}};
$('next-page').onclick=()=>{if(listingPage<listingPages){listingPage++;loadListings();}};
$('close-dialog').onclick=()=>$('listing-dialog').close();
$('listing-dialog').addEventListener('close',()=>{listingDetailSequence++;listingDetailController?.abort();});
$('close-provenance').onclick=()=>$('provenance-dialog').close();
async function openSourceReview(pk,{resume=false}={}){
  if(!sourceReviewEditor)sourceReviewEditor=initSourceReview({dialog:$('source-review-dialog'),root:$('source-review-content'),call,onSaved:async()=>{
    rememberDeskScenario();
    if(deskLoadPromise)await deskLoadPromise;
    if(currentView==='research')await loadDesk({preserveDetail:true});
    if(currentView==='observations')await loadListings();
    if(currentView==='analyst')await loadAnalysis();
    await itemWorkspace?.refresh();
  }});
  $('listing-dialog').close();$('provenance-dialog').close();
  return resume&&sourceReviewEditor.resume(pk)?true:sourceReviewEditor.open(pk);
}
$('close-source-review').onclick=()=>{if(!sourceReviewEditor?.isBusy())$('source-review-dialog').close();};
$('reload-source-review').onclick=()=>{const pk=sourceReviewEditor?.listingPk();if(pk&&!sourceReviewEditor.isBusy())sourceReviewEditor.open(pk);};
$('dialog-source-review').onclick=()=>selectedListing&&openSourceReview(selectedListing.listing_pk);
$('provenance-source-review').onclick=()=>provenanceListing&&openSourceReview(provenanceListing.listing_pk);
$('provenance-dialog').addEventListener('close',()=>{provenanceSequence++;provenanceController?.abort();provenancePeers=[];provenanceRevision='';provenanceCheckedAt=0;provenanceChangedAt=0;});
// Re-read on focus. A dialog can sit open behind another window for a long time,
// and "when did this panel last check" has to stay true while it is open.
$('provenance-dialog').addEventListener('focus',revalidateProvenance);
$('provenance-peers').onclick=()=>{if(provenanceListing){const row=provenanceListing;$('provenance-dialog').close();openListing(row.listing_pk,row);}};
document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',event=>{if(event.target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom){if(dialog.id==='source-review-dialog'&&sourceReviewEditor?.isBusy())return;dialog.close();}}}));
$('dialog-purchase').onclick=()=>beginOwnedAcquisition(selectedListing);
$('dialog-research').onclick=()=>openDecision(selectedListing);
$('capture-calculator-decision').onclick=()=>openDecision(scenarioListing,true);
$('buy-form').onsubmit=event=>saveLedger(event,'buy');
$('sale-form').onsubmit=event=>saveLedger(event,'sell');
$('purchase-options').onchange=()=>selectSale($('purchase-options').value);
$('new-unit').onclick=()=>newOwnedUnit();
$('close-inventory-dialog').onclick=()=>$('inventory-dialog').close();
$('unit-purchase').onchange=acquisitionMode;
$('unit-source').onchange=acquisitionMode;
$('inventory-state').onchange=()=>{inventoryPage=1;loadInventory();};
$('inventory-search').oninput=()=>{clearTimeout(inventorySearchTimer);inventorySearchTimer=setTimeout(()=>{inventoryPage=1;loadInventory();},250);};
$('inventory-prev').onclick=()=>{if(inventoryPage>1){inventoryPage--;loadInventory();}};
$('inventory-next').onclick=()=>{if(inventoryData&&inventoryPage<inventoryData.pages){inventoryPage++;loadInventory();}};
$('reload-unit').onclick=()=>{if(selectedUnit)openUnit(selectedUnit.sku);};
$('close-unit').onclick=()=>{if(unitBusy||ownedOutcomeEditor?.isBusy())return;unitSequence++;unitDirty=false;ownedOutcomeEditor?.discard();selectedUnit=null;$('unit-detail').hidden=true;};
$('eligibility-channel').onchange=eligibilityChannelChoice;
$('unit-create-form').onsubmit=event=>saveInventoryForm(event,'create');
if($('reload-owned-outcomes'))$('reload-owned-outcomes').onclick=()=>loadOwnedOutcomes();
if($('preview-resale'))$('preview-resale').onclick=previewResaleEvidence;
for(const name of ['resale_listing_id','sold_on'])$('new-order-form').elements[name]?.addEventListener('input',()=>{resalePreviewSequence++;setHTML('resale-preview','<p class="inventory-help">The selection changed. Preview retained evidence again before saving.</p>');});
attemptCapture=initAttemptCapture({dialog:$('attempt-dialog'),form:$('attempt-form'),context:$('attempt-context'),call,onSaved:async()=>{await loadResearch();itemWorkspace?.refresh();}});
soldCapture=initSoldCapture({root:$('sold-guided-capture'),call,onSaved:()=>loadImports()});
$('close-attempt').onclick=()=>{if(!attemptCapture.isBusy())$('attempt-dialog').close();};
$('attempt-dialog').addEventListener('cancel',event=>{if(attemptCapture.isBusy())event.preventDefault();});
$('unit-facts-form').onsubmit=event=>saveInventoryForm(event,'update');
$('unit-inspection-form').onsubmit=event=>saveInventoryForm(event,'inspect');
$('unit-eligibility-form').onsubmit=event=>saveInventoryForm(event,'evaluate');
document.addEventListener('click',event=>{
  const sourceReview=event.target.closest('[data-source-review]');if(sourceReview){openSourceReview(Number(sourceReview.dataset.sourceReview));return;}
  const analysisResearch=event.target.closest('[data-analysis-research-open]');if(analysisResearch){const row=lastAnalysis?.research_inventory?.items?.[Number(analysisResearch.dataset.analysisResearchOpen)];if(row)openListing(row.listing_pk,{...row,listed_price_eur:row.ask_eur,effective_filters:lastAnalysis.filters,reference_at:lastAnalysis.generated_at});return;}
  const deskResearch=event.target.closest('[data-desk-research-open]');if(deskResearch){openDeskItem(deskReport?.research_inventory?.items?.[Number(deskResearch.dataset.deskResearchOpen)]);return;}
  const deskRawSelect=event.target.closest('[data-desk-select-pk]');if(deskRawSelect){toggleDeskSelection(Number(deskRawSelect.dataset.deskSelectPk));return;}
  const deskKeep=event.target.closest('[data-desk-keep]');if(deskKeep&&deskDetail){toggleDeskSelection(deskDetail.listing_pk);drawDeskDetail(deskDetail);return;}
  const deskSaved=event.target.closest('[data-desk-saved-open]');if(deskSaved){openDeskSession(deskSaved.dataset.deskSavedSession).then(()=>openDeskItem({listing_pk:Number(deskSaved.dataset.deskSavedOpen)}));return;}
  const deskSelect=event.target.closest('[data-desk-select]');if(deskSelect){const pk=deskReport?.candidates?.[Number(deskSelect.dataset.deskSelect)]?.listing_pk;if(pk!=null)toggleDeskSelection(pk);return;}
  const deskCheck=event.target.closest('[data-desk-check]');if(deskCheck&&deskDetail){const key=String(deskDetail.listing_pk);deskScenarios[key]??={};deskScenarios[key].checks??={};deskScenarios[key].checks[deskCheck.dataset.deskCheck]=deskCheck.checked;return;}
  const deskDecision=event.target.closest('[data-desk-decision]');if(deskDecision){openDeskDecision(deskDecision.dataset.deskDecision);return;}
  const deskOpen=event.target.closest('[data-desk-open],[data-desk-detail]');if(deskOpen){openDeskDetail(Number(deskOpen.dataset.deskOpen??deskOpen.dataset.deskDetail));return;}
  const internetOffer=event.target.closest('[data-internet-monitor-listing]');if(internetOffer){openDeskItem({listing_pk:Number(internetOffer.dataset.internetMonitorListing)});return;}
  const deskProvenance=event.target.closest('[data-desk-provenance]');if(deskProvenance&&deskDetail){openProvenance(deskDetail.listing);return;}
  const deskEvidence=event.target.closest('[data-desk-evidence]');if(deskEvidence&&deskDetail){openListing(deskDetail.listing.listing_pk,deskDetail.listing);return;}
  const deskOpenListing=event.target.closest('[data-desk-open-listing]');if(deskOpenListing&&deskDetail){$('listing-search').value=deskDetail.listing.listing_id;$('listing-availability').value='all';listingPage=1;showView('observations');return;}
  const deskOpenSession=event.target.closest('[data-desk-open-session]');if(deskOpenSession){openDeskSession(deskOpenSession.dataset.deskOpenSession);return;}
  const deskDeleteSession=event.target.closest('[data-desk-delete-session]');if(deskDeleteSession){deleteDeskSession(deskDeleteSession.dataset.deskDeleteSession);return;}
  const owned=event.target.closest('[data-owned-sku]');if(owned){openUnit(owned.dataset.ownedSku);return;}
  const step=event.target.closest('[data-unit-step]');if(step){unitStepShow(step.dataset.unitStep);return;}
  const clearFilter=event.target.closest('[data-clear-filter]');if(clearFilter){applyFilter(clearFilter.dataset.clearFilter,'');return;}
  const evidence=event.target.closest('[data-analyst-evidence]');if(evidence){openAnalystEvidence(evidence.dataset.analystEvidence);return;}
  const saved=event.target.closest('[data-saved]');if(saved){const row=savedSegments[Number(saved.dataset.saved)];if(!row)return;for(const key of FILTER_KEYS)delete FILTERS[key];for(const key of FILTER_KEYS)if(typeof row.filters[key]==='string'&&row.filters[key].length<=300)FILTERS[key]=row.filters[key];FILTERS.platform=FILTERS.platform||'tori';FILTERS.subplatform=FILTERS.subplatform||'standard';listingPage=1;if(analysisController)analysisController.abort();syncURL();refresh();if(currentView==='analyst')loadAnalysis();return;}
  const removeSaved=event.target.closest('[data-remove-saved]');if(removeSaved){const row=savedSegments[Number(removeSaved.dataset.removeSaved)];if(!row?.segment_pk)return;removeSaved.disabled=true;researchAction('delete_segment',{segment_pk:row.segment_pk}).then(result=>{savedSegments=result.segments;drawSavedSegments();}).catch(error=>{removeSaved.disabled=false;toast(error.message,true);});return;}
  const decisionDetail=event.target.closest('[data-decision-detail]');if(decisionDetail){journalDetail(decisionDetail.dataset.decisionDetail);return;}
  const decisionButton=event.target.closest('[data-research-listing]');if(decisionButton){openDecision(listingRows.find(row=>row.listing_pk===Number(decisionButton.dataset.researchListing)));return;}
  const focusDrillButton=event.target.closest('[data-focus-drill]');if(focusDrillButton){focusDrill(focusDrillButton.dataset.focusDrill);return;}
  const focusGotoButton=event.target.closest('[data-focus-goto]');if(focusGotoButton){focusGoto(focusGotoButton.dataset.focusGoto);return;}
  const drill=event.target.closest('[data-drill]');if(drill){applyFilter(lastAnalysis.dimension,drill.dataset.drill);if(lastAnalysis.dimension==='category_norm'){$('advanced-filters').hidden=false;$('more-filters').setAttribute('aria-expanded','true');}return;}
  const series=event.target.closest('[data-series]');if(series){activitySeries=series.dataset.series;drawActivity();return;}
  const day=event.target.closest('[data-day]');if(day){const row=lastAnalysis?.timeline.find(item=>item.date===day.dataset.day);if(row)$('activity-detail').textContent=row.date+' · '+num(row.new_observations)+' discoveries · '+num(row.sold_declared)+' declared sales · '+num(row.sold_verified)+' verified sales · '+num(row.price_reductions)+' price reductions.';return;}
  const candidate=event.target.closest('[data-model-candidate],[data-inspect-candidate]');if(candidate){const index=Number(candidate.dataset.modelCandidate??candidate.dataset.inspectCandidate),row=lastAnalysis?.candidates[index];if(!row)return;if(candidate.hasAttribute('data-model-candidate')){scenarioListing=row;$('calculator-form').elements.buy.value=row.ask_eur;drawCalculator();$('deal-calculator').scrollIntoView({behavior:'smooth',block:'start'});$('calculator-form').elements.resale.focus({preventScroll:true});}else{$('listing-search').value=row.listing_id;$('listing-availability').value='all';listingPage=1;showView('observations');}return;}
  const provenance=event.target.closest('[data-provenance-candidate]');if(provenance){openProvenanceCandidate(provenance.dataset.provenanceCandidate);return;}
  const category=event.target.closest('[data-category]');if(category){applyFilter('category_root_norm',category.dataset.category);return;}
  const municipality=event.target.closest('[data-municipality]');if(municipality){applyFilter('municipality_norm',municipality.dataset.municipality);return;}
  const go=event.target.closest('[data-go]');if(go){showView(go.dataset.go);return;}
  const action=event.target.closest('[data-action]');if(action&&!action.disabled){startAction(action.dataset.action);return;}
  const details=event.target.closest('[data-run]');if(details){const id=details.dataset.run;if(openRunDetails.has(id))openRunDetails.delete(id);else openRunDetails.add(id);drawActions(lastSnapshot.recent_actions||[],lastSnapshot.health);return;}
  const listing=event.target.closest('[data-listing]');if(listing){openListing(listing.dataset.listing);return;}
  const buy=event.target.closest('[data-buy]');if(buy){beginOwnedAcquisition(listingRows.find(row=>row.listing_pk===Number(buy.dataset.buy)));return;}
  const sell=event.target.closest('[data-sell]');if(sell){selectSale(sell.dataset.sell);$('sale-form').elements.final_sale_price_eur.focus();return;}
  const zero=event.target.closest('[data-zero]');if(zero){$(zero.dataset.zero).querySelectorAll('.optional-cost').forEach(input=>{if(input.value==='')input.value='0';});}
});
document.addEventListener('keydown',navigationKeydown);
window.addEventListener('resize',scheduleTableScroll);
if('ResizeObserver' in window){const observer=new ResizeObserver(scheduleTableScroll);document.querySelectorAll('.table-wrap').forEach(wrap=>observer.observe(wrap));}
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden){refresh();return;}
  if(liveSource)liveSource.close();liveSource=null;liveConnected=false;$('sse-indicator').hidden=true;
});
window.addEventListener('pagehide',()=>{if(liveSource)liveSource.close();});
window.addEventListener('hashchange',()=>showView(location.hash.slice(1),false));
for(const id of ['unit-facts-form','unit-inspection-form','unit-eligibility-form'])if($(id))for(const event of ['input','change'])$(id).addEventListener(event,()=>{unitDirty=true;});
for(const event of ['input','change'])$('order-action-form').addEventListener(event,()=>{orderDirty=true;});
scenarioWorkspace=initScenarioWorkspace({dialog:$('scenario-workspace-dialog'),root:$('scenario-workspace-content'),call,onReturn:seed=>openItemWorkspace(seed)});
function openForecastModel(selected){
  if(itemEditorBusy()){toast('Finish saving the current evidence before switching views.',true);return;}
  closeModelDetail();clearTimeout(predictSearchTimer);
  Object.assign(predictScope,{family:selected.family,version:selected.version,horizon:selected.horizon,category:'',region:'',search:'',page:1});
  pendingModelListing=selected.listing_pk;predictURL();drawModelTabs();showView('predict');
}
forecastReader=initForecastEvidence({dialog:$('forecast-evidence-dialog'),root:$('forecast-evidence-content'),call,onModel:openForecastModel});
document.addEventListener('click',event=>{
  const evidence=event.target.closest('[data-forecast-sources]');
  if(evidence){forecastReader.open(evidence.dataset.forecastSources.split(',').map(Number),evidence.dataset.forecastLabel||'Selected marketplace offers');return;}
  const model=event.target.closest('#item-workspace-content [data-forecast-model]');
  if(model)openForecastModel({family:model.dataset.forecastModel,version:model.dataset.forecastVersion,horizon:Number(model.dataset.forecastHorizon),listing_pk:Number(model.dataset.forecastListing)});
});
itemWorkspace=initItemWorkspace({root:$('item-workspace-content'),call,onAction:async(action,data)=>{try{return await itemAction(action,data);}catch(error){toast(error.message,true);throw error;}},onContext:data=>{
  itemSeed=data.seed;const [kind,id]=Object.entries(itemSeed)[0],params=new URLSearchParams(location.search);params.set('item_kind',kind);params.set('item_id',id);history.replaceState(null,'',location.pathname+'?'+params+'#'+currentView);updateItemRail();
},onUpdated:itemContextUpdated});
document.addEventListener('click',event=>{
  if(event.target.closest('[data-item-return]')){openItemWorkspace();return;}
  const railAction=event.target.closest('#item-context [data-item-action]');
  if(railAction&&itemWorkspace.data()){itemAction({type:railAction.dataset.itemAction},itemWorkspace.data()).catch(error=>toast(error.message,true));return;}
  if(event.target.closest('[data-guided-sold]')){if(itemEditorBusy()){toast('Finish saving the current evidence first.',true);return;}showView('imports');soldCapture.focus();return;}
  const decision=event.target.closest('[data-item-decision]');if(decision){openItemWorkspace({decision_pk:decision.dataset.itemDecision});return;}
  if(event.target.closest('[data-desk-item-workspace]')&&deskListing)openItemWorkspace({listing_pk:deskListing.listing_pk});
});
$('dialog-item-workspace').onclick=()=>selectedListing&&openItemWorkspace({listing_pk:selectedListing.listing_pk});
$('provenance-item-workspace').onclick=()=>provenanceListing&&openItemWorkspace({listing_pk:provenanceListing.listing_pk});
$('unit-item-workspace').onclick=()=>selectedUnit&&openItemWorkspace({sku:selectedUnit.sku});
$('draft-item-workspace').onclick=()=>selectedDraft&&openItemWorkspace({draft_id:selectedDraft.draft_id});
$('order-item-workspace').onclick=()=>selectedOrder&&openItemWorkspace({order_id:selectedOrder.order_id});
/**
 * The page is drawn only after the deployment has said what it can answer.
 *
 * `establishCapabilities` runs first and is awaited, so the navigation is pruned
 * and every write control is disabled before `showView` renders anything. The
 * analyst history and quality readouts are then requested only where `/api/analyst`
 * exists -- asking for them unconditionally was one of the requests that could only
 * ever refuse.
 */
async function boot(){
  try{await establishCapabilities();}
  catch(error){message('connection-error',error.message+' The full workspace was left available because the deployment could not be asked what it supports.','error');}
  drawSavedSegments();drawCalculator();showView(location.hash.slice(1)||'analyst',false);syncURL();actionButtons();refresh();
  if(currentView!=='journal')loadResearch();
  if(capabilities.reads.has('/api/analyst')){loadAnalystHistory();loadAnalystQuality();}
}
boot();
