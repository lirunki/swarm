"""Real-browser acceptance coverage for the multi-instance browser registry UI."""

from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401
from test_settings_layout_browser import launch_chromium

from swarm.browser_registry import BrowserRegistry, BrowserRegistryConflict

playwright = pytest.importorskip("playwright.sync_api")

LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
X = "33333333-3333-4333-8333-333333333333"
PEER = "peer/browser"
LEGACY = "peer/legacy"
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    return _dashboard_static_url


@pytest.fixture(scope="module")
def chromium_browser():
    if LIBRARY_PATH_FILE.exists():
        assert Path(LIBRARY_PATH_FILE.read_text(encoding="utf-8").strip()).is_dir()
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


def managed(identifier, name, *, headless=True, executable="/opt/chromium"):
    return {
        "id": identifier,
        "name": name,
        "kind": "managed",
        "port": 9300 + len(name),
        "running": True,
        "state": "running",
        "can_stop": True,
        "headless": headless,
        "executable": executable,
    }


def external(identifier=X, name="Attached", port=9222):
    return {
        "id": identifier,
        "name": name,
        "kind": "external",
        "port": port,
        "running": True,
        "state": "running",
        "can_stop": False,
        "headless": False,
        "executable": None,
    }


class RegistryAPI:
    """Stateful, owner-scoped fake; it never launches or attaches a real browser."""

    def __init__(self):
        self.registries = {
            "local": {
                "revision": 7,
                "browsers": [
                    managed(A, "Local A"),
                    managed(B, "Local B", headless=False),
                    external(),
                ],
            },
            PEER: {
                "revision": 19,
                # Deliberately reuse IDs: owner plus ID, never ID alone, is the address.
                "browsers": [
                    managed(A, "Peer A"),
                    managed(B, "Peer B"),
                    external(X, '<img src=x onerror="window.registryXSS=1">', 9444),
                ],
            },
        }
        self.requests = []
        self.instance_requests = []
        self.mutations = []
        self.next_ids = iter(
            [
                "44444444-4444-4444-8444-444444444444",
                "55555555-5555-4555-8555-555555555555",
                "66666666-6666-4666-8666-666666666666",
            ]
        )
        self.tabs = {
            (owner, identifier): [
                {
                    "id": f"tab-{identifier[0]}-{owner.replace('/', '-')}",
                    "title": f"{owner} {identifier[0]}",
                    "url": f"https://{identifier[0].lower()}.{owner.replace('/', '-')}.test/",
                }
            ]
            for owner in ("local", PEER)
            for identifier in (A, B, X)
        }

    @staticmethod
    def owner_endpoint(path):
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
    def lifecycle_posts(self):
        return [
            request
            for request in self.requests
            if request[0] == "POST"
            and request[1].rsplit("/", 1)[-1]
            in {"launch", "add", "rename", "stop", "remove"}
        ]

    def projection(self, owner):
        return copy.deepcopy(self.registries[owner])

    def mutate(self, owner, action, body):
        registry = self.registries[owner]
        expected = {
            "launch": {"revision", "request_id", "name", "headless", "executable"},
            "add": {"revision", "request_id", "name", "port"},
            "rename": {"revision", "request_id", "id", "name"},
            "stop": {"revision", "request_id", "id"},
            "remove": {"revision", "request_id", "id"},
        }[action]
        assert set(body) == expected
        assert body["revision"] == registry["revision"]
        assert UUID_PATTERN.fullmatch(body["request_id"])
        browser_id = body.get("id")
        if action in {"launch", "add"}:
            browser_id = next(self.next_ids)
            if action == "launch":
                item = managed(
                    browser_id,
                    body["name"],
                    headless=body["headless"],
                    executable=body["executable"],
                )
            else:
                item = external(browser_id, body["name"], body["port"])
            registry["browsers"].append(item)
            self.tabs[(owner, browser_id)] = []
        else:
            assert UUID_PATTERN.fullmatch(browser_id)
            item = next(entry for entry in registry["browsers"] if entry["id"] == browser_id)
            if action == "rename":
                item["name"] = body["name"]
            elif action == "stop":
                assert item["kind"] == "managed" and item["running"] and item["can_stop"]
                item.update(running=False, state="stopped")
            else:
                assert item["kind"] == "external" or (
                    not item["running"] and item["state"] == "stopped"
                )
                registry["browsers"].remove(item)
        registry["revision"] += 1
        response = self.projection(owner) | {"operation": {"browser_id": browser_id}}
        self.mutations.append((owner, action, copy.deepcopy(body), copy.deepcopy(response)))
        return response

    def route(self, route):
        request = route.request
        method = request.method
        path = urlsplit(request.url).path
        owner, endpoint = self.owner_endpoint(path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((method, endpoint, owner, copy.deepcopy(body), path))
        status = 200

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {
                        "id": PEER,
                        "name": "Peer Browser",
                        "online": True,
                        "state_ready": True,
                        "capabilities": {"browser": True},
                    },
                    {
                        "id": LEGACY,
                        "name": "Legacy Viewer",
                        "online": True,
                        "state_ready": True,
                        "capabilities": {},
                    },
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
        elif endpoint == "/api/browser/instances":
            if owner == LEGACY:
                status, value = 404, {"detail": "Browser registry unavailable"}
            else:
                value = self.projection(owner)
        elif endpoint == "/api/browser/discovery":
            value = {
                "browsers": [
                    {
                        "name": "Chromium",
                        "executable": "/opt/chromium",
                        "supported": True,
                    }
                ],
                "unavailable": [],
            }
        elif re.fullmatch(r"/api/browser/instances/(launch|add|rename|stop|remove)", endpoint):
            action = endpoint.rsplit("/", 1)[-1]
            value = self.mutate(owner, action, body)
        elif match := re.fullmatch(
            r"/api/browser/instances/([0-9a-f-]{36})/(tabs|select|screenshot|click|drag|text|key|copy|new|close|back|forward|reload)",
            endpoint,
        ):
            browser_id, action = match.groups()
            assert any(item["id"] == browser_id for item in self.registries[owner]["browsers"])
            self.instance_requests.append(
                (owner, browser_id, action, copy.deepcopy(body), request.url)
            )
            tabs = self.tabs[(owner, browser_id)]
            if action == "tabs":
                value = {"tabs": copy.deepcopy(tabs), "selected": tabs[0]["id"] if tabs else None}
            elif action == "screenshot":
                route.fulfill(status=200, content_type="image/png", body=PNG)
                return
            elif action == "copy":
                value = {"text": "copied"}
            else:
                value = {"ok": True}
        elif endpoint.startswith("/api/browser/"):
            raise AssertionError(f"obsolete or unscoped browser route used: {endpoint}")
        else:
            value = dashboard_state()
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


def open_page(browser, static_url, api, *, width=1280):
    context = browser.new_context(
        viewport={"width": width, "height": 720},
        has_touch=width == 390,
        is_mobile=width == 390,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        """
        window.WebSocket = class {
          static OPEN = 1;
          constructor(url) { this.url = String(url); this.readyState = 1;
            setTimeout(() => this.onopen?.(), 0); }
          close() { this.readyState = 3; this.onclose?.({code: 1000}); }
          send() {}
        };
        """
    )
    page.route("**/api/**", no_auth_mock(api.route))
    page.goto(static_url)
    playwright.expect(page.locator("#agent-count")).to_have_text("0")
    return context, page, errors


def open_settings(page):
    page.locator("#notifications-button").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.locator("#settings-dialog")
    playwright.expect(dialog).to_be_visible()
    section = dialog.locator("#browser-settings-section")
    if section.get_attribute("open") is None:
        section.locator("summary").first.click()
    playwright.expect(section.locator("#browser-settings-launch")).to_be_enabled()
    return dialog, section


def open_browser(page):
    if page.locator("#dashboard").is_hidden():
        page.locator("#dashboard-tab").click()
    page.locator("#add-menu-button").click()
    page.locator("#open-browser-button").click()
    playwright.expect(page.locator("#browser-panel")).to_be_visible()


def select_owner(page, owner):
    page.locator("#swarm-selector").select_option(owner)
    page.wait_for_function(
        "owner => activeSwarm === owner && state.home === '/fixture'", arg=owner
    )


def row(section, text):
    return section.locator("#browser-management-list .browser-management-row").filter(
        has_text=text
    )


@pytest.mark.parametrize("width", [390, 1280])
def test_settings_manages_multiple_named_instances_without_legacy_enable_or_pane_lifecycle(
    static_url, chromium_browser, width
):
    api = RegistryAPI()
    context, page, errors = open_page(chromium_browser, static_url, api, width=width)
    try:
        dialog, section = open_settings(page)
        browser_gets = [
            endpoint
            for method, endpoint, owner, _body, _path in api.requests
            if method == "GET" and owner == "local" and endpoint.startswith("/api/browser/")
        ]
        assert browser_gets[:2] == [
            "/api/browser/instances",
            "/api/browser/discovery",
        ]
        assert page.locator("#browser-enabled").count() == 0
        assert page.locator("#browser-launch").count() == 0
        assert page.locator("#browser-panel").get_by_role(
            "button", name=re.compile("Launch|Stop", re.I)
        ).count() == 0

        section.locator("#browser-instance-name").fill("Profile One")
        section.locator("#browser-executable-select").select_option("__custom__")
        section.locator("#browser-executable").fill(" /opt/one-browser ")
        section.locator("#browser-headless").check()
        section.locator("#browser-settings-launch").click()
        playwright.expect(row(section, "Profile One")).to_have_count(1)

        section.locator("#browser-instance-name").fill("Profile Two")
        section.locator("#browser-executable-select").select_option("")
        section.locator("#browser-headless").uncheck()
        section.locator("#browser-settings-launch").click()
        playwright.expect(row(section, "Profile Two")).to_have_count(1)
        launches = [request[3] for request in api.lifecycle_posts if request[1].endswith("/launch")]
        assert [(item["name"], item["headless"], item["executable"]) for item in launches] == [
            ("Profile One", True, "/opt/one-browser"),
            ("Profile Two", False, None),
        ]

        add_existing = section.locator("details.browser-add-section").nth(1)
        add_existing.locator("summary").click()
        section.locator("#browser-existing-name").fill("Local DevTools")
        section.locator("#browser-existing-port").fill("9333")
        launches_before = len(launches)
        section.locator("#browser-existing-add").click()
        attached = row(section, "Local DevTools")
        playwright.expect(attached).to_have_count(1)
        assert len([r for r in api.lifecycle_posts if r[1].endswith("/launch")]) == launches_before
        add = [request[3] for request in api.lifecycle_posts if request[1].endswith("/add")][-1]
        assert add["name"] == "Local DevTools" and add["port"] == 9333
        assert attached.get_by_role("button", name="Stop", exact=True).is_disabled()

        page.once("dialog", lambda prompt: prompt.accept("Renamed external"))
        attached.get_by_role("button", name="Rename", exact=True).click()
        playwright.expect(row(section, "Renamed external")).to_have_count(1)
        renamed = row(section, "Renamed external")
        page.once("dialog", lambda prompt: prompt.accept())
        renamed.get_by_role("button", name="Remove", exact=True).click()
        playwright.expect(row(section, "Renamed external")).to_have_count(0)

        local_a = row(section, "Local A")
        assert local_a.get_by_role("button", name="Remove", exact=True).is_disabled()
        page.once("dialog", lambda prompt: prompt.accept())
        local_a.get_by_role("button", name="Stop", exact=True).click()
        playwright.expect(row(section, "Local A")).to_contain_text("stopped")
        local_a = row(section, "Local A")
        assert local_a.get_by_role("button", name="Stop", exact=True).is_disabled()
        page.once("dialog", lambda prompt: prompt.accept())
        local_a.get_by_role("button", name="Remove", exact=True).click()
        playwright.expect(row(section, "Local A")).to_have_count(0)

        assert all(
            response["revision"] == body["revision"] + 1
            and response["operation"]["browser_id"]
            == body.get("id", response["operation"]["browser_id"])
            for _owner, _action, body, response in api.mutations
        )
        assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")
        assert page.evaluate("document.body.scrollWidth <= innerWidth")
        assert not errors
    finally:
        context.close()


def test_viewer_connects_explicitly_routes_by_owner_and_instance_and_settings_stop_disconnects(
    static_url, chromium_browser
):
    api = RegistryAPI()
    context, page, errors = open_page(chromium_browser, static_url, api)
    try:
        open_browser(page)
        select = page.locator("#browser-instance-select")
        playwright.expect(select).to_have_value(A)
        playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
        assert not api.instance_requests

        page.locator("#browser-connect").click()
        playwright.expect(page.locator("#browser-connect")).to_have_text("Disconnect")
        page.wait_for_function("() => document.querySelector('#browser-screen').src.startsWith('blob:')")
        assert ("local", A, "tabs") in [item[:3] for item in api.instance_requests]

        select.select_option(B)
        playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
        playwright.expect(page.locator("#browser-screen")).to_be_hidden()
        before_b = len([item for item in api.instance_requests if item[1] == B])
        page.wait_for_timeout(100)
        assert len([item for item in api.instance_requests if item[1] == B]) == before_b

        page.locator("#browser-connect").click()
        page.wait_for_function("() => document.querySelector('#browser-screen').src.startsWith('blob:')")
        box = page.locator("#browser-screen").bounding_box()
        assert box
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_function(
            "() => document.querySelector('#browser-status').textContent.includes('tab')"
        )
        page.wait_for_timeout(100)
        click = [item for item in api.instance_requests if item[2] == "click"][-1]
        assert click[:3] == ("local", B, "click")
        assert click[3]["target_id"].startswith("tab-2-local")

        stops = len([request for request in api.lifecycle_posts if request[1].endswith("/stop")])
        page.locator("#browser-connect").click()
        playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
        assert len([request for request in api.lifecycle_posts if request[1].endswith("/stop")]) == stops
        page.locator("#browser-connect").click()
        playwright.expect(page.locator("#browser-connect")).to_have_text("Disconnect")

        dialog, section = open_settings(page)
        playwright.expect(row(section, "Local A")).to_contain_text("running")
        page.once("dialog", lambda prompt: prompt.accept())
        row(section, "Local B").get_by_role("button", name="Stop", exact=True).click()
        playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
        playwright.expect(page.locator("#browser-screen")).to_be_hidden()
        playwright.expect(row(section, "Local B")).to_contain_text("stopped")
        playwright.expect(row(section, "Local A")).to_contain_text("running")
        assert next(item for item in api.registries["local"]["browsers"] if item["id"] == A)[
            "running"
        ]

        dialog.locator("[data-close]").last.click()
        select.select_option(A)
        page.locator("#browser-connect").click()
        playwright.expect(page.locator("#browser-connect")).to_have_text("Disconnect")
        stop_count = len([request for request in api.lifecycle_posts if request[1].endswith("/stop")])
        page.locator("#browser-tab-close").click()
        playwright.expect(page.locator("#browser-panel")).to_be_hidden()
        assert len([request for request in api.lifecycle_posts if request[1].endswith("/stop")]) == stop_count

        page.reload()
        playwright.expect(page.locator("#agent-count")).to_have_text("0")
        open_browser(page)
        playwright.expect(select).to_have_value(A)
        playwright.expect(page.locator("#browser-connect")).to_have_text("Connect")
        assert page.locator("#browser-screen").is_hidden()
        assert not errors
    finally:
        context.close()


def test_owner_guards_stale_results_duplicate_ids_and_literal_names(static_url, chromium_browser):
    api = RegistryAPI()
    context, page, errors = open_page(chromium_browser, static_url, api)
    try:
        page.evaluate(
            """() => {
              const auth = SwarmAuth.BrowserAuth, original = auth.fetch.bind(auth);
              auth.fetch = async (path, options) => {
                const response = await original(path, options);
                if (String(path) === '/api/browser/instances')
                  await new Promise(resolve => { window.releaseOldRegistry = resolve; });
                return response;
              };
            }"""
        )
        open_browser(page)
        page.wait_for_function("() => typeof window.releaseOldRegistry === 'function'")
        select_owner(page, PEER)
        page.evaluate("window.releaseOldRegistry()")
        page.wait_for_timeout(100)
        assert page.locator("#browser-instance-select").locator("option").count() == 0

        open_browser(page)
        options = page.locator("#browser-instance-select option")
        playwright.expect(options).to_have_count(3)
        assert "Peer A" in options.nth(0).inner_text()
        assert "Local A" not in page.locator("#browser-instance-select").inner_text()
        assert '<img src=x onerror="window.registryXSS=1">' in options.nth(2).inner_text()
        assert page.locator("#browser-instance-select img").count() == 0
        assert page.evaluate("window.registryXSS") is None

        page.locator("#browser-instance-select").select_option(A)
        page.locator("#browser-connect").click()
        page.wait_for_function("() => document.querySelector('#browser-screen').src.startsWith('blob:')")
        routed = [item for item in api.instance_requests if item[0] == PEER and item[1] == A]
        assert {item[2] for item in routed} >= {"tabs", "select", "screenshot"}
        assert not [item for item in api.instance_requests if item[0] == "local" and item[1] == A]
        assert not errors
    finally:
        context.close()


def test_legacy_registry_404_is_unsupported_without_autolaunch(static_url, chromium_browser):
    api = RegistryAPI()
    context, page, errors = open_page(chromium_browser, static_url, api)
    try:
        select_owner(page, LEGACY)
        open_browser(page)
        playwright.expect(page.locator("#browser-status")).to_have_text(
            "Browser instances unavailable"
        )
        playwright.expect(page.locator("#browser-connect")).to_be_disabled()
        assert api.matching("/api/browser/instances", owner=LEGACY)
        assert not api.lifecycle_posts
        assert not api.instance_requests
        assert not errors
    finally:
        context.close()


class ProfileLauncher:
    def __init__(self):
        self.identities = {}
        self.profiles = []
        self.stops = []

    def launch(self, port, **options):
        identity = {"browser": "Chromium/Fake", "browser_id": str(uuid4())}
        self.identities[port] = identity
        self.profiles.append(options["profile"])
        return {"started": True, **identity}

    def probe_identity(self, port, timeout=1):
        del timeout
        return copy.deepcopy(self.identities.get(port))

    def stop_browser(self, port, browser_id, timeout=5):
        del timeout
        assert self.identities[port]["browser_id"] == browser_id
        self.stops.append((port, browser_id))
        self.identities.pop(port)
        return {"running": False, "stopped": True}


class ProfileControls:
    def __init__(self, _port):
        self.closed = False

    def close(self):
        self.closed = True


def registry_operation(revision, **fields):
    return {"revision": revision, "request_id": str(uuid4()), **fields}


def test_backend_managed_stop_then_remove_retains_profile_and_close_only_detaches(tmp_path):
    launcher = ProfileLauncher()
    registry = BrowserRegistry(
        tmp_path / "home",
        {"enabled": False, "headless": True, "executable": None, "cdp_port": 0},
        launcher=launcher,
        browser_factory=ProfileControls,
    )
    launched = registry.launch(
        registry_operation(0, name="Managed", executable=None, headless=True)
    )
    browser_id = launched["operation"]["browser_id"]
    profile = registry.instances / UUID(browser_id).hex / "remote-browser" / "profile"
    assert profile.is_dir() and launcher.profiles == [profile]

    with pytest.raises(BrowserRegistryConflict, match="Stop"):
        registry.remove(registry_operation(1, id=browser_id))
    stopped = registry.stop(registry_operation(1, id=browser_id))
    assert stopped["revision"] == 2 and len(launcher.stops) == 1
    removed = registry.remove(registry_operation(2, id=browser_id))
    assert removed["revision"] == 3 and removed["browsers"] == []
    assert profile.is_dir(), "removing registry metadata must retain the managed profile"

    second = registry.launch(
        registry_operation(3, name="Detached", executable=None, headless=False)
    )
    second_id = second["operation"]["browser_id"]
    running_before = copy.deepcopy(launcher.identities)
    assert second_id in registry._controls  # launch cached the instance manager
    registry.close()
    assert launcher.identities == running_before
    assert len(launcher.stops) == 1, "closeBrowser/server shutdown detaches; it does not stop"
