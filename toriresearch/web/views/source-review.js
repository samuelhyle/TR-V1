import {esc,date} from '../format.js';
import {badge,metric} from '../components.js';

const reviewName=value=>String(value).replaceAll('_',' ');
const reviewFieldLabel=key=>({battery_health_pct:'Battery health (%)',hardware_code:'Hardware / model code',storage:'Storage capacity',quantity:'Number of units',offer_kind:'Offer type',defects:'Disclosed defects',condition:'Condition',functionality:'Functionality',completeness:'Completeness',edition:'Edition',mount:'Lens mount'}[key]||reviewName(key));
const reviewChoices=(values,selected='')=>'<option value="">Unknown / unverified</option>'+Object.entries(values).map(([key,label])=>`<option value="${esc(key)}"${key===selected?' selected':''}>${esc(label)}</option>`).join('');
const reviewBasis=name=>`<label class="field">Verification basis<select name="basis_${name}"><option value="">Choose after checking</option><option value="seller_statement">Seller statement</option><option value="physical_inspection">Physical inspection</option></select></label>`;
const reviewField=(name,label,definition)=>{
  const input=definition.type==='enum'?`<select name="value_${name}">${reviewChoices(Object.fromEntries(definition.values.map(key=>[key,reviewName(key)])))}</select>`:
    `<input name="value_${name}" ${definition.type==='integer'?`type="number" min="${definition.minimum}" max="${definition.maximum}" step="1"`:`maxlength="${definition.max_length}"`} placeholder="Unknown">`;
  return `<section class="source-review-field"><div class="form-grid"><label class="field">${esc(label)}${input}</label>${reviewBasis(name)}</div></section>`;
};

export function sourceReviewHistory(data){
  if(!data.history?.length)return '<p class="inventory-help">No operator review recorded.</p>';
  return `<div class="unit-history">${data.history.map((row,index)=>`<article><strong>${index===0?'Latest recorded review':'Earlier review'} · ${row.current?'Source and model still match':'Stale / unavailable evidence'}</strong><p>${esc(row.reviewer||'Unknown reviewer')} · ${esc(reviewName(row.review_outcome||'unknown'))} · ${date(row.reviewed_at)} · ${esc(reviewName(row.review_source||'unknown'))}</p><p>${esc(row.review_note||'No review note retained.')} · ${esc(row.evidence_ref||'No private reference retained.')}</p><details><summary>Reviewed values and verification</summary><p>Family: ${esc(row.expected?.family||'Unknown')} · model: ${esc(row.expected?.model_key||'Unknown')}</p>${Object.entries(row.expected?.attributes||{}).filter(([,value])=>value!=null).map(([key,value])=>`<p>${esc(reviewName(key))}: ${esc(value)} · ${esc(reviewName(row.verification?.[key]||'unverified'))}</p>`).join('')}<p class="note">Source checked ${date(row.source?.checked_at)} · ${esc(row.source?.availability||'not checked')}${row.supersedes_review_id?' · corrects '+esc(row.supersedes_review_id):''}</p></details></article>`).join('')}</div><p class="note">Showing ${data.history.length} of ${data.history_total} retained reviews.</p>`;
}

export function sourceReviewContext(data){
  const listing=data.listing,source=data.source,known=data.current_review;
  return `<h3>${esc(listing.title||'Listing #'+listing.listing_id)}</h3><p class="inventory-help">${esc(listing.platform)} / #${esc(listing.listing_id)} · ${esc(listing.category_norm||'Unknown category')} · ${esc(listing.condition_norm||'Unknown source condition')}</p>`+
    (source.public_url?`<p><a class="btn small" href="${esc(source.public_url)}" target="_blank" rel="noopener noreferrer">Open source listing</a></p>`:'<p class="inventory-help">No supported public listing link.</p>')+
    `<div class="metric-list">${metric('Current identity status',esc(data.current_identity.status))}${metric('Current reviewed offer gate',badge(data.offer.eligible?'Admitted':'Withheld',data.offer.eligible?'green':'amber'))}${metric('Current evidence',known?esc(reviewName(known.review_source)):'No current operator review')}</div><p class="inventory-help">${esc(data.note)}</p>`+
    `<details><summary>Retained seller assertions</summary><p class="inventory-help">These are extracted source statements. Confirm each value independently before recording it.</p>${(data.source_assertions?.assertions||[]).map(row=>`<p>${esc(reviewName(row.attribute))}: ${esc(row.value)} · ${esc(row.source_field)} · ${esc(reviewName(row.verification_basis||'unknown'))}</p>`).join('')||'<p class="note">No bounded seller assertions retained.</p>'}</details>`;
}

export function sourceReviewForm(data){
  if(!data.can_review)return `<div class="message info">${data.blocked_reasons.map(esc).join(' ')}</div>`;
  const familyChoices=Object.fromEntries(data.families.map(row=>[row.id,row.label]));familyChoices.outside_pilot='Outside the pilot';
  return `<div class="unit-actions"><button class="btn primary" type="button" data-review-start>Begin source review</button></div><form data-source-review-form><fieldset data-review-fields disabled><p class="inventory-help">Record only what you checked. Blank fields withdraw assertions in this new review. Suggested model names are not verified labels; no derived values are copied automatically.</p><div class="form-grid"><label class="field">Source access<select name="availability" required><option value="">Not checked</option><option value="available">Available</option><option value="unavailable">Unavailable</option></select></label><label class="field">Checked at<input name="checked_at" type="datetime-local" step="0.001" required></label><button class="btn small" type="button" data-review-now>Checked just now</button><label class="field">Review evidence<select name="review_source"><option value="listing_review">Source listing</option><option value="physical_inspection">Physical inspection</option></select></label><label class="field">Reviewer<input name="reviewer" required maxlength="80" placeholder="Your initials / internal label"></label><label class="field">Observed outcome<select name="review_outcome" required><option value="">Choose explicitly</option>${data.outcomes.map(key=>`<option value="${key}">${esc(reviewName(key))}</option>`).join('')}</select></label><label class="field">Private evidence reference<input name="evidence_ref" required maxlength="500"></label><label class="field wide">Review / correction reason<textarea name="review_note" required maxlength="1000"></textarea></label></div>
  <h3>Reviewed identity and offer</h3><p class="inventory-help">Each known value needs its own verification basis. Source listing reviews cannot claim physical inspection. An unavailable listing cannot establish seller labels.</p><div class="unit-actions">${data.current_review?'<button class="btn small" type="button" data-review-copy>Copy current reviewed values</button>':''}<button class="btn quiet small" type="button" data-review-clear>Clear asserted values</button></div>
  <section class="source-review-field"><div class="form-grid"><label class="field">Product family<select name="value_family">${reviewChoices(familyChoices)}</select></label>${reviewBasis('family')}<label class="field">Model<select name="value_model_key"><option value="">Unknown / unverified</option></select></label>${reviewBasis('model_key')}</div><p class="inventory-help" data-review-model-checks></p></section>
  ${Object.entries(data.fields).map(([key,definition])=>reviewField(key,reviewFieldLabel(key),definition)).join('')}
  <details><summary>Physical-unit grouping</summary><p class="inventory-help">Reuse the same reference for relists of one physical unit. Creating a reference does not verify that grouping or create an owned inspection.</p><label class="field">Physical unit reference<input name="unit_group" maxlength="36" placeholder="Verified group ID, if applicable"></label><button class="btn quiet small" type="button" data-review-group>Create a unit reference</button><label class="unit-attestation"><input name="group_verified" type="checkbox">I verified that this reference identifies the same physical unit.</label></details>
  <label class="unit-attestation"><input name="confirm_source_review" type="checkbox" required>I checked each entered value and verification basis against this source or physical unit.</label><div class="form-footer"><button class="btn" type="submit">Preview review</button><button class="btn primary" type="button" data-review-save disabled>Save reviewed evidence</button></div></fieldset></form><div data-review-preview aria-live="polite"></div>`;
}

export function sourceReviewAnswers(form,definitions){
  const values=Object.fromEntries(new FormData(form)),attributes={},verification={};
  const expected={family:values.value_family||null,model_key:values.value_model_key||null,attributes};
  for(const [key,definition] of Object.entries(definitions)){const raw=values['value_'+key];attributes[key]=raw===''?null:definition.type==='integer'?Number(raw):raw;}
  for(const [key,value] of Object.entries({...attributes,family:expected.family,model_key:expected.model_key}))if(value!=null)verification[key]=values['basis_'+key];
  return {availability:values.availability,checked_at:values.checked_at?new Date(values.checked_at).toISOString():null,
    review_source:values.review_source,reviewer:values.reviewer,review_outcome:values.review_outcome,
    evidence_ref:values.evidence_ref,review_note:values.review_note,expected,verification,
    unit_group:values.unit_group||null,group_verified:form.elements.group_verified.checked,confirm_source_review:form.elements.confirm_source_review.checked};
}

export function sourceReviewPreview(result){
  if(!result.valid)return `<div class="message error">${result.errors.map(row=>esc(row.error)).join('<br>')}</div>`;
  const value=result.effect,offer=value?.offer;
  return `<section class="eligibility-result"><h3>Proposed evidence</h3><p>${badge(offer?.eligible?'Supports reviewed offer comparison':'Comparison remains withheld',offer?.eligible?'green':'amber')} · ${esc(value?.identity_status||'Unknown')}</p><p class="inventory-help">${value?.missing_fields?.length?'Missing fields: '+esc(value.missing_fields.join(', '))+'. ':''}${offer?.reasons?.length?'Offer gate: '+esc(offer.reasons.map(reviewName).join(', '))+'. ':''}${value?.requires_physical_inspection?'Physical inspection is still required. ':''}Current prices and enough comparable peers are still required. This does not approve a purchase or validate forecast accuracy. Saving retains this new review and all earlier evidence.</p></section>`;
}

export function initSourceReview({dialog,root,call,onSaved}){
  let data=null,token=null,started=false,busy=false,sequence=0,controller=null,proposal=null,pendingSave=null;
  const post=(action,params)=>call('/api/research/source-review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});
  const feedback=(value,error=false)=>{const node=root.querySelector('[data-review-message]');if(node){node.hidden=false;node.className='message '+(error?'error':'success');node.textContent=value;if(error){if(pendingSave){const retry=document.createElement('button');retry.type='button';retry.className='btn small';retry.setAttribute('data-review-retry','');retry.textContent='Retry Save';node.append(' ',retry);}dialog.scrollTop=0;}}};
  const draw=()=>{root.innerHTML=`<div data-review-message class="message" role="status" hidden></div>${sourceReviewContext(data)}${sourceReviewForm(data)}<details class="source-review-history"><summary>Retained review history</summary>${sourceReviewHistory(data)}</details>`;dialog.scrollTop=0;};
  const invalidatePreview=()=>{proposal=null;const save=root.querySelector('[data-review-save]');if(save)save.disabled=true;const preview=root.querySelector('[data-review-preview]');if(preview)preview.innerHTML='';};
  const modelChoices=()=>{const form=root.querySelector('form'),family=data.families.find(row=>row.id===form.elements.value_family.value),chosen=form.elements.value_model_key.value;form.elements.value_model_key.innerHTML=reviewChoices(Object.fromEntries((family?.models||[]).map(row=>[row.id,row.brand+' '+row.label])));form.elements.value_model_key.value=chosen;};
  const modelChecks=()=>{const form=root.querySelector('form'),model=data.families.flatMap(row=>row.models).find(row=>row.id===form.elements.value_model_key.value);root.querySelector('[data-review-model-checks]').textContent=model?'Identity fields to verify for this model: '+model.variant_fields.map(reviewName).join(', ')+'.':'Choose a model only after checking its identity.';};
  const run=async(work)=>{
    if(busy)return;busy=true;root.setAttribute('aria-busy','true');const nodes=[...root.querySelectorAll('button,input,select,textarea')],disabled=nodes.map(node=>node.disabled);nodes.forEach(node=>node.disabled=true);
    try{await work();}catch(error){feedback(error.message+(pendingSave?' Retry Save to recover the same review; its answers are retained.':''),true);}finally{busy=false;root.setAttribute('aria-busy','false');nodes.forEach((node,index)=>node.disabled=disabled[index]);if(pendingSave)root.querySelectorAll('form input,form select,form textarea,form button').forEach(node=>node.disabled=true);const save=root.querySelector('[data-review-save]');if(save)save.disabled=!(pendingSave||proposal);}
  };
  const open=async(pk)=>{
    if(busy)return false;
    controller?.abort();controller=new AbortController();const stamp=++sequence;data=null;token=null;started=false;proposal=null;pendingSave=null;root.innerHTML='<p class="note" role="status">Reading source and review history…</p>';if(!dialog.open)dialog.showModal();
    try{const value=await call('/api/research/source-review?listing_pk='+pk,{signal:controller.signal});if(stamp!==sequence||!dialog.open)return false;data=value;draw();return true;}
    catch(error){if(error.name!=='AbortError'&&stamp===sequence){root.innerHTML='<p class="message error" role="alert"></p>';root.firstElementChild.textContent=error.message;}return false;}
  };
  root.addEventListener('click',event=>{
    if(busy||!data)return;
    const form=root.querySelector('form');
    if(event.target.closest('[data-review-start]')){
      token=token||crypto.randomUUID();run(async()=>{await post('start',{client_token:token,listing_pk:data.listing.listing_pk,expected_revision:data.revision});started=true;root.querySelector('[data-review-fields]').disabled=false;root.querySelector('[data-review-start]').hidden=true;feedback('Source snapshot retained. Enter your independently checked values.');});return;
    }
    if(event.target.closest('[data-review-save],[data-review-retry]')&&(pendingSave||proposal)){
      const approved=pendingSave||proposal;run(async()=>{
        pendingSave=approved;
        feedback('Saving reviewed evidence…');
        let saved;
        try{saved=await post('save',{client_token:token,answers:approved});}
        catch(error){if(error.status>=400&&error.status<500){pendingSave=null;proposal=null;}throw error;}
        pendingSave=null;proposal=null;started=false;token=null;
        root.querySelector('[data-review-fields]').disabled=true;
        try{await onSaved?.(data.listing);data=await call('/api/research/source-review?listing_pk='+saved.result.listing_pk);draw();feedback('Source review saved. Earlier evidence remains in history.');}
        catch(error){feedback('Source review saved, but refreshed evidence could not be read. Reload the review to continue. '+error.message,true);}
      });return;
    }
    if(!form||!started||pendingSave)return;
    if(event.target.closest('[data-review-now]')){const now=new Date();form.elements.checked_at.value=new Date(now.getTime()-now.getTimezoneOffset()*60000).toISOString().slice(0,23);}
    if(event.target.closest('[data-review-group]'))form.elements.unit_group.value=crypto.randomUUID();
    if(event.target.closest('[data-review-clear]'))for(const node of form.querySelectorAll('[name^="value_"],[name^="basis_"]'))node.value='';
    if(event.target.closest('[data-review-copy]')){
      const review=data.current_review;form.elements.value_family.value=review.expected.family||'';modelChoices();form.elements.value_model_key.value=review.expected.model_key||'';
      for(const [key,value] of Object.entries(review.expected.attributes))form.elements['value_'+key].value=value??'';
      // Copy only human labels; every verification basis must be checked again.
      for(const node of form.querySelectorAll('[name^="basis_"]'))node.value='';
      feedback('Reviewed values copied. Recheck each verification basis and the current source.');
    }
    modelChecks();invalidatePreview();
  });
  root.addEventListener('input',invalidatePreview);
  root.addEventListener('change',event=>{if(event.target.name==='value_family'){modelChoices();const form=root.querySelector('form');form.elements.value_model_key.value='';form.elements.basis_model_key.value='';}modelChecks();invalidatePreview();});
  root.addEventListener('submit',event=>{
    const form=event.target.closest('[data-source-review-form]');if(!form)return;event.preventDefault();if(busy||!started||!form.reportValidity())return;
    const answers=sourceReviewAnswers(form,data.fields);run(async()=>{const result=await post('preview',{client_token:token,answers});root.querySelector('[data-review-preview]').innerHTML=sourceReviewPreview(result.result);proposal=result.result.valid?answers:null;});
  });
  dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  dialog.addEventListener('close',()=>{sequence++;controller?.abort();});
  const resume=pk=>{if(busy||data?.listing.listing_pk!==pk)return false;if(!dialog.open)dialog.showModal();return true;};
  return {open,resume,isBusy:()=>busy,listingPk:()=>data?.listing.listing_pk};
}
