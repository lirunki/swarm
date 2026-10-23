from __future__ import annotations

import json
from pathlib import Path

from swarm.pr_monitor import (
    EXIT_BLOCKING_FEEDBACK,
    EXIT_CI_AND_FEEDBACK,
    EXIT_CLEAN,
    EXIT_TIMEOUT,
    Assessment,
    Config,
    Snapshot,
    _normalize_checks,
    _parse_target,
    assess,
    monitor,
    render_ci,
    render_feedback,
)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def config(tmp_path: Path, **overrides) -> Config:
    values = {
        "pr": "42",
        "repo": "owner/repo",
        "timeout": 20.0,
        "interval": 5.0,
        "settle": 5.0,
        "ci_output": tmp_path / "ci.md",
        "feedback_output": tmp_path / "feedback.md",
    }
    values.update(overrides)
    return Config(**values)


def snapshot(*, checks: tuple[dict, ...], threads: tuple[dict, ...] = (), **overrides) -> Snapshot:
    values = {
        "repo": "owner/repo",
        "number": 42,
        "url": "https://github.example/owner/repo/pull/42",
        "state": "OPEN",
        "merged": False,
        "draft": False,
        "review_decision": "APPROVED",
        "head_oid": "abc123",
        "checks": checks,
        "threads": threads,
    }
    values.update(overrides)
    return Snapshot(**values)


def check(bucket: str, name: str = "test") -> dict:
    return {
        "bucket": bucket,
        "name": name,
        "state": bucket.upper(),
        "workflow": "CI",
        "link": f"https://ci.example/{name}",
    }


def test_enterprise_url_preserves_host_for_gh_and_graphql() -> None:
    assert _parse_target(
        "https://microsoft.ghe.com/bic/lobster/pull/42", None, "gh"
    ) == ("microsoft.ghe.com/bic/lobster", 42)


def test_status_rollup_normalizes_checks_without_newer_gh_pr_checks_json() -> None:
    checks = _normalize_checks(
        [
            {
                "__typename": "CheckRun",
                "name": "unit",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://ci/unit",
                "workflowName": "CI",
            },
            {
                "__typename": "CheckRun",
                "name": "build",
                "status": "IN_PROGRESS",
                "conclusion": "",
            },
            {
                "__typename": "StatusContext",
                "context": "policy",
                "state": "SUCCESS",
                "targetUrl": "https://ci/policy",
            },
        ]
    )

    assert [item["bucket"] for item in checks] == ["fail", "pending", "pass"]
    assert checks[0]["workflow"] == "CI"


def test_assessment_finds_ci_and_unresolved_review_feedback() -> None:
    current = snapshot(
        checks=(check("fail"),),
        review_decision="CHANGES_REQUESTED",
        threads=(
            {
                "isResolved": False,
                "isOutdated": True,
                "path": "src/app.py",
                "line": 12,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "reviewer"},
                            "body": "Please fix this.",
                            "url": "https://github.example/thread",
                        }
                    ]
                },
            },
            {"isResolved": True, "comments": {"nodes": []}},
        ),
    )

    result = assess(current)

    assert result.exit_code == EXIT_CI_AND_FEEDBACK
    assert len(result.ci_failures) == 1
    assert len(result.blocking_feedback) == 2
    assert "CI failures" in render_ci(current, result)
    feedback = render_feedback(current, result)
    assert "CHANGES_REQUESTED" in feedback
    assert "Please fix this" in feedback
    assert "outdated diff" in feedback


def test_monitor_blocks_until_green_is_stable_and_writes_both_files(tmp_path, capsys) -> None:
    cfg = config(tmp_path)
    clock = Clock()
    calls = 0

    def fetch(_config: Config, _repo: str, _number: int) -> Snapshot:
        nonlocal calls
        calls += 1
        return snapshot(checks=(check("pass"),))

    code = monitor(cfg, fetch=fetch, monotonic=clock.monotonic, sleep=clock.sleep)

    assert code == EXIT_CLEAN
    assert calls == 2
    assert "All green" in cfg.ci_output.read_text()
    assert "Nothing blocking" in cfg.feedback_output.read_text()
    summary = json.loads(capsys.readouterr().out)
    assert summary["reason"] == "all-green"
    assert summary["exit_code"] == EXIT_CLEAN


def test_monitor_returns_immediately_for_blocking_feedback(tmp_path) -> None:
    cfg = config(tmp_path)
    current = snapshot(
        checks=(check("pending"),),
        review_decision="CHANGES_REQUESTED",
    )

    code = monitor(cfg, fetch=lambda *_: current)

    assert code == EXIT_BLOCKING_FEEDBACK
    assert "CI pending" in cfg.ci_output.read_text()
    assert "CHANGES_REQUESTED" in cfg.feedback_output.read_text()


def test_monitor_timeout_reports_last_pending_snapshot(tmp_path, capsys) -> None:
    cfg = config(tmp_path, timeout=5.0, interval=5.0)
    clock = Clock()
    current = snapshot(checks=(check("pending", "slow-test"),))

    code = monitor(
        cfg,
        fetch=lambda *_: current,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert code == EXIT_TIMEOUT
    assert "Timed out with CI pending" in cfg.ci_output.read_text()
    assert "slow-test" in cfg.ci_output.read_text()
    assert "Nothing blocking" in cfg.feedback_output.read_text()
    assert json.loads(capsys.readouterr().out)["reason"] == "timeout"


def test_closed_unmerged_pr_is_blocking_feedback() -> None:
    current = snapshot(checks=(check("pass"),), state="CLOSED", review_decision="")

    result: Assessment = assess(current)

    assert result.exit_code == EXIT_BLOCKING_FEEDBACK
    assert "closed without merging" in render_feedback(current, result)
