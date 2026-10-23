"""User CLI and the tiny reporting tool called by the Swarm skill."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import uuid
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


def _tls_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--certfile", type=Path, help="TLS certificate PEM file")
    command.add_argument("--keyfile", type=Path, help="TLS private key PEM file")
    command.add_argument(
        "--generate-certs-if-absent",
        action="store_true",
        help="generate and use self-signed TLS files when both are absent (requires OpenSSL)",
    )
    command.add_argument(
        "--cert-host",
        action="append",
        default=[],
        metavar="DNS_OR_IP",
        help="extra certificate DNS name or IP (repeatable; includes localhost and machine hostname)",
    )
    command.add_argument(
        "--cert-days", type=int, default=365, help="generated validity in days (365)"
    )


def _tls_options(args: argparse.Namespace, *, relay: bool = False) -> dict:
    if bool(args.certfile) != bool(args.keyfile):
        raise ValueError("--certfile and --keyfile must be supplied together")
    if args.cert_host and not args.generate_certs_if_absent:
        raise ValueError("--cert-host requires --generate-certs-if-absent")
    if not args.certfile and not args.generate_certs_if_absent:
        if relay:
            raise ValueError(
                "relay TLS requires --certfile and --keyfile or --generate-certs-if-absent"
            )
        return {}
    directory = args.home.expanduser().resolve() / "tls" / ("relay" if relay else "dashboard")
    cert = (args.certfile or directory / "cert.pem").expanduser().absolute()
    key = (args.keyfile or directory / "privkey.pem").expanduser().absolute()
    if args.generate_certs_if_absent:
        from .certificates import generate_if_absent

        generated = generate_if_absent(
            cert, key, hosts=[args.host, *args.cert_host], days=args.cert_days
        )
        print(f"TLS certificate {'generated' if generated else 'reused'}: {cert}", flush=True)
        print(
            "Self-signed certificates must be explicitly trusted by browsers and relay clients. "
            "TLS does not add authentication.",
            file=sys.stderr,
            flush=True,
        )
    return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}


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
    serve.add_argument(
        "--host",
        choices=["127.0.0.1", "localhost", "::1", "0.0.0.0"],
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1; 0.0.0.0 exposes unauthenticated network access)",
    )
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument(
        "--allow-ip",
        "--allowed-ip",
        action="append",
        type=_ip_address_argument,
        dest="allowed_ips",
        metavar="IP",
        help="allow only this client IP in network mode (repeat for each IP)",
    )
    serve.add_argument(
        "--password-file",
        type=Path,
        help="optional private UTF-8 password file for browser and relayed access",
    )
    _tls_arguments(serve)
    serve.add_argument("--pi", default=os.environ.get("SWARM_PI", "pi"), help="pi executable path")
    serve.add_argument(
        "--no-recover", action="store_true", help="do not launch saved sessions at startup"
    )
    serve.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    serve.add_argument("--relay-url", default=os.environ.get("SWARM_RELAY_URL"))
    serve.add_argument("--relay-node-id", default=os.environ.get("SWARM_RELAY_NODE_ID"))
    serve.add_argument("--relay-name", default=os.environ.get("SWARM_RELAY_NAME"))
    serve.add_argument("--relay-api-key-file", type=Path)
    serve.add_argument(
        "--allow-remote-restart",
        action="store_true",
        help='allow authenticated federated requests for backend and fleet agent restarts',
    )
    relay = commands.add_parser("relay", help="operate the HTTPS/WSS federation relay")
    relay_commands = relay.add_subparsers(dest="relay_command", required=True)
    relay_serve = relay_commands.add_parser("serve", help="run the federation relay")
    relay_serve.add_argument("--host", default="0.0.0.0")
    relay_serve.add_argument("--port", type=int, default=9443)
    _tls_arguments(relay_serve)
    relay_serve.add_argument(
        "--api-key-file", action="append", type=Path, help="one or two key files (repeatable)"
    )
    commands.add_parser("list", help="list agent records as JSON")
    commands.add_parser("p", help="alias for list (all agent records as JSON)")
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
    report.add_argument(
        "--json", action="store_true", help="print the complete updated record as JSON"
    )

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

    mailbox = commands.add_parser("message", help="use an agent's durable message mailbox")
    mailbox_commands = mailbox.add_subparsers(dest="message_command", required=True)
    send_message = mailbox_commands.add_parser("send", help="queue an ordinary text message")
    send_message.add_argument("id", help="recipient agent ID")
    send_message.add_argument("text", help="message text (32 KiB maximum)")
    send_message.add_argument("--request-id", help="UUID; generated locally when omitted")
    send_message.add_argument(
        "--sender-id",
        default=os.environ.get("SWARM_AGENT_ID"),
        help="sender UUID (defaults to SWARM_AGENT_ID; omitted for a local operator)",
    )
    list_messages = mailbox_commands.add_parser("list", help="list outstanding messages")
    list_messages.add_argument("id", help="recipient agent ID")
    delete_message = mailbox_commands.add_parser("delete", help="cancel an outstanding message")
    delete_message.add_argument("id", help="recipient agent ID")
    delete_message.add_argument("message_id", help="message ID")

    agent_command = commands.add_parser("command", help="queue a supported self-command")
    agent_command.add_argument("text", help="command text, for example '/compact instructions'")
    agent_command.add_argument("--request-id", help="UUID; generated locally when omitted")
    agent_command.add_argument(
        "--id",
        default=os.environ.get("SWARM_AGENT_ID"),
        help="target agent ID (defaults to SWARM_AGENT_ID)",
    )

    files = commands.add_parser(
        "file", help="register and browse original-file references on an agent"
    )
    files.add_argument(
        "--id",
        default=os.environ.get("SWARM_AGENT_ID"),
        help="agent ID (defaults to SWARM_AGENT_ID)",
    )
    file_commands = files.add_subparsers(dest="file_command", required=True)
    add_file = file_commands.add_parser(
        "add", help="register an existing local file without copying it"
    )
    add_file.add_argument("path", type=Path)
    add_file.add_argument("--label", default="")
    file_commands.add_parser("list", help="list registered files and current availability")
    remove_file = file_commands.add_parser(
        "remove", help="remove a reference, never the original file"
    )
    remove_file.add_argument("file_id")

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
    client = AgentClient(
        args.api_url, token_file=local_path(args.home) / "api-auth" / "client.token"
    )
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


def _request_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("--request-id must be a UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError("--request-id must be a canonical UUID")
    return str(parsed)


def _message_text(value: str) -> str:
    if not value.strip():
        raise ValueError("TEXT must be nonempty")
    if len(value.encode("utf-8")) > 32 * 1024:
        raise ValueError("TEXT must be at most 32 KiB in UTF-8")
    return value


def _validate_command(value: str) -> str:
    text = _message_text(value)
    if not text.startswith("/"):
        raise ValueError("command text must start with '/'")
    name, _, argument = text.partition(" ")
    forbidden = {"/new", "/fork", "/clone", "/resume", "/tree", "/import", "/quit"}
    if name in forbidden:
        raise ValueError(f"{name} is forbidden in a Swarm-managed session")
    if name == "/compact":
        return text
    if name == "/model" and argument.strip() and len(argument.split()) == 1:
        return text
    if name == "/thinking" and argument.strip() in {
        "off", "minimal", "low", "medium", "high", "xhigh", "max"
    }:
        return text
    if name == "/name" and argument.strip():
        return text
    if name == "/reload" and not argument:
        return text
    raise ValueError(f"unsupported agent command: {name}")


def run_mailbox_command(args: argparse.Namespace) -> dict:
    client = AgentClient(
        args.api_url, token_file=local_path(args.home) / "api-auth" / "client.token"
    )
    if args.message_command == "list":
        return client.list_messages(args.id)
    if args.message_command == "delete":
        return client.delete_message(args.id, args.message_id)
    request_id = _request_id(args.request_id)  # Generate before making the network request.
    return client.send_message(
        args.id,
        _message_text(args.text),
        sender_id=args.sender_id,
        request_id=request_id,
    )


def run_self_command(args: argparse.Namespace) -> dict:
    managed_id = os.environ.get("SWARM_AGENT_ID")
    if not args.id:
        raise ValueError("--id or SWARM_AGENT_ID is required for a self-command")
    if managed_id and args.id != managed_id:
        raise ValueError("a managed agent may queue commands only for itself")
    request_id = _request_id(args.request_id)  # Generate before making the network request.
    client = AgentClient(
        args.api_url, token_file=local_path(args.home) / "api-auth" / "client.token"
    )
    return client.command(
        args.id,
        _validate_command(args.text),
        sender_id=args.id,
        request_id=request_id,
    )


def _ip_address_argument(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid IP address: {value}") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def _secret(path: Path | None, environment: str) -> str:
    if path:
        try:
            expanded = path.expanduser()
            value = expanded.read_text(encoding="utf-8").strip()
            if os.name != "nt" and expanded.stat().st_mode & 0o077:
                raise ValueError(f"secret file permissions must be 0600: {expanded}")
        except OSError as exc:
            raise ValueError(f"cannot read secret file {path}: {exc}") from exc
    else:
        value = os.environ.get(environment, "").strip()
    if not value:
        raise ValueError(f"a nonempty secret is required via file or {environment}")
    if len(value.encode()) < 32:
        raise ValueError("relay API keys must be at least 32 bytes")
    return value


def main(argv: list[str] | None = None) -> None:
    root = parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = root.parse_args(argv)
    if args.command is None:
        args = root.parse_args([*argv, "serve"])
    try:
        if args.command == "relay":
            import uvicorn

            from .relay import create_relay_app

            files = args.api_key_file or []
            if len(files) > 2:
                raise ValueError("--api-key-file may be supplied at most twice")
            if files:
                keys = [_secret(path, "") for path in files]
            else:
                keys = [os.environ.get("SWARM_RELAY_API_KEY", "").strip()]
                rotated = os.environ.get("SWARM_RELAY_API_KEY_NEXT", "").strip()
                if rotated:
                    keys.append(rotated)
            keys = [key for key in keys if key]
            if not keys:
                raise ValueError(
                    "relay needs --api-key-file or SWARM_RELAY_API_KEY; a second key may use "
                    "SWARM_RELAY_API_KEY_NEXT"
                )
            uvicorn.run(
                create_relay_app(keys),
                host=args.host,
                port=args.port,
                workers=1,
                ws_max_size=1_048_576,
                **_tls_options(args, relay=True),
            )
            return
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
            print(
                json.dumps(
                    AgentClient(
                        args.api_url, token_file=local_path(args.home) / "api-auth" / "client.token"
                    ).product(args.id, payload),
                    indent=2,
                )
            )
            return
        if args.command == "message":
            print(json.dumps(run_mailbox_command(args), indent=2))
            return
        if args.command == "command":
            print(json.dumps(run_self_command(args), indent=2))
            return
        if args.command == "file":
            if not args.id:
                raise ValueError("--id or SWARM_AGENT_ID is required for file registration")
            client = AgentClient(
                args.api_url, token_file=local_path(args.home) / "api-auth" / "client.token"
            )
            if args.file_command == "add":
                result = client.add_file(
                    args.id, {"path": str(local_path(args.path)), "label": args.label}
                )
            elif args.file_command == "list":
                result = client.files(args.id)
            else:
                result = client.remove_file(args.id, args.file_id)
            print(json.dumps(result, indent=2))
            return
        if args.command in {"spawn", "agent"}:
            print(json.dumps(run_agent_command(args), indent=2))
            return
        store = Store(args.home)
        if args.command == "serve":
            import uvicorn

            from .federation import FederationConfig
            from .server import create_app

            federation_config = None
            if args.relay_url:
                if not args.relay_node_id:
                    raise ValueError("--relay-node-id is required with --relay-url")
                federation_config = FederationConfig(
                    url=args.relay_url,
                    node_id=args.relay_node_id,
                    name=args.relay_name or socket.gethostname(),
                    api_key=_secret(args.relay_api_key_file, "SWARM_RELAY_API_KEY"),
                    local_api_url="",  # Filled after the local dashboard URL is known.
                    allow_remote_restart=args.allow_remote_restart,
                )
            elif args.relay_node_id or args.relay_api_key_file or args.allow_remote_restart:
                raise ValueError("relay node/key/restart options require --relay-url")

            network = args.host == "0.0.0.0"
            allowed_ips = list(dict.fromkeys(args.allowed_ips or []))
            if allowed_ips and not network:
                raise ValueError("--allow-ip requires --host 0.0.0.0")
            browser_host = (
                "127.0.0.1" if network else ("[::1]" if args.host == "::1" else args.host)
            )
            tls = _tls_options(args)
            scheme = "https" if tls else "http"
            url = f"{scheme}://{browser_host}:{args.port}"
            if tls:
                # Local agents and federation forwarding trust this controller's certificate
                # without changing trust for the remote relay or disabling verification.
                os.environ["SWARM_API_CA_FILE"] = tls["ssl_certfile"]
            if federation_config:
                federation_config = FederationConfig(
                    url=federation_config.url,
                    node_id=federation_config.node_id,
                    name=federation_config.name,
                    api_key=federation_config.api_key,
                    local_api_url=url,
                    allow_remote_restart=federation_config.allow_remote_restart,
                )
            print(
                f"Swarm: {url}\nRegistry: {store.home}\nClosing the dashboard does not stop agents.",
                flush=True,
            )
            if network:
                print(f"Listening on 0.0.0.0:{args.port} (all IPv4 interfaces).", flush=True)
                if allowed_ips:
                    print(f"Client IP allowlist: {', '.join(allowed_ips)}", flush=True)
                print(
                    (
                        "WARNING: Browser password authentication is enabled. Network mode allows any "
                        "Host header; use HTTPS, a strong password and firewall restrictions."
                        if args.password_file
                        else "WARNING: Swarm has no authentication and network mode allows any Host header. "
                        "Anyone who can reach this port can access private data and control agents "
                        "with your permissions. Use only a trusted network with firewall restrictions; "
                        "never expose it publicly."
                    ),
                    file=sys.stderr,
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
                    allowed_hosts=["*"] if network else None,
                    allowed_client_ips=allowed_ips or None,
                    api_url=url,
                    federation_config=federation_config,
                    password_file=local_path(args.password_file) if args.password_file else None,
                ),
                host=args.host,
                port=args.port,
                workers=1,
                ws_max_size=262144,
                proxy_headers=not bool(allowed_ips),
                **tls,
            )
        elif args.command in {"list", "p"}:
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
            record = run_report(store, args)
            if args.json:
                print(json.dumps(record, indent=2))
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
