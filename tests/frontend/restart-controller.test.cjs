const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');

function restartHarness(confirmResult = true, reject = false) {
  const calls = [];
  const restart = {disabled: false, textContent: '↻', attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }};
  const context = vm.createContext({
    restartingController: false,
    restartTimer: null,
    activeSwarm: 'local',
    federationNodes: new Map(),
    confirm: message => { calls.push(['confirm', message]); return confirmResult; },
    $: selector => { assert.equal(selector, '#restart-controller'); return restart; },
    api: async (url, method) => { calls.push(['api', url, method]); if (reject) throw Error('restart failed'); return {restarting: true}; },
    toast: (message, error) => calls.push(['toast', message, error]),
    setTimeout: (callback, delay) => { calls.push(['timeout', delay]); return {callback}; },
    clearTimeout: () => {},
  });
  const start = source.indexOf('async function restartController()');
  const end = source.indexOf("\n$('#restart-controller').addEventListener", start);
  const capability = source.slice(source.indexOf('function updateRestartCapability()'), source.indexOf('function renderSwarmSelector('));
  vm.runInContext(capability + '\n' + source.slice(start, end), context);
  context.updateRestartCapability();
  return {context, calls, restart};
}

test('restart control remains in the topbar beside live status', () => {
  const topMeta = html.slice(html.indexOf('<div class="top-meta">'), html.indexOf('</div>', html.indexOf('<div class="top-meta">')));
  assert.match(topMeta, /id="connection"[^>]*>Connecting…<\/span><button id="restart-controller"/);
  assert.ok(topMeta.indexOf('restart-controller') > topMeta.indexOf('connection'));
  assert.match(topMeta, /id="restart-controller"[^>]*aria-label="Restart backend"[^>]*>↻<\/button>/);
});

test('confirmed restart posts once, disables itself, and explains agent preservation', async () => {
  const harness = restartHarness();
  await harness.context.restartController();
  assert.deepEqual(harness.calls[0][0], 'confirm');
  assert.match(harness.calls[0][1], /Agent processes are owned by tmux and will keep running/);
  assert.deepEqual(harness.calls.find(call => call[0] === 'api'), ['api', '/api/controller/restart', 'POST']);
  assert.equal(harness.restart.disabled, true);
  assert.equal(harness.restart.textContent, '↻');
  assert.equal(harness.restart.attributes['aria-busy'], 'true');
  assert.equal(harness.restart.attributes['aria-label'], 'Restarting backend');
  assert.ok(harness.calls.some(call => call[0] === 'timeout' && call[1] === 20000));
});

test('cancel and request failure do not leave the restart button disabled', async () => {
  const cancelled = restartHarness(false); await cancelled.context.restartController();
  assert.equal(cancelled.calls.some(call => call[0] === 'api'), false);
  const failed = restartHarness(true, true); await failed.context.restartController();
  assert.equal(failed.restart.disabled, false);
  assert.equal(failed.restart.textContent, '↻');
  assert.equal(failed.restart.attributes['aria-busy'], 'false');
  assert.ok(failed.calls.some(call => call[0] === 'toast' && call[1] === 'restart failed' && call[2] === true));
});

test('state websocket reconnection clears restart state and reports success', () => {
  assert.match(source, /if \(restartingController\) \{\s*restartingController = false; clearTimeout\(restartTimer\);\s*updateRestartCapability\(\)/);
  assert.doesNotMatch(source, /textContent = '↻ Restart backend'|textContent = 'Restarting…'/);
  assert.match(source, /Swarm backend controller restarted\. Agents kept running\./);
});


test('restart stays icon-only after timeout and remote capability changes', async () => {
  const harness = restartHarness();
  await harness.context.restartController();
  await harness.context.restartController();
  assert.equal(harness.calls.filter(c => c[0] === 'api').length, 1);
  harness.context.restartTimer.callback();
  assert.equal(harness.restart.textContent, '↻');
  assert.equal(harness.restart.disabled, false);
  assert.equal(harness.restart.attributes['aria-busy'], 'false');
  harness.context.activeSwarm = 'peer';
  harness.context.updateRestartCapability();
  assert.equal(harness.restart.disabled, true);
  assert.equal(harness.restart.textContent, '↻');
  harness.context.federationNodes.set('peer', {capabilities: {remote_restart: true}});
  harness.context.updateRestartCapability();
  assert.equal(harness.restart.disabled, false);
});
