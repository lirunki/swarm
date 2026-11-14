"""EdgeMCP's dedicated browser launcher, adapted for explicit settings.

Adapted from EdgeMCP commit c09dc257886305539ea90bc454ceda63b9b150a6.
The browser intentionally outlives this process. Retained launch attempts prevent
retrying a slow launch from creating a second browser.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .browser_settings import BrowserSettings

_LOCK = threading.Lock()
_ATTEMPTS: dict[tuple[int, str], subprocess.Popen] = {}
_READY: set[tuple[int, str]] = set()

_LINUX_BROWSER_COMMANDS = (
    ("Microsoft Edge", (
        "msedge", "microsoft-edge", "microsoft-edge-stable", "msedge-stable", "msedge-beta",
    )),
    ("Google Chrome", ("google-chrome", "google-chrome-stable", "googlechrome")),
    ("Chromium", ("chromium-browser", "chromium")),
)
_LINUX_BROWSER_DIRECTORIES = ("/usr/bin", "/usr/local/bin", "/snap/bin")
_WSL_EDGE_PATHS = (
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
)


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("CDP port must be an integer from 1 to 65535")
    return port


def canonical_cdp_host(host: str = "127.0.0.1") -> str:
    """Return one canonical, explicit CDP IP address (with localhost as an alias)."""
    if host == "localhost":
        return "127.0.0.1"
    if not isinstance(host, str) or not host or "%" in host:
        raise ValueError("CDP host must be an IP address")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("CDP host must be an IP address") from exc
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        raise ValueError("CDP host must be a unicast IP address")
    return str(address)


def _http_authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _allowed_advertised_host(advertised: str | None, requested: str) -> bool:
    if advertised == "localhost":
        return True
    try:
        address = ipaddress.ip_address(advertised or "")
    except ValueError:
        return False
    return str(address) == requested or address.is_loopback


def _lexical_absolute(path: str | os.PathLike[str]) -> Path:
    """Make a path absolute without resolving a browser wrapper symlink."""
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _native_header(path: Path) -> tuple[Path, bytes] | None:
    """Read only the header of a resolved regular file without following its final link."""
    try:
        target = path.resolve(strict=True)
        descriptor = os.open(
            target,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            return target, os.read(descriptor, 128)
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError):
        return None


def _launchable_path(path: str | os.PathLike[str], *, platform: str | None = None) -> Path | None:
    """Validate the ultimate file but retain the supplied wrapper path."""
    platform = platform or sys.platform
    supplied = _lexical_absolute(path)
    if platform != "win32" and supplied.suffix.lower() in {".exe", ".cmd", ".bat"}:
        return None
    native = _native_header(supplied)
    if native is None:
        return None
    target, header = native
    if platform == "win32":
        supported = target.suffix.lower() == ".exe" and header.startswith(b"MZ")
    elif platform == "darwin":
        supported = header[:4] in {
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
        } or BrowserSettings._sane_shebang(header)
    else:
        supported = header.startswith(b"\x7fELF") or BrowserSettings._sane_shebang(header)
    if not supported or (platform != "win32" and not os.access(supplied, os.X_OK)):
        return None
    return supplied


def _rooted(root: Path, absolute: str) -> Path:
    return Path(absolute) if root == Path("/") else root / absolute.lstrip("/")


def _is_wsl(environ: dict[str, str], root: Path) -> bool:
    if environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in _rooted(root, "/proc/sys/kernel/osrelease").read_text(
            encoding="ascii", errors="ignore"
        ).lower()
    except OSError:
        return False


def discover_browsers(
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    search_path: str | None = None,
    root: str | os.PathLike[str] = "/",
) -> dict[str, list[dict[str, object]]]:
    """Discover launchable local browsers without launching or probing any of them."""
    platform = platform or sys.platform
    environ = dict(os.environ if environ is None else environ)
    root_path = Path(root)
    browsers: list[dict[str, object]] = []
    unavailable: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(name: str, candidate: str | os.PathLike[str]) -> None:
        path = _launchable_path(candidate, platform=platform)
        if path is None or str(path) in seen:
            return
        seen.add(str(path))
        browsers.append({"name": name, "executable": str(path), "supported": True})

    if platform == "win32":
        for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            if environ.get(variable):
                add("Microsoft Edge", Path(environ[variable]) / "Microsoft/Edge/Application/msedge.exe")
        for name, command in (("Microsoft Edge", "msedge.exe"), ("Google Chrome", "chrome.exe")):
            found = shutil.which(command, path=search_path if search_path is not None else environ.get("PATH"))
            if found:
                add(name, found)
        return {"browsers": browsers, "unavailable": unavailable}

    if platform == "darwin":
        for name, candidate in (
            ("Microsoft Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("Google Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("Microsoft Edge", Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ):
            add(name, candidate)
        for name, command in (
            ("Microsoft Edge", "microsoft-edge"),
            ("Google Chrome", "google-chrome"),
            ("Chromium", "chromium"),
        ):
            found = shutil.which(command, path=search_path if search_path is not None else environ.get("PATH"))
            if found:
                add(name, found)
        return {"browsers": browsers, "unavailable": unavailable}

    path_value = search_path if search_path is not None else environ.get("PATH")
    for name, commands in _LINUX_BROWSER_COMMANDS:
        for command in commands:
            found = shutil.which(command, path=path_value)
            if found:
                add(name, found)
            for directory in _LINUX_BROWSER_DIRECTORIES:
                add(name, _rooted(root_path, f"{directory}/{command}"))

    if _is_wsl(environ, root_path):
        for windows_path in _WSL_EDGE_PATHS:
            candidate = _rooted(root_path, windows_path)
            try:
                exists = candidate.is_file()
            except OSError:
                exists = False
            if exists:
                unavailable.append({
                    "name": "Windows Microsoft Edge",
                    "executable": str(candidate),
                    "reason": "Use native Windows Swarm or install Edge/Chromium inside WSL.",
                })
                break
    return {"browsers": browsers, "unavailable": unavailable}


def _find_browser(configured: str | None) -> Path:
    if configured:
        path = _launchable_path(configured)
        if path is None:
            supplied = _lexical_absolute(configured)
            if sys.platform != "win32" and supplied.suffix.lower() in {".exe", ".cmd", ".bat"}:
                raise RuntimeError("Run with Windows Python to launch a Windows browser")
            if not supplied.exists():
                raise RuntimeError("Configured browser executable does not exist")
            raise RuntimeError(
                "Configured browser executable must be a native browser executable or "
                "executable shebang wrapper"
            )
        return path

    discovery = discover_browsers()
    if discovery["browsers"]:
        return Path(str(discovery["browsers"][0]["executable"]))
    raise RuntimeError("Edge or Chromium was not found; configure a browser executable")


def probe_identity(
    port: int, timeout: float = 1, host: str = "127.0.0.1"
) -> dict[str, str] | None:
    """Return validated CDP browser metadata, or None when nothing is listening."""
    _validate_port(port)
    host = canonical_cdp_host(host)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        return None
    except OSError as exc:
        raise RuntimeError(f"Cannot check CDP endpoint {host}:{port}: {exc}") from exc

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    endpoint = f"http://{_http_authority(host, port)}/json/version"
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
            or not _allowed_advertised_host(parsed.hostname, host)
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


def stop_browser(port: int, browser_id: str, timeout: float = 5) -> dict:
    """Gracefully close one verified browser-level CDP endpoint."""
    port = _validate_port(port)
    try:
        browser_id = str(UUID(browser_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Expected browser ID must be a UUID") from exc
    if timeout <= 0:
        raise ValueError("Stop timeout must be positive")

    identity = probe_identity(port)
    if identity is None:
        return {"running": False, "stopped": False}
    if identity["browser_id"] != browser_id:
        raise RuntimeError(f"CDP port {port} belongs to a different browser")

    # websocket-client is already a pychrome dependency. Keep it lazy so status and
    # disabled configurations do not import browser-control dependencies.
    import websocket

    endpoint = f"ws://127.0.0.1:{port}/devtools/browser/{browser_id}"
    connection = websocket.create_connection(
        endpoint,
        timeout=timeout,
        suppress_origin=True,
        http_proxy_host=None,
    )
    try:
        connection.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
    finally:
        connection.close()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Browser stop could not be confirmed; retry status before acting")
        identity = probe_identity(port, timeout=min(1, remaining))
        if identity is None:
            return {"running": False, "stopped": True}
        if identity["browser_id"] != browser_id:
            return {
                "running": False,
                "stopped": True,
                "error": "The stopped browser's CDP port now belongs to a different browser",
            }
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


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
