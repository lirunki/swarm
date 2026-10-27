"""Browser integration coverage for durable multi-agent broadcasts."""

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Expose the shared dashboard server fixture under its original name."""
    return _dashboard_static_url


def agent(agent_id, name, *, alive, status, managed=True, goal="Fixture goal"):
    return {
        "id": agent_id,
        "name": name,
        "goal": goal,
        "status": status,
        "alive": alive,
        "managed": managed,
        "backend": "pi",
        "activity": "Browser fixture",
        "folder": "/fixture",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def states():
    return {
        "local": {
            "agents": [
                agent(
                    "local-live",
                    "<img src=x onerror=window.unsafe=true>",
                    alive=True,
                    status="done",
                    goal="Literal <script>window.unsafe=true</script>",
                ),
                agent(
                    "local-sleep",
                    "Local Sleeper",
                    alive=False,
                    status="waiting",
                    goal="Documentation",
                ),
                agent(
                    "local-observed", "Observed Local", alive=True, status="running", managed=False
                ),
            ],
            "home": "/local",
            "health": {"tmux": True, "pi": True},
            "errors": {},
        },
        "peer/a": {
            "agents": [
                agent(
                    "remote-live",
                    "Remote Live",
                    alive=True,
                    status="dormant",
                    goal="Needle research",
                ),
                agent("remote-sleep", "Remote Sleeper", alive=False, status="idle"),
            ],
            "home": "/remote",
            "health": {"tmux": True, "pi": True},
            "errors": {},
        },
        "guarded": {
            "agents": [agent("guarded-agent", "Guarded Agent", alive=True, status="running")],
            "home": "/guarded",
            "health": {"tmux": True, "pi": True},
            "errors": {},
        },
    }


def chromium(runner):
    try:
        return runner.chromium.launch(headless=True)
    except playwright.Error as exc:
        if "Executable doesn't exist" in str(exc) or "error while loading shared libraries" in str(
            exc
        ):
            pytest.skip(
                f"Optional Chromium runtime unavailable: {Path(runner.chromium.executable_path)}"
            )
        raise


def install_websocket_mock(page):
    page.add_init_script(
        """window.WebSocket = class {
            static OPEN = 1;
            constructor(url) { this.url = url; this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
            close() { this.readyState = 3; }
            send() {}
        };"""
    )


def api_fixture(page, *, compact=False):
    fixture_states = states()
    if compact:
        fixture_states["local"]["agents"] = [fixture_states["local"]["agents"][0]]
        fixture_states["peer/a"]["agents"] = [fixture_states["peer/a"]["agents"][0]]
        fixture_states["guarded"]["agents"] = []
    nodes = {
        "enabled": True,
        "relay_online": True,
        "nodes": [
            {
                "id": "peer/a",
                "name": "Peer & A",
                "online": True,
                "capabilities": {"remote_restart": True},
            },
            {"id": "guarded", "name": "Guarded", "online": True, "capabilities": {}},
            {
                "id": "offline",
                "name": "Offline",
                "online": False,
                "capabilities": {"remote_restart": True},
            },
        ],
    }
    requests = []
    post_number = 0

    def respond(route):
        nonlocal post_number
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path
        body = request.post_data_json if request.post_data else None
        requests.append((request.method, path, body))

        status = 200
        if path == "/api/federation/nodes":
            value = nodes
        elif path == "/api/federation/settings":
            value = {
                "enabled": False,
                "connected": False,
                "status": "disconnected",
                "has_api_key": False,
                "relay_url": "wss://relay.invalid/v1/connect",
                "relay_node_id": "local-browser",
                "relay_name": "Local Browser",
                "last_error": None,
            }
        elif path.endswith("/api/fleet/restarts"):
            value = {"jobs": []}
        elif path.endswith("/api/fleet/restarts/preview"):
            value = {"targets": [], "skipped": [], "home": "/remote"}
        elif path == "/api/notifications" or path.endswith("/proxy/api/notifications"):
            value = {"revision": 1, "count": 0, "notifications": []}
        elif path == "/api/state":
            value = fixture_states["local"]
        elif "/proxy/api/state" in path:
            source = unquote(path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0])
            value = fixture_states[source]
        elif request.method == "POST" and path.endswith("/messages"):
            post_number += 1
            source = "local"
            if "/api/federation/" in path:
                source = unquote(path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0])
            agent_id = unquote(path.rsplit("/agents/", 1)[1].split("/messages", 1)[0])
            if compact and source == "peer/a":
                status, value = 503, {"detail": "remote temporarily unavailable"}
            else:
                value = {
                    "agent_id": agent_id,
                    "message": {
                        "id": body["request_id"],
                        "text": body["text"],
                        "status": "pending",
                    },
                }
        elif request.method == "POST" and path.endswith("/controller/restart"):
            value = {"accepted": True}
        else:
            value = fixture_states["local"]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

    page.route("**/api/**", no_auth_mock(respond))
    return fixture_states, nodes, requests


def open_broadcast(page, text):
    page.locator("#notifications-button").click()
    playwright.expect(page.locator("#notifications-dialog")).to_be_visible()
    page.locator("#notification-broadcast-text").fill(text)
    page.locator("#notification-broadcast-open").click()
    playwright.expect(page.locator("#notifications-dialog")).to_be_hidden()
    playwright.expect(page.locator("#broadcast-agents-dialog")).to_be_visible()
    playwright.expect(page.locator("#broadcast-refresh")).to_be_enabled()


@pytest.mark.parametrize("width", [390, 1280])
def test_broadcast_filters_controls_and_inbox_modal_scope(static_url, width):
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(
                viewport={"width": width, "height": 1000}, has_touch=width == 390
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_websocket_mock(page)
            _, _, requests = api_fixture(page)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(3)

            topbar = page.locator(".topbar")
            for control in ("#fleet-control-button", "#settings-button", "#restart-controller"):
                assert topbar.locator(control).count() == 0
                assert page.locator(f"#notifications-dialog {control}").count() == 1
            assert (
                page.locator("#fleet-control-button").get_attribute("aria-label")
                == "Master control"
            )
            assert page.locator("#settings-button").get_attribute("aria-label") == "Settings"
            assert (
                page.locator("#restart-controller").get_attribute("aria-label") == "Restart backend"
            )
            assert page.locator("#restart-controller").inner_text() == "↻ Restart active Swarm"

            open_broadcast(page, "snapshot message")
            assert page.locator("#broadcast-scope").input_value() == "current"
            assert page.locator("#broadcast-awake").input_value() == "all"
            playwright.expect(page.locator("label.broadcast-target")).to_have_count(2)
            assert "observed agent" in page.locator("#broadcast-targets").inner_text().lower()

            page.locator("#broadcast-awake").select_option("awake")
            playwright.expect(page.locator("label.broadcast-target")).to_have_count(1)
            assert "done" in page.locator("label.broadcast-target").inner_text()
            page.locator("#broadcast-scope").select_option("all")
            playwright.expect(page.locator("label.broadcast-target")).to_have_count(3)
            assert "dormant" in page.locator("#broadcast-targets").inner_text()
            page.locator("#broadcast-awake").select_option("asleep")
            playwright.expect(page.locator("label.broadcast-target")).to_have_count(2)
            page.locator("#broadcast-search").fill("Needle")
            playwright.expect(page.locator("label.broadcast-target")).to_have_count(0)
            page.locator("#broadcast-awake").select_option("awake")
            playwright.expect(page.locator("label.broadcast-target")).to_have_count(1)
            assert "Remote Live" in page.locator("#broadcast-targets").inner_text()
            page.locator("#broadcast-select-none").click()
            playwright.expect(page.locator("#broadcast-send")).to_be_disabled()
            page.locator("#broadcast-select-all").click()
            playwright.expect(page.locator("#broadcast-send")).to_be_enabled()
            assert "never wakes" in page.locator("#broadcast-summary").inner_text()
            assert not any("offline/proxy/api/state" in path for _, path, _ in requests)

            dialog = page.locator("#broadcast-agents-dialog")
            box = dialog.bounding_box()
            for selector in (
                "#broadcast-scope",
                "#broadcast-awake",
                "#broadcast-search",
                "#broadcast-select-all",
                "#broadcast-select-none",
                "#broadcast-send",
            ):
                control = page.locator(selector)
                playwright.expect(control).to_be_visible()
                bounds = control.bounding_box()
                assert bounds["x"] >= box["x"] - 1
                assert bounds["x"] + bounds["width"] <= box["x"] + box["width"] + 1
            page.locator("#broadcast-cancel").click()

            page.locator("#swarm-selector").select_option("guarded")
            playwright.expect(page.locator("#notifications-active-swarm")).to_contain_text(
                "Guarded"
            )
            page.locator("#notifications-button").click()
            playwright.expect(page.locator("#fleet-control-button")).to_be_disabled()
            playwright.expect(page.locator("#restart-controller")).to_be_disabled()
            page.locator("#settings-button").click()
            playwright.expect(page.locator("#notifications-dialog")).to_be_hidden()
            playwright.expect(page.locator("#settings-dialog")).to_be_visible()
            assert any(path == "/api/federation/settings" for _, path, _ in requests)
            assert not any("/proxy/api/federation/settings" in path for _, path, _ in requests)
            page.locator("#settings-dialog [data-close]").first.click()

            page.locator("#swarm-selector").select_option("peer/a")
            page.locator("#notifications-button").click()
            playwright.expect(page.locator("#fleet-control-button")).to_be_enabled()
            page.locator("#fleet-control-button").click()
            playwright.expect(page.locator("#notifications-dialog")).to_be_hidden()
            playwright.expect(page.locator("#fleet-restart-dialog")).to_be_visible()
            playwright.expect(page.locator("#fleet-restart-workspace")).to_have_text("peer/a")
            assert any(
                path == "/api/federation/peer%2Fa/proxy/api/fleet/restarts"
                for _, path, _ in requests
            )
            page.locator("#fleet-restart-cancel").click()

            page.locator("#notifications-button").click()
            page.once("dialog", lambda prompt: prompt.dismiss())
            page.locator("#restart-controller").click()
            assert not any(
                method == "POST" and path.endswith("/controller/restart")
                for method, path, _ in requests
            )
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert page.evaluate("window.unsafe") is None
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_broadcast_confirm_cancel_then_selected_proxy_send_is_durable_and_literal(
    static_url, width
):
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(
                viewport={"width": width, "height": 1000}, has_touch=width == 390
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_websocket_mock(page)
            _, _, requests = api_fixture(page)
            page.goto(static_url)
            message = "Notify <script>window.unsafe=true</script> & keep literal"
            open_broadcast(page, message)
            assert page.locator("#broadcast-targets script").count() == 0
            assert (
                "<img src=x onerror=window.unsafe=true>"
                in page.locator("#broadcast-targets").inner_text()
            )

            page.locator("#broadcast-scope").select_option("all")
            page.locator("#broadcast-select-none").click()
            page.locator("#broadcast-search").fill("Remote Live")
            page.locator("#broadcast-select-all").click()
            page.evaluate(
                "document.querySelector('#notification-broadcast-text').value = 'changed after snapshot'"
            )
            page.once("dialog", lambda prompt: prompt.dismiss())
            page.locator("#broadcast-send").click()
            page.wait_for_timeout(50)
            assert not [
                item for item in requests if item[0] == "POST" and item[1].endswith("/messages")
            ]

            page.once("dialog", lambda prompt: prompt.accept())
            page.locator("#broadcast-send").click()
            playwright.expect(page.locator(".broadcast-result.status-queued")).to_have_count(1)
            posts = [
                item for item in requests if item[0] == "POST" and item[1].endswith("/messages")
            ]
            assert len(posts) == 1
            method, path, body = posts[0]
            assert method == "POST"
            assert path == "/api/federation/peer%2Fa/proxy/api/agents/remote-live/messages"
            assert body["text"] == message
            assert len(body["request_id"]) == 36
            assert "Request ID" in page.locator("#broadcast-results").inner_text()
            assert page.locator("#broadcast-results script").count() == 0
            forbidden = ("/wake", "/terminal", "/stop", "/controller/restart")
            assert not any(
                item[0] == "POST" and any(token in item[1] for token in forbidden)
                for item in requests
            )
            assert page.evaluate("window.unsafe") is None
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_partial_broadcast_results_survive_reopen_without_blind_resend(static_url, width):
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(
                viewport={"width": width, "height": 1000}, has_touch=width == 390
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_websocket_mock(page)
            _, _, requests = api_fixture(page, compact=True)
            page.goto(static_url)
            message = "same durable body"
            open_broadcast(page, message)
            page.locator("#broadcast-scope").select_option("all")
            playwright.expect(page.locator("label.broadcast-target")).to_have_count(2)
            page.once("dialog", lambda prompt: prompt.accept())
            page.locator("#broadcast-send").click()
            playwright.expect(page.locator(".broadcast-result.status-queued")).to_have_count(1)
            playwright.expect(page.locator(".broadcast-result.status-uncertain")).to_have_count(1)
            results = page.locator(".broadcast-result")
            ids = results.locator(".hint").all_text_contents()
            assert len(ids) == 2 and len(set(ids)) == 2
            assert all(
                "Request ID" in value and len(value.rsplit(" ", 1)[-1]) == 36 for value in ids
            )
            assert "Do not retry blindly" in page.locator("#broadcast-results").inner_text()
            first_posts = [
                item for item in requests if item[0] == "POST" and item[1].endswith("/messages")
            ]
            assert len(first_posts) == 2

            page.locator("#broadcast-close").click()
            open_broadcast(page, message)
            page.locator("#broadcast-scope").select_option("all")
            playwright.expect(page.locator(".broadcast-result.status-queued")).to_have_count(1)
            playwright.expect(page.locator(".broadcast-result.status-uncertain")).to_have_count(1)
            assert all(value in page.locator("#broadcast-results").inner_text() for value in ids)
            page.locator("#broadcast-send").click()
            playwright.expect(page.locator("#broadcast-errors")).to_contain_text(
                "do not retry blindly"
            )
            second_posts = [
                item for item in requests if item[0] == "POST" and item[1].endswith("/messages")
            ]
            assert len(second_posts) == 2
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()
