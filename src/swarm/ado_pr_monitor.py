"""Azure DevOps variant of the blocking pull-request monitor."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

from .pr_monitor import Config, Snapshot, monitor

_COMPLETED_POLICY = {"approved", "notapplicable"}
_FAILED_POLICY = {"rejected", "broken"}
_CI_POLICY_TYPES = {"build", "status"}


def _run_az_json(command: list[str]) -> object:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out: {' '.join(command[:3])}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown Azure CLI error").strip()
        raise RuntimeError(f"Azure CLI command failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Azure CLI returned invalid JSON") from exc


def _parse_ado_target(pr: str, repo: str | None, _az: str) -> tuple[str, int]:
    parsed = urlparse(pr)
    match = re.fullmatch(r"/([^/]+)/_git/([^/]+)/pullrequest/(\d+)/?", parsed.path, re.I)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not match:
        raise ValueError(
            "Azure DevOps PR must be a full URL like "
            "https://ORG.visualstudio.com/PROJECT/_git/REPO/pullrequest/123"
        )
    project, repository, number = unquote(match.group(1)), unquote(match.group(2)), int(match.group(3))
    if repo and repo.lower() != repository.lower():
        raise ValueError("--repo does not match the repository in the PR URL")
    organization = f"{parsed.scheme}://{parsed.netloc}"
    return json.dumps([organization, project, repository], separators=(",", ":")), number


def _policy_name(policy: dict) -> tuple[str, str, bool]:
    configuration = policy.get("configuration") or {}
    policy_type = str((configuration.get("type") or {}).get("displayName") or "Policy")
    settings = configuration.get("settings") or {}
    context = policy.get("context") or {}
    name = (
        settings.get("displayName")
        or settings.get("statusName")
        or context.get("buildDefinitionName")
        or policy_type
    )
    return policy_type, str(name), bool(configuration.get("isBlocking"))


def _policy_check(policy: dict, organization: str, project: str) -> dict:
    policy_type, name, _blocking = _policy_name(policy)
    status = str(policy.get("status") or "queued").lower()
    if status in _FAILED_POLICY:
        bucket = "fail"
    elif status in _COMPLETED_POLICY:
        bucket = "pass"
    else:
        bucket = "pending"
    build_id = (policy.get("context") or {}).get("buildId")
    link = (
        f"{organization}/{project}/_build/results?buildId={build_id}"
        if build_id
        else (policy.get("configuration") or {}).get("url")
    )
    return {
        "name": name,
        "state": status.upper(),
        "bucket": bucket,
        "link": link,
        "workflow": policy_type,
    }


def _feedback_item(summary: str, url: str, *, author: str = "Azure DevOps") -> dict:
    return {
        "isResolved": False,
        "isOutdated": False,
        "path": None,
        "line": None,
        "comments": {"nodes": [{"author": {"login": author}, "body": summary, "url": url}]},
    }


def _fetch_threads(
    executable: str,
    organization: str,
    project: str,
    repository_id: str,
    number: int,
) -> list[dict]:
    threads: list[dict] = []
    continuation: str | None = None
    while True:
        command = [
            executable,
            "devops",
            "invoke",
            "--organization",
            organization,
            "--area",
            "git",
            "--resource",
            "pullRequestThreads",
            "--route-parameters",
            f"project={project}",
            f"repositoryId={repository_id}",
            f"pullRequestId={number}",
            "--api-version",
            "7.1",
            "--output",
            "json",
        ]
        if continuation:
            command.extend(["--query-parameters", f"continuationToken={continuation}"])
        response = _run_az_json(command)
        if not isinstance(response, dict) or not isinstance(response.get("value"), list):
            raise RuntimeError("Azure DevOps returned an unexpected review-thread response")
        threads.extend(item for item in response["value"] if isinstance(item, dict))
        continuation = response.get("continuation_token")
        if not continuation:
            return threads


def _fetch_ado_snapshot(config: Config, target: str, number: int) -> Snapshot:
    organization, project, repository_name = json.loads(target)
    metadata = _run_az_json(
        [
            config.gh,
            "repos",
            "pr",
            "show",
            "--id",
            str(number),
            "--organization",
            organization,
            "--output",
            "json",
        ]
    )
    policies = _run_az_json(
        [
            config.gh,
            "repos",
            "pr",
            "policy",
            "list",
            "--id",
            str(number),
            "--organization",
            organization,
            "--output",
            "json",
        ]
    )
    if not isinstance(metadata, dict) or not isinstance(policies, list):
        raise RuntimeError("Azure DevOps returned an unexpected pull-request response")
    repository = metadata.get("repository") or {}
    repository_id = str(repository.get("id") or repository_name)
    raw_threads = _fetch_threads(
        config.gh, organization, project, repository_id, number
    )

    checks: list[dict] = []
    feedback: list[dict] = []
    url = config.pr
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        policy_type, name, blocking = _policy_name(policy)
        status = str(policy.get("status") or "queued").lower()
        if policy_type.lower() in _CI_POLICY_TYPES:
            checks.append(_policy_check(policy, organization, project))
        elif blocking and status in _FAILED_POLICY:
            feedback.append(
                _feedback_item(f"Blocking policy '{name}' is {status}.", url)
            )

    for reviewer in metadata.get("reviewers") or []:
        if isinstance(reviewer, dict) and int(reviewer.get("vote") or 0) < 0:
            name = reviewer.get("displayName") or reviewer.get("uniqueName") or "reviewer"
            feedback.append(
                _feedback_item(
                    f"Reviewer {name} voted {reviewer.get('vote')}; changes are requested.",
                    url,
                    author=str(reviewer.get("uniqueName") or name),
                )
            )

    for thread in raw_threads:
        if thread.get("isDeleted") or str(thread.get("status") or "").lower() not in {
            "active",
            "pending",
        }:
            continue
        comments = [
            comment
            for comment in (thread.get("comments") or [])
            if isinstance(comment, dict)
            and not comment.get("isDeleted")
            and str(comment.get("commentType") or "text").lower() == "text"
            and str(comment.get("content") or "").strip()
        ]
        if not comments:
            continue
        latest = comments[-1]
        context = thread.get("threadContext") or {}
        feedback.append(
            {
                "isResolved": False,
                "isOutdated": False,
                "path": context.get("filePath"),
                "line": ((context.get("rightFileStart") or {}).get("line")),
                "comments": {
                    "nodes": [
                        {
                            "author": {
                                "login": str(
                                    (latest.get("author") or {}).get("uniqueName")
                                    or (latest.get("author") or {}).get("displayName")
                                    or "reviewer"
                                )
                            },
                            "body": latest.get("content"),
                            "url": url,
                        }
                    ]
                },
            }
        )

    status = str(metadata.get("status") or "unknown").lower()
    state = "MERGED" if status == "completed" else ("CLOSED" if status == "abandoned" else "OPEN")
    source = metadata.get("lastMergeSourceCommit") or {}
    return Snapshot(
        repo=f"{project}/{repository_name}",
        number=number,
        url=url,
        state=state,
        merged=state == "MERGED",
        draft=bool(metadata.get("isDraft")),
        review_decision="",
        head_oid=str(source.get("commitId") or ""),
        checks=tuple(checks),
        threads=tuple(feedback),
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="swarm-pr-monitor-one",
        description="Block on an Azure DevOps One PR until it needs attention or settles clean.",
    )
    command.add_argument("pr", help="full Azure DevOps pull-request URL")
    command.add_argument("--repo", help="optional repository-name cross-check")
    command.add_argument("--timeout", type=float, default=3600)
    command.add_argument("--interval", type=float, default=30)
    command.add_argument("--settle", type=float, default=30)
    command.add_argument("--ci-output", type=Path, default=Path("pr-ci.md"))
    command.add_argument("--feedback-output", type=Path, default=Path("pr-feedback.md"))
    command.add_argument("--az", default="az", help="Azure CLI executable (az)")
    command.add_argument("--max-query-errors", type=int, default=3)
    return command


def main(argv: list[str] | None = None) -> None:
    command = parser()
    args = command.parse_args(argv)
    if args.timeout <= 0 or args.interval <= 0 or args.settle < 0:
        command.error("--timeout/--interval must be positive and --settle must be nonnegative")
    if args.max_query_errors <= 0:
        command.error("--max-query-errors must be positive")
    config = Config(
        pr=args.pr,
        repo=args.repo,
        timeout=args.timeout,
        interval=args.interval,
        settle=args.settle,
        ci_output=args.ci_output,
        feedback_output=args.feedback_output,
        gh=args.az,
        max_query_errors=args.max_query_errors,
    )
    try:
        raise SystemExit(
            monitor(config, fetch=_fetch_ado_snapshot, parse_target=_parse_ado_target)
        )
    except (OSError, ValueError) as exc:
        command.exit(2, f"swarm-pr-monitor-one: {exc}\n")


if __name__ == "__main__":
    main()
