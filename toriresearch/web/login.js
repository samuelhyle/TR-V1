import {call, setSession} from '/static/api.js';
import {workspacePath} from '/static/auth-client.js';
const login = document.getElementById('login-form');
const password = document.getElementById('password-form');
const message = document.getElementById('login-message');
let user;
function showPassword() { login.hidden=true; password.hidden=false; document.getElementById('login-title').textContent='Choose your password'; }
function destination() { location.replace(workspacePath(user)); }
async function check() {
  try {
    // 200 either way, so a signed-out visitor sees no failed request in the console.
    const data=await call('/api/auth/session');
    if(!data.user)return;
    user=data.user; setSession(data.csrf_token);
    if(user.must_change_password || location.hash==='#password') showPassword(); else destination();
  } catch(error) {message.textContent=error.message;message.hidden=false;}
}
function submit(form, task) {
  form.addEventListener('submit', async event => {
    event.preventDefault(); const button=form.querySelector('button[type="submit"]'); button.disabled=true; message.hidden=true;
    try { await task(); } catch(error) {message.textContent=error.message;message.hidden=false;} finally {button.disabled=false;}
  });
}
submit(login, async () => {
  const data=await call('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:login.elements.username.value, password:login.elements.password.value})});
  user=data.user; setSession(data.csrf_token); login.elements.password.value='';
  if(user.must_change_password) showPassword(); else destination();
});
submit(password, async () => {
  if(password.elements.new_password.value!==password.elements.confirm_password.value) throw new Error('The new passwords do not match.');
  await call('/api/auth/password', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({current_password:password.elements.current_password.value, new_password:password.elements.new_password.value})});
  setSession(''); password.reset(); password.hidden=true; login.hidden=false;
  document.getElementById('login-title').textContent='Sign in to your workspace';
  message.textContent='Password changed. Sign in with your new password.'; message.hidden=false;
});
check();
