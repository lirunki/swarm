import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.controller import Controller
from swarm.importers.pi_history import PiHistory
from swarm.messaging import MessageRequest, prepare_bridge
from swarm.models import Agent, now
from swarm.restarts import RestartAction, RestartRequest
from swarm.server import create_app
from swarm.store import Store, atomic_write


class FakeProcesses:
    def __init__(self, store):
        self.store = store
        self.live = {}
        self.next_pid = 100
        self.launches = []
        self.kills = []
        self.prompts = []
        self.fail_launch = False
        self.fail_stop = False

    def alive(self, a):
        return str(a.id) in self.live

    def identity(self, a):
        pid = self.live.get(str(a.id))
        return {"pid": pid, "pane": f"%{pid}", "session": f"${pid}"} if pid else None

    def pid(self, a):
        return self.live.get(str(a.id))

    def health(self):
        return {"tmux": True, "pi": True}

    def launch(self, a, prompt):
        if self.fail_launch:
            raise RuntimeError("Pi launch unavailable")
        assert not self.alive(a)
        assert Path(a.session_file).is_file()
        self.next_pid += 1
        self.live[str(a.id)] = self.next_pid
        directory, epoch = prepare_bridge(self.store, a)
        heartbeat(self.store.get(a.id), directory)
        self.launches.append((a.id, prompt))
        return self.next_pid

    def stop(self, a):
        self.kills.append(a.id)
        if not self.fail_stop:
            self.live.pop(str(a.id), None)

    def send_prompt(self, a, text):
        self.prompts.append((a.id, text))


def heartbeat(agent, directory, native=True):
    atomic_write(
        directory / "heartbeat.json",
        json.dumps(
            {
                "protocol": 1,
                "epoch": agent.control_epoch,
                "session_id": agent.session_id,
                "instance": str(uuid4()),
                "updated_at": now().isoformat(),
                "capabilities": {"graceful_shutdown": native},
            }
        ),
    )


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    ctl = app.state.controller
    ctl.processes = FakeProcesses(ctl.store)
    return app, ctl


def agent(ctl, *, live=True, native=True, **fields):
    id = uuid4()
    session_id = str(uuid4())
    path = ctl.store.sessions_dir / f"{id}.jsonl"
    path.write_text(PiHistory(session_id, str(ctl.store.home), "A").encode())
    a = ctl.store.create(
        Agent(
            **(
                {
                    "id": id,
                    "name": "Worker",
                    "goal": "Preserve goal",
                    "folder": str(ctl.store.home),
                    "managed": True,
                    "status": "running",
                    "session_id": session_id,
                    "session_file": str(path),
                    "auto_resume": True,
                    "notes": "Material checkpoint",
                    "wait_reason": "Original wait",
                }
                | fields
            )
        )
    )
    if live:
        ctl.processes.next_pid += 1
        ctl.processes.live[str(a.id)] = ctl.processes.next_pid
        if native:
            directory, _ = prepare_bridge(ctl.store, a)
            a = ctl.store.get(a.id)
            heartbeat(a, directory)
    return a


def start(ctl, *agents):
    preview = ctl.restarts.preview()
    ids = {str(a.id) for a in agents}
    targets = [t for t in preview["targets"] if t["id"] in ids]
    assert len(targets) == len(agents)
    request = RestartRequest(
        request_id=uuid4(),
        targets=[{"id": t["id"], "token": t["token"]} for t in targets],
        confirmed=True,
    )
    return ctl.restarts.start(request)["job"]["id"], request


def native_ready(ctl, a, id):
    current = ctl.store.get(a.id)
    directory = ctl.messages.control(a.id)
    atomic_write(
        directory / "lifecycle-ack.json",
        json.dumps(
            {
                "id": id,
                "agent_id": str(a.id),
                "epoch": current.control_epoch,
                "session_id": current.session_id,
                "state": "ready",
                "updated_at": now().isoformat(),
            }
        ),
    )


def state(ctl, id, a):
    return next(t for t in ctl.restarts.get(id)["job"]["targets"] if t["id"] == str(a.id))


def complete(ctl, a, id):
    ctl.tick()
    native_ready(ctl, a, id)
    ctl.tick()
    assert state(ctl, id, a)["state"] == "stopping"
    ctl.processes.live.pop(str(a.id))  # Native Pi actually exited; ACK alone is insufficient.
    ctl.tick()
    ctl.tick()
    return ctl.store.get(a.id)


def test_preview_includes_only_safe_live_owned_processes_not_just_running_status(setup):
    _, ctl = setup
    eligible = [agent(ctl, status=s) for s in ("running", "idle", "waiting", "done", "dormant")]
    stopped = agent(ctl, live=False)
    observed = agent(ctl, managed=False)
    setup_agent = agent(ctl, spawn_state="provisioning")
    compacting = agent(ctl, meta_compaction={"status": "running"})
    stopping = agent(ctl, stop_pending=True)
    missing = agent(ctl)
    Path(missing.session_file).unlink()
    preview = ctl.restarts.preview()
    assert {t["id"] for t in preview["targets"]} == {str(a.id) for a in eligible}
    assert {t["id"] for t in preview["skipped"]} == {
        str(a.id) for a in (stopped, observed, setup_agent, compacting, stopping, missing)
    }
    assert not ctl.processes.kills and not ctl.processes.prompts and not ctl.processes.launches


@pytest.mark.parametrize("status", ["running", "idle", "waiting", "done", "dormant"])
def test_graceful_handshake_preserves_all_user_state_and_never_submits_prompt(setup, status):
    _, ctl = setup
    target = agent(
        ctl,
        status=status,
        wake_at=now() + timedelta(hours=1),
        auto_resume=False,
        child_wait={"child_id": str(uuid4()), "state": "pending"},
    )
    original = ctl.store.get(target.id).model_dump()
    history = Path(target.session_file).read_bytes()
    ctl.messages.send(str(target.id), MessageRequest(text="Mail stays queued"))
    id, _ = start(ctl, target)
    assert not ctl.processes.kills and not ctl.processes.launches
    ctl.tick()
    request = json.loads((ctl.messages.control(target.id) / "lifecycle.json").read_text())
    assert request["phase"] == "prepare"
    assert ctl.messages.read(target.id).messages[0].status in {"pending", "delivering"}
    native_ready(ctl, target, id)
    ctl.tick()
    assert (
        json.loads((ctl.messages.control(target.id) / "lifecycle.json").read_text())["phase"]
        == "commit"
    )
    assert not ctl.processes.launches
    ctl.tick()
    assert (
        not ctl.processes.launches
    )  # ready/shutdown ACK never substitutes for actual process exit.
    ctl.processes.live.pop(str(target.id))
    ctl.tick()
    ctl.tick()
    current = ctl.store.get(target.id)
    assert state(ctl, id, target)["state"] == "completed"
    assert (
        ctl.processes.launches == [(target.id, None)]
        and not ctl.processes.kills
        and not ctl.processes.prompts
    )
    for key in (
        "id",
        "session_id",
        "session_file",
        "folder",
        "goal",
        "notes",
        "status",
        "auto_resume",
        "wake_at",
        "wait_reason",
        "child_wait",
    ):
        assert getattr(current, key) == original[key], key
    assert Path(current.session_file).read_bytes() == history
    assert current.restart_job_id is None
    assert len(ctl.messages.list(str(target.id))["messages"]) == 1


def test_snapshot_changed_process_is_rejected_before_any_side_effects(setup):
    _, ctl = setup
    target = agent(ctl)
    before = ctl.restarts.preview()["targets"][0]
    ctl.processes.live[str(target.id)] += 100
    request = RestartRequest(
        request_id=uuid4(), targets=[{"id": target.id, "token": before["token"]}], confirmed=True
    )
    with pytest.raises(RuntimeError, match="Fleet changed"):
        ctl.restarts.start(request)
    assert not ctl.restarts.jobs() and not getattr(ctl.store.get(target.id), "restart_job_id", None)


def test_start_is_idempotent_and_process_replacement_is_never_killed(setup):
    _, ctl = setup
    target = agent(ctl)
    id, request = start(ctl, target)
    assert ctl.restarts.start(request)["reused"]
    changed = request.model_copy(update={"timeout_seconds": 100})
    with pytest.raises(RuntimeError, match="different request"):
        ctl.restarts.start(changed)
    ctl.processes.live[str(target.id)] += 100
    ctl.tick()
    assert state(ctl, id, target)["state"] == "failed"
    assert not ctl.processes.kills and not ctl.processes.launches
    assert ctl.processes.alive(target)


def test_cancel_before_commit_removes_prepare_but_not_schedule(setup):
    _, ctl = setup
    target = agent(ctl, wake_at=now() + timedelta(minutes=5))
    id, _ = start(ctl, target)
    ctl.tick()
    native_ready(ctl, target, id)
    ctl.restarts.action(id, RestartAction(agent_ids=[target.id], confirmed=True), "cancel")
    assert not (ctl.messages.control(target.id) / "lifecycle.json").exists()
    ctl.tick()
    assert state(ctl, id, target)["state"] == "cancelled"
    assert ctl.store.get(target.id).wake_at == target.wake_at and ctl.processes.alive(target)
    assert not ctl.processes.kills and not ctl.processes.launches


def test_cancel_after_commit_refused_and_operator_stop_prevents_restart(setup):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    ctl.tick()
    native_ready(ctl, target, id)
    ctl.tick()
    with pytest.raises(RuntimeError):
        ctl.restarts.action(id, RestartAction(agent_ids=[target.id], confirmed=True), "cancel")
    ctl.stop(str(target.id))
    ctl.tick()
    assert state(ctl, id, target)["state"] == "cancelled"
    assert not ctl.store.get(target.id).auto_resume and not ctl.processes.launches


def test_timeout_never_kills_and_later_idle_can_still_complete(setup):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    job = ctl.restarts.read(id)
    job["targets"][0]["deadline"] = (now() - timedelta(seconds=1)).isoformat()
    ctl.restarts.save(job)
    ctl.tick()
    assert state(ctl, id, target)["state"] == "blocked"
    assert ctl.processes.alive(target) and not ctl.processes.kills
    native_ready(ctl, target, id)
    ctl.tick()
    assert state(ctl, id, target)["state"] == "stopping"


def test_legacy_requires_explicit_action_then_observed_exit(setup):
    _, ctl = setup
    target = agent(ctl, native=False)
    id, _ = start(ctl, target)
    ctl.tick()
    assert state(ctl, id, target)["state"] == "blocked" and not ctl.processes.prompts
    ctl.restarts.action(id, RestartAction(agent_ids=[target.id], confirmed=True), "legacy")
    assert ctl.processes.prompts == [(target.id, "/quit")]
    ctl.tick()
    ctl.tick()
    assert ctl.processes.prompts == [(target.id, "/quit")] and not ctl.processes.launches
    ctl.processes.live.pop(str(target.id))
    ctl.tick()
    ctl.tick()
    assert state(ctl, id, target)["state"] == "completed"
    assert ctl.processes.launches == [(target.id, None)]


def test_force_is_explicit_scoped_and_failed_kill_does_not_launch(setup):
    _, ctl = setup
    one, two = agent(ctl, native=False), agent(ctl, native=False)
    id, _ = start(ctl, one, two)
    ctl.processes.fail_stop = True
    ctl.restarts.action(id, RestartAction(agent_ids=[one.id], confirmed=True), "force")
    ctl.tick()
    assert ctl.processes.kills == [one.id] and not ctl.processes.launches
    assert ctl.processes.alive(two)
    ctl.processes.fail_stop = False
    ctl.restarts.action(id, RestartAction(agent_ids=[one.id], confirmed=True), "force")
    ctl.tick()
    ctl.tick()
    assert state(ctl, id, one)["state"] == "completed"
    assert state(ctl, id, two)["state"] == "blocked" and ctl.processes.alive(two)


def test_backend_restart_resumes_job_without_ordinary_recovery_prompt(setup):
    _, ctl = setup
    target = agent(ctl, status="dormant", wake_at=now() + timedelta(hours=1))
    id, _ = start(ctl, target)
    ctl.tick()
    native_ready(ctl, target, id)
    ctl.tick()
    ctl.processes.live.pop(str(target.id))
    boot = Controller(Store(ctl.store.home), ctl.processes)
    boot.recover()
    boot.tick()
    assert state(boot, id, target)["state"] == "completed"
    assert ctl.processes.launches == [(target.id, None)]
    assert (
        boot.store.get(target.id).status == "dormant"
        and boot.store.get(target.id).wake_at == target.wake_at
    )


def test_crash_after_job_intent_before_binding_recovers_reservation(setup, monkeypatch):
    _, ctl = setup
    target = agent(ctl)
    original = ctl.restarts.bind
    monkeypatch.setattr(
        ctl.restarts, "bind", lambda job: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        start(ctl, target)
    assert not getattr(ctl.store.get(target.id), "restart_job_id", None)
    monkeypatch.setattr(ctl.restarts, "bind", original)
    ctl.restarts.recover()
    assert ctl.store.get(target.id).restart_job_id == ctl.restarts.jobs()[0]["id"]
    assert not ctl.processes.kills and not ctl.processes.launches


def test_failed_launch_is_not_retried_and_preserves_schedule(setup):
    _, ctl = setup
    target = agent(ctl, wake_at=now() + timedelta(hours=1))
    id, _ = start(ctl, target)
    ctl.tick()
    native_ready(ctl, target, id)
    ctl.tick()
    ctl.processes.fail_launch = True
    ctl.processes.live.pop(str(target.id))
    ctl.tick()
    ctl.tick()
    ctl.recover()
    current = ctl.store.get(target.id)
    assert state(ctl, id, target)["state"] == "failed" and not current.auto_resume
    assert current.wake_at == target.wake_at
    assert not ctl.processes.launches


def test_crash_after_launch_intent_does_not_blindly_launch_again(setup):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    job = ctl.restarts.read(id)
    item = job["targets"][0]
    item.update(state="launching", launch_started_at=now().isoformat())
    ctl.restarts.save(job)
    ctl.processes.live.pop(str(target.id))
    prepare_bridge(
        ctl.store, ctl.store.get(target.id)
    )  # Launch crossed its durable epoch boundary.
    ctl.recover()
    assert state(ctl, id, target)["state"] == "failed" and not ctl.processes.launches
    assert not ctl.store.get(target.id).auto_resume


def test_other_lifecycle_actions_blocked_but_messages_can_queue(setup):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    for action in (lambda: ctl.wake(str(target.id), ""), lambda: ctl.forget(str(target.id))):
        with pytest.raises(RuntimeError, match="restart"):
            action()
    result = ctl.messages.send(str(target.id), MessageRequest(text="After restart"))
    assert result["message"]["status"] == "pending"
    assert not ctl.processes.prompts


def test_api_confirmation_auth_and_invalid_targets(setup, tmp_path):
    app, ctl = setup
    target = agent(ctl)
    with TestClient(app) as client:
        preview = client.get("/api/fleet/restarts/preview").json()
        request = {
            "request_id": str(uuid4()),
            "targets": [
                {"id": target["id"], "token": target["token"]} for target in preview["targets"]
            ],
        }
        for value in (None, False, 1, "true"):
            assert (
                client.post("/api/fleet/restarts", json=request | {"confirmed": value}).status_code
                == 422
            )
        assert (
            client.post(
                "/api/fleet/restarts",
                json=request | {"confirmed": True},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        result = client.post("/api/fleet/restarts", json=request | {"confirmed": True})
        assert result.status_code == 202, result.text
        id = result.json()["job"]["id"]
        assert client.get("/api/fleet/restarts/" + id).status_code == 200
        assert (
            client.post(
                "/api/fleet/restarts/" + id + "/cancel",
                json={"confirmed": True, "agent_ids": [str(target.id)]},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/fleet/restarts/" + id + "/unknown",
                json={"confirmed": True, "agent_ids": [str(target.id)]},
            ).status_code
            == 400
        )


@pytest.mark.skipif(
    not __import__("shutil").which("pi") or not __import__("shutil").which("tmux"),
    reason="Native Pi/tmux integration optional",
)
@pytest.mark.parametrize("retain_pane", [True, False])
def test_real_pi_tmux_graceful_restart_is_promptless_and_preserves_sleep(
    tmp_path, monkeypatch, retain_pane
):
    import shutil
    import time

    from swarm.paths import agent_path

    config = tmp_path / "empty-config"
    config.mkdir()
    (config / "settings.json").write_text(
        json.dumps({"enableAnalytics": False, "enableInstallTelemetry": False})
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent_path(config))
    for key in list(__import__("os").environ):
        if key.endswith("_API_KEY") or key in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_AUTH_TOKEN",
        }:
            monkeypatch.delenv(key, raising=False)
    app = create_app(
        tmp_path / "real-home", pi=shutil.which("pi"), recover=False, allowed_hosts=["testserver"]
    )
    ctl = app.state.controller
    id = uuid4()
    session_id = str(uuid4())
    history = ctl.store.sessions_dir / f"{id}.jsonl"
    history.write_text(PiHistory(session_id, agent_path(tmp_path), "No model calls").encode())
    target = ctl.store.create(
        Agent(
            id=id,
            name="Real restart test",
            goal="No work requested",
            folder=str(tmp_path),
            managed=True,
            status="dormant",
            auto_resume=False,
            wake_at=now() + timedelta(hours=1),
            session_file=str(history),
            session_id=session_id,
            notes="Keep this checkpoint",
        )
    )

    def wait(predicate):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            current = ctl.store.get(id)
            assert not current.last_error, current.last_error
            time.sleep(0.05)
        raise AssertionError(
            "Timed out waiting for native graceful restart: " + str(ctl.store.get(id).model_dump())
        )

    try:
        ctl.processes.launch(target, None)
        if not retain_pane:
            ctl.processes.run(
                "set-option", "-t", ctl.processes.target(target), "remain-on-exit", "off"
            )
        with TestClient(app) as client:
            wait(lambda: ctl.restarts.native(ctl.store.get(id)))
            old_pid = ctl.processes.pid(target)
            old_epoch = ctl.store.get(id).control_epoch
            preview = client.get("/api/fleet/restarts/preview").json()
            candidate = next(t for t in preview["targets"] if t["id"] == str(id))
            response = client.post(
                "/api/fleet/restarts",
                json={
                    "request_id": str(uuid4()),
                    "confirmed": True,
                    "targets": [{"id": str(id), "token": candidate["token"]}],
                },
            )
            assert response.status_code == 202, response.text
            job_id = response.json()["job"]["id"]
            wait(lambda: state(ctl, job_id, target)["state"] == "completed")
            current = ctl.store.get(id)
            assert ctl.processes.pid(current) != old_pid and current.control_epoch != old_epoch
            assert current.status == "dormant" and current.wake_at == target.wake_at
            assert not current.auto_resume and current.notes == target.notes
            assert current.session_file == target.session_file and current.session_id == session_id
            assert current.restart_job_id is None
            entries = [json.loads(line) for line in history.read_text().splitlines()]
            assert not any(e.get("type") == "message" for e in entries)  # No prompt or model turn.
            receipt = json.loads((ctl.messages.control(id) / "lifecycle-ack.json").read_text())
            assert receipt["state"] == "shutdown_requested" and receipt["epoch"] == old_epoch
    finally:
        ctl.processes.run("kill-server", check=False)


def test_inspection_uncertainty_cannot_trigger_a_kill_or_relaunch(setup, monkeypatch):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    monkeypatch.setattr(
        ctl.processes,
        "identity",
        lambda a: (_ for _ in ()).throw(RuntimeError("Tmux inspection failed")),
    )
    ctl.tick()
    assert state(ctl, id, target)["state"] == "blocked"
    assert not ctl.processes.kills and not ctl.processes.launches


def test_missing_pane_does_not_prove_the_old_process_exited(setup, monkeypatch):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    ctl.processes.live.pop(str(target.id))
    monkeypatch.setattr(ctl.processes, "exited", lambda identity: False, raising=False)
    ctl.tick()
    assert state(ctl, id, target)["state"] == "blocked"
    assert "not confirmed" in state(ctl, id, target)["stage"]
    assert not ctl.processes.kills and not ctl.processes.launches


def test_cancel_cleanup_failure_keeps_lease_and_retries_explicitly(setup, monkeypatch):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    ctl.tick()
    remove = ctl.restarts.remove_request
    monkeypatch.setattr(
        ctl.restarts, "remove_request", lambda *a: (_ for _ in ()).throw(OSError("unlink failed"))
    )
    with pytest.raises(RuntimeError, match="could not be removed"):
        ctl.restarts.action(id, RestartAction(agent_ids=[target.id], confirmed=True), "cancel")
    assert ctl.store.get(target.id).restart_job_id == id
    assert state(ctl, id, target)["state"] == "blocked" and state(ctl, id, target)["can_cancel"]
    assert (ctl.messages.control(target.id) / "lifecycle.json").exists()
    monkeypatch.setattr(ctl.restarts, "remove_request", remove)
    ctl.restarts.action(id, RestartAction(agent_ids=[target.id], confirmed=True), "cancel")
    assert ctl.store.get(target.id).restart_job_id is None


def test_oversized_restart_intent_rejected_before_leases_or_actions(setup, monkeypatch):
    _, ctl = setup
    target = agent(ctl)
    monkeypatch.setattr("swarm.restarts.MAX_JOB_BYTES", 100)
    with pytest.raises(ValueError, match="exceeds"):
        start(ctl, target)
    assert not getattr(ctl.store.get(target.id), "restart_job_id", None)
    assert not ctl.restarts.jobs() and not ctl.processes.kills


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_orphan_restart_does_not_starve_timers_and_stop_remains_available(setup, damage):
    _, ctl = setup
    first = agent(ctl)
    second = agent(ctl, status="dormant", wake_at=now() - timedelta(seconds=1))
    id, _ = start(ctl, first)
    if damage == "missing":
        ctl.restarts.path(id).unlink()
    else:
        ctl.restarts.path(id).write_text("{broken")
    ctl.tick()
    assert ctl.store.get(first.id).restart["stage"] == "Restart metadata needs repair"
    assert ctl.store.get(second.id).wake_at is None
    assert any(a == second.id for a, _ in ctl.processes.prompts)
    ctl.stop(str(first.id))
    assert not ctl.processes.alive(first) and not ctl.store.get(first.id).restart_job_id


def test_real_process_inspector_rejects_errors_and_targets_immutable_panes(setup, monkeypatch):
    import subprocess

    from swarm.processes import Processes

    _, ctl = setup
    target = agent(ctl)
    proc = Processes(ctl.store)
    monkeypatch.setattr(
        proc, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "permission denied")
    )
    with pytest.raises(RuntimeError, match="inspection failed"):
        proc.identity(target)
    identity = {"session": "$4", "pane": "%9", "pid": 9999, "birth": "original"}
    monkeypatch.setattr(proc, "inspect_pane", lambda pane: identity | {"dead": False})
    calls = []

    def reject(*a, **kw):
        calls.append(a)
        return subprocess.CompletedProcess([], 0, "SWARM_RESTART_TARGET_MISMATCH", "")

    monkeypatch.setattr(proc, "run", reject)
    with pytest.raises(RuntimeError, match="changed"):
        proc.stop_identity(target, identity)
    assert calls[0][0] == "if-shell" and calls[0][-2] == "kill-pane -t %9"
    assert not any(c[0] == "kill-session" for c in calls)
    assert "pane_pid" in calls[0][4] and "pane_id" in calls[0][4]


def test_crash_before_launch_epoch_boundary_resumes_unstarted_intent(setup):
    _, ctl = setup
    target = agent(ctl, status="done", auto_resume=False)
    id, _ = start(ctl, target)
    job = ctl.restarts.read(id)
    job["targets"][0].update(state="launching", launch_started_at=now().isoformat())
    ctl.restarts.save(job)
    ctl.processes.live.pop(str(target.id))
    ctl.recover()
    ctl.tick()
    assert state(ctl, id, target)["state"] == "completed"
    assert ctl.processes.launches == [(target.id, None)]
    assert ctl.store.get(target.id).status == "done" and not ctl.store.get(target.id).auto_resume


def test_terminal_job_write_before_record_ack_failure_does_not_reopen_launch(setup, monkeypatch):
    _, ctl = setup
    target = agent(ctl)
    id, _ = start(ctl, target)
    job = ctl.restarts.read(id)
    item = job["targets"][0]
    transition = ctl.store.transition

    def fail_clear(agent_id, callback):
        current = ctl.store.get(agent_id)
        fields = callback(current)
        if fields and fields.get("restart_job_id", "not-clearing") is None:
            raise OSError("record ack unavailable")
        return transition(agent_id, lambda _current: fields)

    monkeypatch.setattr(ctl.store, "transition", fail_clear)
    ctl.restarts.finish(
        job, item, ctl.store.get(target.id), "failed", "Interrupted launch", "Inspect first", True
    )
    assert ctl.restarts.read(id)["targets"][0]["state"] == "failed"
    ctl.tick()
    assert ctl.restarts.read(id)["targets"][0]["state"] == "failed"
    assert not ctl.processes.launches and ctl.store.get(target.id).restart_job_id
    monkeypatch.setattr(ctl.store, "transition", transition)
    ctl.tick()
    current = ctl.store.get(target.id)
    assert current.restart_job_id is None and not current.auto_resume
