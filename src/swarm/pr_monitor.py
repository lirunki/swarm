"""Blocking GitHub pull-request monitor for LLM-driven workflows.

The monitor deliberately does no repair work. It waits until CI fails, blocking
review feedback appears, CI settles green, the PR merges, or a caller deadline
expires. It always leaves separate, atomic CI and feedback reports for the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

EXIT_CLEAN = 0
EXIT_CI_FAILURE = 10
EXIT_BLOCKING_FEEDBACK = 11
EXIT_CI_AND_FEEDBACK = 12
EXIT_TIMEOUT = 20
EXIT_QUERY_ERROR = 21

_FAILURE_BUCKETS = {"fail", "cancel"}
_PENDING_BUCKETS = {"pending"}

_THREAD_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(last: 1) {
            nodes {
              author { login }
              body
              url
              createdAt
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


@dataclass(frozen=True)
class Config:
    pr: str
    repo: str | None
    timeout: float
    interval: float
    settle: float
    ci_output: Path
    feedback_output: Path
    gh: str = "gh"
    max_query_errors: int = 3


@dataclass(frozen=True)
class Snapshot:
    repo: str
    number: int
    url: str
    state: str
    merged: bool
    draft: bool
    review_decision: str
    head_oid: str
    checks: tuple[dict, ...]
    threads: tuple[dict, ...]


@dataclass(frozen=True)
class Assessment:
    ci_failures: tuple[dict, ...]
    ci_pending: tuple[dict, ...]
    ci_passing: tuple[dict, ...]
    blocking_feedback: tuple[dict, ...]
    all_green: bool

    @property
    def exit_code(self) -> int | None:
        if self.ci_failures and self.blocking_feedback:
            return EXIT_CI_AND_FEEDBACK
        if self.ci_failures:
            return EXIT_CI_FAILURE
        if self.blocking_feedback:
            return EXIT_BLOCKING_FEEDBACK
        return None


def _run_json(command: list[str], *, allowed_codes: frozenset[int] = frozenset({0})) -> object:
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
    if result.returncode not in allowed_codes:
        detail = (result.stderr or result.stdout or "unknown gh error").strip()
        raise RuntimeError(f"gh command failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh returned invalid JSON") from exc


def _parse_target(pr: str, repo: str | None, gh: str) -> tuple[str, int]:
    if re.fullmatch(r"[1-9][0-9]*", pr):
        number = int(pr)
        if not repo:
            data = _run_json([gh, "repo", "view", "--json", "nameWithOwner"])
            if not isinstance(data, dict) or not data.get("nameWithOwner"):
                raise RuntimeError("could not resolve the current GitHub repository")
            repo = str(data["nameWithOwner"])
    else:
        parsed = urlparse(pr)
        match = re.fullmatch(r"/([^/]+)/([^/]+)/pull/(\d+)/?", parsed.path)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not match:
            raise ValueError("PR must be a positive number or a GitHub pull-request URL")
        short_repo = f"{match.group(1)}/{match.group(2)}"
        hosted_repo = f"{parsed.netloc}/{short_repo}"
        if repo and repo.lower() not in {short_repo.lower(), hosted_repo.lower()}:
            raise ValueError("--repo does not match the repository in the PR URL")
        # Preserve the host so `gh` and GraphQL use the right GitHub Enterprise instance.
        repo = hosted_repo
        number = int(match.group(3))
    if not re.fullmatch(r"[^/\s]+/[^/\s]+(?:/[^/\s]+)?", repo or ""):
        raise ValueError("--repo must use OWNER/REPO or HOST/OWNER/REPO syntax")
    return str(repo), number


def _normalize_checks(raw: object) -> tuple[dict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RuntimeError("gh returned an unexpected statusCheckRollup response")
    checks: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("__typename")
        if kind == "StatusContext" or "context" in item:
            state = str(item.get("state") or "").upper()
            bucket = "pending" if state == "PENDING" else ("fail" if state in {"ERROR", "FAILURE"} else "pass")
            checks.append(
                {
                    "name": item.get("context") or "status",
                    "state": state or "UNKNOWN",
                    "bucket": bucket,
                    "link": item.get("targetUrl"),
                    "workflow": "",
                }
            )
            continue
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or "").upper()
        if status != "COMPLETED":
            bucket = "pending"
            state = status or "PENDING"
        elif conclusion in {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
            bucket, state = "fail", conclusion
        elif conclusion in {"CANCELLED", "STALE"}:
            bucket, state = "cancel", conclusion
        else:
            bucket, state = "pass", conclusion or "SUCCESS"
        checks.append(
            {
                "name": item.get("name") or "check",
                "state": state,
                "bucket": bucket,
                "link": item.get("detailsUrl"),
                "workflow": item.get("workflowName") or "",
            }
        )
    return tuple(checks)


def _fetch_snapshot(config: Config, repo: str, number: int) -> Snapshot:
    metadata = _run_json(
        [
            config.gh,
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,url,state,isDraft,reviewDecision,headRefOid,statusCheckRollup",
        ]
    )
    parts = repo.split("/")
    host, owner, name = (parts[0], parts[1], parts[2]) if len(parts) == 3 else (None, parts[0], parts[1])
    graphql_command = [config.gh, "api"]
    if host:
        graphql_command.extend(["--hostname", host])
    graphql_command.extend(
        [
            "graphql",
            "-f",
            f"query={_THREAD_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
    )
    if not isinstance(metadata, dict):
        raise RuntimeError("gh returned an unexpected PR response")
    checks = _normalize_checks(metadata.get("statusCheckRollup"))
    # Paginate explicitly instead of relying on newer `gh api --slurp`; the
    # enterprise workstation currently ships a gh build without that flag.
    threads: list[dict] = []
    cursor: str | None = None
    while True:
        command = list(graphql_command)
        if cursor:
            command.extend(["-F", f"endCursor={cursor}"])
        page = _run_json(command)
        try:
            connection = page["data"]["repository"]["pullRequest"]["reviewThreads"]
            threads.extend(node for node in connection["nodes"] if isinstance(node, dict))
            page_info = connection["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("gh review-thread response is missing expected fields") from exc
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise RuntimeError("gh review-thread pagination returned no end cursor")
    state = str(metadata.get("state") or "UNKNOWN").upper()
    return Snapshot(
        repo=repo,
        number=number,
        url=str(metadata.get("url") or ""),
        state=state,
        merged=state == "MERGED",
        draft=bool(metadata.get("isDraft")),
        review_decision=str(metadata.get("reviewDecision") or "").upper(),
        head_oid=str(metadata.get("headRefOid") or ""),
        checks=checks,
        threads=tuple(threads),
    )


def assess(snapshot: Snapshot) -> Assessment:
    failures, pending, passing = [], [], []
    for check in snapshot.checks:
        bucket = str(check.get("bucket") or "").lower()
        if bucket in _FAILURE_BUCKETS:
            failures.append(check)
        elif bucket in _PENDING_BUCKETS or not bucket:
            pending.append(check)
        else:
            passing.append(check)

    feedback: list[dict] = []
    if snapshot.review_decision == "CHANGES_REQUESTED":
        feedback.append(
            {
                "kind": "review-decision",
                "summary": "The pull request review decision is CHANGES_REQUESTED.",
                "url": snapshot.url,
            }
        )
    for thread in snapshot.threads:
        if thread.get("isResolved"):
            continue
        comments = thread.get("comments") or {}
        nodes = comments.get("nodes") if isinstance(comments, dict) else []
        latest = nodes[-1] if isinstance(nodes, list) and nodes else {}
        feedback.append(
            {
                "kind": "unresolved-thread",
                "path": thread.get("path"),
                "line": thread.get("line"),
                "outdated": bool(thread.get("isOutdated")),
                "author": ((latest.get("author") or {}).get("login") if isinstance(latest, dict) else None),
                "body": (latest.get("body") if isinstance(latest, dict) else None),
                "url": (latest.get("url") if isinstance(latest, dict) else snapshot.url),
            }
        )
    if snapshot.state == "CLOSED":
        feedback.append(
            {
                "kind": "closed-unmerged",
                "summary": "The pull request was closed without merging.",
                "url": snapshot.url,
            }
        )
    return Assessment(
        ci_failures=tuple(failures),
        ci_pending=tuple(pending),
        ci_passing=tuple(passing),
        blocking_feedback=tuple(feedback),
        all_green=bool(snapshot.checks) and not failures and not pending,
    )


def _atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _check_line(check: dict) -> str:
    name = check.get("name") or "unnamed check"
    workflow = check.get("workflow") or ""
    state = check.get("state") or check.get("bucket") or "unknown"
    label = f"{workflow} / {name}" if workflow else str(name)
    url = check.get("link") or ""
    return f"- **{label}** — `{state}`" + (f" — {url}" if url else "")


def render_ci(snapshot: Snapshot, result: Assessment, *, timed_out: bool = False) -> str:
    lines = ["# Pull request CI", "", f"PR: {snapshot.url or f'{snapshot.repo}#{snapshot.number}'}", ""]
    if result.ci_failures:
        lines += ["## CI failures", "", *(_check_line(c) for c in result.ci_failures)]
    elif result.all_green:
        lines += ["## All green", "", f"All {len(snapshot.checks)} observed checks completed without failure."]
    elif not snapshot.checks:
        lines += ["## CI not registered", "", "No checks were visible before the monitor finished."]
    else:
        heading = "Timed out with CI pending" if timed_out else "CI pending"
        lines += [f"## {heading}", "", *(_check_line(c) for c in result.ci_pending)]
    lines += ["", f"Head: `{snapshot.head_oid or 'unknown'}`", ""]
    return "\n".join(lines)


def render_feedback(snapshot: Snapshot, result: Assessment, *, timed_out: bool = False) -> str:
    lines = ["# Pull request blocking feedback", "", f"PR: {snapshot.url or f'{snapshot.repo}#{snapshot.number}'}", ""]
    if not result.blocking_feedback:
        lines += ["## Nothing blocking", "", "No changes-requested decision or unresolved review thread was found."]
        if timed_out:
            lines += ["", "The monitor timed out while waiting for CI; feedback was clear at the final poll."]
    else:
        lines += ["## Blocking feedback", ""]
        for item in result.blocking_feedback:
            kind = item.get("kind")
            if kind == "unresolved-thread":
                location = item.get("path") or "general review"
                if item.get("line"):
                    location += f":{item['line']}"
                author = item.get("author") or "unknown reviewer"
                body = re.sub(r"\s+", " ", str(item.get("body") or "")).strip()
                suffix = " (outdated diff)" if item.get("outdated") else ""
                lines.append(f"- **Unresolved thread at {location}{suffix}** — @{author}")
                if body:
                    lines.append(f"  - {body}")
                if item.get("url"):
                    lines.append(f"  - {item['url']}")
            else:
                lines.append(f"- **{item.get('summary', kind)}**")
                if item.get("url"):
                    lines.append(f"  - {item['url']}")
    lines.append("")
    return "\n".join(lines)


def _write_reports(config: Config, snapshot: Snapshot, result: Assessment, *, timed_out: bool = False) -> None:
    _atomic_write(config.ci_output, render_ci(snapshot, result, timed_out=timed_out))
    _atomic_write(
        config.feedback_output,
        render_feedback(snapshot, result, timed_out=timed_out),
    )


def _summary(config: Config, snapshot: Snapshot, code: int, reason: str) -> None:
    print(
        json.dumps(
            {
                "exit_code": code,
                "reason": reason,
                "pr": snapshot.url or f"{snapshot.repo}#{snapshot.number}",
                "ci_output": str(config.ci_output.expanduser().resolve()),
                "feedback_output": str(config.feedback_output.expanduser().resolve()),
            }
        ),
        flush=True,
    )


def monitor(
    config: Config,
    *,
    fetch: Callable[[Config, str, int], Snapshot] = _fetch_snapshot,
    parse_target: Callable[[str, str | None, str], tuple[str, int]] = _parse_target,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    repo, number = parse_target(config.pr, config.repo, config.gh)
    started = monotonic()
    green_since: float | None = None
    last_snapshot: Snapshot | None = None
    last_assessment: Assessment | None = None
    query_errors = 0

    while True:
        now = monotonic()
        if now - started >= config.timeout and last_snapshot and last_assessment:
            _write_reports(config, last_snapshot, last_assessment, timed_out=True)
            _summary(config, last_snapshot, EXIT_TIMEOUT, "timeout")
            return EXIT_TIMEOUT
        try:
            snapshot = fetch(config, repo, number)
            result = assess(snapshot)
            query_errors = 0
            last_snapshot, last_assessment = snapshot, result
            _write_reports(config, snapshot, result)
        except RuntimeError as exc:
            query_errors += 1
            if query_errors >= config.max_query_errors:
                print(f"swarm-pr-monitor: {exc}", file=sys.stderr)
                return EXIT_QUERY_ERROR
            remaining = config.timeout - (monotonic() - started)
            if remaining <= 0:
                print(f"swarm-pr-monitor: timed out after query errors: {exc}", file=sys.stderr)
                return EXIT_TIMEOUT
            sleep(min(config.interval, remaining))
            continue

        code = result.exit_code
        if code is not None:
            reason = {
                EXIT_CI_FAILURE: "ci-failure",
                EXIT_BLOCKING_FEEDBACK: "blocking-feedback",
                EXIT_CI_AND_FEEDBACK: "ci-and-feedback",
            }[code]
            _summary(config, snapshot, code, reason)
            return code
        if snapshot.merged:
            _summary(config, snapshot, EXIT_CLEAN, "merged-clean")
            return EXIT_CLEAN
        if result.all_green and not snapshot.draft:
            green_since = green_since if green_since is not None else monotonic()
            if monotonic() - green_since >= config.settle:
                _summary(config, snapshot, EXIT_CLEAN, "all-green")
                return EXIT_CLEAN
        else:
            green_since = None

        remaining = config.timeout - (monotonic() - started)
        if remaining <= 0:
            _write_reports(config, snapshot, result, timed_out=True)
            _summary(config, snapshot, EXIT_TIMEOUT, "timeout")
            return EXIT_TIMEOUT
        sleep(min(config.interval, remaining))


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="swarm-pr-monitor",
        description="Block until a PR needs attention, settles green, merges, or times out.",
    )
    command.add_argument("pr", help="pull-request number or full GitHub PR URL")
    command.add_argument(
        "--repo", help="OWNER/REPO or HOST/OWNER/REPO (required for a number outside a checkout)"
    )
    command.add_argument("--timeout", type=float, default=3600, help="deadline in seconds (3600)")
    command.add_argument("--interval", type=float, default=30, help="poll interval in seconds (30)")
    command.add_argument(
        "--settle",
        type=float,
        default=30,
        help="seconds CI must remain green before success (30)",
    )
    command.add_argument("--ci-output", type=Path, default=Path("pr-ci.md"))
    command.add_argument("--feedback-output", type=Path, default=Path("pr-feedback.md"))
    command.add_argument("--gh", default="gh", help="GitHub CLI executable (gh)")
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
        gh=args.gh,
        max_query_errors=args.max_query_errors,
    )
    try:
        raise SystemExit(monitor(config))
    except (OSError, ValueError) as exc:
        command.exit(2, f"swarm-pr-monitor: {exc}\n")


if __name__ == "__main__":
    main()
