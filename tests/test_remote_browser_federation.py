import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path

import uvicorn

from swarm.federation import FederationConfig
from swarm.relay import create_relay_app
from swarm.server import create_app

MESH_KEY = "remote-browser-federation-key-0123456789"
PNG = b"\x89PNG\r\n\x1a\n" + b"federated-browser-pixel" * 95_326
BROWSER_A = "04aa79a2-26de-4b2d-b1ef-2c2eafdcc4a1"
BROWSER_B = "239f94a4-a089-465c-9a7a-f0d66b769559"


class FakeRemoteBrowser:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def status(self):
        self.calls.append(("status",))
        return {"enabled": True, "running": True}

    def launch(self, request_id):
        self.calls.append(("launch", request_id))
        return {"running": True}

    def request(self, action, body=None, query=None):
        self.calls.append(("request", action, body, query))
        if action == "tabs":
            return {"tabs": [{"target_id": "page1", "title": "Remote tab"}]}
        if action == "screenshot":
            assert query == {"target_id": "page1"}
            return PNG
        if action == "click":
            assert body == {"target_id": "page1", "x_ratio": 0.25, "y_ratio": 0.75}
            return {"clicked": True}
        raise AssertionError(f"unexpected browser action: {action}")


class FakeBrowserRegistry:
    """Federation boundary fake; no browser is launched or probed."""

    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def list(self):
        self.calls.append(("list",))
        return {
            "revision": 4,
            "browsers": [
                {"id": BROWSER_A, "name": "Managed A", "running": True},
                {"id": BROWSER_B, "name": "External B", "running": True},
            ],
        }

    def add_existing(self, body):
        self.calls.append(("add_existing", body))
        return {"revision": 5, "operation": {"browser_id": BROWSER_B}}

    def request(self, browser_id, action, body=None, query=None):
        self.calls.append(("request", browser_id, action, body, query))
        if browser_id not in {BROWSER_A, BROWSER_B}:
            raise ValueError("Browser instance was not found")
        if action == "tabs":
            return {"tabs": [{"id": f"tab-{browser_id}", "browser_id": browser_id}]}
        if action == "screenshot":
            return PNG
        return {"browser_id": browser_id, "action": action}


@contextmanager
def running_server(app, port=0):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("test server did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def password_file(directory: Path, password: str) -> Path:
    path = directory / "password.txt"
    path.write_text(password + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def request(base, path, method="GET", body=None, headers=None):
    response = None
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, response.headers, response.read()


def json_request(base, path, method="GET", body=None, headers=None):
    status, response_headers, content = request(base, path, method, body, headers)
    return status, json.loads(content), response_headers


def login(base, password, headers=None, path="/api/auth/login"):
    status, value, _ = json_request(base, path, "POST", {"password": password}, headers)
    assert status == 200
    return "Bearer " + value["token"]


def test_protected_remote_browser_routes_use_existing_federated_rpc(tmp_path):
    with ExitStack() as stack:
        relay_port = stack.enter_context(running_server(create_relay_app([MESH_KEY])))
        probes = [socket.socket(), socket.socket()]
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        port_a, port_b = [probe.getsockname()[1] for probe in probes]
        for probe in probes:
            probe.close()

        apps = []
        fakes = []
        for node, port in (("alpha", port_a), ("beta", port_b)):
            root = tmp_path / node
            root.mkdir()
            app = create_app(
                root / "home",
                recover=False,
                api_url=f"http://127.0.0.1:{port}",
                password_file=password_file(root, f"{node}-browser-password"),
                federation_config=FederationConfig(
                    f"ws://127.0.0.1:{relay_port}/v1/connect",
                    node,
                    node.title(),
                    MESH_KEY,
                    f"http://127.0.0.1:{port}",
                ),
            )
            fake = FakeRemoteBrowser()
            app.state.remote_browser = fake
            apps.append(app)
            fakes.append(fake)
            stack.enter_context(running_server(app, port))

        app_a, _app_b = apps
        browser_a, browser_b = fakes
        base = f"http://127.0.0.1:{port_a}"
        prefix = "/api/federation/beta/proxy/api/browser"

        # Both the gateway and destination are independently protected.
        assert request(base, prefix + "/status")[0] == 401
        local_headers = {"Authorization": login(base, "alpha-browser-password")}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, value, _ = json_request(base, "/api/federation/nodes", headers=local_headers)
            if status == 200 and any(
                node["id"] == "beta" and node["state_ready"] for node in value["nodes"]
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("protected relay peer did not become ready")

        assert request(base, prefix + "/status", headers=local_headers)[0] == 401
        assert browser_a.calls == browser_b.calls == []

        remote_login = "/api/federation/beta/proxy/api/auth/login"
        remote_token = login(base, "beta-browser-password", local_headers, path=remote_login)
        remote_headers = local_headers | {"X-Swarm-Authorization": remote_token}
        # Logging in must not create a global destination credential cache.
        assert request(base, prefix + "/tabs", headers=local_headers)[0] == 401

        status, value, _ = json_request(base, prefix + "/status", headers=remote_headers)
        assert status == 200 and value == {"enabled": True, "running": True}
        status, value, _ = json_request(base, prefix + "/tabs", headers=remote_headers)
        assert status == 200 and value["tabs"][0]["target_id"] == "page1"

        status, screenshot_headers, screenshot = request(
            base, prefix + "/screenshot?target_id=page1", headers=remote_headers
        )
        assert status == 200
        assert screenshot_headers.get_content_type() == "image/png"
        assert len(PNG) > 2 * 1024 * 1024
        assert screenshot == PNG

        status, value, _ = json_request(
            base,
            prefix + "/launch",
            "POST",
            {"request_id": "783761ac-26ef-4bb9-8f63-90268e7659a3"},
            remote_headers,
        )
        assert status == 200 and value == {"running": True}

        calls_before_cross_origin = list(browser_b.calls)
        crossed = remote_headers | {"Origin": "https://evil.example"}
        assert (
            json_request(
                base,
                prefix + "/click",
                "POST",
                {"target_id": "page1", "x_ratio": 0.25, "y_ratio": 0.75},
                crossed,
            )[0]
            == 403
        )
        assert browser_b.calls == calls_before_cross_origin

        status, value, _ = json_request(
            base,
            prefix + "/click",
            "POST",
            {"target_id": "page1", "x_ratio": 0.25, "y_ratio": 0.75},
            remote_headers,
        )
        assert status == 200 and value == {"clicked": True}

        # Owner-only browser routes reject A's local agent-service credential.
        local_service = app_a.state.access.service_file.read_text(encoding="utf-8").strip()
        service_headers = {
            "Authorization": "Bearer " + local_service,
            "X-Swarm-Authorization": remote_token,
        }
        calls_before_service = list(browser_b.calls)
        assert request(base, prefix + "/tabs", headers=service_headers)[0] == 403
        assert browser_b.calls == calls_before_service

        # Browser RPCs run only on B and do not alter B's normal grouped state.
        assert browser_a.calls == []
        status, groups, _ = json_request(
            base, "/api/federation/beta/proxy/api/groups", headers=remote_headers
        )
        assert status == 200
        assert groups == {"revision": 0, "groups": [], "assignments": {}}
        status, nodes, _ = json_request(base, "/api/federation/nodes", headers=local_headers)
        beta = next(node for node in nodes["nodes"] if node["id"] == "beta")
        assert status == 200 and beta["state_ready"] is True

        assert browser_b.calls == [
            ("status",),
            ("request", "tabs", None, {}),
            ("request", "screenshot", None, {"target_id": "page1"}),
            ("launch", "783761ac-26ef-4bb9-8f63-90268e7659a3"),
            (
                "request",
                "click",
                {"target_id": "page1", "x_ratio": 0.25, "y_ratio": 0.75},
                None,
            ),
        ]


def test_multi_instance_browser_registry_routes_federate_to_destination_only(tmp_path):
    with ExitStack() as stack:
        relay_port = stack.enter_context(running_server(create_relay_app([MESH_KEY])))
        probes = [socket.socket(), socket.socket()]
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        port_a, port_b = [probe.getsockname()[1] for probe in probes]
        for probe in probes:
            probe.close()

        apps = []
        registries = []
        for node, port in (("alpha", port_a), ("beta", port_b)):
            root = tmp_path / node
            root.mkdir()
            app = create_app(
                root / "home",
                recover=False,
                api_url=f"http://127.0.0.1:{port}",
                password_file=password_file(root, f"{node}-browser-password"),
                federation_config=FederationConfig(
                    f"ws://127.0.0.1:{relay_port}/v1/connect",
                    node,
                    node.title(),
                    MESH_KEY,
                    f"http://127.0.0.1:{port}",
                ),
            )
            registry = FakeBrowserRegistry()
            app.state.browser_registry = registry
            apps.append(app)
            registries.append(registry)
            stack.enter_context(running_server(app, port))

        app_a, _app_b = apps
        registry_a, registry_b = registries
        base = f"http://127.0.0.1:{port_a}"
        prefix = "/api/federation/beta/proxy/api/browser/instances"
        local_headers = {"Authorization": login(base, "alpha-browser-password")}

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, value, _ = json_request(base, "/api/federation/nodes", headers=local_headers)
            if status == 200 and any(
                node["id"] == "beta" and node["state_ready"] for node in value["nodes"]
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("protected relay peer did not become ready")

        remote_token = login(
            base,
            "beta-browser-password",
            local_headers,
            path="/api/federation/beta/proxy/api/auth/login",
        )
        remote_headers = local_headers | {"X-Swarm-Authorization": remote_token}

        status, listed, _ = json_request(base, prefix, headers=remote_headers)
        assert status == 200
        assert [item["id"] for item in listed["browsers"]] == [BROWSER_A, BROWSER_B]

        add_body = {
            "revision": 4,
            "request_id": "6cadf343-e1fb-47d8-9940-f7b212a7e342",
            "name": "Loopback external",
            "port": 9222,
        }
        status, added, _ = json_request(
            base, prefix + "/add", "POST", add_body, remote_headers
        )
        assert status == 200
        assert added["operation"]["browser_id"] == BROWSER_B

        status, tabs_a, _ = json_request(
            base, f"{prefix}/{BROWSER_A}/tabs", headers=remote_headers
        )
        assert status == 200
        status, tabs_b, _ = json_request(
            base, f"{prefix}/{BROWSER_B}/tabs", headers=remote_headers
        )
        assert status == 200
        assert tabs_a["tabs"][0]["browser_id"] == BROWSER_A
        assert tabs_b["tabs"][0]["browser_id"] == BROWSER_B

        status, clicked, _ = json_request(
            base,
            f"{prefix}/{BROWSER_B}/click",
            "POST",
            {"target_id": "tab-b", "x_ratio": 0.5, "y_ratio": 0.5},
            remote_headers,
        )
        assert status == 200
        assert clicked == {"browser_id": BROWSER_B, "action": "click"}

        # Local service credentials remain forbidden on browser-prefixed gateway paths.
        local_service = app_a.state.access.service_file.read_text(encoding="utf-8").strip()
        denied_headers = {
            "Authorization": "Bearer " + local_service,
            "X-Swarm-Authorization": remote_token,
        }
        assert request(base, prefix, headers=denied_headers)[0] == 403

        assert registry_a.calls == []
        assert registry_b.calls == [
            ("list",),
            ("add_existing", add_body),
            ("request", BROWSER_A, "tabs", None, {}),
            ("request", BROWSER_B, "tabs", None, {}),
            (
                "request",
                BROWSER_B,
                "click",
                {"target_id": "tab-b", "x_ratio": 0.5, "y_ratio": 0.5},
                None,
            ),
        ]
