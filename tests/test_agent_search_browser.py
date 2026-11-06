"""Browser coverage for full-card dashboard search metadata and group scopes."""

import copy
import json
from urllib.parse import unquote, urlsplit

import pytest
from test_agent_groups_browser import (
    AGENT_IDS,
    GROUP_A,
    GroupServer,
    agent,
    browser_state,
    card,
    open_page,
)
from test_dashboard_browser import static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

GROUP_NAME = "Named Search Team"
XSS_LABEL = 'XSS file label <img src=x onerror="window.fileSearchExecuted=true">'


class SearchServer(GroupServer):
    """Group server that traces every API request and serves expanded messages."""

    def __init__(self, state):
        super().__init__(state)
        self.requests = []

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        self.requests.append((request.method, path))
        if request.method == "GET" and path.endswith("/messages"):
            agent_id = unquote(path.split("/")[-2])
            value = {"agent_id": agent_id, "revision": 0, "messages": []}
            route.fulfill(content_type="application/json", body=json.dumps(value))
            return
        super().route(route)


def search_state(*, server_grouping):
    projection = {
        "revision": 4,
        "groups": [{"id": GROUP_A, "name": GROUP_NAME}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    state = browser_state(projection)
    state["agents"].append(agent(5, parent=3))

    parent, product_child, detail_child, group_parent, excluded, group_child = state[
        "agents"
    ]
    parent.update(name="Root Atlas", status="idle", activity="Coordinating children")
    product_child.update(
        name="Product Scout",
        status="running",
        activity="Shared collapsed branch marker",
        product={
            "kind": "link",
            "label": "Launch manifest product",
            "url": "https://products.example/release/search-only-route?view=card",
        },
    )
    detail_child.update(
        name="Metadata Analyst",
        status="waiting",
        activity="Shared collapsed branch marker",
        activities=[{"title": "Investigate lunar selector title", "status": "done"}],
        notes="Durable cobalt checkpoint note",
        last_error="Quartz renderer failure",
        repository="/repos/searchable-dashboard",
        session_id="session-browser-search-42",
        product={
            "kind": "file",
            "id": "product-safe-id",
            "label": "Offline architecture bundle",
            "filename": "architecture-search.pdf",
        },
        file_count=2,
        file_search=[
            {
                "label": XSS_LABEL,
                "filename": "safe-index-basename.txt",
                "media_type": "application/x-search-fixture",
                "path": "/private/original-file-never-match.txt",
                "payload": {"token": "nested-file-payload-never-match"},
            }
        ],
        messages=[{"text": "mail-body-never-match"}],
        credentials={"api_key": "credential-object-never-match"},
        terminal_history=["terminal-history-never-match"],
    )
    group_parent.update(name="Grouped Parent", status="idle")
    group_child.update(
        name="Grouped File Child",
        status="done",
        file_count=1,
        file_search=[
            {
                "label": "Grouped evidence label",
                "filename": "grouped-evidence.csv",
                "media_type": "text/x-grouped-search",
                "path": "/vault/grouped-original-never-match.csv",
            }
        ],
    )
    excluded.update(
        name="Excluded Payload Agent",
        status="dormant",
        messages=[{"text": "second-mail-body-never-match"}],
        terminal_history="shell-secret-never-match",
        credential_object={"password": "password-object-never-match"},
    )

    if server_grouping:
        state["grouping_settings"] = {
            "revision": projection["revision"],
            "server_managed": True,
            "initialized": True,
            "grouped_branches": [AGENT_IDS[0]],
        }
        state["grouping_settings_error"] = None
    return state


def install_preferences(page):
    page.add_init_script(
        f"""const ids = {json.dumps(AGENT_IDS)};
        localStorage.setItem('swarm.cardViews.v1:/fixture', JSON.stringify({{
            [ids[0]]: true,
            [ids[1]]: true,
            [ids[2]]: true
        }}));
        localStorage.setItem('swarm.ui-view-state.local', JSON.stringify({{
            terminalTabs: {{ids: [], active: 'dashboard'}},
            groupedBranches: [ids[0]],
            agentGroupView: {{selected: null, opened: []}}
        }}));"""
    )


def fill_search(page, value):
    menu = page.locator("#status-filter-menu")
    if menu.is_hidden():
        page.locator("#status-filter-button").click()
    page.locator("#search").fill(value)


def card_ids(page):
    return page.locator(".agent-card").evaluate_all(
        "cards => cards.map(card => card.dataset.agentId)"
    )


def expect_cards(page, expected, *, context=()):
    playwright.expect(page.locator(".agent-card")).to_have_count(len(expected))
    assert card_ids(page) == [AGENT_IDS[index] for index in expected]
    context_ids = page.locator(".org-node.is-context > .agent-card").evaluate_all(
        "cards => cards.map(card => card.dataset.agentId)"
    )
    assert context_ids == [AGENT_IDS[index] for index in context]


def assert_search_made_no_requests(server, baseline):
    normal_gets = {
        "/api/state",
        "/api/federation/nodes",
        "/api/federation/settings",
        "/api/notifications",
    }
    after_baseline = server.requests[len(baseline) :]
    assert all(method == "GET" and path in normal_gets for method, path in after_baseline)
    assert not [request for request in server.requests if request[0] != "GET"]
    forbidden = ("/files", "/content", "/terminal", "/wake")
    assert not [
        request
        for request in after_baseline
        if any(part in request[1] for part in forbidden) or request[1].endswith("/messages")
    ]


@pytest.mark.parametrize(
    ("width", "server_grouping"), [(390, False), (1280, True)]
)
def test_root_search_uses_full_safe_metadata_without_expanding_or_fetching(
    static_url,  # noqa: F811
    width,
    server_grouping,
):
    state = search_state(server_grouping=server_grouping)
    server = SearchServer(state)
    with open_page(width, server, touch=width == 390) as (page, errors):
        install_preferences(page)
        page.goto(static_url)
        playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(4)
        baseline = copy.copy(server.requests)

        search = page.locator("#search")
        assert search.get_attribute("type") == "search"
        assert search.get_attribute("placeholder") == (
            "Search cards, products, files, details…"
        )
        assert page.locator("#status-filter-menu").evaluate(
            "menu => menu.contains(document.querySelector('#search'))"
        )

        # Product URLs and filenames, activity titles, notes, errors, repository,
        # session metadata, and the bounded file index all work while cards remain
        # in their saved compact/expanded states.
        cases = [
            ("Launch manifest product", (0, 1)),
            ("release/search-only-route", (0, 1)),
            ("Offline architecture bundle", (0, 2)),
            ("architecture-search.pdf", (0, 2)),
            ("lunar selector title", (0, 2)),
            ("Waiting", (0, 2)),
            ("cobalt checkpoint", (0, 2)),
            ("Quartz renderer failure", (0, 2)),
            ("/repos/searchable-dashboard", (0, 2)),
            ("session-browser-search-42", (0, 2)),
            ("safe-index-basename.txt", (0, 2)),
            ("application/x-search-fixture", (0, 2)),
        ]
        for query, expected in cases:
            fill_search(page, query)
            expect_cards(page, expected, context=(0,))
            playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(2)

        fill_search(page, "Root Atlas")
        expect_cards(page, (0, 1, 2))
        fill_search(page, "XSS file label")
        expect_cards(page, (0, 2), context=(0,))
        assert XSS_LABEL not in page.locator("body").inner_text()
        assert not page.locator(".agent-card img, .agent-card script").count()
        assert not page.evaluate("window.fileSearchExecuted === true")

        # Only explicit safe fields are indexed: not mailbox bodies, original file
        # locations, arbitrary nested payloads/credentials, or terminal history.
        for secret in (
            "/private/original-file-never-match.txt",
            "nested-file-payload-never-match",
            "mail-body-never-match",
            "credential-object-never-match",
            "terminal-history-never-match",
            "password-object-never-match",
            "shell-secret-never-match",
        ):
            fill_search(page, secret)
            expect_cards(page, ())

        # Text and status are intersected rather than replacing one another.
        fill_search(page, "lunar selector title")
        page.locator('input[data-status="all"]').uncheck()
        page.locator('input[data-status="waiting"]').check()
        expect_cards(page, (0, 2), context=(0,))
        page.locator('input[data-status="waiting"]').uncheck()
        page.locator('input[data-status="running"]').check()
        expect_cards(page, ())
        assert search.input_value() == "lunar selector title"
        page.locator('input[data-status="all"]').check()

        # Search filtering does not unfold a saved grouped branch or change card
        # view preferences, even when both descendants match.
        fill_search(page, "Shared collapsed branch marker")
        expect_cards(page, (0, 1, 2), context=(0,))
        branch = page.locator(f'.org-branch-toggle[data-agent-id="{AGENT_IDS[0]}"]')
        playwright.expect(branch).to_have_attribute("aria-expanded", "false")
        assert not card(page, 1).is_visible() and not card(page, 2).is_visible()
        fill_search(page, "")
        expect_cards(page, (0, 1, 2, 4))
        playwright.expect(branch).to_have_attribute("aria-expanded", "false")
        playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(4)

        # Replacing product and file-index sections in a live state update must
        # immediately remove old matches and add new ones without a stale cache.
        fill_search(page, "Launch manifest product")
        expect_cards(page, (0, 1), context=(0,))
        state["agents"][1]["product"] = {
            "kind": "file",
            "id": "replacement-product",
            "label": "Replacement live artifact",
            "filename": "replacement-live.zip",
        }
        state["agents"][2]["file_search"] = [
            {
                "label": "Replacement indexed file",
                "filename": "replacement-index.json",
                "media_type": "application/json",
            }
        ]
        page.evaluate("value => applyState(value)", server.state_value())
        expect_cards(page, ())
        fill_search(page, "replacement-live.zip")
        expect_cards(page, (0, 1), context=(0,))
        fill_search(page, "safe-index-basename.txt")
        expect_cards(page, ())
        fill_search(page, "replacement-index.json")
        expect_cards(page, (0, 2), context=(0,))

        assert_search_made_no_requests(server, baseline)
        assert not server.posts
        assert not server.other_mutations
        assert not errors


@pytest.mark.parametrize(
    ("width", "server_grouping"), [(390, False), (1280, True)]
)
def test_folder_and_scoped_group_search_share_matches_and_live_file_index(
    static_url,  # noqa: F811
    width,
    server_grouping,
):
    state = search_state(server_grouping=server_grouping)
    server = SearchServer(state)
    with open_page(width, server, touch=width == 390) as (page, errors):
        page.goto(static_url)
        playwright.expect(page.locator(".agent-card")).to_have_count(4)
        baseline = copy.copy(server.requests)

        # A member-only file match keeps its root folder. Entering that folder
        # retains the matching child's dimmed parent for hierarchy context.
        fill_search(page, "grouped-evidence.csv")
        expect_cards(page, ())
        folder = page.get_by_role("button", name=f"Open group {GROUP_NAME}: 2 agents")
        playwright.expect(folder).to_be_visible()
        page.keyboard.press("Escape")
        folder.click()
        expect_cards(page, (3, 5), context=(3,))
        assert page.locator("#status-filter-count").inner_text() == "2"

        # The named group is explicit card metadata, so its name matches every
        # member rather than only the folder shell.
        fill_search(page, GROUP_NAME)
        expect_cards(page, (3, 5))

        # Scoped status changes still intersect the same text predicate.
        fill_search(page, "Grouped evidence label")
        page.locator('input[data-status="all"]').uncheck()
        page.locator('input[data-status="done"]').check()
        expect_cards(page, (3, 5), context=(3,))
        page.locator('input[data-status="done"]').uncheck()
        page.locator('input[data-status="idle"]').check()
        expect_cards(page, ())
        page.locator('input[data-status="all"]').check()

        # Safe labels and media types are searchable but stay absent from compact
        # DOM text. Original paths remain excluded.
        fill_search(page, "text/x-grouped-search")
        expect_cards(page, (3, 5), context=(3,))
        assert "Grouped evidence label" not in page.locator("body").inner_text()
        fill_search(page, "/vault/grouped-original-never-match.csv")
        expect_cards(page, ())

        state["agents"][5]["file_search"] = [
            {
                "label": "Live grouped replacement",
                "filename": "live-grouped.parquet",
                "media_type": "application/x-parquet",
            }
        ]
        page.evaluate("value => applyState(value)", server.state_value())
        expect_cards(page, ())
        fill_search(page, "live-grouped.parquet")
        expect_cards(page, (3, 5), context=(3,))
        fill_search(page, "")
        expect_cards(page, (3, 5))
        playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(2)

        assert_search_made_no_requests(server, baseline)
        assert not server.posts
        assert not server.other_mutations
        assert not errors
