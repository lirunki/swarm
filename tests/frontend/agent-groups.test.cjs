const test = require('node:test');
const assert = require('node:assert/strict');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.events = new Map(); this.attributes = new Map();
    this.className = ''; this.text = ''; this.hidden = true; this.disabled = false; this.checked = false; this.style = {};
    this.parentNode = null; this.offsetWidth = 100; this.offsetHeight = 80;
  }
  append(...children) { for (const child of children) { this.children.push(child); if (child instanceof Element) child.parentNode = this; } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent || '').join(''); }
  addEventListener(name, callback) { if (!this.events.has(name)) this.events.set(name, []); this.events.get(name).push(callback); }
  dispatch(name, extra = {}) {
    const event = {target: this, currentTarget: this, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...extra};
    for (const callback of this.events.get(name) || []) callback(event); return event;
  }
  click(extra) { if (!this.disabled) return this.dispatch('click', extra); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  contains(target) { for (let node = target; node; node = node.parentNode) if (node === this) return true; return false; }
  get classList() { return {toggle: (name, enabled) => {
    const values = new Set(this.className.split(/\s+/).filter(Boolean)); enabled ? values.add(name) : values.delete(name); this.className = [...values].join(' ');
  }, contains: name => this.className.split(/\s+/).includes(name)}; }
  getBoundingClientRect() { return {left: 25, bottom: 35}; }
  focus() { this.focused = true; }
}

const documentEvents = new Map();
global.document = {
  createElement: tag => new Element(tag), documentElement: {clientWidth: 500, clientHeight: 400},
  addEventListener(name, callback) { documentEvents.set(name, callback); },
};
const {familyIndex, effectiveAssignments, AgentGroupsController} = require('../../src/swarm/static/agent-groups.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const agents = [
  {id: 'root', name: 'Root'}, {id: 'child', name: 'Child', parent_id: 'root'}, {id: 'sibling', name: 'Sibling', parent_id: 'root'},
  {id: 'other', name: 'Other'},
];

function fixture(handler = async (path, method, body) => ({revision: body.revision + 1, groups: [], assignments: {}}), options = {}) {
  const ids = ['group-selection-bar', 'group-selection-count', 'group-selection-open', 'group-selection-clear', 'group-context-menu',
    'grouping-submenu-button', 'grouping-submenu'];
  const nodes = Object.fromEntries(ids.map(id => [`#${id}`, new Element(id.includes('button') || id.endsWith('open') || id.endsWith('clear') ? 'button' : 'div')]));
  let source = 'one', generation = 1; const calls = [], changes = [], toasts = [];
  const controller = new AgentGroupsController({
    query: selector => nodes[selector], create: (tag, cls = '', text = '') => Object.assign(new Element(tag), {className: cls, textContent: text}),
    api: async (...args) => { calls.push(args); return handler(...args); }, currentSwarm: () => source, currentGeneration: () => generation,
    onChanged: async (...values) => changes.push(values), onOpenGroup: (...values) => { fixture.opened = values; },
    prompt: options.prompt || (() => 'Group A'), confirm: options.confirm || (() => true), toast: text => toasts.push(text),
    requestTimeout: options.requestTimeout ?? 20000,
  });
  controller.setState(agents, {revision: 4, groups: [{id: 'g', name: '<img onerror=bad>'}], assignments: {root: 'g'}});
  return {controller, nodes, calls, changes, toasts,
    source(value) { source = value; }, generation(value) { generation = value; }};
}

test('familyIndex uses only existing-record edges for deterministic components', () => {
  const index = familyIndex([
    {id: 'z', parent_id: 'missing'}, {id: 'a', parent_id: 'missing'},
    {id: 'c', parent_id: 'b'}, {id: 'b', parent_id: 'c'}, {id: 'solo', parent_id: 'solo'},
  ]);
  assert.deepEqual(index.families, [['a'], ['b', 'c'], ['solo'], ['z']]);
  assert.notStrictEqual(index.get('a'), index.get('z'), 'a missing parent is not a virtual sibling node');
  assert.deepEqual(familyIndex([{id: 'missing'}, {id: 'z', parent_id: 'missing'}, {id: 'a', parent_id: 'missing'}]).families,
    [['a', 'missing', 'z']], 'an actual parent record connects its children');
  assert.deepEqual(familyIndex([...agents].reverse()).families, [['child', 'root', 'sibling'], ['other']]);
});

test('effective assignments use backend BFS depth then ID and ignore persisted nulls', () => {
  const groups = [{id: 'red', name: 'Red'}, {id: 'blue', name: 'Blue'}];
  const projection = {groups, assignments: {child: 'blue', root: 'red'}};
  assert.deepEqual(effectiveAssignments(agents, projection), {child: 'red', root: 'red', sibling: 'red', other: null});

  const deepLexicalFirst = [{id: 'm'}, {id: 'z', parent_id: 'm'}, {id: 'a', parent_id: 'z'}];
  assert.deepEqual(effectiveAssignments(deepLexicalFirst, {groups, assignments: {a: 'red', z: 'blue'}}),
    {a: 'blue', m: 'blue', z: 'blue'}, 'the nearest assigned ancestor wins before lexical ID');
  assert.deepEqual(effectiveAssignments(deepLexicalFirst, {groups, assignments: {m: null, z: 'blue'}}),
    {a: 'blue', m: 'blue', z: 'blue'}, 'null is a UI fallback, not a persisted assignment candidate');

  const cycle = [{id: 'c', parent_id: 'b'}, {id: 'b', parent_id: 'a'}, {id: 'a', parent_id: 'c'}];
  assert.deepEqual(effectiveAssignments(cycle, {groups, assignments: {c: 'red', b: 'blue'}}),
    {a: 'blue', b: 'blue', c: 'blue'}, 'cycle ranking starts at the lexical cut and follows child depth');
});

test('card selection is family-wide, survives filtering renders, and does not intercept native actions', () => {
  const t = fixture();
  const root = new Element('article'), child = new Element('article'), action = new Element('button'); root.append(action);
  t.controller.decorateCard(root, agents[0]); t.controller.decorateCard(child, agents[1]);
  root.dispatch('click');
  assert.deepEqual([...t.controller.selectedIDs].sort(), ['child', 'root', 'sibling']);
  assert.equal(root.classList.contains('is-group-selected'), true); assert.equal(child.classList.contains('is-group-selected'), true);
  assert.equal(t.nodes['#group-selection-count'].textContent, '1 family unit selected');
  action.dispatch('click'); root.dispatch('click', {target: action});
  assert.deepEqual([...t.controller.selectedIDs].sort(), ['child', 'root', 'sibling'], 'a button click does not alter selection');

  const replacement = new Element('article'); t.controller.decorateCard(replacement, agents[0]);
  assert.equal(root.classList.contains('is-group-selected'), false, 'only a current card remains marked');
  t.controller.setState(agents, t.controller.projection); t.controller.decorateCard(new Element('article'), agents[3]);
  assert.deepEqual([...t.controller.selectedIDs].sort(), ['child', 'root', 'sibling'], 'hidden family IDs survive a valid state/render update');
  assert.equal(t.nodes['#group-selection-count'].textContent, '1 family unit selected');
});

test('context selection retains existing families and produces accessible, safely rendered menu items', () => {
  const t = fixture(); const first = new Element('article'), second = new Element('article');
  t.controller.decorateCard(first, agents[0]); t.controller.decorateCard(second, agents[3]); first.dispatch('click');
  const event = second.dispatch('contextmenu', {clientX: 10000, clientY: 10000});
  assert.equal(event.defaultPrevented, true); assert.equal(t.controller.selectedFamilies().length, 2);
  assert.equal(t.nodes['#group-context-menu'].hidden, false); assert.equal(t.nodes['#group-context-menu'].style.left, '400px');
  const menu = t.nodes['#grouping-submenu'];
  assert.equal(menu.getAttribute('role'), 'menu'); assert.deepEqual(menu.children.map(node => node.textContent), ['Default group', 'New group…', '<img onerror=bad>']);
  assert.ok(menu.children.every(node => node.getAttribute('role') === 'menuitem'));
  assert.equal(menu.children.some(node => node.tagName === 'IMG'), false);
  documentEvents.get('keydown')({key: 'Escape', preventDefault() {}}); assert.equal(t.nodes['#group-context-menu'].hidden, true);
});

test('toolbar opens the grouping submenu and cards provide keyboard context-menu focus', () => {
  const t = fixture(); const card = new Element('article');
  t.controller.decorateCard(card, agents[0]);
  assert.equal(card.getAttribute('tabindex'), '0');
  card.dispatch('click'); t.nodes['#group-selection-open'].click();
  assert.equal(t.nodes['#group-context-menu'].hidden, false);
  assert.equal(t.nodes['#group-context-menu'].style.left, '25px');
  assert.equal(t.nodes['#group-context-menu'].style.top, '35px');
  assert.equal(t.nodes['#grouping-submenu'].hidden, false);
  assert.equal(t.nodes['#grouping-submenu-button'].getAttribute('aria-expanded'), 'true');
  assert.equal(t.nodes['#grouping-submenu-button'].focused, true, 'keyboard opening moves focus into the menu');

  t.controller.closeMenu(); t.controller.clearSelection();
  const keyboardEvent = card.dispatch('keydown', {key: 'F10', shiftKey: true});
  assert.equal(keyboardEvent.defaultPrevented, true); assert.equal(t.nodes['#group-context-menu'].hidden, false);
  assert.equal(t.nodes['#grouping-submenu-button'].focused, true);

  const existingTabStop = new Element('article'); existingTabStop.setAttribute('tabindex', '-1');
  t.controller.decorateCard(existingTabStop, agents[3]); assert.equal(existingTabStop.getAttribute('tabindex'), '-1');
});

test('unchanged polling state retains the open menu and expanded submenu', () => {
  const t = fixture(); const card = new Element('article'); t.controller.decorateCard(card, agents[0]);
  card.dispatch('contextmenu', {clientX: 50, clientY: 50}); t.nodes['#grouping-submenu-button'].click();
  const children = t.nodes['#grouping-submenu'].children;
  t.controller.setState([...agents], {revision: 5, groups: [{id: 'g', name: '<img onerror=bad>'}], assignments: {child: 'g'}});
  assert.equal(t.nodes['#group-context-menu'].hidden, false);
  assert.equal(t.nodes['#grouping-submenu'].hidden, false);
  assert.strictEqual(t.nodes['#grouping-submenu'].children, children, 'unchanged groups do not rebuild menu controls');
  assert.deepEqual([...t.controller.selectedIDs].sort(), ['child', 'root', 'sibling']);
});

test('submenu expansion reclamps the parent menu to the viewport', () => {
  const t = fixture(); t.controller.selectFamily('root');
  const parent = t.nodes['#group-context-menu'];
  t.controller.openMenu(490, 390); assert.equal(parent.style.top, '320px');
  parent.offsetHeight = 300; t.nodes['#grouping-submenu-button'].click();
  assert.equal(parent.style.top, '100px');
  assert.equal(parent.style.maxHeight, 'calc(100vh - 100px)');
  assert.equal(parent.style.overflowY, 'auto');
});

test('mutations capture revision, source, selected family, request ID and guard double clicks', async () => {
  let release;
  const t = fixture((path, method, body) => new Promise(resolve => { release = () => resolve({revision: 5, groups: [{id: 'n', name: body.name}], assignments: {root: 'n'}}); }));
  t.controller.selectFamily('root');
  const first = t.controller.createGroup(); const duplicate = t.controller.createGroup(); await tick();
  assert.equal(t.calls.length, 1); const [path, method, body] = t.calls[0];
  assert.equal(path, '/api/groups'); assert.equal(method, 'POST');
  assert.equal(body.action, 'create'); assert.equal(body.revision, 4); assert.deepEqual(body.agent_ids, ['child', 'root', 'sibling']);
  assert.match(body.request_id, /^[0-9a-f]{8}-[0-9a-f-]{27}$/i);
  release(); await Promise.all([first, duplicate]);
  assert.equal(t.changes.length, 1); assert.equal(t.changes[0][0].revision, 5); assert.deepEqual(t.changes[0][1], {operation: 'create'});
});

test('409 asks the parent for a guarded refresh while uncertain failures expose the request ID', async () => {
  const conflict = fixture(async () => { throw Object.assign(Error('stale revision'), {status: 409}); });
  conflict.controller.selectFamily('root'); await conflict.controller.assignGroup('g');
  assert.deepEqual(conflict.changes, [[null, {refresh: true}]]); assert.match(conflict.toasts[0], /stale revision/);

  const uncertain = fixture(async () => { throw Error('connection lost'); });
  uncertain.controller.selectFamily('root'); await uncertain.controller.assignGroup(null);
  assert.match(uncertain.toasts[0], /uncertain.*Do not retry blindly.*Request ID [0-9a-f-]{36}/i);
});

test('mutations time out without retry and stale timeout failures stay source-guarded', async () => {
  const timedOut = fixture(() => new Promise(() => {}), {requestTimeout: 5});
  timedOut.controller.selectFamily('root'); await timedOut.controller.assignGroup(null);
  assert.equal(timedOut.calls.length, 1, 'a timed-out uncertain mutation is never retried');
  assert.match(timedOut.toasts[0], /uncertain.*Request ID [0-9a-f-]{36}.*timed out/i);
  assert.equal(timedOut.controller.busy, false);

  const stale = fixture(() => new Promise(() => {}), {requestTimeout: 5});
  stale.controller.selectFamily('root'); const pending = stale.controller.assignGroup(null);
  stale.source('two'); stale.generation(2); await pending;
  assert.deepEqual(stale.toasts, [], 'an old source timeout does not report into the new source');
  assert.deepEqual([...stale.controller.selectedIDs], []);
});

test('API dispatch captures context synchronously before a microtask source change', async () => {
  let activeSource = 'one', release;
  const t = fixture(() => {
    activeSource = t.controller.currentSwarm();
    return new Promise(resolve => { release = () => resolve({revision: 5, groups: [], assignments: {}}); });
  });
  t.controller.selectFamily('root');
  const pending = t.controller.assignGroup(null);
  assert.equal(t.calls.length, 1, 'callApi is invoked in the mutation turn, not a deferred microtask');
  assert.equal(activeSource, 'one', 'a parent API that reads activeSwarm captures the checked source');
  t.source('two'); t.generation(2);
  release(); await pending;
  assert.deepEqual(t.changes, []); assert.deepEqual([...t.controller.selectedIDs], [], 'stale completion clears the old source selection');
  assert.equal(t.nodes['#group-context-menu'].hidden, true);
  t.controller.selectFamily('other'); assert.deepEqual([...t.controller.selectedIDs], ['other'], 'the new source can be selected normally');
});

test('an async prompt never dispatches after its source changes', async () => {
  let resolvePrompt;
  const t = fixture(async () => { throw Error('must not call API'); }, {
    prompt: () => new Promise(resolve => { resolvePrompt = resolve; }),
  });
  t.controller.selectFamily('root'); const pending = t.controller.createGroup();
  t.source('two'); t.generation(2); resolvePrompt('Late group');
  assert.equal(await pending, false);
  assert.deepEqual(t.calls, []); assert.deepEqual(t.toasts, []);
  t.controller.setState(agents, {revision: 1, groups: [], assignments: {}});
  assert.equal(t.controller.busy, false);
});

test('an old operation cannot release or clear a busy operation in the new context', async () => {
  let releaseOld, releaseNew;
  const t = fixture((path, method, body) => new Promise(resolve => {
    if (!releaseOld) releaseOld = () => resolve({revision: 5, groups: [], assignments: {}});
    else releaseNew = () => resolve({revision: 2, groups: [], assignments: {}});
  }));
  t.controller.selectFamily('root'); const oldPending = t.controller.assignGroup(null);

  t.source('two'); t.generation(2);
  t.controller.setState(agents, {revision: 1, groups: [], assignments: {}});
  t.controller.selectFamily('other'); t.controller.openMenu(10, 10);
  const newPending = t.controller.assignGroup(null);
  assert.equal(t.controller.busy, true); assert.equal(t.nodes['#group-context-menu'].hidden, false);

  releaseOld(); await oldPending;
  assert.equal(t.controller.busy, true, 'the old finally does not release the new operation token');
  assert.deepEqual([...t.controller.selectedIDs], ['other']);
  assert.equal(t.nodes['#group-context-menu'].hidden, false, 'a stale completion does not close the new context menu');
  assert.deepEqual(t.toasts, []); assert.deepEqual(t.changes, []);

  releaseNew(); await newPending;
  assert.equal(t.controller.busy, false); assert.equal(t.changes.length, 1);
});

test('projection errors and corrupt selections disable every mutation without an API call', async () => {
  const t = fixture(async () => { throw Error('must not call API'); });
  t.controller.selectFamily('root');
  t.controller.setState(agents, {revision: 'corrupt', groups: [{id: 'g', name: 'Group'}], assignments: {}});
  assert.equal(t.nodes['#group-selection-open'].disabled, true);
  assert.equal(t.nodes['#group-selection-clear'].disabled, false, 'selection clearing remains available');
  assert.ok(t.nodes['#grouping-submenu'].children.every(button => button.disabled));
  assert.equal(await t.controller.assignGroup(null), false);
  assert.equal(await t.controller.createGroup(), false);
  assert.equal(await t.controller.renameGroup('g'), false);
  assert.equal(await t.controller.deleteGroup('g'), false);

  const valid = {revision: 5, groups: [{id: 'g', name: 'Group'}], assignments: {}};
  t.controller.setState(agents, valid);
  assert.ok(t.nodes['#grouping-submenu'].children.every(button => !button.disabled));
  t.controller.setState(agents, valid, Error('load failed'));
  assert.ok(t.nodes['#grouping-submenu'].children.every(button => button.disabled), 'a same-projection error disables existing menu actions');
  assert.equal(await t.controller.renameGroup('g'), false);
  assert.equal(await t.controller.deleteGroup('g'), false);

  t.controller.setState(agents, {revision: 6, groups: [], assignments: {}});
  t.controller.selectedIDs.clear(); t.controller.selectedIDs.add('unknown-agent');
  assert.equal(await t.controller.assignGroup(null), false, 'unknown selected IDs are not sent');
  assert.deepEqual(t.calls, []);
});

test('rename/delete validate names and deletion explicitly preserves agents', async () => {
  const confirmations = [];
  const t = fixture(async (path, method, body) => ({revision: 5, groups: [], assignments: {}}), {
    prompt: () => 'Renamed', confirm: text => { confirmations.push(text); return true; },
  });
  await t.controller.renameGroup('g'); await t.controller.deleteGroup('g');
  assert.deepEqual(t.calls.map(call => call[2].action), ['rename', 'delete']);
  assert.match(confirmations[0], /families return to the default group.*Agents are never deleted/is);

  const reserved = fixture(async () => { throw Error('must not call API'); }, {prompt: () => 'ＤｅｆａｕｌｔＧｒｏｕｐ'});
  reserved.controller.selectFamily('root'); await reserved.controller.createGroup();
  assert.deepEqual(reserved.calls, []); assert.match(reserved.toasts[0], /reserved default-group name/);
});
