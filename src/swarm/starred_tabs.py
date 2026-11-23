"""Private, revisioned user preferences for starred dashboard tabs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from .store import Store, atomic_write

MAX_STARS = 512
MAX_RECEIPTS = 256
MAX_FILE_BYTES = 256 * 1024
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.01


class StarredTabsError(ValueError):
    """A safe-to-display request validation error."""


class StarredTabsConflict(RuntimeError):
    """A stale revision or reused idempotency identity."""


class StarredTabsCorrupt(RuntimeError):
    """The preference file cannot safely be read or replaced."""


class StarredTabs:
    """Store starred tab tokens in one bounded, private JSON file.

    ``source`` accepts a :class:`Store` for controller use or a home path for
    callers which only need this preference store.
    """

    def __init__(self, source: Store | Path):
        if isinstance(source, Store):
            self.home = source.home
            self.locks_dir = source.locks_dir
        else:
            self.home = Path(source).expanduser().resolve()
            self.locks_dir = self.home / "locks"
        self.path = self.home / "starred-tabs.json"
        self.lock_path = self.locks_dir / "starred-tabs.lock"

    @staticmethod
    def empty() -> dict[str, Any]:
        return {"version": 1, "revision": 0, "starred": [], "receipts": []}

    @staticmethod
    def projection(state: dict[str, Any]) -> dict[str, Any]:
        return {"revision": state["revision"], "starred": list(state["starred"])}

    @staticmethod
    def _integer(value: Any) -> bool:
        return type(value) is int and value >= 0

    @staticmethod
    def _uuid(value: Any) -> str:
        if not isinstance(value, str):
            raise StarredTabsError("request_id must be a UUID")
        try:
            return str(UUID(value))
        except (ValueError, AttributeError) as exc:
            raise StarredTabsError("request_id must be a UUID") from exc

    @staticmethod
    def _token(value: Any) -> str:
        if value == "browser":
            return "browser"
        if not isinstance(value, str):
            raise StarredTabsError("starred tab tokens must use a supported format")
        prefix, separator, identifier = value.partition(":")
        if separator != ":" or prefix not in {"agent", "agent-group"}:
            raise StarredTabsError("starred tab tokens must use a supported format")
        try:
            selected = str(UUID(identifier))
        except (ValueError, AttributeError) as exc:
            raise StarredTabsError("starred tab tokens must use a supported format") from exc
        return f"{prefix}:{selected}"

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
            self.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            raise StarredTabsCorrupt("Starred tabs are temporarily unavailable") from exc
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
                "starred",
                "receipts",
            }:
                raise ValueError
            if type(value["version"]) is not int or value["version"] != 1:
                raise ValueError
            if not cls._integer(value["revision"]):
                raise ValueError
            starred = value["starred"]
            if not isinstance(starred, list) or len(starred) > MAX_STARS:
                raise ValueError
            clean_starred: list[str] = []
            seen_tokens: set[str] = set()
            for token in starred:
                selected = cls._token(token)
                if selected != token or selected in seen_tokens:
                    raise ValueError
                seen_tokens.add(selected)
                clean_starred.append(selected)
            receipts = value["receipts"]
            if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS:
                raise ValueError
            clean_receipts: list[dict[str, str]] = []
            seen_requests: set[str] = set()
            for receipt in receipts:
                if not isinstance(receipt, dict) or set(receipt) != {
                    "request_id",
                    "payload_hash",
                }:
                    raise ValueError
                request_id = cls._uuid(receipt["request_id"])
                payload_hash = receipt["payload_hash"]
                if (
                    request_id != receipt["request_id"]
                    or request_id in seen_requests
                    or not isinstance(payload_hash, str)
                    or len(payload_hash) != 64
                    or any(character not in "0123456789abcdef" for character in payload_hash)
                ):
                    raise ValueError
                seen_requests.add(request_id)
                clean_receipts.append(
                    {"request_id": request_id, "payload_hash": payload_hash}
                )
            return {
                "version": 1,
                "revision": value["revision"],
                "starred": clean_starred,
                "receipts": clean_receipts,
            }
        except (KeyError, TypeError, ValueError, StarredTabsError) as exc:
            raise StarredTabsCorrupt("Saved starred tabs could not be read") from exc

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
            blocks: list[bytes] = []
            remaining = MAX_FILE_BYTES + 1
            while remaining:
                block = os.read(descriptor, min(64 * 1024, remaining))
                if not block:
                    break
                blocks.append(block)
                remaining -= len(block)
            content = b"".join(blocks)
            if len(content) > MAX_FILE_BYTES or os.read(descriptor, 1):
                raise ValueError
            after = os.fstat(descriptor)
            stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
                raise ValueError
            return self._validate_state(json.loads(content.decode("utf-8")))
        except FileNotFoundError:
            return self.empty()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            StarredTabsCorrupt,
        ) as exc:
            raise StarredTabsCorrupt("Saved starred tabs could not be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write(self, state: dict[str, Any]) -> None:
        content = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise StarredTabsError("Saved starred tabs are too large")
        try:
            atomic_write(self.path, content)
        except OSError as exc:
            raise StarredTabsCorrupt("Starred tabs could not be saved") from exc

    def get(self) -> dict[str, Any]:
        with self._lock():
            return self.projection(self._read())

    @classmethod
    def _request(cls, body: Any) -> tuple[dict[str, Any], str]:
        if not isinstance(body, dict):
            raise StarredTabsError("Request body must be an object")
        common = {"action", "revision", "request_id"}
        action = body.get("action")
        expected = common | ({"tab_id"} if action in {"star", "unstar"} else {"starred"})
        if action not in {"star", "unstar", "import"} or set(body) != expected:
            raise StarredTabsError("Request fields do not match the starred tab action")
        if not cls._integer(body["revision"]):
            raise StarredTabsError("revision must be a nonnegative integer")
        request_id = cls._uuid(body["request_id"])
        normalized: dict[str, Any] = {
            "action": action,
            "revision": body["revision"],
            "request_id": request_id,
        }
        if action in {"star", "unstar"}:
            normalized["tab_id"] = cls._token(body["tab_id"])
        else:
            if not isinstance(body["starred"], list):
                raise StarredTabsError("starred must be an array of tab tokens")
            normalized["starred"] = [cls._token(token) for token in body["starred"]]
        payload_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return normalized, payload_hash

    def apply(self, body: Any) -> dict[str, Any]:
        request, payload_hash = self._request(body)
        with self._lock():
            state = self._read()
            for receipt in state["receipts"]:
                if receipt["request_id"] == request["request_id"]:
                    if receipt["payload_hash"] != payload_hash:
                        raise StarredTabsConflict(
                            "request_id was already used with a different payload"
                        )
                    return self.projection(state)
            if request["revision"] != state["revision"]:
                raise StarredTabsConflict("Starred tabs revision is stale")

            before = list(state["starred"])
            if request["action"] == "star":
                if request["tab_id"] not in state["starred"]:
                    state["starred"].append(request["tab_id"])
            elif request["action"] == "unstar":
                if request["tab_id"] in state["starred"]:
                    state["starred"].remove(request["tab_id"])
            else:
                known = set(state["starred"])
                for token in request["starred"]:
                    if token not in known:
                        state["starred"].append(token)
                        known.add(token)
            if len(state["starred"]) > MAX_STARS:
                raise StarredTabsError(f"At most {MAX_STARS} tabs may be starred")
            if state["starred"] != before:
                state["revision"] += 1
            state["receipts"].append(
                {"request_id": request["request_id"], "payload_hash": payload_hash}
            )
            state["receipts"] = state["receipts"][-MAX_RECEIPTS:]
            self._write(state)
            return self.projection(state)
