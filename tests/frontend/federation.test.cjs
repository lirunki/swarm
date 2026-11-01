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
  assert.match(source, /remember\(\);\s*for \(const id of \[\.\.\.terminals\.keys\(\)\]\) closeTerminal\(id, false\)/);
  assert.match(source, /activate\('dashboard', false\); render\(\)/);
});

test('switching Swarms preserves each browser-local tab list independently', () => {
  const values = new Map([
    ['swarm.tabs.remote', JSON.stringify({ids: ['remote-a'], active: 'remote-a'})],
  ]);
  const calls = [];
  const context = vm.createContext({
    activeSwarm: 'local', activeTab: 'local-b', terminals: new Map([['local-a', {}], ['local-b', {}]]),
    localStorage: {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)},
    notificationsInbox: {clear: () => calls.push(['clear'])}, document: {querySelectorAll: () => []}, stateSocket: null,
    stateGeneration: 0, reconnectDelay: 1000, restored: true, groupedBranches: {clear: () => {}},
    state: {}, dashboardControls: {status: {reset: () => {}}},
    $: selector => selector === '#swarm-selector'
      ? {value: '', title: '', selectedOptions: [{textContent: 'Remote'}]}
      : {value: ''},
    closeTerminal: (id, persist) => { calls.push(['close', id, persist]); context.terminals.delete(id); },
    activate: (id, persist) => calls.push(['activate', id, persist]), render: () => {}, updateRestartCapability: () => {},
    connectState: () => {}, updateNotificationSources: () => {},
  });
  const memory = source.slice(source.indexOf('const tabStorageKey'), source.indexOf('function applyState'));
  const switching = source.slice(source.indexOf('function switchSwarm('), source.indexOf('function revealNotificationAgent'));
  vm.runInContext(`${memory}\n${switching}`, context);
  context.switchSwarm('remote');

  assert.deepEqual(JSON.parse(values.get('swarm.tabs.local')), {ids: ['local-a', 'local-b'], active: 'local-b'});
  assert.deepEqual(JSON.parse(values.get('swarm.tabs.remote')), {ids: ['remote-a'], active: 'remote-a'});
  assert.deepEqual(calls.filter(call => call[0] === 'close'), [['close', 'local-a', false], ['close', 'local-b', false]]);
  assert.ok(calls.some(call => call[0] === 'activate' && call[1] === 'dashboard' && call[2] === false));
});

test('first state from a selected Swarm restores only available remembered tabs and active tab', () => {
  const values = new Map([['swarm.tabs.remote', JSON.stringify({ids: ['available', 'removed'], active: 'available'})]]);
  const opened = [], activated = [];
  const context = vm.createContext({
    activeSwarm: 'remote', activeTab: 'dashboard', terminals: new Map(), restored: false, state: {agents: []},
    localStorage: {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)},
    fleetControl: null, agentById: id => context.state.agents.find(agent => agent.id === id), closeTerminal: () => {}, render: () => {},
    openTerminal: (id, focus) => { opened.push([id, focus]); context.terminals.set(id, {}); },
    activate: id => activated.push(id), consumeNotificationReveal: () => {}, notificationsInbox: null,
  });
  const restore = source.slice(source.indexOf('const tabStorageKey'), source.indexOf('async function refresh'));
  vm.runInContext(restore, context);
  context.applyState({agents: [{id: 'available'}]});

  assert.deepEqual(opened, [['available', false]]);
  assert.deepEqual(activated, ['available']);
  assert.equal(context.restored, true);

  values.set('swarm.tabs.remote', '{invalid json');
  context.terminals.clear(); context.restored = false; opened.length = 0; activated.length = 0;
  assert.doesNotThrow(() => context.applyState({agents: [{id: 'available'}]}));
  assert.deepEqual(opened, []);
  assert.deepEqual(activated, []);
  assert.equal(context.restored, true);
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
