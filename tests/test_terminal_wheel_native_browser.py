"""Trusted Chromium wheel events through the real WebSocket and tmux pane."""

import re
import shutil
import sys
import time

import pytest
from test_federation_integration import running_server
from test_terminal_wheel_browser import chromium

from swarm.models import Agent, Status
from swarm.server import create_app

playwright = pytest.importorskip("playwright.sync_api", reason="optional browser checks")
SGR_WHEEL_UP = re.compile(r"\x1b\[<64;\d+;\d+M")


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is required")
def test_native_wheel_multiplier_scales_real_tmux_scrollback(tmp_path):
    """A 3x setting sends three reports and moves real tmux about 3x as far."""
    fixture = tmp_path / "wheel-fixture"
    fixture.write_text(
        f"""#!{sys.executable}
import sys
for row in range(200):
    print(f"TMUX-WHEEL-ROW-{{row:03d}}", flush=True)
print("TMUX-WHEEL-READY", flush=True)
for _line in sys.stdin:
    pass
"""
    )
    fixture.chmod(0o700)

    app = create_app(tmp_path / "home", recover=False)
    controller = app.state.controller
    processes = controller.processes
    agent = controller.store.create(
        Agent(
            name="Native wheel",
            goal="Verify trusted wheel scaling in tmux",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            status=Status.running,
            spawn_state="ready",
        )
    )
    pid = processes._start(agent, [str(fixture)], tmp_path)
    agent = controller.store.update(agent.id, {"pid": pid})
    target = processes.target(agent) + ":0.0"

    def tmux_mode():
        value = processes.run(
            "display-message", "-p", "-t", target, "#{pane_in_mode}|#{scroll_position}"
        ).stdout.strip()
        active, _, position = value.partition("|")
        return active == "1", int(position or "0")

    def wait_for_position(minimum=1):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            active, position = tmux_mode()
            if active and position >= minimum:
                return position
            time.sleep(0.02)
        assert tmux_mode()[0] and tmux_mode()[1] >= minimum

    try:
        for _ in range(100):
            captured = processes.run("capture-pane", "-p", "-t", target, "-S", "-").stdout
            if "TMUX-WHEEL-READY" in captured:
                break
            time.sleep(0.02)
        assert "TMUX-WHEEL-READY" in captured
        assert len(re.findall(r"TMUX-WHEEL-ROW-\d{3}", captured)) == 200

        with playwright.sync_playwright() as runner, running_server(app) as port:
            browser = chromium(runner)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(f"http://127.0.0.1:{port}")
            page.locator(f'[data-agent-id="{agent.id}"] .card-terminal').click()
            page.wait_for_function(
                "id => ['vt200','drag','any'].includes(terminals.get(id)?.term?.modes.mouseTrackingMode)",
                arg=str(agent.id),
            )
            page.evaluate(
                """id => {
                    const ws = terminals.get(id).ws, send = ws.send.bind(ws);
                    window.wheelFrames = [];
                    ws.send = raw => {
                        const value = JSON.parse(raw);
                        if (value.type === 'input') window.wheelFrames.push(value.data);
                        send(raw);
                    };
                }""",
                str(agent.id),
            )
            box = page.locator(".terminal-pane:not([hidden]) .xterm-screen").bounding_box()
            assert box and box["width"] > 4 and box["height"] > 4
            point = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

            def trusted_wheel():
                page.evaluate("window.wheelFrames = []")
                page.mouse.move(*point)
                page.mouse.wheel(0, -120)
                page.wait_for_timeout(100)
                frames = page.evaluate("window.wheelFrames")
                return sum(len(SGR_WHEEL_UP.findall(frame)) for frame in frames)

            assert tmux_mode() == (False, 0)
            processes.run("copy-mode", "-e", "-t", target)
            assert tmux_mode() == (True, 0)
            baseline_reports = trusted_wheel()
            baseline_position = wait_for_position()
            assert baseline_reports == 1

            processes.run("send-keys", "-t", target, "q")
            for _ in range(100):
                if tmux_mode()[0] is False:
                    break
                time.sleep(0.02)
            assert tmux_mode() == (False, 0)
            processes.run("copy-mode", "-e", "-t", target)
            assert tmux_mode() == (True, 0)

            page.evaluate(
                """() => {
                    localStorage.setItem('swarm.terminal-wheel-multiplier', '3');
                    SwarmTerminalWheel.preferences.reload();
                }"""
            )
            assert page.evaluate(
                "localStorage.getItem('swarm.terminal-wheel-multiplier')"
            ) == "3"
            scaled_reports = trusted_wheel()
            scaled_position = wait_for_position(baseline_position * 2)
            assert scaled_reports == 3
            assert 2.5 <= scaled_position / baseline_position <= 3.5

            assert processes.pid(agent) == pid and processes.alive(agent)
            assert not any("terminal-history" in url for url in requests)
            assert not page.locator(".terminal-history").count()
            page.get_by_role("button", name="Close terminal tab").click()
            assert processes.alive(agent)
            assert not errors
    finally:
        try:
            controller.stop(str(agent.id))
        except (OSError, RuntimeError, ValueError):
            processes.stop(agent)
