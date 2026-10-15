"""User CLI and the tiny reporting tool called by the Swarm skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import webbrowser
from pathlib import Path, PureWindowsPath

from .agent_client import AgentClient
from .importers.grok import GrokImportRequest, GrokSessions
from .models import Status
from .paths import local_path
from .processes import Processes
from .sessions import AdoptRequest, Sessions
from .store import Store
from .tool_inputs import retrieve


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="swarm", description="A local dashboard for many agents")
    root.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("SWARM_HOME", "~/.swarm")),
        help="registry folder (default: SWARM_HOME or ~/.swarm)",
    )
    root.add_argument(
        "--api-url",
        default=os.environ.get("SWARM_API_URL", "http://127.0.0.1:8787"),
        help="loopback controller URL (default: SWARM_API_URL or http://127.0.0.1:8787)",
    )
    root.add_argument(
        "--parent-id",
        default=None,
        help="parent agent ID (operator override; managed agents use SWARM_AGENT_ID)",
    )
    root.add_argument(
        "--pi-sessions-dir",
        action="append",
        type=Path,
        help="custom pi session root for discovery/adoption (repeatable)",
    )
    root.add_argument(
        "--grok-sessions-dir",
        action="append",
        type=Path,
        help="Grok Build session root for import (repeatable; Windows drive paths accepted on WSL)",
    )
    commands = root.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="start the graphical dashboard")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--pi", default=os.environ.get("SWARM_PI", "pi"), help="pi executable path")
    serve.add_argument(
        "--no-recover", action="store_true", help="do not launch saved sessions at startup"
    )
    serve.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    commands.add_parser("list", help="list agent records as JSON")
    commands.add_parser("doctor", help="check runtime prerequisites")
    commands.add_parser("sessions", help="list adoptable pi sessions as JSON")
    tool_input = commands.add_parser(
        "tool-input", help="read archived tool input; never execute it"
    )
    tool_input.add_argument("toolrun_id")
    tool_input.add_argument("--offset", type=int, default=0, help="character offset (default 0)")
    tool_input.add_argument("--limit", type=int, default=8000, help="characters per page (1–64000)")
    adopt = commands.add_parser(
        "adopt", help="register an inactive pi session, without starting it"
    )
    adopt.add_argument("session_file", type=Path)
    adopt.add_argument("--name", default="")
    adopt.add_argument("--goal", default="")
    commands.add_parser("grok-sessions", help="list Grok Build sessions available for import")
    grok_import = commands.add_parser(
        "import-grok", help="convert a Grok Build session to pi and adopt offline"
    )
    grok_import.add_argument(
        "session_file", type=Path, help="Grok session directory or chat_history.jsonl"
    )
    grok_import.add_argument("--name", default="")
    grok_import.add_argument("--goal", default="")
    grok_import.add_argument("--folder", default="", help="local working-folder override")
    report = commands.add_parser("report", help="atomically update this agent's Markdown record")
    report.add_argument("--id", default=os.environ.get("SWARM_AGENT_ID"))
    report.add_argument("--status", choices=[s.value for s in Status])
    for key in (
        "goal",
        "activity",
        "wait_reason",
        "notes",
        "folder",
        "worktree",
        "repository",
        "branch",
    ):
        report.add_argument("--" + key.replace("_", "-"))
    report.add_argument("--wake-at", help="timezone-aware ISO-8601 timestamp, or 'none'")
    report.add_argument(
        "--activities", help='JSON array of {"title": "...", "status": "pending|running|done"}'
    )
    report.add_argument("--pid", type=int)

    product = commands.add_parser(
        "product", help="advertise the latest result on this agent's card"
    )
    product.add_argument(
        "--id",
        default=os.environ.get("SWARM_AGENT_ID"),
        help="agent ID (defaults to SWARM_AGENT_ID)",
    )
    product.add_argument("--label", default="", help="short link label")
    source = product.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="external HTTP(S) product link")
    source.add_argument("--file", help="local result file to snapshot for download")
    source.add_argument("--clear", action="store_true", help="remove the current product")

    spawn = commands.add_parser("spawn", help="create or reuse a direct child agent")
    spawn.add_argument("--request-id", required=True, help="stable idempotency slug (max 80)")
    spawn.add_argument("--name", required=True)
    spawn.add_argument("--goal", required=True)
    spawn.add_argument("--repository", type=Path, required=True)
    spawn.add_argument("--worktree", type=Path, required=True)
    spawn.add_argument("--branch", required=True)
    spawn.add_argument("--base", default="HEAD")
    spawn.add_argument("--context", choices=["fresh", "fork"], default="fresh")
    spawn.add_argument("--model")
    spawn.add_argument("--instructions", default="")

    agent = commands.add_parser("agent", help="manage direct child agents through the API")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("list", help="list direct children")
    status = agent_commands.add_parser("status", help="show one direct child")
    status.add_argument("id")
    message = agent_commands.add_parser("message", help="send a nonempty message")
    message.add_argument("id")
    message.add_argument("text")
    wake = agent_commands.add_parser("wake", help="wake a direct child")
    wake.add_argument("id")
    wake.add_argument("--message")
    stop = agent_commands.add_parser("stop", help="stop a direct child cooperatively")
    stop.add_argument("id")
    stop.add_argument("--force", action="store_true")
    wait = agent_commands.add_parser("wait", help="wait event-first for a direct child")
    wait.add_argument("id")
    return root


def run_report(store: Store, args: argparse.Namespace) -> dict:
    if not args.id:
        raise ValueError("--id or SWARM_AGENT_ID is required")
    fields = {
        key: getattr(args, key)
        for key in (
            "status",
            "goal",
            "activity",
            "wait_reason",
            "notes",
            "folder",
            "worktree",
            "repository",
            "branch",
            "pid",
        )
        if getattr(args, key) is not None
    }
    if args.wake_at is not None:
        fields["wake_at"] = None if args.wake_at.lower() == "none" else args.wake_at
    if args.activities is not None:
        fields["activities"] = json.loads(args.activities)
    session_id, session_file = os.environ.get("PI_SESSION_ID"), os.environ.get("PI_SESSION_FILE")
    agent = store.get(args.id)
    if session_id:
        fields["session_id"] = session_id
    if session_file:
        if (
            agent.managed
            and agent.session_file
            and local_path(agent.session_file) != local_path(session_file)
        ):
            raise ValueError(
                "report came from a different pi session; do not switch managed sessions"
            )
        fields["session_file"] = str(local_path(session_file))
    return store.update(args.id, fields).model_dump(mode="json")


def _parent_id(args: argparse.Namespace) -> str:
    parent_id = args.parent_id or os.environ.get("SWARM_AGENT_ID")
    if not parent_id:
        raise ValueError(
            "this command requires a Swarm-managed context (SWARM_AGENT_ID), or the operator "
            "must pass global --parent-id before the subcommand"
        )
    return parent_id


def run_agent_command(args: argparse.Namespace) -> dict:
    parent_id = _parent_id(args)
    client = AgentClient(args.api_url)
    if args.command == "spawn":
        if len(args.request_id) > 80:
            raise ValueError("--request-id must be at most 80 characters")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,78}[A-Za-z0-9])?", args.request_id):
            raise ValueError(
                "--request-id must be a URL-safe slug using letters, numbers, '.', '_', or '-'"
            )
        if not args.name.strip() or not args.goal.strip():
            raise ValueError("--name and --goal must be nonempty")
        if not args.branch.strip() or not args.base.strip():
            raise ValueError("--branch and --base must be nonempty")
        if not (
            args.repository.is_absolute() or PureWindowsPath(str(args.repository)).is_absolute()
        ):
            raise ValueError("--repository must be an absolute path")
        if not (args.worktree.is_absolute() or PureWindowsPath(str(args.worktree)).is_absolute()):
            raise ValueError("--worktree must be an absolute path")
        payload = {
            "request_id": args.request_id,
            "name": args.name,
            "goal": args.goal,
            "instructions": args.instructions,
            "repository": str(local_path(args.repository)),
            "worktree": str(local_path(args.worktree)),
            "branch": args.branch,
            "base": args.base,
            "context": args.context,
        }
        if args.model is not None:
            payload["model"] = args.model
        return client.spawn(parent_id, payload)
    if args.agent_command == "list":
        return client.list(parent_id)
    if args.agent_command == "status":
        return client.status(parent_id, args.id)
    if args.agent_command == "message":
        if not args.text.strip():
            raise ValueError("message TEXT must be nonempty")
        return client.action(parent_id, args.id, "message", {"message": args.text})
    if args.agent_command == "wake":
        body = {} if args.message is None else {"message": args.message}
        return client.action(parent_id, args.id, "wake", body)
    if args.agent_command == "stop":
        return client.action(parent_id, args.id, "stop", {"force": args.force})
    if args.agent_command == "wait":
        return client.action(parent_id, args.id, "wait", {})
    raise ValueError("unknown agent command")


def main(argv: list[str] | None = None) -> None:
    root = parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = root.parse_args(argv)
    if args.command is None:
        args = root.parse_args([*argv, "serve"])
    try:
        if args.command == "product":
            if not args.id:
                raise ValueError("--id or SWARM_AGENT_ID is required to advertise a product")
            if args.clear and args.label:
                raise ValueError("--label cannot be used with --clear")
            payload = None if args.clear else {"label": args.label}
            if args.url:
                payload["url"] = args.url
            elif args.file:
                payload["path"] = str(local_path(args.file))
            print(json.dumps(AgentClient(args.api_url).product(args.id, payload), indent=2))
            return
        if args.command in {"spawn", "agent"}:
            print(json.dumps(run_agent_command(args), indent=2))
            return
        store = Store(args.home)
        if args.command == "serve":
            import uvicorn

            from .server import create_app

            host = "[::1]" if args.host == "::1" else args.host
            url = f"http://{host}:{args.port}"
            print(
                f"Swarm: {url}\nRegistry: {store.home}\nClosing the dashboard does not stop agents.",
                flush=True,
            )
            if args.open:
                timer = threading.Timer(1.0, lambda: webbrowser.open(url))
                timer.daemon = True
                timer.start()
            uvicorn.run(
                create_app(
                    store.home,
                    pi=args.pi,
                    recover=not args.no_recover,
                    session_roots=args.pi_sessions_dir,
                    grok_session_roots=args.grok_sessions_dir,
                    api_url=url,
                ),
                host=args.host,
                port=args.port,
                workers=1,
                ws_max_size=262144,
            )
        elif args.command == "list":
            print(
                json.dumps(
                    {
                        "agents": [a.model_dump(mode="json") for a in store.scan()],
                        "errors": store.errors,
                    },
                    indent=2,
                )
            )
        elif args.command == "report":
            print(json.dumps(run_report(store, args), indent=2))
        elif args.command == "tool-input":
            print(json.dumps(retrieve(store, args.toolrun_id, args.offset, args.limit), indent=2))
        elif args.command == "sessions":
            print(json.dumps(Sessions(store, args.pi_sessions_dir).list(), indent=2))
        elif args.command == "adopt":
            request = AdoptRequest(
                session_file=str(args.session_file), name=args.name, goal=args.goal
            )
            print(
                json.dumps(
                    Sessions(store, args.pi_sessions_dir).adopt(request).model_dump(mode="json"),
                    indent=2,
                )
            )
        elif args.command == "grok-sessions":
            print(json.dumps(GrokSessions(store, args.grok_sessions_dir).list(), indent=2))
        elif args.command == "import-grok":
            request = GrokImportRequest(
                session_file=str(args.session_file),
                name=args.name,
                goal=args.goal,
                folder=args.folder,
            )
            agent = GrokSessions(store, args.grok_sessions_dir).import_session(request)
            print(json.dumps(agent.model_dump(mode="json"), indent=2))
        elif args.command == "doctor":
            print(
                json.dumps(
                    {
                        "home": str(store.home),
                        "health": Processes(store).health(),
                        "session_roots": [
                            str(p) for p in Sessions(store, args.pi_sessions_dir).roots
                        ],
                        "grok_session_roots": [
                            str(p) for p in GrokSessions(store, args.grok_sessions_dir).roots
                        ],
                    },
                    indent=2,
                )
            )
    except (OSError, ValueError, RuntimeError) as exc:
        root.exit(1, f"swarm: {exc}\n")


if __name__ == "__main__":
    main()
