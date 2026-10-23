import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_controller import FakeProcesses

from swarm.cli import main
from swarm.controller import Controller
from swarm.models import Agent, Status, WakeDelivery
from swarm.server import create_app
from swarm.store import Store


@pytest.fixture
def timers(tmp_path, monkeypatch):
    clock = [datetime(2035, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("swarm.timers.now", lambda: clock[0])
    store = Store(tmp_path / "swarm")
    proc = FakeProcesses()
    return Controller(store, proc), clock


def add(ctrl, clock, **fields):
    values = {
        "name": "CI monitor",
        "goal": "Monitor a pull request",
        "folder": str(ctrl.store.home),
        "managed": True,
        "status": "waiting",
        "wake_at": clock[0] - timedelta(minutes=1),
        "activity": "Monitoring CI",
        "wait_reason": "Waiting for CI or review changes",
    }
    return ctrl.store.create(Agent(**(values | fields)))


def restarted(ctrl):
    return Controller(Store(ctrl.store.home), ctrl.processes)


@pytest.mark.parametrize("status", ["waiting", "dormant"])
@pytest.mark.parametrize("alive", [True, False])
def test_overdue_wakes_survive_restart_and_are_delivered_once(timers, status, alive):
    ctrl, clock = timers
    agent = add(ctrl, clock, status=status)
    if alive:
        ctrl.processes.running.add(str(agent.id))
    boot = restarted(ctrl)
    boot.recover()
    record = boot.store.get(agent.id)
    assert record.status == Status.running and record.wake_at is None
    assert record.wake_delivery.state == "delivered" and record.wake_delivery.attempts == 1
    calls = ctrl.processes.messages if alive else ctrl.processes.launches
    assert len(calls) == 1
    assert str(record.wake_delivery.id) in calls[0][1]
    assert "Waiting for CI or review changes" in calls[0][1]
    again = restarted(boot)
    again.recover()
    again.tick()
    assert len(ctrl.processes.messages) == (1 if alive else 0)
    assert len(ctrl.processes.launches) == (0 if alive else 1)


@pytest.mark.parametrize("status", ["waiting", "dormant"])
def test_future_timer_is_not_launched_early_on_reboot(timers, status):
    ctrl, clock = timers
    target = clock[0] + timedelta(minutes=5)
    agent = add(ctrl, clock, status=status, wake_at=target, pid=999)
    boot = restarted(ctrl)
    boot.recover()
    boot.tick()
    record = boot.store.get(agent.id)
    assert not ctrl.processes.launches and not ctrl.processes.messages
    assert record.wake_at == target and record.status == status and record.last_error is None
    clock[0] = target + timedelta(seconds=1)
    boot.tick()
    assert len(ctrl.processes.launches) == 1
    assert boot.store.get(agent.id).wake_delivery.state == "delivered"


def test_cli_can_persist_waiting_timer_while_backend_is_down(timers, monkeypatch, capsys):
    ctrl, clock = timers
    monkeypatch.delenv("PI_SESSION_FILE", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    agent = add(ctrl, clock, status="running", wake_at=None)
    target = clock[0] + timedelta(minutes=5)
    main(
        [
            "--home",
            str(ctrl.store.home),
            "report",
            "--id",
            str(agent.id),
            "--status",
            "waiting",
            "--wake-at",
            target.isoformat(),
            "--activity",
            "Monitoring CI",
            "--wait-reason",
            "Waiting for review",
            "--json",
        ]
    )
    assert json.loads(capsys.readouterr().out)["status"] == "waiting"
    assert Store(ctrl.store.home).get(agent.id).wake_at == target
    clock[0] = target + timedelta(hours=2)
    restarted(ctrl).recover()
    assert len(ctrl.processes.launches) == 1


@pytest.mark.parametrize("failure", ["send", "launch"])
def test_failed_delivery_is_persistent_and_backed_off(timers, monkeypatch, failure):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    if failure == "send":
        ctrl.processes.running.add(str(agent.id))
    method = "send_prompt" if failure == "send" else "launch"
    original = getattr(ctrl.processes, method)
    attempts = []

    def fail(*args):
        attempts.append(args)
        raise OSError("tmux unavailable")

    monkeypatch.setattr(ctrl.processes, method, fail)
    ctrl.tick()
    first = ctrl.store.get(agent.id)
    assert first.wake_at == agent.wake_at and first.status == Status.waiting
    assert first.wake_delivery.state == "pending"
    assert first.wake_delivery.retry_at == clock[0] + timedelta(seconds=5)
    assert "tmux unavailable" in first.last_error
    boot = restarted(ctrl)
    boot.recover()
    boot.tick()
    assert len(attempts) == 1  # Restart cannot reset backoff.
    clock[0] += timedelta(seconds=5)
    boot.tick()
    second = boot.store.get(agent.id)
    assert second.wake_delivery.id == first.wake_delivery.id
    assert second.wake_delivery.attempts == 2
    assert second.wake_delivery.retry_at == clock[0] + timedelta(seconds=10)
    monkeypatch.setattr(ctrl.processes, method, original)
    clock[0] += timedelta(seconds=10)
    restarted(ctrl).recover()
    done = ctrl.store.get(agent.id)
    assert done.wake_at is None and done.last_error is None
    assert done.wake_delivery.state == "delivered" and done.wake_delivery.attempts == 3


@pytest.mark.parametrize("after_send", [False, True])
def test_crash_window_retries_same_durable_event_id(timers, monkeypatch, after_send):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))
    original = ctrl.processes.send_prompt

    def crash(agent, prompt):
        stored = Store(ctrl.store.home).get(agent.id)
        assert stored.wake_delivery.state == "pending" and stored.wake_at is not None
        if after_send:
            original(agent, prompt)
        raise KeyboardInterrupt("simulated controller death")

    monkeypatch.setattr(ctrl.processes, "send_prompt", crash)
    with pytest.raises(KeyboardInterrupt):
        ctrl.tick()
    pending = ctrl.store.get(agent.id).wake_delivery
    monkeypatch.setattr(ctrl.processes, "send_prompt", original)
    clock[0] = pending.retry_at + timedelta(seconds=1)
    restarted(ctrl).recover()
    delivered = ctrl.store.get(agent.id)
    assert delivered.wake_delivery.id == pending.id and delivered.wake_delivery.state == "delivered"
    assert len(ctrl.processes.messages) == (2 if after_send else 1)
    assert all(str(pending.id) in prompt for _, prompt in ctrl.processes.messages)


def test_ack_write_failure_never_loses_pending_intent(timers, monkeypatch):
    import swarm.store as module

    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))
    real_write = module.atomic_write

    def fail_ack(path, content):
        if path == ctrl.store.path(agent.id) and "state: delivered" in content:
            raise OSError("disk temporarily unavailable")
        return real_write(path, content)

    monkeypatch.setattr(module, "atomic_write", fail_ack)
    ctrl.tick()
    pending = ctrl.store.get(agent.id)
    assert pending.wake_at is not None and pending.wake_delivery.state == "pending"
    assert len(ctrl.processes.messages) == 1
    monkeypatch.setattr(module, "atomic_write", real_write)
    clock[0] = pending.wake_delivery.retry_at
    restarted(ctrl).recover()
    assert ctrl.store.get(agent.id).wake_delivery.state == "delivered"
    assert len(ctrl.processes.messages) == 2


def test_intent_write_failure_sends_nothing(timers, monkeypatch):
    import swarm.store as module

    ctrl, clock = timers
    agent = add(ctrl, clock)
    real_write = module.atomic_write

    def fail_intent(path, content):
        if path == ctrl.store.path(agent.id) and "state: pending" in content:
            raise OSError("disk full")
        return real_write(path, content)

    monkeypatch.setattr(module, "atomic_write", fail_intent)
    ctrl.tick()
    assert not ctrl.processes.launches and not ctrl.processes.messages
    assert ctrl.store.get(agent.id).wake_at == agent.wake_at
    assert ctrl.store.get(agent.id).wake_delivery is None


@pytest.mark.parametrize("action", ["reschedule", "cancel", "done", "stop"])
def test_concurrent_report_or_stop_is_not_overwritten_by_delivery_ack(timers, monkeypatch, action):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))
    target = clock[0] + timedelta(minutes=5) if action == "reschedule" else agent.wake_at

    def delivered(a, prompt):
        if action == "stop":
            ctrl.stop(str(a.id))
        elif action == "done":
            Store(ctrl.store.home).update(a.id, {"status": "done", "notes": "Goal completed"})
        else:
            Store(ctrl.store.home).update(
                a.id,
                {
                    "status": "waiting",
                    "notes": "New checkpoint",
                    "wake_at": None if action == "cancel" else target,
                },
            )

    monkeypatch.setattr(ctrl.processes, "send_prompt", delivered)
    ctrl.tick()
    current = ctrl.store.get(agent.id)
    assert current.wake_delivery is None
    if action in {"cancel", "done", "stop"}:
        assert current.wake_at is None
    else:
        assert current.wake_at == target and current.notes == "New checkpoint"
    assert current.status != Status.running
    if action == "stop":
        assert not current.auto_resume


def test_new_checkpoint_is_preserved_even_if_timer_is_consumed(timers, monkeypatch):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))
    monkeypatch.setattr(
        ctrl.processes,
        "send_prompt",
        lambda a, p: Store(ctrl.store.home).update(
            a.id,
            {"status": "waiting", "wait_reason": "Need human review", "notes": "Work finished"},
        ),
    )
    ctrl.tick()
    current = ctrl.store.get(agent.id)
    assert current.wake_at is None and current.wake_delivery.state == "delivered"
    assert current.status == Status.waiting and current.wait_reason == "Need human review"
    assert current.notes == "Work finished"


def test_pause_cancel_and_done_do_not_resurrect_timers(timers, monkeypatch):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    original = ctrl.processes.launch
    monkeypatch.setattr(
        ctrl.processes, "launch", lambda *a: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    ctrl.tick()
    pending = ctrl.store.get(agent.id).wake_delivery
    ctrl.store.update(agent.id, {"auto_resume": False})
    monkeypatch.setattr(ctrl.processes, "launch", original)
    clock[0] += timedelta(hours=1)
    restarted(ctrl).recover()
    assert not ctrl.processes.launches and ctrl.store.get(agent.id).wake_delivery.id == pending.id
    ctrl.store.update(agent.id, {"wake_at": None, "auto_resume": True})
    ctrl.tick()
    assert not ctrl.processes.launches
    ctrl.store.update(agent.id, {"status": "waiting", "wake_at": clock[0]})
    ctrl.store.update(agent.id, {"status": "done"})
    restarted(ctrl).recover()
    assert not ctrl.processes.launches and ctrl.store.get(agent.id).wake_at is None


def test_observed_compacting_and_event_only_agents_are_not_timer_woken(timers):
    ctrl, clock = timers
    add(ctrl, clock, managed=False)
    add(ctrl, clock, meta_compaction={"status": "running"})
    add(ctrl, clock, status="dormant", wake_at=None)
    add(ctrl, clock, status="done")
    ctrl.tick()
    assert not ctrl.processes.launches and not ctrl.processes.messages


def test_delivery_failure_does_not_block_other_due_agents(timers, monkeypatch):
    ctrl, clock = timers
    first, second = add(ctrl, clock), add(ctrl, clock)
    ctrl.processes.running.update({str(first.id), str(second.id)})
    send = ctrl.processes.send_prompt

    def dispatch(agent, prompt):
        if agent.id == first.id:
            raise RuntimeError("failed pane")
        send(agent, prompt)

    monkeypatch.setattr(ctrl.processes, "send_prompt", dispatch)
    ctrl.tick()
    assert ctrl.store.get(first.id).wake_delivery.state == "pending"
    assert ctrl.store.get(second.id).wake_delivery.state == "delivered"


def test_application_startup_immediately_delivers_overdue_waiting_timer(tmp_path):
    app = create_app(tmp_path / "home", recover=True, allowed_hosts=["testserver"])
    proc = FakeProcesses()
    app.state.controller.processes = proc
    agent = app.state.controller.store.create(
        Agent(
            name="Scheduled",
            goal="Check CI",
            folder=str(tmp_path),
            managed=True,
            status="waiting",
            wake_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    with TestClient(app) as client:
        state = client.get("/api/state").json()["agents"][0]
        assert state["wake_at"] is None and state["wake_delivery"]["state"] == "delivered"
        assert state["status"] == "running" and state["alive"]
        assert proc.launches[0][0].id == agent.id


def test_error_size_and_backoff_are_bounded(timers, monkeypatch):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))
    monkeypatch.setattr(
        ctrl.processes, "send_prompt", lambda *a: (_ for _ in ()).throw(RuntimeError("x" * 50000))
    )
    for _ in range(10):
        ctrl.tick()
        current = ctrl.store.get(agent.id)
        assert len(current.last_error) <= 4096
        assert current.wake_delivery.retry_at - clock[0] <= timedelta(minutes=5)
        clock[0] = current.wake_delivery.retry_at
    assert current.wake_at is not None and current.wake_delivery.attempts == 10


def test_transition_fences_old_attempt_and_validates_timestamps(timers):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    assert ctrl.store.transition(agent.id, lambda _: None) is None
    with pytest.raises(ValueError, match="timezone"):
        WakeDelivery(id=uuid4(), scheduled_for=datetime(2035, 1, 1))
    with pytest.raises(ValueError, match="immutable"):
        ctrl.store.transition(agent.id, lambda _: {"id": uuid4()})
    before = ctrl.store.path(agent.id).read_bytes()
    with pytest.raises(ValueError, match="exceeds"):
        ctrl.store.update(agent.id, {"notes": "x" * (1024 * 1024)})
    assert ctrl.store.path(agent.id).read_bytes() == before


def test_re_reporting_same_deadline_keeps_event_id_and_retry_backoff(timers, monkeypatch):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))
    monkeypatch.setattr(
        ctrl.processes, "send_prompt", lambda *a: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    ctrl.tick()
    original = ctrl.store.get(agent.id).wake_delivery
    Store(ctrl.store.home).update(
        agent.id, {"wake_at": agent.wake_at.isoformat(), "activity": "Updated monitoring note"}
    )
    current = ctrl.store.get(agent.id)
    assert current.wake_delivery.id == original.id
    assert current.wake_delivery.retry_at == original.retry_at
    ctrl.tick()
    assert ctrl.store.get(agent.id).wake_delivery.attempts == 1


def test_stale_ack_cannot_complete_a_newer_attempt(timers, monkeypatch):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))

    def send(a, prompt):
        event = Store(ctrl.store.home).get(a.id).wake_delivery
        newer = event.model_copy(update={"attempts": event.attempts + 1})
        Store(ctrl.store.home).update(a.id, {"wake_delivery": newer.model_dump(mode="json")})

    monkeypatch.setattr(ctrl.processes, "send_prompt", send)
    ctrl.tick()
    current = ctrl.store.get(agent.id)
    assert current.wake_delivery.state == "pending" and current.wake_delivery.attempts == 2
    assert current.wake_at == agent.wake_at


def test_manual_promptless_wake_cancels_pending_timer(timers, monkeypatch):
    ctrl, clock = timers
    agent = add(ctrl, clock)
    ctrl.processes.running.add(str(agent.id))
    original = ctrl.processes.send_prompt
    monkeypatch.setattr(
        ctrl.processes, "send_prompt", lambda *a: (_ for _ in ()).throw(RuntimeError("failed"))
    )
    ctrl.tick()
    assert ctrl.store.get(agent.id).wake_delivery.state == "pending"
    monkeypatch.setattr(ctrl.processes, "send_prompt", original)
    ctrl.wake(str(agent.id), "")
    current = ctrl.store.get(agent.id)
    assert current.wake_at is None and current.wake_delivery is None
    clock[0] += timedelta(hours=1)
    ctrl.tick()
    assert not ctrl.processes.messages
