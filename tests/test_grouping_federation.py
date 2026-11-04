import json
import socket
import time
from contextlib import ExitStack
from uuid import UUID, uuid4

from test_auth import password_file
from test_federation_integration import running_server
from test_password_relay import http

from swarm.federation import FederationConfig
from swarm.models import Agent
from swarm.processes import Processes
from swarm.relay import create_relay_app
from swarm.server import create_app


def wait_for_authenticated_remote(base, node_id, headers):
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        status, last = http(base, "/api/federation/nodes", headers=headers)
        if status == 200 and any(
            node["id"] == node_id and node["state_ready"] for node in last["nodes"]
        ):
            return last
        time.sleep(0.05)
    raise AssertionError(f"remote node did not become ready: {last}")


def test_remote_groups_are_authenticated_family_aware_and_destination_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(Processes, "health", lambda self: {"tmux": True, "pi": True})

    def no_tmux(self, *args, **kwargs):
        raise AssertionError(f"grouping must not invoke tmux: {args!r} {kwargs!r}")

    monkeypatch.setattr(Processes, "run", no_tmux)

    key = "test-grouping-federation-key-0123456789"
    with ExitStack() as stack:
        relay_port = stack.enter_context(running_server(create_relay_app([key])))
        probes = [socket.socket(), socket.socket()]
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        alpha_port, beta_port = [probe.getsockname()[1] for probe in probes]
        for probe in probes:
            probe.close()

        apps = []
        passwords = {
            "alpha": "alpha-private-grouping-password",
            "beta": "beta-private-grouping-password",
        }
        for node, port in (("alpha", alpha_port), ("beta", beta_port)):
            node_root = tmp_path / node
            node_root.mkdir()
            private_password = password_file(node_root, passwords[node])
            assert private_password.stat().st_mode & 0o077 == 0
            base = f"http://127.0.0.1:{port}"
            app = create_app(
                node_root / "home",
                pi="/test-only/no-pi",
                recover=False,
                api_url=base,
                password_file=private_password,
                federation_config=FederationConfig(
                    f"ws://127.0.0.1:{relay_port}/v1/connect",
                    node,
                    node.title(),
                    key,
                    base,
                ),
            )
            apps.append(app)

        app_alpha, app_beta = apps
        remote_root = app_beta.state.controller.store.create(
            Agent(
                id=uuid4(),
                name="Remote root",
                goal="Group this family without starting it",
                folder=str(tmp_path / "remote-work"),
                managed=False,
                auto_resume=False,
            )
        )
        remote_child = app_beta.state.controller.store.create(
            Agent(
                id=uuid4(),
                name="Remote child",
                goal="Follow the root's group",
                folder=str(tmp_path / "remote-work"),
                parent_id=remote_root.id,
                managed=False,
                auto_resume=False,
            )
        )
        assert all(
            UUID(str(agent.id)) and not agent.managed for agent in (remote_root, remote_child)
        )

        stack.enter_context(running_server(app_alpha, alpha_port))
        stack.enter_context(running_server(app_beta, beta_port))
        alpha_base = f"http://127.0.0.1:{alpha_port}"
        beta_base = f"http://127.0.0.1:{beta_port}"

        alpha_status, alpha_login = http(
            alpha_base,
            "/api/auth/login",
            "POST",
            {"password": passwords["alpha"]},
        )
        beta_status, beta_login = http(
            beta_base,
            "/api/auth/login",
            "POST",
            {"password": passwords["beta"]},
        )
        assert (alpha_status, beta_status) == (200, 200)
        local_auth = {"Authorization": "Bearer " + alpha_login["token"]}
        # The gateway forwards this browser's destination token to beta.
        both_auth = local_auth | {
            "X-Swarm-Authorization": "Bearer " + beta_login["token"]
        }
        wait_for_authenticated_remote(alpha_base, "beta", local_auth)

        prefix = "/api/federation/beta/proxy/api/groups"
        assert http(alpha_base, prefix)[0] in {401, 403}
        assert http(alpha_base, prefix, headers=local_auth)[0] == 401
        assert http(alpha_base, prefix, headers=both_auth) == (
            200,
            {"revision": 0, "groups": [], "assignments": {}},
        )

        unsafe_name = "<img src=x onerror=alert(1)>"
        request_id = str(uuid4())
        body = {
            "action": "create",
            "revision": 0,
            "request_id": request_id,
            "name": unsafe_name,
            "agent_ids": [str(remote_child.id)],
        }
        status, created = http(alpha_base, prefix, "POST", body, both_auth)
        assert status == 200
        group_id = created["operation"]["group_id"]
        UUID(group_id)
        expected = {
            "revision": 1,
            "groups": [{"id": group_id, "name": unsafe_name}],
            "assignments": {
                str(remote_root.id): group_id,
                str(remote_child.id): group_id,
            },
        }
        assert {key: created[key] for key in expected} == expected

        assert http(alpha_base, prefix, "POST", body, both_auth) == (200, created)
        assert http(alpha_base, prefix, headers=both_auth) == (200, expected)
        state_status, remote_state = http(
            alpha_base,
            "/api/federation/beta/proxy/api/state",
            headers=both_auth,
        )
        assert state_status == 200
        assert remote_state["agent_groups"] == expected
        assert remote_state["agent_groups_error"] is None

        assert app_beta.state.controller.groups.get() == expected
        shared = json.loads(app_beta.state.controller.groups.path.read_text(encoding="utf-8"))
        assert {key: shared[key] for key in expected} == expected
        assert shared["receipts"][0]["request_id"] == request_id
        assert app_alpha.state.controller.groups.get() == {
            "revision": 0,
            "groups": [],
            "assignments": {},
        }
        assert app_alpha.state.controller.store.scan() == []
        assert all(
            not agent.managed and agent.pid is None and agent.wake_at is None
            for agent in app_beta.state.controller.store.scan()
        )
