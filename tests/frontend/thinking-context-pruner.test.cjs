const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const extensionPath = path.join(__dirname, '../../src/swarm/extensions/thinking-context-pruner.js');
const source = fs.readFileSync(extensionPath, 'utf8');

function load(env = {}) {
  const handlers = new Map(), commands = new Map(), renderers = new Map(), entries = [], notifications = [];
  const context = vm.createContext({
    process: {env: {...env}},
    Buffer,
    console,
    Date,
    setTimeout,
    clearTimeout,
    require(name) {
      if (name === '@earendil-works/pi-tui') return {Text: class Text { constructor(text, x, y) { Object.assign(this, {text, x, y}); } }};
      return require(name);
    },
  });
  const module = {exports: {}};
  const wrapper = vm.runInContext(`(function(require,module,exports,__filename,__dirname){${source}\n})`, context);
  wrapper(context.require, module, module.exports, extensionPath, path.dirname(extensionPath));
  const pi = {
    on(name, handler) { handlers.set(name, handler); },
    appendEntry(customType, data) { entries.push({type: 'custom', customType, data, id: crypto.randomUUID()}); },
    registerCommand(name, value) { commands.set(name, value); },
    registerEntryRenderer(name, renderer) { renderers.set(name, renderer); },
  };
  module.exports(pi);
  const branch = [];
  const ctx = {
    sessionManager: {getBranch: () => branch, getEntries: () => branch},
    ui: {notify(text, level) { notifications.push([text, level]); }},
  };
  handlers.get('session_start')({type: 'session_start'}, ctx);
  return {module: module.exports, handlers, commands, renderers, entries, notifications, branch, ctx};
}

const thinking = (text, extra = {}) => ({type: 'thinking', thinking: text, ...extra});
const assistant = (content, extra = {}) => ({role: 'assistant', provider: 'test-provider', model: 'test-model', content, ...extra});

test('strips finalized thinking blocks before persistence and retains separate non-context copies', () => {
  const h = load();
  const original = assistant([
    {type: 'text', text: 'answer'},
    thinking('private reasoning', {thinkingSignature: 'opaque', redacted: true}),
    thinking('more reasoning'),
  ], {stopReason: 'stop', usage: {totalTokens: 42}});
  const result = h.handlers.get('message_end')({type: 'message_end', message: original}, h.ctx);
  assert.equal(original.content[1].thinking, 'private reasoning', 'incoming message remains immutable');
  assert.deepEqual(JSON.parse(JSON.stringify(result.message.content)), [
    {type: 'text', text: 'answer'},
  ]);
  assert.equal(result.message.usage.totalTokens, 42);
  assert.equal(h.entries.length, 1);
  assert.equal(h.entries[0].customType, 'swarm.thinking-context-pruner.thinking');
  assert.equal(h.entries[0].data.text, 'private reasoning\n\nmore reasoning');
});

test('removes legacy context reasoning and preserves user/tool messages and transcript metadata', () => {
  const h = load();
  const thinkingMessage = assistant([thinking('old reasoning', {thinkingSignature: 'sig'})]);
  const user = {role: 'user', content: [{type: 'text', text: 'hello'}]};
  const tool = {role: 'toolResult', content: [{type: 'text', text: 'tool'}]};
  const messages = [user, thinkingMessage, tool];
  const result = h.handlers.get('context')({type: 'context', messages});
  assert.deepEqual(JSON.parse(JSON.stringify(result.messages[0])), user);
  assert.deepEqual(JSON.parse(JSON.stringify(result.messages[1].content)), []);
  assert.equal(result.messages[2], tool);
  assert.equal(messages[1].content[0].thinking, 'old reasoning');
});

test('signature preservation retains only the opaque token, never reasoning text', () => {
  const h = load();
  h.commands.get('thinking-context').handler('signatures preserve', h.ctx);
  const input = assistant([thinking('reasoning', {thinkingSignature: 'opaque'})]);
  const result = h.handlers.get('message_end')({type: 'message_end', message: input}, h.ctx);
  assert.deepEqual(JSON.parse(JSON.stringify(result.message.content)), [
    {type: 'thinking', thinking: '', thinkingSignature: 'opaque'},
  ]);
  assert.equal(result.message.content[0].thinking.includes('reasoning'), false);
});

test('strip-signatures option removes the opaque signature payload too', () => {
  const h = load();
  h.commands.get('thinking-context').handler('signatures strip', h.ctx);
  const input = assistant([thinking('reasoning', {thinkingSignature: 'opaque'})]);
  const result = h.handlers.get('message_end')({type: 'message_end', message: input}, h.ctx);
  assert.deepEqual(JSON.parse(JSON.stringify(result.message.content)), []);
});

test('commands restore from branch and render retained copy as non-context custom entry', async () => {
  const h = load();
  h.entries[0] && h.branch.push(h.entries[0]);
  await h.commands.get('thinking-context').handler('copy off', h.ctx);
  assert.equal(h.entries.at(-1).customType, 'swarm.thinking-context-pruner.config');
  h.branch.push(h.entries.at(-1));
  const fresh = load();
  fresh.branch.push(...h.branch);
  fresh.handlers.get('session_start')({type: 'session_start'}, fresh.ctx);
  const result = fresh.handlers.get('message_end')({type: 'message_end', message: assistant([thinking('do not copy')])}, fresh.ctx);
  assert.deepEqual(JSON.parse(JSON.stringify(result.message.content)), []);
  assert.equal(fresh.entries.length, 0);
  const retained = {type: 'custom', customType: 'swarm.thinking-context-pruner.thinking', data: {provider: 'p', model: 'm', text: 'retained thought'}};
  const rendered = fresh.renderers.get(retained.customType)(retained, {expanded: false}, {fg: (color, text) => `${color}:${text}`});
  assert.match(rendered.text, /Thinking \(p\/m\)/);
  await fresh.commands.get('thinking-copy').handler('', {...fresh.ctx, sessionManager: {getBranch: () => [retained]}});
  assert.match(fresh.notifications.at(-1)[0], /retained thought/);
});

test('save writes effective settings and resets session overrides', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'thinking-pruner-save-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const configPath = path.join(directory, 'settings.json');
  const h = load({PI_THINKING_CONTEXT_CONFIG: configPath});
  await h.commands.get('thinking-context').handler('copy off', h.ctx);
  await h.commands.get('thinking-context').handler('save', h.ctx);
  assert.equal(JSON.parse(fs.readFileSync(configPath, 'utf8')).keepCopy, false);
  assert.equal(h.entries.at(-1).customType, 'swarm.thinking-context-pruner.config');
  assert.equal(Object.keys(h.entries.at(-1).data.overrides).length, 0);
});

test('reload restores persisted branch overrides after rereading file configuration', async () => {
  const h = load();
  await h.commands.get('thinking-context').handler('off', h.ctx);
  h.branch.push(h.entries.at(-1));
  await h.commands.get('thinking-context').handler('on', h.ctx);
  h.branch.push(h.entries.at(-1));
  await h.commands.get('thinking-context').handler('reload', h.ctx);
  const result = h.handlers.get('message_end')({type: 'message_end', message: assistant([thinking('reasoning')])}, h.ctx);
  assert.deepEqual(JSON.parse(JSON.stringify(result.message.content)), []);
});

test('disable switch keeps thinking and custom config rejects unsafe relative paths', () => {
  const h = load();
  h.commands.get('thinking-context').handler('off', h.ctx);
  const input = assistant([thinking('reasoning')]);
  assert.equal(h.handlers.get('message_end')({type: 'message_end', message: input}, h.ctx), undefined);
  assert.throws(() => h.module.resolveConfigPath({PI_THINKING_CONTEXT_CONFIG: 'relative.json'}, '/tmp'), /absolute path/);
});
