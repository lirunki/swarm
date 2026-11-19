"use strict";

const crypto = require("node:crypto");
const { createRequire } = require("node:module");
const { Type } = require("typebox");

const nativeRequire = createRequire(__filename);
let core;
let backends;

function loadDependencies() {
  if (typeof nativeRequire.resolve === "function" && nativeRequire.cache) {
    const coreFilename = nativeRequire.resolve("./decision-core.js");
    const backendsFilename = nativeRequire.resolve("./decision-backends.js");
    delete nativeRequire.cache[coreFilename];
    delete nativeRequire.cache[backendsFilename];
    core = nativeRequire(coreFilename);
    backends = nativeRequire(backendsFilename);
    return;
  }
  // Dependency-injection/VM loaders may intentionally provide only a callable require.
  core = require("./decision-core.js");
  backends = require("./decision-backends.js");
}

const INSTALLED = Symbol.for("swarm.decision.installed");
const COMMANDS = ["status", "list", "models", "show", "set", "add", "remove", "reload"];
const OWN = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

const parameters = Type.Object({
  state: Type.Optional(Type.Any({ description: "Inline JSON state to evaluate. Use exactly one of state or state_asset." })),
  state_asset: Type.Optional(Type.Any({ description: "A text, file, or current-conversation context source." })),
  model: Type.Optional(Type.String({ description: "Optional configured decision model alias." })),
  questions: Type.Record(Type.String(), Type.Any(), {
    minProperties: 1,
    maxProperties: 8,
    description: "Map of 1-8 question IDs to choice, score, noul, or box question contracts.",
  }),
  assets: Type.Optional(Type.Record(Type.String(), Type.Any(), {
    maxProperties: 4,
    description: "Optional map of image asset IDs to explicit image assets.",
  })),
}, { additionalProperties: false });

const outputSchema = Type.Object({
  answers: Type.Record(Type.String(), Type.Any()),
  metadata: Type.Any(),
}, { additionalProperties: false });

function sessionKey(ctx) {
  try { return String(ctx?.sessionManager?.getSessionId?.() || ""); }
  catch (_) { return ""; }
}

function safeClone(value) {
  try { return core.deepClone(value); }
  catch (_) { return undefined; }
}

function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function textBlocks(content, textTypes = new Set(["text", "input_text", "output_text"])) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return [];
  const blocks = [];
  for (const block of content) {
    if (!record(block)) continue;
    if (textTypes.has(block.type) && typeof block.text === "string") blocks.push({ type: "text", text: block.text });
    else if (block.type === "image" || block.type === "input_image" || block.type === "image_url") blocks.push({ type: "image" });
  }
  return blocks;
}

function wireArguments(value) {
  if (typeof value !== "string") return safeClone(value) ?? {};
  try { return JSON.parse(value); }
  catch (_) { return value; }
}

function systemText(value) {
  if (Array.isArray(value)) {
    return value.map(block => record(block) && typeof block.text === "string" && (block.type === undefined || block.type === "text") ? block.text : "").filter(Boolean).join("\n");
  }
  const content = textBlocks(value);
  if (typeof content === "string") return content;
  return content.map(block => block.text).filter(text => typeof text === "string").join("\n");
}

function normalizeMessages(messages) {
  if (!Array.isArray(messages)) return undefined;
  const normalized = [];
  for (const message of messages) {
    if (!record(message)) continue;
    const role = message.role === "developer" ? "system" : message.role;
    if (role === "system" || role === "user") {
      const content = textBlocks(message.content);
      if ((typeof content === "string" && content) || (Array.isArray(content) && content.length)) normalized.push({ role, content });
      if (role === "user" && Array.isArray(message.content)) {
        for (const block of message.content) {
          if (!record(block) || block.type !== "tool_result") continue;
          normalized.push({
            role: "toolResult",
            toolCallId: String(block.tool_use_id || ""),
            toolName: "",
            content: textBlocks(block.content),
            isError: block.is_error === true,
          });
        }
      }
      continue;
    }
    if (role === "assistant") {
      const content = textBlocks(message.content);
      const blocks = typeof content === "string" ? (content ? [{ type: "text", text: content }] : []) : content;
      if (Array.isArray(message.content)) {
        for (const block of message.content) {
          if (record(block) && (block.type === "tool_use" || block.type === "toolCall") && typeof block.name === "string") {
            blocks.push({ type: "toolCall", id: String(block.id || ""), name: block.name, arguments: safeClone(block.input ?? block.arguments) ?? {} });
          }
        }
      }
      if (Array.isArray(message.tool_calls)) {
        for (const call of message.tool_calls) {
          const fn = call?.function || call?.custom;
          if (record(fn) && typeof fn.name === "string") blocks.push({ type: "toolCall", id: String(call.id || ""), name: fn.name, arguments: wireArguments(fn.arguments ?? fn.input) });
        }
      }
      if (blocks.length) normalized.push({ role: "assistant", content: blocks });
      continue;
    }
    if (role === "tool" || role === "toolResult") {
      normalized.push({
        role: "toolResult",
        toolCallId: String(message.tool_call_id || message.toolCallId || ""),
        toolName: String(message.name || message.toolName || ""),
        content: textBlocks(message.content),
        isError: message.isError === true,
      });
    }
  }
  return normalized;
}

function normalizeResponses(input) {
  if (typeof input === "string") return [{ role: "user", content: input }];
  if (!Array.isArray(input)) return undefined;
  const messages = [], assistantTools = [];
  const flushTools = () => {
    if (assistantTools.length) messages.push({ role: "assistant", content: assistantTools.splice(0) });
  };
  for (const item of input) {
    if (!record(item)) continue;
    if (item.type === "function_call" || item.type === "custom_tool_call") {
      assistantTools.push({ type: "toolCall", id: String(item.call_id || item.id || ""), name: String(item.name || ""), arguments: wireArguments(item.arguments ?? item.input) });
    } else {
      flushTools();
      if (item.type === "function_call_output" || item.type === "custom_tool_call_output") {
        messages.push({ role: "toolResult", toolCallId: String(item.call_id || ""), toolName: "", content: textBlocks(item.output) });
      } else if (typeof item.role === "string") {
        const converted = normalizeMessages([item]);
        if (converted) messages.push(...converted);
      }
    }
  }
  flushTools();
  return messages;
}

function normalizeGoogle(payload) {
  if (!Array.isArray(payload?.contents)) return undefined;
  const messages = [];
  for (const item of payload.contents) {
    if (!record(item) || !Array.isArray(item.parts)) continue;
    const role = item.role === "model" ? "assistant" : "user";
    const content = [];
    for (const part of item.parts) {
      if (!record(part) || part.thought === true) continue;
      if (typeof part.text === "string") content.push({ type: "text", text: part.text });
      else if (role === "assistant" && record(part.functionCall)) content.push({ type: "toolCall", id: String(part.functionCall.id || ""), name: String(part.functionCall.name || ""), arguments: safeClone(part.functionCall.args) ?? {} });
      else if (record(part.functionResponse)) messages.push({ role: "toolResult", toolCallId: String(part.functionResponse.id || ""), toolName: String(part.functionResponse.name || ""), content: JSON.stringify(part.functionResponse.response ?? null) });
    }
    if (content.length) messages.push({ role, content });
  }
  return messages;
}

function finalPayloadContext(payload) {
  if (!record(payload)) return undefined;
  let messages, systemPrompt;
  if (record(payload.context) && Array.isArray(payload.context.messages)) {
    messages = normalizeMessages(payload.context.messages);
    if (typeof payload.context.systemPrompt === "string") systemPrompt = payload.context.systemPrompt;
  } else if (Array.isArray(payload.messages)) {
    messages = normalizeMessages(payload.messages);
  } else if (payload.input !== undefined) {
    messages = normalizeResponses(payload.input);
  } else {
    messages = normalizeGoogle(payload);
  }
  if (!Array.isArray(messages)) return undefined;
  if (systemPrompt === undefined && payload.system !== undefined) systemPrompt = systemText(payload.system);
  if (systemPrompt === undefined && typeof payload.instructions === "string") systemPrompt = payload.instructions;
  if (systemPrompt === undefined && typeof payload.config?.systemInstruction === "string") systemPrompt = payload.config.systemInstruction;
  if (systemPrompt === undefined && record(payload.systemInstruction)) systemPrompt = systemText(payload.systemInstruction.parts);
  const leadingSystem = messages.filter(message => message.role === "system").map(message => systemText(message.content)).filter(Boolean).join("\n");
  if (systemPrompt === undefined && leadingSystem) systemPrompt = leadingSystem;
  return { messages, systemPrompt };
}

function contextSource(input) {
  if (OWN(input, "state")) return "inline";
  return input?.state_asset?.type === "context" ? "context" : input?.state_asset?.type || "asset";
}

function descriptionHash(resolved) {
  return crypto.createHash("sha256").update(JSON.stringify({ state: resolved.state, questions: resolved.questions })).digest("hex");
}

function safeCategory(error) {
  return typeof error?.category === "string" && /^[a-z][a-z0-9_-]{0,31}$/.test(error.category) ? error.category : "internal";
}

function errorText(category, requestId) {
  const descriptions = {
    validation: "The decision request is invalid.",
    config: "Decision configuration is unavailable or invalid. After an extension update, run Pi /reload (not /decision reload), then retry.",
    lock: "Decision configuration is busy.",
    source: "A decision input source is unavailable or invalid.",
    bounds: "The decision request or response exceeds configured limits.",
    capability: "The selected decision model does not support this request.",
    notavailable: "The selected decision model is unavailable.",
    auth: "Authentication for the selected decision model is unavailable.",
    provider: "The decision provider failed.",
    output: "The decision provider returned invalid output.",
    timeout: "The decision request timed out.",
    aborted: "The decision request was aborted.",
    internal: "The decision request failed.",
  };
  return `${descriptions[category] || descriptions.internal} Request ID: ${requestId}`;
}

function splitWords(text) {
  const words = [];
  let value = "", quote = "", escaped = false;
  for (const character of String(text || "").trim()) {
    if (quote === "'") {
      if (character === "'") quote = "";
      else value += character;
      continue;
    }
    if (escaped) { value += character; escaped = false; continue; }
    if (character === "\\") { escaped = true; continue; }
    if (quote) {
      if (character === quote) quote = "";
      else value += character;
      continue;
    }
    if (character === '"' || character === "'") { quote = character; continue; }
    if (/\s/.test(character)) {
      if (value) { words.push(value); value = ""; }
    } else value += character;
  }
  if (escaped) value += "\\";
  if (quote) throw new Error("Unclosed quote");
  if (value) words.push(value);
  return words;
}

function chatModels(registry, availableOnly = false) {
  let models = [];
  if (availableOnly && typeof registry?.getAvailable === "function") models = registry.getAvailable() || [];
  else if (typeof registry?.getModelsOfType === "function") models = registry.getModelsOfType("chat") || [];
  else if (typeof registry?.getAll === "function") models = registry.getAll() || [];
  return models.filter(model => !model?.type || model.type === "chat");
}

function findChat(registry, provider, modelId, availableOnly = false) {
  const list = chatModels(registry, availableOnly);
  return list.find(model => model?.provider === provider && model?.id === modelId) ||
    (!availableOnly && typeof registry?.find === "function" ? registry.find(provider, modelId) : undefined);
}

function profileAvailable(profile, registry) {
  if (profile.adapter === "typesafe-http") return "configured HTTP bridge";
  const model = profile.adapter === "classifier"
    ? registry?.getModelOfType?.("classifier", profile.provider, profile.model)
    : findChat(registry, profile.provider, profile.model, true);
  return model ? "available" : "not currently available";
}

function modelLabel(model) {
  const id = `${model.provider}/${model.id}`;
  const name = typeof model.name === "string" && model.name !== model.id ? ` — ${model.name}` : "";
  return `${id}${name}`;
}

function safeAlias(provider, model) {
  const raw = `${provider}.${model}`.replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 128);
  return /^[A-Za-z0-9]/.test(raw) ? raw : `model-${crypto.createHash("sha256").update(`${provider}/${model}`).digest("hex").slice(0, 12)}`;
}

function notifyError(ctx, error) {
  const category = safeCategory(error);
  ctx.ui.notify(errorText(category, crypto.randomUUID()), "error");
}

function extension(pi) {
  if (pi[INSTALLED]) return;
  Object.defineProperty(pi, INSTALLED, { value: true, enumerable: false, configurable: false, writable: false });
  loadDependencies();

  const pendingContexts = new Map();
  const cachedContexts = new Map();

  pi.on("context_with_system", (event, ctx) => {
    if (!Array.isArray(event?.messages)) return undefined;
    const key = sessionKey(ctx);
    pendingContexts.clear();
    pendingContexts.set(key, event.messages);
    cachedContexts.delete(key);
    return undefined;
  });

  pi.on("before_provider_request", (event, ctx) => {
    const key = sessionKey(ctx);
    if (!pendingContexts.has(key)) return undefined;
    const snapshot = finalPayloadContext(event?.payload);
    pendingContexts.delete(key);
    cachedContexts.delete(key);
    if (snapshot) cachedContexts.set(key, snapshot);
    return undefined;
  });

  pi.registerTool({
    name: "decision",
    label: "Decision",
    description: "Evaluate 1-8 typed questions against inline state, a bounded file, or effective conversation context using an explicitly configured decision model.",
    promptSnippet: "Evaluate structured choice, score, noul, or box questions with a configured decision model",
    parameters,
    outputSchema,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    async execute(callID, params, signal, _onUpdate, ctx) {
      const requestId = crypto.randomUUID();
      try {
        const configPath = core.resolveConfigPath();
        const config = core.loadConfig(configPath);
        const cached = cachedContexts.get(sessionKey(ctx));
        const projection = cached ? { messages: safeClone(cached.messages) } : undefined;

        const resolved = core.resolveRequest(params, {
          config,
          cwd: ctx?.cwd || process.cwd(),
          projection,
          systemPrompt: cached?.systemPrompt,
          currentToolCallId: callID,
        });
        const result = await backends.runDecision(resolved.modelConfig, resolved, ctx, {
          signal,
          timeoutMs: resolved.limits.timeout_ms,
          maxOutputBytes: resolved.limits.max_output_bytes,
        });
        const metadata = {
          ...result.metadata,
          request_id: requestId,
          source: contextSource(params),
          description_hash: descriptionHash(resolved),
        };
        const structuredContent = { answers: result.answers, metadata };
        return {
          content: [{ type: "text", text: JSON.stringify(structuredContent) }],
          structuredContent,
          details: metadata,
          ...(result.usage ? { usage: result.usage } : {}),
        };
      } catch (error) {
        const category = safeCategory(error);
        return {
          content: [{ type: "text", text: errorText(category, requestId) }],
          details: { request_id: requestId, category },
          isError: true,
          ...(error?.usage ? { usage: error.usage } : {}),
        };
      }
    },
  });

  let aliases = [];
  pi.registerCommand("decision", {
    description: "Inspect or configure decision models (status|list|models|show|set|add|remove|reload)",
    getArgumentCompletions(prefix) {
      const word = String(prefix || "").trim();
      const values = word.includes(" ") ? aliases : COMMANDS;
      const current = word.split(/\s+/).pop() || "";
      const filtered = values.filter(value => value.startsWith(current));
      return filtered.length ? filtered.map(value => ({ value, label: value })) : null;
    },
    async handler(args, ctx) {
      try {
        const configPath = core.resolveConfigPath();
        let config = core.loadConfig(configPath);
        aliases = Object.keys(config.models);
        const words = splitWords(args);
        const action = (words.shift() || "status").toLowerCase();

        if (action === "status") {
          const profile = config.models[config.selected];
          ctx.ui.notify(
            `Decision model: ${config.selected} (${profile.adapter}; ${profile.provider || "typesafe-http"}/${profile.model || "configured"}; ${profileAvailable(profile, ctx.modelRegistry)})\n` +
            `Config: ${configPath}\nLimits: input ${config.limits.max_input_bytes} bytes, output ${config.limits.max_output_bytes} bytes, timeout ${config.limits.timeout_ms} ms`,
            "info",
          );
          return;
        }
        if (action === "list") {
          const lines = Object.entries(config.models).map(([name, profile]) =>
            `${name === config.selected ? "*" : " "} ${name}: ${profile.adapter} ${profile.provider || "typesafe-http"}/${profile.model || "configured"}`);
          ctx.ui.notify(lines.join("\n"), "info");
          return;
        }
        if (action === "models") {
          const models = chatModels(ctx.modelRegistry, true);
          ctx.ui.notify(models.length ? models.map(modelLabel).join("\n") : "No available chat models.", "info");
          return;
        }
        if (action === "show") {
          const name = words[0] || config.selected;
          if (!OWN(config.models, name)) throw new core.DecisionError("config", "Unknown decision model alias");
          ctx.ui.notify(`${name}${name === config.selected ? " (selected)" : ""}:\n${JSON.stringify(config.models[name], null, 2)}`, "info");
          return;
        }
        if (action === "reload") {
          config = core.loadConfig(configPath);
          aliases = Object.keys(config.models);
          ctx.ui.notify(`Decision configuration reloaded. Selected: ${config.selected}.`, "info");
          return;
        }
        if (action === "set") {
          let target = words.join(" ");
          if (!target) {
            if (!ctx.hasUI) throw new core.DecisionError("validation", "A model alias is required");
            const available = chatModels(ctx.modelRegistry, true);
            const choices = available.map(modelLabel);
            const selected = await ctx.ui.select("Select decision chat model", choices);
            if (!selected) return;
            const match = available[choices.indexOf(selected)];
            if (!match) return;
            target = `${match.provider}/${match.id}`;
          }
          config = core.updateConfig(configPath, current => {
            if (OWN(current.models, target)) { current.selected = target; return current; }
            const slash = target.indexOf("/");
            if (slash < 1 || slash === target.length - 1) throw new core.DecisionError("config", "Unknown decision model alias");
            const provider = target.slice(0, slash), modelId = target.slice(slash + 1);
            const model = findChat(ctx.modelRegistry, provider, modelId, true);
            if (!model) throw new core.DecisionError("notavailable", "Chat model is unavailable");
            const alias = safeAlias(provider, modelId);
            current.models[alias] = { adapter: "llm", provider, model: modelId, images: model.input?.includes?.("image") === true, box: false };
            current.selected = alias;
            return current;
          });
          aliases = Object.keys(config.models);
          ctx.ui.notify(`Selected decision model: ${config.selected}.`, "info");
          return;
        }
        if (action === "remove") {
          const name = words[0];
          if (!name || !OWN(config.models, name)) throw new core.DecisionError("config", "Unknown decision model alias");
          if (name === config.selected) throw new core.DecisionError("config", "The selected decision model cannot be removed");
          config = core.updateConfig(configPath, current => { delete current.models[name]; return current; });
          aliases = Object.keys(config.models);
          ctx.ui.notify(`Removed decision model: ${name}.`, "info");
          return;
        }
        if (action === "add") {
          const name = words.shift();
          if (!name) throw new core.DecisionError("validation", "A model alias is required");
          let profile;
          const remainder = words.join(" ");
          if (remainder.startsWith("{")) {
            try { profile = JSON.parse(remainder); }
            catch (_) { throw new core.DecisionError("validation", "The model profile JSON is malformed"); }
          } else if (words[0] === "typesafe-http") {
            const [, modelId, endpoint, authEnv, ...flags] = words;
            if (!modelId || !endpoint) throw new core.DecisionError("validation", "typesafe-http requires MODEL and ENDPOINT");
            profile = { adapter: "typesafe-http", model: modelId, endpoint, ...(authEnv && !authEnv.startsWith("--") ? { auth_env: authEnv } : {}), images: flags.includes("--images") || authEnv === "--images", box: flags.includes("--box") || authEnv === "--box" };
          } else if (words[0] === "classifier") {
            const spec = words[1] || "", slash = spec.indexOf("/");
            if (slash < 1 || slash === spec.length - 1) throw new core.DecisionError("validation", "classifier requires provider/model");
            profile = { adapter: "classifier", provider: spec.slice(0, slash), model: spec.slice(slash + 1), images: words.includes("--images"), box: false };
            if (!ctx.modelRegistry?.getModelOfType?.("classifier", profile.provider, profile.model)) throw new core.DecisionError("notavailable", "Classifier model is unavailable");
          } else {
            const spec = words[0] || "", slash = spec.indexOf("/");
            if (slash < 1 || slash === spec.length - 1) throw new core.DecisionError("validation", "add requires provider/model");
            const provider = spec.slice(0, slash), modelId = spec.slice(slash + 1);
            const model = findChat(ctx.modelRegistry, provider, modelId, false);
            if (!model) throw new core.DecisionError("notavailable", "Chat model is unavailable");
            profile = { adapter: "llm", provider, model: modelId, images: model.input?.includes?.("image") === true, box: false };
          }
          config = core.updateConfig(configPath, current => { current.models[name] = profile; return current; });
          aliases = Object.keys(config.models);
          ctx.ui.notify(`Added decision model: ${name}.`, "info");
          return;
        }
        ctx.ui.notify("Usage: /decision [status|list|models|show [NAME]|set [NAME|provider/model]|add NAME provider/model|add NAME 'JSON'|remove NAME|reload]", "warning");
      } catch (error) { notifyError(ctx, error); }
    },
  });
}

module.exports = extension;
module.exports.default = extension;
module.exports.extension = extension;
module.exports.INSTALLED = INSTALLED;
module.exports.splitWords = splitWords;
