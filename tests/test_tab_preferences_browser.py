"""Tab ordering and stars navigate groups without waking or closing agents."""

import re

import pytest
from test_agent_groups_browser import (
    AGENT_IDS,
    GROUP_A,
    GROUP_B,
    GroupServer,
    browser_state,
    card,
    open_page,
)
from test_dashboard_browser import static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


def terminal_tab(page, index):
    return page.locator(f'#tabs [data-agent-tab="{AGENT_IDS[index]}"]')


@pytest.mark.parametrize("width", [390, 1280])
def test_stars_cross_groups_and_order_survives_reload_without_agent_actions(static_url, width):  # noqa: F811
    projection = {
        "revision": 1,
        "groups": [{"id": GROUP_A, "name": "Group A"}, {"id": GROUP_B, "name": "Group B"}],
        "assignments": {AGENT_IDS[i]: GROUP_A for i in [0, 1, 2]} | {AGENT_IDS[3]: GROUP_B},
    }
    server = GroupServer(browser_state(projection))
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
        assert not server.posts
        assert not errors


def test_native_drag_reorders_terminal_slots_only(static_url):  # noqa: F811
    server = GroupServer(browser_state())
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
