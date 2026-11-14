const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {RemoteBrowserController, BrowserSettingsController, parseCDPAddress, MAX_QUEUE} =
  require('../../src/swarm/static/remote-browser.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const C = '33333333-3333-4333-8333-333333333333';

class Element {
  constructor() {
    this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false;
    this.checked = false; this.events = new Map(); this.children = []; this.attributes = new Map();
    this.classList = {toggle() {}}; this.dataset = {};
  }
  addEventListener(name, callback) { this.events.set(name, callback); }
  dispatch(name, event = {}) { return this.events.get(name)?.({preventDefault() {}, stopPropagation() {}, ...event, target: this, currentTarget: this}); }
  appendChild(node) { this.children.push(node); this.firstChild = this.children[0]; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); this.firstChild = this.children[0]; }
  replaceChildren(...nodes) { this.children = nodes; this.firstChild = nodes[0]; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); if (name === 'src') this.src = ''; }
  getBoundingClientRect() { return {left: 10, top: 20, width: 200, height: 100}; }
  setPointerCapture() {} releasePointerCapture() {} focus() { this.focused = true; }
}

const VIEW_IDS = ['panel', 'owner', 'status', 'connect', 'instance-select', 'refresh-tabs', 'back', 'forward',
  'reload', 'copy', 'paste', 'url', 'new', 'interval', 'pause', 'tabs', 'screen', 'placeholder'];
const SETTINGS_IDS = ['settings-owner', 'settings-status', 'headless', 'executable-select', 'executable-manual',
  'executable-custom', 'discovery-status', 'instance-name', 'management-list', 'settings-launch',
  'existing-name', 'existing-port', 'existing-add', 'management-refresh'];
function nodes() {
  const result = Object.fromEntries([...VIEW_IDS, ...SETTINGS_IDS].map(id => [`#browser-${id}`, new Element()]));
  const doc = {createElement: () => { const node = new Element(); node.ownerDocument = doc; return node; }};
  Object.values(result).forEach(node => { node.ownerDocument = doc; });
  return result;
}
function managed(id = A, name = 'Main', extra = {}) {
  return {id, name, kind: 'managed', port: null, running: true, can_stop: true,
    state: 'running', executable: '/opt/edge', headless: true, ...extra};
}
function external(id = B, name = 'Existing', extra = {}) {
  return {id, name, kind: 'external', port: 9222, running: true, can_stop: false,
    state: 'running', executable: null, headless: true, ...extra};
}
function registry(revision = 3, browsers = [managed(), external()]) { return {revision, browsers}; }

function viewerFixture(options = {}) {
  const elements = nodes(), calls = [], toasts = [];
  let source = options.source || 'local', generation = 1, list = options.registry || registry();
  let tabs = [{id: 'page-a', title: '<b>plain</b>', url: 'https://a.test/'}];
  const request = async (url, method = 'GET', body, binary = false) => {
    calls.push([url, method, body, binary]);
    if (options.request) return options.request(url, method, body, binary);
    if (/\/instances$/.test(url)) return structuredClone(list);
    if (/\/tabs$/.test(url)) return {tabs, selected: tabs[0]?.id || null};
    if (/\/screenshot\?/.test(url)) return new Blob(['png'], {type: 'image/png'});
    return {ok: true};
  };
  const controller = new RemoteBrowserController({query: selector => elements[selector] || null, request,
    currentSwarm: () => source, currentGeneration: () => generation,
    ownerName: value => value === 'local' ? 'Local' : `Peer ${value}`, toast: (...args) => toasts.push(args)});
  return {controller, elements, calls, toasts, setRegistry(value) { list = value; }, switchSource(value) {
    source = value; generation += 1; controller.setContext();
  }};
}

async function activateAndConnect(t) {
  assert.equal(await t.controller.activate(), true);
  assert.equal(t.controller.connected, false);
  assert.equal(await t.controller.connect(), true);
}

test('activation lists multiple instances, restores a selection, and never auto-connects', async () => {
  const t = viewerFixture();
  assert.equal(await t.controller.activate(), true);
  assert.deepEqual(t.calls.map(call => call.slice(0, 2)), [['/api/browser/instances', 'GET']]);
  assert.equal(t.controller.browserId, A);
  assert.equal(t.controller.connected, false);
  assert.equal(t.elements['#browser-connect'].textContent, 'Connect');
  assert.deepEqual(t.elements['#browser-instance-select'].children.map(node => node.value), [A, B]);
  assert.match(t.elements['#browser-instance-select'].children[1].textContent, /external 127\.0\.0\.1:9222/);
  assert.equal(t.calls.some(call => call[0].endsWith('/tabs')), false);
  t.controller.deactivate();
});

test('connect revalidates ownership/running and uses instance-scoped viewer HTTP', async () => {
  const t = viewerFixture({source: 'peer/a'}); await activateAndConnect(t);
  assert.deepEqual(t.calls.slice(0, 5).map(call => [call[0].replace(/&t=\d+$/, '&t=*'), call[1], call[3]]), [
    ['/api/federation/peer%2Fa/proxy/api/browser/instances', 'GET', false],
    ['/api/federation/peer%2Fa/proxy/api/browser/instances', 'GET', false],
    [`/api/federation/peer%2Fa/proxy/api/browser/instances/${A}/tabs`, 'GET', false],
    [`/api/federation/peer%2Fa/proxy/api/browser/instances/${A}/select`, 'POST', false],
    [`/api/federation/peer%2Fa/proxy/api/browser/instances/${A}/screenshot?target_id=page-a&t=*`, 'GET', true],
  ]);
  assert.equal(t.elements['#browser-tabs'].children[0].textContent, '<b>plain</b>');
  assert.equal(t.elements['#browser-tabs'].children[0].children.length, 1, 'page title stays text');
  assert.equal(t.elements['#browser-connect'].textContent, 'Disconnect');
  t.controller.deactivate();
});

test('selector change disconnects before changing ID and never auto-connects', async () => {
  const t = viewerFixture(); await activateAndConnect(t);
  assert.notEqual(t.controller.timer, null); t.calls.length = 0;
  t.elements['#browser-instance-select'].value = B;
  t.elements['#browser-instance-select'].dispatch('change');
  assert.equal(t.controller.browserId, B); assert.equal(t.controller.connected, false);
  assert.equal(t.controller.timer, null); assert.equal(t.controller.tabs.length, 0);
  assert.equal(t.elements['#browser-screen'].src || '', '');
  assert.equal(t.calls.length, 0, 'changing instances performs no implicit connect');
  t.controller.deactivate();
});

test('registry removal disconnects old instance before selecting a running replacement', async () => {
  const t = viewerFixture(); await activateAndConnect(t); t.calls.length = 0;
  t.setRegistry(registry(4, [external()]));
  assert.equal(t.controller.updateInstances(registry(4, [external()])), true);
  assert.equal(t.controller.browserId, B); assert.equal(t.controller.connected, false);
  assert.equal(t.controller.tabs.length, 0); assert.equal(t.controller.queue.length, 0);
  assert.equal(t.controller.displayed, null); assert.equal(t.elements['#browser-screen'].src || '', '');
  t.elements['#browser-screen'].dispatch('pointerdown', {button: 0, pointerId: 7, clientX: 20, clientY: 30});
  t.elements['#browser-screen'].dispatch('pointerup', {button: 0, pointerId: 7, clientX: 20, clientY: 30});
  await tick();
  assert.equal(t.calls.some(call => call[0].includes(`/instances/${A}/click`)), false);
  assert.equal(t.calls.length, 0, 'registry retarget performs no implicit connect or old-instance click');
  assert.equal(await t.controller.connect(), true);
  assert.equal(t.controller.browserId, B); assert.equal(t.controller.connected, true);
  assert.equal(t.calls.some(call => call[0].includes(`/instances/${B}/tabs`)), true);
  t.controller.deactivate();
});

test('stopped or identity-changed selected instance disconnects without changing its ID', async () => {
  const t = viewerFixture(); await activateAndConnect(t);
  t.controller.updateInstances(registry(4, [managed(A, 'Main', {running: false, can_stop: false, state: 'stopped'}), external()]));
  assert.equal(t.controller.browserId, A); assert.equal(t.controller.connected, false);
  t.setRegistry(registry(5)); assert.equal(await t.controller.connect(), true);
  t.controller.updateInstances(registry(6, [external(A, 'Reused'), external()]));
  assert.equal(t.controller.browserId, A); assert.equal(t.controller.connected, false);
  t.controller.deactivate();
});

test('changed host on the selected ID disconnects without retargeting', async () => {
  const first = external(B, 'Remote', {host: '10.0.0.8'});
  const t = viewerFixture({registry: registry(3, [first])}); await activateAndConnect(t); t.calls.length = 0;
  assert.equal(t.controller.updateInstances(registry(4, [external(B, 'Remote', {host: '10.0.0.9'})])), true);
  assert.equal(t.controller.browserId, B); assert.equal(t.controller.connected, false);
  assert.equal(t.controller.tabs.length, 0); assert.equal(t.controller.displayed, null);
  assert.equal(t.calls.length, 0, 'host identity changes never connect or retarget implicitly');
  assert.match(t.elements['#browser-instance-select'].children[0].textContent, /10\.0\.0\.9:9222/);
  t.controller.deactivate();
});

test('lower-revision viewer refresh cannot overwrite a newer registry projection', async () => {
  let release; const pending = new Promise(resolve => { release = resolve; });
  const t = viewerFixture({request: url => url.endsWith('/instances') ? pending : {ok: true}});
  const loading = t.controller.activate(); await tick();
  t.controller.updateInstances(registry(9, [external()]));
  release(registry(3)); await loading;
  assert.equal(t.controller.registry.revision, 9); assert.equal(t.controller.browserId, B);
  t.controller.deactivate();
});

test('deactivate disconnects but keeps selection; reactivation still does not connect', async () => {
  const t = viewerFixture(); await activateAndConnect(t); const id = t.controller.browserId;
  t.controller.deactivate(); assert.equal(t.controller.connected, false); assert.equal(t.controller.browserId, id);
  t.calls.length = 0; await t.controller.activate();
  assert.equal(t.controller.browserId, id); assert.equal(t.controller.connected, false);
  assert.deepEqual(t.calls.map(call => call[0]), ['/api/browser/instances']);
  t.controller.deactivate();
});

test('owner switch clears viewer state and late old-owner list cannot paint', async () => {
  let release; const pending = new Promise(resolve => { release = resolve; });
  const t = viewerFixture({source: 'peer/a', request: url => url.includes('peer%2Fa') ? pending : registry(8, [external()])});
  const loading = t.controller.activate(); await tick(); t.switchSource('peer/b');
  release(registry()); await loading;
  assert.equal(t.controller.source, 'peer/b'); assert.equal(t.controller.registry, null);
  assert.equal(t.controller.browserId, null); assert.equal(t.controller.connected, false);
  assert.equal(t.elements['#browser-instance-select'].children.length, 0);
  t.controller.deactivate();
});

test('queued input is bound to owner, instance, lifecycle, and page target', async () => {
  let release; const blocked = new Promise(resolve => { release = resolve; });
  const t = viewerFixture({request: async (url) => {
    if (url.endsWith('/instances')) return registry();
    if (url.endsWith('/tabs')) return {tabs: [{id: 'page-a', title: 'A', url: 'https://a'}], selected: 'page-a'};
    if (url.includes('/screenshot?')) return new Blob(['png'], {type: 'image/png'});
    if (url.endsWith('/text')) return blocked;
    return {ok: true};
  }});
  await activateAndConnect(t); t.calls.length = 0;
  const send = t.controller.sendText('page-a', 'x'.repeat(4096)); await tick();
  t.elements['#browser-instance-select'].value = B; t.elements['#browser-instance-select'].dispatch('change');
  release({ok: true}); assert.equal(await send, false); await tick();
  const textCalls = t.calls.filter(call => call[0].endsWith('/text'));
  assert.equal(textCalls.length, 1, 'second chunk is dropped');
  assert.match(textCalls[0][0], new RegExp(`/instances/${A}/text$`));
  assert.equal(t.calls.some(call => call[0].includes(`/instances/${B}/text`)), false);
  t.controller.deactivate();
});

test('refresh means registry while disconnected and tabs while connected', async () => {
  const t = viewerFixture(); await t.controller.activate(); t.calls.length = 0;
  await t.controller.refreshConnection(); assert.deepEqual(t.calls.map(call => call[0]), ['/api/browser/instances']);
  await t.controller.connect(); t.calls.length = 0; await t.controller.refreshConnection();
  assert.equal(t.calls[0][0], `/api/browser/instances/${A}/tabs`);
  t.controller.deactivate();
});

function settingsFixture(options = {}) {
  const elements = nodes(), calls = [], changed = [], toasts = [];
  let source = options.source || 'local', generation = 1, list = options.registry || registry();
  const discovery = options.discovery || {browsers: [], unavailable: []};
  const request = async (url, method = 'GET', body) => {
    calls.push([url, method, body]);
    if (options.request) return options.request(url, method, body);
    if (url.endsWith('/discovery')) return discovery;
    if (method === 'GET') return structuredClone(list);
    list = {...list, revision: list.revision + 1};
    return {...structuredClone(list), operation: {browser_id: body.id || A}};
  };
  const controller = new BrowserSettingsController({query: selector => elements[selector] || null, request,
    currentSwarm: () => source, currentGeneration: () => generation,
    ownerName: value => value === 'local' ? 'Local' : `Peer ${value}`,
    confirm: options.confirm || (() => true), prompt: options.prompt || ((_message, value) => `${value} renamed`),
    toast: (...args) => toasts.push(args), onChanged: (...args) => changed.push(args)});
  return {controller, elements, calls, changed, toasts, setRegistry(value) { list = value; }, switchSource(value) {
    source = value; generation += 1; controller.setContext();
  }};
}

test('settings load registry then discovery and renders aliases/XSS as plain text', async () => {
  const lexical = '/snap/bin/chromium-link';
  const t = settingsFixture({source: 'peer/a', discovery: {browsers: [
    {name: '<img src=x onerror=alert(1)>', executable: lexical, supported: true},
  ], unavailable: [{name: 'Windows Edge', executable: 'C:\\Edge\\msedge.exe', reason: 'Use native Edge'}]}});
  await t.controller.load();
  assert.deepEqual(t.calls.map(call => call.slice(0, 2)), [
    ['/api/federation/peer%2Fa/proxy/api/browser/instances', 'GET'],
    ['/api/federation/peer%2Fa/proxy/api/browser/discovery', 'GET'],
  ]);
  const options = t.elements['#browser-executable-select'].children;
  assert.equal(options[1].value, lexical); assert.equal(options[1].textContent, `<img src=x onerror=alert(1)> — ${lexical}`);
  assert.equal(options[1].children.length, 0); assert.equal(options[2].disabled, true);
  const rows = t.elements['#browser-management-list'].children;
  assert.equal(rows.length, 2); assert.match(rows[1].children[0].textContent, /Existing — external port 127\.0\.0\.1:9222/);
});

test('CDP address parser preserves bare ports and canonicalizes explicit IP hosts', () => {
  assert.deepEqual(parseCDPAddress('9222'), {port: 9222});
  assert.deepEqual(parseCDPAddress('127.0.0.1:9333'), {port: 9333, host: '127.0.0.1'});
  assert.deepEqual(parseCDPAddress('localhost:9333'), {port: 9333, host: '127.0.0.1'});
  assert.deepEqual(parseCDPAddress('[2001:0db8:0:0::1]:9444'), {port: 9444, host: '2001:db8::1'});
  for (const value of ['http://127.0.0.1:9222', 'example.test:9222', 'user@127.0.0.1:9222',
    '127.0.0.1:9222/path', '127.0.0.1:9222?x', '127.0.0.1 :9222', '1.2.3.4:9.5',
    '0', '65536', '09222', '1.2.3.04:9222', '1.2.3.256:9222', '0.0.0.0:9222',
    '224.0.0.1:9222', '[::]:9222', '[ff02::1]:9222', '[fe80::1%25eth0]:9222'])
    assert.equal(parseCDPAddress(value), null, value);
});

test('registry projection brackets canonical IPv6 and rejects URL/XSS or noncanonical hosts', () => {
  const t = viewerFixture();
  assert.equal(t.controller.updateInstances(registry(3, [external(B, 'IPv6', {host: '2001:db8::1'})])), true);
  assert.match(t.elements['#browser-instance-select'].children[0].textContent, /external \[2001:db8::1\]:9222/);
  for (const host of ['https://10.0.0.8', '<img src=x onerror=alert(1)>', '010.0.0.8', 'localhost', '2001:0db8::1']) {
    assert.equal(t.controller.updateInstances(registry(4, [external(B, 'Unsafe', {host})])), false, host);
    assert.equal(t.controller.registry.revision, 3);
  }
  assert.equal(t.elements['#browser-instance-select'].children.some(node => node.textContent.includes('<img')), false);
  t.controller.deactivate();
});

test('launch and add snapshot strict fields and post through owner path', async () => {
  const t = settingsFixture({source: 'peer/a'}); await t.controller.load(); t.calls.length = 0;
  t.elements['#browser-instance-name'].value = ' New managed ';
  t.elements['#browser-headless'].checked = false;
  t.elements['#browser-executable-manual'].value = ' /opt/ms edge ';
  assert.equal(await t.controller.launch(), true);
  const launch = t.calls.find(call => call[0].endsWith('/launch'));
  assert.equal(t.calls[0][0], '/api/federation/peer%2Fa/proxy/api/browser/instances', 'fresh registry is read before mutation');
  assert.deepEqual(Object.keys(launch[2]).sort(), ['executable', 'headless', 'name', 'request_id', 'revision'].sort());
  assert.deepEqual({...launch[2], request_id: '<uuid>'}, {revision: 3, request_id: '<uuid>', name: 'New managed', headless: false, executable: '/opt/ms edge'});
  assert.match(launch[2].request_id, /^[0-9a-f-]{36}$/i);
  t.elements['#browser-existing-name'].value = ' DevTools ';
  t.elements['#browser-existing-port'].value = '9333'; t.calls.length = 0;
  assert.equal(await t.controller.addExisting(), true);
  const add = t.calls.find(call => call[0].endsWith('/add'));
  assert.deepEqual(Object.keys(add[2]).sort(), ['name', 'port', 'request_id', 'revision'].sort());
  assert.equal(add[2].name, 'DevTools'); assert.equal(add[2].port, 9333); assert.equal(add[2].revision, 4);
});

test('explicit IPv4 and IPv6 addresses post canonical host through the owner proxy', async () => {
  const t = settingsFixture({source: 'peer/a'}); await t.controller.load();
  t.elements['#browser-existing-name'].value = ' IPv4 owner '; t.elements['#browser-existing-port'].value = '10.0.0.8:9333';
  t.calls.length = 0; assert.equal(await t.controller.addExisting(), true);
  let add = t.calls.find(call => call[0].endsWith('/add'));
  assert.equal(add[0], '/api/federation/peer%2Fa/proxy/api/browser/instances/add');
  assert.deepEqual({...add[2], request_id: '<uuid>'}, {revision: 3, request_id: '<uuid>', name: 'IPv4 owner', port: 9333, host: '10.0.0.8'});

  t.elements['#browser-existing-name'].value = 'IPv6 owner'; t.elements['#browser-existing-port'].value = '[2001:0db8::1]:9444';
  t.calls.length = 0; assert.equal(await t.controller.addExisting(), true);
  add = t.calls.find(call => call[0].endsWith('/add'));
  assert.deepEqual({...add[2], request_id: '<uuid>'}, {revision: 4, request_id: '<uuid>', name: 'IPv6 owner', port: 9444, host: '2001:db8::1'});

  t.elements['#browser-existing-port'].value = '[::1]@evil.test:9222'; t.calls.length = 0;
  assert.equal(await t.controller.addExisting(), false); assert.equal(t.calls.length, 0);
  assert.match(t.elements['#browser-settings-status'].textContent, /port, IPv4:port, or \[IPv6\]:port/);
});

test('external stop does nothing; external remove is safe; running managed remove is disabled', async () => {
  const t = settingsFixture(); await t.controller.load(); t.calls.length = 0;
  assert.equal(await t.controller.stop(B), false); assert.equal(t.calls.length, 0);
  assert.equal(await t.controller.remove(A), false); assert.equal(t.calls.length, 0);
  const managedRemove = t.elements['#browser-management-list'].children[0].children.at(-1);
  const externalStop = t.elements['#browser-management-list'].children[1].children[2];
  assert.equal(managedRemove.disabled, true); assert.equal(externalStop.disabled, true);
  assert.equal(await t.controller.remove(B), true);
  const post = t.calls.find(call => call[0].endsWith('/remove'));
  assert.deepEqual(Object.keys(post[2]).sort(), ['id', 'request_id', 'revision'].sort()); assert.equal(post[2].id, B);
});

test('revision conflict refreshes projection and requires operator review without POST', async () => {
  let gets = 0;
  const t = settingsFixture({request: async (url, method) => {
    if (url.endsWith('/discovery')) return {browsers: [], unavailable: []};
    if (method === 'POST') throw Error('must not post');
    gets += 1; return gets === 1 ? registry(3) : registry(4, [managed(A, 'Changed')]);
  }});
  await t.controller.load(); t.elements['#browser-instance-name'].value = 'new'; t.calls.length = 0;
  assert.equal(await t.controller.launch(), false);
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 0);
  assert.equal(t.controller.registry.revision, 4);
  assert.match(t.elements['#browser-settings-status'].textContent, /changed.*review/i);
});

test('lost lifecycle POST locks mutations with its UUID until a successful explicit refresh', async () => {
  let posts = 0;
  const t = settingsFixture({request: async (url, method) => {
    if (url.endsWith('/discovery')) return {browsers: [], unavailable: []};
    if (method === 'POST') { posts += 1; throw Error('transport lost'); }
    return registry(3);
  }});
  await t.controller.load(); t.elements['#browser-instance-name'].value = 'new';
  assert.equal(await t.controller.launch(), false); assert.equal(posts, 1); assert.equal(t.controller.uncertain, true);
  assert.match(t.elements['#browser-settings-status'].textContent, /Request ID [0-9a-f-]{36}/i);
  assert.equal(await t.controller.launch(), false); assert.equal(posts, 1, 'uncertain action is never retried');
  await t.controller.load(true); assert.equal(t.controller.uncertain, false);
});

test('uncertain 409 preserves the posted request ID and blocks another POST until refresh', async () => {
  let posts = 0, postedId;
  const t = settingsFixture({request: async (url, method, body) => {
    if (url.endsWith('/discovery')) return {browsers: [], unavailable: []};
    if (method === 'POST') {
      posts += 1; postedId = body.request_id;
      throw Object.assign(Error(`Browser outcome uncertain. Request ID: ${body.request_id}`), {status: 409});
    }
    return registry(3);
  }});
  await t.controller.load(); t.elements['#browser-instance-name'].value = 'new';
  assert.equal(await t.controller.launch(), false); assert.equal(posts, 1); assert.equal(t.controller.uncertain, true);
  assert.match(postedId, /^[0-9a-f-]{36}$/i);
  assert.match(t.elements['#browser-settings-status'].textContent, new RegExp(postedId));
  assert.equal(await t.controller.launch(), false); assert.equal(posts, 1);
  await t.controller.load(true); assert.equal(t.controller.uncertain, false);
});

test('mutation acknowledgement accepts a valid projection advanced by an unrelated revision', async () => {
  const t = settingsFixture({request: async (url, method, body) => {
    if (url.endsWith('/discovery')) return {browsers: [], unavailable: []};
    if (method === 'GET') return registry(3);
    return {...registry(5, [managed(), external(), managed(C, 'New')]), operation: {browser_id: C}, request_id: body.request_id};
  }});
  await t.controller.load(); t.elements['#browser-instance-name'].value = 'New';
  assert.equal(await t.controller.launch(), true);
  assert.equal(t.controller.registry.revision, 5); assert.deepEqual(t.controller.operationIds, [C]);
  assert.equal(t.changed.length, 1);
});

test('slow lower-revision settings GET cannot overwrite a newer projection', async () => {
  let release, gets = 0;
  const pending = new Promise(resolve => { release = resolve; });
  const t = settingsFixture({request: async (url) => {
    if (url.endsWith('/discovery')) return {browsers: [], unavailable: []};
    gets += 1; return gets === 1 ? registry(3) : pending;
  }});
  await t.controller.load();
  const loading = t.controller.load(false); await tick();
  t.controller.setContext(registry(8, [external()]));
  release(registry(4, [managed(A, 'Stale')])); await loading;
  assert.equal(t.controller.registry.revision, 8);
  assert.deepEqual(t.controller.registry.browsers.map(item => item.id), [B]);
});

test('owner context change clears busy state and stale completion has no effect', async () => {
  let release; const pending = new Promise(resolve => { release = resolve; });
  let source = 'local';
  const t = settingsFixture({request: async (url, method, body) => {
    if (url.endsWith('/discovery')) return {browsers: [], unavailable: []};
    if (method === 'POST') return pending;
    return source === 'local' ? registry(1) : registry(9, [external()]);
  }});
  await t.controller.load(); t.elements['#browser-instance-name'].value = 'new';
  const saving = t.controller.launch(); await tick(); await tick(); assert.equal(t.controller.saving, true);
  source = 'peer/b'; t.switchSource('peer/b');
  assert.equal(t.controller.saving, false); assert.equal(t.controller.loading, false); assert.equal(t.controller.registry, null);
  release({...registry(2), operation: {browser_id: A}}); await saving;
  assert.equal(t.controller.source, 'peer/b'); assert.equal(t.controller.registry, null); assert.equal(t.changed.length, 0);
});

test('rename uses captured ID/name and list labels containing quotes stay text-only', async () => {
  const nasty = managed(A, `O'Reilly <script>alert(1)</script>`);
  const t = settingsFixture({registry: registry(3, [nasty]), prompt: () => `"safe" renamed`}); await t.controller.load();
  const label = t.elements['#browser-management-list'].children[0].children[0];
  assert.equal(label.textContent, `${nasty.name} — managed — running`); assert.equal(label.children.length, 0);
  t.calls.length = 0; assert.equal(await t.controller.rename(A), true);
  const post = t.calls.find(call => call[0].endsWith('/rename'));
  assert.equal(post[2].id, A); assert.equal(post[2].name, `"safe" renamed`);
});

test('source keeps EdgeMCP screenshot core and forbids old lifecycle/protocol controls', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/remote-browser.js'), 'utf8');
  assert.match(source, /c09dc257886305539ea90bc454ceda63b9b150a6/);
  assert.match(source, /createObjectURL\?\.\(blob\)/);
  assert.doesNotMatch(source, /WebSocket|createSocket|frame_id|MAX_FRAME|stream extension/i);
  assert.doesNotMatch(source, /\.innerHTML\s*=|document\.write|<iframe|evaluate\s*\(/i);
  assert.doesNotMatch(source, /#browser-enabled|toggleLaunch|saveSettings/);
  assert.equal(MAX_QUEUE, 32);
});
