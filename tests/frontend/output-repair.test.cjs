const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.css'), 'utf8');
const predicateSource = source.slice(source.indexOf('const provisioningAgent'), source.indexOf('function canRevertToolInputs'));
const handlerSource = source.slice(source.indexOf('function outputRepairSummary'), source.indexOf('async function forgetAgent'));

function fixture({agent = {}, confirmResult = true, response, rejection, swarm = 'local'} = {}) {
  const base = {
    id: 'agent/one', name: '<unsafe agent>', managed: true, backend: 'pi', session_file: '/tmp/<session>.jsonl',
    alive: false, spawn_state: 'completed', status: 'idle', ...agent,
  };
  let resolveRequest;
  const pending = response === 'pending' ? new Promise(resolve => { resolveRequest = resolve; }) : null;
  const calls = [], toasts = [], confirmations = [], renders = [];
  const dialog = {open: false};
  const formError = {textContent: ''};
  const context = vm.createContext({
    state: {agents: [base]}, activeSwarm: swarm, stateGeneration: 4, editId: base.id,
    metaCompactingId: null, toolInputCompactingId: null, toolOutputRepairRequests: new Map(),
    agentById: id => context.state.agents.find(item => item.id === id),
    apiPath: value => context.activeSwarm === 'local' ? value : `/api/federation/${encodeURIComponent(context.activeSwarm)}/proxy${value}`,
    localApi: async (...args) => { calls.push(args); if (rejection) throw rejection; return pending || response || {status: 'unchanged', outputs_repaired: 0, original_bytes: 0, preview_bytes: 0}; },
    render: () => renders.push(true), refresh: async () => calls.push(['refresh']),
    toast: (...args) => toasts.push(args), confirm: message => { confirmations.push(message); return confirmResult; },
    $: selector => selector === '#edit-dialog' ? dialog : selector === '#edit-form' ? {id: 'edit-form'} : selector === '.form-error' ? formError : null,
    console,
  });
  vm.runInContext(predicateSource + '\n' + handlerSource, context);
  return {context, base, calls, toasts, confirmations, renders, resolveRequest, dialog, formError};
}

const evaluate = (fixture, expression) => vm.runInContext(expression, fixture.context);

test('repair is available only in Edit, with styling and versioned assets without changing compact dimensions', () => {
  assert.match(html, /<h3 id="tool-output-repair-heading">Oversized tool outputs<\/h3>/);
  assert.match(html, /id="repair-tool-outputs"[^>]*>Fix output limit…<\/button>/);
  assert.doesNotMatch(source, /repair-tool-outputs-card/);
  assert.match(source, /\$\('#repair-tool-outputs'\)\.addEventListener\('click', \(\) => repairToolOutputs\(editId\)\)/);
  assert.match(css, /\.tool-output-repair-section/);
  assert.match(css, /\.agent-card\.is-compact\{width:128px;height:128px/);
  for (const asset of ['app.css', 'app.js']) assert.ok(html.includes(`/static/${asset}?v=output-repair-edit-1`));
});

test('repair availability requires a stopped persisted managed Pi session and guards setup, awake, stopping, restart, and server jobs', () => {
  const t = fixture();
  assert.equal(evaluate(t, 'outputRepairUnavailableReason(state.agents[0])'), '');
  const cases = [
    [{session_file: null}, /persisted Pi session/],
    [{managed: false}, /Only managed Pi/],
    [{backend: 'grok'}, /Only managed Pi/],
    [{spawn_state: 'provisioning'}, /Setup is still running/],
    [{alive: true}, /Stop first/],
    [{stop_pending: true}, /Stop first/],
    [{restart: {job_id: 'job', state: 'running'}}, /Fleet restart/],
    [{tool_output_repair: {status: 'running'}}, /Repairing oversized outputs/],
    [{meta_compaction: {status: 'running'}}, /Another saved-session operation/],
  ];
  for (const [patch, pattern] of cases) {
    Object.assign(t.base, {managed: true, backend: 'pi', session_file: '/tmp/s', alive: false, spawn_state: 'completed', stop_pending: false, restart: null, tool_output_repair: null, meta_compaction: null}, patch);
    assert.match(evaluate(t, 'outputRepairUnavailableReason(state.agents[0])'), pattern);
  }
});

test('confirmation explains exact repair scope and cancellation performs no mutation', async () => {
  const t = fixture({confirmResult: false});
  await t.context.repairToolOutputs(t.base.id);
  assert.equal(t.calls.length, 0);
  assert.equal(t.renders.length, 0);
  assert.match(t.confirmations[0], /single tool-output string limit \(10,485,760 characters\)/);
  assert.match(t.confirmations[0], /larger than 1 MiB.*32 KiB preview/s);
  assert.match(t.confirmations[0], /No model calls.*Incomplete or raw history.*images and tool inputs are unchanged/s);
  assert.match(t.confirmations[0], /not overall conversation compaction/);
  assert.match(t.confirmations[0], /stays stopped until you manually Wake/);
});

test('request uses one captured API path, locks all session mutations, persists report, and never wakes', async () => {
  const report = {status: 'completed', id: 'report-1', outputs_repaired: 2, original_bytes: 12000000, preview_bytes: 64000, archive: '<archive>', directory: '<directory>'};
  const t = fixture({response: 'pending', swarm: 'peer one'});
  const work = t.context.repairToolOutputs(t.base.id);
  assert.equal(t.calls.length, 1);
  assert.equal(t.calls[0][0], '/api/federation/peer%20one/proxy/api/agents/agent%2Fone/repair-tool-outputs');
  assert.equal(t.calls[0][1], 'POST'); assert.equal(Object.keys(t.calls[0][2]).length, 0);
  assert.equal(evaluate(t, 'compactionBlocked(state.agents[0])'), true);
  assert.equal(evaluate(t, 'outputRepairUnavailableReason(state.agents[0])').includes('Repairing'), true);
  t.resolveRequest(report); await work;
  assert.equal(t.base.tool_output_repair.status, 'completed');
  assert.match(t.toasts[0][0], /Repaired 2 oversized tool outputs.*12000000.*64000/);
  assert.equal(t.calls.filter(call => String(call[0]).includes('/wake')).length, 0);
  assert.equal(t.calls.filter(call => String(call[0]).includes('repair-tool-outputs')).length, 1);
  assert.equal(evaluate(t, 'compactionBlocked(state.agents[0])'), false);
});

test('late callbacks cannot mutate or toast in a newly selected workspace', async () => {
  const t = fixture({response: 'pending'});
  const work = t.context.repairToolOutputs(t.base.id);
  t.context.activeSwarm = 'other'; t.context.stateGeneration = 5;
  t.resolveRequest({status: 'completed', outputs_repaired: 1, original_bytes: 20, preview_bytes: 10, archive: '/old'});
  await work;
  assert.equal(t.base.tool_output_repair, undefined);
  assert.equal(t.toasts.length, 0);
  assert.equal(t.renders.length, 1);
});

test('failed reports and request errors are safe text and explain old controllers or uncertain outcomes', async () => {
  const failed = fixture({response: {status: 'failed', error: '<img src=x onerror=alert(1)>', outputs_repaired: 0}});
  await failed.context.repairToolOutputs(failed.base.id);
  assert.deepEqual(failed.toasts[0], ['<img src=x onerror=alert(1)>', true]);
  assert.match(source, /tool-output-repair-error'\)\.textContent = outputJob\.error/);
  assert.doesNotMatch(handlerSource, /innerHTML/);

  const old = fixture({rejection: Object.assign(Error('missing'), {status: 404})});
  await old.context.repairToolOutputs(old.base.id);
  assert.match(old.toasts[0][0], /selected controller does not support.*Update that controller/i);

  const uncertain = fixture({rejection: Error('connection lost')});
  await uncertain.context.repairToolOutputs(uncertain.base.id);
  assert.match(uncertain.toasts[0][0], /outcome unknown.*persisted repair report.*before retrying/i);
});

test('source interlocks Wake, Save, Forget, compact, revert, and duplicate repair while leaving Stop independent', () => {
  assert.match(source, /const compactionBlocked = a => compactionRunning\(a\) \|\| compactionRequestInFlight\(a\)/);
  assert.match(source, /wake\.disabled = compacting/);
  assert.match(source, /\$\('#save-agent'\)\.disabled = blocked/);
  assert.match(source, /\$\('#forget-agent'\)\.disabled = forgettingAgent \|\| !a \|\| active \|\| compacting/);
  assert.ok((source.match(/toolOutputRepairInFlight\(a\).*updateEditAvailability\(\); return;/g) || []).length >= 3);
  assert.match(source, /const id = agentId, a = agentById\(id\), reason = outputRepairUnavailableReason\(a\)/);
  const stopBody = source.slice(source.indexOf('async function stopAgent'), source.indexOf('async function submit'));
  assert.doesNotMatch(stopBody, /compactionBlocked|toolOutputRepair/);
});
