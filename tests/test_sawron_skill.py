"""Approval guidance supports two backends without relaxing session/approval guards."""

import json
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parents[1] / "src/swarm/skills/sawron.md"


def test_backend_selection_is_explicit_and_unconfigured_by_default():
    text = SKILL.read_text()
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert "Playwright or EdgeMCP" in metadata["description"]
    state = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    assert state["browserBackend"] == "auto"
    assert state["selectedBackend"] is None
    assert state["playwrightEndpoint"] is None and state["mcpEndpoint"] is None
    assert state["enabled"] is False and state["requestors"] == []
    assert state["autoApprove"] is False and state["requireExplicitConfirmation"] is True
    assert "An explicit backend choice\nmust be honored" in text
    assert "A fallback is allowed only in auto mode" in text
    assert "Perform one polling cycle through one backend only" in text


def test_playwright_attaches_only_to_an_approved_existing_session():
    text = SKILL.read_text()
    playwright = text.split("### Playwright\n", 1)[1].split("### EdgeMCP\n", 1)[0]
    for required in (
        "explicitly approved running browser",
        "never guess one or start a fresh/persistent context",
        "verified context and portal",
        "credentials: 'same-origin'",
        "cache: 'no-store'",
        "Never close the user's",
        "do not assume `browser.close()` is detach-only",
    ):
        assert required in playwright
    assert "Do not send MCP initialization messages to a Playwright/CDP endpoint" in text


def test_backend_switch_does_not_repeat_approvals_or_extend_consent():
    text = SKILL.read_text()
    for required in (
        "Switching never retries an\n   approval",
        "`submitting`/`uncertain` operations",
        "the original monitoring deadline",
        "Require request-specific\n  confirmation",
        "Never self-approve",
        "unknown**, never empty",
        "do not click again",
        "After **three consecutive unsuccessful cycles**",
    ):
        assert required in text
