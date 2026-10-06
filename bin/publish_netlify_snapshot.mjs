#!/usr/bin/env node
// Publish private research using the owner's Netlify CLI login. No credentials
// or data are added to the static deploy or logged by this command.
import {readFile} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {getStore} from '@netlify/blobs';
import {publishSnapshot} from '../netlify/lib/data.mjs';

let token=process.env.NETLIFY_AUTH_TOKEN;
if(!token) {
  const folder=process.platform==='darwin'?join(homedir(),'Library','Preferences','netlify'):process.platform==='win32'?join(process.env.APPDATA||homedir(),'netlify','Config'):join(process.env.XDG_CONFIG_HOME||join(homedir(),'.config'),'netlify');
  const config=JSON.parse(await readFile(join(folder,'config.json'),'utf8'));
  token=config.users?.[config.userId]?.auth?.token;
}
if(!token)throw new Error('Run netlify login or set NETLIFY_AUTH_TOKEN first.');
const state=JSON.parse(await readFile(new URL('../.netlify/state.json',import.meta.url),'utf8'));
const store=getStore({name:'tori-research',siteID:state.siteId,token,region:'eu-central-1',consistency:'strong'});
const file=process.argv[2]||new URL('../data/netlify/snapshot.json.gz',import.meta.url);
const result=await publishSnapshot(store,await readFile(file),'netlify-owner');
console.log(JSON.stringify({observations:result.observations,generated_at:result.generated_at,uploaded_at:result.uploaded_at}));
