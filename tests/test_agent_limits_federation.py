import json
import socket
from contextlib import ExitStack
from uuid import uuid4

from test_grouping_federation import (
    http,
    password_file,
    running_server,
    wait_for_authenticated_remote,
)

from swarm.agent_limits import AgentLimits
from swarm.federation import FederationConfig
from swarm.models import Agent, Status
from swarm.processes import Processes
from swarm.relay import create_relay_app
from swarm.server import create_app
from swarm.store import Store


def reserve_ports(count):
    probes = [socket.socket() for _ in range(count)]
    try:
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        return [probe.getsockname()[1] for probe in probes]
    finally:
        for probe in probes:
            probe.close()


def add_record(app, folder, name, *, managed=True, auto_resume=False, status=Status.done):
    work = folder / name
    work.mkdir()
    return app.state.controller.store.create(
        Agent(
            name=name,
            goal="Exercise federated agent-limit accounting",
            folder=str(work),
            managed=managed,
            auto_resume=auto_resume,
            status=status,
        )
    )


def login(base, password):
    status, result = http(base, "/api/auth/login", "POST", {"password": password})
    assert status == 200
    return {"Authorization": "Bearer " + result["token"]}


def mutation(limit, revision, request_id=None):
    return {
        "max_active_managed": limit,
        "revision": revision,
        "request_id": str(request_id or uuid4()),
    }


def test_password_authenticated_agent_limits_are_destination_owned_and_durable(
    tmp_path, monkeypatch
):
    alive_names = {"remote-managed-done-alive", "remote-orphan-alive"}
    monkeypatch.setattr(Processes, "alive", lambda self, agent: agent.name in alive_names)
    monkeypatch.setattr(Processes, "health", lambda self: {"tmux": True, "pi": True})

    key = "test-agent-limits-federation-key-0123456789"
    alpha_password = "alpha-private-agent-limits-password"
    beta_password = "beta-private-agent-limits-password"

    with ExitStack() as stack:
        relay_port = stack.enter_context(running_server(create_relay_app([key])))
        alpha_port, beta_port = reserve_ports(2)
        alpha_base = f"http://127.0.0.1:{alpha_port}"
        beta_base = f"http://127.0.0.1:{beta_port}"

        apps = []
        for node, port, password in (
            ("alpha", alpha_port, alpha_password),
            ("beta", beta_port, beta_password),
        ):
            root = tmp_path / node
            root.mkdir()
            app = create_app(
                root / "home",
                pi="/test-only/no-pi",
                recover=False,
                api_url=f"http://127.0.0.1:{port}",
                password_file=password_file(root, password),
                federation_config=FederationConfig(
                    f"ws://127.0.0.1:{relay_port}/v1/connect",
                    node,
                    node.title(),
                    key,
                    f"http://127.0.0.1:{port}",
                ),
            )
            apps.append(app)

        app_alpha, app_beta = apps
        add_record(
            app_alpha,
            tmp_path,
            "remote-managed-waiting",
            auto_resume=True,
            status=Status.waiting,
        )
        done_alive = add_record(app_alpha, tmp_path, "remote-managed-done-alive")
        orphan = add_record(
            app_alpha,
            tmp_path,
            "remote-orphan-alive",
            managed=False,
        )
        add_record(app_alpha, tmp_path, "remote-managed-done-stopped")
        add_record(
            app_beta,
            tmp_path,
            "local-managed-waiting",
            auto_resume=True,
            status=Status.waiting,
        )

        stack.enter_context(running_server(app_alpha, alpha_port))
        stack.enter_context(running_server(app_beta, beta_port))
        alpha_auth = login(alpha_base, alpha_password)
        beta_auth = login(beta_base, beta_password)
        both_auth = beta_auth | {
            "X-Swarm-Authorization": alpha_auth["Authorization"]
        }
        wait_for_authenticated_remote(beta_base, "alpha", beta_auth)

        prefix = "/api/federation/alpha/proxy/api/agent-limits"
        assert http(beta_base, prefix)[0] == 401
        assert http(beta_base, prefix, headers=beta_auth)[0] == 401

        expected_remote_default = {
            "revision": 0,
            "max_active_managed": 20,
            "active_managed": 2,
            "scope": "orchestration",
        }
        expected_local_default = expected_remote_default | {"active_managed": 1}
        assert http(beta_base, prefix, headers=both_auth) == (
            200,
            expected_remote_default,
        )
        assert http(beta_base, "/api/agent-limits", headers=beta_auth) == (
            200,
            expected_local_default,
        )
        assert done_alive.managed and done_alive.status == Status.done
        assert not orphan.managed and orphan.name in alive_names

        request_id = uuid4()
        request = mutation(37, 0, request_id)
        status, updated = http(beta_base, prefix, "POST", request, both_auth)
        assert status == 200
        expected_updated = expected_remote_default | {
            "revision": 1,
            "max_active_managed": 37,
        }
        assert updated == expected_updated

        remote_path = app_alpha.state.controller.agent_limits.path
        saved = json.loads(remote_path.read_text(encoding="utf-8"))
        assert saved["revision"] == 1
        assert saved["max_active_managed"] == 37
        assert saved["receipts"][-1]["request_id"] == str(request_id)
        assert not app_beta.state.controller.agent_limits.path.exists()
        assert http(beta_base, "/api/agent-limits", headers=beta_auth) == (
            200,
            expected_local_default,
        )

        state_status, state = http(
            beta_base,
            "/api/federation/alpha/proxy/api/state",
            headers=both_auth,
        )
        assert state_status == 200
        assert state["agent_limits"] == expected_updated
        assert state["agent_limits_error"] is None

        fresh_limits = AgentLimits(Store(app_alpha.state.controller.store.home))
        assert fresh_limits.path == remote_path
        assert fresh_limits.get(2) == expected_updated

        assert http(beta_base, prefix, "POST", request, both_auth) == (200, updated)
        stale = mutation(36, 0)
        assert http(beta_base, prefix, "POST", stale, both_auth)[0] == 409
        assert http(beta_base, prefix, headers=both_auth) == (200, expected_updated)

        service = app_alpha.state.access.service_file.read_text(encoding="utf-8").strip()
        service_auth = {"Authorization": "Bearer " + service}
        assert http(alpha_base, "/api/agent-limits", "POST", mutation(36, 1), service_auth)[0] == 403
        assert http(
            beta_base,
            prefix,
            "POST",
            mutation(36, 1),
            beta_auth | {"X-Swarm-Authorization": service_auth["Authorization"]},
        )[0] == 401
        assert http(beta_base, prefix, headers=both_auth) == (200, expected_updated)

        records_before = {
            str(agent.id): agent.model_dump(mode="json")
            for agent in app_alpha.state.controller.store.scan()
        }
        lower_status, lowered = http(
            beta_base,
            prefix,
            "POST",
            mutation(1, 1),
            both_auth,
        )
        assert lower_status == 200
        assert lowered == expected_updated | {"revision": 2, "max_active_managed": 1}
        records_after = {
            str(agent.id): agent.model_dump(mode="json")
            for agent in app_alpha.state.controller.store.scan()
        }
        assert records_after == records_before
        assert http(beta_base, "/api/agent-limits", headers=beta_auth) == (
            200,
            expected_local_default,
        )
