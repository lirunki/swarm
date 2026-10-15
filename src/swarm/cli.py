"""User CLI and the tiny reporting tool called by the Swarm skill."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

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


def main(argv: list[str] | None = None) -> None:
    root = parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = root.parse_args(argv)
    if args.command is None:
        args = root.parse_args([*argv, "serve"])
    store = Store(args.home)
    try:
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
