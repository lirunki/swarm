"""Small entry point that diagnoses an unsupported Python before POSIX imports."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    if sys.platform == "win32":
        raise SystemExit(
            "Swarm needs its bundled Cygwin runtime, not native Windows Python. "
            "From the checkout run `bash ./swarm-windows.sh` in Git Bash, "
            "or run swarm-windows.cmd. Prerequisites are installed automatically."
        )
    from .cli import main as run

    run(argv)
