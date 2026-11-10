"""Browser integration coverage for browser-local and server-managed grouping.

Every HTTP and websocket dependency is synthetic.  The fixture deliberately keeps
one revision token for groups and grouping settings, as the real grouping store
does, so mode changes race with group and branch changes realistically.
"""

import copy
import json
from contextlib import contextmanager
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
    click_group_name_action,
    expect_group_name,
    open_group_menu,
)
from test_dashboard_browser import no_auth_mock, static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

LOCAL_KEY = "swarm.browser-groups.local.%2Ffixture"


def settings(revision=0, *, server_managed=False, initialized=False, branches=None):
    return {
        "revision": revision,
        "server_managed": server_managed,
        "initialized": initialized,
        "grouped_branches": list(branches or []),
    }


class GroupingModeServer(GroupServer):
    """Stateful mock for the shared groups/settings revision domain."""

    def __init__(self, state, grouping_settings=None, *, expose_settings=True):
        super().__init__(state)
        self.settings = copy.deepcopy(
            grouping_settings
            or settings(self.projection["revision"], server_managed=False, initialized=False)
        )
        self.projection["revision"] = self.settings["revision"]
        self.state["agent_groups"] = copy.deepcopy(self.projection)
        self.expose_settings = expose_settings
        self.settings_gets = []
        self.settings_posts = []
        self.requests = []
        self.fail_group_request = None

    def state_value(self):
        value = super().state_value()
        if self.expose_settings:
            value["grouping_settings"] = copy.deepcopy(self.settings)
            value["grouping_settings_error"] = None
        return value

    def _advance(self):
        revision = self.settings["revision"] + 1
        self.settings["revision"] = revision
        self.projection["revision"] = revision
        self.state["agent_groups"] = copy.deepcopy(self.projection)

    def mutate(self, body):
        # GroupServer performs all family expansion and operation shaping.  Make
        # its increment start at the globally shared settings revision.
        self.projection["revision"] = self.settings["revision"]
        value = super().mutate(body)
        self.settings["revision"] = self.projection["revision"]
        return value

    def mutate_settings(self, body):
        assert REQUEST_ID.fullmatch(body.get("request_id", ""))
        UUID(body["request_id"])
        assert body["revision"] == self.settings["revision"]
        action = body.get("action")
        if action == "configure":
            assert set(body) <= {
                "action",
                "revision",
                "request_id",
                "server_managed",
                "seed",
            }
            desired = body["server_managed"]
            seed = body.get("seed")
            if seed is not None:
                assert desired and not self.settings["initialized"]
                assert set(seed) == {"groups", "assignments", "grouped_branches"}
                self.projection = {
                    "revision": self.settings["revision"],
                    "groups": copy.deepcopy(seed["groups"]),
                    "assignments": copy.deepcopy(seed["assignments"]),
                }
                self.settings["grouped_branches"] = list(seed["grouped_branches"])
            self.settings["server_managed"] = desired
            self.settings["initialized"] = self.settings["initialized"] or desired
        elif action == "branches":
            assert self.settings["server_managed"]
            assert set(body) == {
                "action",
                "revision",
                "request_id",
                "grouped_branches",
            }
            assert len(body["grouped_branches"]) == len(set(body["grouped_branches"]))
            assert all(UUID(value) for value in body["grouped_branches"])
            self.settings["grouped_branches"] = list(body["grouped_branches"])
        else:
            raise AssertionError(f"unexpected grouping-settings action: {action}")
        self._advance()
        return copy.deepcopy(self.settings)

    def external_group(self, name):
        """Model another browser changing canonical groups and the shared token."""
        group_id = GROUP_B
        self.projection["groups"] = [{"id": group_id, "name": name}]
        self.projection["assignments"] = {AGENT_IDS[3]: group_id}
        self._advance()

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        self.requests.append((method, path))
        if path == "/api/grouping-settings" and method == "GET":
            self.settings_gets.append(path)
            if not self.expose_settings:
                return route.fulfill(
                    status=404,
                    content_type="application/json",
                    body='{"detail":"not found"}',
                )
            value = copy.deepcopy(self.settings)
        elif path == "/api/grouping-settings" and method == "POST":
            body = request.post_data_json
            self.settings_posts.append(copy.deepcopy(body))
            try:
                value = self.mutate_settings(body)
                status = 200
            except (AssertionError, KeyError, TypeError, ValueError) as exc:
                value, status = {"detail": f"invalid settings request: {exc}"}, 409
            return route.fulfill(
                status=status, content_type="application/json", body=json.dumps(value)
            )
        elif path == "/api/groups" and method == "POST" and self.fail_group_request:
            body = request.post_data_json
            self.posts.append(copy.deepcopy(body))
            failure, self.fail_group_request = self.fail_group_request, None
            failure()
            return route.fulfill(
                status=409,
                content_type="application/json",
                body='{"detail":"Group revision changed; refresh and retry"}',
            )
        else:
            return super().route(route)
        route.fulfill(content_type="application/json", body=json.dumps(value))


class FederationModeServer:
    """Expose destination A through gateway B while retaining B's own state."""

    REMOTE = "owner-a"

    def __init__(self, local, remote):
        self.local = local
        self.remote = remote
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
                        "name": "Owning A",
                        "online": True,
                        "capabilities": {"remote_restart": False},
                    }
                ],
            }
            return route.fulfill(content_type="application/json", body=json.dumps(value))
        if path == "/api/federation/settings" and method == "GET":
            value = {
                "enabled": True,
                "connected": True,
                "status": "connected",
                "has_api_key": True,
                "relay_url": "",
                "relay_node_id": "gateway-b",
                "relay_name": "Gateway B",
            }
            return route.fulfill(content_type="application/json", body=json.dumps(value))
        if path.startswith(prefix):
            original = request.url
            scoped = path[len(prefix) :]

            # ModeServer intentionally keys only on URL path; provide a tiny
            # request/route adapter so every remote groups/settings call is
            # exercised through the captured proxy prefix.
            class Request:
                url = original.split(path, 1)[0] + scoped
                method = request.method
                post_data = request.post_data
                post_data_json = request.post_data_json

            class Route:
                request = Request()

                @staticmethod
                def fulfill(**kwargs):
                    route.fulfill(**kwargs)

            return self.remote.route(Route())
        return self.local.route(route)


@contextmanager
def open_pages(width, server, *, count=1, init_scripts=(), shared_context=False):
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(
                exc
            ) or "error while loading shared libraries" in str(exc):
                pytest.skip(f"Optional Chromium runtime unavailable: {exc}")
            raise
        contexts, pages, errors = [], [], []
        try:
            shared = None
            for index in range(count):
                context = shared
                if context is None:
                    context = browser.new_context(
                        viewport={"width": width, "height": 900},
                        has_touch=width == 390,
                        is_mobile=width == 390,
                    )
                    contexts.append(context)
                    if shared_context:
                        shared = context
                page = context.new_page()
                page_errors = []
                page.on("pageerror", lambda exc, target=page_errors: target.append(str(exc)))
                page.add_init_script(
                    """window.WebSocket = class {
                        static OPEN = 1;
                        constructor(url) { this.url=url; this.readyState=1; setTimeout(()=>this.onopen?.(),0); }
                        close() { this.readyState=3; }
                        send() {}
                    };"""
                )
                if index < len(init_scripts) and init_scripts[index]:
                    page.add_init_script(init_scripts[index])
                page.route("**/api/**", no_auth_mock(server.route))
                pages.append(page)
                errors.append(page_errors)
            yield pages, errors
        finally:
            for context in contexts:
                context.close()
            browser.close()


def mode_state(projection=None, grouping_settings=None):
    state = browser_state(projection)
    state["grouping_settings"] = copy.deepcopy(
        grouping_settings
        or settings((projection or {"revision": 0})["revision"], server_managed=False)
    )
    state["grouping_settings_error"] = None
    return state


def close_settings(page):
    settings = page.locator("#settings-dialog")
    if settings.get_attribute("open") is not None:
        settings.get_by_role("button", name="Close", exact=True).click()


def open_mode_panel(page):
    close_settings(page)
    page.locator("#notifications-button").click()
    playwright.expect(page.locator("#notifications-dialog")).to_be_visible()
    page.locator("#settings-button").click()
    settings = page.locator("#settings-dialog")
    playwright.expect(settings).to_be_visible()
    return settings


def select_family_and_create(page, index, name):
    card(page, index).get_by_role("checkbox", name=f"Select family of Agent {index}").check()
    open_group_menu(page)
    page.once("dialog", lambda dialog: dialog.accept(name))
    page.locator("#grouping-submenu").get_by_role("menuitem", name="New group…").click()


@pytest.mark.parametrize("width", [390, 1280])
def test_browser_local_prime_copies_groups_families_and_branches(static_url, width):  # noqa: F811
    unsafe = 'Private <img src=x onerror="window.modeUnsafe=true">'
    server = GroupingModeServer(mode_state())
    with open_pages(width, server) as ([page], [errors]):
        page.goto(static_url)
        playwright.expect(page.locator(".agent-card")).to_have_count(5)

        # Branch collapse is browser UI state in local mode, while group writes
        # are persisted only in this browser's scoped record.
        page.locator(f'.org-branch-toggle[data-agent-id="{AGENT_IDS[0]}"]').click()
        select_family_and_create(page, 0, unsafe)
        assert not server.posts
        assert not [item for item in server.requests if item == ("POST", "/api/groups")]
        saved = page.evaluate("key => JSON.parse(localStorage.getItem(key))", LOCAL_KEY)
        assert set(saved) == {"version", "revision", "groups", "assignments", "receipts"}
        assert saved["version"] == 1
        assert saved["groups"][0]["name"] == unsafe
        assert set(saved["assignments"]) == set(AGENT_IDS[:3])

        panel = open_mode_panel(page)
        playwright.expect(panel.locator("#grouping-mode-owner")).to_have_text(
            "Owning server: Local Swarm"
        )
        assert "Manage this Swarm's grouping on its server" in panel.inner_text()
        assert not panel.locator("img,script").count()
        assert not page.evaluate("window.modeUnsafe === true")
        panel.locator("#grouping-server-managed").check()
        panel.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_visible()
        # Opening the first-enable prompt closes Settings (and the inbox remains closed).
        playwright.expect(page.locator("#settings-dialog")).to_be_hidden()
        playwright.expect(page.locator("#notifications-dialog")).to_be_hidden()
        prime_text = page.locator("#grouping-prime-dialog").inner_text()
        assert "whole-family memberships" in prime_text
        assert "No agents or terminal sessions are stopped, woken or deleted" in prime_text
        page.locator("#grouping-prime-current").click()
        close_settings(page)

        playwright.expect(
            page.get_by_role("button", name=f"Open group {unsafe}: 3 agents")
        ).to_be_visible()
        post = server.settings_posts[-1]
        assert post["action"] == "configure" and post["server_managed"] is True
        UUID(post["request_id"])
        assert post["seed"]["groups"] == saved["groups"]
        assert set(post["seed"]["assignments"]) == set(AGENT_IDS[:3])
        assert post["seed"]["grouped_branches"] == [AGENT_IDS[0]]
        assert set(post["seed"]) == {"groups", "assignments", "grouped_branches"}
        assert "receipts" not in post["seed"] and "version" not in post["seed"]
        assert server.settings["server_managed"] is True
        assert server.settings["initialized"] is True
        assert server.projection["groups"] == saved["groups"]
        assert not server.posts and not server.other_mutations
        assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
        assert not errors


def test_same_context_storage_sync_and_prime_revalidates_latest_full_snapshot(
    static_url,  # noqa: F811
):
    canonical = {
        "revision": 4,
        "groups": [{"id": GROUP_A, "name": "Original local"}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    server = GroupingModeServer(mode_state(canonical, settings(4)))
    with open_pages(1280, server, count=2, shared_context=True) as (pages, all_errors):
        first, second = pages
        first.goto(static_url)
        second.goto(static_url)
        for page in pages:
            playwright.expect(
                page.get_by_role("button", name="Open group Original local: 1 agents")
            ).to_be_visible()

        # A local mutation in one tab is committed under the origin-wide Web
        # Lock.  The other tab consumes the storage event and repaints its
        # dashboard folder instead of retaining its startup projection.
        first.get_by_role("button", name="Open group Original local: 1 agents").click()
        first.once("dialog", lambda dialog: dialog.accept("Latest shared local"))
        click_group_name_action(first, "rename")
        expect_group_name(first, "Latest shared local")
        playwright.expect(
            second.get_by_role("button", name="Open group Latest shared local: 1 agents")
        ).to_be_visible()

        # The first snapshot shown by the first-enable flow is the latest full
        # shared record, not a projection cached before the storage event.
        open_mode_panel(first)
        first.locator("#grouping-server-managed").check()
        first.locator("#grouping-mode-change").click()
        playwright.expect(first.locator("#grouping-prime-dialog")).to_be_visible()
        playwright.expect(first.locator("#grouping-prime-summary")).to_contain_text(
            "1 current UI group and 1 agent assignment"
        )

        # Change that same record from tab two while the dialog is open.  Seed
        # revalidation must cancel without a POST and surface the stale-source
        # status rather than replacing canonical state with the old snapshot.
        second.get_by_role("button", name="Open group Latest shared local: 1 agents").click()
        second.once("dialog", lambda dialog: dialog.accept("Changed during prime"))
        click_group_name_action(second, "rename")
        expect_group_name(second, "Changed during prime")
        first.locator("#grouping-prime-current").click()
        close_settings(first)
        playwright.expect(first.locator("#grouping-prime-dialog")).to_be_hidden()
        playwright.expect(first.locator("#toast")).to_contain_text(
            "Source grouping changed while the dialog was open"
        )
        assert not server.settings_posts
        assert server.projection == canonical

        # Reopening captures the new full shared snapshot and can now seed it.
        open_mode_panel(first)
        first.locator("#grouping-server-managed").check()
        first.locator("#grouping-mode-change").click()
        playwright.expect(first.locator("#grouping-prime-dialog")).to_be_visible()
        first.locator("#grouping-prime-current").click()
        close_settings(first)
        playwright.expect(first.locator("#grouping-mode-status")).to_contain_text(
            "Server-managed grouping is on"
        )
        expect_group_name(first, "Changed during prime")
        assert len(server.settings_posts) == 1
        post = server.settings_posts[0]
        assert post["seed"] == {
            "groups": [{"id": GROUP_A, "name": "Changed during prime"}],
            "assignments": {AGENT_IDS[3]: GROUP_A},
            "grouped_branches": [],
        }
        assert server.projection["groups"] == post["seed"]["groups"]
        assert server.projection["assignments"] == post["seed"]["assignments"]
        assert all(not errors for errors in all_errors)


def test_same_context_web_locks_reject_one_same_revision_write_without_loss(
    static_url,  # noqa: F811
):
    canonical = {
        "revision": 8,
        "groups": [{"id": GROUP_A, "name": "Before concurrent writes"}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    server = GroupingModeServer(mode_state(canonical, settings(8)))
    with open_pages(1280, server, count=2, shared_context=True) as (pages, all_errors):
        first, second = pages
        for page in pages:
            page.goto(static_url)
            playwright.expect(
                page.get_by_role("button", name="Open group Before concurrent writes: 1 agents")
            ).to_be_visible()

        bodies = [
            {
                "action": "rename",
                "revision": 8,
                "request_id": "30000000-0000-4000-8000-000000000001",
                "group_id": GROUP_A,
                "name": "Concurrent winner one",
            },
            {
                "action": "rename",
                "revision": 8,
                "request_id": "30000000-0000-4000-8000-000000000002",
                "group_id": GROUP_A,
                "name": "Concurrent winner two",
            },
        ]
        start = first.evaluate("Date.now()") + 500
        arguments = {
            "key": LOCAL_KEY,
            "agents": server.state["agents"],
            "seed": canonical,
            "start": start,
        }
        for page, body in zip(pages, bodies):
            page.evaluate(
                """args => {
                    window.lockedApplyResult = null;
                    const store = (typeof browserGroups !== 'undefined' && browserGroups)
                        || new SwarmGroupingMode.BrowserGroups();
                    setTimeout(async () => {
                        try {
                            const value = await store.applyLocked(
                                args.key, args.body, args.agents, args.seed,
                                {recordsValid: true});
                            window.lockedApplyResult = {ok: true, value};
                        } catch (error) {
                            window.lockedApplyResult = {
                                ok: false, status: error.status || null,
                                message: error.message,
                            };
                        }
                    }, Math.max(0, args.start - Date.now()));
                }""",
                {**arguments, "body": body},
            )
        for page in pages:
            page.wait_for_function("window.lockedApplyResult !== null")
        results = [page.evaluate("window.lockedApplyResult") for page in pages]

        assert sum(result["ok"] for result in results) == 1
        rejected = next(result for result in results if not result["ok"])
        assert rejected["status"] == 409
        assert "revision changed" in rejected["message"]
        saved = first.evaluate("key => JSON.parse(localStorage.getItem(key))", LOCAL_KEY)
        winner = next(result for result in results if result["ok"])["value"]
        assert saved["revision"] == 9
        assert saved["groups"] == winner["groups"]
        assert saved["assignments"] == canonical["assignments"]
        assert len(saved["receipts"]) == 1
        assert not server.posts and not server.settings_posts
        assert all(not errors for errors in all_errors)


def test_group_conflict_refresh_keeps_concurrent_branch_winner(static_url):  # noqa: F811
    canonical = {
        "revision": 5,
        "groups": [{"id": GROUP_A, "name": "Winning canonical group"}],
        "assignments": {AGENT_IDS[0]: GROUP_A},
    }
    managed = settings(5, server_managed=True, initialized=True)
    server = GroupingModeServer(mode_state(canonical, managed), managed)

    def win_branch_race():
        server.settings["grouped_branches"] = [AGENT_IDS[0]]
        server._advance()

    with open_pages(1280, server) as ([page], [errors]):
        page.goto(static_url)
        page.get_by_role("button", name="Open group Winning canonical group: 3 agents").click()
        server.fail_group_request = win_branch_race
        page.once("dialog", lambda dialog: dialog.accept("Losing browser rename"))
        click_group_name_action(page, "rename")

        playwright.expect(page.locator("#toast")).to_contain_text("Group revision changed")
        page.wait_for_function(
            "id => groupedBranches.has(id) && state.agent_groups.revision === 6",
            arg=AGENT_IDS[0],
        )
        expect_group_name(page, "Winning canonical group")
        assert server.posts[-1]["revision"] == 5
        assert server.projection == canonical | {"revision": 6}
        assert server.settings["revision"] == 6
        assert server.settings["grouped_branches"] == [AGENT_IDS[0]]
        assert page.evaluate("state.grouping_settings.grouped_branches") == [AGENT_IDS[0]]
        assert not server.invalid_requests and not server.other_mutations and not errors


def test_first_enable_cancel_and_concurrent_state_then_keep_are_lossless(static_url):  # noqa: F811
    canonical = {
        "revision": 4,
        "groups": [{"id": GROUP_A, "name": "Canonical old"}],
        "assignments": {AGENT_IDS[0]: GROUP_A},
    }
    server = GroupingModeServer(mode_state(canonical, settings(4)))
    with open_pages(1280, server) as ([page], [errors]):
        page.goto(static_url)
        open_mode_panel(page)
        page.locator("#grouping-server-managed").check()
        page.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_visible()
        page.locator("#grouping-prime-cancel").click()
        close_settings(page)
        assert not server.settings_posts

        # A group update from another browser advances the same revision and
        # closes a stale first-enable prompt before it can post a mode change.
        open_mode_panel(page)
        page.locator("#grouping-server-managed").check()
        page.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_visible()
        server.external_group("Concurrent canonical")
        page.evaluate("value => applyState(value)", server.state_value())
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_hidden()
        playwright.expect(page.locator("#grouping-mode-status")).to_contain_text(
            "changed while the dialog was open"
        )
        assert not server.settings_posts
        assert server.projection["groups"][0]["name"] == "Concurrent canonical"

        # Keeping saved state sends no seed and therefore cannot replace the
        # canonical group created concurrently.
        open_mode_panel(page)
        page.locator("#grouping-server-managed").check()
        page.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_visible()
        page.locator("#grouping-prime-server").click()
        close_settings(page)
        playwright.expect(
            page.get_by_role("button", name="Open group Concurrent canonical: 1 agents")
        ).to_be_visible()
        assert len(server.settings_posts) == 1
        assert "seed" not in server.settings_posts[0]
        assert server.projection["groups"] == [{"id": GROUP_B, "name": "Concurrent canonical"}]
        assert server.settings["initialized"] is True
        assert not server.other_mutations and not errors


def test_disable_local_edits_and_reenable_preserve_canonical_server_state(static_url):  # noqa: F811
    canonical = {
        "revision": 7,
        "groups": [{"id": GROUP_A, "name": "Canonical team"}],
        "assignments": {agent_id: GROUP_A for agent_id in AGENT_IDS[:3]},
    }
    server = GroupingModeServer(
        mode_state(canonical, settings(7, server_managed=True, initialized=True)),
        settings(7, server_managed=True, initialized=True),
    )
    with open_pages(390, server) as ([page], [errors]):
        page.goto(static_url)
        page.get_by_role("button", name="Open group Canonical team: 3 agents").click()
        open_mode_panel(page)
        page.locator("#grouping-server-managed").uncheck()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-mode-status")).to_contain_text(
            "Browser-local grouping is on"
        )
        close_settings(page)

        # Rename and branch collapse now stay browser-local and do not touch
        # either server endpoint or the canonical projection.
        page.once("dialog", lambda dialog: dialog.accept("My browser name"))
        click_group_name_action(page, "rename")
        page.locator(f'.org-branch-toggle[data-agent-id="{AGENT_IDS[0]}"]').click()
        expect_group_name(page, "My browser name")
        assert not server.posts
        assert len(server.settings_posts) == 1
        assert server.projection["groups"][0]["name"] == "Canonical team"
        local = page.evaluate("key => JSON.parse(localStorage.getItem(key))", LOCAL_KEY)
        assert local["groups"][0]["name"] == "My browser name"

        # initialized is durable: re-enable has a confirmation, not a prime
        # dialog, and sends no seed.  The canonical server display returns.
        open_mode_panel(page)
        page.locator("#grouping-server-managed").check()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_hidden()
        playwright.expect(page.locator("#grouping-mode-status")).to_contain_text(
            "Server-managed grouping is on"
        )
        close_settings(page)
        expect_group_name(page, "Canonical team")
        assert len(server.settings_posts) == 2
        assert "seed" not in server.settings_posts[-1]
        assert server.settings["initialized"] is True
        assert server.projection["groups"][0]["name"] == "Canonical team"
        assert not server.posts and not server.other_mutations
        assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
        assert not errors


@pytest.mark.parametrize("storage_case", ["corrupt", "blocked"])
def test_bad_browser_storage_can_escape_to_server_but_offline_still_disables_mode(
    static_url,  # noqa: F811
    storage_case,  # noqa: F811
):
    canonical = {
        "revision": 3,
        "groups": [{"id": GROUP_A, "name": "Never overwrite"}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    server = GroupingModeServer(mode_state(canonical, settings(3)))
    if storage_case == "corrupt":
        init = f"localStorage.setItem({json.dumps(LOCAL_KEY)}, '{{broken');"
    else:
        init = "Object.defineProperty(window, 'localStorage', {get(){throw Error('storage blocked')}});"
    with open_pages(1280, server, init_scripts=(init,)) as ([page], [errors]):
        page.goto(static_url)
        playwright.expect(page.locator("#agent-groups-error")).to_be_visible()
        playwright.expect(page.locator("#agent-groups-error")).to_contain_text(
            "browser groups" if storage_case == "corrupt" else "storage blocked"
        )
        playwright.expect(page.locator("#group-selection-open")).to_be_disabled()

        # A broken local source must not trap the user in local mode.  The mode
        # control remains authoritative and the first-enable prompt offers the
        # lossless server escape, but cannot offer an unsafe seed.
        open_mode_panel(page)
        playwright.expect(page.locator("#grouping-server-managed")).to_be_enabled()
        page.locator("#grouping-server-managed").check()
        page.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_visible()
        playwright.expect(page.locator("#grouping-prime-current")).to_be_disabled()
        playwright.expect(page.locator("#grouping-prime-server")).to_be_enabled()
        page.locator("#grouping-prime-server").click()
        close_settings(page)
        playwright.expect(
            page.get_by_role("button", name="Open group Never overwrite: 1 agents")
        ).to_be_visible()
        assert len(server.settings_posts) == 1
        assert "seed" not in server.settings_posts[0]
        assert server.settings["server_managed"] is True
        assert server.settings["initialized"] is True
        assert server.projection["groups"] == canonical["groups"]
        assert server.projection["assignments"] == canonical["assignments"]
        if storage_case == "corrupt":
            assert page.evaluate("key => localStorage.getItem(key)", LOCAL_KEY) == "{broken"

        # Actual owner unavailability still disables both mode changes and
        # edits, unlike a local-storage failure.
        damaged = server.state_value()
        damaged["federation_offline"] = True
        page.evaluate("value => applyState(value)", damaged)
        open_mode_panel(page)
        playwright.expect(page.locator("#grouping-mode-status")).to_contain_text(
            "Grouping mode unavailable"
        )
        playwright.expect(page.locator("#grouping-server-managed")).to_be_disabled()
        close_settings(page)
        card(page, 0).get_by_role("checkbox", name="Select family of Agent 0").check()
        playwright.expect(page.locator("#group-selection-open")).to_be_disabled()
        assert not server.posts
        assert server.projection["groups"] == canonical["groups"]
        assert server.projection["assignments"] == canonical["assignments"]
        assert not server.other_mutations
        # A deliberately blocked Storage accessor is handled as UI state, not
        # an uncaught page exception.
        assert not errors


def test_legacy_server_managed_uninitialized_primes_canonical_and_local_branch_ui(
    static_url,  # noqa: F811
):
    canonical = {
        "revision": 6,
        "groups": [{"id": GROUP_A, "name": "Legacy v1 canonical"}],
        "assignments": {AGENT_IDS[0]: GROUP_A},
    }
    legacy = settings(6, server_managed=True, initialized=False)
    server = GroupingModeServer(mode_state(canonical, legacy), legacy)
    with open_pages(1280, server) as ([page], [errors]):
        page.goto(static_url)
        playwright.expect(
            page.get_by_role("button", name="Open group Legacy v1 canonical: 3 agents")
        ).to_be_visible()

        # Before the v1 server has been initialized, branch collapse remains UI
        # state and is included with canonical groups and effective family
        # assignments in the one-time full prime snapshot.
        page.get_by_role("button", name="Open group Legacy v1 canonical: 3 agents").click()
        page.locator(f'.org-branch-toggle[data-agent-id="{AGENT_IDS[0]}"]').click()
        assert not server.settings_posts
        panel = open_mode_panel(page)
        playwright.expect(panel.locator("#grouping-server-managed")).to_be_checked()
        panel.locator("#grouping-mode-change").click()
        playwright.expect(page.locator("#grouping-prime-dialog")).to_be_visible()
        playwright.expect(page.locator("#grouping-prime-current")).to_be_enabled()
        page.locator("#grouping-prime-current").click()
        close_settings(page)

        assert len(server.settings_posts) == 1
        post = server.settings_posts[0]
        assert post["server_managed"] is True
        assert post["seed"] == {
            "groups": canonical["groups"],
            "assignments": {agent_id: GROUP_A for agent_id in AGENT_IDS[:3]},
            "grouped_branches": [AGENT_IDS[0]],
        }
        assert server.settings["initialized"] is True
        assert server.settings["grouped_branches"] == [AGENT_IDS[0]]
        assert server.projection["groups"] == canonical["groups"]
        assert server.projection["assignments"] == {agent_id: GROUP_A for agent_id in AGENT_IDS[:3]}
        assert not server.posts and not server.other_mutations and not errors


def test_malformed_settings_lock_edits_but_missing_legacy_settings_allow_groups(
    static_url,  # noqa: F811
):
    canonical = {
        "revision": 2,
        "groups": [{"id": GROUP_A, "name": "Existing canonical"}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    malformed_state = browser_state(canonical)
    malformed_state["grouping_settings"] = settings(2) | {"server_managed": "yes"}
    malformed_state["grouping_settings_error"] = None
    malformed = GroupServer(malformed_state)
    with open_pages(1280, malformed) as ([page], [errors]):
        page.goto(static_url)
        open_mode_panel(page)
        playwright.expect(page.locator("#grouping-mode-status")).to_contain_text("invalid")
        playwright.expect(page.locator("#grouping-server-managed")).to_be_disabled()
        close_settings(page)
        card(page, 0).get_by_role("checkbox", name="Select family of Agent 0").check()
        playwright.expect(page.locator("#group-selection-open")).to_be_disabled()
        assert not malformed.posts and not malformed.other_mutations and not errors

    legacy = GroupingModeServer(browser_state(canonical), expose_settings=False)
    with open_pages(1280, legacy) as ([page], [errors]):
        page.goto(static_url)
        open_mode_panel(page)
        playwright.expect(page.locator("#grouping-server-managed")).to_be_disabled()
        close_settings(page)
        select_family_and_create(page, 4, "Legacy client group")
        playwright.expect(
            page.get_by_role("button", name="Open group Legacy client group: 1 agents")
        ).to_be_visible()
        assert legacy.posts[-1]["revision"] == 2
        assert not legacy.invalid_requests and not legacy.other_mutations and not errors


def test_delayed_browser_load_cannot_overwrite_newer_local_mutation(static_url):  # noqa: F811
    canonical = {
        "revision": 4,
        "groups": [{"id": GROUP_A, "name": "Before local mutation"}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    server = GroupingModeServer(mode_state(canonical, settings(4)))
    with open_pages(1280, server) as ([page], [errors]):
        page.goto(static_url)
        playwright.expect(
            page.get_by_role("button", name="Open group Before local mutation: 1 agents")
        ).to_be_visible()
        loaded = page.evaluate("() => structuredClone(browserGroupingProjection)")
        page.evaluate(
            """value => {
                const original = browserGroups.loadLocked.bind(browserGroups);
                window.delayedBrowserLoadStarted = false;
                window.releaseDelayedBrowserLoad = null;
                browserGroups.loadLocked = (...args) => {
                    window.delayedBrowserLoadStarted = true;
                    return new Promise(resolve => {
                        window.releaseDelayedBrowserLoad = () => {
                            browserGroups.loadLocked = original;
                            resolve(structuredClone(value));
                        };
                    });
                };
            }""",
            loaded,
        )
        page.evaluate("value => applyState(value)", server.state_value())
        page.wait_for_function("window.delayedBrowserLoadStarted")

        page.get_by_role("button", name="Open group Before local mutation: 1 agents").click()
        page.once("dialog", lambda dialog: dialog.accept("Newer local mutation"))
        click_group_name_action(page, "rename")
        expect_group_name(page, "Newer local mutation")
        page.evaluate("window.releaseDelayedBrowserLoad()")
        page.wait_for_timeout(100)

        expect_group_name(page, "Newer local mutation")
        saved = page.evaluate("key => JSON.parse(localStorage.getItem(key))", LOCAL_KEY)
        assert saved["revision"] == 5
        assert saved["groups"] == [{"id": GROUP_A, "name": "Newer local mutation"}]
        assert page.evaluate("browserGroupingProjection.revision") == 5
        assert page.evaluate("browserGroupingProjection.groups[0].name") == "Newer local mutation"
        assert not server.posts and not server.settings_posts and not errors


def test_missing_settings_is_legacy_server_mode_and_cannot_toggle(static_url):  # noqa: F811
    canonical = {
        "revision": 2,
        "groups": [{"id": GROUP_A, "name": "Legacy canonical"}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    server = GroupingModeServer(browser_state(canonical), expose_settings=False)
    with open_pages(1280, server) as ([page], [errors]):
        page.goto(static_url)
        playwright.expect(
            page.get_by_role("button", name="Open group Legacy canonical: 1 agents")
        ).to_be_visible()
        open_mode_panel(page)
        playwright.expect(page.locator("#grouping-mode-status")).to_contain_text("not supported")
        playwright.expect(page.locator("#grouping-server-managed")).to_be_disabled()
        assert page.evaluate("serverManagedGrouping()") is True
        assert not server.settings_posts and not server.posts and not errors


def test_remote_owner_paths_and_mode_update_are_shared_but_browser_views_are_not(
    static_url,  # noqa: F811
):
    local_projection = {
        "revision": 5,
        "groups": [{"id": GROUP_A, "name": "Gateway B local"}],
        "assignments": {AGENT_IDS[3]: GROUP_A},
    }
    remote_projection = {
        "revision": 9,
        "groups": [{"id": GROUP_B, "name": "Owner A canonical"}],
        "assignments": {AGENT_IDS[0]: GROUP_B},
    }
    local = GroupingModeServer(mode_state(local_projection, settings(5)))
    remote = GroupingModeServer(mode_state(remote_projection, settings(9)))
    federation = FederationModeServer(local, remote)
    with open_pages(1280, federation, count=2) as (pages, all_errors):
        first, second = pages
        first.goto(static_url)
        second.goto(static_url)
        for page in pages:
            page.locator("#swarm-selector").select_option(FederationModeServer.REMOTE)
            playwright.expect(
                page.get_by_role("button", name="Open group Owner A canonical: 3 agents")
            ).to_be_visible()

        # Keep a view/tab only in browser two; browser one changes owner A's
        # mode through gateway B and must not mutate B's local grouping.
        second.get_by_role("button", name="Open group Owner A canonical: 3 agents").click()
        before_view = second.evaluate(
            "JSON.parse(localStorage.getItem('swarm.ui-view-state.owner-a'))"
        )
        open_mode_panel(first)
        playwright.expect(first.locator("#grouping-mode-owner")).to_have_text(
            "Owning server: Owning A"
        )
        first.locator("#grouping-server-managed").check()
        first.locator("#grouping-mode-change").click()
        playwright.expect(first.locator("#grouping-prime-dialog")).to_be_visible()
        first.locator("#grouping-prime-server").click()
        close_settings(first)
        playwright.expect(first.locator("#grouping-mode-status")).to_contain_text(
            "Server-managed grouping is on"
        )

        # Browser two receives the next canonical poll/websocket state.  The
        # mode is shared, but its selected group/tab remains its local view.
        second.evaluate("value => applyState(value)", remote.state_value())
        open_mode_panel(second)
        playwright.expect(second.locator("#grouping-server-managed")).to_be_checked()
        after_view = second.evaluate(
            "JSON.parse(localStorage.getItem('swarm.ui-view-state.owner-a'))"
        )
        assert after_view["agentGroupView"] == before_view["agentGroupView"]

        prefix = f"/api/federation/{FederationModeServer.REMOTE}/proxy"
        remote_calls = [path for _, path in federation.requests if path.startswith(prefix)]
        assert f"{prefix}/api/state" in remote_calls
        assert f"{prefix}/api/grouping-settings" in remote_calls
        assert all(
            path.startswith(prefix)
            for method, path in federation.requests
            if method == "POST" and path.endswith(("/api/groups", "/api/grouping-settings"))
        )
        assert remote.settings["server_managed"] is True
        assert local.settings == settings(5)
        assert local.projection == local_projection
        assert not local.posts and not local.settings_posts
        assert not remote.posts
        assert all(not errors for errors in all_errors)
