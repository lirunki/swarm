"""Chromium integration coverage for whole-agent archive and forget workflows."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")
AGENT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AGENT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AGENT_BUSY = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
AGENT_OTHER = "ffffffff-ffff-4fff-8fff-ffffffffffff"
ARCHIVE_A = "11111111-1111-4111-8111-111111111111"
ARCHIVE_B = "22222222-2222-4222-8222-222222222222"
ARCHIVE_XSS = "33333333-3333-4333-8333-333333333333"
JOB_QUEUED = "40000000-0000-4000-8000-000000000001"
JOB_RUNNING = "40000000-0000-4000-8000-000000000002"
JOB_COMPLETED = "40000000-0000-4000-8000-000000000003"
JOB_FAILED = "40000000-0000-4000-8000-000000000004"


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Expose the dashboard static server from the established browser fixture."""
    return _dashboard_static_url


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
            pytest.skip(f"Optional Chromium runtime unavailable: {runner.chromium.executable_path}")
        raise


def agent(agent_id, name, **patch):
    value = {
        "id": agent_id,
        "name": name,
        "goal": f"Goal for {name}",
        "status": "idle",
        "alive": False,
        "pid": None,
        "managed": True,
        "backend": "pi",
        "auto_resume": False,
        "activity": "Stopped browser fixture",
        "folder": f"/workspace/{agent_id[:8]}",
        "session_file": f"/sessions/{agent_id}.jsonl",
        "session_id": f"session-{agent_id[:8]}",
        "spawn_state": "completed",
        "file_count": 1,
        "files_version": 0,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    value.update(patch)
    return value


def archived(archive_id, agent_id, name, **patch):
    value = {
        "id": archive_id,
        "name": name,
        "agent_id": agent_id,
        "created_at": "2026-01-01T00:00:00Z",
        "archived_at": "2026-01-02T00:00:00Z",
        "last_accessed_at": None,
        "last_resurrected_at": None,
        "status": "archived",
        "goal": "Private archived goal",
        "folder": "/private/workspace",
        "session_id": f"archived-{agent_id[:8]}",
        "size": 2048,
        "files_included": False,
        "restorable": True,
        "missing_zip": False,
    }
    value.update(patch)
    return value


def job(job_id, archive_id, agent_id, name, status, **patch):
    value = {
        "id": job_id,
        "request_id": job_id,
        "archive_id": archive_id,
        "agent_id": agent_id,
        "name": name,
        "status": status,
        "created_at": "2026-01-02T00:00:00Z",
    }
    value.update(patch)
    return value


def fixture_sources():
    local_agents = [
        agent(AGENT_A, '<img src=x onerror="window.unsafe=true">'),
        agent(AGENT_B, "Second stopped agent"),
        agent(
            AGENT_BUSY,
            "Archive already queued",
            alive=True,
            status="running",
            agent_archive={"id": JOB_QUEUED, "status": "queued"},
        ),
        agent(
            AGENT_OTHER,
            "Native OTHER agent",
            backend="other",
            session_file=None,
            session_id=None,
        ),
    ]
    archives = [
        archived(ARCHIVE_A, AGENT_A, "Archived alpha"),
        archived(
            ARCHIVE_XSS,
            "99999999-9999-4999-8999-999999999999",
            '<svg onload="window.unsafe=true">' + "X" * 110,
            goal="<script>window.unsafe=true</script>",
            files_included=True,
            last_accessed_at="2026-01-03T00:00:00Z",
        ),
    ]
    jobs = [
        job(
            JOB_QUEUED,
            "44444444-4444-4444-8444-444444444444",
            AGENT_BUSY,
            "Archive already queued",
            "queued",
        ),
        job(
            JOB_RUNNING,
            "55555555-5555-4555-8555-555555555555",
            AGENT_A,
            "Running fixture",
            "running",
        ),
        job(JOB_COMPLETED, ARCHIVE_B, AGENT_B, "Completed fixture", "completed"),
        job(
            JOB_FAILED,
            ARCHIVE_B,
            AGENT_B,
            '<img src=x onerror="window.unsafe=true">',
            "failed",
            error='<svg onload="window.unsafe=true">disk failed',
        ),
    ]
    state = {
        "agents": local_agents,
        "home": "/fixture/local",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }
    peer_state = {
        "agents": [agent(AGENT_A, "Remote same-ID agent"), agent(AGENT_B, "Remote second")],
        "home": "/fixture/peer-a",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }
    return {
        "local": {"state": state, "archives": archives, "jobs": jobs},
        "peer/a": {"state": peer_state, "archives": [], "jobs": []},
    }


class ArchiveAPI:
    """Stateful HTTP-only archive registry; it never creates ZIPs or starts services."""

    def __init__(self, sources=None):
        self.sources = sources or fixture_sources()
        self.requests = []
        self.delete_failure = None
        self.archive_serial = 0
        self.download_requests = []

    @staticmethod
    def source(path):
        if "/api/federation/" not in path:
            return "local"
        return unquote(path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0])

    @staticmethod
    def direct_path(path):
        return path.split("/proxy", 1)[1] if "/proxy/" in path else path

    def selected(self, source):
        return self.sources[source]

    def transition(self, source, request_id, status, *, error=None):
        registry = self.selected(source)
        selected = next(item for item in registry["jobs"] if item["id"] == request_id)
        selected["status"] = status
        if error:
            selected["error"] = error
        target = next(
            (item for item in registry["state"]["agents"] if item["id"] == selected["agent_id"]),
            None,
        )
        if status == "failed":
            if target:
                target["agent_archive"] = deepcopy(selected)
            return
        if status != "completed":
            if target:
                target["agent_archive"] = deepcopy(selected)
            return
        if target:
            registry["state"]["agents"] = [
                item for item in registry["state"]["agents"] if item["id"] != target["id"]
            ]
            if not any(item["id"] == selected["archive_id"] for item in registry["archives"]):
                registry["archives"].insert(
                    0,
                    archived(
                        selected["archive_id"],
                        target["id"],
                        target["name"],
                        goal=target["goal"],
                        files_included=selected.get("include_files", False),
                    ),
                )

    def respond(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path
        method = request.method
        body = request.post_data_json if request.post_data else None
        self.requests.append((method, path, body))
        source = self.source(path)
        registry = self.selected(source) if source in self.sources else self.selected("local")
        direct = self.direct_path(path)
        status = 200

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {
                        "id": "peer/a",
                        "name": "Peer A",
                        "online": True,
                        "capabilities": {"remote_restart": True},
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
                "relay_node_id": "browser-local",
                "relay_name": "Browser Local",
            }
        elif direct == "/api/state":
            value = registry["state"]
        elif direct == "/api/archives" and method == "GET":
            value = {"archives": registry["archives"], "jobs": registry["jobs"]}
        elif direct.endswith("/archive-and-forget") and method == "POST":
            agent_id = unquote(direct.rsplit("/agents/", 1)[1].split("/", 1)[0])
            selected = next(item for item in registry["state"]["agents"] if item["id"] == agent_id)
            self.archive_serial += 1
            archive_id = f"70000000-0000-4000-8000-{self.archive_serial:012d}"
            accepted = job(
                body["request_id"],
                archive_id,
                agent_id,
                selected["name"],
                "queued",
                include_files=body["include_files"],
            )
            registry["jobs"].insert(0, accepted)
            selected["agent_archive"] = deepcopy(accepted)
            status = 202
            value = {"job": accepted, "warning": "Private archives may contain secrets."}
        elif re.fullmatch(r"/api/archives/[0-9a-f-]+/resurrect", direct) and method == "POST":
            archive_id = direct.split("/")[3]
            item = next(item for item in registry["archives"] if item["id"] == archive_id)
            restored = agent(item["agent_id"], item["name"], goal=item["goal"])
            restored["alive"] = False
            restored["auto_resume"] = False
            restored["status"] = "idle"
            registry["state"]["agents"] = [
                current
                for current in registry["state"]["agents"]
                if current["id"] != restored["id"]
            ] + [restored]
            item["last_resurrected_at"] = "2026-01-04T00:00:00Z"
            value = {"agent": restored, "archive": item}
        elif re.fullmatch(r"/api/archives/[0-9a-f-]+", direct) and method == "DELETE":
            archive_id = direct.rsplit("/", 1)[1]
            if self.delete_failure == archive_id:
                status, value = 503, {"detail": "simulated disk busy"}
            else:
                registry["archives"] = [
                    item for item in registry["archives"] if item["id"] != archive_id
                ]
                value = {"id": archive_id, "deleted": True}
        elif re.fullmatch(r"/api/agents/[0-9a-f-]+", direct) and method == "DELETE":
            agent_id = direct.rsplit("/", 1)[1]
            registry["state"]["agents"] = [
                item for item in registry["state"]["agents"] if item["id"] != agent_id
            ]
            value = {"id": agent_id, "deleted": True}
        elif re.fullmatch(r"/api/agents/[0-9a-f-]+/files", direct) and method == "GET":
            value = {"revision": 0, "files": []}
        elif direct.endswith("/download"):
            self.download_requests.append((method, path, body))
            status, value = 500, {"detail": "browser tests never fetch fake ZIP bytes"}
        elif direct.endswith("/api/notifications") or path == "/api/notifications":
            value = {"revision": 0, "count": 0, "notifications": []}
        elif direct.endswith("/api/fleet/restarts"):
            value = {"jobs": []}
        elif method in {"POST", "PATCH", "DELETE"}:
            status, value = 418, {"detail": f"unexpected mutation {method} {path}"}
        else:
            value = registry["state"]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


def install_page(page, api):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        """
        window.WebSocket = class {
          static OPEN = 1;
          constructor(url) { this.url = url; this.readyState = 1;
            setTimeout(() => this.onopen?.(), 0); }
          close() { this.readyState = 3; }
          send() {}
        };
        """
    )
    page.route("**/api/**", no_auth_mock(api.respond))
    return errors


def open_edit(page, agent_id):
    playwright.expect(page.locator(f'.agent-card[data-agent-id="{agent_id}"]')).to_be_visible()
    page.evaluate("agentId => editAgent(agentId)", agent_id)
    playwright.expect(page.locator("#edit-dialog")).to_be_visible()


def open_archives(page):
    page.locator("#add-menu-button").click()
    playwright.expect(page.locator("#archived-agents-button")).to_be_visible()
    page.locator("#archived-agents-button").click()
    playwright.expect(page.locator("#archives-dialog")).to_be_visible()


def archive_posts(api):
    return [
        item
        for item in api.requests
        if item[0] == "POST" and item[1].endswith("/archive-and-forget")
    ]


def forbidden_runtime_requests(api):
    tokens = ("/wake", "/stop", "/terminal", "/tmux", "/command", "/retry")
    return [
        item
        for item in api.requests
        if item[0] in {"POST", "PATCH", "DELETE"} and any(token in item[1] for token in tokens)
    ]


@pytest.mark.parametrize("width", [390, 1280])
def test_archived_agents_menu_listing_is_private_safe_selectable_and_responsive(static_url, width):
    api = ArchiveAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(
                viewport={"width": width, "height": 1000}, has_touch=width == 390
            )
            errors = install_page(page, api)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(4)
            open_archives(page)

            playwright.expect(page.locator("#archives-owner")).to_have_text("Local Swarm")
            playwright.expect(page.locator(".archive-row")).to_have_count(2)
            assert page.locator(".archive-item").count() == 0
            playwright.expect(page.locator(".archive-job")).to_have_count(4)
            for status in ("queued", "running", "completed", "failed"):
                playwright.expect(page.locator(f".archive-job.status-{status}")).to_have_count(1)
            assert page.locator('#archives-list input[type="checkbox"]:checked').count() == 0
            playwright.expect(page.locator("#archives-delete-selected")).to_be_disabled()

            labels = (
                page.locator(".archive-actions").first.get_by_role("button").all_text_contents()
            )
            assert labels == ["Resurrect…", "Delete archive…"]
            assert (
                page.locator(".archive-actions").first.get_by_role("link").inner_text()
                == "Download ZIP"
            )
            links = page.locator(".archive-actions a")
            assert (
                links.nth(0).get_attribute("href").endswith(f"/api/archives/{ARCHIVE_A}/download")
            )
            assert links.nth(0).get_attribute("download") == ""
            assert page.evaluate(
                "path => SwarmAuth.BrowserAuth.isResourcePath.call({}, path)",
                f"/api/archives/{ARCHIVE_A}/download",
            )
            assert page.evaluate(
                "path => SwarmAuth.BrowserAuth.isResourcePath.call({}, path)",
                f"/api/federation/peer%2Fa/proxy/api/archives/{ARCHIVE_A}/download",
            )
            assert not api.download_requests

            first = page.locator(f'.archive-row[data-archive-id="{ARCHIVE_A}"] input')
            first.check()
            playwright.expect(page.locator("#archives-delete-selected")).to_be_enabled()
            select_all = page.locator("#archives-select-all")
            assert select_all.is_checked() is False
            assert select_all.evaluate("element => element.indeterminate") is True
            select_all.check()
            assert page.locator('#archives-list input[type="checkbox"]:checked').count() == 2

            assert (
                '<svg onload="window.unsafe=true">' in page.locator("#archives-list").inner_text()
            )
            assert (
                "<script>window.unsafe=true</script>" in page.locator("#archives-list").inner_text()
            )
            assert (
                page.locator(
                    "#archives-list script, #archives-list svg, #archives-jobs img"
                ).count()
                == 0
            )
            assert page.evaluate("window.unsafe") is None

            dialog = page.locator("#archives-dialog").bounding_box()
            for row in page.locator(".archive-row").all():
                box = row.bounding_box()
                assert box["x"] >= dialog["x"] - 1
                assert box["x"] + box["width"] <= dialog["x"] + dialog["width"] + 1
                assert row.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
            assert page.evaluate("document.body.scrollWidth <= innerWidth")

            page.locator("#archives-close").click()
            open_edit(page, AGENT_B)
            page.locator("#forget-agent").click()
            choice = page.locator("#archive-agent-choice-dialog")
            playwright.expect(choice).to_be_visible()
            choice_box = choice.bounding_box()
            footer_box = choice.locator(".dialog-footer").bounding_box()
            assert footer_box["x"] >= choice_box["x"] - 1
            assert (
                footer_box["x"] + footer_box["width"] <= choice_box["x"] + choice_box["width"] + 1
            )
            assert choice.locator(".dialog-footer").evaluate(
                "element => element.scrollWidth <= element.clientWidth + 1"
            )
            page.locator("#archive-agent-cancel").click()
            assert not errors
        finally:
            browser.close()


def test_forget_choice_keeps_old_confirmation_and_queues_two_agents_without_waiting(static_url):
    api = ArchiveAPI()
    api.sources["local"]["archives"] = []
    api.sources["local"]["jobs"] = []
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = install_page(page, api)
            page.goto(static_url)

            open_edit(page, AGENT_A)
            page.locator("#forget-agent").click()
            playwright.expect(page.locator("#edit-dialog")).to_be_hidden()
            choice = page.locator("#archive-agent-choice-dialog")
            playwright.expect(choice).to_be_visible()
            playwright.expect(page.locator("#archive-agent-name")).to_have_text(
                '<img src=x onerror="window.unsafe=true">'
            )
            assert page.locator("#archive-agent-include-files").is_checked() is False
            assert choice.locator("img").count() == 0
            page.locator("#archive-agent-cancel").click()
            assert not archive_posts(api)

            open_edit(page, AGENT_A)
            page.locator("#forget-agent").click()
            confirmations = []
            page.once(
                "dialog",
                lambda dialog: (confirmations.append(dialog.message), dialog.accept()),
            )
            page.locator("#archive-agent-forget-only").click()
            playwright.expect(
                page.locator(f'.agent-card[data-agent-id="{AGENT_A}"]')
            ).to_have_count(0)
            assert len(confirmations) == 1
            assert "Saved session history will be kept" in confirmations[0]
            assert not archive_posts(api)

            open_edit(page, AGENT_B)
            page.locator("#forget-agent").click()
            page.locator("#archive-agent-include-files").check()
            page.locator("#archive-agent-submit").click()
            playwright.expect(choice).to_be_hidden()
            playwright.expect(page.locator("#toast")).to_contain_text("Archive queued")
            posts = archive_posts(api)
            assert len(posts) == 1
            assert posts[0][2]["include_files"] is True
            assert re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                posts[0][2]["request_id"],
                re.IGNORECASE,
            )
            assert page.locator(f'.agent-card[data-agent-id="{AGENT_B}"]').count() == 1

            # A queued job is asynchronous and does not globally block another eligible agent.
            extra = agent(AGENT_A, "Re-added independent agent")
            api.sources["local"]["state"]["agents"].append(extra)
            page.evaluate("refresh()")
            playwright.expect(
                page.locator(f'.agent-card[data-agent-id="{AGENT_A}"]')
            ).to_be_visible()
            open_edit(page, AGENT_A)
            page.locator("#forget-agent").click()
            page.locator("#archive-agent-submit").click()
            playwright.expect(choice).to_be_hidden()
            assert len(archive_posts(api)) == 2
            assert archive_posts(api)[0][2]["request_id"] != archive_posts(api)[1][2]["request_id"]
            assert not forbidden_runtime_requests(api)
            assert page.evaluate("window.unsafe") is None
            assert not errors
        finally:
            browser.close()


def test_queued_running_failed_and_completed_state_gate_actions_without_waking(static_url):
    api = ArchiveAPI()
    registry = api.sources["local"]
    registry["archives"] = []
    registry["jobs"] = []
    queued_agent = next(item for item in registry["state"]["agents"] if item["id"] == AGENT_BUSY)
    queued_agent["alive"] = False
    queued_agent["status"] = "idle"
    queued = job(JOB_QUEUED, ARCHIVE_A, AGENT_BUSY, queued_agent["name"], "queued")
    queued_agent["agent_archive"] = deepcopy(queued)
    registry["jobs"].append(queued)

    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = install_page(page, api)
            page.goto(static_url)
            card = page.locator(f'.agent-card[data-agent-id="{AGENT_BUSY}"]')
            card.locator(".card-toggle").click()
            playwright.expect(card.get_by_role("button", name="Wake", exact=True)).to_be_disabled()
            open_edit(page, AGENT_BUSY)
            guarded = (
                "#save-agent",
                "#forget-agent",
                "#meta-compact-toggle",
                "#compact-tool-inputs",
                "#repair-tool-outputs",
            )
            queued_disabled = {
                selector: page.locator(selector).is_disabled() for selector in guarded
            }
            for field in ("name", "goal", "status", "auto_resume"):
                playwright.expect(page.locator(f'#edit-form [name="{field}"]')).to_be_enabled()
            page.locator("#edit-dialog [data-close]").first.click()

            # File upload selection is a backend-independent library operation, but insertion/liveness is blocked.
            card.locator(".card-files").click()
            playwright.expect(page.locator("#files-dialog")).to_be_visible()
            playwright.expect(page.locator("#upload-agent-file")).to_be_enabled()
            playwright.expect(page.get_by_role("button", name="Insert", exact=True)).to_have_count(
                0
            )
            page.locator("#files-dialog [data-close]").first.click()

            api.transition("local", JOB_QUEUED, "running")
            page.evaluate("refresh()")
            playwright.expect(card).to_be_visible()
            open_edit(page, AGENT_BUSY)
            running_disabled = {
                selector: page.locator(selector).is_disabled() for selector in guarded
            }
            page.locator("#edit-dialog [data-close]").first.click()

            api.transition("local", JOB_QUEUED, "failed", error="simulated archive failure")
            page.evaluate("refresh()")
            playwright.expect(card).to_be_visible()
            open_edit(page, AGENT_BUSY)
            playwright.expect(page.locator("#save-agent")).to_be_enabled()
            page.locator("#edit-dialog [data-close]").first.click()

            queued["status"] = "running"
            queued_agent["agent_archive"] = deepcopy(queued)
            api.transition("local", JOB_QUEUED, "completed")
            page.evaluate("refresh()")
            playwright.expect(card).to_have_count(0)
            assert any(item["agent_id"] == AGENT_BUSY for item in registry["archives"])
            assert not forbidden_runtime_requests(api)
            assert not errors
            assert all(queued_disabled.values()), queued_disabled
            assert all(running_disabled.values()), running_disabled
        finally:
            browser.close()


def test_resurrection_is_explicit_stopped_same_id_nonwaking_and_archive_is_retained(static_url):
    api = ArchiveAPI()
    api.sources["local"]["state"]["agents"] = [
        item for item in api.sources["local"]["state"]["agents"] if item["id"] != AGENT_A
    ]
    api.sources["local"]["jobs"] = []
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = install_page(page, api)
            page.goto(static_url)
            open_archives(page)
            warnings = []
            page.once("dialog", lambda dialog: (warnings.append(dialog.message), dialog.accept()))
            page.locator(f'.archive-row[data-archive-id="{ARCHIVE_A}"]').get_by_role(
                "button", name="Resurrect…"
            ).click()
            playwright.expect(
                page.locator(f'.agent-card[data-agent-id="{AGENT_A}"]')
            ).to_be_visible()
            assert len(warnings) == 1
            assert "same ID" in warnings[0]
            assert "timers remain disabled" in warnings[0]
            assert "not awakened" in warnings[0]
            assert "archive remains available" in warnings[0]
            restored = next(
                item for item in api.sources["local"]["state"]["agents"] if item["id"] == AGENT_A
            )
            assert restored["status"] == "idle"
            assert restored["alive"] is False
            assert restored["auto_resume"] is False
            assert any(item["id"] == ARCHIVE_A for item in api.sources["local"]["archives"])
            assert page.locator(f'.archive-row[data-archive-id="{ARCHIVE_A}"]').count() == 1
            playwright.expect(page.locator("#toast")).to_contain_text("archive was kept")
            assert not forbidden_runtime_requests(api)
            assert not errors
        finally:
            browser.close()


def test_selected_delete_stops_after_partial_failure_and_requires_refresh(static_url):
    api = ArchiveAPI()
    api.sources["local"]["jobs"] = []
    api.delete_failure = ARCHIVE_XSS
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = install_page(page, api)
            page.goto(static_url)
            open_archives(page)
            page.locator("#archives-select-all").check()
            confirmations = []
            page.once(
                "dialog",
                lambda dialog: (confirmations.append(dialog.message), dialog.accept()),
            )
            page.locator("#archives-delete-selected").click()
            playwright.expect(page.locator("#archives-error")).to_contain_text(
                "Stopped after 1 of 2; refresh before retrying"
            )
            deletes = [
                item for item in api.requests if item[0] == "DELETE" and "/api/archives/" in item[1]
            ]
            assert [item[1].rsplit("/", 1)[1] for item in deletes] == [ARCHIVE_A, ARCHIVE_XSS]
            assert len(confirmations) == 1
            assert "only the archive ZIP and archive record" in confirmations[0]
            assert "does not delete any agent" in confirmations[0]
            assert page.locator(f'.archive-row[data-archive-id="{ARCHIVE_A}"]').count() == 0
            assert page.locator(f'.archive-row[data-archive-id="{ARCHIVE_XSS}"]').count() == 1
            page.wait_for_timeout(100)
            assert (
                len(
                    [
                        item
                        for item in api.requests
                        if item[0] == "DELETE" and "/api/archives/" in item[1]
                    ]
                )
                == 2
            )
            assert not errors
        finally:
            browser.close()


def install_deferred_archive_fetch(page):
    page.add_init_script(
        """
        const originalFetch = window.fetch.bind(window);
        window.deferredArchives = {
          enabled: false, calls: [], pending: [],
          release(index = 0) {
            const item = this.pending[index];
            if (!item || item.released) return false;
            item.released = true;
            const response = {job: {
              id: item.body.request_id, archive_id: '88888888-8888-4888-8888-888888888888',
              agent_id: item.agentId, name: item.name, status: 'queued'
            }};
            item.resolve(new Response(JSON.stringify(response), {
              status: 202, headers: {'Content-Type': 'application/json'}
            }));
            return true;
          }
        };
        window.fetch = (input, options = {}) => {
          const path = typeof input === 'string' ? input : input.url;
          if (window.deferredArchives.enabled && (options.method || 'GET') === 'POST' &&
              path.split('?', 1)[0].endsWith('/archive-and-forget')) {
            const body = JSON.parse(options.body || '{}');
            const agentId = decodeURIComponent(path.split('/agents/')[1].split('/')[0]);
            const name = agentId === 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
              ? 'Remote same-ID agent' : 'Remote second';
            const call = {path, body, agentId, name};
            window.deferredArchives.calls.push(call);
            return new Promise(resolve => window.deferredArchives.pending.push({...call, resolve, released: false}));
          }
          return originalFetch(input, options);
        };
        """
    )


def test_remote_request_capture_is_stable_and_late_ack_cannot_touch_another_swarm(static_url):
    api = ArchiveAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            install_deferred_archive_fetch(page)
            errors = install_page(page, api)
            page.goto(static_url)
            selector = page.locator("#swarm-selector")
            playwright.expect(selector.locator('option[value="peer/a"]')).to_have_count(1)
            selector.select_option("peer/a")
            playwright.expect(selector).to_have_value("peer/a")
            open_edit(page, AGENT_A)
            page.locator("#forget-agent").click()
            page.evaluate("deferredArchives.enabled = true")
            page.locator("#archive-agent-submit").click()
            page.wait_for_function("deferredArchives.calls.length === 1")
            captured = page.evaluate("deferredArchives.calls[0]")
            assert captured["path"] == (
                f"/api/federation/peer%2Fa/proxy/api/agents/{AGENT_A}/archive-and-forget"
            )

            selector.select_option("local")
            playwright.expect(selector).to_have_value("local")
            local_count = page.locator(".agent-card").count()
            assert page.evaluate("deferredArchives.release(0)") is True
            page.wait_for_timeout(100)
            assert page.locator(".agent-card").count() == local_count
            assert page.locator(f'.agent-card[data-agent-id="{AGENT_A}"]').count() == 1
            assert "Archive queued" not in page.locator("#toast").inner_text()
            assert not forbidden_runtime_requests(api)
            assert not errors
        finally:
            browser.close()


def test_archive_timeout_keeps_one_request_id_and_does_not_retry_late_ack(static_url):
    api = ArchiveAPI()
    api.sources["local"]["jobs"] = []
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            install_deferred_archive_fetch(page)
            errors = install_page(page, api)
            page.goto(static_url)
            page.evaluate(
                "agentArchivesController.requestTimeout = 30; deferredArchives.enabled = true"
            )
            open_edit(page, AGENT_A)
            page.locator("#forget-agent").click()
            page.locator("#archive-agent-submit").click()
            playwright.expect(page.locator("#toast")).to_contain_text("Outcome uncertain")
            message = page.locator("#toast").inner_text()
            request_id = re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                message,
                re.IGNORECASE,
            )
            assert request_id
            page.locator("#archive-agent-submit").click()
            page.wait_for_timeout(50)
            calls = page.evaluate("deferredArchives.calls")
            assert len(calls) == 1
            assert calls[0]["body"]["request_id"] == request_id.group(0)
            assert page.evaluate("deferredArchives.release(0)") is True
            page.wait_for_timeout(100)
            assert len(page.evaluate("deferredArchives.calls")) == 1
            assert page.locator(f'.agent-card[data-agent-id="{AGENT_A}"]').count() == 1
            assert not forbidden_runtime_requests(api)
            assert not errors
        finally:
            browser.close()


def test_other_backend_uses_existing_forget_flow_and_files_remain_backend_independent(static_url):
    api = ArchiveAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = install_page(page, api)
            page.goto(static_url)
            other_card = page.locator(f'.agent-card[data-agent-id="{AGENT_OTHER}"]')
            other_card.locator(".card-files").click()
            playwright.expect(page.locator("#files-dialog")).to_be_visible()
            playwright.expect(page.locator("#upload-agent-file")).to_be_enabled()
            page.locator("#files-dialog [data-close]").first.click()

            open_edit(page, AGENT_OTHER)
            prompts = []
            page.once("dialog", lambda dialog: (prompts.append(dialog.message), dialog.accept()))
            page.locator("#forget-agent").click()
            playwright.expect(other_card).to_have_count(0)
            assert len(prompts) == 1 and "Forget Native OTHER agent?" in prompts[0]
            assert page.locator("#archive-agent-choice-dialog").is_visible() is False
            assert not archive_posts(api)
            assert not forbidden_runtime_requests(api)
            assert not errors
        finally:
            browser.close()
