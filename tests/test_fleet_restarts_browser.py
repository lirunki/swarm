from datetime import timedelta

import pytest
from test_federation_integration import running_server
from test_fleet_restarts import FakeProcesses, agent, native_ready

from swarm.models import now
from swarm.server import create_app

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("native", [False, True])
def test_master_control_preview_confirmation_and_scoped_restart(tmp_path, width, native):
    app = create_app(tmp_path / "registry", recover=False)
    ctl = app.state.controller
    ctl.processes = FakeProcesses(ctl.store)
    target = agent(
        ctl,
        name="<Alpha & Co>",
        native=native,
        status="dormant",
        auto_resume=False,
        wake_at=now() + timedelta(hours=1),
    )
    other = agent(ctl, name="Do not restart", native=False, status="waiting", auto_resume=False)
    stopped = agent(ctl, name="Stopped stays stopped", live=False, auto_resume=False)
    original_other = ctl.processes.identity(other)
    original_send = ctl.processes.send_prompt

    def quit_exits(a, text):
        original_send(a, text)
        if text == "/quit":
            ctl.processes.live.pop(str(a.id), None)

    ctl.processes.send_prompt = quit_exits
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
                playwright.expect(page.locator(".agent-card")).to_have_count(3)
                page.locator("#notifications-button").click()
                page.get_by_role("button", name="Master control", exact=True).click()
                dialog = page.locator("#fleet-restart-dialog")
                playwright.expect(dialog).to_be_visible()
                playwright.expect(page.locator("#fleet-restart-cancel")).to_be_focused()
                playwright.expect(page.locator(".fleet-preview-row")).to_have_count(2)
                assert "<Alpha & Co>" in page.locator("#fleet-preview-targets").inner_text()
                assert not page.locator("#fleet-preview-targets script").count()
                skipped = page.locator("#fleet-skipped-section")
                assert not skipped.evaluate("(node) => node.open")
                playwright.expect(page.locator("#fleet-skipped-count")).to_have_text("1")
                playwright.expect(page.locator("#fleet-preview-skipped")).to_be_hidden()
                skipped.locator("summary").focus()
                skipped.locator("summary").press("Enter")
                assert skipped.evaluate("(node) => node.open")
                playwright.expect(page.locator("#fleet-preview-skipped")).to_be_visible()
                assert (
                    "Stopped stays stopped" in page.locator("#fleet-preview-skipped").inner_text()
                )
                page.locator("#fleet-preview-refresh").click()
                playwright.expect(page.locator("#fleet-preview-refresh")).to_be_enabled()
                playwright.expect(page.locator(".fleet-preview-row")).to_have_count(2)
                assert skipped.evaluate("(node) => node.open"), "refresh must preserve expansion"
                skipped.locator("summary").click()
                playwright.expect(page.locator("#fleet-preview-skipped")).to_be_hidden()
                page.locator(f'input[data-agent-id="{other.id}"]').uncheck()
                page.once("dialog", lambda value: value.dismiss())
                page.locator("#fleet-restart-start").click()
                assert not ctl.restarts.jobs() and not ctl.processes.launches
                page.once("dialog", lambda value: value.accept())
                page.locator("#fleet-restart-start").click()
                playwright.expect(page.locator(".fleet-job-row")).to_have_count(1)
                job_details = page.locator("#fleet-job-section")
                assert not job_details.evaluate("(node) => node.open")
                playwright.expect(page.locator("#fleet-job-status")).to_be_visible()
                playwright.expect(page.locator("#fleet-job-targets")).to_be_hidden()
                dialog.screenshot(
                    path=str(tmp_path / f"fleet-control-compact-{width}-{native}.png")
                )
                assert (
                    not ctl.processes.launches
                    and not ctl.processes.kills
                    and not ctl.processes.prompts
                )
                job = ctl.restarts.jobs()[0]
                job_id = job["id"]
                assert [t["id"] for t in job["targets"]] == [str(target.id)]
                page.locator(
                    "#fleet-restart-cancel"
                ).click()  # Now labelled Close: does not cancel the job.
                playwright.expect(dialog).to_be_hidden()
                assert ctl.store.get(target.id).restart_job_id == job_id
                page.locator("#notifications-button").click()
                page.get_by_role("button", name="Master control", exact=True).click()
                playwright.expect(page.locator(".fleet-job-row")).to_have_count(1)
                assert not job_details.evaluate("(node) => node.open"), (
                    "reopening must default compact"
                )
                job_details.locator("summary").focus()
                job_details.locator("summary").press("Enter")
                assert job_details.evaluate("(node) => node.open")
                playwright.expect(page.locator("#fleet-job-targets")).to_be_visible()
                if native:
                    native_ready(ctl, target, job_id)
                    playwright.expect(page.locator(".fleet-target-state")).to_have_text(
                        "stopping", timeout=15000
                    )
                    assert not ctl.processes.launches
                    ctl.processes.live.pop(str(target.id))
                else:
                    playwright.expect(page.locator(".fleet-target-state")).to_have_text("blocked")
                    page.once("dialog", lambda value: value.accept())
                    page.get_by_role("button", name="Legacy /quit…", exact=True).click()
                playwright.expect(page.locator(".fleet-target-state")).to_have_text(
                    "completed", timeout=15000
                )
                assert ctl.processes.launches == [(target.id, None)] and not ctl.processes.kills
                assert ctl.processes.prompts == ([] if native else [(target.id, "/quit")])
                assert ctl.processes.identity(other) == original_other and not ctl.processes.alive(
                    stopped
                )
                current = ctl.store.get(target.id)
                assert (
                    current.status == "dormant"
                    and current.wake_at == target.wake_at
                    and not current.auto_resume
                )
                assert current.restart_job_id is None
                assert job_details.evaluate("(node) => node.open"), (
                    "job polling must preserve expansion"
                )
                assert page.evaluate("document.body.scrollWidth<=innerWidth")
                assert not errors
                dialog.screenshot(path=str(tmp_path / f"fleet-control-{width}-{native}.png"))
        finally:
            browser.close()
