import json
from uuid import uuid4

from test_messaging import FakeProcesses, ack, connect_bridge
from test_password_relay import http
from test_password_relay import pair as pair

from swarm.models import Agent


def test_messages_queue_on_destination_relay_node_then_deliver_without_gateway_tty_input(pair):
    app_a, app_b, base, headers = pair
    ctl = app_b.state.controller
    ctl.processes = FakeProcesses()
    agent = ctl.store.create(
        Agent(
            name="Remote mailbox",
            goal="Later",
            folder=str(ctl.store.home),
            managed=True,
            status="idle",
            auto_resume=False,
            session_id=str(uuid4()),
        )
    )
    prefix = "/api/federation/beta/proxy"
    path = prefix + f"/api/agents/{agent.id}/messages"
    assert http(base, path, "POST", {"text": "Unauthorized"}, headers)[0] == 401
    _, login = http(
        base,
        prefix + "/api/auth/login",
        "POST",
        {"password": "beta-separate-test-password"},
        headers,
    )
    remote = headers | {"X-Swarm-Authorization": "Bearer " + login["token"]}
    id = str(uuid4())
    status, result = http(
        base, path, "POST", {"text": "Keep until this remote agent wakes", "request_id": id}, remote
    )
    assert status == 202 and result["message"]["status"] == "pending"
    assert http(base, path, headers=remote)[1]["messages"][0]["id"] == id
    assert not ctl.processes.launches
    assert app_a.state.controller.store.scan() == []
    agent, directory, _ = connect_bridge(ctl, agent)
    ctl.messages.tick(agent)
    message = ctl.messages.list(str(agent.id))["messages"][0]
    assert message["status"] == "delivering"
    assert json.loads((directory / "inbox.json").read_text())["items"][0]["id"] == id
    assert http(base, path + "/" + id, "DELETE", headers=remote)[0] == 409
    ack(directory, agent, message)
    ctl.messages.tick(agent)
    assert http(base, path, headers=remote)[1]["messages"] == []
    ctl.processes.running.clear()
    status, second = http(base, path, "POST", {"text": "Cancel before awake"}, remote)
    assert status == 202
    assert http(base, path + "/" + second["message"]["id"], "DELETE", headers=remote)[1]["deleted"]
    assert ctl.messages.list(str(agent.id))["messages"] == []
