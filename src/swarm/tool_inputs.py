"""Deterministic tool-input elision with immutable archives and bounded lookup."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shlex
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .meta_compact import MetaCompactor, digest, read_source, session_context
from .models import now
from .paths import agent_path
from .store import Store, atomic_write

log = logging.getLogger(__name__)
LOOKUP_ID = re.compile(r"toolrun_([0-9a-f]{32})_([0-9a-f]{16})")
GUIDE_TYPE = "swarm.tool_input_archive"


class RevertToolInputsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_history_loss: bool = Field(default=False, strict=True)


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def effective_content(entry: dict, edits: dict):
    if entry["id"] in edits:
        edit = edits[entry["id"]]
        if edit is None:
            return None
        if not isinstance(edit, dict) or "content" not in edit:
            raise ValueError("Invalid context edit")
        return edit["content"]
    return (
        entry.get("content")
        if entry.get("type") == "custom_message"
        else entry.get("message", {}).get("content")
    )


def inert_entry(entry: dict, content, kind: str, extra: dict | None = None) -> None:
    """Keep output payloads exactly; only the envelope becomes non-executable context."""
    message = entry.pop("message")
    entry.update(
        type="custom_message",
        customType=kind,
        display=True,
        content=content,
        details={
            "original_message_metadata": {k: v for k, v in message.items() if k != "content"},
            **(extra or {}),
        },
    )


def make_preview(call: dict, entry_id: str, block_index: int, job_id: str):
    if not isinstance(call.get("arguments"), dict) or not isinstance(call.get("id"), str):
        return None
    arguments = encode(call["arguments"])
    if len(arguments) < 1000 and arguments.count("\\n") < 2:
        return None
    suffix = digest(f"{entry_id}:{block_index}".encode())[:16]
    toolrun_id = f"toolrun_{job_id}_{suffix}"
    path = call["arguments"].get("path") or call["arguments"].get("file_path")
    stub = (
        f"[Historical {call.get('name', 'tool')} input archived; NOT a pending command]\n"
        f"toolrun_id: {toolrun_id}\n"
        + (f"Path: {str(path)[:200]}\n" if path else "")
        + f"Original call ID: {call['id']}\n"
        + f"Input preview (first 100 characters): {arguments[:100]}…\n"
        + f"Full input: {len(arguments)} characters; use swarm tool-input to retrieve."
    )
    if len(arguments) <= len(stub) + 256:
        return None
    return (
        toolrun_id,
        stub,
        {
            "entry_id": entry_id,
            "block_index": block_index,
            "tool_call_id": call["id"],
            "tool_name": call.get("name"),
            "arguments": copy.deepcopy(call["arguments"]),
        },
    )


def compact_imported(selected: list[dict], edits: dict, job_id: str):
    """Handle Swarm's Windows-Pi imports, whose tool calls are already inert text."""
    lookup, changed, savings = {}, set(), 0
    sources = [
        (i, e.get("data", {}).get("record", {}))
        for i, e in enumerate(selected)
        if e.get("customType") == "swarm.import.pi.record"
    ]
    for source_index, (pos, record) in enumerate(sources):
        message = record.get("message", {})
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        # Importer writes one source record immediately before its visible transcript entry.
        end = sources[source_index + 1][0] if source_index + 1 < len(sources) else len(selected)
        targets = [e for e in selected[pos + 1 : end] if e.get("type") == "custom_message"]
        if len(targets) != 1:
            continue
        target = targets[0]
        text = effective_content(target, edits)
        if not isinstance(text, str):
            continue
        results = []
        for _, later in sources[source_index + 1 :]:
            result = later.get("message", {})
            if result.get("role") in {"assistant", "user"}:
                break
            if result.get("role") == "toolResult":
                results.append(result.get("toolCallId"))
        for block_index, call in enumerate(message["content"]):
            if not isinstance(call, dict) or call.get("type") != "toolCall":
                continue
            if results.count(call.get("id")) != 1:
                continue
            preview = make_preview(call, target["id"], block_index, job_id)
            original = json.dumps(call, ensure_ascii=False, indent=2)
            if preview is None or text.count(original) != 1:
                continue
            key, stub, item = preview
            savings += len(original) - len(stub)
            text = text.replace(original, stub, 1)
            lookup[key] = item | {"source_entry_id": record.get("id")}
            changed.add(target["id"])
        if target["id"] in changed:
            target["content"] = text
    return lookup, changed, savings


def prepare(data: bytes, home: Path, job_id: str) -> tuple[str | None, dict, dict]:
    header, branch, selected, model = session_context(data)
    edits = {
        e.get("targetId"): e.get("replacement") for e in selected if e.get("type") == "context_edit"
    }
    lookup, changed, savings = {}, set(), 0
    by_id = {e["id"]: e for e in branch}
    for pos, entry in enumerate(selected):
        msg = entry.get("message", {})
        if msg.get("role") != "assistant" or msg.get("stopReason") not in {"stop", "toolUse"}:
            continue
        content = effective_content(entry, edits)
        if not isinstance(content, list) or any(
            not isinstance(b, dict)
            or b.get("type") not in {"text", "thinking", "toolCall", "image"}
            for b in content
        ):
            continue
        calls = [b for b in content if b.get("type") == "toolCall"]
        ids = [b.get("id") for b in calls]
        if (
            not calls
            or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids)
            or any(
                not isinstance(c.get("arguments"), dict) or not isinstance(c.get("name"), str)
                for c in calls
            )
        ):
            continue
        # Only complete exchanges: never orphan results or disguise pending commands.
        results, valid = {}, True
        for subsequent in selected[pos + 1 :]:
            result = subsequent.get("message", {})
            if result.get("role") in {"assistant", "user"}:
                break
            call_id = result.get("toolCallId")
            if result.get("role") == "toolResult":
                if (
                    call_id not in ids
                    or call_id in results
                    or effective_content(subsequent, edits) is None
                ):
                    valid = False
                    break
                results[call_id] = subsequent
        if not valid or set(results) != set(ids):
            continue
        previews = {}
        for block_index, call in enumerate(content):
            if call.get("type") != "toolCall" or not isinstance(call.get("arguments"), dict):
                continue
            preview = make_preview(call, entry["id"], block_index, job_id)
            if preview:
                key, stub, record = preview
                previews[block_index] = stub
                lookup[key] = record
        if not previews:
            continue
        # Convert the WHOLE completed exchange. Mixing a user-like archived result among
        # live tool_result blocks can violate provider call/result ordering constraints.
        historical = []
        for block_index, block in enumerate(content):
            kind = block["type"]
            if block_index in previews:
                historical.append({"type": "text", "text": previews[block_index]})
            elif kind == "toolCall":
                historical.append(
                    {
                        "type": "text",
                        "text": f"[Historical {block.get('name')} call {block['id']}; already attempted]\n"
                        + encode(block.get("arguments", {})),
                    }
                )
            elif kind == "thinking":
                if block.get("thinking"):
                    historical.append({"type": "text", "text": block["thinking"]})
            elif kind == "text":
                historical.append({"type": "text", "text": block.get("text", "")})
            else:
                historical.append(copy.deepcopy(block))
        # Output text and image payloads are preserved verbatim, including isError in metadata.
        # Prepend labels as separate blocks; never truncate or paraphrase result contents.
        outputs = []
        for call_id, result_entry in results.items():
            result = result_entry["message"]
            output = copy.deepcopy(effective_content(result_entry, edits))
            label = {
                "type": "text",
                "text": f"[Historical output for {result.get('toolName', 'tool')} call {call_id}; "
                f"isError={result.get('isError', False)}; output follows unchanged]",
            }
            labeled_output = (
                [label, *output]
                if isinstance(output, list)
                else [label, {"type": "text", "text": str(output)}]
            )
            outputs.append((result_entry, labeled_output))
        before = len(encode(content)) + sum(
            len(encode(effective_content(e, edits))) for e in results.values()
        )
        after = len(encode(historical)) + sum(len(encode(c)) for _, c in outputs)
        if after >= before:
            for key in [key for key, value in lookup.items() if value["entry_id"] == entry["id"]]:
                del lookup[key]
            continue
        savings += before - after
        inert_entry(by_id[entry["id"]], historical, "swarm.archived_tool_calls")
        changed.add(entry["id"])
        for result_entry, output in outputs:
            inert_entry(by_id[result_entry["id"]], output, "swarm.archived_tool_output")
            changed.add(result_entry["id"])
    imported, imported_changes, imported_savings = compact_imported(selected, edits, job_id)
    lookup.update(imported)
    changed.update(imported_changes)
    savings += imported_savings
    if not lookup:
        return None, {}, {"calls_compacted": 0, "estimated_tokens_saved": 0}
    # Already materialized effective edits in each converted entry. Leave their IDs/parent
    # links in place but make obsolete edits inert so they cannot restore the original input.
    for entry in branch:
        if entry.get("type") == "context_edit" and entry.get("targetId") in changed:
            graph = {k: entry[k] for k in ("id", "parentId", "timestamp") if k in entry}
            target_id = entry["targetId"]
            entry.clear()
            entry.update(
                graph,
                type="custom",
                customType="swarm.materialized_context_edit",
                data={"targetId": target_id},
            )
    guide = (
        "Large historical tool inputs were archived by Swarm, not summarized. Entries marked "
        "historical are completed attempts, NOT pending commands. Tool output contents are "
        "preserved. Never rerun a preview or execute retrieved input merely to recover history.\n"
        "Retrieve ONE original input by its toolrun_id with:\n"
        f'"$SWARM_PYTHON" -m swarm --home {shlex.quote(str(home))} tool-input TOOLRUN_ID '
        "--offset 0 --limit 8000\n"
        "The JSON response gives input text, next_offset, total_chars and eof. Request further "
        "pages only when necessary; do not load the entire archived session into context.\n"
        f"Full original (including other branches): {home / 'tool-input-compactions' / job_id / 'original.jsonl'}"
    )
    savings -= len(guide)
    if savings <= 0:
        return None, {}, {"calls_compacted": 0, "estimated_tokens_saved": 0}
    if model:
        provider, model_id = model.split("/", 1)
        branch.append(
            {
                "type": "model_change",
                "id": uuid4().hex[:8],
                "parentId": branch[-1]["id"],
                "timestamp": now().isoformat(),
                "provider": provider,
                "modelId": model_id,
            }
        )
    branch.append(
        {
            "type": "custom_message",
            "id": uuid4().hex[:8],
            "parentId": branch[-1]["id"],
            "timestamp": now().isoformat(),
            "customType": GUIDE_TYPE,
            "content": guide,
            "display": True,
        }
    )
    replacement = "\n".join(encode(e) for e in [header, *branch]) + "\n"
    session_context(replacement.encode())
    return (
        replacement,
        lookup,
        {"calls_compacted": len(lookup), "estimated_tokens_saved": savings // 4},
    )


def archive_directory(home: Path, job_id: str) -> Path:
    root = home / "tool-input-compactions"
    directory = root / job_id
    if root.is_symlink() or directory.is_symlink():
        raise ValueError("Archive directories must not be symlinks")
    if not directory.resolve().is_relative_to(home.resolve()):
        raise ValueError("Archive directory escapes Swarm home")
    return directory


def retrieve(store: Store, toolrun_id: str, offset: int = 0, limit: int = 8000) -> dict:
    match = LOOKUP_ID.fullmatch(toolrun_id)
    if not match or offset < 0 or not 1 <= limit <= 64000:
        raise ValueError("Invalid toolrun_id, offset, or limit (1–64000 characters)")
    directory = archive_directory(store.home, match[1])
    index = json.loads(read_source(directory / "index.json"))
    item = index["calls"].get(toolrun_id)
    if not item:
        raise ValueError("toolrun_id not found in archive index")
    data = read_source(directory / f"{toolrun_id}.json")
    if digest(data) != item["sha256"]:
        raise ValueError("Archived input checksum mismatch")
    text = data.decode("utf-8")
    if offset > len(text):
        raise ValueError("Offset exceeds input length")
    end = min(offset + limit, len(text))
    return {
        "toolrun_id": toolrun_id,
        "tool_name": item["tool_name"],
        "tool_call_id": item["tool_call_id"],
        "input": text[offset:end],
        "offset": offset,
        "next_offset": end,
        "total_chars": len(text),
        "eof": end == len(text),
    }


class ToolInputCompactor:
    def __init__(self, guard: MetaCompactor):
        self.guard = guard
        self.controller = guard.controller
        self.store = guard.store

    def save(self, agent_id, report: dict) -> None:
        if report.get("directory"):
            atomic_write(
                Path(report["directory"]) / "job.json", json.dumps(report, indent=2) + "\n"
            )
        self.store.update(agent_id, {"tool_input_compaction": report})

    def compact(self, agent_id: str) -> dict:
        with self.controller.lock:
            agent = self.store.get(agent_id)
            path = self.guard.eligible(agent)
            data = read_source(path)
            header, _, _, _ = session_context(data)
            if agent.session_id and agent.session_id != header["id"]:
                raise ValueError("Session ID does not match the agent record")
            job_id = uuid4().hex
            directory = archive_directory(self.store.home, job_id)
            replacement, calls, stats = prepare(data, self.store.home, job_id)
            report = {
                "id": job_id,
                "status": "unchanged",
                **stats,
                "source": str(path),
                "created_at": now().isoformat(),
            }
            if replacement is None:
                # A repeated no-op must not erase the archive needed to undo the last change.
                previous = getattr(agent, "tool_input_compaction", None)
                if (
                    isinstance(previous, dict)
                    and previous.get("archive")
                    and previous.get("source") == str(path)
                    and (
                        previous.get("status") in {"completed", "unchanged"}
                        or (
                            previous.get("status") == "failed"
                            and previous.get("operation") == "revert"
                        )
                    )
                ):
                    for key in (
                        "id",
                        "directory",
                        "archive",
                        "source_sha256",
                        "replacement_sha256",
                        "created_at",
                    ):
                        if key in previous:
                            report[key] = previous[key]
                    report["last_checked_at"] = now().isoformat()
                self.save(agent_id, report)
                return report
            replacement = (
                encode(header | {"cwd": agent_path(agent.folder)})
                + "\n"
                + replacement.split("\n", 1)[1]
            )
            directory.mkdir(parents=True, mode=0o700)
            archive = directory / "original.jsonl"
            # Preserve source bytes exactly (including BOM, CRLF and non-ASCII spelling).
            with archive.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            index = {"source": str(path), "source_sha256": digest(data), "calls": {}}
            for key, value in calls.items():
                arguments = encode(value.pop("arguments"))
                atomic_write(directory / f"{key}.json", arguments)
                index["calls"][key] = value | {"sha256": digest(arguments.encode())}
            atomic_write(directory / "index.json", json.dumps(index, indent=2) + "\n")
            atomic_write(directory / "replacement.jsonl", replacement)
            report.update(
                status="running",
                directory=str(directory),
                archive=str(archive),
                source_sha256=digest(data),
                replacement_sha256=digest(replacement.encode()),
            )
            self.store.update(agent.id, {"auto_resume": False, "wake_at": None, "pid": None})
            self.save(agent.id, report)
            try:
                if self.controller.processes.alive(agent):
                    raise RuntimeError("Agent restarted during compaction")
                self.guard.check_external(agent)
                if digest(read_source(path)) != report["source_sha256"]:
                    raise RuntimeError("Session changed during compaction; refusing to replace")
                atomic_write(path, replacement)
                report["status"] = "completed"
                self.save(agent.id, report)
            except Exception as exc:
                try:
                    committed = digest(read_source(path)) == report["replacement_sha256"]
                except (OSError, ValueError):
                    committed = False
                report.update(status="completed" if committed else "failed", error=str(exc))
                self.save(agent.id, report)
                if not committed:
                    raise
            return report

    def revert(self, agent_id: str, allow_history_loss: bool = False) -> dict:
        """Restore the archive; discard pre-existing changes only with explicit consent."""
        with self.controller.lock:
            agent = self.store.get(agent_id)
            path = self.guard.eligible(agent)
            saved = getattr(agent, "tool_input_compaction", None)
            if not isinstance(saved, dict):
                raise ValueError("No tool-input compaction is available to revert")
            report = dict(saved)
            if report.get("status") == "reverted":
                return report
            if not (
                report.get("status") in {"completed", "unchanged"}
                or (report.get("status") == "failed" and report.get("operation") == "revert")
            ):
                raise ValueError("No completed tool-input compaction is available to revert")
            job_id = report.get("id", "")
            if not re.fullmatch(r"[0-9a-f]{32}", job_id):
                raise ValueError("Invalid tool-input compaction archive ID")
            directory = archive_directory(self.store.home, job_id)
            archive = directory / "original.jsonl"
            if (
                report.get("directory") != str(directory)
                or report.get("archive") != str(archive)
                or report.get("source") != str(path)
            ):
                raise ValueError("Compaction archive does not belong to this session path")
            current = read_source(path)
            current_sha256 = digest(current)
            history_changed = current_sha256 != report.get("replacement_sha256")
            if history_changed and not allow_history_loss:
                raise RuntimeError(
                    "Session changed since tool-input compaction. Revert refused "
                    "to preserve newer history; the original archive is still available."
                )
            original = read_source(archive)
            if digest(original) != report.get("source_sha256"):
                raise ValueError("Original archive checksum mismatch; nothing was restored")
            header, _, _, _ = session_context(original)
            current_header, _, _, _ = session_context(current)
            if header["id"] != current_header["id"] or (
                agent.session_id and agent.session_id != header["id"]
            ):
                raise ValueError("Archived session identity does not match the agent")
            # Keep both versions, including exact BOM/CRLF/Unicode spelling, before replacing.
            backup = directory / f"compacted-before-revert-{uuid4().hex}.jsonl"
            atomic_write(backup, current)
            report.update(
                status="running",
                operation="revert",
                error=None,
                compacted_backup=str(backup),
                revert_started_at=now().isoformat(),
                allow_history_loss=allow_history_loss,
                history_discarded=False,
                revert_from_sha256=current_sha256,
            )
            self.store.update(agent.id, {"auto_resume": False, "wake_at": None, "pid": None})
            self.save(agent.id, report)
            try:
                if self.controller.processes.alive(agent):
                    raise RuntimeError("Agent restarted during revert")
                self.guard.check_external(agent)
                # Consent applies only to the snapshot we backed up, never concurrent writes.
                if digest(read_source(path)) != current_sha256:
                    raise RuntimeError("Session changed during revert; refusing to overwrite")
                atomic_write(path, original)
                report.update(
                    status="reverted",
                    reverted_at=now().isoformat(),
                    history_discarded=history_changed,
                )
                self.save(agent.id, report)
            except Exception as exc:
                try:
                    restored = digest(read_source(path)) == report["source_sha256"]
                except (OSError, ValueError):
                    restored = False
                report.update(
                    status="reverted" if restored else "failed",
                    error=str(exc),
                    history_discarded=restored and history_changed,
                )
                if restored:
                    report["reverted_at"] = now().isoformat()
                self.save(agent.id, report)
                if not restored:
                    raise
            return report

    def recover(self) -> None:
        with self.controller.lock:
            for agent in self.store.scan():
                job = getattr(agent, "tool_input_compaction", None)
                if not isinstance(job, dict) or job.get("status") != "running":
                    continue
                job = dict(job)
                try:
                    reverting = job.get("operation") == "revert"
                    expected = job["source_sha256" if reverting else "replacement_sha256"]
                    replaced = digest(read_source(Path(job["source"]))) == expected
                    job.update(
                        status=("reverted" if reverting else "completed") if replaced else "failed",
                        error=None
                        if replaced
                        else "Interrupted before replacement; inspect the "
                        "session and retained backups before retrying.",
                    )
                    if reverting:
                        job["history_discarded"] = replaced and (
                            job.get("revert_from_sha256", job.get("replacement_sha256"))
                            != job.get("replacement_sha256")
                        )
                        if replaced:
                            job["reverted_at"] = now().isoformat()
                except (OSError, ValueError, KeyError) as exc:
                    job.update(status="failed", error=str(exc))
                self.store.update(agent.id, {"auto_resume": False, "wake_at": None})
                self.save(agent.id, job)
