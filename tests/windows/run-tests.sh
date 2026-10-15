#!/usr/bin/env bash
# Run through the installed Cygwin, not the runner's Windows Python.
set -euo pipefail
source "$1/env.sh"
cd "$2"
pip_args=()
if [ -n "${3:-}" ]; then
  pip_args+=(--no-index --find-links "$3")
fi
"$SWARM_VENV_PYTHON" -m pip install "${pip_args[@]}" pytest httpx
exec "$SWARM_VENV_PYTHON" -m pytest -q tests
