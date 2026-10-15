import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.importers.pi_history import PiHistory
from swarm.models import Agent, Status, now
from swarm.orchestration import ChildMessage, Orchestrator, OwnershipError, SpawnRequest
from swarm.server import create_app


class FakeProcesses:
    def __init__(self):
        self.running, self.launches, self.messages = set(), [], []

    def alive(self, agent):
        return str(agent.id) in self.running

    def pid(self, agent):
        return 123 if self.alive(agent) else None

    def launch(self, agent, prompt):
        assert Path(agent.folder).is_dir() and Path(agent.session_file).is_file()
        self.running.add(str(agent.id))
        self.launches.append((agent, prompt))
        return 123

    def stop(self, agent):
        self.running.discard(str(agent.id))

    def send_prompt(self, agent, prompt):
        self.messages.append((agent.id, prompt))

    def health(self):
        return {"tmux": True, "pi": True}


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "code.txt").write_text("committed version\n")
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "base")
    app = create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    ctl = app.state.controller
    ctl.processes = FakeProcesses()
    history = PiHistory(str(uuid4()), str(repo), "Parent")
    history.user("Parent instructions and useful context. " * 20)
    path = ctl.store.sessions_dir / "parent.jsonl"
    path.write_text(history.encode())
    parent = ctl.store.create(
        Agent(
            name="Parent",
            goal="Coordinate",
            folder=str(repo),
            managed=True,
            status="running",
            session_file=str(path),
            session_id=history.entries[0]["id"],
        )
    )
    ctl.processes.running.add(str(parent.id))
    request = SpawnRequest(
        request_id="task-1",
        name="Child",
        goal="Implement feature",
        repository=str(repo),
        worktree=str(tmp_path / "trees" / "child"),
        branch="feature/child",
        base="main",
        instructions="Add tests.",
    )
    return app, parent, request, repo


def spawn(setup, **updates):
    app, parent, request, _ = setup
    req = SpawnRequest(**(request.model_dump() | updates))
    result = app.state.orchestration.spawn(str(parent.id), req)
    return app.state.controller.store.get(result["agent"]["id"])


def test_spawn_provisions_before_launch_and_keeps_parent_dirty_changes_separate(setup):
    app, parent, request, repo = setup
    original_session = Path(parent.session_file).read_bytes()
    commit = git(repo, "rev-parse", "HEAD")
    (repo / "code.txt").write_text("uncommitted parent version")
    (repo / "untracked.txt").write_text("parent only")
    child = spawn(setup)
    assert child.parent_id == parent.id and child.id != parent.id
    assert child.session_id != parent.session_id and child.spawn_state == "ready"
    assert git(Path(child.folder), "branch", "--show-current") == request.branch
    assert git(Path(child.folder), "rev-parse", "HEAD") == commit
    assert (Path(child.folder) / "code.txt").read_text() == "committed version\n"
    assert not (Path(child.folder) / "untracked.txt").exists()
    assert git(repo, "branch", "--show-current") == "main"
    assert Path(parent.session_file).read_bytes() == original_session
    launched, prompt = app.state.controller.processes.launches[0]
    assert launched.folder == child.folder and "already been created" in prompt
    assert launched.spawn_instructions == "Add tests."
    entries = [json.loads(line) for line in Path(child.session_file).read_text().splitlines()]
    assert not any(e.get("type") == "custom_message" for e in entries)


def test_idempotent_spawn_repeated_concurrently_never_launches_twice(setup):
    app, parent, request, _ = setup
    service = app.state.orchestration
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: service.spawn(str(parent.id), request), range(4)))
    assert len({r["agent"]["id"] for r in results}) == 1
    assert sum(not r["reused"] for r in results) == 1
    assert len(app.state.controller.processes.launches) == 1
    with pytest.raises(RuntimeError, match="different options"):
        service.spawn(str(parent.id), request.model_copy(update={"goal": "Different task"}))


@pytest.mark.parametrize("invalid", ["destination", "branch", "base", "metadata"])
def test_invalid_workspace_requests_have_no_child_or_git_side_effects(setup, invalid):
    app, parent, request, repo = setup
    req = request.model_dump()
    if invalid == "destination":
        Path(request.worktree).mkdir(parents=True)
    elif invalid == "branch":
        req["branch"] = "main"
    elif invalid == "base":
        req["base"] = "missing-reference"
    else:
        req["worktree"] = str(repo / ".git" / "nested")
    with pytest.raises(ValueError):
        app.state.orchestration.spawn(str(parent.id), SpawnRequest(**req))
    assert len(app.state.controller.store.scan()) == 1
    assert not app.state.controller.processes.launches
    assert git(repo, "branch", "--list", "feature/child") == ""


def test_explicit_base_ref_and_hooks_disabled(setup):
    app, _, _, repo = setup
    base = git(repo, "rev-parse", "HEAD")
    (repo / "code.txt").write_text("new commit")
    git(repo, "commit", "-am", "second")
    hook = repo / ".git" / "hooks" / "post-checkout"
    marker = repo / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    child = spawn(setup, base=base)
    assert git(Path(child.folder), "rev-parse", "HEAD") == base
    assert not marker.exists()


def test_fork_is_portable_new_identity_and_parent_snapshot_is_preserved(setup):
    app, parent, _, _ = setup
    source = Path(parent.session_file).read_bytes()
    child = spawn(setup, context="fork")
    assert Path(parent.session_file).read_bytes() == source
    path = app.state.controller.store.home / "spawns" / str(child.id) / "parent.jsonl"
    assert path.read_bytes() == source
    entries = [json.loads(line) for line in Path(child.session_file).read_text().splitlines()]
    assert entries[0]["id"] == child.session_id != parent.session_id
    assert "Parent instructions" in next(
        e["content"] for e in entries if e["type"] == "custom_message"
    )
    assert not any(e.get("message", {}).get("role") == "toolResult" for e in entries)


def test_fork_ignores_only_unfinished_final_line_and_rejects_oversize(setup, monkeypatch):
    app, parent, _, _ = setup
    source = Path(parent.session_file)
    original = source.read_bytes()
    source.write_bytes(original + b'{"unfinished":')
    child = spawn(setup, context="fork")
    snapshot = app.state.controller.store.home / "spawns" / str(child.id) / "parent.jsonl"
    assert snapshot.read_bytes() == original
    monkeypatch.setattr("swarm.orchestration.MAX_FORK_CHARS", 10)
    with pytest.raises(ValueError, match="Fork context exceeds"):
        spawn(
            setup,
            request_id="oversize",
            branch="feature/two",
            worktree=str(Path(child.folder).parent / "two"),
            context="fork",
        )


def test_failed_provisioning_and_restart_do_not_repeat_git_or_launch(setup, monkeypatch):
    import swarm.orchestration as module

    app, parent, request, _ = setup
    real_git = module.git

    def fail_after_git(folder, *args):
        result = real_git(folder, *args)
        if args[:2] == ("worktree", "add"):
            raise RuntimeError("interrupted after worktree creation")
        return result

    monkeypatch.setattr(module, "git", fail_after_git)
    child = spawn(setup)
    assert child.spawn_state == "failed" and not child.auto_resume
    assert Path(child.folder).is_dir() and not app.state.controller.processes.launches
    assert app.state.orchestration.spawn(str(parent.id), request)["reused"]
    with pytest.raises(RuntimeError, match="setup is incomplete"):
        app.state.controller.wake(str(child.id))
    app.state.controller.store.update(
        child.id, {"spawn_state": "provisioning", "auto_resume": True}
    )
    app.state.orchestration.recover()
    app.state.controller.recover()
    assert app.state.controller.store.get(child.id).spawn_state == "failed"
    assert not app.state.controller.processes.launches


def test_launch_failure_keeps_ready_worktree_and_requires_explicit_wake(setup, monkeypatch):
    app, parent, request, _ = setup
    real_launch = app.state.controller.processes.launch
    monkeypatch.setattr(
        app.state.controller.processes,
        "launch",
        lambda *args: (_ for _ in ()).throw(RuntimeError("Pi unavailable")),
    )
    child = spawn(setup)
    assert child.spawn_state == "ready" and child.last_error and not child.auto_resume
    assert app.state.orchestration.spawn(str(parent.id), request)["reused"]
    monkeypatch.setattr(app.state.controller.processes, "launch", real_launch)
    assert app.state.orchestration.wake(str(parent.id), str(child.id))["alive"]


def test_direct_child_ownership_and_api_controls(setup):
    app, parent, request, repo = setup
    other = app.state.controller.store.create(
        Agent(name="Other", goal="Other", folder=str(repo), managed=True)
    )
    with TestClient(app) as client:
        base = f"/api/agents/{parent.id}/children"
        result = client.post(base, json=request.model_dump())
        assert result.status_code == 201, result.text
        child_id = result.json()["agent"]["id"]
        assert len(client.get(base).json()["agents"]) == 1
        assert client.get(base + "/" + child_id).json()["alive"]
        assert client.get(f"/api/agents/{other.id}/children/{child_id}").status_code == 403
        assert client.post(base + f"/{other.id}/stop", json={"force": True}).status_code == 403
        assert (
            client.post(
                base + f"/{child_id}/message", json={"message": "Add edge cases"}
            ).status_code
            == 200
        )
        assert client.post(base + f"/{child_id}/stop", json={"force": True}).status_code == 200
        assert not client.get(base + "/" + child_id).json()["alive"]
        assert client.post(base + f"/{child_id}/message", json={"message": "Hi"}).status_code == 409
        assert client.post(base + f"/{child_id}/wake", json={}).json()["alive"]
        assert (
            client.post(
                base, json=request.model_dump(), headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
    assert Path(request.worktree).is_dir()


@pytest.mark.parametrize("finish", ["checkpoint", "timeout"])
def test_cooperative_stop_preserves_workspace_and_checkpoint(setup, finish):
    app, parent, _, _ = setup
    child = spawn(setup)
    svc, ctl = app.state.orchestration, app.state.controller
    report = svc.stop(str(parent.id), str(child.id))
    assert report["alive"] and not report["auto_resume"] and report["orchestration_stop"]
    assert "Save a concise checkpoint" in ctl.processes.messages[-1][1]
    if finish == "checkpoint":
        ctl.store.update(child.id, {"status": "idle", "notes": "Saved checkpoint"})
    else:
        stop = report["orchestration_stop"] | {
            "deadline": (now() - timedelta(seconds=1)).isoformat()
        }
        ctl.store.update(child.id, {"orchestration_stop": stop})
    svc.tick()
    assert not ctl.processes.alive(child) and Path(child.folder).is_dir()
    assert not ctl.store.get(child.id).auto_resume


def test_branch_switch_blocks_wake_and_failed_stop_is_retried(setup, monkeypatch):
    app, parent, _, _ = setup
    child = spawn(setup)
    ctl = app.state.controller
    original_stop = ctl.processes.stop
    monkeypatch.setattr(ctl.processes, "stop", lambda a: None)
    with pytest.raises(RuntimeError, match="termination remains pending"):
        ctl.stop(str(child.id))
    assert ctl.store.get(child.id).stop_pending
    monkeypatch.setattr(ctl.processes, "stop", original_stop)
    ctl.recover()
    assert not ctl.store.get(child.id).stop_pending and not ctl.processes.alive(child)
    git(Path(child.folder), "switch", "-c", "unexpected")
    with pytest.raises(RuntimeError, match="branch changed"):
        app.state.orchestration.wake(str(parent.id), str(child.id))


def test_wait_notifies_once_and_operator_stop_cancels_wait(setup):
    app, parent, request, _ = setup
    child = spawn(setup)
    svc, ctl = app.state.orchestration, app.state.controller
    assert not svc.wait(str(parent.id), str(child.id))["ready"]
    assert ctl.store.get(parent.id).status == Status.dormant
    ctl.store.update(child.id, {"status": "done"})
    svc.tick()
    notified = ctl.store.get(parent.id)
    assert notified.status == Status.running and notified.child_wait is None
    event_id = notified.last_child_event["id"]
    assert event_id in ctl.processes.messages[-1][1]
    count = len(ctl.processes.messages)
    svc.tick()
    assert len(ctl.processes.messages) == count
    ctl.store.update(child.id, {"status": "running"})
    svc.wait(str(parent.id), str(child.id))
    ctl.stop(str(parent.id))
    ctl.store.update(child.id, {"status": "done"})
    svc.tick()
    assert not ctl.processes.alive(parent) and len(ctl.processes.messages) == count


def test_wait_event_survives_crash_before_delivery(setup, monkeypatch):
    app, parent, _, _ = setup
    child = spawn(setup)
    svc, ctl = app.state.orchestration, app.state.controller
    svc.wait(str(parent.id), str(child.id))
    ctl.store.update(child.id, {"status": "waiting"})
    wake = ctl.wake
    monkeypatch.setattr(ctl, "wake", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        svc.tick()
    pending = ctl.store.get(parent.id).child_wait
    assert pending["state"] == "ready"
    monkeypatch.setattr(ctl, "wake", wake)
    restarted = Orchestrator(ctl)
    restarted.recover()
    restarted.tick()
    assert pending["event"]["id"] in ctl.processes.messages[-1][1]
    assert ctl.store.get(parent.id).child_wait is None


def test_limits_and_retained_child_limit_does_not_block_wake(setup, monkeypatch):
    app, parent, request, _ = setup
    child = spawn(setup)
    ctl = app.state.controller
    monkeypatch.setattr("swarm.orchestration.MAX_ACTIVE_CHILDREN", 1)
    with pytest.raises(ValueError, match="active children"):
        spawn(
            setup,
            request_id="second",
            branch="second",
            worktree=str(Path(request.worktree).parent / "second"),
        )
    ctl.stop(str(child.id))
    monkeypatch.setattr("swarm.orchestration.MAX_CHILDREN", 1)
    assert app.state.orchestration.wake(str(parent.id), str(child.id))["alive"]
    with pytest.raises(ValueError, match="retained children"):
        spawn(
            setup,
            request_id="second",
            branch="second",
            worktree=str(Path(request.worktree).parent / "second"),
        )


def test_wrong_parent_cannot_control_child_directly(setup):
    app, parent, _, repo = setup
    child = spawn(setup)
    other = app.state.controller.store.create(
        Agent(name="Other", goal="Other", folder=str(repo), managed=True)
    )
    with pytest.raises(OwnershipError):
        app.state.orchestration.message(str(other.id), str(child.id), ChildMessage(message="No"))


def test_placeholder_tmux_session_is_not_considered_a_live_agent(setup, monkeypatch):
    from swarm.processes import Processes

    app, parent, _, _ = setup
    proc = Processes(app.state.controller.store)
    monkeypatch.setattr("swarm.processes.shutil.which", lambda _: "/bin/tmux")
    monkeypatch.setattr(
        proc, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, '0|"sleep 86400"\n', "")
    )
    assert not proc.alive(parent)
    monkeypatch.setattr(
        proc, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, "0|exec pi\n", "")
    )
    assert proc.alive(parent)


@pytest.mark.parametrize("limit", ["depth", "global", "fork-source", "fork-total"])
def test_nesting_global_and_fork_storage_limits_fail_before_side_effects(setup, monkeypatch, limit):
    app, parent, request, _ = setup
    if limit == "depth":
        monkeypatch.setattr("swarm.orchestration.MAX_DEPTH", 0)
    elif limit == "global":
        monkeypatch.setattr("swarm.orchestration.MAX_ACTIVE_MANAGED", 1)
    elif limit == "fork-source":
        monkeypatch.setattr("swarm.orchestration.MAX_FORK_SOURCE_BYTES", 1)
    else:
        monkeypatch.setattr("swarm.orchestration.MAX_FORK_ARCHIVE_BYTES", 1)
    with pytest.raises(ValueError):
        spawn(setup, context="fork" if limit.startswith("fork") else "fresh")
    assert not Path(request.worktree).exists()
    assert len(app.state.controller.store.scan()) == 1
    assert not app.state.controller.processes.launches


def test_wait_delivery_failure_retries_same_event_and_preserves_operator_stop(setup, monkeypatch):
    app, parent, _, _ = setup
    child = spawn(setup)
    svc, ctl = app.state.orchestration, app.state.controller
    svc.wait(str(parent.id), str(child.id))
    ctl.store.update(child.id, {"status": "done"})
    send = ctl.processes.send_prompt
    monkeypatch.setattr(
        ctl.processes,
        "send_prompt",
        lambda *args: (_ for _ in ()).throw(RuntimeError("transport failed")),
    )
    svc.tick()
    failed = ctl.store.get(parent.id)
    assert failed.child_wait["state"] == "ready" and failed.child_wait["attempts"] == 1
    assert failed.status == Status.dormant
    wait = failed.child_wait | {"retry_after": (now() - timedelta(seconds=1)).isoformat()}
    ctl.store.update(parent.id, {"child_wait": wait})
    monkeypatch.setattr(ctl.processes, "send_prompt", send)
    svc.tick()
    assert wait["event"]["id"] in ctl.processes.messages[-1][1]
    assert ctl.store.get(parent.id).child_wait is None


def test_children_receive_own_identity_and_custom_controller_url(setup, monkeypatch):
    from types import SimpleNamespace

    from swarm.processes import Processes

    app, parent, _, _ = setup
    child = spawn(setup)
    proc = Processes(app.state.controller.store, api_url="http://127.0.0.1:9999")
    commands = []
    proc.adapters = {"pi": SimpleNamespace(command=lambda a, p: ["fake-pi"])}
    monkeypatch.setattr(proc, "alive", lambda a: False)
    monkeypatch.setattr(proc, "pid", lambda a: 777)
    monkeypatch.setattr(proc, "run", lambda *args, **kw: commands.append(args))
    proc.launch(child, "Do work")
    launch = next(command for command in commands if command[0] == "new-session")
    assert f"SWARM_AGENT_ID={child.id}" in launch
    assert f"SWARM_PARENT_ID={parent.id}" in launch
    assert "SWARM_API_URL=http://127.0.0.1:9999" in launch
    assert f"SWARM_AGENT_ID={parent.id}" not in launch


def test_spawn_cannot_reuse_key_after_record_forgotten(setup):
    app, parent, request, _ = setup
    child = spawn(setup)
    ctl = app.state.controller
    ctl.stop(str(child.id))
    ctl.forget(str(child.id))
    # Even if an operator deletes the Git artifacts, the retained request is a tombstone.
    git(Path(request.repository), "worktree", "remove", str(child.folder))
    git(Path(request.repository), "branch", "-D", child.branch)
    with pytest.raises(RuntimeError, match="existing artifacts"):
        app.state.orchestration.spawn(str(parent.id), request)
    assert len(ctl.processes.launches) == 1
