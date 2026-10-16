import json
import socket
import time
import urllib.error
import urllib.request

from test_federation_integration import running_server, wait_for_remote
from websockets.sync.client import connect

from swarm.relay import create_relay_app
from swarm.server import create_app

KEY = "settings-integration-key-0123456789abcd"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def call(base, path, body=None, method=None):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def wait_connected(base, expected=True):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, value = call(base, "/api/federation/settings")
        if value["connected"] is expected:
            return value
        time.sleep(0.03)
    raise AssertionError(f"Relay status never became connected={expected}")


def presence_until(websocket, predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = json.loads(websocket.recv(timeout=5))
        if predicate(value):
            return value
    raise AssertionError("Expected relay presence event was not delivered")


def test_runtime_connect_disconnect_and_reconnect_preserve_presence_subscription(tmp_path):
    with running_server(create_relay_app([KEY])) as port:
        relay = f"ws://127.0.0.1:{port}/v1/connect"
        a, b = free_port(), free_port()
        base_a, base_b = f"http://127.0.0.1:{a}", f"http://127.0.0.1:{b}"
        app_a = create_app(tmp_path / "a", recover=False, api_url=base_a)
        app_b = create_app(tmp_path / "b", recover=False, api_url=base_b)
        with running_server(app_a, a), running_server(app_b, b):
            for base, node in ((base_a, "alpha"), (base_b, "beta")):
                status, value = call(
                    base,
                    "/api/federation/connect",
                    {
                        "relay_url": relay,
                        "relay_node_id": node,
                        "relay_name": node.title(),
                        "relay_api_key": KEY,
                    },
                )
                assert status == 202 and KEY not in json.dumps(value)
                wait_connected(base)
            wait_for_remote(base_a, "beta")
            wait_for_remote(base_b, "alpha")
            original = app_a.state.federation
            with connect(base_a.replace("http:", "ws:") + "/ws/federation/presence") as ws:
                presence_until(ws, lambda value: any(n["id"] == "beta" for n in value["nodes"]))
                status, value = call(base_a, "/api/federation/disconnect", method="POST")
                assert status == 200 and not value["enabled"]
                presence_until(ws, lambda value: not value["enabled"])
                assert not original.ready
                status, value = call(
                    base_a,
                    "/api/federation/connect",
                    {
                        "relay_url": relay,
                        "relay_node_id": "alpha",
                        "relay_name": "Alpha",
                        "relay_api_key": None,
                    },
                )
                assert status == 202 and value["has_api_key"]
                wait_connected(base_a)
                presence_until(
                    ws,
                    lambda value: (
                        value["enabled"] and any(n["id"] == "beta" for n in value["nodes"])
                    ),
                )
                assert app_a.state.federation is original
            wait_for_remote(base_b, "alpha")
            status, value = call(base_b, "/api/federation/alpha/proxy/api/state")
            assert status == 200 and value["agents"] == []
            status, value = call(base_b, "/api/federation/alpha/proxy/api/federation/settings")
            assert status == 502 and "forbidden" in value["detail"]


def test_authentication_failure_is_visible_and_never_returns_or_logs_key(tmp_path, caplog):
    with running_server(create_relay_app([KEY])) as port:
        own = free_port()
        base = f"http://127.0.0.1:{own}"
        app = create_app(tmp_path, recover=False, api_url=base)
        with running_server(app, own):
            wrong = "wrong-key-0123456789-abcdefghijklmnopqrstuvwxyz"
            status, _ = call(
                base,
                "/api/federation/connect",
                {
                    "relay_url": f"ws://127.0.0.1:{port}/v1/connect",
                    "relay_node_id": "alpha",
                    "relay_name": "Alpha",
                    "relay_api_key": wrong,
                },
            )
            assert status == 202
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, value = call(base, "/api/federation/settings")
                if value["last_error"]:
                    break
                time.sleep(0.03)
            assert value["enabled"] and not value["connected"] and value["status"] == "reconnecting"
            assert value["last_error"] and wrong not in json.dumps(value)
            assert wrong not in caplog.text and KEY not in caplog.text
            assert call(base, "/api/federation/disconnect", method="POST")[0] == 200
