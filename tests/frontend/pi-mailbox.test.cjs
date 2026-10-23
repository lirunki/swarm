const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID } = require('node:crypto');

const extensionPath = path.join(__dirname, '../../src/swarm/extensions/mailbox.js');
const extensionSource = fs.readFileSync(extensionPath, 'utf8');

function loadExtension(env, clock = {}) {
  const timers = [];
  const context = vm.createContext({
    process: { env },
    setInterval(callback, delay) {
      const timer = { callback, delay, cleared: false, unrefCalled: false, unref() { this.unrefCalled = true; } };
      timers.push(timer);
      return timer;
    },
    clearInterval(timer) { timer.cleared = true; },
    setTimeout, clearTimeout,
    Date: clock.Date || Date,
    Buffer,
  });
  const module = { exports: {} };
  const typeStub = {
    Object(properties) { return { type: 'object', properties }; },
    String(options = {}) { return { type: 'string', ...options }; },
  };
  const localRequire = name => name === 'typebox' ? { Type: typeStub } : require(name);
  const wrapper = vm.runInContext(`(function(require,module,exports,__filename,__dirname){${extensionSource}\n})`, context);
  wrapper(localRequire, module, module.exports, extensionPath, path.dirname(extensionPath));
  return { extension: module.exports, timers };
}

function createHarness(options = {}) {
  const directory = options.directory || fs.mkdtempSync(path.join(os.tmpdir(), 'pi-mailbox-'));
  const env = {
    SWARM_CONTROL_DIR: directory,
    SWARM_CONTROL_EPOCH: options.epoch || randomUUID(),
    SWARM_AGENT_ID: options.agentId || randomUUID(),
  };
  const sessionId = options.sessionId || randomUUID();
  const entries = options.entries || [];
  const calls = [];
  const handlers = new Map();
  const commands = new Map();
  let tool;
  let idle = options.idle !== undefined ? options.idle : true;
  let pending = options.pending || false;
  const { extension, timers } = loadExtension(env, options.clock);
  const pi = {
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name); },
    registerCommand(name, registration) { commands.set(name, registration); },
    registerTool(registration) { tool = registration; },
    sendMessage(message, delivery) { calls.push(['sendMessage', message, delivery]); },
    sendUserMessage(message, delivery) { calls.push(['sendUserMessage', message, delivery]); },
    appendEntry(customType, data) {
      calls.push(['appendEntry', customType, data]);
      entries.push({ type: 'custom', customType, data, id: randomUUID(), parentId: null, timestamp: new Date().toISOString() });
    },
    async setModel(model) { calls.push(['setModel', model]); return true; },
    setThinkingLevel(level) { calls.push(['setThinkingLevel', level]); },
    setSessionName(name) { calls.push(['setSessionName', name]); },
  };
  extension(pi);
  const ctx = {
    sessionManager: { getSessionId: () => sessionId, getEntries: () => entries.slice() },
    modelRegistry: { find: (provider, id) => options.models?.[`${provider}/${id}`] },
    isIdle: () => idle,
    hasPendingMessages: () => pending,
    compact(config) { calls.push(['compact', config]); config.onComplete({}); },
    async reload() { calls.push(['reload']); },
    shutdown() {
      calls.push(['shutdown']);
      if (options.shutdownError) throw new Error(options.shutdownError);
    },
    ui: new Proxy({}, { get() { throw new Error('mailbox must not access UI/editor APIs'); } }),
  };
  if (options.shutdownUnavailable) delete ctx.shutdown;
  function writeInbox(items, override = {}) {
    fs.writeFileSync(path.join(directory, 'inbox.json'), JSON.stringify({
      epoch: env.SWARM_CONTROL_EPOCH,
      agent_id: env.SWARM_AGENT_ID,
      session_id: sessionId,
      awake: true,
      items,
      ...override,
    }));
  }
  function writeLifecycle(phase, override = {}) {
    fs.writeFileSync(path.join(directory, 'lifecycle.json'), JSON.stringify({
      id: randomUUID(),
      agent_id: env.SWARM_AGENT_ID,
      epoch: env.SWARM_CONTROL_EPOCH,
      session_id: sessionId,
      action: 'shutdown',
      phase,
      requested_at: new Date().toISOString(),
      ...override,
    }));
  }
  function lifecycleAck() {
    const filename = path.join(directory, 'lifecycle-ack.json');
    return fs.existsSync(filename) ? JSON.parse(fs.readFileSync(filename, 'utf8')) : null;
  }
  function receipt(id) {
    const filename = path.join(directory, 'acks', `${id}.json`);
    return fs.existsSync(filename) ? JSON.parse(fs.readFileSync(filename, 'utf8')) : null;
  }
  async function flush() {
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
  }
  return {
    directory, env, sessionId, entries, calls, handlers, commands, get tool() { return tool; }, ctx, timers,
    writeInbox, writeLifecycle, lifecycleAck, receipt, flush,
    setIdle(value) { idle = value; },
    setPending(value) { pending = value; },
  };
}

function message(id = randomUUID(), text = 'Review the current test failure.') {
  return { id, kind: 'message', text, sender_id: randomUUID(), sender_name: 'Coordinator', status: 'delivering' };
}

function command(text, id = randomUUID()) {
  return { id, kind: 'command', text, sender_id: randomUUID(), status: 'executing' };
}

test('factory starts no timer; session lifecycle writes heartbeat and stops polling', async t => {
  const h = createHarness();
  t.after(() => fs.rmSync(h.directory, { recursive: true, force: true }));
  assert.equal(h.timers.length, 0);
  await h.handlers.get('session_start')({ type: 'session_start', reason: 'startup' }, h.ctx);
  await h.flush();
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].delay, 500);
  assert.equal(h.timers[0].unrefCalled, true);
  const heartbeat = JSON.parse(fs.readFileSync(path.join(h.directory, 'heartbeat.json'), 'utf8'));
  assert.equal(heartbeat.epoch, h.env.SWARM_CONTROL_EPOCH);
  assert.equal(heartbeat.session_id, h.sessionId);
  assert.equal(heartbeat.protocol, 1);
  assert.match(heartbeat.instance, /^[0-9a-f-]{36}$/i);
  await h.handlers.get('session_shutdown')({ type: 'session_shutdown', reason: 'quit' }, h.ctx);
  assert.equal(h.timers[0].cleared, true);
});

test('message uses native custom delivery and is acknowledged only after durable session entry', async t => {
  const h = createHarness();
  t.after(() => fs.rmSync(h.directory, { recursive: true, force: true }));
  const item = message();
  h.writeInbox([item]);
  await h.handlers.get('session_start')({ type: 'session_start', reason: 'startup' }, h.ctx);
  await h.flush();
  const sends = h.calls.filter(call => call[0] === 'sendMessage');
  assert.equal(sends.length, 1);
  assert.equal(sends[0][1].customType, 'swarm.message');
  assert.equal(sends[0][1].display, true);
  assert.equal(sends[0][1].details.message_id, item.id);
  assert.match(sends[0][1].content, new RegExp(item.id));
  assert.match(sends[0][1].content, /do not duplicate external actions/i);
  assert.equal(sends[0][2].triggerTurn, true);
  assert.equal(sends[0][2].deliverAs, 'steer');
  assert.equal(h.receipt(item.id), null, 'queueing is not durable delivery');
  assert.equal(JSON.parse(fs.readFileSync(path.join(h.directory, 'inbox.json'))).items.length, 1, 'extension never consumes inbox');

  h.entries.push({ type: 'custom_message', customType: 'swarm.message', details: { message_id: item.id }, content: 'saved', display: true });
  await h.handlers.get('message_end')({ type: 'message_end' }, h.ctx);
  await h.flush();
  assert.equal(h.receipt(item.id).status, 'delivered');
  assert.equal(h.calls.filter(call => call[0] === 'sendMessage').length, 1, 'durable IDs are deduplicated');
});

test('foreign, sleeping, and oversized inbox files are ignored', async t => {
  const h = createHarness();
  t.after(() => fs.rmSync(h.directory, { recursive: true, force: true }));
  const item = message();
  h.writeInbox([item], { epoch: randomUUID() });
  await h.handlers.get('session_start')({ type: 'session_start', reason: 'startup' }, h.ctx);
  await h.flush();
  assert.equal(h.calls.some(call => call[0] === 'sendMessage'), false);
  h.writeInbox([item], { awake: false });
  h.timers[0].callback(); await h.flush();
  assert.equal(h.calls.some(call => call[0] === 'sendMessage'), false);
  fs.writeFileSync(path.join(h.directory, 'inbox.json'), ' '.repeat(2 * 1024 * 1024 + 1));
  h.timers[0].callback(); await h.flush();
  assert.equal(h.calls.some(call => call[0] === 'sendMessage'), false);
});

test('commands defer while busy and execute only the whitelist with durable receipts', async t => {
  const h = createHarness({ idle: false });
  t.after(() => fs.rmSync(h.directory, { recursive: true, force: true }));
  const allowed = command('/thinking high');
  h.writeInbox([allowed]);
  await h.handlers.get('session_start')({ type: 'session_start', reason: 'startup' }, h.ctx);
  await h.flush();
  assert.equal(h.receipt(allowed.id), null);
  assert.equal(h.calls.some(call => call[0] === 'setThinkingLevel'), false);

  h.setIdle(true);
  h.timers[0].callback(); await h.flush();
  assert.ok(h.calls.some(call => call[0] === 'setThinkingLevel' && call[1] === 'high'));
  assert.equal(h.receipt(allowed.id).status, 'delivered');
  assert.deepEqual(h.entries.filter(entry => entry.customType === 'swarm.command').map(entry => entry.data.state), ['executing', 'delivered']);

  const blocked = command('/new');
  h.writeInbox([blocked]);
  h.timers[0].callback(); await h.flush();
  assert.equal(h.receipt(blocked.id).status, 'failed');
  assert.match(h.receipt(blocked.id).error, /not permitted/);
});

test('exported validator enforces command syntax and UUID nonces', () => {
  const env = { SWARM_CONTROL_DIR: '/tmp/no-use', SWARM_CONTROL_EPOCH: randomUUID(), SWARM_AGENT_ID: randomUUID() };
  const { extension } = loadExtension(env);
  assert.equal(extension.validateCommand('/compact preserve decisions').ok, true);
  assert.equal(extension.validateCommand('/model anthropic/claude-sonnet').ok, true);
  assert.equal(extension.validateCommand('/thinking max').ok, true);
  assert.equal(extension.validateCommand('/name Build agent').ok, true);
  for (const text of ['/new', '/fork abc', '/clone', '/resume', '/tree', '/import x', '/quit', '/reload now', '/model broken']) {
    assert.equal(extension.validateCommand(text).ok, false, text);
  }
  assert.equal(extension.isUUID(randomUUID()), true);
  assert.equal(extension.isUUID('../inbox'), false);
  assert.equal(extension.isUUID('not-a-nonce'), false);
});

test('reload runs only through owned internal command and reconciles on new instance', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-mailbox-reload-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const entries = [];
  const first = createHarness({ directory, entries });
  const item = command('/reload');
  first.writeInbox([item]);
  await first.handlers.get('session_start')({ type: 'session_start', reason: 'startup' }, first.ctx);
  await first.flush();
  assert.equal(first.receipt(item.id).status, 'executing');
  const queued = first.calls.find(call => call[0] === 'sendUserMessage');
  assert.equal(queued[1], `/swarm-control-reload ${item.id}`);
  assert.equal(queued[2].deliverAs, 'followUp');
  assert.equal(queued[2].expandPromptTemplates, true);
  await first.commands.get('swarm-control-reload').handler(randomUUID(), first.ctx);
  assert.equal(first.calls.some(call => call[0] === 'reload'), false, 'foreign nonce cannot invoke reload');
  await first.commands.get('swarm-control-reload').handler(item.id, first.ctx);
  assert.equal(first.calls.filter(call => call[0] === 'reload').length, 1);
  assert.ok(entries.some(entry => entry.data?.message_id === item.id && entry.data.state === 'reload_started'));
  await first.handlers.get('session_shutdown')({ type: 'session_shutdown', reason: 'reload' }, first.ctx);

  const second = createHarness({ directory, entries, epoch: first.env.SWARM_CONTROL_EPOCH, agentId: first.env.SWARM_AGENT_ID, sessionId: first.sessionId });
  await second.handlers.get('session_start')({ type: 'session_start', reason: 'reload' }, second.ctx);
  await second.flush();
  assert.equal(second.receipt(item.id).status, 'delivered');
  assert.equal(second.calls.some(call => call[0] === 'sendUserMessage'), false, 'executing reload is never replayed');
});

test('model-callable tool writes one durable request and never invokes the command', async t => {
  const h = createHarness();
  t.after(() => fs.rmSync(h.directory, { recursive: true, force: true }));
  assert.equal(h.tool.name, 'swarm_session_command');
  assert.equal(h.tool.parameters.properties.command.maxLength, 32000);
  const result = await h.tool.execute('tool-call', { command: '/compact retain swarm state' });
  const files = fs.readdirSync(path.join(h.directory, 'requests'));
  assert.equal(files.length, 1);
  assert.match(files[0], /^[0-9a-f-]{36}\.json$/i);
  const request = JSON.parse(fs.readFileSync(path.join(h.directory, 'requests', files[0]), 'utf8'));
  assert.equal(request.id, files[0].slice(0, -5));
  assert.equal(request.kind, 'command');
  assert.equal(request.text, '/compact retain swarm state');
  assert.equal(request.sender_id, h.env.SWARM_AGENT_ID);
  assert.match(request.created_at, /^\d{4}-\d\d-\d\dT/);
  assert.match(result.content[0].text, /Finish the current turn/);
  assert.equal(h.calls.length, 0, 'tool only writes a request for the parent controller');
  await assert.rejects(() => h.tool.execute('tool-call-2', { command: '/quit' }), /not permitted/);
});


test('controller wire contract accepts nullable senders and initially-delivering commands', async t => {
  const h = createHarness(); t.after(() => fs.rmSync(h.directory, {recursive:true,force:true}));
  const ordinary={...message(),sender_id:null}; const cmd={...command('/name Controller contract'),status:'delivering',sender_id:null};
  h.writeInbox([ordinary,cmd]);
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx); await h.flush();
  assert.equal(h.calls.filter(call=>call[0]==='sendMessage').length,1);
  assert.equal(h.receipt(cmd.id).status,'delivered');
  assert.equal(h.calls.find(call=>call[0]==='setSessionName')[1],'Controller contract');
});

test('a process startup cannot falsely acknowledge a merely-started reload', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const cmd=command('/reload');h.writeInbox([cmd]);
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  await h.commands.get('swarm-control-reload').handler(cmd.id,h.ctx);await h.flush();
  assert.equal(h.receipt(cmd.id).status,'executing');
  await h.handlers.get('session_shutdown')({type:'session_shutdown',reason:'quit'},h.ctx);
  const reboot=createHarness({directory:h.directory,epoch:h.env.SWARM_CONTROL_EPOCH,agentId:h.env.SWARM_AGENT_ID,sessionId:h.sessionId,entries:h.entries});
  reboot.writeInbox([cmd]);
  await reboot.handlers.get('session_start')({type:'session_start',reason:'startup'},reboot.ctx);await reboot.flush();
  assert.equal(reboot.receipt(cmd.id).status,'uncertain');
  assert.equal(reboot.calls.filter(call=>call[0]==='sendUserMessage').length,0);
});

test('heartbeat remains live during a long asynchronous session command', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const cmd={...command('/compact Preserve decisions'),status:'delivering'};h.writeInbox([cmd]);
  let finish;h.ctx.compact=options=>{finish=options.onComplete;};
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  assert.equal(h.receipt(cmd.id).status,'executing');
  const file=path.join(h.directory,'heartbeat.json');fs.unlinkSync(file);
  h.timers[0].callback();await h.flush();
  assert.equal(JSON.parse(fs.readFileSync(file)).protocol,1);
  finish({});await h.flush();assert.equal(h.receipt(cmd.id).status,'delivered');
});


test('message_end notification cannot deadlock a compaction waiting on its handlers', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const cmd=command('/compact Keep decisions');h.writeInbox([cmd]);
  h.ctx.compact=options=>{
    Promise.resolve(h.handlers.get('message_end')({type:'message_end'},h.ctx)).then(()=>options.onComplete({}));
  };
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  assert.equal(h.receipt(cmd.id).status,'delivered');
});

test('heartbeat advertises graceful shutdown and lifecycle ignores inbox awake state', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  h.writeInbox([], {awake:false});
  const id=randomUUID();h.writeLifecycle('prepare',{id});
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  const heartbeat=JSON.parse(fs.readFileSync(path.join(h.directory,'heartbeat.json'),'utf8'));
  assert.deepEqual(heartbeat.capabilities,{graceful_shutdown:true});
  assert.equal(heartbeat.idle,true);
  assert.equal(h.lifecycleAck().id,id);
  assert.equal(h.lifecycleAck().state,'ready');
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,0);
});

test('foreign lifecycle requests neither acknowledge nor suppress ordinary delivery', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const item=message();h.writeInbox([item]);
  h.writeLifecycle('commit',{agent_id:randomUUID()});
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  assert.equal(h.lifecycleAck(),null);
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,0);
  assert.equal(h.calls.filter(call=>call[0]==='sendMessage').length,1);

  h.writeLifecycle('commit',{epoch:randomUUID()});h.timers[0].callback();await h.flush();
  h.writeLifecycle('commit',{session_id:randomUUID()});h.timers[0].callback();await h.flush();
  h.writeLifecycle('commit',{id:'not-a-uuid'});h.timers[0].callback();await h.flush();
  assert.equal(h.lifecycleAck(),null);
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,0);
});

test('prepare is changed-only and commit requests native shutdown exactly once', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const id=randomUUID();h.writeLifecycle('prepare',{id});
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  const ready=fs.readFileSync(path.join(h.directory,'lifecycle-ack.json'),'utf8');
  const readyAt=h.lifecycleAck().updated_at;
  h.timers[0].callback();await h.flush();
  assert.equal(fs.readFileSync(path.join(h.directory,'lifecycle-ack.json'),'utf8'),ready);
  assert.equal(h.lifecycleAck().updated_at,readyAt);
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,0);

  h.writeLifecycle('commit',{id});h.timers[0].callback();await h.flush();
  const ack=h.lifecycleAck();
  assert.equal(ack.state,'shutdown_requested');
  assert.equal(ack.id,id);
  assert.equal(ack.agent_id,h.env.SWARM_AGENT_ID);
  assert.equal(ack.epoch,h.env.SWARM_CONTROL_EPOCH);
  assert.equal(ack.session_id,h.sessionId);
  assert.match(ack.instance,/^[0-9a-f-]{36}$/i);
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,1);
  h.timers[0].callback();h.timers[0].callback();await h.flush();
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,1);
});

test('commit waits for an in-flight command without blocking lifecycle polling', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const cmd=command('/compact Preserve lifecycle state');h.writeInbox([cmd]);
  let finish;h.ctx.compact=options=>{finish=options.onComplete;};
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  assert.equal(typeof finish,'function');
  const id=randomUUID();h.writeLifecycle('commit',{id});
  h.timers[0].callback();await h.flush();
  assert.equal(h.lifecycleAck().state,'waiting_for_idle');
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,0);
  finish({});await h.flush();
  h.timers[0].callback();await h.flush();
  assert.equal(h.lifecycleAck().state,'shutdown_requested');
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,1);
});

test('valid lifecycle state suppresses inbox delivery and deleted prepare cancels it', async t => {
  const h=createHarness();t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const item=message();h.writeInbox([item]);
  h.writeLifecycle('prepare');
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  assert.equal(h.lifecycleAck().state,'ready');
  assert.equal(h.calls.filter(call=>call[0]==='sendMessage').length,0);
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,0);
  fs.unlinkSync(path.join(h.directory,'lifecycle.json'));
  h.timers[0].callback();await h.flush();
  assert.equal(h.calls.filter(call=>call[0]==='sendMessage').length,1);
  assert.equal(h.calls.filter(call=>call[0]==='shutdown').length,0);
});

test('unavailable graceful shutdown API produces a durable failed acknowledgement', async t => {
  const h=createHarness({shutdownUnavailable:true});t.after(()=>fs.rmSync(h.directory,{recursive:true,force:true}));
  const id=randomUUID();h.writeLifecycle('commit',{id});
  await h.handlers.get('session_start')({type:'session_start',reason:'startup'},h.ctx);await h.flush();
  assert.equal(h.lifecycleAck().state,'failed');
  assert.match(h.lifecycleAck().error,/unavailable/i);
  assert.equal(h.lifecycleAck().id,id);
  h.timers[0].callback();await h.flush();
  assert.equal(h.lifecycleAck().state,'failed');
});

test('shutdown_requested acknowledgement from a prior instance is not reinvoked', async t => {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'pi-mailbox-lifecycle-'));
  t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const first=createHarness({directory});
  const id=randomUUID();first.writeLifecycle('commit',{id});
  await first.handlers.get('session_start')({type:'session_start',reason:'startup'},first.ctx);await first.flush();
  assert.equal(first.calls.filter(call=>call[0]==='shutdown').length,1);
  const prior=first.lifecycleAck();
  await first.handlers.get('session_shutdown')({type:'session_shutdown',reason:'quit'},first.ctx);

  const second=createHarness({directory,epoch:first.env.SWARM_CONTROL_EPOCH,agentId:first.env.SWARM_AGENT_ID,sessionId:first.sessionId});
  await second.handlers.get('session_start')({type:'session_start',reason:'startup'},second.ctx);await second.flush();
  assert.equal(second.calls.filter(call=>call[0]==='shutdown').length,0);
  assert.deepEqual(second.lifecycleAck(),prior);
});
