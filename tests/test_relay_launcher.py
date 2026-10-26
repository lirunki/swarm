"""Relay URL configuration without production credentials, networking, or Swarm startup."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("host", [None, "relay.example.com"])
def test_relay_host_default_and_environment_override(tmp_path, host):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash required")
    repo = tmp_path / "checkout with spaces"
    repo.mkdir()
    launcher = repo / "launch_swarm.sh"
    shutil.copyfile(ROOT / "launch_swarm.sh", launcher)
    # Dummy fixture only; never open or copy any real checkout credentials.
    (repo / "apikey.txt").write_text("non-secret-test-fixture\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    capture = tmp_path / "arguments.json"
    swarm = repo / ".venv/bin/swarm"
    swarm.parent.mkdir(parents=True)
    swarm.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\nfrom pathlib import Path\n"
        'Path(os.environ["TEST_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n'
    )
    swarm.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in {"SWARM_RELAY_HOST", "BASH_ENV", "ENV"}}
    env.update(TEST_CAPTURE=str(capture), TMPDIR=str(runtime))
    if host:
        env["SWARM_RELAY_HOST"] = host
    result = subprocess.run(
        [bash, str(launcher), "--no-recover"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads(capture.read_text())
    expected = host or "achilipuvm.lirunki.com"
    assert args[args.index("--relay-url") + 1] == f"wss://{expected}:9443/v1/connect"
    assert args[-1] == "--no-recover"
    assert list(runtime.iterdir()) == [], "private temporary key copy must be cleaned"
    assert "non-secret-test-fixture" not in result.stdout + result.stderr
