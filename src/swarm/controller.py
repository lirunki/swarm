"""Registry orchestration, reboot recovery and one-shot dormant timers."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

from .adapters import resume_prompt, session_header
from .discovery import sibling_worktree_path
from .files import AgentFiles
from .meta_compact import busy, require_not_busy
from .models import Agent, CreateAgent, Status, now
from .paths import local_path
from .processes import Processes
from .store import Store

log = logging.getLogger(__name__)


def git(folder: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(folder), *args], text=True, capture_output=True, timeout=30, check=False
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def git_metadata(folder: Path) -> tuple[str, str, str]:
    """Return the shared repository, this worktree root, and checked-out branch."""
    worktree = git(folder, "rev-parse", "--show-toplevel")
    common = Path(git(folder, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = folder / common
    common = common.resolve()
    repository = str(common.parent if common.name == ".git" else common)
    return repository, worktree, git(folder, "branch", "--show-current")


class Controller:
    def __init__(self, store: Store, processes: Processes):
        self.store = store
        self.processes = processes
        self.lock = threading.RLock()
        self.files = AgentFiles(store)
        self.state: dict = {}
        self.fingerprint = ""
        self.snapshot_fingerprint = ""

    def create(self, request: CreateAgent) -> Agent:
        with self.lock:
            selected_folder = local_path(request.folder)
            folder = selected_folder
            if request.create_worktree:
                if request.automatic_worktree_path:
                    repo = selected_folder
                    folder = sibling_worktree_path(selected_folder, request.branch)
                else:
                    repo = local_path(request.repository)
                if not repo.is_dir():
                    raise ValueError("repository must be an existing local repository folder")
                if folder.exists() or folder.is_symlink():
                    raise ValueError(f"worktree destination already exists: {folder}")
                git(repo, "check-ref-format", "--branch", request.branch)
                git(repo, "worktree", "add", "-b", request.branch, "--", str(folder))
            elif not folder.is_dir():
                raise ValueError("working folder must exist (or select create worktree)")
            repository, branch, worktree = "", "", ""
            try:
                repository, worktree, branch = git_metadata(folder)
            except (ValueError, FileNotFoundError):
                pass  # research agents need not be in a Git repository
            if request.branch and not request.create_worktree and request.branch != branch:
                raise ValueError(
                    "requested branch is not checked out in this folder; use a worktree"
                )
            agent = Agent(
                name=request.name,
                goal=request.goal,
                folder=str(folder),
                repository=repository,
                branch=branch,
                worktree=worktree,
                model=request.model,
                managed=True,
                status=Status.running,
            )
            agent.session_file = str(self.store.sessions_dir / f"{agent.id}.jsonl")
            self.store.create(agent)
            # Preserve the record even when pi fails, making the error visible and retryable.
            self._launch(
                agent,
                f"Read the swarm skill and your Swarm record. Work toward this goal:\n{agent.goal}",
            )
            self.refresh()
            return self.store.get(agent.id)

    def _launch(self, agent: Agent, prompt: str | None) -> None:
        try:
            from .sessions import Sessions

            require_not_busy(agent)
            if agent.spawn_state not in {None, "ready"}:
                raise RuntimeError("Child workspace setup is incomplete; inspect its spawn error")
            if getattr(agent, "stop_pending", False):
                raise RuntimeError("Agent termination is pending")
            if agent.parent_id:
                from .orchestration import verify_workspace

                verify_workspace(agent)
            agent = Sessions(self.store).ensure_agent_portable(agent)
            pid = self.processes.launch(agent, prompt)
            self.store.update(agent.id, {"pid": pid, "last_error": None})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            log.warning("Cannot launch %s: %s", agent.id, exc)
            self.store.update(agent.id, {"pid": None, "last_error": str(exc)})

    def recover(self) -> None:
        """Only once at controller startup; never automatically retry crashed agents."""
        with self.lock:
            for agent in self.store.scan():
                if getattr(agent, "stop_pending", False):
                    try:
                        self.stop(str(agent.id))
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        log.warning("Pending stop failed: %s", exc)
                    continue
                if (
                    f"{agent.id}.md" in self.store.errors
                    or not agent.managed
                    or not agent.auto_resume
                    or busy(agent)
                    or agent.spawn_state not in {None, "ready"}
                    or agent.status in {Status.done, Status.dormant}
                ):
                    continue
                if self.processes.alive(agent):
                    continue  # server restart != reboot; don't inject a duplicate prompt
                prompt = resume_prompt(agent) if agent.status == Status.running else None
                self._launch(agent, prompt)
            self.refresh()

    def wake(
        self, agent_id: str, message: str | None = None, *, preserve_child_wait: bool = False
    ) -> Agent:
        with self.lock:
            agent = self.store.get(agent_id)
            require_not_busy(agent)
            if not agent.managed:
                raise ValueError("observed agents must be explicitly adopted before waking")
            if agent.spawn_state not in {None, "ready"}:
                raise RuntimeError("Child workspace setup is incomplete; inspect its spawn error")
            if getattr(agent, "orchestration_stop", None) or getattr(agent, "stop_pending", False):
                raise RuntimeError("Child is stopping; wait until it has stopped")
            if agent.parent_id:
                from .orchestration import verify_workspace

                verify_workspace(agent)
            # Omitted instructions retain the safe automated-resume prompt used by
            # timers and API clients. Explicit "" means launch with no user prompt.
            prompt = resume_prompt(agent) if message is None else (message or None)
            agent = self.store.update(
                agent.id,
                {
                    "status": Status.running,
                    "wake_at": None,
                    "wait_reason": "",
                    "auto_resume": True,
                    "last_error": None,
                    "child_wait": getattr(agent, "child_wait", None)
                    if preserve_child_wait
                    else None,
                },
            )
            if self.processes.alive(agent):
                if prompt is not None:
                    try:
                        self.processes.send_prompt(agent, prompt)
                    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        self.store.update(agent.id, {"last_error": str(exc)})
            else:
                self._launch(agent, prompt)
            self.refresh()
            return self.store.get(agent.id)

    def stop(self, agent_id: str) -> Agent:
        with self.lock:
            agent = self.store.get(agent_id)
            require_not_busy(agent)
            if not agent.managed:
                raise ValueError("Swarm cannot stop an externally observed process")
            # Persist intent first; a reboot during stop must not resurrect this process.
            agent = self.store.update(
                agent.id,
                {
                    "auto_resume": False,
                    "wake_at": None,
                    "status": Status.done if agent.status == Status.done else Status.idle,
                    "pid": None,
                    "child_wait": None,
                    "orchestration_stop": None,
                    "stop_pending": True,
                },
            )
            self.processes.stop(agent)
            if self.processes.alive(agent):
                raise RuntimeError("Could not stop the agent; termination remains pending")
            agent = self.store.update(agent.id, {"stop_pending": False})
            self.refresh()
            return agent

    def forget(self, agent_id: str) -> Agent:
        """Stop an inactive owned worker and unregister it, preserving all history."""
        with self.lock:
            agent = self.store.get(agent_id)
            require_not_busy(agent)
            alive = self.processes.alive(agent)
            if alive and agent.status == Status.running:
                raise RuntimeError("Agent is actively running. Stop it before forgetting it.")
            if agent.managed:
                # Persist intent first: failed termination or a reboot must not resume work.
                agent = self.store.update(agent.id, {"auto_resume": False, "wake_at": None})
                if alive:
                    self.processes.stop(agent)
                if self.processes.alive(agent):
                    raise RuntimeError("Could not stop the agent; its dashboard record was kept.")
            # Never terminate a PID from a record; externally observed processes are not owned.
            removed = self.store.remove(agent.id)
            self.refresh()
            return removed

    def tick(self) -> bool:
        with self.lock:
            agents = self.store.scan()
            for agent in agents:
                if f"{agent.id}.md" in self.store.errors or busy(agent):
                    continue
                if getattr(agent, "stop_pending", False):
                    try:
                        self.stop(str(agent.id))
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        log.warning("Pending stop failed: %s", exc)
                    continue
                if (
                    agent.managed
                    and agent.auto_resume
                    and agent.status == Status.dormant
                    and agent.wake_at
                    and agent.wake_at <= now()
                ):
                    self.wake(str(agent.id))
                    continue
                fields = {}
                if agent.managed:
                    alive = self.processes.alive(agent)
                    pid = self.processes.pid(agent) if alive else None
                    if agent.pid != pid:
                        fields["pid"] = pid
                    if not alive and agent.pid and not agent.last_error:
                        fields["last_error"] = (
                            "Agent process exited. Session retained; wake to resume."
                        )
                    session_id = session_header(agent.session_file)
                    if session_id and session_id != agent.session_id:
                        fields["session_id"] = session_id
                if fields:
                    self.store.update(agent.id, fields)
            return self.refresh()

    def refresh(self) -> bool:
        with self.lock:
            agents = self.store.scan()
            entries, errors = [], dict(self.store.errors)
            for agent in agents:
                try:
                    files = self.files.summary(agent.id)
                except (OSError, ValueError):
                    files = {
                        "file_count": 0,
                        "files_version": None,
                        "files_error": "File references could not be read",
                    }
                    errors[f"files/{agent.id}.json"] = files["files_error"]
                entries.append(
                    agent.model_dump(mode="json") | {"alive": self.processes.alive(agent)} | files
                )
            self.state = {
                "agents": entries,
                "errors": errors,
                "home": str(self.store.home),
                "health": self.processes.health(),
            }
            record_fingerprint = json.dumps(
                [a.model_dump(mode="json") for a in agents], sort_keys=True
            )
            if record_fingerprint != self.snapshot_fingerprint:
                self.store.snapshot(agents)
                self.snapshot_fingerprint = record_fingerprint
            fingerprint = json.dumps(self.state, sort_keys=True)
            changed = fingerprint != self.fingerprint
            self.fingerprint = fingerprint
            return changed
