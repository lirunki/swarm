"""Focused coverage for the fixed, bounded browser resize action."""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm import edgemcp_launcher
from swarm.browser_settings import BrowserSettings
from swarm.edgemcp_browser import EdgeBrowser
from swarm.remote_browser import BrowserError, RemoteBrowser
from swarm.server import create_app

TARGET = "resize-page"
BROWSER_ID = "04aa79a2-26de-4b2d-b1ef-2c2eafdcc4a1"


class CallMethodException(Exception):
    pass


class Domain:
    def __init__(self, tab, name):
        object.__setattr__(self, "tab", tab)
        object.__setattr__(self, "name", name)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)

    def __getattr__(self, method):
        def call(**kwargs):
            self.tab.calls.append((self.name, method, kwargs))
            if self.name == "Runtime" and method == "evaluate":
                return {"result": {"value": 1}}
            if self.name == "Browser" and method == "getWindowForTarget":
                if self.tab.window_unsupported:
                    raise CallMethodException("Browser window is unavailable")
                return {"windowId": 17}
            return {}

        return call


class FakeTab:
    def __init__(self):
        self.id = TARGET
        self.type = "page"
        self._kwargs = {"id": TARGET, "type": "page", "url": "about:blank", "title": ""}
        self.calls = []
        self.window_unsupported = False
        self.Page = Domain(self, "Page")
        self.Runtime = Domain(self, "Runtime")
        self.Browser = Domain(self, "Browser")
        self.Emulation = Domain(self, "Emulation")

    def start(self):
        pass

    def stop(self):
        pass


class FakeCDPBrowser:
    def __init__(self, tab):
        self.tab = tab

    def list_tab(self, timeout=None):
        return [self.tab]


class FakePychrome:
    CallMethodException = CallMethodException

    def __init__(self):
        self.tab = FakeTab()

    def Browser(self, url):
        return FakeCDPBrowser(self.tab)


def _resize_calls(tab):
    return [call for call in tab.calls if call[0] in {"Browser", "Emulation"}]


def test_resize_uses_fixed_browser_commands_then_exact_viewport_and_caches():
    pychrome = FakePychrome()
    browser = EdgeBrowser(9222, pychrome)

    expected = {"resized": True, "width": 800, "height": 600}
    assert browser.resize(TARGET, 800, 600) == expected
    assert _resize_calls(pychrome.tab) == [
        ("Browser", "getWindowForTarget", {"targetId": TARGET, "_timeout": 5}),
        (
            "Browser",
            "setWindowBounds",
            {"windowId": 17, "bounds": {"windowState": "normal"}, "_timeout": 5},
        ),
        (
            "Browser",
            "setWindowBounds",
            {"windowId": 17, "bounds": {"width": 800, "height": 600}, "_timeout": 5},
        ),
        (
            "Emulation",
            "setDeviceMetricsOverride",
            {
                "width": 800,
                "height": 600,
                "deviceScaleFactor": 1,
                "mobile": False,
                "_timeout": 5,
            },
        ),
    ]
    assert [kwargs["expression"] for domain, method, kwargs in pychrome.tab.calls
            if domain == "Runtime" and method == "evaluate"] == ["1"]

    before = list(_resize_calls(pychrome.tab))
    assert browser.resize(TARGET, 800, 600) == expected
    assert _resize_calls(pychrome.tab) == before

    browser.close()
    browser.resize(TARGET, 800, 600)
    assert len(_resize_calls(pychrome.tab)) == len(before) * 2


def test_resize_ignores_only_known_window_command_failure_and_keeps_viewport():
    pychrome = FakePychrome()
    pychrome.tab.window_unsupported = True
    browser = EdgeBrowser(9222, pychrome)

    assert browser.resize(TARGET, 1280, 720) == {
        "resized": True,
        "width": 1280,
        "height": 720,
    }
    assert _resize_calls(pychrome.tab)[-1] == (
        "Emulation",
        "setDeviceMetricsOverride",
        {
            "width": 1280,
            "height": 720,
            "deviceScaleFactor": 1,
            "mobile": False,
            "_timeout": 5,
        },
    )


@pytest.mark.parametrize(
    "body",
    [
        {"target_id": TARGET, "width": True, "height": 600},
        {"target_id": TARGET, "width": 800.0, "height": 600},
        {"target_id": TARGET, "width": "８００", "height": 600},
        {"target_id": TARGET, "width": 319, "height": 600},
        {"target_id": TARGET, "width": 800, "height": 239},
        {"target_id": TARGET, "width": 3841, "height": 2160},
        {"target_id": TARGET, "width": 800, "height": 600, "expression": "document.cookie"},
        {"target_id": 123, "width": 800, "height": 600},
    ],
)
def test_resize_rejects_malformed_dimensions_before_cdp(tmp_path, monkeypatch, body):
    remote = RemoteBrowser(
        tmp_path,
        {"enabled": True, "headless": True, "executable": None, "cdp_port": 0},
    )
    calls = []
    monkeypatch.setattr(remote, "_controls", lambda: calls.append("cdp") or None)

    with pytest.raises(ValueError):
        remote.request("resize", body)
    assert calls == []


def test_resize_remote_response_is_fixed_and_forwards_validated_values(tmp_path, monkeypatch):
    remote = RemoteBrowser(
        tmp_path,
        {"enabled": True, "headless": True, "executable": None, "cdp_port": 0},
    )

    class Controls:
        def resize(self, target, width, height):
            assert (target, width, height) == (TARGET, 1280, 800)
            return {"resized": True, "width": width, "height": height}

    monkeypatch.setattr(remote, "_controls", lambda: Controls())
    assert remote.request(
        "resize", {"target_id": TARGET, "width": 1280, "height": 800}
    ) == {"resized": True, "width": 1280, "height": 800}


class RouteRegistry:
    def __init__(self):
        self.calls = []
        self.stop_calls = []

    def request(self, browser_id, action, body=None, query=None):
        self.calls.append((browser_id, action, body, query))
        return {"resized": True, "width": body["width"], "height": body["height"]}

    def close(self):
        pass


class GuardedLegacy:
    def __init__(self):
        self.calls = []

    def request(self, action, body=None, query=None):
        self.calls.append((action, body, query))
        return {"resized": True, "width": body["width"], "height": body["height"]}

    def close(self):
        pass


def test_owner_post_routes_resize_to_selected_instance_without_lifecycle_stop(tmp_path):
    password = tmp_path / "password.txt"
    password.write_text("resize-owner-password\n", encoding="utf-8")
    password.chmod(0o600)
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password,
    )
    registry = RouteRegistry()
    app.state.browser_registry = registry
    app.state.controller.processes.health = lambda: {}
    body = {"target_id": TARGET, "width": 800, "height": 600}

    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        assert client.post(f"/api/browser/instances/{BROWSER_ID}/resize", json=body).status_code == 401
        login = client.post("/api/auth/login", json={"password": "resize-owner-password"})
        token = login.json()["token"]
        response = client.post(
            f"/api/browser/instances/{BROWSER_ID}/resize",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"resized": True, "width": 800, "height": 600}
    assert registry.calls == [(BROWSER_ID, "resize", body, None)]
    assert registry.stop_calls == []


def test_legacy_resize_obeys_registry_mutation_guard(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    legacy = GuardedLegacy()
    app.state.remote_browser = legacy
    app.state.controller.processes.health = lambda: {}
    guards = []

    class Registry(RouteRegistry):
        def legacy_mutation_guard(self):
            guards.append(True)
            raise RuntimeError("use selected browser instance")

    app.state.browser_registry = Registry()
    with TestClient(app) as client:
        response = client.post(
            "/api/browser/resize",
            json={"target_id": TARGET, "width": 800, "height": 600},
        )

    assert response.status_code == 409
    assert guards == [True]
    assert legacy.calls == []


def _png_size(content: bytes) -> tuple[int, int]:
    assert content.startswith(b"\x89PNG\r\n\x1a\n") and content[12:16] == b"IHDR"
    return struct.unpack(">II", content[16:24])


def test_native_headless_resize_makes_screenshot_exact_viewport(tmp_path):
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("native Chromium is not launched as root")
    playwright = pytest.importorskip("playwright.sync_api", reason="optional native resize check")
    pytest.importorskip("pychrome", reason="optional native resize check")
    with playwright.sync_playwright() as runner:
        executable = Path(runner.chromium.executable_path).resolve()
    if not executable.is_file():
        pytest.skip("optional Chromium executable is unavailable")
    if os.name != "nt":
        linked = subprocess.run(
            ["ldd", str(executable)], capture_output=True, text=True, check=False, timeout=10
        )
        if "not found" in linked.stdout:
            pytest.skip("optional Chromium shared libraries are unavailable")

    home = tmp_path / "native-home"
    settings = BrowserSettings(home)
    settings.apply(
        {
            "revision": 0,
            "request_id": str(uuid4()),
            "enabled": True,
            "headless": True,
            "executable": str(executable),
        }
    )
    remote = RemoteBrowser(home, settings)
    process = None
    key = None
    try:
        remote.launch(uuid4())
        state = remote._read_state()
        assert state is not None
        key = (state["port"], str(remote.profile.resolve()))
        process = edgemcp_launcher._ATTEMPTS[key]
        target = remote.request("tabs")["tabs"][0]["id"]
        remote.request("resize", {"target_id": target, "width": 1280, "height": 720})
        assert remote.request(
            "resize", {"target_id": target, "width": 800, "height": 600}
        ) == {"resized": True, "width": 800, "height": 600}
        assert _png_size(remote.request("screenshot", {"target_id": target})) == (800, 600)

        # Arbitrary page evaluation is test-only; the production resize path uses
        # only the fixed Browser and Emulation CDP commands asserted above.
        tab = remote._controls()._target(target)
        evaluated = tab.Runtime.evaluate(
            expression="({width:innerWidth,height:innerHeight})",
            returnByValue=True,
            _timeout=5,
        )
        assert evaluated["result"]["value"] == {"width": 800, "height": 600}
    except BrowserError as exc:
        pytest.fail(f"native resize failed after Chromium was selected: {exc}")
    finally:
        remote.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if key is not None and edgemcp_launcher._ATTEMPTS.get(key) is process:
            edgemcp_launcher._ATTEMPTS.pop(key, None)
            edgemcp_launcher._READY.discard(key)
