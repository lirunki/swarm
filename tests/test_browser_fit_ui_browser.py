"""Real-browser coverage for fitting the screenshot viewer to its rendered space."""

from __future__ import annotations

import binascii
import copy
import json
import re
import struct
import zlib
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401
from test_settings_layout_browser import launch_chromium

playwright = pytest.importorskip("playwright.sync_api")

LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")
LOCAL_BROWSER = "11111111-1111-4111-8111-111111111111"
PEER_BROWSER = "22222222-2222-4222-8222-222222222222"
PEER = "peer/fit"
TARGETS = {"local": "local-page", PEER: "peer-page"}
INSTANCE_ACTION = re.compile(
    r"/api/browser/instances/([0-9a-f-]{36})/"
    r"(tabs|select|resize|screenshot|click|drag)$"
)


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    return _dashboard_static_url


@pytest.fixture(scope="module")
def real_browser():
    """Use the installed Playwright browser without downloading or installing anything."""
    if LIBRARY_PATH_FILE.exists():
        library_path = Path(LIBRARY_PATH_FILE.read_text(encoding="utf-8").strip())
        assert library_path.is_dir()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        yield browser
        browser.close()


def _chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", binascii.crc32(payload))


def solid_png(width: int, height: int) -> bytes:
    """Generate a small-on-the-wire grayscale PNG with the requested dimensions."""
    assert 1 <= width <= 3840 and 1 <= height <= 2160
    scanline = b"\0" + b"\x86" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(scanline * height, 1))
        + _chunk(b"IEND", b"")
    )


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


def managed(identifier: str, name: str):
    return {
        "id": identifier,
        "name": name,
        "kind": "managed",
        "port": 9222,
        "running": True,
        "state": "running",
        "can_stop": True,
        "headless": True,
        "executable": "/opt/fake-browser",
    }


class FitAPI:
    """Read-only browser fixture apart from captured resize and launch requests."""

    def __init__(self):
        self.requests = []
        self.instances = {
            "local": [managed(LOCAL_BROWSER, "Local fitted browser")],
            PEER: [managed(PEER_BROWSER, "Peer fitted browser")],
        }
        self.sizes = {
            ("local", LOCAL_BROWSER, TARGETS["local"]): (1, 1),
            (PEER, PEER_BROWSER, TARGETS[PEER]): (1, 1),
        }
        self.revisions = {"local": 3, PEER: 7}

    @staticmethod
    def owner_endpoint(path: str):
        if path.startswith("/api/federation/") and "/proxy/" in path:
            encoded, endpoint = path.split("/api/federation/", 1)[1].split("/proxy", 1)
            return unquote(encoded), endpoint
        return "local", path

    def matching(self, action: str, *, owner: str | None = None):
        return [
            item
            for item in self.requests
            if item[2] == action and (owner is None or item[0] == owner)
        ]

    def route(self, route):
        request = route.request
        method = request.method
        parsed_url = urlsplit(request.url)
        path = parsed_url.path
        owner, endpoint = self.owner_endpoint(path)
        body = request.post_data_json if request.post_data else None
        status = 200

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {
                        "id": PEER,
                        "name": "Fit peer",
                        "online": True,
                        "state_ready": True,
                        "capabilities": {"browser": True},
                    }
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
            value = {
                "revision": self.revisions[owner],
                "browsers": copy.deepcopy(self.instances[owner]),
            }
        elif endpoint == "/api/browser/discovery" and method == "GET":
            value = {
                "browsers": [
                    {
                        "name": "Windows Microsoft Edge",
                        "executable": "wsl:msedge",
                        "supported": True,
                    }
                ],
                "unavailable": [],
            }
        elif endpoint == "/api/browser/instances/launch" and method == "POST":
            self.requests.append((owner, None, "launch", copy.deepcopy(body), path))
            made = managed("33333333-3333-4333-8333-333333333333", body["name"])
            made.update(executable=body["executable"], headless=body["headless"])
            self.instances[owner].append(made)
            self.revisions[owner] += 1
            value = {
                "revision": self.revisions[owner],
                "browsers": copy.deepcopy(self.instances[owner]),
                "operation": {"browser_id": made["id"]},
            }
        elif match := INSTANCE_ACTION.fullmatch(endpoint):
            browser_id, action = match.groups()
            expected_browser = LOCAL_BROWSER if owner == "local" else PEER_BROWSER
            assert browser_id == expected_browser
            target = TARGETS[owner]
            self.requests.append((owner, browser_id, action, copy.deepcopy(body), path))
            if action == "tabs":
                value = {
                    "tabs": [{"id": target, "title": owner, "url": f"https://{owner}.test/"}],
                    "selected": target,
                }
            elif action == "resize":
                assert body["target_id"] == target
                self.sizes[(owner, browser_id, target)] = (body["width"], body["height"])
                value = {"ok": True}
            elif action == "screenshot":
                assert parse_qs(parsed_url.query).get("target_id") == [target]
                width, height = self.sizes[(owner, browser_id, target)]
                route.fulfill(
                    status=200,
                    content_type="image/png",
                    body=solid_png(width, height),
                )
                return
            else:
                assert body["target_id"] == target
                value = {"ok": True}
        elif endpoint.startswith("/api/browser/"):
            raise AssertionError(f"Unexpected browser endpoint: {endpoint}")
        else:
            value = dashboard_state()
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


def open_page(browser, static_url, api, *, width: int, height: int = 900):
    context = browser.new_context(
        viewport={"width": width, "height": height},
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


def open_browser(page):
    page.locator("#add-menu-button").click()
    page.locator("#open-browser-button").click()
    playwright.expect(page.locator("#browser-panel")).to_be_visible()
    page.locator("#browser-interval").fill("60")
    page.locator("#browser-interval").dispatch_event("change")


def connect(page):
    page.locator("#browser-connect").click()
    playwright.expect(page.locator("#browser-connect")).to_have_text("Disconnect")
    page.wait_for_function(
        """() => {
          const image = document.querySelector('#browser-screen');
          return !image.hidden && image.complete && image.naturalWidth > 1;
        }"""
    )


def content_size(page):
    return page.locator("#browser-screen").evaluate(
        """image => {
          const parent = image.parentElement, style = getComputedStyle(parent);
          return {
            width: Math.floor(parent.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)),
            height: Math.floor(parent.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)),
            naturalWidth: image.naturalWidth,
            naturalHeight: image.naturalHeight,
            renderedWidth: image.getBoundingClientRect().width,
            renderedHeight: image.getBoundingClientRect().height,
            scrollWidth: parent.scrollWidth,
            clientWidth: parent.clientWidth,
            scrollHeight: parent.scrollHeight,
            clientHeight: parent.clientHeight,
          };
        }"""
    )


def select_owner(page, owner):
    page.locator("#swarm-selector").select_option(owner)
    page.wait_for_function("owner => activeSwarm === owner && state.home === '/fixture'", arg=owner)


@pytest.mark.parametrize("width", [390, 1280])
def test_first_capture_fits_allocated_space_and_resize_is_stable(static_url, real_browser, width):
    api = FitAPI()
    context, page, errors = open_page(real_browser, static_url, api, width=width)
    try:
        open_browser(page)
        connect(page)

        actions = [item[2] for item in api.requests if item[0] == "local"]
        assert actions.index("resize") < actions.index("screenshot")
        first_resize = api.matching("resize", owner="local")[0]
        assert first_resize[1] == LOCAL_BROWSER
        assert first_resize[3]["target_id"] == TARGETS["local"]
        size = content_size(page)
        assert first_resize[3]["width"] == max(320, min(3840, size["width"]))
        assert first_resize[3]["height"] == max(240, min(2160, size["height"]))
        assert (size["naturalWidth"], size["naturalHeight"]) == (
            first_resize[3]["width"],
            first_resize[3]["height"],
        )
        assert size["renderedWidth"] <= size["width"] + 1
        assert size["renderedHeight"] <= size["height"] + 1
        assert size["scrollWidth"] <= size["clientWidth"]
        assert size["scrollHeight"] <= size["clientHeight"]

        before = len(api.matching("resize", owner="local"))
        page.set_viewport_size({"width": width, "height": 820})
        page.wait_for_function(
            "expected => document.querySelector('#browser-screen').naturalHeight === expected",
            arg=max(240, min(2160, content_size(page)["height"])),
        )
        page.wait_for_timeout(500)
        resized = api.matching("resize", owner="local")
        assert len(resized) == before + 1
        requested = resized[-1][3]
        assert (requested["width"], requested["height"]) == (
            content_size(page)["naturalWidth"],
            content_size(page)["naturalHeight"],
        )

        # Repeated unchanged observer/resize notifications must neither loop nor storm.
        page.evaluate(
            """() => {
              for (let index = 0; index < 8; index += 1)
                window.dispatchEvent(new Event('resize'));
            }"""
        )
        page.wait_for_timeout(500)
        assert len(api.matching("resize", owner="local")) == before + 1

        # Explicit fit may deduplicate an unchanged size, but can dispatch at most one action.
        fit_before = len(api.matching("resize", owner="local"))
        page.locator("#browser-fit").click()
        page.wait_for_timeout(300)
        fit_after = len(api.matching("resize", owner="local"))
        assert fit_after in {fit_before, fit_before + 1}
        if fit_after > fit_before:
            assert api.matching("resize", owner="local")[-1][3] == requested
        assert not errors
    finally:
        context.close()


def test_fit_pauses_for_gesture_disconnect_hidden_and_stale_owner(static_url, real_browser):
    api = FitAPI()
    context, page, errors = open_page(real_browser, static_url, api, width=1280)
    try:
        open_browser(page)
        connect(page)
        screen = page.locator("#browser-screen")
        old_dimensions = screen.evaluate("image => [image.naturalWidth, image.naturalHeight]")
        box = screen.bounding_box()
        assert box
        page.mouse.click(box["x"] + 20, box["y"] + 20)
        page.wait_for_function(
            "() => document.querySelector('#browser-status').textContent.includes('tab')"
        )
        page.wait_for_timeout(100)
        assert api.matching("click", owner="local")[-1][3]["target_id"] == TARGETS["local"]
        assert (
            screen.evaluate("image => [image.naturalWidth, image.naturalHeight]") == old_dimensions
        )

        old_count = len(api.matching("resize", owner="local"))
        page.mouse.move(box["x"] + 20, box["y"] + 20)
        page.mouse.down()
        page.set_viewport_size({"width": 1080, "height": 820})
        page.wait_for_timeout(350)
        assert len(api.matching("resize", owner="local")) == old_count
        assert (
            screen.evaluate("image => [image.naturalWidth, image.naturalHeight]") == old_dimensions
        )
        page.mouse.move(box["x"] + 100, box["y"] + 70)
        page.mouse.up()
        page.wait_for_function(
            "old => document.querySelector('#browser-screen').naturalHeight !== old[1]",
            arg=old_dimensions,
        )
        sequence = [
            item[2]
            for item in api.requests
            if item[0] == "local" and item[2] in {"drag", "resize", "screenshot"}
        ]
        drag_index = len(sequence) - 1 - sequence[::-1].index("drag")
        following = sequence[drag_index + 1 :]
        assert "resize" in following
        assert following[following.index("resize") + 1] == "screenshot"

        page.locator("#browser-connect").click()
        disconnected = len(api.matching("resize", owner="local"))
        page.set_viewport_size({"width": 1000, "height": 800})
        page.wait_for_timeout(350)
        assert len(api.matching("resize", owner="local")) == disconnected

        page.locator("#browser-connect").click()
        page.wait_for_function("() => document.querySelector('#browser-screen').naturalWidth > 1")
        page.locator("#browser-tab-close").click()
        hidden = len(api.matching("resize", owner="local"))
        page.set_viewport_size({"width": 960, "height": 780})
        page.wait_for_timeout(350)
        assert len(api.matching("resize", owner="local")) == hidden

        open_browser(page)
        connect(page)
        page.evaluate(
            """() => {
              const auth = SwarmAuth.BrowserAuth, original = auth.fetch.bind(auth);
              window.holdFitResize = true;
              auth.fetch = async (path, options) => {
                const response = await original(path, options);
                if (window.holdFitResize && String(path).endsWith('/resize')) {
                  window.holdFitResize = false;
                  await new Promise(resolve => { window.releaseFitResize = resolve; });
                }
                return response;
              };
            }"""
        )
        page.set_viewport_size({"width": 900, "height": 760})
        page.wait_for_function("() => typeof window.releaseFitResize === 'function'")
        old_shots = len(api.matching("screenshot", owner="local"))
        select_owner(page, PEER)
        page.evaluate("window.releaseFitResize()")
        page.wait_for_timeout(350)
        assert len(api.matching("screenshot", owner="local")) == old_shots
        assert page.locator("#browser-screen").get_attribute("src") is None
        assert not api.matching("resize", owner=PEER)
        assert not errors
    finally:
        context.close()


def test_wsl_discovery_sentinel_launch_payload_is_lexical(static_url, real_browser):
    api = FitAPI()
    context, page, errors = open_page(real_browser, static_url, api, width=1280)
    try:
        page.locator("#notifications-button").click()
        page.get_by_role("button", name="Settings", exact=True).click()
        section = page.locator("#browser-settings-section")
        section.locator("summary").first.click()
        playwright.expect(section.locator("#browser-settings-launch")).to_be_enabled()
        option = section.locator('#browser-executable-select option[value="wsl:msedge"]')
        playwright.expect(option).to_have_count(1)
        playwright.expect(option).to_contain_text("Windows Microsoft Edge")
        section.locator("#browser-executable-select").select_option("wsl:msedge")
        playwright.expect(section.locator("#browser-executable-select")).to_have_value("wsl:msedge")
        section.locator("#browser-instance-name").fill("WSL Edge fixture")
        section.locator("#browser-headless").check()
        section.locator("#browser-settings-launch").click()
        playwright.expect(section.locator("#browser-management-list")).to_contain_text(
            "WSL Edge fixture"
        )
        launch = api.matching("launch")[-1][3]
        assert set(launch) == {
            "revision",
            "request_id",
            "name",
            "headless",
            "executable",
        }
        assert launch["name"] == "WSL Edge fixture"
        assert launch["headless"] is True
        assert launch["executable"] == "wsl:msedge"
        assert not any(key in launch for key in ("user_path", "user_data_dir", "profile"))
        assert not errors
    finally:
        context.close()
