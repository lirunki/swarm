"""Owned child agents, isolated Git worktrees, and durable coordination state.

These checks are cooperative safeguards on a trusted local registry, not an OS sandbox.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent_limits import DEFAULT_MAX_ACTIVE_MANAGED
from .agent_limits import active as active_managed
from .meta_compact import busy, require_not_busy
from .models import Agent, Status, now
from .paths import local_path

if TYPE_CHECKING:
    from .controller import Controller

MAX_ACTIVE_CHILDREN = 4
# Compatibility export only. Runtime enforcement reads agent-limits.json.
MAX_ACTIVE_MANAGED = DEFAULT_MAX_ACTIVE_MANAGED
MAX_CHILDREN = 32
MAX_DEPTH = 3
MAX_FORK_CHARS = 400000
MAX_FORK_SOURCE_BYTES = 16 * 1024 * 1024
MAX_FORK_ARCHIVE_BYTES = 256 * 1024 * 1024
STOP_GRACE_SECONDS = 30


class SpawnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=32000)
    instructions: str = Field(default="", max_length=32000)
    repository: str = Field(min_length=1)
    worktree: str = Field(min_length=1)
    branch: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    base: str = Field(default="HEAD", min_length=1, max_length=240)
    context: Literal["fresh", "fork"] = "fresh"
    model: str | None = Field(default=None, max_length=200)

    @field_validator("repository", "worktree")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            raise ValueError("Workspace paths must be absolute")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("Workspace paths must not contain control characters")
        return value

    @field_validator("name", "goal")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ChildMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32000)
    request_id: UUID = Field(default_factory=uuid4)


class ChildStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = Field(default=False, strict=True)


def git(folder: Path, *args: str) -> str:
    # Provisioning must not execute repository post-checkout hooks.
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(folder), *args],
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git worktree operation failed")
    return result.stdout.strip()


BRANCH_DRIFT_ERROR = "Child worktree branch changed; refusing to launch"


def verify_workspace(agent: Agent) -> None:
    if not agent.parent_id:
        return
    folder = local_path(agent.folder)
    expected = local_path(getattr(agent, "spawn_workspace", agent.folder))
    if folder != expected or local_path(git(folder, "rev-parse", "--show-toplevel")) != expected:
        raise RuntimeError("Child worktree path changed; refusing to launch")
    branch = getattr(agent, "spawn_branch", agent.branch)
    if git(folder, "branch", "--show-current") != branch:
        raise RuntimeError(BRANCH_DRIFT_ERROR)
    common = local_path(git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common != local_path(agent.spawn_git_common):
        raise RuntimeError("Child repository identity changed; refusing to launch")
    registered = [
        local_path(line[9:])
        for line in git(folder, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    if expected not in registered:
        raise RuntimeError("Child worktree is no longer registered")


def _session_identity(agent: Agent) -> tuple[Path, str, Path]:
    if not agent.session_file or not agent.session_id:
        raise ValueError("Child session identity is incomplete")
    path = local_path(agent.session_file)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("Child session is not a regular file")
    with path.open(encoding="utf-8") as stream:
        header = json.loads(stream.readline(65536))
    if not isinstance(header, dict) or header.get("type") != "session":
        raise ValueError("Child session header is invalid")
    if str(header.get("id", "")) != agent.session_id:
        raise ValueError("Child session ID changed")
    cwd = header.get("cwd")
    if not isinstance(cwd, str) or local_path(cwd) != local_path(agent.spawn_workspace):
        raise ValueError("Child session workspace changed")
    return path.resolve(), agent.session_id, local_path(agent.spawn_workspace)


def reconcile_child_branch(controller: Controller, agent: Agent) -> Agent:
    """Accept only a stopped child's already-recorded branch transition.

    This repairs launch identity metadata; it never changes Git state or infers identity
    from the checkout alone. Any failed proof leaves the record untouched.
    """
    store = controller.store
    if (
        not agent.parent_id
        or not agent.managed
        or agent.backend != "pi"
        or agent.spawn_state != "ready"
        or not agent.branch
        or agent.branch == getattr(agent, "spawn_branch", agent.branch)
    ):
        return agent
    try:
        agents, errors = store.scan_with_errors()
        if errors:
            return store.get(agent.id)
        scanned = next((item for item in agents if item.id == agent.id), None)
        if scanned is None:
            return store.get(agent.id)
        source, session_id, expected = _session_identity(scanned)
        for other in agents:
            if other.id == scanned.id:
                continue
            other_source = local_path(other.session_file).resolve() if other.session_file else None
            imported = getattr(other, "import_source", None)
            if (
                (other.session_id and other.session_id == session_id)
                or other_source == source
                or (
                    imported
                    and (
                        imported.session_id == session_id
                        or local_path(imported.session_file).resolve() == source
                    )
                )
            ):
                return store.get(agent.id)

        def repair(current: Agent) -> dict | None:
            if (
                current.id != scanned.id
                or current.session_id != scanned.session_id
                or current.session_file != scanned.session_file
                or not current.managed
                or current.backend != "pi"
                or not current.parent_id
                or current.spawn_state != "ready"
                or current.status == Status.done
                or busy(current)
                or getattr(current, "restart_job_id", None)
                or getattr(current, "stop_pending", False)
                or getattr(current, "orchestration_stop", None)
            ):
                return None
            current_source, current_session, current_expected = _session_identity(current)
            if (current_source, current_session, current_expected) != (
                source,
                session_id,
                expected,
            ):
                return None
            folder = local_path(current.folder)
            if folder != expected or local_path(current.spawn_workspace) != expected:
                return None
            if local_path(git(folder, "rev-parse", "--show-toplevel")) != expected:
                return None
            common = local_path(
                git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")
            )
            if not getattr(current, "spawn_git_common", None) or common != local_path(
                current.spawn_git_common
            ):
                return None
            registered = {
                local_path(line[9:])
                for line in git(folder, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")
            }
            actual = git(folder, "branch", "--show-current")
            previous = getattr(current, "spawn_branch", "")
            if (
                expected not in registered
                or not actual
                or actual != current.branch
                or actual == previous
            ):
                return None
            if controller.processes.alive(current):
                return None
            from .sessions import running_pi_folders

            running = running_pi_folders()
            if any(value is None or local_path(value) == expected for value in running):
                return None
            event = {
                "previous_branch": previous,
                "current_branch": actual,
                "workspace": str(expected),
                "verified_at": now().isoformat(),
                "reason": "recorded branch transition",
            }
            prior = getattr(current, "workspace_recovery", None)
            transitions = list(prior.get("transitions", [])) if isinstance(prior, dict) else []
            recovery = event | {"transitions": (transitions + [event])[-16:]}
            fields = {
                "spawn_branch": actual,
                "original_spawn_branch": getattr(current, "original_spawn_branch", previous),
                "workspace_recovery": recovery,
            }
            if current.last_error == BRANCH_DRIFT_ERROR:
                fields["last_error"] = None
            return fields

        repaired = store.transition(agent.id, repair)
        return repaired if repaired is not None else store.get(agent.id)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        try:
            return store.get(agent.id)
        except (OSError, ValueError):
            return agent


class OwnershipError(ValueError):
    pass


class Orchestrator:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.store = controller.store

    def parent(self, parent_id: str) -> Agent:
        parent = self.store.get(parent_id)
        if not parent.managed:
            raise OwnershipError("Only managed agents can orchestrate children")
        return parent

    def owned(self, parent_id: str, child_id: str) -> Agent:
        self.parent(parent_id)
        child = self.store.get(child_id)
        if str(child.parent_id) != str(parent_id):
            raise OwnershipError("Only a direct child of this parent may be controlled")
        return child

    def describe(self, agent: Agent) -> dict:
        return agent.model_dump(mode="json") | {"alive": self.controller.processes.alive(agent)}

    def children(self, parent_id: str) -> dict:
        with self.controller.lock:
            self.parent(parent_id)
            return {
                "agents": [
                    self.describe(a)
                    for a in self.store.scan()
                    if str(a.parent_id) == str(parent_id)
                ]
            }

    def status(self, parent_id: str, child_id: str) -> dict:
        with self.controller.lock:
            return self.describe(self.owned(parent_id, child_id))

    def depth(self, parent: Agent) -> int:
        seen, depth = set(), 1
        while parent.parent_id:
            if str(parent.id) in seen:
                raise ValueError("Invalid parent chain")
            seen.add(str(parent.id))
            parent = self.store.get(parent.parent_id)
            depth += 1
        return depth

    def active(self, agent: Agent) -> bool:
        return active_managed(agent, self.controller.processes)

    def check_limits(self, parent: Agent, creating: bool = True) -> None:
        agents, record_errors = self.store.scan_with_errors()
        if record_errors:
            raise RuntimeError("Registry contains unreadable records; repair before spawning")
        children = [a for a in agents if a.parent_id == parent.id]
        if self.depth(parent) > MAX_DEPTH:
            raise ValueError(f"Maximum child nesting depth is {MAX_DEPTH}")
        if creating and len(children) >= MAX_CHILDREN:
            raise ValueError(f"Parent already has {MAX_CHILDREN} retained children")
        if sum(self.active(a) for a in children) >= MAX_ACTIVE_CHILDREN:
            raise ValueError(f"Parent already has {MAX_ACTIVE_CHILDREN} active children")
        active_count = sum(self.active(a) for a in agents if a.managed)
        limits = self.controller.agent_limits.get(active_count)
        if active_count >= limits["max_active_managed"]:
            raise ValueError(
                f"Swarm already has {limits['max_active_managed']} active managed agents"
            )

    def spawn(self, parent_id: str, request: SpawnRequest) -> dict:
        with self.controller.lock:
            parent = self.parent(parent_id)
            require_not_busy(parent)
            repo, folder = local_path(request.repository), local_path(request.worktree)
            spec = request.model_dump(mode="json") | {
                "repository": str(repo),
                "worktree": str(folder),
            }
            spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            child_id = uuid5(parent.id, "swarm.spawn:" + request.request_id)
            if self.store.path(child_id).exists():
                child = self.owned(parent_id, str(child_id))
                if getattr(child, "spawn_spec_sha256", None) != spec_hash:
                    raise RuntimeError("Spawn request ID was already used with different options")
                return {"agent": self.describe(child), "reused": True}
            self.check_limits(parent)
            from .provisioning import queued, save_queued

            artifact = self.store.home / "spawns" / str(child_id)
            if artifact.exists() or (self.store.home / "setup" / str(child_id)).exists():
                raise RuntimeError(
                    "Spawn request has existing artifacts; inspect them before retrying"
                )
            child = Agent(
                id=child_id,
                parent_id=parent.id,
                name=request.name,
                goal=request.goal,
                folder=str(folder),
                repository=str(repo),
                worktree=str(folder),
                branch=request.branch,
                model=request.model or parent.model,
                managed=True,
                spawn_request_id=request.request_id,
                spawn_spec_sha256=spec_hash,
                spawn_workspace=str(folder),
                spawn_branch=request.branch,
                spawn_context=request.context,
                spawn_instructions=request.instructions,
                notes=(
                    f"Delegated by parent {parent.id}. Work only in {folder} on {request.branch}. "
                    "Parent uncommitted changes are not copied. Report checkpoints and final results "
                    "in your own record; do not merge, push or delete worktrees unless requested.\n\n"
                    + request.instructions
                ),
            )
            prompt = (
                "You are a new Swarm child agent, not the parent. Read the Swarm skill and "
                "your own record first. Your worktree and branch have already been created. "
                "Do not recreate them or change sessions. Verify your cwd, branch and files. "
                f"Parent: {parent.id}. Goal: {request.goal}\nInstructions: {request.instructions}"
            )
            child = queued(child, "spawn", spec, prompt, self.store)
            save_queued(self.store, child)
            self.controller.queued_state(child)
            return {"agent": child.model_dump(mode="json") | {"alive": False}, "reused": False}

    def message(self, parent_id: str, child_id: str, request: ChildMessage) -> dict:
        with self.controller.lock:
            child = self.owned(parent_id, child_id)
            from .messaging import MessageRequest

            self.controller.messages.send(
                str(child.id),
                MessageRequest(
                    text=request.message, request_id=request.request_id, sender_id=UUID(parent_id)
                ),
            )
            return self.describe(self.store.get(child.id))

    def wake(self, parent_id: str, child_id: str, message: str | None = None) -> dict:
        with self.controller.lock:
            child = self.owned(parent_id, child_id)
            if getattr(child, "orchestration_stop", None):
                raise RuntimeError("Child is stopping; wait until it has stopped")
            if not self.active(child):
                self.check_limits(self.parent(parent_id), creating=False)
            return self.describe(self.controller.wake(child_id, message))

    def stop(self, parent_id: str, child_id: str, force: bool = False) -> dict:
        with self.controller.lock:
            child = self.owned(parent_id, child_id)
            require_not_busy(child)
            if (
                force
                or getattr(child, "restart_job_id", None)
                or child.spawn_state == "provisioning"
                or child.status != Status.running
                or not self.controller.processes.alive(child)
            ):
                self.controller.stop(child_id)
                child = self.store.update(child.id, {"orchestration_stop": None})
            elif not getattr(child, "orchestration_stop", None):
                child = self.store.update(
                    child.id,
                    {
                        "auto_resume": False,
                        "wake_at": None,
                        "orchestration_stop": {
                            "parent_id": parent_id,
                            "requested_at": now().isoformat(),
                            "deadline": (now() + timedelta(seconds=STOP_GRACE_SECONDS)).isoformat(),
                        },
                    },
                )
                try:
                    self.controller.processes.send_prompt(
                        child,
                        "Your parent requested a stop. Save a concise checkpoint in your own "
                        "Swarm record and report idle or done. Do not begin new work. Swarm will "
                        "stop this process after your checkpoint or in 30 seconds; your session "
                        "and worktree will be retained.",
                    )
                except Exception:
                    self.controller.stop(child_id)
                    self.store.update(child.id, {"orchestration_stop": None})
                    raise
            self.controller.refresh()
            return self.describe(self.store.get(child.id))

    def attention(self, child: Agent) -> bool:
        if getattr(child, "restart_job_id", None):
            return False  # A planned process exit is not a child failure/completion event.
        if child.spawn_state == "provisioning" and not child.last_error:
            return False  # Queued setup is legitimate work even before its pane is launched.
        return (
            child.status in {Status.done, Status.waiting, Status.idle}
            or child.spawn_state == "failed"
            or bool(child.last_error)
            or not self.controller.processes.alive(child)
        )

    def wait(self, parent_id: str, child_id: str) -> dict:
        with self.controller.lock:
            parent, child = self.parent(parent_id), self.owned(parent_id, child_id)
            require_not_busy(parent)
            if self.attention(child):
                return {"ready": True, "agent": self.describe(child)}
            if not parent.auto_resume:
                raise RuntimeError(
                    "Parent auto-resume is disabled; explicitly wake it before waiting"
                )
            self.store.update(
                parent.id,
                {
                    "status": Status.dormant,
                    "wake_at": None,
                    "wait_reason": f"Waiting for child {child.id}",
                    "child_wait": {
                        "child_id": str(child.id),
                        "state": "pending",
                        "requested_at": now().isoformat(),
                    },
                },
            )
            self.controller.refresh()
            return {"ready": False, "agent": self.describe(child), "parent_status": "dormant"}

    def tick(self) -> bool:
        with self.controller.lock:
            for agent in self.store.scan():
                if busy(agent) or getattr(agent, "restart_job_id", None):
                    continue
                stop = getattr(agent, "orchestration_stop", None)
                if stop:
                    deadline = datetime.fromisoformat(stop["deadline"])
                    if agent.status != Status.running or now() >= deadline:
                        self.controller.stop(str(agent.id))
                        self.store.update(agent.id, {"orchestration_stop": None})
                wait = getattr(agent, "child_wait", None)
                if not (
                    isinstance(wait, dict)
                    and wait.get("state") in {"pending", "ready"}
                    and (
                        agent.status == Status.dormant
                        or (wait.get("state") == "ready" and agent.status == Status.running)
                    )
                    and agent.auto_resume
                ):
                    continue
                if wait.get("retry_after") and now() < datetime.fromisoformat(wait["retry_after"]):
                    continue
                if wait["state"] == "pending":
                    try:
                        child = self.owned(str(agent.id), wait["child_id"])
                    except FileNotFoundError:
                        child = None
                    if child and not self.attention(child):
                        continue
                    event = {
                        "id": str(uuid4()),
                        "child_id": wait["child_id"],
                        "at": now().isoformat(),
                        "status": str(child.status) if child else "missing",
                        "last_error": child.last_error if child else "Child record was removed",
                    }
                    wait = wait | {"state": "ready", "event": event}
                    self.store.update(agent.id, {"child_wait": wait, "last_child_event": event})
                event = wait["event"]
                try:
                    result = self.controller.wake(
                        str(agent.id),
                        f"Child event {event['id']}: {event['child_id']} needs attention "
                        f"(status: {event['status']}). Ignore this notification if you already handled "
                        "its event ID. Read the child record and inspect its worktree; no automatic "
                        "merge or cleanup was performed.",
                        preserve_child_wait=True,
                    )
                    if result.last_error:
                        raise RuntimeError(result.last_error)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    attempts = wait.get("attempts", 0) + 1
                    wait = wait | {
                        "attempts": attempts,
                        "retry_after": (now() + timedelta(seconds=30)).isoformat(),
                        "state": "failed" if attempts >= 3 else "ready",
                        "error": str(exc),
                    }
                    self.store.update(
                        agent.id,
                        {
                            "child_wait": wait,
                            "last_error": str(exc),
                            "status": Status.waiting if attempts >= 3 else Status.dormant,
                        },
                    )
                else:
                    self.store.update(agent.id, {"child_wait": None})
            return self.controller.refresh()

    def recover(self) -> None:
        with self.controller.lock:
            for agent in self.store.scan():
                if agent.spawn_state == "provisioning" and not getattr(agent, "creation", None):
                    self.store.update(
                        agent.id,
                        {
                            "spawn_state": "failed",
                            "auto_resume": False,
                            "last_error": "Child setup interrupted. Inspect recorded workspace and "
                            "spawn artifacts; setup was not retried automatically.",
                        },
                    )
