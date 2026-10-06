import {call} from '/static/api.js';
import {session,accountControls} from '/static/auth-client.js';
import {esc,eur,num,percent,date} from '/static/format.js';

const $=id=>document.getElementById(id);
const titles={overview:'Market overview',observations:'Observations',research:'Price research',records:'Workspace records',calculator:'Deal calculator',accounts:'Accounts',dataset:'Research snapshot'};
const descriptions={overview:'A view of the private research snapshot.',observations:'Browse retained observations and their source evidence.',research:'Supported comparisons from the existing research engine.',records:'Read shared inventory, draft, order and decision records.',calculator:'Estimate a deal using your own temporary assumptions.',accounts:'Manage access to the private workspace.',dataset:'Manage the private data used by both workspaces.'};
let current='overview',page=1,sequence=0,searchTimer;
const user=await session(document.body.dataset.role);
function table(headers,rows,empty='No stored records match this view.') {return rows.length?'<div class="table-wrap"><table><thead><tr>'+headers.map(value=>'<th>'+esc(value)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(value=>'<td>'+value+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>':'<p class="muted">'+esc(empty)+'</p>';}
function metric(label,value,note) {return '<article class="card"><span class="muted">'+esc(label)+'</span><div class="metric-value">'+esc(value)+'</div><p class="muted">'+esc(note)+'</p></article>';}
function evidence(id) {return '<button type="button" data-item="'+esc(id)+'">Read evidence</button>';}
function errorMessage(error) {$('workspace-message').textContent=error.message;$('workspace-message').hidden=false;}
function params() {const values=new URLSearchParams({subplatform:$('market-filter').value});if(current!=='research')for(const [id,key] of [['category-filter','category_norm'],['municipality-filter','municipality_norm'],['workspace-search','search']])if($(id).value)values.set(key,$(id).value);return values;}
function pagination(data) {return data.pages>1?'<div class="pagination"><button data-page="'+(data.page-1)+'"'+(data.page===1?' disabled':'')+'>Previous</button><span>'+num(data.page)+' / '+num(data.pages)+'</span><button data-page="'+(data.page+1)+'"'+(data.page===data.pages?' disabled':'')+'>Next</button></div>':'';}
function details(record) {return '<dl>'+Object.entries(record).map(([key,value])=>'<dt>'+esc(key.replaceAll('_',' '))+'</dt><dd>'+esc(value===null||value===undefined?'Unknown':typeof value==='object'?JSON.stringify(value):String(value))+'</dd>').join('')+'</dl>';}
async function item(id) {
  $('item-detail').showModal();$('detail-content').textContent='Reading evidence…';
  try {const data=await call('/api/cloud/item?'+new URLSearchParams({listing_pk:id}));$('detail-title').textContent=data.listing.title;$('detail-content').innerHTML='<h3>Retained observation</h3>'+details(data.listing)+'<h3>Supported comparison</h3>'+(data.comparison?details({asking_price:eur(data.comparison.ask_eur),peer_median:eur(data.comparison.peer_median_eur),other_peers:data.comparison.n_peers,asking_price_gap:percent(data.comparison.gap_pct)}):'<p class="muted">No supported comparison was retained for this observation.</p>')+'<p class="muted">Snapshot: '+esc(date(data.generated_at))+'. Asking prices do not establish realised sale prices or profit.</p>';}
  catch(error){$('detail-content').textContent=error.message;}
}
function calculator() {
  const inputs=[['buy','Purchase price (€)',0],['resale','Assumed resale (€)',0],['inbound','Inbound shipping (€)',0],['fee','Platform fee (%)',0],['outbound','Shipping / packaging (€)',0],['repair','Repair costs (€)',0],['other','Other costs (€)',0],['target','Target profit (€)',0],['days','Days held',0],['carrying','Daily holding cost (€)',0],['haircut','Downside resale reduction (%)',20]];
  $('workspace-content').innerHTML='<section class="card"><h2>Deal assumptions</h2><p class="muted">These calculations stay in your browser. They are assumptions and are not saved as market evidence.</p><form id="deal-calculator"><div class="cards">'+inputs.map(([key,label,value])=>'<label>'+esc(label)+'<input type="number" name="'+key+'" min="0" step="0.01" value="'+value+'" required></label>').join('')+'</div><button type="submit" class="primary">Calculate</button></form><div id="calculator-result"></div></section>';
  $('deal-calculator').onsubmit=event=>{
    event.preventDefault();const values=Object.fromEntries(inputs.map(([key])=>[key,Number(event.target.elements[key].value)]));
    if(Object.values(values).some(value=>!Number.isFinite(value)||value<0)||values.fee>=100||values.haircut>100)return errorMessage(new Error('Use non-negative amounts, a fee below 100%, and a reduction of 0–100%.'));
    const costs=values.buy+values.inbound+values.outbound+values.repair+values.other+values.days*values.carrying,net=values.resale*(1-values.fee/100),base=net-costs,downside=net*(1-values.haircut/100)-costs,maxBuy=net-(costs-values.buy)-values.target;
    $('calculator-result').innerHTML='<h3 style="margin-top:24px">Assumed result</h3><div class="cards">'+metric('Base profit',eur(base),'Under your resale assumption')+metric('Downside profit',eur(downside),'With the resale reduction')+metric('Maximum buy at target',eur(maxBuy),'Includes all entered costs')+'</div>';
  };
}
async function accountView() {
  const data=await call('/api/auth/users');
  return '<section class="card"><h2>Workspace access</h2><p class="muted">Seller / viewer accounts can read the shared research. Developer accounts also manage accounts and replace the research snapshot.</p>'+data.users.map(row=>'<details data-user="'+esc(row.user_id)+'"><summary>'+esc(row.username)+' · '+esc(row.role)+' · '+(row.active?'Active':'Disabled')+'</summary><p>'+(row.must_change_password?'Password change required at next login.':'Personal password is set.')+'</p><button data-active="'+(!row.active)+'">'+(row.active?'Disable':'Enable')+'</button> <button data-role="'+(row.role==='viewer'?'developer':'viewer')+'">Change to '+(row.role==='viewer'?'developer':'viewer')+'</button><form data-reset><label>New temporary password<input type="password" name="password" minlength="15" maxlength="256" required autocomplete="new-password"></label><button type="submit">Reset password</button></form></details>').join('')+'</section><section class="card"><h2>Create account</h2><form id="create-account"><label>Username<input name="username" required minlength="3" maxlength="150" autocomplete="off"></label><label>Access<select name="role"><option value="viewer">Seller / viewer · read only</option><option value="developer">Developer</option></select></label><label>Temporary password<input type="password" name="password" minlength="15" maxlength="256" required autocomplete="new-password"></label><p class="muted">The new user must change this password at first login.</p><button type="submit" class="primary">Create account</button></form></section>';
}
async function load() {
  const requested=++sequence;$('workspace-message').hidden=true;$('refresh-view').disabled=true;
  $('workspace-filters').hidden=!['overview','observations','research'].includes(current);document.querySelectorAll('[data-observation-filter]').forEach(value=>value.hidden=current==='research');
  if(current==='calculator'){calculator();$('refresh-view').disabled=false;return;}
  $('workspace-content').setAttribute('aria-busy','true');
  try {
    let html='',data;
    if(current==='overview') {
      data=await call('/api/cloud/overview?'+params());
      html='<div class="cards">'+metric('Retained observations',num(data.total),'In the selected view')+metric('Available priced observations',num(data.priced_available),'Fresh within 7 days of the snapshot')+metric('Median asking price',eur(data.median_eur),data.median_status==='Available'?'Asking prices, not realised sales':'At least 30 priced observations required')+'</div><section class="card"><h2>Observed categories</h2><p class="muted">This is an observed search-page sample. Counts do not establish complete market coverage.</p>'+table(['Category','Observations'],data.category_counts.map(([name,count])=>[esc(name),num(count)]))+'</section>';
      for(const [id,key,all] of [['category-filter','category_norm','All categories'],['municipality-filter','municipality_norm','All municipalities']]) {const selected=$(id).value;$(id).innerHTML='<option value="">'+all+'</option>'+data.segments[key].map(value=>'<option value="'+esc(value)+'">'+esc(value)+'</option>').join('');$(id).value=selected;}
    }else if(current==='observations') {
      const query=params();query.set('page',page);data=await call('/api/cloud/listings?'+query);
      html='<section class="card"><h2>'+num(data.total)+' observations</h2>'+table(['Listing','Asking price','Condition','Municipality','Evidence'],data.items.map(row=>[esc(row.title),eur(row.listed_price_eur),esc(row.condition_label_raw||row.condition_norm||'Unknown'),esc(row.municipality_norm||'Unknown'),evidence(row.listing_pk)]))+pagination(data)+'</section>';
    }else if(current==='research') {
      data=await call('/api/cloud/research?'+params());const candidates=data.candidates||[];
      html='<section class="card"><h2>Supported asking-price comparisons</h2><p class="muted">'+esc(data.scope_note)+'</p><p>'+num(data.summary?.total_candidates??candidates.length)+' supported candidates; showing '+num(candidates.length)+'. Asking-price gaps do not establish realised profit.</p>'+table(['Listing','Ask','Peer median','Other peers','Asking-price gap','Evidence'],candidates.map(row=>[esc(row.title),eur(row.ask_eur),eur(row.peer_median_eur),num(row.n_peers),percent(row.gap_pct),evidence(row.listing_pk)]),'No supported comparisons were retained for this market. Thin or unsupported evidence stays unavailable.')+'</section>';
      if(data.research_inventory?.items?.length)html+='<section class="card"><h2>Observations requiring more evidence</h2><p class="muted">These records have no supported price gap. Review the underlying observation before making a decision.</p>'+table(['Listing','Evidence'],data.research_inventory.items.slice(0,50).map(row=>[esc(row.title||row.listing?.title||'Observation'),evidence(row.listing_pk||row.listing?.listing_pk)]))+'</section>';
    }else if(current==='records') {
      data=await call('/api/cloud/records');html=Object.entries({inventory:'Owned inventory',drafts:'Listing drafts',orders:'Orders and returns',journal:'Decision journal',scenarios:'Saved scenarios'}).map(([key,label])=>'<section class="card"><h2>'+label+'</h2>'+table(['Record','Reference','State','Recorded value'],data[key].map(row=>[esc(row.title||row.name||row.sku||'Stored record'),esc(row.sku||row.order_id||row.draft_id||row.decision_pk||''),esc(row.state||row.phase||row.decision||''),eur(row.acquisition_cost_eur??row.proceeds_eur)]),'No '+label.toLowerCase()+' were recorded in this snapshot.')+'</section>').join('');
    }else if(current==='accounts')html=await accountView();
    else if(current==='dataset') {
      data=await call('/api/cloud/status');html='<section class="card"><h2>Private research snapshot</h2>'+details({observations:num(data.observations),snapshot_taken:date(data.generated_at),uploaded:date(data.uploaded_at),storage:data.storage})+'<p class="muted">Login and data run on Netlify. This basic hosted workspace uses a private uploaded snapshot. No LLM or collection jobs run from this site.</p></section><section class="card"><h2>Replace the snapshot</h2><p>Upload a .json.gz file generated by the repository’s snapshot exporter. Both workspaces will read the new snapshot after it is published.</p><form id="snapshot-upload"><label>Snapshot file<input type="file" name="snapshot" accept=".gz,application/gzip" required></label><button type="submit" class="primary">Publish private snapshot</button></form></section>';
    }
    if(requested!==sequence)return;$('workspace-content').innerHTML=html;
    if(data?.generated_at)$('workspace-freshness').textContent='Private snapshot · '+date(data.generated_at);else if(!['records'].includes(current))$('workspace-freshness').textContent='';
  }catch(error){if(requested===sequence)errorMessage(error);}
  finally{if(requested===sequence){$('refresh-view').disabled=false;$('workspace-content').setAttribute('aria-busy','false');}}
}
function show(view) {current=Object.hasOwn(titles,view)&&(!['accounts','dataset'].includes(view)||user.role==='developer')?view:'overview';page=1;$('view-title').textContent=titles[current];$('view-description').textContent=descriptions[current];document.querySelectorAll('[data-view]').forEach(button=>{if(button.dataset.view===current)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});history.replaceState(null,'',location.pathname+'#'+current);load();}
if(user) {
  accountControls(user,$('account-controls'));
  if(user.role==='developer') {document.querySelectorAll('[data-developer]').forEach(value=>value.hidden=false);$('access-name').textContent='Developer workspace';$('access-mode').textContent='Account and data management';}
  $('workspace-label').textContent=user.role==='developer'?'Developer workspace':'Seller workspace · Read only';
  document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>show(button.dataset.view));$('refresh-view').onclick=load;$('close-detail').onclick=()=>$('item-detail').close();
  $('workspace-content').addEventListener('click',async event=>{
    const itemButton=event.target.closest('[data-item]');if(itemButton)item(itemButton.dataset.item);
    const pager=event.target.closest('[data-page]');if(pager){page=Number(pager.dataset.page);load();}
    const action=event.target.closest('button[data-active],button[data-role]');if(action){action.disabled=true;try{const params={user_id:action.closest('[data-user]').dataset.user};if(action.hasAttribute('data-active'))params.active=action.dataset.active==='true';else params.role=action.dataset.role;await call('/api/auth/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'update',params})});await load();}catch(error){errorMessage(error);action.disabled=false;}}
  });
  $('workspace-content').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target;if(form.id==='deal-calculator')return;
    const button=form.querySelector('button[type=submit]');button.disabled=true;
    try {
      if(form.id==='snapshot-upload') {const file=form.elements.snapshot.files[0];if(!file||file.size>4*1024*1024)throw new Error('Choose a compressed snapshot of at most 4 MB.');await call('/api/cloud/snapshot',{method:'POST',headers:{'Content-Type':'application/gzip'},body:file});}
      else {const params=form.id==='create-account'?{username:form.elements.username.value,role:form.elements.role.value,password:form.elements.password.value}:{user_id:form.closest('[data-user]').dataset.user,password:form.elements.password.value};await call('/api/auth/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:form.id==='create-account'?'create':'update',params})});form.reset();}
      await load();
    }catch(error){errorMessage(error);button.disabled=false;}
  });
  for(const id of ['market-filter','category-filter','municipality-filter'])$(id).onchange=()=>{page=1;if(id==='market-filter'){$('category-filter').value='';$('municipality-filter').value='';}load();};
  $('workspace-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;load();},350);};
  try{const first=await call('/api/cloud/overview?'+params());for(const [id,key] of [['category-filter','category_norm'],['municipality-filter','municipality_norm']])$(id).innerHTML+=first.segments[key].map(value=>'<option value="'+esc(value)+'">'+esc(value)+'</option>').join('');}catch(error){errorMessage(error);}
  show(location.hash.slice(1));
}
