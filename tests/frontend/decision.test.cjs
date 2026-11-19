const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');

const extensionPath = path.join(__dirname, '../../src/swarm/extensions/decision.js');
const extensionSource = fs.readFileSync(extensionPath, 'utf8');
const realCore = require('../../src/swarm/extensions/decision-core.js');
const plain = value => JSON.parse(JSON.stringify(value));

function typeboxStub() {
  const Type = {
    Any(options = {}) { return {...options}; },
    String(options = {}) { return {type: 'string', ...options}; },
    Optional(schema) { return {...schema, optional: true}; },
    Object(properties, options = {}) { return {type: 'object', properties, ...options}; },
    Record(key, value, options = {}) { return {type: 'object', key, additionalProperties: value, ...options}; },
    Array(items, options = {}) { return {type: 'array', items, ...options}; },
  };
  return {Type};
}

function loadExtension(configPath, runDecision) {
  // The production extension and core share one realm. Normalize updater output here because this
  // dependency-injection harness deliberately evaluates the extension in a vm context.
  const core = {
    ...realCore,
    deepClone(value) { return realCore.deepClone(JSON.parse(JSON.stringify(value))); },
    resolveConfigPath: () => configPath,
    updateConfig(filename, updater) {
      return realCore.updateConfig(filename, current => JSON.parse(JSON.stringify(updater(current))));
    },
  };
  const module = {exports: {}};
  const context = vm.createContext({process, Buffer, console, setTimeout, clearTimeout, URL, AbortController});
  const localRequire = name => {
    if (name === 'typebox') return typeboxStub();
    if (name === './decision-core.js') return core;
    if (name === './decision-backends.js') return {runDecision};
    return require(name);
  };
  const wrapper = vm.runInContext(`(function(require,module,exports,__filename,__dirname){${extensionSource}\n})`, context);
  wrapper(localRequire, module, module.exports, extensionPath, path.dirname(extensionPath));
  return module.exports;
}

function answerFor(question) {
  if (question.type === 'choice') {
    const labels = Object.keys(question.criteria), probability = 1 / labels.length;
    return {type: 'choice', choice: labels[0], probabilities: Object.fromEntries(labels.map(label => [label, probability])), confidence: 0.7};
  }
  if (question.type === 'score') return {type: 'score', score: 0, confidence: 0.7};
  if (question.type === 'box') return {type: 'box', box: {x: 1}};
  return {type: 'noul', noul: 0.7};
}

function harness(t, options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'decision-extension-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const configPath = path.join(directory, 'decision.json');
  if (options.config !== undefined) fs.writeFileSync(configPath, typeof options.config === 'string' ? options.config : JSON.stringify(options.config));
  const calls = [];
  const runDecision = options.runDecision || (async (profile, resolved, ctx, execution) => {
    calls.push({profile, resolved, ctx, execution});
    return {
      answers: Object.fromEntries(Object.entries(resolved.questions).map(([id, question]) => [id, answerFor(question)])),
      usage: {input: 3, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 5, cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}},
      metadata: {adapter: profile.adapter, provider: profile.provider, model: profile.model, probability_source: 'native'},
    };
  });
  const extension = loadExtension(configPath, runDecision);
  const handlers = new Map(), commands = new Map(), tools = [], notifications = [], selections = [];
  const pi = {
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name); },
    registerTool(tool) { tools.push(tool); },
    registerCommand(name, command) { commands.set(name, command); },
  };
  extension(pi);
  const messages = options.messages || [{role: 'user', content: 'fresh user prompt'}];
  const available = options.available || [
    {provider: 'github-copilot', id: 'gpt-6-luna', name: 'Luna', input: ['text', 'image']},
    {provider: 'local', id: 'chat-one', name: 'Chat One', input: ['text']},
  ];
  const registryCalls = {getAvailable: 0, auth: 0};
  const registry = {
    getAvailable() { registryCalls.getAvailable++; return available; },
    getAll() { return available; },
    getModelsOfType(type) { return type === 'chat' ? available : []; },
    find(provider, id) { return available.find(model => model.provider === provider && model.id === id); },
    getModelOfType(type, provider, id) {
      if (type === 'chat') return available.find(model => model.provider === provider && model.id === id);
      return options.classifier?.provider === provider && options.classifier?.id === id ? options.classifier : undefined;
    },
    getApiKeyAndHeaders() { registryCalls.auth++; throw new Error('commands must not request credentials'); },
  };
  const mainModel = {provider: 'main', id: 'unchanged'};
  const ctx = {
    cwd: directory,
    hasUI: true,
    mode: 'tui',
    model: mainModel,
    modelRegistry: registry,
    getSystemPrompt: () => 'effective system prompt',
    sessionManager: {
      getSessionId: () => 'session-1',
      buildSessionProjection: () => ({messages: plain(messages)}),
    },
    ui: {
      notify(message, level) { notifications.push([message, level]); },
      async select(title, values) { selections.push([title, values]); return options.select?.(title, values); },
    },
  };
  return {directory, configPath, calls, extension, pi, handlers, commands, tools, notifications, selections, registryCalls, registry, mainModel, ctx,
    tool: tools[0], execute(params, id = 'decision-call') { return tools[0].execute(id, params, undefined, undefined, ctx); }};
}

const noulRequest = overrides => ({state: {description: 'ordinary state'}, questions: {approved: {type: 'noul', instructions: 'Approve?'}}, ...overrides});

function notifyText(h) { return h.notifications.map(item => item[0]).join('\n'); }

async function command(h, args) {
  await h.commands.get('decision').handler(args, h.ctx);
}

test('registers one guarded CommonJS tool with the exact strict outer contract', t => {
  const h = harness(t);
  assert.equal(h.extension.default, h.extension);
  assert.equal(h.extension.extension, h.extension);
  assert.equal(h.tools.length, 1);
  assert.equal(h.tool.name, 'decision');
  assert.equal(h.tool.parameters.additionalProperties, false);
  assert.deepEqual(Object.keys(h.tool.parameters.properties).sort(), ['assets', 'model', 'questions', 'state', 'state_asset']);
  assert.equal(h.tool.parameters.properties.state.optional, true);
  assert.equal(h.tool.parameters.properties.questions.minProperties, 1);
  assert.equal(h.tool.parameters.properties.questions.maxProperties, 8);
  assert.equal(h.tool.parameters.properties.assets.type, 'object');
  assert.equal(h.tool.parameters.properties.assets.maxProperties, 4);
  assert.equal(h.tool.parameters.properties.assets.maxItems, undefined);
  assert.deepEqual(Object.keys(h.tool.outputSchema.properties).sort(), ['answers', 'metadata']);
  assert.equal(h.handlers.has('context_with_system'), true);
  assert.equal(h.handlers.has('before_provider_request'), true);
  assert.equal(h.commands.has('decision'), true);
  const installed = Object.getOwnPropertyDescriptor(h.pi, h.extension.INSTALLED);
  assert.deepEqual({enumerable: installed.enumerable, configurable: installed.configurable, writable: installed.writable},
    {enumerable: false, configurable: false, writable: false});

  h.extension(h.pi);
  assert.equal(h.tools.length, 1);
});

test('Symbol guard prevents duplicate registration across separately loaded copies', t => {
  const h = harness(t);
  const second = loadExtension(h.configPath, async () => { throw new Error('unused'); });
  second(h.pi);
  assert.equal(h.tools.length, 1);
  assert.equal(h.commands.size, 1);
  assert.equal(h.handlers.size, 2);
});

test('uses only the final provider payload when a later privacy transform replaces context', async t => {
  const messages = [{role: 'user', content: 'RAW_SESSION_SECRET'}];
  const h = harness(t, {messages});
  h.handlers.get('context_with_system')({messages: [
    {role: 'system', content: 'RAW_SYSTEM_SECRET'},
    {role: 'user', content: 'RAW_CONTEXT_SECRET'},
  ]}, h.ctx);
  h.handlers.get('before_provider_request')({payload: {messages: [
    {role: 'system', content: 'redacted system'},
    {role: 'user', content: 'redacted user'},
  ]}}, h.ctx);
  const result = await h.execute({state_asset: {type: 'context', include_system: true}, questions: {approved: {type: 'noul', instructions: 'Approve?'}}});
  assert.equal(result.isError, undefined, JSON.stringify(result));
  assert.equal(h.calls.length, 1);
  assert.deepEqual(plain(h.calls[0].resolved.state.conversation), [
    {role: 'system', content: 'redacted system'},
    {role: 'user', content: 'redacted user'},
  ]);
  assert.equal(JSON.stringify(h.calls[0].resolved.state).includes('RAW_'), false);
  assert.equal(result.usage.totalTokens, 5);
  assert.equal(result.structuredContent.answers.approved.noul, 0.7);
  assert.equal(result.structuredContent.metadata.source, 'context');
  assert.match(result.structuredContent.metadata.description_hash, /^[0-9a-f]{64}$/);
  assert.match(result.structuredContent.metadata.request_id, /^[0-9a-f-]{36}$/);
});

test('normalizes the final provider request and fails closed without a supported payload', async t => {
  const h = harness(t);
  h.handlers.get('context_with_system')({messages: [{role: 'user', content: 'RAW_CONTEXT_SECRET'}]}, h.ctx);
  h.handlers.get('before_provider_request')({payload: {unknown: [{secret: 'RAW_PROVIDER_SECRET'}]}}, h.ctx);
  const unavailable = await h.execute({state_asset: {type: 'context'}, questions: {approved: {type: 'noul', instructions: 'Approve?'}}});
  assert.equal(unavailable.isError, true);
  assert.equal(unavailable.details.category, 'source');
  assert.equal(h.calls.length, 0);

  h.handlers.get('context_with_system')({messages: [{role: 'user', content: 'stale'}]}, h.ctx);
  h.handlers.get('before_provider_request')({payload: {
    system: [{type: 'text', text: 'provider system'}],
    messages: [{role: 'user', content: [{type: 'text', text: 'provider user'}, {type: 'thinking', thinking: 'PRIVATE_THOUGHT'}]}],
  }}, h.ctx);
  const result = await h.execute({state_asset: {type: 'context', include_system: true}, questions: {approved: {type: 'noul', instructions: 'Approve?'}}});
  assert.equal(result.isError, undefined, JSON.stringify(result));
  assert.deepEqual(plain(h.calls[0].resolved.state.conversation), [{role: 'system', content: 'provider system'}, {role: 'user', content: [{type: 'text', text: 'provider user'}]}]);
  assert.equal(JSON.stringify(h.calls[0].resolved.state).includes('PRIVATE_THOUGHT'), false);
});

test('validates aliases and requests before backend dispatch and defaults missing config', async t => {
  const h = harness(t);
  assert.equal(fs.existsSync(h.configPath), false);
  const ok = await h.execute(noulRequest());
  assert.equal(ok.isError, undefined);
  assert.equal(h.calls[0].profile.model, 'gpt-6-luna');
  assert.equal(fs.existsSync(h.configPath), false, 'tool execution does not install or write defaults');

  const invalid = await h.execute(noulRequest({model: 'missing'}));
  assert.equal(invalid.isError, true);
  assert.equal(invalid.details.category, 'config');
  assert.equal(h.calls.length, 1, 'invalid profile is rejected before backend invocation');
  assert.equal(JSON.stringify(invalid).includes('ordinary state'), false);
});

test('rejects direct and symlinked credential sources before backend dispatch', async t => {
  const h = harness(t);
  const credential = path.join(h.directory, '.config', 'gh', 'hosts.yml');
  fs.mkdirSync(path.dirname(credential), {recursive: true});
  fs.writeFileSync(credential, 'secret');
  fs.symlinkSync(credential, path.join(h.directory, 'innocent.txt'));
  for (const source of ['.config/gh/hosts.yml', 'innocent.txt']) {
    const result = await h.execute({state_asset: {type: 'file', path: source}, questions: noulRequest().questions});
    assert.equal(result.isError, true);
    assert.equal(result.details.category, 'source');
  }
  assert.equal(h.calls.length, 0);
});

test('returns sanitized request IDs and preserves paid usage on backend failure', async t => {
  const usage = {input: 9, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 10, cost: {input: 1, output: 0, cacheRead: 0, cacheWrite: 0, total: 1}};
  const h = harness(t, {runDecision: async () => {
    const error = new realCore.DecisionError('output', 'raw provider output SECRET_STATE');
    error.usage = usage;
    throw error;
  }});
  const result = await h.execute({state: {raw: 'SECRET_STATE'}, questions: {approved: {type: 'noul', instructions: 'SECRET_QUESTION'}}});
  assert.equal(result.isError, true);
  assert.equal(result.details.category, 'output');
  assert.match(result.details.request_id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(plain(result.usage), usage);
  assert.equal(JSON.stringify(result).includes('SECRET_STATE'), false);
  assert.equal(JSON.stringify(result).includes('SECRET_QUESTION'), false);
  assert.match(result.content[0].text, /Request ID:/);
});

test('status, list, models, and show are display-only and use current registry availability', async t => {
  const h = harness(t);
  await command(h, 'status');
  await command(h, 'list');
  await command(h, 'models');
  await command(h, 'show luna');
  const text = notifyText(h);
  assert.match(text, /Decision model: luna/);
  assert.match(text, /gpt-6-luna/);
  assert.match(text, /local\/chat-one/);
  assert.match(text, /"adapter": "llm"/);
  assert.equal(h.registryCalls.getAvailable >= 2, true);
  assert.equal(h.registryCalls.auth, 0);
  assert.equal(h.ctx.model, h.mainModel);
  assert.equal(fs.existsSync(h.configPath), false);
});

test('set and add atomically preserve unrelated profiles without changing the main model', async t => {
  const h = harness(t);
  await command(h, 'set jev');
  let config = realCore.loadConfig(h.configPath);
  assert.equal(config.selected, 'jev');
  assert.equal(config.models.luna.model, 'gpt-6-luna');

  await command(h, 'add local local/chat-one');
  config = realCore.loadConfig(h.configPath);
  assert.deepEqual(plain(config.models.local), {adapter: 'llm', provider: 'local', model: 'chat-one', images: false, box: false});
  assert.equal(config.models.jev.adapter, 'classifier');

  await command(h, 'set local');
  assert.equal(realCore.loadConfig(h.configPath).selected, 'local');
  assert.equal(h.registryCalls.auth, 0);
  assert.equal(h.ctx.model, h.mainModel);
});

test('add accepts a shell-quoted JSON profile', async t => {
  const h = harness(t);
  await command(h, `add quoted '{"adapter":"llm","provider":"local","model":"chat-one","images":false,"box":false}'`);
  assert.deepEqual(plain(realCore.loadConfig(h.configPath).models.quoted), {
    adapter: 'llm', provider: 'local', model: 'chat-one', images: false, box: false,
  });
  await command(h, 'not-a-command');
  assert.match(notifyText(h), /add NAME 'JSON'/);
});

test('interactive set lists only available chat models and stores the selected profile', async t => {
  const h = harness(t, {select: (_title, choices) => choices.find(choice => choice.startsWith('local/chat-one'))});
  await command(h, 'set');
  const config = realCore.loadConfig(h.configPath);
  assert.equal(config.models[config.selected].provider, 'local');
  assert.equal(config.models[config.selected].model, 'chat-one');
  assert.equal(h.selections.length, 1);
  assert.equal(h.selections[0][1].some(value => value.startsWith('github-copilot/gpt-6-luna')), true);
  assert.equal(h.registryCalls.auth, 0);
});

test('adds classifier and credential-name-only HTTPS profiles and refuses selected removal', async t => {
  const h = harness(t, {classifier: {type: 'classifier', provider: 'typesafe', id: 'jev-latest', input: ['text']}});
  await command(h, 'add native classifier typesafe/jev-latest');
  await command(h, 'add bridge typesafe-http remote https://decision.example/v1 DECISION_TOKEN --images --box');
  let config = realCore.loadConfig(h.configPath);
  assert.equal(config.models.native.adapter, 'classifier');
  assert.deepEqual(plain(config.models.bridge), {adapter: 'typesafe-http', endpoint: 'https://decision.example/v1', auth_env: 'DECISION_TOKEN', model: 'remote', images: true, box: true});
  await command(h, 'remove luna');
  config = realCore.loadConfig(h.configPath);
  assert.equal(Object.hasOwn(config.models, 'luna'), true);
  assert.match(notifyText(h), /cannot be removed|configuration/i);
  assert.equal(notifyText(h).includes('DECISION_TOKEN='), false);
});

test('malformed config fails closed for tools and commands without provider calls or overwrite', async t => {
  const h = harness(t, {config: '{"secret":"DO_NOT_ECHO"'});
  const before = fs.readFileSync(h.configPath, 'utf8');
  const result = await h.execute(noulRequest());
  assert.equal(result.isError, true);
  assert.equal(result.details.category, 'config');
  assert.equal(h.calls.length, 0);
  assert.equal(JSON.stringify(result).includes('DO_NOT_ECHO'), false);
  await command(h, 'set luna');
  assert.equal(fs.readFileSync(h.configPath, 'utf8'), before);
  assert.equal(notifyText(h).includes('DO_NOT_ECHO'), false);
});


test('single-quoted JSON profile preserves backslash, quote and Unicode escapes', () => {
  const {splitWords}=loadExtension('/unused/decision.json',async()=>{});
  const json=JSON.stringify({adapter:'llm',provider:'example',model:'path\\model"quote–unicode',images:false,box:false}).replace('–','\\u2013');
  const words=splitWords(`add custom '${json}'`);
  assert.deepEqual(JSON.parse(words.slice(2).join(' ')),JSON.parse(json));
});


test('configuration errors distinguish Pi code reload from decision file reload', async t => {
  const h=harness(t,{config:'not JSON'});
  await h.commands.get('decision').handler('list',h.ctx);
  assert.match(JSON.stringify(h.notifications),/Pi \/reload \(not \/decision reload\)/);
});
