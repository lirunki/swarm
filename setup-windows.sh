#!/usr/bin/env bash
# Bootstrap entry point for Git Bash.
# Invokes the PowerShell installer, forwarding argv verbatim via "$@".
#
# This does NOT use Cygwin; it only launches Windows PowerShell from Git Bash,
# so it works in a stock Git for Windows environment with no extra setup.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$repo/scripts/windows/install.ps1"

if [ ! -f "$script" ]; then
    echo "[swarm] installer not found: $script" >&2
    exit 2
fi

# Prefer pwsh (PowerShell 7+) if present, else Windows PowerShell.
ps_bin=""
for cand in pwsh powershell.exe; do
    if command -v "$cand" >/dev/null 2>&1; then
        ps_bin="$cand"
        break
    fi
done
if [ -z "$ps_bin" ]; then
    echo "[swarm] neither pwsh nor powershell.exe found on PATH" >&2
    exit 3
fi

# Convert the repo script path to a Windows path for PowerShell -File.
# Git Bash provides MSYS-style paths; PowerShell needs a Windows path.
if command -v cygpath >/dev/null 2>&1; then
    win_script="$(cygpath -w "$script")"
else
    # Best-effort: strip a leading /c and convert.
    win_script="$script"
fi

# All installer path options use Windows syntax (e.g. C:/Users/you/Swarm).
# Suppress Git Bash's implicit rewriting at the Windows process boundary.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'
exec "$ps_bin" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$win_script" "$@"
