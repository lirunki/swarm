const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');

test('top-level selector switches API, state, and terminal paths by Swarm', () => {
  assert.match(html, /id="swarm-switcher"[\s\S]*id="swarm-selector"/);
  assert.match(source, /const apiPath = path => activeSwarm === 'local' \? path : `\/api\/federation\/\$\{encodeURIComponent\(activeSwarm\)\}\/proxy\$\{path\}`/);
  assert.match(source, /const stateWSPath = \(\) => activeSwarm === 'local' \? '\/ws\/state' : `\/ws\/federation/);
  assert.match(source, /const terminalWSPath = id => activeSwarm === 'local' \? `\/ws\/terminal/);
  assert.match(source, /if\(uiViewStateRestored\) rememberUIViewState\(\);\s*for \(const id of \[\.\.\.terminals\.keys\(\)\]\) closeTerminal\(id, false\)/);
  assert.match(source, /activate\('dashboard', false\); render\(\)/);
});

test('switching Swarms preserves each browser-local UI view state independently', () => {
  const remoteView = {terminalTabs: {ids: ['remote-a'], active: 'remote-a'}, groupedBranches: ['remote-parent']};
  const values = new Map([['swarm.ui-view-state.remote', JSON.stringify(remoteView)]]);
  const calls = [], groupedBranches = new Set(['local-parent']);
  const context = vm.createContext({
    selectedAgentGroup:null,openedAgentGroups:new Set(),agentGroupTabs:new Map(),isAgentGroupTab:()=>false,SwarmAgentGroups:{validProjection:()=>false},
    SwarmGroupingMode:{validSettings:()=>false},syncGroupingStorage(){},serverManagedBranches:()=>false,groupingBranchOperation:0,browserGroupingOperation:0,groupingBranchBusy:false,browserGroupingProjection:null,browserGroupingError:null,
    activeSwarm: 'local', activeTab: 'local-b', terminals: new Map([['local-a', {}], ['local-b', {}]]), groupedBranches,
    localStorage: {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)},
    notificationsInbox: {clear: () => calls.push(['clear'])}, document: {querySelectorAll: () => []}, stateSocket: null,
    stateGeneration: 0, reconnectDelay: 1000, uiViewStateRestored: true,
    state: {}, dashboardControls: {status: {reset: () => {}}},
    $: selector => selector === '#swarm-selector'
      ? {value: '', title: '', selectedOptions: [{textContent: 'Remote'}]}
      : {value: ''},
    closeTerminal: (id, persist) => { calls.push(['close', id, persist]); context.terminals.delete(id); },
    activate: (id, persist) => calls.push(['activate', id, persist]), render: () => {}, updateRestartCapability: () => {},
    connectState: () => {}, updateNotificationSources: () => {},
  });
  const memory = source.slice(source.indexOf('const uiViewStateStorageKey'), source.indexOf('function applyState'));
  const switching = source.slice(source.indexOf('function switchSwarm('), source.indexOf('function revealNotificationAgent'));
  vm.runInContext(`${memory}\n${switching}`, context);
  context.switchSwarm('remote');

  assert.deepEqual(JSON.parse(values.get('swarm.ui-view-state.local')), {
    terminalTabs: {ids: ['local-a', 'local-b'], active: 'local-b'}, groupedBranches: ['local-parent'], agentGroupView:{selected:null,opened:[]},
  });
  assert.deepEqual(JSON.parse(values.get('swarm.ui-view-state.remote')), remoteView);
  assert.deepEqual([...groupedBranches], []);
  assert.deepEqual(calls.filter(call => call[0] === 'close'), [['close', 'local-a', false], ['close', 'local-b', false]]);
  assert.ok(calls.some(call => call[0] === 'activate' && call[1] === 'dashboard' && call[2] === false));
});

test('first state restores available tabs, active tab, and grouped branches before rendering', () => {
  const saved = {terminalTabs: {ids: ['available', 'removed'], active: 'available'}, groupedBranches: ['parent', 'removed-parent']};
  const values = new Map([['swarm.ui-view-state.remote', JSON.stringify(saved)]]);
  const opened = [], activated = [], renderedGroups = [], groupedBranches = new Set();
  const context = vm.createContext({
    selectedAgentGroup:null,openedAgentGroups:new Set(),agentGroupTabs:new Map(),isAgentGroupTab:()=>false,SwarmAgentGroups:{validProjection:()=>false},
    SwarmGroupingMode:{validSettings:()=>false},syncGroupingStorage(){},serverManagedBranches:()=>false,groupingBranchOperation:0,browserGroupingOperation:0,groupingBranchBusy:false,browserGroupingProjection:null,browserGroupingError:null,
    activeSwarm: 'remote', activeTab: 'dashboard', terminals: new Map(), groupedBranches, uiViewStateRestored: false, state: {agents: []},
    localStorage: {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)},
    fleetControl: null, agentById: id => context.state.agents.find(agent => agent.id === id), closeTerminal: () => {},
    render: () => renderedGroups.push([...groupedBranches]),
    openTerminal: (id, focus) => { opened.push([id, focus]); context.terminals.set(id, {}); },
    activate: id => activated.push(id), consumeNotificationReveal: () => {}, notificationsInbox: null,
  });
  const restore = source.slice(source.indexOf('const uiViewStateStorageKey'), source.indexOf('async function refresh'));
  vm.runInContext(restore, context);
  context.applyState({agents: [{id: 'available'}, {id: 'parent'}]});

  assert.deepEqual(renderedGroups[0], ['parent', 'removed-parent']);
  assert.deepEqual(opened, [['available', false]]);
  assert.deepEqual(activated, ['available']);
  assert.equal(context.uiViewStateRestored, true);

  values.set('swarm.ui-view-state.remote', '{invalid json');
  context.terminals.clear(); context.uiViewStateRestored = false; opened.length = 0; activated.length = 0; groupedBranches.clear();
  assert.doesNotThrow(() => context.applyState({agents: [{id: 'available'}]}));
  assert.deepEqual(opened, []); assert.deepEqual(activated, ['dashboard']); assert.deepEqual([...groupedBranches], []);
  assert.equal(context.uiViewStateRestored, true);
});

test('legacy per-Swarm tab storage migrates into UI view state', () => {
  const values = new Map([['swarm.tabs.remote', JSON.stringify({ids: ['available'], active: 'available'})]]);
  const groupedBranches = new Set(), context = vm.createContext({
    selectedAgentGroup:null,openedAgentGroups:new Set(),agentGroupTabs:new Map(),isAgentGroupTab:()=>false,SwarmAgentGroups:{validProjection:()=>false},
    SwarmGroupingMode:{validSettings:()=>false},syncGroupingStorage(){},serverManagedBranches:()=>false,groupingBranchOperation:0,browserGroupingOperation:0,groupingBranchBusy:false,browserGroupingProjection:null,browserGroupingError:null,
    activeSwarm: 'remote', activeTab: 'dashboard', terminals: new Map(), groupedBranches, uiViewStateRestored: false, state: {agents: []},
    localStorage: {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)},
    fleetControl: null, agentById: id => context.state.agents.find(agent => agent.id === id), closeTerminal: () => {}, render: () => {},
    openTerminal: id => { context.terminals.set(id, {}); context.rememberUIViewState(); },
    activate: id => { context.activeTab = id; context.rememberUIViewState(); }, consumeNotificationReveal: () => {}, notificationsInbox: null,
  });
  const restore = source.slice(source.indexOf('const uiViewStateStorageKey'), source.indexOf('async function refresh'));
  vm.runInContext(restore, context);
  context.applyState({agents: [{id: 'available'}]});

  assert.deepEqual(JSON.parse(values.get('swarm.ui-view-state.remote')), {
    terminalTabs: {ids: ['available'], active: 'available'}, groupedBranches: [], agentGroupView:{selected:null,opened:[]},
  });
  assert.equal(values.has('swarm.tabs.remote'), false);
});

test('remote restart UI obeys advertised capability', () => {
  assert.match(source, /capabilities\?\.remote_restart/);
  assert.match(source, /Remote restart is disabled on this Swarm/);
  assert.match(source, /--allow-remote-restart/);
});

test('federation discovery always uses local backend rather than recursive proxy', () => {
  assert.match(source, /localApi\('\/api\/federation\/nodes'\)/);
  assert.match(source, /authenticatedSocketURL\('\/ws\/federation\/presence'\)/);
});


test('Swarm picker follows the logo without a redundant visible label', () => {
  const group = html.slice(html.indexOf('<div class="brand-cluster">'), html.indexOf('<div class="top-meta">'));
  assert.match(group, /class="brand"[\s\S]*id="swarm-switcher"[\s\S]*class="edition"/);
  assert.match(group, /id="swarm-switcher"[^>]*><select id="swarm-selector" aria-label="Active Swarm"/);
  assert.doesNotMatch(group, /<span>Swarm<\/span>/);
});


test('only the selected folder is attached directly after Dashboard without closing hidden terminals', () => {
  class Element {
    constructor(){this.children=[];this.parentNode=null;this.dataset={};this.hidden=false;this.attributes={};this.classList={toggle(){}};}
    append(...items){for(const item of items){item.remove();item.parentNode=this;this.children.push(item);}}
    remove(){if(this.parentNode){const items=this.parentNode.children;items.splice(items.indexOf(this),1);this.parentNode=null;}}
    insertBefore(item,before){if(item===before)return;item.remove();const index=before?this.children.indexOf(before):this.children.length;assert.ok(index>=0);this.children.splice(index,0,item);item.parentNode=this;}
    get nextSibling(){if(!this.parentNode)return null;return this.parentNode.children[this.parentNode.children.indexOf(this)+1]||null;}
    setAttribute(name,value){this.attributes[name]=value;}
  }
  const tabs=new Element(),dashboard=new Element(),a=new Element(),b=new Element(),rootTerminal=new Element(),aTerminal=new Element(),bTerminal=new Element();
  tabs.append(dashboard,rootTerminal,a,aTerminal,b,bTerminal);
  const entries=new Map([['a',{wrapper:a,tab:new Element()}],['b',{wrapper:b,tab:new Element()}]]);
  const context=vm.createContext({selectedAgentGroup:'a',openedAgentGroups:new Set(['a','b']),agentGroupTabs:entries,
    activeTab:'agent-group:a',state:{agents:[{id:'root'},{id:'a-agent'},{id:'b-agent'}]},
    agentGroupProjection:()=>({groups:[{id:'a',name:'A'},{id:'b',name:'B'}]}),
    agentGroupTabId:id=>'agent-group:'+id,
    SwarmAgentGroups:{effectiveAssignments:()=>({'a-agent':'a','b-agent':'b'})},
    terminals:new Map([['root',{group:rootTerminal}],['a-agent',{group:aTerminal}],['b-agent',{group:bTerminal}]]),
    $:selector=>selector==='#tabs'?tabs:dashboard,
  });
  const code=source.slice(source.indexOf('function syncAgentGroupTabs()'),source.indexOf('function renderAgentFolders('));
  vm.runInContext(code,context);context.syncAgentGroupTabs();
  assert.equal(dashboard.nextSibling,a);assert.equal(b.parentNode,null);
  assert.equal(rootTerminal.hidden,true);assert.equal(aTerminal.hidden,false);assert.equal(bTerminal.hidden,true);
  context.selectedAgentGroup='b';context.activeTab='agent-group:b';context.syncAgentGroupTabs();
  assert.equal(dashboard.nextSibling,b);assert.equal(a.parentNode,null);assert.equal(aTerminal.hidden,true);assert.equal(bTerminal.hidden,false);
  context.selectedAgentGroup=null;context.activeTab='dashboard';context.syncAgentGroupTabs();
  assert.equal(a.parentNode,null);assert.equal(b.parentNode,null);assert.equal(rootTerminal.hidden,false);
  assert.equal(context.terminals.size,3);assert.equal(entries.size,2);
  context.selectedAgentGroup='a';context.syncAgentGroupTabs();
  assert.equal(dashboard.nextSibling,a);assert.equal(b.parentNode,null);assert.equal(aTerminal.parentNode,tabs);
});
