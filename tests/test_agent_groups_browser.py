"""Browser coverage for family grouping and group-scoped dashboard tabs."""

import copy
import json
import re
from contextlib import contextmanager
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from test_dashboard_browser import no_auth_mock, static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

AGENT_IDS = [f"00000000-0000-4000-8000-{index:012d}" for index in range(6)]
GROUP_A = "10000000-0000-4000-8000-000000000001"
GROUP_B = "10000000-0000-4000-8000-000000000002"
REQUEST_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


def agent(index, *, parent=None, alive=False):
    return {
        "id": AGENT_IDS[index],
        "name": f"Agent {index}",
        "goal": f"Goal {index}",
        "status": "running" if alive else "idle",
        "alive": alive,
        "parent_id": AGENT_IDS[parent] if parent is not None else None,
        "backend": "pi",
        "managed": True,
        "auto_resume": False,
        "activity": f"Current task {index}",
        "folder": f"/workspace/{index}",
        "notes": "Checkpoint",
        "session_file": f"/sessions/{index}.jsonl",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def browser_state(projection=None):
    # 0 is the parent of sibling children 1 and 2. Agents 3 and 4 are
    # independent families. UUIDs are intentional: the real groups API rejects
    # the short a0-style IDs used by the general dashboard fixture.
    return {
        "agents": [
            agent(0, alive=True),
            agent(1, parent=0),
            agent(2, parent=0),
            agent(3, alive=True),
            agent(4),
        ],
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
        "agent_groups": copy.deepcopy(
            projection or {"revision": 0, "groups": [], "assignments": {}}
        ),
        "agent_groups_error": None,
    }


class GroupServer:
    """Stateful route mock with the same revision/request shape as /api/groups."""

    def __init__(self, state):
        self.state = state
        self.projection = copy.deepcopy(state["agent_groups"])
        self.posts = []
        self.other_mutations = []
        self.invalid_requests = []
        self.fail_next = None
        self.notifications = []

    def projection_value(self, operation=None):
        value = copy.deepcopy(self.projection)
        if operation is not None:
            value["operation"] = operation
        return value

    def state_value(self):
        value = copy.deepcopy(self.state)
        value["agent_groups"] = copy.deepcopy(self.projection)
        return value

    def families(self):
        agents = {value["id"]: value for value in self.state["agents"]}
        adjacent = {value: set() for value in agents}
        for value in agents.values():
            parent = value.get("parent_id")
            if parent in agents and parent != value["id"]:
                adjacent[value["id"]].add(parent)
                adjacent[parent].add(value["id"])
        result, seen = [], set()
        for start in sorted(agents):
            if start in seen:
                continue
            stack, family = [start], []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                family.append(current)
                stack.extend(adjacent[current] - seen)
            result.append(sorted(family))
        return result

    def expanded_ids(self, requested):
        requested = set(requested)
        return sorted(
            member
            for family in self.families()
            if requested.intersection(family)
            for member in family
        )

    def mutate(self, body):
        action = body.get("action")
        operation = {"action": action}
        if action == "create":
            group_id = f"20000000-0000-4000-8000-{self.projection['revision'] + 1:012d}"
            self.projection["groups"].append({"id": group_id, "name": body["name"]})
            operation["group_id"] = group_id
            for agent_id in self.expanded_ids(body["agent_ids"]):
                self.projection["assignments"][agent_id] = group_id
        elif action == "assign":
            group_id = body.get("group_id")
            operation["group_id"] = group_id
            for agent_id in self.expanded_ids(body["agent_ids"]):
                if group_id is None:
                    self.projection["assignments"].pop(agent_id, None)
                else:
                    self.projection["assignments"][agent_id] = group_id
        elif action == "rename":
            operation["group_id"] = body["group_id"]
            for group in self.projection["groups"]:
                if group["id"] == body["group_id"]:
                    group["name"] = body["name"]
                    break
        elif action == "delete":
            group_id = body["group_id"]
            operation["group_id"] = group_id
            self.projection["groups"] = [
                group for group in self.projection["groups"] if group["id"] != group_id
            ]
            self.projection["assignments"] = {
                key: value
                for key, value in self.projection["assignments"].items()
                if value != group_id
            }
        else:
            raise AssertionError(f"unexpected group action: {action}")
        self.projection["revision"] += 1
        self.state["agent_groups"] = copy.deepcopy(self.projection)
        return self.projection_value(operation)

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        status = 200

        if path == "/api/state" and method == "GET":
            value = self.state_value()
        elif path == "/api/groups" and method == "GET":
            value = self.projection_value()
        elif path == "/api/groups" and method == "POST":
            body = request.post_data_json
            self.posts.append(copy.deepcopy(body))
            try:
                assert REQUEST_ID.fullmatch(body.get("request_id", ""))
                UUID(body["request_id"])
                assert body["revision"] == self.projection["revision"]
                assert all(UUID(value) for value in body.get("agent_ids", []))
            except (AssertionError, KeyError, TypeError, ValueError) as exc:
                self.invalid_requests.append((body, str(exc)))
                status, value = 400, {"detail": "invalid mock group request"}
            else:
                failure, self.fail_next = self.fail_next, None
                if failure:
                    status, detail = failure
                    value = {"detail": detail}
                else:
                    value = self.mutate(body)
        elif path == "/api/federation/nodes" and method == "GET":
            value = {"enabled": False, "nodes": []}
        elif path == "/api/federation/settings" and method == "GET":
            value = {
                "enabled": False,
                "connected": False,
                "status": "disconnected",
                "has_api_key": False,
                "relay_url": "",
                "relay_node_id": "local",
                "relay_name": "Local",
            }
        elif path == "/api/notifications" and method == "GET":
            value = {"notifications": copy.deepcopy(self.notifications)}
        elif path.startswith("/api/notifications/") and method == "DELETE":
            notification_id = path.rsplit("/", 1)[-1]
            self.notifications = [n for n in self.notifications if n["id"] != notification_id]
            value = {"ok": True}
        elif method != "GET":
            self.other_mutations.append((method, path, request.post_data))
            status, value = 418, {"detail": f"unexpected mutation {method} {path}"}
        else:
            # Startup helpers are deliberately mocked as well; no browser test
            # request may escape to a production/provider API.
            value = {"sessions": [], "errors": {}, "roots": []}

        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


class FederationGroupServer:
    """Route group state independently through local and federation proxy paths."""

    REMOTE = "peer-groups"

    def __init__(self, local, remote):
        self.servers = {"local": GroupServer(local), self.REMOTE: GroupServer(remote)}
        self.requests = []

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        self.requests.append((method, path))
        prefix = f"/api/federation/{self.REMOTE}/proxy"

        if path == "/api/federation/nodes" and method == "GET":
            value = {
                "enabled": True,
                "nodes": [
                    {
                        "id": self.REMOTE,
                        "name": "Remote groups",
                        "online": True,
                        "capabilities": {"remote_restart": False},
                    }
                ],
            }
        elif path == "/api/federation/settings" and method == "GET":
            value = {
                "enabled": True,
                "connected": True,
                "status": "connected",
                "has_api_key": True,
                "relay_url": "",
                "relay_node_id": "local",
                "relay_name": "Local",
            }
        else:
            swarm_id = self.REMOTE if path.startswith(prefix) else "local"
            scoped_path = path[len(prefix) :] if swarm_id == self.REMOTE else path
            server = self.servers[swarm_id]
            if scoped_path == "/api/state" and method == "GET":
                value = server.state_value()
            elif scoped_path == "/api/groups" and method == "GET":
                value = server.projection_value()
            elif scoped_path == "/api/groups" and method == "POST":
                body = request.post_data_json
                server.posts.append(copy.deepcopy(body))
                value = server.mutate(body)
            elif scoped_path == "/api/notifications" and method == "GET":
                value = {"notifications": []}
            else:
                value = {"sessions": [], "errors": {}, "roots": []}

        route.fulfill(content_type="application/json", body=json.dumps(value))


@contextmanager
def open_page(width, server, *, touch=False):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(
                exc
            ) or "error while loading shared libraries" in str(exc):
                pytest.skip(f"Optional Chromium runtime unavailable: {exc}")
            raise
        context = browser.new_context(
            viewport={"width": width, "height": 900}, has_touch=touch, is_mobile=touch
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.add_init_script(
            """window.WebSocket = class {
                static OPEN = 1;
                constructor(url) {
                    this.url = url; this.readyState = 1;
                    setTimeout(() => this.onopen?.(), 0);
                }
                close() { this.readyState = 3; }
                send() {}
            };"""
        )
        page.route("**/api/**", no_auth_mock(server.route))
        try:
            yield page, errors
        finally:
            context.close()
            browser.close()


def card(page, index):
    return page.locator(f'.agent-card[data-agent-id="{AGENT_IDS[index]}"]')


def visible_workspace_tabs(page):
    """Describe visible direct tab-strip children in their DOM order."""
    return page.locator("#tabs > :not([hidden])").evaluate_all(
        """elements => elements.map(element => {
            if (element.id === 'dashboard-tab') return 'dashboard';
            if (element.id === 'browser-tab') return null;
            if (element.classList.contains('agent-group-tab')) {
                return `group:${element.dataset.groupId}`;
            }
            const terminal = [...terminals].find(([, value]) => value.group === element);
            return terminal ? `terminal:${terminal[0]}` : 'unknown';
        }).filter(value => value !== null)"""
    )


def open_group_menu(page):
    page.locator("#group-selection-open").click()
    playwright.expect(page.locator("#group-context-menu")).to_be_visible()
    playwright.expect(page.locator("#grouping-submenu")).to_be_visible()


def expect_group_name(page, name):
    """Assert the visible group name without including the heading's caret."""
    playwright.expect(page.locator("#agent-group-name")).to_have_text(name, use_inner_text=True)
    playwright.expect(page.locator("#agent-group-name-button")).to_be_visible()


def open_group_name_menu(page):
    button = page.locator("#agent-group-name-button")
    playwright.expect(button).to_be_enabled()
    button.click()
    playwright.expect(page.locator("#agent-group-name-menu")).to_be_visible()


def click_group_name_action(page, action):
    open_group_name_menu(page)
    page.locator(f"#agent-group-{action}").click()


@pytest.mark.parametrize("width", [390, 1280])
def test_family_selection_context_assignment_folders_and_safe_names(static_url, width):  # noqa: F811
    unsafe_name = 'Existing <img src=x onerror="window.groupUnsafe=true">'
    projection = {
        "revision": 1,
        "groups": [{"id": GROUP_A, "name": unsafe_name}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    server = GroupServer(browser_state(projection))
    with open_page(width, server, touch=width == 390) as (page, errors):
        page.goto(static_url)
        playwright.expect(page.locator(".agent-card")).to_have_count(4)
        playwright.expect(page.locator("#agent-group-folders > .agent-group-folder")).to_have_count(
            1
        )
        playwright.expect(
            page.get_by_role("button", name=f"Open group {unsafe_name}: 1 agents")
        ).to_be_visible()
        assert not page.locator("#agent-group-folders img").count()
        assert not page.evaluate("window.groupUnsafe === true")

        # A collapsed hierarchy still selects the complete backend family.
        page.locator(f'.org-branch-toggle[data-agent-id="{AGENT_IDS[0]}"]').click()
        playwright.expect(card(page, 1)).to_be_hidden()
        card(page, 0).get_by_role("checkbox", name="Select family of Agent 0").check()
        for index in (0, 1, 2):
            assert card(page, index).evaluate(
                "node => node.classList.contains('is-group-selected')"
            )
        playwright.expect(page.locator("#group-selection-count")).to_have_text(
            "1 family unit selected"
        )

        # Native card controls do not alter selection; Ctrl-click adds a second family.
        card(page, 0).locator(".card-toggle").click()
        playwright.expect(page.locator("#group-selection-count")).to_have_text(
            "1 family unit selected"
        )
        card(page, 4).click(modifiers=["Control"])
        playwright.expect(page.locator("#group-selection-count")).to_have_text(
            "2 family units selected"
        )

        # Right-clicking an already-selected member is additive/idempotent and clamps
        # the context menu into the small viewport.
        card(page, 0).click(button="right", position={"x": 120, "y": 120})
        playwright.expect(page.locator("#group-selection-count")).to_have_text(
            "2 family units selected"
        )
        menu_box = page.locator("#group-context-menu").bounding_box()
        assert menu_box["x"] >= 0 and menu_box["x"] + menu_box["width"] <= width + 1
        page.keyboard.press("Escape")

        open_group_menu(page)
        menu = page.locator("#grouping-submenu")
        assert menu.get_by_role("menuitem").all_text_contents() == [
            "Default group",
            "New group…",
            unsafe_name,
        ]
        assert not menu.locator("img,script").count()
        page.once("dialog", lambda dialog: dialog.accept("Created team"))
        menu.get_by_role("menuitem", name="New group…").click()

        playwright.expect(page.locator("#agent-group-folders > .agent-group-folder")).to_have_count(
            2
        )
        playwright.expect(
            page.get_by_role("button", name="Open group Created team: 4 agents")
        ).to_be_visible()
        playwright.expect(
            page.get_by_role("button", name=f"Open group {unsafe_name}: 1 agents")
        ).to_be_visible()
        playwright.expect(page.locator(".agent-card")).to_have_count(0)
        assert server.posts[-1]["agent_ids"] == sorted(AGENT_IDS[index] for index in (0, 1, 2, 4))

        # Move one entire family to the existing group from inside its folder.
        page.get_by_role("button", name="Open group Created team: 4 agents").click()
        card(page, 0).get_by_role("checkbox", name="Select family of Agent 0").check()
        open_group_menu(page)
        page.locator("#grouping-submenu").get_by_role(
            "menuitem", name=unsafe_name, exact=True
        ).click()
        expect_group_name(page, "Created team")
        playwright.expect(page.locator(".agent-card")).to_have_count(1)
        page.locator("#dashboard-tab").click()
        playwright.expect(
            page.get_by_role("button", name=f"Open group {unsafe_name}: 4 agents")
        ).to_be_visible()
        playwright.expect(
            page.get_by_role("button", name="Open group Created team: 1 agents")
        ).to_be_visible()

        # Keyboard context-menu access remains available on touch and desktop layouts.
        page.get_by_role("button", name="Open group Created team: 1 agents").click()
        card(page, 4).focus()
        page.keyboard.press("Shift+F10")
        playwright.expect(page.locator("#group-context-menu")).to_be_visible()
        playwright.expect(page.locator("#grouping-submenu-button")).to_be_focused()
        assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
        assert not server.invalid_requests
        assert not server.other_mutations
        assert not errors


@pytest.mark.parametrize("width", [390, 1280])
def test_only_active_folder_tab_is_visible_and_root_terminal_restores(
    static_url,  # noqa: F811
    width,  # noqa: F811
):
    projection = {
        "revision": 2,
        "groups": [
            {"id": GROUP_A, "name": "Folder A"},
            {"id": GROUP_B, "name": "Folder B"},
        ],
        "assignments": {AGENT_IDS[0]: GROUP_A, AGENT_IDS[3]: GROUP_B},
    }
    server = GroupServer(browser_state(projection))
    with open_page(width, server, touch=width == 390) as (page, errors):
        page.goto(static_url)

        # Keep a root terminal and one terminal in each folder alive while moving
        # between folder dashboards.
        card(page, 4).locator(".card-terminal").click()
        page.locator("#dashboard-tab").click()
        page.get_by_role("button", name="Open group Folder A: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_A}"] .tab').click()
        page.locator("#dashboard-tab").click()
        page.get_by_role("button", name="Open group Folder B: 1 agents").click()
        card(page, 3).locator(".card-terminal").click()
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_B}"] .tab').click()

        assert page.evaluate("[...terminals.keys()]") == [
            AGENT_IDS[4],
            AGENT_IDS[0],
            AGENT_IDS[3],
        ]
        playwright.expect(page.locator("#tabs > .agent-group-tab")).to_have_count(1)
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"group:{GROUP_B}",
            f"terminal:{AGENT_IDS[3]}",
        ]
        assert page.locator(f'.agent-group-tab[data-group-id="{GROUP_A}"]').count() == 0
        assert page.evaluate("document.body.scrollWidth <= window.innerWidth")

        # Folder navigation remains bolder and brighter than runtime terminal tabs.
        folder_visuals = page.evaluate(
            r"""() => {
                const style = selector => getComputedStyle(document.querySelector(selector));
                const rgb = value => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
                const light = value => rgb(value).reduce((sum, part) => sum + part, 0);
                return {
                    dashboardWeight: style('#dashboard-tab').fontWeight,
                    folderWeight: style('.agent-group-tab > .tab').fontWeight,
                    folderLight: light(style('.agent-group-tab').backgroundColor),
                    terminalLight: light(style('.tab-group:not(.agent-group-tab)').backgroundColor),
                };
            }"""
        )
        assert folder_visuals["dashboardWeight"] == "700"
        assert folder_visuals["folderWeight"] == "700"
        assert folder_visuals["folderLight"] > folder_visuals["terminalLight"]
        page.locator('.tab-group:not(.agent-group-tab) .tab[aria-label^="Agent 3"]').click()
        selected_terminal_visuals = page.evaluate(
            r"""() => {
                const light = selector => [...getComputedStyle(document.querySelector(selector)).backgroundColor.matchAll(/[\d.]+/g)].slice(0, 3).reduce((sum, match) => sum + Number(match[0]), 0);
                return {dashboard: light('#dashboard-tab'), terminal: light('.tab-group:not(.agent-group-tab)')};
            }"""
        )
        assert selected_terminal_visuals["dashboard"] > selected_terminal_visuals["terminal"]
        assert (
            page.locator("#dashboard-tab").evaluate("node => getComputedStyle(node).fontWeight")
            == "700"
        )
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_B}"] .tab').click()

        # An inactive folder is reached from the root Dashboard and its folder tile,
        # never through a retained inactive folder tab.
        page.locator("#dashboard-tab").click()
        playwright.expect(page.locator("#tabs > .agent-group-tab")).to_have_count(0)
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"terminal:{AGENT_IDS[4]}",
        ]
        page.locator('.tab-group:not(.agent-group-tab) .tab[aria-label^="Agent 4"]').click()
        playwright.expect(page.locator(".terminal-pane:not([hidden])")).to_have_count(1)
        assert page.evaluate("activeTab") == AGENT_IDS[4]
        page.locator("#dashboard-tab").click()
        page.get_by_role("button", name="Open group Folder A: 3 agents").click()
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"group:{GROUP_A}",
            f"terminal:{AGENT_IDS[0]}",
        ]
        assert page.locator(f'.agent-group-tab[data-group-id="{GROUP_B}"]').count() == 0

        # Closing the selected folder returns to root and removes every folder tab
        # from the DOM without disposing any terminal session.
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_A}"] .tab-close').click()
        playwright.expect(page.locator("#dashboard")).to_be_visible()
        playwright.expect(page.locator("#tabs > .agent-group-tab")).to_have_count(0)
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"terminal:{AGENT_IDS[4]}",
        ]
        assert page.evaluate("[...terminals.keys()]") == [
            AGENT_IDS[4],
            AGENT_IDS[0],
            AGENT_IDS[3],
        ]
        assert page.locator(".terminal-pane").count() == 3
        assert page.evaluate("[...terminals.values()].every(value => value.pane.isConnected)")
        assert not server.posts
        assert not server.other_mutations
        assert not errors


@pytest.mark.parametrize("width", [390, 1280])
def test_group_name_menu_keyboard_state_dismissal_and_safe_prompts(
    static_url,  # noqa: F811
    width,  # noqa: F811
):
    projection = {
        "revision": 2,
        "groups": [
            {"id": GROUP_A, "name": "Menu A"},
            {"id": GROUP_B, "name": "Menu B"},
        ],
        "assignments": {AGENT_IDS[0]: GROUP_A, AGENT_IDS[3]: GROUP_B},
    }
    server = GroupServer(browser_state(projection))
    with open_page(width, server, touch=width == 390) as (page, errors):
        page.goto(static_url)
        name_button = page.locator("#agent-group-name-button")
        name_menu = page.locator("#agent-group-name-menu")
        rename = page.locator("#agent-group-rename")
        delete = page.locator("#agent-group-delete")

        # Root has no visible group actions, and the removed back control is absent.
        playwright.expect(name_button).to_be_hidden()
        playwright.expect(name_menu).to_be_hidden()
        playwright.expect(rename).to_be_hidden()
        playwright.expect(delete).to_be_hidden()
        playwright.expect(page.locator("#agent-group-back")).to_have_count(0)

        page.get_by_role("button", name="Open group Menu A: 3 agents").click()
        expect_group_name(page, "Menu A")

        name_button.focus()
        page.keyboard.press("ArrowDown")
        playwright.expect(name_menu).to_be_visible()
        playwright.expect(rename).to_be_focused()
        page.keyboard.press("ArrowDown")
        playwright.expect(delete).to_be_focused()
        page.keyboard.press("ArrowDown")
        playwright.expect(rename).to_be_focused()
        page.keyboard.press("ArrowUp")
        playwright.expect(delete).to_be_focused()
        page.keyboard.press("Home")
        playwright.expect(rename).to_be_focused()
        page.keyboard.press("End")
        playwright.expect(delete).to_be_focused()
        page.keyboard.press("Escape")
        playwright.expect(name_menu).to_be_hidden()
        playwright.expect(name_button).to_be_focused()

        # Poll repainting in the same Swarm/group context preserves an open menu.
        open_group_name_menu(page)
        page.evaluate("value => applyState(value)", server.state_value())
        playwright.expect(name_menu).to_be_visible()
        page.mouse.click(width - 2, 898)
        playwright.expect(name_menu).to_be_hidden()

        # A group context change closes it. Dashboard is the only route back to root.
        open_group_name_menu(page)
        page.evaluate("id => openAgentGroup(id)", GROUP_B)
        expect_group_name(page, "Menu B")
        playwright.expect(name_menu).to_be_hidden()
        page.locator("#dashboard-tab").click()
        playwright.expect(name_button).to_be_hidden()
        playwright.expect(name_menu).to_be_hidden()

        # Cancelled deletion preserves the group and performs no mutation.
        page.get_by_role("button", name="Open group Menu B: 1 agents").click()
        page.once("dialog", lambda dialog: dialog.dismiss())
        click_group_name_action(page, "delete")
        expect_group_name(page, "Menu B")
        assert not server.posts

        # Prompt text is rendered literally after a real rename mutation.
        unsafe = 'Literal <img src=x onerror="window.menuUnsafe=true"> XSS'
        page.once("dialog", lambda dialog: dialog.accept(unsafe))
        click_group_name_action(page, "rename")
        expect_group_name(page, unsafe)
        assert not page.locator("#agent-group-heading img, #agent-group-heading script").count()
        assert not page.evaluate("window.menuUnsafe === true")
        assert server.posts[-1]["action"] == "rename"
        assert server.posts[-1]["name"] == unsafe
        assert not server.invalid_requests
        assert not server.other_mutations
        assert not errors


def test_group_tabs_terminals_notifications_dynamic_children_rename_and_delete(static_url):  # noqa: F811
    projection = {
        "revision": 2,
        "groups": [
            {"id": GROUP_A, "name": "Family group"},
            {"id": GROUP_B, "name": "Solo group"},
        ],
        "assignments": {AGENT_IDS[0]: GROUP_A, AGENT_IDS[3]: GROUP_B},
    }
    state = browser_state(projection)
    server = GroupServer(state)
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)

        page.get_by_role("button", name="Open group Family group: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(".agent-group-tab .tab", has_text="Family group").click()
        page.locator("#dashboard-tab").click()
        page.get_by_role("button", name="Open group Solo group: 1 agents").click()
        card(page, 3).locator(".card-terminal").click()
        page.locator(".agent-group-tab .tab", has_text="Solo group").click()

        assert page.evaluate("[...terminals.keys()]") == [AGENT_IDS[0], AGENT_IDS[3]]
        playwright.expect(page.locator(".agent-group-tab")).to_have_count(1)
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"group:{GROUP_B}",
            f"terminal:{AGENT_IDS[3]}",
        ]
        playwright.expect(
            page.locator(".tab-group:not(.agent-group-tab):not([hidden])")
        ).to_have_count(1)
        assert card(page, 3).count() == 1 and card(page, 0).count() == 0

        page.locator("#dashboard-tab").click()
        playwright.expect(page.locator("#tabs > .agent-group-tab")).to_have_count(0)
        page.get_by_role("button", name="Open group Family group: 3 agents").click()
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"group:{GROUP_A}",
            f"terminal:{AGENT_IDS[0]}",
        ]
        playwright.expect(
            page.locator(".tab-group:not(.agent-group-tab):not([hidden])")
        ).to_have_count(1)
        assert (
            page.locator(
                '.tab-group:not(.agent-group-tab)[data-status]:not([hidden]) button[aria-label^="Agent 0"]'
            ).count()
            == 1
        )
        assert page.locator(".terminal-pane").count() == 2
        assert page.evaluate("[...terminals.values()].every(value => value.pane.isConnected)")

        page.locator("#dashboard-tab").click()
        playwright.expect(page.locator("#dashboard")).to_be_visible()
        playwright.expect(page.locator("#tabs > .agent-group-tab")).to_have_count(0)
        playwright.expect(
            page.locator(".tab-group:not(.agent-group-tab):not([hidden])")
        ).to_have_count(0)
        assert page.locator(".terminal-pane").count() == 2

        # Notification reveal must enter a grouped sub-dashboard even for a stopped
        # agent, expand/highlight its card, and retain all terminal session objects.
        page.evaluate(
            "([swarmId,agentId]) => window.revealNotificationAgent({swarmId,agentId})",
            ["local", AGENT_IDS[1]],
        )
        expect_group_name(page, "Family group")
        playwright.expect(card(page, 1)).to_have_class(re.compile(r"notification-reveal"))
        assert card(page, 1).locator(".goal").is_visible()
        assert page.evaluate("terminals.size") == 2

        # A newly appearing child inherits the root assignment without rewriting
        # the projection, matching the backend's family-effective behavior.
        child = agent(5, parent=0)
        server.state["agents"].append(child)
        page.evaluate("value => applyState(value)", server.state_value())
        playwright.expect(card(page, 5)).to_be_visible()
        assert page.evaluate("id => agentGroupFor(id)", AGENT_IDS[5]) == GROUP_A
        assert server.projection["assignments"] == projection["assignments"]

        page.once("dialog", lambda dialog: dialog.accept("Renamed family"))
        click_group_name_action(page, "rename")
        expect_group_name(page, "Renamed family")
        page.once("dialog", lambda dialog: dialog.accept())
        click_group_name_action(page, "delete")
        playwright.expect(page.locator("#agent-group-name-menu")).to_be_hidden()
        playwright.expect(card(page, 0)).to_be_visible()
        playwright.expect(card(page, 5)).to_be_visible()
        assert page.locator(".agent-group-tab", has_text="Renamed family").count() == 0
        assert not server.other_mutations, "group UI must not call stop/wake/tmux/command APIs"
        assert not errors


def test_group_views_and_terminal_tabs_survive_reload(static_url):  # noqa: F811
    projection = {
        "revision": 3,
        "groups": [
            {"id": GROUP_A, "name": "Persist A"},
            {"id": GROUP_B, "name": "Persist B"},
        ],
        "assignments": {AGENT_IDS[0]: GROUP_A, AGENT_IDS[3]: GROUP_B},
    }
    server = GroupServer(browser_state(projection))
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)
        page.get_by_role("button", name="Open group Persist A: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(".agent-group-tab .tab", has_text="Persist A").click()
        page.locator("#dashboard-tab").click()
        page.get_by_role("button", name="Open group Persist B: 1 agents").click()
        card(page, 3).locator(".card-terminal").click()
        page.locator(".agent-group-tab .tab", has_text="Persist B").click()
        page.locator("#dashboard-tab").click()
        playwright.expect(page.locator("#tabs > .agent-group-tab")).to_have_count(0)
        page.get_by_role("button", name="Open group Persist A: 3 agents").click()

        saved = page.evaluate("JSON.parse(localStorage.getItem('swarm.ui-view-state.local'))")
        assert saved["terminalTabs"]["ids"] == [AGENT_IDS[0], AGENT_IDS[3]]
        assert saved["agentGroupView"]["selected"] == GROUP_A
        assert set(saved["agentGroupView"]["opened"]) == {GROUP_A, GROUP_B}

        page.reload()
        expect_group_name(page, "Persist A")
        playwright.expect(page.locator(".agent-group-tab")).to_have_count(1)
        playwright.expect(
            page.locator(f'#dashboard-tab + .agent-group-tab[data-group-id="{GROUP_A}"]')
        ).to_have_count(1)
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"group:{GROUP_A}",
            f"terminal:{AGENT_IDS[0]}",
        ]
        assert page.evaluate("[...terminals.keys()]") == [AGENT_IDS[0], AGENT_IDS[3]]
        assert page.evaluate("selectedAgentGroup") == GROUP_A
        assert page.evaluate("activeTab") == f"agent-group:{GROUP_A}"
        playwright.expect(
            page.locator(".tab-group:not(.agent-group-tab):not([hidden])")
        ).to_have_count(1)
        assert page.locator(".terminal-pane").count() == 2
        assert server.projection == projection
        assert not server.other_mutations
        assert not errors


def test_conflict_refresh_and_offline_failure_keep_family_selection(static_url):  # noqa: F811
    projection = {
        "revision": 7,
        "groups": [{"id": GROUP_A, "name": "Destination"}],
        "assignments": {},
    }
    server = GroupServer(browser_state(projection))
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)
        card(page, 0).get_by_role("checkbox", name="Select family of Agent 0").check()
        server.fail_next = (409, "stale revision from another browser")
        open_group_menu(page)
        page.locator("#grouping-submenu").get_by_role(
            "menuitem", name="Destination", exact=True
        ).click()
        playwright.expect(page.locator("#toast")).to_contain_text("stale revision")
        playwright.expect(page.locator("#group-selection-count")).to_have_text(
            "1 family unit selected"
        )
        assert all(
            card(page, index).evaluate("node => node.classList.contains('is-group-selected')")
            for index in (0, 1, 2)
        )
        assert server.projection["assignments"] == {}

        server.fail_next = (503, "groups backend offline")
        open_group_menu(page)
        page.locator("#grouping-submenu").get_by_role(
            "menuitem", name="Destination", exact=True
        ).click()
        playwright.expect(page.locator("#toast")).to_contain_text("outcome is uncertain")
        playwright.expect(page.locator("#toast")).to_contain_text("Request ID")
        playwright.expect(page.locator("#group-selection-open")).to_be_enabled()
        playwright.expect(page.locator("#group-selection-count")).to_have_text(
            "1 family unit selected"
        )
        assert len(server.posts) == 2
        assert all(post["revision"] == 7 for post in server.posts)
        assert not server.invalid_requests
        assert not server.other_mutations
        assert not errors


def test_external_family_move_keeps_terminal_and_group_dashboards_safe(static_url):  # noqa: F811
    projection = {
        "revision": 4,
        "groups": [
            {"id": GROUP_A, "name": "Group A"},
            {"id": GROUP_B, "name": "Group B"},
        ],
        "assignments": {AGENT_IDS[0]: GROUP_A, AGENT_IDS[3]: GROUP_B},
    }
    server = GroupServer(browser_state(projection))
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)

        # Keep both dashboards open, with A selected and its live root terminal open.
        page.get_by_role("button", name="Open group Group B: 1 agents").click()
        page.locator("#dashboard-tab").click()
        page.get_by_role("button", name="Open group Group A: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_A}"] .tab').click()
        playwright.expect(page.locator(".agent-group-tab")).to_have_count(1)
        assert set(page.evaluate("[...openedAgentGroups]")) == {GROUP_A, GROUP_B}

        # Another process moves the complete active family to B.
        external = copy.deepcopy(projection)
        external["revision"] += 1
        external["assignments"][AGENT_IDS[0]] = GROUP_B
        server.projection = copy.deepcopy(external)
        page.evaluate("value => applyState(value)", server.state_value())

        # A is now empty, but its dashboard and connected terminal are retained.
        assert page.evaluate("selectedAgentGroup") == GROUP_A
        assert page.evaluate("activeTab") == f"agent-group:{GROUP_A}"
        expect_group_name(page, "Group A")
        playwright.expect(page.locator(".agent-group-tab")).to_have_count(1)
        assert page.evaluate("id => terminals.has(id)", AGENT_IDS[0])
        playwright.expect(page.locator(".terminal-pane:not([hidden])")).to_have_count(0)

        page.locator("#dashboard-tab").click()
        playwright.expect(page.locator("#tabs > .agent-group-tab")).to_have_count(0)
        page.get_by_role("button", name="Open group Group B: 4 agents").click()
        expect_group_name(page, "Group B")
        terminal_tab = page.locator(".tab-group:not(.agent-group-tab):not([hidden])")
        playwright.expect(terminal_tab).to_have_count(1)
        playwright.expect(page.locator(".agent-group-tab")).to_have_count(1)
        assert visible_workspace_tabs(page) == [
            "dashboard",
            f"group:{GROUP_B}",
            f"terminal:{AGENT_IDS[0]}",
        ]
        assert page.evaluate("[...terminals.keys()]") == [AGENT_IDS[0]]
        terminal_tab.locator(".tab").click()
        playwright.expect(page.locator(".terminal-pane:not([hidden])")).to_have_count(1)
        assert not server.posts
        assert not errors


def test_group_backend_error_disables_mutations_without_losing_terminal(static_url):  # noqa: F811
    projection = {
        "revision": 2,
        "groups": [{"id": GROUP_A, "name": "Read-only group"}],
        "assignments": {AGENT_IDS[0]: GROUP_A},
    }
    server = GroupServer(browser_state(projection))
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)
        page.get_by_role("button", name="Open group Read-only group: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_A}"] .tab').click()

        damaged = server.state_value()
        damaged["agent_groups_error"] = "Saved groups are corrupt; mutations are unavailable"
        page.evaluate("value => applyState(value)", damaged)
        playwright.expect(page.locator("#agent-groups-error")).to_contain_text(
            "Saved groups are corrupt"
        )
        playwright.expect(page.locator("#agent-group-name-button")).to_be_disabled()
        playwright.expect(page.locator("#agent-group-name-menu")).to_be_hidden()
        playwright.expect(page.locator("#agent-group-rename")).to_be_disabled()
        playwright.expect(page.locator("#agent-group-delete")).to_be_disabled()

        card(page, 0).get_by_role("checkbox", name="Select family of Agent 0").check()
        playwright.expect(page.locator("#group-selection-open")).to_be_disabled()
        card(page, 0).click(button="right")
        playwright.expect(page.locator("#group-context-menu")).to_be_hidden()
        # Dispatch native button clicks without asking Playwright to force hidden
        # controls; disabled menu items must not run their handlers.
        page.evaluate(
            """() => {
                document.querySelector('#agent-group-rename').click();
                document.querySelector('#agent-group-delete').click();
            }"""
        )

        assert page.evaluate("id => terminals.has(id)", AGENT_IDS[0])
        assert page.locator(".terminal-pane").count() == 1
        assert not server.posts
        assert not server.other_mutations
        assert not errors


@pytest.mark.parametrize(
    ("target", "group_id", "group_name"),
    [(0, GROUP_A, "Stopped A"), (3, GROUP_B, "Stopped B")],
)
def test_stopped_root_notification_group_persists_reload(
    static_url,  # noqa: F811
    target,
    group_id,
    group_name,
):
    projection = {
        "revision": 6,
        "groups": [
            {"id": GROUP_A, "name": "Stopped A"},
            {"id": GROUP_B, "name": "Stopped B"},
        ],
        "assignments": {AGENT_IDS[0]: GROUP_A, AGENT_IDS[3]: GROUP_B},
    }
    state = browser_state(projection)
    state["agents"][target]["alive"] = False
    state["agents"][target]["status"] = "idle"
    server = GroupServer(state)
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)
        page.evaluate(
            "([swarmId,agentId]) => revealNotificationAgent({swarmId,agentId})",
            ["local", AGENT_IDS[target]],
        )

        expect_group_name(page, group_name)
        playwright.expect(card(page, target).locator(".goal")).to_be_visible()
        assert page.evaluate("selectedAgentGroup") == group_id
        saved = page.evaluate("JSON.parse(localStorage.getItem('swarm.ui-view-state.local'))")
        assert saved["agentGroupView"]["selected"] == group_id
        assert group_id in saved["agentGroupView"]["opened"]

        page.reload()
        expect_group_name(page, group_name)
        assert page.evaluate("selectedAgentGroup") == group_id
        assert page.evaluate("activeTab") == f"agent-group:{group_id}"
        playwright.expect(card(page, target)).to_be_visible()
        assert not server.posts
        assert not errors


def test_per_swarm_group_views_terminals_and_revisions_are_isolated(static_url):  # noqa: F811
    local_projection = {
        "revision": 10,
        "groups": [{"id": GROUP_A, "name": "Local-only group"}],
        "assignments": {AGENT_IDS[0]: GROUP_A},
    }
    remote_projection = {
        "revision": 20,
        "groups": [{"id": GROUP_B, "name": "Remote-only group"}],
        "assignments": {AGENT_IDS[0]: GROUP_B},
    }
    server = FederationGroupServer(
        browser_state(local_projection), browser_state(remote_projection)
    )
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)
        playwright.expect(page.locator("#swarm-selector")).to_be_visible()
        page.get_by_role("button", name="Open group Local-only group: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_A}"] .tab').click()

        page.locator("#swarm-selector").select_option(FederationGroupServer.REMOTE)
        remote_folder = page.get_by_role("button", name="Open group Remote-only group: 3 agents")
        playwright.expect(remote_folder).to_be_visible()
        remote_folder.click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(f'.agent-group-tab[data-group-id="{GROUP_B}"] .tab').click()

        # A delayed lower-revision projection cannot replace API-visible remote state.
        stale = browser_state(
            {
                "revision": 19,
                "groups": [{"id": GROUP_A, "name": "Stale wrong scope"}],
                "assignments": {AGENT_IDS[0]: GROUP_A},
            }
        )
        page.evaluate("value => applyState(value)", stale)
        expect_group_name(page, "Remote-only group")
        assert page.evaluate("state.agent_groups.revision") == 20

        open_group_name_menu(page)
        page.locator("#swarm-selector").select_option("local")
        playwright.expect(page.locator("#agent-group-name-menu")).to_be_hidden()
        expect_group_name(page, "Local-only group")
        assert page.evaluate("[...terminals.keys()]") == [AGENT_IDS[0]]
        assert page.evaluate("selectedAgentGroup") == GROUP_A

        page.locator("#swarm-selector").select_option(FederationGroupServer.REMOTE)
        expect_group_name(page, "Remote-only group")
        assert page.evaluate("[...terminals.keys()]") == [AGENT_IDS[0]]
        assert page.evaluate("selectedAgentGroup") == GROUP_B
        assert (
            page.evaluate(
                "JSON.parse(localStorage.getItem('swarm.ui-view-state.local')).agentGroupView.selected"
            )
            == GROUP_A
        )
        page.reload()
        expect_group_name(page, "Remote-only group")
        assert page.evaluate("[...terminals.keys()]") == [AGENT_IDS[0]]

        local_saved, remote_saved = page.evaluate(
            """() => [
                JSON.parse(localStorage.getItem('swarm.ui-view-state.local')),
                JSON.parse(localStorage.getItem('swarm.ui-view-state.peer-groups'))
            ]"""
        )
        assert local_saved["agentGroupView"]["selected"] == GROUP_A
        assert remote_saved["agentGroupView"]["selected"] == GROUP_B
        serialized = json.dumps([local_saved, remote_saved])
        assert "Local-only group" not in serialized
        assert "Remote-only group" not in serialized
        assert ("GET", "/api/state") in server.requests
        assert (
            "GET",
            f"/api/federation/{FederationGroupServer.REMOTE}/proxy/api/state",
        ) in server.requests
        assert not server.servers["local"].posts
        assert not server.servers[FederationGroupServer.REMOTE].posts
        assert not errors


@pytest.mark.parametrize("offline", [False, True])
def test_missing_or_offline_group_projection_preserves_view_and_disables_mutations(
    static_url,  # noqa: F811
    offline,  # noqa: F811
):
    projection = {
        "revision": 4,
        "groups": [{"id": GROUP_A, "name": "Retained group"}],
        "assignments": {AGENT_IDS[0]: GROUP_A},
    }
    server = GroupServer(browser_state(projection))
    with open_page(390, server, touch=True) as (page, errors):
        page.goto(static_url)
        page.get_by_role("button", name="Open group Retained group: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        page.locator(".agent-group-tab .tab").click()
        before = page.evaluate('localStorage.getItem("swarm.ui-view-state.local")')
        missing = server.state_value()
        missing.pop("agent_groups", None)
        if offline:
            missing["federation_offline"] = True
        page.evaluate("value=>applyState(value)", missing)
        expect_group_name(page, "Retained group")
        playwright.expect(page.locator("#agent-groups-error")).to_contain_text(
            "Swarm is offline" if offline else "Grouping is unavailable"
        )
        playwright.expect(page.locator("#agent-group-name-button")).to_be_disabled()
        playwright.expect(page.locator("#agent-group-rename")).to_be_disabled()
        card(page, 0).get_by_role("checkbox", name="Select family of Agent 0").check()
        playwright.expect(page.locator("#group-selection-open")).to_be_disabled()
        assert page.evaluate('localStorage.getItem("swarm.ui-view-state.local")') == before
        assert page.evaluate("id=>terminals.has(id)", AGENT_IDS[0])
        page.evaluate("value=>applyState(value)", server.state_value())
        playwright.expect(page.locator("#agent-group-name-button")).to_be_enabled()
        playwright.expect(page.locator("#agent-group-rename")).to_be_enabled()
        assert not server.posts and not errors


def test_external_active_terminal_move_persists_the_visible_dashboard_on_reload(static_url):  # noqa: F811
    projection = {
        "revision": 1,
        "groups": [{"id": GROUP_A, "name": "A"}, {"id": GROUP_B, "name": "B"}],
        "assignments": {AGENT_IDS[0]: GROUP_A},
    }
    server = GroupServer(browser_state(projection))
    with open_page(1280, server) as (page, errors):
        page.goto(static_url)
        page.get_by_role("button", name="Open group A: 3 agents").click()
        card(page, 0).locator(".card-terminal").click()
        server.projection["assignments"] = {AGENT_IDS[index]: GROUP_B for index in (0, 1, 2)}
        server.projection["revision"] = 2
        page.evaluate("value=>applyState(value)", server.state_value())
        playwright.expect(page.locator("#dashboard")).to_be_visible()
        saved = page.evaluate('JSON.parse(localStorage.getItem("swarm.ui-view-state.local"))')
        assert saved["terminalTabs"]["active"] == "agent-group:" + GROUP_A
        assert page.evaluate("id=>terminals.has(id)", AGENT_IDS[0])
        page.reload()
        expect_group_name(page, "A")
        playwright.expect(page.locator("#terminals")).to_be_hidden()
        assert page.evaluate("id=>terminals.has(id)", AGENT_IDS[0])
        assert not errors
