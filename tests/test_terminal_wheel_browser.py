"""Real Chromium coverage for the browser wheel -> xterm -> terminal WebSocket path."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_dashboard_browser import no_auth_mock
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")

LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")
SGR_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)M")


@pytest.fixture(name="static_url")
def imported_static_url(_dashboard_static_url):  # noqa: F811
    """Expose the dashboard's isolated static server fixture."""
    return _dashboard_static_url


def agent(agent_id):
    return {
        "id": agent_id,
        "name": f"Wheel {agent_id}",
        "goal": "Exercise the browser terminal wheel pipeline",
        "status": "running",
        "alive": True,
        "parent_id": None,
        "backend": "pi",
        "managed": True,
        "auto_resume": False,
        "activity": "Waiting for browser input",
        "folder": f"/fixture/{agent_id}",
        "notes": "",
        "session_file": f"/sessions/{agent_id}.jsonl",
        "session_id": f"session-{agent_id}",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def fixture_state():
    return {
        "agents": [agent("wheel-a"), agent("wheel-b")],
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }


class MockAPI:
    def __init__(self):
        self.state = fixture_state()
        self.requests = []

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        self.requests.append((request.method, path, request.post_data))
        if path == "/api/federation/nodes":
            value = {"enabled": False, "relay_online": False, "nodes": []}
        elif path == "/api/federation/settings":
            value = {
                "enabled": False,
                "connected": False,
                "status": "disconnected",
                "has_api_key": False,
                "relay_url": "",
                "relay_node_id": "wheel-browser",
                "relay_name": "Wheel browser",
                "last_error": None,
            }
        elif path == "/api/notifications":
            value = {"revision": 1, "count": 0, "notifications": []}
        elif path.endswith("/api/fleet/restarts"):
            value = {"jobs": []}
        else:
            value = self.state
        route.fulfill(content_type="application/json", body=json.dumps(value))


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


def install_websocket(page):
    page.add_init_script(
        r"""(() => {
          window.sentFrames = [];
          window.socketLog = [];
          window.nativeWheels = [];
          let serial = 0;
          window.WebSocket = class {
            static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
            constructor(url) {
              this.url = String(url); this.serial = ++serial; this.readyState = 0;
              window.socketLog.push({url: this.url, serial: this.serial});
              setTimeout(() => {
                if (this.readyState !== 0) return;
                this.readyState = 1; this.onopen?.();
                if (this.url.includes('/ws/terminal/')) {
                  const bytes = new TextEncoder().encode(
                    '\x1b[?1049h\x1b[?1000h\x1b[?1006hWHEEL-READY\r\n'
                  );
                  this.onmessage?.({data: bytes.buffer});
                }
              }, 0);
            }
            close() { this.readyState = 3; }
            send(raw) {
              let value;
              try { value = JSON.parse(raw); } catch { value = raw; }
              window.sentFrames.push({url: this.url, serial: this.serial, value});
            }
          };
        })();"""
    )


def open_terminal(page, agent_id):
    page.locator(f'[data-agent-id="{agent_id}"] .card-terminal').click()
    page.wait_for_function(
        """id => {
          const t = terminals.get(id);
          return t?.ws?.readyState === WebSocket.OPEN &&
            ['vt200','drag','any'].includes(t.term?.modes.mouseTrackingMode) &&
            t.term.buffer.active.type === 'alternate';
        }""",
        arg=agent_id,
    )
    page.evaluate(
        """id => {
          const t = terminals.get(id);
          if (!t.screen.dataset.wheelObserved) {
            t.screen.dataset.wheelObserved = 'true';
            t.screen.addEventListener('wheel', event => window.nativeWheels.push({
              trusted: event.isTrusted, dx: event.deltaX, dy: event.deltaY,
              ctrl: event.ctrlKey, meta: event.metaKey,
              multiplierContext: t.wheelSpeed.context?.count ?? null,
            }), {capture: true, passive: true});
          }
        }""",
        agent_id,
    )


def activate_terminal(page, agent_id):
    page.get_by_role("button", name=re.compile(rf"^Wheel {re.escape(agent_id)} ·")).click()
    page.wait_for_function("id => !terminals.get(id).pane.hidden", arg=agent_id)


def reset_frames(page):
    # Resize frames are deliberately discarded before every input assertion.
    page.wait_for_timeout(50)
    page.evaluate("window.sentFrames = []; window.nativeWheels = []")


def input_data(page, agent_id):
    marker = f"/ws/terminal/{agent_id}?"
    return page.evaluate(
        """marker => window.sentFrames
          .filter(frame => frame.url.includes(marker) && frame.value?.type === 'input')
          .map(frame => frame.value.data)""",
        marker,
    )


def mouse_reports(chunks):
    return [tuple(map(int, match)) for chunk in chunks for match in SGR_MOUSE.findall(chunk)]


def wheel(page, agent_id, dx, dy):
    screen = page.locator('.terminal-pane:not([hidden]) .xterm-screen')
    box = screen.bounding_box()
    assert box and box["width"] > 4 and box["height"] > 4
    point = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    reset_frames(page)
    page.mouse.move(*point)
    page.mouse.wheel(dx, dy)
    page.wait_for_timeout(100)
    chunks = input_data(page, agent_id)
    return chunks, mouse_reports(chunks), page.evaluate("window.nativeWheels"), point


def open_settings(page):
    page.locator("#notifications-button").click()
    playwright.expect(page.locator("#notifications-dialog")).to_be_visible()
    page.get_by_role("button", name="Settings").click()
    playwright.expect(page.locator("#settings-dialog")).to_be_visible()


def save_multiplier(page, value):
    open_settings(page)
    field = page.locator("#mouse-wheel-multiplier")
    field.fill(str(value))
    page.locator("#mouse-wheel-save").click()
    playwright.expect(page.locator("#mouse-wheel-status")).to_have_text(f"Saved · {value}×")
    page.locator('#settings-dialog [data-close][aria-label="Close settings"]').click()
    playwright.expect(page.locator("#settings-dialog")).to_be_hidden()


def assert_repeated_trace(baseline, scaled, count):
    assert baseline, "the trusted browser wheel must make xterm emit an SGR mouse report"
    assert len(scaled) == len(baseline) * count
    assert scaled == [report for report in baseline for _ in range(count)]


def test_trusted_wheel_uses_live_setting_persists_and_scales_existing_tabs(static_url):
    """Prove the app's native wheel -> xterm onData -> JSON input pipeline end to end."""
    api = MockAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_websocket(page)
            page.route("**/api/**", no_auth_mock(api.route))
            page.goto(static_url)
            open_terminal(page, "wheel-a")

            open_settings(page)
            field = page.locator("#mouse-wheel-multiplier")
            assert field.input_value() == "1"
            assert field.get_attribute("min") == "1"
            assert field.get_attribute("max") == "20"
            page.locator('#settings-dialog [data-close][aria-label="Close settings"]').click()

            before_socket = page.evaluate("terminals.get('wheel-a').ws.serial")
            up_one_chunks, up_one, native, point = wheel(page, "wheel-a", 0, -120)
            assert up_one_chunks
            assert all(button == 64 for button, _column, _row in up_one)
            assert native and all(item["trusted"] for item in native)
            assert all(item["dy"] == -120 and item["dx"] == 0 for item in native)
            down_one_chunks, down_one, native, _ = wheel(page, "wheel-a", 0, 120)
            assert down_one_chunks
            assert all(button == 65 for button, _column, _row in down_one)
            assert native and all(item["trusted"] for item in native)

            save_multiplier(page, 3)
            assert page.evaluate("terminals.get('wheel-a').ws.serial") == before_socket
            up_three_chunks, up_three, native, same_point = wheel(page, "wheel-a", 0, -120)
            assert same_point == point
            assert native and all(item["trusted"] for item in native)
            assert_repeated_trace(up_one, up_three, 3)

            down_three_chunks, down_three, native, _ = wheel(page, "wheel-a", 0, 120)
            assert down_three_chunks and all(button == 65 for button, _column, _row in down_three)
            assert_repeated_trace(down_one, down_three, 3)
            assert {(column, row) for _button, column, row in down_three} == {
                (column, row) for _button, column, row in up_three
            }
            assert native and all(item["trusted"] for item in native)

            page.locator("#dashboard-tab").click()
            open_terminal(page, "wheel-b")
            second_socket = page.evaluate("terminals.get('wheel-b').ws.serial")
            _chunks, second_three, _native, _ = wheel(page, "wheel-b", 0, -120)
            assert_repeated_trace(up_one, second_three, 3)
            assert page.evaluate("terminals.get('wheel-b').ws.serial") == second_socket

            save_multiplier(page, 20)
            _chunks, upper_bound, _native, _ = wheel(page, "wheel-b", 0, -120)
            assert_repeated_trace(up_one, upper_bound, 20)
            assert page.evaluate("terminals.get('wheel-b').ws.serial") == second_socket

            page.reload()
            page.wait_for_function("() => terminals.size === 2")
            activate_terminal(page, "wheel-a")
            page.wait_for_function(
                """() => terminals.size === 2 && [...terminals.values()].every(t =>
                  t.ws?.readyState === WebSocket.OPEN &&
                  ['vt200','drag','any'].includes(t.term?.modes.mouseTrackingMode))"""
            )
            open_settings(page)
            assert page.locator("#mouse-wheel-multiplier").input_value() == "20"
            page.locator('#settings-dialog [data-close][aria-label="Close settings"]').click()
            assert page.evaluate("terminals.size") == 2
            assert len(
                page.evaluate("window.socketLog.filter(item => item.url.includes('/ws/terminal/'))")
            ) == 2
            assert not [request for request in api.requests if request[0] != "GET"]
            assert not errors
        finally:
            browser.close()


def test_untrusted_and_ineligible_wheels_never_scale_or_inject_terminal_keys(static_url):
    api = MockAPI()
    with playwright.sync_playwright() as runner:
        browser = chromium(runner)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            install_websocket(page)
            page.route("**/api/**", no_auth_mock(api.route))
            page.goto(static_url)
            open_terminal(page, "wheel-a")
            save_multiplier(page, 3)

            _chunks, trusted_baseline, native, point = wheel(page, "wheel-a", 0, -120)
            assert native and all(item["trusted"] for item in native)
            assert trusted_baseline

            reset_frames(page)
            assert page.evaluate(
                """({id, x, y}) => SwarmTerminalTouch.scrollTerminal(
                  terminals.get(id).term, -1, {x, y})""",
                {"id": "wheel-a", "x": point[0], "y": point[1]},
            )
            page.wait_for_timeout(50)
            synthetic = mouse_reports(input_data(page, "wheel-a"))
            assert len(synthetic) == 1
            assert all(button == 64 for button, _column, _row in synthetic)
            assert page.evaluate("window.nativeWheels.length") == 1
            assert page.evaluate("window.nativeWheels[0].trusted") is False

            reset_frames(page)
            page.keyboard.down("Control")
            page.mouse.move(*point)
            page.mouse.wheel(0, -120)
            page.keyboard.up("Control")
            page.wait_for_timeout(50)
            ctrl_reports = mouse_reports(input_data(page, "wheel-a"))
            assert len(ctrl_reports) == 1
            assert all(button == 80 for button, _column, _row in ctrl_reports)
            ctrl_trace = page.evaluate("window.nativeWheels")
            assert ctrl_trace and all(item["trusted"] and item["ctrl"] for item in ctrl_trace)

            horizontal_chunks, horizontal, horizontal_native, _ = wheel(
                page, "wheel-a", 120, 0
            )
            assert horizontal_chunks == [] and horizontal == []
            assert horizontal_native and all(item["trusted"] for item in horizontal_native)

            page.evaluate("terminals.get('wheel-a').copyText.active = true")
            copy_chunks, copy_reports, _native, _ = wheel(page, "wheel-a", 0, -120)
            assert copy_chunks == [] and copy_reports == []
            page.evaluate("terminals.get('wheel-a').copyText.active = false")

            page.evaluate(
                """id => terminals.get(id).term.write(
                  '\x1b[?1000l\x1b[?1006l\x1b[?1049l')""",
                "wheel-a",
            )
            page.wait_for_function(
                "id => terminals.get(id).term.modes.mouseTrackingMode === 'none' && terminals.get(id).term.buffer.active.type === 'normal'",
                arg="wheel-a",
            )
            normal_chunks, normal_reports, _native, _ = wheel(page, "wheel-a", 0, -120)
            assert normal_chunks == [] and normal_reports == []

            page.evaluate("id => terminals.get(id).term.write('\\x1b[?1049h')", "wheel-a")
            page.wait_for_function(
                "id => terminals.get(id).term.buffer.active.type === 'alternate'", arg="wheel-a"
            )
            alternate_chunks, alternate_reports, _native, _ = wheel(
                page, "wheel-a", 0, -120
            )
            assert alternate_chunks == [] and alternate_reports == []

            page.evaluate(
                "id => terminals.get(id).term.write('\\x1b[?1000h\\x1b[?1006h')", "wheel-a"
            )
            page.wait_for_function(
                "id => terminals.get(id).term.modes.mouseTrackingMode === 'vt200'", arg="wheel-a"
            )
            reset_frames(page)
            page.evaluate("id => terminals.get(id).term.focus()", "wheel-a")
            page.keyboard.type("a")
            page.wait_for_timeout(50)
            assert input_data(page, "wheel-a") == ["a"]

            all_input = "".join(
                frame["value"]["data"]
                for frame in page.evaluate("window.sentFrames")
                if frame["value"].get("type") == "input"
            )
            assert "\x1b[A" not in all_input and "\x1b[B" not in all_input
            assert not [request for request in api.requests if request[0] != "GET"]
            assert not errors
        finally:
            browser.close()
