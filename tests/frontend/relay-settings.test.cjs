const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {RelaySettingsDialog} = require('../../src/swarm/static/relay-settings.js');
const KEY = 'frontend-test-key-0123456789-abcdef';

class Element {
  constructor() { this.value = ''; this.textContent = ''; this.disabled = false; this.events = new Map(); this.classes = new Set(); }
  get classList() { return {toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)}; }
  addEventListener(name, callback) { this.events.set(name, callback); }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.get('close')?.(); }
}
function initial(changes = {}) {
  return {relay_url: 'wss://relay.example:9443/v1/connect', relay_node_id: 'office-a', relay_name: 'Office A',
    enabled: false, connected: false, status: 'disconnected', has_api_key: false, last_error: null, ...changes};
}
function fixture(profile = initial(), customApi) {
  const ids = ['settings-dialog', 'relay-settings-form', 'relay-settings-fields', 'relay-connect', 'settings-button', 'relay-settings-error', 'relay-settings-status', 'relay-settings-connection-error'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, new Element()]));
  const form = elements['#relay-settings-form']; form.elements = Object.fromEntries(['relay_url', 'relay_node_id', 'relay_name', 'relay_api_key'].map(name => [name, new Element()]));
  form.reportValidity = () => form.valid !== false;
  const calls = [], changes = [], timers = new Map(); let nextTimer = 0;
  const api = async (url, method, body) => {
    calls.push({url, method, body});
    if (customApi) return customApi(url, method, body);
    if (url.endsWith('/connect')) profile = initial({...profile, ...body, has_api_key: true, enabled: true, connected: false, status: 'connecting'});
    if (url.endsWith('/disconnect')) profile = initial({...profile, enabled: false, connected: false, status: 'disconnected'});
    const {relay_api_key, ...redacted} = profile;
    return {...redacted};
  };
  const controller = new RelaySettingsDialog({query: selector => elements[selector], api,
    onChange: async value => changes.push(value), schedule: callback => { timers.set(++nextTimer, callback); return nextTimer; }, cancel: id => timers.delete(id)});
  return {controller, elements, form, calls, changes, timers, action: elements['#relay-connect'], dialog: elements['#settings-dialog']};
}

test('Settings loads local redacted profile with empty password and Connect action', async () => {
  const t = fixture(); await t.controller.open();
  assert.equal(t.dialog.open, true); assert.equal(t.action.textContent, 'Connect'); assert.equal(t.action.disabled, false);
  assert.equal(t.form.elements.relay_node_id.value, 'office-a');
  assert.equal(t.form.elements.relay_api_key.value, ''); assert.equal(t.form.elements.relay_api_key.required, true);
  assert.equal(t.calls[0].url, '/api/federation/settings');
  assert.equal(t.elements['#relay-settings-fields'].disabled, false);
  assert.equal(t.elements['#relay-settings-status'].textContent, 'Disconnected');
  assert.ok(t.elements['#settings-button'].events.has('click') && t.form.events.has('submit'));
});

test('Settings click uses native timer defaults without a dialog receiver', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/relay-settings.js'), 'utf8');
  const scheduled = [], cancelled = [];
  const sandbox = {
    module: {exports: {}},
    setTimeout: function (callback, delay) {
      'use strict';
      assert.equal(this, undefined, 'a browser timer must not receive the dialog as this');
      scheduled.push({callback, delay}); return scheduled.length;
    },
    clearTimeout: function (timer) {
      'use strict';
      assert.equal(this, undefined, 'a browser timer must not receive the dialog as this');
      cancelled.push(timer);
    },
  };
  vm.runInNewContext(source, sandbox);
  const t = fixture();
  new sandbox.module.exports.RelaySettingsDialog({
    query: selector => t.elements[selector], api: async () => initial(),
  });
  await t.elements['#settings-button'].events.get('click')();
  assert.equal(t.dialog.open, true);
  assert.equal(t.form.elements.relay_node_id.value, 'office-a');
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].delay, 1500);
  await scheduled[0].callback();
  assert.equal(scheduled.length, 2, 'polling continues with the default scheduler');
  t.dialog.close();
  assert.equal(cancelled.at(-1), 2, 'closing cancels the browser timer');
});

test('Connect sends config only through local API and immediately clears the password', async () => {
  const t = fixture(); await t.controller.open();
  t.form.elements.relay_api_key.value = KEY;
  await t.controller.submit();
  const request = t.calls.find(call => call.method === 'POST');
  assert.equal(request.url, '/api/federation/connect'); assert.equal(request.body.relay_api_key, KEY);
  assert.equal(t.form.elements.relay_api_key.value, '');
  assert.equal(t.action.textContent, 'Disconnect'); assert.equal(t.elements['#relay-settings-fields'].disabled, true);
  assert.equal(t.elements['#relay-settings-status'].textContent, 'Connecting…');
  assert.equal(t.changes.length, 1); assert.equal(t.changes[0].relay_api_key, undefined);
});

test('Disconnect needs no password and re-enables editing while keeping saved-key indicator', async () => {
  const t = fixture(initial({enabled: true, connected: true, status: 'connected', has_api_key: true}));
  await t.controller.open(); assert.equal(t.action.textContent, 'Disconnect');
  await t.controller.submit();
  assert.equal(t.calls.at(-1).url, '/api/federation/disconnect'); assert.equal(t.calls.at(-1).body, undefined);
  assert.equal(t.action.textContent, 'Connect'); assert.equal(t.elements['#relay-settings-fields'].disabled, false);
  assert.equal(t.form.elements.relay_api_key.required, false);
  assert.match(t.form.elements.relay_api_key.placeholder, /Saved key/);
});

test('Saved key reuse sends null, but changing the URL requires a new key', async () => {
  const t = fixture(initial({has_api_key: true})); await t.controller.open();
  t.form.elements.relay_url.value = 'wss://different.example/v1/connect';
  t.form.elements.relay_url.events.get('input')();
  assert.equal(t.form.elements.relay_api_key.required, true);
  t.form.elements.relay_url.value = initial().relay_url;
  t.form.elements.relay_url.events.get('input')();
  assert.equal(t.form.elements.relay_api_key.required, false);
  await t.controller.submit(); assert.equal(t.calls.at(-1).body.relay_api_key, null);
});

test('Closing clears a draft key, cancels polling and ignores late GET results', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const t = fixture(initial(), () => pending);
  const opened = t.controller.open(); t.form.elements.relay_api_key.value = KEY;
  t.dialog.close(); resolve(initial({relay_name: 'Late response'})); await opened;
  assert.equal(t.form.elements.relay_api_key.value, ''); assert.equal(t.timers.size, 0);
  assert.notEqual(t.form.elements.relay_name.value, 'Late response');
});

test('Polling preserves unsaved fields/key and updates actual connection status', async () => {
  let value = initial();
  const t = fixture(value, async () => value); await t.controller.open();
  t.form.elements.relay_name.value = 'Unsaved name'; t.form.elements.relay_api_key.value = KEY;
  value = initial({last_error: 'Relay unreachable', status: 'reconnecting', enabled: true});
  await t.timers.get(t.controller.timer)();
  assert.equal(t.form.elements.relay_name.value, 'Unsaved name');
  assert.equal(t.form.elements.relay_api_key.value, KEY);
  assert.equal(t.elements['#relay-settings-status'].textContent, 'Reconnecting…');
  assert.equal(t.elements['#relay-settings-connection-error'].textContent, 'Relay unreachable');
  t.dialog.close(); assert.equal(t.form.elements.relay_api_key.value, ''); assert.equal(t.timers.size, 0);
});

test('Failed Connect restores controls and redacts any unexpected echoed typed key', async () => {
  const t = fixture(initial(), async (url, method) => {
    if (method === 'POST') throw Error(`Rejected: ${KEY}`);
    return initial();
  });
  await t.controller.open(); t.form.elements.relay_api_key.value = KEY; await t.controller.submit();
  assert.equal(t.action.textContent, 'Connect'); assert.equal(t.action.disabled, false);
  assert.equal(t.form.elements.relay_api_key.value, ''); assert.equal(t.controller.busy, false);
  assert.equal(t.elements['#relay-settings-error'].textContent, 'Rejected: [redacted]');
  assert.equal(t.changes.length, 0);
});

test('Load failure and native invalid form never issue a Connect request', async () => {
  const t = fixture(initial(), async () => { throw Error('backend unavailable'); });
  await t.controller.open(); await t.controller.submit();
  assert.match(t.elements['#relay-settings-error'].textContent, /Cannot load/); assert.equal(t.action.disabled, true);
  assert.equal(t.calls.length, 1);
  const valid = fixture(); await valid.controller.open(); valid.form.valid = false;
  await valid.controller.submit(); assert.equal(valid.calls.length, 1);
});

test('busy guard prevents duplicate submissions and close never disconnects', async () => {
  let resolve;
  const t = fixture(initial(), async (url, method) => method ? new Promise(done => { resolve = done; }) : initial());
  await t.controller.open(); t.form.elements.relay_api_key.value = KEY;
  const pending = t.controller.submit(); await t.controller.submit();
  assert.equal(t.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(t.form.elements.relay_api_key.value, ''); assert.equal(t.action.disabled, true);
  t.dialog.close(); resolve(initial({enabled: true, status: 'connecting', has_api_key: true})); await pending;
  assert.equal(t.changes.length, 1); assert.equal(t.calls.some(call => call.url.endsWith('/disconnect')), false);
});

test('settings opens from Notifications, password masked, and app binds localApi', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const app = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const inbox=html.split('id="notifications-dialog"')[1].split('</dialog>')[0];
  assert.ok(inbox.includes('id="settings-button"'));
  assert.ok(!html.split('<header class="topbar">')[1].split('</header>')[0].includes('settings-button'));
  assert.match(html, /name="relay_api_key" type="password"/);
  assert.match(html, /Example: <code>wss:\/\/swarm-relay\.example\.com:9443\/v1\/connect<\/code>/);
  assert.match(app, /new SwarmRelaySettings\.RelaySettingsDialog\(\{\s*api: localApi/);
  assert.ok(html.indexOf('/static/relay-settings.js') < html.indexOf('/static/app.js'));
});
