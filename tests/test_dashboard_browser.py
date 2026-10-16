"""Optional real layout checks: pip install playwright; playwright install chromium --only-shell."""

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
STATIC = Path(__file__).resolve().parents[1] / "src" / "swarm" / "static"


class StaticHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/static/"):
            self.path = self.path[len("/static") :]
        super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture
def static_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticHandler, directory=str(STATIC)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fixture_state():
    agents = []
    for i in range(18):
        agents.append(
            {
                "id": f"a{i}",
                "name": f"Agent {i}",
                "goal": "Hidden goal details",
                "status": "running" if i == 2 else "idle",
                "alive": i in (0, 2),
                "parent_id": "a0" if 1 <= i <= 4 else "a2" if i in (5, 6) else None,
                "backend": "pi",
                "managed": True,
                "auto_resume": False,
                "activity": f"Current task {i}",
                "folder": "/workspace",
                "notes": "Checkpoint",
                "session_file": "/session.jsonl",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
    agents[0]["product"] = {
        "id": "product-0",
        "kind": "link",
        "label": "PR #42",
        "url": "https://example.com/pr/42",
    }
    agents[7]["product"] = {
        "id": "product-7",
        "kind": "file",
        "label": "A downloadable report with a long label",
        "filename": "report.zip",
    }
    agents[5]["name"] = "Needle child"
    agents[7]["name"] = "Long unbroken name " + "X" * 95
    agents[8]["name"] = '<img src=x onerror="window.unsafe=true">'
    return {
        "agents": agents,
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }


@pytest.mark.parametrize("width", [1280, 390])
def test_dense_org_chart_layout_and_interactions(static_url, tmp_path, width):
    state = fixture_state()
    with playwright.sync_playwright() as runner:
        executable = Path(runner.chromium.executable_path)
        # --only-shell installs a separate executable; launch decides the exact location.
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(
                exc
            ) or "error while loading shared libraries" in str(exc):
                pytest.skip(f"Optional Chromium runtime unavailable: {executable}")
            raise
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script("""window.WebSocket = class {
                static OPEN = 1;
                constructor() { this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
                close() { this.readyState = 3; }
                send() {}
            };""")
            page.route(
                "**/api/**",
                lambda route: route.fulfill(
                    content_type="application/json", body=json.dumps(state)
                ),
            )
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(18)
            playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(18)
            assert not page.locator(".goal:visible").count()
            assert not page.locator(".agent-card img").count()
            playwright.expect(page.locator(".product-link:visible")).to_have_count(2)
            assert (
                page.locator('[data-agent-id="a0"] .product-link').get_attribute("href")
                == "https://example.com/pr/42"
            )
            assert (
                page.locator('[data-agent-id="a7"] .product-link').get_attribute("download")
                == "report.zip"
            )
            for id in ("a0", "a7"):
                tile = page.locator(f'[data-agent-id="{id}"]').bounding_box()
                link = page.locator(f'[data-agent-id="{id}"] .product-link').bounding_box()
                assert link["y"] + link["height"] <= tile["y"] + tile["height"]
                assert link["x"] + link["width"] <= tile["x"] + tile["width"]
            assert (
                page.locator('[data-agent-id="a0"] h3').evaluate("e => getComputedStyle(e).color")
                == "rgb(255, 123, 123)"
            )
            assert (
                page.locator('[data-agent-id="a1"] h3').evaluate("e => getComputedStyle(e).color")
                != "rgb(255, 123, 123)"
            )
            boxes = page.locator(".agent-card").evaluate_all(
                "els => els.map(e => { const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; })"
            )
            for box in boxes:
                assert box["width"] == 128 and box["height"] == 128
            for i, a in enumerate(boxes):
                for b in boxes[i + 1 :]:
                    assert (
                        a["x"] + a["width"] <= b["x"]
                        or b["x"] + b["width"] <= a["x"]
                        or a["y"] + a["height"] <= b["y"]
                        or b["y"] + b["height"] <= a["y"]
                    )
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert (
                page.locator('[data-agent-id="a5"]').evaluate(
                    "e => e.parentElement.parentElement.parentElement.querySelector(':scope > .agent-card').dataset.agentId"
                )
                == "a2"
            )
            expand = page.locator('[data-agent-id="a0"] .card-toggle').bounding_box()
            shortcut = page.locator('[data-agent-id="a0"] .card-terminal')
            shortcut_box = shortcut.bounding_box()
            assert shortcut.inner_text() == "↗"
            assert shortcut_box["x"] == expand["x"]
            assert shortcut_box["y"] >= expand["y"] + expand["height"]
            shortcut.click()
            playwright.expect(page.locator("#terminals")).to_be_visible()
            assert page.locator(".terminal-header button:visible").all_text_contents() == [
                "History",
                "↻ Reconnect",
            ]
            page.locator(".tab-group .tab-close").click()
            playwright.expect(page.locator("#dashboard")).to_be_visible()
            page.locator('[data-agent-id="a0"] .card-toggle').click()
            playwright.expect(page.locator('[data-agent-id="a0"] .card-terminal')).to_be_hidden()
            playwright.expect(page.locator('[data-agent-id="a0"] .goal')).to_be_visible()
            assert page.locator('[data-agent-id="a0"]').bounding_box()["width"] > 128
            page.locator("#collapse-all").click()
            playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(18)
            page.locator("#search").fill("Needle")
            playwright.expect(page.locator(".agent-card")).to_have_count(3)
            playwright.expect(page.locator(".org-node.is-context")).to_have_count(2)
            playwright.expect(page.locator("#hierarchy-summary")).to_contain_text("1 match")
            toggle = page.locator('[data-agent-id="a5"] .card-toggle')
            toggle.focus()
            state["agents"][5]["activity"] = "Updated hover task"
            page.evaluate("data => applyState(data)", state)
            playwright.expect(page.locator('[data-agent-id="a5"] .card-toggle')).to_be_focused()
            assert "Updated hover task" in page.locator('[data-agent-id="a5"]').get_attribute(
                "title"
            )
            page.locator('[data-agent-id="a5"] .card-terminal').focus()
            page.evaluate("data => applyState(data)", state)
            playwright.expect(page.locator('[data-agent-id="a5"] .card-terminal')).to_be_focused()
            page.locator("#search").fill("")
            page.locator("#agents").screenshot(path=str(tmp_path / f"org-chart-{width}.png"))
            page.locator("#search").fill("No such agent")
            playwright.expect(page.locator("#agents")).to_be_hidden()
            playwright.expect(page.locator("#empty")).to_be_visible()
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_local_relay_settings_dialog_connect_disconnect_and_errors(static_url, width):
    profile = {
        "relay_url": "wss://relay.example:9443/v1/connect",
        "relay_node_id": "demo-a",
        "relay_name": "Demo A",
        "has_api_key": False,
        "enabled": False,
        "connected": False,
        "status": "disconnected",
        "last_error": None,
    }
    key = "browser-fixture-key-0123456789-abcdefgh"
    requests = []
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
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script("""window.WebSocket = class {
                static OPEN = 1;
                constructor() { this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
                close() { this.readyState = 3; }
                send() {}
            };""")

            def route_api(route):
                from urllib.parse import urlsplit

                path = urlsplit(route.request.url).path
                requests.append((route.request.method, path))
                status = 200
                if path == "/api/federation/settings":
                    value = dict(profile)
                elif path == "/api/federation/connect":
                    body = route.request.post_data_json
                    if not body["relay_url"].startswith("wss://"):
                        status, value = 400, {"detail": "Relay URL must use wss://"}
                    else:
                        assert body["relay_api_key"] == key
                        profile.update(
                            {
                                name: body[name]
                                for name in ("relay_url", "relay_node_id", "relay_name")
                            }
                        )
                        profile.update(
                            enabled=True, connected=True, has_api_key=True, status="connected"
                        )
                        status, value = 202, dict(profile)
                elif path == "/api/federation/disconnect":
                    profile.update(enabled=False, connected=False, status="disconnected")
                    value = dict(profile)
                elif path == "/api/federation/nodes":
                    value = {
                        "enabled": profile["enabled"],
                        "relay_online": profile["connected"],
                        "nodes": [
                            {"id": "peer-b", "name": "Peer B", "online": True, "capabilities": {}}
                        ]
                        if profile["enabled"]
                        else [],
                    }
                else:
                    value = fixture_state()
                route.fulfill(
                    status=status, content_type="application/json", body=json.dumps(value)
                )

            page.route("**/api/**", route_api)
            page.goto(static_url)
            page.locator("#settings-button").click()
            assert not errors, f"Settings click raised a browser error: {errors}"
            playwright.expect(page.locator("#settings-dialog")).to_be_visible()
            form = page.locator("#relay-settings-form")
            playwright.expect(page.locator("#relay-connect")).to_have_text("Connect")
            playwright.expect(form.locator('[name="relay_node_id"]')).to_have_value("demo-a")
            assert "wss://swarm-relay.example.com:9443/v1/connect" in form.inner_text()
            assert form.locator('[name="relay_api_key"]').get_attribute("type") == "password"
            form.locator('[name="relay_api_key"]').fill(key)
            page.locator("#relay-connect").click()
            playwright.expect(page.locator("#relay-settings-status")).to_have_text("Connected")
            playwright.expect(page.locator("#relay-connect")).to_have_text("Disconnect")
            playwright.expect(form.locator('[name="relay_api_key"]')).to_have_value("")
            assert key not in page.evaluate("JSON.stringify(localStorage)")
            page.locator("#settings-dialog [data-close]").last.click()
            page.locator("#swarm-selector").select_option("peer-b")
            page.locator("#settings-button").click()
            playwright.expect(page.locator("#relay-connect")).to_have_text("Disconnect")
            page.locator("#relay-connect").click()
            playwright.expect(page.locator("#swarm-selector")).to_have_value("local")
            playwright.expect(page.locator("#settings-dialog")).to_be_hidden()
            page.locator("#settings-button").click()
            playwright.expect(page.locator("#relay-connect")).to_have_text("Connect")
            form.locator('[name="relay_url"]').fill("https://relay.example/v1/connect")
            form.locator('[name="relay_api_key"]').fill(key)
            page.locator("#relay-connect").click()
            playwright.expect(page.locator("#relay-settings-error")).to_contain_text("wss://")
            playwright.expect(page.locator("#relay-connect")).to_be_enabled()
            playwright.expect(form.locator('[name="relay_api_key"]')).to_have_value("")
            assert not any("/proxy/api/federation/" in path for _, path in requests)
            assert any(
                method == "POST" and path == "/api/federation/disconnect"
                for method, path in requests
            )
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_touch_terminal_history_scrolling_and_live_input(static_url, width):
    state = fixture_state()
    history = "\n".join(f"HISTORY-ROW-{i:04d} <script>literal</script>" for i in range(350))
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
            page = browser.new_page(
                viewport={"width": width, "height": 1000}, has_touch=True, is_mobile=width == 390
            )
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script("""window.sentFrames = []; window.WebSocket = class {
                static OPEN = 1;
                constructor() { this.readyState = 1; setTimeout(() => {
                    this.onopen?.(); this.onmessage?.({data: new TextEncoder().encode('\\x1b[?1049h\\x1b[?1000h\\x1b[?1006hLIVE VIEW').buffer});
                }, 0); }
                close() { this.readyState = 3; }
                send(data) { window.sentFrames.push(JSON.parse(data)); }
            };""")

            def respond(route):
                if "/terminal-history" in route.request.url:
                    value = {"text": history, "lines": 350, "truncated": False}
                elif "/api/federation/nodes" in route.request.url:
                    value = {"enabled": False, "nodes": []}
                else:
                    value = state
                route.fulfill(content_type="application/json", body=json.dumps(value))

            page.route("**/api/**", respond)
            page.goto(static_url)
            page.locator('[data-agent-id="a0"] .card-terminal').click()
            playwright.expect(page.locator(".terminal-screen")).to_be_visible()
            cdp = page.context.new_cdp_session(page)

            def swipe(x, start, end):
                cdp.send(
                    "Input.dispatchTouchEvent",
                    {"type": "touchStart", "touchPoints": [{"x": x, "y": start}]},
                )
                for step in range(1, 7):
                    cdp.send(
                        "Input.dispatchTouchEvent",
                        {
                            "type": "touchMove",
                            "touchPoints": [{"x": x, "y": start + (end - start) * step / 6}],
                        },
                    )
                    page.wait_for_timeout(20)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

            screen = page.locator(".terminal-screen").bounding_box()
            swipe(screen["x"] + 60, screen["y"] + 150, screen["y"] + 280)
            panel = page.locator(".terminal-history")
            playwright.expect(panel).to_be_visible()
            playwright.expect(page.locator(".terminal-history-toggle")).to_have_text("Live input")
            playwright.expect(page.locator(".terminal-history-status")).to_contain_text("350 lines")
            assert page.locator(".terminal-history-text").inner_text() == history
            assert not page.locator(".terminal-history script").count()
            before = panel.evaluate("e => e.scrollTop")
            box = panel.bounding_box()
            swipe(box["x"] + 60, box["y"] + 150, box["y"] + 320)
            page.wait_for_timeout(150)
            assert panel.evaluate("e => e.scrollTop") < before
            page.evaluate("terminals.get('a0').term.input('MUST NOT TYPE', true)")
            assert not page.evaluate("window.sentFrames.some(frame => frame.type === 'input')")
            page.locator(".terminal-history-refresh").click()
            playwright.expect(page.locator(".terminal-history-status")).to_contain_text(
                "History snapshot"
            )
            page.locator(".terminal-history-toggle").click()
            playwright.expect(page.locator(".terminal-screen")).to_be_visible()
            playwright.expect(panel).to_be_hidden()
            page.evaluate("terminals.get('a0').term.input('LIVE TYPING', true)")
            assert page.evaluate(
                "window.sentFrames.some(frame => frame.type === 'input' && frame.data === 'LIVE TYPING')"
            )
            page.locator(".terminal-history-toggle").click()
            playwright.expect(panel).to_be_visible()
            panel.focus()
            page.keyboard.press("Escape")
            playwright.expect(panel).to_be_hidden()
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()
