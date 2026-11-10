"""Chromium integration coverage for the per-Swarm active-agent limit control."""

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")
REQUEST_ID = "10000000-0000-4000-8000-000000000001"


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Expose the dashboard's isolated static server fixture."""
    return _dashboard_static_url


def projection(*, revision=4, maximum=20, active=20):
    return {
        "revision": revision,
        "max_active_managed": maximum,
        "active_managed": active,
        "scope": "orchestration",
    }


def agents():
    return [
        {
            "id": f"agent-{index:02d}",
            "name": f"Agent {index}",
            "goal": "Browser limit fixture",
            "status": "running" if index % 2 else "idle",
            "alive": index < 3,
            "managed": True,
            "backend": "pi",
            "auto_resume": False,
            "activity": "Fixture activity",
            "folder": "/fixture",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        for index in range(16)
    ]


def state(limit=None, **patch):
    value = {
        "agents": agents(),
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }
    if limit is not None:
        value["agent_limits"] = deepcopy(limit)
        value["agent_limits_error"] = None
    value.update(patch)
    return value


class LimitAPI:
    def __init__(self):
        self.states = {
            "local": state(projection()),
            "peer/a": state(projection(maximum=37, active=7), home="/remote-a"),
            "offline": state(
                projection(maximum=51, active=9),
                home="/offline",
                federation_offline=True,
            ),
            "legacy": state(
                None,
                home="/legacy",
                agent_limits_error="Fixture-safe legacy server has no limit projection.",
            ),
        }
        self.nodes = {
            "enabled": True,
            "relay_online": True,
            "nodes": [
                {
                    "id": "peer/a",
                    "name": '<img src=x onerror="window.unsafe=true"> Remote A',
                    "online": True,
                    "capabilities": {},
                },
                {
                    "id": "offline",
                    "name": "Offline owner",
                    "online": False,
                    "capabilities": {},
                },
                {
                    "id": "legacy",
                    "name": "Legacy owner",
                    "online": True,
                    "capabilities": {},
                },
            ],
        }
        self.requests = []

    @staticmethod
    def source(path):
        if "/api/federation/" not in path:
            return "local"
        encoded = path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0]
        return unquote(encoded)

    def limit(self, source):
        return self.states[source].get("agent_limits")

    def set_limit(self, source, value):
        self.states[source]["agent_limits"] = deepcopy(value)
        self.states[source]["agent_limits_error"] = None

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body))
        status = 200

        if path == "/api/federation/nodes":
            value = self.nodes
        elif path == "/api/federation/settings":
            value = {
                "enabled": True,
                "connected": True,
                "status": "connected",
                "has_api_key": True,
                "relay_url": "wss://relay.invalid/v1/connect",
                "relay_node_id": "browser",
                "relay_name": "Browser",
                "last_error": None,
            }
        elif path == "/api/notifications" or path.endswith("/proxy/api/notifications"):
            value = {"revision": 1, "count": 0, "notifications": []}
        elif path == "/api/state":
            value = self.states["local"]
        elif path.endswith("/proxy/api/state"):
            value = self.states[self.source(path)]
        elif path == "/api/agent-limits" or path.endswith("/proxy/api/agent-limits"):
            source = self.source(path)
            current = self.limit(source)
            if request.method == "POST":
                if current is None:
                    status, value = 404, {"detail": "Agent limits are unsupported"}
                else:
                    value = projection(
                        revision=current["revision"] + 1,
                        maximum=body["max_active_managed"],
                        active=current["active_managed"],
                    )
                    self.set_limit(source, value)
            elif current is None:
                status, value = 404, {"detail": "Agent limits are unsupported"}
            else:
                value = current
        elif path.endswith("/api/fleet/restarts"):
            value = {"jobs": []}
        else:
            value = self.states["local"]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

    def limit_requests(self, method=None):
        values = [item for item in self.requests if item[1].endswith("/api/agent-limits")]
        return [item for item in values if method is None or item[0] == method]


def chromium(runner):
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
            pytest.skip(
                f"Optional Chromium runtime unavailable: {Path(runner.chromium.executable_path)}"
            )
        raise


def install_browser_mocks(page, *, post_mode="native", short_limit_timeout=False):
    page.add_init_script(
        f"""
        window.unsafe = false;
        Object.defineProperty(Crypto.prototype, 'randomUUID', {{
          configurable: true, value: () => {json.dumps(REQUEST_ID)}
        }});
        window.WebSocket = class {{
          static OPEN = 1;
          constructor(url) {{ this.url = url; this.readyState = 1;
            setTimeout(() => this.onopen?.(), 0); }}
          close() {{ this.readyState = 3; }}
          send() {{}}
        }};
        const nativeFetch = window.fetch.bind(window);
        window.limitFetchMock = {{mode: {json.dumps(post_mode)}, calls: [], pending: [],
          release(index = 0, value = null) {{
            const item = this.pending[index];
            if (!item || item.released) return false;
            item.released = true;
            const result = value || {{revision: 5, max_active_managed: 25,
              active_managed: 20, scope: 'orchestration'}};
            item.resolve(new Response(JSON.stringify(result), {{status: 200,
              headers: {{'Content-Type': 'application/json'}}}}));
            return true;
          }}
        }};
        window.fetch = (input, options = {{}}) => {{
          const url = typeof input === 'string' ? input : input.url;
          const method = options.method || 'GET';
          if (method === 'POST' && url.split('?')[0].endsWith('/api/agent-limits') &&
              window.limitFetchMock.mode !== 'native') {{
            window.limitFetchMock.calls.push({{url, method, body: JSON.parse(options.body)}});
            return new Promise(resolve => window.limitFetchMock.pending.push({{resolve, released: false}}));
          }}
          return nativeFetch(input, options);
        }};
        {"const nativeTimer = window.setTimeout.bind(window); window.setTimeout = (callback, delay, ...args) => nativeTimer(callback, delay === 20000 && window.limitFetchMock.calls.length ? 60 : delay, ...args);" if short_limit_timeout else ""}
        """
    )


def setup_page(browser, api, width, **mock_options):
    page = browser.new_page(viewport={"width": width, "height": 1000}, has_touch=width == 390)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    install_browser_mocks(page, **mock_options)
    page.route("**/api/**", no_auth_mock(api.route))
    return page, errors


def open_limits(page):
    settings = page.locator("#settings-dialog")
    if settings.get_attribute("open") is not None:
        settings.get_by_role("button", name="Close", exact=True).click()
    page.locator("#notifications-button").click()
    playwright.expect(page.locator("#notifications-dialog")).to_be_visible()
    page.locator("#settings-button").click()
    playwright.expect(settings).to_be_visible()
    return settings.locator("#agent-limits-max"), settings.locator("#agent-limits-save")


def apply_limit_state(page, value, error=None):
    page.evaluate(
        "([value, error]) => applyState({...state, agent_limits: value, agent_limits_error: error})",
        [value, error],
    )


def assert_no_agent_mutations(api):
    mutations = [
        item
        for item in api.requests
        if item[0] != "GET" and not item[1].endswith("/api/agent-limits")
    ]
    assert mutations == []


@pytest.mark.parametrize("width", [390, 1280])
def test_default_projection_save_contract_scope_and_layout(static_url, width):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, width)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(16)
            limit_input, save = open_limits(page)

            playwright.expect(page.locator("#settings-dialog #agent-limits-owner")).to_have_text(
                "Owning server: Local Swarm"
            )
            playwright.expect(page.locator("#settings-dialog #agent-limits-count")).to_have_text(
                "Active managed agents: 20"
            )
            playwright.expect(limit_input).to_have_value("20")
            assert [limit_input.get_attribute(name) for name in ("min", "max", "step")] == [
                "1",
                "10000",
                "1",
            ]
            section = page.locator("#settings-dialog .agent-limits-settings")
            section.locator("details summary").click()
            section_text = section.inner_text()
            assert "Default 20" in section_text
            assert "lowering never stops agents" in section_text
            assert "Operator Create/Wake remain overrides" in section_text
            assert "does not stop or wake agents" in page.locator(
                "#agent-limits-status"
            ).inner_text()

            limit_input.fill("25")
            playwright.expect(save).to_be_enabled()
            save.click()
            playwright.expect(limit_input).to_have_value("25")
            playwright.expect(save).to_be_disabled()
            playwright.expect(page.locator("#agent-limits-error")).to_have_text("")

            requests = api.limit_requests()
            assert [(method, path) for method, path, _ in requests[-2:]] == [
                ("GET", "/api/agent-limits"),
                ("POST", "/api/agent-limits"),
            ]
            body = requests[-1][2]
            assert body["max_active_managed"] == 25
            assert body["revision"] == 4
            assert set(body) == {"max_active_managed", "revision", "request_id"}
            parsed = uuid.UUID(body["request_id"])
            assert parsed.version == 4 and str(parsed) == REQUEST_ID
            assert page.locator(".agent-card").count() == 16
            assert_no_agent_mutations(api)

            dialog = page.locator("#settings-dialog")
            assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()


def test_stale_http_and_lower_revision_cannot_replace_newer_live_limit(static_url):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280)
            page.goto(static_url)
            limit_input, save = open_limits(page)

            live = projection(revision=5, maximum=25, active=8)
            apply_limit_state(page, live)
            playwright.expect(limit_input).to_have_value("25")

            # Both app-level state application and the module reject a lower
            # projection together with the stale error attached to it.
            stale = projection(revision=4, maximum=20, active=2)
            apply_limit_state(page, stale, "stale offline")
            page.evaluate(
                "([value, error]) => agentLimitsController.setState(value, Error(error))",
                [stale, "stale module error"],
            )
            playwright.expect(limit_input).to_have_value("25")
            playwright.expect(limit_input).to_be_enabled()
            playwright.expect(save).to_be_disabled()
            playwright.expect(page.locator("#agent-limits-error")).to_have_text("")

            # Hold an older refresh, then deliver live state as a WebSocket would.
            # The refresh must discard its whole response because state changed.
            held = []
            page.route("**/api/state", lambda route: held.append(route))
            page.evaluate("void refresh()")
            for _ in range(20):
                if held:
                    break
                page.wait_for_timeout(10)
            assert len(held) == 1

            apply_limit_state(page, projection(revision=5, maximum=25, active=9))
            old_http = state(
                None,
                agent_limits=None,
                agent_limits_error="stale HTTP says limits are unsupported",
            )
            held[0].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(old_http),
            )
            page.wait_for_timeout(50)

            playwright.expect(limit_input).to_have_value("25")
            playwright.expect(limit_input).to_be_enabled()
            playwright.expect(save).to_be_disabled()
            playwright.expect(page.locator("#settings-dialog #agent-limits-count")).to_have_text(
                "Active managed agents: 9"
            )
            playwright.expect(page.locator("#agent-limits-error")).to_have_text("")
            assert not any("/wake" in path for _, path, _ in api.requests)
            assert_no_agent_mutations(api)
            assert not errors
        finally:
            browser.close()


def test_external_revision_matching_dirty_draft_clears_save(static_url):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280)
            page.goto(static_url)
            limit_input, save = open_limits(page)
            limit_input.fill("25")
            playwright.expect(save).to_be_enabled()

            apply_limit_state(page, projection(revision=5, maximum=25, active=8))
            playwright.expect(limit_input).to_have_value("25")
            playwright.expect(save).to_be_disabled()
            playwright.expect(page.locator("#agent-limits-error")).to_have_text("")
            assert api.limit_requests("POST") == []
            assert not any("/wake" in path for _, path, _ in api.requests)
            assert_no_agent_mutations(api)
            assert not errors
        finally:
            browser.close()


def test_poll_preserves_dirty_draft_and_concurrent_revision_requires_rebase(static_url):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280)
            page.goto(static_url)
            limit_input, save = open_limits(page)
            limit_input.fill("30")

            apply_limit_state(page, projection(active=24))
            playwright.expect(limit_input).to_have_value("30")
            playwright.expect(page.locator("#settings-dialog #agent-limits-count")).to_have_text(
                "Active managed agents: 24"
            )

            changed = projection(revision=5, maximum=22, active=24)
            api.set_limit("local", changed)
            apply_limit_state(page, changed)
            playwright.expect(limit_input).to_have_value("30")
            playwright.expect(page.locator("#agent-limits-error")).to_contain_text(
                "draft was preserved"
            )

            # A second concurrent update appears only in the save's mandatory fresh GET.
            concurrent = projection(revision=6, maximum=23, active=25)
            api.set_limit("local", concurrent)
            save.click()
            playwright.expect(page.locator("#agent-limits-error")).to_contain_text(
                "changed. Your draft was preserved"
            )
            playwright.expect(limit_input).to_have_value("30")
            assert len(api.limit_requests("POST")) == 0

            # The UI is now rebased to revision 6; an explicit second save may mutate it.
            save.click()
            playwright.expect(limit_input).to_have_value("30")
            playwright.expect(save).to_be_disabled()
            posts = api.limit_requests("POST")
            assert len(posts) == 1 and posts[0][2]["revision"] == 6
            assert posts[0][2]["max_active_managed"] == 30
            assert_no_agent_mutations(api)
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("bad_value", [0, 10001, 2.5, True])
def test_invalid_or_untrusted_projection_disables_controls(static_url, bad_value):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280)
            page.goto(static_url)
            limit_input, save = open_limits(page)
            malformed = projection()
            malformed["max_active_managed"] = bad_value
            apply_limit_state(page, malformed)
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(save).to_be_disabled()
            playwright.expect(page.locator("#agent-limits-error")).to_contain_text("Malformed")
            assert api.limit_requests("POST") == []
            assert not errors
        finally:
            browser.close()


def test_invalid_drafts_and_boolean_count_never_enable_save(static_url):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280)
            page.goto(static_url)
            limit_input, save = open_limits(page)
            for value in ("0", "10001", "1.5"):
                limit_input.fill(value)
                playwright.expect(save).to_be_disabled()
            malformed = projection()
            malformed["active_managed"] = True
            apply_limit_state(page, malformed)
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(save).to_be_disabled()
            assert api.limit_requests("POST") == []
            assert not errors
        finally:
            browser.close()


def test_lowering_cancel_then_confirm_never_stops_or_wakes_agents(static_url):
    api = LimitAPI()
    current = projection(active=12)
    api.set_limit("local", current)
    api.states["local"]["agent_limits"] = current
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280)
            page.goto(static_url)
            limit_input, save = open_limits(page)
            limit_input.fill("10")

            cancelled = []
            page.once("dialog", lambda dialog: (cancelled.append(dialog.message), dialog.dismiss()))
            save.click()
            playwright.expect(save).to_be_enabled()
            assert len(api.limit_requests("POST")) == 0
            assert "10 while 12 are active" in cancelled[0]
            assert "does not stop or wake any agents" in cancelled[0]
            assert "only blocks additional orchestration starts" in cancelled[0]

            accepted = []
            page.once("dialog", lambda dialog: (accepted.append(dialog.message), dialog.accept()))
            save.click()
            playwright.expect(limit_input).to_have_value("10")
            playwright.expect(save).to_be_disabled()
            assert len(api.limit_requests("POST")) == 1
            assert accepted == cancelled
            assert page.locator(".agent-card").count() == 16
            assert not any("/stop" in path or "/wake" in path for _, path, _ in api.requests)
            assert_no_agent_mutations(api)
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_remote_owner_is_independent_and_offline_or_legacy_is_read_only(static_url, width):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, width)
            page.goto(static_url)
            playwright.expect(page.locator("#swarm-selector")).to_be_visible()

            page.locator("#swarm-selector").select_option("peer/a")
            playwright.expect(page.locator(".agent-card")).to_have_count(16)
            limit_input, save = open_limits(page)
            owner = page.locator("#settings-dialog #agent-limits-owner")
            playwright.expect(owner).to_have_text(
                'Owning server: <img src=x onerror="window.unsafe=true"> Remote A'
            )
            assert owner.locator("img").count() == 0
            assert page.evaluate("window.unsafe") is False
            playwright.expect(limit_input).to_have_value("37")
            limit_input.fill("38")
            save.click()
            playwright.expect(save).to_be_disabled()
            remote_post = api.limit_requests("POST")[-1]
            assert remote_post[1] == "/api/federation/peer%2Fa/proxy/api/agent-limits"

            page.locator("#swarm-selector").select_option("local")
            limit_input, _ = open_limits(page)
            playwright.expect(limit_input).to_have_value("20")

            page.locator("#swarm-selector").select_option("offline")
            limit_input, save = open_limits(page)
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(save).to_be_disabled()
            playwright.expect(page.locator("#agent-limits-error")).to_contain_text("offline")
            save.click(force=True)

            page.locator("#swarm-selector").select_option("legacy")
            limit_input, save = open_limits(page)
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(save).to_be_disabled()
            playwright.expect(page.locator("#agent-limits-status")).to_contain_text("unavailable")
            error_text = page.locator("#agent-limits-error").inner_text()
            assert error_text == "Fixture-safe legacy server has no limit projection."
            assert "password" not in error_text.lower() and "token" not in error_text.lower()
            save.click(force=True)

            posts = api.limit_requests("POST")
            assert len(posts) == 1 and "peer%2Fa" in posts[0][1]
            assert not any(path == "/api/agent-limits" for _, path, _ in posts)
            assert page.locator("#settings-dialog").evaluate(
                "node => node.scrollWidth <= node.clientWidth"
            )
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert_no_agent_mutations(api)
            assert not errors
        finally:
            browser.close()


def test_late_post_after_source_switch_cannot_alter_new_owner_controls(static_url):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280, post_mode="defer")
            page.goto(static_url)
            limit_input, save = open_limits(page)
            limit_input.fill("25")
            save.click()
            page.wait_for_function("limitFetchMock.calls.length === 1")

            page.locator("#swarm-selector").select_option("peer/a")
            limit_input, save = open_limits(page)
            playwright.expect(limit_input).to_have_value("37")
            playwright.expect(limit_input).to_be_enabled()
            limit_input.fill("38")
            page.evaluate("limitFetchMock.mode = 'native'")
            save.click()
            playwright.expect(limit_input).to_have_value("38")
            playwright.expect(save).to_be_disabled()

            page.evaluate(
                "limitFetchMock.release(0, {revision: 99, max_active_managed: 999, "
                "active_managed: 20, scope: 'orchestration'})"
            )
            page.wait_for_timeout(50)
            playwright.expect(limit_input).to_have_value("38")
            playwright.expect(page.locator("#settings-dialog #agent-limits-owner")).to_contain_text("Remote A")
            playwright.expect(page.locator("#agent-limits-error")).to_have_text("")
            assert len(api.limit_requests("POST")) == 1
            assert "peer%2Fa" in api.limit_requests("POST")[0][1]
            assert not errors
        finally:
            browser.close()


def test_projection_loss_after_post_keeps_actual_error_and_request_receipt(static_url):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(browser, api, 1280, post_mode="defer")
            page.goto(static_url)
            limit_input, save = open_limits(page)
            limit_input.fill("25")
            save.click()
            page.wait_for_function("limitFetchMock.calls.length === 1")

            apply_limit_state(page, None, "Actual owning Swarm offline.")
            error = page.locator("#agent-limits-error")
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(save).to_be_disabled()
            playwright.expect(error).to_contain_text("Actual owning Swarm offline.")
            playwright.expect(error).to_contain_text("outcome is uncertain")
            playwright.expect(error).to_contain_text(REQUEST_ID)

            # An obsolete malformed acknowledgement must not erase the receipt or
            # turn the torn-down UI back into a writable state.
            page.evaluate(
                "limitFetchMock.release(0, {revision: 4, max_active_managed: 20})"
            )
            page.wait_for_timeout(50)
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(save).to_be_disabled()
            playwright.expect(error).to_contain_text("Actual owning Swarm offline.")
            playwright.expect(error).to_contain_text(REQUEST_ID)
            save.click(force=True)
            assert page.evaluate("limitFetchMock.calls.length") == 1
            assert not any("/wake" in path for _, path, _ in api.requests)
            assert_no_agent_mutations(api)
            assert not errors
        finally:
            browser.close()


def test_post_timeout_is_uncertain_once_until_new_authoritative_state(static_url):
    api = LimitAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page, errors = setup_page(
                browser, api, 1280, post_mode="defer", short_limit_timeout=True
            )
            page.goto(static_url)
            limit_input, save = open_limits(page)
            page.evaluate(
                "agentLimitsController.setTimer = (callback, delay) => "
                "setTimeout(callback, agentLimitsController.mutationSent ? 60 : delay)"
            )
            limit_input.fill("25")
            save.click()
            error = page.locator("#agent-limits-error")
            playwright.expect(error).to_contain_text("outcome is uncertain")
            playwright.expect(error).to_contain_text("Do not retry blindly")
            playwright.expect(error).to_contain_text(REQUEST_ID)
            playwright.expect(error).to_contain_text("timed out after 20000ms")
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(save).to_be_disabled()
            assert page.evaluate("limitFetchMock.calls.length") == 1
            save.click(force=True)
            assert page.evaluate("limitFetchMock.calls.length") == 1

            apply_limit_state(page, projection(active=26))
            playwright.expect(limit_input).to_be_disabled()
            playwright.expect(error).to_contain_text(REQUEST_ID)
            accepted = projection(revision=5, maximum=25, active=26)
            api.set_limit("local", accepted)
            apply_limit_state(page, accepted)
            playwright.expect(limit_input).to_be_enabled()
            playwright.expect(limit_input).to_have_value("25")
            playwright.expect(save).to_be_disabled()
            playwright.expect(error).to_have_text("")
            assert page.evaluate("limitFetchMock.calls.length") == 1
            page.evaluate("limitFetchMock.release(0)")
            assert not errors
        finally:
            browser.close()
