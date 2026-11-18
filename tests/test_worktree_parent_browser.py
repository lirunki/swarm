"""Mocked browser coverage for the New agent worktree-parent field."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from test_dashboard_browser import fixture_state, no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

REMOTE = "remoteA"


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Reuse the dashboard's static-only HTTP server."""
    return _dashboard_static_url


def dashboard_state(owner):
    value = fixture_state()
    folder = "/repos/app" if owner == "local" else "/srv/remote app"
    value["agents"][0]["folder"] = folder
    value["home"] = "/home/user" if owner == "local" else "/srv/remote-user"
    return value


class WorktreeAPI:
    """Owner-aware API double; creation is captured and never reaches Git or pi."""

    def __init__(self):
        self.requests = []
        self.states = {owner: dashboard_state(owner) for owner in ("local", REMOTE)}
        self.agent_ids = iter(
            (
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            )
        )

    @staticmethod
    def owner_endpoint(path):
        if path.startswith("/api/federation/") and "/proxy/" in path:
            encoded, endpoint = path.split("/api/federation/", 1)[1].split("/proxy", 1)
            return unquote(encoded), endpoint
        return "local", path

    def posts(self, owner=None):
        return [
            body
            for method, request_owner, endpoint, body in self.requests
            if method == "POST"
            and endpoint == "/api/agents"
            and (owner is None or request_owner == owner)
        ]

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        owner, endpoint = self.owner_endpoint(path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, owner, endpoint, copy.deepcopy(body)))
        status = 200

        if path == "/api/federation/nodes":
            value = {
                "enabled": True,
                "relay_online": True,
                "nodes": [
                    {
                        "id": REMOTE,
                        "name": "Remote A",
                        "online": True,
                        "capabilities": {},
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
                "relay_node_id": "fixture-local",
                "relay_name": "Fixture local",
                "last_error": None,
            }
        elif endpoint == "/api/state":
            value = copy.deepcopy(self.states[owner])
        elif endpoint == "/api/config/models":
            value = {
                "default": "fixture/pi-model",
                "models": [{"id": "fixture/pi-model", "thinking": "yes"}],
                "error": None,
            }
        elif endpoint == "/api/folders":
            status = 403
            value = {"detail": "Permission denied by fixture"}
        elif endpoint == "/api/agents" and request.method == "POST":
            value = {
                "id": next(self.agent_ids),
                "name": body["name"],
                "goal": body["goal"],
                "status": "idle",
                "alive": False,
                "parent_id": None,
                "backend": "pi",
                "managed": True,
                "auto_resume": False,
                "activity": "Queued by browser fixture",
                "folder": body["folder"],
                "notes": "",
                "session_file": "",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        elif endpoint == "/api/notifications":
            value = {"revision": 1, "count": 0, "notifications": []}
        elif endpoint == "/api/fleet/restarts":
            value = {"jobs": []}
        elif endpoint == "/api/browser/instances":
            value = {"revision": 1, "browsers": []}
        elif endpoint == "/api/browser/discovery":
            value = {"browsers": [], "unavailable": []}
        elif endpoint in ("/api/pi/sessions", "/api/grok/sessions"):
            value = {"sessions": [], "errors": {}, "roots": []}
        else:
            value = {}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


def launch_chromium(runner):
    try:
        return runner.chromium.launch(headless=True)
    except playwright.Error as exc:
        if "Executable doesn't exist" in str(exc) or "error while loading shared libraries" in str(
            exc
        ):
            pytest.skip(
                f"Optional Chromium runtime unavailable: {Path(runner.chromium.executable_path)}"
            )
        raise


def install_mocks(page):
    page.add_init_script(
        """window.WebSocket = class {
            static OPEN = 1;
            constructor() { this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
            close() { this.readyState = 3; }
            send() {}
        };"""
    )


def open_create_form(page):
    page.locator("#add-menu-button").click()
    page.locator("#create-button").click()
    playwright.expect(page.locator("#create-dialog")).to_be_visible()
    playwright.expect(page.locator('#create-form [name="model"]')).to_be_enabled()


def configure_worktree(page, folder, branch="feature/x"):
    form = page.locator("#create-form")
    form.locator('[name="folder"]').select_option(folder)
    form.locator('[name="create_worktree"]').check()
    form.locator('[name="branch"]').fill(branch)
    return form


def assert_clean_page(page, errors):
    assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
    assert not errors


@pytest.mark.parametrize("width", [390, 1280])
def test_parent_preview_events_permission_error_and_responsive_layout(static_url, width):
    api = WorktreeAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context = browser.new_context(viewport={"width": width, "height": 1000})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_mocks(page)
            page.route("**/api/**", no_auth_mock(api.route))
            page.goto(static_url)
            open_create_form(page)

            form = configure_worktree(page, "/repos/app")
            destination = page.locator("#worktree-destination")
            playwright.expect(destination).to_have_text("/repos/app_feature_x")
            assert not api.posts()

            parent = form.locator('[name="worktree_parent"]')
            parent.fill("/home/user/git/")
            playwright.expect(destination).to_have_text("/home/user/git/feature_x")
            assert page.evaluate(
                "localStorage.getItem('swarm.create-worktree-parent.local')"
            ) is None
            parent.dispatch_event("change")
            assert page.evaluate(
                "localStorage.getItem('swarm.create-worktree-parent.local')"
            ) == "/home/user/git"
            assert not api.posts()

            labels = page.locator(".worktree-location-row > label")
            first, second = labels.nth(0).bounding_box(), labels.nth(1).bounding_box()
            if width == 1280:
                assert second["x"] >= first["x"] + first["width"]
                assert abs(first["y"] - second["y"]) < 1
            else:
                assert second["y"] >= first["y"] + first["height"]
                assert abs(first["x"] - second["x"]) < 1
            dialog = page.locator("#create-dialog").bounding_box()
            for control in labels.locator("input").all():
                box = control.bounding_box()
                assert box["x"] >= dialog["x"]
                assert box["x"] + box["width"] <= dialog["x"] + dialog["width"] + 1

            long_parent = "  /home/user/Work trees/" + "nested path " * 12 + "  "
            parent.fill(long_parent)
            expected = long_parent.strip().rstrip("/") + "/feature_x"
            playwright.expect(destination).to_have_text(expected)
            assert parent.input_value() == long_parent

            page.locator("#browse-folder").click()
            playwright.expect(page.locator("#folder-browser-message")).to_contain_text(
                "Permission denied by fixture"
            )
            assert parent.input_value() == long_parent
            page.locator("#folder-dialog").get_by_role("button", name="Cancel", exact=True).click()
            assert parent.input_value() == long_parent
            assert not api.posts()
            assert_clean_page(page, errors)
        finally:
            browser.close()


def test_parent_preference_is_scoped_persistent_and_post_contract_is_exact(static_url):
    api = WorktreeAPI()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 1000})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_mocks(page)
            page.route("**/api/**", no_auth_mock(api.route))
            page.goto(static_url)

            open_create_form(page)
            form = configure_worktree(page, "/repos/app")
            parent = form.locator('[name="worktree_parent"]')
            parent.fill("  /home/user/git/  ")
            parent.dispatch_event("change")
            page.locator("#create-dialog .create-dialog-footer [data-close]").click()
            open_create_form(page)
            assert form.locator('[name="worktree_parent"]').input_value() == "/home/user/git"
            # The folder is the other user selection worth retaining; branch and checkbox
            # intentionally have no persistence requirement.
            form.locator('[name="folder"]').select_option("/repos/app")
            assert not api.posts()

            # Blank is a real preference: reopening uses the normal sibling destination.
            configure_worktree(page, "/repos/app")
            parent.fill("   ")
            parent.dispatch_event("change")
            assert page.evaluate(
                "Object.prototype.hasOwnProperty.call("
                "localStorage, 'swarm.create-worktree-parent.local')"
            )
            assert page.evaluate(
                "localStorage.getItem('swarm.create-worktree-parent.local')"
            ) == ""
            page.locator("#create-dialog .create-dialog-footer [data-close]").click()
            open_create_form(page)
            form = configure_worktree(page, "/repos/app")
            assert parent.input_value() == ""
            playwright.expect(page.locator("#worktree-destination")).to_have_text(
                "/repos/app_feature_x"
            )

            # A later nonblank value survives a full reload of the same browser origin.
            parent.fill(" /home/user/git/ ")
            parent.dispatch_event("change")
            page.locator("#create-dialog .create-dialog-footer [data-close]").click()
            page.reload()
            open_create_form(page)
            assert parent.input_value() == "/home/user/git"
            assert not api.posts()

            # The remote owner starts clean and stores a host-appropriate path independently.
            page.locator("#create-dialog .create-dialog-footer [data-close]").click()
            page.locator("#swarm-selector").select_option(REMOTE)
            page.wait_for_function("activeSwarm === 'remoteA' && state.home === '/srv/remote-user'")
            assert any(
                method == "GET" and owner == REMOTE and endpoint == "/api/state"
                for method, owner, endpoint, _body in api.requests
            )
            open_create_form(page)
            remote_form = configure_worktree(page, "/srv/remote app", "release/deep/path")
            remote_parent = remote_form.locator('[name="worktree_parent"]')
            assert remote_parent.input_value() == ""
            playwright.expect(page.locator("#worktree-destination")).to_have_text(
                "/srv/remote app_release_deep_path"
            )
            remote_parent.fill("  /mnt/remote Worktrees/  ")
            remote_parent.dispatch_event("change")
            playwright.expect(page.locator("#worktree-destination")).to_have_text(
                "/mnt/remote Worktrees/release_deep_path"
            )
            assert page.evaluate(
                "Object.fromEntries(["
                "'local','remoteA'].map(owner => [owner, "
                "localStorage.getItem(`swarm.create-worktree-parent.${owner}`)]))"
            ) == {"local": "/home/user/git", REMOTE: "/mnt/remote Worktrees"}
            assert not api.posts()

            remote_form.locator('[name="name"]').fill("Remote fixture agent")
            remote_form.locator('[name="goal"]').fill("Capture only; launch nothing")
            remote_form.locator('button[type="submit"]').click()
            playwright.expect(page.locator("#create-dialog")).to_be_hidden()
            playwright.expect(
                page.locator(
                    '.agent-card[data-agent-id="11111111-1111-4111-8111-111111111111"]'
                )
            ).to_have_count(1)
            assert api.posts(REMOTE) == [
                {
                    "name": "Remote fixture agent",
                    "goal": "Capture only; launch nothing",
                    "folder": "/srv/remote app",
                    "create_worktree": True,
                    "branch": "release/deep/path",
                    "worktree_parent": "/mnt/remote Worktrees/",
                    "automatic_worktree_path": True,
                }
            ]

            # A regular agent request must not leak branch, parent, or automatic path state.
            open_create_form(page)
            plain = page.locator("#create-form")
            plain.locator('[name="name"]').fill("Plain fixture agent")
            plain.locator('[name="goal"]').fill("No worktree")
            plain.locator('[name="folder"]').select_option("/srv/remote app")
            plain.locator('button[type="submit"]').click()
            playwright.expect(page.locator("#create-dialog")).to_be_hidden()
            assert api.posts(REMOTE)[1] == {
                "name": "Plain fixture agent",
                "goal": "No worktree",
                "folder": "/srv/remote app",
                "create_worktree": False,
                "automatic_worktree_path": False,
            }
            assert len(api.posts()) == 2
            assert_clean_page(page, errors)
        finally:
            browser.close()
