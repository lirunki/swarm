import json
import subprocess
from datetime import timedelta

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
            folder=str(ctrl.store.home),
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
