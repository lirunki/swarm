"""Run dependency-free frontend policy/renderer tests when Node is available."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for frontend tests")
def test_frontend_card_behavior():
    files = sorted((Path(__file__).parent / "frontend").glob("*.test.cjs"))
    result = subprocess.run(
        [shutil.which("node"), "--test", *map(str, files)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
