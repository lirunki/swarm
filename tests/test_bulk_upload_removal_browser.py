"""Bulk removal uses actual upload deletion while preserving ordinary originals."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_federation_integration import running_server

from swarm.files import FileRequest
from swarm.models import Agent
from swarm.server import create_app

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [390, 1280])
def test_select_uploads_by_default_then_remove_only_checked_copies(tmp_path, width):
    app = create_app(tmp_path / "home", recover=False)
    app.state.uploads.temp_root = tmp_path / "uploaded-copies"
    ctl = app.state.controller
    agent = ctl.store.create(
        Agent(name="Upload cleanup", goal="Test", folder=str(tmp_path), auto_resume=False)
    )
    original = tmp_path / "original.txt"
    original.write_text("Keep original")
    ctl.files.add(agent.id, FileRequest(path=str(original), label="Ordinary reference"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for name in ("remove.txt", "keep.txt", "remove-too.txt"):
            response = client.post(
                f"/api/agents/{agent.id}/files/uploads",
                json={"upload_id": str(uuid4()), "filename": name, "size": 0},
            )
            assert response.status_code == 201, response.text
            upload_id = response.json()["upload_id"]
            response = client.post(f"/api/agents/{agent.id}/files/uploads/{upload_id}/finish")
            assert response.status_code == 200, response.text
    uploaded = {ref.filename: ref for ref in ctl.files.read(agent.id).files if ref.uploaded}
    assert len(uploaded) == 3
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
                page = browser.new_page(
                    viewport={"width": width, "height": 1000},
                    has_touch=width == 390,
                    is_mobile=width == 390,
                )
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                page.locator(f'.agent-card[data-agent-id="{agent.id}"] .card-files').click()
                page.get_by_role("button", name="Remove uploads…", exact=True).click()
                checks = page.locator(".upload-remove-checkbox")
                playwright.expect(checks).to_have_count(3)
                assert checks.evaluate_all("nodes=>nodes.every(node=>node.checked)")
                page.get_by_role("checkbox", name="Remove upload keep.txt", exact=True).uncheck()
                playwright.expect(page.locator("#bulk-remove-count")).to_have_text(
                    "2 uploads selected"
                )
                assert (
                    not page.locator(".agent-file-row")
                    .filter(has_text="Ordinary reference")
                    .locator("input")
                    .count()
                )
                page.once("dialog", lambda dialog: dialog.dismiss())
                page.get_by_role("button", name="Remove selected", exact=True).click()
                assert len(ctl.files.read(agent.id).files) == 4
                page.once("dialog", lambda dialog: dialog.accept())
                page.get_by_role("button", name="Remove selected", exact=True).click()
                playwright.expect(page.locator("#bulk-remove-status")).to_have_text(
                    "Removed 2 of 2 selected uploads."
                )
                playwright.expect(page.locator(".agent-file-row")).to_have_count(2)
                assert original.read_text() == "Keep original"
                from swarm.paths import local_path

                assert local_path(uploaded["keep.txt"].path).exists()
                assert not local_path(uploaded["remove.txt"].path).exists()
                assert not local_path(uploaded["remove-too.txt"].path).exists()
                page.get_by_role("button", name="Remove uploads…", exact=True).click()
                playwright.expect(
                    page.get_by_role("checkbox", name="Remove upload keep.txt", exact=True)
                ).to_be_checked()
                page.get_by_role("button", name="Cancel selection", exact=True).click()
                playwright.expect(checks).to_have_count(0)
                assert page.evaluate("document.body.scrollWidth <= innerWidth")
                assert not errors
        finally:
            browser.close()
