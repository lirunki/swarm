const test=require('node:test');
const assert=require('node:assert/strict');
const {normalize,move,validToken,validProjection,TabPreferences}=require('../../src/swarm/static/tab-preferences.js');

const AGENT='agent:10000000-0000-4000-8000-000000000001';
const OTHER='agent:10000000-0000-4000-8000-000000000002';
const GROUP='agent-group:10000000-0000-4000-8000-000000000003';
const REQUEST='20000000-0000-4000-8000-000000000001';

class Element{
  constructor(){this.events=new Map();this.hidden=false;this.disabled=false;this.textContent='';this.children=[];this.attributes=new Map();}
  addEventListener(name,callback){this.events.set(name,callback);}
  click(extra={}){if(!this.disabled)return this.events.get('click')?.({target:this,stopPropagation(){},...extra});}
  setAttribute(name,value){this.attributes.set(name,String(value));}
  replaceChildren(...children){this.children=[...children];}
  append(...children){this.children.push(...children);}
}

function mapStorage(initial={}){
  const values=new Map(Object.entries(initial));
  return {values,storage:()=>({getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))})};
}

function memoryServer(initial={revision:0,starred:[]}){
  let state={revision:initial.revision,starred:[...initial.starred]};const calls=[];
  const api=(path,method,body)=>{
    calls.push([path,method,body]);
    if(!method)return Promise.resolve({revision:state.revision,starred:[...state.starred]});
    if(body.revision!==state.revision)throw Object.assign(Error('stale revision'),{status:409});
    const before=state.starred.slice();
    if(body.action==='star'&&!state.starred.includes(body.tab_id))state.starred.push(body.tab_id);
    if(body.action==='unstar')state.starred=state.starred.filter(value=>value!==body.tab_id);
    if(body.action==='import')for(const token of body.starred)if(!state.starred.includes(token))state.starred.push(token);
    if(JSON.stringify(before)!==JSON.stringify(state.starred))state.revision+=1;
    return Promise.resolve({revision:state.revision,starred:[...state.starred]});
  };
  return {api,calls,get state(){return {revision:state.revision,starred:[...state.starred]};},set(value){state={revision:value.revision,starred:[...value.starred]};}};
}

function fixture(options={}){
  const local=options.local||mapStorage();
  const nodes={
    '#starred-tabs-status':new Element(),
    '#starred-tabs-refresh':new Element(),
    '#starred-tabs-import':new Element(),
  };
  let source=options.source||'local',generation=options.generation||1;
  const errors=[],changed=[];
  const controller=new TabPreferences({
    query:selector=>nodes[selector]||null,storage:options.storage||local.storage,
    api:options.api||memoryServer().api,currentSwarm:()=>source,currentGeneration:()=>generation,
    onChanged:async(...args)=>{changed.push(args);await options.onChanged?.(...args);},onError:message=>errors.push(message),
    requestTimeout:options.requestTimeout??20000,requestId:options.requestId||(()=>REQUEST),confirm:options.confirm||(()=>true),
  });
  const sync=(projection={revision:0,starred:[]},error=null,key='prefs')=>controller.sync({key,items:options.items||[
    {id:AGENT,agentId:AGENT.slice(6),kind:'agent',name:'Agent',group:'Group'},
    {id:OTHER,agentId:OTHER.slice(6),kind:'agent',name:'Other',group:'Group'},
    {id:GROUP,kind:'folder',name:'Folder',group:'Folder'},
    {id:'browser',kind:'browser',name:'Browser',group:'Dashboard'},
  ],starred:projection,error});
  return {controller,nodes,errors,changed,local,sync,source(value){source=value;},generation(value){generation=value;}};
}

test('local preferences are bounded and token validation is strict',()=>{
  assert.deepEqual(normalize(null),{version:1,order:[],starred:[]});
  assert.deepEqual(normalize({version:1,order:['a','a',null,'bad\n'],starred:[AGENT,AGENT]}),{version:1,order:['a'],starred:[AGENT]});
  assert.equal(normalize({version:1,order:Array.from({length:600},(_,i)=>String(i))}).order.length,512);
  for(const token of [AGENT,GROUP,'browser'])assert.equal(validToken(token),true,token);
  for(const token of ['a','agent:a','folder:'+REQUEST,'agent:'+REQUEST+'x'])assert.equal(validToken(token),false,token);
  assert.equal(validProjection({revision:0,starred:[AGENT,'browser']}),true);
  assert.equal(validProjection({revision:0,starred:[AGENT,AGENT]}),false);
  assert.equal(validProjection({revision:'0',starred:[]}),false);
});

test('moving tabs does not lose hidden entries or mutate the source',()=>{
  const original=[AGENT,'agent:10000000-0000-4000-8000-000000000099',OTHER];
  assert.deepEqual(move(original,AGENT,OTHER,true),[original[1],OTHER,AGENT]);
  assert.equal(original[0],AGENT);
  assert.deepEqual(move(original,'missing',OTHER),original);
});

test('local order and legacy stars remain stored but never become canonical UI state',()=>{
  const local=mapStorage({prefs:JSON.stringify({version:1,order:[AGENT],starred:[AGENT]})});
  const t=fixture({local});t.controller.sync({key:'prefs',items:[],starred:undefined,error:null});
  assert.deepEqual(t.controller.value.order,[AGENT]);
  assert.deepEqual(t.controller.value.starred,[AGENT],'legacy data is retained for explicit migration');
  assert.deepEqual(t.controller.stars(),[],'legacy stars are not painted as shared authority');
  assert.equal(t.nodes['#starred-tabs-import'].hidden,false);
  assert.equal(t.nodes['#starred-tabs-import'].disabled,true,'an old server cannot be overwritten by migration');
  assert.match(t.nodes['#starred-tabs-status'].textContent,/not supported/i);
  t.controller.save({...t.controller.value,order:[OTHER]});
  assert.deepEqual(JSON.parse(local.values.get('prefs')).starred,[AGENT]);
});

test('two controllers mirror one backend-owned star without localStorage star writes',async()=>{
  const server=memoryServer(),storageA=mapStorage(),storageB=mapStorage();
  const a=fixture({api:server.api,local:storageA}),b=fixture({api:server.api,local:storageB});
  a.sync(server.state);b.sync(server.state);
  assert.equal(await a.controller.toggle(AGENT),true);
  b.sync(server.state);
  assert.deepEqual(a.controller.stars(),[AGENT]);assert.deepEqual(b.controller.stars(),[AGENT]);
  assert.equal(storageA.values.has('prefs'),false);assert.equal(storageB.values.has('prefs'),false);
  assert.deepEqual(server.calls[0][2],{action:'star',revision:0,request_id:REQUEST,tab_id:AGENT});
  assert.equal(a.changed.length,1);assert.equal(a.changed[0][0].revision,1);
});

test('a 409 refreshes once and requires another click rather than blindly retrying',async()=>{
  const server=memoryServer(),t=fixture({api:server.api});t.sync(server.state);
  server.set({revision:1,starred:['browser']});
  assert.equal(await t.controller.toggle(AGENT),false);
  assert.equal(server.calls.filter(call=>call[1]==='POST').length,1);
  assert.equal(server.calls.filter(call=>!call[1]).length,1);
  assert.deepEqual(t.controller.stars(),['browser']);
  assert.match(t.nodes['#starred-tabs-status'].textContent,/click again/i);
  assert.equal(await t.controller.toggle(AGENT),true);
  assert.equal(server.calls.filter(call=>call[1]==='POST').length,2);
  assert.deepEqual(server.state.starred,['browser',AGENT]);
});

test('a failed 409 refresh reports the GET failure instead of claiming refreshed state',async()=>{
  let posts=0,gets=0;
  const api=(path,method)=>{
    if(method){posts+=1;throw Object.assign(Error('stale revision'),{status:409});}
    gets+=1;throw Object.assign(Error('refresh denied'),{status:403});
  };
  const t=fixture({api});t.sync({revision:2,starred:['browser']});
  assert.equal(await t.controller.toggle(AGENT),false);
  assert.equal(posts,1);assert.equal(gets,1);
  assert.deepEqual(t.controller.stars(),['browser'],'a failed refresh does not invent new authority');
  assert.match(t.nodes['#starred-tabs-status'].textContent,/could not be refreshed.*refresh denied/i);
  assert.doesNotMatch(t.nodes['#starred-tabs-status'].textContent,/review the refreshed list/i);
});

test('captured federation scope drops stale completions and old finally cannot clear new busy',async()=>{
  let releaseOld,releaseNew;const calls=[];
  const api=(...args)=>{calls.push(args);return new Promise(resolve=>{if(!releaseOld)releaseOld=()=>resolve({revision:1,starred:[AGENT]});else releaseNew=()=>resolve({revision:1,starred:[OTHER]});});};
  const t=fixture({api,source:'old'});t.sync({revision:0,starred:[]});
  const old=t.controller.toggle(AGENT);
  assert.equal(calls[0][0],'/api/federation/old/proxy/api/starred-tabs','API dispatch captures the source synchronously');
  t.source('new');t.generation(2);t.sync({revision:0,starred:[]},null,'new-prefs');
  const current=t.controller.toggle(OTHER);
  assert.equal(calls[1][0],'/api/federation/new/proxy/api/starred-tabs');assert.equal(t.controller.busy,true);
  releaseOld();assert.equal(await old,false);assert.equal(t.controller.busy,true);
  releaseNew();assert.equal(await current,true);assert.equal(t.controller.busy,false);assert.deepEqual(t.controller.stars(),[OTHER]);
  assert.equal(t.changed.length,1);assert.equal(t.changed[0][1].source,'new');
});

test('unknown HTTP failures and timeouts expose request IDs, never retry, and require authoritative refresh',async()=>{
  let state={revision:0,starred:[]},postCalls=0;
  const api=(path,method)=>{
    if(!method)return state;
    postCalls+=1;state={revision:1,starred:[AGENT]};throw Object.assign(Error('gateway failed'),{status:500});
  };
  const t=fixture({api});t.sync({revision:0,starred:[]});
  assert.equal(await t.controller.toggle(AGENT),false);assert.equal(postCalls,1);
  assert.match(t.nodes['#starred-tabs-status'].textContent,new RegExp(`uncertain.*${REQUEST}`,'i'));
  assert.equal(await t.controller.toggle(OTHER),false);assert.equal(postCalls,1,'uncertain mutations are disabled');
  t.sync({revision:0,starred:[]});assert.ok(t.controller.uncertain,'same-revision polling does not clear uncertainty');
  assert.equal(await t.controller.refreshStars(),true);assert.equal(t.controller.uncertain,null);assert.deepEqual(t.controller.stars(),[AGENT]);

  const timed=fixture({api:()=>new Promise(()=>{}),requestTimeout:5});timed.sync({revision:0,starred:[]});
  assert.equal(await timed.controller.toggle(AGENT),false);
  assert.match(timed.nodes['#starred-tabs-status'].textContent,/uncertain.*Request ID.*timed out/i);
});

test('definite HTTP failures stay visible without uncertain edit locking or automatic retries',async()=>{
  let calls=0;
  const api=()=>{
    calls+=1;
    if(calls===1)throw Object.assign(Error('login required'),{status:403});
    return {revision:1,starred:[AGENT]};
  };
  const t=fixture({api});t.sync({revision:0,starred:[]});
  assert.equal(await t.controller.toggle(AGENT),false);
  assert.equal(calls,1,'a definite rejection is not retried automatically');
  assert.equal(t.controller.uncertain,null);assert.equal(t.controller.mutationsDisabled(),false);
  assert.match(t.nodes['#starred-tabs-status'].textContent,new RegExp(`failed.*${REQUEST}.*login required`,'i'));
  assert.equal(await t.controller.toggle(AGENT),true,'a later explicit user action remains possible');
  assert.equal(calls,2);assert.deepEqual(t.controller.stars(),[AGENT]);
});

test('malformed update acknowledgements are uncertain and retain the request lock',async()=>{
  let calls=0;const t=fixture({api:()=>{calls+=1;return {revision:1,starred:[]};}});t.sync({revision:0,starred:[]});
  assert.equal(await t.controller.toggle(AGENT),false);assert.equal(calls,1);
  assert.equal(t.controller.uncertain.requestId,REQUEST);assert.equal(t.controller.mutationsDisabled(),true);
  assert.match(t.nodes['#starred-tabs-status'].textContent,/uncertain.*Request ID/i);
  assert.equal(await t.controller.toggle(OTHER),false);assert.equal(calls,1);
});

test('websocket authority that advances before an older acknowledgement remains canonical',async()=>{
  let release;const t=fixture({api:()=>new Promise(resolve=>{release=resolve;})});t.sync({revision:2,starred:[]});
  const pending=t.controller.toggle(AGENT);
  t.sync({revision:4,starred:['browser']});
  release({revision:3,starred:[AGENT]});
  assert.equal(await pending,false);assert.deepEqual(t.controller.stars(),['browser']);assert.equal(t.changed.length,0);
});

test('legacy import is explicit, merges once, preserves backup, and never reseeds removed stars',async()=>{
  const local=mapStorage({prefs:JSON.stringify({version:1,order:[],starred:[AGENT,'invalid',GROUP,AGENT]})});
  const server=memoryServer({revision:2,starred:['browser']}),confirms=[];
  const t=fixture({api:server.api,local,confirm:message=>{confirms.push(message);return true;}});t.sync(server.state);
  assert.equal(server.calls.length,0,'sync never imports automatically');
  assert.equal(t.nodes['#starred-tabs-import'].hidden,false);
  assert.equal(await t.controller.importStars(),true);
  assert.match(confirms[0],/Import 2 local starred tabs.*merge/i);
  assert.deepEqual(server.calls[0][2],{action:'import',revision:2,request_id:REQUEST,starred:[AGENT,GROUP]});
  assert.equal(local.values.get('prefs.stars-imported'),'true');
  assert.deepEqual(JSON.parse(local.values.get('prefs')).starred,[AGENT,'invalid',GROUP,AGENT],'legacy backup is preserved');
  server.set({revision:4,starred:['browser',GROUP]});t.sync(server.state);
  assert.equal(t.nodes['#starred-tabs-import'].hidden,true);assert.deepEqual(t.controller.stars(),['browser',GROUP]);
  assert.equal(server.calls.length,1,'a removed legacy star is not automatically re-seeded');
});

test('lost import acknowledgement is marked imported after canonical GET confirmation',async()=>{
  const local=mapStorage({prefs:JSON.stringify({version:1,order:[],starred:[AGENT]})});
  const server=memoryServer({revision:2,starred:[AGENT]});let losePost=true;
  const api=async(...args)=>{
    const result=await server.api(...args);
    if(args[1]&&losePost){losePost=false;throw Error('connection lost after write');}
    return result;
  };
  const t=fixture({api,local});t.sync(server.state);
  assert.equal(await t.controller.importStars(),false);
  assert.ok(t.controller.uncertain);assert.equal(server.state.revision,2,'a no-op import intentionally keeps its revision');
  assert.equal(local.values.has('prefs.stars-imported'),false);
  assert.equal(await t.controller.refreshStars(),true,'an explicit same-revision GET can resolve canonical intent');
  assert.equal(t.controller.uncertain,null);assert.equal(local.values.get('prefs.stars-imported'),'true');
  server.set({revision:3,starred:[]});t.sync(server.state);
  assert.equal(t.nodes['#starred-tabs-import'].hidden,true);
  assert.equal(await t.controller.importStars(),false);
  assert.equal(server.calls.filter(call=>call[1]==='POST').length,1,'a later server-side removal is not re-seeded in this browser');
});

test('corrupt projections disable mutations while storage failures do not block canonical stars',async()=>{
  const server=memoryServer({revision:3,starred:[AGENT]});
  const broken=()=>({getItem(){throw Error('blocked');},setItem(){throw Error('blocked');}});
  const t=fixture({api:server.api,storage:broken});t.sync(server.state);
  assert.deepEqual(t.controller.stars(),[AGENT]);
  assert.equal(await t.controller.toggle(AGENT),true,'shared state works even when browser storage is unavailable');
  assert.deepEqual(server.state.starred,[]);

  t.sync({revision:'bad',starred:[AGENT]});
  assert.equal(t.controller.projectionValid,false);assert.equal(await t.controller.toggle(AGENT),false);
  assert.match(t.nodes['#starred-tabs-status'].textContent,/invalid/i);assert.equal(server.calls.length,1);
});

test('failed migration marker storage keeps import visible but only explicit clicks can repeat the harmless merge',async()=>{
  const persisted=JSON.stringify({version:1,order:[],starred:[AGENT]});
  const storage=()=>({getItem:key=>key==='prefs'?persisted:null,setItem(){throw Error('blocked');}});
  const server=memoryServer();const t=fixture({api:server.api,storage});t.sync(server.state);
  assert.equal(await t.controller.importStars(),true);assert.equal(t.nodes['#starred-tabs-import'].hidden,false);
  assert.equal(server.calls.length,1);
  t.sync(server.state);assert.equal(server.calls.length,1,'render and sync do not repeat an import');
  assert.equal(await t.controller.importStars(),true);assert.equal(server.calls.length,2,'a second merge requires another explicit confirmation/click');
});
