const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {TerminalCopy, visibleText} = require('../../src/swarm/static/terminal-copy.js');

class Element {
  constructor(doc) { this.ownerDocument = doc; this.dataset = {}; this.style = {}; this.events = new Map(); this.attributes = new Map(); this.children = []; this.classes = new Set(); this.value = ''; this.selectionStart = this.selectionEnd = 0; }
  get classList() { return {toggle: (name, yes) => yes ? this.classes.add(name) : this.classes.delete(name)}; }
  addEventListener(type, callback) { this.events.set(type, callback); }
  removeEventListener(type, callback) { if (this.events.get(type) === callback) this.events.delete(type); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
  append(child) { this.children.push(child); }
  remove() { this.removed = true; }
  focus() { this.ownerDocument.activeElement = this; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
}
function fixture(mobile = true) {
  const jobs = new Map(), listeners = new Set(), calls = []; let sequence = 0;
  const media = {matches: mobile, addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn)};
  const doc = {events: new Map(), createElement: () => new Element(doc), addEventListener: (type, fn) => doc.events.set(type, fn), removeEventListener: type => doc.events.delete(type)};
  const view = {matchMedia: () => media,
    setTimeout(callback) { assert.equal(this, view); jobs.set(++sequence, callback); return sequence; },
    clearTimeout(id) { assert.equal(this, view); jobs.delete(id); }};
  doc.defaultView = view;
  const screen = new Element(doc), button = new Element(doc); button.dataset.agentId = 'agent-1';
  const rows = ['not visible', 'older', 'VISIBLE <script>literal</script>', 'second row', 'not yet visible'];
  const term = {rows: 2, cols: 80, options: {fontFamily: 'monospace', fontSize: 13}, buffer: {active: {viewportY: 2,
    getLine: index => ({translateToString: (trim, start, end) => { assert.equal(trim, true); assert.equal(start, 0); assert.equal(end, 80); return rows[index]; }})}}};
  const copy = new TerminalCopy({term, screen, button, onOpen: () => calls.push('open'), onClose: focus => calls.push(['close', focus])});
  const flush = () => { for (const [id, callback] of [...jobs]) { jobs.delete(id); callback(); } };
  return {copy, term, screen, button, doc, media, listeners, jobs, calls, rows, flush};
}

test('captures only the rendered viewport and exposes a mobile read-only native text control', () => {
  const t = fixture(); assert.equal(visibleText(t.term), 'VISIBLE <script>literal</script>\nsecond row');
  t.copy.open(); assert.equal(t.copy.active, true); assert.equal(t.copy.text.readOnly, true);
  assert.equal(t.copy.text.value, visibleText(t.term)); assert.equal(t.button.textContent, 'Done copying');
  assert.equal(t.copy.text.getAttribute('inputmode'), 'none'); assert.equal(t.copy.text.style.fontSize, '16px');
  assert.equal(t.screen.classes.has('copying-text'), true); assert.equal(t.doc.activeElement, t.copy.text);
  t.rows[2] = 'live output changed'; assert.notEqual(t.copy.text.value, visibleText(t.term));
  assert.equal(t.copy.text.innerHTML, undefined); // Plain .value, never interpreted markup.
});

test('native Copy finishes before auto-close clears the text/selection', () => {
  const t = fixture(); t.copy.open(); t.copy.text.setSelectionRange(0, 7);
  let prevented = false;
  t.doc.events.get('copy')({target: t.copy.text, preventDefault() { prevented = true; }});
  assert.equal(prevented, false); assert.equal(t.copy.active, true);
  const clipboard = t.copy.text.value.slice(t.copy.text.selectionStart, t.copy.text.selectionEnd);
  assert.equal(clipboard, 'VISIBLE');
  t.flush(); assert.equal(t.copy.active, false); assert.equal(t.copy.text.value, '');
  assert.equal(t.button.textContent, 'Copy text'); assert.equal(t.screen.classes.has('copying-text'), false);
});

test('Done copying and Escape close without copying or sending terminal input', () => {
  const t = fixture(); t.button.events.get('click')(); t.button.events.get('click')();
  assert.equal(t.copy.active, false); assert.equal(t.jobs.size, 0);
  t.copy.open(); let prevented = false;
  t.copy.text.events.get('keydown')({key: 'Escape', preventDefault() { prevented = true; }, stopPropagation() {}});
  assert.equal(prevented, true); assert.equal(t.copy.active, false);
});

test('empty or unrelated copy actions do not close the overlay', () => {
  const t = fixture(); t.copy.open(); t.doc.events.get('copy')({target: t.copy.text});
  assert.equal(t.jobs.size, 0);
  t.copy.text.setSelectionRange(0, 5); t.doc.activeElement = new Element(t.doc);
  t.doc.events.get('copy')({target: t.doc.activeElement}); assert.equal(t.jobs.size, 0);
});

test('desktop stays unchanged and changing input capability safely closes mobile overlay', () => {
  const t = fixture(false); assert.equal(t.button.hidden, true); t.copy.open(); assert.equal(t.copy.active, false);
  t.media.matches = true; for (const changed of t.listeners) changed();
  assert.equal(t.button.hidden, false); t.copy.open();
  t.media.matches = false; for (const changed of t.listeners) changed();
  assert.equal(t.copy.active, false); assert.equal(t.button.hidden, true);
});

test('late copy timers cannot close a newer overlay and disposing clears private drafts/listeners', () => {
  const t = fixture(); t.copy.open(); t.copy.text.setSelectionRange(0, 3);
  t.doc.events.get('copy')({target: t.copy.text}); const late = [...t.jobs.values()][0];
  t.copy.close(); t.copy.open(); late(); assert.equal(t.copy.active, true);
  t.copy.dispose(); assert.equal(t.copy.text.value, ''); assert.equal(t.copy.text.removed, true);
  assert.equal(t.listeners.size, 0); assert.equal(t.doc.events.size, 0); assert.equal(t.button.events.size, 0);
  assert.deepEqual(t.calls.at(-1), ['close', false]);
});

test('copy is client-only, not a history API, clipboard permission request or tmux setting', () => {
  const module = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/terminal-copy.js'), 'utf8');
  assert.doesNotMatch(module, /navigator\.clipboard|execCommand|fetch\(|terminal-history|mouse off/);
  const app = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  assert.match(app, /!t\.copyText\?\.active && t\.ws/);
  assert.match(app, /!t\.copyText\?\.active && t\.ws\?\.readyState/);
  assert.match(app, /t\.copyText\?\.dispose\(\)/);
});
