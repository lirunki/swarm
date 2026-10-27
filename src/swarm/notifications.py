"""Durable, controller-independent user notification inbox."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Status, now
from .paths import local_path
from .store import Store, atomic_write

MAX_TEXT_BYTES = 32 * 1024
MAX_UNREAD = 128
MAX_DELETED_RECEIPTS = 256
MAX_STORE_BYTES = 5 * 1024 * 1024


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    agent_id: UUID
    agent_name: str
    status: Literal["waiting", "done"]
    text: str
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=now)
    state: Literal["unread", "deleted"] = "unread"

    @field_validator("created_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Notification timestamps require a timezone")
        return value


class NotificationInbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    notifications: list[Notification] = Field(default_factory=list)


def validate_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("notification TEXT must be nonblank")
    if any(
        (ord(char) < 32 and char not in "\n\r\t") or ord(char) in (127, 0x2028, 0x2029)
        for char in text
    ):
        raise ValueError("notification TEXT contains unsafe control characters")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("notification TEXT must be valid UTF-8") from exc
    if size > MAX_TEXT_BYTES:
        raise ValueError("notification TEXT must be at most 32 KiB in UTF-8")
    return text


def _digest(agent_id: UUID, text: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"agent_id": str(agent_id), "text": text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class Notifications:
    """A bounded JSON inbox sharing a stable flock sidecar across atomic replaces."""

    def __init__(self, store: Store):
        self.store = store
        self.path = self.store.home / "notifications.json"
        self.lock_path = self.store.home / "notifications.lock"

    @contextmanager
    def lock(self) -> Iterator[None]:
        if self.lock_path.is_symlink():
            raise ValueError("Notification lock must not be a symbolic link")
        with self.lock_path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def read(self) -> NotificationInbox:
        if not self.path.exists():
            return NotificationInbox()
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("Notification store must be a regular file")
        if self.path.stat().st_size > MAX_STORE_BYTES:
            raise ValueError("Notification store exceeds 5 MiB")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("Notification store must be valid UTF-8") from exc
        if not isinstance(value, dict):
            raise ValueError("Notification store must contain an object")
        return NotificationInbox.model_validate(value)

    def _save(self, inbox: NotificationInbox) -> None:
        unread = [item for item in inbox.notifications if item.state == "unread"]
        deleted = [item for item in inbox.notifications if item.state == "deleted"]
        if len(unread) > MAX_UNREAD:
            raise ValueError(
                "Notification inbox has 128 unread items; delete old notifications first"
            )
        # Deleted records are bounded receipts: they retain enough payload metadata to
        # distinguish an idempotent retry from a request-ID collision. Drop the oldest
        # receipts further when unusually large payloads would exceed the byte bound.
        receipts = deleted[-MAX_DELETED_RECEIPTS:]
        for item in receipts:
            item.text = ""
        inbox.revision += 1
        while True:
            inbox.notifications = sorted([*unread, *receipts], key=lambda item: item.created_at)
            content = inbox.model_dump_json() + "\n"
            if len(content.encode("utf-8")) <= MAX_STORE_BYTES:
                break
            if not receipts:
                raise ValueError("Notification store exceeds 5 MiB")
            receipts.pop(0)
        atomic_write(self.path, content)

    @staticmethod
    def _verify_session(agent, session_id: str | None, session_file: str | None) -> None:
        if not agent.managed:
            return
        if session_id and agent.session_id and session_id != agent.session_id:
            raise ValueError(
                "notification came from a different pi session; do not switch managed sessions"
            )
        if (
            session_file
            and agent.session_file
            and local_path(agent.session_file) != local_path(session_file)
        ):
            raise ValueError(
                "notification came from a different pi session; do not switch managed sessions"
            )

    def post(
        self,
        agent_id: str | UUID,
        text: str,
        request_id: str | UUID | None = None,
        *,
        session_id: str | None = None,
        session_file: str | None = None,
    ) -> dict:
        text = validate_text(text)
        selected_id = UUID(str(agent_id))
        selected_request = uuid4() if request_id is None else UUID(str(request_id))
        digest = _digest(selected_id, text)

        # Agent and inbox locks make the eligibility decision and commit one operation.
        # The ordering matches other agent-scoped stores (agent lock before their own data).
        with self.store.lock(selected_id):
            agent = self.store.get(selected_id)
            self._verify_session(agent, session_id, session_file)
            with self.lock():
                inbox = self.read()
                existing = next(
                    (item for item in inbox.notifications if item.id == selected_request), None
                )
                if existing is not None:
                    if existing.payload_sha256 != digest:
                        raise RuntimeError(
                            "Request ID was already used for different notification content"
                        )
                    return {
                        "notification": existing.model_dump(mode="json"),
                        "revision": inbox.revision,
                        "reused": True,
                    }
                if agent.status not in {Status.waiting, Status.done} or (
                    agent.status == Status.waiting and not agent.wait_reason.strip()
                ):
                    raise ValueError(
                        "agent may notify only while waiting with a reason, or when done"
                    )
                notification = Notification(
                    id=selected_request,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    status=agent.status.value,
                    text=text,
                    payload_sha256=digest,
                )
                inbox.notifications.append(notification)
                self._save(inbox)
                return {
                    "notification": notification.model_dump(mode="json"),
                    "revision": inbox.revision,
                    "reused": False,
                }

    def list(self) -> dict:
        with self.lock():
            inbox = self.read()
            notifications = [
                item.model_dump(mode="json")
                for item in inbox.notifications
                if item.state == "unread"
            ]
            return {
                "notifications": notifications,
                "count": len(notifications),
                "revision": inbox.revision,
            }

    def summary(self) -> dict:
        result = self.list()
        return {"count": result["count"], "revision": result["revision"]}

    def delete(self, notification_id: str | UUID) -> dict:
        selected_id = UUID(str(notification_id))
        with self.lock():
            inbox = self.read()
            item = next((value for value in inbox.notifications if value.id == selected_id), None)
            if item is None:
                raise FileNotFoundError("Notification not found")
            if item.state == "deleted":
                return {
                    "deleted": True,
                    "notification_id": str(selected_id),
                    "revision": inbox.revision,
                    "reused": True,
                }
            item.state = "deleted"
            self._save(inbox)
            return {
                "deleted": True,
                "notification_id": str(selected_id),
                "revision": inbox.revision,
                "reused": False,
            }
