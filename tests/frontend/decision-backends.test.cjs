const test = require('node:test');
const assert = require('node:assert/strict');

const core = require('../../src/swarm/extensions/decision-core.js');
const {runDecision} = require('../../src/swarm/extensions/decision-backends.js');

const tinyPng = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+X2NDWQAAAABJRU5ErkJggg==';
const tinyJpg = '/9j/';
const questions = {
  pick: {type: 'choice', instructions: 'Pick', criteria: {yes: 'yes', no: 'no'}},
  quality: {type: 'score', instructions: 'Rate', criteria: ['bad', 'good']},
  approved: {type: 'noul', instructions: 'Approved?'},
};

function resolvedFor(profile, overrides = {}) {
  const config = core.deepClone(core.DEFAULT_CONFIG);
  config.selected = 'test';
  config.models.test = profile;
  return core.resolveRequest({state: {text: 'hello'}, questions, ...overrides}, {config});
}

function nativeAnswers() {
  return {
    pick: {type: 'choice', choice: 'yes', probabilities: {yes: .8, no: .2}, confidence: .7},
    quality: {type: 'score', score: .75, confidence: .6},
    approved: {type: 'bool', probability: .9},
  };
}

function publicAnswers() {
  return {...nativeAnswers(), approved: {type: 'noul', noul: .9}};
}

function assistant(text, overrides = {}) {
  return {
    role: 'assistant', content: [{type: 'text', text}], stopReason: 'stop',
    usage: {input: 4, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 6,
      cost: {input: .01, output: .02, cacheRead: 0, cacheWrite: 0, total: .03}},
    ...overrides,
  };
}

test('classifier uses the exact configured native model, preserves object state, maps bool, and converts usage', async () => {
  const profile = {adapter: 'classifier', provider: 'fake', model: 'decision-1', images: false, box: false};
  const resolved = resolvedFor(profile, {state: {instruction: 'Inspect carefully'}});
  const model = {type: 'classifier', provider: 'fake', id: 'decision-1', input: ['text']};
  const calls = [];
  const registry = {
    getModelOfType(type, provider, id) { calls.push(['model', type, provider, id]); return model; },
    async getApiKeyAndHeaders(actual) { calls.push(['auth', actual]); return {ok: true, apiKey: 'not-exposed'}; },
    async classify(actual, context, options) {
      calls.push(['classify', actual, context, options]);
      return {stopReason: 'stop', answers: nativeAnswers(),
        usage: {tokens: {input: 11, output: 3}, cost: {total: .25}}};
    },
  };
  const output = await runDecision(profile, resolved, {modelRegistry: registry}, {timeoutMs: 1000});
  assert.equal(calls.filter(call => call[0] === 'classify').length, 1);
  assert.deepEqual(Object.keys(calls[2][3]), ['signal'], 'credentials are resolved by the registry and not passed to classify');
  assert.deepEqual(calls[2][2].state, {instruction: 'Inspect carefully'});
  assert.equal(calls[2][2].images, undefined);
  assert.deepEqual(output.answers.approved, {type: 'noul', noul: .9});
  assert.deepEqual(output.answers.quality, {type: 'score', score: .75, confidence: .6});
  assert.deepEqual(output.usage, {input: 11, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: 14,
    cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: .25}});
  assert.deepEqual(output.metadata.capabilities, {images: false, box: false});
  assert.equal(output.metadata.probability_source, 'native');
  assert.match(output.metadata.notes[0], /not reported/);
});

test('an already-aborted signal dispatches no lookup, auth, stream, classify, or fetch work', async () => {
  const profiles = [
    {adapter: 'classifier', provider: 'fake', model: 'native', images: false, box: false},
    {adapter: 'llm', provider: 'fake', model: 'chat', images: false, box: false},
    {adapter: 'typesafe-http', endpoint: 'https://decision.example/pinned', provider: 'typed', model: 'v1', images: false, box: false},
  ];
  for (const profile of profiles) {
    let dispatches = 0;
    const controller = new AbortController();
    controller.abort();
    const registry = {
      find() { dispatches++; },
      getModelOfType() { dispatches++; },
      getApiKeyAndHeaders() { dispatches++; },
      classify() { dispatches++; },
      streamSimple() { dispatches++; },
    };
    await assert.rejects(runDecision(profile, resolvedFor(profile), {modelRegistry: registry}, {
      signal: controller.signal,
      fetch() { dispatches++; },
      timeoutMs: 100,
    }), error => error.category === 'aborted');
    assert.equal(dispatches, 0, `${profile.adapter} must not dispatch after pre-abort`);
  }
});

test('native classifier rejects primitive state and named image assets before auth or classify', async () => {
  const profile = {adapter: 'classifier', provider: 'fake', model: 'native', images: true, box: false};
  const model = {type: 'classifier', provider: 'fake', id: 'native', input: ['text', 'image']};
  let paidCalls = 0; let registryCalls = 0;
  const registry = {
    getModelOfType() { registryCalls++; return model; },
    getApiKeyAndHeaders() { paidCalls++; return {ok: true}; },
    classify() { paidCalls++; },
  };
  await assert.rejects(runDecision(profile, resolvedFor(profile, {state: 'literal state'}), {modelRegistry: registry}, {timeoutMs: 100}),
    error => error.category === 'capability' && /object state.*LLM or direct HTTP/.test(error.message));
  await assert.rejects(runDecision(profile, resolvedFor(profile, {
    assets: {'photo-1': {type: 'image', format: 'jpg', data_base64: tinyJpg}},
  }), {modelRegistry: registry}, {timeoutMs: 100}),
  error => error.category === 'capability' && /asset ID associations.*LLM or direct HTTP/.test(error.message));
  assert.equal(registryCalls, 0);
  assert.equal(paidCalls, 0);
});

test('classifier fails closed without catalog model or API-key auth and never lists or refreshes models', async () => {
  const profile = {adapter: 'classifier', provider: 'fake', model: 'missing', images: false, box: false};
  const resolved = resolvedFor(profile);
  let sideEffects = 0;
  await assert.rejects(runDecision(profile, resolved, {modelRegistry: {
    getModelOfType() { return undefined; },
    getAvailableOfType() { sideEffects++; }, refresh() { sideEffects++; },
  }}, {timeoutMs: 100}), error => error.category === 'notavailable');
  assert.equal(sideEffects, 0);

  const model = {type: 'classifier', provider: 'fake', id: 'missing', input: ['text']};
  await assert.rejects(runDecision(profile, resolved, {modelRegistry: {
    getModelOfType() { return model; },
    async getApiKeyAndHeaders() { return {ok: false, error: 'SECRET provider response'}; },
    classify() { sideEffects++; },
  }}, {timeoutMs: 100}), error => error.category === 'auth' && !error.message.includes('SECRET'));
  assert.equal(sideEffects, 0);
});

test('hard timeout aborts one classifier call without retrying', async () => {
  const profile = {adapter: 'classifier', provider: 'fake', model: 'slow', images: false, box: false};
  const resolved = resolvedFor(profile);
  const model = {type: 'classifier', provider: 'fake', id: 'slow', input: ['text']};
  let calls = 0; let aborted = false;
  const registry = {
    getModelOfType() { return model; }, async getApiKeyAndHeaders() { return {ok: true}; },
    classify(_model, _context, {signal}) {
      calls++;
      return new Promise(resolve => signal.addEventListener('abort', () => {
        aborted = true; resolve({stopReason: 'aborted', answers: {}});
      }, {once: true}));
    },
  };
  await assert.rejects(runDecision(profile, resolved, {modelRegistry: registry}, {timeoutMs: 15}), error => error.category === 'timeout');
  assert.equal(calls, 1);
  assert.equal(aborted, true);
});

test('default Luna dispatches through the normal Copilot chat bridge without classifier auth', async () => {
  const resolved = core.resolveRequest({state: {text: 'hello'}, questions});
  const profile = resolved.modelConfig;
  let classifierAuthCalls = 0;
  const model = {provider: 'github-copilot', id: 'gpt-6-luna', input: ['text', 'image']};
  const registry = {
    find(provider, id) {
      assert.equal(provider, 'github-copilot');
      assert.equal(id, 'gpt-6-luna');
      return model;
    },
    getApiKeyAndHeaders() { classifierAuthCalls++; throw new Error('classifier auth must not load'); },
    streamSimple() { return {result: async () => assistant(JSON.stringify({answers: publicAnswers()}))}; },
  };
  const output = await runDecision(profile, resolved, {modelRegistry: registry}, {timeoutMs: 1000});
  assert.equal(profile.adapter, 'llm');
  assert.equal(classifierAuthCalls, 0);
  assert.deepEqual(output.answers, publicAnswers());
  assert.deepEqual(output.metadata.capabilities, {images: true, box: false});
});

test('generic LLM requires the exact normal registry lookup and has no model-type fallback', async () => {
  const profile = {adapter: 'llm', provider: 'fake', model: 'missing', images: false, box: false};
  let fallbackCalls = 0;
  await assert.rejects(runDecision(profile, resolvedFor(profile), {modelRegistry: {
    find(provider, model) { assert.deepEqual([provider, model], ['fake', 'missing']); return undefined; },
    getModelOfType() { fallbackCalls++; return {provider: 'other', id: 'fallback', input: ['text']}; },
    streamSimple() { fallbackCalls++; },
  }}, {timeoutMs: 100}), error => error.category === 'notavailable');
  assert.equal(fallbackCalls, 0);
});

test('LLM sends strict structured context with images and no tools, then validates one JSON response', async () => {
  const profile = {adapter: 'llm', provider: 'fake', model: 'chat', images: true, box: false};
  const resolved = resolvedFor(profile, {assets: {'photo-1': {type: 'image', format: 'png', data_base64: tinyPng}}});
  const model = {provider: 'fake', id: 'chat', input: ['text', 'image']};
  const calls = [];
  const registry = {
    find(provider, id) { calls.push(['find', provider, id]); return model; },
    streamSimple(actual, context, options) {
      calls.push(['stream', actual, context, options]);
      return {result: async () => assistant(JSON.stringify({answers: publicAnswers()}))};
    },
  };
  const output = await runDecision(profile, resolved, {modelRegistry: registry}, {timeoutMs: 1000});
  const [, actual, context, options] = calls[1];
  assert.equal(actual, model);
  assert.deepEqual(context.tools, []);
  assert.deepEqual(context.messages.map(message => message.role), ['system', 'user']);
  assert.match(context.messages[0].content, /exactly one JSON object/);
  assert.equal(context.messages[1].content[1].type, 'image');
  assert.deepEqual(context.messages[1].content[2], {type: 'text', text: '{"asset_id":"photo-1"}'});
  assert.deepEqual({...options, signal: undefined}, {signal: undefined, maxTokens: 4096, reasoning: 'low', maxRetries: 0});
  assert.deepEqual(output.answers, publicAnswers());
  assert.equal(output.metadata.probability_source, 'llm_estimate');
});

test('LLM rejects string score probabilities and retains paid usage', async () => {
  const profile = {adapter: 'llm', provider: 'fake', model: 'chat', images: false, box: false};
  const answers = publicAnswers();
  answers.quality = {type: 'score', score: 0.75, confidence: 0.6, probabilities: {'0': '0.25', '1': 0.75}};
  const message = assistant(JSON.stringify({answers}));
  await assert.rejects(runDecision(profile, resolvedFor(profile), {modelRegistry: {
    find() { return {provider: 'fake', id: 'chat', input: ['text']}; },
    streamSimple(_model, _context, options) {
      assert.equal(options.maxRetries, 0);
      return {result: async () => message};
    },
  }}, {timeoutMs: 100}), error => {
    assert.equal(error.category, 'output');
    assert.deepEqual(error.usage, message.usage);
    return true;
  });
});

test('LLM malformed JSON, refusal, and thrown provider errors are sanitized and never repaired or retried', async () => {
  const profile = {adapter: 'llm', provider: 'fake', model: 'chat', images: false, box: false};
  const resolved = resolvedFor(profile);
  for (const result of [
    async () => assistant('{"secret":"DO_NOT_ECHO"'),
    async () => assistant('', {stopReason: 'error', errorMessage: 'DO_NOT_ECHO input'}),
  ]) {
    let calls = 0;
    await assert.rejects(runDecision(profile, resolved, {modelRegistry: {
      find() { return {provider: 'fake', id: 'chat', input: ['text']}; },
      streamSimple() { calls++; return {result}; },
    }}, {timeoutMs: 100}), error => !error.message.includes('DO_NOT_ECHO'));
    assert.equal(calls, 1);
  }
  let calls = 0;
  await assert.rejects(runDecision(profile, resolved, {modelRegistry: {
    find() { return {provider: 'fake', id: 'chat', input: ['text']}; },
    streamSimple() { calls++; throw new Error('DO_NOT_ECHO user payload'); },
  }}, {timeoutMs: 100}), error => error.category === 'provider' && !error.message.includes('DO_NOT_ECHO'));
  assert.equal(calls, 1);
});

test('typesafe HTTP uses explicit endpoint/env auth, no redirects, bounded streaming, and opaque box output', async t => {
  const old = process.env.DECISION_BACKEND_TEST_KEY;
  process.env.DECISION_BACKEND_TEST_KEY = 'test-token';
  t.after(() => old === undefined ? delete process.env.DECISION_BACKEND_TEST_KEY : process.env.DECISION_BACKEND_TEST_KEY = old);
  const profile = {adapter: 'typesafe-http', endpoint: 'https://decision.example/pinned', auth_env: 'DECISION_BACKEND_TEST_KEY',
    provider: 'typesafe', model: 'jev', images: true, box: true};
  const config = core.deepClone(core.DEFAULT_CONFIG); config.selected = 'typed'; config.models.typed = profile;
  const resolved = core.resolveRequest({
    state: {x: 1},
    questions: {where: {type: 'box', instructions: 'Locate'}},
    assets: {'photo-1': {type: 'image', format: 'png', data_base64: tinyPng}},
  }, {config});
  let call;
  const fetch = async (...args) => {
    call = args;
    return new Response(JSON.stringify({answers: {where: {type: 'box', box: {x: 1, y: 2}}},
      usage: {input_tokens: 2, output_tokens: 1, total_cost: .1}}), {status: 200});
  };
  const output = await runDecision(profile, resolved, {}, {fetch, timeoutMs: 1000});
  assert.equal(call[0], profile.endpoint);
  assert.equal(call[1].redirect, 'error');
  assert.equal(call[1].headers.authorization, 'Bearer test-token');
  assert.deepEqual(JSON.parse(call[1].body), {
    model: 'jev', state: {x: 1}, questions: resolved.questions,
    assets: {'photo-1': {type: 'image', format: 'png', data_base64: tinyPng}},
  });
  assert.deepEqual(output.answers.where.box, {x: 1, y: 2});
  assert.deepEqual(output.metadata.capabilities, {images: true, box: true});

  await assert.rejects(runDecision(profile, resolved, {}, {fetch: async () => new Response('x'.repeat(50)), timeoutMs: 100, maxOutputBytes: 20}),
    error => error.category === 'bounds');

  const paidUsage = {input_tokens: 7, output_tokens: 2, total_cost: .4};
  await assert.rejects(runDecision(profile, resolved, {}, {fetch: async () => new Response(JSON.stringify({answers: {}, usage: paidUsage})), timeoutMs: 100}),
    error => error.category === 'output' && error.usage?.input === 7 && error.usage?.output === 2 && error.usage?.cost.total === .4);
  await assert.rejects(runDecision(profile, resolved, {}, {fetch: async () => new Response('DO_NOT_ECHO provider detail', {status: 503}), timeoutMs: 100}),
    error => error.category === 'provider' && !error.message.includes('DO_NOT_ECHO'));
});


test('Foundry api-key file is private, endpoint-bound and never becomes decision state', async t => {
  const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'decision-key-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const file=path.join(directory,'credential.json'),endpoint='https://foundry.example/providers/microsoft/v1/systemone';
  fs.writeFileSync(file,JSON.stringify({endpoint,key:'test-private-key'}),{mode:0o600});
  const profile={adapter:'typesafe-http',provider:'microsoft-foundry',model:'Decision-1.1',endpoint,auth_file:file,auth_header:'api-key',images:true,box:false};
  const resolved=resolvedFor(profile);let calls=0;
  const fetch=async(url,options)=>{
    calls++;assert.equal(url,endpoint);assert.equal(options.headers['api-key'],'test-private-key');
    assert.equal(options.headers.authorization,undefined);assert.equal(options.redirect,'error');
    assert.equal(JSON.stringify(JSON.parse(options.body)).includes('test-private-key'),false);
    assert.equal(JSON.parse(options.body).model,'Decision-1.1');
    return new Response(JSON.stringify({answers:publicAnswers()}));
  };
  assert.ok((await runDecision(profile,resolved,{}, {fetch})).answers.pick);
  fs.writeFileSync(file,JSON.stringify({endpoint:'https://another.example/',key:'test-private-key'}));
  await assert.rejects(runDecision(profile,resolved,{}, {fetch}),e=>e.category==='auth'&&!e.message.includes('test-private-key'));
  fs.writeFileSync(file,JSON.stringify({endpoint,key:'test-private-key'}));fs.chmodSync(file,0o644);
  await assert.rejects(runDecision(profile,resolved,{}, {fetch}),e=>e.category==='auth');
  fs.chmodSync(file,0o600);fs.writeFileSync(file,'not-json test-private-key');
  await assert.rejects(runDecision(profile,resolved,{}, {fetch}),e=>e.category==='auth'&&!e.message.includes('test-private-key'));
  fs.unlinkSync(file);const target=path.join(directory,'target.json');fs.writeFileSync(target,JSON.stringify({endpoint,key:'test-private-key'}),{mode:0o600});fs.symlinkSync(target,file);
  await assert.rejects(runDecision(profile,resolved,{}, {fetch}),e=>e.category==='auth');
  assert.equal(calls,1);
});

test('HTTP auth sources are exclusive and header names are bounded', () => {
  for(const extra of [{auth_env:'KEY',auth_file:'/tmp/key.json'},{auth_header:'x-arbitrary',auth_env:'KEY'},{auth_file:'relative'},{auth_header:'api-key'}]) {
    const config=core.deepClone(core.DEFAULT_CONFIG);
    config.models.test={adapter:'typesafe-http',endpoint:'https://foundry.example/',model:'Decision-1.1',images:false,box:false,...extra};
    assert.throws(()=>core.validateConfig(config),error=>error.category==='config');
  }
});
