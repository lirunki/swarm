"""Small synchronous wrapper around EdgeMCP's existing browser controls.

Browser controls and launching are adapted from EdgeMCP commit
c09dc257886305539ea90bc454ceda63b9b150a6. This module adds only settings,
a stable private CDP port, and route-shaped request validation; it has no MCP,
SSH, custom CDP protocol, or browser-stream implementation.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from . import edgemcp_launcher
from .edgemcp_browser import EdgeBrowser

_MAX_REQUEST = 16 * 1024
_MAX_SCREENSHOT = 8 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BrowserError(RuntimeError):
    """Browser launch or control failed."""


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700)
        created = True
    except FileExistsError:
        created = False
    if path.is_symlink() or not path.is_dir():
        raise BrowserError(f"Unsafe browser directory: {path.name}")
    if os.name != "nt":
        if created:
            path.chmod(0o700)
        info = path.stat()
        if info.st_uid != os.getuid():
            raise BrowserError(f"Unsafe browser directory owner: {path.name}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise BrowserError(f"Unsafe browser directory mode: {path.name}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class RemoteBrowser:
    """Public synchronous API for Swarm's dedicated Edge/Chromium profile."""

    def __init__(
        self,
        home: str | os.PathLike[str],
        settings: Any,
        *,
        launcher: Any = edgemcp_launcher,
        browser_factory: Callable[[int], EdgeBrowser] = EdgeBrowser,
    ):
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / "remote-browser"
        self.profile = self.root / "profile"
        self.state_path = self.root / "state.json"
        self.settings = settings
        self._launcher = launcher
        self._browser_factory = browser_factory
        self._browser: EdgeBrowser | None = None
        self._launch_results: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._last_error: str | None = None

    def _config(self) -> dict[str, Any]:
        source = self.settings.config if hasattr(self.settings, "config") else self.settings
        value = source() if callable(source) else source
        if not isinstance(value, Mapping):
            raise BrowserError("Remote browser configuration is invalid")
        port = value.get("cdp_port", 0)
        if type(port) is not int or port != 0:
            raise BrowserError("cdp_port must be 0")
        return {
            "enabled": value.get("enabled") is True,
            "headless": value.get("headless") is True,
            "executable": value.get("executable", value.get("exe")),
        }

    def _read_state(self) -> dict[str, Any] | None:
        try:
            info = self.state_path.lstat()
            _private_directory(self.root)
            if self.profile.exists() or self.profile.is_symlink():
                _private_directory(self.profile)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BrowserError("Unsafe remote browser state")
            if os.name != "nt" and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise BrowserError("Unsafe remote browser state")
            if info.st_size > 4096:
                raise BrowserError("Invalid remote browser state")
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            port = value.get("port") if isinstance(value, dict) and value.get("version") == 1 else None
            browser_id = value.get("browser_id") if isinstance(value, dict) else None
            if type(port) is not int or not 1 <= port <= 65535:
                raise BrowserError("Invalid remote browser state")
            if browser_id is not None:
                try:
                    canonical_browser_id = str(UUID(browser_id))
                    if browser_id != canonical_browser_id:
                        raise ValueError("non-canonical browser ID")
                    browser_id = canonical_browser_id
                except (ValueError, AttributeError, TypeError) as exc:
                    raise BrowserError("Invalid remote browser state") from exc
            return {"port": port, "browser_id": browser_id}
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise BrowserError("Invalid remote browser state") from exc

    def _read_port(self) -> int | None:
        state = self._read_state()
        return state["port"] if state else None

    def _write_state(self, port: int, browser_id: str | None = None) -> None:
        _private_directory(self.root)
        temporary = self.root / f".state-{uuid4().hex}.tmp"
        value = {"version": 1, "port": port}
        if browser_id is not None:
            value["browser_id"] = str(UUID(browser_id))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, json.dumps(value).encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.state_path)
        if os.name != "nt":
            self.state_path.chmod(0o600)

    def _port(self) -> int:
        state = self._read_state()
        if state is not None:
            return state["port"]
        _private_directory(self.root)
        _private_directory(self.profile)
        port = _free_port()
        self._write_state(port)
        return port

    def discovery(self) -> dict:
        """List local browser choices without creating profiles or launching processes."""
        return self._launcher.discover_browsers()

    def status(self) -> dict:
        """Report state without launching or attaching pychrome."""
        with self._lock:
            config = self._config()
            if not config["enabled"]:
                return {
                    "enabled": False, "running": False, "state": "disabled",
                    "error": self._last_error,
                }
            state = self._read_state()
            if state is None:
                return {"enabled": True, "running": False, "state": "stopped", "error": None}
            try:
                identity = self._launcher.probe_identity(state["port"])
                if identity is not None and (
                    state["browser_id"] is None
                    or identity["browser_id"] != state["browser_id"]
                ):
                    raise RuntimeError(
                        f"CDP port {state['port']} belongs to an unowned browser"
                    )
            except (RuntimeError, ValueError) as exc:
                self._last_error = str(exc)
                return {
                    "enabled": True, "running": False, "state": "error",
                    "error": self._last_error,
                }
            self._last_error = None
            running = identity is not None
            return {
                "enabled": True, "running": running,
                "state": "running" if running else "stopped", "error": None,
                **({"browser": identity["browser"]} if identity else {}),
            }

    def launch(self, request_id: Any | None = None) -> dict:
        """Explicitly launch (or reuse) the dedicated browser."""
        with self._lock:
            config = self._config()
            if not config["enabled"]:
                raise BrowserError("Remote browser is disabled")
            key = str(request_id) if request_id is not None else None
            if key is not None and key in self._launch_results:
                if self.status()["running"]:
                    return dict(self._launch_results[key])
                self._launch_results.pop(key, None)
            state = self._read_state()
            port = state["port"] if state else self._port()
            expected_browser_id = state["browser_id"] if state else None
            _private_directory(self.profile)
            try:
                launched = self._launcher.launch(
                    port, executable=config["executable"], profile=self.profile,
                    headless=config["headless"], expected_browser_id=expected_browser_id,
                )
            except (RuntimeError, ValueError, OSError) as exc:
                self._last_error = str(exc)
                raise BrowserError(str(exc)) from exc
            try:
                returned_browser_id = launched.get("browser_id")
                browser_id = str(UUID(returned_browser_id))
                if returned_browser_id != browser_id:
                    raise ValueError("non-canonical browser ID")
            except (ValueError, AttributeError, TypeError) as exc:
                self._last_error = "Browser launcher returned an invalid browser identity"
                raise BrowserError(self._last_error) from exc
            self._write_state(port, browser_id)
            result = {
                "running": True,
                "started": bool(launched.get("started")),
                "browser": launched.get("browser", ""),
            }
            self._last_error = None
            if key is not None:
                if len(self._launch_results) >= 256:
                    self._launch_results.pop(next(iter(self._launch_results)))
                self._launch_results[key] = dict(result)
            return result

    @staticmethod
    def _payload(value: Any) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Request data must be an object")
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("Request data must be JSON-compatible") from exc
        if len(encoded) > _MAX_REQUEST:
            raise ValueError("Request data must not exceed 16384 bytes")
        return value

    @staticmethod
    def _target(data: dict) -> str:
        target = data.get("target_id")
        if not isinstance(target, str) or not target:
            raise ValueError("target_id must be a non-empty string")
        return target

    @staticmethod
    def _ratio(value: Any, message: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(message)
        return float(value)

    def _controls(self) -> EdgeBrowser:
        status = self.status()
        if not status["enabled"] or not status["running"]:
            raise BrowserError(status.get("error") or "Remote browser is not running")
        if self._browser is None:
            state = self._read_state()
            if state is None or state["browser_id"] is None:
                raise BrowserError("Remote browser is not owned")
            self._browser = self._browser_factory(state["port"])
        return self._browser

    def request(self, action: str, body: Any = None, *, query: Any = None) -> dict | bytes:
        """Run one EdgeMCP Remote Web action and return JSON data or screenshot PNG."""
        with self._lock:
            if not self._config()["enabled"]:
                raise BrowserError("Remote browser is disabled")
            if body is not None and query is not None:
                raise ValueError("Supply body or query, not both")
            data = self._payload(query if query is not None else body)
            controls = self._controls()
            if action == "tabs":
                return controls.tabs()
            if action == "screenshot":
                content = controls.screenshot(self._target(data))
                if not isinstance(content, bytes) or not content.startswith(_PNG_SIGNATURE):
                    raise BrowserError("Browser screenshot was not a PNG")
                if len(content) > _MAX_SCREENSHOT:
                    raise BrowserError("Browser screenshot exceeds the 8 MiB API limit")
                return content
            if action == "new":
                url = data.get("url")
                if not isinstance(url, str) or not url.strip() or len(url) > 4096:
                    raise ValueError("url must contain 1-4096 characters")
                parsed = urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError("url must use http or https")
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError("url must not contain user information")
                return controls.new(url)

            target = self._target(data)
            if action == "select":
                return controls.select(target)
            if action == "close":
                return controls.close_tab(target)
            if action in ("back", "forward", "reload", "copy"):
                return getattr(controls, action)(target)
            if action == "click":
                message = "x_ratio and y_ratio must be numbers from 0 to 1"
                return controls.click(
                    target, self._ratio(data.get("x_ratio"), message),
                    self._ratio(data.get("y_ratio"), message),
                )
            if action == "drag":
                start, end = data.get("start"), data.get("end")
                if not isinstance(start, dict) or not isinstance(end, dict):
                    raise ValueError("start and end must be coordinate objects")
                message = "drag coordinates must be numbers from 0 to 1"
                values = [
                    self._ratio(start.get("x_ratio"), message),
                    self._ratio(start.get("y_ratio"), message),
                    self._ratio(end.get("x_ratio"), message),
                    self._ratio(end.get("y_ratio"), message),
                ]
                return controls.drag(target, *values)
            if action == "text":
                text = data.get("text")
                if not isinstance(text, str) or not text or len(text) > 4096:
                    raise ValueError("text must contain 1-4096 characters")
                return controls.text(target, text)
            if action == "key":
                key, modifiers = data.get("key"), data.get("modifiers", [])
                if not isinstance(key, str) or not key or len(key) > 32:
                    raise ValueError("key must contain 1-32 characters")
                if not isinstance(modifiers, list) or any(
                    modifier not in ("ctrl", "alt", "shift", "meta") for modifier in modifiers
                ):
                    raise ValueError("Invalid modifiers")
                return controls.key(target, key, modifiers)
            raise ValueError("Unsupported browser action")

    def close(self) -> None:
        """Detach pychrome callbacks; intentionally leave the browser running."""
        with self._lock:
            if self._browser is not None:
                try:
                    owned = self.status()["running"]
                except BrowserError:
                    owned = False
                if owned:
                    self._browser.close()
                self._browser = None
