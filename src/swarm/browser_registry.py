"""Durable registry for several isolated local browser instances.

The registry owns lifecycle metadata and idempotency.  Actual launch and CDP
control remain in :mod:`remote_browser`; external browsers are registered only
after their loopback CDP identity has been verified.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
import time
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from . import edgemcp_launcher
from .browser_settings import BrowserSettings, BrowserSettingsError
from .edgemcp_browser import EdgeBrowser
from .remote_browser import BrowserError, RemoteBrowser, _private_directory
from .store import atomic_write

MAX_INSTANCES = 16
MAX_RECEIPTS = 256
MAX_FILE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.01


class BrowserRegistryError(ValueError):
    """A safe-to-display registry request validation error."""


class BrowserRegistryConflict(RuntimeError):
    """A stale revision, reused request ID, or unsafe lifecycle conflict."""


class BrowserRegistryUncertain(RuntimeError):
    """A lifecycle request was sent but its durable outcome is not yet known."""

    def __init__(self, message: str, request_id: str):
        self.request_id = request_id
        super().__init__(f"{message} Request ID: {request_id}")


class BrowserRegistryCorrupt(RuntimeError):
    """The registry cannot safely be read, locked, or replaced."""


class _ExternalRemoteBrowser(RemoteBrowser):
    """Reuse RemoteBrowser's bounded action validation with external controls."""

    def __init__(self, home: Path, port: int, controls: EdgeBrowser):
        super().__init__(home, {"enabled": True, "headless": False, "cdp_port": 0})
        self._external_controls = controls

    def _controls(self) -> EdgeBrowser:
        return self._external_controls

    def close(self) -> None:
        self._external_controls.close()


class BrowserRegistry:
    """Persistent owner registry for managed and explicitly attached browsers."""

    def __init__(
        self,
        home: str | os.PathLike[str],
        settings: Any,
        *,
        launcher: Any = edgemcp_launcher,
        browser_factory: Any = EdgeBrowser,
        legacy_manager: RemoteBrowser | None = None,
    ):
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / "remote-browser"
        self.path = self.root / "registry.json"
        self.lock_path = self.root / "registry.lock"
        self.instances = self.root / "instances"
        self.settings = settings
        self._launcher = launcher
        self._browser_factory = browser_factory
        self._legacy_manager = legacy_manager
        self._controls: dict[str, RemoteBrowser] = {}
        self._manager_locks: dict[str, threading.RLock] = {}
        self._cache_lock = threading.RLock()

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": 1,
            "revision": 0,
            "legacy_imported": False,
            "browsers": [],
            "receipts": [],
        }

    @staticmethod
    def _safe_file(info: os.stat_result, maximum: int | None = None) -> bool:
        safe = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        if os.name != "nt":
            safe = safe and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
        return safe and (maximum is None or info.st_size <= maximum)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor: int | None = None
        locked = False
        try:
            try:
                _private_directory(self.root)
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                if not self._safe_file(os.fstat(descriptor)):
                    raise OSError("unsafe registry lock")
                deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise OSError("registry lock timeout")
                        time.sleep(LOCK_RETRY_SECONDS)
            except (BrowserError, OSError) as exc:
                raise BrowserRegistryCorrupt(
                    "Browser registry is temporarily unavailable"
                ) from exc
            yield
        finally:
            if descriptor is not None:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _canonical_uuid(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise BrowserRegistryError(f"{field} must be a UUID")
        try:
            result = str(UUID(value))
        except (ValueError, AttributeError) as exc:
            raise BrowserRegistryError(f"{field} must be a UUID") from exc
        if value != result:
            raise BrowserRegistryError(f"{field} must be a canonical UUID")
        return result

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 80:
            raise BrowserRegistryError("name must contain 1-80 characters")
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise BrowserRegistryError("name must not contain Unicode control characters")
        return value

    @staticmethod
    def _nonnegative_revision(value: Any) -> int:
        if type(value) is not int or value < 0:
            raise BrowserRegistryError("revision must be a nonnegative integer")
        return value

    @classmethod
    def _request(cls, body: Any, fields: set[str]) -> tuple[dict[str, Any], str]:
        if not isinstance(body, dict):
            raise BrowserRegistryError("Request body must be an object")
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise BrowserRegistryError("Request body must be JSON-compatible") from exc
        if len(encoded) > MAX_REQUEST_BYTES:
            raise BrowserRegistryError("Request body exceeds 16 KiB")
        if set(body) != fields:
            raise BrowserRegistryError("Request contains missing or unexpected fields")
        normalized = dict(body)
        normalized["revision"] = cls._nonnegative_revision(body["revision"])
        normalized["request_id"] = cls._canonical_uuid(body["request_id"], "request_id")
        payload_hash = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return normalized, payload_hash

    @classmethod
    def _validate_record(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError
        kind = value.get("kind")
        common = {"id", "name", "kind", "executable", "headless", "legacy"}
        expected = common if kind == "managed" else common | {"port", "browser_uuid"}
        allowed = {frozenset(expected)}
        if kind == "external":
            allowed.add(frozenset(expected | {"host"}))
        if kind not in {"managed", "external"} or frozenset(value) not in allowed:
            raise ValueError
        browser_id = cls._canonical_uuid(value["id"], "id")
        name = cls._name(value["name"])
        if type(value["headless"]) is not bool or type(value["legacy"]) is not bool:
            raise ValueError
        if kind == "managed":
            executable = value["executable"]
            if executable is not None and not isinstance(executable, str):
                raise ValueError
            return {
                "id": browser_id,
                "name": name,
                "kind": kind,
                "executable": executable,
                "headless": value["headless"],
                "legacy": value["legacy"],
            }
        if value["executable"] is not None or value["headless"] or value["legacy"]:
            raise ValueError
        port = value["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        return {
            "id": browser_id,
            "name": name,
            "kind": kind,
            "executable": None,
            "headless": False,
            "legacy": False,
            "host": edgemcp_launcher.canonical_cdp_host(value.get("host", "127.0.0.1")),
            "port": port,
            "browser_uuid": cls._canonical_uuid(value["browser_uuid"], "browser_uuid"),
        }

    @classmethod
    def _validate_state(cls, value: Any) -> dict[str, Any]:
        try:
            if not isinstance(value, dict) or set(value) != {
                "version", "revision", "legacy_imported", "browsers", "receipts"
            }:
                raise ValueError
            if value["version"] != 1 or type(value["version"]) is not int:
                raise ValueError
            revision = cls._nonnegative_revision(value["revision"])
            if type(value["legacy_imported"]) is not bool:
                raise ValueError
            if not isinstance(value["browsers"], list) or len(value["browsers"]) > MAX_INSTANCES:
                raise ValueError
            browsers = [cls._validate_record(item) for item in value["browsers"]]
            ids = [item["id"] for item in browsers]
            names = [item["name"].casefold() for item in browsers]
            endpoints = [
                (item["host"], item["port"])
                for item in browsers if item["kind"] == "external"
            ]
            if (
                len(ids) != len(set(ids))
                or len(names) != len(set(names))
                or len(endpoints) != len(set(endpoints))
            ):
                raise ValueError
            receipts = value["receipts"]
            if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS:
                raise ValueError
            clean_receipts = []
            seen = set()
            for receipt in receipts:
                if not isinstance(receipt, dict):
                    raise ValueError
                status = receipt.get("status")
                expected = {
                    "request_id", "payload_hash", "status", "action", "browser_id", "revision"
                }
                if status == "failed":
                    expected.add("error")
                if set(receipt) != expected:
                    raise ValueError
                request_id = cls._canonical_uuid(receipt["request_id"], "request_id")
                payload_hash = receipt["payload_hash"]
                error = receipt.get("error")
                if (
                    request_id in seen
                    or not isinstance(payload_hash, str)
                    or len(payload_hash) != 64
                    or any(character not in "0123456789abcdef" for character in payload_hash)
                    or status not in {"pending", "completed", "failed"}
                    or receipt["action"] not in {"launch", "add", "rename", "stop", "remove"}
                    or (status == "failed" and (
                        receipt["action"] != "launch"
                        or not isinstance(error, str)
                        or not 1 <= len(error) <= 500
                        or any(unicodedata.category(character).startswith("C") for character in error)
                    ))
                    or (
                        receipt["revision"] is not None
                        and (type(receipt["revision"]) is not int or receipt["revision"] < 0)
                    )
                    or (status == "pending") != (receipt["revision"] is None)
                ):
                    raise ValueError
                seen.add(request_id)
                clean = {
                    "request_id": request_id,
                    "payload_hash": payload_hash,
                    "status": status,
                    "action": receipt["action"],
                    "browser_id": cls._canonical_uuid(receipt["browser_id"], "browser_id"),
                    "revision": receipt["revision"],
                }
                if status == "failed":
                    clean["error"] = error
                clean_receipts.append(clean)
            managed_ids = {
                record["id"] for record in browsers if record["kind"] == "managed"
            }
            if any(
                receipt["status"] == "pending"
                and (
                    receipt["action"] not in {"launch", "stop"}
                    or receipt["browser_id"] not in managed_ids
                )
                for receipt in clean_receipts
            ):
                raise ValueError
            return {
                "version": 1,
                "revision": revision,
                "legacy_imported": value["legacy_imported"],
                "browsers": browsers,
                "receipts": clean_receipts,
            }
        except (BrowserRegistryError, KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise BrowserRegistryCorrupt("Saved browser registry could not be read") from exc

    def _read(self) -> dict[str, Any]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            before = os.fstat(descriptor)
            if not self._safe_file(before, MAX_FILE_BYTES):
                raise ValueError
            content = os.read(descriptor, MAX_FILE_BYTES + 1)
            after = os.fstat(descriptor)
            stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if len(content) > MAX_FILE_BYTES or any(
                getattr(before, field) != getattr(after, field) for field in stable
            ):
                raise ValueError
            return self._validate_state(json.loads(content.decode("utf-8")))
        except FileNotFoundError:
            return self.empty()
        except (BrowserRegistryCorrupt, json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
            raise BrowserRegistryCorrupt("Saved browser registry could not be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write(self, state: dict[str, Any]) -> None:
        content = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise BrowserRegistryError("Saved browser registry exceeds 1 MiB")
        try:
            _private_directory(self.root)
            atomic_write(self.path, content)
            if os.name != "nt":
                self.path.chmod(0o600)
        except (BrowserError, OSError) as exc:
            raise BrowserRegistryCorrupt("Browser registry could not be saved") from exc

    def _legacy_config(self) -> dict[str, Any]:
        source = self.settings.config if hasattr(self.settings, "config") else self.settings
        value = source() if callable(source) else source
        if not isinstance(value, Mapping):
            raise BrowserRegistryCorrupt("Legacy browser configuration could not be read")
        try:
            executable = BrowserSettings._validate_executable(
                value.get("executable", value.get("exe"))
            )
        except BrowserSettingsError as exc:
            raise BrowserRegistryCorrupt("Legacy browser configuration could not be read") from exc
        if "headless" in value and type(value["headless"]) is not bool:
            raise BrowserRegistryCorrupt("Legacy browser configuration could not be read")
        return {"enabled": True, "headless": value.get("headless") is True,
                "executable": executable, "cdp_port": 0}

    def _legacy_record(self) -> dict[str, Any] | None:
        state_path = self.root / "state.json"
        if not state_path.exists() and not state_path.is_symlink():
            return None
        config = self._legacy_config()
        remote = self._legacy_manager or RemoteBrowser(
            self.home, config, launcher=self._launcher, browser_factory=self._browser_factory
        )
        try:
            state = remote._read_state()
        except BrowserError as exc:
            raise BrowserRegistryCorrupt("Legacy browser state could not be read") from exc
        if state is None or state["browser_id"] is None:
            raise BrowserRegistryCorrupt("Legacy browser state has no bound browser identity")
        identifier = str(uuid5(NAMESPACE_URL, f"swarm-browser-legacy:{self.root}"))
        return {
            "id": identifier,
            "name": "Existing Swarm browser",
            "kind": "managed",
            "executable": config["executable"],
            "headless": config["headless"],
            "legacy": True,
        }

    def _with_legacy(self, state: dict[str, Any], *, persist: bool) -> dict[str, Any]:
        if state["legacy_imported"]:
            return state
        legacy = self._legacy_record()
        if legacy is not None:
            if len(state["browsers"]) >= MAX_INSTANCES:
                raise BrowserRegistryConflict("Browser instance limit of 16 has been reached")
            if any(item["name"].casefold() == legacy["name"].casefold()
                   for item in state["browsers"]):
                raise BrowserRegistryConflict("Legacy browser name conflicts with the registry")
            state["browsers"].insert(0, legacy)
        if persist:
            state["legacy_imported"] = True
        return state

    def _manager(self, record: dict[str, Any]) -> RemoteBrowser:
        browser_id = record["id"]
        with self._cache_lock:
            manager = self._controls.get(browser_id)
            if manager is not None:
                return manager
            if record["kind"] == "managed":
                if record["legacy"] and self._legacy_manager is not None:
                    manager = self._legacy_manager
                else:
                    manager = RemoteBrowser(
                        self.home if record["legacy"] else self.instances / UUID(browser_id).hex,
                        {
                            "enabled": True,
                            "headless": record["headless"],
                            "executable": record["executable"],
                            "cdp_port": 0,
                        },
                        launcher=self._launcher,
                        browser_factory=self._browser_factory,
                    )
            else:
                host = record["host"]
                if host == "127.0.0.1":
                    controls = self._browser_factory(record["port"])
                else:
                    controls = self._browser_factory(record["port"], host=host)
                manager = _ExternalRemoteBrowser(self.home, record["port"], controls)
            self._controls[browser_id] = manager
            if record["legacy"] and self._legacy_manager is manager:
                self._manager_locks.setdefault(browser_id, manager._lock)
            else:
                self._manager_locks.setdefault(browser_id, threading.RLock())
            return manager

    def _manager_lock(self, browser_id: str) -> threading.RLock:
        with self._cache_lock:
            return self._manager_locks.setdefault(browser_id, threading.RLock())

    def _detach(self, browser_id: str) -> None:
        with self._cache_lock:
            manager = self._controls.pop(browser_id, None)
            manager_lock = self._manager_locks.setdefault(browser_id, threading.RLock())
        if manager is not None:
            with manager_lock:
                manager.close()

    @staticmethod
    def _find(state: dict[str, Any], browser_id: str) -> dict[str, Any]:
        for record in state["browsers"]:
            if record["id"] == browser_id:
                return record
        raise BrowserRegistryError("Browser instance was not found")

    @staticmethod
    def _pending_for(state: dict[str, Any], browser_id: str) -> bool:
        return any(
            receipt["browser_id"] == browser_id and receipt["status"] == "pending"
            for receipt in state["receipts"]
        )

    def _external_status(self, record: dict[str, Any]) -> dict[str, Any]:
        try:
            probe_options = {"timeout": 1}
            if record["host"] != "127.0.0.1":
                probe_options["host"] = record["host"]
            identity = self._launcher.probe_identity(record["port"], **probe_options)
            if identity is None:
                return {"running": False, "state": "stopped", "error": None}
            if identity["browser_id"] != record["browser_uuid"]:
                raise RuntimeError(f"CDP port {record['port']} belongs to a different browser")
            return {"running": True, "state": "running", "error": None}
        except (RuntimeError, ValueError, OSError) as exc:
            return {"running": False, "state": "error", "error": str(exc)}

    def _managed_status(self, record: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
        manager = self._manager(record)
        with self._manager_lock(record["id"]):
            status = manager.status()
            saved = manager._read_state()
        return status, saved["port"] if saved else None

    def _project_record(self, record: dict[str, Any]) -> dict[str, Any]:
        port = record.get("port")
        if record["kind"] == "managed":
            self._manager(record)
        manager_lock = self._manager_lock(record["id"])
        if not manager_lock.acquire(blocking=False):
            status = {
                "running": False,
                "state": "busy",
                "error": "Browser operation is in progress",
            }
        else:
            try:
                if record["kind"] == "external":
                    status = self._external_status(record)
                else:
                    status, port = self._managed_status(record)
            except (BrowserError, RuntimeError, ValueError, OSError) as exc:
                status = {"running": False, "state": "error", "error": str(exc)}
                port = None
            finally:
                manager_lock.release()
        projected = {
            "id": record["id"], "name": record["name"], "kind": record["kind"],
            "host": record.get("host", "127.0.0.1"),
            "port": port, "running": bool(status["running"]), "state": status["state"],
            "executable": record["executable"], "headless": record["headless"],
            "can_stop": record["kind"] == "managed",
        }
        if status.get("error"):
            projected["error"] = status["error"]
        return projected

    def _projection(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "revision": state["revision"],
            "browsers": [self._project_record(record) for record in state["browsers"]],
        }

    def _snapshot(self, *, persist_legacy: bool = False) -> dict[str, Any]:
        with self._lock():
            return self._with_legacy(self._read(), persist=persist_legacy)

    def list(self) -> dict[str, Any]:
        """Return a best-effort status snapshot without holding the registry file lock."""
        if not self.root.exists() and not self.root.is_symlink():
            return {"revision": 0, "browsers": []}
        if not self.path.exists() and not self.path.is_symlink():
            try:
                _private_directory(self.root)
            except BrowserError as exc:
                raise BrowserRegistryCorrupt("Browser registry is temporarily unavailable") from exc
            return self._projection(self._with_legacy(self.empty(), persist=False))

        state = self._snapshot()
        # One recovery pass only: status/probe I/O is outside the file lock and
        # each finalizer rechecks the exact receipt and record before writing.
        for receipt in [item for item in state["receipts"] if item["status"] == "pending"]:
            try:
                record = self._find(state, receipt["browser_id"])
                self._manager(record)
            except (BrowserRegistryError, BrowserRegistryCorrupt):
                continue
            manager_lock = self._manager_lock(receipt["browser_id"])
            if not manager_lock.acquire(blocking=False):
                continue
            try:
                if self._pending_complete(record, receipt["action"]):
                    self._finalize_pending(receipt, record)
            except (BrowserError, BrowserRegistryError, BrowserRegistryConflict,
                    BrowserRegistryCorrupt, RuntimeError, ValueError, OSError):
                continue
            finally:
                manager_lock.release()
        return self._projection(self._snapshot())

    def legacy_mutation_guard(self) -> None:
        """Reject singleton mutations after durable registry ownership begins."""
        if not self.path.exists() and not self.path.is_symlink():
            return
        state = self._snapshot()
        if state["legacy_imported"] or state["browsers"]:
            raise BrowserRegistryConflict(
                "Use the browser registry and refresh browser instances before changing this browser"
            )

    def _existing_receipt(
        self, state: dict[str, Any], request: dict[str, Any], payload_hash: str, action: str
    ) -> dict[str, Any] | None:
        for receipt in state["receipts"]:
            if receipt["request_id"] != request["request_id"]:
                continue
            if receipt["payload_hash"] != payload_hash or receipt["action"] != action:
                raise BrowserRegistryConflict("request_id was already used with a different payload")
            return receipt
        return None

    def _append_receipt(self, state: dict[str, Any], receipt: dict[str, Any]) -> None:
        if len(state["receipts"]) >= MAX_RECEIPTS:
            for index, previous in enumerate(state["receipts"]):
                if previous["status"] != "pending":
                    state["receipts"].pop(index)
                    break
            else:
                raise BrowserRegistryConflict("Too many browser operations have uncertain outcomes")
        state["receipts"].append(receipt)

    def _operation_result(self, state: dict[str, Any], browser_id: str) -> dict[str, Any]:
        return {**self._projection(state), "operation": {"browser_id": browser_id}}

    def _begin(
        self, body: Any, fields: set[str], action: str
    ) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any] | None]:
        request, payload_hash = self._request(body, fields)
        state = self._with_legacy(self._read(), persist=True)
        receipt = self._existing_receipt(state, request, payload_hash, action)
        if receipt is None and request["revision"] != state["revision"]:
            raise BrowserRegistryConflict("Browser registry revision is stale")
        return state, request, payload_hash, receipt

    def _completed_receipt(
        self, request: dict[str, Any], payload_hash: str, action: str, browser_id: str,
        revision: int,
    ) -> dict[str, Any]:
        return {
            "request_id": request["request_id"], "payload_hash": payload_hash,
            "status": "completed", "action": action, "browser_id": browser_id,
            "revision": revision,
        }

    def _pending_complete(self, record: dict[str, Any], action: str) -> bool:
        if record["kind"] != "managed":
            return False
        status, _port = self._managed_status(record)
        return (action == "launch" and status["running"]) or (
            action == "stop" and not status["running"] and status["state"] == "stopped"
        )

    def _finalize_pending(
        self, expected_receipt: dict[str, Any], expected_record: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock():
            state = self._with_legacy(self._read(), persist=True)
            receipt = next(
                (item for item in state["receipts"]
                 if item["request_id"] == expected_receipt["request_id"]),
                None,
            )
            if receipt is None or any(
                receipt[key] != expected_receipt[key]
                for key in ("payload_hash", "action", "browser_id")
            ):
                raise BrowserRegistryConflict("Browser operation receipt changed before completion")
            record = self._find(state, receipt["browser_id"])
            if record != expected_record:
                raise BrowserRegistryConflict("Browser instance changed before operation completion")
            if receipt["status"] == "pending":
                state["revision"] += 1
                receipt["status"] = "completed"
                receipt["revision"] = state["revision"]
                self._write(state)
            return state

    @staticmethod
    def _uncertain(request_id: str) -> BrowserRegistryUncertain:
        return BrowserRegistryUncertain(
            "Browser operation outcome is uncertain; check status before retrying.", request_id
        )

    @staticmethod
    def _definite_failure(receipt: dict[str, Any]) -> BrowserRegistryConflict:
        return BrowserRegistryConflict(
            f"{receipt['error']} Request ID: {receipt['request_id']}"
        )

    def _fail_pending_launch(
        self, expected_receipt: dict[str, Any], expected_record: dict[str, Any], error: str
    ) -> dict[str, Any]:
        safe_error = " ".join(error.split())[:500]
        if not safe_error:
            safe_error = "Browser could not be started and is not running"
        with self._lock():
            state = self._with_legacy(self._read(), persist=True)
            receipt = next(
                (item for item in state["receipts"]
                 if item["request_id"] == expected_receipt["request_id"]),
                None,
            )
            if receipt is None or any(
                receipt.get(key) != expected_receipt.get(key)
                for key in ("payload_hash", "status", "action", "browser_id", "revision")
            ):
                raise BrowserRegistryConflict("Browser operation receipt changed before failure")
            record = self._find(state, receipt["browser_id"])
            if record != expected_record or receipt["action"] != "launch":
                raise BrowserRegistryConflict("Browser instance changed before launch failure")
            state["browsers"] = [
                item for item in state["browsers"] if item["id"] != record["id"]
            ]
            receipt["status"] = "failed"
            receipt["revision"] = state["revision"]
            receipt["error"] = safe_error
            self._write(state)
        self._detach(record["id"])
        return receipt

    def _replay_or_recover(
        self, state: dict[str, Any], receipt: dict[str, Any]
    ) -> dict[str, Any]:
        if receipt["status"] == "failed":
            raise self._definite_failure(receipt)
        if receipt["status"] == "pending":
            record = self._find(state, receipt["browser_id"])
            try:
                complete = self._pending_complete(record, receipt["action"])
            except (BrowserError, RuntimeError, ValueError, OSError):
                complete = False
            if not complete:
                raise self._uncertain(receipt["request_id"])
            state = self._finalize_pending(receipt, record)
        return self._operation_result(state, receipt["browser_id"])

    def launch(self, body: Any) -> dict[str, Any]:
        fields = {"revision", "request_id", "name", "executable", "headless"}
        request, payload_hash = self._request(body, fields)
        request["name"] = self._name(request["name"])
        if type(request["headless"]) is not bool:
            raise BrowserRegistryError("headless must be a boolean")
        try:
            executable = BrowserSettings._validate_executable(request["executable"])
        except BrowserSettingsError as exc:
            raise BrowserRegistryError(str(exc)) from exc

        with self._lock():
            state = self._with_legacy(self._read(), persist=True)
            receipt = self._existing_receipt(state, request, payload_hash, "launch")
            if receipt is not None:
                replay = (state, receipt)
                record = None
            else:
                if request["revision"] != state["revision"]:
                    raise BrowserRegistryConflict("Browser registry revision is stale")
                if len(state["browsers"]) >= MAX_INSTANCES:
                    raise BrowserRegistryConflict("Browser instance limit of 16 has been reached")
                if any(item["name"].casefold() == request["name"].casefold()
                       for item in state["browsers"]):
                    raise BrowserRegistryConflict("Browser instance name is already in use")
                browser_id = str(uuid5(UUID(request["request_id"]), "browser-instance"))
                if any(item["id"] == browser_id for item in state["browsers"]):
                    raise BrowserRegistryConflict("Browser instance ID is already in use")
                record = {
                    "id": browser_id, "name": request["name"], "kind": "managed",
                    "executable": executable, "headless": request["headless"], "legacy": False,
                }
                state["browsers"].append(record)
                receipt = {
                    "request_id": request["request_id"], "payload_hash": payload_hash,
                    "status": "pending", "action": "launch", "browser_id": browser_id,
                    "revision": None,
                }
                self._append_receipt(state, receipt)
                self._write(state)
                replay = None
        if replay is not None:
            return self._replay_or_recover(*replay)
        assert record is not None
        try:
            manager = self._manager(record)
            with self._manager_lock(record["id"]):
                manager.launch(request["request_id"])
        except (BrowserError, RuntimeError, ValueError, OSError) as exc:
            if getattr(exc, "definite", False):
                failed = self._fail_pending_launch(receipt, record, str(exc))
                raise self._definite_failure(failed) from exc
            raise self._uncertain(request["request_id"]) from exc
        return self._operation_result(self._finalize_pending(receipt, record), record["id"])

    def add_existing(self, body: Any) -> dict[str, Any]:
        old_fields = {"revision", "request_id", "name", "port"}
        fields = old_fields | {"host"} if isinstance(body, dict) and "host" in body else old_fields
        request, payload_hash = self._request(body, fields)
        name = self._name(request["name"])
        port = request["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise BrowserRegistryError("port must be an integer from 1 to 65535")
        try:
            host = edgemcp_launcher.canonical_cdp_host(request.get("host", "127.0.0.1"))
        except ValueError as exc:
            raise BrowserRegistryError(str(exc)) from exc
        with self._lock():
            state, request, payload_hash, receipt = self._begin(body, fields, "add")
            if receipt is not None:
                replay = (state, receipt["browser_id"])
            else:
                replay = None
                if len(state["browsers"]) >= MAX_INSTANCES:
                    raise BrowserRegistryConflict("Browser instance limit of 16 has been reached")
                if any(item["name"].casefold() == name.casefold() for item in state["browsers"]):
                    raise BrowserRegistryConflict("Browser instance name is already in use")
                if any(
                    item["kind"] == "external"
                    and item["host"] == host and item["port"] == port
                    for item in state["browsers"]
                ):
                    raise BrowserRegistryConflict("Browser CDP endpoint is already registered")
        if replay is not None:
            return self._operation_result(*replay)
        try:
            probe_options = {"timeout": 1}
            if host != "127.0.0.1":
                probe_options["host"] = host
            identity = self._launcher.probe_identity(port, **probe_options)
        except (RuntimeError, ValueError, OSError) as exc:
            raise BrowserRegistryError(str(exc)) from exc
        if identity is None:
            raise BrowserRegistryError("No browser CDP endpoint is running at that host and port")
        browser_uuid = self._canonical_uuid(identity.get("browser_id"), "browser_uuid")
        browser_id = str(uuid5(UUID(request["request_id"]), "browser-instance"))
        with self._lock():
            state = self._with_legacy(self._read(), persist=True)
            receipt = self._existing_receipt(state, request, payload_hash, "add")
            if receipt is not None:
                browser_id = receipt["browser_id"]
            else:
                if request["revision"] != state["revision"]:
                    raise BrowserRegistryConflict("Browser registry revision is stale")
                if len(state["browsers"]) >= MAX_INSTANCES:
                    raise BrowserRegistryConflict("Browser instance limit of 16 has been reached")
                if any(item["name"].casefold() == name.casefold() for item in state["browsers"]):
                    raise BrowserRegistryConflict("Browser instance name is already in use")
                if any(
                    item["kind"] == "external"
                    and item["host"] == host and item["port"] == port
                    for item in state["browsers"]
                ):
                    raise BrowserRegistryConflict("Browser CDP endpoint is already registered")
                record = {
                    "id": browser_id, "name": name, "kind": "external",
                    "host": host, "port": port,
                    "browser_uuid": browser_uuid, "executable": None, "headless": False,
                    "legacy": False,
                }
                state["browsers"].append(record)
                state["revision"] += 1
                self._append_receipt(
                    state, self._completed_receipt(request, payload_hash, "add", browser_id,
                                                   state["revision"]),
                )
                self._write(state)
        return self._operation_result(state, browser_id)

    def rename(self, body: Any) -> dict[str, Any]:
        fields = {"revision", "request_id", "id", "name"}
        with self._lock():
            state, request, payload_hash, receipt = self._begin(body, fields, "rename")
            if receipt is not None:
                browser_id = receipt["browser_id"]
            else:
                browser_id = self._canonical_uuid(request["id"], "id")
                record = self._find(state, browser_id)
                if self._pending_for(state, browser_id):
                    raise BrowserRegistryConflict("Browser has an uncertain lifecycle operation")
                name = self._name(request["name"])
                if any(item["id"] != browser_id and item["name"].casefold() == name.casefold()
                       for item in state["browsers"]):
                    raise BrowserRegistryConflict("Browser instance name is already in use")
                record["name"] = name
                state["revision"] += 1
                self._append_receipt(
                    state, self._completed_receipt(request, payload_hash, "rename", browser_id,
                                                   state["revision"]),
                )
                self._write(state)
        return self._operation_result(state, browser_id)

    def stop(self, body: Any) -> dict[str, Any]:
        fields = {"revision", "request_id", "id"}
        with self._lock():
            state, request, payload_hash, receipt = self._begin(body, fields, "stop")
            if receipt is not None:
                replay = (state, receipt)
                record = None
            else:
                browser_id = self._canonical_uuid(request["id"], "id")
                record = self._find(state, browser_id)
                if record["kind"] != "managed":
                    raise BrowserRegistryConflict("External browser instances cannot be stopped")
                if self._pending_for(state, browser_id):
                    raise BrowserRegistryConflict("Browser has an uncertain lifecycle operation")
                receipt = {
                    "request_id": request["request_id"], "payload_hash": payload_hash,
                    "status": "pending", "action": "stop", "browser_id": browser_id,
                    "revision": None,
                }
                self._append_receipt(state, receipt)
                self._write(state)
                replay = None
        if replay is not None:
            return self._replay_or_recover(*replay)
        assert record is not None
        try:
            manager = self._manager(record)
            with self._manager_lock(record["id"]):
                manager.stop(request["request_id"])
        except (BrowserError, RuntimeError, ValueError, OSError) as exc:
            raise self._uncertain(request["request_id"]) from exc
        return self._operation_result(self._finalize_pending(receipt, record), record["id"])

    def remove(self, body: Any) -> dict[str, Any]:
        fields = {"revision", "request_id", "id"}
        with self._lock():
            state, request, payload_hash, receipt = self._begin(body, fields, "remove")
            if receipt is not None:
                replay = (state, receipt["browser_id"])
                record = None
            else:
                replay = None
                browser_id = self._canonical_uuid(request["id"], "id")
                record = dict(self._find(state, browser_id))
                if self._pending_for(state, browser_id):
                    raise BrowserRegistryConflict("Browser has an uncertain lifecycle operation")
        if replay is not None:
            return self._operation_result(*replay)
        assert record is not None
        if record["kind"] == "managed":
            status, _port = self._managed_status(record)
            if status["running"]:
                raise BrowserRegistryConflict("Stop the managed browser before removing it")
        with self._lock():
            state = self._with_legacy(self._read(), persist=True)
            receipt = self._existing_receipt(state, request, payload_hash, "remove")
            if receipt is not None:
                removed_now = False
                browser_id = receipt["browser_id"]
            else:
                if request["revision"] != state["revision"]:
                    raise BrowserRegistryConflict("Browser registry revision is stale")
                if self._find(state, browser_id) != record:
                    raise BrowserRegistryConflict("Browser instance changed before removal")
                if self._pending_for(state, browser_id):
                    raise BrowserRegistryConflict("Browser has an uncertain lifecycle operation")
                state["browsers"] = [item for item in state["browsers"] if item["id"] != browser_id]
                state["revision"] += 1
                self._append_receipt(
                    state, self._completed_receipt(request, payload_hash, "remove", browser_id,
                                                   state["revision"]),
                )
                self._write(state)
                removed_now = True
        if removed_now:
            self._detach(browser_id)
        return self._operation_result(state, browser_id)

    def request(
        self, browser_id: Any, action: str, body: Any = None, query: Any = None
    ) -> dict | bytes:
        """Dispatch a verified existing CDP action for one registered instance."""
        browser_id = self._canonical_uuid(browser_id, "id")
        if action in {"launch", "stop", "remove", "rename", "add", "add_existing"}:
            raise BrowserRegistryError("Lifecycle actions must use the registry API")
        state = self._snapshot()
        record = self._find(state, browser_id)
        manager = self._manager(record)
        with self._manager_lock(browser_id):
            if record["kind"] == "external":
                status = self._external_status(record)
                if not status["running"]:
                    raise BrowserError(status.get("error") or "External browser is not running")
            return manager.request(action, body, query=query)

    def close(self) -> None:
        """Detach all cached CDP controls without stopping any browser."""
        with self._cache_lock:
            cached = list(self._controls.items())
            self._controls.clear()
        records: dict[str, dict[str, Any]] = {}
        try:
            if self.path.exists() or self.path.is_symlink():
                state = self._snapshot()
            elif self.root.exists() and not self.root.is_symlink():
                state = self._with_legacy(self.empty(), persist=False)
            else:
                state = self.empty()
            records = {record["id"]: record for record in state["browsers"]}
        except BrowserRegistryCorrupt:
            records = {}
        for browser_id, manager in cached:
            record = records.get(browser_id)
            with self._manager_lock(browser_id):
                if (record is not None and record["kind"] == "external"
                        and not self._external_status(record)["running"]):
                    continue
                manager.close()
