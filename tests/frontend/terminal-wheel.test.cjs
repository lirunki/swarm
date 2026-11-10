const test = require('node:test');
const assert = require('node:assert/strict');
const {
  WheelPreferences, WheelSettingsController, TerminalWheelMultiplier, validMultiplier,
} = require('../../src/swarm/static/terminal-wheel.js');

const ESC = '\x1b';
const sgr = (button = 64, column = 10, row = 5) => `${ESC}[<${button};${column};${row}M`;
const x10 = (button = 64, column = 10, row = 5) => `${ESC}[M${String.fromCharCode(button + 32, column + 32, row + 32)}`;

class Element {
  constructor() { this.events = new Map(); this.value = ''; this.textContent = ''; }
  addEventListener(type, callback, options) { this.events.set(type, {callback, options}); }
  removeEventListener(type, callback) { if (this.events.get(type)?.callback === callback) this.events.delete(type); }
  dispatch(type, event) { return this.events.get(type)?.callback(event); }
}

function wheelEvent(changes = {}) {
  let prevented = false, stopped = false;
  return Object.assign({isTrusted: true, deltaY: -100, deltaX: 0, ctrlKey: false, metaKey: false,
    preventDefault() { prevented = true; }, stopPropagation() { stopped = true; },
    altered: () => ({prevented, stopped})}, changes);
}

function fixture({count = 3, mode = 'vt200', canScale = () => true, customHook = false} = {}) {
  const element = new Element(), jobs = [];
  const term = {modes: {mouseTrackingMode: mode}, customHookCalls: 0};
  if (customHook) term.attachCustomWheelEventHandler = callback => {
    term.customHookCalls += 1; term.customWheelHandler = callback;
  };
  const wheel = new TerminalWheelMultiplier({element, term, canScale, multiplier: () => count, queueMicrotask: callback => jobs.push(callback)});
  const capture = event => customHook ? term.customWheelHandler(event) : element.dispatch('wheel', event);
  const flush = () => { while (jobs.length) jobs.shift()(); };
  return {element, term, wheel, jobs, capture, flush};
}

for (const count of [1, 3, 20]) test(`SGR wheel reports use multiplier ${count}`, () => {
  const t = fixture({count}); const event = wheelEvent(); t.capture(event);
  assert.equal(t.wheel.transform(sgr(64)), sgr(64).repeat(count));
  assert.deepEqual(event.altered(), {prevented: false, stopped: false});
});

test('SGR reports scale only for the captured direction with supported modifiers', () => {
  const t = fixture();
  for (const [button, deltaY] of [[64, -100], [65, 100], [68, -100], [73, 100], [80, -100], [92, -100]]) {
    t.capture(wheelEvent({deltaY}));
    assert.equal(t.wheel.transform(sgr(button)), sgr(button).repeat(3));
  }
  for (const button of [0, 1, 32, 35, 66, 67, 64 | 2, 4294967360]) {
    t.capture(wheelEvent()); assert.equal(t.wheel.transform(sgr(button)), sgr(button));
  }
  for (const frame of [`${ESC}[<64;0;5M`, `${ESC}[<64;10;65536M`, `${ESC}[<64;10;5m`]) {
    t.capture(wheelEvent()); assert.equal(t.wheel.transform(frame), frame);
  }
});

test('X10 reports require the captured direction and bytes and coordinates from 33 through 255', () => {
  const t = fixture();
  for (const [button, deltaY] of [[64, -100], [65, 100], [68, -100], [73, 100], [92, -100]]) {
    t.capture(wheelEvent({deltaY}));
    assert.equal(t.wheel.transform(x10(button)), x10(button).repeat(3));
  }
  for (const frame of [x10(0), x10(32), x10(66), x10(67),
    `${ESC}[M${String.fromCharCode(96, 32, 37)}`,
    `${ESC}[M${String.fromCharCode(96, 256, 37)}`,
    x10(64, 224, 5)]) {
    t.capture(wheelEvent()); assert.equal(t.wheel.transform(frame), frame);
  }
  const boundary = x10(64, 223, 223);
  t.capture(wheelEvent()); assert.equal(t.wheel.transform(boundary), boundary.repeat(3));
});

test('only packets made entirely of complete matching wheel reports are scaled', () => {
  const t = fixture(), a = sgr(64, 2, 3), b = x10(68, 7, 8);
  t.capture(wheelEvent());
  assert.equal(t.wheel.transform(`${a}${b}`), `${a.repeat(3)}${b.repeat(3)}`);

  for (const packet of [
    `raw:${a}:key:${b}:tail`, `keyboard text ${ESC}[<64;2;3`, `${ESC}[A`,
    `paste ${a}`, `${a}paste`, `${a}${sgr(65)}`,
    `${ESC}[M${String.fromCharCode(96, 0x100, 37)}`,
  ]) {
    t.capture(wheelEvent()); assert.equal(t.wheel.transform(packet), packet);
  }
});

test('the first transform attempt consumes context regardless of its contents or direction', () => {
  const t = fixture(), up = sgr(64), down = sgr(65);
  t.capture(wheelEvent());
  assert.equal(t.wheel.transform(down), down, 'an opposite-direction report is not scaled');
  assert.equal(t.wheel.transform(up), up, 'the rejected report still consumed the context');

  t.capture(wheelEvent());
  assert.equal(t.wheel.transform('keyboard or paste data'), 'keyboard or paste data');
  assert.equal(t.wheel.transform(up), up, 'non-mouse data also consumes the context');

  t.capture(wheelEvent());
  assert.equal(t.wheel.transform('😀文'), '😀文', 'non-X10 Unicode is ordinary input');
  assert.equal(t.wheel.transform(up), up);

  const mixed = `paste:${up}:tail`;
  t.capture(wheelEvent()); assert.equal(t.wheel.transform(mixed), mixed);
  assert.equal(t.wheel.transform(up), up, 'a mixed packet cannot leave context for later data');
});

test('capture is passive, never modifies native dispatch, and context ends in its microtask', () => {
  const t = fixture();
  assert.deepEqual(t.element.events.get('wheel').options, {capture: true, passive: true});
  const event = wheelEvent(); t.capture(event);
  assert.equal(t.wheel.transform(sgr()), sgr().repeat(3));
  assert.deepEqual(event.altered(), {prevented: false, stopped: false});
  t.flush(); assert.equal(t.wheel.transform(sgr()), sgr());
});

test('xterm public wheel hook captures once and emits one transformed data packet', () => {
  const t = fixture({customHook: true}), sent = [];
  const onData = data => sent.push(t.wheel.transform(data));
  assert.equal(t.term.customHookCalls, 1);
  assert.equal(t.element.events.has('wheel'), false, 'the DOM capture fallback is not also installed');
  assert.equal(t.capture(wheelEvent()), true);
  onData(sgr(64, 10, 5)); onData('a');
  assert.deepEqual(sent, [sgr(64, 10, 5).repeat(3), 'a']);
  assert.equal(sent.length, 2);
  assert.equal(sent[0].split(sgr(64, 10, 5)).length - 1, 3, 'the original xterm report is not dispatched again');
  t.flush(); onData(sgr(64, 10, 5));
  assert.equal(sent.at(-1), sgr(64, 10, 5), 'the context clears after xterm returns to the microtask checkpoint');
});

test('a subsequent public-hook capture replaces the pending context without DOM listeners', () => {
  const t = fixture({customHook: true});
  t.capture(wheelEvent());
  t.capture(wheelEvent({deltaY: 100}));
  assert.equal(t.element.events.has('wheel'), false);
  assert.equal(t.term.customHookCalls, 1);
  assert.equal(t.wheel.transform(sgr(65)), sgr(65).repeat(3));
  assert.equal(t.wheel.transform(sgr(65)), sgr(65));
});

test('control-wheel returns through the public hook without creating scale context', () => {
  const t = fixture({customHook: true});
  assert.equal(t.capture(wheelEvent({ctrlKey: true})), true);
  assert.equal(t.wheel.transform(sgr(80)), sgr(80));
  assert.equal(t.wheel.transform(sgr(64)), sgr(64));
});

test('custom hook blocks trusted vertical wheels from xterm alternate-buffer cursor-key fallback', () => {
  for (const count of [1, 3]) {
    const t = fixture({count, mode: 'none', canScale: () => false, customHook: true});
    t.term.buffer = {active: {type: 'alternate'}};
    assert.equal(t.capture(wheelEvent()), false, 'blocking is independent of multiplier and scaling availability');
    assert.equal(t.capture(wheelEvent({ctrlKey: true})), false, 'browser zoom remains native without xterm cursor keys');
    assert.equal(t.capture(wheelEvent({metaKey: true})), false);
    assert.equal(t.capture(wheelEvent({isTrusted: false})), true, 'synthetic touch wheels remain unsupported by the blocker');
    assert.equal(t.capture(wheelEvent({deltaX: 100, deltaY: 0})), true);
    t.term.buffer.active.type = 'normal';
    assert.equal(t.capture(wheelEvent()), true);
  }
});

test('disposing a custom-hook multiplier clears context and restores native xterm wheel handling', () => {
  const t = fixture({customHook: true});
  const ownedHandler = t.term.customWheelHandler;
  t.capture(wheelEvent()); assert.equal(t.wheel.transform(sgr()), sgr().repeat(3));
  t.wheel.dispose();
  assert.notEqual(t.term.customWheelHandler, ownedHandler);
  assert.equal(t.term.customWheelHandler(wheelEvent()), true);
  assert.equal(t.wheel.transform(sgr()), sgr());
});

test('synthetic touch wheels and ineligible browser wheels cannot inherit or create scale context', () => {
  const cases = [
    {isTrusted: false}, {ctrlKey: true}, {metaKey: true}, {deltaY: 0}, {deltaY: NaN},
    {deltaX: 100}, {deltaX: -200},
  ];
  for (const changes of cases) {
    const t = fixture(); t.capture(wheelEvent(changes)); assert.equal(t.wheel.transform(sgr()), sgr());
  }
  const nested = fixture(); nested.capture(wheelEvent());
  nested.capture(wheelEvent({isTrusted: false}));
  assert.equal(nested.wheel.transform(sgr()), sgr(), 'TerminalTouchScroll synthetic event clears a surrounding native context');
});

test('mouse-free normal/alternate screens, copy mode, and disconnects leave xterm data untouched', () => {
  for (const bufferType of ['normal', 'alternate']) {
    const t = fixture({mode: 'none'}); t.term.buffer = {active: {type: bufferType}};
    t.capture(wheelEvent()); assert.equal(t.wheel.transform(sgr()), sgr());
  }
  let available = true;
  const t = fixture({canScale: () => available}); t.capture(wheelEvent());
  available = false; assert.equal(t.wheel.transform(sgr()), sgr(), 'canScale is checked again if copy/disconnect starts mid-wheel');
  available = true; assert.equal(t.wheel.transform(sgr()), sgr(), 'the failed first attempt consumed the context');
});

test('event snapshot, stale cleanup, and dispose preserve newer contexts', () => {
  let count = 3; const t = fixture({count});
  t.wheel.multiplier = () => count;
  t.capture(wheelEvent()); count = 20;
  assert.equal(t.wheel.transform(sgr()), sgr().repeat(3));
  const firstCleanup = t.jobs.shift(); t.capture(wheelEvent());
  firstCleanup(); assert.equal(t.wheel.transform(sgr()), sgr().repeat(20));
  t.wheel.dispose(); assert.equal(t.element.events.has('wheel'), false);
  assert.equal(t.wheel.transform(sgr()), sgr());
  for (const cleanup of t.jobs) cleanup(); assert.equal(t.wheel.context, null);
});

test('invalid multipliers and oversized transformed packets safely return the original string', () => {
  for (const count of [0, 1.5, 21, '3', NaN]) {
    const t = fixture({count}); t.capture(wheelEvent()); assert.equal(t.wheel.transform(sgr()), sgr());
  }
  const t = fixture({count: 20}), report = sgr();
  const data = report.repeat(Math.floor(131072 / Buffer.byteLength(report)));
  assert.ok(Buffer.byteLength(data) <= 131072);
  t.capture(wheelEvent());
  const result = t.wheel.transform(data);
  assert.equal(result, data); assert.equal(result.endsWith(report), true);
});

test('validMultiplier accepts only strict integer values from 1 through 20', () => {
  for (const value of [1, 2, 20]) assert.equal(validMultiplier(value), true);
  for (const value of [0, 21, 1.5, '1', null, NaN, Infinity]) assert.equal(validMultiplier(value), false);
});

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial)), writes = [];
  return {values, writes, getItem: key => values.has(key) ? values.get(key) : null,
    setItem(key, value) { writes.push([key, value]); values.set(key, value); }};
}

test('preferences load, persist, notify, and strictly validate the public value', () => {
  const key = 'swarm.terminal-wheel-multiplier', storage = memoryStorage({[key]: '3'});
  const preferences = new WheelPreferences({storage}); const changes = [];
  const unsubscribe = preferences.subscribe(value => changes.push(value));
  assert.equal(preferences.value, 3); assert.equal(preferences.set(20), 20);
  assert.deepEqual(storage.writes, [[key, '20']]); assert.equal(preferences.value, 20); assert.deepEqual(changes, [20]);
  assert.throws(() => preferences.set('3'), /integer from 1 to 20/);
  assert.throws(() => preferences.set(0), /integer from 1 to 20/);
  assert.equal(preferences.value, 20); unsubscribe(); preferences.set(2); assert.deepEqual(changes, [20]);
});

test('invalid or unreadable saved preferences safely reload to one', () => {
  const key = 'swarm.terminal-wheel-multiplier';
  for (const stored of [null, '', '0', '21', '1.5', '03', ' 3 ', 'true']) {
    assert.equal(new WheelPreferences({storage: memoryStorage(stored === null ? {} : {[key]: stored})}).value, 1);
  }
  const throwing = {getItem() { throw new Error('blocked'); }, setItem() {}};
  assert.equal(new WheelPreferences({storage: throwing}).reload(), 1);
});

test('failed or unavailable storage writes throw without silently applying the value', () => {
  const blocked = {getItem: () => '3', setItem() { throw new Error('quota denied'); }};
  const preferences = new WheelPreferences({storage: blocked});
  assert.throws(() => preferences.set(5), /quota denied/); assert.equal(preferences.value, 3);
  const unavailable = new WheelPreferences({storage: null});
  assert.throws(() => unavailable.set(4), /storage is unavailable/i); assert.equal(unavailable.value, 1);
});

function settingsFixture(storage = memoryStorage()) {
  const nodes = Object.fromEntries(['#mouse-wheel-multiplier', '#mouse-wheel-save', '#mouse-wheel-status'].map(id => [id, new Element()]));
  const errors = [], preferences = new WheelPreferences({storage});
  const controller = new WheelSettingsController({query: id => nodes[id], preferences, onError: error => errors.push(error)});
  return {nodes, errors, preferences, controller};
}

test('settings controller validates, saves live preferences, and preserves dirty edits during reload', () => {
  const key = 'swarm.terminal-wheel-multiplier', storage = memoryStorage({[key]: '3'}), t = settingsFixture(storage);
  const input = t.nodes['#mouse-wheel-multiplier'], save = t.nodes['#mouse-wheel-save'];
  assert.equal(input.value, '3'); assert.equal(input.min, '1'); assert.equal(input.max, '20'); assert.equal(input.step, '1');
  input.value = '7'; input.dispatch('input', {}); assert.equal(t.controller.dirty, true);
  storage.values.set(key, '9'); t.controller.reload(); assert.equal(t.preferences.value, 9); assert.equal(input.value, '7');
  save.dispatch('click', {preventDefault() { this.prevented = true; }});
  assert.equal(t.preferences.value, 7); assert.equal(input.value, '7'); assert.equal(t.controller.dirty, false);
  input.value = '1.5'; input.dispatch('input', {}); assert.equal(t.controller.save(), false);
  assert.equal(t.preferences.value, 7); assert.match(t.nodes['#mouse-wheel-status'].textContent, /integer from 1 to 20/);
  assert.equal(t.errors.length, 1);
});

test('opening settings safely reloads storage, failed saves retain the existing live multiplier, and dispose unbinds', () => {
  const key = 'swarm.terminal-wheel-multiplier', storage = memoryStorage({[key]: '4'}), t = settingsFixture(storage);
  const input = t.nodes['#mouse-wheel-multiplier']; input.value = '12'; input.dispatch('input', {});
  storage.values.set(key, '6'); assert.equal(t.controller.open(), 6); assert.equal(input.value, '6');
  storage.setItem = () => { throw new Error('storage blocked'); }; input.value = '8'; input.dispatch('input', {});
  assert.equal(t.controller.save(), false); assert.equal(t.preferences.value, 6); assert.match(t.nodes['#mouse-wheel-status'].textContent, /storage blocked/);
  t.controller.dispose(); assert.equal(input.events.has('input'), false); assert.equal(t.nodes['#mouse-wheel-save'].events.has('click'), false);
});
