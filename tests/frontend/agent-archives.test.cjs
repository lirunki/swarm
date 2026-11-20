const test = require('node:test');
const assert = require('node:assert/strict');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.events = new Map(); this.dataset = {};
    this._text = ''; this.value = ''; this.checked = false; this.disabled = false; this.hidden = false; this.open = false;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(item => item?.textContent || '').join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ''; }
  addEventListener(type, callback) { this.events.set(type, callback); }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.get('close')?.({target: this}); }
  click() { if (!this.disabled) return this.events.get('click')?.({target: this, currentTarget: this}); }
  change() { return this.events.get('change')?.({target: this, currentTarget: this}); }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  closest() { return this.parentDetails || null; }
}

global.document = {createElement: tag => new Element(tag), querySelector: () => null};
const {AgentArchivesController, sourcePath, validJob} = require('../../src/swarm/static/agent-archives.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const IDS = [
  'archive-agent-choice-dialog', 'archive-agent-name', 'archive-agent-include-files', 'archive-agent-submit',
  'archive-agent-forget-only', 'archive-agent-cancel', 'archived-agents-button', 'archives-dialog', 'archives-owner',
  'archives-list', 'archives-jobs', 'archives-status', 'archives-error', 'archives-refresh', 'archives-close',
  'archives-delete-selected', 'archives-select-all',
];
const AGENT_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const AGENT_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const ARCHIVE_A = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const ARCHIVE_B = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const REQUESTS = [
  '10000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000003', '10000000-0000-4000-8000-000000000004',
];

function agent(id = AGENT_A, name = '<Agent A>') {
  return {id, name, status: 'stopped', alive: false, auto_resume: false, folder: '/work/<private>'};
}
function job(body, target = agent(), patch = {}) {
  return {id: body.request_id, archive_id: target.id === AGENT_A ? ARCHIVE_A : ARCHIVE_B,
    agent_id: target.id, name: target.name, status: 'queued', ...patch};
}
function archive(id = ARCHIVE_A, patch = {}) {
  return {id, name: '<Archived & private>', agent_id: AGENT_A, created_at: '2025-01-01T00:00:00Z',
    archived_at: '2025-01-02T00:00:00Z', last_accessed_at: null, last_resurrected_at: null,
    status: 'archived', goal: '<img src=x onerror=bad()>', folder: '/private', session_id: 'session',
    size: 2048, files_included: false, restorable: true, missing_zip: false, ...patch};
}

function fixture(options = {}) {
  const elements = Object.fromEntries(IDS.map(id => [`#${id}`, new Element(id.includes('dialog') ? 'dialog' : id.includes('include') || id.includes('select-all') ? 'input' : 'div')]));
  let swarm = options.swarm || 'local', generation = 4, requestIndex = 0;
  const calls = [], changes = [], toasts = [], confirms = [], opened = [];
  const api = async (...args) => {
    calls.push(args);
    if (options.api) return options.api(...args);
    if (args[1] === 'POST' && args[0].endsWith('/archive-and-forget')) {
      const selected = args[0].includes(AGENT_B) ? agent(AGENT_B, 'Agent B') : agent();
      return {job: job(args[2], selected)};
    }
    return {archives: options.archives || [], jobs: options.jobs || []};
  };
  const controller = new AgentArchivesController({
    query: selector => elements[selector] || null, api, currentSwarm: () => swarm, currentGeneration: () => generation,
    ownerName: source => `Owner ${source}`, requestId: () => REQUESTS[requestIndex++],
    onChanged: async value => { changes.push(value); await options.onChanged?.(value); },
    toast: (...value) => toasts.push(value), confirm: message => { confirms.push(message); return options.confirm !== false; },
    openAgent: (...value) => opened.push(value), setTimer: options.setTimer, clearTimer: options.clearTimer,
    requestTimeout: options.requestTimeout || 20000,
  });
  return {controller, elements, calls, changes, toasts, confirms, opened,
    switchSwarm(value) { swarm = value; generation += 1; }, setSwarm(value) { swarm = value; },
    get swarm() { return swarm; }, get generation() { return generation; }};
}


test('paths and archive acknowledgements are pinned and strictly correlated', () => {
  assert.equal(sourcePath('local', '/api/archives'), '/api/archives');
  assert.equal(sourcePath('peer/a', '/api/archives'), '/api/federation/peer%2Fa/proxy/api/archives');
  const body = {request_id: REQUESTS[0]}, target = agent();
  assert.equal(validJob(job(body), {requestId: REQUESTS[0], agentId: target.id, name: target.name}), true);
  assert.equal(validJob(job(body, target, {name: 'retargeted'}), {requestId: REQUESTS[0], agentId: target.id, name: target.name}), false);
});


test('choice snapshots agent/context, defaults files off, cancel does nothing, and forget receives capture', async () => {
  const t = fixture(), selected = agent(); let forgotten;
  assert.equal(t.controller.choose(selected, (...args) => { forgotten = args; }), true);
  selected.name = 'mutated';
  assert.equal(t.elements['#archive-agent-include-files'].checked, false);
  assert.equal(t.elements['#archive-agent-name'].textContent, '<Agent A>');
  t.elements['#archive-agent-cancel'].click();
  assert.equal(forgotten, undefined);

  t.controller.choose(agent(), (...args) => { forgotten = args; });
  await t.elements['#archive-agent-forget-only'].click();
  assert.equal(forgotten[0].name, '<Agent A>');
  assert.equal(forgotten[1].source, 'local');
  assert.equal(t.calls.length, 0);
});


test('accepted archive snapshots include_files, closes choice, refreshes without wake, and permits another agent', async () => {
  const releases = [];
  const t = fixture({api: (...args) => new Promise(resolve => releases.push(() => resolve({job: job(args[2],
    args[0].includes(AGENT_B) ? agent(AGENT_B, 'Agent B') : agent())}))) });
  t.controller.choose(agent(), () => {}); t.elements['#archive-agent-include-files'].checked = true;
  const first = t.controller.startChoiceArchive(); await tick();
  assert.deepEqual(t.calls[0][2], {include_files: true, request_id: REQUESTS[0]});
  releases[0](); assert.equal((await first).agent_id, AGENT_A);
  assert.equal(t.elements['#archive-agent-choice-dialog'].open, false);
  assert.deepEqual(t.changes, [{refresh: true, noAutoWake: true}]);
  assert.match(t.toasts[0][0], new RegExp(`<Agent A>.*${REQUESTS[0]}`));

  t.controller.choose(agent(AGENT_B, 'Agent B'), () => {});
  const second = t.controller.startChoiceArchive(); await tick();
  assert.equal(t.calls.length, 2, 'a queued job for A does not block a POST for B');
  releases[1](); await second;
  assert.equal(t.calls.some(call => /wake|terminal/i.test(call[0])), false);
});


test('captured remote route never retargets and stale completion cannot update current UI', async () => {
  let release;
  const t = fixture({swarm: 'peer/a', api: (...args) => new Promise(resolve => { release = () => resolve({job: job(args[2])}); })});
  t.controller.choose(agent(), () => {}); const work = t.controller.startChoiceArchive(); await tick();
  assert.equal(t.calls[0][0], `/api/federation/peer%2Fa/proxy/api/agents/${AGENT_A}/archive-and-forget`);
  t.switchSwarm('peer/b'); release(); await work;
  assert.equal(t.toasts.length, 0, 'old owner does not receive completion UI');
  assert.equal(t.changes.length, 1, 'accepted server mutation still requests a no-wake parent refresh');
});


test('20 second timeout retains request UUID and prevents blind retry while ignoring late response', async () => {
  let timeout, release;
  const t = fixture({api: (...args) => new Promise(resolve => { release = () => resolve({job: job(args[2])}); }),
    setTimer: callback => { timeout = callback; return 9; }, clearTimer: () => {}});
  const first = t.controller.startArchive(agent(), false); await tick(); timeout();
  assert.equal(await first, null);
  assert.match(t.toasts[0][0], new RegExp(`timed out.*uncertain.*${REQUESTS[0]}`, 'i'));
  assert.equal(await t.controller.startArchive(agent(), false), REQUESTS[0]);
  assert.equal(t.calls.length, 1);
  release(); await tick(); assert.equal(t.changes.length, 0);
});


test('open loads a source-pinned listing, renders literal text/download route, and polls only active jobs', async () => {
  const timers = [];
  const active = job({request_id: REQUESTS[0]}, agent(), {archive_id: ARCHIVE_A, status: 'running'});
  const t = fixture({swarm: 'peer/a', archives: [archive()], jobs: [active], setTimer: (callback, delay) => { timers.push({callback, delay}); return timers.length; }, clearTimer: () => {}});
  await t.controller.open();
  assert.equal(t.calls[0][0], '/api/federation/peer%2Fa/proxy/api/archives');
  assert.equal(t.elements['#archives-owner'].textContent, 'Owner peer/a');
  assert.match(t.elements['#archives-list'].textContent, /<Archived & private>.*<img src=x onerror=bad\(\)>/s);
  const row = t.elements['#archives-list'].children[0], link = row.children[2].children[1];
  assert.equal(link.href, `/api/federation/peer%2Fa/proxy/api/archives/${ARCHIVE_A}/download`);
  assert.match(link.title, /secrets.*authenticated/i);
  assert.equal(timers.at(-1).delay, 2000);

  t.controller.jobs = [{...active, status: 'completed'}]; t.controller.schedulePoll();
  assert.equal(t.controller.pollTimer, null);
});


test('selection defaults false, persists across polls, prunes removed IDs, and never auto-selects additions', async () => {
  let listing = {archives: [archive()], jobs: []};
  const t = fixture({api: () => listing}); await t.controller.open();
  assert.equal(t.controller.selection.size, 0);
  const firstCheck = t.elements['#archives-list'].children[0].children[0]; firstCheck.checked = true; firstCheck.change();
  listing = {archives: [archive(), archive(ARCHIVE_B, {name: 'new'})], jobs: []}; await t.controller.refresh();
  assert.deepEqual([...t.controller.selection], [ARCHIVE_A]);
  listing = {archives: [archive(ARCHIVE_B)], jobs: []}; await t.controller.refresh();
  assert.equal(t.controller.selection.size, 0);
});


test('resurrection confirms stopped semantics, validates same identity/no wake, keeps archive, and can restore again when restorable', async () => {
  const item = archive();
  const t = fixture({archives: [item], api: (path, method, body) => {
    if (method === 'POST') return {archive: {...item, last_resurrected_at: '2025-01-03T00:00:00Z'}, agent: {...agent(), alive: false, auto_resume: false}};
    return {archives: [{...item, last_resurrected_at: '2025-01-03T00:00:00Z', restorable: true}], jobs: []};
  }});
  await t.controller.open(); const result = await t.controller.resurrect(item);
  assert.equal(result.agent.id, AGENT_A);
  assert.match(t.confirms[0], /same ID.*working folder is unchanged.*timers remain disabled.*not awakened.*archive remains/is);
  assert.deepEqual(t.changes, [{refresh: true, noAutoWake: true}]);
  assert.equal(t.opened[0][0].alive, false);
  assert.equal(t.calls.some(call => /wake|terminal/i.test(call[0])), false);
  assert.equal(t.controller.archives[0].restorable, true, 'a later forget may make the same retained archive restorable again');
});


test('malformed resurrection is rejected and is not retried automatically', async () => {
  const item = archive();
  const t = fixture({archives: [item], api: (path, method) => method === 'POST'
    ? {archive: item, agent: {...agent(AGENT_B), alive: false, auto_resume: false}}
    : {archives: [item], jobs: []}});
  await t.controller.open(); assert.equal(await t.controller.resurrect(item), null);
  assert.match(t.elements['#archives-error'].textContent, new RegExp(`Invalid resurrection.*${REQUESTS[0]}.*not retried`, 'i'));
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
  assert.equal(t.opened.length, 0);
});


test('batch deletion uses captured UUIDs, stops on first failure, and never deletes active archive jobs', async () => {
  const items = [archive(), archive(ARCHIVE_B, {name: 'Second'})];
  const t = fixture({archives: items, api: (path, method) => {
    if (method === 'DELETE' && path.endsWith(ARCHIVE_A)) return {id: ARCHIVE_A, deleted: true};
    if (method === 'DELETE') { const error = Error('disk busy'); error.status = 500; throw error; }
    return {archives: items, jobs: []};
  }});
  await t.controller.open();
  assert.equal(await t.controller.deleteArchives([ARCHIVE_A, ARCHIVE_B]), false);
  assert.equal(t.calls.filter(call => call[1] === 'DELETE').length, 2);
  assert.match(t.elements['#archives-error'].textContent, /Stopped after 1 of 2.*refresh before retrying/i);
  assert.match(t.confirms[0], /only the archive ZIP.*does not delete any agent.*session.*working folder.*worktree/is);

  t.controller.jobs = [{...job({request_id: REQUESTS[0]}), archive_id: ARCHIVE_B, status: 'running'}];
  assert.equal(await t.controller.deleteArchives([ARCHIVE_B]), false);
  assert.equal(t.calls.filter(call => call[1] === 'DELETE').length, 2);
});


test('closing or switching source stops an in-progress deletion batch before the next DELETE', async () => {
  let release;
  const items = [archive(), archive(ARCHIVE_B)];
  const t = fixture({archives: items, api: (path, method) => method === 'DELETE'
    ? new Promise(resolve => { release = () => resolve({id: ARCHIVE_A, deleted: true}); })
    : {archives: items, jobs: []}});
  await t.controller.open(); const deleting = t.controller.deleteArchives([ARCHIVE_A, ARCHIVE_B]); await tick();
  t.elements['#archives-dialog'].close(); release();
  assert.equal(await deleting, false);
  assert.equal(t.calls.filter(call => call[1] === 'DELETE').length, 1);

  await t.controller.open(); t.switchSwarm('other'); assert.equal(t.controller.sync(), true);
  assert.equal(t.controller.context.source, 'other');
});


test('404 listing reports unsupported without mutation and controller never reads credentials or keys', async () => {
  const error = Error('missing'); error.status = 404;
  const t = fixture({api: () => { throw error; }}); await t.controller.open();
  assert.match(t.elements['#archives-error'].textContent, /unsupported/i);
  assert.equal(t.calls.length, 1);
  const source = require('node:fs').readFileSync(require.resolve('../../src/swarm/static/agent-archives.js'), 'utf8');
  assert.doesNotMatch(source, /localStorage|sessionStorage|Authorization|password|access_ticket/);
});
