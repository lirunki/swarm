"""Real-browser coverage for the integrated remote-browser workspace UI."""

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

# A real 1x1 PNG is sufficient: the viewer maps against rendered bounds, not fake frame data.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I
)


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


class BrowserUIAPI:
    """Complete synthetic owner API with independently captured browser state."""

    REMOTE = "peer/browser"
    LEGACY = "peer/legacy"

    def __init__(self):
        self.requests = []
        self.settings = {
            source: {"revision": 7, "enabled": False, "headless": True, "executable": None}
            for source in ("local", self.REMOTE, self.LEGACY)
        }
        self.statuses = {
            source: {"enabled": False, "running": False, "state": "disabled", "error": None}
            for source in ("local", self.REMOTE, self.LEGACY)
        }
        self.tabs = {
            source: [
                {
                    "id": "tab-a",
                    "title": '<img src=x onerror="window.browserXSS=1">',
                    "url": "https://a.test/",
                },
                {"id": "tab-b", "title": "Second page", "url": "https://b.test/"},
            ]
            for source in ("local", self.REMOTE, self.LEGACY)
        }
        self.selected = {source: "tab-a" for source in self.tabs}
        self.reject_settings = False

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
                    {
                        "id": self.REMOTE,
                        "name": "Captured Browser",
                        "online": True,
                        "capabilities": {},
                    },
                    {"id": self.LEGACY, "name": "Older Node", "online": True, "capabilities": {}},
                ],
            }
        elif endpoint == "/api/state":
            value = dashboard_state()
        elif endpoint == "/api/notifications":
            value = {"revision": 1, "count": 0, "notifications": []}
        elif endpoint == "/api/fleet/restarts":
            value = {"jobs": []}
        elif endpoint == "/api/browser/settings":
            if self.reject_settings:
                status, value = 409, {"detail": "settings changed safely"}
            elif method == "POST":
                assert UUID.fullmatch(body["request_id"])
                current = self.settings[owner]
                assert body["revision"] == current["revision"]
                value = {
                    "revision": current["revision"] + 1,
                    "enabled": body["enabled"],
                    "headless": body["headless"],
                    "executable": body["executable"],
                }
                self.settings[owner] = copy.deepcopy(value)
                self.statuses[owner].update(
                    enabled=value["enabled"],
                    state="stopped" if value["enabled"] else "disabled",
                )
            else:
                value = copy.deepcopy(self.settings[owner])
        elif endpoint == "/api/browser/status":
            if owner == self.LEGACY:
                status, value = 404, {"detail": "Browser API is unavailable on this older node"}
            else:
                value = copy.deepcopy(self.statuses[owner])
        elif endpoint == "/api/browser/launch":
            assert UUID.fullmatch(body["request_id"])
            assert self.settings[owner]["enabled"]
            self.statuses[owner] = {
                "enabled": True,
                "running": True,
                "state": "running",
                "error": None,
            }
            value = {"running": True, "started": True}
        elif endpoint == "/api/browser/tabs":
            value = {"tabs": copy.deepcopy(self.tabs[owner]), "selected": self.selected[owner]}
        elif endpoint == "/api/browser/select":
            self.selected[owner] = body["target_id"]
            value = {"ok": True}
        elif endpoint == "/api/browser/new":
            made = {"id": "tab-new", "title": "New page", "url": body["url"]}
            self.tabs[owner].append(made)
            self.selected[owner] = made["id"]
            value = {"id": made["id"]}
        elif endpoint == "/api/browser/close":
            self.tabs[owner] = [tab for tab in self.tabs[owner] if tab["id"] != body["target_id"]]
            if self.selected[owner] == body["target_id"]:
                self.selected[owner] = self.tabs[owner][0]["id"] if self.tabs[owner] else None
            value = {"ok": True}
        elif endpoint == "/api/browser/copy":
            value = {"text": "remote selection"}
        elif endpoint.startswith("/api/browser/screenshot"):
            route.fulfill(status=200, content_type="image/png", body=PNG)
            return
        elif endpoint.startswith("/api/browser/"):
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
    errors, socket_urls = [], []
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
    socket_urls.extend(page.evaluate("window.__socketURLs"))
    return context, page, errors, socket_urls


def open_settings(page):
    page.locator("#notifications-button").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    playwright.expect(page.locator("#settings-dialog")).to_be_visible()
    return page.locator("#browser-settings-section")


def select_swarm(page, source):
    playwright.expect(page.locator("#swarm-selector")).to_be_visible()
    page.locator("#swarm-selector").select_option(source)
    page.wait_for_function(
        "source => activeSwarm === source && state.home === '/fixture'", arg=source
    )


def open_browser(page):
    """Open the optional Browser workspace through the dashboard Add menu."""
    if page.locator("#dashboard").is_hidden():
        page.locator("#dashboard-tab").click()
    page.locator("#add-menu-button").click()
    playwright.expect(page.locator("#add-menu-options")).to_be_visible()
    page.locator("#open-browser-button").click()
    playwright.expect(page.locator("#browser-panel")).to_be_visible()


@pytest.mark.parametrize("width", [390, 1280])
def test_remote_browser_full_ui_flow_is_responsive_and_http_only(static_url, width):
    api = BrowserUIAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors, _ = open_page(browser, api, static_url, width=width)
            section = open_settings(page)
            assert section.get_attribute("open") is None
            assert api.count("/api/browser/settings") == 0
            section.locator("summary").click()
            playwright.expect(section.locator("#browser-enabled")).to_be_enabled()
            assert api.count("/api/browser/settings", method="GET") == 1
            section.locator("#browser-enabled").check()
            section.locator("#browser-headless").uncheck()
            section.locator('#browser-executable-select').select_option('__custom__')
            section.locator("#browser-executable").fill(" /opt/captured-browser ")
            section.locator("#browser-settings-save").click()
            playwright.expect(section.locator("#browser-settings-save")).to_be_disabled()
            saved = api.bodies("/api/browser/settings")[-1]
            assert saved == {
                "revision": 7,
                "request_id": saved["request_id"],
                "enabled": True,
                "headless": False,
                "executable": "/opt/captured-browser",
            }
            assert UUID.fullmatch(saved["request_id"])
            assert api.count("/api/browser/launch", method="POST") == 0

            page.locator("#settings-dialog [data-close]").last.click()
            dashboard = page.locator("#dashboard-tab")
            browser_group = page.locator("#browser-tab-group")
            playwright.expect(browser_group).to_be_hidden()
            page.locator('.agent-card[data-agent-id="a2"] .card-terminal').click()
            playwright.expect(browser_group).to_be_hidden()
            open_browser(page)
            playwright.expect(page.locator("#browser-status")).to_have_text("Browser stopped")
            assert dashboard.evaluate("node => node.nextElementSibling.id") == "browser-tab-group"
            assert browser_group.evaluate("node => getComputedStyle(node).position") == "sticky"
            assert browser_group.evaluate("node => getComputedStyle(node).left") == "102px"
            assert dashboard.bounding_box()["width"] == pytest.approx(102, abs=0.1)
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert api.count("/api/browser/launch", method="POST") == 0

            page.locator("#browser-launch").click()
            playwright.expect(page.locator("#browser-screen")).to_be_visible()
            launch = api.bodies("/api/browser/launch")[-1]
            assert list(launch) == ["request_id"] and UUID.fullmatch(launch["request_id"])
            playwright.expect(page.locator("#browser-interval")).to_have_value("2")
            assert page.locator("#browser-tabs img").count() == 0
            playwright.expect(page.locator("#browser-tabs button").first).to_contain_text(
                '<img src=x onerror="window.browserXSS=1">'
            )
            assert page.evaluate("window.browserXSS") is None

            box = page.locator("#browser-screen").bounding_box()
            assert box and box["width"] > 0 and box["height"] > 0
            page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.4)
            page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.2)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.8)
            page.mouse.up()
            page.locator("#browser-screen").press("q")
            page.locator("#browser-screen").press("Enter")
            page.wait_for_function(
                "() => document.querySelector('#browser-screen').src.startsWith('blob:')"
            )
            page.wait_for_timeout(100)
            click = api.bodies("/api/browser/click")[-1]
            assert click["target_id"] == "tab-a"
            assert click["x_ratio"] == pytest.approx(0.6, abs=0.08)
            assert click["y_ratio"] == pytest.approx(0.4, abs=0.08)
            drag = api.bodies("/api/browser/drag")[-1]
            assert drag["target_id"] == "tab-a" and set(drag) == {"target_id", "start", "end"}
            assert api.bodies("/api/browser/text")[-1] == {"target_id": "tab-a", "text": "q"}
            assert api.bodies("/api/browser/key")[-1]["key"] == "Enter"

            page.evaluate("navigator.clipboard.writeText('paste through controls')")
            page.locator("#browser-paste").click()
            page.wait_for_function(
                "() => document.querySelector('#browser-status').textContent.includes('tab')"
            )
            page.locator("#browser-copy").click()
            page.wait_for_function(
                "() => document.querySelector('#browser-status').textContent.startsWith('Copied')"
            )
            assert api.bodies("/api/browser/text")[-1]["text"] == "paste through controls"
            assert page.evaluate("navigator.clipboard.readText()") == "remote selection"

            page.locator("#browser-url").fill("javascript:alert(1)")
            page.locator("#browser-new").click()
            playwright.expect(page.locator("#toast")).to_contain_text("Enter an http(s) URL")
            before = api.count("/api/browser/new", method="POST")
            page.locator("#browser-url").fill("https://new.test/path")
            page.locator("#browser-new").click()
            playwright.expect(page.locator("#browser-tabs button")).to_have_count(3)
            assert api.count("/api/browser/new", method="POST") == before + 1
            assert api.bodies("/api/browser/new")[-1] == {"url": "https://new.test/path"}
            assert not any(
                "/ws/browser" in url.lower() for url in page.evaluate("window.__socketURLs")
            )
            assert not errors
            context.close()
        finally:
            browser.close()


def test_remote_legacy_status_is_safe_and_owner_paths_use_authenticated_fetch(static_url):
    api = BrowserUIAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors, _ = open_page(browser, api, static_url)
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
                "Browser status unavailable"
            )
            playwright.expect(page.locator("#browser-launch")).to_be_disabled()
            legacy_path = (
                f"/api/federation/{api.LEGACY.replace('/', '%2F')}/proxy/api/browser/status"
            )
            assert api.count("/api/browser/status", source=api.LEGACY) == 1
            assert legacy_path in page.evaluate("window.browserAuthPaths")
            assert api.count("/api/browser/launch", method="POST") == 0

            page.locator("#dashboard-tab").click()
            select_swarm(page, api.REMOTE)
            api.settings[api.REMOTE]["enabled"] = True
            api.statuses[api.REMOTE].update(enabled=True, state="stopped")
            open_browser(page)
            playwright.expect(page.locator("#browser-owner")).to_have_text(
                "Owning server: Captured Browser"
            )
            playwright.expect(page.locator("#browser-launch")).to_be_enabled()
            assert api.count("/api/browser/status", source=api.REMOTE) == 1
            assert api.count("/api/browser/status", source="local") == 0
            assert not errors
            context.close()
        finally:
            browser.close()


def test_polling_pause_deactivate_close_and_late_owner_results_are_guarded(static_url):
    api = BrowserUIAPI()
    api.settings["local"]["enabled"] = True
    api.statuses["local"] = {"enabled": True, "running": True, "state": "running", "error": None}
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors, _ = open_page(browser, api, static_url)
            open_browser(page)
            playwright.expect(page.locator("#browser-screen")).to_be_visible()
            page.locator("#browser-interval").fill("0.25")
            page.locator("#browser-interval").dispatch_event("change")
            screenshots = api.count("/api/browser/screenshot")
            page.wait_for_function(
                "count => performance.getEntriesByType('resource').length >= 0 && "
                "document.querySelector('#browser-screen').src.startsWith('blob:')",
                arg=screenshots,
            )
            page.wait_for_timeout(600)
            assert api.count("/api/browser/screenshot") > screenshots
            page.locator("#browser-pause").click()
            paused = api.count("/api/browser/screenshot")
            page.wait_for_timeout(650)
            assert api.count("/api/browser/screenshot") == paused

            page.locator('#browser-tabs button[data-id="tab-b"]').click()
            playwright.expect(page.locator('#browser-tabs button[data-id="tab-b"]')).to_have_class(
                re.compile("active")
            )
            page.locator('#browser-tabs button[data-id="tab-a"] .tab-close').click()
            page.wait_for_function(
                "() => !document.querySelector('#browser-tabs button[data-id=\"tab-a\"]')"
            )
            assert api.bodies("/api/browser/close")[-1] == {"target_id": "tab-a"}
            assert api.selected["local"] == "tab-b"
            assert api.count("/api/browser/stop", method="POST") == 0
            page.locator("#browser-pause").click()
            page.wait_for_function(
                "() => document.querySelector('#browser-screen').src.startsWith('blob:')"
            )
            page.locator("#browser-pause").click()

            page.evaluate(
                """() => {
                  window.revokedBrowserURLs = [];
                  const revoke = URL.revokeObjectURL.bind(URL);
                  URL.revokeObjectURL = value => { window.revokedBrowserURLs.push(value); revoke(value); };
                  const auth = SwarmAuth.BrowserAuth, original = auth.fetch.bind(auth);
                  window.delayNextBrowserShot = true;
                  auth.fetch = async (path, options) => {
                    const response = await original(path, options);
                    if (window.delayNextBrowserShot && String(path).includes('/api/browser/screenshot')) {
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
            stopped = api.count("/api/browser/screenshot")
            page.wait_for_timeout(650)
            assert api.count("/api/browser/screenshot") == stopped
            select_swarm(page, api.REMOTE)
            page.evaluate("window.releaseLateBrowserShot()")
            page.wait_for_timeout(100)
            assert old_source in page.evaluate("window.revokedBrowserURLs")
            assert page.locator("#browser-screen").get_attribute("src") is None
            assert api.count("/api/browser/stop", method="POST") == 0
            assert not errors
            context.close()
        finally:
            browser.close()


def test_settings_load_without_viewer_running_lock_and_server_rejection_are_safe(static_url):
    api = BrowserUIAPI()
    api.settings["local"]["enabled"] = True
    api.statuses["local"] = {"enabled": True, "running": True, "state": "running", "error": None}
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors, _ = open_page(browser, api, static_url)
            section = open_settings(page)
            section.locator("summary").click()
            playwright.expect(section.locator("#browser-enabled")).to_be_enabled()
            assert api.count("/api/browser/settings", method="GET") == 1
            assert api.count("/api/browser/status") == 0

            page.locator("#settings-dialog [data-close]").last.click()
            open_browser(page)
            playwright.expect(page.locator("#browser-screen")).to_be_visible()
            page.locator("#notifications-button").click()
            page.get_by_role("button", name="Settings", exact=True).click()
            playwright.expect(section.locator("#browser-enabled")).to_be_disabled()
            playwright.expect(section.locator("#browser-settings-status")).to_contain_text(
                "cannot be changed while"
            )

            page.locator("#settings-dialog [data-close]").last.click()
            page.locator("#dashboard-tab").click()
            api.statuses["local"]["running"] = False
            api.statuses["local"]["state"] = "stopped"
            page.locator("#notifications-button").click()
            page.get_by_role("button", name="Settings", exact=True).click()
            section.locator("summary").click() if section.get_attribute("open") is None else None
            page.evaluate("browserSettingsController.setContext({running:false})")
            section.locator("#browser-headless").uncheck()
            api.reject_settings = True
            section.locator("#browser-settings-save").click()
            playwright.expect(section.locator("#browser-settings-status")).to_have_text(
                "settings changed safely"
            )
            assert api.count("/api/browser/launch", method="POST") == 0
            assert not errors
            context.close()
        finally:
            browser.close()


def test_browser_view_persists_per_owner_without_restoring_a_terminal_id(static_url):
    api = BrowserUIAPI()
    api.settings["local"]["enabled"] = True
    api.statuses["local"].update(enabled=True, state="stopped")
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context, page, errors, _ = open_page(browser, api, static_url)
            page.evaluate(
                """() => localStorage.setItem('swarm.ui-view-state.local', JSON.stringify({
                  terminalTabs: {ids: ['a2'], active: 'browser'}, groupedBranches: [],
                  agentGroupView: {selected: null, opened: []}
                }))"""
            )
            page.reload()
            playwright.expect(page.locator("#browser-panel")).to_be_visible()
            playwright.expect(page.locator("#browser-tab")).to_have_class(re.compile("active"))
            assert page.evaluate("activeTab") == "browser"
            assert page.locator(".terminal-pane:not([hidden])").count() == 0
            assert api.count("/api/browser/status", source="local") >= 1

            page.locator("#dashboard-tab").click()
            select_swarm(page, api.REMOTE)
            open_browser(page)
            playwright.expect(page.locator("#browser-owner")).to_have_text(
                "Owning server: Captured Browser"
            )
            remote_statuses = api.count("/api/browser/status", source=api.REMOTE)
            page.locator("#dashboard-tab").click()
            select_swarm(page, "local")
            page.locator("#browser-tab").click()
            playwright.expect(page.locator("#browser-owner")).to_have_text(
                "Owning server: Local Swarm"
            )
            assert api.count("/api/browser/status", source=api.REMOTE) == remote_statuses
            assert api.count("/api/browser/tabs", source=api.REMOTE) == 0
            assert not any(
                "/ws/browser" in url.lower() for url in page.evaluate("window.__socketURLs")
            )
            assert not errors
            context.close()
        finally:
            browser.close()
