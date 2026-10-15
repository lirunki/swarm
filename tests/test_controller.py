import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from swarm.adapters import PiAdapter, resume_prompt
from swarm.controller import Controller
from swarm.models import Agent, CreateAgent, Status, now
from swarm.store import Store


class FakeProcesses:
    def __init__(self):
        self.running = set()
        self.launches = []
        self.messages = []

    def alive(self, agent):
        return str(agent.id) in self.running

    def pid(self, agent):
        return 4321 if self.alive(agent) else None

    def launch(self, agent, prompt):
        self.running.add(str(agent.id))
        self.launches.append((agent, prompt))
        return 4321

    def send_prompt(self, agent, prompt):
        self.messages.append((agent, prompt))

    def stop(self, agent):
        self.running.discard(str(agent.id))

    def health(self):
        return {"tmux": True, "pi": True}


@pytest.fixture
def ctrl(tmp_path):
    store = Store(tmp_path / "home")
    return Controller(store, FakeProcesses())


def add(ctrl, status, **fields):
    return ctrl.store.create(
        Agent(
            name=status,
            goal="Make progress",
            folder=fields.pop("folder", str(ctrl.store.home)),
            managed=True,
            status=status,
            **fields,
        )
    )


def test_recovery_prompts_running_only_and_never_duplicates_live_agent(ctrl):
    running = add(ctrl, "running", activity="Testing", notes="Parser done")
    add(ctrl, "waiting", wait_reason="Need API key")
    add(ctrl, "idle")
    add(ctrl, "done")
    add(ctrl, "dormant")
    add(ctrl, "idle", auto_resume=False)
    ctrl.recover()
    assert len(ctrl.processes.launches) == 3
    launches = {a.status: prompt for a, prompt in ctrl.processes.launches}
    assert "Resume where you were" in launches[Status.running]
    assert "Parser done" in launches[Status.running]
    assert launches[Status.waiting] is None
    assert launches[Status.idle] is None
    assert ctrl.store.get(running.id).status == Status.running
    ctrl.recover()
    assert len(ctrl.processes.launches) == 3


def test_timer_is_one_shot_and_external_dormancy_is_preserved(ctrl):
    timer = add(ctrl, "dormant", wake_at=now() - timedelta(seconds=1))
    external = add(ctrl, "dormant", wait_reason="PR merged event")
    ctrl.tick()
    ctrl.tick()
    assert len(ctrl.processes.launches) == 1
    assert ctrl.store.get(timer.id).wake_at is None
    assert ctrl.store.get(timer.id).status == Status.running
    assert ctrl.store.get(external.id).status == Status.dormant


def test_explicit_stop_disables_recovery(ctrl):
    agent = add(ctrl, "running")
    ctrl.recover()
    stopped = ctrl.stop(str(agent.id))
    assert stopped.status == Status.idle and not stopped.auto_resume
    ctrl.recover()
    assert len(ctrl.processes.launches) == 1
    ctrl.wake(str(agent.id), "Continue with the agreed design")
    assert ctrl.store.get(agent.id).auto_resume
    assert len(ctrl.processes.launches) == 2


def test_observed_or_invalid_agents_never_execute(ctrl):
    agent = ctrl.store.create(
        Agent(name="External", goal="Observe", folder="/tmp", status="running")
    )
    invalid = add(ctrl, "running")
    ctrl.store.scan()
    ctrl.store.path(invalid.id).write_text("partial edit")
    ctrl.recover()
    assert not ctrl.processes.launches
    with pytest.raises(ValueError, match="adopted"):
        ctrl.wake(str(agent.id))


def test_session_header_is_captured_and_snapshot_updated(ctrl, tmp_path):
    session = tmp_path / "test.jsonl"
    session.write_text(json.dumps({"type": "session", "id": "actual-id"}) + "\n")
    agent = add(ctrl, "idle", session_file=str(session))
    ctrl.tick()
    assert ctrl.store.get(agent.id).session_id == "actual-id"
    assert "actual-id" in (ctrl.store.home / "state.json").read_text()


def test_no_silent_replacement_of_missing_pi_history(ctrl, tmp_path):
    agent = add(
        ctrl, "running", session_file=str(tmp_path / "missing.jsonl"), session_id="known-session"
    )
    with pytest.raises(RuntimeError, match="missing"):
        PiAdapter("/bin/echo").command(agent, resume_prompt(agent))


def test_wake_converts_windows_pi_session_to_portable_copy(ctrl, tmp_path, monkeypatch):
    import swarm.sessions as module

    monkeypatch.setattr(module, "is_wsl", lambda: True)
    mount = tmp_path / "mnt"
    monkeypatch.setattr(module, "WSL_MOUNT_ROOT", mount)
    windows_home = mount / "c" / "Users" / Path.home().name
    sessions_root = windows_home / ".pi" / "agent" / "sessions"
    sessions_root.mkdir(parents=True)
    project = windows_home / "work"
    project.mkdir()
    session = sessions_root / "windows.jsonl"
    from uuid import uuid4

    session_id = str(uuid4())
    session.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {
                    "type": "session",
                    "version": 3,
                    "id": session_id,
                    "cwd": fr"C:\\Users\\{Path.home().name}\\work",
                },
                {
                    "type": "message",
                    "message": {"role": "user", "content": "Resume safely"},
                },
            ]
        )
        + "\n"
    )
    agent = add(
        ctrl,
        "idle",
        folder=str(project),
        session_file=str(session),
        session_id=session_id,
    )

    ctrl.wake(str(agent.id), "Resume where you were")
    updated = ctrl.store.get(agent.id)
    assert updated.import_source and updated.import_source.backend == "pi"
    assert updated.import_source.session_file == str(session.resolve())
    assert Path(updated.session_file).is_relative_to(ctrl.store.sessions_dir)
    assert ctrl.processes.launches[-1][0].session_file == updated.session_file


def test_real_git_worktree_metadata_and_branch_isolation(ctrl, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    folder = tmp_path / "isolated"
    agent = ctrl.create(
        CreateAgent(
            name="Coder",
            goal="Ship",
            folder=str(folder),
            repository=str(repo),
            branch="swarm/task",
            create_worktree=True,
        )
    )
    assert agent.branch == "swarm/task" and agent.worktree == str(folder)
    assert agent.repository == str(repo)
    assert (folder / ".git").is_file()
    branch = subprocess.check_output(
        ["git", "-C", str(repo), "branch", "--show-current"], text=True
    ).strip()
    assert branch != agent.branch


def test_timer_launch_failure_does_not_retry_forever(ctrl, monkeypatch):
    timer = add(ctrl, "dormant", wake_at=now() - timedelta(seconds=1))
    calls = []

    def fail(agent, prompt):
        calls.append(agent)
        raise RuntimeError("pi unavailable")

    monkeypatch.setattr(ctrl.processes, "launch", fail)
    ctrl.tick()
    ctrl.tick()
    assert len(calls) == 1
    assert "pi unavailable" in ctrl.store.get(timer.id).last_error


@pytest.mark.parametrize("status", ["idle", "waiting", "dormant", "done"])
def test_forget_inactive_owned_agent_stops_process_but_keeps_history(ctrl, tmp_path, status):
    session = tmp_path / "saved-pi.jsonl"
    session.write_text("saved conversation\n")
    agent = add(ctrl, status, session_file=str(session), wake_at=now() + timedelta(hours=1))
    ctrl.processes.running.add(str(agent.id))
    ctrl.refresh()
    removed = ctrl.forget(str(agent.id))
    assert removed.id == agent.id
    assert not ctrl.processes.alive(agent)
    assert not ctrl.store.path(agent.id).exists()
    assert session.read_text() == "saved conversation\n"
    assert not ctrl.state["agents"]
    assert not json.loads((ctrl.store.home / "state.json").read_text())["agents"]
    ctrl.recover()
    assert not ctrl.processes.launches


def test_forget_running_agent_is_guarded_against_stale_client_state(ctrl):
    agent = add(ctrl, "running")
    ctrl.processes.running.add(str(agent.id))
    with pytest.raises(RuntimeError, match="actively running"):
        ctrl.forget(str(agent.id))
    assert ctrl.processes.alive(agent)
    assert ctrl.store.get(agent.id).auto_resume


def test_forget_offline_stale_running_agent_is_allowed(ctrl):
    agent = add(ctrl, "running")
    ctrl.forget(str(agent.id))
    assert ctrl.store.scan() == []


def test_forget_failed_stop_keeps_record_and_disables_future_wakes(ctrl, monkeypatch):
    agent = add(ctrl, "dormant", wake_at=now() + timedelta(hours=1))
    ctrl.processes.running.add(str(agent.id))
    monkeypatch.setattr(ctrl.processes, "stop", lambda agent: None)
    with pytest.raises(RuntimeError, match="record was kept"):
        ctrl.forget(str(agent.id))
    retained = ctrl.store.get(agent.id)
    assert not retained.auto_resume and retained.wake_at is None
    assert ctrl.processes.alive(agent)


def test_forget_external_observation_never_kills_reported_pid(ctrl, monkeypatch):
    agent = ctrl.store.create(Agent(name="Observed", goal="Observe", folder="/tmp", pid=1))
    monkeypatch.setattr(
        ctrl.processes, "stop", lambda agent: pytest.fail("must not kill an unowned PID")
    )
    ctrl.forget(str(agent.id))
    assert ctrl.store.scan() == []
