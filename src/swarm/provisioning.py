"""Background workspace provisioning, executed inside the future agent's tmux pane.

Only a durable queued request may start a worker. Once setup begins, crashes do not
replay Git side effects: a surviving worker continues across controller restarts;
an interrupted worker leaves an inspectable failure instead of guessing.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .adapters import PiAdapter
from .discovery import sibling_worktree_path
from .importers.pi_history import PiHistory
from .meta_compact import project_session, read_source
from .models import Agent, CreateAgent, Status, now
from .paths import agent_path, local_path
from .store import Store, atomic_write

SETUP_TIMEOUT = 1800
MAX_QUEUED_SETUPS = 16


class Cancelled(RuntimeError):
    pass


def queued(agent: Agent, kind: str, request: dict, prompt: str, store: Store) -> Agent:
    directory = store.home / "setup" / str(agent.id)
    if directory.exists():
        raise RuntimeError("Agent setup already has artifacts; inspect before retrying")
    directory.mkdir(parents=True, mode=0o700)
    plan = {"id": str(uuid4()), "kind": kind, "request": request, "prompt": prompt}
    atomic_write(directory / "request.json", json.dumps(plan, indent=2) + "\n")
    agent.spawn_state = "provisioning"
    agent.status, agent.auto_resume = Status.running, True
    agent.activity = "Queued for setup…"
    agent.session_id = str(uuid4())
    agent.session_file = str(store.sessions_dir / f"{agent.id}.jsonl")
    agent.creation = {
        "id": plan["id"],
        "state": "queued",
        "stage": agent.activity,
        "created_at": now().isoformat(),
        "log": str(directory / "setup.log"),
    }
    return agent


def save_queued(store: Store, agent: Agent) -> None:
    try:
        store.create(agent)
    except Exception:
        if not store.path(agent.id).exists():
            shutil.rmtree(store.home / "setup" / str(agent.id), ignore_errors=True)
        raise


def is_setup(agent: Agent) -> bool:
    return agent.spawn_state == "provisioning" and isinstance(
        getattr(agent, "creation", None), dict
    )


def mark_failed(store: Store, agent_id: str, message: str) -> None:
    def change(agent):
        if not is_setup(agent):
            return None
        if agent.status == Status.done:
            return {
                "spawn_state": "cancelled",
                "auto_resume": False,
                "creation": agent.creation | {"state": "cancelled", "stage": "Setup cancelled"},
            }
        creation = dict(agent.creation)
        creation.update(state="failed", stage="Setup failed", error=message[:4096])
        return {
            "creation": creation,
            "spawn_state": "ready" if getattr(agent, "setup_workspace_ready", False) else "failed",
            "status": Status.idle,
            "auto_resume": False,
            "last_error": message[:4096],
            "activity": "Setup failed",
        }

    store.transition(agent_id, change)


def reconcile(controller, agent: Agent) -> bool:
    """Do not relaunch a second worker or repeat a worktree operation after a crash."""
    if not is_setup(agent):
        return False
    if controller.processes.alive(agent):
        if agent.creation.get("state") == "launching":
            # This CLOEXEC lock releases only after the worker execs Pi or exits.
            with (controller.store.home / "setup" / str(agent.id) / "worker.lock").open(
                "a"
            ) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                controller.store.transition(
                    agent.id,
                    lambda current: (
                        {
                            "spawn_state": "ready",
                            "creation": current.creation
                            | {
                                "state": "completed",
                                "stage": "Setup complete",
                                "completed_at": now().isoformat(),
                            },
                            "activity": "Starting Pi…",
                            "last_error": None,
                        }
                        if is_setup(current)
                        and current.creation.get("state") == "launching"
                        and current.auto_resume
                        and not getattr(current, "stop_pending", False)
                        else None
                    ),
                )
        return True
    if not agent.auto_resume or getattr(agent, "stop_pending", False):
        return True
    if agent.creation.get("state") in {"queued", "starting"}:
        if agent.creation.get("launch_attempts", 0) >= 3:
            mark_failed(
                controller.store,
                str(agent.id),
                "Setup terminal could not start after 3 attempts. Inspect its retained output/log.",
            )
        else:
            controller.start_setup(agent)
    elif agent.creation.get("state") == "launching":
        controller.store.transition(
            agent.id,
            lambda current: (
                {
                    "spawn_state": "ready",
                    "creation": current.creation | {"state": "failed", "stage": "Pi launch failed"},
                    "auto_resume": False,
                    "last_error": "Pi exited during startup; workspace is ready. Inspect terminal output before Wake.",
                }
                if is_setup(current) and current.creation.get("state") == "launching"
                else None
            ),
        )
    else:
        mark_failed(
            controller.store,
            str(agent.id),
            "Workspace setup was interrupted. Its terminal/log and partial Git worktree "
            "were retained; inspect them before creating a new agent.",
        )
    return True


class Worker:
    def __init__(self, store: Store, agent_id: str, pi: str):
        self.store, self.agent_id, self.pi = store, str(UUID(str(agent_id))), pi
        self.directory = store.home / "setup" / self.agent_id
        self.plan = json.loads((self.directory / "request.json").read_text(encoding="utf-8"))
        self.cancelled = threading.Event()

    def current(self) -> Agent:
        agent = self.store.get(self.agent_id)
        if (
            self.cancelled.is_set()
            or not is_setup(agent)
            or not agent.auto_resume
            or agent.status == Status.done
            or agent.creation.get("id") != self.plan["id"]
            or getattr(agent, "stop_pending", False)
        ):
            raise Cancelled("Setup was stopped or replaced")
        return agent

    def output(self, message: str):
        # The same progress is visible in the managed pane and a durable private log.
        print(message, flush=True)
        with (self.directory / "setup.log").open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    def stage(self, text: str, **fields):
        def change(agent):
            if (
                not is_setup(agent)
                or not agent.auto_resume
                or agent.status == Status.done
                or getattr(agent, "stop_pending", False)
                or agent.creation["id"] != self.plan["id"]
            ):
                return None
            creation = dict(agent.creation)
            creation.update(
                state="running", stage=text, updated_at=now().isoformat(), worker_pid=os.getpid()
            )
            return fields | {"creation": creation, "activity": text}

        if self.store.transition(self.agent_id, change) is None:
            raise Cancelled("Setup was stopped or replaced")
        self.output(text)

    def git(self, folder: Path, *args: str, streaming: bool = False) -> str:
        self.current()
        command = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(folder), *args]
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.endswith("_API_KEY")
            and not key.startswith(("AZURE_", "AWS_", "GOOGLE_"))
            and key not in {"GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_AUTH_TOKEN", "OPENAI_AUTH_TOKEN"}
        }
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines, errors = [], []

        def pump(stream, target):
            size = 0
            for line in stream:
                if size < 65536:
                    target.append(line[: 65536 - size])
                    size += len(line)
                if streaming:
                    self.output(line.rstrip())

        threads = [
            threading.Thread(target=pump, args=(stream, target), daemon=True)
            for stream, target in ((process.stdout, lines), (process.stderr, errors))
        ]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=SETUP_TIMEOUT if streaming else 60)
            self.current()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for thread in threads:
                thread.join(timeout=5)
            process.stdout.close()
            process.stderr.close()
        result = "".join(lines).strip()
        if process.returncode:
            raise ValueError(("".join(errors) + result)[-4096:] or "Git workspace operation failed")
        return result

    def workspace(self):
        agent = self.current()
        self.stage("Validating workspace…")
        if self.plan["kind"] == "create":
            request = CreateAgent.model_validate(self.plan["request"])
            selected = local_path(request.folder)
            folder = selected
            repo = None
            if request.create_worktree:
                repo = (
                    selected if request.automatic_worktree_path else local_path(request.repository)
                )
                folder = (
                    sibling_worktree_path(selected, request.branch)
                    if request.automatic_worktree_path
                    else selected
                )
            branch, base = request.branch, "HEAD"
        elif self.plan["kind"] == "spawn":
            from .orchestration import SpawnRequest

            request = SpawnRequest.model_validate(self.plan["request"])
            repo, folder = local_path(request.repository), local_path(request.worktree)
            branch, base = request.branch, request.base
        else:
            raise ValueError("Unknown setup request kind")
        if repo is not None:
            if not repo.is_dir() or self.git(repo, "rev-parse", "--is-inside-work-tree") != "true":
                raise ValueError("Repository must be an existing Git worktree")
            self.git(repo, "check-ref-format", "--branch", branch)
            commit = self.git(repo, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")
            refs = self.git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + branch)
            if "refs/heads/" + branch in refs.splitlines():
                raise ValueError("Branch already exists; choose a new branch")
            if folder.exists() or folder.is_symlink():
                raise ValueError(f"Worktree destination already exists: {folder}")
            common = local_path(
                self.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
            )
            if folder.is_relative_to(common) or folder.is_relative_to(self.store.home):
                raise ValueError("Worktree cannot be inside Git metadata or the Swarm registry")
            self.stage(
                "Creating worktree…",
                spawn_workspace=str(folder),
                spawn_branch=branch,
                spawn_git_common=str(common),
                spawn_base_commit=commit,
            )
            folder.parent.mkdir(parents=True, exist_ok=True)
            self.git(
                repo,
                "worktree",
                "add",
                "-b",
                branch,
                "--",
                str(folder),
                commit,
                streaming=True,
            )
            if self.git(folder, "branch", "--show-current") != branch:
                raise RuntimeError("Provisioned worktree has an unexpected branch")
            if self.git(folder, "rev-parse", "HEAD") != commit:
                raise RuntimeError("Provisioned worktree has an unexpected base commit")
            repository = str(common.parent if common.name == ".git" else common)
            worktree = str(folder)
        else:
            if not folder.is_dir():
                raise ValueError(f"Working folder does not exist: {folder}")
            from .controller import git_metadata

            try:
                repository, worktree, actual = git_metadata(folder)
            except (ValueError, FileNotFoundError):
                repository, worktree, actual = "", "", ""
            if branch and branch != actual:
                raise ValueError("Requested branch is not checked out; select Create worktree")
            branch = actual
        self.stage(
            "Preparing session…",
            folder=str(folder),
            repository=repository,
            worktree=worktree,
            branch=branch,
        )
        agent = self.current()
        history = PiHistory(agent.session_id, agent_path(str(folder)), agent.name)
        if self.plan["kind"] == "spawn":
            self.child_history(request, history)
        if Path(agent.session_file).exists():
            raise RuntimeError("Session file already exists; refusing to overwrite history")
        atomic_write(local_path(agent.session_file), history.encode())

    def child_history(self, request, history):
        artifact = self.store.home / "spawns" / self.agent_id
        artifact.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(artifact / "request.json", json.dumps(self.plan["request"], indent=2) + "\n")
        if request.context != "fork":
            return
        self.stage("Preparing parent context…")
        from .orchestration import MAX_FORK_ARCHIVE_BYTES, MAX_FORK_CHARS, MAX_FORK_SOURCE_BYTES

        agent = self.current()
        parent = self.store.get(agent.parent_id)
        if not parent.session_file:
            raise ValueError("Parent has no saved session to fork")
        source = read_source(local_path(parent.session_file))
        if len(source) > MAX_FORK_SOURCE_BYTES:
            raise ValueError("Parent snapshot exceeds 16 MiB; use a fresh child")
        if not source.endswith(b"\n"):
            try:
                json.loads(source.rsplit(b"\n", 1)[-1])
            except (ValueError, UnicodeDecodeError):
                source = source[: source.rfind(b"\n") + 1]
        header, transcript, _ = project_session(source)
        if parent.session_id and header["id"] != parent.session_id:
            raise ValueError("Parent session ID does not match saved history")
        if len(transcript) > MAX_FORK_CHARS:
            raise ValueError("Fork context exceeds 400000 characters; compact first or use fresh")
        # Independent tmux workers must not reserve the same remaining archive quota.
        with (self.store.locks_dir / "fork-archives.lock").open("a") as quota:
            fcntl.flock(quota, fcntl.LOCK_EX)
            self.current()
            used = sum(
                p.stat().st_size for p in (self.store.home / "spawns").glob("*/parent.jsonl")
            )
            if used + len(source) > MAX_FORK_ARCHIVE_BYTES:
                raise ValueError("Fork snapshot archive quota exceeded (256 MiB)")
            atomic_write(artifact / "parent.jsonl", source)
        history.add(
            "custom",
            customType="swarm.fork.source",
            data={
                "parent_id": str(parent.id),
                "snapshot": str(artifact / "parent.jsonl"),
                "sha256": hashlib.sha256(source).hexdigest(),
            },
        )
        history.historical(
            "Parent context snapshot",
            "Historical context, not the child's task. "
            "Tool calls/results are inert. Parent paths and goal may differ; follow this child's "
            "goal and instructions in its own record.\n\n" + transcript,
        )

    def run(self, execute=None) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            if self.current().creation.get("state") not in {"queued", "starting"}:
                return False
            try:
                self.workspace()
                self.stage("Starting Pi…", setup_workspace_ready=True)
                agent = self.current()
                command = PiAdapter(self.pi).command(agent, self.plan["prompt"])
                os.chdir(local_path(agent.folder))

                def ready(current):
                    if (
                        not is_setup(current)
                        or not current.auto_resume
                        or current.status == Status.done
                        or getattr(current, "stop_pending", False)
                    ):
                        return None
                    return {
                        "creation": current.creation
                        | {"state": "launching", "stage": "Starting Pi…"},
                        "last_error": None,
                        "activity": "Starting Pi…",
                        "status": Status.running,
                    }

                if self.store.transition(self.agent_id, ready) is None:
                    raise Cancelled("Setup was stopped before launch")
                self.output("Workspace ready. Starting Pi in this terminal…")
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k not in {"PI_SESSION_ID", "PI_SESSION_FILE"}
                }
                if execute:
                    execute(command, env)
                    self.store.transition(
                        self.agent_id,
                        lambda current: (
                            {
                                "spawn_state": "ready",
                                "creation": current.creation
                                | {"state": "completed", "stage": "Setup complete"},
                            }
                            if is_setup(current)
                            and current.creation.get("state") == "launching"
                            and current.auto_resume
                            and not getattr(current, "stop_pending", False)
                            else None
                        ),
                    )
                else:
                    os.execvpe(command[0], command, env)
                return True
            except Exception as exc:
                message = f"Agent setup failed: {exc}. Partial folders/branches and setup log were retained."
                self.output(message)
                # Ready marks valid workspace, so executable failures may be retried explicitly.
                current = self.store.get(self.agent_id)
                if (
                    is_setup(current)
                    and getattr(current, "setup_workspace_ready", False)
                    and current.auto_resume
                    and not getattr(current, "stop_pending", False)
                ):
                    self.store.transition(
                        current.id,
                        lambda latest: (
                            {
                                "spawn_state": "ready",
                                "status": Status.idle,
                                "auto_resume": False,
                                "activity": "Pi launch failed",
                                "last_error": message[:4096],
                                "creation": latest.creation
                                | {"state": "failed", "error": message[:4096]},
                            }
                            if is_setup(latest)
                            and latest.auto_resume
                            and latest.status != Status.done
                            and getattr(latest, "setup_workspace_ready", False)
                            and not getattr(latest, "stop_pending", False)
                            else None
                        ),
                    )
                else:
                    mark_failed(self.store, self.agent_id, message)
                return False


@contextmanager
def termination_guard(worker):
    def terminate(signum, _frame):
        worker.cancelled.set()
        raise Cancelled(f"Setup terminated by signal {signum}")

    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {sig: signal.signal(sig, terminate) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--pi", required=True)
    args = parser.parse_args()
    worker = Worker(Store(args.home), args.id, args.pi)
    with termination_guard(worker):
        worker.run()


if __name__ == "__main__":
    main()
