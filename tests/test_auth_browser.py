import json

import pytest
from test_auth import PASSWORD, password_file
from test_federation_integration import running_server

from swarm.files import FileRequest
from swarm.models import Agent
from swarm.server import create_app

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [390, 1280])
def test_browser_requires_password_again_on_refresh_and_downloads_with_memory_auth(tmp_path, width):
    app = create_app(tmp_path / "home", recover=False, password_file=password_file(tmp_path))
    agent = app.state.controller.store.create(
        Agent(name="Private agent", goal="Private goal", folder=str(tmp_path), auto_resume=False)
    )
    source = tmp_path / "private report.txt"
    source.write_bytes(b"private report only after login")
    app.state.controller.files.add(agent.id, FileRequest(path=str(source)))
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
                context = browser.new_context(viewport={"width": width, "height": 1000})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                playwright.expect(page.locator("#auth-dialog")).to_be_visible()
                assert page.locator(".agent-card").count() == 0
                assert page.request.get(f"http://127.0.0.1:{port}/api/state").status == 401
                field = page.locator("#auth-dialog input[type=password]")
                field.fill("wrong password")
                page.locator("#auth-dialog button[type=submit]").click()
                playwright.expect(page.locator("#auth-error")).to_contain_text("Incorrect password")
                assert field.input_value() == ""
                field.fill(PASSWORD)
                page.locator("#auth-dialog button[type=submit]").click()
                playwright.expect(page.locator(".agent-card")).to_have_count(1)
                assert not page.locator("#auth-dialog").is_visible()
                before = page.evaluate("Object.fromEntries(Object.entries(localStorage))")
                assert PASSWORD not in json.dumps(before)
                assert not context.cookies()
                page.locator(".card-files").click()
                with page.expect_download() as pending:
                    page.locator(".file-download").click()
                target = tmp_path / "downloaded.txt"
                pending.value.save_as(target)
                assert target.read_bytes() == source.read_bytes()
                page.reload()
                playwright.expect(page.locator("#auth-dialog")).to_be_visible()
                assert page.locator(".agent-card").count() == 0
                assert page.locator("#auth-dialog input[type=password]").input_value() == ""
                field.fill(PASSWORD)
                page.locator("#auth-dialog button[type=submit]").click()
                playwright.expect(page.locator(".agent-card")).to_have_count(1)
                assert page.evaluate("document.body.scrollWidth <= innerWidth")
                assert not errors
                context.close()
        finally:
            browser.close()
