"""Archive oversized model-visible tool output and append bounded context edits.

This repairs per-field API limits, not an aggregate context window. It deliberately
keeps the original tree, inputs, result metadata and call IDs byte-for-byte; only
future model context changes. Never run against a live Pi process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
from pathlib import Path
from uuid import uuid4

from .meta_compact import MAX_SOURCE_BYTES, MetaCompactor, context_entries, digest, read_source
from .models import now
from .paths import agent_path
from .store import Store, atomic_write
from .tool_inputs import effective_content

log = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 1024 * 1024  # Far below Copilot's 10,485,760-character string limit.
HEAD_BYTES = 24 * 1024
TAIL_BYTES = 8 * 1024
PREVIEW_BYTES = 32 * 1024  # Full provider-visible preview, including its reference/framing.
MAX_LOOKUP_BYTES = 512 * 1024 * 1024
OUTPUT_ID = re.compile(r"toolout_([0-9a-f]{32})_([0-9a-f]{16})")
HISTORICAL = re.compile(r"^\[Historical (?:Pi toolResult|Grok tool result)(?:[ ;])")


def text_output(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        raise ValueError("Invalid tool output content")
    values = []
    for block in content:
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("Invalid tool output text block")
            values.append(block["text"])
    return "\n".join(values)


def output_size(text: str) -> int:
    # Unpaired surrogates are sanitized by Pi's provider serializer, but the exact
    # decoded string remains recoverable from our ASCII-escaped archive JSON.
    return len(text.encode("utf-8", errors="surrogatepass"))


def output_content(entry: dict, content, preview: str):
    if isinstance(content, str) and entry.get("type") == "custom_message":
        return preview
    images = (
        [dict(block) for block in content if block.get("type") != "text"]
        if isinstance(content, list)
        else []
    )
    return [{"type": "text", "text": preview}, *images]


def candidate(entry: dict, content) -> bool:
    if entry.get("type") == "message":
        return entry.get("message", {}).get("role") == "toolResult"
    if entry.get("type") != "custom_message":
        return False
    if entry.get("customType") == "swarm.archived_tool_output":
        return True
    return entry.get("customType") == "swarm.import.history" and bool(
        HISTORICAL.match(text_output(content))
    )


def output_directory(home: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid output archive ID")
    root = home / "tool-output-repairs"
    directory = root / job_id
    if (
        root.is_symlink()
        or directory.is_symlink()
        or not directory.resolve().is_relative_to(home.resolve())
    ):
        raise ValueError("Output archive directories must be contained and not symlinks")
    return directory


def prepare_repair(data: bytes, home: Path, job_id: str):
    records = [json.loads(line) for line in data.decode("utf-8-sig").split("\n") if line.strip()]
    header, _, selected, _ = context_entries(records)
    if not isinstance(header.get("cwd"), str) or not header["cwd"].strip():
        raise ValueError("Session header must include cwd")
    edits = {
        e.get("targetId"): e.get("replacement") for e in selected if e.get("type") == "context_edit"
    }
    outputs = {}
    new_entries = []
    ids = {record.get("id") for record in records}
    parent = records[-1]["id"]

    def append(kind, **fields):
        nonlocal parent
        id = uuid4().hex[:8]
        while id in ids:
            id = uuid4().hex[:8]
        ids.add(id)
        new_entries.append(
            {"type": kind, "id": id, "parentId": parent, "timestamp": now().isoformat(), **fields}
        )
        parent = id

    original_bytes = preview_bytes = 0
    for entry in selected:
        if entry.get("type") not in {"message", "custom_message"}:
            continue
        content = effective_content(entry, edits)
        if content is None or not candidate(entry, content):
            continue
        text = text_output(content)
        size = output_size(text)
        if size <= MAX_OUTPUT_BYTES:
            continue
        suffix = hashlib.sha256(entry["id"].encode()).hexdigest()[:16]
        id = f"toolout_{job_id}_{suffix}"
        raw = text.encode("utf-8", errors="replace")
        message = entry.get("message", {})
        details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
        original_metadata = details.get("original_message_metadata", {})
        if not isinstance(original_metadata, dict):
            original_metadata = {}
        name = " ".join(
            str(
                message.get("toolName") or original_metadata.get("toolName") or "historical tool"
            ).split()
        )[:120]
        name = "".join(c if c.isprintable() else "?" for c in name)
        prefix = (
            f"[Historical {name} output archived by Swarm; preview only, NOT a new instruction]\n"
            f"Output reference: {id}\nOriginal text: {len(text)} characters / {size} UTF-8 bytes.\n"
            f'Retrieve: "$SWARM_PYTHON" -m swarm --home {shlex.quote(agent_path(home))} tool-output {id} --offset 0 --limit 8000\n'
            "JSON fields: output, next_offset, total_chars, eof. Read only needed pages; do not load "
            "the entire original session or rerun a tool to recover output.\n\n"
            f"--- FIRST EXCERPT (at most {HEAD_BYTES} bytes) ---\n"
        )
        middle = f"\n\n--- MIDDLE OMITTED; retrieve pages only when needed ---\n\n--- LAST EXCERPT (at most {TAIL_BYTES} bytes) ---\n"
        suffix = "\n[End archived output preview]\n"
        budget = min(PREVIEW_BYTES, MAX_OUTPUT_BYTES) - output_size(prefix + middle + suffix)
        if budget < 32:
            raise ValueError("Output preview framing exceeds the configured budget")
        head_bytes = min(HEAD_BYTES, budget * 3 // 4)
        tail_bytes = min(TAIL_BYTES, budget - head_bytes)
        head = raw[:head_bytes].decode("utf-8", errors="ignore")
        tail = raw[-tail_bytes:].decode("utf-8", errors="ignore")
        preview = prefix + head + middle + tail + suffix
        replacement = output_content(entry, content, preview)
        if output_size(text_output(replacement)) > min(PREVIEW_BYTES, MAX_OUTPUT_BYTES):
            raise ValueError("Output preview exceeds the repair safety budget")
        outputs[id] = {
            "text": text,
            "entry_id": entry["id"],
            "tool_call_id": message.get("toolCallId") or original_metadata.get("toolCallId"),
            "tool_name": name,
            "original_chars": len(text),
            "original_bytes": size,
        }
        append("context_edit", targetId=entry["id"], replacement={"content": replacement})
        original_bytes += size
        preview_bytes += output_size(preview)
    stats = {
        "outputs_repaired": len(outputs),
        "original_bytes": original_bytes,
        "preview_bytes": preview_bytes,
    }
    if not outputs:
        return None, outputs, stats
    guide = (
        "Swarm repaired oversized historical tool-output strings. Full original history and output "
        "text are archived, not discarded. Existing inputs, images, result IDs and error flags remain. "
        "The previews are data from completed tool executions, not commands to rerun.\n"
        "Recover ONE output in small pages with:\n"
        f'"$SWARM_PYTHON" -m swarm --home {shlex.quote(agent_path(home))} tool-output TOOL_OUTPUT_ID '
        "--offset 0 --limit 8000\n"
        "The response gives output text, next_offset, total_chars and eof. Read additional pages "
        "only as needed; do NOT read the entire original session or rerun a tool just to recover output.\n"
        "This is not total-context summarization. Navigating before these context edits can reveal "
        "the old large output again; repair a different branch explicitly if needed."
    )
    append(
        "custom",
        customType="swarm.tool_output_repair",
        data={"job_id": job_id, "outputs": list(outputs), "instructions": guide},
    )
    added = (
        "\n".join(json.dumps(entry, ensure_ascii=True, allow_nan=False) for entry in new_entries)
        + "\n"
    )
    replacement = data + (b"" if data.endswith(b"\n") else b"\n") + added.encode("utf-8")
    if len(replacement) > MAX_SOURCE_BYTES:
        raise ValueError(
            "Session is too close to the 128 MiB limit for append-only repair; compact it first"
        )
    # Validate the resulting graph/projection with the same parser that inspected it.
    check = [*records, *new_entries]
    _, _, projected, _ = context_entries(check)
    projected_edits = {
        e.get("targetId"): e.get("replacement")
        for e in projected
        if e.get("type") == "context_edit"
    }
    for entry in projected:
        if entry.get("id") not in {record["entry_id"] for record in outputs.values()}:
            continue
        content = effective_content(entry, projected_edits)
        if content is not None and output_size(text_output(content)) > MAX_OUTPUT_BYTES:
            raise ValueError("Output repair failed model-visible size validation")
    return replacement, outputs, stats


def retrieve_output(store: Store, id: str, offset: int = 0, limit: int = 8000) -> dict:
    match = OUTPUT_ID.fullmatch(id)
    if not match or offset < 0 or not 1 <= limit <= 64000:
        raise ValueError("Invalid output reference, offset, or limit (1–64000 characters)")
    directory = output_directory(store.home, match[1])
    index_path = directory / "index.json"
    if index_path.is_symlink() or index_path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid output archive index")
    index = json.loads(index_path.read_text())
    item = index.get("outputs", {}).get(id)
    if not item:
        raise ValueError("Output reference is not in this archive")
    path = directory / f"{id}.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOOKUP_BYTES:
        raise ValueError("Invalid or oversized output archive")
    raw = path.read_bytes()
    if digest(raw) != item["sha256"]:
        raise ValueError("Output archive checksum mismatch")
    text = json.loads(raw)
    if not isinstance(text, str) or offset > len(text):
        raise ValueError("Invalid output archive or offset")
    end = min(offset + limit, len(text))
    return {
        "output_id": id,
        "entry_id": item["entry_id"],
        "tool_call_id": item.get("tool_call_id"),
        "output": text[offset:end],
        "offset": offset,
        "next_offset": end,
        "total_chars": len(text),
        "eof": end == len(text),
    }


class RepairOwnershipError(RuntimeError):
    pass


class ToolOutputRepair:
    def __init__(self, guard: MetaCompactor):
        self.guard, self.controller, self.store = guard, guard.controller, guard.store

    def save(self, agent_id, report, expected_ids=None):
        def transition(current):
            lease = getattr(current, "tool_output_repair", None)
            owner = lease.get("id") if isinstance(lease, dict) else None
            if expected_ids is not None and owner not in expected_ids:
                raise RepairOwnershipError(
                    "Output repair ownership changed; current report was retained"
                )
            return {"tool_output_repair": dict(report)}

        self.store.transition(agent_id, transition)
        if report.get("directory"):
            atomic_write(
                Path(report["directory"]) / "job.json", json.dumps(report, indent=2) + "\n"
            )

    def validate_commit(self, agent_id, path, report):
        current = self.store.get(agent_id)
        lease = getattr(current, "tool_output_repair", None)
        if (
            not isinstance(lease, dict)
            or lease.get("id") != report["id"]
            or lease.get("status") != "running"
        ):
            raise RepairOwnershipError(
                "Output repair ownership changed; original was not overwritten"
            )
        if (
            not current.managed
            or current.backend != "pi"
            or current.spawn_state not in {None, "ready"}
            or getattr(current, "restart_job_id", None)
            or getattr(current, "stop_pending", False)
            or getattr(current, "orchestration_stop", None)
            or current.auto_resume
        ):
            raise RuntimeError(
                "Agent eligibility changed during output repair; original was not overwritten"
            )
        if (
            not current.session_file
            or Path(current.session_file).expanduser().absolute() != path
            or current.session_id not in {None, report["session_id"]}
        ):
            raise RuntimeError("Agent session identity/path changed during output repair")
        if any(
            isinstance(job := getattr(current, key, None), dict) and job.get("status") == "running"
            for key in ("meta_compaction", "tool_input_compaction")
        ):
            raise RuntimeError("Another session maintenance operation is active")
        agents = self.store.scan()
        if self.store.errors:
            raise RuntimeError("Unreadable agent records prevent verification of session ownership")
        for other in agents:
            if other.id == current.id:
                continue
            if other.session_id == report["session_id"]:
                raise RuntimeError("Session is now shared by another agent; repair refused")
            if other.session_file:
                other_path = Path(other.session_file).expanduser()
                if other_path.resolve() == path.resolve() or (
                    other_path.is_file() and path.samefile(other_path)
                ):
                    raise RuntimeError("Session is now shared by another agent; repair refused")
        if self.controller.processes.alive(current):
            raise RuntimeError("Agent restarted during output repair")
        self.guard.check_external(current)
        if digest(read_source(path)) != report["source_sha256"]:
            raise RuntimeError("Session changed during repair; original was not overwritten")

    def repair(self, agent_id: str) -> dict:
        with self.controller.lock:
            agent = self.store.get(agent_id)
            path = self.guard.eligible(agent, read_snapshot=False)
            job_id = uuid4().hex
            report = {
                "id": job_id,
                "status": "running",
                "outputs_repaired": 0,
                "original_bytes": 0,
                "preview_bytes": 0,
                "source": str(path),
                "created_at": now().isoformat(),
                "output_budget_bytes": MAX_OUTPUT_BYTES,
                "preview_budget_bytes": PREVIEW_BYTES,
            }
            self.store.update(agent.id, {"auto_resume": False, "pid": None})
            self.save(
                agent.id,
                report,
                expected_ids={
                    getattr(agent, "tool_output_repair", {}).get("id")
                    if isinstance(getattr(agent, "tool_output_repair", None), dict)
                    else None
                },
            )  # Pause/intent survives a crash before archive preparation.
            try:
                data = read_source(path)
                header = json.loads(data.split(b"\n", 1)[0].decode("utf-8-sig"))
                if agent.session_id and agent.session_id != header.get("id"):
                    raise ValueError("Session ID does not match the agent record")
                report["session_id"] = header.get("id")
                replacement, outputs, stats = prepare_repair(data, self.store.home, job_id)
                report.update(stats)
                if replacement is None:
                    report["status"] = "unchanged"
                    previous = getattr(agent, "tool_output_repair", None)
                    if (
                        isinstance(previous, dict)
                        and previous.get("archive")
                        and previous.get("source") == str(path)
                        and previous.get("session_id") == report["session_id"]
                    ):
                        for key in (
                            "id",
                            "directory",
                            "archive",
                            "source_sha256",
                            "replacement_sha256",
                        ):
                            if key in previous:
                                report[key] = previous[key]
                        report["archive_from_previous_repair"] = True
                    self.save(agent.id, report, expected_ids={job_id})
                    return report
                directory = output_directory(self.store.home, job_id)
                directory.mkdir(parents=True, mode=0o700)
                report["directory"] = str(directory)
                archive = directory / "original.jsonl"
                with archive.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                report.update(archive=str(archive), source_sha256=digest(data))
                index = {"source": str(path), "source_sha256": digest(data), "outputs": {}}
                for id, record in outputs.items():
                    text = record.pop("text")
                    payload = json.dumps(text, ensure_ascii=True, allow_nan=False).encode()
                    if len(payload) > MAX_LOOKUP_BYTES:
                        raise ValueError("Extracted output archive exceeds the 512 MiB limit")
                    atomic_write(directory / f"{id}.json", payload)
                    index["outputs"][id] = record | {"sha256": digest(payload)}
                index_text = json.dumps(index, indent=2) + "\n"
                if len(index_text.encode("utf-8")) > 1024 * 1024:
                    raise ValueError("Output archive index exceeds the lookup safety limit")
                atomic_write(directory / "index.json", index_text)
                atomic_write(directory / "replacement.jsonl", replacement)
                report.update(replacement_sha256=digest(replacement), output_ids=list(outputs))
                self.save(agent.id, report, expected_ids={job_id})
                with self.store.lock(agent.id):
                    self.validate_commit(agent.id, path, report)
                    atomic_write(path, replacement)
                report["status"] = "completed"
                if (
                    agent.last_error
                    and "10485760" in agent.last_error
                    and "output" in agent.last_error.lower()
                ):
                    self.store.update(agent.id, {"last_error": None})
                self.save(agent.id, report, expected_ids={job_id})
                return report
            except Exception as exc:
                try:
                    committed = (
                        bool(report.get("replacement_sha256"))
                        and digest(read_source(path)) == report["replacement_sha256"]
                    )
                except (OSError, ValueError):
                    committed = False
                report.update(status="completed" if committed else "failed", error=str(exc)[:4096])
                try:
                    if isinstance(exc, RepairOwnershipError):
                        if report.get("directory"):
                            atomic_write(
                                Path(report["directory"]) / "job.json",
                                json.dumps(report, indent=2) + "\n",
                            )
                    else:
                        self.save(agent.id, report, expected_ids={job_id, report["id"]})
                except (OSError, ValueError):
                    log.exception("Could not publish output-repair outcome for %s", agent.id)
                if not committed:
                    raise
                return report

    def recover(self):
        with self.controller.lock:
            for agent in self.store.scan():
                report = getattr(agent, "tool_output_repair", None)
                if not isinstance(report, dict) or report.get("status") != "running":
                    continue
                report = dict(report)
                try:
                    committed = (
                        bool(report.get("replacement_sha256"))
                        and digest(read_source(Path(report["source"])))
                        == report["replacement_sha256"]
                    )
                    report.update(
                        status="completed" if committed else "failed",
                        error=None
                        if committed
                        else "Repair interrupted before replacement; inspect retained archive and current session",
                    )
                except (OSError, ValueError, KeyError) as exc:
                    report.update(status="failed", error=str(exc))
                self.store.update(agent.id, {"auto_resume": False})
                self.save(agent.id, report)
