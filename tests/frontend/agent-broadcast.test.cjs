const test = require('node:test');
const assert = require('node:assert/strict');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.events = new Map(); this.open = false; this.disabled = false;
    this.checked = false; this.value = ''; this.text = ''; this.className = ''; this.type = '';
  }
  append(...values) { this.children.push(...values); }
  replaceChildren(...values) { this.children = values; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(value => value.textContent || '').join(''); }
  addEventListener(type, callback) { this.events.set(type, callback); }
  dispatch(type, extra = {}) { return this.events.get(type)?.({preventDefault() { this.defaultPrevented = true; }, currentTarget: this, ...extra}); }
  click() { if (!this.disabled) return this.dispatch('click'); }
  showModal() { this.open = true; }
  close() { if (!this.open) return; this.open = false; this.dispatch('close'); }
  reportValidity() { this.reported = true; }
}
const walk = value => value instanceof Element ? [value, ...value.children.flatMap(walk)] : [];
global.document = {createElement: tag => new Element(tag)};
const {BroadcastController, statePath, messagesPath, identity} = require('../../src/swarm/static/agent-broadcast.js');
const tick = () => new Promise(resolve => setImmediate(resolve));

const localAgents = [
  {id: 'same', name: '<Local Alpha>', goal: 'Build parser', swarm: 'core', managed: true, alive: true, status: 'done'},
  {id: 'sleep', name: 'Sleeper', goal: 'Write docs', swarm: 'core', managed: true, alive: false, status: 'waiting'},
  {id: 'observed', name: '<script>bad()</script>', goal: 'Observe', managed: false, alive: true},
];
const remoteAgents = [
  {id: 'same', name: 'Remote Alpha', goal: 'Remote research', swarm: 'edge', managed: true, alive: false, status: 'dormant'},
  {id: 'awake', name: 'Remote Live', goal: 'Ship UI', swarm: 'edge', managed: true, alive: true, status: 'running'},
];

function fixture(handler, options = {}) {
  const ids = ['notifications-dialog', 'notification-broadcast-text', 'notification-broadcast-open', 'notification-broadcast-status',
    'broadcast-agents-dialog', 'broadcast-scope', 'broadcast-awake', 'broadcast-search', 'broadcast-select-all', 'broadcast-select-none',
    'broadcast-targets', 'broadcast-summary', 'broadcast-errors', 'broadcast-send', 'broadcast-results', 'broadcast-refresh',
    'broadcast-close', 'broadcast-cancel'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, new Element(id.includes('dialog') ? 'dialog' : id.includes('text') ? 'textarea' : 'div')]));
  elements['#notifications-dialog'].open = true;
  let swarm = 'local', generation = 1;
  const calls = [], confirmations = [], changes = [];
  const controller = new BroadcastController({
    query: selector => elements[selector],
    create: (tag, cls = '', text = '') => Object.assign(new Element(tag), {className: cls, textContent: text}),
    api: async (...args) => { calls.push(args); return handler(...args); },
    currentSwarm: () => swarm, currentGeneration: () => generation,
    sources: () => options.sources || [{id: 'local', name: 'Local Swarm'}, {id: 'peer/a', name: '<Peer & A>', online: true}],
    confirm: question => { confirmations.push(question); return options.confirm ? options.confirm(question) : true; },
    onChange: async value => changes.push(value),
    requestTimeout: options.requestTimeout ?? 20000,
    setTimer: options.setTimer,
    clearTimer: options.clearTimer,
  });
  return {controller, elements, calls, confirmations, changes,
    switchTo(value) { swarm = value; generation += 1; }, bumpGeneration() { generation += 1; }};
}

function standard(path, method, body) {
  if (!method) return path === '/api/state' ? {agents: localAgents} : {agents: remoteAgents};
  const id = decodeURIComponent(path.match(/agents\/([^/]+)\/messages$/)[1]);
  return {agent_id: id, message: {id: body.request_id, text: body.text, status: 'pending'}};
}
async function open(t, text = ' durable notice ') { t.elements['#notification-broadcast-text'].value = text; await t.controller.open(); }


test('filters liveness, scope, observed agents, and colliding IDs independently', async () => {
  const t = fixture(standard); await open(t);
  assert.equal(t.elements['#notifications-dialog'].open, false); assert.equal(t.elements['#broadcast-agents-dialog'].open, true);
  assert.equal(t.controller.rows.size, 2, 'current scope shows two managed local agents');
  assert.ok(t.controller.rows.has(identity('local', 'same'))); assert.ok(!t.controller.rows.has(identity('peer/a', 'same')));
  assert.match(t.elements['#broadcast-summary'].textContent, /“Awake”.*live process.*dormant or done.*never wakes/is);
  assert.match(t.elements['#broadcast-targets'].textContent, /observed agent.*not selectable/i);

  t.elements['#broadcast-awake'].value = 'awake'; t.elements['#broadcast-awake'].dispatch('change');
  assert.deepEqual([...t.controller.rows.keys()], [identity('local', 'same')], 'alive=true counts awake even when done');
  t.elements['#broadcast-scope'].value = 'all'; t.elements['#broadcast-awake'].value = 'asleep'; t.elements['#broadcast-scope'].dispatch('change');
  assert.deepEqual([...t.controller.rows.keys()].sort(), [identity('local', 'sleep'), identity('peer/a', 'same')].sort());
  t.elements['#broadcast-search'].value = 'remote research'; t.elements['#broadcast-search'].dispatch('input');
  assert.deepEqual([...t.controller.rows.keys()], [identity('peer/a', 'same')]);
});


test('all-swarm send uses captured absolute auth paths, immutable message and visible targets only', async () => {
  const t = fixture(standard, {confirm: () => {
    t.elements['#notification-broadcast-text'].value = 'changed after confirmation';
    t.elements['#broadcast-search'].value = 'nothing';
    return true;
  }});
  await open(t, ' original message ');
  t.elements['#notification-broadcast-text'].value = 'changed in background before send';
  t.elements['#broadcast-scope'].value = 'all'; t.elements['#broadcast-scope'].dispatch('change');
  // Hide one checked row before send: hidden checked targets must not leak into the snapshot.
  t.elements['#broadcast-search'].value = 'Alpha'; t.elements['#broadcast-search'].dispatch('input');
  await t.controller.send();
  const posts = t.calls.filter(call => call[1] === 'POST');
  assert.deepEqual(posts.map(call => call[0]).sort(), [
    '/api/agents/same/messages', '/api/federation/peer%2Fa/proxy/api/agents/same/messages',
  ]);
  assert.ok(posts.every(call => call[2].text === 'original message'));
  assert.ok(posts.every(call => /^[0-9a-f-]{36}$/i.test(call[2].request_id)));
  assert.match(t.confirmations[0], /2 agents.*2 Swarms.*queued.*does not wake/is);
  assert.deepEqual(t.changes, [{noAutoWake: true}]);
  assert.equal(t.calls.some(call => /wake|terminal/i.test(call[0])), false);
});


test('select shown/none controls only visible rows', async () => {
  const t = fixture(standard); await open(t);
  t.elements['#broadcast-search'].value = 'Sleeper'; t.elements['#broadcast-search'].dispatch('input');
  t.elements['#broadcast-select-none'].click();
  assert.equal(t.controller.checked.get(identity('local', 'sleep')), false);
  assert.equal(t.controller.checked.get(identity('local', 'same')), true, 'hidden selection is unchanged');
  t.elements['#broadcast-select-all'].click(); assert.equal(t.controller.checked.get(identity('local', 'sleep')), true);
});


test('partial outcomes retain request IDs and classify 4xx versus uncertain responses', async () => {
  const t = fixture(async (path, method, body) => {
    if (!method) return path === '/api/state' ? {agents: localAgents} : {agents: remoteAgents};
    if (path.includes('/sleep/')) throw Object.assign(Error('not allowed'), {status: 403});
    if (path.includes('/peer%2Fa/') && path.includes('/same/')) throw Object.assign(Error('backend broke'), {status: 503});
    if (path.endsWith('/awake/messages')) return {agent_id: 'awake', message: {id: 'different-request', text: body.text, status: 'pending'}};
    return {agent_id: 'same', message: {id: body.request_id, text: body.text, status: 'pending'}};
  });
  await open(t); t.elements['#broadcast-scope'].value = 'all'; t.elements['#broadcast-scope'].dispatch('change');
  await t.controller.send();
  assert.deepEqual(t.controller.results.map(result => result.outcome), ['queued', 'failed', 'uncertain', 'uncertain']);
  assert.equal(new Set(t.controller.results.map(result => result.requestId)).size, 4);
  assert.match(t.elements['#broadcast-results'].textContent, /queued.*failed.*uncertain.*Request ID/is);
  assert.equal(t.elements['#notification-broadcast-text'].value, ' durable notice ', 'partial failure does not clear draft');
  const firstPostCount = t.calls.filter(call => call[1] === 'POST').length;
  await t.controller.send();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, firstPostCount + 1, 'only the definite 4xx failure is eligible for an explicit repeat');
});


test('invalid/offline state is excluded and unsafe names render only as text', async () => {
  const t = fixture(async path => path === '/api/state' ? {agents: localAgents} : {federation_offline: true, agents: remoteAgents});
  await open(t);
  assert.match(t.elements['#broadcast-errors'].textContent, /Peer & A.*excluded/);
  assert.ok(walk(t.elements['#broadcast-targets']).some(node => node.textContent.includes('<Local Alpha>')));
  assert.equal(walk(t.elements['#broadcast-targets']).some(node => ['SCRIPT', 'IMG'].includes(node.tagName)), false);
  assert.deepEqual(t.calls.map(call => call[0]), [statePath('local'), statePath('peer/a')]);
});


test('close or source switch stops new POSTs, suppresses stale rendering, and retains not-sent IDs', async () => {
  let release;
  const t = fixture(async (path, method, body) => {
    if (!method) return path === '/api/state' ? {agents: localAgents} : {agents: remoteAgents};
    return new Promise(resolve => { release = () => resolve({agent_id: decodeURIComponent(path.match(/agents\/([^/]+)/)[1]), message: {id: body.request_id, text: body.text, status: 'pending'}}); });
  });
  await open(t); const before = t.elements['#broadcast-results'].textContent;
  const sending = t.controller.send(); await tick();
  assert.equal(t.controller.results.length, 2, 'all IDs allocated before first POST finishes');
  t.elements['#broadcast-agents-dialog'].close(); t.switchTo('peer/a'); release(); await sending;
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
  assert.deepEqual(t.controller.results.map(result => result.outcome), ['queued', 'not-sent']);
  assert.match(t.controller.results[1].detail, /dialog closed or the Swarm changed/);
  assert.notEqual(t.controller.results[0].requestId, t.controller.results[1].requestId);
  assert.deepEqual(t.changes, [], 'a stale completion cannot refresh the newly selected Swarm');
  assert.notEqual(before, undefined);

  await t.controller.open();
  assert.match(t.elements['#broadcast-results'].textContent, new RegExp(t.controller.results[0].requestId));
  assert.match(t.elements['#broadcast-results'].textContent, new RegExp(t.controller.results[1].requestId));
  assert.match(t.elements['#broadcast-results'].textContent, /queued.*not-sent/is, 'late outcome remains inspectable after reopen');
});


test('double click and repeated send never blindly resend queued or uncertain targets', async () => {
  let release;
  const t = fixture(async (path, method, body) => {
    if (!method) return path === '/api/state' ? {agents: [localAgents[0]]} : {agents: []};
    return new Promise((resolve, reject) => { release = path.includes('/same/')
      ? () => resolve({agent_id: 'same', message: {id: body.request_id, text: body.text, status: 'pending'}}) : () => reject(Error('lost')); });
  }, {sources: [{id: 'local', name: 'Local'}]});
  await open(t); const first = t.controller.send(); const duplicate = t.controller.send(); await tick();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1); release(); await Promise.all([first, duplicate]);
  await t.controller.send();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
  assert.match(t.elements['#broadcast-errors'].textContent, /do not retry blindly/i);
});


test('confirmed receipts survive reopen, suppress the same body, and allow a new draft', async () => {
  const t = fixture(standard, {sources: [{id: 'local', name: 'Local'}]});
  await open(t, 'same body');
  // Keep one target selected so the receipt identity is unambiguous.
  t.controller.rows.get(identity('local', 'sleep')).checkbox.checked = false;
  await t.controller.send();
  const receiptId = t.controller.results[0].requestId;
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);

  t.controller.close(); await open(t, 'same body');
  assert.match(t.elements['#broadcast-results'].textContent, new RegExp(receiptId));
  assert.match(t.elements['#broadcast-results'].textContent, /queued/);
  t.controller.rows.get(identity('local', 'sleep')).checkbox.checked = false;
  await t.controller.send();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1, 'same source, agent, and body is not resent');

  t.controller.close(); await open(t, 'a genuinely new draft');
  t.controller.rows.get(identity('local', 'sleep')).checkbox.checked = false;
  await t.controller.send();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 2, 'new text is a distinct receipt identity');
});


test('POST timeout is uncertain, has no retry, and ignores a late acknowledgement', async () => {
  let release;
  const t = fixture(async (path, method, body) => {
    if (!method) return {agents: [localAgents[0]]};
    return new Promise(resolve => {
      release = () => resolve({agent_id: 'same', message: {id: body.request_id, text: body.text, status: 'pending'}});
    });
  }, {sources: [{id: 'local', name: 'Local'}], requestTimeout: 5});
  await open(t, 'timeout body');
  await t.controller.send();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
  assert.equal(t.controller.results[0].outcome, 'uncertain');
  assert.match(t.controller.results[0].detail, /timed out.*do not retry blindly/i);

  const receiptId = t.controller.results[0].requestId;
  t.controller.close(); await open(t, 'timeout body');
  assert.match(t.elements['#broadcast-results'].textContent, new RegExp(receiptId));
  await t.controller.send();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1, 'timeout does not auto-retry or permit a blind resend');
  release(); await tick();
  assert.equal(t.controller.results[0].outcome, 'uncertain', 'late promise settlement cannot revise the timeout conclusion');
});


test('paths encode both source and agent IDs', () => {
  assert.equal(messagesPath('peer/a', 'agent one'), '/api/federation/peer%2Fa/proxy/api/agents/agent%20one/messages');
});


test('Notifications owns the composer and scoped controls; picker loads before app', () => {
  const fs=require('node:fs'),path=require('node:path');
  const html=fs.readFileSync(path.join(__dirname,'../../src/swarm/static/index.html'),'utf8');
  const inbox=html.split('id="notifications-dialog"')[1].split('</dialog>')[0];
  const topbar=html.split('<header class="topbar">')[1].split('</header>')[0];
  for(const id of ['fleet-control-button','settings-button','restart-controller','notification-broadcast-text','notification-broadcast-open']) {
    assert.ok(inbox.includes(`id="${id}"`));assert.ok(!topbar.includes(`id="${id}"`));
  }
  assert.match(inbox,/id="notification-broadcast-text"[^>]*maxlength="32000"/);
  assert.ok(html.indexOf('/static/agent-broadcast.js')<html.indexOf('/static/app.js'));
  assert.ok(html.includes('Only non-awaken (no live process)'));
});

test('an offline active Swarm does not silently select others but permits explicit all scope', async () => {
  const t=fixture(standard,{sources:[{id:'local',name:'Local'}]});t.switchTo('offline');await open(t);
  assert.equal(t.elements['#broadcast-agents-dialog'].open,true);
  assert.equal(t.controller.rows.size,0);
  assert.match(t.elements['#broadcast-errors'].textContent,/active Swarm is offline/);
  t.elements['#broadcast-scope'].value='all';t.elements['#broadcast-scope'].dispatch('change');
  assert.equal(t.controller.rows.size,2);
});
