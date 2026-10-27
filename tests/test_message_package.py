"""A normal wheel install must carry the Pi extension, not just Python sources."""

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


@pytest.mark.skipif(
    importlib.util.find_spec("setuptools") is None, reason="Wheel build tooling optional"
)
def test_wheel_includes_mailbox_extension_and_messages_ui(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    for name in ("README.md", "pyproject.toml"):
        shutil.copy2(repo / name, project / name)
    shutil.copytree(
        repo / "src" / "swarm",
        project / "src" / "swarm",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    result = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel;build_wheel('dist')"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    wheel = next((project / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert {
            "swarm/extensions/mailbox.js",
            "swarm/extensions/output-spill.js",
            "swarm/static/agent-messages.js",
            "swarm/static/agent-broadcast.js",
            "swarm/messaging.py",
        }.issubset(archive.namelist())
