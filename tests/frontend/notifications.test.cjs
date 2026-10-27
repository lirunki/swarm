const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {NotificationsInbox, sourcePath} = require('../../src/swarm/static/notifications.js');

class ClassList {
  constructor(element) { this.element = element; }
  values() { return new Set((this.element.className || '').split(/\s+/).filter(Boolean)); }
  toggle(name, force) { const values = this.values(); const enabled = force === undefined ? !values.has(name) : force; enabled ? values.add(name) : values.delete(name); this.element.className = [...values].join(' '); }
}
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.events = new Map(); this.attributes = new Map(); this.className = ''; this.classList = new ClassList(this); this.hidden = false; this.textContent = ''; }
  addEventListener(name, callback) { this.events.set(name, callback); }
  removeEventListener(name) { this.events.delete(name); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
function fixture(fetch, onOpenAgent = () => {}) {
  const ids = ['notifications-button', 'notifications-count', 'notifications-dialog', 'notification-list', 'notifications-summary', 'notifications-error', 'refresh-notifications'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, new Element()]));
  const document = {createElement: tag => new Element(tag), querySelector: selector => elements[selector]};
  const inbox = new NotificationsInbox({document, fetch, onOpenAgent, setInterval: null});
  inbox.setSources([{id: 'local', name: 'Local Swarm'}, {id: 'relay/a', name: 'Remote <unsafe>', online: true}, {id: 'offline', name: 'Offline', online: false}]);
  return {inbox, elements};
}
const response = value => ({ok: true, status: 200, json: async () => value});
const payload = (id, overrides = {}) => ({revision: 1, count: 1, notifications: [{id, agent_id: `agent-${id}`, agent_name: `Agent ${id}`, status: 'waiting', text: `Text ${id}`, created_at: '2025-01-01T00:00:00Z', ...overrides}]});

test('source paths address local and encoded federation notification APIs', () => {
  assert.equal(sourcePath({id: 'local'}), '/api/notifications');
  assert.equal(sourcePath({id: 'relay/a'}), '/api/federation/relay%2Fa/proxy/api/notifications');
});

test('aggregates connected sources, tolerates partial failures, and renders literal text', async () => {
  const calls = [];
  const unsafe = '<img src=x onerror=alert(1)>';
  const {inbox, elements} = fixture(async url => {
    calls.push(url);
    if (url.includes('federation')) throw new Error('relay unavailable');
    return response(payload('one', {agent_name: unsafe, text: unsafe}));
  });
  await inbox.refresh();
  assert.deepEqual(calls.sort(), ['/api/federation/relay%2Fa/proxy/api/notifications', '/api/notifications'].sort());
  assert.equal(inbox.items().length, 1);
  assert.equal(elements['#notifications-count'].textContent, '1');
  assert.match(elements['#notifications-error'].textContent, /Remote <unsafe>/);
  const row = elements['#notification-list'].children[0];
  assert.equal(row.children[0].children[0].children[1].textContent, unsafe);
  assert.equal(row.children[1].textContent, unsafe);
  assert.equal(row.children[1].children.length, 0, 'notification text is assigned as text, not parsed markup');
});

test('refresh requests never overlap and coalesce a refresh requested in flight', async () => {
  let release, active = 0, maximum = 0, calls = 0;
  const first = new Promise(resolve => { release = resolve; });
  const {inbox} = fixture(async () => {
    calls += 1; active += 1; maximum = Math.max(maximum, active);
    if (calls <= 2) await first;
    active -= 1; return response({revision: calls, count: 0, notifications: []});
  });
  inbox.setSources([{id: 'local', name: 'Local Swarm'}]);
  const a = inbox.refresh(); const b = inbox.refresh();
  release(); await Promise.all([a, b]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2, 'one queued update runs after the first request');
  assert.equal(maximum, 1);
});

test('Open agent deletes at the originating source before invoking app callback', async () => {
  const events = [], requests = [];
  const {inbox, elements} = fixture(async (url, options) => {
    requests.push([url, options.method]);
    if (options.method === 'DELETE') { events.push('delete'); return {ok: true, status: 204}; }
    return response(url.includes('federation') ? payload('remote') : {revision: 1, count: 0, notifications: []});
  }, value => { events.push('callback'); assert.equal(value.swarmId, 'relay/a'); assert.equal(value.agentId, 'agent-remote'); });
  await inbox.refresh();
  const open = elements['#notification-list'].children[0].children[2].children[1];
  await inbox.removeAndOpen(inbox.items()[0], open);
  assert.deepEqual(events, ['delete', 'callback']);
  assert.ok(requests.some(([url, method]) => url === '/api/federation/relay%2Fa/proxy/api/notifications/remote' && method === 'DELETE'));
  assert.equal(inbox.items().length, 0);
  assert.equal(elements['#notifications-dialog'].open, false);
});

test('late Open agent completion cannot navigate after the inbox context is cleared', async () => {
  let releaseDelete;
  const opened = [];
  const {inbox, elements} = fixture(async (url, options) => {
    if (options.method === 'DELETE') return await new Promise(resolve => { releaseDelete = resolve; });
    return response(url.includes('federation') ? payload('remote') : {revision: 1, count: 0, notifications: []});
  }, value => opened.push(value));
  await inbox.refresh();
  const item = inbox.items()[0];
  const open = elements['#notification-list'].children[0].children[2].children[1];
  const operation = inbox.removeAndOpen(item, open);
  inbox.clear();
  releaseDelete({ok: true, status: 200});
  await operation;
  assert.deepEqual(opened, []);
  assert.equal(elements['#notifications-dialog'].open, undefined);
});

test('page loads notifications before app and app implements pending cross-Swarm reveal', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const app = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const version = html.match(/\/static\/app\.js\?v=([^"']+)/)[1];
  assert.ok(html.indexOf(`/static/notifications.js?v=${version}`) < html.indexOf(`/static/app.js?v=${version}`));
  assert.ok(html.includes(`/static/app.css?v=${version}`));
  const brand = html.slice(html.indexOf('class="brand"'), html.indexOf('id="swarm-switcher"'));
  assert.match(brand, /id="notifications-button"/);
  assert.match(app, /pendingNotificationReveal = \{swarmId, agentId\}/);
  assert.match(app, /consumeNotificationReveal\(\);[\s\S]*notificationsInbox\?\.refresh\(\)/);
  assert.match(app, /agent\.alive && terminals\.has\(agent\.id\)/);
  assert.match(app, /cardViews\.setCompact\(state\.home, agent\.id, false\)/);
  assert.match(app, /scrollIntoView\(\{behavior: 'smooth'/);
});
