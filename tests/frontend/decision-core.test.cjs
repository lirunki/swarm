const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const core = require('../../src/swarm/extensions/decision-core.js');
const plain = value => JSON.parse(JSON.stringify(value));
const choice = (extra = {}) => ({type: 'choice', instructions: 'Pick the best label', criteria: {yes: 'Matches', no: 'Does not match'}, ...extra});
const score = () => ({type: 'score', instructions: {task: 'Rate quality'}, criteria: ['bad', {meaning: 'okay'}, 'good']});
const noul = () => ({type: 'noul', instructions: 'Is this approved?'});
const baseRequest = (overrides = {}) => ({state: {message: 'hello'}, questions: {decision: choice()}, ...overrides});

function temporary(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'decision-core-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  return directory;
}

function tinyPng() {
  return Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+X2NDWQAAAABJRU5ErkJggg==', 'base64');
}

test('ships the requested immutable generic LLM default and optional native classifier', () => {
  assert.deepEqual(plain(core.DEFAULT_CONFIG), {
    version: 1,
    selected: 'luna',
    models: {
      luna: {adapter: 'llm', provider: 'github-copilot', model: 'gpt-6-luna', images: true, box: false},
      jev: {adapter: 'classifier', provider: 'typesafe', model: 'jev-latest', images: false, box: false},
    },
    limits: {max_input_bytes: 524288, max_output_bytes: 65536, timeout_ms: 60000},
  });
  assert.equal(Object.isFrozen(core.DEFAULT_CONFIG.models.luna), true);
  const clone = core.deepClone(core.DEFAULT_CONFIG); clone.selected = 'jev';
  assert.equal(core.DEFAULT_CONFIG.selected, 'luna');
});

test('validates choice, score, noul, structured instructions, and exact source selection', () => {
  const request = core.validateRequest({
    state: {nested: [{text: '<script>globalThis.pwned=1</script>'}]},
    questions: {pick: choice({instructions: {rule: 'literal'}}), quality: score(), approved: {...noul(), criteria: {true: 'yes', false: {why: 'no'}}}},
    model: 'jev',
  });
  assert.deepEqual(plain(request.questions.pick.instructions), {rule: 'literal'});
  assert.equal(globalThis.pwned, undefined, 'state is data and is never executed');
  assert.throws(() => core.validateRequest({...baseRequest(), state_asset: {type: 'text', text: 'also'}}), /exactly one/);
  const missing = baseRequest(); delete missing.state;
  assert.throws(() => core.validateRequest(missing), /exactly one/);
  assert.throws(() => core.validateRequest({...baseRequest(), surprise: true}), /unknown field/);
});

test('enforces question counts, criteria bounds, finite JSON, and no invented questions', () => {
  assert.throws(() => core.validateRequest({state: {}, questions: {}}), /1-8/);
  const nine = {}; for (let i = 0; i < 9; i++) nine[`q${i}`] = noul();
  assert.throws(() => core.validateRequest({state: {}, questions: nine}), /1-8/);
  assert.throws(() => core.validateRequest({state: {}, questions: {q: choice({criteria: {only: 'one'}})}}), /2-255/);
  assert.throws(() => core.validateRequest({state: {}, questions: {q: {...score(), criteria: ['one']}}}), /2-10/);
  assert.throws(() => core.validateRequest({state: JSON.parse('{"__proto__":{"polluted":true}}'), questions: {q: noul()}}), /prohibited key/);
  assert.throws(() => core.validateRequest({state: {bad: Infinity}, questions: {q: noul()}}), /finite JSON/);
  const resolved = core.resolveRequest(baseRequest());
  assert.deepEqual(Object.keys(resolved.questions), ['decision']);
});

test('classifier adapter serializes structured question fields but preserves every JSON state type', () => {
  const config = core.deepClone(core.DEFAULT_CONFIG); config.selected = 'jev';
  const states = [{x: 1}, [1, 'two', false, null], 'plain text', 42, true, false, null];
  for (const state of states) {
    const resolved = core.resolveRequest({state, questions: {
      pick: {type: 'choice', instructions: {rule: 1}, criteria: {a: null, b: {description: 'B'}}},
      rate: score(), truth: noul(),
    }}, {config});
    const native = core.nativeRequest(resolved);
    assert.deepEqual(native.state, state);
    if (state !== null && typeof state === 'object') {
      assert.notEqual(native.state, resolved.state, 'objects and arrays are cloned at the native boundary');
    }
    assert.equal(native.questions.pick.instructions, '{"rule":1}');
    assert.deepEqual(plain(native.questions.pick.criteria), {a: 'null', b: '{"description":"B"}'});
    assert.equal(native.questions.truth.type, 'bool');
    assert.deepEqual(plain(native.questions.truth.criteria), {true: 'true', false: 'false'});
  }
});

test('validates mixed choice, score, and noul answers without fabricating score metadata', () => {
  const request = core.validateRequest({state: {}, questions: {pick: choice(), quality: score(), approved: noul()}});
  const answers = core.validateAnswers(request, {
    pick: {type: 'choice', choice: 'yes', probabilities: {yes: 0.75, no: 0.25}, confidence: 0.8},
    quality: {type: 'score', score: 1.4, confidence: 0.6, probabilities: [0.1, 0.4, 0.5], legend: {source: 'jev'}},
    approved: {type: 'noul', noul: 0.9},
  });
  assert.deepEqual(plain(answers.quality.probabilities), [0.1, 0.4, 0.5]);
  assert.deepEqual(plain(answers.quality.legend), {source: 'jev'});
  const mapped = core.validateAnswers(
    {state: {}, questions: {quality: score()}},
    {quality: {type: 'score', score: 1.01, confidence: 0.7, probabilities: {'0': 0.2, '1': 0.59, '2': 0.21}}},
  );
  assert.deepEqual(plain(mapped.quality.probabilities), {'0': 0.2, '1': 0.59, '2': 0.21}, 'numeric-keyed map shape is preserved');
  assert.equal(Array.isArray(mapped.quality.probabilities), false);
  const rounded = core.validateAnswers(
    {state: {}, questions: {quality: score()}},
    {quality: {type: 'score', score: 1.42, confidence: 0.7, probabilities: [0.1, 0.4, 0.5]}},
  );
  assert.equal(rounded.quality.score, 1.42, 'two-decimal provider rounding is accepted at the 0.02 boundary');
  assert.throws(() => core.validateAnswers(
    {state: {}, questions: {quality: score()}},
    {quality: {type: 'score', score: 1.421, confidence: 0.7, probabilities: [0.1, 0.4, 0.5]}},
  ), /expected score/);
  const scoreWithoutDistribution = core.validateAnswers(
    {state: {}, questions: {quality: score()}},
    {quality: {type: 'score', score: 2, confidence: 0.7}},
  );
  assert.deepEqual(plain(scoreWithoutDistribution.quality), {type: 'score', score: 2, confidence: 0.7});
  assert.throws(() => core.validateAnswers(
    {state: {}, questions: {quality: score()}},
    {quality: {type: 'score', score: 2, confidence: 1.1}},
  ), /confidence/);
  const badScoreProbabilities = [
    ['string value', {'0': 0.1, '1': '0.4', '2': 0.5}, /finite number/],
    ['missing index', {'0': 0.5, '2': 0.5}, /exactly indices/],
    ['extra index', {'0': 0.1, '1': 0.4, '2': 0.4, '3': 0.1}, /exactly indices/],
    ['bad sum', [0.1, 0.4, 0.4], /sum to 1/],
    ['score mismatch', [0.1, 0.4, 0.5], /expected score/],
  ];
  for (const [label, probabilities, pattern] of badScoreProbabilities) {
    assert.throws(() => core.validateAnswers(
      {state: {}, questions: {quality: score()}},
      {quality: {type: 'score', score: 0, confidence: 0.7, probabilities}},
    ), pattern, label);
  }
  const sparse = [0.5, , 0.5];
  assert.throws(() => core.validateAnswers(
    {state: {}, questions: {quality: score()}},
    {quality: {type: 'score', score: 1, confidence: 0.7, probabilities: sparse}},
  ), /exactly indices/);
  const extraProperty = [0.1, 0.4, 0.5]; extraProperty.extra = 0;
  assert.throws(() => core.validateAnswers(
    {state: {}, questions: {quality: score()}},
    {quality: {type: 'score', score: 1.4, confidence: 0.7, probabilities: extraProperty}},
  ), /exactly indices/);
  assert.equal(Object.hasOwn(answers.approved, 'confidence'), false);
  assert.throws(() => core.validateAnswers(request, {...answers, extra: {}}), /exactly match/);
  assert.throws(() => core.validateAnswers({state: {}, questions: {pick: choice()}}, {pick: {type: 'choice', choice: 'yes', probabilities: {yes: .8, no: .3}, confidence: 1}}), /sum to 1/);
  assert.throws(() => core.validateAnswers({state: {}, questions: {approved: noul()}}, {approved: {type: 'bool', probability: .5}}), /unknown field|noul answer/);
});

test('box questions require verified typesafe HTTP and answers remain bridge-opaque JSON', () => {
  const input = {state: {}, questions: {where: {type: 'box', instructions: 'Locate it'}}};
  assert.throws(() => core.resolveRequest(input), error => error.category === 'capability');
  const unsupported = core.deepClone(core.DEFAULT_CONFIG); unsupported.models.luna.box = true;
  assert.throws(() => core.resolveRequest(input, {config: unsupported}), /no verified box schema/);
  const config = core.deepClone(core.DEFAULT_CONFIG);
  config.selected = 'boxed';
  config.models.boxed = {adapter: 'typesafe-http', endpoint: 'https://decision.example/v1', images: false, box: true};
  const resolved = core.resolveRequest(input, {config});
  const output = core.validateAnswers(resolved, {where: {type: 'box', box: {provider_shape: [1, 2, 3]}}}, {model: resolved.modelConfig});
  assert.deepEqual(plain(output.where), {type: 'box', box: {provider_shape: [1, 2, 3]}});
  assert.throws(() => core.validateAnswers(
    resolved,
    {where: {type: 'box', box: {provider_shape: true}, raw: 'must not escape'}},
    {model: resolved.modelConfig},
  ), /unknown field/);
  assert.deepEqual(plain(core.nativeRequest(resolved).questions), plain(resolved.questions));
});

test('loads explicit image assets, checks magic, and rejects unsupported profiles before calls', t => {
  const directory = temporary(t);
  const image = tinyPng();
  const imagePath = path.join(directory, 'pixel.png'); fs.writeFileSync(imagePath, image);
  const input = {...baseRequest(), assets: {'photo-1': {type: 'image', path: 'pixel.png'}}};
  const resolved = core.resolveRequest(input, {cwd: directory});
  assert.equal(resolved.assets['photo-1'].format, 'png');
  assert.deepEqual(core.nativeRequest(resolved).assets, resolved.assets, 'the default LLM bridge keeps the ID-keyed asset map');
  const classifierConfig = core.deepClone(core.DEFAULT_CONFIG); classifierConfig.selected = 'native';
  classifierConfig.models.native = {adapter: 'classifier', provider: 'fake', model: 'vision', images: true, box: false};
  const classifierResolved = core.resolveRequest(input, {cwd: directory, config: classifierConfig});
  assert.deepEqual(plain(classifierResolved.state), {message: 'hello'});
  assert.throws(() => core.nativeRequest(classifierResolved), error => {
    assert.equal(error.category, 'capability');
    assert.deepEqual(plain(classifierResolved.state), {message: 'hello'});
    return true;
  });
  assert.throws(() => core.resolveRequest({...input, model: 'jev'}, {cwd: directory}), error => error.category === 'capability');
  assert.throws(() => core.resolveRequest({...baseRequest(), assets: {'photo-1': {type: 'image', format: 'jpg', data_base64: image.toString('base64')}}}), /does not match/);
  assert.throws(() => core.resolveRequest({...baseRequest(), assets: Object.fromEntries(new Array(5).fill(0).map((_, index) => [`photo-${index}`, {type: 'image', path: imagePath}]))}), /at most 4/);
  assert.throws(() => core.validateRequest({...baseRequest(), assets: [{type: 'image', path: imagePath}]}), /must be a map/);
  assert.throws(() => core.validateRequest({...baseRequest(), assets: JSON.parse('{"__proto__":{"type":"image","path":"x"}}')}), /prohibited/);
});

test('projects last N user-started turns after compaction and strips thinking and image payloads', () => {
  const messages = [
    {role: 'compactionSummary', summary: 'old compacted context', tokensBefore: 100},
    {role: 'user', content: 'first'},
    {role: 'assistant', content: [{type: 'thinking', thinking: 'secret'}, {type: 'text', text: 'one'}]},
    {role: 'user', content: [{type: 'text', text: 'second'}, {type: 'image', data: 'SECRETBASE64', mimeType: 'image/png'}]},
    {role: 'assistant', content: [{type: 'text', text: 'two'}]},
  ];
  const resolved = core.resolveRequest({state_asset: {type: 'context', last_n_turns: 1}, questions: {q: noul()}}, {messages});
  assert.deepEqual(plain(resolved.state.conversation), [
    {role: 'user', content: [{type: 'text', text: 'second'}, {type: 'text', text: '[image omitted; pass it explicitly in assets]'}]},
    {role: 'assistant', content: [{type: 'text', text: 'two'}]},
  ]);
  assert.equal(JSON.stringify(resolved).includes('secret'), false);
  assert.equal(JSON.stringify(resolved).includes('SECRETBASE64'), false);
});

test('full context keeps summaries and safe tool narrative but excludes system unless explicit', () => {
  const messages = [
    {role: 'system', content: 'hidden system'},
    {role: 'compactionSummary', summary: 'checkpoint'},
    {role: 'user', content: 'task'},
    {role: 'assistant', content: [{type: 'toolCall', id: 'old', name: 'read', arguments: {path: 'x'}}], usage: {secret: 1}},
    {role: 'toolResult', toolCallId: 'old', toolName: 'read', content: [{type: 'text', text: 'result'}], isError: false, details: {path: '/secret'}},
    {role: 'custom', customType: 'notice', content: 'custom context', details: {hidden: true}},
    {role: 'custom', customType: 'private', content: 'excluded custom context', excludeFromContext: true},
  ];
  const without = core.resolveRequest({state_asset: {type: 'context'}, questions: {q: noul()}}, {messages, systemPrompt: 'effective'});
  assert.equal(JSON.stringify(without.state).includes('hidden system'), false);
  assert.equal(JSON.stringify(without.state).includes('/secret'), false);
  assert.equal(JSON.stringify(without.state).includes('excluded custom context'), false);
  assert.equal(without.state.conversation[0].role, 'compactionSummary');
  const withSystem = core.resolveRequest({state_asset: {type: 'context', include_system: true}, questions: {q: noul()}}, {messages, systemPrompt: 'effective'});
  assert.equal(withSystem.state.conversation[0].role, 'system');
  assert.equal(withSystem.state.conversation[0].content, 'effective', 'parent-provided effective prompt wins over raw system patches');
});

test('removes the entire current parallel tool batch and partial results without dropping its user', () => {
  const messages = [
    {role: 'user', content: 'make a decision'},
    {role: 'assistant', content: [
      {type: 'text', text: 'calling tools'},
      {type: 'toolCall', id: 'decision-1|provider', name: 'decision', arguments: {}},
      {type: 'toolCall', id: 'read-2', name: 'read', arguments: {path: 'x'}},
    ]},
    {role: 'toolResult', toolCallId: 'read-2', toolName: 'read', content: [{type: 'text', text: 'pending sibling'}], isError: false},
  ];
  const projected = core.projectContext(messages, {currentToolCallId: 'decision-1', lastNTurns: 1});
  assert.deepEqual(plain(projected.conversation), [{role: 'user', content: 'make a decision'}]);
  const resolved = core.resolveRequest({state_asset: {type: 'context', last_n_turns: 1}, questions: {q: noul()}}, {
    messages,
    currentToolCallId: 'decision-1',
  });
  assert.deepEqual(plain(resolved.state.conversation), [{role: 'user', content: 'make a decision'}]);
});

test('prefers a parent-provided effective projection over raw messages', () => {
  const input = {state_asset: {type: 'context'}, questions: {q: noul()}};
  const resolved = core.resolveRequest(input, {
    messages: [{role: 'user', content: 'raw hidden history'}],
    projection: {messages: [{role: 'user', content: 'effective pruned history'}]},
  });
  assert.equal(resolved.state.conversation[0].content, 'effective pruned history');
});

test('text and file assets resolve to string state and configured source bounds', t => {
  const directory = temporary(t);
  const textRequest = {state_asset: {type: 'text', text: 'requested text'}, questions: {q: noul()}};
  assert.equal(core.resolveRequest(textRequest).state, 'requested text');
  fs.writeFileSync(path.join(directory, 'state.txt'), 'hello🙂');
  fs.symlinkSync(path.join(directory, 'state.txt'), path.join(directory, 'state-link.txt'));
  const request = {state_asset: {type: 'file', path: 'state-link.txt'}, questions: {q: noul()}};
  assert.equal(core.resolveRequest(request, {cwd: directory}).state, 'hello🙂');
  fs.writeFileSync(path.join(directory, 'bad.txt'), Buffer.from([0xc3, 0x28]));
  assert.throws(() => core.resolveRequest({...request, state_asset: {type: 'file', path: 'bad.txt'}}, {cwd: directory}), /UTF-8/);
  fs.writeFileSync(path.join(directory, 'large.txt'), Buffer.alloc(600 * 1024, 0x61));
  assert.throws(() => core.resolveRequest({...request, state_asset: {type: 'file', path: 'large.txt'}}, {cwd: directory}), /exceeds/);
  const raised = core.deepClone(core.DEFAULT_CONFIG); raised.limits.max_input_bytes = 700 * 1024;
  assert.equal(core.resolveRequest({...request, state_asset: {type: 'file', path: 'large.txt'}}, {cwd: directory, config: raised}).state.length, 600 * 1024);
});

test('denies common credential source names, directories, and both sides of aliases', t => {
  const directory = temporary(t);
  const requestFor = source => ({state_asset: {type: 'file', path: source}, questions: {q: noul()}});
  const deniedNames = [
    '.env', '.env.local', '.npmrc', '.pypirc', '.netrc', '_netrc', '.git-credentials', '.pgpass', 'credentials.json',
    'service-account-prod.json', 'application_default_credentials.json', 'secret.key',
    'id_rsa', 'id_ed25519', 'private-key-prod.pem', 'cert.pem', 'apikey.txt',
  ];
  for (const name of deniedNames) {
    assert.throws(() => core.resolveRequest(requestFor(name), {cwd: directory}), /credential/, `${name} must be rejected before reading`);
  }
  const deniedPaths = [
    '.ssh/config', '.aws/config', '.azure/profile.json', '.git/config', '.kube/config', '.docker/config.json',
    '.config/gcloud/configurations/config_default', '.config/gh/hosts.yml',
  ];
  for (const relative of deniedPaths) {
    const filename = path.join(directory, relative);
    fs.mkdirSync(path.dirname(filename), {recursive: true});
    fs.writeFileSync(filename, 'secret');
    assert.throws(() => core.resolveRequest(requestFor(relative), {cwd: directory}), /credential/, `${relative} must be rejected directly`);
    const alias = `alias-${deniedPaths.indexOf(relative)}.txt`;
    fs.symlinkSync(filename, path.join(directory, alias));
    assert.throws(() => core.resolveRequest(requestFor(alias), {cwd: directory}), /credential/, `${relative} must be rejected through a symlink`);
  }
  fs.writeFileSync(path.join(directory, 'safe.txt'), 'safe');
  fs.symlinkSync(path.join(directory, 'safe.txt'), path.join(directory, '.env.alias'));
  assert.throws(() => core.resolveRequest(requestFor('.env.alias'), {cwd: directory}), /credential/);
  fs.writeFileSync(path.join(directory, 'credentials.json'), 'secret');
  fs.symlinkSync(path.join(directory, 'credentials.json'), path.join(directory, 'innocent.txt'));
  assert.throws(() => core.resolveRequest(requestFor('innocent.txt'), {cwd: directory}), /credential/);
  fs.writeFileSync(path.join(directory, 'cert.crt'), 'public certificate');
  fs.writeFileSync(path.join(directory, 'public.pem'), 'public certificate');
  assert.equal(core.resolveRequest(requestFor('cert.crt'), {cwd: directory}).state, 'public certificate');
  assert.equal(core.resolveRequest(requestFor('public.pem'), {cwd: directory}).state, 'public certificate');
});

test('applies resolved request byte budgets including base64 without truncation', () => {
  const config = core.deepClone(core.DEFAULT_CONFIG); config.limits.max_input_bytes = 200;
  assert.throws(() => core.resolveRequest({state: {text: 'x'.repeat(500)}, questions: {q: noul()}}, {config}), error => {
    assert.equal(error.category, 'bounds');
    assert.match(error.message, /resolved request is \d+ bytes/);
    assert.match(error.message, /max_input_bytes 200; context is not truncated/);
    return true;
  });
  assert.throws(() => core.validateConfig({...core.deepClone(core.DEFAULT_CONFIG), limits: {...core.DEFAULT_CONFIG.limits, max_input_bytes: 32 * 1024 * 1024 + 1}}), /max_input_bytes/);
  assert.throws(() => core.validateConfig({...core.deepClone(core.DEFAULT_CONFIG), limits: {...core.DEFAULT_CONFIG.limits, timeout_ms: 120001}}), /timeout_ms/);
});

test('native request preserves the original typesafe HTTP payload', () => {
  const config = core.deepClone(core.DEFAULT_CONFIG);
  config.selected = 'typed';
  config.models.typed = {adapter: 'typesafe-http', endpoint: 'https://decision.example/v1', images: true, box: false};
  const state = ['literal', {nested: true}, null];
  const resolved = core.resolveRequest({state, questions: {q: noul()}}, {config});
  assert.deepEqual(core.nativeRequest(resolved), {state, questions: resolved.questions});
});

test('native request rejects a mismatched execution profile before provider dispatch', () => {
  const resolved = core.resolveRequest(baseRequest());
  assert.throws(() => core.nativeRequest(resolved, {...resolved.modelConfig, adapter: 'classifier'}), /adapter does not match/);
  assert.throws(() => core.nativeRequest(resolved, {...resolved.modelConfig, provider: 'openai-codex'}), /provider does not match/);
  assert.throws(() => core.nativeRequest({questions: resolved.questions}, resolved.modelConfig), /invalid shape/);
});

test('validates strict classifier, llm, and HTTPS typesafe-http configuration without credentials', () => {
  const config = core.deepClone(core.DEFAULT_CONFIG);
  config.models.chat = {adapter: 'llm', provider: 'local', model: 'vision', images: true, box: false};
  config.models.typed = {adapter: 'typesafe-http', endpoint: 'https://decision.example/v1', auth_env: 'DECISION_TOKEN', images: false, box: true};
  assert.equal(core.validateConfig(config).models.typed.endpoint, 'https://decision.example/v1');
  assert.throws(() => core.validateConfig({...config, extra: true}), /unknown config field/);
  config.models.typed.endpoint = 'http://decision.example/v1';
  assert.throws(() => core.validateConfig(config), /HTTPS/);
  config.models.typed.endpoint = 'https://user:secret@decision.example/v1';
  assert.throws(() => core.validateConfig(config), /credentials/);
});

test('config loading is bounded, strict UTF-8, fail-closed, and never echoes malformed snippets', t => {
  const directory = temporary(t); const file = path.join(directory, 'decision.json');
  assert.deepEqual(plain(core.loadConfig(file)), plain(core.DEFAULT_CONFIG));
  fs.writeFileSync(file, '{"secret":"DO_NOT_ECHO"');
  assert.throws(() => core.loadConfig(file), error => {
    assert.equal(error.category, 'config');
    assert.equal(error.message.includes('DO_NOT_ECHO'), false);
    return true;
  });
  fs.writeFileSync(file, Buffer.from([0xc3, 0x28]));
  assert.throws(() => core.loadConfig(file), /UTF-8/);
  fs.writeFileSync(file, Buffer.alloc(16 * 1024 + 1));
  assert.throws(() => core.loadConfig(file), /exceeds/);
  assert.throws(() => core.resolveConfigPath({PI_DECISION_CONFIG: 'relative.json'}, directory), /absolute/);
  assert.equal(core.resolveConfigPath({}, directory), path.join(directory, '.pi', 'agent', 'decision.json'));
});

test('atomic updates reread under an immediate lock, preserve models, and write mode 0600', t => {
  const directory = temporary(t); const file = path.join(directory, 'decision.json');
  const updated = core.updateConfig(file, config => {
    config.selected = 'jev';
    config.models.local = {adapter: 'llm', provider: 'local', model: 'configured-by-user', images: false, box: false};
    return config;
  });
  assert.equal(updated.selected, 'jev');
  assert.equal(updated.models.luna.model, 'gpt-6-luna');
  assert.equal(updated.models.luna.box, false);
  assert.equal(updated.models.local.images, false);
  assert.equal(updated.models.local.box, false);
  assert.equal(core.loadConfig(file).models.local.model, 'configured-by-user');
  if (process.platform !== 'win32') assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  fs.writeFileSync(`${file}.lock`, 'busy');
  assert.throws(() => core.updateConfig(file, value => value), error => error.category === 'lock');
  assert.equal(fs.existsSync(`${file}.lock`), true, 'a lock owned by another process is not removed');
});

test('malformed existing config aborts update instead of falling back or overwriting', t => {
  const directory = temporary(t); const file = path.join(directory, 'decision.json');
  fs.writeFileSync(file, '{broken');
  assert.throws(() => core.updateConfig(file, () => core.deepClone(core.DEFAULT_CONFIG)), /malformed/);
  assert.equal(fs.readFileSync(file, 'utf8'), '{broken');
  assert.equal(fs.existsSync(`${file}.lock`), false);
});

test('errors expose stable category-derived codes and output size bounds', () => {
  assert.throws(() => core.validateRequest({}), error => {
    assert.equal(error.code, `DECISION_${error.category.toUpperCase()}`);
    return true;
  });
  const request = {state: {}, questions: {q: {type: 'box', instructions: 'opaque'}}};
  assert.throws(() => core.validateAnswers(request, {q: {type: 'box', box: 'x'.repeat(100)}}, {box: true, maxOutputBytes: 20}), /exceed/);
});


test('Foundry supplied and private credential paths are prohibited as state sources', () => {
  const core=require('../../src/swarm/extensions/decision-core.js');
  for(const filename of ['/workspace/decision.txt','/home/user/.pi/agent/decision-credentials/msdecision.json']) {
    assert.throws(()=>core.resolveRequest({state_asset:{type:'file',path:filename},questions:{check:{type:'noul',instructions:'Check'}}}),e=>e.category==='source');
  }
});
