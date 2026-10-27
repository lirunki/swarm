"""Keep the canonical agent notification contract explicit and consistently ordered."""

from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parents[1] / "src/swarm/skills/swarm.md"


def section(text, heading):
    return text.split("## " + heading + "\n", 1)[1].split("\n## ", 1)[0]


def test_notification_contract_is_mandatory_and_visible_before_companion_workflows():
    text = SKILL.read_text()
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert "MUST notify the user" in metadata["description"]
    assert text.index("## Mandatory user notifications") < text.index("## Companion workflows")
    contract = section(text, "Mandatory user notifications")
    assert "**User action is required:**" in contract
    assert "**The user's requested work is complete:**" in contract
    assert "not optional suggestions" in contract
    for action in (
        "login",
        "credential",
        "permission",
        "manual command",
        "restart/reload",
        "review",
    ):
        assert action in contract
    for substitute in (
        "chat message",
        "ask-tool dialog",
        "OS desktop notification",
        "agent-to-agent message",
    ):
        assert substitute in contract
    assert "**does not replace a Swarm inbox notification**" in contract


def test_human_action_order_is_report_notify_then_question_not_report_question_notify():
    text = SKILL.read_text()
    flow = section(text, "Before requesting any user action")
    assert (
        flow.index("1. First report `waiting`")
        < flow.index("2. After that report succeeds")
        < flow.index("3. Only after sending the notification")
    )
    assert "**you MUST post one Swarm notification**" in flow
    assert "every required user action, not just" in flow
    assert "before a blocking tool suspends you or you end the turn" in flow
    example = flow.split("```bash", 1)[1].split("```", 1)[0]
    assert (
        example.index("report --status waiting")
        < example.index("-m swarm notify")
        < example.index("# Ask the user now")
    )


def test_completion_notification_is_required_before_final_response_without_progress_spam():
    text = SKILL.read_text()
    flow = section(text, "Required notifications for user action and completed requests")
    assert "**you MUST make the durable `done` report first" in flow
    assert "Swarm notification before your final response**" in flow
    assert "A final response alone is not sufficient" in flow
    assert "Never notify for `running`, `idle`, or `dormant`" in flow
    assert "Internal subtasks or the end of a turn are not completed user requests" in flow
    assert "never include secret values" in text.lower()
    example = flow.split("```bash", 1)[1].split("```", 1)[0]
    assert example.index("report --status done") < example.index("-m swarm notify")
