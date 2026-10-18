const test = require('node:test');
const assert = require('node:assert/strict');
const {TerminalKeys, encodeKey, controlCharacter} = require('../../src/swarm/static/terminal-keys.js');

class Events {
  constructor() { this.events = new Map(); }
  addEventListener(name, fn) { if (!this.events.has(name)) this.events.set(name, new Set()); this.events.get(name).add(fn); }
  removeEventListener(name, fn) { this.events.get(name)?.delete(fn); }
  fire(name, event = {}) { for (const fn of this.events.get(name) || []) fn(event); }
}
class Element extends Events {
  constructor(doc) { super(); this.ownerDocument = doc; this.style = {}; this.dataset = {}; this.attributes = new Map(); this.children = []; }
  append(child) { this.children.push(child); }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key); }
  getBoundingClientRect() { return {height: 48}; }
  remove() { this.removed = true; }
}
function fixture({mobile = true, viewport = true} = {}) {
  const doc = new Events(); doc.documentElement = {clientWidth: 390, clientHeight: 1000};
  doc.createElement = () => new Element(doc); doc.body = new Element(doc); doc.activeElement = doc.body;
  const view = new Events(); Object.assign(view, {innerWidth: 390, innerHeight: 1000}); doc.defaultView = view;
  if (viewport) { view.visualViewport = new Events(); Object.assign(view.visualViewport, {width: 390, height: 1000, offsetTop: 0, offsetLeft: 0, scale: 1}); }
  const vk = new Events(); vk.boundingRect = {height: 0, top: 0}; view.navigator = {virtualKeyboard: vk};
  const media = new Events(); media.matches = mobile;
  const pane = new Element(doc); pane.parentElement = {getBoundingClientRect: () => ({bottom: view.innerHeight})};
  const input = new Element(doc), term = {textarea: input, modes: {applicationCursorKeysMode: false}};
  const sent = [], state = {active: true, ready: true};
  const keys = new TerminalKeys({term, pane, media, isActive: () => state.active, ready: () => state.ready, send: data => sent.push(data)});
  const focus = () => { doc.activeElement = input; doc.fire('focusin'); };
  const keyboard = height => { if (view.visualViewport) { view.visualViewport.height = height; view.visualViewport.fire('resize'); } else { view.innerHeight = height; doc.documentElement.clientHeight = height; view.fire('resize'); } };
  return {keys, term, pane, input, doc, view, vk, media, sent, state, focus, keyboard};
}

test('navigation keys respect application cursor mode and modifiers, Sup is forward Delete', () => {
  for (const [key, code] of Object.entries({up: 'A', down: 'B', right: 'C', left: 'D', home: 'H', end: 'F'})) {
    assert.equal(encodeKey(key), `\x1b[${code}`);
    assert.equal(encodeKey(key, {}, {applicationCursorKeysMode: true}), `\x1bO${code}`);
    assert.equal(encodeKey(key, {ctrl: true}), `\x1b[1;5${code}`);
    assert.equal(encodeKey(key, {alt: true}), `\x1b[1;3${code}`);
    assert.equal(encodeKey(key, {ctrl: true, alt: true}), `\x1b[1;7${code}`);
  }
  assert.equal(encodeKey('delete'), '\x1b[3~'); assert.equal(encodeKey('delete', {ctrl: true}), '\x1b[3;5~');
  assert.equal(encodeKey('escape'), '\x1b'); assert.equal(encodeKey('escape', {alt: true}), '\x1b\x1b');
  assert.equal(encodeKey('tab'), '\t'); assert.equal(encodeKey('tab', {alt: true}), '\x1b\t');
});

test('ASCII control mappings are standard and non-ASCII characters are not corrupted', () => {
  assert.equal(controlCharacter('a'), '\x01'); assert.equal(controlCharacter('C'), '\x03');
  assert.equal(controlCharacter(' '), '\x00'); assert.equal(controlCharacter('['), '\x1b');
  assert.equal(controlCharacter('?'), '\x7f'); assert.equal(controlCharacter('汉'), '汉');
});

test('bar needs mobile, terminal focus and an actually shrunken keyboard viewport', () => {
  const t = fixture(); assert.equal(t.keys.bar.hidden, true);
  t.focus(); assert.equal(t.keys.bar.hidden, true);
  t.keyboard(650); assert.equal(t.keys.bar.hidden, false); assert.equal(t.keys.bar.style.top, '602px');
  assert.equal(t.pane.style.bottom, '398px'); assert.equal(t.doc.activeElement, t.input);
  t.keys.press('ctrl'); t.keyboard(1000); assert.equal(t.keys.bar.hidden, true); assert.equal(t.keys.ctrl, false);
  assert.equal(t.pane.style.bottom, '');
  const desktop = fixture({mobile: false}); desktop.focus(); desktop.keyboard(650); assert.equal(desktop.keys.bar.hidden, true);
});

test('Ctrl and Alt affect exactly one typed key, including a combined shortcut', () => {
  const t = fixture(); t.focus(); t.keyboard(650);
  t.keys.press('ctrl'); assert.equal(t.keys.buttons.get('ctrl').getAttribute('aria-pressed'), 'true');
  assert.equal(t.keys.transform('c'), '\x03'); assert.equal(t.keys.ctrl, false); assert.equal(t.keys.transform('x'), 'x');
  t.keys.press('alt'); assert.equal(t.keys.transform('x'), '\x1bx');
  t.keys.press('ctrl'); t.keys.press('alt'); assert.equal(t.keys.transform('a'), '\x1b\x01');
  t.keys.press('ctrl'); t.keys.press('ctrl'); assert.equal(t.keys.transform('a'), 'a');
});

test('navigation keys consume modifiers once without sending modifier keys alone', () => {
  const t = fixture(); t.focus(); t.keyboard(650);
  t.keys.press('ctrl'); assert.deepEqual(t.sent, []); t.keys.press('left'); assert.deepEqual(t.sent, ['\x1b[1;5D']);
  assert.equal(t.keys.ctrl, false); t.term.modes.applicationCursorKeysMode = true; t.keys.press('up');
  assert.equal(t.sent.at(-1), '\x1bOA'); t.keys.press('home'); assert.equal(t.sent.at(-1), '\x1bOH');
  t.keys.press('end'); assert.equal(t.sent.at(-1), '\x1bOF');
  t.keys.press('ctrl'); t.keys.press('home'); assert.equal(t.sent.at(-1), '\x1b[1;5H');
  t.keys.press('delete'); assert.equal(t.sent.at(-1), '\x1b[3~');
});

test('mouse/protocol reports and paste/IME words never become control commands', () => {
  const t = fixture(); t.focus(); t.keyboard(650); t.keys.press('ctrl');
  assert.equal(t.keys.transform('\x1b[<64;10;5M'), '\x1b[<64;10;5M'); assert.equal(t.keys.ctrl, true);
  assert.equal(t.keys.transform('\x1b[?1;2c'), '\x1b[?1;2c'); assert.equal(t.keys.ctrl, true);
  assert.equal(t.keys.transform('pasted word'), 'pasted word'); assert.equal(t.keys.ctrl, false);
  t.keys.press('alt'); const pasted = '\x1b[200~paste\x1b[201~'; assert.equal(t.keys.transform(pasted), pasted); assert.equal(t.keys.alt, false);
  t.keys.press('ctrl'); t.doc.fire('paste'); assert.equal(t.keys.transform('c'), 'c');
  t.keys.press('alt'); assert.equal(t.keys.transform('\x1b[D'), '\x1b[1;3D'); assert.equal(t.keys.alt, false);
  t.keys.press('ctrl'); assert.equal(t.keys.transform('\x1b[1~'), '\x1b[1;5H');
  t.keys.press('alt'); assert.equal(t.keys.transform('\x1bOF'), '\x1b[1;3F');
});

test('key taps suppress focus loss and connection/copy/tab changes clear modifiers', () => {
  const t = fixture(); t.focus(); t.keyboard(650);
  let prevented = false; t.keys.buttons.get('ctrl').fire('pointerdown', {preventDefault() { prevented = true; }});
  assert.equal(prevented, true); assert.equal(t.doc.activeElement, t.input);
  t.keys.press('alt'); t.state.ready = false; t.keys.update(); assert.equal(t.keys.alt, false);
  assert.equal(t.keys.buttons.get('up').disabled, true); t.keys.press('up'); assert.equal(t.sent.length, 0);
  t.state.ready = true; t.keys.update(); t.keys.press('ctrl'); t.state.active = false; t.keys.update();
  assert.equal(t.keys.bar.hidden, true); assert.equal(t.keys.ctrl, false); assert.equal(t.pane.style.bottom, '');
});

test('normal inputs/copy overlay focus cannot show a terminal key bar', () => {
  const t = fixture(); t.focus(); t.keyboard(650);
  t.doc.activeElement = new Element(t.doc); t.doc.fire('focusout'); assert.equal(t.keys.bar.hidden, true);
});

test('VirtualKeyboard geometry supports overlays and visual viewport offsets', () => {
  const t = fixture(); t.focus(); t.vk.boundingRect = {height: 300, top: 700}; t.vk.fire('geometrychange');
  assert.equal(t.keys.bar.hidden, false); assert.equal(t.keys.bar.style.top, '652px');
  t.vk.boundingRect.height = 0; t.view.visualViewport.height = 650; t.view.visualViewport.offsetTop = 40; t.view.visualViewport.offsetLeft = 5;
  t.view.visualViewport.fire('scroll'); assert.equal(t.keys.bar.style.top, '642px'); assert.equal(t.keys.bar.style.left, '24.5px');
});

test('resized-layout keyboards share their original baseline across terminal instances', () => {
  const t = fixture({viewport: false}); t.focus(); t.keyboard(650); assert.equal(t.keys.visible, true);
  const other = new TerminalKeys({term: t.term, pane: new Element(t.doc), media: t.media, isActive: () => true, ready: () => true, send() {}});
  assert.equal(other.visible, true); other.dispose();
  t.keyboard(1000); assert.equal(t.keys.visible, false);
});

test('pinch zoom is not mistaken for a keyboard and orientation rebases height', () => {
  const t = fixture(); t.focus(); t.view.visualViewport.scale = 2; t.keyboard(500); assert.equal(t.keys.visible, false);
  t.view.visualViewport.scale = 1; t.keyboard(650); assert.equal(t.keys.visible, true);
  t.view.innerWidth = 844; t.view.innerHeight = 390; t.doc.documentElement.clientHeight = 390;
  Object.assign(t.view.visualViewport, {width: 844, height: 390}); t.view.fire('orientationchange'); assert.equal(t.keys.visible, false);
  t.keyboard(200); assert.equal(t.keys.visible, true);
});

test('disposal removes all listeners and restores pane space without sending anything', () => {
  const t = fixture(); t.focus(); t.keyboard(650); t.keys.press('ctrl'); t.keys.dispose();
  assert.equal(t.keys.bar.removed, true); assert.equal(t.pane.style.bottom, ''); assert.equal(t.keys.ctrl, false);
  assert.equal(t.doc.events.get('focusin').size, 0); assert.equal(t.view.visualViewport.events.get('resize').size, 0);
  assert.equal(t.sent.length, 0);
});

test('all eleven keys stay ordered in a centered 90-percent bar and labels fit their cells', () => {
  const t = fixture(); t.focus(); t.keyboard(650);
  assert.deepEqual([...t.keys.labels.values()].map(label => label.textContent), ['Esc', 'Tab', 'Ctrl', 'Alt', '←', '↑', '↓', '→', 'Home', 'End', 'Sup']);
  assert.equal(t.keys.bar.style.width, '351px'); assert.equal(t.keys.bar.style.left, '19.5px');
  const label = t.keys.labels.get('home'), button = t.keys.buttons.get('home');
  label.scrollWidth = 40; button.clientWidth = 24; t.keys.fitLabels();
  assert.equal(label.style.transform, 'scaleX(0.5)');
  button.clientWidth = 80; t.keys.fitLabels(); assert.equal(label.style.transform, 'scaleX(1)');
  t.keys.press('tab'); assert.equal(t.sent.at(-1), '\t');
});
