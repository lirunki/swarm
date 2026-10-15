#!/usr/bin/env bash
# Windows Test-Path cannot reliably follow a Cygwin venv's POSIX symlinks.
set -euo pipefail
export PATH="/usr/bin:/bin:$PATH"
"$1/bin/python" -c 'import sys, pip, maturin; assert sys.prefix != sys.base_prefix'
