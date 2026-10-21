const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');

test('new-agent form uses discovered model and folder selectors', () => {
  const createForm = html.slice(html.indexOf('<form id="create-form">'), html.indexOf('</form>', html.indexOf('<form id="create-form">')));
  assert.match(createForm, /<select name="folder" required>/);
  assert.match(createForm, /id="browse-folder"[^>]*>Browse…/);
  assert.match(createForm, /<select name="model">/);
  assert.doesNotMatch(createForm, /<input name="(?:folder|model)"/);
  assert.match(html, /id="folder-dialog"/);
  assert.match(source, /api\('\/api\/config\/models'\)/);
  assert.match(source, /api\(`\/api\/folders/);
  assert.match(source, /new Set\(state\.agents\.map\(agent => agent\.folder\)/);
});

test('create response is rendered immediately without retrying or crossing workspaces', () => {
  assert.match(source, /const requestGeneration = stateGeneration, requestSwarm = activeSwarm/);
  assert.match(source, /requestGeneration !== stateGeneration \|\| requestSwarm !== activeSwarm/);
  assert.match(source, /applyState\(\{\.\.\.state, agents\}\)/);
  assert.match(source, /activate\('dashboard'\)/);
  assert.match(source, /Agent created; setup is running in its terminal\./);
  const handler = source.slice(source.indexOf("$('#create-form').addEventListener"), source.indexOf("$('#wake-form').addEventListener"));
  assert.equal((handler.match(/api\('\/api\/agents', 'POST'/g) || []).length, 1);
  assert.doesNotMatch(handler, /await refresh\(\)/);
});

test('non-JSON server failures explain uncertainty instead of suggesting a blind retry', async () => {
  const start = source.indexOf('function nonJsonResponseError('), end = source.indexOf('\nconst agentById', start);
  const context = vm.createContext({
    fetch: async () => ({status: 500, ok: false, json: async () => { throw Error('not json'); }}),
    apiPath: value => value,
  });
  vm.runInContext(source.slice(start, end), context);
  await assert.rejects(context.localApi('/api/test', 'POST', {}), /Server error 500; check backend logs.*outcome unknown.*check the card before retrying/i);
});

test('worktree destination is a sanitized sibling while branch remains unchanged', () => {
  const start = source.indexOf('function siblingWorktreePath(');
  const end = source.indexOf('\nfunction updateWorktreeCreateFields()', start);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({});
  vm.runInContext(source.slice(start, end), context);
  assert.equal(
    context.siblingWorktreePath('/repos/app', 'feature/nested'),
    '/repos/app_feature_nested',
  );
  assert.equal(context.siblingWorktreePath('/repos/app', ''), '');
  assert.match(source, /data\.automatic_worktree_path = data\.create_worktree/);
  assert.match(source, /data\.branch\.trim\(\)/);
});
