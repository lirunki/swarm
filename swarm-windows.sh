#!/usr/bin/env bash
# One command from stock Git Bash: install missing prerequisites, then run Swarm.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *) printf '%s\n' 'Use this launcher from Windows Git Bash. On Linux/macOS use swarm serve.' >&2; exit 2 ;;
esac
if ! command -v cygpath >/dev/null 2>&1; then
    printf '%s\n' 'Git Bash cygpath is required to locate the Windows installation.' >&2
    exit 2
fi
root_win="${SWARM_WINDOWS_INSTALL_DIR:-${LOCALAPPDATA:?LOCALAPPDATA is not set}/Swarm}"
root="$(cygpath -u "$root_win")"
root_win="$(cygpath -w "$root")"
if [ ! -f "$root/ready.json" ] || [ ! -f "$root/swarm" ]; then
    bash "$repo/setup-windows.sh" -InstallDir "$root_win"
fi
if [ "$#" -eq 0 ]; then
    set -- serve --open
fi
exec bash "$root/swarm" "$@"
