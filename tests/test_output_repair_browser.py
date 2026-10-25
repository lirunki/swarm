from pathlib import Path

import pytest
from test_federation_integration import running_server
from test_messaging import FakeProcesses
from test_output_repair_integration import add_source

from swarm.server import create_app
from swarm.tool_outputs import PREVIEW_BYTES

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [390, 1280])
def test_edit_output_limit_fix_requires_stop_archives_and_does_not_wake(
    tmp_path, monkeypatch, width
):
    app = create_app(tmp_path / "home", recover=False)
    ctl = app.state.controller
    ctl.processes = FakeProcesses()
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    target, path, original = add_source(app)
    ctl.processes.running.add(str(target.id))
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
                page = browser.new_page(viewport={"width": width, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                card = page.locator(f'[data-agent-id="{target.id}"]')
                card.locator(".card-toggle").click()
                assert not card.get_by_role("button", name="Fix output limit…", exact=True).count()
                card.get_by_role("button", name="Edit", exact=True).click()
                fix = page.locator("#repair-tool-outputs")
                playwright.expect(fix).to_be_disabled()
                ctl.processes.running.clear()
                playwright.expect(fix).to_be_enabled(timeout=10000)
                page.once("dialog", lambda dialog: dialog.dismiss())
                fix.click()
                assert path.read_bytes() == original
                assert not getattr(ctl.store.get(target.id), "tool_output_repair", None)
                confirmations = []

                def accept(dialog):
                    confirmations.append(dialog.message)
                    dialog.accept()

                page.once("dialog", accept)
                fix.click()
                playwright.expect(page.locator(".toast").last).to_contain_text("Repaired")
                report = ctl.store.get(target.id).tool_output_repair
                assert report["status"] == "completed" and report["outputs_repaired"] == 1
                assert report["preview_bytes"] <= PREVIEW_BYTES
                assert "10,485,760" in confirmations[0] and "No model calls" in confirmations[0]
                assert path.read_bytes().startswith(original)
                assert not ctl.processes.launches and not ctl.processes.alive(target)
                section = page.locator(".tool-output-repair-section")
                playwright.expect(page.locator("#tool-output-repair-state")).to_have_text(
                    "completed"
                )
                playwright.expect(page.locator("#tool-output-repair-count")).to_have_text("1")
                assert page.locator("#tool-output-repair-archive").inner_text() == report["archive"]
                assert Path(report["archive"]).read_bytes() == original
                section.screenshot(path=str(tmp_path / f"output-repair-{width}.png"))
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator("#repair-tool-outputs").click()
                playwright.expect(page.locator("#tool-output-repair-state")).to_have_text(
                    "unchanged"
                )
                assert not ctl.processes.launches and not ctl.store.get(target.id).auto_resume
                assert page.evaluate("document.body.scrollWidth<=innerWidth")
                assert not errors
        finally:
            browser.close()
