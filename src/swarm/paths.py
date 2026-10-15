"""Path boundary between Swarm's POSIX runtime and Windows-native agent tools."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath


def cygwin() -> bool:
    return sys.platform == "cygwin"


def _cygpath(value: str, mode: str) -> str:
    try:
        result = subprocess.run(
            ["cygpath", mode, "--", value],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            "Cannot translate Windows path; the Swarm Cygwin runtime is incomplete"
        ) from exc
    translated = result.stdout.strip()
    if not translated:
        raise ValueError("cygpath returned an empty path")
    return translated


def local_path(value: str | Path) -> Path:
    """Accept native paths; map drive paths only in a compatible runtime.

    Never interpret C:\\project as a relative POSIX filename. Swarm records keep
    local POSIX paths; Pi on Windows receives native paths at the launch boundary.
    """
    raw = str(value)
    if raw.upper().startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]
    windows = PureWindowsPath(raw)
    if windows.drive:
        if not windows.is_absolute():
            raise ValueError("Windows path must be absolute")
        if os.name == "nt":
            return Path(raw).expanduser().resolve()
        if cygwin():
            if windows.drive.startswith("\\\\"):
                raise ValueError("UNC network paths are not supported; use a local drive folder")
            return Path(_cygpath(raw, "-au")).resolve()
        if sys.platform == "linux" and re.fullmatch(r"[A-Za-z]:", windows.drive):
            mount = Path("/mnt") / windows.drive[0].lower()
            if mount.is_dir():
                return mount.joinpath(*windows.parts[1:]).resolve()
        raise ValueError(
            "Windows path cannot be mapped here; provide a local working-folder override"
        )
    # The setup launcher accepts folders pasted from Git Bash as well.
    if cygwin() and re.match(r"^/[A-Za-z]/", raw):
        raw = _cygpath(raw[1].upper() + ":/" + raw[3:], "-au")
    return Path(raw).expanduser().resolve()


def agent_path(value: str | Path) -> str:
    """A path understood by native Node/Pi (unchanged on Linux and macOS)."""
    if cygwin():
        return _cygpath(str(local_path(value)), "-am")
    return str(value)
