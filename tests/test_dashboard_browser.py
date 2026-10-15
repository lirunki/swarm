"""Optional real layout checks: pip install playwright; playwright install chromium --only-shell."""

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
STATIC = Path(__file__).resolve().parents[1] / "src" / "swarm" / "static"


class StaticHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/static/"):
            self.path = self.path[len("/static") :]
        super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture
def static_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticHandler, directory=str(STATIC)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fixture_state():
    agents = []
    for i in range(18):
        agents.append(
            {
                "id": f"a{i}",
                "name": f"Agent {i}",
                "goal": "Hidden goal details",
                "status": "running" if i == 2 else "idle",
                "alive": i in (0, 2),
                "parent_id": "a0" if 1 <= i <= 4 else "a2" if i in (5, 6) else None,
                "backend": "pi",
                "managed": True,
                "auto_resume": False,
                "activity": f"Current task {i}",
                "folder": "/workspace",
                "notes": "Checkpoint",
                "session_file": "/session.jsonl",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
    agents[0]["product"] = {
        "id": "product-0",
        "kind": "link",
        "label": "PR #42",
        "url": "https://example.com/pr/42",
    }
    agents[7]["product"] = {
        "id": "product-7",
        "kind": "file",
        "label": "A downloadable report with a long label",
        "filename": "report.zip",
    }
    agents[5]["name"] = "Needle child"
    agents[7]["name"] = "Long unbroken name " + "X" * 95
    agents[8]["name"] = '<img src=x onerror="window.unsafe=true">'
    return {
        "agents": agents,
        "home": "/fixture",
        "health": {"tmux": True, "pi": True},
        "errors": {},
    }


@pytest.mark.parametrize("width", [1280, 390])
def test_dense_org_chart_layout_and_interactions(static_url, tmp_path, width):
    state = fixture_state()
    with playwright.sync_playwright() as runner:
        executable = Path(runner.chromium.executable_path)
        # --only-shell installs a separate executable; launch decides the exact location.
        try:
            browser = runner.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(
                exc
            ) or "error while loading shared libraries" in str(exc):
                pytest.skip(f"Optional Chromium runtime unavailable: {executable}")
            raise
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script("""window.WebSocket = class {
                static OPEN = 1;
                constructor() { this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
                close() { this.readyState = 3; }
                send() {}
            };""")
            page.route(
                "**/api/**",
                lambda route: route.fulfill(
                    content_type="application/json", body=json.dumps(state)
                ),
            )
            page.goto(static_url)
            playwright.expect(page.locator(".agent-card")).to_have_count(18)
            playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(18)
            assert not page.locator(".goal:visible").count()
            assert not page.locator(".agent-card img").count()
            playwright.expect(page.locator(".product-link:visible")).to_have_count(2)
            assert (
                page.locator('[data-agent-id="a0"] .product-link').get_attribute("href")
                == "https://example.com/pr/42"
            )
            assert (
                page.locator('[data-agent-id="a7"] .product-link').get_attribute("download")
                == "report.zip"
            )
            for id in ("a0", "a7"):
                tile = page.locator(f'[data-agent-id="{id}"]').bounding_box()
                link = page.locator(f'[data-agent-id="{id}"] .product-link').bounding_box()
                assert link["y"] + link["height"] <= tile["y"] + tile["height"]
                assert link["x"] + link["width"] <= tile["x"] + tile["width"]
            assert (
                page.locator('[data-agent-id="a0"] h3').evaluate("e => getComputedStyle(e).color")
                == "rgb(255, 123, 123)"
            )
            assert (
                page.locator('[data-agent-id="a1"] h3').evaluate("e => getComputedStyle(e).color")
                != "rgb(255, 123, 123)"
            )
            boxes = page.locator(".agent-card").evaluate_all(
                "els => els.map(e => { const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; })"
            )
            for box in boxes:
                assert box["width"] == 128 and box["height"] == 128
            for i, a in enumerate(boxes):
                for b in boxes[i + 1 :]:
                    assert (
                        a["x"] + a["width"] <= b["x"]
                        or b["x"] + b["width"] <= a["x"]
                        or a["y"] + a["height"] <= b["y"]
                        or b["y"] + b["height"] <= a["y"]
                    )
            assert page.evaluate("document.body.scrollWidth <= window.innerWidth")
            assert (
                page.locator('[data-agent-id="a5"]').evaluate(
                    "e => e.parentElement.parentElement.parentElement.querySelector(':scope > .agent-card').dataset.agentId"
                )
                == "a2"
            )
            page.locator('[data-agent-id="a0"] .card-toggle').click()
            playwright.expect(page.locator('[data-agent-id="a0"] .goal')).to_be_visible()
            assert page.locator('[data-agent-id="a0"]').bounding_box()["width"] > 128
            page.locator("#collapse-all").click()
            playwright.expect(page.locator(".agent-card.is-compact")).to_have_count(18)
            page.locator("#search").fill("Needle")
            playwright.expect(page.locator(".agent-card")).to_have_count(3)
            playwright.expect(page.locator(".org-node.is-context")).to_have_count(2)
            playwright.expect(page.locator("#hierarchy-summary")).to_contain_text("1 match")
            toggle = page.locator('[data-agent-id="a5"] .card-toggle')
            toggle.focus()
            state["agents"][5]["activity"] = "Updated hover task"
            page.evaluate("data => applyState(data)", state)
            playwright.expect(page.locator('[data-agent-id="a5"] .card-toggle')).to_be_focused()
            assert "Updated hover task" in page.locator('[data-agent-id="a5"]').get_attribute(
                "title"
            )
            page.locator("#search").fill("")
            page.locator("#agents").screenshot(path=str(tmp_path / f"org-chart-{width}.png"))
            page.locator("#search").fill("No such agent")
            playwright.expect(page.locator("#agents")).to_be_hidden()
            playwright.expect(page.locator("#empty")).to_be_visible()
            assert not errors
        finally:
            browser.close()
