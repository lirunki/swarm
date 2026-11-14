"""Real-browser tests for registry launch executable discovery in Settings."""

from __future__ import annotations

import copy
import json
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401
from test_settings_layout_browser import launch_chromium

playwright = pytest.importorskip("playwright.sync_api")

SOURCE_A = "source/a"
SOURCE_B = "source-b"
OLD_SOURCE = "old-controller"
SCRIPT_PATH = "/home/operator/bin/browser-wrapper.sh"
WINDOWS_EDGE = "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
CREATED = "44444444-4444-4444-8444-444444444444"

LOCAL_DISCOVERY = {
    "browsers": [
        {"name": "Microsoft Edge", "executable": "/usr/bin/msedge", "supported": True},
        {"name": "Chromium", "executable": "/snap/bin/chromium", "supported": True},
        {"name": "Chromium", "executable": "/usr/bin/chromium-browser", "supported": True},
    ],
    "unavailable": [
        {
            "name": "Windows Microsoft Edge",
            "executable": WINDOWS_EDGE,
            "reason": "Use native Windows Swarm or install Edge/Chromium inside WSL.",
        }
    ],
}


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    return _dashboard_static_url


@pytest.fixture(scope="module")
def chromium_browser():
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        yield browser
        browser.close()


def dashboard_state():
    return {
        "agents": [],
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
        "agent_groups": {"revision": 1, "groups": [], "assignments": {}},
        "agent_groups_error": None,
        "grouping_settings": {
            "revision": 1,
            "server_managed": False,
            "initialized": True,
            "grouped_branches": [],
        },
        "grouping_settings_error": None,
        "agent_limits": {
            "revision": 1,
            "max_active_managed": 20,
            "active_managed": 0,
            "scope": "orchestration",
        },
        "agent_limits_error": None,
    }


def launched(body):
    return {
        "id": CREATED,
        "name": body["name"],
        "kind": "managed",
        "port": 9222,
        "executable": body["executable"],
        "headless": body["headless"],
        "running": True,
        "state": "running",
        "can_stop": True,
    }


class DiscoveryAPI:
    """Owner-aware fake registry with lexical executable validation."""

    owners = ("local", SOURCE_A, SOURCE_B, OLD_SOURCE)

    def __init__(self):
        self.requests = []
        self.revisions = {owner: 4 for owner in self.owners}
        self.instances = {owner: [] for owner in self.owners}
        self.discoveries = {
            "local": copy.deepcopy(LOCAL_DISCOVERY),
            SOURCE_A: {
                "browsers": [{"name": "Browser A", "executable": "/opt/a-browser", "supported": True}],
                "unavailable": [],
            },
            SOURCE_B: {
                "browsers": [{"name": "Browser B", "executable": "/opt/b-browser", "supported": True}],
                "unavailable": [],
            },
            OLD_SOURCE: None,
        }
        self.discovery_status = {owner: 200 for owner in self.owners}
        self.discovery_status[OLD_SOURCE] = 404
        self.validated_executables = []

    @staticmethod
    def owner_and_endpoint(path):
        if path.startswith("/api/federation/") and "/proxy/" in path:
            encoded, endpoint = path.split("/api/federation/", 1)[1].split("/proxy", 1)
            return unquote(encoded), endpoint
        return "local", path

    def matching(self, endpoint, *, method=None, owner=None):
        return [
            request
            for request in self.requests
            if request[1] == endpoint
            and (method is None or request[0] == method)
            and (owner is None or request[2] == owner)
        ]

    @property
    def browser_mutations(self):
        return [request for request in self.requests if request[0] != "GET" and request[1].startswith("/api/browser/")]

    def registry(self, owner, operation=None):
        value = {"revision": self.revisions[owner], "browsers": copy.deepcopy(self.instances[owner])}
        if operation:
            value["operation"] = {"browser_id": operation}
        return value

    def route(self, route):
        request = route.request
        method = request.method
        path = urlsplit(request.url).path
        owner, endpoint = self.owner_and_endpoint(path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((method, endpoint, owner, copy.deepcopy(body), path))
        status = 200

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {"id": SOURCE_A, "name": "Source A", "online": True, "state_ready": True, "capabilities": {"browser": True}},
                    {"id": SOURCE_B, "name": "Source B", "online": True, "state_ready": True, "capabilities": {"browser": True}},
                    {"id": OLD_SOURCE, "name": "Old backend", "online": True, "state_ready": True, "capabilities": {}},
                ],
            }
        elif path == "/api/federation/settings":
            value = {
                "enabled": True,
                "connected": True,
                "status": "connected",
                "has_api_key": True,
                "relay_url": "wss://relay.invalid/v1/connect",
                "relay_node_id": "local",
                "relay_name": "Local",
                "last_error": None,
            }
        elif endpoint == "/api/state":
            value = dashboard_state()
        elif endpoint == "/api/notifications":
            value = {"revision": 1, "count": 0, "notifications": []}
        elif endpoint == "/api/fleet/restarts":
            value = {"jobs": []}
        elif endpoint == "/api/browser/instances" and method == "GET":
            value = self.registry(owner)
        elif endpoint == "/api/browser/instances/launch" and method == "POST":
            assert set(body) == {"revision", "request_id", "name", "headless", "executable"}
            executable = body["executable"]
            assert executable is None or (isinstance(executable, str) and executable.startswith("/") and "\0" not in executable)
            self.validated_executables.append(executable)
            assert body["revision"] == self.revisions[owner]
            self.instances[owner].append(launched(body))
            self.revisions[owner] += 1
            value = self.registry(owner, CREATED)
        elif endpoint == "/api/browser/discovery":
            status = self.discovery_status[owner]
            value = copy.deepcopy(self.discoveries[owner]) if status == 200 else {"detail": "Browser discovery endpoint is unavailable"}
        elif endpoint.startswith("/api/browser/"):
            raise AssertionError(f"Discovery test attempted unexpected browser action: {endpoint}")
        else:
            value = dashboard_state()
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


def open_page(browser, static_url, api, *, width=1280):
    context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=width == 390, is_mobile=width == 390)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        """window.WebSocket = class {
          static OPEN = 1; constructor(url) { this.url = String(url); this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
          close() { this.readyState = 3; } send() {}
        };"""
    )
    page.route("**/api/**", no_auth_mock(api.route))
    page.goto(static_url)
    playwright.expect(page.locator("#agent-count")).to_have_text("0")
    return context, page, errors


def open_browser_settings(page):
    page.locator("#notifications-button").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.locator("#settings-dialog")
    playwright.expect(dialog).to_be_visible()
    section = dialog.locator("#browser-settings-section")
    assert section.get_attribute("open") is None
    section.locator("summary").first.click()
    playwright.expect(section.locator("#browser-settings-launch")).to_be_enabled()
    return dialog, section


def select_owner(page, owner):
    page.locator("#swarm-selector").select_option(owner)
    page.wait_for_function("owner => activeSwarm === owner && state.home === '/fixture'", arg=owner)


def option_projection(section):
    return section.locator("#browser-executable-select option").evaluate_all(
        "options => options.map(option => ({value: option.value, text: option.textContent, disabled: option.disabled}))"
    )


def dispatch_input(locator, value):
    locator.evaluate("(node, value) => { node.value = value; node.dispatchEvent(new Event('input', {bubbles: true})); }", value)


def test_local_discovery_options_and_lexical_launch(static_url, chromium_browser):
    api = DiscoveryAPI()
    context, page, errors = open_page(chromium_browser, static_url, api)
    try:
        _dialog, section = open_browser_settings(page)
        playwright.expect(section.locator("#browser-discovery-status")).to_contain_text("3 installed browser option(s) found")
        relevant_gets = [endpoint for method, endpoint, owner, _body, _path in api.requests if method == "GET" and owner == "local" and endpoint in ("/api/browser/instances", "/api/browser/discovery")]
        assert relevant_gets[:2] == ["/api/browser/instances", "/api/browser/discovery"]
        options = option_projection(section)
        assert [item["value"] for item in options] == ["", "/usr/bin/msedge", "/snap/bin/chromium", "/usr/bin/chromium-browser", "__unavailable_0__", "__custom__"]
        assert options[0]["text"] == "Auto-discover"
        assert options[-1]["text"] == "Custom executable"
        assert options[-2] == {
            "value": "__unavailable_0__",
            "text": "Windows Microsoft Edge — Use native Windows Swarm or install Edge/Chromium inside WSL.",
            "disabled": True,
        }
        section.locator("#browser-instance-name").fill("Lexical browser")
        section.locator("#browser-executable-select").select_option("__custom__")
        section.locator("#browser-executable").fill(f"  {SCRIPT_PATH}  ")
        section.locator("#browser-settings-launch").click()
        playwright.expect(section.locator("#browser-management-list")).to_contain_text("Lexical browser")
        assert api.validated_executables == [SCRIPT_PATH]
        body = api.matching("/api/browser/instances/launch", method="POST")[-1][3]
        assert body["name"] == "Lexical browser" and body["headless"] is True
        assert not errors
    finally:
        context.close()


def test_discovery_404_keeps_manual_launch_safe(static_url, chromium_browser):
    api = DiscoveryAPI()
    api.discovery_status["local"] = 404
    context, page, errors = open_page(chromium_browser, static_url, api)
    try:
        _dialog, section = open_browser_settings(page)
        playwright.expect(section.locator("#browser-discovery-status")).to_contain_text("discovery is unavailable")
        section.locator("#browser-instance-name").fill("Manual browser")
        section.locator("#browser-executable-select").select_option("__custom__")
        dispatch_input(section.locator("#browser-executable"), "/opt/manual-browser-script")
        section.locator("#browser-settings-launch").click()
        playwright.expect(section.locator("#browser-management-list")).to_contain_text("Manual browser")
        assert api.validated_executables == ["/opt/manual-browser-script"]
        assert not errors
    finally:
        context.close()


def test_dirty_custom_field_survives_async_discovery_completion(static_url, chromium_browser):
    api = DiscoveryAPI()
    context, page, errors = open_page(chromium_browser, static_url, api)
    try:
        page.evaluate(
            """() => {
              const auth = SwarmAuth.BrowserAuth, original = auth.fetch.bind(auth);
              auth.fetch = async (path, options) => {
                const response = await original(path, options);
                if (String(path) === '/api/browser/discovery') await new Promise(resolve => { window.releaseLocalDiscovery = resolve; });
                return response;
              };
            }"""
        )
        page.locator("#notifications-button").click()
        page.get_by_role("button", name="Settings", exact=True).click()
        section = page.locator("#browser-settings-section")
        section.locator("summary").first.click()
        page.wait_for_function("() => typeof window.releaseLocalDiscovery === 'function'")
        field = section.locator("#browser-executable")
        dispatch_input(field, SCRIPT_PATH)
        page.evaluate("window.releaseLocalDiscovery()")
        playwright.expect(section.locator("#browser-discovery-status")).to_contain_text("3 installed browser option(s) found")
        playwright.expect(field).to_have_value(SCRIPT_PATH)
        playwright.expect(section.locator("#browser-executable-select")).to_have_value("__custom__")
        assert not api.browser_mutations
        assert not errors
    finally:
        context.close()


def test_remote_discovery_is_owner_scoped_and_late_source_cannot_override(static_url, chromium_browser):
    api = DiscoveryAPI()
    context, page, errors = open_page(chromium_browser, static_url, api)
    try:
        select_owner(page, SOURCE_A)
        page.evaluate(
            """source => {
              const auth = SwarmAuth.BrowserAuth, original = auth.fetch.bind(auth);
              auth.fetch = async (path, options) => {
                const response = await original(path, options);
                if (String(path).includes(`/api/federation/${encodeURIComponent(source)}/proxy/api/browser/discovery`))
                  await new Promise(resolve => { window.releaseSourceADiscovery = resolve; });
                return response;
              };
            }""",
            SOURCE_A,
        )
        dialog, section = open_browser_settings(page)
        page.wait_for_function("() => typeof window.releaseSourceADiscovery === 'function'")
        select_owner(page, SOURCE_B)
        playwright.expect(dialog).to_be_hidden()
        page.locator("#notifications-button").click()
        page.get_by_role("button", name="Settings", exact=True).click()
        playwright.expect(section.locator("#browser-settings-owner")).to_have_text("Owning server: Source B")
        playwright.expect(section.locator("#browser-discovery-status")).to_contain_text("1 installed browser option(s) found")
        assert section.locator('#browser-executable-select option[value="/opt/b-browser"]').count() == 1
        page.evaluate("window.releaseSourceADiscovery()")
        page.wait_for_timeout(100)
        assert section.locator('#browser-executable-select option[value="/opt/a-browser"]').count() == 0

        select_owner(page, OLD_SOURCE)
        page.locator("#notifications-button").click()
        page.get_by_role("button", name="Settings", exact=True).click()
        playwright.expect(section.locator("#browser-settings-owner")).to_have_text("Owning server: Old backend")
        playwright.expect(section.locator("#browser-discovery-status")).to_contain_text("discovery is unavailable")
        playwright.expect(section.locator("#browser-executable-select")).to_be_enabled()
        assert not api.browser_mutations
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_candidate_text_is_literal_bounded_and_responsive(static_url, chromium_browser, width):
    api = DiscoveryAPI()
    attack = '<img src=x onerror="window.discoveryXSS=1">'
    unavailable_attack = '<svg onload="window.discoveryXSS=2">'
    api.discoveries["local"] = {
        "browsers": [{"name": attack, "executable": "/opt/literal-browser", "supported": True}],
        "unavailable": [{"name": unavailable_attack, "executable": "/mnt/c/literal.exe", "reason": '<b onclick="window.discoveryXSS=3">native Windows only</b>'}],
    }
    context, page, errors = open_page(chromium_browser, static_url, api, width=width)
    try:
        dialog, section = open_browser_settings(page)
        text = section.locator("#browser-executable-select").all_text_contents()[0]
        assert attack in text and unavailable_attack in text
        assert "<b onclick=" in section.locator("#browser-discovery-status").inner_text()
        assert section.locator("img, svg, b").count() == 0
        assert page.evaluate("window.discoveryXSS") is None
        assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")
        assert page.evaluate("document.body.scrollWidth <= innerWidth")
        assert not api.browser_mutations
        assert not errors
    finally:
        context.close()
