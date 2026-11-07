"""Chromium integration coverage for the stopped-Pi history archive UI."""

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

UUIDS = [f"00000000-0000-4000-8000-{number:012d}" for number in range(1, 8)]
LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Expose the dashboard's isolated static server fixture."""
    return _dashboard_static_url


def archive_report(agent, body, **patch):
    report = {
        "id": "0123456789abcdef0123456789abcdef",
        "archive_id": "0123456789abcdef0123456789abcdef",
        "request_id": body["request_id"],
        "status": "completed",
        "source": agent["session_file"],
        "session_id": agent["session_id"],
        "percent_requested": body["percent"],
        "context_before_bytes": 12_345,
        "archive_bytes": 5_099,
        "actual_percent": 41.305,
        "history_file": "/private/history/" + "h" * 180 + "<img src=x>.jsonl",
        "archive": "/private/original/<script>window.unsafe=true</script>.jsonl",
    }
    report.update(patch)
    return report


def make_agent(index, name, **patch):
    value = {
        "id": UUIDS[index],
        "name": name,
        "goal": f"Fixture goal {index}",
        "status": "idle",
        "alive": False,
        "pid": None,
        "managed": True,
        "backend": "pi",
        "auto_resume": False,
        "activity": "Stopped browser fixture",
        "folder": f"/workspace/{index}",
        "session_file": f"/sessions/raw-{index}.jsonl",
        "session_id": f"session-{index}",
        "spawn_state": "completed",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    value.update(patch)
    return value


def fixture_states():
    completed = make_agent(0, '<img src=x onerror="window.unsafe=true">')
    completed["context_archive"] = archive_report(
        completed, {"percent": 50, "request_id": UUIDS[6]}
    )
    failed = make_agent(1, "Failed archive")
    failed["context_archive"] = archive_report(
        failed,
        {"percent": 37, "request_id": UUIDS[5]},
        status="failed",
        error="<svg onload=window.unsafe=true>partial archive failure",
        archive_bytes=731,
        actual_percent=5.9,
    )
    agents = [
        completed,
        failed,
        make_agent(2, "Live external process", alive=True, pid=8123, status="running"),
        make_agent(3, "Old tool job", tool_input_compaction={"status": "running"}),
        make_agent(4, "Archive running", context_archive={"status": "running"}),
        make_agent(5, "Observed or missing", managed=False, session_file=None, session_id=None),
    ]
    local = {
        "agents": agents,
        "home": "/fixture/local",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }
    remote_agent = make_agent(0, "Remote same-ID collision")
    remote_agent.update(
        session_file="/remote/session-same-id.jsonl",
        session_id="remote-session",
        folder="/remote/workspace",
    )
    remote = {
        "agents": [remote_agent],
        "home": "/fixture/remote",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }
    return {"local": local, "peer/a": remote}


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


def install_browser_mocks(page):
    page.add_init_script(
        """
        window.WebSocket = class {
          static OPEN = 1;
          constructor(url) { this.url = url; this.readyState = 1;
            setTimeout(() => this.onopen?.(), 0); }
          close() { this.readyState = 3; }
          send() {}
        };
        const nativeFetch = window.fetch.bind(window);
        window.archiveFetchMock = {
          mode: 'network', calls: [], pending: [], source: '', sessionId: '',
          response: {},
          release(index = 0, patch = {}) {
            const pending = this.pending[index];
            if (!pending || pending.released) return false;
            pending.released = true;
            const report = {...pending.report, ...patch};
            pending.resolve(new Response(JSON.stringify(report), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            return true;
          }
        };
        window.fetch = (input, options = {}) => {
          const path = typeof input === 'string' ? input : input.url;
          const method = options.method || 'GET';
          const mock = window.archiveFetchMock;
          if (mock.mode !== 'network' && method === 'POST' &&
              path.split('?', 1)[0].endsWith('/archive-history')) {
            const body = JSON.parse(options.body || '{}');
            const call = {path, method, body}; mock.calls.push(call);
            const report = {
              id: 'fedcba9876543210fedcba9876543210',
              archive_id: 'fedcba9876543210fedcba9876543210',
              request_id: body.request_id, status: 'completed', source: mock.source,
              session_id: mock.sessionId, percent_requested: body.percent,
              context_before_bytes: 1000, archive_bytes: 487, actual_percent: 48.7,
              history_file: '/deferred/history.jsonl', archive: '/deferred/original.jsonl',
              ...mock.response
            };
            if (mock.mode === '503') return Promise.resolve(new Response(
              JSON.stringify({detail: 'remote archive temporarily unavailable'}),
              {status: 503, headers: {'Content-Type': 'application/json'}}));
            if (mock.mode === 'defer') return new Promise(resolve =>
              mock.pending.push({resolve, report, call, released: false}));
          }
          return nativeFetch(input, options);
        };
        """
    )


def api_fixture(page, *, states=None):
    fixture = states or fixture_states()
    requests = []
    control = {"archive_status": "completed"}
    nodes = {
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

    def respond(route):
        request = route.request
        path = urlsplit(request.url).path
        body = request.post_data_json if request.post_data else None
        requests.append((request.method, path, body))
        status = 200
        if path == "/api/federation/nodes":
            value = nodes
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
        elif path.endswith("/api/fleet/restarts"):
            value = {"jobs": []}
        elif path == "/api/notifications" or path.endswith("/proxy/api/notifications"):
            value = {"revision": 0, "count": 0, "notifications": []}
        elif path == "/api/state":
            value = fixture["local"]
        elif "/proxy/api/state" in path:
            source = unquote(path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0])
            value = fixture[source]
        elif request.method == "POST" and path.endswith("/archive-history"):
            source = "local"
            if "/api/federation/" in path:
                source = unquote(path.split("/api/federation/", 1)[1].split("/proxy/", 1)[0])
            agent_id = unquote(path.rsplit("/agents/", 1)[1].split("/archive-history", 1)[0])
            selected = next(agent for agent in fixture[source]["agents"] if agent["id"] == agent_id)
            patch = {}
            if control["archive_status"] == "failed":
                patch = {"status": "failed", "error": "safe-cut write failed after partial work"}
            value = archive_report(selected, body, **patch)
            selected["context_archive"] = deepcopy(value)
        elif request.method in {"PATCH", "DELETE", "POST"}:
            # No production controller/session operation is reached by these browser fixtures.
            status, value = 418, {"detail": f"unexpected mutation {request.method} {path}"}
        else:
            value = fixture["local"]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))

    page.route("**/api/**", no_auth_mock(respond))
    return fixture, requests, control


def open_edit(page, agent_id):
    playwright.expect(page.locator(f'.agent-card[data-agent-id="{agent_id}"]')).to_be_visible()
    page.evaluate("agentId => editAgent(agentId)", agent_id)
    playwright.expect(page.locator("#edit-dialog")).to_be_visible()


def close_edit(page):
    page.locator("#edit-dialog [data-close]").first.click()
    playwright.expect(page.locator("#edit-dialog")).to_be_hidden()


def archive_posts(requests):
    return [item for item in requests if item[0] == "POST" and item[1].endswith("/archive-history")]


def assert_no_runtime_mutations(requests):
    forbidden = ("/wake", "/stop", "/tmux", "/terminal", "/command", "/retry")
    assert not [
        item
        for item in requests
        if item[0] in {"POST", "PATCH", "DELETE"}
        and any(token in item[1] for token in forbidden)
    ]


@pytest.mark.parametrize("width", [390, 1280])
def test_archive_is_edit_only_and_persisted_statuses_are_safe_and_responsive(static_url, width):
    states = fixture_states()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, has_touch=width == 390)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_browser_mocks(page)
            _, requests, _ = api_fixture(page, states=states)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(6)

            assert page.locator(".agent-card #archive-history").count() == 0
            assert page.locator("#edit-dialog #archive-history").count() == 1
            open_edit(page, UUIDS[0])
            percent = page.locator("#history-archive-percent")
            assert percent.input_value() == "50"
            assert percent.get_attribute("min") == "1"
            assert percent.get_attribute("max") == "95"
            assert percent.get_attribute("step") == "1"
            assert page.locator("#archive-history").get_attribute("type") == "button"
            playwright.expect(page.locator("#archive-history")).to_be_enabled()
            playwright.expect(page.locator("#history-archive-availability")).to_contain_text(
                "Available while this managed Pi agent is stopped"
            )
            playwright.expect(page.locator("#history-archive-state")).to_have_text("completed")
            playwright.expect(page.locator("#history-archive-result")).to_contain_text("41.3%")
            assert "<img src=x>" in page.locator("#history-archive-file").inner_text()
            assert "<script>" in page.locator("#history-archive-original").inner_text()
            assert page.locator("#history-archive-status script, #history-archive-status img").count() == 0
            dialog_box = page.locator("#edit-dialog").bounding_box()
            for selector in ("#history-archive-file", "#history-archive-original"):
                box = page.locator(selector).bounding_box()
                assert box["x"] >= dialog_box["x"] - 1
                assert box["x"] + box["width"] <= dialog_box["x"] + dialog_box["width"] + 1
            close_edit(page)

            open_edit(page, UUIDS[1])
            playwright.expect(page.locator("#history-archive-state")).to_have_text("failed")
            playwright.expect(page.locator("#history-archive-error")).to_contain_text(
                "partial archive failure"
            )
            assert page.locator("#history-archive-error svg").count() == 0
            close_edit(page)

            open_edit(page, UUIDS[2])
            playwright.expect(page.locator("#archive-history")).to_be_disabled()
            playwright.expect(page.locator("#history-archive-availability")).to_contain_text(
                "Stop first"
            )
            close_edit(page)

            open_edit(page, UUIDS[3])
            playwright.expect(page.locator("#archive-history")).to_be_disabled()
            playwright.expect(page.locator("#history-archive-availability")).to_contain_text(
                "saved-session operation"
            )
            for selector in (
                "#meta-compact-toggle",
                "#compact-tool-inputs",
                "#repair-tool-outputs",
                "#save-agent",
                "#forget-agent",
            ):
                playwright.expect(page.locator(selector)).to_be_disabled()
            close_edit(page)

            open_edit(page, UUIDS[4])
            playwright.expect(page.locator("#history-archive-state")).to_have_text("running")
            playwright.expect(page.locator("#archive-history")).to_be_disabled()
            for selector in (
                "#meta-compact-toggle",
                "#compact-tool-inputs",
                "#repair-tool-outputs",
                "#save-agent",
                "#forget-agent",
            ):
                playwright.expect(page.locator(selector)).to_be_disabled()
            close_edit(page)
            running_card = page.locator(f'.agent-card[data-agent-id="{UUIDS[4]}"]')
            running_card.locator(".card-toggle").click()
            playwright.expect(running_card.get_by_role("button", name="Wake", exact=True)).to_be_disabled()

            open_edit(page, UUIDS[5])
            playwright.expect(page.locator("#archive-history")).to_be_disabled()
            playwright.expect(page.locator("#history-archive-availability")).to_contain_text(
                "managed Pi"
            )
            assert not archive_posts(requests)
            assert page.evaluate("window.unsafe") is None
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_archive_validation_cancel_and_completed_request_are_exact_and_nonwaking(static_url, width):
    states = fixture_states()
    target = states["local"]["agents"][0]
    target.pop("context_archive")
    original_session = (target["session_file"], target["session_id"], target["alive"], target["auto_resume"])
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, has_touch=width == 390)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_browser_mocks(page)
            fixture, requests, _ = api_fixture(page, states=states)
            page.goto(static_url)
            open_edit(page, UUIDS[0])
            percent = page.locator("#history-archive-percent")

            for invalid in ("", "0", "96", "1.5"):
                percent.fill(invalid)
                page.locator("#archive-history").click()
                playwright.expect(page.locator("#history-archive-error")).to_contain_text(
                    "whole percentage from 1 through 95"
                )
            assert not archive_posts(requests)

            percent.fill("50")
            cancelled = []
            page.once("dialog", lambda dialog: (cancelled.append(dialog.message), dialog.dismiss()))
            page.locator("#archive-history").click()
            page.wait_for_timeout(30)
            assert len(cancelled) == 1
            assert not archive_posts(requests)

            confirmations = []
            page.once("dialog", lambda dialog: (confirmations.append(dialog.message), dialog.accept()))
            page.locator("#archive-history").click()
            playwright.expect(page.locator("#history-archive-state")).to_have_text("completed")
            posts = archive_posts(requests)
            assert len(posts) == 1
            method, path, body = posts[0]
            assert method == "POST"
            assert path == f"/api/agents/{UUIDS[0]}/archive-history"
            assert body["percent"] == 50
            assert re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                body["request_id"],
                re.IGNORECASE,
            )
            warning = confirmations[0]
            assert "serialized model-content bytes, not tokens" in warning
            assert "whole user/tool batches" in warning
            assert "complete raw source" in warning
            assert "Old prompts, images, thinking, tool calls, and outputs" in warning
            assert "No LLM is called" in warning
            assert "stays stopped until you manually wake it" in warning
            assert target["context_archive"]["actual_percent"] != body["percent"]
            assert (
                target["session_file"],
                target["session_id"],
                target["alive"],
                target["auto_resume"],
            ) == original_session
            assert fixture["local"]["agents"][0]["alive"] is False
            assert fixture["local"]["agents"][0]["auto_resume"] is False
            playwright.expect(page.locator("#toast")).to_contain_text("without waking")

            close_edit(page)
            open_edit(page, UUIDS[0])
            playwright.expect(page.locator("#history-archive-state")).to_have_text("completed")
            playwright.expect(page.locator("#history-archive-result")).to_contain_text("41.3%")
            assert_no_runtime_mutations(requests)
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_pending_archive_mutually_gates_actions_and_cannot_retarget_another_edit(static_url, width):
    states = fixture_states()
    for agent in states["local"]["agents"][:2]:
        agent.pop("context_archive", None)
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, has_touch=width == 390)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_browser_mocks(page)
            _, requests, _ = api_fixture(page, states=states)
            page.goto(static_url)
            page.evaluate(
                """() => Object.assign(window.archiveFetchMock, {
                  mode: 'defer', source: '/sessions/raw-0.jsonl', sessionId: 'session-0',
                  response: {}
                })"""
            )
            open_edit(page, UUIDS[0])
            page.locator('#edit-form [name="name"]').fill("Edited locally while pending")
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#archive-history").click()
            playwright.expect(page.locator("#archive-history")).to_have_text("Archiving…")
            playwright.expect(page.locator("#archive-history")).to_be_disabled()
            playwright.expect(page.locator("#history-archive-percent")).to_be_disabled()
            for selector in (
                "#meta-compact-toggle",
                "#start-meta-compact",
                "#compact-tool-inputs",
                "#revert-tool-input-compaction",
                "#repair-tool-outputs",
                "#save-agent",
                "#forget-agent",
            ):
                playwright.expect(page.locator(selector)).to_be_disabled()
            for field in ("name", "goal", "status", "auto_resume"):
                playwright.expect(page.locator(f'#edit-form [name="{field}"]')).to_be_enabled()
            assert page.locator('#edit-form [name="name"]').input_value() == (
                "Edited locally while pending"
            )

            close_edit(page)
            target_card = page.locator(f'.agent-card[data-agent-id="{UUIDS[0]}"]')
            target_card.locator(".card-toggle").click()
            playwright.expect(target_card.get_by_role("button", name="Wake", exact=True)).to_be_disabled()
            open_edit(page, UUIDS[1])
            playwright.expect(page.locator('#edit-form [name="name"]')).to_have_value("Failed archive")
            assert page.locator("#history-archive-state").inner_text() == ""

            assert page.evaluate("archiveFetchMock.release(0)") is True
            page.wait_for_timeout(50)
            playwright.expect(page.locator('#edit-form [name="name"]')).to_have_value("Failed archive")
            assert page.locator("#history-archive-state").inner_text() == ""
            assert page.locator("#toast").is_hidden()
            assert len(page.evaluate("archiveFetchMock.calls")) == 1
            assert not archive_posts(requests)
            assert not [item for item in requests if item[0] in {"POST", "PATCH", "DELETE"}]
            assert_no_runtime_mutations(requests)
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_uncertain_503_and_late_cross_swarm_response_never_retry_or_leak(static_url, width):
    states = fixture_states()
    states["local"]["agents"][0].pop("context_archive", None)
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, has_touch=width == 390)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_browser_mocks(page)
            _, requests, _ = api_fixture(page, states=states)
            page.goto(static_url)
            page.evaluate(
                """() => Object.assign(window.archiveFetchMock, {
                  mode: '503', source: '/sessions/raw-0.jsonl', sessionId: 'session-0'
                })"""
            )
            open_edit(page, UUIDS[0])
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#archive-history").click()
            playwright.expect(page.locator("#history-archive-error")).to_contain_text(
                "outcome is uncertain"
            )
            error_text = page.locator("#history-archive-error").inner_text()
            match = re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                error_text,
                re.IGNORECASE,
            )
            assert match, error_text
            request_id = match.group(0)
            assert page.evaluate("archiveFetchMock.calls[0].body.request_id") == request_id
            playwright.expect(page.locator("#archive-history")).to_be_disabled()
            page.locator("#archive-history").click(force=True)
            page.wait_for_timeout(30)
            assert len(page.evaluate("archiveFetchMock.calls")) == 1

            # A different stopped agent can still start a request. Its late response belongs
            # to local state and must not affect the remote agent with the same UUID.
            close_edit(page)
            page.evaluate(
                """() => Object.assign(window.archiveFetchMock, {
                  mode: 'defer', source: '/sessions/raw-1.jsonl', sessionId: 'session-1',
                  response: {}, pending: []
                })"""
            )
            open_edit(page, UUIDS[1])
            page.locator("#history-archive-percent").fill("95")
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#archive-history").click()
            playwright.expect(page.locator("#archive-history")).to_have_text("Archiving…")
            close_edit(page)
            page.locator("#swarm-selector").select_option("peer/a")
            playwright.expect(page.locator(".agent-card")).to_have_count(1)
            open_edit(page, UUIDS[0])
            playwright.expect(page.locator("#history-archive-percent")).to_have_value("95")
            # The input is shared DOM state, but no local report/status or toast may leak.
            assert page.locator("#history-archive-state").inner_text() == ""
            page.evaluate("document.querySelector('#toast').hidden = true")
            assert page.evaluate("archiveFetchMock.release(0)") is True
            page.wait_for_timeout(50)
            assert page.locator("#history-archive-state").inner_text() == ""
            assert page.locator("#toast").is_hidden()
            calls = page.evaluate("archiveFetchMock.calls")
            assert len(calls) == 2
            assert calls[1]["body"]["percent"] == 95
            assert calls[1]["path"] == f"/api/agents/{UUIDS[1]}/archive-history"
            assert not archive_posts(requests)
            assert_no_runtime_mutations(requests)
            assert not errors
        finally:
            browser.close()
