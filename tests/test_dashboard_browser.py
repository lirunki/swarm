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
                "↻ Reconnect"
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
def test_compact_status_adoption_and_help_controls(static_url, width):
    state = fixture_state()
    state["agents"] = state["agents"][:8]
    statuses = ["running", "waiting", "idle", "done", "dormant", "idle", "running", "idle"]
    for agent, status in zip(state["agents"], statuses, strict=True):
        agent.update(status=status, parent_id=None)
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

            def respond(route):
                url = route.request.url
                if "/api/federation/nodes" in url:
                    value = {"enabled": False, "nodes": []}
                elif "/api/pi/sessions" in url or "/api/grok/sessions" in url:
                    value = {"sessions": [], "errors": {}, "roots": []}
                elif "/api/federation/settings" in url:
                    value = {
                        "enabled": False,
                        "status": "disconnected",
                        "has_api_key": False,
                        "relay_url": "",
                        "relay_node_id": "local-node",
                        "relay_name": "Local",
                    }
                else:
                    value = state
                route.fulfill(content_type="application/json", body=json.dumps(value))

            page.route("**/api/**", respond)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(8)
            assert not page.locator("#summary").count()
            if width >= 1000:
                title = page.locator(".page-head-title .eyebrow").bounding_box()
                control = page.locator("#status-filter").bounding_box()
                adopt = page.locator("#adoption-menu-button").bounding_box()
                search = page.locator("#search").bounding_box()
                create = page.locator("#create-button").bounding_box()
                assert title["x"] == control["x"]
                assert control["y"] >= title["y"] + title["height"]
                assert control["x"] + control["width"] <= adopt["x"]
                assert search["y"] >= adopt["y"] + adopt["height"]
                assert abs(create["x"] + create["width"] - search["x"] - search["width"]) < 1
            page.locator("#status-filter-button").click()
            playwright.expect(page.locator('[data-status-count="idle"]')).to_have_text("3")
            playwright.expect(page.locator('[data-status-count="running"]')).to_have_text("2")
            assert (
                page.locator('[data-status-count="running"]').evaluate(
                    "e => getComputedStyle(e).color"
                )
                == "rgb(104, 216, 174)"
            )
            page.locator('input[data-status="all"]').uncheck()
            playwright.expect(page.locator("#empty")).to_be_visible()
            page.locator('input[data-status="running"]').check()
            page.locator('input[data-status="waiting"]').check()
            playwright.expect(page.locator(".agent-card")).to_have_count(3)
            playwright.expect(page.locator("#status-filter-label")).to_have_text("2 states")
            playwright.expect(page.locator("#status-filter-count")).to_have_text("3")
            selected = page.locator('input[data-status="waiting"]')
            selected.focus()
            state["agents"].append({**state["agents"][1], "id": "new-wait"})
            page.evaluate("data => applyState(data)", state)
            playwright.expect(selected).to_be_focused()
            playwright.expect(page.locator('[data-status-count="waiting"]')).to_have_text("2")
            playwright.expect(page.locator(".agent-card")).to_have_count(4)
            page.keyboard.press("Escape")
            playwright.expect(page.locator("#status-filter-menu")).to_be_hidden()
            playwright.expect(page.locator("#status-filter-button")).to_be_focused()
            page.locator("#status-filter-button").click()
            page.locator('input[data-status="all"]').check()
            page.keyboard.press("Escape")
            playwright.expect(page.locator(".agent-card")).to_have_count(9)
            for item, dialog in (
                ("#adopt-button", "#adopt-dialog"),
                ("#import-grok-button", "#import-grok-dialog"),
            ):
                page.locator("#adoption-menu-button").click()
                playwright.expect(page.locator("#adoption-options")).to_be_visible()
                page.locator(item).click()
                playwright.expect(page.locator(dialog)).to_be_visible()
                playwright.expect(page.locator("#adoption-options")).to_be_hidden()
                page.locator(dialog + " [data-close]").first.click()
            assert page.locator("#settings-button").inner_text() == "⚙"
            assert page.locator("#settings-button").get_attribute("aria-label") == "Settings"
            page.locator("#help-button").click()
            playwright.expect(page.get_by_role("heading", name="Swarm help")).to_be_visible()
            assert "Shift" in page.locator("#help-dialog").inner_text()
            assert "Done copying" in page.locator("#help-dialog").inner_text()
            page.keyboard.press("Escape")
            playwright.expect(page.locator("#help-dialog")).to_be_hidden()
            page.get_by_role("button", name="Settings", exact=True).click()
            playwright.expect(page.locator("#settings-dialog")).to_be_visible()
            page.keyboard.press("Escape")
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("mobile", [True, False])
def test_native_mobile_copy_overlay_and_unchanged_desktop(static_url, mobile):
    state = fixture_state()
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
            context = browser.new_context(
                viewport={"width": 390 if mobile else 1280, "height": 1000},
                has_touch=mobile,
                is_mobile=mobile,
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.add_init_script("""window.sentFrames = []; window.WebSocket = class {
                static OPEN = 1;
                constructor(url) { this.readyState = 1; setTimeout(() => {
                    this.onopen?.();
                    if (url.includes('/terminal/')) this.onmessage?.({data: new TextEncoder().encode('\\x1b[?1049h\\x1b[?1000h\\x1b[?1006hVISIBLE-ONE <script>literal</script>\\r\\nVISIBLE-TWO\\r\\n').buffer});
                }, 0); }
                close() { this.readyState = 3; }
                send(data) { window.sentFrames.push(JSON.parse(data)); }
            };""")
            page.route(
                "**/api/**",
                lambda route: route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {"enabled": False, "nodes": []}
                        if "/api/federation/nodes" in route.request.url
                        else state
                    ),
                ),
            )
            page.goto(static_url)
            page.locator('[data-agent-id="a0"] .card-terminal').click()
            page.wait_for_function(
                "terminals.get('a0').term.buffer.active.getLine(0)?.translateToString().includes('VISIBLE-ONE')"
            )
            copy = page.locator(".terminal-copy-button")
            if not mobile:
                playwright.expect(copy).to_be_hidden()
                assert page.locator(".terminal-header button:visible").all_text_contents() == [
                    "↻ Reconnect"
                ]
                playwright.expect(page.locator(".terminal-copy-text")).to_be_hidden()
                box = page.locator(".xterm-screen").bounding_box()
                cols, rows = page.evaluate(
                    "[terminals.get('a0').term.cols, terminals.get('a0').term.rows]"
                )
                page.keyboard.down("Shift")
                page.mouse.move(box["x"] + 1, box["y"] + box["height"] / rows / 2)
                page.mouse.down()
                page.mouse.move(
                    box["x"] + box["width"] / cols * 11,
                    box["y"] + box["height"] / rows / 2,
                    steps=8,
                )
                page.mouse.up()
                page.keyboard.up("Shift")
                selected = page.evaluate("terminals.get('a0').term.getSelection()")
                assert selected.startswith("VISIBLE")
                page.evaluate("navigator.clipboard.writeText('desktop-copy-sentinel')")
                assert page.evaluate("document.execCommand('copy')")
                assert page.evaluate("navigator.clipboard.readText()") == selected
                playwright.expect(page.locator(".terminal-copy-text")).to_be_hidden()
            else:
                playwright.expect(copy).to_have_text("Copy text")
                expected = page.evaluate("SwarmTerminalCopy.visibleText(terminals.get('a0').term)")
                copy.click()
                area = page.locator(".terminal-copy-text")
                playwright.expect(area).to_be_visible()
                playwright.expect(area).to_have_value(expected)
                assert area.evaluate("e => e.readOnly")
                assert not page.locator(".terminal-copy-text script").count()
                assert not page.locator("dialog[open]").count()
                playwright.expect(copy).to_have_text("Done copying")
                screen_box, copy_box = (
                    page.locator(".terminal-screen").bounding_box(),
                    area.bounding_box(),
                )
                for key in ("x", "y", "width", "height"):
                    assert abs(screen_box[key] - copy_box[key]) < 1, (screen_box, copy_box)
                page.keyboard.type("NOT-EDITABLE")
                playwright.expect(area).to_have_value(expected)
                page.evaluate("terminals.get('a0').term.input('NO TERMINAL INPUT', true)")
                assert not page.evaluate("window.sentFrames.some(value => value.type === 'input')")
                page.evaluate("terminals.get('a0').term.write('NEW OUTPUT')")
                playwright.expect(area).to_have_value(expected)
                area.evaluate("e => { e.focus(); e.setSelectionRange(0, 11); }")
                selected = expected[:11]
                page.keyboard.press("Control+C")
                playwright.expect(area).to_be_hidden()
                playwright.expect(copy).to_have_text("Copy text")
                assert page.evaluate("navigator.clipboard.readText()") == selected
                assert not page.evaluate("window.sentFrames.some(value => value.type === 'input')")
                copy.click()
                playwright.expect(area).to_be_visible()
                copy.click()
                playwright.expect(area).to_be_hidden()
                assert area.input_value() == ""
                page.evaluate("terminals.get('a0').term.input('LIVE INPUT', true)")
                assert page.evaluate(
                    "window.sentFrames.some(value => value.type === 'input' && value.data === 'LIVE INPUT')"
                )
                assert not any("terminal-history" in url for url in requests)
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()


def test_dashboard_reference_layout(static_url, tmp_path):
    state = fixture_state()
    state["agents"] = state["agents"][:13]
    names = [
        "coordinator",
        "parser-fix-test-evidence",
        "workspace-fix-timer-evidence",
        "build_checks",
        "Research",
        "App-Integration",
        "UTILITIES",
        "RECOVERY_WORKER",
        "reviewer",
        "audit",
        "FIXER",
        "deployment",
        "Caretaker PR #42",
    ]
    for i, agent in enumerate(state["agents"]):
        agent.update(
            name=names[i],
            parent_id="a10" if i == 12 else None,
            status="done"
            if i == 0
            else "dormant"
            if i == 12
            else "running"
            if i in (3, 10, 11)
            else "idle",
            alive=i in (0, 3, 10, 11, 12),
        )
        agent.pop("product", None)
        if i in (3, 10, 11, 12):
            agent["product"] = {
                "id": f"product-{i}",
                "kind": "link",
                "label": "PR / report produced",
                "url": "https://example.invalid/result",
            }
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
            page = browser.new_page(viewport={"width": 1450, "height": 762})
            page.add_init_script("""window.WebSocket = class {
                static OPEN = 1;
                constructor() { this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
                close() { this.readyState = 3; } send() {}
            };""")

            def respond(route):
                payload = (
                    {"enabled": False, "nodes": []}
                    if "/api/federation/" in route.request.url
                    else state
                )
                route.fulfill(content_type="application/json", body=json.dumps(payload))

            page.route("**/api/**", respond)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(13)
            dashboard = page.locator("#dashboard").bounding_box()
            boxes = [page.locator(f'[data-agent-id="a{i}"]').bounding_box() for i in range(13)]
            assert all(abs(box["y"] - boxes[0]["y"]) < 1 for box in boxes[:10])
            assert 140 <= boxes[0]["y"] - dashboard["y"] <= 150
            assert all(box["width"] == 128 and box["height"] == 128 for box in boxes)
            assert boxes[10]["y"] > boxes[0]["y"] + 128
            assert boxes[12]["y"] > boxes[10]["y"] + 128
            assert abs(boxes[10]["x"] - boxes[12]["x"]) < 1
            assert page.get_by_text("Your agents, in sync.", exact=True).count() == 0
            assert page.locator("#hierarchy-summary").evaluate(
                "e => e.classList.contains('sr-only')"
            )
            assert page.locator("#agents").bounding_box()["width"] >= 1388
            footer = page.locator(".page-footer").bounding_box()
            assert footer["y"] >= boxes[12]["y"] + boxes[12]["height"]
            assert footer["y"] + footer["height"] <= dashboard["y"] + dashboard["height"]
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            page.locator("#dashboard").screenshot(path=str(tmp_path / "dashboard-reference.png"))
        finally:
            browser.close()
