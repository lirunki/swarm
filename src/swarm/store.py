"""Atomic, locked Markdown registry, shared by the daemon and agent CLI."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from .models import Agent, Status, now
from .paths import local_path

MAX_RECORD_BYTES = 1024 * 1024
log = logging.getLogger(__name__)


def atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def encode(agent: Agent) -> str:
    fields = agent.model_dump(mode="json")
    notes = fields.pop("notes", "")
    return (
        "---\n"
        + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + notes.rstrip()
        + "\n"
    )


def decode(text: str) -> Agent:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("record must start with YAML frontmatter (---)")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("missing closing frontmatter delimiter") from exc
    fields = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(fields, dict):
        raise ValueError("frontmatter must be an object")
    fields["notes"] = "\n".join(lines[end + 1 :]).strip()
    return Agent.model_validate(fields)


class Store:
    def __init__(self, home: Path):
        self.home = local_path(home)
        self.agents_dir = self.home / "agents"
        self.sessions_dir = self.home / "sessions"
        self.locks_dir = self.home / "locks"
        for folder in (self.home, self.agents_dir, self.sessions_dir, self.locks_dir):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache: dict[str, Agent] = {}
        self.errors: dict[str, str] = {}

    def path(self, agent_id: str | UUID) -> Path:
        return self.agents_dir / f"{UUID(str(agent_id))}.md"

    @contextmanager
    def lock(self, agent_id: str | UUID) -> Iterator[None]:
        # Lock a stable sidecar, not the file replaced by atomic_write.
        with (self.locks_dir / f"{UUID(str(agent_id))}.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def get(self, agent_id: str | UUID) -> Agent:
        path = self.path(agent_id)
        if path.is_symlink():
            raise ValueError("agent files must not be symbolic links")
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError("agent record exceeds 1 MiB")
        agent = decode(path.read_text(encoding="utf-8"))
        if str(agent.id) != path.stem:
            raise ValueError("record id must match its UUID filename")
        return agent

    def create(self, agent: Agent) -> Agent:
        with self.lock(agent.id):
            if self.path(agent.id).exists():
                raise ValueError("agent already exists")
            content = encode(agent)
            if len(content.encode("utf-8")) > MAX_RECORD_BYTES:
                raise ValueError("agent record exceeds 1 MiB")
            atomic_write(self.path(agent.id), content)
        return agent

    @staticmethod
    def updated(agent: Agent, fields: dict[str, Any]) -> Agent:
        if "id" in fields:
            raise ValueError("agent id is immutable")
        data = agent.model_dump()
        data.update(fields)
        data["updated_at"] = now()
        updated = Agent.model_validate(data)
        # A changed deadline or explicit cancellation supersedes pending delivery.
        # Re-reporting the same deadline preserves its event ID/backoff. The controller
        # supplies both fields when atomically acknowledging its own event.
        if (
            "wake_at" in fields
            and "wake_delivery" not in fields
            and (updated.wake_at is None or updated.wake_at != agent.wake_at)
        ):
            updated.wake_delivery = None
        if fields.get("status") == Status.done:
            updated.wake_at = None
            if updated.wake_delivery and updated.wake_delivery.state == "pending":
                updated.wake_delivery = None
        return updated

    def pause_mailbox(self, agent: Agent) -> None:
        """Close an exposed inbox during a sleep/stop report, not on a later monitor tick."""
        if not (
            agent.status in {Status.dormant, Status.done}
            or getattr(agent, "stop_pending", False)
            or getattr(agent, "orchestration_stop", None)
        ):
            return
        directory = self.home / "control" / str(agent.id)
        path = directory / "inbox.json"
        if not path.exists():
            return
        try:
            if (
                any(p.is_symlink() for p in (directory.parent, directory, path))
                or path.stat().st_size > 2 * 1024 * 1024
            ):
                raise ValueError("Invalid mailbox path")
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and data.get("agent_id") == str(agent.id)
                and data.get("awake") is True
            ):
                data["awake"] = False
                atomic_write(path, json.dumps(data, ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            log.warning(
                "Could not immediately pause inbox for %s; persisted state is authoritative",
                agent.id,
            )

    def transition(
        self, agent_id: str | UUID, change: Callable[[Agent], dict[str, Any] | None]
    ) -> Agent | None:
        """Compare and update under the same file lock used by the agent reporting CLI.

        The callback must not perform external actions. Returning None leaves the record
        untouched; delivery code can thus acknowledge without erasing a newer schedule.
        """
        with self.lock(agent_id):
            agent = self.get(agent_id)
            fields = change(agent)
            if fields is None:
                return None
            updated = self.updated(agent, fields)
            # Validate and apply timer/status normalization before deciding whether this
            # report changed anything. Timestamps are bookkeeping, not substantive state.
            before = agent.model_dump(exclude={"updated_at"})
            after = updated.model_dump(exclude={"updated_at"})
            if after == before:
                self.pause_mailbox(agent)
                return agent
            content = encode(updated)
            if len(content.encode("utf-8")) > MAX_RECORD_BYTES:
                raise ValueError("agent record exceeds 1 MiB")
            atomic_write(self.path(agent_id), content)
            self.pause_mailbox(updated)
            return updated

    def update(self, agent_id: str | UUID, fields: dict[str, Any]) -> Agent:
        if "id" in fields:
            raise ValueError("agent id is immutable")
        updated = self.transition(agent_id, lambda _: fields)
        assert updated is not None
        return updated

    def remove(self, agent_id: str | UUID) -> Agent:
        """Remove only the dashboard record; session/source files are never touched."""
        with self.lock(agent_id):
            agent = self.get(agent_id)
            self.path(agent_id).unlink()
            directory = os.open(self.agents_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self.cache.pop(str(agent.id), None)
        self.errors.pop(f"{agent.id}.md", None)
        return agent

    def scan(self) -> list[Agent]:
        """Keep last-good records during a bad/partial external edit; report the error."""
        found: set[str] = set()
        errors: dict[str, str] = {}
        for path in sorted(self.agents_dir.glob("*.md")):
            try:
                agent_id = str(UUID(path.stem))
            except ValueError:
                errors[path.name] = "filename must be a UUID.md"
                continue
            found.add(agent_id)
            try:
                self.cache[agent_id] = self.get(agent_id)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                errors[path.name] = str(exc)
        self.cache = {key: value for key, value in self.cache.items() if key in found}
        self.errors = errors
        return sorted(self.cache.values(), key=lambda agent: agent.created_at)

    def snapshot(self, agents: list[Agent]) -> None:
        """Source records survive reboot independently; snapshot is a last-good audit view."""
        content = json.dumps(
            {
                "version": 1,
                "saved_at": now().isoformat(),
                "agents": [a.model_dump(mode="json") for a in agents],
            },
            indent=2,
        )
        atomic_write(self.home / "state.json", content + "\n")

    @contextmanager
    def controller_lock(self) -> Iterator[None]:
        with (self.home / "controller.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"another Swarm controller is using {self.home}") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
