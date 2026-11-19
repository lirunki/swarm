"use strict";

const fs = require("node:fs");
const { TextDecoder } = require("node:util");
const { DecisionError, nativeRequest, validateAnswers } = require("./decision-core.js");

const DEFAULT_OUTPUT_BYTES = 64 * 1024;
const DEFAULT_TIMEOUT_MS = 60 * 1000;
const LLM_MAX_TOKENS = 4096;

function own(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function safeIdentifier(value) {
  const text = typeof value === "string" ? value : "unknown";
  return /^[A-Za-z0-9._:/-]{1,255}$/.test(text) ? text : "configured";
}

function fail(category, message, usage) {
  const error = new DecisionError(category, message);
  if (usage !== undefined) error.usage = usage;
  throw error;
}

function capabilities(profile, model) {
  const inputs = Array.isArray(model?.input) ? model.input : [];
  return {
    images: profile.adapter === "typesafe-http" ? profile.images === true :
      profile.adapter === "llm" && inputs.includes("image"),
    box: profile.adapter === "typesafe-http" && profile.box === true,
  };
}

function metadata(profile, model, source, notes) {
  const value = {
    adapter: profile.adapter,
    provider: safeIdentifier(profile.provider || model?.provider || "typesafe-http"),
    model: safeIdentifier(profile.model || model?.id || "configured"),
    probability_source: source,
    capabilities: capabilities(profile, model),
  };
  if (notes?.length) value.notes = notes;
  return value;
}

function finiteNonnegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

/** Normalize Pi Usage and the nested ModelUsage shape returned by some decision services. */
function convertUsage(value) {
  if (!record(value)) return undefined;
  if (record(value.cost) && (own(value, "input") || own(value, "output"))) {
    const input = finiteNonnegative(value.input);
    const output = finiteNonnegative(value.output);
    const cacheRead = finiteNonnegative(value.cacheRead);
    const cacheWrite = finiteNonnegative(value.cacheWrite);
    return {
      input,
      output,
      cacheRead,
      cacheWrite,
      totalTokens: finiteNonnegative(value.totalTokens) || input + output + cacheRead + cacheWrite,
      cost: {
        input: finiteNonnegative(value.cost.input),
        output: finiteNonnegative(value.cost.output),
        cacheRead: finiteNonnegative(value.cost.cacheRead),
        cacheWrite: finiteNonnegative(value.cost.cacheWrite),
        total: finiteNonnegative(value.cost.total),
      },
    };
  }

  const tokens = record(value.tokens) ? value.tokens : value;
  const input = finiteNonnegative(tokens.input ?? tokens.input_tokens ?? tokens.prompt);
  const output = finiteNonnegative(tokens.output ?? tokens.output_tokens ?? tokens.completion);
  const totalCost = finiteNonnegative(
    (record(value.cost) ? value.cost.total : undefined) ?? value.total_cost ?? value.cost,
  );
  if (input === 0 && output === 0 && totalCost === 0) return undefined;
  return {
    input,
    output,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: input + output,
    // Preserve an aggregate charge without inventing how it was split across token classes.
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: totalCost },
  };
}

function answerNotes(resolved, answers) {
  const unavailable = Object.entries(resolved.questions).some(([id, question]) =>
    question.type === "score" && !own(answers[id], "probabilities"));
  return unavailable ? ["Score probabilities were not reported; no values were invented."] : undefined;
}

function normalizeClassifierAnswers(resolved, answers, maxOutputBytes, profile) {
  if (!record(answers)) fail("output", "Decision classifier returned an invalid answer object");
  const normalized = {};
  for (const [id, question] of Object.entries(resolved.questions)) {
    const answer = answers[id];
    if (question.type === "noul" && record(answer) && answer.type === "bool") {
      normalized[id] = { type: "noul", noul: answer.probability };
    } else {
      normalized[id] = answer;
    }
  }
  return validateAnswers(resolved, normalized, { model: profile, maxOutputBytes });
}

function assertRequestCapabilities(resolved, profile, model) {
  if (Object.values(resolved.questions).some(question => question.type === "box") && profile.adapter !== "typesafe-http") {
    fail("capability", `Decision adapter ${safeIdentifier(profile.adapter)} does not support box questions`);
  }
  if (resolved.assets && Object.keys(resolved.assets).length && !capabilities(profile, model).images) {
    fail("capability", `Decision model ${safeIdentifier(profile.provider)}/${safeIdentifier(profile.model)} does not support images`);
  }
}

function withDeadline(work, callerSignal, timeoutMs) {
  if (callerSignal?.aborted) {
    return Promise.reject(new DecisionError("aborted", "Decision request was aborted"));
  }
  const controller = new AbortController();
  let timedOut = false;
  let callerAborted = false;
  let timer;
  let rejectTerminal;
  const terminal = new Promise((_, reject) => { rejectTerminal = reject; });
  const abortFromCaller = () => {
    callerAborted = true;
    controller.abort();
    rejectTerminal(new DecisionError("aborted", "Decision request was aborted"));
  };
  callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
    rejectTerminal(new DecisionError("timeout", "Decision request timed out"));
  }, timeoutMs);

  let operation;
  try { operation = Promise.resolve(work(controller.signal)); }
  catch (error) { operation = Promise.reject(error); }
  // The rejection handler remains attached if the hard deadline wins the race.
  operation.catch(() => {});
  return Promise.race([operation, terminal]).catch(error => {
    if (timedOut) fail("timeout", "Decision request timed out", error?.usage);
    if (callerAborted) fail("aborted", "Decision request was aborted", error?.usage);
    throw error;
  }).finally(() => {
    clearTimeout(timer);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  });
}

function classifierModel(registry, profile) {
  if (!registry || typeof registry.getModelOfType !== "function") {
    fail("notavailable", "Decision classifier registry is unavailable");
  }
  const model = registry.getModelOfType("classifier", profile.provider, profile.model);
  if (!model || model.type !== "classifier") {
    fail("notavailable", `Decision classifier ${safeIdentifier(profile.provider)}/${safeIdentifier(profile.model)} is not available`);
  }
  return model;
}

async function runClassifier(profile, resolved, ctx, options) {
  if (!record(resolved.state)) {
    fail("capability", "Native decision classifiers require object state; use an LLM or direct HTTP adapter for primitive state");
  }
  if (resolved.assets && Object.keys(resolved.assets).length) {
    fail("capability", "Native decision classifiers cannot preserve asset ID associations; use an LLM or direct HTTP adapter for images");
  }
  const registry = ctx?.modelRegistry;
  const model = classifierModel(registry, profile);
  assertRequestCapabilities(resolved, profile, model);
  if (profile.provider === "openai" && profile.model === "gpt-6-luna" &&
      typeof registry.isUsingOAuth === "function" && registry.isUsingOAuth(model)) {
    fail("auth", "Decision classifier openai/gpt-6-luna requires an API key; OAuth is not supported");
  }
  if (typeof registry.getApiKeyAndHeaders !== "function") fail("auth", "Decision classifier authentication is unavailable");
  const auth = await registry.getApiKeyAndHeaders(model);
  if (!auth?.ok) fail("auth", `Authentication is unavailable for decision classifier ${safeIdentifier(profile.provider)}/${safeIdentifier(profile.model)}`);
  if (typeof registry.classify !== "function") fail("notavailable", "Decision classifier execution is unavailable");

  const result = await registry.classify(model, nativeRequest(resolved, profile), { signal: options.signal });
  const usage = convertUsage(result?.usage);
  if (!record(result) || result.stopReason !== "stop") {
    const category = result?.stopReason === "aborted" ? "aborted" : "provider";
    fail(category, `Decision classifier ${safeIdentifier(profile.provider)}/${safeIdentifier(profile.model)} failed`, usage);
  }
  let answers;
  try { answers = normalizeClassifierAnswers(resolved, result.answers, options.maxOutputBytes, profile); }
  catch (_) { fail("output", "Decision classifier returned invalid answers", usage); }
  return {
    answers,
    ...(usage ? { usage } : {}),
    metadata: metadata(profile, model, "native", answerNotes(resolved, answers)),
  };
}

function llmModel(registry, profile) {
  if (!registry || typeof registry.find !== "function") fail("notavailable", "Decision model registry is unavailable");
  const model = registry.find(profile.provider, profile.model);
  if (!model) fail("notavailable", `Decision model ${safeIdentifier(profile.provider)}/${safeIdentifier(profile.model)} is not available`);
  return model;
}

function llmSystemPrompt(resolved) {
  const guide = Object.fromEntries(Object.entries(resolved.questions).map(([id, question]) => {
    if (question.type === "choice") return [id, { type: "choice", choice: "one criteria label", probabilities: "object containing every label and summing to 1", confidence: "number 0..1" }];
    if (question.type === "score") return [id, { type: "score", score: `number 0..${question.criteria.length - 1}`, confidence: "number 0..1", probabilities: "optional; if supplied, use them to calculate the expected score" }];
    if (question.type === "noul") return [id, { type: "noul", noul: "probability from 0..1" }];
    return [id, { type: "box", box: "provider-specific value" }];
  }));
  return [
    "Return exactly one JSON object with the single key answers. Do not use Markdown or code fences.",
    "Return no commentary, tool calls, or extra keys. Answer every question exactly once.",
    "Choice probabilities are your own uncalibrated estimates; do not claim they are calibrated.",
    `Required answer shapes: ${JSON.stringify(guide)}`,
  ].join("\n");
}

function llmContext(resolved) {
  const timestamp = Date.now();
  const content = [{ type: "text", text: JSON.stringify({ state: resolved.state, questions: resolved.questions }) }];
  for (const [id, asset] of Object.entries(resolved.assets || {})) {
    content.push({ type: "image", data: asset.data_base64, mimeType: `image/${asset.format === "jpg" ? "jpeg" : asset.format}` });
    content.push({ type: "text", text: JSON.stringify({ asset_id: id }) });
  }
  return {
    messages: [
      { role: "system", content: llmSystemPrompt(resolved), timestamp },
      { role: "user", content, timestamp },
    ],
    tools: [],
  };
}

function assistantText(message, maxOutputBytes) {
  if (!record(message) || message.stopReason !== "stop" || !Array.isArray(message.content)) {
    fail(message?.stopReason === "aborted" ? "aborted" : "provider", "Decision language model did not complete normally", convertUsage(message?.usage));
  }
  if (message.content.some(block => block?.type === "toolCall")) fail("output", "Decision language model returned a tool call", convertUsage(message.usage));
  const text = message.content.filter(block => block?.type === "text").map(block => block.text).join("");
  if (!text || Buffer.byteLength(text, "utf8") > maxOutputBytes) fail("bounds", `Decision response exceeds ${maxOutputBytes} bytes`, convertUsage(message.usage));
  return text;
}

async function runLlm(profile, resolved, ctx, options) {
  const registry = ctx?.modelRegistry;
  const model = llmModel(registry, profile);
  assertRequestCapabilities(resolved, profile, model);
  if (typeof registry.streamSimple !== "function") fail("notavailable", "Decision language model execution is unavailable");
  const stream = registry.streamSimple(model, llmContext(resolved), {
    signal: options.signal,
    maxTokens: LLM_MAX_TOKENS,
    reasoning: "low",
    maxRetries: 0,
  });
  if (!stream || typeof stream.result !== "function") fail("provider", "Decision language model returned an invalid stream");
  const message = await stream.result();
  const usage = convertUsage(message?.usage);
  let parsed;
  try { parsed = JSON.parse(assistantText(message, options.maxOutputBytes)); }
  catch (error) {
    if (error instanceof DecisionError) throw error;
    fail("output", "Decision language model returned malformed JSON", usage);
  }
  if (!record(parsed) || Object.keys(parsed).length !== 1 || !own(parsed, "answers")) {
    fail("output", "Decision language model returned an invalid response object", usage);
  }
  let answers;
  try { answers = validateAnswers(resolved, parsed.answers, { model: profile, maxOutputBytes: options.maxOutputBytes }); }
  catch (_) { fail("output", "Decision language model returned invalid answers", usage); }
  return { answers, ...(usage ? { usage } : {}), metadata: metadata(profile, model, "llm_estimate", answerNotes(resolved, answers)) };
}

async function readBoundedResponse(response, maximum) {
  if (!response?.body || typeof response.body.getReader !== "function") fail("provider", "Decision HTTP service returned an unreadable response");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let bytes = 0;
  let text = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > maximum) {
        await reader.cancel();
        fail("bounds", `Decision response exceeds ${maximum} bytes`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return text;
  } catch (error) {
    if (error instanceof DecisionError) throw error;
    fail("provider", "Decision HTTP service returned an invalid response body");
  }
}

function httpHeaders(profile) {
  let token;
  if (profile.auth_file !== undefined) {
    let fd;
    try {
      fd = fs.openSync(profile.auth_file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
      const before = fs.fstatSync(fd, {bigint: true});
      if (!before.isFile() || before.nlink !== 1n || before.size > 16384n ||
          (process.platform !== 'win32' && (before.mode & 0o077n)) ||
          (typeof process.getuid === 'function' && before.uid !== BigInt(process.getuid()))) throw Error();
      const bytes = Buffer.alloc(Number(before.size));
      let offset = 0;
      while (offset < bytes.length) {
        const count = fs.readSync(fd, bytes, offset, bytes.length - offset, offset);
        if (!count) throw Error();
        offset += count;
      }
      const after = fs.fstatSync(fd, {bigint: true});
      if (['dev','ino','size','mtimeNs','ctimeNs'].some(key => before[key] !== after[key])) throw Error();
      const value = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
      if (!record(value) || Object.keys(value).sort().join(',') !== 'endpoint,key' || value.endpoint !== profile.endpoint) throw Error();
      token = value.key;
    } catch (_) { fail('auth', 'Decision credential file is unavailable, unsafe, or not bound to this endpoint'); }
    finally { if (fd !== undefined) fs.closeSync(fd); }
  } else if (profile.auth_env) token = process.env[profile.auth_env];
  if (profile.auth_env || profile.auth_file) {
    if (typeof token !== 'string' || !token || token.length > 8192 || /[\x00-\x20\x7f-\uffff]/.test(token)) fail('auth', 'Decision HTTP service credential is unavailable or invalid');
  }
  const headers = {'content-type':'application/json'};
  if (token) {
    if (profile.auth_header === 'api-key') headers['api-key'] = token;
    else headers.authorization = `Bearer ${token}`;
  }
  return headers;
}

async function runTypesafeHttp(profile, resolved, _ctx, options) {
  assertRequestCapabilities(resolved, profile);
  const requestFetch = options.fetch || globalThis.fetch;
  if (typeof requestFetch !== "function") fail("notavailable", "Decision HTTP transport is unavailable");
  const headers = httpHeaders(profile);
  const native = nativeRequest(resolved, profile);
  const response = await requestFetch(profile.endpoint, {
    method: "POST",
    redirect: "error",
    headers,
    body: JSON.stringify({ ...(profile.model ? { model: profile.model } : {}), ...native }),
    signal: options.signal,
  });
  if (!response?.ok) fail("provider", `Decision HTTP service failed with status ${Number.isInteger(response?.status) ? response.status : "unknown"}`);
  const text = await readBoundedResponse(response, options.maxOutputBytes);
  let payload;
  try { payload = JSON.parse(text); }
  catch (_) { fail("output", "Decision HTTP service returned malformed JSON"); }
  const usage = convertUsage(payload?.usage);
  if (!record(payload) || !record(payload.answers)) fail("output", "Decision HTTP service returned an invalid response object", usage);
  let answers;
  try { answers = validateAnswers(resolved, payload.answers, { model: profile, maxOutputBytes: options.maxOutputBytes }); }
  catch (_) { fail("output", "Decision HTTP service returned invalid answers", usage); }
  return { answers, ...(usage ? { usage } : {}), metadata: metadata(profile, undefined, "native", answerNotes(resolved, answers)) };
}

/** Execute one already-resolved decision request through its explicitly configured adapter. */
async function runDecision(profile, resolved, ctx, { signal, timeoutMs, maxOutputBytes, fetch } = {}) {
  if (!record(profile) || !record(resolved)) fail("validation", "Decision profile and resolved request are required");
  const timeout = timeoutMs ?? resolved.limits?.timeout_ms ?? DEFAULT_TIMEOUT_MS;
  const outputLimit = maxOutputBytes ?? resolved.limits?.max_output_bytes ?? DEFAULT_OUTPUT_BYTES;
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 120000) fail("validation", "Decision timeout is invalid");
  if (!Number.isSafeInteger(outputLimit) || outputLimit < 1 || outputLimit > DEFAULT_OUTPUT_BYTES) fail("validation", "Decision output limit is invalid");

  try {
    return await withDeadline(deadlineSignal => {
      const options = { signal: deadlineSignal, timeoutMs: timeout, maxOutputBytes: outputLimit, fetch };
      if (profile.adapter === "classifier") return runClassifier(profile, resolved, ctx, options);
      if (profile.adapter === "llm") return runLlm(profile, resolved, ctx, options);
      if (profile.adapter === "typesafe-http") return runTypesafeHttp(profile, resolved, ctx, options);
      fail("config", `Decision adapter ${safeIdentifier(profile.adapter)} is unsupported`);
    }, signal, timeout);
  } catch (error) {
    if (error instanceof DecisionError) throw error;
    fail("provider", `Decision request failed for ${safeIdentifier(profile.provider || profile.adapter)}/${safeIdentifier(profile.model || resolved.model)}`, convertUsage(error?.usage));
  }
}

module.exports = { runDecision };
