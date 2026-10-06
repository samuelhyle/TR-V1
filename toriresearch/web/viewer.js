import {call} from '/static/api.js';
import {session, accountControls} from '/static/auth-client.js';
import {esc, eur, num, percent, date} from '/static/format.js';
import {calculateScenario} from '/static/views/research.js';

const $=id=>document.getElementById(id);
const titles={overview:'Market overview',opportunities:'Product opportunities',research:'Price research',observations:'Observations',forecasts:'Forecasts',inventory:'Owned inventory',drafts:'Listing drafts',orders:'Orders & returns',journal:'Decision journal',scenarios:'Saved scenarios',calculator:'Deal calculator'};
let current='overview',page=1,sequence=0,searchTimer,snapshot;
const user=await session('viewer');

function table(headers,rows) {
  return rows.length?'<div class="table-wrap"><table><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(value=>'<td>'+value+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>':'<p class="muted">No stored records match this view.</p>';
}
function metric(label,value,note='') {return '<article class="card"><span class="muted">'+esc(label)+'</span><div class="metric-value">'+esc(value)+'</div><p class="muted">'+esc(note)+'</p></article>';}
function filters() {
  const params=new URLSearchParams({platform:'tori',subplatform:$('market-filter').value});
  if($('category-filter').value)params.set('category_norm',$('category-filter').value);
  if($('municipality-filter').value)params.set('municipality_norm',$('municipality-filter').value);
  return params;
}
function itemButton(kind,value) {return '<button type="button" data-item-kind="'+esc(kind)+'" data-item-id="'+esc(value)+'">Read evidence</button>';}
function fields(value,depth=0) {
  if(value===null||value===undefined)return '<span class="muted">Unknown</span>';
  if(typeof value!=='object')return esc(String(value));
  if(depth>5)return '<span class="muted">Additional retained detail</span>';
  if(Array.isArray(value))return value.length?value.slice(0,60).map((row,index)=>'<details'+(index===0?' open':'')+'><summary>Record '+(index+1)+'</summary>'+fields(row,depth+1)+'</details>').join(''):'<span class="muted">None recorded</span>';
  return '<dl>'+Object.entries(value).filter(([key])=>!['actions','next_actions','can_save','can_edit','links','model_path','artifact_path','input_path'].includes(key)).map(([key,entry])=>'<dt>'+esc(key.replaceAll('_',' '))+'</dt><dd>'+fields(entry,depth+1)+'</dd>').join('')+'</dl>';
}
async function detail(kind,id) {
  const dialog=$('viewer-detail');dialog.showModal();$('viewer-detail-content').textContent='Reading stored evidence…';
  try {
    const data=await call('/api/item-workspace?'+new URLSearchParams({[kind]:id}));
    $('detail-title').textContent=data.title||'Item evidence';
    const sections=[['Source evidence',data.source],['Recorded decisions',data.decisions],['Owned stock',data.owned],['Orders and returns',data.orders],['Existing forecasts',data.forecasts],['History',data.history]];
    $('viewer-detail-content').innerHTML=sections.filter(([,v])=>v!==undefined).map(([title,value],index)=>'<details'+(!index?' open':'')+'><summary>'+esc(title)+'</summary>'+fields(value)+'</details>').join('');
  } catch(error) {$('viewer-detail-content').textContent=error.message;}
}
function pagination(data) {
  if(!data.pages||data.pages<2)return '';
  return '<div class="pagination"><button type="button" data-page="'+(data.page-1)+'"'+(data.page<=1?' disabled':'')+'>Previous</button><span>'+num(data.page)+' / '+num(data.pages)+'</span><button type="button" data-page="'+(data.page+1)+'"'+(data.page>=data.pages?' disabled':'')+'>Next</button></div>';
}
function calculator() {
  $('viewer-content').innerHTML='<section class="card"><h2>Temporary deal assumptions</h2><p class="muted">These inputs stay in this page. Results are assumptions, and are not saved as market evidence or actual trade results.</p><form id="viewer-calculator"><div class="cards">'+[['buy','Purchase price (€)'],['resale','Assumed resale (€)'],['inbound','Inbound shipping (€)'],['fee','Platform fee (%)'],['outbound','Outbound shipping / packaging (€)'],['repair','Repair (€)'],['other','Other costs (€)'],['target','Target profit (€)'],['days','Days held'],['carrying','Daily carrying cost (€)'],['haircut','Resale reduction (%)']].map(([key,label])=>'<label>'+label+'<input type="number" name="'+key+'" min="0" step="'+(key==='days'?'1':'0.01')+'" required></label>').join('')+'</div><button type="submit" class="primary">Calculate</button></form><div id="calculator-result"></div></section>';
  $('viewer-calculator').onsubmit=event=>{
    event.preventDefault();const form=event.target,values=Object.fromEntries(['buy','resale','inbound','fee','outbound','repair','other','target','days','carrying'].map(k=>[k,Number(form.elements[k].value)]));
    try {const result=calculateScenario(values,Number(form.elements.haircut.value));if(!result)throw new Error('Enter complete non-negative assumptions, a fee below 100%, and a valid resale reduction.');$('calculator-result').innerHTML='<h3 style="margin-top:24px">Assumed result</h3><div class="cards">'+metric('Base profit',eur(result.base))+metric('Downside profit',eur(result.downside))+metric('Maximum buy at target',eur(result.maxBuy))+'</div>';}
    catch(error){$('calculator-result').textContent=error.message;}
  };
}
async function load() {
  const requested=++sequence; $('refresh-view').disabled=true;$('viewer-message').hidden=true;
  $('viewer-filters').hidden=!['overview','research','opportunities','observations','inventory','forecasts'].includes(current);
  if(current==='calculator'){calculator();$('refresh-view').disabled=false;return;}
  $('viewer-content').setAttribute('aria-busy','true');
  try {
    let html='',params=filters(),data;
    if(current==='overview') {
      data=await call('/api/viewer/snapshot?'+params);snapshot=data;
      const panel=data.panels?.national||{};
      html='<div class="cards">'+metric('Retained priced observations',num(panel.n??panel.n_listings),'Stored asking prices')+metric('Median asking price',eur(panel.median_eur),'Asking prices, not realised sales')+metric('Recorded purchases',num(data.economics?.n_purchases),'Shared workspace records')+'</div><section class="card"><h2>Asking-price context</h2>'+fields(panel)+'</section>';
    } else if(current==='research') {
      data=await call('/api/research/candidates?'+params);
      html='<section class="card"><h2>Supported comparison candidates</h2><p class="muted">'+num(data.summary?.total_candidates??data.candidates?.length)+' candidates. Asking-price gaps do not establish realised profit.</p>'+table(['Listing','Ask','Peer median','Other peers','Asking-price gap','Evidence'],(data.candidates||[]).map(row=>[esc(row.title),eur(row.ask_eur),eur(row.peer_median_eur),num(row.n_peers),percent(row.gap_pct),itemButton('listing_pk',row.listing_pk)]))+(data.candidates?.length?'':'<p>Thin or unsupported comparisons remain unavailable. Browse observations to inspect their evidence.</p>')+'</section>';
    } else if(current==='opportunities') {
      data=await call('/api/opportunities?'+params);
      html='<section class="card"><h2>Product and market evidence</h2><p class="muted">'+esc(data.note||'')+'</p>'+table(['Product','Market','Evidence','Next review'],(data.rows||[]).map(row=>[esc(row.product?.label||row.row_id),esc(row.market?.category||row.market?.category_norm||''),fields({margin:row.margin,work:row.work,cash:row.cash,quantity:row.quantity}),esc(row.next_action||'')]))+'</section>';
    } else if(current==='observations') {
      params.set('page',String(page));if($('viewer-search').value)params.set('search',$('viewer-search').value);
      data=await call('/api/listings?'+params);
      html='<section class="card">'+table(['Listing','Asking price','Condition','Municipality','Evidence'],(data.items||data.listings||data.rows||[]).map(row=>[esc(row.title||row.listing_id),eur(row.listed_price_eur),esc(row.condition_norm),esc(row.municipality_norm),itemButton('listing_pk',row.listing_pk)]))+pagination(data)+'</section>';
    } else if(current==='forecasts') {
      data=await call('/api/viewer/forecasts?'+params);
      html='<section class="card"><h2>Retained forecast evidence</h2><p class="muted">'+esc(data.note||'Forecasts retain their measured support and limitations. No model is trained or run by this view.')+'</p>'+table(['Listing','Retained outputs','Evidence'],(data.items||[]).map(row=>[esc(row.listing?.title||row.listing_pk),fields(row.cards),itemButton('listing_pk',row.listing_pk)]))+'</section>';
    } else if(['inventory','drafts','orders'].includes(current)) {
      const url={inventory:'/api/inventory',drafts:'/api/drafts',orders:'/api/trades'}[current];
      const q=new URLSearchParams({page:String(page)});if(current==='inventory')q.set('search',$('viewer-search').value);
      data=await call(url+'?'+q);
      html='<section class="card">'+table(['Item','Reference','State','Recorded value','Evidence'],(data.items||data.orders||[]).map(row=>[esc(row.title||row.title_raw||row.sku),esc(row.sku||row.order_id||row.draft_id),esc(row.state||row.phase||row.status),eur(row.acquisition_cost_eur??row.proceeds_eur),itemButton(current==='inventory'?'sku':current==='drafts'?'draft_id':'order_id',current==='inventory'?row.sku:current==='drafts'?row.draft_id:row.order_id)]))+pagination(data)+'</section>';
    } else if(current==='journal') {
      data=await call('/api/research?page='+page);const journal=data.journal||{};
      html='<section class="card">'+table(['Decision','Listing','Recorded at','Notes','Evidence'],(journal.items||[]).map(row=>[esc(row.decision),esc(row.title||row.listing_id),esc(date(row.recorded_at||row.created_at)),esc(row.notes||''),itemButton('decision_pk',row.decision_pk)]))+pagination(journal)+'</section>';
    } else if(current==='scenarios') {
      data=await call('/api/research/scenarios');
      html='<section class="card">'+table(['Saved scenario','Updated','Explicit assumptions','Evidence'],(data.entries||[]).map(row=>[esc(row.name),esc(date(row.updated_at)),fields(row.inputs),itemButton('listing_pk',row.listing_pk)]))+'</section>';
    }
    if(requested!==sequence)return;
    $('viewer-content').innerHTML=html;
    $('viewer-freshness').textContent='Read from stored workspace data · '+new Date().toLocaleString();
  } catch(error) {if(requested===sequence){$('viewer-message').textContent=error.message;$('viewer-message').hidden=false;}}
  finally {if(requested===sequence){$('refresh-view').disabled=false;$('viewer-content').setAttribute('aria-busy','false');}}
}
function show(view) {current=Object.hasOwn(titles,view)?view:'overview';page=1;$('view-title').textContent=titles[current];document.querySelectorAll('[data-view]').forEach(button=>{if(button.dataset.view===current)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});history.replaceState(null,'',location.pathname+location.search+'#'+current);load();}
if(user) {
  accountControls(user,$('account-controls'));
  document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>show(button.dataset.view));
  $('refresh-view').onclick=load;$('close-detail').onclick=()=>$('viewer-detail').close();
  $('viewer-content').onclick=event=>{const item=event.target.closest('[data-item-kind]');if(item)detail(item.dataset.itemKind,item.dataset.itemId);const pager=event.target.closest('[data-page]');if(pager){page=Number(pager.dataset.page);load();}};
  for(const id of ['market-filter','category-filter','municipality-filter'])$(id).onchange=()=>{page=1;load();};
  $('viewer-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;load();},350);};
  try {
    snapshot=await call('/api/viewer/snapshot?'+filters());
    for(const [id,key] of [['category-filter','category_norm'],['municipality-filter','municipality_norm']])$(id).innerHTML+='<option disabled>──────────</option>'+(snapshot.segments?.[key]||[]).map(value=>'<option value="'+esc(value)+'">'+esc(value)+'</option>').join('');
  }catch(error){$('viewer-message').textContent=error.message;$('viewer-message').hidden=false;}
  show(location.hash.slice(1));
  setInterval(()=>{if(!document.hidden&&current!=='calculator'&&!$('viewer-detail').open)load();},60000);
}
