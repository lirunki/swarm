"""Discover and adopt dormant *external* Pi sessions without duplicating history."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .controller import git_metadata
from .models import Agent, ImportSource, Status
from .store import Store


class AdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_file: str = Field(min_length=1)
    name: str = ""
    goal: str = ""


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
        config = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
        default = Path(os.environ.get("PI_CODING_AGENT_SESSION_DIR", str(config / "sessions")))
        self.roots = [p.expanduser().resolve() for p in (roots or [default, store.sessions_dir])]

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
            folder = str(Path(header["cwd"]).expanduser().resolve())
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

    def adopt(self, request: AdoptRequest) -> Agent:
        meta = self.metadata(Path(request.session_file))
        # Serialize adoption by the original pi UUID, including across CLI/server processes.
        with self.store.lock(meta["session_id"]):
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
