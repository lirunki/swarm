"""Durable owner settings for orchestration-managed agent admission.

The limit is intentionally narrow: it gates new child spawns and wakes requested
through :class:`Orchestrator`.  Operator create, wake, recovery, and restart paths
remain overrides and do not consult this setting.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from .models import Agent, Status
from .store import Store, atomic_write

DEFAULT_MAX_ACTIVE_MANAGED = 20
MIN_MAX_ACTIVE_MANAGED = 1
MAX_MAX_ACTIVE_MANAGED = 10_000
MAX_RECEIPTS = 256
MAX_FILE_BYTES = 256 * 1024
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.01
SCOPE = "orchestration"


class AgentLimitsError(ValueError):
    """A safe-to-display request validation error."""


class AgentLimitsConflict(RuntimeError):
    """A request revision or idempotency identity conflicts with saved state."""


class AgentLimitsCorrupt(RuntimeError):
    """The settings file cannot safely be read or replaced."""


def active(agent: Agent, processes: Any) -> bool:
    """Return whether an agent occupies a managed admission slot.

    Callers still filter on ``agent.managed``.  Keeping that filter outside this
    predicate also makes the predicate match cached dashboard entries exactly.
    """

    return (
        agent.spawn_state == "provisioning"
        or bool(getattr(agent, "restart_job_id", None))
        or processes.alive(agent)
        or (agent.managed and agent.auto_resume and agent.status != Status.done)
    )


def active_entry(entry: dict[str, Any]) -> bool:
    """Cached-state equivalent of :func:`active`, using an already sampled alive bit."""

    return (
        entry.get("spawn_state") == "provisioning"
        or bool(entry.get("restart_job_id"))
        or entry.get("alive") is True
        or (
            entry.get("managed") is True
            and entry.get("auto_resume") is True
            and entry.get("status") != Status.done.value
        )
    )


def count_active(agents: Sequence[Agent], processes: Any) -> int:
    return sum(1 for agent in agents if agent.managed and active(agent, processes))


def count_active_entries(entries: Sequence[dict[str, Any]]) -> int:
    return sum(1 for entry in entries if entry.get("managed") is True and active_entry(entry))


class AgentLimits:
    def __init__(self, store: Store):
        self.store = store
        self.path = store.home / "agent-limits.json"
        self.lock_path = store.locks_dir / "agent-limits.lock"

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": 1,
            "revision": 0,
            "max_active_managed": DEFAULT_MAX_ACTIVE_MANAGED,
            "receipts": [],
        }

    @staticmethod
    def projection(state: dict[str, Any], active_managed: int) -> dict[str, Any]:
        return {
            "revision": state["revision"],
            "max_active_managed": state["max_active_managed"],
            "active_managed": active_managed,
            "scope": SCOPE,
        }

    @staticmethod
    def _integer(value: Any, *, minimum: int, maximum: int | None = None) -> bool:
        return (
            type(value) is int
            and value >= minimum
            and (maximum is None or value <= maximum)
        )

    @staticmethod
    def _uuid(value: Any) -> str:
        if not isinstance(value, str):
            raise AgentLimitsError("request_id must be a UUID")
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise AgentLimitsError("request_id must be a UUID") from exc

    @staticmethod
    def _safe_file(info: os.stat_result, *, maximum_size: int | None = None) -> bool:
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (maximum_size is None or info.st_size <= maximum_size)
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor: int | None = None
        locked = False
        try:
            self.store.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            if not self._safe_file(os.fstat(descriptor)):
                raise OSError("invalid lock")

            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError("lock timeout")
                    time.sleep(LOCK_RETRY_SECONDS)
            yield
        except OSError as exc:
            raise AgentLimitsCorrupt("Agent limits are temporarily unavailable") from exc
        finally:
            if descriptor is not None:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @classmethod
    def _validate_state(cls, value: Any) -> dict[str, Any]:
        try:
            if not isinstance(value, dict) or set(value) != {
                "version",
                "revision",
                "max_active_managed",
                "receipts",
            }:
                raise ValueError
            if value["version"] != 1 or type(value["version"]) is not int:
                raise ValueError
            if not cls._integer(value["revision"], minimum=0):
                raise ValueError
            if not cls._integer(
                value["max_active_managed"],
                minimum=MIN_MAX_ACTIVE_MANAGED,
                maximum=MAX_MAX_ACTIVE_MANAGED,
            ):
                raise ValueError
            receipts = value["receipts"]
            if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS:
                raise ValueError
            clean_receipts: list[dict[str, str]] = []
            seen: set[str] = set()
            for receipt in receipts:
                if not isinstance(receipt, dict) or set(receipt) != {
                    "request_id",
                    "payload_hash",
                }:
                    raise ValueError
                request_id = cls._uuid(receipt["request_id"])
                payload_hash = receipt["payload_hash"]
                if (
                    request_id in seen
                    or not isinstance(payload_hash, str)
                    or len(payload_hash) != 64
                    or any(char not in "0123456789abcdef" for char in payload_hash)
                ):
                    raise ValueError
                seen.add(request_id)
                clean_receipts.append(
                    {"request_id": request_id, "payload_hash": payload_hash}
                )
            return {
                "version": 1,
                "revision": value["revision"],
                "max_active_managed": value["max_active_managed"],
                "receipts": clean_receipts,
            }
        except (AgentLimitsError, KeyError, TypeError, ValueError) as exc:
            raise AgentLimitsCorrupt("Saved agent limits could not be read") from exc

    def _read(self) -> dict[str, Any]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            before = os.fstat(descriptor)
            if not self._safe_file(before, maximum_size=MAX_FILE_BYTES):
                raise ValueError
            content = os.read(descriptor, MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES or os.read(descriptor, 1):
                raise ValueError
            after = os.fstat(descriptor)
            stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
                raise ValueError
            return self._validate_state(json.loads(content.decode("utf-8")))
        except FileNotFoundError:
            return self.empty()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, AgentLimitsCorrupt) as exc:
            raise AgentLimitsCorrupt("Saved agent limits could not be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write(self, state: dict[str, Any]) -> None:
        content = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise AgentLimitsError("Saved agent limits are too large")
        try:
            atomic_write(self.path, content)
        except OSError as exc:
            raise AgentLimitsCorrupt("Agent limits could not be saved") from exc

    def get(self, active_managed: int) -> dict[str, Any]:
        with self._lock():
            return self.projection(self._read(), active_managed)

    @classmethod
    def _request(cls, body: Any) -> tuple[dict[str, Any], str]:
        if not isinstance(body, dict):
            raise AgentLimitsError("Request body must be an object")
        if set(body) != {"max_active_managed", "revision", "request_id"}:
            raise AgentLimitsError(
                "Request must contain only max_active_managed, revision, and request_id"
            )
        if not cls._integer(body["revision"], minimum=0):
            raise AgentLimitsError("revision must be a nonnegative integer")
        if not cls._integer(
            body["max_active_managed"],
            minimum=MIN_MAX_ACTIVE_MANAGED,
            maximum=MAX_MAX_ACTIVE_MANAGED,
        ):
            raise AgentLimitsError(
                f"max_active_managed must be an integer from {MIN_MAX_ACTIVE_MANAGED} "
                f"to {MAX_MAX_ACTIVE_MANAGED}"
            )
        request_id = cls._uuid(body["request_id"])
        normalized = {
            "max_active_managed": body["max_active_managed"],
            "revision": body["revision"],
            "request_id": request_id,
        }
        payload_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return normalized, payload_hash

    def apply(self, body: Any, active_managed: int) -> dict[str, Any]:
        request, payload_hash = self._request(body)
        with self._lock():
            state = self._read()
            for receipt in state["receipts"]:
                if receipt["request_id"] == request["request_id"]:
                    if receipt["payload_hash"] != payload_hash:
                        raise AgentLimitsConflict(
                            "request_id was already used with a different payload"
                        )
                    return self.projection(state, active_managed)
            if request["revision"] != state["revision"]:
                raise AgentLimitsConflict("Agent limits revision is stale")
            state["max_active_managed"] = request["max_active_managed"]
            state["revision"] += 1
            state["receipts"].append(
                {"request_id": request["request_id"], "payload_hash": payload_hash}
            )
            state["receipts"] = state["receipts"][-MAX_RECEIPTS:]
            self._write(state)
            return self.projection(state, active_managed)
