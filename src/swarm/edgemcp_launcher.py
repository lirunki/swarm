"""EdgeMCP's dedicated browser launcher, adapted for explicit settings.

Adapted from EdgeMCP commit c09dc257886305539ea90bc454ceda63b9b150a6.
The browser intentionally outlives this process. Retained launch attempts prevent
retrying a slow launch from creating a second browser.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

_LOCK = threading.Lock()
_ATTEMPTS: dict[tuple[int, str], subprocess.Popen] = {}
_READY: set[tuple[int, str]] = set()


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("CDP port must be an integer from 1 to 65535")
    return port


def _find_browser(configured: str | None) -> Path:
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuntimeError("Configured browser executable does not exist")
        if sys.platform != "win32" and path.suffix.lower() == ".exe":
            raise RuntimeError("Run with Windows Python to launch a Windows browser")
        if sys.platform != "win32" and not os.access(path, os.X_OK):
            raise RuntimeError("Configured browser executable is not executable")
        return path.resolve()

    candidates: list[Path] = []
    if sys.platform == "win32":
        for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            if os.environ.get(name):
                candidates.append(Path(os.environ[name]) / "Microsoft/Edge/Application/msedge.exe")
        commands = ("msedge.exe", "chrome.exe")
    elif sys.platform == "darwin":
        candidates = [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
        commands = ("microsoft-edge", "google-chrome", "chromium")
    else:
        commands = (
            "microsoft-edge", "microsoft-edge-stable", "google-chrome",
            "google-chrome-stable", "chromium", "chromium-browser",
        )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    for command in commands:
        found = shutil.which(command)
        if found:
            return Path(found).resolve()
    raise RuntimeError("Edge or Chromium was not found; configure a browser executable")


def probe_identity(port: int, timeout: float = 1) -> dict[str, str] | None:
    """Return validated CDP browser metadata, or None when nothing is listening."""
    _validate_port(port)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        return None
    except OSError as exc:
        raise RuntimeError(f"Cannot check local CDP port {port}: {exc}") from exc

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    endpoint = f"http://127.0.0.1:{port}/json/version"
    try:
        with opener.open(endpoint, timeout=timeout) as response:
            if response.geturl() != endpoint:
                raise ValueError("redirected response")
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError("oversized response")
            metadata = json.loads(data)
            version = metadata.get("Browser", "")
            websocket = metadata.get("webSocketDebuggerUrl", "")
        parsed = urlsplit(websocket)
        path_prefix = "/devtools/browser/"
        browser_id = parsed.path.removeprefix(path_prefix)
        if (
            parsed.scheme != "ws"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != port
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith(path_prefix)
            or "/" in browser_id
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid browser websocket URL")
        canonical_browser_id = str(UUID(browser_id))
        if browser_id != canonical_browser_id:
            raise ValueError("non-canonical browser ID")
        browser_id = canonical_browser_id
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise RuntimeError(f"Port {port} is occupied but is not a ready browser CDP endpoint") from exc
    prefixes = ("Edg/", "Microsoft Edge/", "Chrome/", "Chromium/", "HeadlessChrome/")
    if not isinstance(version, str) or not version.startswith(prefixes):
        raise RuntimeError(f"Port {port} is occupied by a non-browser service")
    return {"browser": version, "browser_id": browser_id}


def probe(port: int, timeout: float = 1) -> str | None:
    """Return the browser version, preserving EdgeMCP's original probe API."""
    identity = probe_identity(port, timeout)
    return identity["browser"] if identity else None


def launch(
    port: int,
    *,
    executable: str | None,
    profile: str | os.PathLike[str],
    headless: bool = False,
    timeout: float = 15,
    expected_browser_id: str | None = None,
) -> dict:
    """Ensure a loopback browser CDP listener exists on an explicit port."""
    port = _validate_port(port)
    if timeout <= 0:
        raise ValueError("Launch timeout must be positive")
    profile_path = Path(profile).expanduser().resolve()
    key = (port, str(profile_path))
    if expected_browser_id is not None:
        try:
            expected_browser_id = str(UUID(expected_browser_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Expected browser ID must be a UUID") from exc

    with _LOCK:
        try:
            identity = probe_identity(port)
        except RuntimeError:
            if key not in _ATTEMPTS or key in _READY:
                raise
            identity = None
        if identity:
            if expected_browser_id is not None:
                if (
                    identity["browser_id"] != expected_browser_id
                    and (key not in _ATTEMPTS or key in _READY)
                ):
                    raise RuntimeError(f"CDP port {port} belongs to a different browser")
            elif key not in _ATTEMPTS:
                raise RuntimeError(f"CDP port {port} is already occupied by an unowned browser")
            if key in _ATTEMPTS:
                _READY.add(key)
            return {
                "started": False, "already_running": True,
                "browser": identity["browser"], "browser_id": identity["browser_id"],
            }
        if key in _READY:
            _READY.discard(key)
            previous = _ATTEMPTS.pop(key, None)
            if previous:
                previous.poll()

        process = _ATTEMPTS.get(key)
        started = process is None
        if process is None:
            browser = _find_browser(executable)
            profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                profile_path.chmod(0o700)
            command = [
                str(browser), f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile_path}",
                "--no-first-run", "--no-default-browser-check",
            ]
            if headless:
                command.append("--headless=new")
            command.append("about:blank")
            options = dict(
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False,
            )
            if sys.platform == "win32":
                options["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                options["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **options)
            except OSError as exc:
                raise RuntimeError(f"Could not start browser: {exc}") from exc
            _ATTEMPTS[key] = process

        deadline = time.monotonic() + timeout
        last_error = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                identity = probe_identity(port, timeout=min(1, remaining / 2))
            except RuntimeError as exc:
                last_error = str(exc)
                identity = None
            if identity:
                _READY.add(key)
                return {
                    "started": started, "already_running": False,
                    "browser": identity["browser"], "browser_id": identity["browser_id"],
                    "launch_pid": process.pid,
                }
            code = process.poll()
            if code not in (None, 0):
                _ATTEMPTS.pop(key, None)
                raise RuntimeError(
                    f"Browser exited with code {code} before CDP became ready on port {port}"
                )
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        detail = f" Last probe: {last_error}." if last_error else ""
        raise RuntimeError(
            f"Browser CDP did not become ready on loopback port {port} within {timeout:g}s. "
            f"Launch PID: {process.pid}. The launch may still be running; retries in this "
            f"process will wait for it rather than spawning another instance.{detail}"
        )
