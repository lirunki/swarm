#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/bin:/bin:$PATH"
export SHELL=/bin/bash
exec python3.12 "$1" "$2"
