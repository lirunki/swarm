from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.browser_registry import (
    BrowserRegistryConflict,
    BrowserRegistryError,
    BrowserRegistryUncertain,
)
from swarm.server import create_app

PASSWORD = "browser-registry-api-test-password"
PNG = b"\x89PNG\r\n\x1a\nregistry-api-fixture"
BROWSER_A = "04aa79a2-26de-4b2d-b1ef-2c2eafdcc4a1"
BROWSER_B = "239f94a4-a089-465c-9a7a-f0d66b769559"


class FakeRegistry:
    """Route-boundary fake that never launches or probes a browser."""

    def __init__(self):
        self.calls = []
        self.close_calls = 0
        self.block_legacy_mutations = False

    def legacy_mutation_guard(self):
        if self.block_legacy_mutations:
            raise BrowserRegistryConflict("Use the browser registry and refresh browser instances")

    def list(self):
        self.calls.append(("list",))
        return {
            "revision": 2,
            "browsers": [
                {"id": BROWSER_A, "name": "A", "running": True},
                {"id": BROWSER_B, "name": "B", "running": True},
            ],
        }

    def _lifecycle(self, action, body):
        self.calls.append((action, body))
        if body.get("name") == "bad":
            raise BrowserRegistryError("invalid browser request")
        if body.get("name") == "conflict":
            raise BrowserRegistryConflict("browser operation conflicts")
        if body.get("name") == "uncertain":
            raise BrowserRegistryUncertain("browser outcome is uncertain.", body["request_id"])
        return {"revision": body["revision"] + 1, "operation": {"browser_id": BROWSER_A}}

    def launch(self, body):
        return self._lifecycle("launch", body)

    def add_existing(self, body):
        return self._lifecycle("add_existing", body)

    def rename(self, body):
        return self._lifecycle("rename", body)

    def stop(self, body):
        return self._lifecycle("stop", body)

    def remove(self, body):
        return self._lifecycle("remove", body)

    def request(self, browser_id, action, body=None, query=None):
        self.calls.append(("request", browser_id, action, body, query))
        if browser_id not in {BROWSER_A, BROWSER_B}:
            raise BrowserRegistryError("Browser instance was not found")
        if action == "tabs":
            return {"tabs": [{"id": f"tab-{browser_id}", "title": browser_id}]}
        if action == "screenshot":
            if query["target_id"] == "oversized":
                return PNG[:8] + b"x" * (8 * 1024 * 1024 - 7)
            return PNG
        return {"browser_id": browser_id, "action": action, "body": body}

    def close(self):
        self.close_calls += 1


def password_file(tmp_path):
    path = tmp_path / "password.txt"
    path.write_text(PASSWORD + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def app_with_registry(tmp_path, *, protected=False):
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password_file(tmp_path) if protected else None,
    )
    fake = FakeRegistry()
    app.state.browser_registry = fake
    app.state.controller.processes.health = lambda: {}
    return app, fake


def login(client):
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def lifecycle_body(action):
    common = {"revision": 2, "request_id": str(uuid4())}
    return {
        "launch": common | {"name": "Managed", "executable": None, "headless": True},
        "add": common | {"name": "External", "port": 9222},
        "rename": common | {"id": BROWSER_A, "name": "Renamed"},
        "stop": common | {"id": BROWSER_A},
        "remove": common | {"id": BROWSER_A},
    }[action]


def test_list_and_all_lifecycle_routes_dispatch_exact_bodies(tmp_path):
    app, fake = app_with_registry(tmp_path)
    with TestClient(app) as client:
        listed = client.get("/api/browser/instances")
        responses = {}
        bodies = {}
        for action in ("launch", "add", "rename", "stop", "remove"):
            bodies[action] = lifecycle_body(action)
            responses[action] = client.post(f"/api/browser/instances/{action}", json=bodies[action])

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["browsers"]] == [BROWSER_A, BROWSER_B]
    assert all(response.status_code == 200 for response in responses.values())
    assert fake.calls == [
        ("list",),
        ("launch", bodies["launch"]),
        ("add_existing", bodies["add"]),
        ("rename", bodies["rename"]),
        ("stop", bodies["stop"]),
        ("remove", bodies["remove"]),
    ]
    assert fake.close_calls == 1


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("select", {"target_id": "tab-a"}),
        ("new", {"url": "https://example.test/"}),
        ("close", {"target_id": "tab-a"}),
        ("back", {"target_id": "tab-a"}),
        ("forward", {"target_id": "tab-a"}),
        ("reload", {"target_id": "tab-a"}),
        ("click", {"target_id": "tab-a", "x_ratio": 0.2, "y_ratio": 0.8}),
        (
            "drag",
            {
                "target_id": "tab-a",
                "start": {"x_ratio": 0.1, "y_ratio": 0.2},
                "end": {"x_ratio": 0.8, "y_ratio": 0.9},
            },
        ),
        ("text", {"target_id": "tab-a", "text": "hello"}),
        ("key", {"target_id": "tab-a", "key": "Enter", "modifiers": []}),
        ("copy", {"target_id": "tab-a"}),
    ],
)
def test_instance_mutation_allowlist_scopes_path_id_without_body_injection(action, body, tmp_path):
    app, fake = app_with_registry(tmp_path)
    with TestClient(app) as client:
        response = client.post(f"/api/browser/instances/{BROWSER_B}/{action}", json=body)

    assert response.status_code == 200
    assert response.json()["browser_id"] == BROWSER_B
    assert fake.calls == [("request", BROWSER_B, action, body, None)]


def test_instance_reads_scope_ids_and_screenshot_is_bounded_png(tmp_path):
    app, fake = app_with_registry(tmp_path)
    with TestClient(app) as client:
        tabs_a = client.get(f"/api/browser/instances/{BROWSER_A}/tabs")
        tabs_b = client.get(f"/api/browser/instances/{BROWSER_B}/tabs")
        screenshot = client.get(
            f"/api/browser/instances/{BROWSER_A}/screenshot", params={"target_id": "tab-a"}
        )
        oversized = client.get(
            f"/api/browser/instances/{BROWSER_A}/screenshot",
            params={"target_id": "oversized"},
        )

    assert tabs_a.json()["tabs"][0]["id"] == f"tab-{BROWSER_A}"
    assert tabs_b.json()["tabs"][0]["id"] == f"tab-{BROWSER_B}"
    assert screenshot.status_code == 200
    assert screenshot.content == PNG
    assert screenshot.headers["content-type"] == "image/png"
    assert screenshot.headers["cache-control"] == "no-store"
    assert screenshot.headers["x-content-type-options"] == "nosniff"
    assert oversized.status_code == 400
    assert "8 mib" in oversized.json()["detail"].lower()
    assert all(
        call[1] != BROWSER_B
        for call in fake.calls
        if call[0] == "request" and call[2] == "screenshot"
    )


def test_invalid_unknown_ids_actions_and_query_shapes_are_rejected(tmp_path):
    app, fake = app_with_registry(tmp_path)
    unknown = str(uuid4())
    with TestClient(app) as client:
        invalid_id = client.get("/api/browser/instances/not-a-uuid/tabs")
        unknown_id = client.get(f"/api/browser/instances/{unknown}/tabs")
        bad_read = client.get(f"/api/browser/instances/{BROWSER_A}/eval")
        bad_write = client.post(f"/api/browser/instances/{BROWSER_A}/launch", json={})
        tabs_query = client.get(
            f"/api/browser/instances/{BROWSER_A}/tabs", params={"target_id": "tab-a"}
        )
        screenshot_query = client.get(
            f"/api/browser/instances/{BROWSER_A}/screenshot",
            params={"target_id": "tab-a", "browser_id": BROWSER_B},
        )

    assert invalid_id.status_code == 422
    assert unknown_id.status_code == 400
    assert bad_read.status_code == bad_write.status_code == 400
    assert tabs_query.status_code == screenshot_query.status_code == 400
    assert all(call[2] not in {"eval", "launch"} for call in fake.calls if call[0] == "request")


def test_registry_body_limits_and_errors_map_to_400_or_409(tmp_path):
    app, fake = app_with_registry(tmp_path)
    with TestClient(app) as client:
        malformed = client.post("/api/browser/instances/launch", content=b"{")
        oversized = client.post(
            "/api/browser/instances/launch",
            content=b'{"padding":"' + b"x" * (16 * 1024) + b'"}',
            headers={"Content-Type": "application/json"},
        )
        invalid = client.post(
            "/api/browser/instances/launch",
            json=lifecycle_body("launch") | {"name": "bad"},
        )
        conflict = client.post(
            "/api/browser/instances/launch",
            json=lifecycle_body("launch") | {"name": "conflict"},
        )

    assert malformed.status_code == oversized.status_code == invalid.status_code == 400
    assert conflict.status_code == 409
    assert [call[1]["name"] for call in fake.calls] == ["bad", "conflict"]


def test_uncertain_registry_error_has_safe_structured_409(tmp_path):
    app, _fake = app_with_registry(tmp_path)
    body = lifecycle_body("launch") | {"name": "uncertain"}
    with TestClient(app) as client:
        response = client.post("/api/browser/instances/launch", json=body)

    assert response.status_code == 409
    assert response.json() == {
        "detail": f"browser outcome is uncertain. Request ID: {body['request_id']}",
        "uncertain": True,
        "request_id": body["request_id"],
    }


def test_registry_routes_require_operator_auth_and_same_origin(tmp_path):
    app, fake = app_with_registry(tmp_path, protected=True)
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        service = app.state.access.service_file.read_text(encoding="utf-8").strip()
        service_headers = {"Authorization": "Bearer " + service}
        assert client.get("/api/browser/instances").status_code == 401
        assert client.get("/api/browser/instances", headers=service_headers).status_code == 403

        owner = login(client)
        crossed = owner | {"Origin": "https://evil.example"}
        assert client.get("/api/browser/instances", headers=crossed).status_code == 403
        assert (
            client.post(
                "/api/browser/instances/stop", json=lifecycle_body("stop"), headers=crossed
            ).status_code
            == 403
        )
        accepted = client.get(
            "/api/browser/instances", headers=owner | {"Origin": "http://testserver"}
        )

    assert accepted.status_code == 200
    assert fake.calls == [("list",)]


def test_legacy_mutation_routes_are_blocked_after_registry_ownership(tmp_path):
    app, fake = app_with_registry(tmp_path)
    fake.block_legacy_mutations = True
    app.state.remote_browser.launch = lambda _request_id: pytest.fail("legacy launch called")
    app.state.remote_browser.stop = lambda _request_id: pytest.fail("legacy stop called")
    app.state.remote_browser.status = lambda: {"enabled": True, "running": True}
    body = {"request_id": str(uuid4())}

    with TestClient(app) as client:
        launched = client.post("/api/browser/launch", json=body)
        stopped = client.post("/api/browser/stop", json=body)
        status = client.get("/api/browser/status")

    assert launched.status_code == stopped.status_code == 409
    assert "Use the browser registry" in launched.json()["detail"]
    assert status.status_code == 200
    assert status.json()["running"] is True


def test_legacy_singleton_and_discovery_routes_remain_available(tmp_path):
    app, fake = app_with_registry(tmp_path)
    app.state.remote_browser.discovery = lambda: {"available": True, "executables": []}
    app.state.remote_browser.status = lambda: {"enabled": False, "running": False}
    with TestClient(app) as client:
        discovery = client.get("/api/browser/discovery")
        legacy = client.get("/api/browser/status")

    assert discovery.status_code == 200
    assert discovery.json()["available"] is True
    assert legacy.status_code == 200
    assert legacy.json() == {"enabled": False, "running": False}
    assert fake.calls == []


def test_viewer_screenshot_query_is_accepted_by_the_actual_route(tmp_path):
    import re
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "src/swarm/static/remote-browser.js"
    ).read_text()
    # Exercise the exact template used by the viewer rather than a hand-built API URL.
    template = re.search(r"const suffix = `([^`]*screenshot\?[^`]*)`;", script).group(1)
    query = template.replace("${encodeURIComponent(requestedTarget)}", "tab-a")
    assert query == "screenshot?target_id=tab-a"
    app, fake = app_with_registry(tmp_path)
    with TestClient(app) as client:
        response = client.get(f"/api/browser/instances/{BROWSER_A}/{query}")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["cache-control"] == "no-store"
    assert ("request", BROWSER_A, "screenshot", None, {"target_id": "tab-a"}) in fake.calls
