"""Launch Windows-native Pi from the dedicated Cygwin tmux runtime.

The installer puts a `pi` shim on PATH. Using Node's real entry point avoids
npm's cmd.exe/shell quoting layers. Do not translate prompt text or arbitrary
CLI arguments: PiAdapter translates its own path arguments before this boundary.
"""

from __future__ import annotations

import os
import sys

from .paths import agent_path, cygwin, local_path


def launch(argv: list[str]) -> None:
    if not cygwin():
        raise RuntimeError("The Windows Pi shim must run under Swarm's Cygwin Python")
    paths = []
    for key in ("SWARM_WINDOWS_NODE", "SWARM_WINDOWS_PI_ENTRY"):
        value = os.environ.get(key)
        if not value:
            raise ValueError(f"{key} is missing; rerun setup-windows.cmd")
        path = local_path(value)
        if not path.is_file():
            raise ValueError(f"Missing Windows runtime artifact: {path}; rerun setup-windows.cmd")
        paths.append(path)
    node, entry = paths
    env = os.environ.copy()
    # Node uses native filesystem APIs; its configuration paths cannot be /cygdrive/...
    for key in ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"):
        if env.get(key):
            env[key] = agent_path(env[key])
    # Agent tools may use either native read/write or the configured Cygwin bash.
    if env.get("SWARM_AGENT_FILE"):
        env["SWARM_AGENT_FILE"] = agent_path(env["SWARM_AGENT_FILE"])
    if env.get("SWARM_SKILL"):
        env["SWARM_SKILL"] = agent_path(env["SWARM_SKILL"])
    os.execve(str(node), [agent_path(node), agent_path(entry), *argv], env)


def main() -> None:
    try:
        launch(sys.argv[1:])
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"swarm pi: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
