"""Registry orchestration, reboot recovery and durable one-shot wake timers."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

from .adapters import resume_prompt, session_header
from .discovery import sibling_worktree_path
from .files import AgentFiles
from .messaging import Messages
from .meta_compact import busy, require_not_busy
from .models import Agent, CreateAgent, Status
from .paths import agent_path, local_path
from .processes import Processes
from .store import Store
from .timers import ScheduledWakes, scheduled

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
        self.wakes = ScheduledWakes(self)
        self.messages = Messages(self)
        self.state: dict = {}
        self.fingerprint = ""
        self.snapshot_fingerprint = ""

    def create(self, request: CreateAgent) -> Agent:
        # No Git subprocess or Pi launch runs on the request path. Persist intent first.
        from .provisioning import MAX_QUEUED_SETUPS, is_setup, queued, save_queued

        with self.lock:
            if sum(is_setup(a) for a in self.store.scan()) >= MAX_QUEUED_SETUPS:
                raise ValueError("Too many agents are setting up; wait for a setup to finish")
            selected = local_path(request.folder)
            folder = (
                sibling_worktree_path(selected, request.branch)
                if (request.create_worktree and request.automatic_worktree_path)
                else selected
            )
            agent = Agent(
                name=request.name,
                goal=request.goal,
                folder=str(folder),
                repository=str(
                    selected if request.automatic_worktree_path else local_path(request.repository)
                )
                if request.create_worktree
                else "",
                worktree=str(folder) if request.create_worktree else "",
                branch=request.branch,
                model=request.model,
                managed=True,
            )
            agent = queued(
                agent,
                "create",
                request.model_dump(mode="json")
                | {
                    "folder": str(selected),
                    "repository": str(local_path(request.repository)) if request.repository else "",
                },
                f"Read the swarm skill and your Swarm record. Work toward this goal:\n{agent.goal}",
                self.store,
            )
            save_queued(self.store, agent)
            self.queued_state(agent)
            return agent

    def queued_state(self, agent: Agent) -> None:
        """Publish a just-committed card without a fleet-wide tmux/filesystem scan."""
        entry = agent.model_dump(mode="json") | {
            "alive": False,
            "file_count": 0,
            "files_version": None,
            "files_error": None,
        }
        entries = [a for a in self.state.get("agents", []) if a["id"] != str(agent.id)]
        self.state = self.state | {
            "agents": [*entries, entry],
            "home": str(self.store.home),
            "errors": self.state.get("errors", {}),
            "health": self.state.get("health", {}),
        }

    def start_setup(self, agent: Agent) -> None:
        from .provisioning import is_setup, mark_failed

        try:
            agent = self.store.transition(
                agent.id,
                lambda current: (
                    {
                        "creation": current.creation
                        | {
                            "state": "starting",
                            "stage": "Starting setup terminal…",
                            "launch_attempts": current.creation.get("launch_attempts", 0) + 1,
                        },
                        "activity": "Starting setup terminal…",
                    }
                    if is_setup(current)
                    and current.creation.get("state") in {"queued", "starting"}
                    and current.auto_resume
                    and not getattr(current, "stop_pending", False)
                    else None
                ),
            )
            if agent is None:
                return
            pid = self.processes.launch_setup(agent)
            # The independent worker may already have progressed or failed; never reset its state.
            self.store.transition(
                agent.id,
                lambda current: (
                    {"pid": pid}
                    if current.spawn_state == "provisioning"
                    and not getattr(current, "stop_pending", False)
                    else None
                ),
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            if agent is not None:
                mark_failed(self.store, str(agent.id), f"Cannot start setup terminal: {exc}")

    def _launch(self, agent: Agent, prompt: str | None) -> bool:
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
            return True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            log.warning("Cannot launch %s: %s", agent.id, exc)
            self.store.update(agent.id, {"pid": None, "last_error": str(exc)[:4096]})
            return False

    def recover(self) -> None:
        """Restore unscheduled sessions once; explicitly scheduled wakes use a durable outbox."""
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
                    or agent.status == Status.done
                ):
                    continue
                from .provisioning import reconcile

                if reconcile(self, agent):
                    continue
                if scheduled(agent):
                    self.wakes.attempt(agent)
                    continue  # Future timers stay asleep; overdue ones are delivered now.
                if agent.spawn_state not in {None, "ready"}:
                    continue
                if agent.status == Status.dormant:
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
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        self.store.update(agent.id, {"last_error": str(exc)[:4096]})
            else:
                self._launch(agent, prompt)
            self.refresh()
            return self.store.get(agent.id)

    def insert_file_reference(self, agent_id: str, file_id: str) -> dict:
        # Original files may live on slow mounts; stat/map outside the global controller lock.
        ref = self.files.reference(agent_id, file_id)
        path = agent_path(ref.path)
        if any(ord(c) < 32 or ord(c) in (127, 0x2028, 0x2029) for c in path):
            raise ValueError("File path contains terminal control characters")
        text = " " + json.dumps(path, ensure_ascii=False) + " "
        with self.lock:
            agent = self.store.get(agent_id)
            require_not_busy(agent)
            if agent.spawn_state not in {None, "ready"}:
                raise RuntimeError("Agent setup is incomplete; references cannot be inserted yet")
            if not agent.managed or not self.processes.alive(agent):
                raise RuntimeError(
                    "Agent is stopped or unmanaged; open/wake it explicitly before Insert"
                )
            if getattr(agent, "stop_pending", False) or getattr(agent, "orchestration_stop", None):
                raise RuntimeError("Agent is stopping; reference was not inserted")
            if not any(
                current.id == ref.id and current.path == ref.path
                for current in self.files.read(agent_id).files
            ):
                raise FileNotFoundError("File reference was removed before insertion")
            self.processes.paste_text(agent, text)
            return {"inserted": True, "file_id": str(ref.id), "path": path, "text": text}

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
                    **(
                        {
                            "spawn_state": "cancelled",
                            "activity": "Setup cancelled",
                            "creation": agent.creation
                            | {"state": "cancelled", "stage": "Setup cancelled"},
                        }
                        if agent.spawn_state == "provisioning" and getattr(agent, "creation", None)
                        else {}
                    ),
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
            self.messages.forget(agent.id)
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
                from .provisioning import reconcile

                if reconcile(self, agent):
                    continue
                if self.wakes.attempt(agent):
                    continue
                fields = {}
                if agent.managed:
                    alive = self.processes.alive(agent)
                    pid = self.processes.pid(agent) if alive else None
                    if agent.pid != pid:
                        fields["pid"] = pid
                    if not alive and agent.pid and not agent.last_error and not scheduled(agent):
                        fields["last_error"] = (
                            "Agent process exited. Session retained; wake to resume."
                        )
                    session_id = session_header(agent.session_file)
                    if session_id and session_id != agent.session_id:
                        fields["session_id"] = session_id
                if fields:
                    agent = self.store.update(agent.id, fields)
                try:
                    self.messages.tick(agent)
                except (OSError, ValueError, RuntimeError):
                    log.exception("Mailbox remains queued; could not pump agent %s", agent.id)
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
                try:
                    messages = self.messages.summary(agent.id)
                except (OSError, ValueError):
                    messages = {
                        "message_count": 0,
                        "messages_revision": None,
                        "messages_error": "Mailbox could not be read",
                    }
                    errors[f"messages/{agent.id}.json"] = messages["messages_error"]
                entries.append(
                    agent.model_dump(mode="json")
                    | {"alive": self.processes.alive(agent)}
                    | files
                    | messages
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
