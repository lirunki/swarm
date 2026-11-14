"""Launch Windows Microsoft Edge from WSL through fixed PowerShell bridges."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SENTINEL = "wsl:msedge"
MAX_OUTPUT_BYTES = 16 * 1024
POWERSHELL_TIMEOUT = 10
INSPECT_CACHE_SECONDS = 1.0


class WslEdgeLaunchError(RuntimeError):
    """A sanitized WSL launch failure with an explicit side-effect classification."""

    def __init__(
        self, message: str, *, started: bool, definite: bool, code: str
    ) -> None:
        self.started = started
        self.definite = definite
        self.code = code
        super().__init__(message)


# Request-controlled values are supplied only through a tiny validated environment
# contract. The program emits one JSON object on every handled path and never emits
# PowerShell exception text (which can contain user names or command lines).
_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$prelaunch = $true
try {
    $port = 0
    if (-not [int]::TryParse($env:SWARM_EDGE_CDP_PORT, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw 'Invalid CDP port'
    }
    $tag = $env:SWARM_EDGE_PROFILE_TAG
    if ($tag -notmatch '^[0-9a-f]{16}$') { throw 'Invalid profile tag' }
    $headless = $env:SWARM_EDGE_HEADLESS
    if ($headless -ne '0' -and $headless -ne '1') { throw 'Invalid headless setting' }

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    try { $listener.Start() } catch { throw 'The requested Windows loopback port is occupied' }
    finally { try { $listener.Stop() } catch {} }

    $candidates = @(
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe' }),
        $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe' }),
        $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe' })
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
    $edge = $candidates | Select-Object -First 1
    if (-not $edge) { throw 'Microsoft Edge was not found on Windows' }
    if (-not $env:LOCALAPPDATA) { throw 'Windows LOCALAPPDATA is unavailable' }
    $profile = Join-Path $env:LOCALAPPDATA ("Swarm\Edge-CDP-{0}-{1}" -f $port, $tag)
    $arguments = @(
        "--remote-debugging-port=$port",
        '--remote-debugging-address=127.0.0.1',
        ('--user-data-dir="' + $profile + '"'),
        '--no-first-run',
        '--no-default-browser-check'
    )
    if ($headless -eq '1') { $arguments += '--headless=new' }
    $arguments += 'about:blank'

    # Nothing after this assignment can prove that Start-Process had no effect.
    $prelaunch = $false
    $process = Start-Process -FilePath $edge -ArgumentList $arguments -PassThru
    $birth = $process.StartTime.ToUniversalTime().Ticks
    @{ stage = 'launched'; pid = $process.Id; birth = $birth; profile = $profile;
       executable = $edge; port = $port } | ConvertTo-Json -Compress
    exit 0
} catch {
    if ($prelaunch) {
        $allowed = @(
            'Invalid CDP port', 'Invalid profile tag', 'Invalid headless setting',
            'The requested Windows loopback port is occupied',
            'Microsoft Edge was not found on Windows', 'Windows LOCALAPPDATA is unavailable'
        )
        $message = if ($allowed -contains $_.Exception.Message) {
            $_.Exception.Message
        } else {
            'Windows Edge preflight failed'
        }
        @{ stage = 'preflight'; error = $message } | ConvertTo-Json -Compress
    } else {
        @{ stage = 'unknown'; error = 'Windows Edge launch could not be confirmed' } |
            ConvertTo-Json -Compress
    }
    exit 1
}
""".strip()


# This bridge proves both process identity and listener ownership. A listener is
# owned only when its actual Win32 process is an approved msedge.exe and its own
# command line contains this launch's exact dedicated --user-data-dir argument.
_INSPECT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $port = 0
    $pidValue = 0
    $birthValue = [long]0
    if (-not [int]::TryParse($env:SWARM_EDGE_CDP_PORT, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { throw 'invalid' }
    if (-not [int]::TryParse($env:SWARM_EDGE_PID, [ref]$pidValue) -or $pidValue -lt 0) { throw 'invalid' }
    if (-not [long]::TryParse($env:SWARM_EDGE_BIRTH, [ref]$birthValue) -or $birthValue -lt 0) { throw 'invalid' }
    $tag = $env:SWARM_EDGE_PROFILE_TAG
    if ($tag -notmatch '^[0-9a-f]{16}$' -or -not $env:LOCALAPPDATA) { throw 'invalid' }
    $profile = Join-Path $env:LOCALAPPDATA ("Swarm\Edge-CDP-{0}-{1}" -f $port, $tag)
    $approved = @(
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe' }),
        $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe' }),
        $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe' })
    ) | Where-Object { $_ }

    function Test-OwnedEdge([int]$processId) {
        $item = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $processId) -ErrorAction Stop
        if (-not $item -or -not $item.ExecutablePath -or -not $item.CommandLine) { return $false }
        if ([IO.Path]::GetFileName($item.ExecutablePath) -ine 'msedge.exe') { return $false }
        $pathOK = $false
        foreach ($candidate in $approved) {
            if ($item.ExecutablePath -ieq $candidate) { $pathOK = $true; break }
        }
        if (-not $pathOK) { return $false }
        $signature = Get-AuthenticodeSignature -LiteralPath $item.ExecutablePath -ErrorAction Stop
        if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch '(^|, )O=Microsoft Corporation(,|$)') {
            return $false
        }
        $quoted = '--user-data-dir="' + $profile + '"'
        $plain = '--user-data-dir=' + $profile
        return $item.CommandLine.Contains($quoted) -or $item.CommandLine.Contains($plain)
    }

    $launchAlive = $false
    if ($pidValue -gt 0 -and $birthValue -gt 0) {
        try {
            $candidate = Get-Process -Id $pidValue -ErrorAction Stop
            $ticks = $candidate.StartTime.ToUniversalTime().Ticks
            $launchAlive = ($ticks -eq $birthValue) -and (Test-OwnedEdge $pidValue)
        } catch { $launchAlive = $false }
    }

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop)
    foreach ($listener in $listeners) {
        $ownerPid = [int]$listener.OwningProcess
        if (Test-OwnedEdge $ownerPid) {
            $ownerBirth = (Get-Process -Id $ownerPid -ErrorAction Stop).StartTime.ToUniversalTime().Ticks
            @{ state = 'owned'; pid = $ownerPid; birth = $ownerBirth } | ConvertTo-Json -Compress
            exit 0
        }
    }
    if ($listeners.Count -gt 0) {
        @{ state = 'foreign'; pid = [int]$listeners[0].OwningProcess; birth = $null } |
            ConvertTo-Json -Compress
    } elseif ($launchAlive) {
        @{ state = 'unknown'; pid = $pidValue; birth = $birthValue } | ConvertTo-Json -Compress
    } else {
        @{ state = 'gone'; pid = $null; birth = $null } | ConvertTo-Json -Compress
    }
} catch {
    @{ state = 'unknown'; pid = $null; birth = $null } | ConvertTo-Json -Compress
}
""".strip()


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def encoded_script() -> str:
    """Return the launch bridge's UTF-16LE ``-EncodedCommand`` payload."""
    return _encoded(_POWERSHELL_SCRIPT)


def powershell_executable() -> str | None:
    """Find a Windows PowerShell executable reachable through WSL interop."""
    found = shutil.which("powershell.exe")
    if found:
        return found
    fixed = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return str(fixed) if fixed.is_file() else None


def _command(powershell: str, script: str) -> list[str]:
    return [
        powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", _encoded(script),
    ]


def _bounded_json(completed: Any) -> Any | None:
    stdout = completed.stdout
    stderr = completed.stderr
    if len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES:
        return None
    try:
        raw = stdout.decode("utf-8-sig") if isinstance(stdout, bytes) else stdout.lstrip("\ufeff")
        return json.loads(raw)
    except (AttributeError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        return None


def _inspect(port: int, tag: str, pid: int = 0, birth: int = 0) -> dict[str, Any]:
    powershell = powershell_executable()
    if powershell is None:
        return {"state": "unknown", "pid": None, "birth": None}
    env = os.environ.copy()
    env.update({
        "SWARM_EDGE_CDP_PORT": str(port),
        "SWARM_EDGE_PROFILE_TAG": tag,
        "SWARM_EDGE_PID": str(pid),
        "SWARM_EDGE_BIRTH": str(birth),
    })
    try:
        completed = subprocess.run(
            _command(powershell, _INSPECT_SCRIPT),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            timeout=POWERSHELL_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "unknown", "pid": None, "birth": None}
    value = _bounded_json(completed)
    if not isinstance(value, dict) or set(value) != {"state", "pid", "birth"}:
        return {"state": "unknown", "pid": None, "birth": None}
    if value["state"] not in {"owned", "foreign", "gone", "unknown"}:
        return {"state": "unknown", "pid": None, "birth": None}
    if value["pid"] is not None and (type(value["pid"]) is not int or value["pid"] <= 0):
        return {"state": "unknown", "pid": None, "birth": None}
    if value["birth"] is not None and (type(value["birth"]) is not int or value["birth"] <= 0):
        return {"state": "unknown", "pid": None, "birth": None}
    return value


@dataclass
class WindowsEdgeHandle:
    """Popen-compatible, non-killing Windows Edge identity proof."""

    pid: int
    birth: int
    profile: str
    executable: str
    port: int
    tag: str
    _checked_at: float = field(default=0.0, init=False, repr=False, compare=False)
    _last_inspection: dict[str, Any] | None = field(default=None, init=False, repr=False, compare=False)
    _inspect_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def _identity(self, *, force: bool = False) -> dict[str, Any]:
        with self._inspect_lock:
            now = time.monotonic()
            if (
                force
                or self._last_inspection is None
                or now - self._checked_at >= INSPECT_CACHE_SECONDS
            ):
                self._last_inspection = _inspect(self.port, self.tag, self.pid, self.birth)
                self._checked_at = now
            return dict(self._last_inspection)

    def poll(self) -> int | None:
        """Return 0 only when both the exact process and owned listener are gone."""
        return 0 if self._identity()["state"] == "gone" else None

    def owns_port(self) -> bool | None:
        """Prove this profile owns the Windows listener; never infer ownership."""
        state = self._identity(force=True)["state"]
        if state == "owned":
            return True
        if state == "foreign":
            return False
        return None


_LOCK = threading.Lock()
WSL_ATTEMPTS: dict[tuple[int, str], WindowsEdgeHandle] = {}


def _profile_key(profile: str | os.PathLike[str]) -> str:
    return str(Path(profile).expanduser().resolve())


def forget(port: int, profile: str | os.PathLike[str]) -> None:
    """Forget a previously ready instance after CDP confirms it has stopped."""
    with _LOCK:
        WSL_ATTEMPTS.pop((port, _profile_key(profile)), None)


def _profile_tag(profile: str | os.PathLike[str]) -> str:
    return hashlib.sha256(_profile_key(profile).encode("utf-8")).hexdigest()[:16]


def owns_port(port: int, profile: str | os.PathLike[str]) -> bool | None:
    """Inspect an existing listener using the deterministic dedicated profile tag."""
    state = _inspect(port, _profile_tag(profile))["state"]
    return True if state == "owned" else False if state == "foreign" else None


def launch(
    port: int,
    profile: str | os.PathLike[str],
    *,
    executable: str = SENTINEL,
    headless: bool = False,
) -> WindowsEdgeHandle:
    """Start dedicated Windows Edge and return its detached-process identity."""
    if executable != SENTINEL:
        raise ValueError("WSL Edge launcher requires the wsl:msedge executable")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("CDP port must be an integer from 1 to 65535")
    if type(headless) is not bool:
        raise ValueError("headless must be a boolean")

    tag = _profile_tag(profile)
    key = (port, _profile_key(profile))
    with _LOCK:
        previous = WSL_ATTEMPTS.get(key)
        if previous is not None:
            return previous
        powershell = powershell_executable()
        if powershell is None:
            raise WslEdgeLaunchError(
                "Windows PowerShell interop is unavailable in WSL",
                started=False, definite=True, code="interop_unavailable",
            )
        env = os.environ.copy()
        env.update({
            "SWARM_EDGE_CDP_PORT": str(port),
            "SWARM_EDGE_PROFILE_TAG": tag,
            "SWARM_EDGE_HEADLESS": "1" if headless else "0",
        })
        try:
            completed = subprocess.run(
                _command(powershell, _POWERSHELL_SCRIPT),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                timeout=POWERSHELL_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise WslEdgeLaunchError(
                "Windows Edge launch bridge timed out",
                started=True, definite=False, code="bridge_timeout",
            ) from exc
        except OSError as exc:
            raise WslEdgeLaunchError(
                "Could not run the Windows Edge launch bridge",
                started=False, definite=True, code="bridge_unavailable",
            ) from exc

        result: Any = _bounded_json(completed)
        if completed.returncode != 0:
            if (
                isinstance(result, dict)
                and set(result) == {"stage", "error"}
                and result["stage"] == "preflight"
                and isinstance(result["error"], str)
                and 1 <= len(result["error"]) <= 200
                and all(ord(character) >= 32 for character in result["error"])
            ):
                raise WslEdgeLaunchError(
                    result["error"], started=False, definite=True, code="preflight_failed"
                )
            raise WslEdgeLaunchError(
                "Windows Edge launch bridge failed after launch may have started",
                started=True, definite=False, code="bridge_failed",
            )
        try:
            if not isinstance(result, dict) or set(result) != {
                "stage", "pid", "birth", "profile", "executable", "port"
            } or result["stage"] != "launched":
                raise ValueError
            pid = result["pid"]
            birth = result["birth"]
            windows_profile = result["profile"]
            edge = result["executable"]
            returned_port = result["port"]
            if type(pid) is not int or pid <= 0 or type(birth) is not int or birth <= 0:
                raise ValueError
            if returned_port != port:
                raise ValueError
            if not isinstance(windows_profile, str) or not windows_profile or len(windows_profile) > 4096:
                raise ValueError
            if not isinstance(edge, str) or not edge.lower().endswith("msedge.exe") or len(edge) > 4096:
                raise ValueError
            if any(ord(character) < 32 for character in windows_profile + edge):
                raise ValueError
        except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
            raise WslEdgeLaunchError(
                "Windows Edge launch bridge returned an invalid response after launch may have started",
                started=True, definite=False, code="invalid_response",
            ) from exc
        handle = WindowsEdgeHandle(pid, birth, windows_profile, edge, port, tag)
        WSL_ATTEMPTS[key] = handle
        return handle
