"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { TextDecoder } = require("node:util");

const CONFIG_BYTES = 16 * 1024;
const IMAGE_BYTES = 8 * 1024 * 1024;
const TOTAL_IMAGE_BYTES = 16 * 1024 * 1024;
const MAX_INPUT_BYTES = 32 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 64 * 1024;
const MAX_TIMEOUT_MS = 120000;
const SCORE_EXPECTED_TOLERANCE = 0.02;
const JSON_DEPTH = 64;
const OWN = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

const DEFAULT_CONFIG = deepFreeze({
  version: 1,
  selected: "luna",
  models: {
    luna: { adapter: "llm", provider: "github-copilot", model: "gpt-6-luna", images: true, box: false },
    jev: { adapter: "classifier", provider: "typesafe", model: "jev-latest", images: false, box: false },
  },
  limits: { max_input_bytes: 524288, max_output_bytes: 65536, timeout_ms: 60000 },
});

class DecisionError extends Error {
  constructor(category, message) {
    super(message);
    this.name = "DecisionError";
    this.category = category;
    this.code = `DECISION_${category.toUpperCase()}`;
  }
}

function fail(category, message) {
  throw new DecisionError(category, message);
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function jsonCopy(value, label = "value") {
  const seen = new Set();
  function visit(item, depth) {
    if (depth > JSON_DEPTH) fail("validation", `${label} is nested too deeply`);
    if (item === null || typeof item === "string" || typeof item === "boolean") return item;
    if (typeof item === "number") {
      if (!Number.isFinite(item)) fail("validation", `${label} must contain only finite JSON numbers`);
      return item;
    }
    if (Array.isArray(item)) {
      if (seen.has(item)) fail("validation", `${label} must not be cyclic`);
      seen.add(item);
      const result = item.map(child => visit(child, depth + 1));
      seen.delete(item);
      return result;
    }
    if (isObject(item)) {
      const prototype = Object.getPrototypeOf(item);
      if (prototype !== Object.prototype && prototype !== null) fail("validation", `${label} must contain only JSON values`);
      if (seen.has(item)) fail("validation", `${label} must not be cyclic`);
      seen.add(item);
      const result = {};
      for (const key of Object.keys(item)) {
        if (key === "__proto__" || key === "constructor" || key === "prototype") {
          fail("validation", `${label} contains a prohibited key`);
        }
        const child = item[key];
        if (child === undefined || typeof child === "function" || typeof child === "symbol" || typeof child === "bigint") {
          fail("validation", `${label} must contain only JSON values`);
        }
        result[key] = visit(child, depth + 1);
      }
      seen.delete(item);
      return result;
    }
    fail("validation", `${label} must contain only JSON values`);
  }
  return visit(value, 0);
}

function deepClone(value) {
  return jsonCopy(value, "value");
}

function exactKeys(value, allowed, label) {
  for (const key of Object.keys(value)) if (!allowed.has(key)) fail("validation", `${label} has unknown field: ${key}`);
}

function nonemptyString(value, label, maximum = 8192) {
  if (typeof value !== "string" || value.length === 0 || Buffer.byteLength(value, "utf8") > maximum) {
    fail("validation", `${label} must be a non-empty string of at most ${maximum} bytes`);
  }
  return value;
}

function safeMapKey(value, label) {
  nonemptyString(value, label, 255);
  if (value === "__proto__" || value === "constructor" || value === "prototype") fail("validation", `${label} is prohibited`);
  return value;
}

function structured(value, label) {
  if (typeof value === "string") return nonemptyString(value, label, MAX_INPUT_BYTES);
  return jsonCopy(value, label);
}

function validateLimits(limits) {
  if (!isObject(limits)) fail("config", "limits must be an object");
  const allowed = new Set(["max_input_bytes", "max_output_bytes", "timeout_ms"]);
  for (const key of Object.keys(limits)) if (!allowed.has(key)) fail("config", `unknown limits field: ${key}`);
  const checks = [
    ["max_input_bytes", 1, MAX_INPUT_BYTES],
    ["max_output_bytes", 1, MAX_OUTPUT_BYTES],
    ["timeout_ms", 1, MAX_TIMEOUT_MS],
  ];
  for (const [key, minimum, maximum] of checks) {
    if (!Number.isSafeInteger(limits[key]) || limits[key] < minimum || limits[key] > maximum) {
      fail("config", `${key} must be an integer from ${minimum} to ${maximum}`);
    }
  }
  return deepClone(limits);
}

function validateModel(model, alias) {
  if (!isObject(model)) fail("config", `model ${alias} must be an object`);
  const common = new Set(["adapter", "provider", "model", "images", "box"]);
  const http = new Set(["adapter", "endpoint", "auth_env", "auth_file", "auth_header", "provider", "model", "images", "box"]);
  if (!new Set(["classifier", "llm", "typesafe-http"]).has(model.adapter)) fail("config", `model ${alias} has an unsupported adapter`);
  const allowed = model.adapter === "typesafe-http" ? http : common;
  for (const key of Object.keys(model)) if (!allowed.has(key)) fail("config", `model ${alias} has unknown field: ${key}`);
  if (model.adapter === "typesafe-http") {
    nonemptyString(model.endpoint, `model ${alias} endpoint`);
    let endpoint;
    try { endpoint = new URL(model.endpoint); } catch (_) { fail("config", `model ${alias} endpoint must be HTTPS`); }
    if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password) fail("config", `model ${alias} endpoint must be HTTPS without embedded credentials`);
    if (model.auth_env !== undefined && (typeof model.auth_env !== "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(model.auth_env))) {
      fail("config", `model ${alias} auth_env must be an environment variable name`);
    }
    if (model.auth_file !== undefined && (typeof model.auth_file !== 'string' || !path.isAbsolute(model.auth_file) || /[\x00-\x1f\x7f]/.test(model.auth_file))) {
      fail('config', `model ${alias} auth_file must be an absolute path`);
    }
    if (model.auth_env !== undefined && model.auth_file !== undefined) fail('config', `model ${alias} must select one credential source`);
    if (model.auth_header !== undefined && !['authorization', 'api-key'].includes(model.auth_header)) fail('config', `model ${alias} has an unsupported auth_header`);
    if (model.auth_header !== undefined && model.auth_env === undefined && model.auth_file === undefined) fail('config', `model ${alias} auth_header requires credentials`);
    if (model.provider !== undefined) nonemptyString(model.provider, `model ${alias} provider`);
    if (model.model !== undefined) nonemptyString(model.model, `model ${alias} model`);
  } else {
    nonemptyString(model.provider, `model ${alias} provider`);
    nonemptyString(model.model, `model ${alias} model`);
  }
  if (typeof model.images !== "boolean" || typeof model.box !== "boolean") fail("config", `model ${alias} images and box must be boolean`);
  if (Buffer.byteLength(JSON.stringify(model), "utf8") > 8192) fail("config", `model ${alias} exceeds 8192 bytes`);
  return deepClone(model);
}

function validateConfig(value) {
  if (!isObject(value)) fail("config", "config must be a JSON object");
  for (const key of Object.keys(value)) if (!new Set(["version", "selected", "models", "limits"]).has(key)) fail("config", `unknown config field: ${key}`);
  if (value.version !== 1) fail("config", "config version must be 1");
  nonemptyString(value.selected, "selected model", 255);
  if (!isObject(value.models) || Object.keys(value.models).length === 0) fail("config", "models must be a non-empty object");
  const models = {};
  for (const [alias, model] of Object.entries(value.models)) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(alias) || alias === "constructor" || alias === "prototype") fail("config", "model aliases must be simple safe names");
    models[alias] = validateModel(model, alias);
  }
  if (!OWN(models, value.selected)) fail("config", "selected model is not configured");
  return { version: 1, selected: value.selected, models, limits: validateLimits(value.limits) };
}

function resolveConfigPath(env = process.env, home = os.homedir()) {
  if (env.PI_DECISION_CONFIG) {
    if (!path.isAbsolute(env.PI_DECISION_CONFIG)) fail("config", "PI_DECISION_CONFIG must be an absolute path");
    return path.normalize(env.PI_DECISION_CONFIG);
  }
  const agentDir = path.resolve(env.PI_CODING_AGENT_DIR || path.join(home, ".pi", "agent"));
  return path.join(agentDir, "decision.json");
}

function decodeUtf8(buffer, label, category = "source") {
  try { return new TextDecoder("utf-8", { fatal: true }).decode(buffer).replace(/^\uFEFF/, ""); }
  catch (_) { fail(category, `${label} is not valid UTF-8`); }
}

function readRegular(filename, maximum, label, options = {}) {
  let fd;
  try {
    fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  } catch (error) {
    if (options.missing && error && error.code === "ENOENT") return undefined;
    fail(options.category || "source", `${label} is unavailable`);
  }
  try {
    const before = fs.fstatSync(fd, { bigint: true });
    if (!before.isFile()) fail(options.category || "source", `${label} is not a regular file`);
    if (before.size > BigInt(maximum)) fail(options.category || "source", `${label} exceeds ${maximum} bytes`);
    const buffer = Buffer.alloc(Number(before.size));
    let offset = 0;
    while (offset < buffer.length) {
      const count = fs.readSync(fd, buffer, offset, buffer.length - offset, offset);
      if (!count) fail(options.category || "source", `${label} changed while reading`);
      offset += count;
    }
    const after = fs.fstatSync(fd, { bigint: true });
    for (const key of ["dev", "ino", "size", "mtimeNs", "ctimeNs"]) {
      if (String(before[key]) !== String(after[key])) fail(options.category || "source", `${label} changed while reading`);
    }
    return buffer;
  } finally { fs.closeSync(fd); }
}

function loadConfig(filename = resolveConfigPath()) {
  const bytes = readRegular(filename, CONFIG_BYTES, "decision config", { missing: true, category: "config" });
  if (bytes === undefined) return deepClone(DEFAULT_CONFIG);
  let value;
  try { value = JSON.parse(decodeUtf8(bytes, "decision config", "config")); }
  catch (error) {
    if (error instanceof DecisionError) throw error;
    fail("config", "decision config is malformed JSON");
  }
  return validateConfig(value);
}

function updateConfig(filename, updater) {
  if (typeof filename !== "string" || !path.isAbsolute(filename)) fail("config", "config path must be absolute");
  if (typeof updater !== "function") fail("config", "config updater must be a function");
  const directory = path.dirname(filename);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const lock = `${filename}.lock`;
  let lockFd;
  try { lockFd = fs.openSync(lock, "wx", 0o600); }
  catch (_) { fail("lock", "decision config is locked by another update"); }
  let temporary;
  try {
    const current = loadConfig(filename);
    const candidate = updater(deepClone(current));
    if (candidate && typeof candidate.then === "function") fail("config", "config updater must be synchronous");
    const validated = validateConfig(candidate);
    const output = Buffer.from(`${JSON.stringify(validated, null, 2)}\n`, "utf8");
    if (output.length > CONFIG_BYTES) fail("config", `decision config exceeds ${CONFIG_BYTES} bytes`);
    temporary = `${filename}.${crypto.randomUUID()}.tmp`;
    const fd = fs.openSync(temporary, "wx", 0o600);
    try { fs.writeFileSync(fd, output); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(temporary, filename);
    temporary = undefined;
    try { fs.chmodSync(filename, 0o600); } catch (_) { /* ACL-backed platforms may ignore POSIX modes. */ }
    return validated;
  } finally {
    if (temporary) try { fs.unlinkSync(temporary); } catch (_) { /* best effort */ }
    try { fs.closeSync(lockFd); } catch (_) { /* best effort */ }
    try { fs.unlinkSync(lock); } catch (_) { /* best effort */ }
  }
}

function validateQuestion(question, id) {
  if (!isObject(question)) fail("validation", `question ${id} must be an object`);
  const type = question.type;
  if (!new Set(["choice", "score", "noul", "box"]).has(type)) fail("validation", `question ${id} has an unsupported type`);
  exactKeys(question, type === "box" ? new Set(["type", "instructions"]) : new Set(["type", "instructions", "criteria"]), `question ${id}`);
  const result = { type, instructions: structured(question.instructions, `question ${id} instructions`) };
  if (type === "box") return result;
  if (type === "choice") {
    if (!isObject(question.criteria)) fail("validation", `question ${id} criteria must be an object`);
    const labels = Object.keys(question.criteria);
    if (labels.length < 2 || labels.length > 255) fail("validation", `question ${id} must have 2-255 choices`);
    result.criteria = {};
    for (const label of labels) {
      safeMapKey(label, `question ${id} choice label`);
      const description = question.criteria[label];
      result.criteria[label] = description === null ? null : structured(description, `question ${id} choice ${label}`);
    }
  } else if (type === "score") {
    if (!Array.isArray(question.criteria) || question.criteria.length < 2 || question.criteria.length > 10) fail("validation", `question ${id} must have 2-10 score levels`);
    result.criteria = question.criteria.map((item, index) => structured(item, `question ${id} score level ${index}`));
  } else if (question.criteria !== undefined) {
    if (!isObject(question.criteria) || Object.keys(question.criteria).length !== 2 || !OWN(question.criteria, "true") || !OWN(question.criteria, "false")) {
      fail("validation", `question ${id} noul criteria must contain exactly true and false`);
    }
    result.criteria = {
      true: structured(question.criteria.true, `question ${id} true criterion`),
      false: structured(question.criteria.false, `question ${id} false criterion`),
    };
  }
  return result;
}

function validateRequest(input) {
  if (!isObject(input)) fail("validation", "request must be an object");
  exactKeys(input, new Set(["state", "state_asset", "questions", "assets", "model"]), "request");
  if (OWN(input, "state") === OWN(input, "state_asset")) fail("validation", "request must contain exactly one of state or state_asset");
  const request = {};
  if (OWN(input, "state")) request.state = jsonCopy(input.state, "state");
  else {
    const asset = input.state_asset;
    if (!isObject(asset) || !new Set(["text", "file", "context"]).has(asset.type)) fail("validation", "state_asset has an unsupported type");
    const allowed = asset.type === "context" ? new Set(["type", "last_n_turns", "include_system"]) : new Set(["type", asset.type === "text" ? "text" : "path"]);
    exactKeys(asset, allowed, "state_asset");
    request.state_asset = { type: asset.type };
    if (asset.type === "text") request.state_asset.text = nonemptyString(asset.text, "state_asset text", MAX_INPUT_BYTES);
    if (asset.type === "file") request.state_asset.path = nonemptyString(asset.path, "state_asset path", 8192);
    if (asset.type === "context") {
      if (asset.last_n_turns !== undefined && (!Number.isSafeInteger(asset.last_n_turns) || asset.last_n_turns < 1)) fail("validation", "last_n_turns must be a positive integer");
      if (asset.include_system !== undefined && typeof asset.include_system !== "boolean") fail("validation", "include_system must be boolean");
      if (asset.last_n_turns !== undefined) request.state_asset.last_n_turns = asset.last_n_turns;
      request.state_asset.include_system = asset.include_system === true;
    }
  }
  if (!isObject(input.questions)) fail("validation", "questions must be an object");
  const ids = Object.keys(input.questions);
  if (ids.length < 1 || ids.length > 8) fail("validation", "questions must contain 1-8 entries");
  request.questions = {};
  for (const id of ids) {
    safeMapKey(id, "question ID");
    request.questions[id] = validateQuestion(input.questions[id], id);
  }
  if (input.assets !== undefined) {
    if (!isObject(input.assets) || Object.keys(input.assets).length > 4) fail("validation", "assets must be a map containing at most 4 images");
    request.assets = {};
    for (const [id, asset] of Object.entries(input.assets)) {
      safeMapKey(id, "asset ID");
      if (!isObject(asset) || asset.type !== "image") fail("validation", `asset ${id} must be an image`);
      if (OWN(asset, "path") === OWN(asset, "data_base64")) fail("validation", `asset ${id} must contain exactly one of path or data_base64`);
      if (OWN(asset, "path")) {
        exactKeys(asset, new Set(["type", "path"]), `asset ${id}`);
        request.assets[id] = { type: "image", path: nonemptyString(asset.path, `asset ${id} path`, 8192) };
      } else {
        exactKeys(asset, new Set(["type", "format", "data_base64"]), `asset ${id}`);
        if (!new Set(["jpg", "png", "gif", "webp"]).has(asset.format)) fail("validation", `asset ${id} format is unsupported`);
        nonemptyString(asset.data_base64, `asset ${id} data_base64`, MAX_INPUT_BYTES * 2);
        request.assets[id] = { type: "image", format: asset.format, data_base64: asset.data_base64 };
      }
    }
  }
  if (input.model !== undefined) request.model = nonemptyString(input.model, "model", 128);
  return request;
}

// This is a best-effort filename guard, not a guarantee that a file contains no
// secrets: callers and parent processes remain responsible for what they write.
function prohibitedSource(filename) {
  const normalized = filename.replaceAll("\\", "/").toLowerCase();
  const segments = normalized.split("/").filter(Boolean);
  const base = segments.at(-1) || "";
  const credentialName = base === ".env" || base.startsWith(".env.") ||
    new Set([
      ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", ".pgpass", "auth.json", "apikey.txt", "cert.pem",
      "credentials.json", "decision.txt", "application_default_credentials.json", "swarm-password.txt", "id_rsa", "id_ed25519",
    ]).has(base) ||
    base.endsWith(".key") || /^service-account.*\.json$/.test(base) || /^private-key.*\.pem$/.test(base);
  const deniedDirectories = new Set([".ssh", ".aws", ".azure", ".git", ".kube", ".docker", "decision-credentials"]);
  const credentialDirectory = segments.some(segment => deniedDirectories.has(segment)) ||
    segments.some((segment, index) => segment === ".config" && new Set(["gcloud", "gh"]).has(segments[index + 1]));
  return credentialName || credentialDirectory;
}

function resolveUserFile(rawPath, cwd, maximum, label) {
  const absolute = path.resolve(cwd, rawPath);
  if (prohibitedSource(absolute)) fail("source", `${label} is a prohibited credential file`);
  let real;
  try { real = fs.realpathSync(absolute); } catch (_) { fail("source", `${label} is unavailable`); }
  if (prohibitedSource(real)) fail("source", `${label} is a prohibited credential file`);
  return readRegular(real, maximum, label);
}

function contentBlocks(content, role) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const blocks = [];
  for (const block of content) {
    if (!isObject(block)) continue;
    if (block.type === "text" && typeof block.text === "string") blocks.push({ type: "text", text: block.text });
    else if (block.type === "image") blocks.push({ type: "text", text: "[image omitted; pass it explicitly in assets]" });
    else if (role === "assistant" && block.type === "toolCall" && typeof block.id === "string" && typeof block.name === "string") {
      blocks.push({ type: "toolCall", id: block.id, name: block.name, arguments: jsonCopy(block.arguments, "tool call arguments") });
    }
  }
  return blocks;
}

function sameToolId(left, right) {
  if (typeof left !== "string" || typeof right !== "string") return false;
  return left === right || left.split("|", 1)[0] === right.split("|", 1)[0];
}

function withoutCurrentToolBatch(messages, currentToolCallId) {
  if (!currentToolCallId) return messages.slice();
  let batch = -1;
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (message?.role === "assistant" && Array.isArray(message.content) && message.content.some(block => block?.type === "toolCall" && sameToolId(block.id, currentToolCallId))) {
      batch = index;
      break;
    }
  }
  if (batch < 0) return messages.slice();
  return messages.filter((message, index) => index < batch || (index > batch && message?.role === "user"));
}

function projectContext(messages, options = {}) {
  if (!Array.isArray(messages)) fail("source", "context messages are unavailable");
  let selected = withoutCurrentToolBatch(messages, options.currentToolCallId);
  if (options.lastNTurns !== undefined) {
    let count = 0;
    let start = selected.length;
    for (let index = selected.length - 1; index >= 0; index--) {
      if (selected[index]?.role === "user" && ++count === options.lastNTurns) { start = index; break; }
    }
    if (count > 0) selected = selected.slice(start);
  }
  const conversation = [];
  const hasEffectiveSystem = options.includeSystem && typeof options.systemPrompt === "string";
  if (hasEffectiveSystem) conversation.push({ role: "system", content: options.systemPrompt });
  for (const message of selected) {
    if (!isObject(message)) continue;
    switch (message.role) {
      case "system":
        if (options.includeSystem && !hasEffectiveSystem) conversation.push({ role: "system", content: contentBlocks(message.content, "system") });
        break;
      case "user": conversation.push({ role: "user", content: contentBlocks(message.content, "user") }); break;
      case "assistant": conversation.push({ role: "assistant", content: contentBlocks(message.content, "assistant") }); break;
      case "toolResult":
        conversation.push({ role: "toolResult", toolCallId: String(message.toolCallId || ""), toolName: String(message.toolName || ""), content: contentBlocks(message.content, "toolResult"), isError: message.isError === true });
        break;
      case "custom":
        if (message.excludeFromContext !== true) conversation.push({ role: "customMessage", content: contentBlocks(message.content, "custom") });
        break;
      case "compactionSummary": conversation.push({ role: "compactionSummary", content: String(message.summary || "") }); break;
      case "branchSummary": conversation.push({ role: "branchSummary", content: String(message.summary || "") }); break;
      case "bashExecution":
        if (message.excludeFromContext !== true) conversation.push({ role: "bashExecution", command: String(message.command || ""), output: String(message.output || ""), exitCode: message.exitCode ?? null, cancelled: message.cancelled === true, truncated: message.truncated === true });
        break;
      default: break;
    }
  }
  return { conversation };
}

function imageKind(bytes) {
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "jpg";
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return "png";
  if (bytes.length >= 6 && (bytes.subarray(0, 6).toString("ascii") === "GIF87a" || bytes.subarray(0, 6).toString("ascii") === "GIF89a")) return "gif";
  if (bytes.length >= 12 && bytes.subarray(0, 4).toString("ascii") === "RIFF" && bytes.subarray(8, 12).toString("ascii") === "WEBP") return "webp";
  return undefined;
}

function decodeImageBase64(value, expected, label) {
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) fail("validation", `${label} is not canonical base64`);
  const bytes = Buffer.from(value, "base64");
  if (bytes.length > IMAGE_BYTES) fail("validation", `${label} exceeds ${IMAGE_BYTES} decoded bytes`);
  if (imageKind(bytes) !== expected) fail("validation", `${label} does not match its image format`);
  return bytes;
}

function resolveRequest(input, options = {}) {
  const request = validateRequest(input);
  const config = options.config === undefined ? deepClone(DEFAULT_CONFIG) : validateConfig(options.config);
  const alias = request.model || config.selected;
  if (!OWN(config.models, alias)) fail("config", `model profile is not configured: ${alias}`);
  const model = config.models[alias];
  const resolved = { questions: request.questions, model: alias };
  if (OWN(request, "state")) resolved.state = request.state;
  else if (request.state_asset.type === "text") resolved.state = request.state_asset.text;
  else if (request.state_asset.type === "file") {
    const sourceMaximum = Math.min(config.limits.max_input_bytes, MAX_INPUT_BYTES);
    const bytes = resolveUserFile(request.state_asset.path, options.cwd || process.cwd(), sourceMaximum, "state source");
    resolved.state = decodeUtf8(bytes, "state source");
  } else {
    const projected = Array.isArray(options.projection) ? options.projection :
      options.projection && Array.isArray(options.projection.messages) ? options.projection.messages : options.messages;
    resolved.state = projectContext(projected, {
      lastNTurns: request.state_asset.last_n_turns,
      includeSystem: request.state_asset.include_system,
      systemPrompt: options.systemPrompt,
      currentToolCallId: options.currentToolCallId,
    });
  }
  if (request.assets && Object.keys(request.assets).length) {
    if (!model.images) fail("capability", `model profile ${alias} does not support images`);
    let total = 0;
    resolved.assets = {};
    for (const [id, asset] of Object.entries(request.assets)) {
      let bytes, format;
      if (OWN(asset, "path")) {
        bytes = resolveUserFile(asset.path, options.cwd || process.cwd(), IMAGE_BYTES, `image asset ${id}`);
        format = imageKind(bytes);
        if (!format) fail("validation", `image asset ${id} has an unsupported image format`);
      } else {
        format = asset.format;
        bytes = decodeImageBase64(asset.data_base64, format, `image asset ${id}`);
      }
      total += bytes.length;
      if (total > TOTAL_IMAGE_BYTES) fail("validation", `image assets exceed ${TOTAL_IMAGE_BYTES} decoded bytes`);
      resolved.assets[id] = { type: "image", format, data_base64: bytes.toString("base64") };
    }
  }
  if (Object.values(resolved.questions).some(question => question.type === "box")) {
    if (!model.box) fail("capability", `model profile ${alias} does not support box questions`);
    if (model.adapter !== "typesafe-http") {
      fail("capability", `model profile ${alias} uses ${model.adapter}, which has no verified box schema; box questions require a box-capable typesafe-http profile`);
    }
  }
  const bytes = Buffer.byteLength(JSON.stringify(resolved), "utf8");
  if (bytes > config.limits.max_input_bytes) {
    fail("bounds", `resolved request is ${bytes} bytes, exceeding max_input_bytes ${config.limits.max_input_bytes}; context is not truncated`);
  }
  return { ...resolved, modelConfig: deepClone(model), limits: deepClone(config.limits), inputBytes: bytes };
}

function classifierText(value) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function nativeRequest(resolved, modelConfig = resolved?.modelConfig) {
  if (!isObject(resolved) || !isObject(modelConfig)) fail("validation", "resolved request and model config are required");
  if (!OWN(resolved, "state") || !isObject(resolved.questions) || Object.keys(resolved.questions).length === 0) {
    fail("validation", "resolved request has an invalid shape");
  }
  const checkedModel = validateModel(modelConfig, typeof resolved.model === "string" ? resolved.model : "resolved");
  if (isObject(resolved.modelConfig)) {
    for (const key of ["adapter", "provider", "model", "endpoint"]) {
      if (resolved.modelConfig[key] !== checkedModel[key]) {
        fail("config", `resolved request model ${key} does not match the execution profile`);
      }
    }
  }
  modelConfig = checkedModel;
  if (modelConfig.adapter === "classifier") {
    if (resolved.assets && Object.keys(resolved.assets).length) {
      fail("capability", "native classifiers cannot preserve asset ID associations");
    }
    const questions = {};
    for (const [id, question] of Object.entries(resolved.questions || {})) {
      if (question.type === "box") fail("capability", "native classifiers do not support box questions");
      const type = question.type === "noul" ? "bool" : question.type;
      const native = { type, instructions: classifierText(question.instructions) };
      if (question.type === "choice") {
        native.criteria = {};
        for (const [label, description] of Object.entries(question.criteria)) native.criteria[label] = classifierText(description);
      } else if (question.type === "score") native.criteria = question.criteria.map(classifierText);
      else native.criteria = question.criteria ? { true: classifierText(question.criteria.true), false: classifierText(question.criteria.false) } : { true: "true", false: "false" };
      questions[id] = native;
    }
    return { state: deepClone(resolved.state), questions };
  }
  const output = { state: deepClone(resolved.state), questions: deepClone(resolved.questions || {}) };
  if (resolved.assets) output.assets = deepClone(resolved.assets);
  return output;
}

function finiteUnit(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) fail("output", `${label} must be a finite number from 0 to 1`);
  return value;
}

function scoreProbabilities(value, levels, label) {
  if (!Array.isArray(value) && !isObject(value)) fail("output", `${label} must be an array or numeric-keyed object`);
  const expectedKeys = Array.from({ length: levels }, (_, index) => String(index));
  const keys = Object.keys(value);
  if (keys.length !== levels || expectedKeys.some(key => !OWN(value, key))) {
    fail("output", `${label} must contain exactly indices 0 through ${levels - 1}`);
  }
  if (Array.isArray(value) && value.length !== levels) {
    fail("output", `${label} must contain exactly ${levels} entries`);
  }
  const probabilities = Array.isArray(value) ? [] : {};
  let sum = 0;
  let expectedScore = 0;
  for (let index = 0; index < levels; index++) {
    const probability = finiteUnit(value[index], `${label} index ${index}`);
    probabilities[index] = probability;
    sum += probability;
    expectedScore += index * probability;
  }
  if (Math.abs(sum - 1) > 1e-6) fail("output", `${label} must sum to 1`);
  return { probabilities, expectedScore };
}

function validateAnswers(request, answers, options = {}) {
  const source = request.questions ? request : validateRequest(request);
  if (!isObject(answers)) fail("output", "answers must be an object");
  const expected = Object.keys(source.questions);
  if (Object.keys(answers).length !== expected.length || expected.some(id => !OWN(answers, id))) fail("output", "answer keys must exactly match question keys");
  const result = {};
  for (const id of expected) {
    const question = source.questions[id];
    const answer = answers[id];
    if (!isObject(answer)) fail("output", `answer ${id} must be an object`);
    if (question.type === "choice") {
      exactKeys(answer, new Set(["type", "choice", "probabilities", "confidence"]), `answer ${id}`);
      if (answer.type !== "choice" || !OWN(question.criteria, answer.choice)) fail("output", `answer ${id} has an invalid choice`);
      if (!isObject(answer.probabilities)) fail("output", `answer ${id} probabilities must be an object`);
      const labels = Object.keys(question.criteria);
      if (Object.keys(answer.probabilities).length !== labels.length || labels.some(label => !OWN(answer.probabilities, label))) fail("output", `answer ${id} probabilities must exactly match choice labels`);
      let sum = 0; const probabilities = {};
      for (const label of labels) { probabilities[label] = finiteUnit(answer.probabilities[label], `answer ${id} probability`); sum += probabilities[label]; }
      if (Math.abs(sum - 1) > 1e-6) fail("output", `answer ${id} probabilities must sum to 1`);
      result[id] = { type: "choice", choice: answer.choice, probabilities, confidence: finiteUnit(answer.confidence, `answer ${id} confidence`) };
    } else if (question.type === "score") {
      exactKeys(answer, new Set(["type", "score", "confidence", "probabilities", "legend"]), `answer ${id}`);
      if (answer.type !== "score" || typeof answer.score !== "number" || !Number.isFinite(answer.score) || answer.score < 0 || answer.score > question.criteria.length - 1) fail("output", `answer ${id} score is out of range`);
      result[id] = { type: "score", score: answer.score, confidence: finiteUnit(answer.confidence, `answer ${id} confidence`) };
      if (OWN(answer, "probabilities")) {
        const checked = scoreProbabilities(answer.probabilities, question.criteria.length, `answer ${id} probabilities`);
        // Native providers commonly round reported scores to two decimal places.
        if (Math.abs(answer.score - checked.expectedScore) > SCORE_EXPECTED_TOLERANCE + Number.EPSILON) {
          fail("output", `answer ${id} score must match the probability-weighted expected score within ${SCORE_EXPECTED_TOLERANCE}`);
        }
        result[id].probabilities = checked.probabilities;
      }
      if (OWN(answer, "legend")) result[id].legend = jsonCopy(answer.legend, `answer ${id} legend`);
    } else if (question.type === "noul") {
      exactKeys(answer, new Set(["type", "noul"]), `answer ${id}`);
      if (answer.type !== "noul") fail("output", `answer ${id} must be a noul answer`);
      result[id] = { type: "noul", noul: finiteUnit(answer.noul, `answer ${id} noul`) };
    } else {
      if (!options.model?.box && !options.box) fail("capability", "box answer validation requires a box-capable configured bridge");
      exactKeys(answer, new Set(["type", "box"]), `answer ${id}`);
      if (answer.type !== "box" || !OWN(answer, "box")) fail("output", `answer ${id} must be a box answer`);
      result[id] = { type: "box", box: jsonCopy(answer.box, `answer ${id} box`) };
    }
  }
  const limit = options.maxOutputBytes || source.limits?.max_output_bytes || MAX_OUTPUT_BYTES;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_OUTPUT_BYTES) fail("output", "maxOutputBytes is invalid");
  if (Buffer.byteLength(JSON.stringify(result), "utf8") > limit) fail("output", `answers exceed ${limit} bytes`);
  return result;
}

module.exports = {
  DEFAULT_CONFIG,
  DecisionError,
  deepClone,
  loadConfig,
  nativeRequest,
  projectContext,
  resolveConfigPath,
  resolveRequest,
  updateConfig,
  validateAnswers,
  validateConfig,
  validateRequest,
  withoutCurrentToolBatch,
};
