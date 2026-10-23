"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { Type } = require("typebox");

const MAX_INBOX_BYTES = 2 * 1024 * 1024;
const MAX_RECEIPT_BYTES = 256 * 1024;
const MAX_COMMAND_LENGTH = 32000;
const POLL_MS = 500;
const LOST_MESSAGE_MS = 10000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const THINKING_LEVELS = new Set(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);
const FINAL_STATUSES = new Set(["delivered", "failed", "uncertain"]);

function isUUID(value) {
  return typeof value === "string" && UUID_RE.test(value);
}

/** Parse the deliberately small set of commands controllable by the mailbox. */
function validateCommand(value) {
  if (typeof value !== "string" || value.length === 0 || Array.from(value).length > MAX_COMMAND_LENGTH) {
    return { ok: false, error: `command must contain 1-${MAX_COMMAND_LENGTH} characters` };
  }
  if (/[\x00-\x1f\x7f\u2028\u2029]/.test(value)) {
    return { ok: false, error: "command must be a single line" };
  }
  const match = value.match(/^\/(compact|model|thinking|name|reload)(?:\s+(.*))?$/);
  if (!match) {
    return { ok: false, error: "command is not permitted by the mailbox" };
  }
  const name = match[1];
  const args = (match[2] || "").trim();
  if (name === "model") {
    const model = args.match(/^([^\s/]+)\/(\S+)$/);
    if (!model) return { ok: false, error: "usage: /model provider/model" };
    return { ok: true, name, args, provider: model[1], modelId: model[2], command: value };
  }
  if (name === "thinking") {
    if (!THINKING_LEVELS.has(args)) {
      return { ok: false, error: "usage: /thinking off|minimal|low|medium|high|xhigh|max" };
    }
    return { ok: true, name, args, level: args, command: value };
  }
  if (name === "name" && (!args || Array.from(args).length > 120)) return { ok: false, error: "usage: /name session name (at most 120 characters)" };
  if (name === "reload" && args) return { ok: false, error: "usage: /reload" };
  return { ok: true, name, args, command: value };
}

function readJsonBounded(filename, limit) {
  let fd;
  try {
    fd = fs.openSync(filename, "r");
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > limit) throw new Error(`file exceeds ${limit} byte limit`);
    const buffer = Buffer.alloc(limit + 1);
    let length = 0;
    while (length <= limit) {
      const count = fs.readSync(fd, buffer, length, buffer.length - length, null);
      if (count === 0) break;
      length += count;
    }
    if (length > limit) throw new Error(`file exceeds ${limit} byte limit`);
    return JSON.parse(buffer.toString("utf8", 0, length));
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function fsyncDirectory(directory) {
  let fd;
  try {
    fd = fs.openSync(directory, "r");
    fs.fsyncSync(fd);
  } catch (_) {
    // Some platforms do not permit opening/fsyncing directories. The file itself is synced.
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function atomicJson(filename, value, durable = true) {
  const directory = path.dirname(filename);
  fs.mkdirSync(directory, { recursive: true });
  const temporary = path.join(directory, `${randomUUID()}.tmp`);
  let fd;
  try {
    fd = fs.openSync(temporary, "wx", 0o600);
    fs.writeFileSync(fd, `${JSON.stringify(value)}\n`, "utf8");
    if (durable) fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temporary, filename);
    if (durable) fsyncDirectory(directory);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    try { fs.unlinkSync(temporary); } catch (_) { /* rename or cleanup already won */ }
  }
}

function getConfig(env) {
  const directory = env.SWARM_CONTROL_DIR;
  const epoch = env.SWARM_CONTROL_EPOCH;
  const agentId = env.SWARM_AGENT_ID;
  if (typeof directory !== "string" || !directory) throw new Error("SWARM_CONTROL_DIR is required");
  if (!isUUID(epoch)) throw new Error("SWARM_CONTROL_EPOCH must be a UUID");
  if (!isUUID(agentId)) throw new Error("SWARM_AGENT_ID must be a UUID");
  return { directory, epoch, agentId };
}

function mailboxExtension(pi) {
  let runtime = null;

  function receiptPath(state, id) {
    return path.join(state.config.directory, "acks", `${id}.json`);
  }

  function readReceipt(state, id) {
    try {
      const receipt = readJsonBounded(receiptPath(state, id), MAX_RECEIPT_BYTES);
      if (receipt && receipt.id === id && receipt.epoch === state.config.epoch &&
          receipt.session_id === state.sessionId && typeof receipt.status === "string") return receipt;
    } catch (_) { /* A missing, partial, or foreign receipt is not authoritative. */ }
    return null;
  }

  function writeReceipt(state, id, status, extra = {}) {
    atomicJson(receiptPath(state, id), {
      id,
      epoch: state.config.epoch,
      session_id: state.sessionId,
      status,
      ...extra,
      instance: state.instance,
    });
  }

  function commandEntry(id, state, command, error) {
    const data = { message_id: id, state, command };
    if (error) data.error = String(error).slice(0, 2000);
    pi.appendEntry("swarm.command", data);
  }

  function durableMessageIds(ctx) {
    const ids = new Set();
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry && entry.type === "custom_message" && entry.customType === "swarm.message" &&
          entry.details && isUUID(entry.details.message_id)) ids.add(entry.details.message_id);
    }
    return ids;
  }

  function commandStates(ctx) {
    const states = new Map();
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry && entry.type === "custom" && entry.customType === "swarm.command" &&
          entry.data && isUUID(entry.data.message_id)) states.set(entry.data.message_id, entry.data);
    }
    return states;
  }

  function envelope(item) {
    const sender = item.sender_name
      ? `${item.sender_name}${item.sender_id ? ` (${item.sender_id})` : ""}`
      : (item.sender_id || "Swarm controller");
    return `[Swarm mailbox message]\nStable message ID: ${item.id}\nSender: ${sender}\n\n${item.text}\n\n` +
      `Treat ${item.id} as a stable delivery ID. This mailbox can redeliver after interruption; ` +
      "check the conversation and external state before acting, and do not duplicate external actions.";
  }

  function validInbox(state, inbox) {
    return inbox && inbox.epoch === state.config.epoch && inbox.agent_id === state.config.agentId &&
      inbox.session_id === state.sessionId && inbox.awake === true && Array.isArray(inbox.items);
  }

  function validItem(item) {
    return item && isUUID(item.id) && (item.kind === "message" || item.kind === "command") &&
      typeof item.text === "string" &&
      ((item.kind === "message" && item.status === "delivering") ||
       (item.kind === "command" && ["delivering", "executing"].includes(item.status))) &&
      (item.sender_id == null || isUUID(item.sender_id)) &&
      (item.sender_name === undefined || typeof item.sender_name === "string");
  }

  function validLifecycleRequest(state, request) {
    return request && isUUID(request.id) && isUUID(request.agent_id) && isUUID(request.epoch) &&
      isUUID(request.session_id) && request.agent_id === state.config.agentId &&
      request.epoch === state.config.epoch && request.session_id === state.sessionId &&
      request.action === "shutdown" && (request.phase === "prepare" || request.phase === "commit") &&
      typeof request.requested_at === "string" && !Number.isNaN(Date.parse(request.requested_at));
  }

  function readLifecycleRequest(state) {
    try {
      const request = readJsonBounded(path.join(state.config.directory, "lifecycle.json"), MAX_RECEIPT_BYTES);
      return validLifecycleRequest(state, request) ? request : null;
    } catch (_) {
      return null;
    }
  }

  function readLifecycleAck(state) {
    try {
      const ack = readJsonBounded(path.join(state.config.directory, "lifecycle-ack.json"), MAX_RECEIPT_BYTES);
      if (ack && isUUID(ack.id) && ack.agent_id === state.config.agentId &&
          ack.epoch === state.config.epoch && ack.session_id === state.sessionId &&
          ["waiting_for_idle", "ready", "shutdown_requested", "failed"].includes(ack.state)) return ack;
    } catch (_) { /* A missing, partial, or foreign acknowledgement is not authoritative. */ }
    return null;
  }

  function writeLifecycleAck(state, request, ackState, error) {
    const existing = readLifecycleAck(state);
    const normalizedError = error ? String(error).slice(0, 2000) : undefined;
    if (existing && existing.id === request.id && existing.state === ackState &&
        existing.error === normalizedError) return existing;
    const ack = {
      id: request.id,
      agent_id: state.config.agentId,
      epoch: state.config.epoch,
      session_id: state.sessionId,
      state: ackState,
      instance: state.instance,
      updated_at: new Date().toISOString(),
    };
    if (normalizedError) ack.error = normalizedError;
    atomicJson(path.join(state.config.directory, "lifecycle-ack.json"), ack);
    return ack;
  }

  function lifecycleIdle(state, ctx) {
    return ctx.isIdle() && !ctx.hasPendingMessages() && !state.commandBusy && state.pendingReloads.size === 0;
  }

  async function compact(ctx, instructions) {
    await new Promise((resolve, reject) => {
      ctx.compact({ customInstructions: instructions || undefined, onComplete: resolve, onError: reject });
    });
  }

  async function executeCommand(state, item, parsed, ctx) {
    if (parsed.name === "compact") {
      await compact(ctx, parsed.args);
    } else if (parsed.name === "model") {
      const model = ctx.modelRegistry.find(parsed.provider, parsed.modelId);
      if (!model) throw new Error(`model not found: ${parsed.provider}/${parsed.modelId}`);
      if (!(await pi.setModel(model))) throw new Error(`model authentication unavailable: ${parsed.provider}/${parsed.modelId}`);
    } else if (parsed.name === "thinking") {
      pi.setThinkingLevel(parsed.level);
    } else if (parsed.name === "name") {
      pi.setSessionName(parsed.args.trim());
    } else if (parsed.name === "reload") {
      state.pendingReloads.set(item.id, parsed.command);
      pi.sendUserMessage(`/swarm-control-reload ${item.id}`, {
        deliverAs: "followUp",
        expandPromptTemplates: true,
      });
      return "reload-pending";
    }
    return "done";
  }

  async function handleCommand(state, item, ctx, states) {
    const existing = readReceipt(state, item.id);
    if (existing && FINAL_STATUSES.has(existing.status)) return;
    if (existing && existing.status === "executing") {
      const prior = states.get(item.id);
      if (existing.command === "/reload" && prior && prior.state === "reload_started" &&
          state.startReason === 'reload' && existing.instance !== state.instance) {
        writeReceipt(state, item.id, "delivered", { command: existing.command });
        commandEntry(item.id, "delivered", existing.command);
      } else if (existing.instance !== state.instance) {
        writeReceipt(state, item.id, "uncertain", {
          command: existing.command || item.text,
          error: "command was executing in a previous extension instance; it was not replayed",
        });
        commandEntry(item.id, "uncertain", existing.command || item.text,
          "previous instance ended while command execution was in progress");
      }
      return;
    }
    const historical = states.get(item.id);
    if (historical) {
      const conflict = historical.command !== item.text;
      const status = conflict ? 'failed' : FINAL_STATUSES.has(historical.state) ? historical.state : 'uncertain';
      writeReceipt(state, item.id, status, {command: item.text,
        error: conflict ? 'Command ID was already used for different text' : status === 'uncertain' ? 'Command execution was previously started; not replayed' : undefined});
      return;
    }
    if (!ctx.isIdle() || ctx.hasPendingMessages()) return;

    const parsed = validateCommand(item.text);
    if (!parsed.ok) {
      writeReceipt(state, item.id, "failed", { command: item.text, error: parsed.error });
      commandEntry(item.id, "failed", item.text, parsed.error);
      return;
    }

    writeReceipt(state, item.id, "executing", { command: parsed.command });
    commandEntry(item.id, "executing", parsed.command);
    state.commandBusy = true;
    try {
      const result = await executeCommand(state, item, parsed, ctx);
      if (result === "reload-pending" || runtime !== state || state.stopped) return;
      writeReceipt(state, item.id, "delivered", { command: parsed.command });
      commandEntry(item.id, "delivered", parsed.command);
    } catch (error) {
      if (runtime !== state || state.stopped) return;
      const message = error instanceof Error ? error.message : String(error);
      writeReceipt(state, item.id, "failed", { command: parsed.command, error: message.slice(0, 2000) });
      commandEntry(item.id, "failed", parsed.command, message);
    } finally {
      state.commandBusy = false;
    }
  }

  function handleMessage(state, item, ctx, durableIds, now) {
    const existing = readReceipt(state, item.id);
    if (existing && FINAL_STATUSES.has(existing.status)) return;
    if (durableIds.has(item.id)) {
      state.queuedMessages.delete(item.id);
      writeReceipt(state, item.id, "delivered");
      return;
    }
    const queuedAt = state.queuedMessages.get(item.id);
    if (queuedAt !== undefined) {
      if (now - queuedAt >= LOST_MESSAGE_MS && ctx.isIdle() && !ctx.hasPendingMessages()) {
        state.queuedMessages.delete(item.id);
        writeReceipt(state, item.id, "failed", {
          error: "queued message was not found in durable session storage after 10 seconds",
        });
      }
      return;
    }
    pi.sendMessage({
      customType: "swarm.message",
      content: envelope(item),
      display: true,
      details: { message_id: item.id },
    }, { triggerTurn: true, deliverAs: "steer" });
    state.queuedMessages.set(item.id, now);
  }

  function heartbeat(state, ctx) {
    if (runtime !== state || state.stopped) return;
    atomicJson(path.join(state.config.directory, "heartbeat.json"), {
      epoch: state.config.epoch,
      session_id: state.sessionId,
      updated_at: new Date().toISOString(),
      instance: state.instance,
      protocol: 1,
      capabilities: { graceful_shutdown: typeof ctx.shutdown === 'function' },
      idle: lifecycleIdle(state, ctx),
    }, false);
  }

  function pollLifecycle(state, ctx) {
    if (runtime !== state || state.stopped) return;
    const request = readLifecycleRequest(state);
    if (!request) return;

    const prior = readLifecycleAck(state);
    if (prior && prior.id === request.id && prior.state === "shutdown_requested") return;
    if (state.shutdownJobs.has(request.id)) return;

    if (!lifecycleIdle(state, ctx)) {
      writeLifecycleAck(state, request, "waiting_for_idle");
      return;
    }
    if (request.phase === "prepare") {
      writeLifecycleAck(state, request, "ready");
      return;
    }

    // Re-read the privileged request at the last possible point. A deleted prepare is a cancellation;
    // after commit the controller does not cancel the request.
    const latest = readLifecycleRequest(state);
    if (!latest || latest.id !== request.id || latest.phase !== "commit") return;

    state.shutdownJobs.add(request.id);
    if (typeof ctx.shutdown !== "function") {
      writeLifecycleAck(state, request, "failed", "Pi graceful shutdown API is unavailable");
      return;
    }
    writeLifecycleAck(state, request, "shutdown_requested");
    try {
      ctx.shutdown();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      writeLifecycleAck(state, request, "failed", message);
    }
  }

  async function poll(state, ctx) {
    if (runtime !== state || state.stopped) return;
    heartbeat(state, ctx);
    // A lifecycle request is privileged control state, not an inbox message. While it is present,
    // do not start ordinary delivery even if the backend has not paused the inbox yet.
    if (readLifecycleRequest(state)) return;
    let inbox;
    try {
      inbox = readJsonBounded(path.join(state.config.directory, "inbox.json"), MAX_INBOX_BYTES);
    } catch (_) {
      return;
    }
    if (!validInbox(state, inbox) || runtime !== state || state.stopped || inbox.items.length === 0) return;

    const durableIds = durableMessageIds(ctx);
    const states = commandStates(ctx);
    const now = Date.now();
    for (const item of inbox.items) {
      if (runtime !== state || state.stopped) return;
      if (!validItem(item)) continue;
      if (item.kind === "message") handleMessage(state, item, ctx, durableIds, now);
      else await handleCommand(state, item, ctx, states);
    }
  }

  function requestLifecyclePoll(state, ctx) {
    if (runtime !== state || state.stopped || state.lifecycleInFlight) return state.lifecycleInFlight;
    state.lifecycleInFlight = Promise.resolve().then(() => pollLifecycle(state, ctx)).catch(() => {}).finally(() => {
      state.lifecycleInFlight = null;
    });
    return state.lifecycleInFlight;
  }

  function requestPoll(state, ctx) {
    if (runtime !== state || state.stopped || state.inFlight) return state.inFlight;
    state.inFlight = Promise.resolve().then(() => poll(state, ctx)).catch(() => {}).finally(() => {
      state.inFlight = null;
    });
    return state.inFlight;
  }

  pi.registerCommand("swarm-control-reload", {
    description: "Internal command used by the Swarm mailbox to safely reload this runtime",
    handler: async (args, ctx) => {
      const state = runtime;
      const id = args.trim();
      if (!state || !isUUID(id) || !state.pendingReloads.has(id)) return;
      if (!ctx.isIdle()) return;
      const command = state.pendingReloads.get(id);
      const receipt = readReceipt(state, id);
      if (!receipt || receipt.status !== "executing" || receipt.command !== "/reload") return;
      commandEntry(id, "reload_started", command);
      try {
        await ctx.reload();
        return; // The old context and extension state are invalid after successful reload.
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        state.pendingReloads.delete(id);
        writeReceipt(state, id, "failed", { command, error: message.slice(0, 2000) });
        commandEntry(id, "failed", command, message);
      }
    },
  });

  pi.registerTool({
    name: "swarm_session_command",
    label: "Queue Swarm Session Command",
    description: "Queue a permitted session command through the Swarm controller. Finish the current turn after queuing it.",
    parameters: Type.Object({ command: Type.String({ maxLength: MAX_COMMAND_LENGTH }) }),
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
    async execute(_toolCallId, params) {
      const parsed = validateCommand(params.command);
      if (!parsed.ok) throw new Error(parsed.error);
      const config = getConfig(process.env);
      const requests = path.join(config.directory, 'requests');
      fs.mkdirSync(requests, {recursive:true});
      const waiting = fs.readdirSync(requests).filter(name => /^[0-9a-f-]{36}\.json$/i.test(name));
      const size = waiting.reduce((n, name) => n + fs.lstatSync(path.join(requests, name)).size, 0);
      if (waiting.length >= 128 || size + Buffer.byteLength(params.command, 'utf8') + 1024 > MAX_INBOX_BYTES) {
        throw new Error('Pending self-command requests are full; wait for the controller or remove old requests');
      }
      const id = randomUUID();
      atomicJson(path.join(requests, `${id}.json`), {
        id,
        kind: "command",
        text: params.command,
        created_at: new Date().toISOString(),
        sender_id: config.agentId,
      });
      return {
        content: [{ type: "text", text: `Queued Swarm session command ${id}. Finish the current turn; do not invoke the command directly.` }],
        details: { request_id: id },
      };
    },
  });

  pi.on("session_start", (event, ctx) => {
    if (runtime && runtime.timer) clearInterval(runtime.timer);
    if (runtime) runtime.stopped = true;
    const config = getConfig(process.env);
    const state = {
      config,
      sessionId: ctx.sessionManager.getSessionId(),
      instance: randomUUID(),
      startReason: event.reason,
      stopped: false,
      timer: null,
      inFlight: null,
      lifecycleInFlight: null,
      commandBusy: false,
      queuedMessages: new Map(),
      pendingReloads: new Map(),
      shutdownJobs: new Set(),
    };
    runtime = state;
    state.timer = setInterval(() => {
      try { heartbeat(state, ctx); } catch { /* Retry on next tick. */ }
      requestLifecyclePoll(state, ctx);
      requestPoll(state, ctx);
    }, POLL_MS);
    if (state.timer && typeof state.timer.unref === "function") state.timer.unref();
    requestLifecyclePoll(state, ctx);
    requestPoll(state, ctx);
  });

  pi.on("message_end", (_event, ctx) => {
    // Never await a poll from a lifecycle event it may itself be waiting on.
    if (runtime) void requestPoll(runtime, ctx);
  });

  pi.on("session_shutdown", async () => {
    const state = runtime;
    if (!state) return;
    state.stopped = true;
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    // A running compaction/reload may itself trigger shutdown; never deadlock on it.
    if (state.inFlight) await Promise.race([state.inFlight, new Promise(resolve => setTimeout(resolve, 1000))]);
    if (runtime === state) runtime = null;
  });
}

module.exports = mailboxExtension;
module.exports.default = mailboxExtension;
module.exports.validateCommand = validateCommand;
module.exports.isUUID = isUUID;
module.exports.constants = { MAX_INBOX_BYTES, MAX_COMMAND_LENGTH, POLL_MS, LOST_MESSAGE_MS };
