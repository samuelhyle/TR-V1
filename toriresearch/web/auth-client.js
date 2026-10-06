import {call, setSession} from '/static/api.js';

export function workspacePath(user) {
  if (['/developer', '/seller', '/reader', '/viewer'].includes(user?.workspace_path)) return user.workspace_path;
  return user?.role === 'developer' ? '/developer' : user?.role === 'seller' ? '/seller' : '/viewer';
}

export async function session(role) {
  // `/api/auth/session` answers 200 either way: a signed-out visitor is not an
  // error here, and a 401 would be logged by the browser on every page load.
  const data = await call('/api/auth/session');
  if (!data.user) { location.replace('/login'); return null; }
  setSession(data.csrf_token);
  if (data.user.must_change_password) {
    location.replace('/login#password');
    return null;
  }
  if (role && !(Array.isArray(role) ? role : [role]).includes(data.user.role)) {
    location.replace(workspacePath(data.user));
    return null;
  }
  return data.user;
}

export function accountControls(user, container) {
  container.innerHTML = '<span data-account-name></span> <button type="button" data-password>Change password</button> <button type="button" data-logout>Sign out</button>';
  container.querySelector('[data-account-name]').textContent = user.username + ' · ' + user.role;
  container.querySelector('[data-password]').onclick = () => location.assign('/login#password');
  container.querySelector('[data-logout]').onclick = async () => {
    try { await call('/api/auth/logout', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); }
    finally { setSession(''); location.replace('/login'); }
  };
}
