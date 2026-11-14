"""Private, durable owner configuration for Swarm's dedicated browser.

This module only stores launch permission and the small set of options consumed by
``remote_browser.BrowserManager``.  Runtime lifecycle, routes, federation policy,
and the fixed private browser profile remain owned by their parent components.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from . import paths
from .store import atomic_write

MAX_PAYLOAD_BYTES = 16 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_RECEIPTS = 256
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.01


class BrowserSettingsError(ValueError):
    """A safe-to-display browser-settings request validation error."""


class BrowserSettingsConflict(RuntimeError):
    """The revision or idempotency identity conflicts with saved state."""


class BrowserSettingsCorrupt(RuntimeError):
    """Browser settings cannot safely be read, locked, or replaced."""


class BrowserSettings:
    """Persist the owner-controlled browser launch configuration.

    ``get()`` is read-only with respect to the settings file.  ``apply()`` uses a
    bounded receipt ledger, so a caller can safely retry an uncertain request with
    the same UUID.  The sidecar lock is stable across atomic settings-file replaces.
    """

    def __init__(self, home: str | os.PathLike[str]):
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "browser-settings.json"
        self.lock_path = self.home / "browser-settings.lock"

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": 1,
            "revision": 0,
            "enabled": False,
            "headless": True,
            "executable": None,
            "receipts": [],
        }

    @staticmethod
    def _projection(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "revision": state["revision"],
            "enabled": state["enabled"],
            "headless": state["headless"],
            "executable": state["executable"],
        }

    @staticmethod
    def _integer(value: Any, *, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum

    @staticmethod
    def _uuid(value: Any) -> str:
        if not isinstance(value, str):
            raise BrowserSettingsError("request_id must be a UUID")
        try:
            return str(UUID(value))
        except (ValueError, AttributeError) as exc:
            raise BrowserSettingsError("request_id must be a UUID") from exc

    @staticmethod
    def _safe_file(info: os.stat_result, *, maximum_size: int | None = None) -> bool:
        private = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        if os.name != "nt":
            private = (
                private
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
            )
        return private and (maximum_size is None or info.st_size <= maximum_size)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor: int | None = None
        locked = False
        try:
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            if not self._safe_file(os.fstat(descriptor)):
                raise OSError("unsafe lock")

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
            raise BrowserSettingsCorrupt(
                "Browser settings are temporarily unavailable"
            ) from exc
        finally:
            if descriptor is not None:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _sane_shebang(header: bytes) -> bool:
        if not header.startswith(b"#!"):
            return False
        line, separator, _rest = header.partition(b"\n")
        if not separator or len(line) > 127 or b"\x00" in line:
            return False
        try:
            text = line[2:].decode("ascii").strip()
        except UnicodeDecodeError:
            return False
        if not text or any(ord(character) < 32 or ord(character) == 127 for character in text):
            return False
        interpreter = text.split(None, 1)[0]
        return interpreter.startswith("/") and interpreter not in {"/", "//"}

    @classmethod
    def _validate_executable(cls, value: Any) -> str | None:
        if value is None:
            return None
        if value == "wsl:msedge":
            if sys.platform == "linux" and paths.is_wsl():
                return value
            raise BrowserSettingsError("wsl:msedge is available only from WSL on Linux")
        if not isinstance(value, str) or not value:
            raise BrowserSettingsError("executable must be null or an absolute native path")
        try:
            path_size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise BrowserSettingsError("executable path is not valid UTF-8") from exc
        if path_size > 4096:
            raise BrowserSettingsError("executable must be null or an absolute native path")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise BrowserSettingsError("executable path contains control characters")

        supplied = Path(value)
        if not supplied.is_absolute():
            raise BrowserSettingsError("executable must be an absolute path")
        unix_suffix = supplied.suffix.lower()
        if sys.platform != "win32" and unix_suffix in {".exe", ".cmd", ".bat"}:
            raise BrowserSettingsError(
                "Windows browser executables cannot be launched here; install Edge or Chromium "
                "for this system"
            )
        try:
            resolved = supplied.resolve(strict=True)
            descriptor = os.open(
                resolved,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise BrowserSettingsError("executable must resolve to a regular file")
                header = os.read(descriptor, 128)
            finally:
                os.close(descriptor)
        except BrowserSettingsError:
            raise
        except (OSError, RuntimeError) as exc:
            raise BrowserSettingsError("executable does not exist or has an invalid symbolic link") from exc

        if sys.platform == "win32":
            supported = resolved.suffix.lower() == ".exe" and header.startswith(b"MZ")
        elif header.startswith(b"MZ"):
            raise BrowserSettingsError(
                "Windows browser executables cannot be launched here; install Edge or Chromium "
                "inside WSL/Linux"
            )
        elif sys.platform == "darwin":
            supported = header[:4] in {
                b"\xfe\xed\xfa\xce",
                b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xcf\xfa\xed\xfe",
                b"\xca\xfe\xba\xbe",
                b"\xbe\xba\xfe\xca",
            } or cls._sane_shebang(header)
        else:
            supported = header.startswith(b"\x7fELF") or cls._sane_shebang(header)
        if not supported:
            raise BrowserSettingsError(
                "executable must be a native browser executable or executable shebang wrapper"
            )
        if os.name != "nt" and not os.access(supplied, os.X_OK):
            raise BrowserSettingsError("executable is not executable")
        return str(supplied)

    @classmethod
    def _validate_state(cls, value: Any) -> dict[str, Any]:
        try:
            if not isinstance(value, dict) or set(value) != {
                "version",
                "revision",
                "enabled",
                "headless",
                "executable",
                "receipts",
            }:
                raise ValueError
            if type(value["version"]) is not int or value["version"] != 1:
                raise ValueError
            if not cls._integer(value["revision"]):
                raise ValueError
            if type(value["enabled"]) is not bool or type(value["headless"]) is not bool:
                raise ValueError
            executable = cls._validate_executable(value["executable"])
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
                    or any(character not in "0123456789abcdef" for character in payload_hash)
                ):
                    raise ValueError
                seen.add(request_id)
                clean_receipts.append(
                    {"request_id": request_id, "payload_hash": payload_hash}
                )
            return {
                "version": 1,
                "revision": value["revision"],
                "enabled": value["enabled"],
                "headless": value["headless"],
                "executable": executable,
                "receipts": clean_receipts,
            }
        except (BrowserSettingsError, KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise BrowserSettingsCorrupt("Saved browser settings could not be read") from exc

    def _read(self) -> dict[str, Any]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            before = os.fstat(descriptor)
            if not self._safe_file(before, maximum_size=MAX_FILE_BYTES):
                raise ValueError
            content = os.read(descriptor, MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES or os.read(descriptor, 1):
                raise ValueError
            after = os.fstat(descriptor)
            stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, field) != getattr(after, field) for field in stable):
                raise ValueError
            return self._validate_state(json.loads(content.decode("utf-8")))
        except FileNotFoundError:
            return self.empty()
        except (
            BrowserSettingsCorrupt,
            json.JSONDecodeError,
            OSError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise BrowserSettingsCorrupt("Saved browser settings could not be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write(self, state: dict[str, Any]) -> None:
        content = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise BrowserSettingsError("Saved browser settings are too large")
        try:
            atomic_write(self.path, content)
        except OSError as exc:
            raise BrowserSettingsCorrupt("Browser settings could not be saved") from exc

    def get(self) -> dict[str, Any]:
        """Return the public configuration without creating a settings file."""

        with self._lock():
            return self._projection(self._read())

    def config(self) -> dict[str, Any]:
        """Return exactly the configuration accepted by ``BrowserManager``."""

        return {**self.get(), "cdp_port": 0}

    @classmethod
    def _request(cls, body: Any) -> tuple[dict[str, Any], str]:
        if not isinstance(body, dict):
            raise BrowserSettingsError("Request body must be an object")
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise BrowserSettingsError("Request body must be JSON-compatible") from exc
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise BrowserSettingsError("Request body exceeds 16 KiB")
        if set(body) != {"revision", "request_id", "enabled", "headless", "executable"}:
            raise BrowserSettingsError(
                "Request must contain only revision, request_id, enabled, headless, and executable"
            )
        if not cls._integer(body["revision"]):
            raise BrowserSettingsError("revision must be a nonnegative integer")
        if type(body["enabled"]) is not bool:
            raise BrowserSettingsError("enabled must be a boolean")
        if type(body["headless"]) is not bool:
            raise BrowserSettingsError("headless must be a boolean")

        request_id = cls._uuid(body["request_id"])
        executable = cls._validate_executable(body["executable"])
        normalized = {
            "revision": body["revision"],
            "request_id": request_id,
            "enabled": body["enabled"],
            "headless": body["headless"],
            "executable": executable,
        }
        payload_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return normalized, payload_hash

    def apply(self, body: Any) -> dict[str, Any]:
        """Validate and atomically apply one revisioned settings request."""

        request, payload_hash = self._request(body)
        with self._lock():
            state = self._read()
            for receipt in state["receipts"]:
                if receipt["request_id"] == request["request_id"]:
                    if receipt["payload_hash"] != payload_hash:
                        raise BrowserSettingsConflict(
                            "request_id was already used with a different payload"
                        )
                    return self._projection(state)
            if request["revision"] != state["revision"]:
                raise BrowserSettingsConflict("Browser settings revision is stale")

            state.update(
                enabled=request["enabled"],
                headless=request["headless"],
                executable=request["executable"],
            )
            state["revision"] += 1
            state["receipts"].append(
                {"request_id": request["request_id"], "payload_hash": payload_hash}
            )
            state["receipts"] = state["receipts"][-MAX_RECEIPTS:]
            self._write(state)
            return self._projection(state)
