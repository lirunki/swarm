const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {SystemNotifications, preferenceKey, workerPath} = require('../../src/swarm/static/system-notifications.js');

function storageFixture() {
  const values = new Map();
  return {
    values,
    storage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    },
  };
}

function notificationFixture(permission = 'default', requested = 'granted') {
  const created = [];
  function Notification(title, options) { created.push([title, options]); }
  Notification.permission = permission;
  Notification.requestPermission = async () => {
    Notification.permission = requested;
    return requested;
  };
  return {Notification, created};
}

test('explicit opt-in registers the service worker, persists locally, and displays notification text', async () => {
  const {storage, values} = storageFixture();
  const {Notification} = notificationFixture();
  const shown = [], registrations = [];
  const registration = {showNotification: async (title, options) => shown.push([title, options])};
  const navigator = {serviceWorker: {register: async value => { registrations.push(value); return registration; }}};
  const alerts = new SystemNotifications({Notification, navigator, storage});

  assert.equal(alerts.status(), 'disabled');
  await alerts.enable();
  assert.equal(alerts.status(), 'enabled');
  assert.equal(values.get(preferenceKey), 'true');
  assert.deepEqual(registrations, [workerPath]);
  assert.equal(await alerts.notify({title: 'Agent one · Remote', body: 'Needs approval', tag: 'swarm:remote:n1', data: {agentId: 'a1'}}), true);
  assert.equal(shown.length, 1);
  assert.equal(shown[0][0], 'Agent one · Remote');
  assert.equal(shown[0][1].body, 'Needs approval');
  assert.equal(shown[0][1].tag, 'swarm:remote:n1');
  assert.equal(shown[0][1].icon, '/static/swarm-notification.svg');

  alerts.disable();
  assert.equal(alerts.status(), 'disabled');
  assert.equal(values.has(preferenceKey), false);
  assert.equal(await alerts.notify({title: 'Ignored'}), false);
  assert.equal(shown.length, 1);
});

test('denied, unsupported, and blocked storage states fail safely without enabling alerts', async () => {
  const {storage, values} = storageFixture();
  const denied = notificationFixture('default', 'denied');
  const alerts = new SystemNotifications({Notification: denied.Notification, navigator: {}, storage});
  await assert.rejects(alerts.enable(), /blocked.*site settings/i);
  assert.equal(alerts.status(), 'blocked');
  assert.equal(values.has(preferenceKey), false);

  const unsupported = new SystemNotifications({root: {isSecureContext: false}, Notification: notificationFixture('granted').Notification, navigator: {}, storage});
  assert.equal(unsupported.status(), 'unsupported');
  await assert.rejects(unsupported.enable(), /unavailable/);

  const blockedStorage = new SystemNotifications({
    Notification: notificationFixture('granted').Notification,
    navigator: {},
    storage: {getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); }, removeItem: () => { throw new Error('denied'); }},
  });
  assert.equal(blockedStorage.status(), 'disabled');
  await blockedStorage.enable();
  assert.equal(blockedStorage.status(), 'disabled');
});

test('desktop fallback uses the Notification constructor when service workers are absent', async () => {
  const {storage} = storageFixture();
  const {Notification, created} = notificationFixture('granted');
  const alerts = new SystemNotifications({Notification, navigator: {}, storage});
  await alerts.enable();
  assert.equal(await alerts.notify({title: 'Local agent', body: 'Done', tag: 'done'}), true);
  assert.equal(created[0][0], 'Local agent');
  assert.equal(created[0][1].body, 'Done');
});

test('worker focuses an existing dashboard or opens it, and assets load before Notifications', () => {
  const worker = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/notification-worker.js'), 'utf8');
  const helper = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/system-notifications.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  assert.match(worker, /notificationclick/);
  assert.match(worker, /clients\.matchAll\(\{type: 'window', includeUncontrolled: true\}\)/);
  assert.match(worker, /clients\.openWindow\('\/'\)/);
  assert.ok(html.indexOf('system-notifications.js') < html.indexOf('notifications.js'));
  assert.match(html, /id="system-notifications"[^>]*aria-pressed="false"/);
  assert.match(helper, /swarm-notification\.svg/);
  assert.equal(fs.existsSync(path.join(__dirname, '../../src/swarm/static/swarm-notification.svg')), true);
});
