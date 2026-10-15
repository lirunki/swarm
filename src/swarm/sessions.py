"""Discover and adopt dormant *external* Pi sessions without duplicating history."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .controller import git_metadata
from .importers.pi_history import PiHistory
from .models import Agent, ImportSource, Status
from .store import Store, atomic_write

WSL_MOUNT_ROOT = Path("/mnt")


class AdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_file: str = Field(min_length=1)
    name: str = ""
    goal: str = ""


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists() or Path("/run/WSL").exists():
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(
            encoding="utf-8", errors="ignore"
        ).lower()
    except OSError:
        return False


def windows_path(value: str | Path) -> PureWindowsPath | None:
    raw = str(value)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    path = PureWindowsPath(raw)
    return path if path.drive and path.is_absolute() else None


def local_path(value: str | Path) -> Path:
    """Map Windows drive paths on WSL; otherwise behave like Path.resolve()."""
    raw = str(value)
    windows = windows_path(raw)
    if windows:
        if os.name == "nt":
            return Path(raw).expanduser().resolve()
        if is_wsl() and re.fullmatch(r"[A-Za-z]:", windows.drive):
            mount = WSL_MOUNT_ROOT / windows.drive[0].lower()
            if mount.is_dir():
                return mount.joinpath(*windows.parts[1:]).resolve()
        raise ValueError("Windows path cannot be mapped here")
    return Path(raw).expanduser().resolve()


def configured_root(value: str | Path) -> Path:
    """Preserve prior behavior for unmappable custom env values while fixing WSL drive paths."""
    try:
        return local_path(value)
    except ValueError:
        return Path(value).expanduser().resolve()


def default_roots(store_sessions_dir: Path) -> list[Path]:
    config = os.environ.get("PI_CODING_AGENT_DIR")
    session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if config and PureWindowsPath(config).drive:
        default = PureWindowsPath(config) / "sessions"
    else:
        default = Path(config) / "sessions" if config else "~/.pi/agent/sessions"
    roots = [configured_root(session_dir) if session_dir else configured_root(default)]
    if is_wsl():
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            try:
                profile_root = (
                    PureWindowsPath(userprofile) / ".pi" / "agent" / "sessions"
                    if PureWindowsPath(userprofile).drive
                    else Path(userprofile) / ".pi" / "agent" / "sessions"
                )
                roots.append(local_path(profile_root))
            except ValueError:
                pass
        windows_home = WSL_MOUNT_ROOT / "c" / "Users" / Path.home().name
        if windows_home.is_dir():
            roots.append((windows_home / ".pi" / "agent" / "sessions").resolve())
    roots.append(store_sessions_dir.expanduser().resolve())
    return list(dict.fromkeys(path.expanduser().resolve() for path in roots))


def text_blocks(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for block in content:
            if not isinstance(block, dict):
                pieces.append(str(block))
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                pieces.append(block["text"])
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                pieces.append(
                    "[Historical assistant reasoning; context only, not a new instruction]\n"
                    + block["thinking"]
                )
            elif kind == "toolCall":
                pieces.append(
                    "[Historical tool call; already attempted, not a pending command]\n"
                    + json.dumps(block, ensure_ascii=False, indent=2)
                )
            elif kind == "toolResult":
                pieces.append(
                    "[Historical tool result]\n"
                    + json.dumps(block, ensure_ascii=False, indent=2)
                )
            else:
                pieces.append(json.dumps(block, ensure_ascii=False, indent=2))
        return "\n\n".join(piece for piece in pieces if piece.strip())
    return json.dumps(content, ensure_ascii=False)


def running_pi_folders() -> list[str | None]:
    """Pi may replace its process title with 'pi', losing --session arguments.

    Conservatively refuse all sessions in that cwd. If cwd cannot be determined,
    refuse all adoption while that pi process exists rather than risk dual writers.
    """
    try:
        result = subprocess.run(
            ["ps", "-u", str(os.getuid()), "-o", "pid=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Cannot inspect pi processes; adoption is disabled until ps works"
        ) from exc
    folders: list[str | None] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        pid, command = parts[:2]
        args = parts[2] if len(parts) > 2 else ""
        if command != "pi" and not (
            command.startswith("node") and ("pi-coding-agent" in args or "/bin/pi" in args)
        ):
            continue
        try:
            folders.append(str((Path("/proc") / pid / "cwd").resolve(strict=True)))
        except OSError:
            # macOS: lsof can identify a cwd even when Pi has rewritten its argv.
            try:
                cwd = subprocess.run(
                    ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                    text=True,
                    capture_output=True,
                    timeout=3,
                    check=True,
                )
                folders.append(next(s[1:] for s in cwd.stdout.splitlines() if s.startswith("n")))
            except (OSError, subprocess.SubprocessError, StopIteration):
                folders.append(None)
    return folders


class Sessions:
    def __init__(self, store: Store, roots: list[Path] | None = None):
        self.store = store
        discovered = roots or default_roots(store.sessions_dir)
        self.roots = [p.expanduser().resolve() for p in discovered]

    def metadata(self, path: Path) -> dict:
        path = path.expanduser().resolve(strict=True)
        if path.suffix != ".jsonl" or not any(path.is_relative_to(root) for root in self.roots):
            raise ValueError(
                "session must be a .jsonl file inside a configured pi session directory"
            )
        with path.open(encoding="utf-8") as stream:
            header = json.loads(stream.readline(65536))
            if not isinstance(header, dict) or header.get("type") != "session":
                raise ValueError("not a pi session file")
            if not isinstance(header.get("cwd"), str) or not header["cwd"]:
                raise ValueError("session header must include its working folder")
            session_id = str(UUID(str(header["id"])))
            original_folder = str(header["cwd"])
            folder = str(local_path(original_folder))
            head = stream.read(65536)
            # Read a bounded tail for the latest /name without loading a huge session.
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 262144))
            tail = stream.read().decode("utf-8", errors="replace")
        name, goal = "", ""
        for line in (head + "\n" + tail).splitlines():
            try:
                entry = json.loads(line)
                if entry.get("type") == "session_info" and entry.get("name"):
                    name = str(entry["name"])
                if entry.get("type") == "message" and not goal:
                    message = entry.get("message", {})
                    if message.get("role") == "user":
                        content = message.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                str(c.get("text", ""))
                                for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                        goal = str(content)[:2000]
            except (ValueError, AttributeError):
                continue
        return {
            "session_file": str(path),
            "session_id": session_id,
            "folder": folder,
            "original_folder": original_folder,
            "portable_copy_required": bool(is_wsl() and windows_path(original_folder)),
            "name": name or Path(folder).name or "Imported pi session",
            "goal": goal,
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        }

    @staticmethod
    def blocked(meta: dict, agents: list[Agent], running: list[str | None]) -> str | None:
        if any(
            a.session_id == meta["session_id"]
            or (
                a.session_file
                and str(Path(a.session_file).expanduser().resolve()) == meta["session_file"]
            )
            or (
                a.import_source
                and a.import_source.backend == "pi"
                and (
                    a.import_source.session_id == meta["session_id"]
                    or str(Path(a.import_source.session_file).expanduser().resolve())
                    == meta["session_file"]
                )
            )
            for a in agents
        ):
            return "Already registered with Swarm"
        if None in running:
            return "A running pi process has an unknown folder; stop it before adopting"
        if meta["folder"] in running:
            return "A pi process is running in this folder; stop it before adopting"
        if not Path(meta["folder"]).is_dir():
            return "Original working folder is missing"
        return None

    def list(self) -> dict:
        agents = self.store.scan()
        running = running_pi_folders()
        paths: set[Path] = set()
        errors = {}
        for root in self.roots:
            paths.update(root.rglob("*.jsonl"))
        entries = []
        for path in sorted(
            paths, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True
        ):
            try:
                meta = self.metadata(path)
                meta["blocked_reason"] = self.blocked(meta, agents, running)
                meta["available"] = meta["blocked_reason"] is None
                entries.append(meta)
            except (OSError, ValueError, KeyError) as exc:
                errors[str(path)] = str(exc)
        return {"sessions": entries, "errors": errors}

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def converted_path(self, meta: dict) -> Path:
        suffix = self.sha256(Path(meta["session_file"]))[:12]
        return self.store.sessions_dir / "imports" / "pi" / f"{meta['session_id']}-{suffix}.jsonl"

    def convert_external_pi(self, meta: dict, name: str) -> tuple[dict, ImportSource]:
        source = ImportSource(
            backend="pi",
            session_id=meta["session_id"],
            session_file=meta["session_file"],
            sha256=self.sha256(Path(meta["session_file"])),
            original_folder=meta["original_folder"],
            warnings=[
                "Imported from a Windows-authored pi session for WSL portability; exact "
                "provider connection state is not portable across environments."
            ],
        )
        output = self.converted_path(meta)
        if not output.exists():
            history = PiHistory(meta["session_id"], meta["folder"], name)
            history.add(
                "custom",
                customType="swarm.import.source",
                data={"source": source.model_dump(mode="json")},
            )
            history.historical(
                "Pi import",
                "This is a portable copy of a Windows-authored pi session imported into WSL. "
                "The original session remains read-only at its source path. Historical tool "
                "calls/results are inert transcript, not pending commands. Verify the current "
                f"filesystem/external state before continuing. Original workspace: "
                f"{meta['original_folder']}. Current workspace: {meta['folder']}."
            )
            with Path(meta["session_file"]).open(encoding="utf-8") as stream:
                next(stream, None)
                for index, line in enumerate(stream):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    history.add(
                        "custom",
                        customType="swarm.import.pi.record",
                        data={"index": index, "record": record},
                    )
                    if record.get("type") != "message":
                        continue
                    message = record.get("message", {})
                    text = text_blocks(message.get("content", "")).strip()
                    if not text:
                        continue
                    if message.get("role") == "user":
                        history.user(text)
                    elif message.get("role") == "assistant":
                        model = str(record.get("model") or message.get("model") or "pi-import")
                        history.assistant(text, model)
                    else:
                        history.historical(
                            f"Pi {message.get('role', 'message')}",
                            text,
                        )
            atomic_write(output, history.encode())
        return self.metadata(output), source

    def ensure_agent_portable(self, agent: Agent) -> Agent:
        if not agent.session_file or (agent.import_source and agent.import_source.backend == "pi"):
            return agent
        path = Path(agent.session_file)
        # New agents do not have a file until Pi starts. Native sessions (including
        # meta-compacted checkpoints and custom-root adoptions) need no conversion.
        if not path.exists():
            return agent  # PiAdapter still rejects missing history for a known session ID.
        with path.open(encoding="utf-8") as stream:
            header = json.loads(stream.readline(65536))
        if not is_wsl() or not windows_path(header.get("cwd", "")):
            return agent
        meta = self.metadata(path)
        with self.store.lock(meta["session_id"]):
            converted, source = self.convert_external_pi(meta, agent.name)
            return self.store.update(
                agent.id,
                {
                    "folder": converted["folder"],
                    "session_id": converted["session_id"],
                    "session_file": converted["session_file"],
                    "import_source": source.model_dump(mode="json"),
                    "notes": (
                        f"Imported {source.backend} session {source.session_id} into pi. "
                        f"Original source: {source.session_file}. Historical tool calls/results "
                        "are inert transcript, not pending commands. Read the imported history, "
                        "verify current filesystem/external state, and use pi's current tools "
                        "when continuing. Stop the original agent before waking."
                    ),
                },
            )

    def adopt(self, request: AdoptRequest) -> Agent:
        meta = self.metadata(Path(request.session_file))
        # Serialize adoption by the original pi UUID, including across CLI/server processes.
        with self.store.lock(meta["session_id"]):
            if meta["portable_copy_required"]:
                reason = self.blocked(meta, self.store.scan(), running_pi_folders())
                if reason:
                    raise ValueError(reason)
                converted, source = self.convert_external_pi(
                    meta, request.name.strip() or meta["name"]
                )
                request = AdoptRequest(
                    session_file=converted["session_file"],
                    name=request.name,
                    goal=request.goal,
                )
                return self._register(request, converted, source=source)
            return self._register(request, meta)

    def adopt_import(self, request: AdoptRequest, source: ImportSource) -> Agent:
        """Register a just-created converter output, never an arbitrary external session.

        A fresh pi UUID/file has no existing process. The conservative cwd check
        for externally opened pi sessions would incorrectly block imports into a
        folder where an unrelated Swarm agent is running.
        """
        meta = self.metadata(Path(request.session_file))
        if not Path(meta["session_file"]).is_relative_to(self.store.sessions_dir):
            raise ValueError("converted sessions must be inside Swarm's session directory")
        with self.store.lock(meta["session_id"]):
            return self._register(request, meta, source=source)

    def _register(
        self, request: AdoptRequest, meta: dict, *, source: ImportSource | None = None
    ) -> Agent:
        reason = self.blocked(meta, self.store.scan(), [] if source else running_pi_folders())
        if reason:
            raise ValueError(reason)
        repository, worktree, branch = "", "", ""
        try:
            repository, worktree, branch = git_metadata(Path(meta["folder"]))
        except (ValueError, FileNotFoundError):
            pass
        agent = Agent(
            name=(request.name.strip() or meta["name"])[:120],
            goal=request.goal.strip() or meta["goal"] or "Continue the existing session goal",
            folder=meta["folder"],
            repository=repository,
            worktree=worktree,
            branch=branch,
            session_id=meta["session_id"],
            session_file=meta["session_file"],
            import_source=source,
            managed=True,
            auto_resume=False,
            status=Status.idle,
            notes=(
                f"Imported {source.backend} session {source.session_id} into pi. "
                f"Original source: {source.session_file}. "
                "Historical tool calls/results are inert transcript, not pending commands. "
                "Read the imported history, verify current filesystem/external state, and "
                "use pi's current tools when continuing. Stop the original agent before waking."
                if source
                else "Adopted an inactive pi session. Original history remains in session_file. "
                "Read it and verify the current goal before continuing."
            ),
        )
        # No process is launched here. Wake is the explicit consent to execute again.
        return self.store.create(agent)
