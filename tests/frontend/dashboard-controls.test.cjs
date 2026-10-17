const test = require('node:test');
const assert = require('node:assert/strict');
const {STATES, Dropdown, StatusFilter, DashboardControls} = require('../../src/swarm/static/dashboard-controls.js');

class Element {
  constructor(document) { this.ownerDocument = document; this.events = new Map(); this.attributes = new Map(); this.children = []; this.dataset = {}; }
  addEventListener(type, callback) { this.events.set(type, callback); }
  removeEventListener(type, callback) { if (this.events.get(type) === callback) this.events.delete(type); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  append(...children) { this.children.push(...children); }
  contains(target) { return target === this || this.children.some(child => child.contains(target)); }
  querySelectorAll() { return this.children; }
  focus() { this.ownerDocument.activeElement = this; }
}
function fixture() {
  const tasks = [], listeners = new Set();
  const doc = {defaultView: {setTimeout(fn) { tasks.push(fn); }}, addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn)};
  const names = ['status-filter', 'status-filter-button', 'status-filter-menu', 'status-filter-label', 'status-filter-count', 'status-filter-dot', 'adoption-menu', 'adoption-menu-button', 'adoption-options', 'help-button', 'help-dialog'];
  const values = Object.fromEntries(names.map(id => [`#${id}`, new Element(doc)]));
  const inputs = Object.fromEntries(['all', ...STATES].map(status => {
    const input = new Element(doc); input.dataset.status = status;
    values[`[data-status-count="${status}"]`] = new Element(doc); return [status, input];
  }));
  values['#status-filter-menu'].append(...Object.values(inputs));
  values['#status-filter'].append(values['#status-filter-button'], values['#status-filter-menu']);
  values['#adoption-options'].append(new Element(doc), new Element(doc));
  values['#adoption-menu'].append(values['#adoption-menu-button'], values['#adoption-options']);
  values['#help-dialog'].showModal = () => { values['#help-dialog'].open = true; };
  return {values, inputs, doc, tasks, listeners, query: selector => values[selector]};
}
const agents = [{status: 'running'}, {status: 'waiting'}, {status: 'idle'}, {status: 'idle'}, {status: 'done'}];

test('all and each colored status expose live independent counts', () => {
  const t = fixture(); const filter = new StatusFilter({query: t.query, onChange() {}}); filter.update(agents);
  assert.equal(filter.all, true); assert.equal(t.values['[data-status-count="all"]'].textContent, '5');
  assert.equal(t.values['[data-status-count="idle"]'].textContent, '2'); assert.equal(t.inputs.all.checked, true);
  assert.equal(t.values['#status-filter-label'].textContent, 'All states');
  assert.equal(t.inputs.running.getAttribute('aria-label'), 'Running, 1 agent');
});

test('multiple status choices filter together; All toggles all/none and reset restores defaults', () => {
  const t = fixture(); let changed = 0;
  const filter = new StatusFilter({query: t.query, onChange: () => changed++}); filter.update(agents);
  t.inputs.all.checked = false; t.inputs.all.events.get('change')();
  assert.equal(filter.matches('running'), false); assert.equal(t.values['#status-filter-label'].textContent, 'No states');
  for (const status of ['running', 'waiting']) { t.inputs[status].checked = true; t.inputs[status].events.get('change')(); }
  assert.equal(filter.matches('running'), true); assert.equal(filter.matches('waiting'), true); assert.equal(filter.matches('idle'), false);
  assert.equal(t.values['#status-filter-label'].textContent, '2 states'); assert.equal(t.inputs.all.indeterminate, true);
  assert.equal(t.values['#status-filter-count'].textContent, '2');
  t.inputs.running.checked = false; t.inputs.running.events.get('change')();
  assert.equal(t.values['#status-filter-label'].textContent, 'Waiting'); assert.equal(t.values['#status-filter-dot'].className, 'status-dot waiting');
  filter.reset(); assert.equal(filter.all, true); assert.equal(changed, 4);
});

test('state updates preserve checkbox focus/selection and do not fire filter changes', () => {
  const t = fixture(); let changed = 0;
  const filter = new StatusFilter({query: t.query, onChange: () => changed++});
  t.inputs.idle.checked = false; t.inputs.idle.events.get('change')(); t.inputs.running.focus();
  filter.update([...agents, {status: 'running'}]);
  assert.equal(changed, 1); assert.equal(t.doc.activeElement, t.inputs.running); assert.equal(filter.matches('idle'), false);
  assert.equal(t.values['[data-status-count="running"]'].textContent, '2');
});

test('dropdown supports keyboard, Escape focus restoration, outside dismissal and disposal', () => {
  const t = fixture(), button = t.values['#adoption-menu-button'], panel = t.values['#adoption-options'];
  const dropdown = new Dropdown({container: t.values['#adoption-menu'], button, panel});
  button.focus();
  const key = value => ({key: value, target: button, preventDefault() {}, stopPropagation() {}});
  dropdown.keys(key('ArrowDown')); assert.equal(panel.hidden, false); assert.equal(t.doc.activeElement, panel.children[0]);
  dropdown.keys(key('ArrowDown')); assert.equal(t.doc.activeElement, panel.children[1]);
  dropdown.keys(key('Escape')); assert.equal(panel.hidden, true); assert.equal(t.doc.activeElement, button);
  dropdown.open(); dropdown.outside({target: new Element(t.doc)}); assert.equal(panel.hidden, true);
  dropdown.open(); t.doc.activeElement = new Element(t.doc); dropdown.blur(); t.tasks.at(-1)(); assert.equal(panel.hidden, true);
  dropdown.dispose(); assert.equal(t.listeners.size, 0);
});

test('help opens its dialog and adoption/status menus remain separate controls', () => {
  const t = fixture(), controls = new DashboardControls({query: t.query, onStatusChange() {}});
  t.values['#help-button'].events.get('click')(); assert.equal(t.values['#help-dialog'].open, true);
  controls.adoption.open(); assert.equal(t.values['#adoption-options'].hidden, false);
  assert.equal(t.values['#status-filter-menu'].hidden, true);
});
