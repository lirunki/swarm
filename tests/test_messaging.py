import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.controller import Controller
from swarm.messaging import MessageRequest, prepare_bridge
from swarm.models import Agent, Status, now
from swarm.server import create_app
from swarm.store import Store, atomic_write


class FakeProcesses:
    def __init__(self):
        self.running, self.launches = set(), []

    def alive(self, agent):
        return str(agent.id) in self.running

    def pid(self, agent):
        return 123 if self.alive(agent) else None

    def health(self):
        return {"tmux": True, "pi": True}

    def launch(self, agent, prompt):
        self.running.add(str(agent.id))
        self.launches.append((agent, prompt))
        return 123

    def stop(self, agent):
        self.running.discard(str(agent.id))

    def send_prompt(self, *args):
        raise AssertionError("Mailbox must not type into the terminal editor")


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    ctl = app.state.controller
    ctl.processes = FakeProcesses()
    agent = ctl.store.create(
        Agent(
            name="Target",
            goal="Work",
            folder=str(tmp_path),
            managed=True,
            status="idle",
            auto_resume=False,
            session_id=str(uuid4()),
        )
    )
    return app, ctl, agent


def connect_bridge(ctl, agent):
    directory, epoch = prepare_bridge(ctl.store, agent)
    current = ctl.store.get(agent.id)
    ctl.processes.running.add(str(agent.id))
    atomic_write(
        directory / "heartbeat.json",
        json.dumps(
            {
                "epoch": epoch,
                "session_id": current.session_id,
                "updated_at": now().isoformat(),
                "protocol": 1,
                "instance": str(uuid4()),
            }
        ),
    )
    return current, directory, epoch


def ack(directory, agent, message, status="delivered", **extra):
    atomic_write(
        directory / "acks" / f"{message['id']}.json",
        json.dumps(
            {
                "id": message["id"],
                "epoch": agent.control_epoch,
                "session_id": agent.session_id,
                "status": status,
                **extra,
            }
        ),
    )


def test_asleep_messages_persist_without_wake_and_delete(setup):
    app, ctl, agent = setup
    with TestClient(app) as client:
        base = f"/api/agents/{agent.id}/messages"
        first = client.post(base, json={"text": "Read this later"})
        assert first.status_code == 202
        id = first.json()["message"]["id"]
        current = ctl.store.get(agent.id)
        assert (
            current.status == Status.idle and not current.auto_resume and not ctl.processes.launches
        )
        ctl.tick()
        assert client.get(base).json()["messages"][0]["status"] == "pending"
        reboot = Controller(Store(ctl.store.home), ctl.processes)
        assert reboot.messages.list(str(agent.id))["messages"][0]["text"] == "Read this later"
        assert client.get("/api/state").json()["agents"][0]["message_count"] == 1
        assert "Read this later" not in json.dumps(client.get("/api/state").json())
        assert client.delete(base + "/" + id).json()["deleted"]
        assert client.get(base).json()["messages"] == []
        ctl.tick()
        assert not ctl.processes.launches


def test_awake_uses_native_bridge_and_ack_not_tty_or_status_change(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    with TestClient(app) as client:
        base = f"/api/agents/{agent.id}/messages"
        result = client.post(
            base, json={"text": "/quit is only message text, not a command"}
        ).json()
        message = result["message"]
        assert message["status"] == "delivering"
        inbox = json.loads((directory / "inbox.json").read_text())
        assert (
            inbox["awake"]
            and inbox["epoch"] == epoch
            and inbox["items"][0]["text"].startswith("/quit")
        )
        assert client.delete(base + "/" + message["id"]).status_code == 409
        assert ctl.store.get(agent.id).status == Status.idle
        ack(directory, agent, message)
        ctl.tick()
        assert client.get(base).json()["messages"] == []
        assert ctl.messages.read(agent.id).messages[0].status == "delivered"
        assert ctl.messages.read(agent.id).messages[0].text == ""
        assert json.loads((directory / "inbox.json").read_text())["items"] == []


@pytest.mark.parametrize("status", ["dormant", "done"])
def test_alive_sleeping_or_done_agents_do_not_receive_until_explicit_wake(setup, status):
    app, ctl, agent = setup
    agent, directory, _ = connect_bridge(ctl, agent)
    ctl.store.update(agent.id, {"status": status})
    result = ctl.messages.send(str(agent.id), MessageRequest(text="Only after waking"))
    assert result["message"]["status"] == "pending"
    assert not json.loads((directory / "inbox.json").read_text())["awake"]
    ctl.wake(str(agent.id), "")
    ctl.tick()
    assert ctl.messages.list(str(agent.id))["messages"][0]["status"] == "delivering"
    assert not ctl.processes.launches


def test_missing_extension_is_visible_not_falsely_delivered(setup):
    app, ctl, agent = setup
    ctl.processes.running.add(str(agent.id))
    result = ctl.messages.send(str(agent.id), MessageRequest(text="Legacy agent"))
    assert result["message"]["status"] == "pending"
    assert "relaunch" in result["message"]["last_error"]
    assert not ctl.processes.launches


def test_idempotent_request_and_payload_collision(setup):
    app, ctl, agent = setup
    id = uuid4()
    request = MessageRequest(text="once", request_id=id)
    ctl.messages.send(str(agent.id), request)
    assert ctl.messages.send(str(agent.id), request)["reused"]
    assert len(ctl.messages.list(str(agent.id))["messages"]) == 1
    with pytest.raises(RuntimeError, match="different message"):
        ctl.messages.send(str(agent.id), request.model_copy(update={"text": "changed"}))
    ctl.messages.delete(str(agent.id), str(id))
    assert ctl.messages.send(str(agent.id), request)["message"]["status"] == "deleted"
    assert ctl.messages.list(str(agent.id))["messages"] == []


def test_sender_is_not_limited_to_parent_and_unknown_target_or_sender_rejected(setup):
    app, ctl, agent = setup
    sender = ctl.store.create(
        Agent(name="Peer", goal="Different project", folder=agent.folder, managed=True)
    )
    result = ctl.messages.send(
        str(agent.id), MessageRequest(text="Peer message", sender_id=sender.id)
    )
    assert result["message"]["sender_id"] == str(sender.id)
    assert result["message"]["sender_name"] == "Peer"
    with pytest.raises(FileNotFoundError):
        ctl.messages.send(str(uuid4()), MessageRequest(text="Missing target"))
    with pytest.raises(FileNotFoundError):
        ctl.messages.send(str(agent.id), MessageRequest(text="Missing sender", sender_id=uuid4()))
    observed = ctl.store.create(Agent(name="Observed", goal="Observe", folder=agent.folder))
    with pytest.raises(ValueError, match="Adopt"):
        ctl.messages.send(str(observed.id), MessageRequest(text="Do not inject"))


def test_backend_restart_keeps_same_lease_until_native_persisted_ack(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    request = MessageRequest(text="Survive controller outage")
    sent = ctl.messages.send(str(agent.id), request)["message"]
    reboot = Controller(Store(ctl.store.home), ctl.processes)
    reboot.messages.tick(reboot.store.get(agent.id))
    assert reboot.messages.list(str(agent.id))["messages"][0]["dispatch_epoch"] == epoch
    ack(directory, agent, sent)
    reboot.messages.tick(reboot.store.get(agent.id))
    assert reboot.messages.list(str(agent.id))["messages"] == []
    assert reboot.messages.send(str(agent.id), request)["reused"]
    assert json.loads((directory / "inbox.json").read_text())["items"] == []


def test_process_death_requeues_message_but_never_replays_uncertain_command(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    sent = ctl.messages.send(str(agent.id), MessageRequest(text="Message"))["message"]
    cmd = ctl.messages.send(
        str(agent.id), MessageRequest(text="/name Example", sender_id=agent.id), "command"
    )["message"]
    ack(directory, agent, cmd, "executing")
    ctl.messages.tick(agent)
    assert ctl.messages.read(agent.id).messages[-1].status == "executing"
    ctl.processes.running.clear()
    ctl.messages.tick(agent)
    states = {m.id: m.status for m in ctl.messages.read(agent.id).messages}
    assert states[UUID(sent["id"])] == "pending"
    assert states[UUID(cmd["id"])] == "uncertain"
    ctl.messages.delete(str(agent.id), cmd["id"])
    new_agent, _, new_epoch = connect_bridge(ctl, ctl.store.get(agent.id))
    ctl.messages.tick(new_agent)
    current = ctl.messages.list(str(agent.id))["messages"]
    assert (
        len(current) == 1
        and current[0]["id"] == sent["id"]
        and current[0]["dispatch_epoch"] == new_epoch
    )


def test_stale_epoch_and_other_session_acks_cannot_consume_pending_delivery(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    sent = ctl.messages.send(str(agent.id), MessageRequest(text="Not yet accepted"))["message"]
    for bad in ({"epoch": str(uuid4())}, {"session_id": str(uuid4())}, {"id": str(uuid4())}):
        ack(directory, agent, sent, **bad)
        ctl.messages.tick(agent)
        assert ctl.messages.list(str(agent.id))["messages"][0]["status"] == "delivering"


@pytest.mark.parametrize(
    "command",
    [
        "/new",
        "/resume x",
        "/fork",
        "/tree",
        "/import x",
        "/quit",
        "!echo bad",
        "/thinking unknown",
        "/model missing",
        "/name",
        "/reload x",
        "/name bad\n/new",
    ],
)
def test_unsafe_session_commands_rejected_before_persistence(setup, command):
    app, ctl, agent = setup
    with TestClient(app) as client:
        response = client.post(
            f"/api/agents/{agent.id}/commands", json={"text": command, "sender_id": str(agent.id)}
        )
        assert response.status_code in (400, 422)
        assert client.get(f"/api/agents/{agent.id}/messages").json()["messages"] == []


def test_self_command_request_survives_controller_outage_and_ingests_once(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    id = uuid4()
    path = directory / "requests" / f"{id}.json"
    atomic_write(
        path,
        json.dumps(
            {
                "id": str(id),
                "sender_id": str(agent.id),
                "kind": "command",
                "text": "/name my session",
                "created_at": now().isoformat(),
            }
        ),
    )
    reboot = Controller(Store(ctl.store.home), ctl.processes)
    reboot.tick()
    pending = reboot.messages.list(str(agent.id))["messages"]
    assert (
        len(pending) == 1
        and pending[0]["kind"] == "command"
        and pending[0]["status"] == "delivering"
    )
    assert not path.exists()
    assert reboot.messages.send(
        str(agent.id),
        MessageRequest(text="/name my session", sender_id=agent.id, request_id=id),
        "command",
    )["reused"]
    ack(directory, agent, pending[0], "delivered")
    reboot.tick()
    assert reboot.messages.list(str(agent.id))["messages"] == []


def test_command_self_boundary_and_other_backend_rejected(setup):
    app, ctl, agent = setup
    with pytest.raises(ValueError, match="sender itself"):
        ctl.messages.send(
            str(agent.id), MessageRequest(text="/reload", sender_id=uuid4()), "command"
        )
    other = ctl.store.create(
        Agent(name="Other backend", goal="Work", folder=agent.folder, backend="other", managed=True)
    )
    with pytest.raises(ValueError, match="only for Pi"):
        ctl.messages.send(str(other.id), MessageRequest(text="/reload"), "command")


def test_limit_and_control_symlink_errors_are_safe(setup, monkeypatch, tmp_path):
    app, ctl, agent = setup
    monkeypatch.setattr("swarm.messaging.MAX_MESSAGES", 2)
    ctl.messages.send(str(agent.id), MessageRequest(text="One"))
    ctl.messages.send(str(agent.id), MessageRequest(text="Two"))
    with pytest.raises(ValueError, match="128 outstanding"):
        ctl.messages.send(str(agent.id), MessageRequest(text="Three"))
    assert len(ctl.messages.list(str(agent.id))["messages"]) == 2
    directory = ctl.messages.control(agent.id)
    directory.parent.mkdir(exist_ok=True)
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        ctl.messages.tick(agent)


def test_expired_heartbeat_does_not_dispatch_and_cleanup_forget_keeps_session(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    heartbeat = {
        "epoch": epoch,
        "session_id": agent.session_id,
        "protocol": 1,
        "updated_at": (now() - timedelta(minutes=2)).isoformat(),
    }
    atomic_write(directory / "heartbeat.json", json.dumps(heartbeat))
    sent = ctl.messages.send(str(agent.id), MessageRequest(text="Wait for bridge"))
    assert sent["message"]["status"] == "pending"
    session = Path(agent.folder) / "history.jsonl"
    session.write_text("original history")
    ctl.store.update(agent.id, {"session_file": str(session)})
    ctl.processes.running.clear()
    ctl.forget(str(agent.id))
    assert session.read_text() == "original history"
    assert not ctl.messages.path(agent.id).exists() and not directory.exists()


def test_auth_and_cross_origin_protect_message_apis(setup, tmp_path):
    app, ctl, agent = setup
    from test_auth import login, password_file

    protected = create_app(
        ctl.store.home,
        recover=False,
        password_file=password_file(tmp_path),
        allowed_hosts=["testserver"],
    )
    protected.state.controller.processes = ctl.processes
    with TestClient(protected) as client:
        base = f"/api/agents/{agent.id}/messages"
        assert client.get(base).status_code == 401
        assert client.post(base, json={"text": "No auth"}).status_code == 401
        headers = login(client)
        response = client.post(base, json={"text": "Authenticated"}, headers=headers)
        assert response.status_code == 202
        assert (
            client.delete(
                base + "/" + response.json()["message"]["id"],
                headers=headers | {"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert client.get(base, headers=headers).json()["messages"][0]["text"] == "Authenticated"


@pytest.mark.parametrize(
    "kind,text", [("message", "Waiting receipt"), ("command", "/name Example")]
)
def test_stale_bridge_does_not_strand_inflight_forever_and_accepts_late_proof(
    setup, monkeypatch, kind, text
):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    sent = ctl.messages.send(
        str(agent.id),
        MessageRequest(text=text, sender_id=agent.id if kind == "command" else None),
        kind,
    )["message"]
    if kind == "command":
        ack(directory, agent, sent, "executing")
    monkeypatch.setattr("swarm.messaging.STALE_DELIVERY_SECONDS", -1)
    (directory / "heartbeat.json").unlink()
    ctl.messages.tick(agent)
    outstanding = ctl.messages.list(str(agent.id))["messages"]
    assert outstanding[0]["status"] == "uncertain"
    assert not json.loads((directory / "inbox.json").read_text())["awake"]
    ack(directory, agent, sent, "delivered")
    ctl.messages.tick(agent)
    assert ctl.messages.list(str(agent.id))["messages"] == []


def test_sleep_report_closes_exposed_inbox_before_monitor_tick(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    ctl.messages.send(str(agent.id), MessageRequest(text="In flight"))
    assert json.loads((directory / "inbox.json").read_text())["awake"]
    Store(ctl.store.home).update(agent.id, {"status": "dormant"})
    assert not json.loads((directory / "inbox.json").read_text())["awake"]
    ctl.messages.tick(agent)  # An earlier fleet-scan snapshot must not reopen it.
    assert not json.loads((directory / "inbox.json").read_text())["awake"]


def test_malformed_request_entry_does_not_starve_valid_self_command(setup):
    app, ctl, agent = setup
    agent, directory, epoch = connect_bridge(ctl, agent)
    (directory / "requests" / "bad.json").symlink_to(directory / "missing")
    request_id = uuid4()
    atomic_write(
        directory / "requests" / f"{request_id}.json",
        json.dumps(
            {
                "id": str(request_id),
                "sender_id": str(agent.id),
                "kind": "command",
                "text": "/name Valid",
                "created_at": now().isoformat(),
            }
        ),
    )
    ctl.messages.tick(agent)
    assert ctl.messages.list(str(agent.id))["messages"][0]["id"] == str(request_id)


@pytest.mark.parametrize("text", ["/name bad\u2028line", "/name bad\u2029line"])
def test_unicode_line_separators_are_rejected_before_command_queue(setup, text):
    app, ctl, agent = setup
    with pytest.raises(ValueError, match="one slash-prefixed line"):
        ctl.messages.send(str(agent.id), MessageRequest(text=text, sender_id=agent.id), "command")
    assert ctl.messages.list(str(agent.id))["messages"] == []
