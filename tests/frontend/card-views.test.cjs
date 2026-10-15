const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {CardViews, defaultCompact, applyCardView} = require('../../src/swarm/static/card-views.js');
const home = '/registry/one';
const agent = (status = 'running', alive = true) => ({id: 'agent-1', status, alive});
function memoryStorage() {
  const values = new Map();
  return {values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
}

test('automatic view follows activity and actual process liveness', () => {
  for (const status of ['running', 'waiting']) {
    assert.equal(defaultCompact(agent(status, true)), false);
    assert.equal(defaultCompact(agent(status, false)), true);
  }
  for (const status of ['idle', 'dormant', 'done']) {
    for (const alive of [true, false]) assert.equal(defaultCompact(agent(status, alive)), true);
  }
  const views = new CardViews(() => memoryStorage());
  assert.equal(views.isCompact(home, agent('running')), false);
  assert.equal(views.isCompact(home, agent('done')), true);
  assert.equal(views.isCompact(home, agent('waiting')), false);
});

test('explicit expanded=false and compact=true preferences survive updates and reloads', () => {
  const storage = memoryStorage();
  const views = new CardViews(() => storage);
  views.setCompact(home, 'agent-1', false);
  assert.equal(views.isCompact(home, agent('done', false)), false);
  assert.equal(new CardViews(() => storage).isCompact(home, agent('dormant')), false);
  views.setCompact(home, 'agent-1', true);
  assert.equal(views.isCompact(home, agent('running')), true);
  assert.equal(new CardViews(() => storage).isCompact(home, agent('waiting')), true);
  assert.equal(views.isCompact('/registry/two', agent('running')), false);
});

test('automatic defaults are not persisted as manual overrides', () => {
  const storage = memoryStorage(), views = new CardViews(() => storage);
  views.isCompact(home, agent('done'));
  views.isCompact(home, agent('running'));
  assert.equal(storage.values.size, 0);
});

test('blocked reads, malformed storage and denied writes do not break controls', () => {
  for (const saved of ['not JSON', 'null', '[]', 'true', '{"agent-1":"false"}']) {
    const views = new CardViews(() => ({getItem: () => saved, setItem: () => { throw Error('quota'); }}));
    assert.equal(views.isCompact(home, agent('done')), true);
    views.setCompact(home, 'agent-1', false);
    assert.equal(views.isCompact(home, agent('done')), false);
  }
  const blocked = new CardViews(() => { throw Error('storage blocked'); });
  assert.equal(blocked.isCompact(home, agent('running')), false);
  blocked.setCompact(home, 'agent-1', true);
  assert.equal(blocked.isCompact(home, agent('running')), true);
  blocked.forget(home, 'agent-1');
  assert.equal(blocked.isCompact(home, agent('running')), false);
  assert.throws(() => blocked.setCompact(home, 'agent-1', 'false'), TypeError);
});

test('forget removes only the selected card override', () => {
  const storage = memoryStorage(), views = new CardViews(() => storage);
  views.setCompact(home, 'agent-1', false); views.setCompact(home, 'agent-2', true);
  views.forget(home, 'agent-1');
  const restored = new CardViews(() => storage);
  assert.equal(restored.isCompact(home, agent('done')), true);
  assert.equal(restored.isCompact(home, {id: 'agent-2', status: 'running', alive: true}), true);
});

// Minimal DOM contract for the actual renderer. Browser layout/keyboard behavior
// still needs a browser; these tests exercise rendered structure and click handlers.
class Text {
  constructor(value) { this.textContent = String(value); }
}
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.childNodes = []; this.attributes = new Map(); this.dataset = {}; this.style = {}; this.hidden = false; this.events = new Map(); this.className = ''; }
  get classList() {
    return {
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, on) => { const names = new Set(this.className.split(/\s+/).filter(Boolean)); on ? names.add(name) : names.delete(name); this.className = [...names].join(' '); },
    };
  }
  set textContent(value) { this.childNodes = value ? [new Text(value)] : []; }
  get textContent() { return this.childNodes.map(child => child.textContent).join(''); }
  append(...items) { this.childNodes.push(...items.map(item => typeof item === 'string' ? new Text(item) : item)); }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  addEventListener(type, callback) { this.events.set(type, callback); }
  click() { if (!this.disabled) this.events.get('click')?.(); }
}
function walk(root) { return root instanceof Element ? [root, ...root.childNodes.flatMap(walk)] : []; }
function find(root, cls) { return walk(root).find(el => el.classList.contains(cls)); }
function visibleText(root) { return root.hidden ? '' : root instanceof Element ? root.childNodes.map(visibleText).join(' ') : root.textContent; }
function renderer() {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const storage = memoryStorage(), calls = [];
  const context = vm.createContext({
    document: {createElement: tag => new Element(tag), createTextNode: value => new Text(value)},
    state: {home}, cardViews: new CardViews(() => storage), SwarmCardViews: {applyCardView},
    metaCompactingId: null, toolInputCompactingId: null,
    labels: {running: 'Running', waiting: 'Waiting for input', idle: 'Idle', dormant: 'Dormant', done: 'Done'},
    since: () => 'just now', openTerminal: id => calls.push(['terminal', id]), editAgent: id => calls.push(['edit', id]),
    wakeAgent: id => calls.push(['wake', id]), stopAgent: id => calls.push(['stop', id]),
  });
  vm.runInContext(source.slice(source.indexOf('const $ ='), source.indexOf('const labels =')) + '\n' + source.slice(source.indexOf('function badge('), source.indexOf('\nfunction render()')), context);
  return {render: a => context.renderCard(a), context, calls, storage};
}
function fullAgent(status = 'running', alive = true) {
  return {...agent(status, alive), name: 'Researcher', backend: 'pi', goal: 'Track the PR', activity: 'Checking CI',
    activities: [{title: 'Review tests', status: 'running'}], folder: '/workspace', repository: '/repo', worktree: '/tree', branch: 'main',
    session_id: 'session-1', session_file: '/sessions/one.jsonl', pid: alive ? 99 : null,
    updated_at: '2026-01-01T00:00:00Z', auto_resume: true, managed: true, notes: 'Next step', last_error: 'Example error', wait_reason: 'Example reason'};
}

test('compact renderer exposes only summary and toggle, expanded form restores all controls', () => {
  const {render, calls} = renderer();
  const card = render(fullAgent('done'));
  const toggle = find(card, 'card-toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.match(toggle.getAttribute('aria-label'), /Expand card for Researcher/);
  assert.match(toggle.getAttribute('aria-controls'), /card-details-agent-1 card-actions-agent-1/);
  assert.match(visibleText(card), /Researcher/); assert.match(visibleText(card), /Track the PR/); assert.match(visibleText(card), /Done/);
  assert.doesNotMatch(visibleText(card), /Checking CI|\/workspace|Checkpoint|Terminal|Example error|Example reason/);
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.match(visibleText(card), /Checking CI/); assert.match(visibleText(card), /\/workspace/);
  for (const text of ['Terminal ↗', 'Edit', 'Wake', 'Stop']) {
    const action = walk(card).find(el => el.tagName === 'BUTTON' && el.textContent === text);
    assert.ok(action, text); action.click();
  }
  assert.deepEqual(calls, [['terminal', 'agent-1'], ['edit', 'agent-1'], ['wake', 'agent-1'], ['stop', 'agent-1']]);
  toggle.click(); assert.equal(find(card, 'card-details').hidden, true); assert.equal(find(card, 'card-foot').hidden, true);
});

test('collapsed name highlight follows process liveness, not task status', () => {
  const {render, context} = renderer();
  context.cardViews.setCompact(home, 'agent-1', true);
  for (const status of ['running', 'waiting', 'idle', 'dormant', 'done']) {
    const card = render(fullAgent(status, true));
    assert.equal(card.classList.contains('is-compact'), true);
    assert.equal(card.classList.contains('has-process'), true, status);
    find(card, 'card-toggle').click();
    assert.equal(card.classList.contains('is-compact'), false);
    find(card, 'card-toggle').click();
    assert.equal(card.classList.contains('is-compact'), true);
    const stopped = render(fullAgent(status, false));
    assert.equal(stopped.classList.contains('has-process'), false, status);
  }
  const css = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.css'), 'utf8');
  assert.match(css, /\.agent-card\.is-compact\.has-process \.card-identity h3\{color:#ff7b7b\}/);
});

test('running meta-compaction disables card Wake without hiding Edit or status access', () => {
  const {render, calls} = renderer();
  const a = fullAgent('idle', false); a.auto_resume = false; a.meta_compaction = {status: 'running', stage: 'summarizing'};
  const card = render(a); find(card, 'card-toggle').click();
  const wake = walk(card).find(el => el.tagName === 'BUTTON' && el.textContent === 'Wake');
  const edit = walk(card).find(el => el.tagName === 'BUTTON' && el.textContent === 'Edit');
  assert.equal(wake.disabled, true); assert.match(wake.title, /Meta-compaction/);
  edit.click(); wake.click(); assert.deepEqual(calls, [['edit', 'agent-1']]);
});

test('meta-compaction Edit UI exposes bounded inputs and safe persisted output fields', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  assert.match(html, /id="meta-compact-toggle"[^>]*>Meta-compact…/);
  assert.match(html, /name="overlap_tokens"[^>]*value="10000"[^>]*min="0"[^>]*max="100000"/);
  assert.match(html, /name="chunk_count"[^>]*value="2"[^>]*min="2"[^>]*max="16"/);
  for (const id of ['meta-compact-stage', 'meta-compact-error', 'meta-compact-archive', 'meta-compact-directory']) assert.ok(html.includes(`id="${id}"`));
  assert.match(source, /\/meta-compact`, 'POST', \{model: model \|\| null, overlap_tokens: overlapTokens, chunk_count: chunkCount\}/);
  assert.match(source, /\$\{chunkCount \+ 1\} Pi stages/); assert.match(source, /Summaries are lossy/);
  assert.match(source, /form\.elements\.chunk_count\.value = '2'/);
  assert.match(source, /chunkCount < 2 \|\| chunkCount > 16/);
  assert.match(source, /After success, Swarm archives and replaces/); assert.match(source, /stays stopped until you explicitly Wake/);
  assert.match(source, /\$\('#meta-compact-error'\)\.textContent = job\.error \|\| ''/);
  assert.match(source, /a\.managed === true && a\.backend === 'pi' && !!a\.session_file && !a\.alive/);
});

test('tool input compaction has a separate safe report UI and blocks Wake while running', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  assert.match(html, /id="compact-tool-inputs"[^>]*>Compact tool inputs</);
  for (const id of ['tool-input-compact-count', 'tool-input-compact-savings', 'tool-input-compact-error', 'tool-input-compact-archive', 'tool-input-compact-directory']) assert.ok(html.includes(`id="${id}"`));
  assert.match(source, /\/compact-tool-inputs`, 'POST', \{\}/);
  assert.match(source, /toolJob\.calls_compacted/); assert.match(source, /toolJob\.estimated_tokens_saved/);
  assert.match(source, /\$\('#tool-input-compact-error'\)\.textContent = toolJob\.error \|\| ''/);
  assert.match(source, /\$\('#tool-input-compact-archive'\)\.textContent = toolJob\.archive \|\| ''/);
  assert.match(source, /\$\('#meta-compact-toggle'\)\.disabled = !eligible \|\| blocked/);
  assert.match(source, /\$\('#save-agent'\)\.disabled = blocked/);
  assert.match(source, /const compacting = compactionBlocked\(a\)/);
  const {render, calls} = renderer();
  const a = fullAgent('idle', false); a.tool_input_compaction = {status: 'running'};
  const card = render(a); find(card, 'card-toggle').click();
  const wake = walk(card).find(el => el.tagName === 'BUTTON' && el.textContent === 'Wake');
  assert.equal(wake.disabled, true); assert.match(wake.title, /Tool input compaction/);
  wake.click(); assert.deepEqual(calls, []);
});

function toolInputDialog({confirmResult = true, reject = false} = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const calls = [], errors = new Element('p'), a = fullAgent('idle', false);
  const form = {querySelector: selector => selector === '.form-error' ? errors : null};
  const dialog = {open: true};
  const context = vm.createContext({
    editId: a.id, metaCompactingId: null, toolInputCompactingId: null, state: {agents: [a]},
    agentById: id => context.state.agents.find(record => record.id === id),
    compactionRunning: record => record?.meta_compaction?.status === 'running' || record?.tool_input_compaction?.status === 'running',
    $: (selector, root) => root ? root.querySelector(selector) : selector === '#edit-form' ? form : selector === '#edit-dialog' ? dialog : null,
    confirm: text => { calls.push(['confirm', text]); return confirmResult; },
    api: async (url, method, body) => { calls.push(['api', url, method, body]); if (reject) throw Error('<server failure>'); return {status: 'completed', calls_compacted: 7, estimated_tokens_saved: 321, archive: '/archive/original.jsonl'}; },
    updateEditAvailability: () => calls.push(['availability']), render: () => calls.push(['render']),
    refresh: async () => calls.push(['refresh']), toast: (text, error) => calls.push(['toast', text, error]),
  });
  const start = source.indexOf('async function compactToolInputs()');
  const end = source.indexOf('\nasync function forgetAgent()', start);
  const predicateStart = source.indexOf('function canRevertToolInputs(');
  const predicateEnd = source.indexOf('\nfunction renderCard(', predicateStart);
  vm.runInContext(source.slice(predicateStart, predicateEnd) + '\n' + source.slice(start, end), context);
  return {context, calls, agent: a, errors};
}

test('confirmed tool input compaction posts an empty object and retains its report', async () => {
  const t = toolInputDialog(); await t.context.compactToolInputs();
  const confirmation = t.calls.find(call => call[0] === 'confirm')[1];
  for (const phrase of ['No model calls', '100-character previews', 'toolrun IDs', 'output contents are preserved', 'inert history', 'original session is archived', 'active branch is retained', 'stays stopped']) assert.ok(confirmation.includes(phrase), phrase);
  const request = t.calls.find(call => call[0] === 'api');
  assert.equal(request[1], '/api/agents/agent-1/compact-tool-inputs'); assert.equal(request[2], 'POST'); assert.equal(Object.keys(request[3]).length, 0);
  assert.equal(t.agent.tool_input_compaction.calls_compacted, 7);
  assert.ok(t.calls.some(call => call[0] === 'toast' && /7 tool inputs/.test(call[1])));
  assert.equal(t.context.toolInputCompactingId, null);
});

test('canceling tool input compaction has no API effects', async () => {
  const t = toolInputDialog({confirmResult: false}); await t.context.compactToolInputs();
  assert.equal(t.calls.filter(call => call[0] === 'api').length, 0);
  assert.equal(t.agent.tool_input_compaction, undefined);
});

test('tool input compaction errors remain text and release the in-flight lock', async () => {
  const t = toolInputDialog({reject: true}); await t.context.compactToolInputs();
  assert.equal(t.errors.textContent, '<server failure>');
  assert.equal(t.errors.childNodes[0] instanceof Text, true);
  assert.equal(t.context.toolInputCompactingId, null);
  assert.equal(t.agent.tool_input_compaction, undefined);
});

function withRevertArchive(t) {
  t.agent.tool_input_compaction = {status: 'completed', archive: '/archive/original.jsonl', source_sha256: 'original', replacement_sha256: 'compacted'};
  return t;
}

test('revert availability requires retained archive metadata and allows no-op repeats', () => {
  const t = withRevertArchive(toolInputDialog());
  assert.equal(t.context.canRevertToolInputs(t.agent), true);
  t.agent.tool_input_compaction.status = 'unchanged';
  assert.equal(t.context.canRevertToolInputs(t.agent), true);
  t.agent.tool_input_compaction.status = 'reverted';
  assert.equal(t.context.canRevertToolInputs(t.agent), false);
  t.agent.tool_input_compaction.status = 'failed'; t.agent.tool_input_compaction.operation = 'revert';
  assert.equal(t.context.canRevertToolInputs(t.agent), true);
  delete t.agent.tool_input_compaction.archive;
  assert.equal(t.context.canRevertToolInputs(t.agent), false);
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  assert.match(html, /id="revert-tool-input-compaction"[^>]*disabled>Revert tool-input compaction/);
  assert.match(source, /revert-tool-input-compaction'\)\.disabled = !eligible \|\| blocked \|\| !canRevertToolInputs\(a\)/);
  assert.match(source, /tool-input-revert-backup'\)\.textContent = toolJob\.compacted_backup \|\| ''/);
});

test('confirmed revert uses dedicated endpoint and retains restored status', async () => {
  const t = withRevertArchive(toolInputDialog());
  t.context.api = async (url, method, body) => {
    t.calls.push(['api', url, method, body]);
    assert.equal(t.context.toolInputCompactingId, t.agent.id);
    return {...t.agent.tool_input_compaction, status: 'reverted', compacted_backup: '/archive/compacted.jsonl'};
  };
  await t.context.revertToolInputs();
  const confirmation = t.calls.find(call => call[0] === 'confirm')[1];
  for (const phrase of ['backed up first', 'newer history', 'No model calls', 'stays stopped']) assert.ok(confirmation.includes(phrase), phrase);
  const request = t.calls.find(call => call[0] === 'api');
  assert.equal(request[1], '/api/agents/agent-1/revert-tool-input-compaction');
  assert.equal(request[2], 'POST'); assert.equal(Object.keys(request[3]).length, 0);
  assert.equal(t.agent.tool_input_compaction.status, 'reverted');
  assert.equal(t.context.toolInputCompactingId, null);
});

test('cancelled, live, busy and unavailable reverts make no API calls', async () => {
  const cases = [withRevertArchive(toolInputDialog({confirmResult: false})), withRevertArchive(toolInputDialog()), withRevertArchive(toolInputDialog()), toolInputDialog()];
  cases[1].agent.alive = true;
  cases[2].agent.meta_compaction = {status: 'running'};
  for (const t of cases) {
    await t.context.revertToolInputs();
    assert.equal(t.calls.filter(call => call[0] === 'api').length, 0);
  }
});

test('revert errors are plain text and release compaction interlock', async () => {
  const t = withRevertArchive(toolInputDialog({reject: true}));
  await t.context.revertToolInputs();
  assert.equal(t.errors.textContent, '<server failure>');
  assert.equal(t.errors.childNodes[0] instanceof Text, true);
  assert.equal(t.agent.tool_input_compaction.status, 'completed');
  assert.equal(t.context.toolInputCompactingId, null);
});

test('manual renderer choice survives new state snapshots and fake reload', () => {
  const {render, storage} = renderer();
  const card = render(fullAgent()); find(card, 'card-toggle').click();
  assert.equal(render(fullAgent('waiting')).classList.contains('is-compact'), true);
  assert.equal(new CardViews(() => storage).isCompact(home, fullAgent()), true);
  const inactive = render(fullAgent('done', false)); find(inactive, 'card-toggle').click();
  assert.equal(render(fullAgent('dormant')).classList.contains('is-compact'), false);
});

test('rendered untrusted names/goals remain text and new helper loads before the app', () => {
  const {render} = renderer();
  const a = fullAgent('idle'); a.name = '<script>bad()</script>'; a.goal = '<img src=x onerror=bad()>';
  const card = render(a);
  assert.ok(visibleText(card).includes(a.name)); assert.ok(visibleText(card).includes(a.goal));
  assert.equal(walk(card).some(el => ['SCRIPT', 'IMG'].includes(el.tagName)), false);
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  assert.ok(html.indexOf('src="/static/card-views.js"') < html.indexOf('src="/static/app.js"'));
  const css = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.css'), 'utf8');
  assert.ok(css.includes('.agent-grid{align-items:start}'));
});

function forgetDialog({active = false, confirm = true, reject = false} = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const calls = [], errors = new Element('p'), save = new Element('button');
  const form = {querySelector: selector => selector === '.form-error' ? errors : save};
  const forget = new Element('button'), explanation = new Element('p');
  const dialog = {open: true, close: () => { calls.push(['close']); dialog.open = false; }};
  const controls = {'#forget-agent': forget, '#forget-explanation': explanation, '#edit-form': form, '#edit-dialog': dialog};
  const a = fullAgent(active ? 'running' : 'idle');
  const context = vm.createContext({
    editId: a.id, forgettingAgent: false, state: {home, agents: [a, {...a, id: 'other-agent'}]},
    compactionBlocked: record => record?.meta_compaction?.status === 'running' || record?.tool_input_compaction?.status === 'running',
    $: (selector, root) => root ? root.querySelector(selector) : controls[selector],
    agentById: id => context.state.agents.find(record => record.id === id),
    confirm: text => { calls.push(['confirm', text]); return confirm; },
    api: async (url, method) => { calls.push(['api', url, method]); if (reject) { a.status = 'running'; throw Error('Agent is actively running'); } return {forgotten: true}; },
    cardViews: {forget: (registry, id) => calls.push(['preference', registry, id])},
    closeTerminal: id => calls.push(['detach', id]),
    applyState: next => { context.state = next; },
    toast: text => calls.push(['toast', text]), refresh: async () => calls.push(['refresh']),
  });
  vm.runInContext(source.slice(source.indexOf('function updateForgetAvailability()'), source.indexOf("\n$('#forget-agent').addEventListener")), context);
  return {context, calls, errors, save, forget, explanation, dialog};
}

test('Edit prevents forgetting active workers based on current state', async () => {
  const t = forgetDialog({active: true});
  t.context.updateForgetAvailability();
  assert.equal(t.forget.disabled, true); assert.match(t.explanation.textContent, /actively running/);
  await t.context.forgetAgent();
  assert.equal(t.calls.length, 0);
});

test('Forget confirmation can be canceled without API or process effects', async () => {
  const t = forgetDialog({confirm: false});
  await t.context.forgetAgent();
  assert.equal(t.calls.length, 1); assert.equal(t.calls[0][0], 'confirm');
  assert.ok(t.calls[0][1].includes('/sessions/one.jsonl'));
  assert.equal(t.context.state.agents.length, 2); assert.equal(t.dialog.open, true);
});

test('confirmed Forget removes only the selected card and cleans its tab/preference', async () => {
  const t = forgetDialog();
  t.context.updateForgetAvailability(); assert.equal(t.forget.disabled, false);
  await t.context.forgetAgent();
  assert.ok(t.calls.some(call => call[0] === 'api' && call[1] === '/api/agents/agent-1' && call[2] === 'DELETE'));
  assert.ok(t.calls.some(call => call[0] === 'preference' && call[2] === 'agent-1'));
  assert.ok(t.calls.some(call => call[0] === 'detach' && call[1] === 'agent-1'));
  assert.equal(t.context.state.agents.length, 1); assert.equal(t.context.state.agents[0].id, 'other-agent');
  assert.equal(t.dialog.open, false); assert.equal(t.context.forgettingAgent, false);
  assert.equal(t.save.disabled, false);
});

test('server-side active-state rejection keeps the dialog, card and terminal intact', async () => {
  const t = forgetDialog({reject: true});
  await t.context.forgetAgent();
  assert.match(t.errors.textContent, /actively running/);
  assert.equal(t.dialog.open, true); assert.equal(t.context.state.agents.length, 2);
  assert.equal(t.calls.some(call => ['detach', 'preference', 'close'].includes(call[0])), false);
  assert.equal(t.forget.disabled, true); assert.equal(t.save.disabled, false);
});
