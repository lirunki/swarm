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
  const ids = ['notifications-button', 'notifications-count', 'notifications-dialog', 'notification-list', 'notifications-summary', 'notifications-error', 'refresh-notifications', 'clear-notifications'];
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
  const text = row.children[1].children[0].children[1];
  assert.equal(text.textContent, unsafe);
  assert.equal(text.children.length, 0, 'notification text is assigned as text, not parsed markup');
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
  const open = elements['#notification-list'].children[0].children[2].children[1].children[1];
  await inbox.removeAndOpen(inbox.groups()[0], open);
  assert.deepEqual(events, ['delete', 'callback']);
  assert.ok(requests.some(([url, method]) => url === '/api/federation/relay%2Fa/proxy/api/notifications/remote' && method === 'DELETE'));
  assert.equal(inbox.items().length, 0);
  assert.equal(elements['#notifications-dialog'].open, false);
});

test('notifications from one agent render in one group and Open agent deletes all before navigating', async () => {
  const requests = [], opened = [];
  const {inbox, elements} = fixture(async (url, options) => {
    requests.push([url, options.method]);
    if (options.method === 'DELETE') return {ok: true, status: 200};
    if (url.includes('federation')) return response({revision: 1, notifications: []});
    return response({revision: 2, notifications: [
      payload('first').notifications[0],
      payload('second', {agent_id: 'agent-first', agent_name: 'Agent first', created_at: '2025-01-02T00:00:00Z'}).notifications[0],
    ]});
  }, value => opened.push(value));
  await inbox.refresh();
  assert.equal(elements['#notification-list'].children.length, 1);
  const group = inbox.groups()[0];
  assert.equal(group.items.length, 2);
  const row = elements['#notification-list'].children[0];
  assert.equal(row.children[1].children.length, 2);
  const actions = row.children[2].children[1];
  assert.equal(actions.className, 'notification-group-actions');
  assert.deepEqual(actions.children.map(child => child.textContent), ['Discard', 'Open agent']);
  assert.equal(actions.children[0].attributes.get('aria-label'), 'Discard 2 notifications from Agent first');
  await inbox.removeAndOpen(group, actions.children[1]);
  assert.equal(requests.filter(([, method]) => method === 'DELETE').length, 2);
  assert.equal(opened.length, 1);
  assert.equal(inbox.items().length, 0);
});

test('Discard clears the agent group without closing or navigating', async () => {
  const requests = [], opened = [];
  const {inbox, elements} = fixture(async (url, options) => {
    requests.push([url, options.method]);
    if (options.method === 'DELETE') return {ok: true, status: 200};
    if (url.includes('federation')) return response({revision: 1, notifications: []});
    return response(payload('local'));
  }, value => opened.push(value));
  await inbox.refresh();
  elements['#notifications-dialog'].showModal();
  const group = inbox.groups()[0];
  const discard = elements['#notification-list'].children[0].children[2].children[1].children[0];
  await inbox.removeGroup(group, discard);
  assert.equal(requests.filter(([, method]) => method === 'DELETE').length, 1);
  assert.equal(inbox.items().length, 0);
  assert.deepEqual(opened, []);
  assert.equal(elements['#notifications-dialog'].open, true);
});

test('Clear all flushes each connected source with pending notifications', async () => {
  const requests = [];
  const {inbox, elements} = fixture(async (url, options) => {
    requests.push([url, options.method]);
    if (options.method === 'DELETE') return {ok: true, status: 200};
    return response(url.includes('federation') ? payload('remote') : payload('local'));
  });
  await inbox.refresh();
  assert.equal(inbox.items().length, 2);
  await inbox.clearAll(elements['#clear-notifications']);
  assert.deepEqual(requests.filter(([, method]) => method === 'DELETE').map(([url]) => url).sort(), [
    '/api/federation/relay%2Fa/proxy/api/notifications', '/api/notifications',
  ].sort());
  assert.equal(inbox.items().length, 0);
  assert.equal(elements['#clear-notifications'].disabled, true);
});

test('late Open agent completion cannot navigate after the inbox context is cleared', async () => {
  let releaseDelete;
  const opened = [];
  const {inbox, elements} = fixture(async (url, options) => {
    if (options.method === 'DELETE') return await new Promise(resolve => { releaseDelete = resolve; });
    return response(url.includes('federation') ? payload('remote') : {revision: 1, count: 0, notifications: []});
  }, value => opened.push(value));
  await inbox.refresh();
  const group = inbox.groups()[0];
  const open = elements['#notification-list'].children[0].children[2].children[1].children[1];
  const operation = inbox.removeAndOpen(group, open);
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
  assert.match(html, /id="clear-notifications"[^>]*>Clear all</);
  const topMeta = html.slice(html.indexOf('class="top-meta"'), html.indexOf('</header>'));
  assert.ok(topMeta.indexOf('id="connection"') < topMeta.indexOf('id="help-button"'), 'Help is rightmost, after Live');
  assert.match(app, /pendingNotificationReveal = \{swarmId, agentId\}/);
  assert.match(app, /consumeNotificationReveal\(\);[\s\S]*notificationsInbox\?\.refresh\(\)/);
  assert.match(app, /agent\.alive && terminals\.has\(agent\.id\)/);
  assert.match(app, /cardViews\.setCompact\(state\.home, agent\.id, false\)/);
  assert.match(app, /scrollIntoView\(\{behavior: 'smooth'/);
});
