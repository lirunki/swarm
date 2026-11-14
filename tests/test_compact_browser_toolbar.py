import pytest
from test_browser_registry_ui_browser import (  # noqa: F401
    RegistryAPI,
    chromium_browser,
    open_browser,
    open_page,
)
from test_dashboard_browser import static_url as _dashboard_static_url  # noqa: F401


@pytest.mark.parametrize("width", [320, 390, 768, 1280])
def test_browser_chrome_is_two_rows_at_most_and_options_keep_controls_accessible(
    chromium_browser,  # noqa: F811
    _dashboard_static_url,  # noqa: F811
    width,
):
    class ToolbarAPI(RegistryAPI):
        def route(self, route):
            if route.request.url.split("?")[0].endswith("/resize"):
                route.fulfill(json={"resized": True})
                return
            super().route(route)

    api = ToolbarAPI()
    context, page, errors = open_page(chromium_browser, _dashboard_static_url, api, width=width)
    try:
        open_browser(page)
        toolbar = page.locator(".browser-toolbar")
        assert toolbar.bounding_box()["height"] <= 69
        assert toolbar.evaluate("node => node.scrollWidth <= node.clientWidth")
        playwright = pytest.importorskip("playwright.sync_api")
        options = page.locator("#browser-options")
        playwright.expect(page.locator("#browser-interval")).to_be_hidden()
        playwright.expect(page.locator("#browser-fit")).to_be_hidden()
        options.locator("summary").click()
        playwright.expect(page.locator("#browser-interval")).to_be_visible()
        box = page.locator(".browser-options-panel").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        page.locator("#browser-interval").fill("5")
        page.locator("#browser-interval").dispatch_event("change")
        options.locator("summary").focus()
        page.keyboard.press("Escape")
        playwright.expect(options.locator("summary")).to_be_focused()
        playwright.expect(page.locator("#browser-interval")).to_be_hidden()
        page.locator("#browser-connect").click()
        playwright.expect(page.locator("#browser-screen")).to_be_visible()
        assert toolbar.bounding_box()["height"] <= 69
        assert toolbar.evaluate("node => node.scrollWidth <= node.clientWidth")
        options.locator("summary").click()
        page.locator("#browser-pause").click()
        playwright.expect(page.locator("#browser-pause")).to_have_text("Resume refresh")
        page.locator(".browser-viewport").click(position={"x": 2, "y": 2})
        assert not options.evaluate("node=>node.open")
        assert not errors
    finally:
        context.close()
