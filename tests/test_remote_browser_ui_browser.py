"""Legacy real-browser coverage for the registry-backed browser viewer."""

from __future__ import annotations

import base64
import copy
import json
import re
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import fixture_state, no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401
from test_settings_layout_browser import launch_chromium

playwright = pytest.importorskip("playwright.sync_api")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I
)
INSTANCE = "11111111-1111-4111-8111-111111111111"
REMOTE_INSTANCE = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    return _dashboard_static_url


def dashboard_state():
    value = fixture_state()
    value.update(
        agent_groups={"revision": 1, "groups": [], "assignments": {}},
        agent_groups_error=None,
        grouping_settings={
            "revision": 1,
            "server_managed": False,
            "initialized": True,
            "grouped_branches": [],
        },
        grouping_settings_error=None,
        agent_limits={
            "revision": 1,
            "max_active_managed": 20,
            "active_managed": 1,
            "scope": "orchestration",
        },
        agent_limits_error=None,
    )
    return value


def managed(browser_id, name):
    return {
        "id": browser_id,
        "name": name,
        "kind": "managed",
        "port": 9222,
        "executable": "/opt/captured-browser",
        "headless": True,
        "running": True,
        "state": "running",
        "can_stop": True,
    }


class BrowserUIAPI:
    """Owner-aware registry fixture with independently captured browser actions."""

    REMOTE = "peer/browser"
    LEGACY = "peer/legacy"

    def __init__(self):
        self.requests = []
        self.revisions = {"local": 1, self.REMOTE: 1, self.LEGACY: 1}
        self.browsers = {
            "local": [managed(INSTANCE, "Captured browser")],
            self.REMOTE: [managed(REMOTE_INSTANCE, "Remote browser")],
            self.LEGACY: [],
        }
        self.tabs = {
            owner: [
                {
                    "id": "tab-a",
                    "title": '<img src=x onerror="window.browserXSS=1">',
                    "url": "https://a.test/",
                },
                {"id": "tab-b", "title": "Second page", "url": "https://b.test/"},
            ]
            for owner in self.browsers
        }
        self.selected = {owner: "tab-a" for owner in self.browsers}

    @staticmethod
    def source(path):
        if not path.startswith("/api/federation/") or "/proxy/" not in path:
            return "local"
        return unquote(path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0])

    @staticmethod
    def endpoint(path):
        return path.split("/proxy", 1)[1] if "/proxy/" in path else path

    def count(self, suffix, *, method=None, source=None):
        return sum(
            path.endswith(suffix)
            and (method is None or request_method == method)
            and (source is None or owner == source)
            for request_method, path, owner, _body in self.requests
        )

    def bodies(self, suffix, *, method="POST", source=None):
        return [
            body
            for request_method, path, owner, body in self.requests
            if request_method == method
            and path.endswith(suffix)
            and (source is None or owner == source)
        ]

    def registry(self, owner, operation=None):
        value = {
            "revision": self.revisions[owner],
            "browsers": copy.deepcopy(self.browsers[owner]),
        }
        if operation is not None:
            value["operation"] = {"browser_id": operation}
        return value

    def route(self, route):
        request = route.request
        method = request.method
        path = urlsplit(request.url).path
        owner = self.source(path)
        endpoint = self.endpoint(path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((method, path, owner, copy.deepcopy(body)))
        status = 200

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {"id": self.REMOTE, "name": "Captured Browser", "online": True, "capabilities": {}},
                    {"id": self.LEGACY, "name": "Older Node", "online": True, "capabilities": {}},
                ],
            }
        elif endpoint == "/api/state":
            value = dashboard_state()
        elif endpoint == "/api/notifications":
            value = {"revision": 1, "count": 0, "notifications": []}
        elif endpoint == "/api/fleet/restarts":
            value = {"jobs": []}
        elif endpoint == "/api/browser/discovery":
            value = {
                "browsers": [
                    {"name": "Chromium", "executable": "/usr/bin/chromium", "supported": True}
                ],
                "unavailable": [],
            }
        elif endpoint == "/api/browser/instances" and method == "GET":
            if owner == self.LEGACY:
                status, value = 404, {"detail": "Browser registry is unavailable"}
            else:
                value = self.registry(owner)
        elif endpoint == "/api/browser/instances/launch" and method == "POST":
            assert body["revision"] == self.revisions[owner]
            assert UUID.fullmatch(body["request_id"])
            browser_id = "33333333-3333-4333-8333-333333333333"
            item = managed(browser_id, body["name"])
            item.update(headless=body["headless"], executable=body["executable"])
            self.browsers[owner].append(item)
            self.revisions[owner] += 1
            value = self.registry(owner, browser_id)
        elif endpoint.startswith("/api/browser/instances/"):
            rest = endpoint.removeprefix("/api/browser/instances/")
            if "/" not in rest:
                value = self.registry(owner)
                route.fulfill(status=status, content_type="application/json", body=json.dumps(value))
                return
            browser_id, action = rest.split("/", 1)
            assert any(item["id"] == browser_id for item in self.browsers[owner])
            if action == "tabs":
                value = {"tabs": copy.deepcopy(self.tabs[owner]), "selected": self.selected[owner]}
            elif action == "screenshot":
                route.fulfill(status=200, content_type="image/png", body=PNG)
                return
            elif action == "select":
                self.selected[owner] = body["target_id"]
                value = {"ok": True}
            elif action == "new":
                made = {"id": "tab-new", "title": "New page", "url": body["url"]}
                self.tabs[owner].append(made)
                self.selected[owner] = made["id"]
                value = {"id": made["id"]}
            elif action == "close":
                self.tabs[owner] = [tab for tab in self.tabs[owner] if tab["id"] != body["target_id"]]
                value = {"ok": True}
            elif action == "copy":
                value = {"text": "remote selection"}
            elif action == "stop":
                item = next(item for item in self.browsers[owner] if item["id"] == browser_id)
                item.update(running=False, state="stopped", can_stop=False)
                self.revisions[owner] += 1
                value = self.registry(owner, browser_id)
            else:
                value = {"ok": True}
        elif path == "/api/federation/settings":
            value = {
                "enabled": False,
                "connected": False,
                "status": "disconnected",
                "has_api_key": False,
                "relay_url": "wss://relay.invalid/v1/connect",
                "relay_node_id": "local",
                "relay_name": "Local",
                "last_error": None,
            }
        else:
            value = dashboard_state()
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


def open_page(browser, api, static_url, *, width=1280):
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        has_touch=width == 390,
        is_mobile=width == 390,
        permissions=["clipboard-read", "clipboard-write"],
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        """
        window.__socketURLs = [];
        window.WebSocket = class {
          static OPEN = 1;
          constructor(url) { this.url = String(url); this.readyState = 1;
            window.__socketURLs.push(this.url); setTimeout(() => this.onopen?.(), 0); }
          close() { this.readyState = 3; this.onclose?.({code: 1000}); }
          send() {}
        };
        """
    )
    page.route("**/api/**", no_auth_mock(api.route))
    page.goto(static_url)
    playwright.expect(page.locator(".agent-card")).to_have_count(18)
    return context, page, errors


def open_settings(page):
    page.locator("#notifications-button").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    playwright.expect(page.locator("#settings-dialog")).to_be_visible()
    return page.locator("#browser-settings-section")


def select_swarm(page, source):
    page.locator("#swarm-selector").select_option(source)
    page.wait_for_function("source => activeSwarm === source && state.home === '/fixture'", arg=source)


def open_browser(page):
    if page.locator("#dashboard").is_hidden():
        page.locator("#dashboard-tab").click()
    page.locator("#add-menu-button").click()
    page.locator("#open-browser-button").click()
    playwright.expect(page.locator("#browser-panel")).to_be_visible()


def action_suffix(browser_id, action):
    return f"/api/browser/instances/{browser_id}/{action}"


def wait_for_request(page, api, suffix, *, method="POST"):
    for _ in range(50):
        if api.count(suffix, method=method):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"Timed out waiting for {method} {suffix}")


@pytest.mark.parametrize("width", [390, 1280])
def test_registry_launch_then_explicit_viewer_connect_and_input(static_url, width):
    api = BrowserUIAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors = open_page(browser, api, static_url, width=width)
            section = open_settings(page)
            assert section.get_attribute("open") is None
            assert api.count("/api/browser/instances") == 0
            section.locator("summary").first.click()
            playwright.expect(section.locator("#browser-management-list")).to_contain_text(
                "Captured browser — managed — running"
            )
            section.locator("#browser-instance-name").fill("Draft browser")
            section.locator("#browser-headless").uncheck()
            section.locator("#browser-executable-select").select_option("__custom__")
            section.locator("#browser-executable").fill(" /opt/draft-browser ")
            section.locator("#browser-settings-launch").click()
            playwright.expect(section.locator("#browser-management-list")).to_contain_text(
                "Draft browser — managed — running"
            )
            launch = api.bodies("/api/browser/instances/launch")[-1]
            assert launch == {
                "revision": 1,
                "request_id": launch["request_id"],
                "name": "Draft browser",
                "headless": False,
                "executable": "/opt/draft-browser",
            }
            assert UUID.fullmatch(launch["request_id"])

            page.locator("#settings-dialog [data-close]").last.click()
            open_browser(page)
            playwright.expect(page.locator("#browser-instance-select")).to_have_value(INSTANCE)
            playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
            playwright.expect(page.locator("#browser-status")).to_contain_text("connect to view")
            assert api.count(action_suffix(INSTANCE, "tabs")) == 0
            page.locator("#browser-connect").click()
            playwright.expect(page.locator("#browser-connect")).to_have_text("Disconnect")
            playwright.expect(page.locator("#browser-screen")).to_be_visible()
            playwright.expect(page.locator("#browser-tabs button").first).to_contain_text(
                '<img src=x onerror="window.browserXSS=1">'
            )
            assert page.evaluate("window.browserXSS") is None

            box = page.locator("#browser-screen").bounding_box()
            assert box and box["width"] > 0 and box["height"] > 0
            page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.4)
            page.locator("#browser-screen").press("q")
            page.locator("#browser-screen").press("Enter")
            for action in ("click", "text", "key"):
                wait_for_request(page, api, action_suffix(INSTANCE, action))
            click = api.bodies(action_suffix(INSTANCE, "click"))[-1]
            assert click["target_id"] == "tab-a"
            assert click["x_ratio"] == pytest.approx(0.6, abs=0.08)
            assert api.bodies(action_suffix(INSTANCE, "text"))[-1] == {
                "target_id": "tab-a",
                "text": "q",
            }
            assert api.bodies(action_suffix(INSTANCE, "key"))[-1]["key"] == "Enter"

            page.evaluate("navigator.clipboard.writeText('paste through controls')")
            page.locator("#browser-paste").click()
            page.locator("#browser-copy").click()
            page.wait_for_function("() => navigator.clipboard.readText().then(v => v === 'remote selection')")
            assert api.bodies(action_suffix(INSTANCE, "text"))[-1]["text"] == "paste through controls"

            page.locator("#browser-back").click()
            wait_for_request(page, api, action_suffix(INSTANCE, "back"))
            assert api.bodies(action_suffix(INSTANCE, "back"))[-1] == {"target_id": "tab-a"}
            page.wait_for_timeout(300)

            before = api.count(action_suffix(INSTANCE, "new"), method="POST")
            page.locator("#browser-url").fill("https://new.test/path")
            page.locator("#browser-new").click()
            wait_for_request(page, api, action_suffix(INSTANCE, "new"))
            assert api.count(action_suffix(INSTANCE, "new"), method="POST") == before + 1
            assert api.bodies(action_suffix(INSTANCE, "new"))[-1] == {"url": "https://new.test/path"}
            assert not any("/ws/browser" in url.lower() for url in page.evaluate("window.__socketURLs"))
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not errors
            context.close()
        finally:
            browser.close()


def test_connect_disconnect_and_settings_stop_are_separate(static_url):
    api = BrowserUIAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors = open_page(browser, api, static_url)
            open_browser(page)
            page.locator("#browser-connect").click()
            playwright.expect(page.locator("#browser-screen")).to_be_visible()
            page.locator("#browser-connect").click()
            playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
            playwright.expect(page.locator("#browser-screen")).to_be_hidden()
            assert api.count(action_suffix(INSTANCE, "stop"), method="POST") == 0
            assert page.locator("#browser-panel").get_by_role("button", name="Stop").count() == 0

            page.locator("#dashboard-tab").click()
            section = open_settings(page)
            section.locator("summary").first.click()
            row = section.locator(".browser-management-row").filter(has_text="Captured browser")
            playwright.expect(row.get_by_role("button", name="Stop", exact=True)).to_be_enabled()
            assert api.count(action_suffix(INSTANCE, "stop"), method="POST") == 0
            assert not errors
            context.close()
        finally:
            browser.close()


def test_remote_registry_unavailable_and_owner_paths_use_authenticated_fetch(static_url):
    api = BrowserUIAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors = open_page(browser, api, static_url)
            page.evaluate(
                """() => {
                  window.browserAuthPaths = [];
                  const auth = SwarmAuth.BrowserAuth, original = auth.fetch.bind(auth);
                  auth.fetch = (path, options) => { window.browserAuthPaths.push(String(path)); return original(path, options); };
                }"""
            )
            select_swarm(page, api.LEGACY)
            open_browser(page)
            playwright.expect(page.locator("#browser-status")).to_have_text(
                "Browser instances unavailable"
            )
            playwright.expect(page.locator("#browser-connect")).to_be_disabled()
            legacy_path = f"/api/federation/{api.LEGACY.replace('/', '%2F')}/proxy/api/browser/instances"
            assert legacy_path in page.evaluate("window.browserAuthPaths")

            page.locator("#dashboard-tab").click()
            select_swarm(page, api.REMOTE)
            open_browser(page)
            playwright.expect(page.locator("#browser-owner")).to_have_text(
                "Owning server: Captured Browser"
            )
            playwright.expect(page.locator("#browser-instance-select")).to_have_value(REMOTE_INSTANCE)
            playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
            assert api.count(action_suffix(REMOTE_INSTANCE, "tabs"), source=api.REMOTE) == 0
            assert not errors
            context.close()
        finally:
            browser.close()


def test_png_polling_pause_and_late_owner_result_are_guarded(static_url):
    api = BrowserUIAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors = open_page(browser, api, static_url)
            open_browser(page)
            page.locator("#browser-connect").click()
            playwright.expect(page.locator("#browser-screen")).to_be_visible()
            page.locator("#browser-interval").fill("0.25")
            page.locator("#browser-interval").dispatch_event("change")
            screenshots = api.count(action_suffix(INSTANCE, "screenshot"))
            page.wait_for_timeout(650)
            assert api.count(action_suffix(INSTANCE, "screenshot")) > screenshots
            page.locator("#browser-pause").click()
            paused = api.count(action_suffix(INSTANCE, "screenshot"))
            page.wait_for_timeout(650)
            assert api.count(action_suffix(INSTANCE, "screenshot")) == paused

            page.evaluate(
                """() => {
                  window.revokedBrowserURLs = [];
                  const revoke = URL.revokeObjectURL.bind(URL);
                  URL.revokeObjectURL = value => { window.revokedBrowserURLs.push(value); revoke(value); };
                  const auth = SwarmAuth.BrowserAuth, original = auth.fetch.bind(auth);
                  window.delayNextBrowserShot = true;
                  auth.fetch = async (path, options) => {
                    const response = await original(path, options);
                    if (window.delayNextBrowserShot && String(path).includes('/screenshot')) {
                      window.delayNextBrowserShot = false;
                      await new Promise(resolve => { window.releaseLateBrowserShot = resolve; });
                    }
                    return response;
                  };
                }"""
            )
            old_source = page.locator("#browser-screen").get_attribute("src")
            page.locator("#browser-pause").click()
            page.wait_for_function("() => typeof window.releaseLateBrowserShot === 'function'")
            page.locator("#dashboard-tab").click()
            select_swarm(page, api.REMOTE)
            page.evaluate("window.releaseLateBrowserShot()")
            page.wait_for_timeout(100)
            assert old_source in page.evaluate("window.revokedBrowserURLs")
            assert page.locator("#browser-screen").get_attribute("src") is None
            assert api.count(action_suffix(INSTANCE, "stop"), method="POST") == 0
            assert not errors
            context.close()
        finally:
            browser.close()
