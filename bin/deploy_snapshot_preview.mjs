#!/usr/bin/env node
// Create a separate snapshot preview. Never alter the linked production site.
import {readFile,writeFile,mkdir,access,chmod} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {randomBytes} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {gzipSync} from 'node:zlib';
import {getStore} from '@netlify/blobs';
import {newUser} from '../netlify/lib/auth.mjs';
import {publishSnapshot,validateSnapshot} from '../netlify/lib/data.mjs';
import {publishWorkspace,validateWorkspace} from '../netlify/lib/workspace.mjs';

const root=new URL('../',import.meta.url);
const directory=new URL('../data/netlify-preview/',import.meta.url);
const credentials=new URL('credentials.json',directory);
await mkdir(directory,{recursive:true,mode:0o700});
await chmod(directory,0o700);
try{await access(credentials);throw new Error('A preview already exists. Use its explicit site ID to update it.');}
catch(error){if(error.code!=='ENOENT')throw error;}
const snapshot=validateSnapshot(JSON.parse(await readFile(new URL('snapshot.json',directory),'utf8')));
const workspace=validateWorkspace(JSON.parse(await readFile(new URL('workspace.json',directory),'utf8')));
const build=spawnSync('npm',['run','build'],{cwd:root,stdio:'inherit'});
if(build.status!==0)throw new Error('The strict build must pass before creating a preview.');
const release=JSON.parse(await readFile(new URL('../dist/netlify/build-manifest.json',import.meta.url),'utf8')).release;
const folder=process.platform==='darwin'?join(homedir(),'Library','Preferences','netlify'):join(process.env.XDG_CONFIG_HOME||join(homedir(),'.config'),'netlify');
const config=JSON.parse(await readFile(join(folder,'config.json'),'utf8'));
const token=process.env.NETLIFY_AUTH_TOKEN||config.users?.[config.userId]?.auth?.token;
if(!token)throw new Error('Authenticate the Netlify CLI first.');
const name='tori-research-preview-'+randomBytes(4).toString('hex');
const response=await fetch('https://api.netlify.com/api/v1/sites',{
  method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({name})});
if(!response.ok)throw new Error('Could not create the isolated preview: HTTP '+response.status);
const site=await response.json();
const linked=JSON.parse(await readFile(new URL('../.netlify/state.json',import.meta.url),'utf8'));
if(site.id===linked.siteId)throw new Error('Refusing to provision the production site.');
const origin=site.ssl_url||'https://'+name+'.netlify.app';
const accounts=[];
for(const [username,role] of [['developer','developer'],['viewer','viewer']]){
  accounts.push({username,role,password:randomBytes(24).toString('base64url')});
}
await writeFile(credentials,JSON.stringify({site_id:site.id,origin,accounts},null,2)+'\n',{mode:0o600,flag:'wx'});
await writeFile(new URL('site.json',directory),JSON.stringify({site_id:site.id,origin,release},null,2)+'\n',{mode:0o600,flag:'wx'});
const users=await Promise.all(accounts.map(row=>newUser(row.username,row.password,row.role)));
const authStore=getStore({name:'tori-auth',siteID:site.id,token,region:'eu-central-1',consistency:'strong'});
if(await authStore.get('state'))throw new Error('Refusing to replace existing preview accounts.');
await authStore.setJSON('state',{version:1,users,sessions:[],attempts:{},events:[]},{onlyIfNew:true});
const dataStore=getStore({name:'tori-research',siteID:site.id,token,region:'eu-central-1',consistency:'strong'});
await publishSnapshot(dataStore,gzipSync(JSON.stringify(snapshot)),'preview-owner');
await publishWorkspace(dataStore,gzipSync(JSON.stringify(workspace)));
const result=spawnSync('node',[new URL('../node_modules/netlify-cli/bin/run.js',import.meta.url).pathname,
  'deploy','--prod','--no-build','--skip-functions-cache','--site',site.id,'--dir','dist/netlify','--functions','netlify/functions',
  '--env','TORI_BACKEND=blobs','--env','TORI_PUBLIC_ORIGIN='+origin,
  '--env','TORI_RELEASE_SCHEMA_VERSION='+release.schema_version,
  '--env','TORI_RELEASE_SOURCE_HASH='+release.source_hash,'--json'],{cwd:root,encoding:'utf8'});
if(result.status!==0)throw new Error('Preview deployment failed. Its site ID is saved in data/netlify-preview/site.json.');
const deployed=JSON.parse(result.stdout);
console.log(JSON.stringify({origin,site_id:site.id,deploy_id:deployed.deploy_id,
  observations:snapshot.listings.length,generated_at:snapshot.generated_at,
  credentials_file:'data/netlify-preview/credentials.json'},null,2));
