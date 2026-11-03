"""Touch copy precedes the product while compact controls keep native selection."""

import json

import pytest
from test_dashboard_browser import fixture_state, no_auth_mock, static_url  # noqa: F401, F811

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [320, 390, 768])
def test_copy_precedes_live_product_and_is_accessible_when_icon_only(static_url, width):  # noqa: F811
    state = fixture_state()
    agent = state["agents"][0]
    agent["product"] = {
        "kind": "file",
        "id": "report",
        "label": "Product report",
        "filename": "report.txt",
    }
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
            context = browser.new_context(
                viewport={"width": width, "height": 1000}, has_touch=True, is_mobile=True
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""window.sentFrames=[];window.WebSocket=class {
              static OPEN=1;
              constructor(url){this.readyState=1;setTimeout(()=>{this.onopen?.();
                if(url.includes('/terminal/'))this.onmessage?.({data:new TextEncoder().encode('Copy fixture text\\r\\n').buffer});},0);}
              send(data){window.sentFrames.push(JSON.parse(data));}close(){this.readyState=3;}
            };""")

            def respond(route):
                value = (
                    {"enabled": False, "nodes": []}
                    if "/api/federation/nodes" in route.request.url
                    else state
                )
                route.fulfill(content_type="application/json", body=json.dumps(value))

            page.route("**/api/**", no_auth_mock(respond))
            page.goto(static_url)
            page.locator('[data-agent-id="a0"] .card-terminal').click()
            copy = page.get_by_role("button", name="Copy text", exact=True)
            playwright.expect(copy).to_be_visible()
            product = page.locator(".terminal-product")
            playwright.expect(product).to_have_attribute("download", "report.txt")

            def check_position():
                copy_box, product_box = copy.bounding_box(), product.bounding_box()
                assert copy_box["x"] + copy_box["width"] <= product_box["x"]
                assert 0 < product_box["x"] - copy_box["x"] - copy_box["width"] <= 8
                assert abs(copy_box["y"] - product_box["y"]) < 1
                assert page.evaluate("document.body.scrollWidth <= innerWidth")

            check_position()
            assert copy.evaluate(
                'node => node.nextElementSibling.classList.contains("terminal-product-slot")'
            )
            style = copy.evaluate(
                'node => ({font:getComputedStyle(node).fontSize,icon:getComputedStyle(node,"::before").content,width:node.getBoundingClientRect().width})'
            )
            if width <= 650:
                assert style["font"] == "0px" and "⧉" in style["icon"] and style["width"] == 24
            else:
                assert style["font"] != "0px"
                playwright.expect(copy).to_have_text("Copy text")
            copy.click()
            done = page.get_by_role("button", name="Done copying", exact=True)
            playwright.expect(done).to_have_attribute("aria-pressed", "true")
            area = page.locator(".terminal-copy-text")
            playwright.expect(area).to_be_visible()
            if width <= 650:
                assert "✓" in done.evaluate('node => getComputedStyle(node,"::before").content')
            done.click()
            playwright.expect(area).to_be_hidden()
            playwright.expect(copy).to_have_attribute("aria-pressed", "false")
            # Product replacement/removal must not move copy to the far toolbar edge.
            agent["product"] = {
                "kind": "link",
                "label": "Preview",
                "url": "https://example.invalid/preview",
            }
            page.evaluate("value => applyState(value)", state)
            playwright.expect(product).to_have_attribute("data-kind", "link")
            check_position()
            del agent["product"]
            page.evaluate("value => applyState(value)", state)
            playwright.expect(page.locator(".terminal-product-slot")).to_be_hidden()
            playwright.expect(copy).to_be_visible()
            copy.click()
            page.keyboard.press("Escape")
            playwright.expect(copy).to_be_focused()
            playwright.expect(area).to_be_hidden()
            assert not page.evaluate('sentFrames.some(frame => frame.type==="input")')
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()
