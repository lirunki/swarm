"""Tab ordering and stars navigate groups without waking or closing agents."""

import copy
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from test_agent_groups_browser import (
    AGENT_IDS,
    GROUP_A,
    GROUP_B,
    REQUEST_ID,
    GroupServer,
    browser_state,
    card,
    open_page,
)
from test_dashboard_browser import static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


def terminal_tab(page, index):
    return page.locator(f'#tabs [data-agent-tab="{AGENT_IDS[index]}"]')


class StatefulStarServer(GroupServer):
    """Group fixture plus an owner-scoped, shared starred-tabs backend."""

    def __init__(self, state, starred=None):
        super().__init__(state)
        self.starred = copy.deepcopy(starred or {"revision": 0, "starred": []})
        self.star_requests = []
        self.star_posts = []
        self.star_fail_next = None
        self.state["starred_tabs"] = copy.deepcopy(self.starred)
        self.state["starred_tabs_error"] = None

    def state_value(self):
        value = super().state_value()
        value["starred_tabs"] = copy.deepcopy(self.starred)
        value["starred_tabs_error"] = self.state.get("starred_tabs_error")
        return value

    def mutate_stars(self, body):
        before = list(self.starred["starred"])
        action = body["action"]
        if action == "star" and body["tab_id"] not in self.starred["starred"]:
            self.starred["starred"].append(body["tab_id"])
        elif action == "unstar":
            self.starred["starred"] = [
                token for token in self.starred["starred"] if token != body["tab_id"]
            ]
        elif action == "import":
            for token in body["starred"]:
                if token not in self.starred["starred"]:
                    self.starred["starred"].append(token)
        if before != self.starred["starred"]:
            self.starred["revision"] += 1
        self.state["starred_tabs"] = copy.deepcopy(self.starred)
        return copy.deepcopy(self.starred)

    def route(self, route):
        path = urlsplit(route.request.url).path
        if path != "/api/starred-tabs":
            return super().route(route)
        self.handle_star(route, path)

    def handle_star(self, route, request_path="/api/starred-tabs"):
        request = route.request
        method = request.method
        body = request.post_data_json if method == "POST" else None
        self.star_requests.append((method, request_path, copy.deepcopy(body)))
        if method == "GET":
            value = copy.deepcopy(self.starred)
            status = 200
        elif method == "POST":
            self.star_posts.append(copy.deepcopy(body))
            failure, self.star_fail_next = self.star_fail_next, None
            if failure == "abort":
                route.abort("failed")
                return
            if failure is not None:
                status, value = failure
            else:
                try:
                    assert REQUEST_ID.fullmatch(body.get("request_id", ""))
                    UUID(body["request_id"])
                    assert body["revision"] == self.starred["revision"]
                    assert body["action"] in {"star", "unstar", "import"}
                except (AssertionError, KeyError, TypeError, ValueError):
                    status, value = 409, {"detail": "stale starred-tabs revision"}
                else:
                    status, value = 200, self.mutate_stars(body)
        else:
            status, value = 405, {"detail": "method not allowed"}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


@pytest.mark.parametrize("width", [390, 1280])
def test_stars_cross_groups_and_order_survives_reload_without_agent_actions(static_url, width):  # noqa: F811
    projection = {
        "revision": 1,
        "groups": [{"id": GROUP_A, "name": "Group A"}, {"id": GROUP_B, "name": "Group B"}],
        "assignments": {AGENT_IDS[i]: GROUP_A for i in [0, 1, 2]} | {AGENT_IDS[3]: GROUP_B},
    }
    server = StatefulStarServer(browser_state(projection))
    with open_page(width, server, touch=width == 390) as (page, errors):
        page.goto(static_url)
        page.get_by_role("button", name="Open group Group A: 3 agents").click()
        for index in (0, 1):
            card(page, index).locator(".card-terminal").click()
            page.locator(".agent-group-tab .tab").click()
        star = terminal_tab(page, 0).get_by_role("button", name="Star Agent 0", exact=True)
        playwright.expect(star).to_have_text("›_")
        assert star.evaluate('node=>node.nextElementSibling.classList.contains("tab")')
        assert terminal_tab(page, 0).locator(".tab").inner_text() == "Agent 0"
        terminal_tab(page, 0).get_by_role("button", name="Star Agent 0", exact=True).click()
        playwright.expect(
            terminal_tab(page, 0).get_by_role("button", name="Unstar Agent 0", exact=True)
        ).to_have_text("★")
        # Keyboard alternative also makes reordering usable without HTML drag support.
        terminal_tab(page, 1).locator(".tab").click()
        terminal_tab(page, 1).locator(".tab").press("Alt+Shift+ArrowLeft")
        ids = page.locator("#tabs [data-agent-tab]:not([hidden])").evaluate_all(
            "nodes=>nodes.map(node=>node.dataset.agentTab)"
        )
        assert ids == [AGENT_IDS[1], AGENT_IDS[0]]
        assert page.evaluate("terminals.size") == 2
        page.locator("#dashboard-tab").click()
        page.get_by_role("button", name="Open group Group B: 1 agents").click()
        card(page, 3).locator(".card-terminal").click()
        terminal_tab(page, 3).get_by_role("button", name="Star Agent 3", exact=True).click()
        page.locator("#starred-tabs-button").click()
        shortcuts = page.locator(".starred-tab-open")
        playwright.expect(shortcuts).to_have_count(2)
        playwright.expect(shortcuts.first).to_contain_text("Group A")
        shortcuts.first.click()
        assert page.evaluate("activeTab") == AGENT_IDS[0]
        assert page.evaluate("selectedAgentGroup") == GROUP_A
        playwright.expect(terminal_tab(page, 3)).to_be_hidden()
        assert page.evaluate("terminals.size") == 3
        terminal_tab(page, 0).locator(".tab-close").click()
        page.locator("#starred-tabs-button").click()
        page.locator(".starred-tab-open").filter(has_text="Agent 0").click()
        assert page.evaluate("activeTab") == AGENT_IDS[0]
        assert page.evaluate("terminals.size") == 3
        page.reload()
        playwright.expect(terminal_tab(page, 0)).to_be_visible()
        ids = page.locator("#tabs [data-agent-tab]:not([hidden])").evaluate_all(
            "nodes=>nodes.map(node=>node.dataset.agentTab)"
        )
        assert ids == [AGENT_IDS[1], AGENT_IDS[0]]
        playwright.expect(page.locator("#starred-tabs-button")).to_have_text("★ 2")
        # A stopped favorite reveals its card and never launches its process.
        server.state["agents"][0]["alive"] = False
        page.evaluate("value=>applyState(value)", server.state_value())
        page.locator("#starred-tabs-button").click()
        page.locator(".starred-tab-open").filter(has_text="Agent 0").click()
        playwright.expect(page.locator("#dashboard")).to_be_visible()
        playwright.expect(card(page, 0)).to_have_class(re.compile("notification-reveal"))
        assert not server.posts and not server.other_mutations
        assert [body["action"] for body in server.star_posts] == ["star", "star"]
        assert not errors


def test_native_drag_reorders_terminal_slots_only(static_url):  # noqa: F811
    server = StatefulStarServer(browser_state())
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)
        for index in (3, 4):
            card(page, index).locator(".card-terminal").click()
            page.locator("#dashboard-tab").click()
        source, target = terminal_tab(page, 3), terminal_tab(page, 4)
        target_box = target.bounding_box()
        source.drag_to(target, target_position={"x": target_box["width"] - 3, "y": 10})
        ids = page.locator("#tabs [data-agent-tab]:not([hidden])").evaluate_all(
            "nodes=>nodes.map(node=>node.dataset.agentTab)"
        )
        assert ids == [AGENT_IDS[4], AGENT_IDS[3]]
        assert page.locator("#tabs").evaluate("node=>node.firstElementChild.id") == "dashboard-tab"
        assert page.evaluate("terminals.size") == 2
        assert not server.posts and not errors
