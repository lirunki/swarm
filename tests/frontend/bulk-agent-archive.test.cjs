const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.events = new Map(); this._text = '';
    this.className = ''; this.checked = false; this.disabled = false; this.hidden = false; this.open = false;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child?.textContent || '').join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ''; }
  addEventListener(name, callback) { this.events.set(name, callback); }
  click() { if (!this.disabled) return this.events.get('click')?.({target: this}); }
  showModal() { this.open = true; }
  close() { const wasOpen = this.open; this.open = false; if (wasOpen) this.events.get('close')?.({target: this}); }
}

global.document = {createElement: tag => new Element(tag), querySelector: () => null};
require('../../src/swarm/static/agent-archives.js');
const {BulkAgentArchiveController, sourcePath, busyReason, archiveReason} = require('../../src/swarm/static/bulk-agent-archive.js');

const A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const C = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const ARCHIVE = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const REQUESTS = [
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000003',
  '10000000-0000-4000-8000-000000000004',
];
const IDS = ['group-bulk-forget', 'bulk-forget-dialog', 'bulk-forget-summary', 'bulk-forget-targets',
  'bulk-forget-include-files', 'bulk-forget-archive', 'bulk-forget-only', 'bulk-forget-cancel',
  'bulk-forget-results', 'bulk-forget-error'];

function agent(id, name, patch = {}) {
  return {id, name, managed: true, backend: 'pi', session_file: `/sessions/${id}.jsonl`, session_id: `session-${id}`,
    working_folder: `/work/${id}`, alive: false, status: 'idle', spawn_state: 'ready', ...patch};
}
function archiveAck(path, body, patch = {}) {
  const id = [A, B, C].find(value => path.includes(value));
  return {job: {id: body.request_id, archive_id: ARCHIVE, agent_id: id, name: 'a stale name is allowed',
    status: 'queued', include_files: body.include_files, ...patch}};
}
function fixture(options = {}) {
  const elements = Object.fromEntries(IDS.map(id => [`#${id}`, new Element(id.includes('dialog') ? 'dialog' : id.includes('include') ? 'input' : 'div')]));
  let source = options.source || 'local', generation = 3;
  let records = options.agents || [agent(A, '<Alpha>'), agent(B, 'Beta')];
  const selected = new Set(options.selected || [A, B]);
  const calls = [], confirms = [], changes = [], toasts = [];
  const api = (...args) => { calls.push(args); return options.api ? options.api(...args) : Promise.resolve({}); };
  const controller = new BulkAgentArchiveController({
    query: selector => elements[selector] || null, api,
    currentSwarm: () => source, currentGeneration: () => generation,
    agents: () => records, selectedIDs: () => selected,
    confirm: message => { confirms.push(message); return options.confirm !== false; },
    onChanged: async value => changes.push(value), toast: (...values) => toasts.push(values),
    requestTimeout: options.requestTimeout, setTimer: options.setTimer, clearTimer: options.clearTimer,
  });
  return {controller, elements, selected, calls, confirms, changes, toasts,
    records: () => records, setRecords(value) { records = value; },
    switchSource(value) { source = value; generation += 1; }, source: () => source, generation: () => generation};
}
function installRequestIDs() {
  let index = 0;
  global.SwarmAgentArchives.secureRequestId = () => REQUESTS[index++];
}
function tick() { return new Promise(resolve => setImmediate(resolve)); }

test('parent loads and wires one dedicated bulk dialog without sharing single-agent controls', () => {
  const root = path.resolve(__dirname, '../..');
  const html = fs.readFileSync(path.join(root, 'src/swarm/static/index.html'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'src/swarm/static/app.js'), 'utf8');
  const source = fs.readFileSync(path.join(root, 'src/swarm/static/bulk-agent-archive.js'), 'utf8');
  for (const id of IDS) assert.equal((html.match(new RegExp(`id=["']${id}["']`, 'g')) || []).length, 1, `${id} is unique`);
  assert.match(html, /bulk-agent-archive\.js[^>]*><\/script>[\s\S]*app\.js/);
  assert.match(app, /new SwarmBulkAgentArchive\.BulkAgentArchiveController\([\s\S]*agents:\(\)=>state\.agents[\s\S]*selectedIDs:\(\)=>agentGroupsController\.selectedIDs/);
  assert.doesNotMatch(source, /#archive-agent-(?:choice|name|include|submit|forget|cancel)/);
  assert.equal(typeof BulkAgentArchiveController.prototype.sync, 'function');
});

test('all live, stopping, spawning, and archiving agents are ineligible', () => {
  assert.equal(sourcePath('local', '/api/agents/x'), '/api/agents/x');
  assert.equal(sourcePath('peer/a', '/api/agents/x'), '/api/federation/peer%2Fa/proxy/api/agents/x');
  for (const status of ['running', 'done', 'Dormant', 'idle']) {
    assert.equal(busyReason(agent(A, 'A', {alive: true, status})), 'Stop this agent first');
    assert.equal(archiveReason(agent(A, 'A', {alive: true, status})), 'Stop this agent first');
  }
  assert.match(busyReason(agent(A, 'A', {stop_pending: true})), /stop.*pending/i);
  assert.match(busyReason(agent(A, 'A', {orchestration_stop: true})), /stop.*pending/i);
  for (const spawn_state of ['provisioning', 'failed', 'cancelled', 'mystery'])
    assert.match(busyReason(agent(A, 'A', {spawn_state})), /Setup is incomplete/);
  assert.equal(busyReason(agent(A, 'A', {spawn_state: null})), '');
  assert.match(busyReason(agent(A, 'A', {agent_archive: {status: 'queued'}})), /already in progress/i);
  assert.match(archiveReason(agent(A, 'A', {managed: false})), /managed Pi session/i);
});

test('dialog freezes the selected family including hidden IDs and safely renders invalid targets', () => {
  const hidden = agent(B, 'Hidden child', {hidden: true});
  const t = fixture({agents: [agent(A, '<Alpha>'), hidden], selected: [A, B, C]});
  t.elements['#group-bulk-forget'].click();
  assert.equal(t.elements['#bulk-forget-dialog'].open, true);
  assert.equal(t.elements['#bulk-forget-include-files'].checked, false);
  assert.equal(t.elements['#bulk-forget-summary'].textContent, '3 selected agents · 2 can be archived · 2 can be forgotten.');
  assert.match(t.elements['#bulk-forget-targets'].textContent, /<Alpha>.*Hidden child.*No longer registered/s);
  assert.equal(t.elements['#bulk-forget-targets'].children.some(child => child.tagName === 'IMG'), false);

  t.selected.clear(); t.selected.add(C); t.setRecords([agent(C, 'New child')]);
  assert.deepEqual(t.controller.context.targets.map(item => item.id), [A, B, C], 'open dialog owns a frozen ID snapshot');
  assert.equal(t.controller.context.targets[2].missing, true, 'an originally missing selected ID remains an explicit skip');
});

test('archive preallocates IDs, runs sequentially, accepts renamed jobs, and never sends stop', async () => {
  installRequestIDs();
  const releases = [];
  const t = fixture({source: 'peer/a', api: (path, method, body) => new Promise(resolve => releases.push(() => resolve(archiveAck(path, body))))});
  t.controller.open(); t.elements['#bulk-forget-include-files'].checked = true;
  const pending = t.controller.run('archive');
  assert.equal(t.calls.length, 1, 'bulk work is deliberately sequential');
  assert.equal(t.calls[0][0], `/api/federation/peer%2Fa/proxy/api/agents/${A}/archive-and-forget`);
  assert.deepEqual(t.calls[0][2], {include_files: true, request_id: REQUESTS[0]});
  releases[0](); await tick();
  assert.equal(t.calls.length, 2); assert.deepEqual(t.calls[1][2], {include_files: true, request_id: REQUESTS[1]});
  releases[1](); assert.equal(await pending, true);
  assert.equal(t.calls.some(call => /stop/i.test(call[0])), false);
  assert.deepEqual(t.changes, [{refresh: true, noAutoWake: true, source: 'peer/a', generation: 3}]);
  assert.match(t.confirms[0], /list is frozen.*No agent will be stopped automatically/is);
  assert.match(t.elements['#bulk-forget-results'].textContent, new RegExp(`${REQUESTS[0]}.*${REQUESTS[1]}`, 's'));
});

test('current state and identity are re-read before every mutation', async () => {
  installRequestIDs();
  let release;
  const t = fixture({api: (path, method, body) => new Promise(resolve => { release = () => resolve(archiveAck(path, body)); })});
  t.controller.open(); const pending = t.controller.run('archive');
  assert.equal(t.calls.length, 1);
  t.setRecords([agent(A, '<Alpha>'), agent(B, 'Beta', {alive: true, status: 'Dormant'})]);
  release(); assert.equal(await pending, false);
  assert.equal(t.calls.length, 1, 'the newly live second target is skipped without an automatic stop');
  assert.match(t.elements['#bulk-forget-results'].textContent, /Beta — left unchanged: Stop this agent first/);

  installRequestIDs();
  const changed = fixture({selected: [A], api: async (path, method, body) => archiveAck(path, body)});
  changed.controller.open(); changed.setRecords([agent(A, '<Alpha>', {session_id: 'replacement'})]);
  assert.equal(await changed.controller.run('archive'), false);
  assert.equal(changed.calls.length, 0);
  assert.match(changed.elements['#bulk-forget-results'].textContent, /identity or saved session changed/i);
});

test('definite 4xx failures continue, while acknowledgements validate include_files', async () => {
  installRequestIDs();
  const t = fixture({api: async (path, method, body) => {
    if (path.includes(A)) throw Object.assign(Error('<unsafe 422>'), {status: 422});
    return archiveAck(path, body, {include_files: !body.include_files});
  }});
  t.controller.open(); assert.equal(await t.controller.run('archive'), false);
  assert.equal(t.calls.length, 2, 'a known client failure does not hide independent outcomes');
  assert.match(t.elements['#bulk-forget-results'].textContent, /<unsafe 422>.*Invalid archive acknowledgement.*outcome uncertain/s);
  assert.equal(t.elements['#bulk-forget-results'].children.some(child => child.tagName === 'UNSAFE'), false);
});

test('timeout stops later POSTs, reports not-sent targets, and reopening does not retry blindly', async () => {
  installRequestIDs();
  const timers = [];
  let invocation = 0;
  const t = fixture({requestTimeout: 20000, setTimer: callback => { timers.push(callback); return timers.length; }, clearTimer: () => {},
    api: (path, method, body) => {
      invocation += 1;
      if (invocation === 1) return new Promise(() => {});
      return Promise.resolve(archiveAck(path, body));
    }});
  t.controller.open(); const first = t.controller.run('archive');
  assert.equal(t.calls.length, 1);
  assert.deepEqual(t.calls[0][2], {include_files: false, request_id: REQUESTS[0]});
  timers[0](); assert.equal(await first, false);
  assert.equal(t.calls.length, 1);
  assert.match(t.elements['#bulk-forget-results'].textContent, new RegExp(`outcome uncertain.*${REQUESTS[0]}.*Beta — not sent`, 's'));

  t.controller.close(); t.controller.open();
  const second = await t.controller.run('archive');
  assert.equal(second, true, 'only the independently safe, previously not-sent target is part of this run');
  assert.equal(t.calls.length, 2);
  assert.ok(t.calls[1][0].includes(B), 'only the previously not-sent target is attempted');
  assert.deepEqual(t.calls[1][2], {include_files: false, request_id: REQUESTS[2]}, 'IDs were allocated for all first-run targets before its first POST');
});

test('forget-only warns explicitly, skips live idle agents, and keeps saved-session semantics', async () => {
  const records = [agent(A, 'Live idle', {alive: true, status: 'idle'}), agent(B, 'Stopped')];
  const t = fixture({agents: records, api: async path => ({id: B, forgotten: path.includes(B)})});
  t.controller.open(); assert.equal(await t.controller.run('forget'), true);
  assert.equal(t.calls.length, 1); assert.ok(t.calls[0][0].includes(B));
  assert.deepEqual(t.calls[0].slice(1), ['DELETE']);
  assert.match(t.confirms[0], /does not send a stop request.*remaining Swarm process to stop.*Saved sessions/is);
  assert.match(t.elements['#bulk-forget-results'].textContent, /Live idle — left unchanged: Stop this agent first.*saved session kept/s);
  assert.deepEqual(t.changes, [{refresh: true, noAutoWake: true, source: 'local', generation: 3}]);
});

test('forget receipt keys block repeats but permit a restored ID with a new session', async () => {
  const t = fixture({selected: [A], api: async path => ({id: A, forgotten: path.includes(A)})});
  t.controller.open(); assert.equal(await t.controller.run('forget'), true); assert.equal(t.calls.length, 1);
  t.controller.close(); t.setRecords([]); t.controller.open();
  assert.equal(await t.controller.run('forget'), false); assert.equal(t.calls.length, 1, 'a missing completed agent is naturally omitted');

  t.controller.close(); t.setRecords([agent(A, 'Restored', {session_id: 'new-session', session_file: '/sessions/new.jsonl'})]); t.controller.open();
  assert.equal(await t.controller.run('forget'), true); assert.equal(t.calls.length, 2, 'a restored identity has a distinct receipt key');
});

test('sync cancels old scope immediately and an old completion cannot unlock a new operation', async () => {
  const pendingRequests = [];
  const t = fixture({source: 'peer/a', selected: [A], api: () => new Promise(resolve => pendingRequests.push(resolve))});
  t.controller.open(); const oldRun = t.controller.run('forget');
  assert.equal(t.calls.length, 1); assert.equal(t.controller.busy, true);
  t.switchSource('peer/b'); assert.equal(t.controller.sync(), true);
  assert.equal(t.elements['#bulk-forget-dialog'].open, false); assert.equal(t.controller.busy, false);

  t.controller.open(); const newRun = t.controller.run('forget');
  assert.equal(t.calls.length, 2); assert.equal(t.controller.busy, true);
  pendingRequests[0]({id: A, forgotten: true}); await tick();
  assert.equal(await oldRun, false);
  assert.equal(t.controller.busy, true, 'the stale finally/result cannot enable controls owned by the new token');
  pendingRequests[1]({id: A, forgotten: true}); assert.equal(await newRun, true);
  assert.deepEqual(t.changes, [{refresh: true, noAutoWake: true, source: 'peer/b', generation: 4}]);
});

test('programmatic dialog close invalidates an in-flight loop but retains its receipt', async () => {
  let release;
  const t = fixture({selected: [A], api: () => new Promise(resolve => { release = resolve; })});
  t.controller.open(); const pending = t.controller.run('forget');
  t.elements['#bulk-forget-dialog'].close();
  release({id: A, forgotten: true}); assert.equal(await pending, false);
  assert.equal(t.controller.busy, false); assert.deepEqual(t.changes, []);
  t.controller.open(); assert.equal(await t.controller.run('forget'), false);
  assert.equal(t.calls.length, 1, 'the successful stale response remains in the receipt ledger');
});
