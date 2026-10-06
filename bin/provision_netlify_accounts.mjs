#!/usr/bin/env node
// Bootstrap a new site's accounts once. Upload only auth-state.json to the
// private tori-auth Blobs store. Never upload credentials.json or publish it.
import {mkdir,writeFile,access} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import {newUser} from '../netlify/lib/auth.mjs';

const directory=new URL('../data/netlify/',import.meta.url);
await mkdir(directory,{recursive:true,mode:0o700});
const file=new URL('credentials.json',directory);
try {await access(file);throw new Error('Credentials already exist. Use developer account management for resets.');}
catch(error){if(error.code!=='ENOENT')throw error;}
const accounts=[];
for(const [username,role] of [['developer','developer'],['seller','viewer']])accounts.push({username,role,password:randomBytes(24).toString('base64url')});
const users=await Promise.all(accounts.map(row=>newUser(row.username,row.password,row.role)));
await writeFile(file,JSON.stringify({site:'https://tori-research-workspace.netlify.app',accounts},null,2)+'\n',{mode:0o600,flag:'wx'});
await writeFile(new URL('auth-state.json',directory),JSON.stringify({version:1,users,sessions:[],attempts:{},events:[]}),{mode:0o600,flag:'wx'});
console.log('Created developer and seller accounts. Temporary passwords are in data/netlify/credentials.json (owner access only).');
