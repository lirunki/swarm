"""Browser coverage for the scoped Settings dialog layout and lifecycle."""

import copy
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    return _dashboard_static_url


def limit(revision=4, maximum=20, active=3):
    return {
        "revision": revision,
        "max_active_managed": maximum,
        "active_managed": active,
        "scope": "orchestration",
    }


def grouping(revision=4, *, managed=False, initialized=True):
    return {
        "revision": revision,
        "server_managed": managed,
        "initialized": initialized,
        "grouped_branches": [],
    }


def state(*, maximum=20, initialized=True, supported=True):
    value = {
        "agents": [],
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
        "agent_groups": {"revision": 4, "groups": [], "assignments": {}},
        "agent_groups_error": None,
    }
    if supported:
        value.update(
            agent_limits=limit(maximum=maximum),
            agent_limits_error=None,
            grouping_settings=grouping(initialized=initialized),
            grouping_settings_error=None,
        )
    else:
        value.update(
            agent_limits=None,
            agent_limits_error="This owner does not support active-agent limits.",
            grouping_settings=None,
            grouping_settings_error="This owner does not support grouping mode.",
        )
    return value


class SettingsAPI:
    """All dashboard dependencies are synthetic and every request is recorded."""

    def __init__(self, *, alpha_initialized=True):
        self.states = {
            "local": state(),
            "remote-alpha": state(maximum=31, initialized=alpha_initialized),
            "remote-legacy": state(supported=False),
        }
        self.requests = []
        self.browser_revisions = {source: 2 for source in self.states}
        self.browser_instances = {source: [] for source in self.states}
        self.relay = {
            "enabled": False,
            "connected": False,
            "status": "disconnected",
            "has_api_key": False,
            "relay_url": "wss://relay.invalid/v1/connect",
            "relay_node_id": "local-controller",
            "relay_name": "Local controller",
            "last_error": None,
        }

    @staticmethod
    def source(path):
        if not path.startswith("/api/federation/") or "/proxy/" not in path:
            return "local"
        return unquote(path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0])

    def endpoint(self, path):
        if "/proxy/" in path:
            return path.split("/proxy", 1)[1]
        return path

    def record(self, method, path, body):
        self.requests.append((method, path, copy.deepcopy(body)))

    def count(self, *, method=None, suffix=None, exact=None):
        return sum(
            1
            for request_method, path, _ in self.requests
            if (method is None or method == request_method)
            and (suffix is None or path.endswith(suffix))
            and (exact is None or path == exact)
        )

    def route(self, route):
        request = route.request
        method = request.method
        path = urlsplit(request.url).path
        body = request.post_data_json if request.post_data else None
        self.record(method, path, body)
        source = self.source(path)
        endpoint = self.endpoint(path)
        status = 200

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {
                        "id": "remote-alpha",
                        "name": "Remote Alpha",
                        "online": True,
                        "capabilities": {},
                    },
                    {
                        "id": "remote-legacy",
                        "name": "Remote Legacy",
                        "online": True,
                        "capabilities": {},
                    },
                ],
            }
        elif path == "/api/federation/settings":
            value = copy.deepcopy(self.relay)
        elif endpoint == "/api/state":
            value = copy.deepcopy(self.states[source])
        elif endpoint == "/api/notifications":
            value = {"revision": 1, "count": 0, "notifications": []}
        elif endpoint == "/api/agent-limits":
            current = self.states[source].get("agent_limits")
            if current is None:
                status, value = 404, {"detail": "Agent limits unsupported"}
            elif method == "POST":
                value = limit(
                    revision=current["revision"] + 1,
                    maximum=body["max_active_managed"],
                    active=current["active_managed"],
                )
                self.states[source]["agent_limits"] = copy.deepcopy(value)
            else:
                value = copy.deepcopy(current)
        elif endpoint == "/api/grouping-settings":
            current = self.states[source].get("grouping_settings")
            if current is None:
                status, value = 404, {"detail": "Grouping mode unsupported"}
            elif method == "POST":
                value = copy.deepcopy(current)
                value.update(
                    revision=current["revision"] + 1,
                    server_managed=body["server_managed"],
                    initialized=True,
                )
                self.states[source]["grouping_settings"] = copy.deepcopy(value)
                self.states[source]["agent_groups"]["revision"] = value["revision"]
            else:
                value = copy.deepcopy(current)
        elif endpoint == "/api/fleet/restarts":
            value = {"jobs": []}
        elif endpoint == "/api/browser/instances" and method == "GET":
            value = {
                "revision": self.browser_revisions[source],
                "browsers": copy.deepcopy(self.browser_instances[source]),
            }
        elif endpoint == "/api/browser/discovery":
            value = {
                "browsers": [
                    {"name": "Chromium", "executable": "/usr/bin/chromium", "supported": True}
                ],
                "unavailable": [],
            }
        elif endpoint == "/api/browser/instances/launch" and method == "POST":
            browser_id = "55555555-5555-4555-8555-555555555555"
            instance = {
                "id": browser_id,
                "name": body["name"],
                "kind": "managed",
                "port": 9222,
                "executable": body["executable"],
                "headless": body["headless"],
                "running": True,
                "state": "running",
                "can_stop": True,
            }
            self.browser_instances[source].append(instance)
            self.browser_revisions[source] += 1
            value = {
                "revision": self.browser_revisions[source],
                "browsers": copy.deepcopy(self.browser_instances[source]),
                "operation": {"browser_id": browser_id},
            }
        elif path in ("/api/federation/connect", "/api/federation/disconnect"):
            raise AssertionError(f"Settings layout test must not mutate relay state: {path}")
        else:
            value = copy.deepcopy(self.states["local"])

        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


def launch_chromium(runner):
    env = dict(os.environ)
    if LIBRARY_PATH_FILE.exists():
        library_path = LIBRARY_PATH_FILE.read_text(encoding="utf-8").strip()
        if library_path:
            env["LD_LIBRARY_PATH"] = library_path
    try:
        return runner.chromium.launch(headless=True, env=env)
    except playwright.Error as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or "error while loading shared libraries" in text:
            pytest.skip(f"Optional Chromium runtime unavailable: {exc}")
        raise


def open_page(browser, api, width=1280):
    page = browser.new_page(
        viewport={"width": width, "height": 900},
        has_touch=width == 390,
        is_mobile=width == 390,
    )
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.add_init_script(
        """
        window.WebSocket = class {
          static OPEN = 1;
          constructor(url) { this.url = url; this.readyState = 1;
            setTimeout(() => this.onopen?.(), 0); }
          close() { this.readyState = 3; }
          send() {}
        };
        """
    )
    page.route("**/api/**", no_auth_mock(api.route))
    return page, errors


def select_remote(page, source="remote-alpha"):
    playwright.expect(page.locator("#swarm-selector")).to_be_visible()
    page.locator("#swarm-selector").select_option(source)
    page.wait_for_function(
        "source => activeSwarm === source && state.home === '/fixture'", arg=source
    )


def open_settings(page):
    page.locator("#notifications-button").click()
    playwright.expect(page.locator("#notifications-dialog")).to_be_visible()
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.locator("#settings-dialog")
    playwright.expect(dialog).to_be_visible()
    playwright.expect(page.locator("#notifications-dialog")).to_be_hidden()
    return dialog


def mutations(api):
    return [(method, path, body) for method, path, body in api.requests if method != "GET"]


@pytest.mark.parametrize("width", [390, 1280])
def test_scoped_settings_layout_is_compact_and_relay_is_lazy(static_url, width):
    api = SettingsAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page, errors = open_page(browser, api, width)
            page.goto(static_url)
            select_remote(page)
            dialog = open_settings(page)

            assert page.locator("#notifications-dialog #agent-limits-max").count() == 0
            assert page.locator("#notifications-dialog #grouping-server-managed").count() == 0
            for selector in ("#agent-limits-max", "#grouping-server-managed"):
                assert page.locator(selector).evaluate("node => node.closest('dialog').id") == (
                    "settings-dialog"
                )

            playwright.expect(dialog.locator("#settings-owner-title")).to_have_text(
                "Selected Swarm"
            )
            playwright.expect(dialog.locator("#settings-active-owner")).to_have_text("Remote Alpha")
            playwright.expect(dialog.locator("#agent-limits-max")).to_have_value("31")
            playwright.expect(dialog.locator("#settings-client-title")).to_have_text("Terminal")
            playwright.expect(
                dialog.locator(".settings-client-section .settings-scope")
            ).to_have_text("This browser")

            relay = dialog.locator("#relay-settings-section")
            assert relay.get_attribute("open") is None
            playwright.expect(relay.locator("summary")).to_contain_text("Relay connection")
            playwright.expect(relay.locator("summary")).to_contain_text("Local controller")
            assert api.count(exact="/api/federation/settings") == 0
            assert not dialog.locator(".settings-help").evaluate("node => node.open")
            playwright.expect(dialog.locator(".settings-help p")).to_be_hidden()

            explanatory = dialog.locator(
                ".settings-owner-section > .hint, .agent-limits-settings > .hint, "
                ".grouping-mode-settings > .hint, .settings-client-section > .hint"
            ).all_text_contents()
            assert explanatory and max(map(len, explanatory)) < 190
            assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not mutations(api)
            assert not errors
        finally:
            browser.close()


def test_browser_registry_settings_are_lazy_and_launch_from_user_draft(static_url):
    api = SettingsAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page, errors = open_page(browser, api)
            page.goto(static_url)
            select_remote(page)
            dialog = open_settings(page)
            section = dialog.locator("#browser-settings-section")
            assert section.get_attribute("open") is None
            assert api.count(suffix="/api/browser/instances") == 0
            section.locator("summary").first.click()
            playwright.expect(section.locator("#browser-settings-status")).to_contain_text(
                "0 browser instance(s)."
            )
            assert api.count(suffix="/api/browser/instances", method="GET") >= 1
            playwright.expect(section.locator("#browser-instance-name")).to_have_value("")
            playwright.expect(section.locator("#browser-headless")).to_be_checked()
            playwright.expect(section.locator("#browser-executable-select")).to_have_value("")
            playwright.expect(section.locator("#browser-existing-name")).to_be_attached()
            playwright.expect(section.locator("#browser-existing-port")).to_be_attached()
            assert not [
                item for item in mutations(api)
                if "/api/browser/instances/" in item[1]
            ]

            section.locator("#browser-instance-name").fill("Remote draft")
            section.locator("#browser-headless").uncheck()
            section.locator("#browser-executable-select").select_option("__custom__")
            section.locator("#browser-executable").fill(" /opt/remote-draft ")
            section.locator("#browser-settings-launch").click()
            playwright.expect(section.locator("#browser-management-list")).to_contain_text(
                "Remote draft — managed — running"
            )
            launch = [
                body for method, path, body in mutations(api)
                if method == "POST" and path.endswith("/api/browser/instances/launch")
            ][-1]
            assert launch["revision"] == 2
            assert launch["name"] == "Remote draft"
            assert launch["headless"] is False
            assert launch["executable"] == "/opt/remote-draft"
            assert not errors
        finally:
            browser.close()


def test_owner_controls_preserve_drafts_and_use_only_remote_capability_paths(static_url):
    api = SettingsAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page, errors = open_page(browser, api)
            page.goto(static_url)
            select_remote(page)
            dialog = open_settings(page)
            page.evaluate(
                """() => {
                  window.relayInvalidEvents = 0;
                  document.querySelector('#relay-settings-form').addEventListener(
                    'invalid', () => window.relayInvalidEvents += 1, true);
                }"""
            )

            limit_input = dialog.locator("#agent-limits-max")
            limit_input.fill("35")
            dialog.locator("#mouse-wheel-multiplier").fill("4")
            dialog.locator("#mouse-wheel-save").click()
            playwright.expect(dialog.locator("#mouse-wheel-status")).to_have_text("Saved · 4×")
            assert page.evaluate("localStorage.getItem('swarm.terminal-wheel-multiplier')") == "4"

            page.once("dialog", lambda prompt: prompt.accept())
            dialog.locator("#grouping-server-managed").check()
            dialog.locator("#grouping-mode-change").click()
            playwright.expect(dialog.locator("#grouping-mode-status")).to_contain_text(
                "Server-managed grouping is on"
            )
            playwright.expect(limit_input).to_have_value("35")

            dialog.locator("#agent-limits-save").click()
            playwright.expect(dialog.locator("#agent-limits-save")).to_be_disabled()
            playwright.expect(limit_input).to_have_value("35")

            posts = [(path, body) for method, path, body in mutations(api) if method == "POST"]
            assert {path for path, _ in posts} == {
                "/api/federation/remote-alpha/proxy/api/grouping-settings",
                "/api/federation/remote-alpha/proxy/api/agent-limits",
            }
            assert not any(
                path in ("/api/grouping-settings", "/api/agent-limits") for path, _ in posts
            )
            assert api.count(exact="/api/federation/settings") == 0
            assert api.count(suffix="/api/federation/connect") == 0
            assert page.evaluate("window.relayInvalidEvents") == 0
            assert not errors
        finally:
            browser.close()


def test_relay_expansion_is_local_masked_and_collapse_stops_polling(static_url):
    api = SettingsAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page, errors = open_page(browser, api)
            page.goto(static_url)
            select_remote(page)
            dialog = open_settings(page)
            relay = dialog.locator("#relay-settings-section")
            relay.locator("summary").click()
            playwright.expect(dialog.locator("#relay-connect")).to_be_enabled()
            assert api.count(exact="/api/federation/settings") == 1
            assert not any("/proxy/api/federation/settings" in path for _, path, _ in api.requests)

            key_input = dialog.locator('[name="relay_api_key"]')
            assert key_input.get_attribute("type") == "password"
            test_key = "browser-test-key-not-a-real-secret-0001"
            key_input.fill(test_key)
            assert test_key not in page.locator("body").inner_text()
            relay.locator("summary").click()
            playwright.expect(key_input).to_have_value("")
            before = api.count(exact="/api/federation/settings")
            page.wait_for_timeout(1700)
            assert api.count(exact="/api/federation/settings") == before

            page.keyboard.press("Escape")
            playwright.expect(dialog).to_be_hidden()
            dialog = open_settings(page)
            assert dialog.locator("#relay-settings-section").get_attribute("open") is None
            assert api.count(exact="/api/federation/settings") == before
            assert api.count(suffix="/api/federation/connect") == 0
            assert not mutations(api)
            assert not errors
        finally:
            browser.close()


def test_first_group_enable_uses_one_modal_restores_settings_and_stops_relay(static_url):
    api = SettingsAPI(alpha_initialized=False)
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page, errors = open_page(browser, api)
            page.goto(static_url)
            select_remote(page)
            settings_dialog = open_settings(page)
            settings_dialog.locator("#relay-settings-section summary").click()
            playwright.expect(settings_dialog.locator("#relay-connect")).to_be_enabled()
            key_input = settings_dialog.locator('[name="relay_api_key"]')
            key_input.fill("temporary-browser-test-credential-0002")
            relay_gets = api.count(exact="/api/federation/settings")

            settings_dialog.locator("#grouping-server-managed").check()
            settings_dialog.locator("#grouping-mode-change").click()
            prime = page.locator("#grouping-prime-dialog")
            playwright.expect(prime).to_be_visible()
            playwright.expect(settings_dialog).to_be_hidden()
            assert page.locator("dialog[open]").count() == 1
            playwright.expect(key_input).to_have_value("")
            page.wait_for_timeout(1700)
            assert api.count(exact="/api/federation/settings") == relay_gets

            prime.locator("#grouping-prime-cancel").click()
            playwright.expect(prime).to_be_hidden()
            playwright.expect(settings_dialog).to_be_visible()
            assert page.locator("dialog[open]").count() == 1
            assert not prime.evaluate("node => node.contains(document.activeElement)")
            playwright.expect(settings_dialog.locator("#grouping-server-managed")).to_be_checked()
            playwright.expect(settings_dialog.locator("#grouping-mode-status")).to_contain_text(
                "Browser-local grouping is on"
            )
            assert not mutations(api)

            page.keyboard.press("Control+Enter")
            assert api.count(suffix="/api/federation/connect") == 0
            assert page.locator("#settings-dialog form form").count() == 0
            assert page.locator("#settings-dialog form").count() == 1
            page.keyboard.press("Escape")
            playwright.expect(settings_dialog).to_be_hidden()
            assert not errors
        finally:
            browser.close()


def test_reopen_after_source_change_updates_owner_and_disables_unsupported_controls(
    static_url,
):
    api = SettingsAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page, errors = open_page(browser, api)
            page.goto(static_url)
            select_remote(page)
            dialog = open_settings(page)
            playwright.expect(dialog.locator("#settings-active-owner")).to_have_text("Remote Alpha")
            page.keyboard.press("Escape")

            select_remote(page, "remote-legacy")
            dialog = open_settings(page)
            playwright.expect(dialog.locator("#settings-active-owner")).to_have_text(
                "Remote Legacy"
            )
            playwright.expect(dialog.locator("#agent-limits-max")).to_be_disabled()
            playwright.expect(dialog.locator("#grouping-server-managed")).to_be_disabled()
            playwright.expect(dialog.locator("#agent-limits-error")).to_contain_text(
                "does not support"
            )
            playwright.expect(dialog.locator("#grouping-mode-status")).to_contain_text(
                "unavailable"
            )

            dialog.locator("#relay-settings-section summary").click()
            playwright.expect(dialog.locator("#relay-connect")).to_be_enabled()
            assert api.count(exact="/api/federation/settings") == 1
            assert not any("/proxy/api/federation/settings" in path for _, path, _ in api.requests)
            assert not mutations(api)
            assert not errors
        finally:
            browser.close()
