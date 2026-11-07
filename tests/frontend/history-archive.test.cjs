const test = require('node:test');
const assert = require('node:assert/strict');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.text = ''; this.value = ''; this.disabled = false;
    this.hidden = false; this.events = new Map(); this.children = [];
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent || '').join(''); }
  addEventListener(type, callback) { this.events.set(type, callback); }
  click() { if (!this.disabled) return this.events.get('click')?.({currentTarget: this}); }
  reportValidity() { this.reported = true; }
}

global.document = {querySelector: () => null};
const {HistoryArchiveController, unavailableReason} = require('../../src/swarm/static/history-archive.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const IDS = [
  'archive-history', 'history-archive-percent', 'history-archive-availability', 'history-archive-report',
  'history-archive-status', 'history-archive-state', 'history-archive-result', 'history-archive-error',
  'history-archive-file', 'history-archive-original', 'history-archive-file-row', 'history-archive-original-row',
];
const UUIDS = [
  '10000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000003',
];

function baseAgent(patch = {}) {
  return {
    id: 'agent/one', name: '<Agent & one>', managed: true, backend: 'pi', alive: false,
    pid: null, spawn_state: 'completed', session_file: '/sessions/<one>.jsonl', session_id: 'session-one',
    ...patch,
  };
}

function acknowledgement(body, patch = {}) {
  return {
    id: '0123456789abcdef0123456789abcdef', archive_id: '0123456789abcdef0123456789abcdef',
    request_id: body.request_id, status: 'completed', source: '/sessions/<one>.jsonl',
    session_id: 'session-one', percent_requested: body.percent, context_before_bytes: 10000,
    archive_bytes: 4300, actual_percent: 43.012, history_file: '/archive/<unsafe>/history.txt',
    archive: '/archive/<unsafe>/original.jsonl', ...patch,
  };
}

function fixture(options = {}) {
  const elements = Object.fromEntries(IDS.map(id => [`#${id}`, new Element(id === 'history-archive-percent' ? 'input' : 'div')]));
  elements['#history-archive-percent'].value = options.percent ?? '50';
  let selected = options.agent || baseAgent(), swarm = options.swarm || 'local', generation = 7, uuidIndex = 0;
  const calls = [], confirms = [], toasts = [], changed = [], busy = [];
  const api = async (...args) => {
    calls.push(args);
    if (options.api) return options.api(...args);
    return acknowledgement(args[2]);
  };
  const controller = new HistoryArchiveController({
    query: selector => elements[selector] || null, api,
    currentSwarm: () => swarm, currentGeneration: () => generation, agent: () => selected,
    requestId: () => UUIDS[uuidIndex++],additionalReason:options.additionalReason,
    confirm: message => { confirms.push(message); return options.confirm === undefined ? true :
      typeof options.confirm === 'function' ? options.confirm(message) : options.confirm; },
    onChanged: async report => { changed.push(report); if (selected) selected.context_archive = report; await options.onChanged?.(report); },
    onBusyChange: value => busy.push(value), toast: (...args) => toasts.push(args),
    requestTimeout: options.requestTimeout ?? 20000, setTimer: options.setTimer, clearTimer: options.clearTimer,
  });
  return {
    controller, elements, calls, confirms, toasts, changed, busy,
    get agent() { return selected; },
    setAgent(value) { selected = value; },
    switchSwarm(value) { swarm = value; generation += 1; },
    setSwarm(value) { swarm = value; }, bumpGeneration() { generation += 1; },
  };
}


test('eligibility requires one stopped managed Pi session and blocks every maintenance guard', () => {
  assert.equal(unavailableReason(baseAgent()), '');
  const cases = [
    [null, /no longer registered/i],
    [baseAgent({managed: false}), /managed Pi/i],
    [baseAgent({backend: 'other'}), /managed Pi/i],
    [baseAgent({session_file: null}), /persisted Pi session/i],
    [baseAgent({alive: true, pid: 3}), /Stop first/i],
    [baseAgent({alive: false, pid: 3}), /Stop first/i],
    [baseAgent({stop_pending: true}), /Stop first/i],
    [baseAgent({orchestration_stop: {stage: 'waiting'}}), /Stop first/i],
    [baseAgent({restart_job_id: 'job'}), /restart/i],
    [baseAgent({restart: {job_id: 'job', state: 'running'}}), /restart/i],
    [baseAgent({spawn_state: 'provisioning'}), /setup or launch/i],
    [baseAgent({meta_compaction: {status: 'running'}}), /saved-session operation/i],
    [baseAgent({tool_input_compaction: {status: 'running'}}), /saved-session operation/i],
    [baseAgent({tool_output_repair: {status: 'running'}}), /saved-session operation/i],
    [baseAgent({context_archive: {status: 'running'}}), /already running/i],
  ];
  for (const [agent, pattern] of cases) assert.match(unavailableReason(agent), pattern);
  for (const spawn_state of [undefined, null, 'ready', 'completed']) assert.equal(unavailableReason(baseAgent({spawn_state})), '');
});


test('update preserves an edited percent and renders persisted report fields as literal text', () => {
  const malicious = acknowledgement({request_id: UUIDS[0], percent: 50}, {
    status: '<img onerror=alert(1)>', error: '<script>bad()</script>',
    history_file: '<img src=x>', archive: '<svg onload=bad()>',
  });
  const agent = baseAgent({context_archive: malicious});
  const t = fixture({agent});
  t.elements['#history-archive-percent'].value = '67';
  t.controller.update(agent);
  assert.equal(t.elements['#history-archive-percent'].value, '67');
  assert.equal(t.elements['#history-archive-state'].textContent, '<img onerror=alert(1)>');
  assert.equal(t.elements['#history-archive-error'].textContent, '<script>bad()</script>');
  assert.match(t.elements['#history-archive-result'].textContent, /Original context 10000.*archived 4300.*43\.0%/);
  assert.equal(t.elements['#history-archive-file'].textContent, '<img src=x>');
  assert.equal(t.elements['#history-archive-original'].textContent, '<svg onload=bad()>');
  assert.equal(t.elements['#history-archive-file-row'].hidden, false);
  assert.equal(t.elements['#history-archive-original-row'].hidden, false);
  assert.equal(t.elements['#history-archive-state'].children.length, 0, 'remote values never create markup nodes');
});


test('confirmed start snapshots percent and context, uses absolute proxy endpoint, and never wakes', async () => {
  const t = fixture({swarm: 'peer/a', confirm: () => {
    t.elements['#history-archive-percent'].value = '80';
    return true;
  }});
  const id = await t.controller.start();
  assert.equal(id, UUIDS[0]);
  assert.deepEqual(t.calls[0], [
    '/api/federation/peer%2Fa/proxy/api/agents/agent%2Fone/archive-history', 'POST',
    {percent: 50, request_id: UUIDS[0]},
  ]);
  assert.match(t.confirms[0], /approximately 50%.*serialized model-content bytes, not tokens/is);
  assert.match(t.confirms[0], /whole user\/tool batches.*smaller or larger.*newest user span/is);
  assert.match(t.confirms[0], /raw source.*prompts, images.*original JSON.*No LLM.*bounded archive reader/is);
  assert.equal(t.changed.length, 1);
  assert.deepEqual(t.busy, [true, false]);
  assert.equal(t.calls.some(call => /wake|terminal/i.test(call[0])), false);
  assert.match(t.toasts[0][0], /without waking/);
});


test('invalid, boolean-like, NaN, fractional, and out-of-range percentages never confirm or post', async () => {
  for (const value of ['', true, 'NaN', '1.5', '0', '96']) {
    const t = fixture({percent: value});
    await t.controller.start();
    assert.equal(t.calls.length, 0, String(value));
    assert.equal(t.confirms.length, 0, String(value));
    assert.match(t.elements['#history-archive-error'].textContent, /whole percentage.*1 through 95/i);
  }
  const accepted = fixture({percent: '1'}); await accepted.controller.start();
  assert.equal(accepted.calls[0][2].percent, 1);
});


test('confirmation cancellation and post-confirm context or session changes perform no mutation', async () => {
  const cancelled = fixture({confirm: false});
  assert.equal(await cancelled.controller.start(), null);
  assert.equal(cancelled.calls.length, 0);

  let changed;
  const switched = fixture({confirm: () => { changed.switchSwarm('other'); return true; }}); changed = switched;
  assert.equal(await switched.controller.start(), null);
  assert.equal(switched.calls.length, 0);

  let replaced;
  const session = fixture({confirm: () => { replaced.setAgent(baseAgent({session_id: 'new-session'})); return true; }}); replaced = session;
  assert.equal(await session.controller.start(), null);
  assert.equal(session.calls.length, 0);
});


test('double start posts once and an ID or session change before completion suppresses stale UI', async () => {
  let resolve;
  const t = fixture({api: () => new Promise(done => { resolve = done; })});
  const first = t.controller.start();
  const second = t.controller.start();
  await tick();
  assert.equal(t.calls.length, 1);
  assert.equal(await second, null);
  t.setAgent(baseAgent({id: 'different', session_id: 'other'}));
  resolve(acknowledgement({percent: 50, request_id: UUIDS[0]}));
  assert.equal(await first, UUIDS[0]);
  assert.equal(t.changed.length, 0);
  assert.equal(t.toasts.length, 0);
  assert.deepEqual(t.busy, [true, false], 'stale completion releases the old busy lock without updating UI');
});


test('same agent ID can start in a new source and old finalization cannot delete the new owner', async () => {
  const releases = [];
  const t = fixture({api: (...args) => new Promise(resolve => releases.push(() => resolve(acknowledgement(args[2]))))});
  const old = t.controller.start(); await tick();
  t.switchSwarm('peer'); t.setAgent(baseAgent());
  const fresh = t.controller.start(); await tick();
  assert.equal(t.calls.length, 2);
  assert.equal(t.controller.inFlight(t.agent), true);
  releases[0](); await old;
  assert.equal(t.controller.inFlight(t.agent), true, 'old finally preserves the replacement map entry');
  releases[1](); await fresh;
  assert.equal(t.controller.inFlight(t.agent), false);
  assert.equal(t.changed.length, 1, 'only the active source receives the report');
});


test('20 second timeout is uncertain, exposes its UUID, ignores late response, and blocks blind retry', async () => {
  let timer, release;
  const t = fixture({
    api: (...args) => new Promise(resolve => { release = () => resolve(acknowledgement(args[2])); }),
    setTimer: callback => { timer = callback; return 1; }, clearTimer: () => {},
  });
  const work = t.controller.start(); await tick(); timer();
  assert.equal(await work, UUIDS[0]);
  assert.equal(t.calls.length, 1);
  assert.match(t.elements['#history-archive-error'].textContent, new RegExp(`timed out.*uncertain.*do not retry.*${UUIDS[0]}`, 'is'));
  assert.equal(t.elements['#archive-history'].disabled, true);
  assert.equal(await t.controller.start(), UUIDS[0]);
  assert.equal(t.calls.length, 1, 'uncertain request is not automatically or blindly retried');
  release(); await tick();
  assert.equal(t.changed.length, 0, 'late acknowledgement cannot revise a timed-out outcome');
});


test('malformed acknowledgements are uncertain and valid failed reports are published without retargeting', async () => {
  const badPatches = [
    {id: ''}, {status: 'running'}, {request_id: UUIDS[2]}, {percent_requested: 51},
    {source: '/different.jsonl'}, {session_id: 'different'}, {archive_id: 'different'},
  ];
  for (const patch of badPatches) {
    const t = fixture({api: (path, method, body) => acknowledgement(body, patch)});
    await t.controller.start();
    assert.equal(t.changed.length, 0);
    assert.match(t.elements['#history-archive-error'].textContent, /Invalid history-archive acknowledgement.*uncertain.*request ID/i);
  }

  const failed = fixture({api: (path, method, body) => acknowledgement(body, {status: 'failed', error: '<failure>'})});
  await failed.controller.start();
  assert.equal(failed.changed[0].status, 'failed');
  assert.deepEqual(failed.toasts[0], ['<failure>', true]);
  assert.equal(failed.agent.context_archive.status, 'failed');
});


test('button wiring starts with getter agent and secure request-ID failure posts nothing', async () => {
  const t = fixture();
  t.elements['#archive-history'].click(); await tick(); await tick();
  assert.equal(t.calls.length, 1);

  const broken = fixture(); broken.controller.requestId = () => { throw Error('no crypto'); };
  await broken.controller.start();
  assert.equal(broken.calls.length, 0);
  assert.match(broken.elements['#history-archive-error'].textContent, /secure request ID/i);
});


test('another maintenance request blocks archival before it publishes a persisted job', async () => {
  let busy=true;
  const t=fixture({additionalReason:()=>busy?'Another maintenance request is in flight.':''});
  t.controller.update();assert.equal(t.elements['#archive-history'].disabled,true);
  await t.controller.start();assert.equal(t.calls.length,0);assert.equal(t.confirms.length,0);
  busy=false;t.controller.update();assert.equal(t.elements['#archive-history'].disabled,false);
  await t.controller.start();assert.equal(t.calls.length,1);
});


test('the available action clearly identifies model-free archival', () => {
  const t=fixture();t.controller.update();
  assert.match(t.elements['#archive-history'].textContent,/no LLM/i);
});
