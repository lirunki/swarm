"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { Text } = require("@earendil-works/pi-tui");

const EXTENSION_ID = "swarm.thinking-context-pruner";
const THINKING_ENTRY = `${EXTENSION_ID}.thinking`;
const CONFIG_ENTRY = `${EXTENSION_ID}.config`;
const CONFIG_LIMIT = 16 * 1024;
const DEFAULTS = Object.freeze({
  enabled: true,
  keepCopy: true,
  stripThinkingSignatures: true,
});
const CONFIG_KEYS = new Set(Object.keys(DEFAULTS));

function resolveConfigPath(env = process.env, home = os.homedir()) {
  if (env.PI_THINKING_CONTEXT_CONFIG) {
    if (!path.isAbsolute(env.PI_THINKING_CONTEXT_CONFIG)) {
      throw new Error("PI_THINKING_CONTEXT_CONFIG must be an absolute path");
    }
    return path.normalize(env.PI_THINKING_CONTEXT_CONFIG);
  }
  return path.join(path.resolve(env.PI_CODING_AGENT_DIR || path.join(home, ".pi", "agent")), "thinking-context-pruner.json");
}

function readConfig(filename) {
  let fd;
  try {
    fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  } catch (error) {
    if (error.code === "ENOENT") return { ...DEFAULTS };
    throw error;
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) throw new Error("config is not a regular file");
    if (stat.size > CONFIG_LIMIT) throw new Error(`config exceeds ${CONFIG_LIMIT} bytes`);
    const bytes = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < bytes.length) {
      const count = fs.readSync(fd, bytes, offset, bytes.length - offset, offset);
      if (!count) break;
      offset += count;
    }
    const value = JSON.parse(bytes.toString("utf8", 0, offset).replace(/^\uFEFF/, ""));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("config must be a JSON object");
    for (const key of Object.keys(value)) if (!CONFIG_KEYS.has(key)) throw new Error(`unknown config field: ${key}`);
    const config = { ...DEFAULTS, ...value };
    for (const key of CONFIG_KEYS) if (typeof config[key] !== "boolean") throw new Error(`${key} must be boolean`);
    return config;
  } finally {
    fs.closeSync(fd);
  }
}

function withoutThinking(message, stripSignatures) {
  let changed = false;
  const content = [];
  for (const block of message.content) {
    if (!block || block.type !== "thinking") {
      content.push(block);
      continue;
    }
    changed = true;
    if (!stripSignatures && block.thinkingSignature) {
      content.push({ type: "thinking", thinking: "", thinkingSignature: block.thinkingSignature, ...(block.redacted ? { redacted: true } : {}) });
    }
  }
  return changed ? { ...message, content } : message;
}

function thinkingText(message) {
  return message.content
    .filter(block => block && block.type === "thinking" && typeof block.thinking === "string" && block.thinking.trim())
    .map(block => block.thinking)
    .join("\n\n")
    .trim();
}

function branchEntries(ctx) {
  const manager = ctx.sessionManager;
  if (typeof manager.getBranch === "function") return manager.getBranch();
  return manager.getEntries();
}

function thinkingEntryData(entry) {
  if (!entry || entry.type !== "custom" || entry.customType !== THINKING_ENTRY) return undefined;
  const data = entry.data;
  return data && typeof data.text === "string" ? data : undefined;
}

function extension(pi) {
  let configPath = resolveConfigPath();
  let fileConfig = { ...DEFAULTS };
  let sessionOverrides = {};
  let configValid = true;
  let warned = false;

  function loadConfig(ctx, notify = false) {
    try {
      fileConfig = readConfig(configPath);
      configValid = true;
      warned = false;
      return true;
    } catch (error) {
      configValid = false;
      if (notify && !warned) {
        warned = true;
        ctx.ui.notify(`Thinking pruner config ignored: ${error.message}`, "warning");
      }
      return false;
    }
  }

  function restoreState(ctx) {
    sessionOverrides = {};
    for (const entry of branchEntries(ctx)) {
      if (entry && entry.type === "custom" && entry.customType === CONFIG_ENTRY &&
          entry.data && entry.data.overrides && typeof entry.data.overrides === "object" && !Array.isArray(entry.data.overrides)) {
        const overrides = entry.data.overrides;
        if (Object.keys(overrides).every(key => CONFIG_KEYS.has(key) && typeof overrides[key] === "boolean")) {
          sessionOverrides = { ...overrides };
        }
      }
    }
  }

  function effectiveConfig() {
    return { ...fileConfig, ...sessionOverrides };
  }

  function persistOverrides() {
    pi.appendEntry(CONFIG_ENTRY, { overrides: { ...sessionOverrides } });
  }

  function notifyStatus(ctx) {
    const config = effectiveConfig();
    ctx.ui.notify(
      `Thinking blocks are ${config.enabled ? "removed" : "kept"} for future context. ` +
      `Separate non-context copy: ${config.enabled && config.keepCopy ? "on" : "off"}. ` +
      `Thinking signatures: ${config.stripThinkingSignatures ? "also removed" : "preserved in empty blocks"}.`,
      "info",
    );
  }

  pi.on("session_start", (_event, ctx) => {
    loadConfig(ctx);
    restoreState(ctx);
  });

  pi.on("message_end", (event, ctx) => {
    if (!event || !event.message || event.message.role !== "assistant" || !Array.isArray(event.message.content)) return undefined;
    const config = effectiveConfig();
    if (!config.enabled || !event.message.content.some(block => block && block.type === "thinking")) return undefined;

    if (config.keepCopy) {
      const text = thinkingText(event.message);
      if (text) {
        pi.appendEntry(THINKING_ENTRY, {
          provider: event.message.provider,
          model: event.message.model,
          text,
        });
      }
    }
    return { message: withoutThinking(event.message, config.stripThinkingSignatures) };
  });

  pi.on("context", (event) => {
    const config = effectiveConfig();
    if (!config.enabled) return undefined;
    let changed = false;
    const messages = event.messages.map(message => {
      if (message.role !== "assistant" || !Array.isArray(message.content) || !message.content.some(block => block && block.type === "thinking")) return message;
      const filtered = withoutThinking(message, config.stripThinkingSignatures);
      if (filtered !== message) changed = true;
      return filtered;
    });
    return changed ? { messages } : undefined;
  });

  pi.registerEntryRenderer(THINKING_ENTRY, (entry, { expanded }, theme) => {
    const data = thinkingEntryData(entry);
    if (!data) return undefined;
    const title = theme.fg("accent", `Thinking (${data.provider || "unknown"}/${data.model || "unknown"})`);
    const preview = data.text.split(/\r?\n/, 1)[0].slice(0, 240);
    return new Text(`${title}\n${theme.fg("muted", expanded ? data.text : preview)}`, 0, 0);
  });

  pi.registerCommand("thinking-copy", {
    description: "Show recent retained thinking copies from this session branch",
    handler: async (_args, ctx) => {
      const entries = branchEntries(ctx).map(thinkingEntryData).filter(Boolean);
      const latest = entries.slice(-5).map(data => `[${data.provider || "unknown"}/${data.model || "unknown"}]\n${data.text}`).join("\n\n---\n\n");
      ctx.ui.notify(latest || "No retained thinking copies on this branch.", "info");
    },
  });

  pi.registerCommand("thinking-context", {
    description: "Configure thinking removal/copy (on|off|copy on|copy off|signatures preserve|signatures strip|status|reload|save)",
    handler: async (args, ctx) => {
      const action = args.trim().toLowerCase();
      if (!action || action === "status") return notifyStatus(ctx);
      if (action === "reload") {
        const previous = { ...fileConfig };
        const loaded = loadConfig(ctx, true);
        if (!loaded) fileConfig = previous;
        else restoreState(ctx);
        return notifyStatus(ctx);
      }
      if (action === "on" || action === "off") {
        sessionOverrides.enabled = action === "on";
      } else if (action === "copy on" || action === "copy off") {
        sessionOverrides.keepCopy = action === "copy on";
      } else if (action === "signatures preserve" || action === "signatures strip") {
        sessionOverrides.stripThinkingSignatures = action === "signatures strip";
      } else if (action === "save") {
        const config = effectiveConfig();
        if (!configValid && fs.existsSync(configPath)) {
          ctx.ui.notify("Refusing to overwrite an invalid thinking-pruner config. Fix or remove the file, then reload.", "error");
          return;
        }
        try {
          fs.mkdirSync(path.dirname(configPath), { recursive: true });
          const temporary = `${configPath}.${crypto.randomUUID()}.tmp`;
          const fd = fs.openSync(temporary, "wx", 0o600);
          try {
            fs.writeFileSync(fd, `${JSON.stringify(config, null, 2)}\n`, "utf8");
            fs.fsyncSync(fd);
          } finally {
            fs.closeSync(fd);
          }
          fs.renameSync(temporary, configPath);
          try { fs.chmodSync(configPath, 0o600); } catch (_) { /* platform ACLs may replace POSIX modes */ }
          fileConfig = config;
          configValid = true;
          sessionOverrides = {};
        } catch (error) {
          try { fs.unlinkSync(temporary); } catch (_) { /* no temporary file remains */ }
          ctx.ui.notify(`Could not save thinking-pruner config: ${error.message}`, "error");
          return;
        }
      } else {
        ctx.ui.notify("Usage: /thinking-context [status|on|off|copy on|copy off|signatures preserve|signatures strip|reload|save]", "warning");
        return;
      }
      persistOverrides();
      notifyStatus(ctx);
    },
  });
}

module.exports = extension;
module.exports.resolveConfigPath = resolveConfigPath;
module.exports.readConfig = readConfig;
module.exports.withoutThinking = withoutThinking;
module.exports.THINKING_ENTRY = THINKING_ENTRY;
module.exports.CONFIG_ENTRY = CONFIG_ENTRY;
module.exports.extension = extension;
