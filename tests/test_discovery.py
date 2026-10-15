import json
from pathlib import Path

import pytest

from swarm.discovery import browse_folders, discover_models, sibling_worktree_path


def test_discover_models_uses_pi_catalog_and_marks_configured_default(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "settings.json").write_text(
        json.dumps({"defaultProvider": "example", "defaultModel": "model-b"})
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    pi = tmp_path / "pi"
    pi.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'provider model context max-out thinking images' "
        "'example model-b 200K 64K yes no' 'other model-a 1M 128K no yes'\n"
    )
    pi.chmod(0o700)

    result = discover_models(str(pi))

    assert result["default"] == "example/model-b"
    assert [model["id"] for model in result["models"]] == [
        "example/model-b",
        "other/model-a",
    ]
    assert result["models"][0] | {"id": "example/model-b"} == {
        "id": "example/model-b",
        "provider": "example",
        "model": "model-b",
        "context": "200K",
        "thinking": "yes",
        "images": "no",
    }
    assert result["error"] is None


def test_discover_models_fails_closed_to_pi_default(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "settings.json").write_text(json.dumps({"defaultModel": "fallback"}))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    result = discover_models(str(tmp_path / "missing-pi"))

    assert result["models"] == []
    assert result["default"] == "fallback"
    assert "Cannot list Pi models" in result["error"]


def test_folder_browser_lists_only_directories_and_reports_git(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "ordinary").mkdir()
    (tmp_path / "file.txt").write_text("not a directory")
    import subprocess

    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    root = browse_folders(str(tmp_path))
    selected = browse_folders(str(repo))

    assert [entry["name"] for entry in root["directories"]] == ["ordinary", "repo"]
    assert root["path"] == str(tmp_path.resolve())
    assert root["parent"] == str(tmp_path.parent.resolve())
    assert not root["git"]
    assert selected["git"]


def test_folder_browser_rejects_files(tmp_path):
    path = tmp_path / "file"
    path.write_text("x")
    with pytest.raises(ValueError, match="existing directory"):
        browse_folders(str(path))


def test_sibling_worktree_path_sanitizes_branch_separators(tmp_path):
    source = tmp_path / "project"
    assert sibling_worktree_path(source, "feature/nested\\task") == Path(
        str(source) + "_feature_nested_task"
    )
