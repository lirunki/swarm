"""Native Chromium coverage for the synchronous RemoteBrowser adapter.

These tests use only a temporary profile and pages served on loopback.  They
never discover, read, or control a user's normal browser profile.
"""

from __future__ import annotations

import os
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from swarm import edgemcp_launcher
from swarm.browser_settings import BrowserSettings
from swarm.remote_browser import BrowserError, RemoteBrowser

playwright = pytest.importorskip("playwright.sync_api", reason="optional native browser checks")
pytest.importorskip("pychrome", reason="native RemoteBrowser checks require pychrome")
pytest.importorskip("requests", reason="pychrome requires requests")

LIBRARY_PATH_FILE = Path("/tmp/swarm-groups-libpath.txt")


def _page(title: str, body: str = "") -> bytes:
    return f"""<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ margin: 0; min-height: 1800px; font: 16px sans-serif; }}
  #button {{ position: fixed; left: 20px; top: 20px; width: 120px; height: 40px; }}
  #input {{ position: fixed; left: 20px; top: 80px; width: 240px; height: 40px; }}
  #area {{ position: fixed; left: 20px; top: 140px; width: 240px; height: 50px; }}
  #password {{ position: fixed; left: 20px; top: 220px; width: 240px; height: 40px; }}
  #drag {{ position: fixed; left: 20px; top: 300px; width: 120px; height: 50px;
           background: #8cf; user-select: none; }}
  #drag-end {{ position: fixed; left: 220px; top: 300px; width: 120px; height: 50px;
               background: #fc8; }}
  #next {{ position: fixed; left: 20px; top: 20px; width: 140px; height: 40px; }}
</style>
{body}
""".encode()


@pytest.fixture
def local_pages():
    state = {"history_two_requests": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.partition("?")[0]
            if path == "/fixture":
                content = _page(
                    "Remote Fixture",
                    """
<button id="button" onclick="document.title='button-clicked'">Click</button>
<input id="input" aria-label="input"
  onkeydown="if(event.key==='Enter')document.title='input:'+this.value">
<textarea id="area" aria-label="textarea"
  oninput="document.title='textarea:'+this.value"></textarea>
<input id="password" aria-label="password" type="password">
<div id="drag">drag start</div><div id="drag-end">drag end</div>
<script>
  let dragging = false;
  document.querySelector('#drag').addEventListener('mousedown', () => dragging = true);
  document.addEventListener('mouseup', event => {
    if (dragging) document.title = event.target.id === 'drag-end' ? 'drag-released' : 'drag-missed';
    dragging = false;
  });
</script>
""",
                )
            elif path == "/history-one":
                content = _page(
                    "History One",
                    '<a id="next" href="/history-two">History two</a>',
                )
            elif path == "/history-two":
                with lock:
                    state["history_two_requests"] += 1
                    count = state["history_two_requests"]
                content = _page(f"History Two {count}", "<p>second history entry</p>")
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def native_chromium(monkeypatch):
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("Native Chromium is not launched as root without changing its sandbox flags")

    with playwright.sync_playwright() as runner:
        executable = Path(runner.chromium.executable_path).resolve()
    if not executable.is_file():
        pytest.skip(f"Optional Chromium executable is unavailable: {executable}")

    if LIBRARY_PATH_FILE.exists():
        library_path = LIBRARY_PATH_FILE.read_text(encoding="utf-8").strip()
        if library_path:
            existing = os.environ.get("LD_LIBRARY_PATH")
            monkeypatch.setenv(
                "LD_LIBRARY_PATH",
                library_path if not existing else f"{library_path}{os.pathsep}{existing}",
            )

    if (
        os.name != "nt"
        and subprocess.run(
            ["ldd", str(executable)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=os.environ.copy(),
        ).stdout.find("not found")
        >= 0
    ):
        pytest.skip(
            "Optional Chromium shared libraries are unavailable; populate "
            f"{LIBRARY_PATH_FILE} with an extracted user library path"
        )
    return executable


def _wait_for_tab(remote: RemoteBrowser, target_id: str, *, title: str, timeout: float = 5):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = remote.request("tabs")
        tab = next((item for item in latest["tabs"] if item["id"] == target_id), None)
        if tab is not None and tab["title"] == title:
            return latest
        time.sleep(0.05)
    pytest.fail(f"tab {target_id} did not reach title {title!r} within {timeout}s: {latest!r}")


def _png_size(content: bytes) -> tuple[int, int]:
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    assert content[12:16] == b"IHDR"
    width, height = struct.unpack(">II", content[16:24])
    assert width > 0 and height > 0
    return width, height


def _ratio(point: tuple[float, float], size: tuple[int, int]) -> dict[str, float]:
    return {"x_ratio": point[0] / size[0], "y_ratio": point[1] / size[1]}


def _enable(settings: BrowserSettings, executable: Path) -> None:
    settings.apply(
        {
            "revision": 0,
            "request_id": str(uuid4()),
            "enabled": True,
            "headless": True,
            "executable": str(executable),
        }
    )


def test_native_remote_browser_lifecycle_controls_and_navigation(
    tmp_path, monkeypatch, local_pages, native_chromium
):
    home = tmp_path / "temporary-home"
    personal = tmp_path / "personal-profile"
    personal.mkdir()
    marker = personal / "never-read-or-changed.txt"
    marker.write_text("personal data", encoding="utf-8")
    monkeypatch.setenv("EDGE_PROFILE_DIR", str(personal))

    settings = BrowserSettings(home)
    remote = RemoteBrowser(home, settings)
    attempts_before = set(edgemcp_launcher._ATTEMPTS.values())

    assert remote.status() == {
        "enabled": False,
        "running": False,
        "state": "disabled",
        "error": None,
    }
    with pytest.raises(BrowserError, match="disabled"):
        remote.launch(uuid4())
    assert set(edgemcp_launcher._ATTEMPTS.values()) == attempts_before
    assert not remote.root.exists()

    _enable(settings, native_chromium)
    assert remote.status() == {
        "enabled": True,
        "running": False,
        "state": "stopped",
        "error": None,
    }
    assert not remote.root.exists(), "status must not launch Chromium or create browser state"

    owned_process = None
    owned_key = None
    try:
        request_id = uuid4()
        launched = remote.launch(request_id)
        assert launched["running"] is True
        assert launched["started"] is True
        assert launched["browser"]
        assert remote.launch(request_id) == launched

        state = remote._read_state()
        assert state is not None and state["browser_id"]
        port = state["port"]
        owned_key = (port, str(remote.profile.resolve()))
        owned_process = edgemcp_launcher._ATTEMPTS[owned_key]
        assert owned_process not in attempts_before
        assert owned_process.poll() is None
        assert remote.profile == home.resolve() / "remote-browser" / "profile"
        assert personal not in remote.profile.parents
        assert marker.read_text(encoding="utf-8") == "personal data"

        initial = remote.request("tabs")
        assert initial["tabs"]
        assert all(tab["id"] and isinstance(tab["url"], str) for tab in initial["tabs"]), initial

        base_url, page_state = local_pages
        made = remote.request("new", {"url": f"{base_url}/fixture"})
        target = made["id"]
        tabs = _wait_for_tab(remote, target, title="Remote Fixture")
        assert tabs["selected"] == target
        assert any(tab["id"] == target and tab["url"] == made["url"] for tab in tabs["tabs"])

        image = remote.request("screenshot", query={"target_id": target})
        assert isinstance(image, bytes)
        size = _png_size(image)

        remote.request("click", {"target_id": target, **_ratio((80, 40), size)})
        _wait_for_tab(remote, target, title="button-clicked")

        remote.request("click", {"target_id": target, **_ratio((100, 100), size)})
        remote.request("text", {"target_id": target, "text": "native input"})
        remote.request("key", {"target_id": target, "key": "Enter"})
        _wait_for_tab(remote, target, title="input:native input")
        remote.request("key", {"target_id": target, "key": "a", "modifiers": ["ctrl"]})
        assert remote.request("copy", {"target_id": target}) == {"text": "native input"}

        remote.request("click", {"target_id": target, **_ratio((100, 165), size)})
        remote.request("text", {"target_id": target, "text": "native textarea"})
        _wait_for_tab(remote, target, title="textarea:native textarea")
        remote.request("key", {"target_id": target, "key": "a", "modifiers": ["ctrl"]})
        assert remote.request("copy", {"target_id": target}) == {"text": "native textarea"}

        remote.request("click", {"target_id": target, **_ratio((100, 240), size)})
        remote.request("text", {"target_id": target, "text": "secret value"})
        remote.request("key", {"target_id": target, "key": "a", "modifiers": ["ctrl"]})
        assert remote.request("copy", {"target_id": target}) == {"text": ""}

        remote.request(
            "drag",
            {
                "target_id": target,
                "start": _ratio((80, 325), size),
                "end": _ratio((280, 325), size),
            },
        )
        _wait_for_tab(remote, target, title="drag-released")

        history = remote.request("new", {"url": f"{base_url}/history-one"})
        history_target = history["id"]
        tabs = _wait_for_tab(remote, history_target, title="History One")
        assert tabs["selected"] == history_target

        history_image = remote.request("screenshot", {"target_id": history_target})
        history_size = _png_size(history_image)
        remote.request("click", {"target_id": history_target, **_ratio((80, 40), history_size)})
        _wait_for_tab(remote, history_target, title="History Two 1")

        remote.request("back", {"target_id": history_target})
        _wait_for_tab(remote, history_target, title="History One")
        remote.request("forward", {"target_id": history_target})
        _wait_for_tab(remote, history_target, title="History Two 1")
        remote.request("reload", {"target_id": history_target})
        _wait_for_tab(remote, history_target, title="History Two 2")
        assert page_state["history_two_requests"] == 2

        assert remote.request("close", {"target_id": history_target}) == {"ok": True}
        assert all(tab["id"] != history_target for tab in remote.request("tabs")["tabs"])

        remote.close()
        assert owned_process.poll() is None
        edgemcp_launcher._ATTEMPTS.pop(owned_key, None)
        edgemcp_launcher._READY.discard(owned_key)
        restarted = RemoteBrowser(home, settings)
        assert restarted.status()["running"] is True
        assert restarted.launch(uuid4())["started"] is False
        assert restarted.request("tabs")["tabs"]
        restarted.close()
        assert owned_process.poll() is None
        assert restarted.status()["running"] is True
        assert marker.read_text(encoding="utf-8") == "personal data"
    finally:
        remote.close()
        if owned_process is not None:
            owned_process.terminate()
            try:
                owned_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                owned_process.kill()
                owned_process.wait(timeout=10)
        if owned_key is not None and edgemcp_launcher._ATTEMPTS.get(owned_key) is owned_process:
            edgemcp_launcher._ATTEMPTS.pop(owned_key, None)
            edgemcp_launcher._READY.discard(owned_key)

    assert marker.read_text(encoding="utf-8") == "personal data"
