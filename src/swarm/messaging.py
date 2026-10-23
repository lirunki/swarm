"""Durable any-agent mailboxes and an explicit, session-bound Pi delivery bridge.

No process is launched by sending mail. Native Pi receives custom messages through
its API rather than keyboard injection. Acknowledgement means persisted context,
not completion of the requested work. Commands with uncertain execution are not replayed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .meta_compact import busy
from .models import Agent, Status, now
from .store import Store, atomic_write

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger(__name__)
MAX_MESSAGES = 128
MAX_MAILBOX_BYTES = 2 * 1024 * 1024
MAX_RECEIPTS = 256
HEARTBEAT_SECONDS = 15
STALE_DELIVERY_SECONDS = 120
FINAL = {"delivered", "deleted"}
OUTSTANDING = {"pending", "delivering", "executing", "failed", "uncertain"}
COMMANDS = {"compact", "model", "thinking", "name", "reload"}


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=32000)
    request_id: UUID = Field(default_factory=uuid4)
    sender_id: UUID | None = None

    @field_validator("text")
    @classmethod
    def valid_text(cls, text: str) -> str:
        if not text.strip() or any(
            (ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in text
        ):
            raise ValueError("message must be nonblank text without terminal control characters")
        return text


def validate_command(text: str) -> None:
    if not text.startswith("/") or any(
        ord(c) < 32 or ord(c) in (127, 0x2028, 0x2029) for c in text
    ):
        raise ValueError("session command must be one slash-prefixed line")
    name, _, args = text[1:].partition(" ")
    args = args.strip()
    if name not in COMMANDS:
        raise ValueError(
            "Supported Pi commands: /compact, /model provider/model, /thinking LEVEL, /name NAME, /reload. Session switching and shell commands are forbidden"
        )
    if name == "model" and (
        "/" not in args
        or not args.partition("/")[0]
        or not args.partition("/")[2]
        or len(args.split()) != 1
    ):
        raise ValueError("/model requires one exact provider/model ID")
    if name == "thinking" and args not in {
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ValueError("invalid /thinking level")
    if name == "name" and not 1 <= len(args) <= 120:
        raise ValueError("/name requires a name of at most 120 characters")
    if name == "reload" and args:
        raise ValueError("/reload takes no arguments")


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    kind: Literal["message", "command"] = "message"
    text: str
    sender_id: UUID | None = None
    sender_name: str = "User"
    status: Literal[
        "pending", "delivering", "executing", "delivered", "deleted", "failed", "uncertain"
    ] = "pending"
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    payload_sha256: str
    dispatch_epoch: str | None = None
    dispatched_at: datetime | None = None
    delivered_at: datetime | None = None
    last_error: str | None = None

    @field_validator("created_at", "updated_at", "dispatched_at", "delivered_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Message timestamps require a timezone")
        return value


class Mailbox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: UUID
    revision: int = Field(default=0, ge=0)
    messages: list[Message] = Field(default_factory=list)


def prepare_bridge(store: Store, agent: Agent) -> tuple[Path, str]:
    """A fresh epoch per process prevents old acknowledgements controlling a new session."""
    directory = store.home / "control" / str(agent.id)
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError("Mailbox control directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("acks", "requests"):
        path = directory / name
        if path.is_symlink():
            raise ValueError("Mailbox control directory must not be a symlink")
        path.mkdir(mode=0o700, exist_ok=True)
    epoch = str(uuid4())
    atomic_write(
        directory / "inbox.json",
        json.dumps(
            {
                "epoch": epoch,
                "agent_id": str(agent.id),
                "session_id": agent.session_id,
                "awake": False,
                "items": [],
            }
        )
        + "\n",
    )
    store.update(agent.id, {"control_epoch": epoch})
    return directory, epoch


class Messages:
    def __init__(self, controller: Controller):
        self.controller, self.store = controller, controller.store
        self.root = self.store.home / "messages"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache: dict[str, tuple[tuple, Mailbox]] = {}
        self.request_errors: dict[str, str] = {}

    def path(self, agent_id) -> Path:
        path = self.root / f"{UUID(str(agent_id))}.json"
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError("Mailbox files must not be symbolic links")
        return path

    def control(self, agent_id) -> Path:
        path = self.store.home / "control" / str(UUID(str(agent_id)))
        if any(p.is_symlink() for p in (path, path.parent, path / "acks", path / "requests")):
            raise ValueError("Mailbox control directory must not be a symbolic link")
        return path

    @staticmethod
    def read_json(path: Path, limit: int) -> dict:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError("Invalid or oversized mailbox data")
        with path.open(encoding="utf-8") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Oversized mailbox data")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Invalid mailbox data")
        return value

    def read(self, agent_id) -> Mailbox:
        path = self.path(agent_id)
        if not path.exists():
            return Mailbox(agent_id=agent_id)
        info = path.stat()
        signature = (info.st_ino, info.st_size, info.st_mtime_ns)
        cached = self.cache.get(str(agent_id))
        if cached and cached[0] == signature:
            return cached[1].model_copy(deep=True)
        mailbox = Mailbox.model_validate(self.read_json(path, MAX_MAILBOX_BYTES))
        if str(mailbox.agent_id) != str(agent_id):
            raise ValueError("Mailbox identity mismatch")
        self.cache[str(agent_id)] = (signature, mailbox.model_copy(deep=True))
        return mailbox

    def save(self, mailbox: Mailbox):
        pending = [m for m in mailbox.messages if m.status not in FINAL]
        if len(pending) > MAX_MESSAGES:
            raise ValueError("Agent has 128 outstanding messages; delete old messages first")
        # Final records retain idempotency/deduplication metadata, not duplicated message bodies.
        done = [m for m in mailbox.messages if m.status in FINAL][-MAX_RECEIPTS:]
        for message in done:
            message.text = ""
        mailbox.messages = sorted([*pending, *done], key=lambda m: m.created_at)
        mailbox.revision += 1
        text = mailbox.model_dump_json() + "\n"
        if len(text.encode("utf-8")) > MAX_MAILBOX_BYTES:
            raise ValueError("Mailbox exceeds 2 MiB; delete old messages first")
        atomic_write(self.path(mailbox.agent_id), text)
        self.cache.pop(str(mailbox.agent_id), None)
        # Keep receipt files bounded with the retained idempotency window.
        try:
            directory = self.control(mailbox.agent_id) / "acks"
            keep = {str(message.id) for message in mailbox.messages}
            if directory.is_dir():
                for receipt in directory.glob("*.json"):
                    if str(UUID(receipt.stem)) not in keep and not receipt.is_symlink():
                        receipt.unlink(missing_ok=True)
        except (OSError, ValueError):
            log.warning("Deferred mailbox receipt cleanup for %s", mailbox.agent_id)

    def summary(self, agent_id) -> dict:
        mailbox = self.read(agent_id)
        return {
            "message_count": sum(m.status in OUTSTANDING for m in mailbox.messages),
            "messages_revision": mailbox.revision,
            "messages_error": None,
        }

    def list(self, agent_id) -> dict:
        self.store.get(agent_id)
        with self.store.lock(agent_id):
            box = self.read(agent_id)
            return {
                "agent_id": str(agent_id),
                "revision": box.revision,
                "messages": [
                    m.model_dump(mode="json") for m in box.messages if m.status in OUTSTANDING
                ],
            }

    def send(self, agent_id: str, request: MessageRequest, kind: str = "message") -> dict:
        with self.controller.lock:
            agent = self.store.get(agent_id)
            if not agent.managed:
                raise ValueError(
                    "Adopt the agent before sending messages; observed terminals are not controlled by Swarm"
                )
            if kind == "command":
                if agent.backend != "pi":
                    raise ValueError("Session commands are currently supported only for Pi agents")
                if request.sender_id is not None and request.sender_id != agent.id:
                    raise ValueError("A session command must be addressed to the sender itself")
                validate_command(request.text)
            sender = self.store.get(request.sender_id) if request.sender_id else None
            digest = hashlib.sha256(
                json.dumps(
                    {"text": request.text, "sender_id": str(request.sender_id), "kind": kind},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            with self.store.lock(agent.id):
                box = self.read(agent.id)
                old = next((m for m in box.messages if m.id == request.request_id), None)
                if old is not None:
                    if old.payload_sha256 != digest:
                        raise RuntimeError(
                            "Request ID was already used for different message content"
                        )
                    return {
                        "agent_id": str(agent.id),
                        "message": old.model_dump(mode="json"),
                        "revision": box.revision,
                        "reused": True,
                    }
                message = Message(
                    id=request.request_id,
                    kind=kind,
                    text=request.text,
                    sender_id=request.sender_id,
                    sender_name=sender.name if sender else "User",
                    payload_sha256=digest,
                )
                box.messages.append(message)
                self.save(box)
            # Do not turn a committed send into an apparent failure due to tmux/refresh.
            try:
                self.tick(agent)
                self.controller.refresh()
            except (OSError, ValueError, RuntimeError):
                log.exception("Message persisted; deferred fleet refresh")
            current = self.read(agent.id)
            return {
                "agent_id": str(agent.id),
                "message": next(
                    m.model_dump(mode="json") for m in current.messages if m.id == message.id
                ),
                "revision": current.revision,
                "reused": False,
            }

    def delete(self, agent_id: str, message_id: str) -> dict:
        with self.controller.lock, self.store.lock(agent_id):
            self.store.get(agent_id)
            box = self.read(agent_id)
            message = next((m for m in box.messages if str(m.id) == str(message_id)), None)
            if not message or message.status in FINAL:
                raise FileNotFoundError("Outstanding message not found")
            if message.status in {"delivering", "executing"}:
                raise RuntimeError(
                    "Delivery is already in flight and cannot be recalled safely; inspect the session before retrying"
                )
            message.status, message.updated_at = "deleted", now()
            self.save(box)
            return {
                "deleted": True,
                "agent_id": str(agent_id),
                "message_id": str(message_id),
                "revision": box.revision,
            }

    def awake(self, agent: Agent, alive: bool | None = None) -> bool:
        return bool(
            agent.managed
            and agent.status not in {Status.dormant, Status.done}
            and agent.spawn_state in {None, "ready"}
            and not busy(agent)
            and not getattr(agent, "stop_pending", False)
            and not getattr(agent, "orchestration_stop", None)
            and (self.controller.processes.alive(agent) if alive is None else alive)
        )

    def heartbeat(self, agent: Agent) -> bool:
        try:
            data = self.read_json(self.control(agent.id) / "heartbeat.json", 4096)
            at = datetime.fromisoformat(data["updated_at"])
            age = (now() - at).total_seconds()
            return (
                data.get("protocol") == 1
                and data.get("epoch") == getattr(agent, "control_epoch", None)
                and data.get("session_id") == agent.session_id
                and -5 <= age <= HEARTBEAT_SECONDS
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def ingest_requests(self, agent: Agent):
        path = self.control(agent.id) / "requests"
        if not path.is_dir() or path.is_symlink():
            return
        # Bound enumeration as well as processing; a malformed entry must not starve others.
        files = []
        with os.scandir(path) as entries:
            for count, entry in enumerate(entries):
                if count >= 256:
                    break
                try:
                    if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                        continue
                    UUID(entry.name[:-5])
                    info = entry.stat(follow_symlinks=False)
                    if info.st_size > 65536:
                        continue
                    files.append((info.st_mtime_ns, Path(entry.path)))
                except (OSError, ValueError):
                    continue
        # Avoid reentrant send -> tick -> ingest. Request identity makes recovery idempotent.
        for _, file in sorted(files)[:32]:
            try:
                id = UUID(file.stem)
                data = self.read_json(file, 65536)
                if (
                    str(data.get("id")) != str(id)
                    or data.get("kind") != "command"
                    or str(data.get("sender_id")) != str(agent.id)
                ):
                    raise ValueError("Invalid self-command request identity")
                if agent.backend != "pi":
                    raise ValueError("Session commands currently require Pi")
                requested_at = datetime.fromisoformat(
                    str(data.get("created_at")).replace("Z", "+00:00")
                )
                if requested_at.tzinfo is None:
                    raise ValueError("Self-command request needs a timezone")
                request = MessageRequest(text=data.get("text"), request_id=id, sender_id=agent.id)
                validate_command(request.text)
                with self.store.lock(agent.id):
                    box = self.read(agent.id)
                    digest = hashlib.sha256(
                        json.dumps(
                            {"text": request.text, "sender_id": str(agent.id), "kind": "command"},
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()
                    old = next((m for m in box.messages if m.id == id), None)
                    if old and old.payload_sha256 != digest:
                        raise ValueError("Self-command ID conflict")
                    if not old:
                        box.messages.append(
                            Message(
                                id=id,
                                kind="command",
                                text=request.text,
                                sender_id=agent.id,
                                sender_name=agent.name,
                                payload_sha256=digest,
                                created_at=requested_at,
                            )
                        )
                        self.save(box)
                file.unlink(missing_ok=True)
                self.request_errors.pop(str(file), None)
            except (OSError, ValueError, RuntimeError) as exc:
                if self.request_errors.get(str(file)) != str(exc):
                    log.warning("Cannot ingest self command %s: %s", file.name, type(exc).__name__)
                    self.request_errors[str(file)] = str(exc)
                    if len(self.request_errors) > 4096:
                        self.request_errors.clear()
                # Retain valid requests when the queue is full; caller can delete old mail.
                continue

    def tick(self, agent: Agent):
        if not agent.managed:
            return
        self.ingest_requests(agent)
        directory = self.control(agent.id)
        alive = self.controller.processes.alive(agent)
        with self.store.lock(agent.id):
            # Reports use this same cross-process lock. Never reopen a bridge using
            # an earlier scan after the agent has just reported dormant/done/stop.
            agent = self.store.get(agent.id)
            epoch = getattr(agent, "control_epoch", None)
            awake = self.awake(agent, alive)
            fresh = self.heartbeat(agent)
            bridge = awake and agent.backend == "pi" and epoch and fresh
            box = self.read(agent.id)
            changed = False
            for message in box.messages:
                if message.status not in {"delivering", "executing", "uncertain"}:
                    continue
                stale = (
                    not fresh
                    and message.dispatched_at is not None
                    and (now() - message.dispatched_at).total_seconds() > STALE_DELIVERY_SECONDS
                )
                ack_path = directory / "acks" / f"{message.id}.json"
                ack = None
                if ack_path.exists():
                    try:
                        ack = self.read_json(ack_path, 256 * 1024)
                    except (OSError, ValueError):
                        pass
                valid = bool(
                    ack
                    and ack.get("id") == str(message.id)
                    and ack.get("epoch") == message.dispatch_epoch
                    and message.dispatch_epoch == epoch
                    and ack.get("session_id") == agent.session_id
                )
                if valid:
                    status = ack.get("status")
                    if status in {"delivered", "failed", "uncertain"} or (
                        status == "executing"
                        and message.kind == "command"
                        and message.status != "uncertain"
                    ):
                        if status == "executing" and (not alive or stale):
                            status = "uncertain"
                            ack["error"] = (
                                "Command process stopped during execution; not automatically replayed"
                            )
                        if status != message.status:
                            message.status = status
                            message.updated_at = now()
                            if status == "delivered":
                                message.delivered_at = now()
                            message.last_error = str(ack.get("error") or "")[:2048] or None
                            changed = True
                        continue
                if message.status == "uncertain":
                    continue
                if stale and alive and message.dispatch_epoch == epoch:
                    message.status = "uncertain"
                    message.last_error = (
                        "Pi mailbox stopped acknowledging delivery. Outcome is unknown; "
                        "inspect the session before resending. Deleting removes tracking, not already queued input."
                    )
                    message.updated_at = now()
                    changed = True
                elif not alive or message.dispatch_epoch != epoch:
                    uncertain = message.kind == "command" and (
                        message.status == "executing" or message.dispatch_epoch != epoch
                    )
                    message.status = "uncertain" if uncertain else "pending"
                    message.last_error = (
                        "Command execution outcome is unknown after process change; not automatically replayed"
                        if uncertain
                        else None
                    )
                    message.updated_at = now()
                    changed = True
            for message in box.messages:
                if message.status == "pending":
                    error = (
                        "Waiting for Pi mailbox; relaunch older agents or inspect extension startup"
                        if awake and not bridge
                        else None
                    )
                    if error != message.last_error:
                        message.last_error, message.updated_at = error, now()
                        changed = True
            if bridge:
                active = [m for m in box.messages if m.status in {"delivering", "executing"}]
                for message in box.messages:
                    if message.status == "pending" and len(active) < 8:
                        message.status = "delivering"
                        message.dispatch_epoch = epoch
                        message.dispatched_at = message.updated_at = now()
                        message.last_error = None
                        active.append(message)
                        changed = True
            if changed:
                self.save(box)  # Persist lease BEFORE making bytes visible to the extension.
            if epoch and directory.is_dir():
                data = {
                    "epoch": epoch,
                    "agent_id": str(agent.id),
                    "session_id": agent.session_id,
                    "awake": bool(bridge),
                    "items": [
                        m.model_dump(mode="json")
                        for m in box.messages
                        if m.status in {"delivering", "executing"}
                    ],
                }
                text = json.dumps(data, ensure_ascii=False) + "\n"
                path = directory / "inbox.json"
                if path.is_symlink():
                    raise ValueError("Mailbox inbox must not be a symlink")
                if not path.exists() or path.read_text(encoding="utf-8") != text:
                    atomic_write(path, text)

    def forget(self, agent_id):
        self.path(agent_id).unlink(missing_ok=True)
        directory = self.control(agent_id)
        if directory.is_dir():
            shutil.rmtree(directory)
        self.cache.pop(str(agent_id), None)
