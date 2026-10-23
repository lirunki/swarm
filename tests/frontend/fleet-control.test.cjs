const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

class Text { constructor(value) { this.textContent = String(value); } }
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.events = new Map(); this.dataset = {}; this.hidden = false; this.disabled = false; this.open = false; this.checked = false; this.text = ''; }
  append(...items) { this.children.push(...items.map(item => typeof item === 'string' ? new Text(item) : item)); }
  replaceChildren(...items) { this.children = items; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(item => item.textContent).join(''); }
  addEventListener(type, callback) { this.events.set(type, callback); }
  click() { if (!this.disabled) return this.events.get('click')?.({currentTarget: this}); }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.get('close')?.(); }
  focus() { this.focused = true; }
  querySelectorAll(selector) {
    const all = this.children.flatMap(walk);
    if (selector === 'input[type="checkbox"]:checked') return all.filter(item => item.tagName === 'INPUT' && item.type === 'checkbox' && item.checked);
    return [];
  }
}
const walk = value => value instanceof Element ? [value, ...value.children.flatMap(walk)] : [];
global.document = {createElement: tag => new Element(tag), createTextNode: value => new Text(value)};
const {FleetControl, isActiveRestart} = require('../../src/swarm/static/fleet-control.js');

const target = {id: '11111111-1111-4111-8111-111111111111', name: '<Alpha & Co>', status: 'idle', token: 'process-token', transport: 'native'};
const legacy = {id: '22222222-2222-4222-8222-222222222222', name: 'Legacy', status: 'done', token: 'legacy-token', transport: 'legacy'};
const runningJob = {id: 'job-1', status: 'running', counts: {waiting: 1}, created_at: 'now', targets: [
  {id: target.id, name: target.name, state: 'blocked', stage: '<waiting for idle>', error: null, can_force: true, can_legacy: true, can_cancel: true},
]};
function fixture(handler, confirm = () => true) {
  const ids = ['fleet-restart-dialog', 'fleet-control-button', 'fleet-preview-refresh', 'fleet-restart-start', 'fleet-job-cancel', 'fleet-restart-close',
    'fleet-restart-cancel', 'fleet-restart-workspace', 'fleet-preview-targets', 'fleet-preview-skipped', 'fleet-skipped-section', 'fleet-preview-home', 'fleet-restart-error',
    'fleet-job-section', 'fleet-job-targets', 'fleet-job-status', 'fleet-job-counts'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, new Element(id.includes('button') || ['fleet-preview-refresh', 'fleet-restart-start', 'fleet-job-cancel', 'fleet-restart-close'].includes(id) ? 'button' : 'div')]));
  const calls = [], confirmations = [], changes = [], timers = [];
  let swarm = 'local', generation = 1;
  const control = new FleetControl({
    query: selector => elements[selector], create: (tag, cls, text) => Object.assign(new Element(tag), {className: cls, textContent: text}),
    apiPath: value => swarm === 'local' ? value : `/api/federation/${swarm}/proxy${value}`,
    api: async (...args) => { calls.push(args); return handler(...args); }, currentSwarm: () => swarm, currentGeneration: () => generation,
    onChange: async () => changes.push(true), confirm: value => { confirmations.push(value); return confirm(value); },
    setTimer: (callback, delay) => { const timer = {callback, delay}; timers.push(timer); return timer; }, clearTimer: timer => { timer.cleared = true; },
  });
  return {control, elements, calls, confirmations, changes, timers, setSwarm(value) { swarm = value; generation += 1; }};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

function standardApi(pathname, method) {
  if (pathname.endsWith('/preview')) return {targets: [target, legacy], skipped: [{id: 'skip', name: '<Stopped>', reason: 'stopped'}], home: '/safe/home'};
  if (method === 'POST') return {job: runningJob};
  return {jobs: []};
}

test('versioned topbar menu opens a safe custom preview for only the selected Swarm', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  assert.match(html, /id="fleet-control-button"[^>]*>⋯<\/button>/);
  assert.ok(html.indexOf('fleet-control-button') < html.indexOf('restart-controller'));
  for (const asset of ['app.css', 'fleet-control.js', 'app.js']) assert.ok(html.includes(`/static/${asset}?v=fleet-control-2`));
  const t = fixture(standardApi); t.elements['#fleet-control-button'].click(); await tick(); await tick();
  assert.equal(t.elements['#fleet-restart-dialog'].open, true); assert.equal(t.elements['#fleet-restart-cancel'].focused, true);
  assert.deepEqual(t.calls.map(call => call[0]), ['/api/fleet/restarts', '/api/fleet/restarts/preview']);
  assert.match(t.elements['#fleet-preview-targets'].textContent, /<Alpha & Co>/);
  assert.equal(walk(t.elements['#fleet-preview-targets']).some(item => item.tagName === 'SCRIPT'), false);
  assert.match(t.elements['#fleet-preview-skipped'].textContent, /<Stopped> — stopped/);
});

test('start confirmation posts one token-bound snapshot with a UUID and no work action', async () => {
  const t = fixture(standardApi); await t.control.open(); t.calls.length = 0;
  await t.control.start();
  const post = t.calls.find(call => call[1] === 'POST');
  assert.equal(post[0], '/api/fleet/restarts');
  assert.match(post[2].request_id, /^[0-9a-f-]{36}$/i);
  assert.deepEqual(post[2].targets, [{id: target.id, token: target.token}, {id: legacy.id, token: legacy.token}]);
  assert.equal(post[2].timeout_seconds, 300); assert.equal(post[2].confirmed, true);
  assert.match(t.confirmations[0], /never auto-forces.*tool calls.*unsent terminal drafts may be lost/is);
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
});

test('legacy and force require separate warnings while cancel uses only pre-commit IDs', async () => {
  const t = fixture(standardApi); await t.control.open(); t.control.job = runningJob; t.control.renderJob(); t.calls.length = 0;
  const buttons = walk(t.elements['#fleet-job-targets']).filter(item => item.tagName === 'BUTTON');
  await buttons.find(item => item.textContent === 'Legacy /quit…').click(); await tick();
  await buttons.find(item => item.textContent === 'Force restart…').click(); await tick();
  await t.elements['#fleet-job-cancel'].click(); await tick();
  assert.match(t.confirmations[0], /existing draft may be submitted.*interrupt work/is);
  assert.match(t.confirmations[1], /kills.*in-progress work.*queued mail/is);
  assert.deepEqual(t.calls.filter(call => call[1] === 'POST').map(call => [call[0].split('/').pop(), call[2].agent_ids]), [
    ['legacy', [target.id]], ['force', [target.id]], ['cancel', [target.id]],
  ]);
});

test('409 cancel explains graceful commit and uncertain mutation is never retried', async () => {
  const error = Object.assign(Error('committed'), {status: 409});
  const t = fixture(async (pathname, method) => {
    if (pathname.endsWith('/preview')) return {targets: [target], skipped: [], home: ''};
    if (!method) return {jobs: []};
    throw error;
  });
  await t.control.open(); t.control.job = runningJob; t.control.renderJob(); t.calls.length = 0;
  await t.control.cancelJob();
  assert.match(t.elements['#fleet-restart-error'].textContent, /committed.*cleanup needs attention/);
  assert.equal(t.calls.length, 1);
});

test('late preview and job responses cannot mutate another Swarm', async () => {
  let resolvePreview;
  const t = fixture(async pathname => {
    if (pathname.endsWith('/preview')) return new Promise(resolve => { resolvePreview = resolve; });
    return {jobs: []};
  });
  const pending = t.control.open(); await tick();
  t.setSwarm('peer'); t.elements['#fleet-restart-dialog'].close();
  resolvePreview({targets: [target], skipped: [], home: '/old'}); await pending;
  assert.equal(t.control.preview, null);
  assert.doesNotMatch(t.elements['#fleet-preview-targets'].textContent, /Alpha/);
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 0);
});

test('restart marker predicate is conservative and terminal states release controls', () => {
  assert.equal(isActiveRestart({restart: {job_id: 'job', state: 'blocked'}}), true);
  assert.equal(isActiveRestart({restart: {job_id: 'job', state: 'completed'}}), false);
  assert.equal(isActiveRestart({restart: {job_id: 'job', state: 'cancelled'}}), false);
  assert.equal(isActiveRestart({restart_job_id: 'old-controller'}), true);
  assert.equal(isActiveRestart({restart_job_id: null, restart: {job_id:'job', state:'failed'}}), false);
  assert.equal(isActiveRestart({restart: {job_id:'job', state:'failed'}}), false);
});


test('remote mutation policy disables starting and actions without hiding read-only preview', async () => {
  const t=fixture(async path=>path.endsWith('/preview')?{targets:[target],skipped:[],home:'/remote'}:{jobs:[runningJob]});
  t.control.canMutate=()=>false;
  await t.control.open();
  assert.equal(t.elements['#fleet-restart-start'].disabled,true);
  await t.control.start();
  await t.control.agentAction('force',runningJob.targets[0]);
  assert.equal(t.calls.filter(call=>call[1]==='POST').length,0);
  assert.equal(t.confirmations.length,0);
});
