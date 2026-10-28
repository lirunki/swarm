"""Browser coverage for the create-footer adoption menu and combined fleet filters."""

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Expose the dashboard's static-server fixture to this module."""
    return _dashboard_static_url


def agent(agent_id, name, status, *, goal="Fixture goal"):
    return {
        "id": agent_id,
        "name": name,
        "goal": goal,
        "status": status,
        "alive": status == "running",
        "parent_id": None,
        "backend": "pi",
        "managed": True,
        "auto_resume": False,
        "activity": f"Activity for {name}",
        "folder": f"/fixture/{agent_id}",
        "notes": "",
        "session_file": f"/sessions/{agent_id}.jsonl",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def fixture_state():
    return {
        "agents": [
            agent("run-needle", "Needle Runner", "running"),
            agent("idle-needle", "Needle Idle", "idle"),
            agent("waiting-other", "Other Worker", "waiting"),
            agent("done-other", "Finished Worker", "done"),
        ],
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }


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


def install_browser_mocks(page):
    page.add_init_script(
        """(() => {
            window.modalTransitions = [];
            const showModal = HTMLDialogElement.prototype.showModal;
            HTMLDialogElement.prototype.showModal = function () {
                window.modalTransitions.push({
                    id: this.id,
                    createOpen: !!document.querySelector('#create-dialog[open]')
                });
                return showModal.call(this);
            };
            window.WebSocket = class {
                static OPEN = 1;
                constructor() {
                    this.readyState = 1;
                    setTimeout(() => this.onopen?.(), 0);
                }
                close() { this.readyState = 3; }
                send() {}
            };
        })();"""
    )


def mock_api(page, state):
    requests = []

    def respond(route):
        request = route.request
        path = urlsplit(request.url).path
        requests.append((request.method, path))
        if path == "/api/federation/nodes":
            value = {"enabled": False, "relay_online": False, "nodes": []}
        elif path == "/api/federation/settings":
            value = {
                "enabled": False,
                "connected": False,
                "status": "disconnected",
                "has_api_key": False,
                "relay_url": "",
                "relay_node_id": "browser-fixture",
                "relay_name": "Browser fixture",
                "last_error": None,
            }
        elif path == "/api/config/models":
            value = {"default": "fixture/default", "models": [], "error": None}
        elif path in ("/api/pi/sessions", "/api/grok/sessions"):
            value = {"sessions": [], "errors": {}, "roots": []}
        else:
            value = state
        route.fulfill(content_type="application/json", body=json.dumps(value))

    page.route("**/api/**", no_auth_mock(respond))
    return requests


def assert_inside(inner, outer, tolerance=1):
    assert inner["x"] >= outer["x"] - tolerance, (inner, outer)
    assert inner["y"] >= outer["y"] - tolerance, (inner, outer)
    assert inner["x"] + inner["width"] <= outer["x"] + outer["width"] + tolerance, (
        inner,
        outer,
    )
    assert inner["y"] + inner["height"] <= outer["y"] + outer["height"] + tolerance, (
        inner,
        outer,
    )


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_create_footer_adoption_menu_layout_and_safe_modal_transition(static_url, width):
    state = fixture_state()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_browser_mocks(page)
            requests = mock_api(page, state)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(4)

            assert page.locator("#collapse-all").count() == 0
            assert page.locator(".mission-toolbar #adoption-menu").count() == 0
            assert (
                page.locator("#create-dialog .create-dialog-footer > #adoption-menu").count() == 1
            )

            page.locator("#create-button").click()
            dialog = page.locator("#create-dialog")
            footer = dialog.locator(".create-dialog-footer")
            adopt = page.locator("#adoption-menu-button")
            cancel = footer.locator("button[data-close]")
            launch = footer.locator('button[type="submit"]')
            playwright.expect(dialog).to_be_visible()
            for control in (adopt, cancel, launch):
                playwright.expect(control).to_be_visible()
                playwright.expect(control).to_be_enabled()

            footer_box = footer.bounding_box()
            boxes = [control.bounding_box() for control in (adopt, cancel, launch)]
            centers = [box["y"] + box["height"] / 2 for box in boxes]
            assert max(centers) - min(centers) < 1
            assert abs(boxes[0]["x"] - footer_box["x"]) < 1
            assert (
                abs(boxes[-1]["x"] + boxes[-1]["width"] - footer_box["x"] - footer_box["width"]) < 1
            )
            assert boxes[0]["x"] + boxes[0]["width"] <= boxes[1]["x"]
            assert boxes[1]["x"] + boxes[1]["width"] <= boxes[2]["x"]

            adopt.click()
            menu = page.locator("#adoption-options")
            playwright.expect(menu).to_be_visible()
            assert menu.get_by_role("menuitem").all_text_contents() == [
                "pi session",
                "Grok session",
            ]
            menu_box = menu.bounding_box()
            button_box = adopt.bounding_box()
            viewport = {"x": 0, "y": 0, "width": width, "height": 1000}
            assert menu_box["y"] + menu_box["height"] <= button_box["y"] + 1
            assert_inside(menu_box, viewport)
            assert page.evaluate(
                """() => {
                    const menu = document.querySelector('#adoption-options');
                    const r = menu.getBoundingClientRect();
                    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                    return !!hit && menu.contains(hit);
                }"""
            )

            cancel.click()
            playwright.expect(dialog).to_be_hidden()
            page.locator("#create-button").click()
            playwright.expect(dialog).to_be_visible()
            playwright.expect(menu).to_be_hidden()
            playwright.expect(adopt).to_have_attribute("aria-expanded", "false")

            for choice, target in (
                ("#adopt-button", "#adopt-dialog"),
                ("#import-grok-button", "#import-grok-dialog"),
            ):
                # Required create fields deliberately remain blank. Choosing an alternative
                # must bypass create-form validation/submission and open only its target.
                assert page.locator('#create-form [name="name"]').input_value() == ""
                assert page.locator('#create-form [name="goal"]').input_value() == ""
                page.evaluate("window.modalTransitions = []")
                adopt.click()
                page.locator(choice).click()
                playwright.expect(dialog).to_be_hidden()
                playwright.expect(page.locator(target)).to_be_visible()
                assert page.evaluate("window.modalTransitions") == [
                    {"id": target[1:], "createOpen": False}
                ]
                assert not [request for request in requests if request[0] == "POST"]
                page.locator(f"{target} [data-close]").first.click()
                page.locator("#create-button").click()
                playwright.expect(dialog).to_be_visible()
                playwright.expect(menu).to_be_hidden()

            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_search_inside_status_menu_combines_live_filters_and_preserves_text_editing(
    static_url, width
):
    state = fixture_state()
    with playwright.sync_playwright() as runner:
        browser = launch_chromium(runner)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_browser_mocks(page)
            mock_api(page, state)
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(4)

            assert page.locator("#collapse-all").count() == 0
            assert page.locator("#status-filter-menu #search").count() == 1
            assert page.locator("#status-filter-menu").evaluate(
                "menu => menu.contains(document.querySelector('#search'))"
            )

            page.locator("#agents").focus()
            page.keyboard.press("/")
            menu = page.locator("#status-filter-menu")
            search = page.locator("#search")
            playwright.expect(menu).to_be_visible()
            playwright.expect(search).to_be_focused()
            search.fill("Needle")
            playwright.expect(page.locator(".agent-card")).to_have_count(2)
            playwright.expect(page.locator("#status-filter-search")).to_be_visible()

            page.locator('input[data-status="all"]').uncheck()
            page.locator('input[data-status="running"]').check()
            playwright.expect(page.locator(".agent-card")).to_have_count(1)
            playwright.expect(page.locator('[data-agent-id="run-needle"]')).to_be_visible()
            playwright.expect(search).to_have_value("Needle")
            playwright.expect(page.locator("#status-filter-label")).to_have_text("Running")

            # Dropdown navigation keys must retain native text-input behavior instead of
            # moving focus into status checkboxes.
            search.focus()
            search.evaluate("input => input.setSelectionRange(3, 3)")
            page.keyboard.press("ArrowDown")
            playwright.expect(search).to_be_focused()
            assert search.evaluate("input => [input.selectionStart, input.selectionEnd]") == [6, 6]
            search.evaluate("input => input.setSelectionRange(3, 3)")
            page.keyboard.press("ArrowUp")
            playwright.expect(search).to_be_focused()
            assert search.evaluate("input => [input.selectionStart, input.selectionEnd]") == [0, 0]
            search.evaluate("input => input.setSelectionRange(3, 3)")
            page.keyboard.press("Home")
            playwright.expect(search).to_be_focused()
            assert search.evaluate("input => [input.selectionStart, input.selectionEnd]") == [0, 0]
            page.keyboard.press("End")
            playwright.expect(search).to_be_focused()
            assert search.evaluate("input => [input.selectionStart, input.selectionEnd]") == [6, 6]

            page.keyboard.press("Escape")
            playwright.expect(menu).to_be_hidden()
            playwright.expect(page.locator("#status-filter-button")).to_be_focused()
            playwright.expect(search).to_have_value("Needle")
            playwright.expect(page.locator("#status-filter-search")).to_be_visible()

            page.locator("#status-filter-button").click()
            playwright.expect(menu).to_be_visible()
            page.locator("#agents").dispatch_event("pointerdown")
            playwright.expect(menu).to_be_hidden()
            playwright.expect(search).to_have_value("Needle")
            playwright.expect(page.locator(".agent-card")).to_have_count(1)

            # A live state replacement must preserve both independent filter dimensions.
            state["agents"].append(agent("run-needle-live", "Second Needle", "running"))
            state["agents"].append(agent("idle-needle-live", "Third Needle", "idle"))
            page.evaluate("data => applyState(data)", state)
            playwright.expect(page.locator(".agent-card")).to_have_count(2)
            playwright.expect(page.locator('[data-agent-id="run-needle-live"]')).to_be_visible()
            playwright.expect(search).to_have_value("Needle")
            assert page.locator('input[data-status="running"]').is_checked()
            assert not page.locator('input[data-status="idle"]').is_checked()
            playwright.expect(page.locator("#status-filter-search")).to_be_visible()

            page.locator("#status-filter-button").click()
            search.fill("")
            playwright.expect(page.locator("#status-filter-search")).to_be_hidden()
            playwright.expect(page.locator(".agent-card")).to_have_count(2)
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()
