import base64

import pytest
from test_federation_integration import running_server

from swarm.files import FileRequest
from swarm.models import Agent
from swarm.products import ProductRequest
from swarm.server import create_app

playwright = pytest.importorskip("playwright.sync_api", reason="optional browser checks")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1kAAAAASUVORK5CYII="
)


@pytest.mark.parametrize("width", [390, 1280])
def test_files_browser_card_terminal_preview_and_reference_removal(tmp_path, width):
    folder = tmp_path / "work"
    folder.mkdir()
    image = folder / "chart.png"
    image.write_bytes(PNG)
    pdf = folder / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture report")
    html = folder / "active.html"
    html.write_text("<script>window.unsafe=true</script>")
    app = create_app(tmp_path / "home", recover=False)
    ctl = app.state.controller
    agent = ctl.store.create(
        Agent(
            name="Results producer with a long name",
            goal="Show results",
            folder=str(folder),
            auto_resume=False,
        )
    )
    app.state.products.publish(
        str(agent.id),
        ProductRequest(
            url="https://example.com/featured", label="A featured product with a long name"
        ),
    )
    for path, label in [
        (image, "<script>chart label</script>"),
        (pdf, "PDF report"),
        (html, "Download HTML"),
    ]:
        ctl.files.add(agent.id, FileRequest(path=str(path), label=label))
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
                errors, requests = [], []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("request", lambda request: requests.append(request.url))
                page.goto(f"http://127.0.0.1:{port}")
                card = page.locator(f'[data-agent-id="{agent.id}"]')
                files = card.locator(".card-files")
                playwright.expect(files).to_have_text("Files 3")
                rect = card.bounding_box()
                for control in [files, card.locator(".product-link")]:
                    box = control.bounding_box()
                    assert (
                        box["x"] >= rect["x"]
                        and box["x"] + box["width"] <= rect["x"] + rect["width"]
                    )
                    assert box["y"] + box["height"] <= rect["y"] + rect["height"]
                assert not any("/content" in request for request in requests)
                files.click()
                dialog = page.locator("#files-dialog")
                playwright.expect(dialog).to_be_visible()
                playwright.expect(page.locator(".agent-file-row")).to_have_count(3)
                assert "<script>chart label</script>" in dialog.inner_text()
                assert not dialog.locator("script").count()
                assert str(image) not in dialog.inner_text()
                assert not any(
                    "/content" in request for request in requests
                )  # No eager bytes/thumbnails.
                with page.expect_popup() as opened:
                    page.locator(".file-open").first.click()
                preview = opened.value
                preview.wait_for_load_state()
                preview.wait_for_function("document.querySelector('img')?.naturalWidth === 1")
                assert preview.evaluate("window.opener === null")
                preview.close()
                with page.expect_download() as download:
                    page.locator(".file-download").nth(2).click()
                result = download.value
                assert result.suggested_filename == "active.html"
                downloaded = tmp_path / "downloaded.html"
                result.save_as(downloaded)
                assert downloaded.read_bytes() == html.read_bytes()
                assert not page.evaluate("window.unsafe === true")
                image.unlink()
                page.locator("#refresh-agent-files").click()
                playwright.expect(page.locator(".file-unavailable")).to_contain_text(
                    "moved or deleted"
                )
                page.once("dialog", lambda event: event.accept())
                page.locator(".agent-file-row").nth(2).get_by_role(
                    "button", name="Remove reference"
                ).click()
                playwright.expect(page.locator(".agent-file-row")).to_have_count(2)
                assert html.exists()  # UI removal never deletes the original.
                dialog.locator("[data-close]").first.click()
                playwright.expect(files).to_have_text("Files 2")
                card.locator(".card-terminal").click()
                page.locator(".terminal-files").click()
                playwright.expect(dialog).to_be_visible()
                playwright.expect(page.locator(".agent-file-row")).to_have_count(2)
                assert page.evaluate("document.body.scrollWidth <= innerWidth")
                assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_browser_upload_download_and_explicit_insert(tmp_path, width):
    import json
    from pathlib import Path

    from test_file_uploads import FakeProcesses

    app = create_app(tmp_path / "upload-home", recover=False)
    app.state.controller.processes = FakeProcesses()
    app.state.uploads.temp_root = tmp_path / "server-tmp"
    agent = app.state.controller.store.create(
        Agent(
            name="Upload reader",
            goal="Read later",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            status="idle",
        )
    )
    original = b"Uploaded from browser\x00\xff\r\n" * 40000
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
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.add_init_script("""window.WebSocket = class {
                    static OPEN = 1;
                    constructor() { this.readyState = 1; setTimeout(() => this.onopen?.(), 0); }
                    close() { this.readyState = 3; } send() {}
                };""")
                page.goto(f"http://127.0.0.1:{port}")
                card = page.locator(f'[data-agent-id="{agent.id}"]')
                card.locator(".card-terminal").click()
                page.locator(".terminal-files").click()
                dialog = page.locator("#files-dialog")
                with page.expect_file_chooser() as selected:
                    page.locator("#upload-agent-file").click()
                selected.value.set_files(
                    {
                        "name": "client notes.bin",
                        "mimeType": "application/octet-stream",
                        "buffer": original,
                    }
                )
                playwright.expect(page.locator(".agent-file-row")).to_have_count(1, timeout=30000)
                playwright.expect(
                    page.get_by_role("button", name="Insert", exact=True)
                ).to_be_enabled()
                assert not app.state.controller.processes.pastes
                dialog.screenshot(path=str(tmp_path / f"files-upload-insert-{width}.png"))
                with page.expect_download() as downloading:
                    page.locator(".file-download").click()
                copied = tmp_path / "download-copy.bin"
                downloading.value.save_as(copied)
                assert copied.read_bytes() == original
                page.get_by_role("button", name="Insert", exact=True).click()
                playwright.expect(dialog).to_be_hidden()
                assert len(app.state.controller.processes.pastes) == 1
                id, pasted = app.state.controller.processes.pastes[0]
                assert id == str(agent.id) and "\n" not in pasted and "\r" not in pasted
                server_path = Path(json.loads(pasted))
                assert server_path.is_relative_to(app.state.uploads.temp_root)
                assert server_path.read_bytes() == original
                assert app.state.controller.processes.draft == "Please read " + pasted
                page.locator(".terminal-files").click()
                page.once("dialog", lambda event: event.accept())
                page.get_by_role("button", name="Remove upload", exact=True).click()
                playwright.expect(page.locator(".agent-file-row")).to_have_count(0)
                assert not server_path.exists() and copied.read_bytes() == original
                assert page.evaluate("document.body.scrollWidth <= innerWidth")
                assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_files_button_colors_follow_live_library_count(tmp_path, width):
    image = tmp_path / "result.png"
    image.write_bytes(PNG)
    app = create_app(tmp_path / "colors-home", recover=False)
    ctl = app.state.controller
    agent = ctl.store.create(
        Agent(
            name="File color check", goal="Share results", folder=str(tmp_path), auto_resume=False
        )
    )
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
                errors, requests = [], []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("request", lambda request: requests.append(request.url))
                page.goto(f"http://127.0.0.1:{port}")
                card = page.locator(f'[data-agent-id="{agent.id}"]')
                playwright.expect(card.locator(".card-files")).to_have_count(0)
                card.locator(".card-terminal").click()
                terminal_files = page.locator(".terminal-files")
                playwright.expect(terminal_files).to_have_css("color", "rgb(232, 235, 241)")

                # The existing state channel updates an already-open terminal.
                ctl.files.add(agent.id, FileRequest(path=str(image), label="Result"))
                playwright.expect(terminal_files).to_have_css("color", "rgb(243, 201, 107)")
                playwright.expect(terminal_files).to_have_attribute("title", "1 registered file")
                assert not any(f"/api/agents/{agent.id}/files" in request for request in requests)
                page.locator("#dashboard-tab").click()
                files = card.locator(".card-files")
                playwright.expect(files).to_have_text("Files 1")
                playwright.expect(files).to_have_css("color", "rgb(243, 201, 107)")
                card.locator(".card-toggle").click()
                playwright.expect(files).to_have_css("color", "rgb(243, 201, 107)")

                files.click()
                page.once("dialog", lambda event: event.accept())
                page.get_by_role("button", name="Remove reference", exact=True).click()
                playwright.expect(page.locator(".agent-file-row")).to_have_count(0)
                playwright.expect(terminal_files).to_have_css("color", "rgb(232, 235, 241)")
                playwright.expect(card.locator(".card-files")).to_have_count(0)
                assert image.exists()
                assert not any("/content" in request for request in requests)
                assert not errors
        finally:
            browser.close()
