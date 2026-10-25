"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const {types} = require('node:util');
const { Type } = require("typebox");

const CONFIG_LIMIT = 16 * 1024;
const DEFAULTS = Object.freeze({
  enabled: true,
  maxBytes: 1024 * 1024,
  previewBytes: 4096,
  maxArchiveBytes: 512 * 1024 * 1024,
});
const MIN_MAX_BYTES = 4096;
const MAX_MAX_BYTES = 128 * 1024 * 1024;
const MAX_PREVIEW_BYTES = 65536;
const MAX_ARCHIVE_BYTES = 512 * 1024 * 1024;
const MAX_READ_BYTES = 64000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const GENERATED_RE = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(txt|json|meta\.json)$/i;
const CONFIG_KEYS = new Set(["enabled", "maxBytes", "previewBytes", "archiveDir", "maxArchiveBytes"]);

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function isUUID(value) {
  return typeof value === "string" && UUID_RE.test(value);
}

function managedInfo(env) {
  const control = env.SWARM_CONTROL_DIR;
  if (typeof control !== "string" || !control) return null;
  const controlDir = path.resolve(control);
  return { controlDir, root: path.dirname(path.dirname(controlDir)), agentId: env.SWARM_AGENT_ID };
}

function resolveLocations(env = process.env, home = os.homedir()) {
  const managed = managedInfo(env);
  const agentDir = path.resolve(env.PI_CODING_AGENT_DIR || path.join(home, ".pi", "agent"));
  let configPath;
  if (env.PI_OUTPUT_SPILL_CONFIG) {
    if (!path.isAbsolute(env.PI_OUTPUT_SPILL_CONFIG)) throw new Error("PI_OUTPUT_SPILL_CONFIG must be an absolute path");
    configPath = path.normalize(env.PI_OUTPUT_SPILL_CONFIG);
  } else if (managed) {
    configPath = path.join(managed.root, "output-spill.json");
  } else {
    configPath = path.join(agentDir, "output-spill.json");
  }
  let archiveDirFromEnv = null;
  if (env.PI_OUTPUT_SPILL_DIR) {
    if (!path.isAbsolute(env.PI_OUTPUT_SPILL_DIR)) throw new Error("PI_OUTPUT_SPILL_DIR must be an absolute path");
    archiveDirFromEnv = path.normalize(env.PI_OUTPUT_SPILL_DIR);
  } else if (managed && !isUUID(managed.agentId)) {
    throw new Error("SWARM_AGENT_ID must be a UUID when SWARM_CONTROL_DIR is set");
  }
  return { configPath, archiveDirFromEnv, agentDir, managed };
}

function sessionArchiveDir(ctx) {
  const sessionFile = ctx.sessionManager.getSessionFile?.();
  // Imported/custom session files can live outside Pi's configured session directory.
  // Always prefer their actual parent, and resolve again for each current context.
  if (typeof sessionFile === 'string' && path.isAbsolute(sessionFile)) return path.join(path.dirname(sessionFile),'output-spills');
  const sessionDir = ctx.sessionManager.getSessionDir?.();
  return typeof sessionDir === 'string' && path.isAbsolute(sessionDir) ? path.join(sessionDir,'output-spills') : null;
}

function readBounded(filename, limit = CONFIG_LIMIT) {
  let fd;
  try {
    fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) throw new Error("not a regular file");
    if (stat.size > limit) throw new Error(`file exceeds ${limit} bytes`);
    const data = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < data.length) {
      const count = fs.readSync(fd, data, offset, data.length - offset, offset);
      if (!count) break;
      offset += count;
    }
    return data.subarray(0, offset);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function validateConfig(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("configuration must be a JSON object");
  for (const key of Object.keys(value)) if (!CONFIG_KEYS.has(key)) throw new Error('unknown configuration field');
  const config = { ...DEFAULTS };
  if ("enabled" in value) {
    if (typeof value.enabled !== "boolean") throw new Error("enabled must be boolean");
    config.enabled = value.enabled;
  }
  for (const key of ["maxBytes", "previewBytes", "maxArchiveBytes"]) {
    if (key in value && (!Number.isSafeInteger(value[key]) || value[key] < 0)) throw new Error(`${key} must be a non-negative integer`);
    if (key in value) config[key] = value[key];
  }
  if (config.maxBytes < MIN_MAX_BYTES || config.maxBytes > MAX_MAX_BYTES) throw new Error(`maxBytes must be ${MIN_MAX_BYTES}-${MAX_MAX_BYTES}`);
  if (config.previewBytes > MAX_PREVIEW_BYTES || config.previewBytes > config.maxBytes) throw new Error("previewBytes must be 0-65536 and no greater than maxBytes");
  if (config.maxArchiveBytes < MIN_MAX_BYTES || config.maxArchiveBytes > MAX_ARCHIVE_BYTES) throw new Error(`maxArchiveBytes must be ${MIN_MAX_BYTES}-${MAX_ARCHIVE_BYTES}`);
  if ("archiveDir" in value) {
    if (typeof value.archiveDir !== "string" || !path.isAbsolute(value.archiveDir)) throw new Error("archiveDir must be an absolute path");
    config.archiveDir = path.normalize(value.archiveDir);
  }
  return config;
}

function parseSize(value) {
  const match = String(value).trim().match(/^(\d+)\s*(b|k|kb|kib|m|mb|mib)?$/i);
  if (!match) throw new Error("size must be an integer with optional B, KiB, or MiB suffix");
  const number = Number(match[1]);
  const unit = (match[2] || "b").toLowerCase();
  const multiplier = unit === "b" ? 1 : unit.startsWith("k") ? 1024 : 1024 * 1024;
  const result = number * multiplier;
  if (!Number.isSafeInteger(result)) throw new Error("size is too large");
  return result;
}

function fsyncDirectory(directory) {
  let fd;
  try {
    fd = fs.openSync(directory, "r");
    fs.fsyncSync(fd);
  } catch (error) {
    // Directory sync is unavailable on native Windows and some mounted filesystems.
    // Do not swallow actual disk/durability failures on platforms that support it.
    const unsupported = ['EINVAL','ENOTSUP','EOPNOTSUPP','ENOSYS'].includes(error.code) ||
      (process.platform==='win32' && ['EPERM','EACCES','EISDIR','EBADF'].includes(error.code));
    if (!unsupported) throw error;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function ensurePrivateDirectory(directory, recursive, privateLeaf = false) {
  let existed = false;
  try {
    const prior = fs.lstatSync(directory);
    existed = true;
    if (prior.isSymbolicLink() || !prior.isDirectory()) throw new Error(`refusing non-directory or symlink: ${directory}`);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  let firstCreated;
  if (!existed) {
    firstCreated = fs.mkdirSync(directory, {recursive,mode:0o700});
    if (!recursive) firstCreated = directory;
  }
  const stat = fs.lstatSync(directory);
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`refusing non-directory or symlink: ${directory}`);
  if (firstCreated) {
    fs.chmodSync(directory,0o700);
    for (let current=directory;;current=path.dirname(current)) {
      fsyncDirectory(current);fsyncDirectory(path.dirname(current));
      if (current===firstCreated || current===path.dirname(current)) break;
    }
  }
  if (privateLeaf && process.platform !== 'win32' && (stat.mode & 0o077)) throw new Error('session archive directory must be private (0700)');
  if (privateLeaf && typeof process.getuid === 'function' && stat.uid !== process.getuid()) throw new Error('session archive directory belongs to another user');
}

function atomicWrite(filename, data) {
  const directory = path.dirname(filename);
  const base = path.basename(filename);
  const existing = (() => { try { return fs.lstatSync(filename); } catch (error) { if (error.code === "ENOENT") return null; throw error; } })();
  if (existing) throw new Error(`refusing existing archive file: ${filename}`);
  const temporary = path.join(directory, `.${base}.${crypto.randomUUID()}.tmp`);
  let fd, committed = false;
  try {
    fd = fs.openSync(temporary, "wx", 0o600);
    fs.writeFileSync(fd, data);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temporary, filename); committed = true;
    fs.chmodSync(filename, 0o600);
    fsyncDirectory(directory);
  } catch (error) {
    if (committed) { try { fs.unlinkSync(filename); } catch {} }
    throw error;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    try { fs.unlinkSync(temporary); } catch (_) { /* renamed or already absent */ }
  }
}

// Serialize large strings as bounded chunks instead of building a second huge
// JSON buffer on Pi's event loop. JSON.stringify still owns object/toJSON semantics.
const MAX_SKELETON_BYTES = 64 * 1024 * 1024;
function quotedSize(value) {
  let size = Buffer.byteLength(value, 'utf8') + 2;
  const escaped = /["\\\x00-\x1f\ud800-\udfff]/g;
  let match;
  while ((match = escaped.exec(value))) {
    const code = value.charCodeAt(match.index);
    if (code >= 0xd800 && code <= 0xdbff && value.charCodeAt(match.index+1) >= 0xdc00 && value.charCodeAt(match.index+1) <= 0xdfff) { escaped.lastIndex++; continue; }
    if (code >= 0xd800 && code <= 0xdfff) size += 3;
    else if (code < 32) size += [8,9,10,12,13].includes(code) ? 1 : 5;
    else size++;
  }
  return size;
}
function jsonPlan(value, maxBytes) {
  const strings = new Map(); let bytes = 0, skeletonBytes = 0, root = true;
  const skeleton = JSON.stringify(value, function(key, current) {
    // Match JSON's boxed-primitive unwrapping without invoking spoofable tags.
    if (types.isStringObject(current)) current = String(current);
    else if (types.isNumberObject(current)) current = +current;
    else if (types.isBooleanObject(current)) current = Boolean.prototype.valueOf.call(current);
    const kind = typeof current;
    const omitted = current === undefined || kind === 'function' || kind === 'symbol';
    if (!root && !Array.isArray(this) && omitted) return undefined;
    const keyBytes = root || Array.isArray(this) ? 1 : quotedSize(key)+2;
    root = false;
    let size = current === null || omitted ? 4 : kind === 'string' ? quotedSize(current)
      : kind === 'number' ? (Number.isFinite(current) ? String(current).length : 4)
      : kind === 'boolean' ? (current ? 4 : 5) : 2;
    bytes += keyBytes + size;
    if (bytes > maxBytes) throw new Error('archive would exceed its byte budget');
    if (kind === 'string' && current.length > 4096) {
      const marker = `__SWARM_OUTPUT_${crypto.randomUUID()}__`;
      strings.set(JSON.stringify(marker), current);
      skeletonBytes += keyBytes + marker.length + 2;
      if (skeletonBytes > MAX_SKELETON_BYTES) throw new Error('archive metadata is too large to serialize safely');
      return marker;
    }
    skeletonBytes += keyBytes + size;
    if (skeletonBytes > MAX_SKELETON_BYTES) throw new Error('archive metadata is too large to serialize safely');
    return current;
  });
  if (skeleton === undefined) throw new Error('tool result is not JSON serializable');
  return {skeleton, strings, bytes};
}
function eachStringChunk(text, fn) {
  for (let start=0; start<text.length;) {
    let end = Math.min(text.length, start+16384);
    if (end<text.length && text.charCodeAt(end-1)>=0xd800 && text.charCodeAt(end-1)<=0xdbff && text.charCodeAt(end)>=0xdc00 && text.charCodeAt(end)<=0xdfff) end--;
    fn(text.slice(start,end)); start=end;
  }
}
function atomicStream(filename, emit, maxBytes) {
  const directory=path.dirname(filename), temporary=path.join(directory,`.${crypto.randomUUID()}.tmp`);
  if (fs.existsSync(filename)) throw new Error('archive path already exists');
  let fd, committed=false; const hash=crypto.createHash('sha256'); let size=0;
  try {
    fd=fs.openSync(temporary,'wx',0o600);
    emit(value=>{
      const buffer=Buffer.isBuffer(value)?value:Buffer.from(value,'utf8');
      size+=buffer.length;
      if (size>maxBytes) throw new Error('archive exceeds its byte budget');
      fs.writeFileSync(fd,buffer); hash.update(buffer);
    });
    fs.fsyncSync(fd); fs.closeSync(fd); fd=undefined;
    fs.renameSync(temporary,filename); committed=true;
    fs.chmodSync(filename,0o600);fsyncDirectory(directory);
    return {size,hash:hash.digest('hex')};
  } catch(error) {
    if(committed) {try{fs.unlinkSync(filename);}catch{}}
    throw error;
  } finally {
    if(fd!==undefined)fs.closeSync(fd);
    try{fs.unlinkSync(temporary);}catch{}
  }
}
function writeJsonPlan(filename, plan, maxBytes) {
  return atomicStream(filename,write=>{
    const pattern=/"__SWARM_OUTPUT_[0-9a-f-]{36}__"/g;let cursor=0,match;
    while((match=pattern.exec(plan.skeleton))) {
      const text=plan.strings.get(match[0]);if(text===undefined)continue;
      write(plan.skeleton.slice(cursor,match.index));write('"');
      eachStringChunk(text,chunk=>write(JSON.stringify(chunk).slice(1,-1)));
      write('"');cursor=pattern.lastIndex;
    }
    write(plan.skeleton.slice(cursor));
  },maxBytes);
}

function atomicConfigWrite(filename, value, limit = CONFIG_LIMIT) {
  const directory = path.dirname(filename);
  fs.mkdirSync(directory, { recursive: true });
  const lock = `${filename}.lock`;
  let lockFd;
  try {
    lockFd = fs.openSync(lock, "wx", 0o600);
  } catch (error) {
    if (error.code === "EEXIST") throw new Error(`configuration is locked: ${lock}`);
    throw error;
  }
  try {
    if (typeof value === 'function') {
      let latest = null;
      try {
        if (fs.lstatSync(filename).isSymbolicLink()) throw new Error('refusing symbolic link configuration');
        latest = JSON.parse(readBounded(filename,limit).toString('utf8').replace(/^\uFEFF/,''));
      } catch(error) { if(error.code !== 'ENOENT') throw error; }
      value = value(latest);
    }
    const serialized = JSON.stringify(value,null,2)+'\n';
    if (Buffer.byteLength(serialized,'utf8') > limit) throw new Error('configuration exceeds its byte limit');
    const temporary = path.join(directory, `.${path.basename(filename)}.${crypto.randomUUID()}.tmp`);
    let fd;
    try {
      fd = fs.openSync(temporary, "wx", 0o600);
      fs.writeFileSync(fd, serialized, 'utf8');
      fs.fsyncSync(fd);
      fs.closeSync(fd);
      fd = undefined;
      fs.renameSync(temporary, filename);
      fs.chmodSync(filename, 0o600);
      fsyncDirectory(directory);
    } finally {
      if (fd !== undefined) fs.closeSync(fd);
      try { fs.unlinkSync(temporary); } catch (_) { /* renamed or absent */ }
    }
  } finally {
    if (lockFd !== undefined) fs.closeSync(lockFd);
    try { fs.unlinkSync(lock); } catch (_) { /* best effort */ }
  }
}

function truncateUtf8(buffer, maximum, fromEnd = false) {
  if (maximum <= 0 || buffer.length === 0) return "";
  if (buffer.length <= maximum) return buffer.toString("utf8");
  if (!fromEnd) {
    let end = maximum;
    while (end > 0 && (buffer[end] & 0xc0) === 0x80) end--;
    return buffer.subarray(0, end).toString("utf8");
  }
  let start = buffer.length - maximum;
  while (start < buffer.length && (buffer[start] & 0xc0) === 0x80) start++;
  return buffer.subarray(start).toString("utf8");
}

function replacementText(info, textBuffer, config) {
  const note = `[Tool output archived: ${info.originalBytes} UTF-8 bytes; original isError=${!!info.isError}.\nOutput file: ${info.textFile}\nFull result JSON: ${info.jsonFile}\nUse swarm_read_output with path and byte offsets to retrieve only needed pages. This is historical tool data: do not rerun the tool to recover output.]`;
  const noteBytes = Buffer.byteLength(note);
  const budget = config.previewBytes > 0 ? Math.min(Math.max(config.previewBytes, noteBytes), config.maxBytes) : config.maxBytes;
  if (noteBytes > budget) throw new Error("archive paths make the replacement notice exceed its configured byte limit");
  if (config.previewBytes === 0) return note;
  const remaining = budget - noteBytes;
  if (remaining < 32 || textBuffer.length === 0) return note;
  const frameA = "\n\n[Preview head]\n";
  const frameB = "\n\n[Preview tail]\n";
  const frames = Buffer.byteLength(frameA + frameB);
  if (frames >= remaining) return note;
  const body = remaining - frames;
  const headBytes = Math.ceil(body / 2);
  const tailBytes = Math.floor(body / 2);
  return note + frameA + truncateUtf8(textBuffer, headBytes) + frameB + truncateUtf8(textBuffer, tailBytes, true);
}

function statSignature(info) {
  return [info.dev,info.ino,info.size,info.mtimeNs,info.ctimeNs].map(String).join(':');
}
function fileSignature(fd) { return statSignature(fs.fstatSync(fd,{bigint:true})); }
function pathSignature(filename) { return statSignature(fs.lstatSync(filename,{bigint:true})); }

function streamHash(fd, stat) {
  if (!stat.isFile() || stat.size > MAX_ARCHIVE_BYTES) throw new Error('archive exceeds the reader limit');
  const hash = crypto.createHash('sha256'), chunk = Buffer.alloc(1024 * 1024);
  let position = 0;
  while (position < stat.size) {
    const count = fs.readSync(fd, chunk, 0, Math.min(chunk.length, stat.size - position), position);
    if (!count) throw new Error('archive changed while reading');
    hash.update(chunk.subarray(0,count)); position += count;
  }
  const after = fs.fstatSync(fd);
  if (after.size !== stat.size || after.mtimeMs !== stat.mtimeMs) throw new Error('archive changed while reading');
  return hash.digest('hex');
}

function outputSpillExtension(pi) {
  let runtime = null;
  let locations = null;
  let configCache = { signature: null, valid: { ...DEFAULTS }, raw: null };
  const warned = new Set();

  function notify(ctx, message, level = "info") {
    if (ctx && ctx.hasUI && ctx.ui && typeof ctx.ui.notify === "function") ctx.ui.notify(String(message).slice(0, 2000), level);
  }

  function warnOnce(ctx, key, message) {
    const normalized = `${key}:${String(message).slice(0,1000)}`.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi,'<archive>');
    if (warned.has(normalized)) return;
    if (warned.size >= 64) warned.clear();
    warned.add(normalized);
    if (ctx?.hasUI) notify(ctx, `Output spill: ${message}`, 'warning');
    else process.stderr?.write?.(`Output spill: ${String(message).slice(0,2000)}\n`);
  }

  function getLocations() {
    if (!locations) locations = resolveLocations(process.env, os.homedir());
    return locations;
  }

  function loadConfig(ctx, force = false) {
    const filename = getLocations().configPath;
    let stat;
    try {
      stat = fs.lstatSync(filename);
      if (stat.isSymbolicLink() || !stat.isFile()) throw new Error("configuration is not a regular non-symlink file");
    } catch (error) {
      if (error.code === "ENOENT") {
        configCache = { signature: "missing", valid: { ...DEFAULTS }, raw: null };
        return configCache.valid;
      }
      warnOnce(ctx, "config-stat", error.message);
      return configCache.valid;
    }
    const signature = `${stat.ino}:${stat.mtimeMs}:${stat.ctimeMs}:${stat.size}`;
    if (!force && signature === configCache.signature) return configCache.valid;
    try {
      const raw = JSON.parse(readBounded(filename).toString('utf8').replace(/^\uFEFF/,''));
      const valid = validateConfig(raw);
      configCache = { signature, valid, raw };
    } catch (error) {
      warnOnce(ctx, `config-${signature}`, `cannot load ${filename}: ${error instanceof SyntaxError ? 'invalid JSON' : error.message}; using last valid settings`);
    }
    return configCache.valid;
  }

  function activeConfig(ctx, force = false) {
    const base = loadConfig(ctx, force);
    const overrides = runtime ? runtime.overrides : {};
    const selectedArchiveDir = getLocations().archiveDirFromEnv || base.archiveDir || sessionArchiveDir(ctx);
    const combined = { ...base, ...overrides, archiveDir: selectedArchiveDir };
    // A file change can increase preview size beneath a smaller session threshold.
    combined.previewBytes = Math.min(combined.previewBytes, combined.maxBytes);
    return combined;
  }

  function restoreState(ctx) {
    const overrides = {};
    const refs = new Map();
    for (const entry of ctx.sessionManager.getBranch()) {
      if (!entry || entry.type !== "custom") continue;
      if (entry.customType === 'swarm.output-spill.config' && entry.data?.overrides && typeof entry.data.overrides === 'object' && !Array.isArray(entry.data.overrides)) {
        for (const key of ["enabled", "maxBytes", "previewBytes"]) {
          if (Object.prototype.hasOwnProperty.call(entry.data.overrides, key)) overrides[key] = entry.data.overrides[key];
          else delete overrides[key];
        }
      }
      if (entry.customType === "swarm.output-spill.archive") addKnownRef(refs, entry.data);
    }
    try { validateConfig({ ...DEFAULTS, ...overrides }); } catch (_) { for (const key of Object.keys(overrides)) delete overrides[key]; }
    return { overrides, refs };
  }

  function addKnownRef(refs, data) {
    if (!data || !isUUID(data.id) || typeof data.session_id !== "string") return;
    for (const key of ["file", "jsonFile", "metadataFile"]) if (typeof data[key] !== "string" || !path.isAbsolute(data[key])) return;
    const file = path.normalize(data.file);
    const jsonFile = path.normalize(data.jsonFile);
    const metadataFile = path.normalize(data.metadataFile);
    const sessionDir = path.dirname(file);
    const expectedSessionDir = `session-${sha256(data.session_id).slice(0, 16)}`;
    if (path.basename(sessionDir) !== expectedSessionDir || path.dirname(jsonFile) !== sessionDir || path.dirname(metadataFile) !== sessionDir) return;
    if (path.basename(file) !== `${data.id}.txt` || path.basename(jsonFile) !== `${data.id}.json` || path.basename(metadataFile) !== `${data.id}.meta.json`) return;
    const reference = { ...data, file, jsonFile, metadataFile, root: path.dirname(sessionDir) };
    refs.set(file, reference);
    refs.set(jsonFile, reference);
  }

  function setOverrides(overrides) {
    pi.appendEntry("swarm.output-spill.config", { overrides: { ...overrides } });
    runtime.overrides = overrides;
  }

  function assertArchiveDirectories(root,sessionDir) {
    for (const directory of [root,sessionDir]) {
      const stat = fs.lstatSync(directory);
      if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error('refusing symlink or non-directory archive path');
      if (directory===sessionDir) {
        if (process.platform!=='win32' && (stat.mode&0o077)) throw new Error('session archive directory is not private');
        if (typeof process.getuid==='function' && stat.uid!==process.getuid()) throw new Error('archive directory belongs to another user');
      }
    }
  }
  function rootRegistry() { return `${getLocations().configPath}.roots.json`; }
  function trustedRoots(ctx) {
    const directory = activeConfig(ctx).archiveDir;
    const roots = directory ? [path.resolve(directory)] : [];
    try {
      const value = JSON.parse(readBounded(rootRegistry(),1024*1024).toString('utf8'));
      if (Array.isArray(value.roots)) for (const root of value.roots.slice(-128)) {
        if (typeof root === 'string' && path.isAbsolute(root)) roots.push(path.normalize(root));
      }
    } catch { /* Current root and durable branch references still work. */ }
    return roots;
  }
  function rememberRoot(root,ctx) {
    try {
      atomicConfigWrite(rootRegistry(), current => {
        const roots = Array.isArray(current?.roots) ? current.roots.filter(v=>typeof v==='string'&&path.isAbsolute(v)) : [];
        return {version:1,roots:[...roots.filter(v=>v!==root),root].slice(-128)};
      },1024*1024);
    } catch(error) { warnOnce(ctx,'root-registry','could not remember historical archive directory; keep session reference entries'); }
  }

  function archiveMessage(message, ctx, config, joined) {
    const sessionId = ctx.sessionManager.getSessionId();
    const sessionHash = sha256(String(sessionId)).slice(0, 16);
    if (!config.archiveDir) throw new Error('Pi session directory is unavailable; set an explicit archiveDir or PI_OUTPUT_SPILL_DIR');
    const root = path.resolve(config.archiveDir);
    const sessionDir = path.join(root, `session-${sessionHash}`);
    const id = crypto.randomUUID();
    const textFile = path.join(sessionDir, `${id}.txt`);
    const jsonFile = path.join(sessionDir, `${id}.json`);
    const metadataFile = path.join(sessionDir, `${id}.meta.json`);
    const textBytes = Buffer.byteLength(joined,'utf8');
    const plan = jsonPlan(message, config.maxArchiveBytes-textBytes);
    const metadata = {
      id,
      session_id: String(sessionId),
      tool_call_id: message.toolCallId,
      tool_name: message.toolName,
      original_bytes: textBytes,
      file: textFile,
      json_file: jsonFile,
      metadata_file: metadataFile,
      created_at: new Date().toISOString(),
    };
    const metadataBuffer = Buffer.from(JSON.stringify(metadata), "utf8");
    if (metadataBuffer.length+200 > 64*1024) throw new Error('archive metadata exceeds its reader limit');
    const archiveBytes = textBytes + plan.bytes + metadataBuffer.length + 200;
    if (archiveBytes > config.maxArchiveBytes || archiveBytes > MAX_ARCHIVE_BYTES) throw new Error(`archive would exceed ${config.maxArchiveBytes} bytes`);
    // Preview only a bounded pair of slices; do not allocate a buffer for all output.
    const excerpt = config.previewBytes ? Buffer.concat([
      Buffer.from(joined.slice(0,config.previewBytes),'utf8'),
      Buffer.from(joined.slice(-config.previewBytes),'utf8')]) : Buffer.alloc(0);
    const replacement = replacementText({ originalBytes: textBytes, textFile, jsonFile, metadataFile, isError:message.isError }, excerpt, config);
    if (Buffer.byteLength(replacement, "utf8") > config.maxBytes) throw new Error("replacement would exceed maxBytes");

    const written = [];
    let referenceAttempted = false;
    try {
      ensurePrivateDirectory(root, true);
      ensurePrivateDirectory(sessionDir, false, true);
      const plain = atomicStream(textFile, write=>eachStringChunk(joined,write), config.maxArchiveBytes);
      written.push(textFile);
      const json = writeJsonPlan(jsonFile,plan,config.maxArchiveBytes-plain.size);
      written.push(jsonFile);
      metadata.sha256=plain.hash;metadata.json_sha256=json.hash;
      const finalMetadata=Buffer.from(JSON.stringify(metadata),'utf8');
      if (plain.size+json.size+finalMetadata.length>config.maxArchiveBytes) throw new Error('archive exceeds total byte budget');
      atomicWrite(metadataFile,finalMetadata);written.push(metadataFile);
      const reference = { id, session_id: String(sessionId), file: textFile, metadataFile, jsonFile };
      referenceAttempted = true;
      pi.appendEntry("swarm.output-spill.archive", reference);
      addKnownRef(runtime.refs, reference);
      rememberRoot(root,ctx);
      return replacement;
    } catch (error) {
      if (!referenceAttempted) {
        for (const filename of written.reverse()) { try { fs.unlinkSync(filename); } catch (_) { /* best effort */ } }
        try { if (fs.existsSync(sessionDir) && fs.readdirSync(sessionDir).length === 0) fs.rmdirSync(sessionDir); } catch (_) { /* best effort */ }
      }
      throw error;
    }
  }

  pi.on("session_start", (_event, ctx) => {
    const base = loadConfig(ctx);
    const restored = restoreState(ctx);
    try {
      const combined = {...base,...restored.overrides};
      combined.previewBytes = Math.min(combined.previewBytes,combined.maxBytes);
      validateConfig(combined);
    } catch (error) {
      warnOnce(ctx, "restored-overrides", `stored session overrides are invalid with current settings: ${error.message}; using file settings`);
      restored.overrides = {};
    }
    runtime = { sessionId: String(ctx.sessionManager.getSessionId()), overrides: restored.overrides, refs: restored.refs, verified: new Map() };
  });

  pi.on("message_end", (event, ctx) => {
    const message = event && event.message;
    if (!runtime || !message || message.role !== 'toolResult') return undefined;
    const config = activeConfig(ctx);
    if (!config.enabled || !Array.isArray(message.content)) return undefined;
    const textBlocks = message.content.filter(block => block && block.type === "text" && typeof block.text === "string");
    if (!textBlocks.length) return undefined;
    const joined = textBlocks.map(block => block.text).join("\n");
    const originalBytes = Buffer.byteLength(joined, "utf8");
    if (originalBytes <= config.maxBytes) return undefined;
    try {
      const replacement = archiveMessage(message, ctx, config, joined);
      let inserted = false;
      const content = [];
      for (const block of message.content) {
        if (block?.type === 'text') {
          if (!inserted) { content.push({type:'text',text:replacement}); inserted = true; }
        } else content.push(block);
      }
      return {message:{...message,content}};
    } catch (error) {
      warnOnce(ctx, `archive-${error.code || error.name || "error"}-${error.message}`, `could not archive tool output: ${error.message}; original retained`);
      return undefined;
    }
  });

  pi.registerCommand("output-spill", {
    description: "Show or change output-spill settings for this session",
    handler: async (args, ctx) => {
      try {
        const words = args.trim().split(/\s+/).filter(Boolean);
        const action = (words.shift() || "status").toLowerCase();
        if (words.length > 1 || (!["limit", "preview"].includes(action) && words.length)) throw new Error("usage: /output-spill [status|on|off|limit SIZE|preview SIZE|reload|defaults|save]");
        if (action === "on" || action === "off") {
          setOverrides({ ...runtime.overrides, enabled: action === "on" });
        } else if (action === "limit") {
          if (!words.length) throw new Error("usage: /output-spill limit SIZE");
          const value = parseSize(words[0]);
          const currentPreview = Object.prototype.hasOwnProperty.call(runtime.overrides, "previewBytes") ? runtime.overrides.previewBytes : loadConfig(ctx).previewBytes;
          validateConfig({ ...DEFAULTS, maxBytes: value, previewBytes: currentPreview });
          setOverrides({ ...runtime.overrides, maxBytes: value });
        } else if (action === "preview") {
          if (!words.length) throw new Error("usage: /output-spill preview SIZE");
          const value = parseSize(words[0]);
          const currentMax = Object.prototype.hasOwnProperty.call(runtime.overrides, "maxBytes") ? runtime.overrides.maxBytes : loadConfig(ctx).maxBytes;
          validateConfig({ ...DEFAULTS, maxBytes: currentMax, previewBytes: value });
          setOverrides({ ...runtime.overrides, previewBytes: value });
        } else if (action === "reload") {
          loadConfig(ctx, true);
        } else if (action === "defaults") {
          setOverrides({});
        } else if (action === "save") {
          const filename = getLocations().configPath;
          let existing = {};
          try {
            const stat = fs.lstatSync(filename);
            if (stat.isSymbolicLink() || !stat.isFile()) throw new Error("existing configuration is not a regular non-symlink file");
            existing = JSON.parse(readBounded(filename).toString('utf8').replace(/^\uFEFF/,''));
            validateConfig(existing);
          } catch (error) {
            if (error.code !== "ENOENT") throw new Error(`refusing to overwrite existing configuration: ${error instanceof SyntaxError ? 'invalid JSON' : error.message}`);
          }
          const active = activeConfig(ctx);
          const saved = { enabled: active.enabled, maxBytes: active.maxBytes, previewBytes: active.previewBytes };
          if (Object.prototype.hasOwnProperty.call(existing, "archiveDir")) saved.archiveDir = existing.archiveDir;
          if (Object.prototype.hasOwnProperty.call(existing, "maxArchiveBytes")) saved.maxArchiveBytes = existing.maxArchiveBytes;
          atomicConfigWrite(filename, latest => {
            if (latest) validateConfig(latest);
            for (const key of ['archiveDir','maxArchiveBytes']) {
              if (latest && Object.prototype.hasOwnProperty.call(latest,key)) saved[key] = latest[key];
              else delete saved[key];
            }
            validateConfig(saved); return saved;
          });
          configCache = { signature: null, valid: validateConfig(saved), raw: saved };
          setOverrides({});
        } else if (action !== "status") {
          throw new Error("usage: /output-spill [status|on|off|limit SIZE|preview SIZE|reload|defaults|save]");
        }
        const current = activeConfig(ctx);
        notify(ctx, `Output spill ${current.enabled ? 'on' : 'off'}; limit ${current.maxBytes} bytes; preview ${current.previewBytes} bytes; archive ${current.archiveDir || 'unavailable (no Pi session directory)'}; config ${getLocations().configPath} (commands are session-only until save)`, 'info');
      } catch (error) {
        notify(ctx, `Output spill: ${error.message}`, "error");
      }
    },
  });

  pi.registerTool({
    name: "swarm_read_output",
    label: "Read Archived Tool Output",
    description: "Read a bounded page from an output-spill .txt or .json archive. Offsets and limits are UTF-8 byte counts; use next_offset until eof.",
    parameters: Type.Object({
      path: Type.String({ description: "Exact archived .txt or .json path from an output-spill notice" }),
      offset: Type.Optional(Type.Integer({ minimum: 0, default: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_READ_BYTES, default: 8000 })),
    }),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      if (!runtime) throw new Error("no active output-spill session");
      const requested = path.normalize(params.path);
      let ref = runtime.refs.get(requested);
      if (!ref) {
        // Swarm's offline compaction can remove old non-context entries while keeping
        // the same session ID. A valid owned sidecar still proves a default-root ref.
        const match = GENERATED_RE.exec(path.basename(requested));
        const root = trustedRoots(ctx).find(value => path.dirname(requested) === path.join(value,`session-${sha256(runtime.sessionId).slice(0,16)}`));
        const expectedDir = root ? path.join(root, `session-${sha256(runtime.sessionId).slice(0,16)}`) : null;
        if (match && expectedDir) {
          assertArchiveDirectories(root,expectedDir);
          const metadataFile = path.join(expectedDir, `${match[1]}.meta.json`);
          const metadata = JSON.parse(readBounded(metadataFile,64*1024).toString('utf8'));
          if (metadata.session_id === runtime.sessionId) {
            addKnownRef(runtime.refs, {id:metadata.id, session_id:metadata.session_id,
              file:metadata.file, jsonFile:metadata.json_file, metadataFile});
            ref = runtime.refs.get(requested);
          }
        }
      }
      if (!ref) throw new Error('path is not an output archive referenced by this session');
      if (ref.session_id !== runtime.sessionId) throw new Error("archive belongs to a different session");
      const root = ref.root;
      const expectedDir = path.join(root, `session-${sha256(runtime.sessionId).slice(0, 16)}`);
      if (path.dirname(requested) !== expectedDir || !GENERATED_RE.test(path.basename(requested))) throw new Error("archive path is outside the owned session directory");
      assertArchiveDirectories(root,expectedDir);
      for (const candidate of [requested,ref.metadataFile]) {
        const stat = fs.lstatSync(candidate);
        if (stat.isSymbolicLink() || !stat.isFile()) throw new Error('refusing symlink or non-regular archive path');
      }
      const metadataSignature = pathSignature(ref.metadataFile);
      const metadata = JSON.parse(readBounded(ref.metadataFile,64*1024).toString('utf8'));
      if (pathSignature(ref.metadataFile)!==metadataSignature) throw new Error('archive manifest changed while reading');
      if (metadata.id !== ref.id || metadata.session_id !== runtime.sessionId || metadata.file !== ref.file || metadata.json_file !== ref.jsonFile || metadata.metadata_file !== ref.metadataFile) throw new Error("archive metadata does not match the session reference");
      const isText = requested === path.normalize(ref.file);
      const isJson = requested === path.normalize(ref.jsonFile);
      if (!isText && !isJson) throw new Error("only referenced .txt and .json archives can be read");
      const expectedHash = isText ? metadata.sha256 : metadata.json_sha256;
      if (typeof expectedHash !== 'string') throw new Error('archive checksum metadata is invalid');
      const stat = fs.statSync(requested);
      let offset = params.offset === undefined ? 0 : params.offset;
      let limit = params.limit === undefined ? 8000 : params.limit;
      if (!Number.isSafeInteger(offset) || offset < 0) throw new Error("offset must be a non-negative integer");
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_READ_BYTES) throw new Error(`limit must be 1-${MAX_READ_BYTES}`);
      offset = Math.min(offset, stat.size);
      const config = activeConfig(ctx);
      limit = Math.min(limit, Math.max(1, config.maxBytes - 512));
      const fd = fs.openSync(requested, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
      let page;
      let start = offset;
      try {
        const opened = fs.fstatSync(fd);
        if (!opened.isFile() || opened.size > MAX_ARCHIVE_BYTES || opened.size !== stat.size || opened.ino !== stat.ino || opened.dev !== stat.dev) throw new Error('archive changed before reading');
        const signature = fileSignature(fd);
        const cached = runtime.verified.get(requested);
        if (!cached || cached.signature !== signature || cached.hash !== expectedHash) {
          if (streamHash(fd, opened) !== expectedHash) throw new Error('archive checksum verification failed');
          runtime.verified.set(requested, {signature, hash: expectedHash});
          if (runtime.verified.size > 256) runtime.verified.delete(runtime.verified.keys().next().value);
        }
        const prefix = Buffer.alloc(Math.min(4, stat.size - start));
        if (prefix.length) fs.readSync(fd, prefix, 0, prefix.length, start);
        let skipped = 0;
        while (skipped < prefix.length && (prefix[skipped] & 0xc0) === 0x80) skipped++;
        start += skipped;
        const available = Math.min(limit, stat.size - start);
        const buffer = Buffer.alloc(Math.min(available + 4, stat.size - start));
        if (buffer.length) fs.readSync(fd, buffer, 0, buffer.length, start);
        let end = available;
        if (start + end < stat.size) while (end > 0 && (buffer[end] & 0xc0) === 0x80) end--;
        if (end === 0 && buffer.length) {
          const first = buffer[0];
          const width = first < 0x80 ? 1 : first < 0xe0 ? 2 : first < 0xf0 ? 3 : 4;
          end = Math.min(width, buffer.length);
        }
        page = buffer.subarray(0, end);
        if (fileSignature(fd) !== signature || pathSignature(ref.metadataFile)!==metadataSignature) throw new Error('archive changed while reading the page');
      } finally {
        fs.closeSync(fd);
      }
      const nextOffset = start + page.length;
      return {
        content: [{ type: 'text', text: `[Archived output page: offset=${start} next_offset=${nextOffset} total_bytes=${stat.size} eof=${nextOffset >= stat.size}]\n${page.toString('utf8')}` }],
        details: { path: requested, offset: start, next_offset: nextOffset, total_bytes: stat.size, eof: nextOffset >= stat.size },
      };
    },
  });
}

module.exports = outputSpillExtension;
module.exports.default = outputSpillExtension;
module.exports.resolveLocations = resolveLocations;
module.exports.validateConfig = validateConfig;
module.exports.parseSize = parseSize;
module.exports.replacementText = replacementText;
module.exports.jsonPlan = jsonPlan;
module.exports.quotedSize = quotedSize;
module.exports.constants = { CONFIG_LIMIT, DEFAULTS, MIN_MAX_BYTES, MAX_MAX_BYTES, MAX_PREVIEW_BYTES, MAX_ARCHIVE_BYTES, MAX_READ_BYTES };
