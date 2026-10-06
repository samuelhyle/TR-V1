import {call} from '/static/api.js';
import {session, accountControls} from '/static/auth-client.js';
import {esc} from '/static/format.js';

const user=await session(['developer', 'seller']);
if(user) {
  await import('/static/dashboard.js');
  const controls=document.createElement('div'); controls.className='auth-controls';
  document.getElementById('sidebar').append(controls); accountControls(user, controls);
  if(user.role==='developer') {
  const capabilities=await call('/api/capabilities');
  const roles=capabilities.account_roles||['developer','viewer'];
  const button=document.createElement('button'); button.type='button';button.className='btn small';button.textContent='Manage users';controls.prepend(button);
  const dialog=document.createElement('dialog');dialog.className='inventory-dialog';dialog.setAttribute('aria-label','Application accounts');
  dialog.innerHTML='<div class="dialog-body"><div class="dialog-head"><h2>Application accounts</h2><button type="button" data-close>Close</button></div><p>Viewers can read shared business data. Developers can manage this workspace.</p><div data-users></div><h3>Create account</h3><form data-create><label class="field">Username<input name="username" required minlength="3" maxlength="150" autocomplete="off"></label><label class="field">Role<select name="role"><option value="viewer">Seller / viewer · read only</option><option value="developer">Developer · full access</option></select></label><label class="field">Temporary password<input name="password" type="password" required minlength="15" maxlength="256" autocomplete="new-password"></label><p>The user must change this password at first login.</p><button class="btn primary" type="submit">Create account</button></form><p data-message role="status"></p><h3>Recent security activity</h3><div data-activity></div></div>';
  document.body.append(dialog);
  if(!capabilities.writes?.includes('/api/auth/users')) {
    dialog.querySelector('[data-create]').hidden=true;
    dialog.querySelector('[data-message]').textContent='Account changes are disabled during read-only verification.';
  }
  dialog.querySelector('[name="role"]').innerHTML=roles.map(role=>'<option value="'+esc(role)+'">'+esc(role)+'</option>').join('');
  const message=text=>dialog.querySelector('[data-message]').textContent=text;
  async function load() {
    const [accounts,activity]=await Promise.all([call('/api/auth/users'),call('/api/auth/activity')]);
    dialog.querySelector('[data-users]').innerHTML=accounts.users.map(row=>'<div class="card" data-user="'+esc(row.user_id)+'"><strong>'+esc(row.username)+'</strong><p>'+esc(row.role)+' · '+(row.active?'Active':'Disabled')+'</p><button type="button" data-active="'+(!row.active)+'">'+(row.active?'Disable':'Enable')+'</button> <label>Role<select data-user-role>'+roles.map(role=>'<option value="'+esc(role)+'"'+(row.role===role?' selected':'')+'>'+esc(role)+'</option>').join('')+'</select></label><button type="button" data-role>Save role</button><form data-reset><label class="field">New temporary password<input name="password" type="password" required minlength="15" maxlength="256" autocomplete="new-password"></label><button type="submit">Reset password</button></form></div>').join('');
    if(!capabilities.writes?.includes('/api/auth/users'))dialog.querySelectorAll('[data-users] button,[data-users] input,[data-users] select').forEach(node=>node.disabled=true);
    dialog.querySelector('[data-activity]').innerHTML=activity.events.slice(0,20).map(row=>'<p>'+esc(row.occurred_at)+' · '+esc(row.event_type)+'</p>').join('')||'<p>No activity yet.</p>';
  }
  const post=async(action,params)=>{await call('/api/auth/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params})});await load();};
  button.onclick=async()=>{dialog.showModal();try{await load();}catch(error){message(error.message);}};
  dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  dialog.addEventListener('click',async event=>{
    const action=event.target.closest('[data-active],[data-role]');if(!action)return;
    const params={user_id:action.closest('[data-user]').dataset.user};
    if(action.hasAttribute('data-active'))params.active=action.dataset.active==='true';else params.role=action.closest('[data-user]').querySelector('[data-user-role]').value;
    action.disabled=true;try{await post('update',params);message('Account updated. Existing sessions were revoked.');}catch(error){message(error.message);action.disabled=false;}
  });
  dialog.addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target,button=form.querySelector('button[type="submit"]');button.disabled=true;
    try {
      if(form.hasAttribute('data-create'))await post('create',{username:form.elements.username.value,role:form.elements.role.value,password:form.elements.password.value});
      else await post('update',{user_id:form.closest('[data-user]').dataset.user,password:form.elements.password.value});
      form.reset();message('Account saved. Share its temporary password privately.');
    }catch(error){message(error.message);}finally{button.disabled=false;}
  });
  }
}
