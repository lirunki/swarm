const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const extensionPath = path.join(__dirname, '../../src/swarm/extensions/history-reader.js');
const extensionSource = fs.readFileSync(extensionPath, 'utf8');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');

function loadExtension() {
  const Type = {
    Object(properties) { return {type: 'object', properties}; },
    String(options = {}) { return {type: 'string', ...options}; },
    Integer(options = {}) { return {type: 'integer', ...options}; },
    Optional(schema) { return {...schema, optional: true}; },
    Literal(value) { return {const: value}; },
    Union(anyOf, options = {}) { return {anyOf, ...options}; },
  };
  const context = vm.createContext({process, Buffer, console});
  const module = {exports: {}};
  const localRequire = name => name === 'typebox' ? {Type} : require(name);
  const wrapper = vm.runInContext(`(function(require,module,exports,__filename,__dirname){${extensionSource}\n})`, context);
  wrapper(localRequire, module, module.exports, extensionPath, path.dirname(extensionPath));
  return module.exports;
}

function makeHarness(options = {}) {
  const directory = options.directory || fs.mkdtempSync(path.join(os.tmpdir(), 'history-reader-'));
  const sessionId = options.sessionId || `session-${crypto.randomUUID()}`;
  const archiveId = options.archiveId || crypto.randomUUID().replaceAll('-', '');
  const sessionParent = options.sessionParent || path.join(directory, 'sessions');
  fs.mkdirSync(sessionParent, {recursive: true, mode: 0o700});
  const sessionFile = options.sessionFile || path.join(sessionParent, 'conversation.jsonl');
  const actualSource = path.normalize(path.resolve(sessionFile));
  if (!fs.existsSync(actualSource)) fs.writeFileSync(actualSource, '{"type":"session"}\n', {mode: 0o600});
  const sessionHash = sha(sessionId).slice(0, 24);
  const root = path.join(sessionParent, 'context-archives');
  const sessionDirectory = path.join(root, sessionHash);
  const archiveDirectory = path.join(sessionDirectory, archiveId);
  fs.mkdirSync(archiveDirectory, {recursive: true, mode: 0o700});
  for (const item of [root, sessionDirectory, archiveDirectory]) fs.chmodSync(item, 0o700);

  const text = Buffer.from(options.text ?? `start🙂尾\n${'x'.repeat(10000)}`, 'utf8');
  const jsonl = Buffer.from(options.jsonl ?? `${JSON.stringify({role:'user', content:'raw🙂'})}\n${JSON.stringify({role:'assistant', content:'full role'})}\n`, 'utf8');
  const artifacts = {'history.txt': text, 'history.jsonl': jsonl};
  for (const [name, data] of Object.entries(artifacts)) fs.writeFileSync(path.join(archiveDirectory, name), data, {mode: 0o600});
  const manifest = {
    version: 1, archive_id: archiveId, session_id: sessionId, source: actualSource,
    sha256: sha('original'), length: 8,
    files: {
      'history.jsonl': {sha256: sha(jsonl), length: jsonl.length},
      'history.txt': {sha256: sha(text), length: text.length},
      'original.jsonl': {sha256: sha('original'), length: 8},
      'replacement.jsonl': {sha256: sha('replacement'), length: 11},
    },
  };
  const manifestFile = path.join(archiveDirectory, 'manifest.json');
  const writeManifest = () => {
    fs.writeFileSync(manifestFile, JSON.stringify(manifest), {mode: 0o600});
    fs.chmodSync(manifestFile, 0o600);
  };
  writeManifest();

  let tool;
  const extension = loadExtension();
  extension({registerTool(value) { tool = value; }});
  const ctx = {sessionManager: {
    getSessionId: () => sessionId,
    getSessionFile: () => actualSource,
  }};
  const execute = params => tool.execute('history-call', params, undefined, undefined, ctx);
  return {directory, sessionId, archiveId, sessionFile: actualSource, actualSource, root, sessionDirectory, archiveDirectory,
    manifest, manifestFile, writeManifest, text, jsonl, tool, ctx, execute};
}

function cleanup(t, harness) {
  t.after(() => fs.rmSync(harness.directory, {recursive: true, force: true}));
}

function rewriteArtifact(h, name, data, updateManifest = true) {
  const filename = path.join(h.archiveDirectory, name);
  const replacement = `${filename}.new`;
  fs.writeFileSync(replacement, data, {mode: 0o600});
  fs.renameSync(replacement, filename);
  if (updateManifest) {
    h.manifest.files[name] = {sha256: sha(data), length: data.length};
    h.writeManifest();
  }
}

test('factory only registers the stable read-only ID-based model tool', () => {
  const extension = loadExtension();
  assert.equal(extension.default, extension);
  let tool;
  extension({registerTool(value) { tool = value; }});
  assert.equal(tool.name, 'swarm_read_history');
  assert.equal(tool.annotations.readOnlyHint, true);
  assert.equal(tool.annotations.openWorldHint, false);
  assert.deepEqual(Object.keys(tool.parameters.properties).sort(), ['archive_id', 'format', 'limit', 'offset']);
  assert.equal(tool.parameters.properties.archive_id.pattern, '^[0-9a-f]{32}$');
  assert.equal(tool.parameters.properties.limit.maximum, 64000);
  assert.equal(tool.parameters.properties.limit.default, 8000);
  assert.equal(extensionSource.includes('session_start'), false, 'the tool reads the current execution context');
});

test('pages text on normalized UTF-8 byte cursors and returns bounded continuation metadata', async t => {
  const h = makeHarness({text: `abc🙂尾TAIL${'z'.repeat(9000)}`}); cleanup(t, h);
  const insideEmoji = await h.execute({archive_id: h.archiveId, offset: 4, limit: 1});
  assert.equal(insideEmoji.details.offset, 7, 'continuation-byte offsets advance to the next code point');
  assert.equal(insideEmoji.content[0].text.endsWith('\n尾'), true);
  assert.equal(insideEmoji.details.next_offset, 10);
  const next = await h.execute({archive_id: h.archiveId, offset: insideEmoji.details.next_offset, limit: 4});
  assert.equal(next.content[0].text.endsWith('\nTAIL'), true);
  assert.equal(next.details.next_offset, 14);
  const beyond = await h.execute({archive_id: h.archiveId, offset: 999999, limit: 10});
  assert.equal(beyond.details.offset, h.text.length);
  assert.equal(beyond.details.eof, true);
  assert.ok(Buffer.byteLength(next.content[0].text) < 500, 'result contains only a bounded page and header');
  assert.equal(next.content[0].text.includes(h.sessionFile), false, 'native paths are never disclosed');
});

test('returns raw jsonl pages without parsing away full roles', async t => {
  const raw = `${JSON.stringify({role:'toolResult', content:[{type:'text', text:'FULL'}], isError:true})}\n`;
  const h = makeHarness({jsonl: raw}); cleanup(t, h);
  const page = await h.execute({archive_id: h.archiveId, format: 'jsonl', limit: 64000});
  assert.equal(page.content[0].text.slice(page.content[0].text.indexOf('\n') + 1), raw);
  assert.equal(page.details.format, 'jsonl');
  assert.equal(page.details.eof, true);
});

test('rejects invalid IDs, cross-session archives, source mismatch, and missing manifests', async t => {
  const h = makeHarness(); cleanup(t, h);
  await assert.rejects(() => h.execute({archive_id: '../secret'}), /archive_id/);
  const originalId = h.ctx.sessionManager.getSessionId;
  h.ctx.sessionManager.getSessionId = () => 'another-session';
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /ENOENT|archive/i);
  h.ctx.sessionManager.getSessionId = originalId;
  h.manifest.session_id = 'another-session'; h.writeManifest();
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /current session/);
  h.manifest.session_id = h.sessionId; h.manifest.source = path.join(path.dirname(h.actualSource), 'other.jsonl'); h.writeManifest();
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /source/);
  h.manifest.source = h.actualSource; h.writeManifest(); fs.rmSync(h.manifestFile);
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), error => {
    assert.match(error.message, /unavailable/);
    assert.equal(error.message.includes(h.actualSource), false, 'filesystem errors do not disclose native paths');
    return true;
  });
});

test('requires a nonempty canonical absolute manifest source bound to the normalized context source', async t => {
  const h = makeHarness(); cleanup(t, h);
  assert.equal(h.manifest.source, h.actualSource, 'published fixtures always bind source to the real context path');
  assert.equal(path.isAbsolute(h.manifest.source), true);
  assert.equal(path.normalize(h.manifest.source), h.manifest.source);

  const rejected = [
    ['missing', manifest => { delete manifest.source; }],
    ['null', manifest => { manifest.source = null; }],
    ['empty', manifest => { manifest.source = ''; }],
    ['relative', manifest => { manifest.source = path.basename(h.actualSource); }],
    ['noncanonical', manifest => {
      manifest.source = `${path.dirname(h.actualSource)}${path.sep}unused${path.sep}..${path.sep}${path.basename(h.actualSource)}`;
    }],
  ];
  for (const [label, mutate] of rejected) {
    await t.test(`rejects ${label} source`, async () => {
      h.manifest.source = h.actualSource;
      mutate(h.manifest); h.writeManifest();
      await assert.rejects(() => h.execute({archive_id: h.archiveId}), /source/);
    });
  }

  h.manifest.source = h.actualSource; h.writeManifest();
  h.ctx.sessionManager.getSessionFile = () =>
    `${path.dirname(h.actualSource)}${path.sep}unused${path.sep}..${path.sep}${path.basename(h.actualSource)}`;
  const page = await h.execute({archive_id: h.archiveId, limit: 1});
  assert.equal(page.details.next_offset > 0, true, 'the context source is normalized before binding');
});

test('validates manifest schema, selected file descriptors, exact lengths, and hashes', async t => {
  const h = makeHarness(); cleanup(t, h);
  h.manifest.version = 2; h.writeManifest();
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /manifest/);
  h.manifest.version = 1; delete h.manifest.files['history.txt']; h.writeManifest();
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /readable history/);
  h.manifest.files['history.txt'] = {sha256: 'bad', length: h.text.length}; h.writeManifest();
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /descriptor/);
  h.manifest.files['history.txt'] = {sha256: sha(h.text), length: h.text.length - 1}; h.writeManifest();
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /length/);
  h.manifest.files['history.txt'].length = h.text.length; h.writeManifest();
  fs.appendFileSync(path.join(h.archiveDirectory, 'history.txt'), '!');
  h.manifest.files['history.txt'].length += 1; h.writeManifest();
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /checksum/);
});

test('rejects symlink, nonregular, nonprivate, and noncanonical archive paths', async t => {
  const h = makeHarness(); cleanup(t, h);
  const selected = path.join(h.archiveDirectory, 'history.txt');
  fs.rmSync(selected); fs.symlinkSync(h.actualSource, selected);
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /regular file/);

  const privateHarness = makeHarness(); cleanup(t, privateHarness);
  if (process.platform !== 'win32') {
    fs.chmodSync(privateHarness.archiveDirectory, 0o755);
    await assert.rejects(() => privateHarness.execute({archive_id: privateHarness.archiveId}), /private/);
    assert.equal(fs.statSync(privateHarness.archiveDirectory).mode & 0o777, 0o755,
      'reader rejects rather than chmodding an unsafe archive directory');
  }

  const linkedBase = fs.mkdtempSync(path.join(os.tmpdir(), 'history-reader-link-'));
  t.after(() => fs.rmSync(linkedBase, {recursive: true, force: true}));
  const real = path.join(linkedBase, 'real'); fs.mkdirSync(real, {mode: 0o700});
  const linked = path.join(linkedBase, 'linked'); fs.symlinkSync(real, linked, process.platform === 'win32' ? 'junction' : 'dir');
  const noncanonical = makeHarness({directory: linkedBase, sessionParent: linked});
  await assert.rejects(() => noncanonical.execute({archive_id: noncanonical.archiveId}), /canonical/);
});

test('rejects world-accessible generated directories and manifest without chmod repair', async t => {
  if (process.platform === 'win32') { t.skip('POSIX permission bits are not available'); return; }
  for (const key of ['root', 'sessionDirectory', 'archiveDirectory']) {
    const h = makeHarness(); cleanup(t, h);
    fs.chmodSync(h[key], 0o755);
    await assert.rejects(() => h.execute({archive_id: h.archiveId}), /private/, key);
    assert.equal(fs.statSync(h[key]).mode & 0o777, 0o755, `${key} mode must not be repaired`);
  }

  const manifest = makeHarness(); cleanup(t, manifest);
  fs.chmodSync(manifest.manifestFile, 0o644);
  await assert.rejects(() => manifest.execute({archive_id: manifest.archiveId}), /private/);
  assert.equal(fs.statSync(manifest.manifestFile).mode & 0o777, 0o644,
    'manifest mode must not be repaired');
});

test('rejects generated root and session job directories owned by another user', async t => {
  if (typeof process.getuid !== 'function') { t.skip('owner IDs are not available'); return; }
  for (const key of ['root', 'sessionDirectory', 'archiveDirectory']) {
    const h = makeHarness(); cleanup(t, h);
    const target = h[key];
    const originalLstat = fs.lstatSync;
    fs.lstatSync = function(filename, options) {
      const stat = originalLstat.call(fs, filename, options);
      if (filename !== target) return stat;
      return new Proxy(stat, {get(value, property) {
        if (property === 'uid') return value.uid + (typeof value.uid === 'bigint' ? 1n : 1);
        const member = Reflect.get(value, property, value);
        return typeof member === 'function' ? member.bind(value) : member;
      }});
    };
    try {
      await assert.rejects(() => h.execute({archive_id: h.archiveId}), /owned by the current user/, key);
    } finally {
      fs.lstatSync = originalLstat;
    }
  }
});

test('hashes once per unchanged bigint file identity and never trusts a replaced cache entry', async t => {
  const h = makeHarness({text: 'x'.repeat(2 * 1024 * 1024)}); cleanup(t, h);
  const originalRead = fs.readSync;
  let hashedBytes = 0;
  fs.readSync = function(fd, buffer, offset, length, position) {
    const count = originalRead.call(fs, fd, buffer, offset, length, position);
    try { if (fs.fstatSync(fd).size === h.text.length && length >= 1024 * 1024) hashedBytes += count; } catch (_) {}
    return count;
  };
  try {
    await h.execute({archive_id: h.archiveId, limit: 20});
    const firstHash = hashedBytes;
    assert.ok(firstHash >= h.text.length);
    await h.execute({archive_id: h.archiveId, offset: 20, limit: 20});
    assert.equal(hashedBytes, firstHash, 'unchanged identity reuses its verified digest');
    const changed = Buffer.from('y'.repeat(h.text.length));
    rewriteArtifact(h, 'history.txt', changed, false);
    await assert.rejects(() => h.execute({archive_id: h.archiveId, limit: 20}), /checksum/);
    assert.ok(hashedBytes >= firstHash + changed.length, 'replacement identity is hashed again');
  } finally { fs.readSync = originalRead; }
});

test('detects selected-file and manifest replacement during a cached page read', async t => {
  const h = makeHarness({text: 'a'.repeat(5000)}); cleanup(t, h);
  await h.execute({archive_id: h.archiveId, limit: 20});
  const originalRead = fs.readSync;
  let changed = false;
  fs.readSync = function(fd, buffer, offset, length, position) {
    const count = originalRead.call(fs, fd, buffer, offset, length, position);
    if (!changed && position === 100 && length > 4) {
      changed = true;
      fs.appendFileSync(path.join(h.archiveDirectory, 'history.txt'), 'race');
    }
    return count;
  };
  try {
    await assert.rejects(() => h.execute({archive_id: h.archiveId, offset: 100, limit: 20}), /changed while reading/);
  } finally { fs.readSync = originalRead; }

  const m = makeHarness({text: 'b'.repeat(5000)}); cleanup(t, m);
  await m.execute({archive_id: m.archiveId, limit: 20});
  let manifestChanged = false;
  fs.readSync = function(fd, buffer, offset, length, position) {
    const count = originalRead.call(fs, fd, buffer, offset, length, position);
    if (!manifestChanged && position === 200 && length > 4) {
      manifestChanged = true;
      m.writeManifest();
    }
    return count;
  };
  try {
    await assert.rejects(() => m.execute({archive_id: m.archiveId, offset: 200, limit: 20}), /manifest changed/);
  } finally { fs.readSync = originalRead; }
});

test('enforces 1 MiB manifests, 128 MiB files, and argument caps before unbounded reads', async t => {
  const h = makeHarness(); cleanup(t, h);
  fs.writeFileSync(h.manifestFile, Buffer.alloc(1024 * 1024 + 1), {mode: 0o600});
  await assert.rejects(() => h.execute({archive_id: h.archiveId}), /size limit/);

  const big = makeHarness(); cleanup(t, big);
  const selected = path.join(big.archiveDirectory, 'history.txt');
  fs.truncateSync(selected, 128 * 1024 * 1024 + 1);
  big.manifest.files['history.txt'] = {sha256: sha('unused'), length: 128 * 1024 * 1024}; big.writeManifest();
  await assert.rejects(() => big.execute({archive_id: big.archiveId}), /size limit|length/);
  await assert.rejects(() => big.execute({archive_id: big.archiveId, offset: -1}), /offset/);
  await assert.rejects(() => big.execute({archive_id: big.archiveId, limit: 64001}), /limit/);
  await assert.rejects(() => big.execute({archive_id: big.archiveId, format: 'original.jsonl'}), /format/);
});


test('native agent-source binding supports a controller path translated at the process boundary', async t => {
  const h=makeHarness();cleanup(t,h);
  const value=JSON.parse(fs.readFileSync(h.manifestFile,'utf8'));
  value.source='/controller-side/session.jsonl';value.agent_source=h.actualSource;
  fs.writeFileSync(h.manifestFile,JSON.stringify(value));
  const result=await h.tool.execute('translated',{archive_id:h.archiveId,limit:20},null,null,h.ctx);
  assert.ok(result.content[0].text);
  value.agent_source='/different-native/session.jsonl';fs.writeFileSync(h.manifestFile,JSON.stringify(value));
  await assert.rejects(()=>h.tool.execute('wrong-native',{archive_id:h.archiveId},null,null,h.ctx),/source/);
});
