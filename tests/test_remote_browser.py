import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import pytest

from swarm import edgemcp_launcher
from swarm.edgemcp_browser import EdgeBrowser
from swarm.remote_browser import BrowserError, RemoteBrowser


class FakeLauncher:
    def __init__(self):
        self.launches = []
        self.probes = []
        self.version = None
        self.browser_id = None
        self.next_browser_id = "11111111-1111-4111-8111-111111111111"

    def probe(self, port):
        identity = self.probe_identity(port)
        return identity["browser"] if identity else None

    def probe_identity(self, port, timeout=1):
        self.probes.append(port)
        if self.version is None:
            return None
        return {"browser": self.version, "browser_id": self.browser_id}

    def launch(self, port, **options):
        self.launches.append((port, options))
        expected = options.get("expected_browser_id")
        if self.version is not None and expected != self.browser_id:
            raise RuntimeError("CDP port belongs to a different browser")
        self.version = "Chromium/Fake"
        self.browser_id = self.next_browser_id
        return {
            "started": len(self.launches) == 1,
            "already_running": len(self.launches) != 1,
            "browser": self.version,
            "browser_id": self.browser_id,
        }


class Domain:
    def __init__(self, tab, name):
        object.__setattr__(self, "tab", tab)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "callbacks", {})

    def __setattr__(self, name, value):
        if callable(value):
            self.callbacks[name] = value
        else:
            object.__setattr__(self, name, value)

    def __getattr__(self, method):
        def call(**kwargs):
            self.tab.calls.append((self.name, method, kwargs))
            if self.name == "Runtime" and method == "evaluate":
                expression = kwargs["expression"]
                if "window.innerWidth" in expression:
                    value = {"width": 800, "height": 600}
                elif "window.getSelection" in expression:
                    value = "copied text"
                else:
                    value = 1
                return {"result": {"value": value}}
            if self.name == "Page" and method == "captureScreenshot":
                return {"data": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()}
            if self.name == "Page" and method == "getNavigationHistory":
                return {
                    "entries": [{"id": 10, "url": "https://before.test/"},
                                {"id": 11, "url": "https://after.test/"}],
                    "currentIndex": 1,
                }
            return {}
        return call


class FakeTab:
    def __init__(self, target_id, url="https://example.test/", title="Example"):
        self.id, self.type = target_id, "page"
        self._kwargs = {"id": target_id, "type": "page", "url": url, "title": title}
        self.calls = []
        self.started = False
        self.stopped = False
        self.Page = Domain(self, "Page")
        self.Runtime = Domain(self, "Runtime")
        self.Input = Domain(self, "Input")

    def start(self):
        self.started = True
        self.stopped = False

    def stop(self):
        self.stopped = True

    def wait(self, timeout):
        self.calls.append(("Tab", "wait", {"timeout": timeout}))


class FakePychromeBrowser:
    def __init__(self, owner, url):
        self.owner, self.url = owner, url

    def list_tab(self, timeout=None):
        return list(self.owner.tabs.values())

    def activate_tab(self, target_id, timeout=None):
        self.owner.activated.append((target_id, timeout))

    def new_tab(self, url=None, timeout=None):
        tab = FakeTab("new-page", url, "")
        self.owner.tabs[tab.id] = tab
        return tab

    def close_tab(self, target_id, timeout=None):
        self.owner.closed.append((target_id, timeout))
        self.owner.tabs.pop(target_id)


class FakePychrome:
    def __init__(self):
        self.tabs = {"page-1": FakeTab("page-1")}
        self.activated = []
        self.closed = []
        self.urls = []

    def Browser(self, url):
        self.urls.append(url)
        return FakePychromeBrowser(self, url)


@pytest.fixture
def harness(tmp_path):
    settings = {"enabled": True, "headless": True, "executable": "/browser", "cdp_port": 0}
    launcher = FakeLauncher()
    pychrome = FakePychrome()
    remote = RemoteBrowser(
        tmp_path, settings, launcher=launcher,
        browser_factory=lambda port: EdgeBrowser(port, pychrome),
    )
    return remote, launcher, pychrome


def test_disabled_status_does_not_probe_import_or_create_state(tmp_path):
    launcher = FakeLauncher()
    remote = RemoteBrowser(tmp_path, {"enabled": False, "cdp_port": 0}, launcher=launcher)

    assert remote.status() == {
        "enabled": False, "running": False, "state": "disabled", "error": None,
    }
    with pytest.raises(BrowserError, match="disabled"):
        remote.launch()
    with pytest.raises(BrowserError, match="disabled"):
        remote.request("tabs")
    assert launcher.probes == launcher.launches == []
    assert not (tmp_path / "remote-browser").exists()


def test_launch_uses_stable_private_state_and_fixed_profile(harness, tmp_path, monkeypatch):
    remote, launcher, _ = harness
    monkeypatch.setenv("EDGE_PROFILE_DIR", str(tmp_path / "personal"))

    first = remote.launch("same-request")
    again = remote.launch("same-request")
    other = remote.launch("other-request")

    assert first == again == {
        "running": True, "started": True, "browser": "Chromium/Fake",
    }
    assert other == {"running": True, "started": False, "browser": "Chromium/Fake"}
    assert len(launcher.launches) == 2
    port, options = launcher.launches[0]
    assert 1 <= port <= 65535
    assert options == {
        "executable": "/browser",
        "profile": tmp_path / "remote-browser" / "profile",
        "headless": True,
        "expected_browser_id": None,
    }
    state = json.loads((tmp_path / "remote-browser/state.json").read_text())
    assert state["browser_id"] == "11111111-1111-4111-8111-111111111111"
    assert str(options["profile"]) != os.environ["EDGE_PROFILE_DIR"]
    assert (tmp_path / "remote-browser").stat().st_mode & 0o777 == 0o700
    assert options["profile"].stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "remote-browser/state.json").stat().st_mode & 0o777 == 0o600
    assert remote.status()["running"] is True
    assert remote.status()["browser"] == "Chromium/Fake"


def test_actual_edgemcp_adapter_tabs_selection_screenshot_and_input(harness):
    remote, _, pychrome = harness
    remote.launch()

    assert remote.request("tabs") == {
        "tabs": [{"id": "page-1", "url": "https://example.test/", "title": "Example"}],
        "selected": None,
    }
    assert remote.request("select", {"target_id": "page-1"}) == {"ok": True}
    assert pychrome.activated == [("page-1", 5)]
    tab = pychrome.tabs["page-1"]
    assert set(tab.Runtime.callbacks) == {
        "executionContextCreated", "executionContextDestroyed", "executionContextsCleared",
    }
    assert remote.request("screenshot", query={"target_id": "page-1"}).startswith(b"\x89PNG")
    assert remote.request("copy", {"target_id": "page-1"}) == {"text": "copied text"}

    remote.request("click", {"target_id": "page-1", "x_ratio": 0.5, "y_ratio": 0.25})
    mouse = [kwargs for domain, method, kwargs in tab.calls
             if domain == "Input" and method == "dispatchMouseEvent"]
    assert [(event["type"], event["x"], event["y"]) for event in mouse[-2:]] == [
        ("mousePressed", 400, 150), ("mouseReleased", 400, 150),
    ]

    remote.request("drag", {
        "target_id": "page-1", "start": {"x_ratio": 0, "y_ratio": 0},
        "end": {"x_ratio": 1, "y_ratio": 1},
    })
    mouse = [kwargs for domain, method, kwargs in tab.calls
             if domain == "Input" and method == "dispatchMouseEvent"]
    assert [event["type"] for event in mouse[-3:]] == [
        "mousePressed", "mouseMoved", "mouseReleased",
    ]

    remote.request("text", {"target_id": "page-1", "text": "hello"})
    remote.request("key", {"target_id": "page-1", "key": "a", "modifiers": ["ctrl"]})
    keys = [kwargs for domain, method, kwargs in tab.calls
            if domain == "Input" and method == "dispatchKeyEvent"]
    assert [event["type"] for event in keys[-2:]] == ["keyDown", "keyUp"]
    assert keys[-2]["text"] == ""


def test_tabs_missing_or_invalid_constructor_metadata_falls_back_to_empty_strings(harness):
    remote, _, pychrome = harness
    remote.launch()
    pychrome.tabs["page-1"]._kwargs = {"url": object(), "title": None}
    pychrome.tabs["page-2"] = FakeTab("page-2")
    del pychrome.tabs["page-2"]._kwargs

    result = remote.request("tabs")

    assert result["tabs"] == [
        {"id": "page-1", "url": "", "title": ""},
        {"id": "page-2", "url": "", "title": ""},
    ]
    assert all(isinstance(tab["url"], str) and isinstance(tab["title"], str)
               for tab in result["tabs"])


def test_screenshot_response_is_bounded_to_8_mib(harness, monkeypatch):
    remote, _, _ = harness
    remote.launch()
    controls = remote._controls()
    oversized = b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024)
    monkeypatch.setattr(controls, "screenshot", lambda _target_id: oversized)

    with pytest.raises(BrowserError, match="8 MiB"):
        remote.request("screenshot", {"target_id": "page-1"})


def test_tab_lifecycle_navigation_and_close_only_detaches(harness):
    remote, _, pychrome = harness
    remote.launch()

    made = remote.request("new", {"url": "https://example.test/new"})
    assert made == {"id": "new-page", "url": "https://example.test/new"}
    new_tab = pychrome.tabs["new-page"]
    remote.request("back", {"target_id": "new-page"})
    remote.request("forward", {"target_id": "new-page"})
    remote.request("reload", {"target_id": "new-page"})
    assert [(method, kwargs.get("entryId")) for domain, method, kwargs in new_tab.calls
            if method == "navigateToHistoryEntry"] == [
        ("navigateToHistoryEntry", 10),
    ]

    remote.close()
    assert new_tab.stopped is True
    assert pychrome.closed == []  # detaching does not close a tab or browser
    assert remote.status()["running"] is True

    remote.request("close", {"target_id": "new-page"})
    assert pychrome.closed == [("new-page", 10)]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.test/", "",
])
def test_remote_web_route_validation_rejects_unsafe_urls(harness, url):
    remote, _, _ = harness
    remote.launch()
    with pytest.raises(ValueError):
        remote.request("new", {"url": url})


def test_remote_web_route_validation_is_bounded_and_fixed(harness):
    remote, _, _ = harness
    remote.launch()

    with pytest.raises(ValueError, match="16384"):
        remote.request("text", {"target_id": "page-1", "text": "x" * 17000})
    with pytest.raises(ValueError, match="1-4096"):
        remote.request("text", {"target_id": "page-1", "text": ""})
    with pytest.raises(ValueError, match="numbers from 0 to 1"):
        remote.request("click", {"target_id": "page-1", "x_ratio": True, "y_ratio": 0})
    with pytest.raises(ValueError, match="Invalid modifiers"):
        remote.request("key", {"target_id": "page-1", "key": "x", "modifiers": ["super"]})
    with pytest.raises(ValueError, match="Unsupported"):
        remote.request("eval", {"target_id": "page-1", "expression": "document.cookie"})


def test_launcher_keeps_one_attempt_and_accepts_chromium(tmp_path, monkeypatch):
    executable = tmp_path / "chromium"
    executable.write_bytes(b"fake")
    executable.chmod(0o700)
    profile = tmp_path / "profile"
    process = type("Process", (), {"pid": 1234, "poll": lambda self: None})()
    commands = []
    identity = {
        "browser": "Chromium/123",
        "browser_id": "22222222-2222-4222-8222-222222222222",
    }
    probes = iter([None, identity, identity])

    monkeypatch.setattr(
        edgemcp_launcher, "probe_identity", lambda port, timeout=1: next(probes),
    )
    monkeypatch.setattr(
        edgemcp_launcher.subprocess, "Popen",
        lambda command, **options: commands.append((command, options)) or process,
    )
    port = 49151
    edgemcp_launcher._ATTEMPTS.pop((port, str(profile.resolve())), None)
    edgemcp_launcher._READY.discard((port, str(profile.resolve())))

    first = edgemcp_launcher.launch(
        port, executable=str(executable), profile=profile, headless=True, timeout=1,
    )
    second = edgemcp_launcher.launch(
        port, executable=str(executable), profile=profile, headless=True, timeout=1,
    )

    assert first["started"] is True
    assert first["browser_id"] == identity["browser_id"]
    assert second["started"] is False and second["already_running"] is True
    assert len(commands) == 1
    command, options = commands[0]
    assert f"--remote-debugging-port={port}" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert f"--user-data-dir={profile.resolve()}" in command
    assert "--headless=new" in command
    assert "DevToolsActivePort" not in " ".join(command)
    assert options["shell"] is False


def test_foreign_browser_identity_refuses_status_launch_and_controls(harness):
    remote, launcher, pychrome = harness
    remote.launch("owned")
    launcher.browser_id = "33333333-3333-4333-8333-333333333333"

    status = remote.status()
    assert status["running"] is False and status["state"] == "error"
    assert "unowned browser" in status["error"]
    with pytest.raises(BrowserError, match="unowned browser"):
        remote.request("tabs")
    with pytest.raises(BrowserError, match="different browser"):
        remote.launch("another")
    assert pychrome.urls == []

    launcher.browser_id = json.loads(remote.state_path.read_text())["browser_id"]
    remote.request("select", {"target_id": "page-1"})
    launcher.browser_id = "33333333-3333-4333-8333-333333333333"
    remote.close()
    assert pychrome.tabs["page-1"].stopped is False


def test_legacy_port_only_state_refuses_preexisting_listener(tmp_path):
    root = tmp_path / "remote-browser"
    profile = root / "profile"
    profile.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    profile.chmod(0o700)
    state_path = root / "state.json"
    state_path.write_text(json.dumps({"version": 1, "port": 49152}), encoding="ascii")
    state_path.chmod(0o600)
    launcher = FakeLauncher()
    launcher.version = "Chromium/Fake"
    launcher.browser_id = "44444444-4444-4444-8444-444444444444"
    remote = RemoteBrowser(
        tmp_path, {"enabled": True, "headless": True, "cdp_port": 0}, launcher=launcher,
    )

    status = remote.status()
    assert status["running"] is False and status["state"] == "error"
    with pytest.raises(BrowserError, match="different browser"):
        remote.launch()
    assert launcher.launches


def test_cached_launch_checks_liveness_and_receipts_are_bounded(harness):
    remote, launcher, _ = harness
    first = remote.launch("cached")
    launcher.version = None
    launcher.next_browser_id = "55555555-5555-4555-8555-555555555555"

    replacement = remote.launch("cached")

    assert first["started"] is True
    assert replacement["started"] is False
    assert len(launcher.launches) == 2
    assert json.loads(remote.state_path.read_text())["browser_id"] == launcher.next_browser_id

    for number in range(300):
        remote.launch(f"receipt-{number}")
    assert len(remote._launch_results) == 256
    assert "cached" not in remote._launch_results
    assert "receipt-0" not in remote._launch_results
    assert "receipt-299" in remote._launch_results


def test_probe_identity_validates_websocket_url_and_probe_keeps_string_api():
    browser_id = "66666666-6666-4666-8666-666666666666"

    class Handler(BaseHTTPRequestHandler):
        websocket_port_offset = 0

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            content = json.dumps({
                "Browser": "Chromium/123",
                "webSocketDebuggerUrl": (
                    f"ws://127.0.0.1:{self.server.server_port + self.websocket_port_offset}"
                    f"/devtools/browser/{browser_id}"
                ),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert edgemcp_launcher.probe_identity(server.server_port) == {
            "browser": "Chromium/123", "browser_id": str(UUID(browser_id)),
        }
        assert edgemcp_launcher.probe(server.server_port) == "Chromium/123"
        Handler.websocket_port_offset = 1
        with pytest.raises(RuntimeError, match="not a ready browser CDP endpoint"):
            edgemcp_launcher.probe_identity(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_launcher_rejects_unrecorded_existing_browser(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    identity = {
        "browser": "Chromium/123",
        "browser_id": "77777777-7777-4777-8777-777777777777",
    }
    monkeypatch.setattr(edgemcp_launcher, "probe_identity", lambda port, timeout=1: identity)
    key = (49153, str(profile.resolve()))
    edgemcp_launcher._ATTEMPTS.pop(key, None)
    edgemcp_launcher._READY.discard(key)

    with pytest.raises(RuntimeError, match="unowned browser"):
        edgemcp_launcher.launch(
            key[0], executable=None, profile=profile, expected_browser_id=None,
        )
    reused = edgemcp_launcher.launch(
        key[0], executable=None, profile=profile,
        expected_browser_id=identity["browser_id"],
    )
    assert reused["already_running"] is True
