"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { Type } = require("typebox");

const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_FILE_BYTES = 128 * 1024 * 1024;
const MAX_READ_BYTES = 64000;
const DEFAULT_READ_BYTES = 8000;
const ARCHIVE_ID = /^[0-9a-f]{32}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const FORMAT_FILES = Object.freeze({ text: "history.txt", jsonl: "history.jsonl" });

function sessionHash(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex").slice(0, 24);
}

function identity(stat) {
  return [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs].map(String).join(":");
}

function sameIdentity(left, right) {
  return identity(left) === identity(right);
}

function privateOwned(stat, kind) {
  if (typeof process.getuid === "function" && stat.uid !== BigInt(process.getuid())) {
    throw new Error(`${kind} is not owned by the current user`);
  }
  if (process.platform !== "win32" && (stat.mode & 0o077n) !== 0n) {
    throw new Error(`${kind} is not private`);
  }
}

function safeDirectory(directory, kind, requirePrivate = true) {
  const stat = fs.lstatSync(directory, { bigint: true });
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`${kind} is unsafe`);
  if (typeof process.getuid === "function" && stat.uid !== BigInt(process.getuid())) {
    throw new Error(`${kind} is not owned by the current user`);
  }
  if (requirePrivate) privateOwned(stat, kind);
}

function hideFilesystemPaths(action) {
  try { return action(); }
  catch (error) {
    if (error && typeof error.code === "string") throw new Error("history archive is unavailable");
    throw error;
  }
}

function openRegular(filename, maximum, kind) {
  const before = fs.lstatSync(filename, { bigint: true });
  if (before.isSymbolicLink() || !before.isFile()) throw new Error(`${kind} is not a regular file`);
  privateOwned(before, kind);
  const fd = fs.openSync(filename, fs.constants.O_RDONLY |
    (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
  try {
    const opened = fs.fstatSync(fd, { bigint: true });
    if (!opened.isFile() || !sameIdentity(before, opened)) throw new Error(`${kind} changed before reading`);
    if (opened.size > BigInt(maximum)) throw new Error(`${kind} exceeds its size limit`);
    return { fd, stat: opened };
  } catch (error) {
    fs.closeSync(fd);
    throw error;
  }
}

function readAt(fd, size, position, changedMessage) {
  const buffer = Buffer.alloc(size);
  let used = 0;
  while (used < size) {
    const count = fs.readSync(fd, buffer, used, size - used, position + used);
    if (!count) throw new Error(changedMessage);
    used += count;
  }
  return buffer;
}

function readManifest(filename) {
  const opened = openRegular(filename, MAX_MANIFEST_BYTES, "archive manifest");
  try {
    const size = Number(opened.stat.size);
    const buffer = size ? readAt(opened.fd, size, 0, "archive manifest changed while reading") : Buffer.alloc(0);
    const after = fs.fstatSync(opened.fd, { bigint: true });
    if (!sameIdentity(opened.stat, after)) throw new Error("archive manifest changed while reading");
    let value;
    try { value = JSON.parse(buffer.toString("utf8")); }
    catch (_) { throw new Error("archive manifest is invalid"); }
    return { value, signature: identity(after) };
  } finally {
    fs.closeSync(opened.fd);
  }
}

function pathIdentity(filename, kind) {
  const stat = fs.lstatSync(filename, { bigint: true });
  if (stat.isSymbolicLink() || !stat.isFile()) throw new Error(`${kind} is not a regular file`);
  return identity(stat);
}

function hashFile(fd, initial) {
  const hash = crypto.createHash("sha256");
  const chunk = Buffer.alloc(1024 * 1024);
  const total = Number(initial.size);
  let position = 0;
  while (position < total) {
    const wanted = Math.min(chunk.length, total - position);
    const count = fs.readSync(fd, chunk, 0, wanted, position);
    if (!count) throw new Error("history archive changed while verifying");
    hash.update(chunk.subarray(0, count));
    position += count;
  }
  if (!sameIdentity(initial, fs.fstatSync(fd, { bigint: true }))) {
    throw new Error("history archive changed while verifying");
  }
  return hash.digest("hex");
}

function validateManifest(value, archiveId, sessionId, sessionFile, filename) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.version !== 1 ||
      value.archive_id !== archiveId || value.session_id !== sessionId) {
    throw new Error("archive manifest does not match the current session");
  }
  const source = value.agent_source === undefined ? value.source : value.agent_source;
  if (typeof value.source !== 'string' || !value.source || typeof source !== "string" || !source || !path.isAbsolute(source) ||
      path.normalize(source) !== source || source !== sessionFile) {
    throw new Error("archive manifest source does not match the current session");
  }
  if (!value.files || typeof value.files !== "object" || Array.isArray(value.files) ||
      !Object.prototype.hasOwnProperty.call(value.files, filename)) {
    throw new Error("archive manifest has no readable history file");
  }
  const descriptor = value.files[filename];
  if (!descriptor || typeof descriptor !== "object" || Array.isArray(descriptor) ||
      !SHA256.test(descriptor.sha256) || !Number.isSafeInteger(descriptor.length) ||
      descriptor.length < 0 || descriptor.length > MAX_FILE_BYTES) {
    throw new Error("archive history descriptor is invalid");
  }
  return descriptor;
}

function historyReaderExtension(pi) {
  const verified = new Map();

  pi.registerTool({
    name: "swarm_read_history",
    label: "Read Offline History Archive",
    description: "Read a bounded UTF-8 byte page from this session's offline history archive by archive ID. Use next_offset until eof; jsonl pages are returned raw and are not parsed.",
    parameters: Type.Object({
      archive_id: Type.String({ pattern: "^[0-9a-f]{32}$", description: "32-character lowercase hexadecimal archive ID from the history archive notice" }),
      offset: Type.Optional(Type.Integer({ minimum: 0, default: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_READ_BYTES, default: DEFAULT_READ_BYTES })),
      format: Type.Optional(Type.Union([Type.Literal("text"), Type.Literal("jsonl")], { default: "text" })),
    }),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const archiveId = params.archive_id;
      const offset = params.offset === undefined ? 0 : params.offset;
      const limit = params.limit === undefined ? DEFAULT_READ_BYTES : params.limit;
      const format = params.format === undefined ? "text" : params.format;
      if (!ARCHIVE_ID.test(archiveId)) throw new Error("archive_id must be 32 lowercase hexadecimal characters");
      if (!Number.isSafeInteger(offset) || offset < 0) throw new Error("offset must be a non-negative integer");
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_READ_BYTES) throw new Error("limit must be 1-64000");
      if (!Object.prototype.hasOwnProperty.call(FORMAT_FILES, format)) throw new Error("format must be text or jsonl");

      return hideFilesystemPaths(() => {
      const rawSessionFile = ctx?.sessionManager?.getSessionFile?.();
      const rawSessionId = ctx?.sessionManager?.getSessionId?.();
      if (typeof rawSessionFile !== "string" || !path.isAbsolute(rawSessionFile) ||
          typeof rawSessionId !== "string" || !rawSessionId) {
        throw new Error("the current session has no readable native history location");
      }
      const sessionFile = path.normalize(rawSessionFile);
      const parent = path.dirname(sessionFile);
      const nativeRealpath = fs.realpathSync.native || fs.realpathSync;
      if (nativeRealpath(parent) !== parent) throw new Error("the session parent is not canonical");
      safeDirectory(parent, "session parent", false);

      const root = path.join(parent, "context-archives");
      const sessionDirectory = path.join(root, sessionHash(rawSessionId));
      const archiveDirectory = path.join(sessionDirectory, archiveId);
      for (const [directory, kind] of [[root, "archive root"], [sessionDirectory, "session archive directory"], [archiveDirectory, "history archive directory"]]) {
        safeDirectory(directory, kind);
      }

      const manifestFile = path.join(archiveDirectory, "manifest.json");
      const selectedName = FORMAT_FILES[format];
      const selectedFile = path.join(archiveDirectory, selectedName);
      const manifest = readManifest(manifestFile);
      const descriptor = validateManifest(manifest.value, archiveId, rawSessionId, sessionFile, selectedName);
      if (pathIdentity(manifestFile, "archive manifest") !== manifest.signature) {
        throw new Error("archive manifest changed while reading");
      }

      const opened = openRegular(selectedFile, MAX_FILE_BYTES, "history archive");
      let page, start;
      try {
        if (opened.stat.size !== BigInt(descriptor.length)) throw new Error("history archive length does not match its manifest");
        const signature = identity(opened.stat);
        const cached = verified.get(selectedFile);
        if (!cached || cached.signature !== signature || cached.sha256 !== descriptor.sha256) {
          if (hashFile(opened.fd, opened.stat) !== descriptor.sha256) throw new Error("history archive checksum does not match its manifest");
          verified.delete(selectedFile);
          verified.set(selectedFile, { signature, sha256: descriptor.sha256 });
          if (verified.size > 64) verified.delete(verified.keys().next().value);
        }

        const total = descriptor.length;
        start = Math.min(offset, total);
        if (start < total) {
          const prefix = readAt(opened.fd, Math.min(4, total - start), start, "history archive changed while reading");
          let skipped = 0;
          while (skipped < prefix.length && (prefix[skipped] & 0xc0) === 0x80) skipped++;
          start += skipped;
        }
        const available = Math.min(limit, total - start);
        const buffer = available ? readAt(opened.fd, Math.min(available + 4, total - start), start, "history archive changed while reading") : Buffer.alloc(0);
        let end = available;
        if (start + end < total) while (end > 0 && (buffer[end] & 0xc0) === 0x80) end--;
        if (end === 0 && buffer.length) {
          const first = buffer[0];
          end = Math.min(first < 0x80 ? 1 : first < 0xe0 ? 2 : first < 0xf0 ? 3 : 4, buffer.length);
        }
        page = buffer.subarray(0, end);
        if (!sameIdentity(opened.stat, fs.fstatSync(opened.fd, { bigint: true }))) {
          throw new Error("history archive changed while reading");
        }
        if (pathIdentity(manifestFile, "archive manifest") !== manifest.signature) {
          throw new Error("archive manifest changed while reading");
        }
      } finally {
        fs.closeSync(opened.fd);
      }

      const nextOffset = start + page.length;
      const eof = nextOffset >= descriptor.length;
      return {
        content: [{ type: "text", text: `[History archive page: archive_id=${archiveId} format=${format} offset=${start} next_offset=${nextOffset} total_bytes=${descriptor.length} eof=${eof}]\n${page.toString("utf8")}` }],
        details: { archive_id: archiveId, format, offset: start, next_offset: nextOffset, total_bytes: descriptor.length, eof },
      };
      });
    },
  });
}

module.exports = historyReaderExtension;
module.exports.default = historyReaderExtension;
