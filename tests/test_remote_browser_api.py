import json
import sys
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.server import create_app

PASSWORD = "remote-browser-api-test-password"
PNG = b"\x89PNG\r\n\x1a\nlocal-api-fixture"
TARGET_ID = "page-1"


class FakeRemoteBrowser:
    """Route-boundary fake: never imports pychrome or starts a process."""

    def __init__(self):
        self.running = False
        self.status_calls = 0
        self.launch_calls = []
        self.stop_calls = []
        self.request_calls = []
        self.close_calls = 0

    def status(self):
        self.status_calls += 1
        return {
            "enabled": True,
            "running": self.running,
            "state": "running" if self.running else "stopped",
            "error": None,
        }

    def launch(self, request_id):
        self.launch_calls.append(request_id)
        self.running = True
        return {"running": True, "started": True, "browser": "Chromium/Fake"}

    def stop(self, request_id):
        self.stop_calls.append(request_id)
        stopped = self.running
        self.running = False
        return {"running": False, "stopped": stopped}

    def request(self, action, body=None, query=None):
        self.request_calls.append((action, body, query))
        data = query if query is not None else body or {}
        if action == "tabs":
            return {"tabs": [{"id": TARGET_ID, "title": "Fixture", "url": "about:blank"}]}
        if action == "screenshot":
            self._target(data)
            if data["target_id"] == "oversized":
                return PNG[:8] + b"x" * (8 * 1024 * 1024 - 7)
            return PNG
        if action == "new":
            return {"id": TARGET_ID, "url": data.get("url")}
        self._target(data)
        return {"ok": True, "action": action}

    @staticmethod
    def _target(data):
        target = data.get("target_id")
        if not isinstance(target, str) or not target:
            raise ValueError("target_id must be a non-empty string")
        if target not in {TARGET_ID, "oversized"}:
            raise ValueError("target_id is not an exposed page target")

    def close(self):
        self.close_calls += 1


def password_file(tmp_path):
    path = tmp_path / "browser-password.txt"
    path.write_text(PASSWORD + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def browser_settings(*, revision=0, enabled=True, headless=True, request_id=None):
    return {
        "revision": revision,
        "request_id": str(request_id or uuid4()),
        "enabled": enabled,
        "headless": headless,
        "executable": None,
    }


def app_with_fake(tmp_path, *, protected=False):
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password_file(tmp_path) if protected else None,
    )
    fake = FakeRemoteBrowser()
    app.state.remote_browser = fake
    # An empty registry does not need to ask the configured pi executable for health.
    app.state.controller.processes.health = lambda: {}
    return app, fake


def login(client):
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_default_disabled_status_and_tabs_are_side_effect_free(tmp_path, monkeypatch):
    home = tmp_path / "home"
    app = create_app(home, recover=False, allowed_hosts=["testserver"])
    app.state.controller.processes.health = lambda: {}
    remote = app.state.remote_browser
    monkeypatch.delitem(sys.modules, "pychrome", raising=False)

    with TestClient(app) as client:
        status = client.get("/api/browser/status")
        tabs = client.get("/api/browser/tabs")

    assert status.status_code == 200
    assert status.json() == {
        "enabled": False,
        "running": False,
        "state": "disabled",
        "error": None,
    }
    assert tabs.status_code == 409
    assert "disabled" in tabs.json()["detail"].lower()
    assert remote._browser is None
    assert "pychrome" not in sys.modules
    assert not remote.root.exists()
    assert not remote.profile.exists()


def test_browser_settings_get_is_default_and_unknown_front_routes_are_closed(tmp_path):
    app, _fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/browser/settings")
        assert response.status_code == 200
        assert response.json() == {
            "revision": 0,
            "enabled": False,
            "headless": True,
            "executable": None,
        }
        assert client.get("/api/browser").status_code == 404
        assert client.get("/api/browser/settings/unsupported").status_code == 404


def test_first_enable_is_revisioned_strict_and_never_automatically_launches(tmp_path):
    app, fake = app_with_fake(tmp_path)
    request_id = uuid4()
    body = browser_settings(request_id=request_id, enabled=True, headless=True)

    with TestClient(app) as client:
        response = client.post("/api/browser/settings", json=body)
        current = client.get("/api/browser/settings")

    assert response.status_code == 200
    assert response.json() == {
        "revision": 1,
        "enabled": True,
        "headless": True,
        "executable": None,
    }
    assert current.json() == response.json()
    assert fake.launch_calls == []
    saved = json.loads(app.state.browser_settings.path.read_text(encoding="utf-8"))
    assert saved["receipts"][0]["request_id"] == str(request_id)


@pytest.mark.parametrize(
    "body",
    [
        {"revision": 0},
        browser_settings() | {"extra": "--remote-debugging-address=0.0.0.0"},
        browser_settings() | {"enabled": 1},
        browser_settings() | {"headless": "true"},
        browser_settings() | {"request_id": "not-a-uuid"},
    ],
)
def test_settings_body_schema_is_exact(body, tmp_path):
    app, fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/browser/settings", json=body)
    assert response.status_code == 400
    assert fake.launch_calls == []
    assert not app.state.browser_settings.path.exists()


def test_settings_reject_invalid_or_oversized_json(tmp_path):
    app, _fake = app_with_fake(tmp_path)
    oversized = b'{"padding":"' + b"x" * (16 * 1024) + b'"}'
    with TestClient(app) as client:
        malformed = client.post(
            "/api/browser/settings",
            content=b'{"enabled":',
            headers={"Content-Type": "application/json"},
        )
        too_large = client.post(
            "/api/browser/settings", content=oversized, headers={"Content-Type": "application/json"}
        )
    assert malformed.status_code == 400
    assert malformed.json()["detail"] == "Invalid browser JSON"
    assert too_large.status_code == 400
    assert "too large" in too_large.json()["detail"].lower()


def test_stale_settings_and_running_browser_changes_conflict_before_apply(tmp_path, monkeypatch):
    app, fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        first = client.post("/api/browser/settings", json=browser_settings(enabled=True))
        assert first.status_code == 200

        stale = client.post(
            "/api/browser/settings", json=browser_settings(revision=0, enabled=False)
        )
        assert stale.status_code == 409

        fake.running = True
        apply_calls = []
        real_apply = app.state.browser_settings.apply

        def tracked_apply(body):
            apply_calls.append(body)
            return real_apply(body)

        monkeypatch.setattr(app.state.browser_settings, "apply", tracked_apply)
        reconfigure = client.post(
            "/api/browser/settings",
            json=browser_settings(revision=1, enabled=True, headless=False),
        )
        disable = client.post(
            "/api/browser/settings", json=browser_settings(revision=1, enabled=False)
        )

    assert reconfigure.status_code == disable.status_code == 409
    for response in (reconfigure, disable):
        assert "close the dedicated browser" in response.json()["detail"].lower()
    assert apply_calls == []
    assert fake.launch_calls == []
    assert app.state.browser_settings.get()["enabled"] is True


def test_password_and_same_origin_guard_every_browser_read_and_settings_write(tmp_path):
    app, _fake = app_with_fake(tmp_path, protected=True)
    reads = [
        "/api/browser/settings",
        "/api/browser/status",
        "/api/browser/tabs",
        f"/api/browser/screenshot?target_id={TARGET_ID}",
    ]
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        service = app.state.access.service_file.read_text(encoding="utf-8").strip()
        service_headers = {"Authorization": "Bearer " + service}
        for path in reads:
            assert client.get(path).status_code == 401, path
            assert client.get(path, headers=service_headers).status_code == 403, path

        owner = login(client)
        crossed = owner | {"Origin": "https://evil.example"}
        assert client.get("/api/browser/status", headers=crossed).status_code == 403
        assert (
            client.post(
                "/api/browser/settings", json=browser_settings(), headers=crossed
            ).status_code
            == 403
        )
        accepted = client.post(
            "/api/browser/settings",
            json=browser_settings(),
            headers=owner | {"Origin": "http://testserver"},
        )
        assert accepted.status_code == 200


def test_launch_requires_exact_uuid_only_body_and_passes_canonical_id(tmp_path):
    app, fake = app_with_fake(tmp_path)
    request_id = uuid4()
    invalid = [
        {},
        {"request_id": "not-a-uuid"},
        {"request_id": str(request_id), "command": "--no-sandbox"},
        {"request_id": str(request_id), "executable": "/tmp/browser"},
        {"request_id": str(request_id), "args": ["--remote-debugging-port=1"]},
    ]
    with TestClient(app) as client:
        for body in invalid:
            assert client.post("/api/browser/launch", json=body).status_code == 400
        response = client.post("/api/browser/launch", json={"request_id": str(request_id).upper()})

    assert response.status_code == 200
    assert fake.launch_calls == [str(UUID(str(request_id).upper()))]


def test_stop_requires_exact_uuid_only_body_and_passes_canonical_id(tmp_path):
    app, fake = app_with_fake(tmp_path)
    request_id = uuid4()
    invalid = [
        {},
        {"request_id": "not-a-uuid"},
        {"request_id": str(request_id), "action": "close"},
    ]
    with TestClient(app) as client:
        for body in invalid:
            assert client.post("/api/browser/stop", json=body).status_code == 400
        response = client.post(
            "/api/browser/stop", json={"request_id": str(request_id).upper()}
        )

    assert response.status_code == 200
    assert response.json() == {"running": False, "stopped": False}
    assert fake.stop_calls == [str(UUID(str(request_id).upper()))]
    assert fake.request_calls == []


def test_stop_requires_owner_auth_and_same_origin(tmp_path):
    app, fake = app_with_fake(tmp_path, protected=True)
    body = {"request_id": str(uuid4())}
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        service = app.state.access.service_file.read_text(encoding="utf-8").strip()
        assert client.post("/api/browser/stop", json=body).status_code == 401
        assert client.post(
            "/api/browser/stop",
            json=body,
            headers={"Authorization": "Bearer " + service},
        ).status_code == 403
        owner = login(client)
        assert client.post(
            "/api/browser/stop",
            json=body,
            headers=owner | {"Origin": "https://evil.example"},
        ).status_code == 403
        accepted = client.post(
            "/api/browser/stop",
            json=body,
            headers=owner | {"Origin": "http://testserver"},
        )

    assert accepted.status_code == 200
    assert fake.stop_calls == [body["request_id"]]


def test_launch_request_body_is_bounded_and_must_be_json_object(tmp_path):
    app, fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        malformed = client.post("/api/browser/launch", content=b"{")
        array = client.post("/api/browser/launch", json=[])
        oversized = client.post(
            "/api/browser/launch",
            content=b'{"request_id":"' + b"x" * 2048 + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert [malformed.status_code, array.status_code, oversized.status_code] == [400, 400, 400]
    assert fake.launch_calls == []


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("select", {"target_id": TARGET_ID}),
        ("new", {"url": "https://example.test/"}),
        ("close", {"target_id": TARGET_ID}),
        ("back", {"target_id": TARGET_ID}),
        ("forward", {"target_id": TARGET_ID}),
        ("reload", {"target_id": TARGET_ID}),
        ("click", {"target_id": TARGET_ID, "x_ratio": 0.25, "y_ratio": 0.75}),
        (
            "drag",
            {
                "target_id": TARGET_ID,
                "start": {"x_ratio": 0.1, "y_ratio": 0.2},
                "end": {"x_ratio": 0.8, "y_ratio": 0.9},
            },
        ),
        ("text", {"target_id": TARGET_ID, "text": "hello"}),
        ("key", {"target_id": TARGET_ID, "key": "Enter", "modifiers": []}),
        ("copy", {"target_id": TARGET_ID}),
    ],
)
def test_post_action_allowlist_forwards_only_documented_actions(action, body, tmp_path):
    app, fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        response = client.post(f"/api/browser/{action}", json=body)
    assert response.status_code == 200
    assert fake.request_calls == [(action, body, None)]


def test_arbitrary_cdp_actions_and_unexposed_target_ids_are_rejected(tmp_path):
    app, fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        for action in ("eval", "download", "Page.navigate", "tabs", "screenshot"):
            assert client.post(f"/api/browser/{action}", json={}).status_code == 400
        for action in ("eval", "download", "Page.navigate"):
            assert client.get(f"/api/browser/{action}").status_code == 400
        assert (
            client.post(
                "/api/browser/select", json={"target_id": "browser-process-root"}
            ).status_code
            == 400
        )
        assert client.post("/api/browser/select", json={"target_id": ""}).status_code == 400

    assert [call[0] for call in fake.request_calls] == ["select", "select"]


def test_action_json_is_invalid_or_over_16_kib_before_fake_dispatch(tmp_path):
    app, fake = app_with_fake(tmp_path)
    oversized = {"target_id": TARGET_ID, "text": "x" * (16 * 1024)}
    with TestClient(app) as client:
        malformed = client.post("/api/browser/text", content=b'{"target_id":')
        too_large = client.post("/api/browser/text", json=oversized)
    assert malformed.status_code == 400
    assert too_large.status_code == 400
    assert fake.request_calls == []


def test_screenshot_is_fixed_png_no_store_and_bounded_to_8_mib(tmp_path):
    app, fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/browser/screenshot", params={"target_id": TARGET_ID})
        oversized = client.get("/api/browser/screenshot", params={"target_id": "oversized"})

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert oversized.status_code == 400
    assert "8 mib" in oversized.json()["detail"].lower()
    assert fake.request_calls[-1] == ("screenshot", None, {"target_id": "oversized"})


def test_browser_api_keeps_security_headers_and_csp_blob_images(tmp_path):
    app, _fake = app_with_fake(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/browser/status")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "img-src 'self' data: blob:" in csp
    assert "frame-ancestors 'none'" in csp


def test_app_disposal_detaches_fake_without_implicitly_stopping(tmp_path):
    app, fake = app_with_fake(tmp_path)
    with TestClient(app):
        assert fake.close_calls == 0
        assert fake.stop_calls == []
    assert fake.close_calls == 1
    assert fake.stop_calls == []
