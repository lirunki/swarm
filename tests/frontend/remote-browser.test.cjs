const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {RemoteBrowserController, BrowserSettingsController, MAX_QUEUE} =
  require('../../src/swarm/static/remote-browser.js');
const tick = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor() {
    this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false;
    this.checked = false; this.events = new Map(); this.children = []; this.attributes = new Map();
    this._dataset = {};
  }
  get dataset() { return this._dataset; }
  addEventListener(name, callback) { this.events.set(name, callback); }
  dispatch(name, event = {}) { return this.events.get(name)?.({preventDefault() {}, stopPropagation() {}, ...event, target: this}); }
  appendChild(node) { this.children.push(node); this.firstChild = this.children[0]; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); this.firstChild = this.children[0]; }
  replaceChildren(...nodes) { this.children = nodes; this.firstChild = nodes[0]; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); if (name === 'src') this.src = ''; }
  getBoundingClientRect() { return {left: 10, top: 20, width: 200, height: 100}; }
  setPointerCapture() {}
  releasePointerCapture() {}
  focus() { this.focused = true; }
}

const IDS = ['panel', 'owner', 'status', 'launch', 'refresh-tabs', 'back', 'forward', 'reload',
  'copy', 'paste', 'url', 'new', 'interval', 'pause', 'tabs', 'screen', 'placeholder'];
const SETTINGS_IDS = ['settings-owner', 'enabled', 'headless', 'executable', 'settings-save', 'settings-status'];

function nodes() {
  const result = Object.fromEntries([...IDS, ...SETTINGS_IDS].map(id => [`#browser-${id}`, new Element()]));
  const ownerDocument = {createElement: () => { const node = new Element(); node.ownerDocument = ownerDocument; return node; }};
  for (const node of Object.values(result)) node.ownerDocument = ownerDocument;
  return result;
}

function fixture(options = {}) {
  const elements = nodes(), calls = [], toasts = [], changes = [];
  let source = options.source || 'local', generation = 1;
  let status = options.status || {enabled: true, running: true, state: 'running'};
  let tabs = options.tabs || [{id: 'tab-a', title: '<b>safe title</b>', url: 'https://a.test/'}];
  let selected = options.selected || tabs[0]?.id || null;
  const request = async (url, method = 'GET', body, binary = false) => {
    calls.push([url, method, body, binary]);
    if (options.request) return options.request(url, method, body, binary, {status, tabs, selected});
    if (url.endsWith('/launch')) { status = {enabled: true, running: true, state: 'running'}; return {ok: true}; }
    if (url.endsWith('/status')) return {...status};
    if (url.endsWith('/tabs')) return {tabs: tabs.map(tab => ({...tab})), selected};
    if (url.includes('/screenshot?')) return new Blob(['png'], {type: 'image/png'});
    if (url.endsWith('/select')) { selected = body.target_id; return {ok: true}; }
    if (url.endsWith('/new')) { const made = {id: 'tab-new', title: 'New', url: body.url}; tabs.push(made); selected = made.id; return {id: made.id}; }
    if (url.endsWith('/close')) { tabs = tabs.filter(tab => tab.id !== body.target_id); selected = tabs[0]?.id || null; return {ok: true}; }
    if (url.endsWith('/copy')) return {text: 'remote selection'};
    return {ok: true};
  };
  const controller = new RemoteBrowserController({query: selector => elements[selector] || null, request,
    currentSwarm: () => source, currentGeneration: () => generation,
    ownerName: value => value === 'local' ? 'Local' : `Peer ${value}`,
    toast: (...args) => toasts.push(args), confirm: async () => true,
    onChanged: async (...args) => changes.push(args)});
  return {controller, elements, calls, toasts, changes,
    setStatus(value) { status = value; }, setTabs(value, active) { tabs = value; selected = active; },
    switchSource(value) { source = value; generation += 1; controller.setContext(); }};
}

async function activate(t) {
  assert.equal(await t.controller.activate(), true);
  assert.deepEqual(t.calls.slice(0, 4).map(call => [call[0].replace(/&t=\d+$/, '&t=*'), call[1], call[3]]), [
    ['/api/browser/status', 'GET', false],
    ['/api/browser/tabs', 'GET', false],
    ['/api/browser/select', 'POST', false],
    ['/api/browser/screenshot?target_id=tab-a&t=*', 'GET', true],
  ]);
}

test('activate uses status, tabs, select, and binary screenshot HTTP only', async () => {
  const t = fixture();
  await activate(t);
  assert.equal(t.controller.selected, 'tab-a');
  assert.equal(t.controller.displayed, 'tab-a');
  assert.equal(t.elements['#browser-screen'].hidden, false);
  assert.equal(t.elements['#browser-tabs'].children[0].textContent, '<b>safe title</b>');
  assert.equal(t.elements['#browser-tabs'].children[0].children.length, 1, 'close control is a node, not page HTML');
  assert.equal(t.calls.some(call => /^wss?:|\/ws\//i.test(call[0])), false);
  t.controller.deactivate();
});

test('stopped activation never launches; launch is explicit and has request_id', async () => {
  const t = fixture({status: {enabled: true, running: false, state: 'stopped'}});
  await t.controller.activate();
  assert.deepEqual(t.calls.map(call => call.slice(0, 2)), [['/api/browser/status', 'GET']]);
  assert.equal(t.calls.some(call => call[0].endsWith('/launch')), false);
  assert.equal(await t.controller.launch(), true);
  const launch = t.calls.find(call => call[0].endsWith('/launch'));
  assert.equal(launch[1], 'POST'); assert.match(launch[2].request_id, /^[0-9a-f-]{36}$/i);
  assert.equal(t.calls.some(call => call[0].endsWith('/stop')), false);
  t.controller.deactivate();
});

test('owner endpoint and generation are captured and stale late UI results are ignored', async () => {
  let resolveStatus;
  const pending = new Promise(resolve => { resolveStatus = resolve; });
  const t = fixture({source: 'peer/a', request: async url => url.endsWith('/status') ? pending : {tabs: [], selected: null}});
  const activation = t.controller.activate();
  assert.equal(t.calls[0][0], '/api/federation/peer%2Fa/proxy/api/browser/status');
  t.switchSource('peer/b');
  resolveStatus({enabled: true, running: true, state: 'running'});
  await activation;
  assert.equal(t.controller.source, 'peer/b');
  assert.equal(t.controller.status, null, 'late peer/a status did not alter peer/b UI');
  assert.equal(t.calls.length, 1, 'late status did not trigger old-owner tabs or screenshot');
  t.controller.deactivate();
});

test('pointer gesture freezes displayed target and posts ratio click without frame IDs', async () => {
  const t = fixture(); await activate(t);
  const before = t.calls.length;
  t.elements['#browser-screen'].dispatch('pointerdown', {button: 0, pointerId: 7, clientX: 110, clientY: 45});
  assert.equal(await t.controller.shot(), false, 'periodic screenshot is deferred during a gesture');
  assert.equal(t.calls.length, before);
  t.elements['#browser-screen'].dispatch('pointerup', {button: 0, pointerId: 7, clientX: 110, clientY: 45});
  await tick(); await tick();
  const click = t.calls.find(call => call[0].endsWith('/click'));
  assert.deepEqual(click.slice(1, 3), ['POST', {target_id: 'tab-a', x_ratio: .5, y_ratio: .25}]);
  assert.equal('frame_id' in click[2], false);
  assert.equal(t.calls.filter(call => call[0].includes('/screenshot?')).length, 2, 'action refreshes screenshot after pointerup');
  t.controller.deactivate();
});

test('navigation and close snapshot target and refresh tabs through HTTP', async () => {
  const t = fixture({tabs: [
    {id: 'tab-a', title: 'A', url: 'https://a.test/'},
    {id: 'tab-b', title: 'B', url: 'https://b.test/'},
  ]});
  await activate(t); const before = t.calls.length;
  assert.equal(await t.controller.navigate('back'), true);
  assert.deepEqual(t.calls[before].slice(0, 3), ['/api/browser/back', 'POST', {target_id: 'tab-a'}]);
  assert.equal(await t.controller.closeTab('tab-a'), true);
  const close = t.calls.find(call => call[0].endsWith('/close'));
  assert.deepEqual(close.slice(1, 3), ['POST', {target_id: 'tab-a'}]);
  assert.equal(t.controller.selected, 'tab-b');
  assert.equal(t.calls.some(call => 'frame_id' in (call[2] || {})), false);
  t.controller.deactivate();
});

test('clipboard reads only on explicit paste, splits 4096 text, and copy writes remote text', async () => {
  const original = global.navigator; let reads = 0, written = '';
  Object.defineProperty(global, 'navigator', {configurable: true, value: {clipboard: {
    readText: async () => { reads += 1; return 'x'.repeat(4096); },
    writeText: async value => { written = value; },
  }}});
  try {
    const t = fixture(); await activate(t);
    assert.equal(reads, 0, 'activation and screenshots never read the OS clipboard');
    assert.equal(await t.controller.paste(), true);
    const chunks = t.calls.filter(call => call[0].endsWith('/text'));
    assert.equal(chunks.length, 2); assert.deepEqual(chunks.map(call => call[2].text.length), [2048, 2048]);
    assert.ok(chunks.every(call => call[2].target_id === 'tab-a'));
    assert.equal(await t.controller.copy(), true); assert.equal(written, 'remote selection');
    t.controller.deactivate();
  } finally { Object.defineProperty(global, 'navigator', {configurable: true, value: original}); }
});

test('input queue is bounded and changing selection drops queued old-page input', async () => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  const t = fixture({tabs: [
    {id: 'tab-a', title: 'A', url: 'https://a.test/'},
    {id: 'tab-b', title: 'B', url: 'https://b.test/'},
  ], request: async (url, method, body, binary) => {
    if (url.endsWith('/status')) return {enabled: true, running: true};
    if (url.endsWith('/tabs')) return {tabs: [
      {id: 'tab-a', title: 'A', url: 'https://a.test/'}, {id: 'tab-b', title: 'B', url: 'https://b.test/'},
    ], selected: 'tab-a'};
    if (url.includes('/screenshot?')) return new Blob(['png'], {type: 'image/png'});
    if (url.endsWith('/text')) return blocked;
    return {ok: true};
  }});
  await activate(t);
  void t.controller.sendText('tab-a', 'first');
  for (let index = 0; index < MAX_QUEUE + 10; index += 1) void t.controller.sendText('tab-a', `q${index}`);
  assert.ok(t.controller.queue.length + 1 <= MAX_QUEUE);
  assert.match(t.toasts.at(-1)[0], /queue is full/i);
  void t.controller.select('tab-b');
  assert.equal(t.controller.queue.length, 0, 'queued input is not replayed after selection changes');
  release({ok: true}); await tick();
  t.controller.deactivate();
});

test('deferred selects do not dispatch a queued selection for a stale owner', async () => {
  let blockSelect = false, release;
  const blocked = new Promise(resolve => { release = resolve; });
  const tabs = [
    {id: 'tab-a', title: 'A', url: 'https://a.test/'},
    {id: 'tab-b', title: 'B', url: 'https://b.test/'},
  ];
  const t = fixture({tabs, request: async (url, method, body) => {
    if (url.endsWith('/status')) return {enabled: true, running: true};
    if (url.endsWith('/tabs')) return {tabs, selected: 'tab-a'};
    if (url.includes('/screenshot?')) return new Blob(['png'], {type: 'image/png'});
    if (url.endsWith('/select') && blockSelect) return blocked;
    return {ok: true};
  }});
  await activate(t); t.calls.length = 0; blockSelect = true;
  const first = t.controller.select('tab-a'); await tick();
  const second = t.controller.select('tab-b');
  assert.deepEqual(t.calls.filter(call => call[0].endsWith('/select')).map(call => call[2].target_id), ['tab-a']);
  t.switchSource('peer/b'); release({ok: true});
  assert.equal(await first, false); assert.equal(await second, false); await tick();
  assert.deepEqual(t.calls.filter(call => call[0].endsWith('/select')).map(call => call[2].target_id), ['tab-a']);
  t.controller.deactivate();
});

test('a context switch or deactivation prevents a second paste chunk', async () => {
  for (const action of ['context switch', 'deactivate']) {
    let release;
    const blocked = new Promise(resolve => { release = resolve; });
    const t = fixture({request: async (url) => {
      if (url.endsWith('/status')) return {enabled: true, running: true};
      if (url.endsWith('/tabs')) return {tabs: [{id: 'tab-a', title: 'A', url: 'https://a.test/'}], selected: 'tab-a'};
      if (url.includes('/screenshot?')) return new Blob(['png'], {type: 'image/png'});
      if (url.endsWith('/text')) return blocked;
      return {ok: true};
    }});
    await activate(t); t.calls.length = 0;
    const sending = t.controller.sendText('tab-a', 'x'.repeat(4096)); await tick();
    assert.equal(t.calls.filter(call => call[0].endsWith('/text')).length, 1, `${action}: first chunk dispatched`);
    if (action === 'context switch') t.switchSource('peer/b'); else t.controller.deactivate();
    release({ok: true}); await sending; await tick();
    assert.equal(t.calls.filter(call => call[0].endsWith('/text')).length, 1, `${action}: second chunk suppressed`);
    t.controller.deactivate();
  }
});

test('an uncertain timeout prevents a second paste chunk', async () => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  const t = fixture({request: async url => {
    if (url.endsWith('/status')) return {enabled: true, running: true};
    if (url.endsWith('/tabs')) return {tabs: [{id: 'tab-a', title: 'A', url: 'https://a.test/'}], selected: 'tab-a'};
    if (url.includes('/screenshot?')) return new Blob(['png'], {type: 'image/png'});
    if (url.endsWith('/text')) return blocked;
    return {ok: true};
  }});
  await activate(t); t.calls.length = 0;
  const originalSetTimeout = global.setTimeout, originalClearTimeout = global.clearTimeout;
  const timeoutToken = {}, scheduled = [];
  global.setTimeout = (callback, delay, ...args) => {
    if (delay === 20000) { scheduled.push(callback); return timeoutToken; }
    return originalSetTimeout(callback, delay, ...args);
  };
  global.clearTimeout = token => token === timeoutToken ? undefined : originalClearTimeout(token);
  try {
    const sending = t.controller.sendText('tab-a', 'x'.repeat(4096)); await tick();
    assert.equal(t.calls.filter(call => call[0].endsWith('/text')).length, 1);
    scheduled[0](); assert.equal(await sending, false);
    release({ok: true}); await tick(); await tick();
    assert.equal(t.calls.filter(call => call[0].endsWith('/text')).length, 1);
  } finally {
    global.setTimeout = originalSetTimeout; global.clearTimeout = originalClearTimeout;
    t.controller.deactivate();
  }
});

function discoverySettingsFixture(options = {}) {
  const elements = nodes(), calls = [], changed = [];
  for (const id of ['executable-select', 'discovery-status']) {
    const node = new Element(); node.ownerDocument = elements['#browser-executable'].ownerDocument;
    elements[`#browser-${id}`] = node;
  }
  let source = options.source || 'local', generation = 1;
  const settings = options.settings || {revision: 3, enabled: false, headless: true, executable: null};
  const discovery = options.discovery || {browsers: [], unavailable: []};
  const request = async (url, method = 'GET', body) => {
    calls.push([url, method, body]);
    if (options.request) return options.request(url, method, body, {source, generation});
    if (url.endsWith('/discovery')) return discovery;
    if (method === 'POST') return {...body, revision: body.revision + 1};
    return settings;
  };
  const controller = new BrowserSettingsController({query: selector => elements[selector] || null, request,
    currentSwarm: () => source, currentGeneration: () => generation,
    ownerName: value => value === 'local' ? 'Local' : `Peer ${value}`,
    onChanged: async (...args) => changed.push(args)});
  return {controller, elements, calls, changed, switchSource(value) {
    source = value; generation += 1; controller.setContext();
  }};
}

function settingsFixture(options = {}) {
  const elements = nodes(), calls = [], changed = []; let source = options.source || 'local', generation = 1;
  let saved = {revision: 3, enabled: false, headless: true, executable: null};
  const request = async (...args) => {
    calls.push(args);
    if (args[1] === 'POST') saved = {...args[2], revision: args[2].revision + 1};
    return {...saved};
  };
  const controller = new BrowserSettingsController({query: selector => elements[selector] || null, request,
    currentSwarm: () => source, currentGeneration: () => generation,
    ownerName: value => value === 'local' ? 'Local' : `Peer ${value}`,
    onChanged: async (...args) => changed.push(args)});
  return {controller, elements, calls, changed};
}

test('settings are lazy, request-based, projected, and blocked while running', async () => {
  const t = settingsFixture({source: 'peer/a'});
  assert.equal(t.calls.length, 0); assert.equal(t.elements['#browser-headless'].checked, true);
  await t.controller.load();
  assert.deepEqual(t.calls[0], ['/api/federation/peer%2Fa/proxy/api/browser/settings', 'GET']);
  t.elements['#browser-enabled'].checked = true; t.elements['#browser-enabled'].dispatch('input');
  t.elements['#browser-executable'].value = ' /opt/edge '; t.elements['#browser-executable'].dispatch('input');
  assert.equal(await t.controller.saveSettings(), true);
  assert.deepEqual(t.calls[1][2], {revision: 3, request_id: t.calls[1][2].request_id,
    enabled: true, headless: true, executable: '/opt/edge'});
  assert.match(t.calls[1][2].request_id, /^[0-9a-f-]{36}$/i);
  assert.equal(t.calls.some(call => call[0].endsWith('/launch')), false);
  t.controller.setContext({running: true});
  t.elements['#browser-headless'].dispatch('input');
  assert.equal(await t.controller.saveSettings(), false);
  assert.match(t.elements['#browser-settings-status'].textContent, /cannot be changed while/i);
});

test('browser discovery renders lexical aliases and unsafe labels as text, then requires an explicit selection', async () => {
  const configured = '/snap/bin/chromium';
  const t = discoverySettingsFixture({settings: {revision: 7, enabled: false, headless: true, executable: configured},
    discovery: {browsers: [
      {name: '<img src=x onerror=alert(1)>', executable: configured, supported: true},
      {name: 'Chromium alias', executable: '/usr/local/bin/chromium-link', supported: true},
    ], unavailable: [{name: 'Windows Microsoft Edge', executable: 'C:\\Program Files\\Edge\\msedge.exe',
      reason: 'Use native Windows Swarm or install Edge/Chromium inside WSL.'}]}});
  assert.equal(await t.controller.load(), t.controller.settings);
  assert.deepEqual(t.calls.map(call => call.slice(0, 2)), [
    ['/api/browser/settings', 'GET'], ['/api/browser/discovery', 'GET'],
  ]);
  const select = t.elements['#browser-executable-select'];
  assert.deepEqual(select.children.map(option => option.value), [
    '', configured, '/usr/local/bin/chromium-link', '__unavailable_0__', '__custom__',
  ]);
  assert.equal(select.children[1].textContent, `<img src=x onerror=alert(1)> — ${configured}`);
  assert.equal(select.children[1].children.length, 0, 'discovery labels are assigned only as text');
  assert.equal(select.children[3].disabled, true);
  assert.equal(select.children[3].textContent,
    'Windows Microsoft Edge — Use native Windows Swarm or install Edge/Chromium inside WSL.');
  assert.equal(select.value, configured, 'the configured lexical path snaps to its matching alias');
  assert.match(t.elements['#browser-discovery-status'].textContent, /WSL/i);

  select.value = ''; select.dispatch('change');
  assert.equal(t.elements['#browser-executable'].value, '');
  assert.equal(t.controller.dirty.has('executable'), true);
  assert.equal(t.calls.length, 2, 'changing the selector does not save automatically');
  select.value = '/usr/local/bin/chromium-link'; select.dispatch('change');
  assert.equal(t.elements['#browser-executable'].value, '/usr/local/bin/chromium-link');
  assert.equal(t.calls.length, 2);
  select.value = '__custom__'; select.dispatch('change');
  assert.equal(t.elements['#browser-executable'].focused, true);
  t.elements['#browser-executable'].value = 'C:\\safe\\custom-browser.exe';
  t.elements['#browser-executable'].dispatch('input');
  assert.equal(select.value, '__custom__');
});

test('settings null defaults to Auto and discovery failures preserve manual editing and saving', async () => {
  const missing = Error('not found'); missing.status = 404;
  const t = discoverySettingsFixture({request: async (url, method, body) => {
    if (url.endsWith('/discovery')) throw missing;
    if (method === 'POST') return {...body, revision: body.revision + 1};
    return {revision: 2, enabled: false, headless: true, executable: null};
  }});
  const loaded = await t.controller.load();
  assert.equal(loaded.executable, null);
  assert.equal(t.controller.settings.revision, 2, 'a discovery error does not discard valid settings');
  assert.equal(t.elements['#browser-executable-select'].value, '');
  assert.match(t.elements['#browser-discovery-status'].textContent, /unavailable.*custom executable/i);
  t.elements['#browser-executable'].value = 'C:\\Browsers\\edge.exe';
  t.elements['#browser-executable'].dispatch('input');
  assert.equal(await t.controller.saveSettings(), true);
  const post = t.calls.find(call => call[1] === 'POST');
  assert.equal(post[2].executable, 'C:\\Browsers\\edge.exe');
});

test('malformed discovery is bounded and does not replace settings or disable custom save', async () => {
  const t = discoverySettingsFixture({discovery: {browsers: [
    {name: `bad\u0000name`, executable: '/bin/browser', supported: true},
  ], unavailable: []}});
  await t.controller.load();
  assert.equal(t.controller.settings.revision, 3);
  assert.deepEqual(t.controller.discovery, {browsers: [], unavailable: []});
  assert.match(t.elements['#browser-discovery-status'].textContent, /malformed.*custom executable/i);
  t.elements['#browser-executable'].value = '/opt/manual-browser';
  t.elements['#browser-executable'].dispatch('input');
  assert.equal(t.elements['#browser-settings-save'].disabled, false);
});

test('owner changes discard pending discovery and never paint stale browser options', async () => {
  let resolveA;
  const pendingA = new Promise(resolve => { resolveA = resolve; });
  const t = discoverySettingsFixture({source: 'peer/a', request: async url => {
    if (url.includes('peer%2Fa') && url.endsWith('/settings'))
      return {revision: 1, enabled: false, headless: true, executable: null};
    if (url.includes('peer%2Fa') && url.endsWith('/discovery')) return pendingA;
    if (url.endsWith('/settings')) return {revision: 4, enabled: true, headless: false, executable: '/b/browser'};
    return {browsers: [{name: 'B browser', executable: '/b/browser', supported: true}], unavailable: []};
  }});
  const loadA = t.controller.load(); await tick();
  assert.equal(t.calls.at(-1)[0], '/api/federation/peer%2Fa/proxy/api/browser/discovery');
  t.switchSource('peer/b');
  assert.deepEqual(t.elements['#browser-executable-select'].children.map(option => option.value), ['', '__custom__']);
  await t.controller.load();
  assert.deepEqual(t.elements['#browser-executable-select'].children.map(option => option.value), ['', '/b/browser', '__custom__']);
  resolveA({browsers: [{name: 'A browser', executable: '/a/browser', supported: true}], unavailable: []});
  await loadA;
  assert.equal(t.controller.source, 'peer/b');
  assert.deepEqual(t.elements['#browser-executable-select'].children.map(option => option.value), ['', '/b/browser', '__custom__']);
});

test('source contains EdgeMCP attribution, no websocket/stream protocol, and no data HTML sink', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/remote-browser.js'), 'utf8');
  assert.match(source, /c09dc257886305539ea90bc454ceda63b9b150a6/);
  assert.doesNotMatch(source, /WebSocket|createSocket|frame_id|MAX_FRAME|stream extension/i);
  assert.doesNotMatch(source, /\.innerHTML\s*=|document\.write|<iframe|evaluate\s*\(/i);
  assert.match(source, /createObjectURL\?\.\(blob\)/);
});


test('settings owner switch clears old pending and running locks without stale completion effects', async () => {
  const elements=nodes();let source='local',generation=1,resolveOld;
  const base={revision:0,enabled:false,headless:true,executable:null};
  const controller=new BrowserSettingsController({query:selector=>elements[selector],currentSwarm:()=>source,currentGeneration:()=>generation,
    request:async(_path,method)=>method==='POST'?new Promise(resolve=>{resolveOld=resolve;}):{...base}});
  await controller.load();elements['#browser-enabled'].checked=true;elements['#browser-enabled'].dispatch('input');
  const saving=controller.saveSettings();await tick();assert.equal(controller.saving,true);
  controller.running=true;source='remote';generation++;controller.setContext();await controller.load();
  assert.equal(controller.saving,false);assert.equal(controller.loading,false);assert.equal(controller.running,false);
  assert.equal(elements['#browser-enabled'].disabled,false);
  resolveOld({...base,revision:1,enabled:true});await saving;
  assert.equal(controller.source,'remote');assert.equal(controller.settings.enabled,false);assert.equal(controller.saving,false);
});
