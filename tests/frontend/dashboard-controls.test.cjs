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
  querySelectorAll(selector) { return selector === 'input[data-status]' ? this.children.filter(child => child.dataset.status) : this.children; }
  focus() { this.ownerDocument.activeElement = this; }
}
function fixture() {
  const tasks = [], listeners = new Set();
  const doc = {defaultView: {setTimeout(fn) { tasks.push(fn); }}, addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn)};
  const names = ['status-filter', 'status-filter-button', 'status-filter-menu', 'status-filter-label', 'status-filter-count', 'status-filter-dot', 'adoption-menu', 'adoption-menu-button', 'adoption-options', 'help-button', 'help-dialog', 'search', 'status-filter-search'];
  const values = Object.fromEntries(names.map(id => [`#${id}`, new Element(doc)]));
  const inputs = Object.fromEntries(['all', ...STATES].map(status => {
    const input = new Element(doc); input.dataset.status = status;
    values[`[data-status-count="${status}"]`] = new Element(doc); return [status, input];
  }));
  values['#search'].tagName = 'INPUT'; values['#search'].type = 'search'; values['#search'].value = '';
  values['#status-filter-menu'].append(values['#search'], ...Object.values(inputs));
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


test('text input keeps its native arrow and Home/End editing, while Escape closes', () => {
  const t=fixture(),filter=new StatusFilter({query:t.query,onChange(){}}),search=t.values['#search'];
  filter.dropdown.open();search.focus();search.value='typed filter';
  for(const key of ['ArrowDown','ArrowUp','Home','End']) {
    let prevented=false;
    filter.dropdown.keys({key,target:search,preventDefault(){prevented=true;},stopPropagation(){}});
    assert.equal(prevented,false);assert.equal(t.doc.activeElement,search);
  }
  filter.dropdown.keys({key:'Escape',target:search,preventDefault(){},stopPropagation(){}});
  assert.equal(t.values['#status-filter-menu'].hidden,true);
  assert.equal(t.doc.activeElement,t.values['#status-filter-button']);
  assert.equal(search.value,'typed filter');
});

test('a hidden active search is signaled without changing status checkbox choices', () => {
  const t=fixture(),filter=new StatusFilter({query:t.query,onChange(){}});
  assert.equal(t.values['#status-filter-search'].hidden,true);
  t.values['#search'].value='Needle';filter.update(agents);
  assert.equal(t.values['#status-filter-search'].hidden,false);
  assert.match(t.values['#status-filter-button'].title,/Text filter: Needle/);
  assert.equal(t.inputs.all.checked,true);
  t.values['#search'].value='';filter.update(agents);
  assert.equal(t.values['#status-filter-search'].hidden,true);
});

test('adoption is a create-footer alternative, search is in the state menu, collapse-all is absent', () => {
  const fs=require('node:fs'),path=require('node:path');
  const html=fs.readFileSync(path.join(__dirname,'../../src/swarm/static/index.html'),'utf8');
  const form=html.split('id="create-form"')[1].split('</form>')[0];
  const footer=form.split('<footer')[1].split('</footer>')[0];
  assert.ok(footer.includes('id="adoption-menu"'));
  assert.ok(footer.indexOf('id="adoption-menu"')<footer.indexOf('>Cancel</button>'));
  assert.ok(footer.includes('>Launch agent ↗</button>'));
  assert.ok(!html.split('class="actions"')[1].split('</div>')[0].includes('adoption-menu'));
  const menu=html.split('id="status-filter-menu"')[1].split('</div>')[0];
  assert.ok(menu.includes('id="search"'));
  assert.ok(!html.includes('collapse-all'));
  const app=fs.readFileSync(path.join(__dirname,'../../src/swarm/static/app.js'),'utf8');
  assert.ok(!app.includes('collapse-all'));
  assert.ok(app.includes("dashboardControls.status.dropdown.open(); $('#search').focus();"));
});
