#!/usr/bin/env bash
# Check the installed runtime, not just whether a partial setup left bash.exe.
set -euo pipefail
export PATH="/usr/bin:/bin:$PATH"
command -v git rustc cargo maturin >/dev/null
python3.12 - <<'PY'
import fcntl
import re
import subprocess
import sys
import termios

import Cython
import hatch_fancy_pypi_readme
import hatchling
import maturin
import pip
import setuptools
import wheel

assert sys.platform == 'cygwin'
version = subprocess.check_output(['tmux', '-V'], text=True)
match = re.search(r'(\d+)\.(\d+)', version)
assert match and tuple(map(int, match.groups())) >= (3, 2), 'tmux 3.2+ is required'
PY
