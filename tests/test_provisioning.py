import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_controller import FakeProcesses
from test_server import make_fake_pi

from swarm.controller import Controller
from swarm.models import CreateAgent
from swarm.provisioning import Worker, reconcile
from swarm.server import create_app
from swarm.store import Store


def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in [
        ("init", "-b", "main"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "code.txt").write_text("committed\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True
    )
    return repo


def make_controller(tmp_path):
    store = Store(tmp_path / "registry")
    return Controller(store, FakeProcesses(store))


def run_worker(ctrl, agent, execute=None):
    cwd = Path.cwd()
    try:
        return Worker(ctrl.store, str(agent.id), "/bin/echo").run(execute or (lambda *args: None))
    finally:
        os.chdir(cwd)


def test_http_returns_durable_queued_card_without_git_or_pi(tmp_path, monkeypatch):
    app = create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    app.state.controller.processes = FakeProcesses(app.state.controller.store)
    # Any Git creation would fail this test: the HTTP path must only register intent.
    monkeypatch.setattr(
        "swarm.provisioning.Worker.git", lambda *a, **kw: pytest.fail("Git ran on request path")
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/agents",
            json={
                "name": "Queued",
                "goal": "Work later",
                "folder": str(tmp_path / "repo"),
                "create_worktree": True,
                "automatic_worktree_path": True,
                "branch": "feature/subbranch",
            },
        )
        assert response.status_code == 201, response.text
        value = response.json()
        assert value["spawn_state"] == "provisioning" and value["creation"]["state"] == "queued"
        assert value["folder"] == str(tmp_path / "repo_feature_subbranch")
        assert client.get("/api/state").json()["agents"][0]["id"] == value["id"]
        assert not Path(value["folder"]).exists()
        assert not Path(value["session_file"]).exists()


def test_worker_reports_worktree_progress_then_executes_in_same_workspace(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    ctl = make_controller(tmp_path)
    agent = ctl.create(
        CreateAgent(
            name="Nested branch",
            goal="Implement safely",
            folder=str(repo),
            create_worktree=True,
            automatic_worktree_path=True,
            branch="feature/subbranch",
        )
    )
    stages, executions = [], []
    git = Worker.git

    def inspect(worker, folder, *args, **kw):
        stages.append(worker.store.get(worker.agent_id).creation["stage"])
        if args[:2] == ("worktree", "add"):
            assert "Creating worktree" in stages[-1]
            assert not Path(agent.session_file).exists()
        return git(worker, folder, *args, **kw)

    monkeypatch.setattr(Worker, "git", inspect)
    assert run_worker(ctl, agent, lambda cmd, env: executions.append((cmd, env, Path.cwd())))
    current = ctl.store.get(agent.id)
    assert current.spawn_state == "ready" and current.creation["state"] == "completed"
    assert any("Creating worktree" in stage for stage in stages)
    assert executions[0][2] == Path(current.folder)
    assert str(current.session_file) in executions[0][0]
    branch = subprocess.check_output(
        ["git", "-C", current.folder, "branch", "--show-current"], text=True
    ).strip()
    assert branch == "feature/subbranch"
    log = Path(current.creation["log"]).read_text()
    assert "Creating worktree" in log and "Starting Pi" in log
    assert not reconcile(ctl, current)  # A ready workspace never schedules another setup.


def test_worktree_timeout_becomes_card_error_not_http500(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    ctl = make_controller(tmp_path)
    agent = ctl.create(
        CreateAgent(
            name="Timeout",
            goal="Work",
            folder=str(repo),
            create_worktree=True,
            automatic_worktree_path=True,
            branch="feature/slow",
        )
    )
    original = Worker.git

    def timeout(worker, folder, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            raise subprocess.TimeoutExpired(["git", "worktree", "add"], 1800)
        return original(worker, folder, *args, **kwargs)

    monkeypatch.setattr(Worker, "git", timeout)
    assert not run_worker(ctl, agent)
    failed = ctl.store.get(agent.id)
    assert failed.spawn_state == "failed" and not failed.auto_resume
    assert "timed out" in failed.last_error
    assert failed.creation["state"] == "failed"
    assert "Agent setup failed" in Path(failed.creation["log"]).read_text()
    with pytest.raises(RuntimeError, match="setup is incomplete"):
        ctl.wake(str(agent.id))


def test_stop_before_worker_starts_cancels_without_side_effects(tmp_path):
    ctl = make_controller(tmp_path)
    agent = ctl.create(
        CreateAgent(
            name="Stop queued",
            goal="Work",
            folder=str(tmp_path),
            branch="feature/stopped",
            create_worktree=True,
            automatic_worktree_path=True,
        )
    )
    ctl.stop(str(agent.id))
    current = ctl.store.get(agent.id)
    assert current.spawn_state == "cancelled" and current.creation["state"] == "cancelled"
    ctl.tick()
    assert not ctl.processes.launches and not Path(agent.folder).exists()


def test_stop_during_git_prevents_starting_pi_and_preserves_partial_artifacts(
    tmp_path, monkeypatch
):
    repo = repository(tmp_path)
    ctl = make_controller(tmp_path)
    agent = ctl.create(
        CreateAgent(
            name="Stop creating",
            goal="Work",
            folder=str(repo),
            branch="feature/stop",
            create_worktree=True,
            automatic_worktree_path=True,
        )
    )
    original = Worker.git
    executions = []

    def cancelled(worker, folder, *args, **kwargs):
        result = original(worker, folder, *args, **kwargs)
        if args[:2] == ("worktree", "add"):
            ctl.stop(str(agent.id))
        return result

    monkeypatch.setattr(Worker, "git", cancelled)
    assert not run_worker(ctl, agent, lambda *a: executions.append(a))
    current = ctl.store.get(agent.id)
    assert current.spawn_state == "cancelled" and not current.auto_resume and not executions
    assert Path(agent.folder).is_dir()  # No automatic rollback/deletion of possible user work.


def test_recovery_keeps_surviving_worker_and_never_repeats_started_git(tmp_path):
    ctl = make_controller(tmp_path)
    agent = ctl.create(CreateAgent(name="Restart", goal="Work", folder=str(tmp_path)))
    ctl.store.update(
        agent.id, {"creation": agent.creation | {"state": "running", "stage": "Creating worktree…"}}
    )
    ctl.processes.running.add(str(agent.id))
    boot = Controller(Store(ctl.store.home), ctl.processes)
    boot.recover()
    assert boot.store.get(agent.id).spawn_state == "provisioning"
    assert not ctl.processes.launches
    ctl.processes.running.clear()
    boot.tick()
    failed = boot.store.get(agent.id)
    assert failed.spawn_state == "failed" and "interrupted" in failed.last_error
    boot.recover()
    assert not ctl.processes.launches


def test_queued_launch_failure_and_starting_worker_death_do_not_retry_forever(
    tmp_path, monkeypatch
):
    ctl = make_controller(tmp_path)
    agent = ctl.create(CreateAgent(name="Launch error", goal="Work", folder=str(tmp_path)))
    calls = []

    def fail(a):
        calls.append(a)
        raise RuntimeError("tmux is missing")

    monkeypatch.setattr(ctl.processes, "launch_setup", fail)
    ctl.tick()
    ctl.tick()
    assert len(calls) == 1 and ctl.store.get(agent.id).spawn_state == "failed"
    other = ctl.create(CreateAgent(name="Starting crashed", goal="Work", folder=str(tmp_path)))
    monkeypatch.setattr(ctl.processes, "launch_setup", lambda a: 123)
    ctl.tick()
    assert ctl.store.get(other.id).creation["state"] == "starting"
    ctl.tick()
    assert ctl.store.get(other.id).spawn_state == "provisioning"
    ctl.tick()
    ctl.tick()
    assert ctl.store.get(other.id).spawn_state == "failed"
    assert ctl.store.get(other.id).creation["launch_attempts"] == 3


def test_structured_create_errors_are_json(tmp_path, monkeypatch):
    app = create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        for error in (OSError("Cannot persist request"), subprocess.TimeoutExpired(["git"], 30)):
            monkeypatch.setattr(
                app.state.controller, "create", lambda *a: (_ for _ in ()).throw(error)
            )
            response = client.post(
                "/api/agents", json={"name": "Failed", "goal": "Work", "folder": str(tmp_path)}
            )
            assert response.status_code == 500
            assert "Check the agent card" in response.json()["detail"]


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for setup progress")


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux required")
def test_real_tmux_slow_checkout_survives_backend_restart_and_switches_to_fake_pi(
    tmp_path, monkeypatch
):
    repo = repository(tmp_path)
    actual_git = shutil.which("git")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gate, entered = tmp_path / "continue-git", tmp_path / "git-entered"
    wrapper = bin_dir / "git"
    wrapper.write_text(f"""#!{sys.executable}
import os, sys, time
from pathlib import Path
if 'worktree' in sys.argv and 'add' in sys.argv:
    Path({str(entered)!r}).write_text('entered')
    while not Path({str(gate)!r}).exists(): time.sleep(0.05)
os.execv({actual_git!r}, [{actual_git!r}, *sys.argv[1:]])
""")
    wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    fake = make_fake_pi(
        tmp_path,
        """
import os, sys
print('FAKE-PI-READY', flush=True)
for line in sys.stdin:
    print('REPLY:' + line.rstrip(), flush=True)
""",
    )
    home = tmp_path / "registry"
    app = create_app(home, pi=str(fake), recover=False, allowed_hosts=["testserver"])
    ctl = app.state.controller
    try:
        with TestClient(app) as client:
            started = time.monotonic()
            response = client.post(
                "/api/agents",
                json={
                    "name": "Background",
                    "goal": "Work",
                    "folder": str(repo),
                    "create_worktree": True,
                    "automatic_worktree_path": True,
                    "branch": "feature/subbranch",
                },
            )
            assert response.status_code == 201 and time.monotonic() - started < 3
            id = response.json()["id"]
            wait_until(entered.exists)
            card = client.get("/api/state").json()["agents"][0]
            assert card["spawn_state"] == "provisioning" and "Creating worktree" in card["activity"]
            agent = ctl.store.get(id)
            pid = ctl.processes.pid(agent)
            assert ctl.processes.alive(agent)
            pane = ctl.processes.run(
                "capture-pane", "-p", "-t", ctl.processes.target(agent) + ":0.0"
            ).stdout
            assert "Creating worktree" in pane
        # Closing the HTTP app does not kill the setup worker or its Git subprocess.
        assert ctl.processes.alive(agent) and not Path(agent.folder).exists()
        restarted = create_app(home, pi=str(fake), recover=True, allowed_hosts=["testserver"])
        with TestClient(restarted) as client:
            assert ctl.processes.pid(agent) == pid
            assert client.get("/api/state").json()["agents"][0]["spawn_state"] == "provisioning"
            gate.touch()
            wait_until(lambda: ctl.store.get(id).spawn_state == "ready")
            wait_until(
                lambda: (
                    "FAKE-PI-READY"
                    in ctl.processes.run(
                        "capture-pane", "-p", "-t", ctl.processes.target(agent) + ":0.0"
                    ).stdout
                )
            )
            assert ctl.processes.pid(agent) == pid  # exec preserves the exact managed pane/process.
            assert Path(agent.folder).is_dir()
            assert "Starting Pi" in Path(ctl.store.get(id).creation["log"]).read_text()
            assert client.post(f"/api/agents/{id}/stop").status_code == 200
    finally:
        gate.touch()
        ctl.processes.run("kill-server", check=False)


def test_relative_source_is_canonicalized_before_worker_changes_cwd(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    ctl = make_controller(tmp_path)
    monkeypatch.chdir(tmp_path)
    agent = ctl.create(
        CreateAgent(
            name="Relative",
            goal="Work",
            folder="repo",
            create_worktree=True,
            automatic_worktree_path=True,
            branch="feature/relative",
        )
    )
    assert str(repo) in (ctl.store.home / "setup" / str(agent.id) / "request.json").read_text()
    monkeypatch.chdir(ctl.store.home)
    assert run_worker(ctl, agent)
    assert Path(agent.folder).is_dir() and Path(agent.folder).parent == tmp_path


def test_creation_response_does_not_depend_on_refresh_or_tmux_liveness(tmp_path, monkeypatch):
    app = create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.controller,
            "refresh",
            lambda: (_ for _ in ()).throw(RuntimeError("slow tmux failed")),
        )
        monkeypatch.setattr(
            app.state.controller.processes,
            "alive",
            lambda a: (_ for _ in ()).throw(RuntimeError("tmux failed")),
        )
        response = client.post(
            "/api/agents", json={"name": "Accepted", "goal": "Work", "folder": str(tmp_path)}
        )
        assert response.status_code == 201, response.text
        assert response.json()["creation"]["state"] == "queued"
        assert app.state.controller.store.get(response.json()["id"]).spawn_state == "provisioning"


def test_no_launch_before_worker_exec_lock_is_released(tmp_path):
    import fcntl

    ctl = make_controller(tmp_path)
    agent = ctl.create(CreateAgent(name="Handshake", goal="Work", folder=str(tmp_path)))
    ctl.store.update(agent.id, {"creation": agent.creation | {"state": "launching"}})
    ctl.processes.running.add(str(agent.id))
    with (ctl.store.home / "setup" / str(agent.id) / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reconcile(ctl, ctl.store.get(agent.id))
        assert ctl.store.get(agent.id).spawn_state == "provisioning"
        with pytest.raises(RuntimeError, match="setup is incomplete"):
            ctl.wake(str(agent.id))
    reconcile(ctl, ctl.store.get(agent.id))
    assert ctl.store.get(agent.id).spawn_state == "ready"


def test_stop_between_starting_and_exec_preserves_user_stop(tmp_path):
    ctl = make_controller(tmp_path)
    agent = ctl.create(CreateAgent(name="Stop before exec", goal="Work", folder=str(tmp_path)))

    def stop_then_fail(*args):
        ctl.stop(str(agent.id))
        raise OSError("exec cancelled")

    assert not run_worker(ctl, agent, stop_then_fail)
    current = ctl.store.get(agent.id)
    assert current.spawn_state == "cancelled" and current.activity == "Setup cancelled"
    assert current.last_error is None and not current.auto_resume


def test_pi_missing_keeps_valid_workspace_retryable_without_recreating_it(tmp_path):
    ctl = make_controller(tmp_path)
    agent = ctl.create(CreateAgent(name="Missing Pi", goal="Work", folder=str(tmp_path)))
    cwd = Path.cwd()
    try:
        assert not Worker(ctl.store, str(agent.id), "/definitely/missing/pi").run()
    finally:
        os.chdir(cwd)
    current = ctl.store.get(agent.id)
    assert current.spawn_state == "ready" and current.setup_workspace_ready
    assert not current.auto_resume and "pi executable not found" in current.last_error
    assert Path(current.session_file).is_file()


def test_uncommitted_setup_artifacts_cleaned_on_record_write_failure(tmp_path, monkeypatch):
    ctl = make_controller(tmp_path)
    monkeypatch.setattr(
        ctl.store, "create", lambda a: (_ for _ in ()).throw(OSError("cannot save"))
    )
    with pytest.raises(OSError):
        ctl.create(CreateAgent(name="Record failed", goal="Work", folder=str(tmp_path)))
    assert list((ctl.store.home / "setup").iterdir()) == []


def test_worker_git_does_not_pass_provider_credentials_to_checkout_helpers(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    ctl = make_controller(tmp_path)
    agent = ctl.create(CreateAgent(name="Env", goal="Work", folder=str(repo)))
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-never-used")
    monkeypatch.setenv("AZURE_SECRET", "test-secret-never-used")
    monkeypatch.setenv("GH_TOKEN", "test-secret-never-used")
    real_popen = subprocess.Popen

    def checked(command, **kwargs):
        assert not any(k in kwargs["env"] for k in ("OPENAI_API_KEY", "AZURE_SECRET", "GH_TOKEN"))
        return real_popen(command, **kwargs)

    worker = Worker(ctl.store, str(agent.id), "/bin/echo")
    monkeypatch.setattr("swarm.provisioning.subprocess.Popen", checked)
    assert worker.git(repo, "branch", "--show-current") == "main"
