"""Check the Cygwin/native-Node terminal boundary before building Python dependencies.

Usage: Cygwin python3.12 probe.py /cygdrive/c/path/to/node.exe
No credentials or provider calls; uses and cleans up its own tmux server.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def main():
    if sys.platform != "cygwin":
        raise SystemExit("This check requires the installed Cygwin Python")
    node = sys.argv[1]
    tmux = ["tmux", "-L", f"swarm-probe-{uuid4().hex}", "-f", "/dev/null"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tty.json"
        native = subprocess.check_output(["cygpath", "-am", str(path)], text=True).strip()
        js = (
            'require("fs").writeFileSync('
            + json.dumps(native)
            + ",JSON.stringify({stdin:!!process.stdin.isTTY,stdout:!!process.stdout.isTTY,"
            + "stderr:!!process.stderr.isTTY}));setTimeout(()=>{},60000)"
        )
        try:
            subprocess.run(
                [*tmux, "new-session", "-d", "-s", "probe", shlex.join([node, "-e", js])],
                check=True,
                timeout=20,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if path.exists():
                    result = json.loads(path.read_text())
                    print("Native Node under Cygwin tmux:", result, flush=True)
                    if not all(result.get(key) for key in ("stdin", "stdout", "stderr")):
                        raise RuntimeError(
                            "Native Node does not see a terminal; Pi TUI cannot work"
                        )
                    return
                time.sleep(0.1)
            subprocess.run([*tmux, "capture-pane", "-p", "-t", "probe"], timeout=10)
            raise RuntimeError("Native Node terminal probe timed out")
        finally:
            subprocess.run([*tmux, "kill-server"], check=False, timeout=10)


if __name__ == "__main__":
    main()
