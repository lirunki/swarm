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
