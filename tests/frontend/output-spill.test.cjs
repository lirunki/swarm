const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const extensionPath = path.join(__dirname, '../../src/swarm/extensions/output-spill.js');
const extensionSource = fs.readFileSync(extensionPath, 'utf8');

function loadExtension(env) {
  const typeStub = {
    Object(properties) { return { type: 'object', properties }; },
    String(options = {}) { return { type: 'string', ...options }; },
    Integer(options = {}) { return { type: 'integer', ...options }; },
    Optional(schema) { return { ...schema, optional: true }; },
  };
  const context = vm.createContext({ process: { env }, Buffer, console, Date, setTimeout, clearTimeout });
  const module = { exports: {} };
  const localRequire = name => name === 'typebox' ? { Type: typeStub } : require(name);
  const wrapper = vm.runInContext(`(function(require,module,exports,__filename,__dirname){${extensionSource}\n})`, context);
  wrapper(localRequire, module, module.exports, extensionPath, path.dirname(extensionPath));
  return module.exports;
}

function harness(options = {}) {
  const directory = options.directory || fs.mkdtempSync(path.join(os.tmpdir(), 'output-spill-'));
  const archiveDir = options.archiveDir || path.join(directory, 'archives');
  const configPath = options.configPath || path.join(directory, 'output-spill.json');
  const env = {PI_OUTPUT_SPILL_CONFIG:configPath,...(!options.defaultLocation ? {PI_OUTPUT_SPILL_DIR:archiveDir} : {}),...(options.env || {})};
  const extension = loadExtension(env);
  const handlers = new Map(), commands = new Map(), entries = options.entries || [], notifications = [], appendCalls = [];
  let tool;
  const pi = {
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name); },
    registerCommand(name, value) { commands.set(name, value); },
    registerTool(value) { tool = value; },
    appendEntry(customType, data) {
      appendCalls.push([customType, data]);
      if (options.appendError && customType === options.appendError) throw new Error('append failed');
      entries.push({ type: 'custom', customType, data, id: crypto.randomUUID(), parentId: null });
    },
  };
  extension(pi);
  const sessionId = options.sessionId || `custom/session/${crypto.randomUUID()}`;
  const sessionFile = Object.hasOwn(options,'sessionFile') ? options.sessionFile : path.join(directory,'sessions','conversation.jsonl');
  const ctx = {
    hasUI: options.hasUI !== false,
    mode: 'tui',
    ui: { notify(message, level) { notifications.push([message, level]); } },
    sessionManager: {
      getSessionId: () => sessionId,
      getSessionFile: () => sessionFile,
      getSessionDir: () => options.sessionDir ?? path.join(directory,'configured-sessions'),
      getBranch: () => entries.slice(),
    },
  };
  handlers.get('session_start')({ type: 'session_start', reason: 'startup' }, ctx);
  return { directory, archiveDir, configPath, env, extension, handlers, commands, entries, appendCalls, notifications, ctx, sessionId, sessionFile, get tool() { return tool; } };
}

function toolMessage(text, patch = {}) {
  return {
    role: 'toolResult', toolCallId: 'call-1', toolName: 'bash',
    content: [{ type: 'text', text }], details: { renderer: '<raw>', nested: { kept: true } },
    usage: { input: 1, output: 2, cacheRead: 3, cacheWrite: 4, totalTokens: 10, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    nestedCalls: { calls: [{ id: 'call-1/1', name: 'read', status: 'ok' }], complete: true },
    isError: false, timestamp: 1234, durationMs: 9, ...patch,
  };
}

const dispatch = (h, message) => h.handlers.get('message_end')({ type: 'message_end', message }, h.ctx);

function cleanup(t, h) { t.after(() => fs.rmSync(h.directory, { recursive: true, force: true })); }

function archiveReference(h) {
  const call = h.appendCalls.find(item => item[0] === 'swarm.output-spill.archive');
  assert.ok(call, 'archive reference should be appended');
  return call[1];
}

test('factory registers only resources and default lifecycle spills oversized output', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'output-spill-factory-'));
  const configPath = path.join(directory, 'missing', 'output-spill.json');
  const archiveDir = path.join(directory, 'missing', 'archives');
  const extension = loadExtension({ PI_OUTPUT_SPILL_CONFIG: configPath, PI_OUTPUT_SPILL_DIR: archiveDir });
  const handlers = new Map(), commands = new Map(); let tool;
  extension({ on(n, fn) { handlers.set(n, fn); }, registerCommand(n, v) { commands.set(n, v); }, registerTool(v) { tool = v; }, appendEntry() {} });
  assert.equal(fs.existsSync(path.dirname(configPath)), false, 'factory creates no files or directories');
  assert.deepEqual([...handlers.keys()].sort(), ['message_end', 'session_start']);
  assert.ok(commands.has('output-spill'));
  assert.equal(tool.name, 'swarm_read_output');
  assert.equal(tool.annotations.readOnlyHint, true);
  fs.rmSync(directory, { recursive: true, force: true });

  const h = harness(); cleanup(t, h);
  const original = toolMessage('x'.repeat(1024 * 1024 + 1));
  const result = dispatch(h, original);
  assert.equal(original.content[0].text.length, 1024 * 1024 + 1, 'event message is never mutated');
  assert.equal(result.message.role, 'toolResult');
  assert.ok(Buffer.byteLength(result.message.content[0].text) <= 4096);
  assert.match(result.message.content[0].text, /swarm_read_output.*byte offsets/s);
  const ref = archiveReference(h);
  assert.match(path.dirname(ref.file), /session-[0-9a-f]{16}$/);
  assert.equal(fs.statSync(path.dirname(ref.file)).mode & 0o777, 0o700);
  for (const file of [ref.file, ref.jsonFile, ref.metadataFile]) assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});

test('multi-text UTF-8 archive is exact while images and all result metadata survive replacement', t => {
  const h = harness(); cleanup(t, h);
  fs.writeFileSync(h.configPath, JSON.stringify({ maxBytes: 4096, previewBytes: 512 }));
  const image = { type: 'image', data: 'YWJj', mimeType: 'image/png' };
  const original = toolMessage('', {
    content: [{ type: 'text', text: '🙂'.repeat(800) }, image, { type: 'text', text: '尾'.repeat(500) }],
    isError: true,
    foreign: { preserved: 'yes' },
  });
  const snapshot = JSON.stringify(original);
  const result = dispatch(h, original);
  const ref = archiveReference(h);
  assert.equal(fs.readFileSync(ref.file, 'utf8'), `${original.content[0].text}\n${original.content[2].text}`);
  assert.equal(fs.readFileSync(ref.jsonFile, 'utf8'), snapshot);
  assert.deepEqual(JSON.parse(fs.readFileSync(ref.jsonFile)), original);
  const metadata = JSON.parse(fs.readFileSync(ref.metadataFile));
  assert.equal(metadata.session_id, h.sessionId);
  assert.equal(metadata.tool_call_id, original.toolCallId);
  assert.equal(metadata.tool_name, original.toolName);
  assert.equal(metadata.sha256, crypto.createHash('sha256').update(fs.readFileSync(ref.file)).digest('hex'));
  assert.equal(metadata.json_sha256, crypto.createHash('sha256').update(snapshot).digest('hex'));
  assert.equal(result.message.content.length, 2);
  assert.deepEqual(result.message.content[1], image);
  for (const key of ['details', 'usage', 'nestedCalls', 'isError', 'timestamp', 'durationMs', 'foreign']) assert.deepEqual(result.message[key], original[key], key);
  assert.equal(JSON.stringify(original), snapshot, 'input remains byte-for-byte JSON equivalent');
  assert.ok(Buffer.byteLength(result.message.content[0].text) <= 512);
});

test('only finalized outer tool results with text spill; assistants, users, images, and small or reader results do not', t => {
  const h = harness(); cleanup(t, h);
  fs.writeFileSync(h.configPath, JSON.stringify({ maxBytes: 4096, previewBytes: 0 }));
  assert.equal(dispatch(h, { role: 'assistant', content: [{ type: 'text', text: 'x'.repeat(5000) }] }), undefined);
  assert.equal(dispatch(h, { role: 'user', content: [{ type: 'text', text: 'x'.repeat(5000) }] }), undefined);
  assert.equal(dispatch(h, toolMessage('', { content: [{ type: 'image', data: 'x', mimeType: 'image/png' }] })), undefined);
  assert.equal(dispatch(h, toolMessage('small')), undefined);
  assert.equal(dispatch(h, toolMessage('bounded page', { toolName: 'swarm_read_output' })), undefined);
  assert.equal(h.appendCalls.length, 0);
  const outer = dispatch(h, toolMessage('x'.repeat(5000), { nestedCalls: { calls: [{ id: 'call-1/1', name: 'mcp__large', status: 'ok' }], complete: true } }));
  assert.ok(outer);
  assert.equal(h.appendCalls.filter(call => call[0] === 'swarm.output-spill.archive').length, 1, 'nested execution records are not intercepted');
});

test('archive or append failure retains the exact original and warns only once', t => {
  const badRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'output-spill-bad-'));
  const archiveFile = path.join(badRoot, 'not-a-directory');
  fs.writeFileSync(archiveFile, 'occupied');
  const h = harness({ directory: badRoot, archiveDir: archiveFile }); cleanup(t, h);
  fs.writeFileSync(h.configPath, JSON.stringify({ maxBytes: 4096, previewBytes: 0 }));
  const original = toolMessage('x'.repeat(5000));
  assert.equal(dispatch(h, original), undefined);
  assert.equal(dispatch(h, original), undefined);
  assert.equal(h.notifications.filter(item => item[1] === 'warning').length, 1);
  assert.equal(h.appendCalls.length, 0);

  const second = harness({ appendError: 'swarm.output-spill.archive' }); cleanup(t, second);
  fs.writeFileSync(second.configPath, JSON.stringify({ maxBytes: 4096, previewBytes: 0 }));
  assert.equal(dispatch(second, original), undefined);
  const sessionRoot = fs.existsSync(second.archiveDir) ? fs.readdirSync(second.archiveDir) : [];
  assert.equal(sessionRoot.length, 1, 'an uncertain append preserves durable files instead of breaking a possibly committed reference');
  const files = fs.readdirSync(path.join(second.archiveDir, sessionRoot[0]));
  assert.equal(files.length, 3);
});

test('session commands persist overrides, restore branch state, reload, default, status, and save safely', async t => {
  const entries = [];
  const h = harness({ entries }); cleanup(t, h);
  fs.writeFileSync(h.configPath, JSON.stringify({ enabled: true, maxBytes: 8192, previewBytes: 1024, maxArchiveBytes: 9000000 }));
  const command = h.commands.get('output-spill').handler;
  await command('off', h.ctx);
  await command('limit 12KiB', h.ctx);
  await command('preview 4kb', h.ctx);
  assert.deepEqual(JSON.parse(JSON.stringify(entries.filter(e => e.customType === 'swarm.output-spill.config').at(-1).data.overrides)),
    { enabled: false, maxBytes: 12288, previewBytes: 4096 });
  await command('status', h.ctx);
  assert.match(h.notifications.at(-1)[0], /off; limit 12288 bytes; preview 4096/);

  fs.writeFileSync(h.configPath, JSON.stringify({ enabled: true, maxBytes: 16384, previewBytes: 2048, maxArchiveBytes: 9000000 }));
  await command('reload', h.ctx);
  assert.match(h.notifications.at(-1)[0], /off; limit 12288 bytes; preview 4096/, 'reload keeps session overrides');
  await command('save', h.ctx);
  assert.deepEqual(JSON.parse(fs.readFileSync(h.configPath)), { enabled: false, maxBytes: 12288, previewBytes: 4096, maxArchiveBytes: 9000000 });
  assert.deepEqual(JSON.parse(JSON.stringify(entries.at(-1).data.overrides)), {});
  await command('defaults', h.ctx);
  assert.match(h.notifications.at(-1)[0], /off; limit 12288 bytes/);

  const restored = harness({ directory: h.directory, entries, configPath: h.configPath, archiveDir: h.archiveDir });
  await restored.commands.get('output-spill').handler('status', restored.ctx);
  assert.match(restored.notifications.at(-1)[0], /off; limit 12288 bytes; preview 4096/);
});

test('malformed and unknown config uses last valid settings and save refuses to clobber it', async t => {
  const h = harness(); cleanup(t, h);
  fs.writeFileSync(h.configPath, JSON.stringify({ maxBytes: 4096, previewBytes: 0 }));
  assert.ok(dispatch(h, toolMessage('x'.repeat(5000))));
  fs.writeFileSync(h.configPath, '{broken');
  const before = fs.readFileSync(h.configPath, 'utf8');
  assert.ok(dispatch(h, toolMessage('y'.repeat(5000))), 'last valid configuration remains active');
  await h.commands.get('output-spill').handler('save', h.ctx);
  assert.equal(fs.readFileSync(h.configPath, 'utf8'), before);
  assert.match(h.notifications.at(-1)[0], /refusing to overwrite/i);

  fs.writeFileSync(h.configPath, JSON.stringify({ maxBytes: 4096, previewBytes: 0, surprise: true }));
  await h.commands.get('output-spill').handler('save', h.ctx);
  assert.equal(JSON.parse(fs.readFileSync(h.configPath)).surprise, true);
  assert.match(h.notifications.at(-1)[0], /unknown configuration field/);
});

test('reader pages UTF-8 by bytes, verifies ownership/checksums, and rejects arbitrary paths and symlinks', async t => {
  const h = harness(); cleanup(t, h);
  fs.writeFileSync(h.configPath, JSON.stringify({ maxBytes: 4096, previewBytes: 0 }));
  const joined = `${'a'.repeat(4094)}🙂TAIL`;
  dispatch(h, toolMessage(joined));
  const ref = archiveReference(h);
  const first = await h.tool.execute('read-1', { path: ref.file, offset: 4093, limit: 2 }, undefined, undefined, h.ctx);
  assert.ok(first.content[0].text.endsWith('\na'));
  assert.match(first.content[0].text, /next_offset=4094/);
  assert.equal(first.details.offset, 4093);
  const second = await h.tool.execute('read-2', { path: ref.file, offset: first.details.next_offset, limit: 2 }, undefined, undefined, h.ctx);
  assert.ok(second.content[0].text.endsWith('\n🙂'));
  assert.equal(second.details.next_offset, 4098);
  const tail = await h.tool.execute('read-3', { path: ref.file, offset: second.details.next_offset, limit: 64000 }, undefined, undefined, h.ctx);
  assert.ok(tail.content[0].text.endsWith('\nTAIL'));
  assert.match(tail.content[0].text, /eof=true/);
  assert.equal(dispatch(h, toolMessage('', {toolName:'swarm_read_output', ...tail})), undefined, 'bounded reader output never needs an exemption');
  assert.equal(tail.details.eof, true);
  assert.ok(Buffer.byteLength(tail.content[0].text) <= 4096);

  const arbitrary = path.join(h.directory, 'secret.txt'); fs.writeFileSync(arbitrary, 'secret');
  await assert.rejects(() => h.tool.execute('bad', { path: arbitrary }, undefined, undefined, h.ctx), /not an output archive/);
  fs.appendFileSync(ref.file, '!');
  await assert.rejects(() => h.tool.execute('tampered', { path: ref.file }, undefined, undefined, h.ctx), /checksum/);

  const target = path.join(h.directory, 'target'); fs.writeFileSync(target, 'target');
  fs.rmSync(ref.jsonFile); fs.symlinkSync(target, ref.jsonFile);
  await assert.rejects(() => h.tool.execute('symlink', { path: ref.jsonFile }, undefined, undefined, h.ctx), /symlink/);
});

test('location, config, size, archive cap, and traversal helpers enforce bounds', () => {
  const extension = loadExtension({});
  const id = crypto.randomUUID();
  const managed = extension.resolveLocations({ SWARM_CONTROL_DIR: '/home/me/.swarm/control/agent', SWARM_AGENT_ID: id }, '/home/me');
  assert.equal(managed.configPath, path.normalize('/home/me/.swarm/output-spill.json'));
  assert.equal(managed.archiveDirFromEnv, null, 'default archive location must come from the active session, not Swarm home');
  assert.throws(() => extension.resolveLocations({PI_OUTPUT_SPILL_DIR:'relative'},'/tmp'), /absolute/);
  assert.throws(() => extension.resolveLocations({ PI_OUTPUT_SPILL_CONFIG: 'relative.json' }, '/tmp'), /absolute/);
  assert.equal(extension.parseSize('4kb'), 4096);
  assert.equal(extension.parseSize('2 MiB'), 2 * 1024 * 1024);
  assert.throws(() => extension.validateConfig({ maxBytes: 4095 }), /maxBytes/);
  assert.throws(() => extension.validateConfig({ maxBytes: 4096, previewBytes: 4097 }), /previewBytes/);
  assert.throws(() => extension.validateConfig({ maxArchiveBytes: 512 * 1024 * 1024 + 1 }), /maxArchiveBytes/);
  assert.throws(() => extension.validateConfig({ archiveDir: '../escape' }), /absolute/);
  assert.throws(() => extension.validateConfig({ unknown: true }), /unknown/);
});


test('streamed JSON preserves escapes, surrogate boundaries and boxed strings without huge buffer copies', t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  const special=('"\\\n\t\u0000🙂尾'+String.fromCharCode(0xd800)).repeat(5000);
  const original=toolMessage('x'.repeat(2*1024*1024),{details:{special,boxed:new String(special),array:[undefined,NaN,Infinity,new Date('2020-01-01T00:00:00Z')]}});
  const expected=JSON.stringify(original);
  const old=Buffer.from;
  Buffer.from=function(value,...args){if(typeof value==='string'&&value.length>128*1024)throw Error('unbounded buffer copy');return old.call(Buffer,value,...args);};
  let result;
  try {result=dispatch(h,original);} finally {Buffer.from=old;}
  assert.ok(result);
  const ref=archiveReference(h);
  assert.equal(fs.readFileSync(ref.jsonFile,'utf8'),expected);
  assert.equal(fs.readFileSync(ref.file,'utf8'),original.content[0].text);
});

test('reader exposes paging metadata to model, stays below threshold and reuses verified file identity', async t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  dispatch(h,toolMessage('x'.repeat(2*1024*1024)));
  const ref=archiveReference(h),read=fs.readSync;let bytes=0;
  fs.readSync=function(fd,buffer,offset,length,position){const count=read.call(fs,fd,buffer,offset,length,position);if(fs.fstatSync(fd).size>1000000)bytes+=count;return count;};
  try {
    const first=await h.tool.execute('first',{path:ref.file,limit:64000},null,null,h.ctx);
    assert.ok(Buffer.byteLength(first.content[0].text)<=4096);
    assert.match(first.content[0].text,/next_offset=.*total_bytes=.*eof=false/);
    const checked=bytes;assert.ok(checked>2000000);
    const second=await h.tool.execute('second',{path:ref.file,offset:first.details.next_offset,limit:64000},null,null,h.ctx);
    assert.ok(bytes-checked<10000,'next page should not hash the entire file again');
    assert.equal(dispatch(h,toolMessage('',{toolName:'swarm_read_output',...second})),undefined);
  } finally {fs.readSync=read;}
});

test('archives remain readable after offline compaction and changing configured archive roots', async t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  dispatch(h,toolMessage('original'.repeat(1000)));const ref=archiveReference(h);
  const other=path.join(h.directory,'other-archives');
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0,archiveDir:other}));
  const newer=harness({directory:h.directory,configPath:h.configPath,archiveDir:other,sessionId:h.sessionId,entries:[]});
  const page=await newer.tool.execute('recover',{path:ref.file,limit:100},null,null,newer.ctx);
  assert.ok(page.content[0].text.includes('original'));
});

test('file changes cannot make session preview overrides write invalid defaults', async t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:8192,previewBytes:4096}));
  await h.commands.get('output-spill').handler('limit 4KiB',h.ctx);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:65536,previewBytes:32768}));
  await h.commands.get('output-spill').handler('save',h.ctx);
  const saved=JSON.parse(fs.readFileSync(h.configPath));assert.equal(saved.maxBytes,4096);assert.equal(saved.previewBytes,4096);
  fs.writeFileSync(h.configPath,'\ufeff'+JSON.stringify({maxBytes:4096,previewBytes:0}));
  await h.commands.get('output-spill').handler('defaults',h.ctx);
  assert.ok(dispatch(h,toolMessage('x'.repeat(5000))));
});

test('unsafe existing session archive directories retain originals without trusting foreign state', t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  const root=path.join(h.archiveDir,`session-${crypto.createHash('sha256').update(h.sessionId).digest('hex').slice(0,16)}`);
  fs.mkdirSync(root,{recursive:true,mode:0o755});fs.chmodSync(root,0o755);
  const original=toolMessage('x'.repeat(5000));
  assert.equal(dispatch(h,original),undefined);assert.equal(original.content[0].text.length,5000);
});

test('no-text image results and nested tool schemas remain outside interception', t => {
  const h=harness();cleanup(t,h);
  assert.deepEqual([...h.handlers.keys()].sort(),['message_end','session_start']);
  assert.equal(h.handlers.has('tool_result'),false,'nested structured tool results must not be transformed');
  assert.equal(dispatch(h,{role:'assistant',content:[{type:'thinking',thinking:'x'.repeat(2*1024*1024)}]}),undefined);
});


test('session off and threshold overrides survive reload after file preview changes', async t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:8192,previewBytes:4096}));
  await h.commands.get('output-spill').handler('off',h.ctx);
  await h.commands.get('output-spill').handler('limit 4KiB',h.ctx);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:1048576,previewBytes:65536}));
  const again=harness({directory:h.directory,configPath:h.configPath,archiveDir:h.archiveDir,sessionId:h.sessionId,entries:h.entries});
  assert.equal(dispatch(again,toolMessage('x'.repeat(5000))),undefined);
  await again.commands.get('output-spill').handler('status',again.ctx);
  assert.match(again.notifications.at(-1)[0],/off; limit 4096 bytes; preview 4096/);
});

test('image-first output ordering is preserved and small preview targets still retain file references', t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:16}));
  const a={type:'image',mimeType:'image/png',data:'YWJj'},b={type:'image',mimeType:'image/png',data:'ZGVm'};
  const result=dispatch(h,toolMessage('',{isError:true,content:[a,{type:'text',text:'x'.repeat(5000)},b,{type:'text',text:'tail'}]}));
  assert.equal(result.message.content[0],a);assert.equal(result.message.content[2],b);
  assert.match(result.message.content[1].text,/Output file:/);assert.match(result.message.content[1].text,/isError=true/);
  assert.ok(Buffer.byteLength(result.message.content[1].text)<4096);
});

test('a concurrent archive edit during cached page read is rejected', async t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  dispatch(h,toolMessage('x'.repeat(5000)));const ref=archiveReference(h);
  await h.tool.execute('verify',{path:ref.file,limit:20},null,null,h.ctx);
  const read=fs.readSync;let changed=false;
  fs.readSync=function(fd,buffer,...args){const count=read.call(fs,fd,buffer,...args);if(!changed&&buffer.length===24){changed=true;fs.appendFileSync(ref.file,'changed');}return count;};
  try {await assert.rejects(()=>h.tool.execute('race',{path:ref.file,limit:20},null,null,h.ctx),/changed while reading/);}
  finally {fs.readSync=read;}
});


test('native JSON brands and custom primitive conversion are preserved without tag spoofing', t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  const tagged={value:'kept',valueOf:()=> 'wrong',[Symbol.toStringTag]:'String'};
  const noTag={value:'kept'};Object.defineProperty(noTag,Symbol.toStringTag,{get(){throw Error('must not inspect tags');}});
  const string=new String('first');string.toString=()=> 'second';string.valueOf=()=> 'third';
  const boolean=new Boolean(true);boolean.valueOf=()=>false;
  const number=new Number(3);number.valueOf=()=> '17';
  const original=toolMessage('x'.repeat(5000),{details:{tagged,noTag,string,boolean,number}});
  assert.ok(dispatch(h,original));assert.equal(fs.readFileSync(archiveReference(h).jsonFile,'utf8'),JSON.stringify(original));
});

test('real directory durability failures never produce an archive success notice', t => {
  const h=harness();cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  const sync=fs.fsyncSync;
  fs.fsyncSync=function(fd){if(fs.fstatSync(fd).isDirectory())throw Object.assign(Error('disk failure'),{code:'EIO'});return sync.call(fs,fd);};
  try {assert.equal(dispatch(h,toolMessage('x'.repeat(5000))),undefined);}
  finally {fs.fsyncSync=sync;}
  assert.equal(h.appendCalls.length,0);assert.match(h.notifications.at(-1)[0],/original retained/);
});


for (const managed of [false,true]) test(`${managed?'managed':'standalone'} defaults colocate archives beside actual session JSONL`, async t => {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'output-spill-location-'));
  const id=crypto.randomUUID();
  const h=harness({directory,defaultLocation:true,env:{PI_CODING_AGENT_DIR:path.join(directory,'global-pi'),
    ...(managed?{SWARM_CONTROL_DIR:path.join(directory,'swarm','control',id),SWARM_AGENT_ID:id}:{})}});cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  assert.ok(dispatch(h,toolMessage('x'.repeat(5000))));
  const ref=archiveReference(h),root=path.join(path.dirname(h.sessionFile),'output-spills');
  assert.equal(path.dirname(path.dirname(ref.file)),root);
  for(const key of ['jsonFile','metadataFile'])assert.equal(path.dirname(ref[key]),path.dirname(ref.file));
  assert.ok(ref.file.startsWith(path.dirname(h.sessionFile)+path.sep));
  assert.equal(fs.existsSync(path.join(h.ctx.sessionManager.getSessionDir(),'output-spills')),false);
  assert.equal(fs.existsSync(path.join(directory,'global-pi','output-spills')),false);
  assert.equal(fs.existsSync(path.join(directory,'swarm','output-spills')),false);
  await h.commands.get('output-spill').handler('save',h.ctx);
  assert.equal(Object.hasOwn(JSON.parse(fs.readFileSync(h.configPath)),'archiveDir'),false,'derived session folder must not become a shared setting');
});

test('default root follows current file location, while previous colocated archives stay readable', async t => {
  const h=harness({defaultLocation:true});cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  dispatch(h,toolMessage('first'.repeat(1000)));const first=archiveReference(h);
  const next=path.join(h.directory,'Project with spaces 旅行','moved.jsonl');
  h.ctx.sessionManager.getSessionFile=()=>next;
  dispatch(h,toolMessage('second'.repeat(1000)));
  const second=h.appendCalls.filter(v=>v[0]==='swarm.output-spill.archive')[1][1];
  assert.equal(path.dirname(path.dirname(second.file)),path.join(path.dirname(next),'output-spills'));
  assert.notEqual(path.dirname(first.file),path.dirname(second.file));
  const page=await h.tool.execute('old',{path:first.file,limit:20},null,null,h.ctx);
  assert.match(page.content[0].text,/first/);
});

test('missing session file uses Pi session directory, but never guesses an unrelated storage root', t => {
  const h=harness({defaultLocation:true,sessionFile:undefined});cleanup(t,h);
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  assert.ok(dispatch(h,toolMessage('x'.repeat(5000))));
  assert.equal(path.dirname(path.dirname(archiveReference(h).file)),path.join(h.ctx.sessionManager.getSessionDir(),'output-spills'));
  const unavailable=harness({defaultLocation:true,sessionFile:undefined,sessionDir:''});cleanup(t,unavailable);
  fs.writeFileSync(unavailable.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  assert.equal(dispatch(unavailable,toolMessage('x'.repeat(5000))),undefined);
  assert.equal(unavailable.appendCalls.length,0);
  assert.match(unavailable.notifications.at(-1)[0],/session directory is unavailable.*original retained/);
});

test('explicit config and environment roots retain precedence over session colocation', t => {
  const h=harness({defaultLocation:true});cleanup(t,h);
  const configRoot=path.join(h.directory,'file-root');
  fs.writeFileSync(h.configPath,JSON.stringify({maxBytes:4096,previewBytes:0,archiveDir:configRoot}));
  dispatch(h,toolMessage('x'.repeat(5000)));
  assert.equal(path.dirname(path.dirname(archiveReference(h).file)),configRoot);
  const environmentRoot=path.join(h.directory,'env-root');
  const newer=harness({directory:h.directory,defaultLocation:true,env:{PI_OUTPUT_SPILL_DIR:environmentRoot}});
  dispatch(newer,toolMessage('y'.repeat(5000)));
  assert.equal(path.dirname(path.dirname(archiveReference(newer).file)),environmentRoot);
});

test('legacy archives stay readable after changing to colocated defaults and offline compaction', async t => {
  const old=harness();cleanup(t,old);
  fs.writeFileSync(old.configPath,JSON.stringify({maxBytes:4096,previewBytes:0}));
  dispatch(old,toolMessage('legacy'.repeat(1000)));const ref=archiveReference(old);
  const newer=harness({directory:old.directory,defaultLocation:true,sessionId:old.sessionId,entries:[]});
  const page=await newer.tool.execute('legacy',{path:ref.file,limit:20},null,null,newer.ctx);
  assert.match(page.content[0].text,/legacy/);
  assert.ok(fs.existsSync(ref.file),'existing artifacts must not move or disappear');
  dispatch(newer,toolMessage('new'.repeat(2000)));
  assert.equal(path.dirname(path.dirname(archiveReference(newer).file)),path.join(path.dirname(newer.sessionFile),'output-spills'));
});
