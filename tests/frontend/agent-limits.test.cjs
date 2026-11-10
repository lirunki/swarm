const test = require('node:test');
const assert = require('node:assert/strict');

const {AgentLimitsController, validProjection} = require('../../src/swarm/static/agent-limits.js');
const REQUEST = '10000000-0000-4000-8000-000000000001';
const tick = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(value = '') { this.value = value; this.textContent = ''; this.disabled = false; this.events = new Map(); this.children = []; }
  addEventListener(name, callback) { this.events.set(name, callback); }
  dispatch(name) { return this.events.get(name)?.({target: this}); }
  click() { if (!this.disabled) return this.dispatch('click'); }
}

const projection = (patch = {}) => ({revision: 4, max_active_managed: 20, active_managed: 3, scope: 'orchestration', ...patch});

function fixture(options = {}) {
  const ids = ['agent-limits-owner', 'agent-limits-count', 'agent-limits-max', 'agent-limits-save', 'agent-limits-status', 'agent-limits-error'];
  const nodes = Object.fromEntries(ids.map(id => [`#${id}`, new Element(id === 'agent-limits-max' ? '20' : '')]));
  let source = options.source || 'local', generation = 1, current = options.current || projection();
  const calls = [], errors = [], changed = [], confirms = [];
  const api = (...args) => {
    calls.push(args);
    if (options.api) return options.api(...args);
    const [path, method, body] = args;
    if (method === 'POST') return current = projection({revision: current.revision + 1, max_active_managed: body.max_active_managed});
    return {...current};
  };
  const controller = new AgentLimitsController({query: selector => nodes[selector] || null, api,
    currentSwarm: () => source, currentGeneration: () => generation,
    ownerName: value => value === 'local' ? 'Local' : `Remote ${value}`,
    onChanged: async (...args) => { changed.push(args); await options.onChanged?.(...args); },
    onError: message => errors.push(message), requestTimeout: options.requestTimeout ?? 20000,
    setTimer: options.setTimer, clearTimer: options.clearTimer,
    requestId: options.requestId || (() => REQUEST),
    confirm: async message => { confirms.push(message); return options.confirm === undefined ? true : options.confirm; },
  });
  controller.setState(current);
  return {controller, nodes, calls, errors, changed, confirms,
    setCurrent(value) { current = value; }, source(value) { source = value; }, generation(value) { generation = value; },
    switchSource(value) { source = value; generation += 1; }};
}

function edit(t, value) { t.nodes['#agent-limits-max'].value = value; t.nodes['#agent-limits-max'].dispatch('input'); }


test('validProjection strictly validates numeric and scoped values', () => {
  assert.equal(validProjection(projection()), true);
  for (const bad of [
    projection({revision: -1}), projection({revision: true}), projection({max_active_managed: 0}),
    projection({max_active_managed: 10001}), projection({max_active_managed: 2.5}),
    projection({active_managed: -1}), projection({active_managed: false}), projection({scope: 'all'}), null,
  ]) assert.equal(validProjection(bad), false);
});


test('renders owner, count, bounds, scope explanation, and unsupported states with text only', () => {
  const t = fixture();
  assert.equal(t.nodes['#agent-limits-owner'].textContent, 'Owning server: Local');
  assert.equal(t.nodes['#agent-limits-count'].textContent, 'Active managed agents: 3');
  assert.deepEqual([t.nodes['#agent-limits-max'].min, t.nodes['#agent-limits-max'].max, t.nodes['#agent-limits-max'].step], ['1', '10000', '1']);
  assert.match(t.nodes['#agent-limits-status'].textContent, /orchestration.*does not stop or wake/i);
  t.controller.setState(undefined);
  assert.equal(t.nodes['#agent-limits-save'].disabled, true);
  assert.match(t.nodes['#agent-limits-status'].textContent, /not supported/i);
  t.controller.setState({max_active_managed: 20}, Error('<offline>'));
  assert.equal(t.nodes['#agent-limits-error'].textContent, '<offline>');
  assert.equal(t.nodes['#agent-limits-error'].children.length, 0);
});


test('input validation and equal polling preserve a draft while changed authority requires resave', () => {
  const t = fixture();
  for (const value of ['', '0', '1.5', 'false', '10001']) {
    edit(t, value); assert.equal(t.nodes['#agent-limits-save'].disabled, true, value);
  }
  edit(t, '30'); assert.equal(t.nodes['#agent-limits-save'].disabled, false);
  t.controller.setState(projection({active_managed: 7}));
  assert.equal(t.nodes['#agent-limits-max'].value, '30');
  t.controller.setState(projection({revision: 5, max_active_managed: 21, active_managed: 7}));
  assert.equal(t.nodes['#agent-limits-max'].value, '30');
  assert.match(t.nodes['#agent-limits-error'].textContent, /changed while you were editing.*preserved.*save again/i);
  assert.equal(t.nodes['#agent-limits-save'].disabled, false);
});


test('an external projection matching the dirty draft reconciles and disables save even at a shared revision', () => {
  const t = fixture(); edit(t, '25');
  t.controller.setState(projection({max_active_managed: 25, active_managed: 8}));
  assert.equal(t.nodes['#agent-limits-max'].value, '25');
  assert.equal(t.controller.dirty, false);
  assert.equal(t.nodes['#agent-limits-save'].disabled, true);
  assert.equal(t.nodes['#agent-limits-error'].textContent, '');
});


test('a lower same-context projection and its error are wholly ignored after newer websocket state', () => {
  const t = fixture();
  t.controller.setState(projection({revision: 6, max_active_managed: 26, active_managed: 9}));
  t.controller.setState(projection({revision: 5, max_active_managed: 21, active_managed: 2}), Error('stale offline'));
  assert.equal(t.controller.settings.revision, 6);
  assert.equal(t.nodes['#agent-limits-max'].value, '26');
  assert.equal(t.nodes['#agent-limits-count'].textContent, 'Active managed agents: 9');
  assert.equal(t.nodes['#agent-limits-error'].textContent, '');
  assert.equal(t.nodes['#agent-limits-max'].disabled, false);

  t.controller.setState(projection({revision: 6, max_active_managed: 26, active_managed: 9}), Error('actual offline'));
  assert.equal(t.nodes['#agent-limits-max'].disabled, true);
  assert.match(t.nodes['#agent-limits-error'].textContent, /actual offline/);
});


test('save GETs a matching snapshot then POSTs an exact UUID body to the captured proxy', async () => {
  const t = fixture({source: 'peer/a'}); edit(t, '25');
  assert.equal(await t.controller.save(), true);
  assert.deepEqual(t.calls[0], ['/api/federation/peer%2Fa/proxy/api/agent-limits', undefined, undefined]);
  assert.deepEqual(t.calls[1], ['/api/federation/peer%2Fa/proxy/api/agent-limits', 'POST',
    {max_active_managed: 25, revision: 4, request_id: REQUEST}]);
  assert.equal(Object.keys(t.calls[1][2]).length, 3);
  assert.equal(t.changed.length, 1);
  assert.equal(t.changed[0][0].max_active_managed, 25);
  assert.deepEqual(t.changed[0][1], {operation: 'set-limit', source: 'peer/a', generation: 1});
  assert.equal(t.nodes['#agent-limits-save'].disabled, true);
});


test('lowering below fresh active count requires the exact warning and cancellation never posts', async () => {
  const t = fixture({current: projection({active_managed: 12}), confirm: false}); edit(t, '10');
  assert.equal(await t.controller.save(), false);
  assert.equal(t.calls.length, 1);
  assert.match(t.confirms[0], /10 while 12 are active.*does not stop or wake.*only blocks additional orchestration starts/is);
  const increase = fixture({current: projection({active_managed: 12})}); edit(increase, '30'); await increase.controller.save();
  assert.equal(increase.confirms.length, 0);
});


test('a changed fresh revision preserves the draft, refreshes the captured owner, and never POSTs', async () => {
  const t = fixture({api: () => projection({revision: 5, max_active_managed: 21})}); edit(t, '30');
  assert.equal(await t.controller.save(), false);
  assert.equal(t.calls.length, 1);
  assert.equal(t.nodes['#agent-limits-max'].value, '30');
  assert.equal(t.changed.length, 1); assert.equal(t.changed[0][1].refresh, true);
  assert.match(t.errors[0], /changed.*preserved/i);
});


test('repeated clicks make one mutation and a websocket advance before HTTP remains valid', async () => {
  let resolvePost;
  const t = fixture({api: (path, method) => method === 'POST' ? new Promise(resolve => { resolvePost = resolve; }) : projection()});
  edit(t, '25'); const first = t.controller.save(); const second = t.controller.save();
  while (!resolvePost) await tick();
  t.controller.setState(projection({revision: 5, max_active_managed: 25, active_managed: 4}));
  resolvePost(projection({revision: 5, max_active_managed: 25, active_managed: 4}));
  assert.equal(await first, true); assert.equal(await second, false);
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);
  assert.equal(t.changed[0][0].revision, 5);
});


test('invalid acknowledgement revision or limit is an uncertain sent outcome', async () => {
  for (const response of [projection({revision: 4, max_active_managed: 25}), projection({revision: 5, max_active_managed: 24})]) {
    const t = fixture({api: (path, method) => method === 'POST' ? response : projection()}); edit(t, '25');
    assert.equal(await t.controller.save(), false);
    assert.match(t.nodes['#agent-limits-error'].textContent, new RegExp(`uncertain.*${REQUEST}`, 'is'));
    assert.equal(t.nodes['#agent-limits-save'].disabled, true);
  }
});


test('GET and POST are bounded; only a sent timeout is uncertain', async () => {
  let timers = [];
  const get = fixture({api: () => new Promise(() => {}), requestTimeout: 20,
    setTimer: callback => { timers.push(callback); return timers.length; }, clearTimer: () => {}});
  edit(get, '25'); const getPending = get.controller.save(); await tick(); timers.shift()();
  assert.equal(await getPending, false); assert.match(get.errors[0], /Cannot load.*timed out/i);
  assert.doesNotMatch(get.errors[0], /uncertain/i); assert.equal(get.calls.length, 1);

  timers = [];
  const post = fixture({api: (path, method) => method === 'POST' ? new Promise(() => {}) : projection(), requestTimeout: 20,
    setTimer: callback => { timers.push(callback); return timers.length; }, clearTimer: () => {}});
  edit(post, '25'); const postPending = post.controller.save();
  while (post.calls.length < 2) await tick();
  timers.at(-1)(); assert.equal(await postPending, false);
  assert.match(post.errors[0], new RegExp(`uncertain.*Do not retry blindly.*${REQUEST}.*timed out`, 'is'));
  assert.equal(post.calls.filter(call => call[1] === 'POST').length, 1);
});


test('400 and 409 are definite, while 503 and network failures lock blind retries', async () => {
  for (const status of [400, 409, 503, null]) {
    const error = Error(status ? `snapshot ${status}` : 'network lost'); if (status) error.status = status;
    const t = fixture({api: (path, method) => { if (method === 'POST') throw error; return projection(); }}); edit(t, '25');
    assert.equal(await t.controller.save(), false);
    if (status === 400 || status === 409) {
      assert.doesNotMatch(t.errors[0], /uncertain/i); assert.equal(t.nodes['#agent-limits-save'].disabled, false);
      assert.equal(t.changed.length, status === 409 ? 1 : 0);
    } else {
      assert.match(t.errors[0], /uncertain.*Request ID/i); assert.equal(t.nodes['#agent-limits-save'].disabled, true);
    }
  }
});


test('uncertain state survives same-revision polling and resolves only from newer authority', async () => {
  const t = fixture({api: (path, method) => { if (method === 'POST') { const error = Error('lost'); error.status = 503; throw error; } return projection(); }});
  edit(t, '25'); await t.controller.save();
  t.controller.setState(projection({max_active_managed: 20}));
  assert.equal(t.nodes['#agent-limits-save'].disabled, true);
  assert.match(t.nodes['#agent-limits-error'].textContent, /Request ID/);
  t.controller.setState(projection({revision: 5, max_active_managed: 25}));
  assert.equal(t.nodes['#agent-limits-max'].value, '25');
  assert.equal(t.nodes['#agent-limits-error'].textContent, '');
  assert.match(t.nodes['#agent-limits-status'].textContent, /does not stop or wake/i);
});


test('a newer differing state resolves uncertainty but preserves the requested draft for manual save', async () => {
  const t = fixture({api: (path, method) => { if (method === 'POST') throw Error('lost'); return projection(); }});
  edit(t, '25'); await t.controller.save();
  t.controller.setState(projection({revision: 5, max_active_managed: 22}));
  assert.equal(t.nodes['#agent-limits-max'].value, '25');
  assert.equal(t.nodes['#agent-limits-save'].disabled, false);
  assert.match(t.nodes['#agent-limits-error'].textContent, /kept limit 22.*save again/i);
});


test('source and generation switches make old GET/POST responses side-effect free', async () => {
  let resolveGet;
  const get = fixture({api: () => new Promise(resolve => { resolveGet = resolve; })}); edit(get, '25');
  const getPending = get.controller.save(); get.switchSource('other'); get.controller.setState(projection());
  resolveGet(projection()); assert.equal(await getPending, false);
  assert.equal(get.calls.length, 1); assert.deepEqual(get.errors, []); assert.deepEqual(get.changed, []);

  let resolvePost;
  const post = fixture({api: (path, method) => method === 'POST' ? new Promise(resolve => { resolvePost = resolve; }) : projection()});
  edit(post, '25'); const postPending = post.controller.save(); while (!resolvePost) await tick();
  post.generation(2); post.controller.setState(projection());
  resolvePost(projection({revision: 5, max_active_managed: 25}));
  assert.equal(await postPending, false); assert.deepEqual(post.errors, []); assert.deepEqual(post.changed, []);
  assert.equal(post.controller.busy, false);
});


test('the captured path is invoked synchronously and a switch inside api cannot apply to the new source', async () => {
  let t;
  t = fixture({source: 'old/id', api: (path, method, body) => {
    if (method === 'POST') { t.switchSource('new/id'); return projection({revision: 5, max_active_managed: body.max_active_managed}); }
    return projection();
  }});
  edit(t, '25'); assert.equal(await t.controller.save(), false);
  assert.equal(t.calls[1][0], '/api/federation/old%2Fid/proxy/api/agent-limits');
  assert.deepEqual(t.changed, []); assert.deepEqual(t.errors, []);
});


test('state loss after POST preserves uncertain request identity and prevents a second POST', async () => {
  let rejectPost;
  const t = fixture({api: (path, method) => method === 'POST'
    ? new Promise((resolve, reject) => { rejectPost = reject; }) : projection()});
  edit(t, '25'); const pending = t.controller.save();
  while (!rejectPost) await tick();

  t.controller.setState(null, Error('Owning Swarm is offline.'));
  assert.equal(t.controller.busy, false);
  assert.equal(t.nodes['#agent-limits-max'].disabled, true);
  assert.match(t.nodes['#agent-limits-status'].textContent, /unavailable/i);
  assert.match(t.nodes['#agent-limits-error'].textContent,
    new RegExp(`offline.*uncertain.*Do not retry blindly.*${REQUEST}`, 'is'));
  assert.equal(await t.controller.save(), false);
  assert.equal(t.calls.filter(call => call[1] === 'POST').length, 1);

  const definite = Error('rejected'); definite.status = 400; rejectPost(definite);
  assert.equal(await pending, false);
  assert.match(t.nodes['#agent-limits-error'].textContent, new RegExp(REQUEST));

  t.controller.setState(projection({revision: 5, max_active_managed: 25}));
  assert.equal(t.nodes['#agent-limits-max'].value, '25');
  assert.equal(t.nodes['#agent-limits-error'].textContent, '');
  assert.equal(t.nodes['#agent-limits-save'].disabled, true);
});


test('invalid/offline state cancels busy GET ownership and an old finally cannot unlock a new busy operation', async () => {
  let resolve;
  const t = fixture({api: () => new Promise(done => { resolve = done; })}); edit(t, '25');
  const old = t.controller.save();
  t.controller.setState(null, Error('offline'));
  assert.equal(t.controller.busy, false); assert.equal(t.nodes['#agent-limits-save'].disabled, true);
  t.controller.setState(projection()); edit(t, '26');
  const newerOperation = t.controller.operation; t.controller.busy = true; t.controller.render();
  resolve(projection()); await old;
  assert.equal(t.controller.operation, newerOperation);
  assert.equal(t.controller.busy, true, 'stale finally did not clear newer ownership');
});
