const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {TerminalHistory} = require('../../src/swarm/static/terminal-history.js');

class Element {
  constructor() { this.hidden = false; this.textContent = ''; this.attributes = new Map(); this.events = new Map(); this.scrollTop = 0; this.scrollHeight = 2200; this.clientHeight = 200; this.focused = false; }
  addEventListener(name, callback, options) { this.events.set(name, {callback, options}); }
  removeEventListener(name, callback) { if (this.events.get(name)?.callback === callback) this.events.delete(name); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  focus() { this.focused = true; }
  click() { return this.events.get('click')?.callback(); }
}
function fixture(load = async () => ({text: 'retained output', lines: 1, truncated: false})) {
  const parts = Object.fromEntries(['screen', 'panel', 'text', 'status', 'toggleButton', 'refreshButton'].map(name => [name, new Element()]));
  parts.panel.id = 'history-one';
  const calls = [];
  const controller = new TerminalHistory({...parts, load: signal => { calls.push(['load', signal]); return load(signal); }, onEnter: () => calls.push(['enter']), onLive: () => calls.push(['live'])});
  return {controller, calls, ...parts};
}
function touch(x, y, count = 1) { return {touches: Array.from({length: count}, () => ({clientX: x, clientY: y})), cancelable: true, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }}; }
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };

test('History is read-only text, starts at newest output and exposes accessible live return', async () => {
  const t = fixture(async () => ({text: '<script>literal terminal data</script>', lines: 1, truncated: true}));
  await t.controller.enter();
  assert.equal(t.screen.hidden, true); assert.equal(t.panel.hidden, false); assert.equal(t.panel.focused, true);
  assert.equal(t.toggleButton.textContent, 'Live input'); assert.equal(t.toggleButton.getAttribute('aria-expanded'), 'true');
  assert.equal(t.toggleButton.getAttribute('aria-controls'), t.panel.id);
  assert.equal(t.text.textContent, '<script>literal terminal data</script>');
  assert.equal(t.panel.scrollTop, 2000); assert.match(t.status.textContent, /limited/);
  t.controller.live();
  assert.equal(t.screen.hidden, false); assert.equal(t.panel.hidden, true); assert.equal(t.status.hidden, true);
  assert.equal(t.refreshButton.hidden, true); assert.equal(t.toggleButton.textContent, 'History');
  assert.deepEqual(t.calls.map(call => call[0]), ['enter', 'load', 'live']);
});

test('vertical drag enters history and continues smoothly even while fetch is pending', async () => {
  let resolve;
  const t = fixture(() => new Promise(done => { resolve = done; }));
  t.controller.touchStart(touch(30, 100));
  const first = touch(31, 130); t.controller.touchMove(first);
  assert.equal(t.controller.active, true); assert.equal(t.controller.loading, true);
  assert.equal(first.prevented, true); assert.equal(first.stopped, true);
  t.controller.touchMove(touch(31, 160));
  resolve({text: 'old retained output', lines: 50}); await tick();
  assert.equal(t.panel.scrollTop, 1940); // Finger downward reveals older output, not editor keys.
  t.controller.touchMove(touch(31, 185)); assert.equal(t.panel.scrollTop, 1915);
  t.screen.events.get('touchend').callback(); assert.equal(t.controller.gesture, null);
  assert.equal(t.screen.events.get('touchmove').options.passive, false);
  assert.equal(t.screen.events.get('touchmove').options.capture, true);
});

test('taps, horizontal gestures and pinch gestures are not hijacked', () => {
  const t = fixture();
  t.controller.touchStart(touch(10, 100)); const tap = touch(11, 105); t.controller.touchMove(tap);
  assert.equal(t.controller.active, false); assert.equal(tap.prevented, undefined);
  t.controller.touchStart(touch(10, 100)); t.controller.touchMove(touch(50, 105)); t.controller.touchMove(touch(50, 200));
  assert.equal(t.controller.active, false);
  t.controller.touchStart(touch(10, 100, 2)); t.controller.touchMove(touch(10, 200, 2));
  assert.equal(t.controller.active, false);
  t.controller.touchStart(touch(10, 100)); t.controller.touchMove(touch(10, 120, 2));
  assert.equal(t.controller.gesture, null); assert.equal(t.calls.length, 0);
});

test('Live input aborts pending capture and ignores late history/errors', async () => {
  let resolve;
  const t = fixture(() => new Promise(done => { resolve = done; }));
  const pending = t.controller.enter(); const signal = t.calls.find(call => call[0] === 'load')[1];
  t.controller.live(); assert.equal(signal.aborted, true);
  resolve({text: 'late private output', lines: 1}); await pending;
  assert.notEqual(t.text.textContent, 'late private output'); assert.equal(t.screen.hidden, false);
  assert.equal(t.controller.loading, false);
});

test('closing a tab disposes handlers/requests without resuming, typing or changing pane mode', async () => {
  let resolve;
  const t = fixture(() => new Promise(done => { resolve = done; }));
  const pending = t.controller.enter(); t.controller.dispose();
  resolve({text: 'late', lines: 1}); await pending;
  assert.equal(t.screen.events.size, 0); assert.equal(t.toggleButton.events.size, 0);
  assert.equal(t.calls.some(call => call[0] === 'live'), false);
  assert.notEqual(t.text.textContent, 'late');
});

test('refresh preserves reading position, follows bottom only when already there, and coalesces requests', async () => {
  const t = fixture(); await t.controller.enter(); t.panel.scrollTop = 400;
  await t.controller.refresh(); assert.equal(t.panel.scrollTop, 400);
  t.panel.scrollTop = 2000; await t.controller.refresh(); assert.equal(t.panel.scrollTop, 2000);
  let resolve;
  t.controller.load = () => new Promise(done => { resolve = done; });
  const pending = t.controller.refresh(); await t.controller.refresh();
  assert.equal(t.refreshButton.disabled, true);
  resolve({text: 'new', lines: 1}); await pending;
  assert.equal(t.refreshButton.disabled, false);
});

test('errors retain previous snapshot and allow retry or Escape back to live', async () => {
  const t = fixture(); await t.controller.enter();
  t.controller.load = async () => { throw Error('pane is gone'); };
  await t.controller.refresh();
  assert.equal(t.text.textContent, 'retained output'); assert.match(t.status.textContent, /pane is gone/);
  assert.equal(t.refreshButton.disabled, false);
  let prevented = false;
  t.panel.events.get('keydown').callback({key: 'Escape', preventDefault() { prevented = true; }});
  assert.equal(prevented, true); assert.equal(t.controller.active, false);
});

test('malformed captures are handled as text errors and empty panes are explicit', async () => {
  const t = fixture(async () => ({text: ''})); await t.controller.enter();
  assert.match(t.text.textContent, /No retained/);
  t.controller.load = async () => ({text: {html: 'invalid'}});
  await t.controller.refresh(); assert.match(t.status.textContent, /Invalid terminal history response/);
});

test('switching back and reopening does not apply an older capture to the new view', async () => {
  const pending = [];
  const t = fixture(() => new Promise(done => pending.push(done)));
  const one = t.controller.enter(); t.controller.live(); const two = t.controller.enter();
  pending[1]({text: 'new snapshot', lines: 1}); await two;
  pending[0]({text: 'stale snapshot', lines: 1}); await one;
  assert.equal(t.text.textContent, 'new snapshot');
});

test('actual terminal input callback blocks typing only while history is active', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  let onData;
  class Terminal {
    constructor() { this.cols = 80; this.rows = 24; }
    loadAddon() {} open() {} onResize() {} onData(callback) { onData = callback; }
  }
  const sent = [], t = {screen: {}, pane: {hidden: false}, history: {active: true}, ws: {readyState: 1, send: message => sent.push(message)}};
  const context = vm.createContext({Terminal, FitAddon: {FitAddon: class {fit() {}}}, ResizeObserver: class {observe() {}}, WebSocket: {OPEN: 1}, attachTerminal() {}});
  vm.runInContext(source.slice(source.indexOf('function initTerminal('), source.indexOf('function overlay(')), context);
  context.initTerminal(t); onData('must not type'); assert.equal(sent.length, 0);
  t.history.active = false; onData('live typing'); assert.equal(JSON.parse(sent[0]).data, 'live typing');
});

test('integration captures workspace path once and disposes history with its terminal', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.css'), 'utf8');
  assert.match(source, /const historyPath = apiPath\(`/);
  assert.match(source, /localApi\(historyPath, 'GET', undefined, \{signal, cache: 'no-store'\}\)/);
  assert.match(source, /t\.history\?\.dispose\(\)/);
  assert.ok(html.indexOf('/static/terminal-history.js') < html.indexOf('/static/app.js'));
  assert.match(css, /\.terminal-history\{[^}]*overflow:auto;[^}]*touch-action:pan-y pinch-zoom/);
  assert.match(css, /\.terminal-history-text\{[^}]*white-space:pre-wrap/);
});
