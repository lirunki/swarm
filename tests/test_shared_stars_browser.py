"""Shared starred tabs use the owning backend, not browser-local storage."""

import copy
import json
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from test_agent_groups_browser import (
    AGENT_IDS,
    GROUP_A,
    GROUP_B,
    FederationGroupServer,
    browser_state,
    card,
)
from test_dashboard_browser import no_auth_mock, static_url  # noqa: F401
from test_tab_preferences_browser import StatefulStarServer, terminal_tab

playwright = pytest.importorskip("playwright.sync_api")

AGENT_0 = f"agent:{AGENT_IDS[0]}"
AGENT_1 = f"agent:{AGENT_IDS[1]}"
AGENT_3 = f"agent:{AGENT_IDS[3]}"
GROUP_A_TOKEN = f"agent-group:{GROUP_A}"
PREFERENCES_KEY = "swarm.tab-preferences.local.%2Ffixture"

WEBSOCKET_STUB = """window.WebSocket = class {
    static OPEN = 1;
    constructor(url) {
        this.url = url; this.readyState = 1;
        setTimeout(() => this.onopen?.(), 0);
    }
    close() { this.readyState = 3; }
    send() {}
};"""


def grouped_state():
    projection = {
        "revision": 1,
        "groups": [
            {"id": GROUP_A, "name": "Group A"},
            {"id": GROUP_B, "name": "Group B"},
        ],
        "assignments": {AGENT_IDS[i]: GROUP_A for i in (0, 1, 2)}
        | {AGENT_IDS[3]: GROUP_B},
    }
    return browser_state(projection)


@contextmanager
def isolated_pages(width, server, count=2, init_scripts=None):
    """Open truly isolated browser contexts against one shared fake backend."""
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc) or "error while loading shared libraries" in str(
                exc
            ):
                pytest.skip(f"Optional Chromium runtime unavailable: {exc}")
            raise
        contexts, pages, errors = [], [], []
        try:
            for index in range(count):
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_init_script(WEBSOCKET_STUB)
                if init_scripts and init_scripts[index]:
                    context.add_init_script(init_scripts[index])
                context.route("**/api/**", no_auth_mock(server.route))
                page = context.new_page()
                page_errors = []
                page.on("pageerror", lambda exc, values=page_errors: values.append(str(exc)))
                contexts.append(context)
                pages.append(page)
                errors.append(page_errors)
            yield pages, errors
        finally:
            for context in contexts:
                context.close()
            browser.close()


def apply_owner_state(page, server):
    page.evaluate("value => applyState(value)", server.state_value())


def open_starred(page):
    page.wait_for_function("() => tabPreferences && uiViewStateRestored && federationRestored")
    page.locator("#starred-tabs-button").click()
    playwright.expect(page.locator("#starred-tabs-menu")).to_be_visible()


def open_agent_terminal(page, index):
    card(page, index).locator(".card-terminal").click()


@pytest.mark.parametrize("width", [390, 1280])
def test_two_contexts_share_canonical_stars_without_local_storage(static_url, width):  # noqa: F811
    server = StatefulStarServer(grouped_state())
    with isolated_pages(width, server) as ((owner, peer), errors):
        owner.goto(static_url)
        peer.goto(static_url)

        owner.get_by_role("button", name="Open group Group A: 3 agents").click()
        open_agent_terminal(owner, 0)
        terminal_tab(owner, 0).get_by_role("button", name="Star Agent 0", exact=True).click()
        playwright.expect(owner.locator("#starred-tabs-button")).to_have_text("★ 1")
        assert server.starred == {"revision": 1, "starred": [AGENT_0]}

        # State polling is explicit in this mock: another client receives the owner response.
        apply_owner_state(peer, server)
        open_starred(peer)
        peer_star = peer.locator(".starred-tab-open").filter(has_text="Agent 0")
        playwright.expect(peer_star).to_have_count(1)
        assert peer.evaluate("Object.keys(localStorage).filter(key => key.includes('tab-preferences'))") == []

        # Selecting a cross-group star switches group but performs no wake/start mutation.
        peer.locator("#starred-tabs-close").click()
        peer.get_by_role("button", name="Open group Group B: 1 agents").click()
        open_starred(peer)
        peer_star.click()
        assert peer.evaluate("selectedAgentGroup") == GROUP_A
        assert peer.evaluate("activeTab") == AGENT_IDS[0]
        assert not server.posts and not server.other_mutations

        # Browser-local order remains browser-local, and stale legacy stars are not auto-primed.
        owner.evaluate(
            """([key, agent]) => localStorage.setItem(key, JSON.stringify({
                version: 1, order: ['agent:' + agent], starred: ['agent:' + agent]
            }))""",
            [PREFERENCES_KEY, AGENT_IDS[0]],
        )
        open_starred(peer)
        peer.locator(".starred-tab-row .small").click()
        playwright.expect(peer.locator("#starred-tabs-button")).to_have_text("★ 0")
        assert server.starred == {"revision": 2, "starred": []}

        apply_owner_state(owner, server)
        open_starred(owner)
        playwright.expect(owner.locator(".starred-tab-open")).to_have_count(0)
        saved = owner.evaluate("key => JSON.parse(localStorage.getItem(key))", PREFERENCES_KEY)
        assert saved == {"version": 1, "order": [AGENT_0], "starred": [AGENT_0]}
        assert [body["action"] for body in server.star_posts] == ["star", "unstar"]
        assert owner.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert peer.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not any(errors)


def test_explicit_legacy_import_merges_once_and_never_auto_imports(static_url):  # noqa: F811
    server = StatefulStarServer(grouped_state(), {"revision": 4, "starred": ["browser"]})
    legacy = json.dumps(
        {
            "version": 1,
            "order": [AGENT_1, AGENT_0],
            "starred": [AGENT_0, GROUP_A_TOKEN, "not-a-valid-star"],
        }
    )
    init = f"""
        localStorage.setItem({json.dumps(PREFERENCES_KEY)}, {json.dumps(legacy)});
        localStorage.setItem('swarm.ui-view-state.local', JSON.stringify({{
            version: 1, terminalTabs: {{ids: [{json.dumps(AGENT_IDS[0])}], active: 'dashboard'}},
            browserOpen: false, groupedBranches: [],
            agentGroupView: {{selected: null, opened: []}}
        }}));
        window.confirm = () => true;
    """
    with isolated_pages(1280, server, count=2, init_scripts=[None, init]) as ((other, legacy_page), errors):
        other.goto(static_url)
        legacy_page.goto(static_url)
        open_starred(legacy_page)
        import_button = legacy_page.locator("#starred-tabs-import")
        playwright.expect(import_button).to_be_visible()
        import_button.click()
        playwright.expect(import_button).to_be_hidden()

        assert server.starred == {
            "revision": 5,
            "starred": ["browser", AGENT_0, GROUP_A_TOKEN],
        }
        body = server.star_posts[-1]
        assert body["action"] == "import"
        assert body["starred"] == [AGENT_0, GROUP_A_TOKEN]
        assert legacy_page.evaluate(
            "key => localStorage.getItem(key + '.stars-imported')", PREFERENCES_KEY
        ) == "true"

        # A later local unstar leaves old local data untouched and the marker prevents reimport.
        other.evaluate("value => applyState(value)", server.state_value())
        open_starred(other)
        other.get_by_role("button", name="Unstar Agent 0", exact=True).click()
        post_count = len(server.star_posts)
        legacy_page.reload()
        playwright.expect(legacy_page.locator("#starred-tabs-import")).to_be_hidden()
        assert len(server.star_posts) == post_count
        saved = legacy_page.evaluate("key => JSON.parse(localStorage.getItem(key))", PREFERENCES_KEY)
        assert saved["starred"] == [AGENT_0, GROUP_A_TOKEN, "not-a-valid-star"]

        # A completely new browser with no migration data performs no write.
        with legacy_page.context.browser.new_context(viewport={"width": 1280, "height": 900}) as fresh:
            fresh.add_init_script(WEBSOCKET_STUB)
            fresh.route("**/api/**", no_auth_mock(server.route))
            page = fresh.new_page()
            page.goto(static_url)
            playwright.expect(page.locator("#starred-tabs-button")).to_have_text("★ 2")
            playwright.expect(page.locator("#starred-tabs-import")).to_be_hidden()
        assert len(server.star_posts) == post_count
        assert not any(errors)


class FederationStarServer(FederationGroupServer):
    """Federation fixture whose local and remote owners have independent stars."""

    def __init__(self, local, remote):
        super().__init__(local, remote)
        self.servers = {
            "local": StatefulStarServer(local),
            self.REMOTE: StatefulStarServer(remote),
        }

    def route(self, route):
        path = urlsplit(route.request.url).path
        remote_path = f"/api/federation/{self.REMOTE}/proxy/api/starred-tabs"
        if path == "/api/starred-tabs":
            self.requests.append((route.request.method, path))
            return self.servers["local"].handle_star(route, path)
        if path == remote_path:
            self.requests.append((route.request.method, path))
            return self.servers[self.REMOTE].handle_star(route, path)
        return super().route(route)


def test_remote_owner_uses_proxy_and_keeps_colliding_swarm_ids_isolated(static_url):  # noqa: F811
    local = grouped_state()
    remote = grouped_state()
    local["agents"][0]["name"] = "Local identity"
    remote["agents"][0]["name"] = "Remote identity"
    server = FederationStarServer(local, remote)
    remote_path = f"/api/federation/{server.REMOTE}/proxy/api/starred-tabs"

    with isolated_pages(1280, server, count=1) as ((page,), errors):
        page.goto(static_url)
        page.locator("#swarm-selector").select_option(server.REMOTE)
        page.get_by_role("button", name="Open group Group A: 3 agents").click()
        open_agent_terminal(page, 0)
        terminal_tab(page, 0).get_by_role("button", name="Star Remote identity", exact=True).click()
        open_starred(page)
        playwright.expect(page.locator(".starred-tab-open")).to_contain_text("Remote identity")

        assert server.servers[server.REMOTE].starred["starred"] == [AGENT_0]
        assert server.servers["local"].starred["starred"] == []
        assert server.servers[server.REMOTE].star_requests
        assert all(path == remote_path for _, path, _ in server.servers[server.REMOTE].star_requests)

        page.locator("#starred-tabs-close").click()
        page.locator("#swarm-selector").select_option("local")
        open_starred(page)
        playwright.expect(page.locator(".starred-tab-open")).to_have_count(0)
        assert not server.servers["local"].star_posts
        assert not any(errors)


def test_missing_offline_stale_conflict_and_unknown_outcomes_are_safe(static_url):  # noqa: F811
    server = StatefulStarServer(grouped_state(), {"revision": 5, "starred": [AGENT_0]})
    with isolated_pages(1280, server, count=1) as ((page,), errors):
        page.goto(static_url)
        page.get_by_role("button", name="Open group Group A: 3 agents").click()
        open_agent_terminal(page, 0)

        # Lower-revision polling cannot roll back a newer projection.
        stale = server.state_value()
        stale["starred_tabs"] = {"revision": 3, "starred": []}
        page.evaluate("value => applyState(value)", stale)
        assert page.evaluate("state.starred_tabs") == {"revision": 5, "starred": [AGENT_0]}

        # Missing/offline state disables writes but leaves the cached shortcut navigable.
        missing = server.state_value()
        missing.pop("starred_tabs")
        missing["starred_tabs_error"] = "Shared stars unavailable"
        page.evaluate("value => applyState(value)", missing)
        playwright.expect(terminal_tab(page, 0).locator(".tab-star")).to_be_disabled()
        open_starred(page)
        cached = page.locator(".starred-tab-open").filter(has_text="Agent 0")
        playwright.expect(cached).to_be_enabled()
        cached.click()
        assert page.evaluate("activeTab") == AGENT_IDS[0]
        assert not server.star_posts

        offline = server.state_value()
        offline["federation_offline"] = True
        page.evaluate("value => applyState(value)", offline)
        playwright.expect(terminal_tab(page, 0).locator(".tab-star")).to_be_disabled()
        open_starred(page)
        playwright.expect(
            page.locator(".starred-tab-open").filter(has_text="Agent 0")
        ).to_be_enabled()
        page.locator("#starred-tabs-close").click()
        assert not server.star_posts

        apply_owner_state(page, server)
        # Simulate another client advancing the backend without this page polling it.
        server.starred = {"revision": 6, "starred": [AGENT_0, "browser"]}
        server.state["starred_tabs"] = copy.deepcopy(server.starred)
        terminal_tab(page, 0).get_by_role("button", name="Star Agent 0", exact=True)
        page.locator(".agent-group-tab .tab").click()
        open_agent_terminal(page, 1)
        terminal_tab(page, 1).get_by_role("button", name="Star Agent 1", exact=True).click()
        playwright.expect(page.locator("#starred-tabs-status")).to_contain_text("click again")
        assert len(server.star_posts) == 1
        assert server.star_posts[0]["revision"] == 5

        # A network failure has an unknown outcome and must not be retried blindly.
        server.star_fail_next = "abort"
        terminal_tab(page, 1).get_by_role("button", name="Star Agent 1", exact=True).click()
        playwright.expect(page.locator("#starred-tabs-status")).to_contain_text("Do not retry blindly")
        failed = server.star_posts[-1]
        assert failed["request_id"] != server.star_posts[0]["request_id"]
        post_count = len(server.star_posts)
        terminal_tab(page, 1).locator(".tab-star").click(force=True)
        assert len(server.star_posts) == post_count

        open_starred(page)
        page.locator("#starred-tabs-refresh").click()
        playwright.expect(page.locator("#starred-tabs-status")).not_to_contain_text(
            "Do not retry blindly"
        )
        assert len(server.star_posts) == post_count
        assert not any(errors)


@pytest.mark.parametrize("width", [390, 1280])
def test_star_names_are_literal_and_menu_never_overflows(static_url, width):  # noqa: F811
    unsafe_agent = '<img src=x onerror="window.starXss=true"> Secret X'
    unsafe_group = '<svg onload="window.groupStarXss=true"> Group X'
    state = grouped_state()
    state["agents"][3]["name"] = unsafe_agent
    state["agent_groups"]["groups"][1]["name"] = unsafe_group
    server = StatefulStarServer(
        state,
        {
            "revision": 9,
            "starred": [AGENT_3, f"agent-group:{GROUP_B}", "browser"],
        },
    )
    with isolated_pages(width, server, count=1) as ((page,), errors):
        page.goto(static_url)
        open_starred(page)
        text = page.locator("#starred-tabs-list").inner_text()
        assert unsafe_agent in text
        assert unsafe_group in text
        assert not page.locator("#starred-tabs-list img, #starred-tabs-list svg").count()
        assert not page.evaluate("window.starXss === true || window.groupStarXss === true")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#starred-tabs-menu").evaluate(
            "node => node.getBoundingClientRect().right <= innerWidth"
        )
        assert not any(errors)
