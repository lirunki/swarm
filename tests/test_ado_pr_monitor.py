from __future__ import annotations

import json
from pathlib import Path

import swarm.ado_pr_monitor as ado
from swarm.pr_monitor import EXIT_CI_AND_FEEDBACK, Config, assess


def config(tmp_path: Path) -> Config:
    return Config(
        pr=(
            "https://msazure.visualstudio.com/One/_git/"
            "AAD-FirstPartyApps/pullrequest/17640047"
        ),
        repo=None,
        timeout=60,
        interval=5,
        settle=0,
        ci_output=tmp_path / "ci.md",
        feedback_output=tmp_path / "feedback.md",
        gh="az",
    )


def test_parse_one_url() -> None:
    target, number = ado._parse_ado_target(
        "https://msazure.visualstudio.com/One/_git/AAD-FirstPartyApps/pullrequest/17640047",
        None,
        "az",
    )

    assert json.loads(target) == [
        "https://msazure.visualstudio.com",
        "One",
        "AAD-FirstPartyApps",
    ]
    assert number == 17640047


def test_ado_snapshot_maps_policies_votes_and_threads(tmp_path, monkeypatch) -> None:
    metadata = {
        "status": "active",
        "isDraft": False,
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "repository": {"id": "repo-id", "name": "AAD-FirstPartyApps"},
        "reviewers": [
            {
                "displayName": "Reviewer",
                "uniqueName": "reviewer@example.com",
                "vote": -10,
            }
        ],
    }
    policies = [
        {
            "status": "rejected",
            "configuration": {
                "isBlocking": True,
                "type": {"displayName": "Build"},
                "settings": {"displayName": "Unit tests"},
            },
            "context": {"buildId": 123},
        },
        {
            "status": "approved",
            "configuration": {
                "isBlocking": True,
                "type": {"displayName": "Status"},
                "settings": {"statusName": "Ownership"},
            },
        },
        {
            "status": "rejected",
            "configuration": {
                "isBlocking": True,
                "type": {"displayName": "Comment requirements"},
                "settings": {},
            },
        },
    ]
    responses = iter([metadata, policies])
    monkeypatch.setattr(ado, "_run_az_json", lambda _command: next(responses))
    monkeypatch.setattr(
        ado,
        "_fetch_threads",
        lambda *_args: [
            {
                "status": "active",
                "comments": [
                    {
                        "commentType": "text",
                        "content": "Please address this.",
                        "author": {"uniqueName": "author@example.com"},
                    }
                ],
                "threadContext": {
                    "filePath": "/src/app.py",
                    "rightFileStart": {"line": 9},
                },
            },
            {
                "status": None,
                "comments": [{"commentType": "system", "content": "joined"}],
            },
        ],
    )
    cfg = config(tmp_path)
    target, number = ado._parse_ado_target(cfg.pr, None, "az")

    current = ado._fetch_ado_snapshot(cfg, target, number)
    result = assess(current)

    assert result.exit_code == EXIT_CI_AND_FEEDBACK
    assert [item["bucket"] for item in current.checks] == ["fail", "pass"]
    assert len(result.blocking_feedback) == 3
    bodies = [
        item["comments"]["nodes"][0]["body"]
        for item in current.threads
    ]
    assert any("voted -10" in body for body in bodies)
    assert any("Comment requirements" in body for body in bodies)
    assert any("Please address" in body for body in bodies)
