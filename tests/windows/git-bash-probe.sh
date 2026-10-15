#!/usr/bin/env bash
# Invoked by test-runtime.ps1 using real Git for Windows Bash, not WSL/Cygwin.
set -euo pipefail
case "$(uname -s)" in
    MINGW*|MSYS*) ;;
    *) printf 'Expected Git Bash; OSTYPE=%s, MSYSTEM=%s, uname=%s\n' "$OSTYPE" "${MSYSTEM:-unset}" "$(uname -s 2>/dev/null || true)" >&2; exit 2 ;;
esac
cd "$(cygpath -u "$SWARM_TEST_REPO")"
exec bash ./swarm-windows.sh --home "$SWARM_TEST_RELATIVE_HOME" doctor
