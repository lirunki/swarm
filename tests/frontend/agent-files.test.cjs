const test = require('node:test');
const assert = require('node:assert/strict');
const {AgentFilesDialog} = require('../../src/swarm/static/agent-files.js');
const ID = '11111111-1111-4111-8111-111111111111';
class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.events = new Map(); this.open = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(type, fn) { this.events.set(type, fn); }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.get('close')?.(); }
}
const flatten = element => [element, ...element.children.flatMap(flatten)];
function fixture(api, confirm = () => true) {
  const ids = ['files-dialog', 'agent-file-list', 'refresh-agent-files', 'files-title', 'files-workspace', 'agent-files-error', 'agent-file-count'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, new Element()]));
  const calls = [], changed = [];
  const controller = new AgentFilesDialog({query: id => elements[id], create: (tag, cls, text) => Object.assign(new Element(tag), {className: cls, textContent: text}),
    api: async (...args) => { calls.push(args); return api(...args); }, onChange: async () => changed.push(true), confirm});
  return {controller, elements, calls, changed};
}
const agent = {id: 'a', name: 'Research', files_version: 1};
const file = {id: ID, label: '<script>literal</script>', filename: 'chart.png', size: 12, media_type: 'image/png', available: true};

test('dialog loads only metadata and builds captured remote-workspace content links', async () => {
  const base = '/api/federation/beta/proxy/api/agents/a/files';
  const t = fixture(async () => ({files: [file], revision: 1}));
  await t.controller.open(agent, base, 'Beta');
  assert.deepEqual(t.calls, [[base]]); assert.equal(t.elements['#files-workspace'].textContent, 'Beta');
  const row = t.elements['#agent-file-list'].children[0], nodes = flatten(row);
  assert.equal(nodes.find(node => node.tag === 'strong').textContent, file.label);
  assert.equal(nodes.some(node => node.tag === 'script'), false);
  const links = nodes.filter(node => node.tag === 'a');
  assert.equal(links[0].href, `${base}/${ID}/content`); assert.equal(links[0].target, '_blank');
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert.equal(links[1].href, `${base}/${ID}/content?download=true`);
});

test('unavailable and invalid IDs never get content links', async () => {
  const t = fixture(async () => ({files: [{...file, available: false, error: 'Original deleted'}, {...file, id: '../elsewhere'}], revision: 1}));
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  assert.equal(flatten(t.elements['#agent-file-list']).some(node => node.tag === 'a'), false);
  assert.ok(flatten(t.elements['#agent-file-list']).some(node => node.textContent === 'Original deleted'));
});

test('metadata revision updates refresh an open list but late results cannot affect another agent', async () => {
  let resolve;
  const t = fixture(async base => base.includes('/a/') ? new Promise(done => { resolve = done; }) : {files: [], revision: 2});
  const old = t.controller.open(agent, '/api/agents/a/files', 'Old');
  t.elements['#files-dialog'].close(); await t.controller.open({...agent, id: 'b'}, '/api/agents/b/files', 'New');
  resolve({files: [file], revision: 1}); await old;
  assert.equal(t.elements['#files-workspace'].textContent, 'New'); assert.equal(t.controller.files.length, 0);
});

test('missing API is an explicit deployment error, not an empty library', async () => {
  const t = fixture(async () => { throw Error('Not Found'); });
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  assert.match(t.elements['#agent-files-error'].textContent, /restart/);
  assert.equal(t.elements['#refresh-agent-files'].disabled, false);
});

test('removing a reference confirms and uses only its selected agent/id', async () => {
  let deleted = false;
  const t = fixture(async (base, method) => { if (method === 'DELETE') { deleted = true; return {removed: ID}; } return {files: deleted ? [] : [file], revision: deleted ? 2 : 1}; });
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  await t.controller.remove(file, new Element('button'));
  assert.ok(t.calls.some(args => args[0] === `/api/agents/a/files/${ID}` && args[1] === 'DELETE'));
  assert.equal(t.changed.length, 1); assert.equal(t.controller.files.length, 0);
});

test('cancel does not unregister anything and deleted agents close the dialog', async () => {
  const t = fixture(async () => ({files: [file], revision: 1}), () => false);
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  await t.controller.remove(file, new Element('button')); assert.equal(t.calls.length, 1);
  t.controller.sync(null); assert.equal(t.elements['#files-dialog'].open, false);
});

test('registrations arriving during a metadata fetch queue one follow-up refresh', async () => {
  let count = 0, resolve;
  const t = fixture(async () => {
    count++;
    if (count === 2) return new Promise(done => { resolve = done; });
    return {files: count === 1 ? [] : [file], revision: count === 1 ? 1 : 3};
  });
  await t.controller.open(agent, '/api/agents/a/files', 'Local');
  t.controller.sync({...agent, files_version: 2});
  t.controller.sync({...agent, files_version: 3});
  resolve({files: [], revision: 2});
  await new Promise(done => setImmediate(done));
  assert.equal(count, 3); assert.equal(t.controller.revision, 3); assert.equal(t.controller.files.length, 1);
});
