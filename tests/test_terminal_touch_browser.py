"""Real Chromium touch -> xterm mouse reports -> PTY -> tmux copy-mode scrolling."""

import json
import shutil
import sys
import time
import urllib.request

import pytest
from test_federation_integration import running_server

from swarm.server import create_app

playwright = pytest.importorskip("playwright.sync_api", reason="optional browser checks")


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is required")
@pytest.mark.parametrize("width", [390, 1280])
def test_real_mobile_wheel_events_scroll_tmux_without_history_api(tmp_path, width):
    fake = tmp_path / "fake-pi"
    received = tmp_path / "received-input.txt"
    fake.write_text(f"""#!{sys.executable}
from pathlib import Path
import sys
for i in range(500):
    print(f"TMUX-ROW-{{i:04d}}", flush=True)
print("TMUX-READY", flush=True)
for line in sys.stdin:
    with Path({str(received)!r}).open("a") as output:
        output.write(line)
""")
    fake.chmod(0o700)
    app = create_app(tmp_path / "home", pi=str(fake), recover=False)
    processes = app.state.controller.processes
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(
                exc
            ) or "error while loading shared libraries" in str(exc):
                pytest.skip("Optional Chromium runtime unavailable")
            raise
        try:
            with running_server(app) as port:
                base = f"http://127.0.0.1:{port}"
                request = urllib.request.Request(
                    base + "/api/agents",
                    headers={"Content-Type": "application/json"},
                    data=json.dumps(
                        {"name": "Mobile", "goal": "Scroll live tmux", "folder": str(tmp_path)}
                    ).encode(),
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    agent = app.state.controller.store.get(json.load(response)["id"])
                assert not agent.last_error
                pid = processes.pid(agent)
                target = processes.target(agent) + ":0.0"
                for _ in range(100):
                    captured = processes.run("capture-pane", "-p", "-t", target, "-S", "-").stdout
                    if "TMUX-READY" in captured:
                        break
                    time.sleep(0.02)
                assert "TMUX-READY" in captured
                with urllib.request.urlopen(base + "/openapi.json", timeout=10) as response:
                    assert not any(
                        path.endswith("/terminal-history") for path in json.load(response)["paths"]
                    )
                page = browser.new_page(
                    viewport={"width": width, "height": 1000},
                    has_touch=True,
                    is_mobile=width == 390,
                )
                errors, requests = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append(request.url))
                page.goto(base)
                page.locator(f'[data-agent-id="{agent.id}"] .card-terminal').click()
                page.wait_for_function(
                    "id => ['vt200','drag','any'].includes(terminals.get(id)?.term?.modes.mouseTrackingMode)",
                    arg=str(agent.id),
                )
                cdp = page.context.new_cdp_session(page)
                page.evaluate(
                    """id => {
                    const ws = terminals.get(id).ws, send = ws.send.bind(ws);
                    window.wheelFrames = []; window.touchEvents = [];
                    const element = terminals.get(id).screen;
                    for (const type of ['touchstart','touchmove','touchcancel','touchend','pointercancel']) element.addEventListener(type, event => window.touchEvents.push(type), {capture: true});
                    ws.send = raw => { const value = JSON.parse(raw); if (value.type === 'input') window.wheelFrames.push(value.data); send(raw); };
                }""",
                    str(agent.id),
                )

                def swipe(start, end):
                    box = page.locator(".xterm-screen").bounding_box()
                    x, y1, y2 = box["x"] + 60, box["y"] + start, box["y"] + end
                    cdp.send(
                        "Input.dispatchTouchEvent",
                        {"type": "touchStart", "touchPoints": [{"x": x, "y": y1}]},
                    )
                    for step in range(1, 9):
                        cdp.send(
                            "Input.dispatchTouchEvent",
                            {
                                "type": "touchMove",
                                "touchPoints": [{"x": x, "y": y1 + (y2 - y1) * step / 8}],
                            },
                        )
                        page.wait_for_timeout(20)
                    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

                def mode():
                    value = processes.run(
                        "display-message", "-p", "-t", target, "#{pane_in_mode}|#{scroll_position}"
                    ).stdout.strip()
                    active, _, position = value.partition("|")
                    return active == "1", int(position or "0")

                assert mode()[0] is False
                swipe(100, 310)  # Finger downward -> WheelUp -> actual tmux scrollback.
                for _ in range(100):
                    if mode()[0] and mode()[1] > 0:
                        break
                    time.sleep(0.02)
                diagnostic = processes.run(
                    "display-message",
                    "-p",
                    "-t",
                    target,
                    "#{pane_mode}|#{scroll_position}|#{copy_cursor_y}|#{copy_cursor_line}|#{history_size}",
                ).stdout.strip()
                visible = page.evaluate(
                    "id => Array.from({length: 3}, (_, y) => terminals.get(id).term.buffer.active.getLine(y)?.translateToString())",
                    str(agent.id),
                )
                assert mode()[0] and mode()[1] > 0, (
                    mode(),
                    diagnostic,
                    visible,
                    page.evaluate("window.wheelFrames"),
                    page.evaluate(
                        "id => terminals.get(id).term.modes.mouseTrackingMode", str(agent.id)
                    ),
                    page.evaluate("window.touchEvents"),
                )
                assert (
                    page.evaluate(
                        "window.wheelFrames.filter(value => value.startsWith(String.fromCharCode(27) + '[<64;')).length"
                    )
                    >= 2
                )
                first = mode()[1]
                swipe(100, 310)
                assert mode()[1] > first
                assert processes.pid(agent) == pid and processes.alive(agent)
                assert not received.exists()
                assert not any("terminal-history" in url for url in requests)
                assert not page.locator(".terminal-history").count()
                for _ in range(5):
                    swipe(390, 60)
                    if not mode()[0]:
                        break
                assert mode()[0] is False, mode()  # Default copy-mode -e returns to live at bottom.
                box = page.locator(".xterm-screen").bounding_box()
                page.touchscreen.tap(box["x"] + 30, box["y"] + 30)
                page.wait_for_function(
                    "document.activeElement.classList.contains('xterm-helper-textarea')"
                )
                page.evaluate(
                    "id => terminals.get(id).term.input('MOBILE-LIVE\\r', true)", str(agent.id)
                )
                for _ in range(100):
                    if received.exists():
                        break
                    time.sleep(0.02)
                assert received.read_text() == "MOBILE-LIVE\n"
                page.evaluate(
                    "id => { window.previousTouchController = terminals.get(id).touchScroll; }",
                    str(agent.id),
                )
                page.get_by_role("button", name="Close terminal tab").click()
                assert page.evaluate("window.previousTouchController.disposed")
                assert processes.alive(agent)
                assert not errors
        finally:
            browser.close()
            processes.run("kill-server", check=False)
