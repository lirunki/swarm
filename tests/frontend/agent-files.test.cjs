const test = require('node:test');
const assert = require('node:assert/strict');
const {AgentFilesDialog, canInsert, clipboardFile, clipboardFiles} = require('../../src/swarm/static/agent-files.js');
const ID = '11111111-1111-4111-8111-111111111111';
class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.events = new Map(); this.open = false; this.hidden = false; this.disabled = false; this.value = ''; this.files = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(type, fn) { this.events.set(type, fn); }
  click() { this.clicked = (this.clicked || 0) + 1; return this.events.get('click')?.({currentTarget: this}); }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.get('close')?.(); }
}
const flatten = element => [element, ...element.children.filter(item => item instanceof Element).flatMap(flatten)];
function fixture(api, options = {}) {
  const ids = ['files-dialog', 'agent-file-list', 'refresh-agent-files', 'files-title', 'files-workspace', 'agent-files-error', 'agent-file-count',
    'agent-file-upload-input', 'upload-agent-file', 'paste-agent-file', 'cancel-agent-file-upload', 'agent-file-upload-status', 'agent-file-upload-progress', 'agent-file-upload-message', 'agent-files-insert-status'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, new Element(id.includes('input') ? 'input' : id.includes('progress') ? 'progress' : 'div')]));
  const calls = [], changed = [], inserted = [];
  const controller = new AgentFilesDialog({
    query: id => elements[id],
    create: (tag, cls, text) => Object.assign(new Element(tag), {className: cls, textContent: text}),
    api: async (...args) => { calls.push(args); return api(...args); },
    onChange: async () => changed.push(true),
    onInsert: (id, base) => inserted.push([id, base]),
    confirm: options.confirm || (() => true), readClipboard: options.readClipboard,
  });
  return {controller, elements, calls, changed, inserted};
}
const agent = {id: 'a', name: 'Research', files_version: 1, alive: true, managed: true};
const file = {id: ID, label: '<script>literal</script>', filename: 'chart.png', size: 12, media_type: 'image/png', available: true};
const makeFile = (name, values) => {
  const data = Uint8Array.from(values);
  return {name, size: data.length, slice: (start, end) => ({arrayBuffer: async () => data.slice(start, end).buffer})};
};
const tick = () => new Promise(resolve => setImmediate(resolve));

test('dialog loads only metadata and keeps existing open and download links on the captured workspace', async () => {
  const base = '/api/federation/beta/proxy/api/agents/a/files';
  const t = fixture(async () => ({files: [file], revision: 1}));
  await t.controller.open(agent, base, 'Beta');
  assert.deepEqual(t.calls, [[base]]); assert.equal(t.elements['#files-workspace'].textContent, 'Beta');
  const nodes = flatten(t.elements['#agent-file-list'].children[0]);
  assert.equal(nodes.find(node => node.tag === 'strong').textContent, file.label);
  assert.equal(nodes.some(node => node.tag === 'script'), false);
  const links = nodes.filter(node => node.tag === 'a');
  assert.equal(links[0].href, `${base}/${ID}/content`); assert.equal(links[0].target, '_blank');
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert.equal(links[1].href, `${base}/${ID}/content?download=true`);
  assert.ok(nodes.some(node => node.textContent === 'Insert'));
  assert.ok(nodes.some(node => node.textContent === 'Remove reference'));
});

test('upload button preserves the user gesture and clears a selection for choosing the same file again', async () => {
  const t = fixture(async () => ({files: [], revision: 1}));
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  t.elements['#upload-agent-file'].click();
  assert.equal(t.elements['#agent-file-upload-input'].clicked, 1);
  let selected;
  t.controller.upload = async value => { selected = value; };
  const chosen = makeFile('repeat.txt', [1]);
  const input = t.elements['#agent-file-upload-input']; input.files = [chosen]; input.value = 'fake-path';
  input.events.get('change')({currentTarget: input}); await tick();
  assert.equal(selected, chosen); assert.equal(input.value, '');
});

test('upload chunks base64 bytes at the server limit, registers only, and never auto-inserts', async () => {
  const base = '/api/federation/beta/proxy/api/agents/a/files'; let uid; let registered = false;
  const t = fixture(async (path, method, body) => {
    if (method === 'GET' || method === undefined) return {files: registered ? [file] : [], revision: registered ? 2 : 1};
    if (path === `${base}/uploads`) { uid = body.upload_id; return {upload_id: uid, offset: 0, chunk_bytes: 3, status: 'receiving'}; }
    if (path.endsWith('/chunks')) return {offset: body.offset + Buffer.from(body.data, 'base64').length};
    if (path.endsWith('/finish')) { registered = true; return {file, revision: 2, agent_id: 'a'}; }
    throw Error(`Unexpected ${method} ${path}`);
  });
  await t.controller.open(agent, base, 'Beta');
  await t.controller.upload(makeFile('payload.bin', [0, 1, 2, 253, 254, 255, 7]));
  const chunks = t.calls.filter(call => call[0].endsWith('/chunks'));
  assert.deepEqual(chunks.map(call => call[2].offset), [0, 3, 6]);
  assert.deepEqual(chunks.flatMap(call => [...Buffer.from(call[2].data, 'base64')]), [0, 1, 2, 253, 254, 255, 7]);
  assert.ok(t.calls.some(call => call[0] === `${base}/uploads/${uid}/finish` && call[1] === 'POST'));
  assert.deepEqual(t.inserted, []); // Upload saves/registers; only an explicit Insert invokes this callback.
  assert.equal(t.changed.length, 1); assert.equal(t.controller.files.length, 1);
  assert.match(t.elements['#agent-file-upload-message'].textContent, /Use Insert/);
  assert.equal(t.elements['#upload-agent-file'].disabled, false);
});

test('oversized uploads are rejected before any upload request', async () => {
  const t = fixture(async () => ({files: [], revision: 1}));
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  await t.controller.upload({name: 'huge.bin', size: 128 * 1024 * 1024 + 1});
  assert.equal(t.calls.length, 1); assert.match(t.elements['#agent-files-error'].textContent, /128\.0 MiB/);
});

test('closing during upload aborts the partial against its captured base and ignores stale completion', async () => {
  const base = '/api/federation/old/proxy/api/agents/a/files'; let resolveStart;
  const t = fixture(async (path, method, body) => {
    if (!method) return {files: [], revision: 1};
    if (path === `${base}/uploads`) return new Promise(resolve => { resolveStart = () => resolve({upload_id: body.upload_id, offset: 0, chunk_bytes: 3, status: 'receiving'}); });
    if (method === 'DELETE') return {};
    throw Error('stale upload continued');
  });
  await t.controller.open(agent, base, 'Old');
  const pending = t.controller.upload(makeFile('old.bin', [1, 2, 3])); await tick();
  const uid = t.calls.find(call => call[0] === `${base}/uploads`)[2].upload_id;
  t.elements['#files-dialog'].close(); resolveStart(); await pending;
  assert.ok(t.calls.some(call => call[0] === `${base}/uploads/${uid}` && call[1] === 'DELETE'));
  assert.equal(t.calls.some(call => call[0].endsWith('/chunks')), false);
  assert.deepEqual(t.inserted, []);
});

test('an uncertain finish is not retried or deleted and releases busy controls', async () => {
  let uid;
  const t = fixture(async (path, method, body) => {
    if (!method) return {files: [], revision: 1};
    if (path.endsWith('/uploads')) { uid = body.upload_id; return {upload_id: uid, offset: 0, chunk_bytes: 8, status: 'receiving'}; }
    if (path.endsWith('/chunks')) return {offset: 2};
    if (path.endsWith('/finish')) throw Error('connection lost');
    if (method === 'DELETE') return {};
  });
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  await t.controller.upload(makeFile('maybe.txt', [4, 5]));
  assert.match(t.elements['#agent-files-error'].textContent, /uncertain.*Refresh/i);
  assert.equal(t.calls.filter(call => call[0].endsWith('/finish')).length, 1);
  assert.equal(t.calls.some(call => call[1] === 'DELETE'), false);
  assert.equal(t.elements['#upload-agent-file'].disabled, false);
});

test('explicit insert posts once, displays only the returned path, and invokes the selected agent callback', async () => {
  const base = '/api/agents/a/files';
  const t = fixture(async (path, method, body) => method === 'POST'
    ? {inserted: true, path: '/safe/returned/chart.png', text: '@file:/safe/returned/chart.png', file_id: ID}
    : {files: [file], revision: 1});
  await t.controller.open(agent, base, 'Local');
  const insert = flatten(t.elements['#agent-file-list']).find(node => node.textContent === 'Insert');
  await t.controller.insert(file, insert);
  assert.ok(t.calls.some(call => call[0] === `${base}/${ID}/insert` && call[1] === 'POST' && Object.keys(call[2]).length === 0));
  assert.deepEqual(t.inserted, [['a', base]]);
  assert.match(t.elements['#agent-files-insert-status'].textContent, /\/safe\/returned\/chart\.png/);
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1); // no retry, wake, Enter, or second paste
});

test('late insert success from a closed workspace cannot navigate or target the next selection', async () => {
  let resolveInsert;
  const oldBase = '/api/federation/old/proxy/api/agents/a/files';
  const t = fixture(async (path, method) => {
    if (method === 'POST') return new Promise(resolve => { resolveInsert = resolve; });
    return {files: path === oldBase ? [file] : [], revision: path === oldBase ? 1 : 2};
  });
  await t.controller.open(agent, oldBase, 'Old');
  const pending = t.controller.insert(file, flatten(t.elements['#agent-file-list']).find(node => node.textContent === 'Insert'));
  t.elements['#files-dialog'].close();
  await t.controller.open({...agent, id: 'b', name: 'Next', files_version: 2}, '/api/agents/b/files', 'New');
  resolveInsert({inserted: true, path: '/old/path', text: '@file:/old/path', file_id: ID}); await pending;
  assert.deepEqual(t.inserted, []); assert.equal(t.controller.agentId, 'b');
  assert.equal(t.elements['#agent-files-insert-status'].textContent, '');
});

test('insert availability tracks live managed and busy state, while backend errors advise checking terminal', async () => {
  assert.equal(canInsert(agent), true);
  for (const update of [{alive: false}, {managed: false}, {spawn_state: 'provisioning'}, {stop_pending: true}, {orchestration_stop: {reason: 'parent'}}, {meta_compaction: {status: 'running'}}, {tool_input_compaction: {status: 'running'}}]) {
    assert.equal(canInsert({...agent, ...update}), false);
  }
  const t = fixture(async (path, method) => { if (method === 'POST') throw Error('network down'); return {files: [file], revision: 1}; });
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  t.controller.sync({...agent, stop_pending: true});
  assert.equal(flatten(t.elements['#agent-file-list']).find(node => node.textContent === 'Insert').disabled, true);
  t.controller.sync(agent);
  await t.controller.insert(file, flatten(t.elements['#agent-file-list']).find(node => node.textContent === 'Insert'));
  assert.match(t.elements['#agent-files-error'].textContent, /Check the terminal before retrying.*may already have succeeded/i);
  assert.deepEqual(t.inserted, []); assert.equal(t.controller.inserting, null);
});

test('unavailable and invalid IDs never get content or action links', async () => {
  const t = fixture(async () => ({files: [{...file, available: false, error: 'Original deleted'}, {...file, id: '../elsewhere'}], revision: 1}));
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  assert.equal(flatten(t.elements['#agent-file-list']).some(node => node.tag === 'a'), false);
  assert.ok(flatten(t.elements['#agent-file-list']).some(node => node.textContent === 'Original deleted'));
});

test('metadata revisions queue refreshes and late fetches cannot affect another agent', async () => {
  let count = 0, resolve;
  const t = fixture(async base => {
    count++;
    if (count === 2) return new Promise(done => { resolve = done; });
    return {files: count === 1 ? [] : [file], revision: count === 1 ? 1 : 3};
  });
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  t.controller.sync({...agent, files_version: 2}); t.controller.sync({...agent, files_version: 3});
  resolve({files: [], revision: 2}); await tick();
  assert.equal(count, 3); assert.equal(t.controller.revision, 3); assert.equal(t.controller.files.length, 1);
});

test('missing API remains explicit and remove still confirms, refreshes, and preserves originals', async () => {
  const missing = fixture(async () => { throw Error('Not Found'); });
  await missing.controller.open(agent, '/api/agents/a/files', 'Local');
  assert.match(missing.elements['#agent-files-error'].textContent, /restart/); assert.equal(missing.elements['#refresh-agent-files'].disabled, false);

  let deleted = false;
  const t = fixture(async (base, method) => { if (method === 'DELETE') { deleted = true; return {removed: ID}; } return {files: deleted ? [] : [file], revision: deleted ? 2 : 1}; });
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  await t.controller.remove(file, new Element('button'));
  assert.ok(t.calls.some(args => args[0] === `/api/agents/a/files/${ID}` && args[1] === 'DELETE'));
  assert.equal(t.changed.length, 1); assert.equal(t.controller.files.length, 0);

  const canceled = fixture(async () => ({files: [file], revision: 1}), {confirm: () => false});
  await canceled.controller.open(agent, '/api/agents/a/files', 'Local');
  await canceled.controller.remove(file, new Element('button')); assert.equal(canceled.calls.length, 1);
  canceled.controller.sync(null); assert.equal(canceled.elements['#files-dialog'].open, false);
});


test('a file chosen from an older picker never uploads into a newly opened workspace', async () => {
  const t = fixture(async () => ({files: [], revision: 1}));
  await t.controller.open(agent, '/api/federation/old/proxy/api/agents/a/files', 'Old');
  t.elements['#upload-agent-file'].click();
  await t.controller.open({...agent, id: 'b'}, '/api/federation/new/proxy/api/agents/b/files', 'New');
  let uploads = 0; t.controller.upload = async () => { uploads++; };
  const input = t.elements['#agent-file-upload-input']; input.files = [makeFile('chosen.txt', [1,2])];
  input.events.get('change')({currentTarget: input}); await tick();
  assert.equal(uploads, 0);
  assert.equal(t.calls.filter(c => c[1] === 'POST').length, 0);
});


test('uploaded copies require explicit deletion confirmation', async () => {
  let question;
  const uploaded = {...file, uploaded: true};
  const t = fixture(async () => ({files: [uploaded], revision: 1}), {confirm: value => { question = value; return false; }});
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  const button = flatten(t.elements['#agent-file-list']).find(el => el.textContent === 'Remove upload');
  await button.click();
  assert.match(question, /delete its uploaded temporary copy/);
  assert.match(question, /browser's computer is NOT deleted/);
  assert.equal(t.calls.filter(c => c[1] === 'DELETE').length, 0);
});

test('pending upload cleanup uses saved target, not current workspace, and ignores unsafe URLs', async () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  const owner = '22222222-2222-4222-8222-222222222222';
  const base = `/api/federation/old-peer/proxy/api/agents/${owner}/files`;
  let stored = JSON.stringify([{id: ID, base}, {id: ID, base: 'https://evil.example/api/agents/a/files'}]);
  Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: {getItem: () => stored, setItem: (key, value) => { stored = value; }}});
  try {
    const t = fixture(async (path, method) => method === 'DELETE' ? {cancelled: true} : {files: [], revision: 1});
    await tick();
    assert.deepEqual(t.calls, [[`${base}/uploads/${ID}`, 'DELETE']]);
    assert.deepEqual(JSON.parse(stored), []);
    assert.equal(t.inserted.length, 0);
  } finally {
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous); else delete globalThis.sessionStorage;
  }
});


function pasteEvent(files, options = {}) {
  return {clipboardData: {files, items: options.items || []}, target: options.target || {},
    prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
}

test('native clipboard file paste reuses upload only and does not duplicate file representations', async () => {
  let reads = 0;
  const t = fixture(async () => ({files: [], revision: 1}), {readClipboard: async () => { reads++; return []; }});
  await t.controller.open(agent, '/api/federation/beta/proxy/api/agents/a/files', 'Beta');
  assert.equal(reads, 0);
  const file = makeFile('copied.pdf', [1,2,3]);
  const event = pasteEvent([file], {items: [{kind: 'file', getAsFile: () => file}]});
  let selected; t.controller.upload = async value => { selected = value; };
  await t.controller.handlePaste(event);
  assert.equal(selected, file); assert.equal(reads, 0);
  assert.equal(event.prevented, true); assert.equal(event.stopped, true);
  assert.deepEqual(t.inserted, []);
  assert.deepEqual(clipboardFiles({items: [{kind: 'file', getAsFile: () => file}]}), [file]);
});

test('paste is scoped to Files and never turns text or copied paths into uploads', async () => {
  const t = fixture(async () => ({files: [], revision: 1}));
  let uploads = 0; t.controller.upload = async () => { uploads++; };
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  const editable = pasteEvent([makeFile('x.png', [1])], {target: {closest: () => ({})}});
  await t.controller.handlePaste(editable);
  assert.equal(editable.prevented, false);
  const text = pasteEvent([], {items: [{kind: 'string', getAsString: () => { throw Error('text must not be read'); }}]});
  await t.controller.handlePaste(text);
  assert.equal(text.prevented, false);
  assert.match(t.elements['#agent-files-error'].textContent, /Text, URLs and copied paths are not uploaded/);
  t.elements['#files-dialog'].close();
  const outside = pasteEvent([makeFile('outside.png', [1])]);
  await t.controller.handlePaste(outside);
  assert.equal(outside.prevented, false); assert.equal(uploads, 0);
});

test('clipboard button chooses one image representation and generates a safe filename', async () => {
  const png = new Blob([Uint8Array.from([1,2,3])], {type: 'image/png'});
  const requested = [];
  const t = fixture(async () => ({files: [], revision: 1}), {readClipboard: async () => [{types: ['text/html','image/webp','image/png'], getType: async type => { requested.push(type); return png; }}]});
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  let selected; t.controller.upload = async file => { selected = file; };
  await t.elements['#paste-agent-file'].click();
  assert.deepEqual(requested, ['image/png']);
  assert.match(selected.name, /^clipboard-[A-Za-z0-9-]+\.png$/);
  assert.deepEqual([...new Uint8Array(await selected.arrayBuffer())], [1,2,3]);
  assert.deepEqual(t.inserted, []); assert.equal(t.elements['#paste-agent-file'].disabled, false);
  assert.equal(clipboardFile(makeFile('keep-name.zip', [1])).name, 'keep-name.zip');
});

test('clipboard permission denial and unsupported/text-only contents make no upload requests', async () => {
  for (const readClipboard of [async () => { throw Error('Permission denied'); }, async () => [{types: ['text/plain','text/html'], getType: () => { throw Error('never read text'); }}]]) {
    const t = fixture(async () => ({files: [], revision: 1}), {readClipboard});
    await t.controller.open(agent, '/api/agents/a/files', 'Local');
    await t.controller.pasteClipboard();
    assert.equal(t.calls.filter(c => c[1] === 'POST').length, 0);
    assert.match(t.elements['#agent-files-error'].textContent, /Upload/);
    assert.equal(t.elements['#paste-agent-file'].disabled, false);
  }
});

test('clipboard completion cannot upload to a newly selected agent or reopen a closed dialog', async () => {
  for (const closeOnly of [false, true]) {
    let resolve, getType = 0, uploads = 0;
    const t = fixture(async () => ({files: [], revision: 1}), {readClipboard: () => new Promise(done => { resolve = done; })});
    await t.controller.open(agent, '/api/federation/old/proxy/api/agents/a/files', 'Old');
    t.controller.upload = async () => { uploads++; };
    const pending = t.controller.pasteClipboard();
    assert.equal(t.elements['#paste-agent-file'].disabled, true);
    assert.equal(t.elements['#upload-agent-file'].disabled, true);
    if (closeOnly) t.elements['#files-dialog'].close();
    else await t.controller.open({...agent, id: 'b'}, '/api/federation/new/proxy/api/agents/b/files', 'New');
    resolve([{types: ['image/png'], getType: async () => { getType++; return new Blob(['x'], {type:'image/png'}); }}]);
    await pending;
    assert.equal(getType, 0); assert.equal(uploads, 0);
  }
});

test('late clipboard blob extraction cannot cross workspace and duplicate paste is blocked', async () => {
  let resolve;
  const t = fixture(async () => ({files: [], revision: 1}), {readClipboard: async () => [{types: ['image/png'], getType: () => new Promise(done => { resolve = done; })}]});
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  let uploads = 0; t.controller.upload = async () => { uploads++; };
  const pending = t.controller.pasteClipboard(); await tick();
  const repeated = pasteEvent([makeFile('image.png', [1])]);
  await t.controller.handlePaste(repeated);
  assert.match(t.elements['#agent-files-error'].textContent, /Wait for the current operation/);
  await t.controller.open({...agent, id:'b'}, '/api/agents/b/files', 'New');
  resolve(new Blob(['x'], {type:'image/png'})); await pending;
  assert.equal(uploads, 0);
});

test('multiple clipboard files are rejected explicitly rather than silently dropped', async () => {
  const t = fixture(async () => ({files: [], revision: 1}));
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  await t.controller.handlePaste(pasteEvent([makeFile('a.pdf', [1]), makeFile('b.pdf', [2])]));
  assert.match(t.elements['#agent-files-error'].textContent, /one image or file at a time/);
  assert.equal(t.calls.filter(c => c[1] === 'POST').length, 0);
});
