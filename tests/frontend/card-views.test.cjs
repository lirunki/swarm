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

test('org chart defaults to compact tiles regardless of state or liveness', () => {
  for (const status of ['running', 'waiting', 'idle', 'dormant', 'done']) {
    for (const alive of [true, false]) assert.equal(defaultCompact(agent(status, alive)), true);
  }
  const views = new CardViews(() => memoryStorage());
  assert.equal(views.isCompact(home, agent('running')), true);
  assert.equal(views.isCompact(home, agent('done')), true);
  assert.equal(views.isCompact(home, agent('waiting')), true);
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
  assert.equal(views.isCompact('/registry/two', agent('running')), true);
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
  assert.equal(blocked.isCompact(home, agent('running')), true);
  blocked.setCompact(home, 'agent-1', true);
  assert.equal(blocked.isCompact(home, agent('running')), true);
  blocked.forget(home, 'agent-1');
  assert.equal(blocked.isCompact(home, agent('running')), true);
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
    document: {createElement: tag => new Element(tag), createTextNode: value => new Text(value)}, URL,
    state: {home, agents: []}, cardViews: new CardViews(() => storage), SwarmCardViews: {applyCardView},
    agentById: id => context.state.agents.find(a => a.id === id),
    metaCompactingId: null, toolInputCompactingId: null,
    labels: {running: 'Running', waiting: 'Waiting for input', idle: 'Idle', dormant: 'Dormant', done: 'Done'},
    since: () => 'just now', apiPath: value => value, openTerminal: id => calls.push(['terminal', id]), editAgent: id => calls.push(['edit', id]),
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

test('compact renderer exposes summary and terminal shortcut, expanded form restores all controls', () => {
  const {render, calls} = renderer();
  const a = fullAgent('done'), card = render(a);
  const toggle = find(card, 'card-toggle'), terminal = find(card, 'card-terminal');
  assert.equal(terminal.textContent, '↗'); assert.equal(terminal.hidden, false);
  assert.equal(terminal.getAttribute('aria-label'), 'Open terminal for Researcher');
  assert.deepEqual(find(card, 'card-controls').childNodes, [toggle, terminal]);
  terminal.click(); assert.deepEqual(calls, [['terminal', a.id]]); calls.length = 0;
  assert.equal(card.classList.contains('is-compact'), true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.match(toggle.getAttribute('aria-label'), /Expand card for Researcher/);
  assert.match(toggle.getAttribute('aria-controls'), /card-details-agent-1 card-actions-agent-1/);
  assert.match(visibleText(card), /Researcher/); assert.match(visibleText(card), /Done/);
  assert.doesNotMatch(visibleText(card), /Track the PR|Checking CI|\/workspace|Checkpoint|Terminal|Example error|Example reason/);
  assert.match(card.title, /Activity: Checking CI/);
  assert.match(find(card, 'card-identity').childNodes[0].title, /Checking CI/);
  assert.equal(toggle.getAttribute('aria-describedby'), 'card-activity-agent-1');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(terminal.hidden, true);
  assert.match(visibleText(card), /Checking CI/); assert.match(visibleText(card), /\/workspace/);
  assert.match(visibleText(card), /Track the PR/);
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

function toolInputDialog({confirmResult = true, reject = false, allowHistoryLoss = false} = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const calls = [], errors = new Element('p'), a = fullAgent('idle', false);
  const form = {querySelector: selector => selector === '.form-error' ? errors : null};
  const dialog = {open: true}, historyLoss = {checked: allowHistoryLoss};
  const context = vm.createContext({
    editId: a.id, metaCompactingId: null, toolInputCompactingId: null, state: {agents: [a]},
    agentById: id => context.state.agents.find(record => record.id === id),
    compactionRunning: record => record?.meta_compaction?.status === 'running' || record?.tool_input_compaction?.status === 'running',
    $: (selector, root) => root ? root.querySelector(selector) : selector === '#edit-form' ? form : selector === '#edit-dialog' ? dialog : selector === '#revert-allow-history-loss' ? historyLoss : null,
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
  assert.equal(request[2], 'POST'); assert.equal(request[3].allow_history_loss, false);
  assert.equal(t.agent.tool_input_compaction.status, 'reverted');
  assert.equal(t.context.toolInputCompactingId, null);
});

test('history-loss checkbox requires explicit opt-in and shows destructive confirmation', async () => {
  const t = withRevertArchive(toolInputDialog({allowHistoryLoss: true}));
  await t.context.revertToolInputs();
  const confirmation = t.calls.find(call => call[0] === 'confirm')[1];
  assert.match(confirmation, /WARNING: You enabled history loss/);
  assert.match(confirmation, /retained only in the backup/);
  assert.equal(t.calls.find(call => call[0] === 'api')[3].allow_history_loss, true);
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const tag = html.match(/<input[^>]+id="revert-allow-history-loss"[^>]*>/)[0];
  assert.doesNotMatch(tag, /\bchecked\b/);
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  assert.match(source, /revert-allow-history-loss'\)\.checked = false/);
  const canceled = withRevertArchive(toolInputDialog({allowHistoryLoss: true, confirmResult: false}));
  await canceled.context.revertToolInputs();
  assert.equal(canceled.calls.filter(call => call[0] === 'api').length, 0);
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
  const {render, storage, context} = renderer();
  context.cardViews.setCompact(home, 'agent-1', false);
  const card = render(fullAgent()); find(card, 'card-toggle').click();
  assert.equal(render(fullAgent('waiting')).classList.contains('is-compact'), true);
  assert.equal(new CardViews(() => storage).isCompact(home, fullAgent()), true);
  const inactive = render(fullAgent('done', false)); find(inactive, 'card-toggle').click();
  assert.equal(render(fullAgent('dormant')).classList.contains('is-compact'), false);
});

test('rendered untrusted names/goals remain text and new helper loads before the app', () => {
  const {render} = renderer();
  const a = fullAgent('idle'); a.name = '<script>bad()</script>'; a.goal = '<img src=x onerror=bad()>';
  const card = render(a); find(card, 'card-toggle').click();
  assert.ok(visibleText(card).includes(a.name)); assert.ok(visibleText(card).includes(a.goal));
  assert.equal(walk(card).some(el => ['SCRIPT', 'IMG'].includes(el.tagName)), false);
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  assert.ok(html.indexOf('src="/static/card-views.js?') < html.indexOf('src="/static/app.js?'));
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


test('Collapse all changes display preferences in one storage write', () => {
  const storage = memoryStorage(); let writes = 0;
  const original = storage.setItem;
  storage.setItem = (key, value) => { writes++; original(key, value); };
  const views = new CardViews(() => storage);
  views.setCompact(home, 'agent-1', false); writes = 0;
  const agents = [agent('running'), {...agent('idle'), id: 'agent-2'}];
  views.setAllCompact(home, agents, true);
  assert.equal(writes, 1);
  for (const a of agents) assert.equal(new CardViews(() => storage).isCompact(home, a), true);
  assert.equal(agents[0].status, 'running'); assert.equal(agents[0].alive, true);
  const blocked = new CardViews(() => { throw Error('denied'); });
  blocked.setAllCompact(home, agents, false);
  assert.equal(blocked.isCompact(home, agents[0]), false);
});

test('org chart integration loads before app and keeps nested-toggle focus', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  assert.ok(html.indexOf('src="/static/org-chart.js?') < html.indexOf('src="/static/app.js?'));
  assert.match(html, /id="agents" class="agent-grid org-chart"/);
  assert.match(html, /id="collapse-all"/);
  assert.match(source, /SwarmOrgChart.buildForest\(state.agents, new Set\(visible.map/);
  assert.match(source, /chart.querySelectorAll\('.agent-card'\)/);
  assert.match(source, /cardViews.setAllCompact\(state.home, state.agents, true\)/);
});


test('latest product is usable on compact and expanded cards without HTML interpretation', () => {
  const {render} = renderer();
  const a = fullAgent('idle', false);
  a.product = {id: 'product-one', kind: 'link', label: 'PR <script>not code</script>', url: 'https://example.com/pr/12'};
  const card = render(a), link = find(card, 'product-link');
  assert.equal(card.classList.contains('is-compact'), true);
  assert.equal(card.classList.contains('has-product'), true);
  assert.equal(link.href, 'https://example.com/pr/12');
  assert.equal(link.target, '_blank'); assert.equal(link.rel, 'noopener noreferrer');
  assert.match(visibleText(card), /PR <script>not code<\/script>/);
  assert.equal(walk(card).filter(el => el.tagName === 'SCRIPT').length, 0);
  find(card, 'card-toggle').click();
  assert.ok(visibleText(card).includes(a.product.label));
  a.product = {id: 'product-two', kind: 'file', label: 'Download report', filename: 'report.zip'};
  const updated = render(a), download = find(updated, 'product-link');
  assert.equal(download.href, '/api/agents/agent-1/products/product-two/download');
  assert.equal(download.download, 'report.zip');
  assert.equal(walk(updated).filter(el => el.classList.contains('product-link')).length, 1);
  assert.ok(!visibleText(updated).includes('PR <script>'));
  a.product = null;
  assert.equal(find(render(a), 'product-link'), undefined);
});

test('unsafe product URLs never become card links', () => {
  const {render} = renderer();
  for (const url of ['javascript:alert(1)', 'data:text/html,evil', 'file:///tmp/private', '/relative', 'https://user:secret@example.com']) {
    const a = {...fullAgent('idle', false), product: {kind: 'link', label: 'Unsafe', url}};
    assert.equal(find(render(a), 'product-link'), undefined, url);
  }
});


test('compact terminal visibility does not depend on a new helper version', () => {
  const {render, context} = renderer();
  context.SwarmCardViews = {applyCardView: (parts, compact, name) => {
    assert.equal('terminal' in parts, false);
    applyCardView(parts, compact, name);
  }};
  const card = render(fullAgent('idle', false));
  const shortcut = find(card, 'card-terminal'), toggle = find(card, 'card-toggle');
  assert.equal(shortcut.hidden, false);
  toggle.click(); assert.equal(shortcut.hidden, true);
  toggle.click(); assert.equal(shortcut.hidden, false);
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const version = html.match(/app\.js\?v=([^"]+)/)[1];
  for (const asset of ['app.css', 'app.js', 'card-views.js', 'org-chart.js', 'relay-settings.js']) {
    assert.ok(html.includes(`/static/${asset}?v=${version}`));
  }
});

test('terminal toolbar keeps reconnect but removes redundant Wake and Close tab', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const a = fullAgent('running', true), calls = [], panels = new Element('div'), tabs = new Element('div');
  const node = (tag, cls = '', text = '') => { const el = new Element(tag); el.className = cls; if (text) el.textContent = text; return el; };
  const context = vm.createContext({
    node, button: (text, cls, fn) => { const el = node('button', cls, text); el.addEventListener('click', fn); return el; },
    $: (selector, root) => root ? find(root, selector.slice(1)) : selector === '#terminals' ? panels : tabs,
    agentById: id => id === a.id ? a : null, terminals: new Map(), labels: {running: 'Running'},
    compactionBlocked: () => false, remember() {}, activate: id => calls.push(['activate', id]),
    closeTerminal: id => calls.push(['close', id]), wakeAgent: id => calls.push(['wake', id]),
    disconnectTerminal: t => calls.push(['disconnect', t.id]), attachTerminal: t => calls.push(['attach', t.id]),
    overlay: (...args) => calls.push(['overlay', ...args.slice(1)]),
  });
  const open = source.slice(source.indexOf('function openTerminal('), source.indexOf('function initTerminal('));
  const sync = source.slice(source.indexOf('function syncTerminal('), source.indexOf('function attachTerminal('));
  vm.runInContext(open + '\n' + sync, context);
  context.openTerminal(a.id);
  const t = context.terminals.get(a.id), header = t.pane.childNodes[0];
  assert.deepEqual(walk(header).filter(el => el.tagName === 'BUTTON').map(el => el.textContent), ['↻ Reconnect']);
  assert.equal('wakeButton' in t, false);
  assert.equal(find(t.group, 'tab-close').getAttribute('aria-label'), 'Close terminal tab');
  find(t.group, 'tab-close').click(); assert.ok(calls.some(c => c[0] === 'close' && c[1] === a.id));
  a.alive = false; context.syncTerminal(t);
  assert.equal(calls.at(-1)[3], 'Wake agent ↗');
  context.compactionBlocked = () => true; context.syncTerminal(t);
  assert.equal(calls.at(-1)[4], null);
  assert.equal(calls.filter(c => c[0] === 'wake').length, 0);
});
