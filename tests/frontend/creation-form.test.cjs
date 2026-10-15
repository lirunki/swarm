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
