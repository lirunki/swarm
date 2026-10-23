const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {MessagesController, requestId} = require('../../src/swarm/static/agent-messages.js');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.events = new Map(); this.open = false; this.hidden = false; this.disabled = false;
    this.className = ''; this.value = ''; this.elements = {};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(type, fn) { this.events.set(type, fn); }
  click() { if (!this.disabled) return this.events.get('click')?.({currentTarget: this}); }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.get('close')?.(); }
  reset() { for (const value of Object.values(this.elements)) value.value = ''; }
  querySelector(selector) { return flatten(this).find(item => item.className.split(/\s+/).includes(selector.slice(1))); }
  setAttribute(name, value) { this[name] = String(value); }
}
const flatten = element => element instanceof Element ? [element, ...element.children.flatMap(flatten)] : [];
const tick = () => new Promise(resolve => setImmediate(resolve));
const ID = '11111111-1111-4111-8111-111111111111';
const SECOND = '22222222-2222-4222-8222-222222222222';
const message = (overrides = {}) => ({id: ID, kind: 'message', text: '<img src=x onerror=bad()> literal', sender_id: 'operator', status: 'pending', created_at: '2026-01-02T03:04:05Z', last_error: null, ...overrides});

function fixture(api, options = {}) {
  const ids = ['messages-dialog', 'agent-message-list', 'refresh-agent-messages', 'messages-title', 'messages-workspace', 'agent-messages-error', 'agent-message-form', 'agent-message-count', 'send-agent-message'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, new Element(id === 'agent-message-form' ? 'form' : 'div')]));
  const text = new Element('textarea'); text.required = true; text.reportValidity = () => { text.reported = true; };
  elements['#agent-message-form'].elements = {text};
  const calls = [], changes = [];
  const controller = new MessagesController({
    query: selector => elements[selector],
    create: (tag, className, value) => Object.assign(new Element(tag), {className, textContent: value}),
    api: async (...args) => { calls.push(args); return api(...args); },
    onChange: async () => changes.push(true), confirm: options.confirm || (() => true),
  });
  return {controller, elements, calls, changes, text};
}
const agent = {id: 'agent-a', name: 'Research', managed: true, message_count: 1, messages_revision: 1};

function safeList(messages = [message()], revision = 1, agentId = agent.id) { return {agent_id: agentId, revision, messages}; }

test('list renders message, state, sender and date as literal text only', async () => {
  const t = fixture(async () => safeList());
  await t.controller.open(agent, '/api/agents/agent-a/messages', 'Local Swarm');
  assert.deepEqual(t.calls, [['/api/agents/agent-a/messages']]);
  const nodes = flatten(t.elements['#agent-message-list']);
  assert.ok(nodes.some(node => node.textContent === message().text));
  assert.ok(nodes.some(node => /From operator/.test(node.textContent)));
  assert.ok(nodes.some(node => node.textContent === 'pending'));
  assert.equal(nodes.some(node => node.tag === 'img' || node.tag === 'script'), false);
  assert.equal(t.elements['#agent-message-count'].textContent, '1 outstanding');
});

test('send posts once with a UUID request id and refreshes without wake or terminal effects', async () => {
  let sent = false;
  const t = fixture(async (url, method, body) => {
    if (method === 'POST') { sent = true; return {agent_id: agent.id, message: message({id: SECOND, text: body.text})}; }
    return safeList(sent ? [message(), message({id: SECOND, text: 'durable context'})] : [message()], sent ? 2 : 1);
  });
  await t.controller.open(agent, '/api/agents/agent-a/messages', 'Local');
  t.text.value = 'durable context'; await t.controller.send();
  const post = t.calls.find(call => call[1] === 'POST');
  assert.equal(post[0], '/api/agents/agent-a/messages'); assert.equal(post[2].text, 'durable context');
  assert.match(post[2].request_id, /^[0-9a-f-]{36}$/i); assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
  assert.equal(t.changes.length, 1); assert.equal(t.text.value, ''); assert.equal(t.controller.messages.length, 2);
});

test('uncertain POST is never retried and exposes its request id for inspection', async () => {
  const t = fixture(async (url, method) => { if (method === 'POST') throw Error('connection lost'); return safeList([]); });
  await t.controller.open(agent, '/api/agents/agent-a/messages', 'Local');
  t.text.value = 'send once'; await t.controller.send();
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
  assert.match(t.elements['#agent-messages-error'].textContent, /uncertain.*Do not retry blindly.*request ID [0-9a-f-]{36}/i);
  assert.equal(t.text.value, 'send once');
});

test('delete confirms and refreshes, while delivering messages cannot be recalled', async () => {
  let deleted = false, question = '';
  const t = fixture(async (url, method) => {
    if (method === 'DELETE') { deleted = true; return {deleted: true, agent_id: agent.id, revision: 2}; }
    return safeList(deleted ? [] : [message()], deleted ? 2 : 1);
  }, {confirm: value => { question = value; return true; }});
  await t.controller.open(agent, '/api/agents/agent-a/messages', 'Local');
  const remove = flatten(t.elements['#agent-message-list']).find(node => node.className.includes('agent-message-delete'));
  await remove.click(); await tick();
  assert.match(question, /Delete this outstanding message/);
  assert.ok(t.calls.some(call => call[0].endsWith(`/${ID}`) && call[1] === 'DELETE'));
  assert.equal(t.controller.messages.length, 0); assert.equal(t.changes.length, 1);

  const delivering = fixture(async () => safeList([message({status: 'delivering'})]));
  await delivering.controller.open(agent, '/api/agents/agent-a/messages', 'Local');
  const blocked = flatten(delivering.elements['#agent-message-list']).find(node => node.className.includes('agent-message-delete'));
  assert.equal(blocked.disabled, true); await blocked.click();
  assert.equal(delivering.calls.filter(call => call[1] === 'DELETE').length, 0);
});

test('late list and send responses cannot mutate a newly opened workspace', async () => {
  let resolveOld, resolvePost;
  const oldBase = '/api/federation/old/proxy/api/agents/agent-a/messages';
  const newAgent = {...agent, id: 'agent-b', name: 'Next', messages_revision: 9};
  const t = fixture(async (url, method, body) => {
    if (url === oldBase && method === 'POST') return new Promise(resolve => { resolvePost = () => resolve({agent_id: agent.id, message: message({text: body.text})}); });
    if (url === oldBase) return new Promise(resolve => { resolveOld = resolve; });
    return safeList([], 9, newAgent.id);
  });
  const opening = t.controller.open(agent, oldBase, 'Old'); await tick();
  const next = t.controller.open(newAgent, '/api/agents/agent-b/messages', 'New');
  resolveOld(safeList([message()], 1)); await opening; await next;
  assert.equal(t.controller.agentId, 'agent-b'); assert.equal(t.controller.messages.length, 0);

  const reopen = t.controller.open(agent, oldBase, 'Old'); await tick(); resolveOld(safeList([], 1)); await reopen;
  t.text.value = 'old target'; const sending = t.controller.send(); await tick();
  await t.controller.open(newAgent, '/api/agents/agent-b/messages', 'New');
  resolvePost(); await sending;
  assert.equal(t.controller.agentId, 'agent-b'); assert.equal(t.controller.messages.length, 0); assert.equal(t.changes.length, 0);
});

test('expanded controller caches by base and revision and explains observed agents', async () => {
  const t = fixture(async () => safeList());
  const first = new Element('section'); await t.controller.mountExpanded(first, agent, '/api/agents/agent-a/messages');
  const second = new Element('section'); await t.controller.mountExpanded(second, agent, '/api/agents/agent-a/messages');
  assert.equal(t.calls.length, 1); assert.match(flatten(second).map(node => node.textContent).join(' '), /1 outstanding/);
  const revised = new Element('section'); await t.controller.mountExpanded(revised, {...agent, messages_revision: 2}, '/api/agents/agent-a/messages');
  assert.equal(t.calls.length, 2);
  const observed = new Element('section'); await t.controller.mountExpanded(observed, {...agent, managed: false}, '/api/agents/observed/messages');
  assert.match(flatten(observed).map(node => node.textContent).join(' '), /unavailable for observed agents/);
  assert.equal(t.calls.length, 2);
});

test('request id fallback is UUID-shaped and messaging assets share the app version', () => {
  assert.match(requestId(), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const version = html.match(/app\.js\?v=([^"]+)/)[1];
  assert.ok(html.indexOf(`/static/agent-messages.js?v=${version}`) < html.indexOf(`/static/app.js?v=${version}`));
  assert.match(html, /id="messages-dialog"/); assert.ok(html.includes(`/static/app.css?v=${version}`));
});
