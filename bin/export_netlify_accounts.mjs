#!/usr/bin/env node
// Export the selected site's current hashes. Sessions and login events stay behind.
import {readFile,writeFile,mkdir,chmod} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join,dirname} from 'node:path';
import {parseArgs} from 'node:util';
import {getStore} from '@netlify/blobs';

const {values}=parseArgs({options:{site:{type:'string'},output:{type:'string'}}});
if(!values.site||!values.output)throw new Error('Provide --site and a private --output path.');
const directory=process.platform==='darwin'?join(homedir(),'Library','Preferences','netlify'):join(process.env.XDG_CONFIG_HOME||join(homedir(),'.config'),'netlify');
let token=process.env.NETLIFY_AUTH_TOKEN;
if(!token){
  const config=JSON.parse(await readFile(join(directory,'config.json'),'utf8'));
  token=config.users?.[config.userId]?.auth?.token;
}
if(!token)throw new Error('Authenticate the Netlify CLI first.');
const store=getStore({name:'tori-auth',siteID:values.site,token,region:'eu-central-1',consistency:'strong'});
const state=await store.get('state',{type:'json'});
if(!Array.isArray(state?.users)||!state.users.some(user=>user.role==='developer'&&user.active))throw new Error('This site has no active developer account.');
const keys=['user_id','username','password_hash','role','active','must_change_password','created_at'];
const users=state.users.map(user=>Object.fromEntries(keys.map(key=>[key,user[key]])));
await mkdir(dirname(values.output),{recursive:true,mode:0o700});
await writeFile(values.output,JSON.stringify({users})+'\n',{mode:0o600,flag:'wx'});
await chmod(values.output,0o600);
console.log(JSON.stringify({site_id:values.site,accounts:users.length,output:values.output}));
