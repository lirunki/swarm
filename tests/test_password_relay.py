import json
import socket
import time
import urllib.error
import urllib.request
from contextlib import ExitStack

import pytest
from test_auth import password_file
from test_federation_integration import running_server
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from swarm.federation import FederationConfig
from swarm.files import FileRequest
from swarm.models import Agent
from swarm.relay import create_relay_app
from swarm.server import create_app


def http(base, path, method="GET", body=None, headers=None):
    request = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    content = response.read()
    return response.status, json.loads(
        content
    ) if response.headers.get_content_type() == "application/json" else content


@pytest.fixture
def pair(tmp_path):
    with ExitStack() as stack:
        key = "test-mesh-password-routing-key-0123456789"
        relay = stack.enter_context(running_server(create_relay_app([key])))
        probes = [socket.socket(), socket.socket()]
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        a, b = [probe.getsockname()[1] for probe in probes]
        for probe in probes:
            probe.close()
        apps = []
        for node, port in [("alpha", a), ("beta", b)]:
            folder = tmp_path / node
            folder.mkdir()
            password = password_file(folder, f"{node}-separate-test-password")
            app = create_app(
                folder / "home",
                recover=False,
                api_url=f"http://127.0.0.1:{port}",
                password_file=password,
                federation_config=FederationConfig(
                    f"ws://127.0.0.1:{relay}/v1/connect",
                    node,
                    node,
                    key,
                    f"http://127.0.0.1:{port}",
                ),
            )
            apps.append(app)
            stack.enter_context(running_server(app, port))
        base = f"http://127.0.0.1:{a}"
        status, login = http(
            base, "/api/auth/login", "POST", {"password": "alpha-separate-test-password"}
        )
        assert status == 200
        headers = {"Authorization": "Bearer " + login["token"]}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, value = http(base, "/api/federation/nodes", headers=headers)
            if status == 200 and any(n["id"] == "beta" for n in value["nodes"]):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Relay peer did not become visible")
        yield apps[0], apps[1], base, headers


def test_destination_password_gates_rpc_cached_state_websocket_and_download(pair, tmp_path):
    app_a, app_b, base, headers = pair
    private = app_b.state.controller.store.create(
        Agent(
            name="Private beta agent", goal="secret work", folder=str(tmp_path), auto_resume=False
        )
    )
    app_b.state.controller.refresh()
    path = tmp_path / "remote.txt"
    path.write_bytes(b"private-beta-file")
    ref = app_b.state.controller.files.add(private.id, FileRequest(path=str(path)))["file"]
    prefix = "/api/federation/beta/proxy"
    assert http(base, prefix + "/api/state")[0] == 401  # Gateway itself is protected.
    assert http(base, prefix + "/api/auth/status", headers=headers) == (200, {"required": True})
    assert http(base, prefix + "/api/state", headers=headers)[0] == 401
    assert (
        http(
            base,
            prefix + "/api/auth/login",
            "POST",
            {"password": "alpha-separate-test-password"},
            headers,
        )[0]
        == 401
    )
    status, result = http(
        base,
        prefix + "/api/auth/login",
        "POST",
        {"password": "beta-separate-test-password"},
        headers,
    )
    assert status == 200
    remote_headers = headers | {"X-Swarm-Authorization": "Bearer " + result["token"]}
    status, value = http(base, prefix + "/api/state", headers=remote_headers)
    assert status == 200 and value["agents"][0]["id"] == str(private.id)
    assert http(base, prefix + "/api/state", headers=headers)[0] == 401
    # A previously unlocked shared cache must not authorize another browser.
    wsbase = base.replace("http://", "ws://")
    with pytest.raises(InvalidStatus):
        with connect(
            wsbase + "/ws/federation/beta/state", additional_headers=headers, open_timeout=10
        ):
            pass
    with connect(
        wsbase + "/ws/federation/beta/state", additional_headers=remote_headers, open_timeout=10
    ) as ws:
        state = json.loads(ws.recv(timeout=15))
        if not state.get("agents"):
            state = json.loads(ws.recv(timeout=15))
        assert state["agents"][0]["id"] == str(private.id)
    nodes = http(base, "/api/federation/nodes", headers=headers)[1]["nodes"]
    assert nodes[0]["capabilities"]["password_required"] and nodes[0]["agent_count"] is None
    download = prefix + f"/api/agents/{private.id}/files/{ref['id']}/content?download=true"
    assert http(base, download, headers=headers)[0] == 401
    assert http(base, download, headers=remote_headers) == (200, b"private-beta-file")
    status, ticket = http(base, "/api/auth/ticket", "POST", {"path": download}, remote_headers)
    assert status == 200
    # Signed navigation carries both authorizations without a raw token in the URL.
    url = download + "&access_ticket=" + ticket["ticket"]
    assert http(base, url) == (200, b"private-beta-file")
    assert http(base, url)[0] == 401
    # Owner-only service credentials must never be accepted as relayed browser credentials.
    service = app_b.state.access.service_file.read_text().strip()
    assert (
        http(
            base,
            prefix + "/api/state",
            headers=headers | {"X-Swarm-Authorization": "Bearer " + service},
        )[0]
        == 401
    )


def test_old_browser_cannot_receive_another_browsers_authenticated_cached_state(pair):
    from websockets.exceptions import ConnectionClosed

    app_a, app_b, base, headers = pair
    prefix = "/api/federation/beta/proxy"
    _, result = http(
        base,
        prefix + "/api/auth/login",
        "POST",
        {"password": "beta-separate-test-password"},
        headers,
    )
    first = headers | {"X-Swarm-Authorization": "Bearer " + result["token"]}
    with connect(
        base.replace("http://", "ws://") + "/ws/federation/beta/state",
        additional_headers=first,
        open_timeout=10,
    ) as old:
        old.recv(timeout=10)
        assert http(base, prefix + "/api/auth/session", "DELETE", headers=first)[0] == 200
        _, login2 = http(
            base,
            prefix + "/api/auth/login",
            "POST",
            {"password": "beta-separate-test-password"},
            headers,
        )
        second = headers | {"X-Swarm-Authorization": "Bearer " + login2["token"]}
        app_b.state.controller.store.create(
            Agent(
                name="New secret only for browser2",
                goal="Private",
                folder=str(app_b.state.controller.store.home),
                auto_resume=False,
            )
        )
        assert http(base, prefix + "/api/state", headers=second)[0] == 200
        # The gateway revalidates the old token before forwarding shared cache updates.
        with pytest.raises(ConnectionClosed) as failure:
            old.recv(timeout=10)
        assert failure.value.rcvd.code == 4401


def test_browser_asks_for_each_destination_password_and_forgets_on_reload(pair):
    playwright = pytest.importorskip("playwright.sync_api")
    app_a, app_b, base, headers = pair
    app_b.state.controller.store.create(
        Agent(
            name="Private remote browser card",
            goal="Private",
            folder=str(app_b.state.controller.store.home),
            auto_resume=False,
        )
    )
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(
                exc
            ) or "error while loading shared libraries" in str(exc):
                pytest.skip("Optional Chromium runtime unavailable")
            raise
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            dialog = page.locator("#auth-dialog")
            playwright.expect(dialog).to_be_visible()
            playwright.expect(page.locator("#auth-workspace")).to_contain_text("Local Swarm")
            page.locator("#auth-dialog input").fill("alpha-separate-test-password")
            page.locator("#auth-dialog button[type=submit]").click()
            playwright.expect(page.locator("#swarm-selector option[value=beta]")).to_have_count(1)
            page.locator("#swarm-selector").select_option("beta")
            playwright.expect(dialog).to_be_visible()
            playwright.expect(page.locator("#auth-workspace")).to_contain_text("beta")
            assert not page.locator(".agent-card").count()
            page.locator("#auth-dialog input").fill("alpha-separate-test-password")
            page.locator("#auth-dialog button[type=submit]").click()
            playwright.expect(page.locator("#auth-error")).to_contain_text("Incorrect password")
            page.locator("#auth-dialog input").fill("beta-separate-test-password")
            page.locator("#auth-dialog button[type=submit]").click()
            playwright.expect(page.locator(".agent-card")).to_have_count(1)
            playwright.expect(page.locator(".agent-card h3")).to_have_text(
                "Private remote browser card"
            )
            page.locator("#swarm-selector").select_option("local")
            playwright.expect(page.locator(".agent-card")).to_have_count(0)
            assert not dialog.is_visible()
            page.locator("#swarm-selector").select_option("beta")
            playwright.expect(page.locator(".agent-card")).to_have_count(1)
            assert not dialog.is_visible()  # Cached only for this page instance.
            page.reload()
            playwright.expect(dialog).to_be_visible()
            playwright.expect(page.locator("#auth-workspace")).to_contain_text("Local Swarm")
            assert not page.locator(".agent-card").count()
            page.locator("#auth-dialog input").fill("alpha-separate-test-password")
            page.locator("#auth-dialog button[type=submit]").click()
            playwright.expect(page.locator("#auth-workspace")).to_contain_text("beta")
            assert dialog.is_visible() and not page.locator(".agent-card").count()
            assert not errors
        finally:
            browser.close()


def test_authenticated_remote_terminal_with_fake_pi_still_preserves_process(pair, tmp_path):
    from test_server import make_fake_pi

    from swarm.adapters import PiAdapter

    app_a, app_b, base, headers = pair
    fake = make_fake_pi(
        tmp_path,
        """import sys
print('AUTH-TERMINAL-READY', flush=True)
for line in sys.stdin:
    print('AUTH-REPLY:' + line.rstrip(), flush=True)
""",
    )
    app_b.state.controller.processes.adapters["pi"] = PiAdapter(str(fake))
    prefix = "/api/federation/beta/proxy"
    _, value = http(
        base,
        prefix + "/api/auth/login",
        "POST",
        {"password": "beta-separate-test-password"},
        headers,
    )
    auth = headers | {"X-Swarm-Authorization": "Bearer " + value["token"]}
    status, agent = http(
        base,
        prefix + "/api/agents",
        "POST",
        {"name": "Auth fake Pi", "goal": "Test terminal only", "folder": str(tmp_path)},
        auth,
    )
    assert status == 201
    id = agent["id"]
    proc = app_b.state.controller.processes
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current = app_b.state.controller.store.get(id)
            if current.spawn_state == "ready" and proc.alive(current):
                break
            assert not current.last_error, current.last_error
            time.sleep(0.05)
        assert current.spawn_state == "ready"
        path = f"/ws/federation/beta/terminal/{id}?cols=100&rows=25"
        status, ticket = http(base, "/api/auth/ticket", "POST", {"path": path}, auth)
        assert status == 200
        with connect(
            base.replace("http://", "ws://") + path + "&access_ticket=" + ticket["ticket"],
            open_timeout=15,
        ) as ws:
            received = b""
            while b"AUTH-TERMINAL-READY" not in received:
                received += ws.recv(timeout=10)
            ws.send(json.dumps({"type": "input", "data": "hello\r"}))
            while b"AUTH-REPLY:hello" not in received:
                received += ws.recv(timeout=10)
        assert proc.alive(current)
        assert http(base, prefix + f"/api/agents/{id}/stop", "POST", {}, auth)[0] == 200
    finally:
        proc.stop(app_b.state.controller.store.get(id))
