const test = require('node:test');
const assert = require('node:assert/strict');

require('../../src/swarm/static/agent-groups.js');
const {safeBrowserGroups, BrowserGroups, GroupingModeController, validSettings} = require('../../src/swarm/static/grouping-mode.js');

const RED = '11111111-1111-4111-8111-111111111111';
const BLUE = '22222222-2222-4222-8222-222222222222';
const ROOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const CHILD = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const REQUEST = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const agents = [{id: ROOT}, {id: CHILD, parent_id: ROOT}];
const seed = (changes = {}) => ({revision: 0, groups: [{id: RED, name: 'Red'}], assignments: {[ROOT]: RED}, ...changes});

class MemoryStorage {
  constructor(values = {}) { this.values = new Map(Object.entries(values)); this.writes = []; }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, value); this.writes.push({key, value}); }
}

class FakeLockManager {
  constructor() { this.tails = new Map(); this.requests = []; }
  request(name, options, callback) {
    this.requests.push({name, options});
    const previous = this.tails.get(name) || Promise.resolve();
    const current = previous.catch(() => {}).then(() => callback());
    this.tails.set(name, current.catch(() => {}));
    return current;
  }
}

class Element {
  constructor() {
    this.textContent = ''; this.disabled = false; this.checked = false; this.open = false;
    this.events = new Map(); this.children = []; this.attributes = new Map();
  }
  addEventListener(name, callback) { if (!this.events.has(name)) this.events.set(name, []); this.events.get(name).push(callback); }
  dispatch(name, extra = {}) { const event = {target: this, preventDefault() { this.defaultPrevented = true; }, ...extra}; for (const fn of this.events.get(name) || []) fn(event); return event; }
  click() { if (!this.disabled) this.dispatch('click'); }
  showModal() { this.open = true; }
  close() { if (!this.open) return; this.open = false; this.dispatch('close'); }
  querySelectorAll() { return this.children.filter(child => child.dataClose); }
  querySelector() { return null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
}
const tick = () => new Promise(resolve => setImmediate(resolve));

function settings(changes = {}) {
  return {revision: 3, server_managed: false, initialized: false, grouped_branches: [], ...changes};
}

function controllerFixture(options = {}) {
  const ids = ['grouping-mode-owner', 'grouping-server-managed', 'grouping-mode-status', 'grouping-mode-change',
    'grouping-prime-dialog', 'grouping-prime-owner', 'grouping-prime-summary', 'grouping-prime-current',
    'grouping-prime-server', 'grouping-prime-cancel', 'grouping-prime-title', 'notifications-dialog', 'settings-dialog',
    'relay-settings-section'];
  const nodes = Object.fromEntries(ids.map(id => [`#${id}`, new Element()]));
  nodes['#notifications-dialog'].open = true;
  const relaySection = nodes['#relay-settings-section']; let relayOpen = false;
  Object.defineProperty(relaySection, 'open', {configurable: true, get: () => relayOpen, set: value => {
    const changed = relayOpen !== !!value; relayOpen = !!value;
    if (changed) queueMicrotask(() => relaySection.dispatch('toggle'));
  }});
  let source = 'local', generation = 1, current = settings();
  const calls = [], changed = [], errors = [], confirms = [];
  const api = async (path, method, body) => {
    calls.push({path, method, body});
    if (options.api) return options.api(path, method, body);
    if (method === 'POST') return current = settings({...current, revision: current.revision + 1, server_managed: body.server_managed, initialized: current.initialized || body.server_managed});
    if (path.endsWith('/api/groups') || path === '/api/groups') return seed({revision: current.revision});
    return {...current};
  };
  const controller = new GroupingModeController({query: selector => nodes[selector], api,
    currentSwarm: () => source, currentGeneration: () => generation, ownerName: value => value === 'local' ? 'Local' : `Remote ${value}`,
    projection: () => seed(), branches: () => [ROOT], primeSnapshot: options.primeSnapshot,
    onChanged: async (...args) => changed.push(args), onError: message => errors.push(message),
    confirm: async text => { confirms.push(text); return options.confirm !== false; }, requestTimeout: options.requestTimeout ?? 20000,
    schedule: options.schedule, cancel: options.cancel,
  });
  controller.setState(current);
  return {controller, nodes, calls, changed, errors, confirms,
    setCurrent(value) { current = value; }, source(value) { source = value; }, generation(value) { generation = value; }};
}

test('safeBrowserGroups creates isolated stores and does not persist a first read', () => {
  const storage = new MemoryStorage(), store = safeBrowserGroups(() => storage);
  assert.ok(store instanceof BrowserGroups);
  const value = store.load('owner-a:/home/a', seed(), agents);
  assert.deepEqual(value.assignments, {[ROOT]: RED, [CHILD]: RED}, 'family assignment is reconciled through the shared helper');
  assert.equal(storage.writes.length, 0, 'loading a seed is not a write');
  store.apply('owner-a:/home/a', {action: 'rename', revision: 0, request_id: REQUEST, group_id: RED, name: 'Warm'}, agents, seed());
  assert.equal(storage.writes.length, 1);
  assert.equal(store.load('owner-b:/home/a', seed({groups: [{id: BLUE, name: 'Blue'}], assignments: {}}), agents).groups[0].id, BLUE);
});

test('browser initialization snapshots a reconciled seed once and is idempotent', () => {
  const storage = new MemoryStorage(), store = safeBrowserGroups(() => storage);
  const first = store.initialize('scope', seed(), agents);
  assert.deepEqual(first, {revision: 0, groups: [{id: RED, name: 'Red'}], assignments: {[ROOT]: RED, [CHILD]: RED}});
  assert.equal(storage.writes.length, 1);

  const second = store.initialize('scope', seed({groups: [{id: BLUE, name: 'New server state'}], assignments: {}}), agents);
  assert.deepEqual(second, first);
  assert.equal(storage.writes.length, 1, 'reinitialization does not write or adopt a later seed');
});

test('browser initialization throws on corrupt existing state without replacing it', () => {
  const storage = new MemoryStorage({scope: '{not json'}), store = safeBrowserGroups(() => storage);
  assert.throws(() => store.initialize('scope', seed(), agents), /could not be read/);
  assert.equal(storage.getItem('scope'), '{not json');
  assert.equal(storage.writes.length, 0);
});

test('browser initialization never overwrites an existing browser snapshot', () => {
  const saved = {version: 1, revision: 7, groups: [{id: RED, name: 'Private'}], assignments: {old: RED}, receipts: []};
  const raw = JSON.stringify(saved), storage = new MemoryStorage({scope: raw}), store = safeBrowserGroups(() => storage);
  const value = store.initialize('scope', seed({groups: [{id: BLUE, name: 'Server secret'}], assignments: {}}), [], {recordsValid: false});
  assert.deepEqual(value, {revision: 7, groups: saved.groups, assignments: {old: RED}});
  assert.equal(storage.getItem('scope'), raw);
  assert.equal(storage.writes.length, 0);
});

test('browser initialization rejects incomplete records before validating or writing a new seed', () => {
  const storage = new MemoryStorage(), store = safeBrowserGroups(() => storage);
  assert.throws(() => store.initialize('scope', {malformed: true}, [], {recordsValid: false}),
    error => error.status === 409 && /records are incomplete/.test(error.message));
  assert.equal(storage.getItem('scope'), null);
  assert.equal(storage.writes.length, 0);
});

test('existing browser state is authoritative, reconciled atomically, and never adopts a new seed', () => {
  const saved = {version: 1, revision: 7, groups: [{id: RED, name: 'Private'}], assignments: {[CHILD]: RED}, receipts: []};
  const storage = new MemoryStorage({scope: JSON.stringify(saved)}), store = new BrowserGroups(() => storage);
  const value = store.load('scope', seed({groups: [{id: BLUE, name: 'Server secret'}], assignments: {}}), agents);
  assert.deepEqual(value.groups, [{id: RED, name: 'Private'}]);
  assert.deepEqual(value.assignments, {[ROOT]: RED, [CHILD]: RED});
  assert.equal(value.revision, 8, 'persisted reconciliation advances the concurrency token');
  assert.equal(storage.writes.length, 1, 'family reconciliation is one complete storage replacement');
});

test('corrupt browser state throws without overwrite or fallback adoption', () => {
  const storage = new MemoryStorage({scope: '{not json'}), store = safeBrowserGroups(() => storage);
  assert.throws(() => store.load('scope', seed(), agents), /could not be read/);
  assert.throws(() => store.apply('scope', {action: 'delete', revision: 0, request_id: REQUEST, group_id: RED}, agents, seed()), /could not be read/);
  assert.equal(storage.writes.length, 0);
  assert.equal(storage.getItem('scope'), '{not json');
});

test('browser mutations enforce revision and replay a receipt without writing twice', () => {
  const storage = new MemoryStorage(), store = safeBrowserGroups(() => storage);
  const body = {action: 'assign', revision: 0, request_id: REQUEST, group_id: RED, agent_ids: [ROOT, CHILD]};
  const first = store.apply('scope', body, agents, seed({assignments: {}}));
  const replay = store.apply('scope', body, agents, seed({assignments: {}}));
  assert.equal(first.revision, 1); assert.deepEqual(replay, first); assert.equal(storage.writes.length, 1);
  assert.throws(() => store.apply('scope', {...body, request_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'}, agents, seed()), error => error.status === 409);
});

test('direct child mutations expand to the complete family before winner reconciliation', () => {
  const groups = [{id: RED, name: 'Red'}, {id: BLUE, name: 'Blue'}];
  const assignStorage = new MemoryStorage(), assignStore = safeBrowserGroups(() => assignStorage);
  const assigned = assignStore.apply('scope', {action: 'assign', revision: 0, request_id: REQUEST, group_id: RED, agent_ids: [CHILD]},
    agents, seed({groups, assignments: {[ROOT]: BLUE, [CHILD]: BLUE}}));
  assert.deepEqual(assigned.assignments, {[ROOT]: RED, [CHILD]: RED}, 'an existing root winner cannot defeat a child-selected assignment');

  const createStorage = new MemoryStorage(), createStore = safeBrowserGroups(() => createStorage);
  const created = createStore.apply('scope', {action: 'create', revision: 0, request_id: REQUEST, name: 'New', agent_ids: [CHILD]},
    agents, seed({groups, assignments: {[ROOT]: BLUE}}));
  assert.equal(created.assignments[ROOT], created.operation.group_id);
  assert.equal(created.assignments[CHILD], created.operation.group_id);
});

test('partial agent snapshots reject new mutations but permit exact receipt replay', () => {
  const storage = new MemoryStorage(), store = safeBrowserGroups(() => storage);
  const body = {action: 'assign', revision: 0, request_id: REQUEST, group_id: RED, agent_ids: [CHILD]};
  const applied = store.apply('scope', body, agents, seed({assignments: {}}));
  assert.deepEqual(store.apply('scope', body, [], seed(), {recordsValid: false}), applied);
  assert.throws(() => store.apply('scope', {...body, revision: 1, request_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'}, [], seed(), {recordsValid: false}),
    error => error.status === 409 && /records are incomplete/.test(error.message));
  assert.equal(storage.writes.length, 1);
});

test('assignment and encoded-size caps fail before overwriting browser state', () => {
  const manyAgents = Array.from({length: 10001}, (_, index) => ({id: `agent-${index}`, ...(index ? {parent_id: 'agent-0'} : {})}));
  const saved = {version: 1, revision: 0, groups: [{id: RED, name: 'Red'}], assignments: {}, receipts: []};
  const raw = JSON.stringify(saved), storage = new MemoryStorage({scope: raw}), store = safeBrowserGroups(() => storage);
  assert.throws(() => store.apply('scope', {action: 'assign', revision: 0, request_id: REQUEST, group_id: RED, agent_ids: ['agent-1']}, manyAgents, seed()), /assignments exceed/);
  assert.equal(storage.getItem('scope'), raw); assert.equal(storage.writes.length, 0);

  const hugeId = 'x'.repeat(1024 * 1024);
  const hugeSeed = seed({assignments: {[hugeId]: RED}});
  assert.throws(() => store.apply('new-scope', {action: 'rename', revision: 0, request_id: REQUEST, group_id: RED, name: 'Warm'}, [{id: hugeId}], hugeSeed), /1 MiB storage limit/);
  assert.equal(storage.getItem('new-scope'), null);
  assert.throws(() => store.initialize('initial-scope', hugeSeed, [{id: hugeId}]), /1 MiB storage limit/);
  assert.equal(storage.getItem('initial-scope'), null, 'initialization encodes and checks bounds before its only write');
});

test('locked browser writes serialize concurrent revisions by scoped key', async () => {
  const storage = new MemoryStorage(), locks = new FakeLockManager();
  const store = new BrowserGroups({storage: () => storage, locks});
  await store.initializeLocked('scope', seed(), agents);
  storage.writes.length = 0;
  const first = {action: 'rename', revision: 0, request_id: REQUEST, group_id: RED, name: 'Warm'};
  const second = {...first, request_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', name: 'Hot'};
  const results = await Promise.allSettled([
    store.applyLocked('scope', first, agents, seed()),
    store.applyLocked('scope', second, agents, seed()),
  ]);
  assert.equal(results.filter(result => result.status === 'fulfilled').length, 1);
  const rejected = results.find(result => result.status === 'rejected');
  assert.equal(rejected.reason.status, 409);
  assert.equal(storage.writes.length, 1);
  assert.equal(locks.requests.every(request => request.name === 'swarm-browser-groups:scope' && request.options.mode === 'exclusive'), true);
});

test('read-only browser load reconciles its projection without storage writes', () => {
  const saved = {version: 1, revision: 7, groups: [{id: RED, name: 'Private'}], assignments: {[CHILD]: RED}, receipts: []};
  const storage = new MemoryStorage({scope: JSON.stringify(saved)}), store = new BrowserGroups(() => storage);
  const value = store.load('scope', seed(), agents, {readOnly: true});
  assert.deepEqual(value.assignments, {[ROOT]: RED, [CHILD]: RED});
  assert.equal(value.revision, 7, 'an uncommitted reconciliation keeps the persisted revision');
  assert.equal(storage.writes.length, 0);
  assert.deepEqual(store.readSnapshot('scope', seed(), agents), value);
  assert.equal(storage.writes.length, 0);
});

test('locked writes clearly reject without Web Locks and do not write', async () => {
  const storage = new MemoryStorage(), store = new BrowserGroups({storage: () => storage, locks: {}});
  await assert.rejects(store.initializeLocked('scope', seed(), agents), /Browser-local grouping needs Web Locks/);
  assert.equal(storage.writes.length, 0);
  assert.deepEqual(store.readSnapshot('scope', seed(), agents).groups, seed().groups, 'read-only UI state remains available');
});

test('browser names, group UUIDs, known agents, and records-valid pruning are guarded', () => {
  const storage = new MemoryStorage(), store = safeBrowserGroups(() => storage);
  assert.throws(() => store.load('scope', {revision: 0, groups: [{id: 'legacy', name: 'No'}], assignments: {}}, agents), /Invalid/);
  assert.throws(() => store.apply('scope', {action: 'assign', revision: 0, request_id: REQUEST, group_id: RED, agent_ids: ['unknown']}, agents, seed()), /known agents/);
  const existing = {version: 1, ...seed({assignments: {[ROOT]: RED, old: RED}}), receipts: []};
  storage.values.set('keep', JSON.stringify(existing));
  assert.equal(store.load('keep', seed(), agents, {recordsValid: false}).assignments.old, RED);
  assert.equal(storage.writes.length, 0);
});

test('validSettings is exported for parent validation', () => {
  assert.equal(validSettings(settings()), true);
  assert.equal(validSettings(settings({grouped_branches: [ROOT, ROOT]})), false);
});

test('panel paints unsupported/error safely and preserves a dirty choice across equal polling', () => {
  const t = controllerFixture();
  assert.equal(t.nodes['#grouping-server-managed'].checked, false);
  assert.match(t.nodes['#grouping-mode-owner'].textContent, /Owning server: Local/);
  t.nodes['#grouping-server-managed'].checked = true; t.nodes['#grouping-server-managed'].dispatch('change');
  t.controller.setState(settings());
  assert.equal(t.nodes['#grouping-server-managed'].checked, true, 'same authoritative settings do not erase a live choice');
  t.controller.setState(settings({revision: 4}));
  assert.equal(t.nodes['#grouping-server-managed'].checked, false, 'a changed revision rebases the choice');
  t.controller.setState(undefined);
  assert.equal(t.nodes['#grouping-mode-change'].disabled, true); assert.match(t.nodes['#grouping-mode-status'].textContent, /not supported/);
  t.controller.setState({revision: 'bad'}, Error('<offline>'));
  assert.equal(t.nodes['#grouping-mode-status'].textContent.includes('<offline>'), true);
  assert.equal(t.nodes['#grouping-mode-status'].children.length, 0, 'status uses text, not markup');
});

test('first enable freezes current UI seed and offers keep, seed, and cancel', async () => {
  const keep = controllerFixture(); keep.nodes['#grouping-server-managed'].checked = true; keep.nodes['#settings-dialog'].open = true;
  const pendingKeep = keep.controller.changeMode(); await tick();
  assert.equal(keep.nodes['#notifications-dialog'].open, false); assert.equal(keep.nodes['#settings-dialog'].open, false);
  assert.equal(keep.nodes['#grouping-prime-dialog'].open, true);
  assert.match(keep.nodes['#grouping-prime-title'].textContent, /Start server-managed grouping/);
  assert.match(keep.nodes['#grouping-prime-summary'].textContent, /agent assignment/);
  assert.match(keep.nodes['#grouping-prime-current'].textContent, /replace owning server/i);
  keep.nodes['#grouping-prime-dialog'].dispatch('close');
  assert.equal(keep.nodes['#grouping-prime-dialog'].open, true, 'a delayed close event cannot cancel a newer open prompt');
  keep.nodes['#grouping-prime-server'].click(); assert.equal(keep.nodes['#settings-dialog'].open, true, 'choice returns to Settings');
  assert.equal(await pendingKeep, true);
  const keepPost = keep.calls.find(call => call.method === 'POST'); assert.equal(keepPost.body.seed, undefined);

  const prime = controllerFixture(); prime.nodes['#grouping-server-managed'].checked = true;
  const pendingPrime = prime.controller.changeMode(); await tick(); prime.nodes['#grouping-prime-current'].click(); await pendingPrime;
  assert.deepEqual(prime.calls.find(call => call.method === 'POST').body.seed, {groups: seed().groups, assignments: seed().assignments, grouped_branches: [ROOT]});

  const cancel = controllerFixture(); cancel.nodes['#grouping-server-managed'].checked = true; cancel.nodes['#settings-dialog'].open = true;
  const pendingCancel = cancel.controller.changeMode(); await tick(); cancel.nodes['#grouping-prime-dialog'].dispatch('cancel');
  assert.equal(cancel.nodes['#settings-dialog'].open, true, 'Escape returns to the current Settings dialog');
  assert.equal(await pendingCancel, false); assert.equal(cancel.calls.some(call => call.method === 'POST'), false);
});

test('priming collapses relay details before closing Settings and restoration leaves polling stopped', async () => {
  const t = controllerFixture(); const relay = t.nodes['#relay-settings-section'];
  let relayGets = 0, key = 'draft secret';
  relay.addEventListener('toggle', () => { if (relay.open) relayGets += 1; else key = ''; });
  relay.open = true; await tick(); relayGets = 0;
  t.nodes['#settings-dialog'].open = true;
  const originalClose = t.nodes['#settings-dialog'].close.bind(t.nodes['#settings-dialog']);
  t.nodes['#settings-dialog'].close = () => { assert.equal(relay.open, false, 'relay collapses before Settings closes'); originalClose(); };
  t.nodes['#grouping-server-managed'].checked = true;

  const pending = t.controller.changeMode(); await tick();
  assert.equal(relay.open, false); assert.equal(key, '');
  t.nodes['#grouping-prime-server'].click();
  assert.equal(await pending, true); await tick();
  assert.equal(t.nodes['#settings-dialog'].open, true); assert.equal(relayGets, 0, 'restoring Settings does not restart relay polling');

  relay.open = true; await tick();
  assert.equal(relayGets, 1, 'polling can restart only after the user expands relay details');
});

test('closing an initially open Settings during preflight or async seed capture cancels without POST', async () => {
  let resolveGet;
  const get = new Promise(resolve => { resolveGet = resolve; });
  const duringGet = controllerFixture({api: async () => get});
  duringGet.nodes['#settings-dialog'].open = true; duringGet.nodes['#grouping-server-managed'].checked = true;
  const getPending = duringGet.controller.changeMode(); await tick();
  duringGet.nodes['#settings-dialog'].close(); resolveGet(settings());
  assert.equal(await getPending, false); assert.equal(duringGet.controller.busy, false);
  assert.equal(duringGet.nodes['#grouping-prime-dialog'].open, false);
  assert.equal(duringGet.calls.some(call => call.method === 'POST'), false);

  let resolveSeed;
  const seedPending = new Promise(resolve => { resolveSeed = resolve; });
  const duringSeed = controllerFixture({primeSnapshot: async () => seedPending});
  duringSeed.nodes['#settings-dialog'].open = true; duringSeed.nodes['#grouping-server-managed'].checked = true;
  const pending = duringSeed.controller.changeMode(); await tick();
  duringSeed.nodes['#settings-dialog'].close();
  resolveSeed({seed: {groups: seed().groups, assignments: seed().assignments, grouped_branches: [ROOT]}});
  assert.equal(await pending, false); assert.equal(duringSeed.controller.busy, false);
  assert.equal(duringSeed.nodes['#grouping-prime-dialog'].open, false);
  assert.equal(duringSeed.calls.some(call => call.method === 'POST'), false);
});

test('failure to restore Settings resolves the prompt as cancel and clears busy state', async () => {
  const t = controllerFixture(); t.nodes['#settings-dialog'].open = true;
  t.nodes['#settings-dialog'].showModal = () => { throw Error('modal restore failed'); };
  t.nodes['#grouping-server-managed'].checked = true;
  const pending = t.controller.changeMode(); await tick();
  t.nodes['#grouping-prime-current'].click();
  assert.equal(await pending, false); assert.equal(t.controller.busy, false);
  assert.equal(t.calls.some(call => call.method === 'POST'), false);
  assert.match(t.errors[0], /Cannot restore Settings.*modal restore failed/i);
});

test('blocked browser snapshot disables seed but still allows keeping server grouping', async () => {
  const t = controllerFixture({primeSnapshot: async () => { throw Error('Browser storage is unavailable'); }});
  t.nodes['#grouping-server-managed'].checked = true;
  const pending = t.controller.changeMode(); await tick();
  assert.equal(t.nodes['#grouping-prime-dialog'].open, true);
  assert.equal(t.nodes['#grouping-prime-current'].disabled, true);
  assert.match(t.nodes['#grouping-prime-summary'].textContent, /unavailable.*still keep/is);
  t.nodes['#grouping-prime-server'].click();
  assert.equal(await pending, true);
  assert.equal(t.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(t.calls.find(call => call.method === 'POST').body.seed, undefined);
});

test('changed browser snapshot after prompt cancels seed POST and requires review', async () => {
  let revision = 4;
  const t = controllerFixture({primeSnapshot: async () => ({
    seed: {groups: seed().groups, assignments: seed().assignments, grouped_branches: [ROOT]}, revision: revision++,
  })});
  t.nodes['#grouping-server-managed'].checked = true;
  const pending = t.controller.changeMode(); await tick();
  t.nodes['#grouping-prime-current'].click();
  assert.equal(await pending, false);
  assert.equal(t.calls.some(call => call.method === 'POST'), false);
  assert.match(t.errors[0], /Source grouping changed.*review it again/i);
  assert.equal(t.changed.length, 1);
  assert.equal(t.changed[0][0], null);
  assert.equal(t.changed[0][1].refreshBrowser, true);
});

test('initialized re-enable has no seed dialog and disable explains preserved canonical state', async () => {
  const enabledBefore = settings({server_managed: false, initialized: true});
  const reenable = controllerFixture(); reenable.setCurrent(enabledBefore); reenable.controller.setState(enabledBefore);
  reenable.nodes['#grouping-server-managed'].checked = true; await reenable.controller.changeMode();
  assert.equal(reenable.nodes['#grouping-prime-dialog'].open, false); assert.equal(reenable.confirms.length, 1);
  assert.equal(reenable.calls.find(call => call.method === 'POST').body.seed, undefined);

  const disable = controllerFixture(); const on = settings({server_managed: true, initialized: true}); disable.setCurrent(on); disable.controller.setState(on);
  disable.nodes['#grouping-server-managed'].checked = false; await disable.controller.changeMode();
  assert.match(disable.confirms[0], /browser-local.*canonical server grouping is preserved.*does not stop/is);
});

test('fresh revision conflict refreshes and refuses overwrite before prompting', async () => {
  const t = controllerFixture({api: async (path, method) => {
    if (method === 'POST') throw Error('must not post');
    return path.endsWith('/groups') ? seed() : settings({revision: 4});
  }});
  t.nodes['#grouping-server-managed'].checked = true;
  assert.equal(await t.controller.changeMode(), false);
  assert.equal(t.calls.some(call => call.method === 'POST'), false);
  assert.equal(t.changed.length, 1); assert.equal(t.changed[0][0].revision, 4); assert.equal(t.changed[0][1].refresh, true);
  assert.match(t.errors[0], /changed/);
});

test('authoritative change closes a live prompt without leaving busy state or posting', async () => {
  const t = controllerFixture(); t.nodes['#grouping-server-managed'].checked = true; t.nodes['#settings-dialog'].open = true;
  const pending = t.controller.changeMode(); await tick();
  assert.equal(t.nodes['#grouping-prime-dialog'].open, true);
  t.controller.setState(settings({revision: 4}));
  assert.equal(await pending, false);
  assert.equal(t.nodes['#grouping-prime-dialog'].open, false);
  assert.equal(t.nodes['#settings-dialog'].open, false, 'authoritative invalidation does not reopen stale Settings');
  assert.equal(t.controller.busy, false);
  assert.match(t.nodes['#grouping-mode-status'].textContent, /changed while the dialog was open/i);
  assert.equal(t.calls.some(call => call.method === 'POST'), false);
});

test('remote owner uses an explicit captured proxy path and stale prompt cannot post or alter new scope', async () => {
  const t = controllerFixture(); t.source('alpha/beta'); t.generation(2); t.controller.setState(settings());
  t.nodes['#settings-dialog'].open = true;
  t.nodes['#grouping-server-managed'].checked = true; const pending = t.controller.changeMode(); await tick();
  assert.equal(t.calls[0].path, '/api/federation/alpha%2Fbeta/proxy/api/grouping-settings');
  assert.match(t.nodes['#grouping-prime-owner'].textContent, /Remote alpha\/beta/);
  t.source('other'); t.generation(3); t.controller.setState(settings());
  assert.equal(await pending, false); assert.equal(t.nodes['#grouping-prime-dialog'].open, false);
  assert.equal(t.nodes['#settings-dialog'].open, false, 'scope changes do not reopen the previous scope Settings');
  assert.equal(t.calls.some(call => call.method === 'POST'), false); assert.deepEqual(t.errors, []);
});

test('settings GET is bounded by the request timeout and never posts after timeout', async () => {
  let timeout;
  const t = controllerFixture({api: async () => new Promise(() => {}), requestTimeout: 20,
    schedule: callback => { timeout = callback; return 1; }, cancel: () => {}});
  t.nodes['#grouping-server-managed'].checked = true;
  const pending = t.controller.changeMode(); await tick(); timeout();
  assert.equal(await pending, false);
  assert.equal(t.calls.some(call => call.method === 'POST'), false);
  assert.match(t.errors[0], /Cannot load grouping settings.*timed out/is);
});

test('authoritative websocket state during POST does not discard its HTTP receipt', async () => {
  let resolvePost;
  const before = settings({initialized: true});
  const after = settings({revision: 4, server_managed: true, initialized: true});
  const t = controllerFixture({api: async (path, method) => method === 'POST'
    ? new Promise(resolve => { resolvePost = resolve; }) : before});
  t.setCurrent(before); t.controller.setState(before);
  t.nodes['#grouping-server-managed'].checked = true;
  const pending = t.controller.changeMode();
  while (!resolvePost) await tick();
  t.controller.setState(after);
  assert.equal(t.controller.busy, true, 'the in-flight mutation remains owned until its receipt arrives');
  resolvePost(after);
  assert.equal(await pending, true);
  assert.equal(t.changed.length, 1);
  assert.equal(t.changed[0][0].revision, 4);
  assert.equal(t.changed[0][1].operation, 'configure');
});

test('lost POST times out once and reports uncertain outcome without blind retry', async () => {
  let timeout;
  const t = controllerFixture({api: async (path, method) => method === 'POST' ? new Promise(() => {}) : settings({initialized: true}),
    requestTimeout: 20, schedule: callback => { timeout = callback; return 1; }, cancel: () => {}});
  t.setCurrent(settings({initialized: true})); t.controller.setState(settings({initialized: true}));
  t.nodes['#grouping-server-managed'].checked = true; const pending = t.controller.changeMode(); await tick(); timeout();
  assert.equal(await pending, false); assert.equal(t.calls.filter(call => call.method === 'POST').length, 1);
  assert.match(t.errors[0], /uncertain.*Do not retry blindly.*timed out/is);
});
