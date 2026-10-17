const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {TerminalTouchScroll, scrollTerminal, supportsScroll} = require('../../src/swarm/static/terminal-touch.js');

class Element {
  constructor() { this.events = new Map(); }
  addEventListener(type, callback, options) { this.events.set(type, {callback, options}); }
  removeEventListener(type, callback) { if (this.events.get(type)?.callback === callback) this.events.delete(type); }
}
function event(x, y, count = 1, timeStamp = 10) {
  return {touches: Array.from({length: count}, () => ({clientX: x, clientY: y})), timeStamp,
    cancelable: true, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
}
function fixture() {
  const element = new Element(), wheels = [], gestures = [];
  const controller = new TerminalTouchScroll({element, wheel: (direction, point) => wheels.push({direction, point}), onGesture: () => gestures.push(true)});
  return {controller, element, wheels, gestures};
}

test('vertical swipes emit real wheel detents with correct direction and fractional accumulation', () => {
  const t = fixture(); t.controller.start(event(40, 100));
  const first = event(42, 117); t.controller.move(first);
  assert.equal(first.prevented, true); assert.equal(first.stopped, true); assert.equal(t.wheels.length, 0);
  t.controller.move(event(42, 150));
  assert.deepEqual(t.wheels.map(w => w.direction), [-1, -1]);
  assert.deepEqual(t.wheels[0].point, {x: 40, y: 100});
  t.controller.move(event(41, 100));
  assert.deepEqual(t.wheels.map(w => w.direction), [-1, -1, 1, 1]);
  assert.equal(t.gestures.length, 1);
});

test('taps, horizontal/back gestures, long-press selection and pinch/zoom are preserved', () => {
  const t = fixture();
  t.controller.start(event(20, 100)); const tap = event(21, 103); t.controller.move(tap);
  assert.equal(tap.prevented, undefined);
  t.controller.start(event(20, 100)); t.controller.move(event(70, 104)); t.controller.move(event(70, 180));
  t.controller.start(event(20, 100, 1, 10)); t.controller.move(event(20, 180, 1, 600));
  t.controller.start(event(20, 100, 2)); t.controller.move(event(20, 180, 2));
  t.controller.start(event(20, 100)); t.controller.move(event(20, 180, 2));
  assert.equal(t.wheels.length, 0); assert.equal(t.gestures.length, 0);
});

test('touchend/cancel/disconnect stop gesture state and fast moves are bounded', () => {
  const t = fixture(); t.controller.start(event(10, 100)); t.controller.move(event(10, 10000));
  assert.equal(t.wheels.length, 12);
  t.element.events.get('touchend').callback(); assert.equal(t.controller.gesture, null);
  t.controller.start(event(10, 100)); t.controller.canScroll = () => false;
  t.controller.move(event(10, 200)); assert.equal(t.wheels.length, 12);
  t.controller.canScroll = () => true; t.controller.start(event(10, 100));
  t.element.events.get('touchcancel').callback(); assert.equal(t.controller.gesture, null);
});

test('touchmove is non-passive capture and disposed handlers cannot emit events', () => {
  const t = fixture(); assert.equal(t.element.events.get('touchmove').options.passive, false);
  assert.equal(t.element.events.get('touchmove').options.capture, true);
  t.controller.dispose(); assert.equal(t.element.events.size, 0);
  t.controller.start(event(10, 100)); t.controller.move(event(10, 300)); assert.equal(t.wheels.length, 0);
});

test('xterm encodes the mouse protocol; coordinates are clamped and no keys are injected', () => {
  const reports = [];
  class WheelEvent { constructor(type, init) { Object.assign(this, {type}, init); } }
  const target = {ownerDocument: {defaultView: {WheelEvent}},
    getBoundingClientRect: () => ({left: 10, top: 20, right: 210, bottom: 320, width: 200, height: 300}),
    dispatchEvent: report => { reports.push(report); return false; }};
  const term = {modes: {mouseTrackingMode: 'vt200'}, element: {querySelector: () => target}, input: () => assert.fail('never synthesize editor keys')};
  assert.equal(supportsScroll(term), true);
  assert.equal(scrollTerminal(term, -1, {x: 999, y: -10}), true);
  assert.equal(reports[0].type, 'wheel'); assert.equal(reports[0].deltaY, -1); assert.equal(reports[0].deltaMode, 1);
  assert.equal(reports[0].clientX, 209); assert.equal(reports[0].clientY, 21);
  assert.equal(reports[0].bubbles, true); assert.equal(reports[0].cancelable, true);
});

test('mouse-free alternate screens cannot accidentally receive Up/Down editor keys', () => {
  const moved = [], term = {modes: {mouseTrackingMode: 'none'}, buffer: {active: {type: 'alternate'}}, scrollLines: lines => moved.push(lines), input: () => assert.fail('no keyboard fallback')};
  assert.equal(supportsScroll(term), false); assert.equal(scrollTerminal(term, -1, {x: 0, y: 0}), false);
  term.buffer.active.type = 'normal'; assert.equal(supportsScroll(term), true);
  assert.equal(scrollTerminal(term, -1, {x: 0, y: 0}), true); assert.deepEqual(moved, [-1]);
});

test('actual terminal input pipeline forwards generated mouse reports and cleans up touch listeners', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  let inputHandler, settings;
  class Terminal {
    constructor() { this.cols = 80; this.rows = 24; }
    loadAddon() {} open() {} onResize() {} blur() {} onData(fn) { inputHandler = fn; }
  }
  const sent = [], t = {screen: {}, pane: {hidden: false}, ws: {readyState: 1, send: raw => sent.push(JSON.parse(raw))}};
  const context = vm.createContext({Terminal, FitAddon: {FitAddon: class {fit() {}}}, WebSocket: {OPEN: 1}, ResizeObserver: class {observe() {}}, attachTerminal() {},
    SwarmTerminalCopy: {TerminalCopy: class {constructor() { this.active = false; }}},
    SwarmTerminalTouch: {TerminalTouchScroll: class {constructor(options) { settings = options; }}, supportsScroll: () => true, scrollTerminal: () => inputHandler('\u001b[<64;10;5M')}});
  vm.runInContext(source.slice(source.indexOf('function initTerminal('), source.indexOf('function overlay(')), context);
  context.initTerminal(t); settings.wheel(-1, {x: 20, y: 30});
  assert.equal(sent[0].type, 'input'); assert.equal(sent[0].data, '\u001b[<64;10;5M');
  assert.equal(settings.canScroll(), true);
  t.copyText.active = true; inputHandler('not while copying'); assert.equal(sent.length, 1); assert.equal(settings.canScroll(), false);
  t.copyText.active = false; t.closed = true; assert.equal(settings.canScroll(), false);
  assert.match(source, /t\.touchScroll\?\.dispose\(\)/);
  assert.doesNotMatch(source, /terminal-history|SwarmTerminalHistory/);
});

test('new frontend loads before app and has no history API or snapshot UI dependency', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  assert.ok(html.indexOf('/static/terminal-touch.js') < html.indexOf('/static/app.js'));
  const css = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.css'), 'utf8');
  assert.match(css, /\.terminal-screen\{touch-action:pan-x pinch-zoom\}/);
  assert.doesNotMatch(html, /terminal-history/);
});

test('pointer capture starts only on a real drag and survives replaced xterm rows', () => {
  const element = new Element(), captured = new Set(), wheels = [];
  element.setPointerCapture = id => captured.add(id);
  element.hasPointerCapture = id => captured.has(id);
  element.releasePointerCapture = id => captured.delete(id);
  const controller = new TerminalTouchScroll({element, pointerEvents: true, wheel: direction => wheels.push(direction)});
  const pointer = (y, id = 7) => ({pointerType: 'touch', pointerId: id, clientX: 40, clientY: y, timeStamp: 10,
    cancelable: true, preventDefault() {}, stopPropagation() {}});
  controller.pointerStart(pointer(100)); assert.equal(captured.size, 0); // Taps retain their normal focus target.
  controller.pointerMove(pointer(130)); assert.ok(captured.has(7));
  element.events.get('lostpointercapture').callback({target: new Element(), pointerId: 7});
  assert.ok(controller.gesture); // Implicit capture lost by an old row is not our root losing capture.
  controller.pointerMove(pointer(210)); assert.equal(wheels.length, 4);
  controller.pointerEnd(pointer(210)); assert.equal(captured.size, 0); assert.equal(controller.contacts.size, 0);
  controller.dispose(); assert.equal(element.events.size, 0);
});

test('a second pointer cancels capture without blocking native pinch', () => {
  const element = new Element(), captured = new Set(), wheels = [];
  element.setPointerCapture = id => captured.add(id); element.hasPointerCapture = id => captured.has(id); element.releasePointerCapture = id => captured.delete(id);
  const controller = new TerminalTouchScroll({element, pointerEvents: true, wheel: direction => wheels.push(direction)});
  const pointer = (id, y) => ({pointerType: 'touch', pointerId: id, clientX: 40, clientY: y, timeStamp: 10,
    cancelable: true, preventDefault() { this.prevented = true; }, stopPropagation() {}});
  controller.pointerStart(pointer(1, 100)); controller.pointerMove(pointer(1, 130));
  controller.pointerStart(pointer(2, 110)); const moved = pointer(1, 220); controller.pointerMove(moved);
  assert.equal(captured.size, 0); assert.equal(moved.prevented, undefined); assert.equal(wheels.length, 1);
  controller.pointerEnd(pointer(2, 110)); controller.pointerEnd(pointer(1, 220));
  assert.equal(controller.contacts.size, 0);
});

test('legacy TouchEvent fallback follows an original row even after it is detached', () => {
  const element = new Element(), row = new Element(), wheels = [];
  const controller = new TerminalTouchScroll({element, pointerEvents: false, wheel: direction => wheels.push(direction)});
  const started = event(30, 100); started.target = row; controller.start(started);
  controller.move(event(30, 130));
  // Simulate the row's removal: subsequent events no longer bubble to the root.
  row.events.get('touchmove').callback(event(30, 210));
  assert.equal(wheels.length, 4);
  row.events.get('touchend').callback(); assert.equal(row.events.size, 0);
  assert.equal(controller.gesture, null);
});

test('unexpected lost root capture cannot leave a contact that blocks the next swipe', () => {
  const element = new Element(), captured = new Set(), wheels = [];
  element.setPointerCapture = id => captured.add(id); element.hasPointerCapture = id => captured.has(id); element.releasePointerCapture = id => captured.delete(id);
  const controller = new TerminalTouchScroll({element, pointerEvents: true, wheel: direction => wheels.push(direction)});
  const pointer = (id, y) => ({pointerType: 'touch', pointerId: id, clientX: 40, clientY: y, timeStamp: 10,
    cancelable: true, preventDefault() {}, stopPropagation() {}});
  controller.pointerStart(pointer(1, 100)); controller.pointerMove(pointer(1, 130));
  element.events.get('lostpointercapture').callback({target: element, pointerId: 1});
  assert.equal(controller.contacts.size, 0); assert.equal(controller.gesture, null);
  controller.pointerStart(pointer(2, 100)); controller.pointerMove(pointer(2, 160));
  assert.equal(wheels.length, 3);
});
