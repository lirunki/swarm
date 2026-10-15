"""Grok Build v1 directory discovery, read-only snapshots and offline pi imports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from ..models import Agent, ImportSource
from ..sessions import AdoptRequest, Sessions
from ..store import Store, atomic_write
from .pi_history import PiHistory

MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_SUMMARY_BYTES = 1024 * 1024
WSL_MOUNT_ROOT = Path("/mnt")


class GrokImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_file: str = Field(min_length=1)  # chat_history.jsonl or its session directory
    name: str = Field(default="", max_length=120)
    goal: str = ""
    folder: str = ""  # override when the old workspace moved or cannot be mapped


def local_path(value: str | Path) -> Path:
    """Map Windows drive paths on WSL; never resolve C:\\... as a POSIX relative path."""
    raw = str(value)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    windows = PureWindowsPath(raw)
    if windows.drive:
        if not windows.is_absolute():
            raise ValueError("Windows path must be absolute")
        if os.name == "nt":
            return Path(raw).expanduser().resolve()
        if sys.platform == "linux" and re.fullmatch(r"[A-Za-z]:", windows.drive):
            mount = WSL_MOUNT_ROOT / windows.drive[0].lower()
            if mount.is_dir():
                return mount.joinpath(*windows.parts[1:]).resolve()
        raise ValueError(
            "Windows path cannot be mapped here; provide a local working-folder override"
        )
    return Path(raw).expanduser().resolve()


def default_roots() -> list[Path]:
    roots = [Path.home() / ".grok" / "sessions"]
    if os.environ.get("GROK_HOME"):
        roots.append(local_path(os.environ["GROK_HOME"]) / "sessions")
    if os.environ.get("USERPROFILE"):
        try:
            roots.append(local_path(os.environ["USERPROFILE"]) / ".grok" / "sessions")
        except ValueError:
            pass
    # WSL commonly has a separate Linux home; inspect only the current user's Windows profile.
    if sys.platform == "linux":
        windows_home = WSL_MOUNT_ROOT / "c" / "Users" / Path.home().name
        if windows_home.is_dir():
            roots.append(windows_home / ".grok" / "sessions")
    return list(dict.fromkeys(path.expanduser().resolve() for path in roots))


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds the {limit // (1024 * 1024)} MiB import limit")
    return data


def reject_constant(value: str):
    raise ValueError(f"non-JSON numeric value {value}")


def parse_json(data: str | bytes):
    return json.loads(data, parse_constant=reject_constant)


def text_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                pieces.append(block["text"])
            else:
                # Keep unsupported attachments visible; never fetch URLs or open referenced files.
                pieces.append(
                    "[Historical content block]\n" + json.dumps(block, ensure_ascii=False)
                )
        return "\n".join(pieces)
    return "[Historical content]\n" + json.dumps(content, ensure_ascii=False)


def prompt_text(record: dict) -> str:
    text = text_content(record.get("content"))
    match = re.fullmatch(r"\s*<user_query>\s*(.*?)\s*</user_query>\s*", text, re.DOTALL)
    return match[1] if match else text


def is_preamble(record: dict) -> bool:
    return bool(record.get("synthetic_reason")) or (
        record.get("type") == "user"
        and "prompt_index" not in record
        and text_content(record.get("content")).lstrip().startswith("<user_info>")
    )


@dataclass
class GrokSnapshot:
    path: Path
    summary_path: Path
    data: bytes
    summary_data: bytes
    summary: dict
    records: list[dict]
    session_id: str
    original_folder: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def pi_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, f"swarm:import:grok:{self.session_id}"))

    def assert_unchanged(self) -> None:
        if (
            read_bounded(self.path, MAX_HISTORY_BYTES) != self.data
            or read_bounded(self.summary_path, MAX_SUMMARY_BYTES) != self.summary_data
        ):
            raise ValueError(
                "Grok session changed during import. Stop Grok and refresh before retrying."
            )

    def metadata(self) -> dict:
        folder, warnings = "", []
        try:
            folder = str(local_path(self.original_folder))
        except ValueError as exc:
            warnings.append(str(exc))
        if not folder or not Path(folder).is_dir():
            warnings.append(
                "Original working folder is unavailable; choose an existing folder before importing."
            )
        if any(
            r["type"] == "system" or r["type"] == "reasoning" or is_preamble(r)
            for r in self.records
        ):
            warnings.append(
                "System prompts, synthetic preambles and encrypted reasoning are archived outside active context."
            )
        if any(
            r["type"] in {"tool_result", "backend_tool_call"} or r.get("tool_calls")
            for r in self.records
        ):
            warnings.append("Tool exchanges are historical text, never executable pi tool calls.")
        if any(
            isinstance(r.get("content"), list)
            and any(not isinstance(c, dict) or c.get("type") != "text" for c in r["content"])
            for r in self.records
        ):
            warnings.append(
                "Non-text attachments are preserved as historical JSON; external files/URLs are not fetched."
            )
        goal = next(
            (
                prompt_text(r)[:2000]
                for r in self.records
                if r["type"] == "user" and not is_preamble(r) and prompt_text(r).strip()
            ),
            "",
        )
        name = (
            self.summary.get("title")
            or self.summary.get("generated_title")
            or self.summary.get("session_summary")
            or Path(folder).name
            or "Imported Grok session"
        )
        unknown = sorted(
            {r["type"] for r in self.records}
            - {"system", "user", "reasoning", "assistant", "tool_result", "backend_tool_call"}
        )
        if unknown:
            warnings.append(
                f"Unknown record types preserved as historical JSON: {', '.join(unknown)}"
            )
        return {
            "session_file": str(self.path),
            "session_id": self.session_id,
            "name": str(name)[:120],
            "goal": goal,
            "folder": folder,
            "original_folder": self.original_folder,
            "folder_exists": bool(folder and Path(folder).is_dir()),
            "num_messages": len(self.records),
            "warnings": warnings,
            "updated_at": datetime.fromtimestamp(self.path.stat().st_mtime, UTC).isoformat(),
        }

    def convert(self, folder: str, name: str, source: ImportSource) -> str:
        history = PiHistory(self.pi_id, folder, name)
        history.add(
            "custom",
            customType="swarm.import.source",
            data={"source": source.model_dump(mode="json"), "summary": self.summary},
        )
        history.historical(
            "Grok import",
            "This is a read-only snapshot of a previous Grok conversation. "
            "The following messages/tool transcripts describe past work; they are not "
            "requests to repeat tool calls. Use pi's current tools and instructions. "
            f"Original workspace: {self.original_folder}. Current workspace: {folder}. "
            "Grok credentials, MCP/tools, memory and system prompt are not installed. "
            "Verify filesystem/external side effects before continuing.",
        )
        for index, record in enumerate(self.records):
            history.add(
                "custom",
                customType="swarm.import.grok.record",
                data={"index": index, "record": record},
            )
            kind = record["type"]
            if kind == "system" or is_preamble(record):
                continue
            if kind == "user":
                text = prompt_text(record)
                if text:
                    history.user(text)
            elif kind == "assistant":
                text = text_content(record.get("content"))
                if record.get("tool_calls"):
                    text += (
                        "\n\n[Historical Grok tool calls — already attempted, not pending commands]\n"
                        + json.dumps(record["tool_calls"], ensure_ascii=False, indent=2)
                    )
                if text.strip():
                    model = str(
                        record.get("model_id")
                        or self.summary.get("current_model_id")
                        or "grok-import"
                    )
                    history.assistant(text, model)
            elif kind == "reasoning":
                summary = record.get("summary", [])
                if isinstance(summary, str):
                    text = summary
                elif isinstance(summary, list):
                    text = "\n".join(
                        part["text"]
                        for part in summary
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
                else:
                    text = ""
                if text:
                    history.historical("Grok reasoning summary", text)
            elif kind == "tool_result":
                history.historical(
                    f"Grok tool result {record.get('tool_call_id', '(unknown call)')}",
                    text_content(record.get("content")),
                )
            else:
                history.historical(f"Grok {kind}", json.dumps(record, ensure_ascii=False, indent=2))
        return history.encode()


class GrokSessions:
    def __init__(self, store: Store, roots: list[Path] | None = None):
        self.store = store
        self.roots = [local_path(p) for p in roots] if roots is not None else default_roots()

    def snapshot(self, value: str | Path) -> GrokSnapshot:
        path = local_path(value)
        if path.is_dir():
            path /= "chat_history.jsonl"
        path = path.resolve(strict=True)
        if path.name != "chat_history.jsonl" or not any(
            path.is_relative_to(root) for root in self.roots
        ):
            raise ValueError(
                "choose chat_history.jsonl inside a configured Grok sessions directory"
            )
        summary_path = (path.parent / "summary.json").resolve(strict=True)
        if not summary_path.is_relative_to(path.parent):
            raise ValueError("Grok summary must be inside its session directory")
        data = read_bounded(path, MAX_HISTORY_BYTES)
        summary_data = read_bounded(summary_path, MAX_SUMMARY_BYTES)
        summary = parse_json(summary_data)
        if not isinstance(summary, dict) or not isinstance(summary.get("info"), dict):
            raise ValueError("Grok summary.json must contain an info object")
        if (
            type(summary.get("chat_format_version", 1)) is not int
            or summary.get("chat_format_version", 1) != 1
        ):
            raise ValueError("unsupported Grok chat_format_version; expected 1")
        session_id = str(UUID(str(summary["info"].get("id", ""))))
        folder = summary["info"].get("cwd") or unquote(path.parent.parent.name)
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("Grok session has no working folder")
        if not Path(folder).is_absolute() and not PureWindowsPath(folder).is_absolute():
            raise ValueError("Grok working folder must be absolute")
        records = []
        for number, line in enumerate(data.decode("utf-8-sig").split("\n"), 1):
            if not line.strip():
                continue
            try:
                record = parse_json(line)
            except ValueError as exc:
                raise ValueError(
                    f"invalid Grok history at line {number}; stop Grok and refresh"
                ) from exc
            if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                raise ValueError(f"Grok history line {number} must be an object with a type")
            records.append(record)
        if not any(r["type"] in {"user", "assistant"} and not is_preamble(r) for r in records):
            raise ValueError("Grok session has no conversation to import")
        snapshot = GrokSnapshot(
            path, summary_path, data, summary_data, summary, records, session_id, folder
        )
        snapshot.assert_unchanged()
        return snapshot

    def duplicate(self, snapshot: GrokSnapshot) -> str | None:
        for agent in self.store.scan():
            source = agent.import_source
            if agent.session_id == snapshot.pi_id or (
                source and source.backend == "grok" and source.session_id == snapshot.session_id
            ):
                return f"Already imported as {agent.name} ({agent.id})"
        return None

    def list(self) -> dict:
        sessions, errors = [], {}
        paths = set()
        for root in self.roots:
            paths.update(root.glob("*/*/chat_history.jsonl"))
        for path in sorted(paths):
            try:
                snapshot = self.snapshot(path)
                meta = snapshot.metadata()
                meta["blocked_reason"] = self.duplicate(snapshot)
                meta["available"] = meta["blocked_reason"] is None
                sessions.append(meta)
            except (OSError, ValueError) as exc:
                errors[str(path)] = str(exc)
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"sessions": sessions, "errors": errors, "roots": [str(p) for p in self.roots]}

    def import_session(self, request: GrokImportRequest) -> Agent:
        snapshot = self.snapshot(request.session_file)
        lock_id = uuid5(NAMESPACE_URL, f"swarm:grok-import-lock:{snapshot.session_id}")
        with self.store.lock(lock_id):
            reason = self.duplicate(snapshot)
            if reason:
                raise ValueError(reason)
            meta = snapshot.metadata()
            folder = local_path(request.folder or snapshot.original_folder)
            if not folder.is_dir():
                raise ValueError(
                    "working folder must exist; provide a local working-folder override"
                )
            name = request.name.strip() or meta["name"]
            source = ImportSource(
                backend="grok",
                session_id=snapshot.session_id,
                session_file=str(snapshot.path),
                sha256=snapshot.sha256,
                original_folder=snapshot.original_folder,
                warnings=meta["warnings"],
            )
            output = self.store.sessions_dir / "imports" / "grok" / f"{snapshot.pi_id}.jsonl"
            if output.exists() or output.is_symlink():
                raise ValueError(
                    "converted pi file already exists; refusing to overwrite saved history"
                )
            content = snapshot.convert(str(folder), name, source)
            snapshot.assert_unchanged()
            atomic_write(output, content)
            try:
                return Sessions(self.store, [self.store.sessions_dir]).adopt_import(
                    AdoptRequest(
                        session_file=str(output),
                        name=name,
                        goal=request.goal.strip() or meta["goal"],
                    ),
                    source,
                )
            except Exception:
                # This invocation owns this new file. Never touch the original Grok history.
                if not any(agent.session_id == snapshot.pi_id for agent in self.store.scan()):
                    output.unlink(missing_ok=True)
                raise
