const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');

test('top-level selector switches API, state, and terminal paths by Swarm', () => {
  assert.match(html, /id="swarm-switcher"[\s\S]*id="swarm-selector"/);
  assert.match(source, /const apiPath = path => activeSwarm === 'local' \? path : `\/api\/federation\/\$\{encodeURIComponent\(activeSwarm\)\}\/proxy\$\{path\}`/);
  assert.match(source, /const stateWSPath = \(\) => activeSwarm === 'local' \? '\/ws\/state' : `\/ws\/federation/);
  assert.match(source, /const terminalWSPath = id => activeSwarm === 'local' \? `\/ws\/terminal/);
  assert.match(source, /for \(const id of \[\.\.\.terminals\.keys\(\)\]\) closeTerminal\(id\)/);
});

test('remote restart UI obeys advertised capability', () => {
  assert.match(source, /capabilities\?\.remote_restart/);
  assert.match(source, /Remote restart is disabled on this Swarm/);
  assert.match(source, /--allow-remote-restart/);
});

test('federation discovery always uses local backend rather than recursive proxy', () => {
  assert.match(source, /localApi\('\/api\/federation\/nodes'\)/);
  assert.match(source, /authenticatedSocketURL\('\/ws\/federation\/presence'\)/);
});


test('Swarm picker follows the logo without a redundant visible label', () => {
  const group = html.slice(html.indexOf('<div class="brand-cluster">'), html.indexOf('<div class="top-meta">'));
  assert.match(group, /class="brand"[\s\S]*id="swarm-switcher"[\s\S]*class="edition"/);
  assert.match(group, /id="swarm-switcher"[^>]*><select id="swarm-selector" aria-label="Active Swarm"/);
  assert.doesNotMatch(group, /<span>Swarm<\/span>/);
});
