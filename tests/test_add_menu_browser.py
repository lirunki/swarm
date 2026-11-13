"""Browser coverage for the global Add menu and explicit Browser workspace tab."""

from __future__ import annotations

import copy
import json
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import fixture_state, no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

REMOTE = "peer-blue"
LEGACY = "peer-legacy"
GROUP_ID = "10000000-0000-4000-8000-000000000001"


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Expose the dashboard static server fixture in this module."""
    return _dashboard_static_url


def dashboard_state():
    value = fixture_state()
    value.update(
        agent_groups={
            "revision": 4,
            "groups": [{"id": GROUP_ID, "name": "Blue folder"}],
            "assignments": {"a0": GROUP_ID},
        },
        agent_groups_error=None,
        grouping_settings={
            "revision": 2,
            "server_managed": True,
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


class MenuBrowserAPI:
    """Synthetic multi-owner API that records every mutation."""

    def __init__(self):
        self.requests = []
        self.states = {owner: dashboard_state() for owner in ("local", REMOTE, LEGACY)}
        self.statuses = {
            owner: {"enabled": True, "running": False, "state": "stopped", "error": None}
            for owner in self.states
        }
        self.settings = {
            owner: {"revision": 3, "enabled": True, "headless": True, "executable": None}
            for owner in self.states
        }

    @staticmethod
    def owner_and_endpoint(path):
        if path.startswith("/api/federation/") and "/proxy/" in path:
            encoded, endpoint = path.split("/api/federation/", 1)[1].split("/proxy", 1)
            return unquote(encoded), endpoint
        return "local", path

    def count(self, endpoint, *, method=None, owner=None):
        return sum(
            recorded_endpoint == endpoint
            and (method is None or recorded_method == method)
            and (owner is None or recorded_owner == owner)
            for recorded_method, recorded_endpoint, recorded_owner, _body in self.requests
        )

    @property
    def mutations(self):
        return [request for request in self.requests if request[0] != "GET"]

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        owner, endpoint = self.owner_and_endpoint(path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((method, endpoint, owner, copy.deepcopy(body)))

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {"id": REMOTE, "name": "Blue owner", "online": True, "capabilities": {}},
                    {"id": LEGACY, "name": "Legacy owner", "online": True, "capabilities": {}},
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
            value = copy.deepcopy(self.states[owner])
        elif endpoint == "/api/browser/status":
            value = copy.deepcopy(self.statuses[owner])
        elif endpoint == "/api/browser/settings":
            value = copy.deepcopy(self.settings[owner])
        elif endpoint == "/api/notifications":
            value = {"revision": 1, "count": 0, "notifications": []}
        elif endpoint == "/api/fleet/restarts":
            value = {"jobs": []}
        elif endpoint in ("/api/pi/sessions", "/api/grok/sessions"):
            value = {"sessions": [], "errors": {}, "roots": []}
        elif endpoint == "/api/config/models":
            value = {"default": "fixture/default", "models": [], "error": None}
        elif method != "GET":
            route.fulfill(
                status=418,
                content_type="application/json",
                body=json.dumps({"detail": f"unexpected mutation {method} {endpoint}"}),
            )
            return
        else:
            value = {"sessions": [], "errors": {}, "roots": []}
        route.fulfill(content_type="application/json", body=json.dumps(value))


@contextmanager
def open_page(static_url, api, *, width=1280, before_load=None):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(
                exc
            ) or "error while loading shared libraries" in str(exc):
                pytest.skip(
                    f"Optional Chromium runtime unavailable: {Path(runner.chromium.executable_path)}"
                )
            raise
        context = browser.new_context(
            viewport={"width": width, "height": 950},
            has_touch=width == 390,
            is_mobile=width == 390,
        )
        page = context.new_page()
        errors, failed_assets = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "response",
            lambda response: (
                failed_assets.append((response.status, response.url))
                if "/static/" in response.url and response.status >= 400
                else None
            ),
        )
        page.add_init_script(
            """
            window.WebSocket = class {
              static OPEN = 1;
              constructor(url) { this.url = String(url); this.readyState = 1;
                setTimeout(() => this.onopen?.(), 0); }
              close() { this.readyState = 3; }
              send() {}
            };
            """
        )
        if before_load:
            page.add_init_script(before_load)
        page.route("**/api/**", no_auth_mock(api.route))
        page.goto(static_url)
        playwright.expect(page.locator("#agent-count")).to_have_text("18")
        playwright.expect(page.locator("#agent-group-folders > .agent-group-folder")).to_have_count(
            1
        )
        try:
            yield page, errors, failed_assets
        finally:
            context.close()
            browser.close()


def open_add_menu(page):
    button = page.locator("#add-menu-button")
    button.click()
    playwright.expect(page.locator("#add-menu-options")).to_be_visible()
    playwright.expect(button).to_have_attribute("aria-expanded", "true")


def select_swarm(page, owner):
    page.locator("#swarm-selector").select_option(owner)
    page.wait_for_function("owner => activeSwarm === owner && state.home === '/fixture'", arg=owner)


@pytest.mark.parametrize("width", [390, 1280])
def test_add_menu_layout_modals_keyboard_and_dismissal(static_url, width):
    api = MenuBrowserAPI()
    with open_page(static_url, api, width=width) as (page, errors, failed_assets):
        assert page.locator(".mission-toolbar #adoption-menu").count() == 1
        assert page.locator("#create-dialog #adoption-menu").count() == 0
        assert page.locator("#add-menu-options #open-browser-button").count() == 1

        open_add_menu(page)
        add_panel = page.locator("#add-menu-options")
        assert add_panel.locator('[role="menuitem"]').all_text_contents() == [
            "New agent",
            "Adopt session",
            "Pi session",
            "Grok session",
            "Open browser",
        ]
        assert add_panel.get_by_role("menuitem").all_text_contents() == [
            "New agent",
            "Adopt session",
            "Open browser",
        ]
        assert page.evaluate("document.body.scrollWidth <= innerWidth")

        # Native DOM querySelectorAll includes nested menu items. Keyboard navigation
        # must filter the hidden adoption descendants until that submenu is expanded.
        page.locator("#add-menu-button").focus()
        page.keyboard.press("ArrowDown")
        playwright.expect(page.locator("#create-button")).to_be_focused()
        page.keyboard.press("ArrowDown")
        playwright.expect(page.locator("#adoption-menu-button")).to_be_focused()
        assert page.locator("#adoption-options").is_hidden()
        page.locator("#add-menu-button").focus()
        page.locator("#adoption-menu-button").focus()
        page.keyboard.press("ArrowDown")
        playwright.expect(page.locator("#adopt-button")).to_be_focused()
        page.keyboard.press("ArrowDown")
        playwright.expect(page.locator("#import-grok-button")).to_be_focused()
        page.keyboard.press("Escape")
        playwright.expect(page.locator("#adoption-menu-button")).to_be_focused()
        page.locator('#add-menu-button').focus()
        page.keyboard.press("End")
        playwright.expect(page.locator("#open-browser-button")).to_be_focused()
        assert page.locator("#adoption-options").is_hidden()

        page.keyboard.press("Escape")
        playwright.expect(add_panel).to_be_hidden()
        playwright.expect(page.locator("#add-menu-button")).to_be_focused()
        open_add_menu(page)
        page.locator("#agents").dispatch_event("pointerdown")
        playwright.expect(add_panel).to_be_hidden()

        # New agent is the only create transition and closes both menu levels.
        open_add_menu(page)
        page.locator("#create-button").click()
        playwright.expect(page.locator("#create-dialog")).to_be_visible()
        playwright.expect(add_panel).to_be_hidden()
        playwright.expect(page.locator("#adoption-options")).to_be_hidden()
        assert page.locator("dialog[open]").evaluate_all("nodes => nodes.map(node => node.id)") == [
            "create-dialog"
        ]
        page.locator("#create-dialog [data-close]").first.click()

        # Adoption choices bypass required blank create fields and never submit it.
        page.evaluate(
            """() => {
              window.createSubmits = 0;
              document.querySelector('#create-form').addEventListener(
                'submit', () => window.createSubmits += 1
              );
            }"""
        )
        for choice, target in (
            ("#adopt-button", "#adopt-dialog"),
            ("#import-grok-button", "#import-grok-dialog"),
        ):
            open_add_menu(page)
            page.locator("#adoption-menu-button").click()
            playwright.expect(page.locator("#adoption-options")).to_be_visible()
            page.locator(choice).click()
            playwright.expect(page.locator(target)).to_be_visible()
            playwright.expect(add_panel).to_be_hidden()
            assert page.evaluate("window.createSubmits") == 0
            assert page.locator("#create-dialog").is_hidden()
            page.locator(f"{target} [data-close]").first.click()

        assert not api.mutations
        assert not failed_assets
        assert not errors


@pytest.mark.parametrize("width", [390, 1280])
def test_browser_open_group_terminal_close_and_reload_lifecycle(static_url, width):
    api = MenuBrowserAPI()
    with open_page(static_url, api, width=width) as (page, errors, failed_assets):
        browser_group = page.locator("#browser-tab-group")
        assert browser_group.count() == 1
        assert page.locator("#browser-tab-group > #browser-tab").count() == 1
        playwright.expect(browser_group).to_be_hidden()
        playwright.expect(page.locator("#browser-panel")).to_be_hidden()
        assert api.count("/api/browser/status") == 0

        open_add_menu(page)
        page.locator("#open-browser-button").click()
        playwright.expect(page.locator("#browser-panel")).to_be_visible()
        playwright.expect(browser_group).to_be_visible()
        playwright.expect(page.locator("#browser-status")).to_have_text("Browser stopped")
        assert page.evaluate("activeTab") == "browser"
        assert page.evaluate("browserOpen") is True
        assert api.count("/api/browser/launch", method="POST") == 0
        assert page.locator("#browser-screen").is_hidden()

        page.locator("#dashboard-tab").click()
        playwright.expect(browser_group).to_be_visible()
        assert (
            page.locator("#tabs").evaluate("node => node.lastElementChild.id")
            == "browser-tab-group"
        )

        # Group membership comes only from the server projection. The Browser tab is
        # global: group dashboards and retained group terminal tabs hide, not close, it.
        page.get_by_role("button", name=re.compile(r"Open group Blue folder:")).click()
        playwright.expect(page.locator("#agent-group-name")).to_have_text("Blue folder")
        playwright.expect(browser_group).to_be_hidden()
        assert page.evaluate("browserOpen") is True
        page.locator('.agent-card[data-agent-id="a0"] .card-terminal').click()
        playwright.expect(page.locator("#terminals")).to_be_visible()
        playwright.expect(browser_group).to_be_hidden()
        assert page.evaluate("browserOpen") is True
        page.locator("#dashboard-tab").click()
        playwright.expect(browser_group).to_be_visible()
        assert page.evaluate("selectedAgentGroup") is None

        page.locator("#browser-tab").click()
        status_requests = api.count("/api/browser/status")
        page.locator("#browser-tab-close").click()
        playwright.expect(page.locator("#dashboard")).to_be_visible()
        playwright.expect(browser_group).to_be_hidden()
        assert page.evaluate("activeTab") == "dashboard"
        assert page.evaluate("browserOpen") is False
        assert api.count("/api/browser/status") == status_requests
        assert not api.mutations

        # Closing persists false and never auto-reopens after reload.
        page.reload()
        playwright.expect(page.locator("#agent-count")).to_have_text("18")
        playwright.expect(browser_group).to_be_hidden()
        assert page.evaluate("activeTab") == "dashboard"
        assert api.count("/api/browser/status") == status_requests

        # The Add menu is required to reopen. An open Browser view persists as an
        # open, active workspace and still does not launch a stopped browser.
        open_add_menu(page)
        page.locator("#open-browser-button").click()
        playwright.expect(page.locator("#browser-status")).to_have_text("Browser stopped")
        page.reload()
        playwright.expect(page.locator("#browser-panel")).to_be_visible()
        assert page.evaluate("activeTab") == "browser"
        saved = page.evaluate("JSON.parse(localStorage.getItem('swarm.ui-view-state.local'))")
        assert saved["browserOpen"] is True
        assert saved["terminalTabs"]["active"] == "browser"
        assert api.count("/api/browser/launch", method="POST") == 0
        assert not [
            request
            for request in api.mutations
            if request[1].startswith("/api/agents/") or request[1].startswith("/api/browser/stop")
        ]
        assert not failed_assets
        assert not errors


def test_browser_state_is_per_swarm_migrates_legacy_and_source_change_closes_menu(static_url):
    api = MenuBrowserAPI()
    with open_page(static_url, api) as (page, errors, failed_assets):
        open_add_menu(page)
        select_swarm(page, REMOTE)
        playwright.expect(page.locator("#add-menu-options")).to_be_hidden()
        playwright.expect(page.locator("#browser-tab-group")).to_be_hidden()
        assert page.evaluate("browserOpen") is False

        open_add_menu(page)
        page.locator("#open-browser-button").click()
        playwright.expect(page.locator("#browser-owner")).to_have_text("Owning server: Blue owner")
        assert api.count("/api/browser/status", owner=REMOTE) == 1
        assert api.count("/api/browser/launch", method="POST") == 0
        page.locator("#dashboard-tab").click()

        select_swarm(page, "local")
        playwright.expect(page.locator("#browser-tab-group")).to_be_hidden()
        open_add_menu(page)
        page.locator("#open-browser-button").click()
        page.locator("#dashboard-tab").click()
        playwright.expect(page.locator("#browser-tab-group")).to_be_visible()

        select_swarm(page, REMOTE)
        playwright.expect(page.locator("#browser-tab-group")).to_be_visible()
        page.locator("#browser-tab-close").click()
        select_swarm(page, "local")
        playwright.expect(page.locator("#browser-tab-group")).to_be_visible()
        local_saved = page.evaluate(
            "JSON.parse(localStorage.getItem('swarm.ui-view-state.local')).browserOpen"
        )
        remote_saved = page.evaluate(
            f"JSON.parse(localStorage.getItem('swarm.ui-view-state.{REMOTE}')).browserOpen"
        )
        assert local_saved is True and remote_saved is False

        # Old terminal-tab state with active Browser migrates to browserOpen=true on
        # the owning Swarm, including across a reload that restores that owner.
        page.evaluate(
            """owner => {
              localStorage.setItem('swarm.active', owner);
              localStorage.removeItem(`swarm.ui-view-state.${owner}`);
              localStorage.setItem(`swarm.tabs.${owner}`, JSON.stringify({ids: [], active: 'browser'}));
            }""",
            LEGACY,
        )
        page.reload()
        page.wait_for_function("owner => activeSwarm === owner && uiViewStateRestored", arg=LEGACY)
        playwright.expect(page.locator("#browser-panel")).to_be_visible()
        assert page.evaluate("browserOpen") is True
        assert api.count("/api/browser/status", owner=LEGACY) == 1
        migrated = page.evaluate(
            f"JSON.parse(localStorage.getItem('swarm.ui-view-state.{LEGACY}'))"
        )
        assert migrated["browserOpen"] is True
        assert migrated["terminalTabs"]["active"] == "browser"

        # Both Browser and settings widgets follow the same owner. Merely opening
        # either widget must never launch its configured-but-stopped browser.
        page.locator("#notifications-button").click()
        page.get_by_role("button", name="Settings", exact=True).click()
        section = page.locator("#browser-settings-section")
        section.locator("summary").click()
        playwright.expect(section.locator("#browser-settings-owner")).to_have_text(
            "Owning server: Legacy owner"
        )
        playwright.expect(section.locator("#browser-enabled")).to_be_enabled()
        assert api.count("/api/browser/settings", owner=LEGACY) == 1
        assert api.count("/api/browser/launch", method="POST") == 0
        assert not api.mutations
        assert not failed_assets
        assert not errors
