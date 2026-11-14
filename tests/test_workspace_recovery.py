import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from swarm.controller import Controller
from swarm.models import Agent, Status, now
from swarm.orchestration import BRANCH_DRIFT_ERROR, reconcile_child_branch
from swarm.store import Store


class FakeProcesses:
    def __init__(self):
        self.running = set()
        self.launches = []

    def alive(self, agent):
        return str(agent.id) in self.running

    def pid(self, agent):
        return None

    def launch(self, agent, prompt):
        self.launches.append((agent, prompt))
        self.running.add(str(agent.id))
        return 123

    def health(self):
        return {"tmux": True, "pi": True}


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("base\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "base")

    workspace = tmp_path / "trees" / "operational"
    workspace.parent.mkdir()
    old_branch = "agent/operational-commands"
    actual_branch = "agent/888-storage-operations"
    git(repo, "worktree", "add", "-b", old_branch, str(workspace), "main")

    store = Store(tmp_path / "registry")
    processes = FakeProcesses()
    controller = Controller(store, processes)
    parent = store.create(
        Agent(
            name="Parent",
            goal="Coordinate",
            folder=str(repo),
            managed=True,
            auto_resume=False,
        )
    )
    session_id = str(uuid4())
    session = store.sessions_dir / "operational.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "cwd": str(workspace),
            }
        )
        + "\n"
    )
    common = git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    child = store.create(
        Agent(
            id="5e5aef68-0000-4000-8000-000000000000",
            parent_id=parent.id,
            name="Operational",
            goal="Implement storage operations",
            folder=str(workspace),
            repository=str(repo),
            worktree=str(workspace),
            branch=old_branch,
            managed=True,
            auto_resume=False,
            status=Status.idle,
            spawn_state="ready",
            spawn_workspace=str(workspace),
            spawn_branch=old_branch,
            spawn_git_common=common,
            spawn_request_id="operational",
            session_id=session_id,
            session_file=str(session),
            last_error=BRANCH_DRIFT_ERROR,
        )
    )
    git(workspace, "switch", "-c", actual_branch)
    return controller, child, workspace, old_branch, actual_branch


def test_startup_repairs_recorded_branch_transition_without_resuming(recovery):
    controller, child, workspace, old_branch, actual_branch = recovery
    before = controller.store.get(child.id)
    controller.store.update(child.id, {"branch": actual_branch})

    controller.recover()

    repaired = controller.store.get(child.id)
    assert repaired.spawn_branch == actual_branch
    assert repaired.original_spawn_branch == old_branch
    assert {
        "previous_branch": old_branch,
        "current_branch": actual_branch,
        "workspace": str(workspace.resolve()),
        "reason": "recorded branch transition",
    }.items() <= repaired.workspace_recovery.items()
    assert repaired.workspace_recovery["verified_at"]
    assert repaired.workspace_recovery["transitions"][-1]["current_branch"] == actual_branch
    assert repaired.last_error is None
    assert repaired.auto_resume is False
    assert repaired.status == before.status
    assert repaired.spawn_request_id == before.spawn_request_id
    assert repaired.session_id == before.session_id
    assert repaired.session_file == before.session_file
    assert not controller.processes.launches


def test_unrecorded_checkout_transition_is_never_trusted(recovery):
    controller, child, _, old_branch, _ = recovery

    unchanged = reconcile_child_branch(controller, child)

    assert unchanged.branch == old_branch
    assert unchanged.spawn_branch == old_branch
    assert not hasattr(unchanged, "workspace_recovery")
    with pytest.raises(RuntimeError, match="branch changed"):
        controller.wake(str(child.id))
    assert not controller.processes.launches


@pytest.mark.parametrize(
    "guard",
    ["done", "busy", "restart", "stop", "wrong-session", "wrong-cwd", "shared", "external"],
)
def test_recovery_refuses_unsafe_or_ambiguous_records(recovery, guard, monkeypatch):
    controller, child, workspace, old_branch, actual_branch = recovery
    controller.store.update(child.id, {"branch": actual_branch})
    fields = {}
    if guard == "done":
        fields["status"] = Status.done
    elif guard == "external":
        monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [str(workspace)])
    elif guard == "busy":
        fields["meta_compaction"] = {"status": "running"}
    elif guard == "restart":
        fields["restart_job_id"] = str(uuid4())
    elif guard == "stop":
        fields["stop_pending"] = True
    elif guard == "wrong-session":
        fields["session_id"] = str(uuid4())
    elif guard == "wrong-cwd":
        Path(child.session_file).write_text(
            json.dumps({"type": "session", "id": child.session_id, "cwd": str(workspace.parent)})
            + "\n"
        )
    elif guard == "shared":
        controller.store.create(
            Agent(
                name="Shared source",
                goal="Observe",
                folder=str(workspace),
                session_id=child.session_id,
                session_file=child.session_file,
            )
        )
    if fields:
        controller.store.update(child.id, fields)

    result = reconcile_child_branch(controller, controller.store.get(child.id))

    assert result.spawn_branch == old_branch
    assert not hasattr(result, "workspace_recovery")


def test_scheduled_branch_repair_preserves_timer_and_recovery_intent(recovery):
    controller, child, _, _, actual = recovery
    deadline = now()
    controller.store.update(child.id, {"branch": actual, "wake_at": deadline, "auto_resume": False})
    repaired = reconcile_child_branch(controller, controller.store.get(child.id))
    assert repaired.spawn_branch == actual
    assert repaired.wake_at == deadline and repaired.auto_resume is False
    assert not controller.processes.launches


def test_repeated_startup_retains_history_and_never_duplicate_repair(recovery):
    controller, child, _, _, actual = recovery
    controller.store.update(child.id, {"branch": actual})
    source = Path(child.session_file)
    original = source.read_bytes()
    controller.recover()
    record = controller.store.path(child.id).read_bytes()
    controller.recover()
    assert controller.store.path(child.id).read_bytes() == record
    assert source.read_bytes() == original
    assert not controller.processes.launches


def test_startup_resumes_only_previously_authorized_recorded_transition(recovery):
    controller, child, _, _, actual = recovery
    controller.store.update(child.id, {'branch': actual, 'auto_resume': True, 'status': Status.running})
    controller.recover()
    assert controller.store.get(child.id).spawn_branch == actual
    assert len(controller.processes.launches) == 1
    controller.recover()
    assert len(controller.processes.launches) == 1


def test_explicit_wake_reconciles_before_workspace_guard(recovery):
    controller, child, _, _, actual = recovery
    controller.store.update(child.id, {'branch': actual})
    controller.wake(str(child.id), message='')
    assert controller.store.get(child.id).spawn_branch == actual
    assert len(controller.processes.launches) == 1


def test_wrong_repository_or_moved_worktree_is_not_rebound(recovery):
    controller, child, workspace, old, actual = recovery
    controller.store.update(child.id, {'branch': actual, 'spawn_git_common': str(workspace/'different.git')})
    assert reconcile_child_branch(controller, controller.store.get(child.id)).spawn_branch == old
    controller.store.update(child.id, {'spawn_git_common': str(workspace), 'folder': str(workspace.parent)})
    assert reconcile_child_branch(controller, controller.store.get(child.id)).spawn_branch == old
    assert not controller.processes.launches
