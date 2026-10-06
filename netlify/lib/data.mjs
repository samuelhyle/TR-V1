import {gunzipSync, gzipSync} from 'node:zlib';
import {randomUUID} from 'node:crypto';
import {Denied} from './auth.mjs';

export const MAX_UPLOAD = 4*1024*1024;
const fields=['listing_pk','platform','subplatform','listing_id','title','category_norm','category_root_norm','municipality_norm','condition_norm','condition_label_raw','seller_type_norm','listed_price_eur','is_giveaway','is_reserved','status_norm','outcome_status','first_seen_at','last_seen_at','last_probed_at','price_observed_at'];
const privateKeys=new Set(['password_hash','csrf_token','token_hash','client_token','actions','next_actions','binding','account_binding','capabilities','queue','outbox','can_edit','can_save','can_publish','input_path','model_path','artifact_path','database_path']);
function project(value) {
  if(Array.isArray(value))return value.map(project);
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).filter(([key])=>!privateKeys.has(key)).map(([key,entry])=>[key,project(entry)]));
  return value;
}
export function validateSnapshot(data) {
  if(data?.version!==1||!Array.isArray(data.listings)||data.listings.length>150000||!data.research||!Number.isFinite(Date.parse(data.generated_at)))throw new Denied(400,'Use a snapshot created by export_netlify_snapshot.py.');
  const ids=new Set();
  for(const row of data.listings) {
    if(!Number.isSafeInteger(row.listing_pk)||ids.has(row.listing_pk)||!['standard','diili'].includes(row.subplatform)||row.platform!=='tori'||typeof row.title!=='string'||row.title.length>2000)throw new Denied(400,'The snapshot contains an invalid observation.');
    ids.add(row.listing_pk);
    for(const [key,value] of Object.entries(row)) {
      if(!fields.includes(key)||!['string','number','boolean'].includes(typeof value)&&value!==null)throw new Denied(400,'The snapshot contains unsupported observation fields.');
      if(typeof value==='string'&&value.length>2000)throw new Denied(400,'An observation field is too long.');
    }
  }
  for(const market of ['standard','diili'])if(!Array.isArray(data.research[market]?.candidates))throw new Denied(400,'The snapshot is missing market research.');
  return project(data);
}
export async function publishSnapshot(store, bytes, actor) {
  let data;
  try {data=JSON.parse(gunzipSync(bytes,{maxOutputLength:40*1024*1024}).toString());}
  catch {throw new Denied(400,'Upload a valid compressed JSON snapshot (.json.gz).');}
  data=validateSnapshot(data);
  const key='snapshots/'+randomUUID();
  await store.set(key,gzipSync(Buffer.from(JSON.stringify(data))));
  const manifest={key,generated_at:data.generated_at,uploaded_at:new Date().toISOString(),uploaded_by:actor,observations:data.listings.length,bytes:bytes.length};
  await store.setJSON('manifest',manifest);
  return manifest;
}
export function dataService(store) {
  let cachedKey,cachedData;
  async function read() {
    const manifest=await store.get('manifest',{type:'json'});
    if(!manifest)throw new Denied(503,'A developer needs to upload the first research snapshot.');
    if(manifest.key!==cachedKey) {
      const bytes=await store.get(manifest.key,{type:'arrayBuffer'});
      if(!bytes)throw new Denied(503,'The research snapshot is temporarily unavailable.');
      cachedData=JSON.parse(gunzipSync(Buffer.from(bytes),{maxOutputLength:40*1024*1024}).toString());cachedKey=manifest.key;
    }
    return {data:cachedData,manifest};
  }
  async function query(path,params) {
    const {data,manifest}=await read();
    const market=params.get('subplatform')||'standard';
    if(!['standard','diili'].includes(market))throw new Denied(400,'Invalid market.');
    if(path==='/api/cloud/status')return {generated_at:manifest.generated_at,uploaded_at:manifest.uploaded_at,observations:manifest.observations,bytes:manifest.bytes,storage:'Private Netlify Blobs',llm_enabled:false};
    if(path==='/api/cloud/research')return {...data.research[market],generated_at:data.generated_at,scope_note:'Saved comparisons use this market across Finland. Category and municipality filters apply to observations only.'};
    if(path==='/api/cloud/records')return Object.fromEntries(['inventory','drafts','orders','journal','scenarios'].map(key=>[key,data[key]||[]]));
    if(path==='/api/cloud/item') {
      const row=data.listings.find(row=>row.listing_pk===Number(params.get('listing_pk')));
      if(!row)throw new Denied(404,'Observation not found.');
      const comparison=data.research[row.subplatform]?.candidates.find(value=>value.listing_pk===row.listing_pk);
      return {listing:row,comparison:comparison||null,generated_at:data.generated_at};
    }
    const category=params.get('category_norm')||'',municipality=params.get('municipality_norm')||'',search=(params.get('search')||'').toLowerCase();
    if(search.length>200||category.length>300||municipality.length>200)throw new Denied(400,'Filter text is too long.');
    const marketRows=data.listings.filter(row=>row.subplatform===market);
    const rows=marketRows.filter(row=>(!category||row.category_norm===category)&&(!municipality||row.municipality_norm===municipality)&&(!search||(row.title||'').toLowerCase().includes(search)||row.listing_id.includes(search)));
    if(path==='/api/cloud/listings') {
      const size=25,pages=Math.max(1,Math.ceil(rows.length/size));
      const requested=Number(params.get('page')||1);
      if(!Number.isSafeInteger(requested)||requested<1)throw new Denied(400,'Invalid page.');
      const page=Math.min(requested,pages);
      return {items:rows.slice((page-1)*size,page*size),total:rows.length,pages,page,page_size:size,generated_at:data.generated_at};
    }
    if(path==='/api/cloud/overview') {
      const cutoff=Date.parse(data.generated_at)-168*3600000;
      const available=rows.filter(row=>row.status_norm==='active'&&row.outcome_status==='unknown'&&!row.is_reserved&&!row.is_giveaway&&Date.parse(row.price_observed_at)>=cutoff&&row.listed_price_eur>0);
      const prices=available.map(row=>row.listed_price_eur).sort((a,b)=>a-b);
      const median=prices.length?(prices[Math.floor((prices.length-1)/2)]+prices[Math.ceil((prices.length-1)/2)])/2:null;
      const categories=new Map();for(const row of rows){const key=row.category_root_norm||'Unclassified';categories.set(key,(categories.get(key)||0)+1);}
      return {generated_at:data.generated_at,total:rows.length,priced_available:prices.length,median_eur:prices.length>=30?median:null,median_status:prices.length>=30?'Available':'Insufficient observations',category_counts:[...categories].sort((a,b)=>b[1]-a[1]).slice(0,12),segments:{category_norm:[...new Set(marketRows.map(row=>row.category_norm).filter(Boolean))].sort(),municipality_norm:[...new Set(marketRows.map(row=>row.municipality_norm).filter(Boolean))].sort()}};
    }
    throw new Denied(404,'This endpoint is unavailable.');
  }
  return {query,read};
}
