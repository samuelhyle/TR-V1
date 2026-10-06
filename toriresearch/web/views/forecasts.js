import {esc,date,eur,num} from '../format.js';
import {badge,metric} from '../components.js';

const forecastLabels={sale_probability:'Sale-label probability',price_trajectory:'Asking-price path'};
const forecastDate=value=>value?date(value):'Unrecorded';
const forecastNumber=value=>value==null?'Unknown':num(value,4);
const forecastCheck=value=>value===true?'Passed':value===false?'Failed':'Unrecorded';
const forecastValue=card=>card.family==='sale_probability'?(card.output.p_sale==null?'Unknown':num(card.output.p_sale*100,1)+'%'):eur(card.output.predicted_price_eur);

function forecastScore(score,family){
  const price=family==='price_trajectory';
  return `<div class="metric-list">${metric('Held-out observations',forecastNumber(score.n_test))}${metric('Followed to horizon',forecastNumber(score.n_horizon_observed))}${metric(price?'Ratio MAE / no-change baseline':'Brier score / incidence baseline',forecastNumber(score[price?'mae_price_ratio':'brier_score'])+' / '+forecastNumber(score[price?'baseline_mae_price_ratio':'baseline_brier_score']))}${metric('Baseline improvement',forecastCheck(score.improves_baseline))}${metric('Calibration',forecastCheck(score.calibration_ok))}${metric(price?'90% interval coverage':'Calibration error',forecastNumber(score[price?'coverage_90':'calibration_error']))}${metric('Holdout starts',forecastDate(score.test_start_at))}${metric('Training boundary',forecastDate(score.train_end_at))}</div>${score.reason?`<p class="note">${esc(score.reason)}</p>`:''}`;
}

export function forecastCard(card){
  const pop=card.population,training=card.training,evaluation=card.evaluation,validation=card.validation;
  const price=card.family==='price_trajectory';
  const interval=price?[card.output.price_ci_lo,card.output.price_ci_hi]:[card.output.p_sale_ci_lo,card.output.p_sale_ci_hi];
  const intervalText=interval.some(value=>value==null)?'Unrecorded':price?interval.map(eur).join('–'):interval.map(value=>num(value*100,1)+'%').join('–');
  return `<details class="forecast-card"><summary><strong>${esc(forecastLabels[card.family])} · ${num(card.horizon_days)} days</strong> ${badge('Research only','amber')}<span class="note">${esc(pop.category||'Category unknown')} / ${esc(pop.condition||'Condition unknown')} · ${card.current_method?forecastValue(card):'Original semantics unverified'}</span><span class="note">Computed ${forecastDate(card.computed_at)} · horizon / segment holdout ${forecastNumber(evaluation.horizon.n_test)} / ${forecastNumber(evaluation.segment.n_test)} · ${validation.is_accepted?'Chronological checks passed':'Chronological approval withheld'}</span></summary><p>${esc(card.target)}</p><p class="note">${esc(card.horizon_basis)} · computed ${forecastDate(card.computed_at)} · horizon ends ${forecastDate(card.target_end_at)}. This retained output does not update as time passes.</p><div class="metric-list">${metric('Retained model output',forecastValue(card))}${metric(card.current_method?(price?'90% predictive interval':'Recorded probability interval'):'Original stored interval',intervalText)}${metric('Frozen category / condition',esc(pop.category||'Unknown')+' / '+esc(pop.condition||'Unknown'))}${metric('Conditional listing age',pop.age_days==null?'Not recorded / not applicable':num(pop.age_days,2)+' days')}${metric('Segment follow-up support',pop.max_duration_days==null?'Unknown':num(pop.max_duration_days,2)+' days')}${metric('Extrapolation',validation.is_extrapolated==null?'Unknown':validation.is_extrapolated?'Outside validated support':'Within chronological support')}</div><p class="note">${esc(pop.basis)}${pop.current_identity_differs?' Current source identity differs from these frozen inputs.':''}</p><div class="validation-callout amber"><strong>Purchase guidance withheld</strong><p>${esc(card.guidance.reason)}</p>${validation.withholding_reasons.map(reason=>`<p>${esc(reason)}</p>`).join('')}</div><h3>Training evidence</h3><p class="model-version">Fit ${esc(card.model_version)}<br>Method ${esc(card.method_version||'Unregistered')}</p><div class="metric-list">${metric('Trained',forecastDate(training.trained_at))}${metric('Training window',forecastDate(training.train_start_at)+' → '+forecastDate(training.train_end_at))}${metric('Observations / entities',forecastNumber(training.n_observations)+' / '+forecastNumber(training.n_entities))}</div><details class="forecast-evaluation"><summary>Inspect current registered evaluation</summary><p class="note">${validation.is_accepted?'Chronological checks pass for this retained forecast.':'Chronological approval is withheld.'} Current policy ${esc(validation.policy_version)}; recorded policy ${esc(validation.recorded_policy_version||'Unrecorded')}.</p><h3>This horizon</h3>${forecastScore(evaluation.horizon,card.family)}<h3>This frozen category / condition</h3>${forecastScore(evaluation.segment,card.family)}${Object.keys(evaluation.age_checkpoints).length?`<h3>Recorded age checkpoints</h3>${Object.entries(evaluation.age_checkpoints).map(([age,score])=>`<details><summary>${esc(age)} days at forecast origin</summary>${forecastScore(score,card.family)}</details>`).join('')}`:''}<p class="model-version">Registered evaluation fingerprint ${esc(evaluation.fingerprint)}</p><p class="note">The fingerprint identifies this reading of registered evidence. It is not a forward-validation approval.</p></details><div class="unit-actions"><button type="button" class="btn small" data-forecast-model="${esc(card.family)}" data-forecast-version="${esc(card.model_version)}" data-forecast-horizon="${card.horizon_days}" data-forecast-listing="${card.listing_pk}">Open this fit and supporting observations</button></div></details>`;
}

export function forecastEvidence(data){
  if(!data?.items?.length)return `<h3>Forecast evidence</h3><p class="note">${esc(data?.note||'No retained marketplace forecast source.')}</p>`;
  return `<h3>Retained forecast evidence</h3><p class="note">${esc(data.note)}</p>${data.items.map(item=>`<section class="forecast-source"><h3>${esc(item.listing?.title||'Unavailable marketplace source')} · #${item.listing_pk}</h3>${item.reason?`<p class="note">${esc(item.reason)}</p>`:''}${item.cards.map(forecastCard).join('')}${(item.missing_families||[]).map(row=>`<div class="forecast-card"><strong>${esc(forecastLabels[row.family])}</strong><p class="note">${esc(row.reason)}. ${esc(row.target)}.</p></div>`).join('')}</section>`).join('')}`;
}

/** A GET-only reader mounted separately from the operator's unfinished forms. */
export function initForecastEvidence({dialog,root,call,onModel}){
  let sequence=0,controller=null,opener=null;
  const close=()=>{sequence++;controller?.abort();dialog.close();opener?.focus();};
  root.addEventListener('click',event=>{
    if(event.target.closest('[data-forecast-close]')){close();return;}
    const button=event.target.closest('[data-forecast-model]');
    if(button){close();onModel?.({family:button.dataset.forecastModel,version:button.dataset.forecastVersion,horizon:Number(button.dataset.forecastHorizon),listing_pk:Number(button.dataset.forecastListing)});}
  });
  dialog.addEventListener('close',()=>{sequence++;controller?.abort();});
  return {open:async(keys,label='Selected marketplace offers')=>{
    const ticket=++sequence;controller?.abort();controller=new AbortController();opener=document.activeElement;
    const header=`<div class="dialog-head"><div><div class="eyebrow">Retained model research</div><h2>${esc(label)}</h2></div><button class="btn quiet small" type="button" data-forecast-close aria-label="Close forecast evidence">×</button></div>`;
    root.innerHTML=header+'<p class="note" role="status">Reading stored outputs and current registered evaluation…</p>';if(!dialog.open)dialog.showModal();
    try{const data=await call('/api/forecast-evidence?'+new URLSearchParams({listing_pks:keys.join(',')}),{signal:controller.signal});if(ticket===sequence)root.innerHTML=header+'<p class="note">These are individual source forecasts, with separate populations and horizons. They are not a product-level forecast.</p>'+forecastEvidence(data);}
    catch(error){if(ticket===sequence&&error.name!=='AbortError')root.innerHTML=header+`<p class="message error" role="alert">${esc(error.message)}</p>`;}
  }};
}
