
import pytest
from test_federation_integration import running_server
from test_messaging import FakeProcesses, ack, connect_bridge

from swarm.models import Agent
from swarm.server import create_app

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [390, 1280])
def test_messages_card_send_pending_delete_and_live_ack(tmp_path, width):
    app = create_app(tmp_path / "home", recover=False)
    ctl = app.state.controller
    ctl.processes = FakeProcesses()
    agent = ctl.store.create(
        Agent(
            name="Mailbox recipient",
            goal="Wait for instructions",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            status="idle",
            session_id="11111111-1111-4111-8111-111111111111",
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
                page = browser.new_page(viewport={"width": width, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                card = page.locator(f'[data-agent-id="{agent.id}"]')
                playwright.expect(card.locator(".card-messages")).to_contain_text("0")
                card.locator(".card-messages").click()
                body = (
                    "<script>window.mailboxUnsafe=true</script>\nPlease review the changed parser."
                )
                page.locator("#agent-message-form textarea").fill(body)
                page.locator("#send-agent-message").click()
                playwright.expect(
                    page.locator("#agent-message-list .agent-message-row")
                ).to_have_count(1)
                playwright.expect(
                    page.locator("#agent-message-list .agent-message-status")
                ).to_have_text("pending")
                assert page.locator("#agent-message-list script").count() == 0
                assert page.locator("#agent-message-list .agent-message-text").inner_text() == body
                assert not ctl.processes.launches and ctl.store.get(agent.id).status == "idle"
                page.get_by_role("button", name="Close messages", exact=True).click()
                playwright.expect(card.locator(".card-messages")).to_contain_text("1")
                bounds = card.bounding_box()
                badge = card.locator(".card-messages").bounding_box()
                assert (
                    bounds["height"] == 128
                    and badge["y"] + badge["height"] <= bounds["y"] + bounds["height"]
                )
                card.locator(".card-toggle").click()
                playwright.expect(
                    card.locator(".expanded-messages .agent-message-row")
                ).to_have_count(1)
                page.once("dialog", lambda dialog: dialog.accept())
                card.locator(".expanded-messages .agent-message-delete").click()
                playwright.expect(
                    card.locator(".expanded-messages .agent-message-row")
                ).to_have_count(0)
                assert ctl.messages.list(str(agent.id))["messages"] == []
                current, directory, epoch = connect_bridge(ctl, ctl.store.get(agent.id))
                card.locator(".expanded-messages").get_by_role(
                    "button", name="Open messages", exact=True
                ).click()
                page.locator("#agent-message-form textarea").fill(
                    "Deliver safely without editor changes"
                )
                page.locator("#send-agent-message").click()
                playwright.expect(
                    page.locator("#agent-message-list .agent-message-status")
                ).to_have_text("delivering")
                playwright.expect(
                    page.locator("#agent-message-list .agent-message-delete")
                ).to_be_disabled()
                message = ctl.messages.list(str(agent.id))["messages"][0]
                ack(directory, current, message)
                playwright.expect(
                    page.locator("#agent-message-list .agent-message-row")
                ).to_have_count(0, timeout=15000)
                assert not ctl.processes.launches
                assert page.evaluate("document.body.scrollWidth <= innerWidth")
                page.locator("#messages-dialog").screenshot(
                    path=str(tmp_path / f"messages-{width}.png")
                )
                assert not errors
        finally:
            browser.close()
