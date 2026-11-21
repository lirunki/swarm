"""Real-browser coverage for family-scoped bulk archive and forget actions.

Every mutation is handled by :class:`BulkArchiveServer`; these tests never create an
archive, inspect Linux processes, or invoke stop/wake/tool/Git endpoints.
"""

from __future__ import annotations

import copy
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

import pytest
from test_agent_groups_browser import (
    AGENT_IDS,
    FederationGroupServer,
    GroupServer,
    agent,
    browser_state,
)
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    return _dashboard_static_url


LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")
ARCHIVE_ID = "90000000-0000-4000-8000-000000000001"
MUTATION = re.compile(
    r"/api/agents/([0-9a-f-]+)(/archive-and-forget)?$", re.IGNORECASE
)
FORBIDDEN = ("/wake", "/stop", "/terminal", "/tmux", "/command", "/tool", "/git")


def bulk_agent(index, **patch):
    value = agent(index)
    value.update(
        {
            "session_id": f"session-{index}",
            "spawn_state": "ready",
            "working_folder": f"/workspace/{index}",
        }
    )
    value.update(patch)
    return value


def bulk_state():
    value = browser_state()
    value["agents"] = [
        bulk_agent(0, alive=True, name="Live root <img src=x onerror=window.bulkXss=true>"),
        bulk_agent(1, parent_id=AGENT_IDS[0], name="Hidden stopped child"),
        bulk_agent(2, parent_id=AGENT_IDS[0], name="Stopped sibling"),
        bulk_agent(3, alive=True, name="Other live family"),
        bulk_agent(4, name="Second stopped family"),
    ]
    return value


class BulkArchiveServer(GroupServer):
    """GroupServer plus strict, in-memory bulk endpoints and complete request logging."""

    def __init__(self, state=None):
        super().__init__(state or bulk_state())
        self.requests = []
        self.archive_posts = []
        self.forget_deletes = []
        self.forgotten_sessions = {}
        self.failures = {}

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        body = request.post_data_json if request.post_data else None
        self.requests.append((method, path, copy.deepcopy(body)))
        match = MUTATION.fullmatch(path)
        if not match or method not in {"POST", "DELETE"}:
            super().route(route)
            return

        agent_id = unquote(match.group(1))
        target = next(
            (item for item in self.state["agents"] if item["id"] == agent_id), None
        )
        kind = "archive" if match.group(2) else "forget"
        failure = self.failures.get((kind, agent_id))
        if failure:
            status, detail = failure
            route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps({"detail": detail}),
            )
            return
        if target is None or target.get("alive"):
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "server requires a stopped registered agent"}),
            )
            return

        if kind == "archive":
            assert method == "POST"
            assert set(body) == {"request_id", "include_files"}
            request_id = str(UUID(body["request_id"]))
            assert isinstance(body["include_files"], bool)
            self.archive_posts.append((agent_id, copy.deepcopy(body)))
            job = {
                "id": request_id,
                "request_id": request_id,
                "archive_id": ARCHIVE_ID,
                "agent_id": agent_id,
                "name": target["name"],
                "status": "queued",
                "include_files": body["include_files"],
            }
            target["agent_archive"] = copy.deepcopy(job)
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps({"job": job}),
            )
            return

        assert method == "DELETE" and body is None
        self.forget_deletes.append(agent_id)
        self.forgotten_sessions[agent_id] = (
            target.get("session_file"),
            target.get("session_id"),
        )
        self.state["agents"] = [
            item for item in self.state["agents"] if item["id"] != agent_id
        ]
        route.fulfill(
            content_type="application/json",
            body=json.dumps({"forgotten": True, "id": agent_id}),
        )


@contextmanager
def open_page(static_url, server, *, width=1280, init_script=None, bind_confirm=True):
    with playwright.sync_playwright() as runner:
        env = dict(os.environ)
        if LIBRARY_PATH_FILE.exists():
            library_path = LIBRARY_PATH_FILE.read_text(encoding="utf-8").strip()
            if library_path:
                env["LD_LIBRARY_PATH"] = library_path
        try:
            browser = runner.chromium.launch(headless=True, env=env)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc) or "error while loading" in str(exc):
                pytest.skip(f"Optional Chromium runtime unavailable: {exc}")
            raise
        context = browser.new_context(
            viewport={"width": width, "height": 900},
            has_touch=width == 390,
            is_mobile=width == 390,
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.add_init_script(
            """window.WebSocket = class {
              static OPEN = 1;
              constructor(url) { this.url = url; this.readyState = 1;
                setTimeout(() => this.onopen?.(), 0); }
              close() { this.readyState = 3; }
              send() {}
            };"""
        )
        if bind_confirm:
            # Work around the separately covered production bug where app.js passes
            # the native method unbound and the controller invokes it as this.confirm.
            page.add_init_script("window.confirm = window.confirm.bind(window)")
        if init_script:
            page.add_init_script(init_script)
        page.route("**/api/**", no_auth_mock(server.route))
        try:
            page.goto(static_url)
            yield page, errors
        finally:
            context.close()
            browser.close()


def card(page, index):
    return page.locator(f'.agent-card[data-agent-id="{AGENT_IDS[index]}"]')


def select_two_families(page):
    page.locator(f'.org-branch-toggle[data-agent-id="{AGENT_IDS[0]}"]').click()
    playwright.expect(card(page, 1)).to_be_hidden()
    card(page, 0).locator(".group-family-select").check()
    card(page, 4).locator(".group-family-select").check()
    playwright.expect(page.locator("#group-selection-count")).to_have_text(
        "2 family units selected"
    )


def accept_next_confirmation(page, messages):
    page.once("dialog", lambda dialog: (messages.append(dialog.message), dialog.accept()))


def cancel_next_confirmation(page, messages):
    page.once("dialog", lambda dialog: (messages.append(dialog.message), dialog.dismiss()))


def mutations(server):
    return [item for item in server.requests if item[0] in {"POST", "PATCH", "DELETE"}]


def assert_no_runtime_side_effects(server):
    assert not [
        item
        for item in mutations(server)
        if any(token in item[1].lower() for token in FORBIDDEN)
    ]


def test_native_confirm_is_callable_without_a_javascript_error(static_url):
    """The integration must bind Window.confirm before handing it to a controller."""
    server = BulkArchiveServer()
    with open_page(static_url, server, bind_confirm=False) as (page, errors):
        card(page, 4).locator(".group-family-select").check()
        page.locator("#group-bulk-forget").click()
        confirmations = []
        accept_next_confirmation(page, confirmations)
        page.locator("#bulk-forget-only").click()
        playwright.expect(page.locator("#bulk-forget-results li")).to_have_count(1)
        assert server.forget_deletes == [AGENT_IDS[4]]
        assert len(confirmations) == 1
        assert not errors


@pytest.mark.parametrize("width", [390, 1280])
def test_two_families_preview_and_queue_all_stopped_hidden_members(static_url, width):
    server = BulkArchiveServer()
    with open_page(static_url, server, width=width) as (page, errors):
        select_two_families(page)
        page.locator("#group-bulk-forget").click()
        dialog = page.locator("#bulk-forget-dialog")
        playwright.expect(dialog).to_be_visible()
        playwright.expect(page.locator("#bulk-forget-summary")).to_have_text(
            "4 selected agents · 3 can be archived · 3 can be forgotten."
        )
        targets = page.locator("#bulk-forget-targets").inner_text()
        assert "Live root <img" in targets and "Stop this agent first" in targets
        assert "Hidden stopped child — ready to archive" in targets
        assert "Stopped sibling — ready to archive" in targets
        assert "Second stopped family — ready to archive" in targets
        assert not dialog.locator("img,script").count()
        assert page.evaluate("window.bulkXss === true") is False

        include_files = page.locator("#bulk-forget-include-files")
        include_files.check()
        confirmations = []
        accept_next_confirmation(page, confirmations)
        page.locator("#bulk-forget-archive").click()
        playwright.expect(page.locator("#bulk-forget-results li")).to_have_count(4)

        expected = [AGENT_IDS[index] for index in (1, 2, 4)]
        assert [agent_id for agent_id, _body in server.archive_posts] == expected
        assert all(body["include_files"] is True for _agent_id, body in server.archive_posts)
        request_ids = [body["request_id"] for _agent_id, body in server.archive_posts]
        assert len(set(request_ids)) == 3
        assert all(str(UUID(value)) == value for value in request_ids)
        assert server.state["agents"][1]["agent_archive"]["status"] == "queued"
        assert len(server.archive_posts) == 3, "later jobs do not wait for a ZIP"
        assert len(confirmations) == 1
        assert "This list is frozen" in confirmations[0]
        assert "No agent will be stopped automatically" in confirmations[0]
        assert page.locator("#bulk-forget-archive").is_disabled()
        assert page.locator("#bulk-forget-only").is_disabled()
        assert page.locator("#bulk-forget-cancel").is_enabled()

        footer = page.locator("#bulk-forget-dialog .dialog-footer")
        assert footer.get_by_role("button", name="Cancel").count() == 1
        assert footer.get_by_role("button", name="Forget only").count() == 1
        assert footer.get_by_role("button", name="Archive and forget").count() == 1
        assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
        box = footer.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1
        assert_no_runtime_side_effects(server)
        assert not server.other_mutations
        assert not errors


def test_forget_cancel_then_one_confirmation_deletes_only_eligible_ids(static_url):
    server = BulkArchiveServer()
    original_sessions = {
        item["id"]: (item["session_file"], item["session_id"])
        for item in server.state["agents"]
    }
    with open_page(static_url, server) as (page, errors):
        select_two_families(page)
        page.locator("#group-bulk-forget").click()
        messages = []
        cancel_next_confirmation(page, messages)
        page.locator("#bulk-forget-only").click()
        assert server.forget_deletes == []

        accept_next_confirmation(page, messages)
        page.locator("#bulk-forget-only").click()
        playwright.expect(page.locator("#bulk-forget-results li")).to_have_count(4)
        expected = [AGENT_IDS[index] for index in (1, 2, 4)]
        assert server.forget_deletes == expected
        assert server.forgotten_sessions == {
            agent_id: original_sessions[agent_id] for agent_id in expected
        }
        assert len(messages) == 2
        assert "does not send a stop request" in messages[1]
        assert "Saved sessions, working folders and worktrees are kept" in messages[1]
        assert AGENT_IDS[0] not in server.forget_deletes
        assert AGENT_IDS[3] not in server.forget_deletes
        for agent_id in expected:
            playwright.expect(
                page.locator(f'.agent-card[data-agent-id="{agent_id}"]')
            ).to_have_count(0)
        assert_no_runtime_side_effects(server)
        assert not errors


def test_busy_spawn_reservations_and_live_agents_are_previewed_and_skipped(static_url):
    state = bulk_state()
    state["agents"][1].update(spawn_state="provisioning", name="Spawn not ready")
    state["agents"][2].update(stop_pending=True, name="Stop reservation")
    state["agents"][4].update(
        name="Maintenance busy", tool_output_repair={"status": "running"}
    )
    server = BulkArchiveServer(state)
    with open_page(static_url, server) as (page, errors):
        select_two_families(page)
        page.locator("#group-bulk-forget").click()
        playwright.expect(page.locator("#bulk-forget-summary")).to_have_text(
            "4 selected agents · 0 can be archived · 0 can be forgotten."
        )
        text = page.locator("#bulk-forget-targets").inner_text()
        assert "Live root <img" in text and "Stop this agent first" in text
        assert "Spawn not ready — Setup is incomplete" in text
        assert "Stop reservation — Agent stop is still pending" in text
        assert "Maintenance busy — Saved-session maintenance is in progress" in text
        assert page.locator("#bulk-forget-archive").is_disabled()
        assert page.locator("#bulk-forget-only").is_disabled()
        assert mutations(server) == []
        assert not errors


def test_known_4xx_continues_but_503_halts_and_receipt_prevents_blind_retry(static_url):
    known = BulkArchiveServer()
    known.failures[("archive", AGENT_IDS[1])] = (422, "known invalid session")
    with open_page(static_url, known) as (page, errors):
        card(page, 0).locator(".group-family-select").check()
        page.locator("#group-bulk-forget").click()
        confirmations = []
        accept_next_confirmation(page, confirmations)
        page.locator("#bulk-forget-archive").click()
        playwright.expect(page.locator("#bulk-forget-results li")).to_have_count(3)
        assert [item[0] for item in known.archive_posts] == [AGENT_IDS[2]]
        attempted = [
            path
            for method, path, _body in known.requests
            if method == "POST" and path.endswith("/archive-and-forget")
        ]
        assert attempted == [
            f"/api/agents/{AGENT_IDS[1]}/archive-and-forget",
            f"/api/agents/{AGENT_IDS[2]}/archive-and-forget",
        ]
        assert "known invalid session" in page.locator("#bulk-forget-results").inner_text()
        assert len(confirmations) == 1
        assert not errors

    uncertain = BulkArchiveServer()
    uncertain.failures[("archive", AGENT_IDS[1])] = (503, "archive queue unavailable")
    with open_page(static_url, uncertain) as (page, errors):
        card(page, 0).locator(".group-family-select").check()
        page.locator("#group-bulk-forget").click()
        confirmations = []
        accept_next_confirmation(page, confirmations)
        page.locator("#bulk-forget-archive").click()
        playwright.expect(page.locator("#bulk-forget-results li")).to_have_count(3)
        first_text = page.locator("#bulk-forget-results").inner_text()
        request_id = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            first_text,
            re.IGNORECASE,
        )
        assert request_id and "outcome uncertain" in first_text
        assert "not sent after an earlier uncertain outcome" in first_text
        assert uncertain.archive_posts == []

        page.locator("#bulk-forget-cancel").click()
        page.locator("#group-bulk-forget").click()
        accept_next_confirmation(page, confirmations)
        page.locator("#bulk-forget-archive").click()
        playwright.expect(page.locator("#bulk-forget-results li")).to_have_count(3)
        attempted_ids = [
            path.split("/agents/", 1)[1].split("/", 1)[0]
            for method, path, _body in uncertain.requests
            if method == "POST" and path.endswith("/archive-and-forget")
        ]
        assert attempted_ids == [AGENT_IDS[1], AGENT_IDS[2]]
        assert [item[0] for item in uncertain.archive_posts] == [AGENT_IDS[2]]
        assert request_id.group(0) in page.locator("#bulk-forget-results").inner_text()
        assert_no_runtime_side_effects(uncertain)
        assert not errors


DEFERRED_FORGET = r"""
const originalFetch = window.fetch.bind(window);
window.bulkGate = {enabled: false, calls: [], pending: [],
  release(index = 0) {
    const item = this.pending[index];
    if (!item || item.released) return false;
    item.released = true;
    item.resolve(new Response(JSON.stringify({forgotten: true, id: item.agentId}),
      {status: 200, headers: {'Content-Type': 'application/json'}}));
    return true;
  }
};
window.fetch = (input, options = {}) => {
  const path = typeof input === 'string' ? input : input.url;
  const method = options.method || 'GET';
  if (window.bulkGate.enabled && method === 'DELETE' && /\/api\/agents\/[0-9a-f-]+$/.test(path)) {
    const agentId = decodeURIComponent(path.split('/agents/')[1]);
    const call = {path, method, agentId};
    window.bulkGate.calls.push(call);
    return new Promise(resolve => window.bulkGate.pending.push({...call, resolve, released: false}));
  }
  return originalFetch(input, options);
};
"""


def test_pending_double_click_and_scope_change_never_retargets_same_id_owner(static_url):
    local = bulk_state()
    remote = bulk_state()
    for item in remote["agents"]:
        item["name"] = f"Remote {item['name']}"
    federation = FederationGroupServer(local, remote)
    with open_page(static_url, federation, init_script=DEFERRED_FORGET) as (page, errors):
        selector = page.locator("#swarm-selector")
        playwright.expect(selector.locator('option[value="peer-groups"]')).to_have_count(1)
        selector.select_option("peer-groups")
        playwright.expect(selector).to_have_value("peer-groups")
        card(page, 4).locator(".group-family-select").check()
        page.locator("#group-bulk-forget").click()
        page.evaluate("bulkGate.enabled = true")
        confirmations = []
        accept_next_confirmation(page, confirmations)
        page.locator("#bulk-forget-only").click()
        page.wait_for_function("bulkGate.calls.length === 1")
        assert page.locator("#bulk-forget-archive").is_disabled()
        assert page.locator("#bulk-forget-only").is_disabled()
        assert page.locator("#bulk-forget-cancel").is_disabled()
        page.locator("#bulk-forget-only").click(force=True)
        assert len(page.evaluate("bulkGate.calls")) == 1
        assert page.evaluate("bulkGate.calls[0].path") == (
            f"/api/federation/peer-groups/proxy/api/agents/{AGENT_IDS[4]}"
        )

        selector.select_option("local")
        playwright.expect(selector).to_have_value("local")
        playwright.expect(page.locator("#bulk-forget-dialog")).to_be_hidden()
        assert page.evaluate("bulkGate.release(0)") is True
        page.wait_for_timeout(100)
        assert len(page.evaluate("bulkGate.calls")) == 1
        assert card(page, 4).count() == 1, "same UUID on the new owner is untouched"
        assert "forgotten" not in page.locator("#toast").inner_text().lower()
        assert len(confirmations) == 1
        assert not federation.servers["local"].other_mutations
        assert not federation.servers["peer-groups"].other_mutations
        assert not errors
