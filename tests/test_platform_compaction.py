"""Regression coverage for the combined Windows/WSL and compaction boundaries."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from swarm import meta_compact, paths, sessions
from swarm.store import Store


@pytest.mark.parametrize("native_windows", [False, True])
def test_compaction_environment_isolates_identity_but_keeps_native_transport(
    monkeypatch, native_windows
):
    monkeypatch.setattr(meta_compact, "cygwin", lambda: native_windows)
    for key, value in {
        "SWARM_WINDOWS_NODE": "C:/runtime/node.exe",
        "SWARM_WINDOWS_PI_ENTRY": "C:/runtime/pi/cli.js",
        "SWARM_AGENT_ID": "parent-identity",
        "SWARM_HOME": "parent-registry",
        "SWARM_PYTHON": "parent-python",
        "SWARM_AGENT_FILE": "parent-record.md",
        "PI_SESSION_ID": "parent-session",
        "PI_SESSION_FILE": "parent.jsonl",
        "TEST_API_KEY": "fixture-value",
    }.items():
        monkeypatch.setenv(key, value)
    env = meta_compact.PiSummarizer.environment()
    transport = {"SWARM_WINDOWS_NODE", "SWARM_WINDOWS_PI_ENTRY"} if native_windows else set()
    assert {key for key in env if key.startswith("SWARM_")} == transport
    assert "PI_SESSION_ID" not in env and "PI_SESSION_FILE" not in env
    assert env["TEST_API_KEY"] == "fixture-value"


def test_compaction_worker_passes_native_session_path_without_parent_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(meta_compact, "cygwin", lambda: True)
    monkeypatch.setattr(meta_compact, "agent_path", lambda path: "C:/worker/session.jsonl")
    monkeypatch.setenv("SWARM_WINDOWS_NODE", "C:/runtime/node.exe")
    monkeypatch.setenv("SWARM_WINDOWS_PI_ENTRY", "C:/runtime/pi/cli.js")
    monkeypatch.setenv("SWARM_AGENT_ID", "parent-identity")
    monkeypatch.setenv("PI_SESSION_FILE", "parent.jsonl")
    summary = (
        "Keep the completed work, original goal and decisions; inspect effects before continuing."
    )
    events = [
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": json.dumps({"summary": summary})}],
            },
        },
        {"type": "agent_settled", "aborted": False},
    ]
    captured = []

    def popen(argv, **kwargs):
        captured.append((argv, kwargs))
        assert kwargs["stdin"].read() == b"Complete offline transcript"
        kwargs["stdout"].write(("\n".join(json.dumps(e) for e in events) + "\n").encode())
        return SimpleNamespace(returncode=0, poll=lambda: 0, wait=lambda: 0)

    monkeypatch.setattr(meta_compact.subprocess, "Popen", popen)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Complete offline transcript")
    result = meta_compact.PiSummarizer("pi")(
        prompt, tmp_path / "worker.jsonl", "provider/model", threading.Event()
    )
    assert result == summary
    argv, kwargs = captured[0]
    assert argv[argv.index("--session") + 1] == "C:/worker/session.jsonl"
    assert kwargs["env"]["SWARM_WINDOWS_NODE"] == "C:/runtime/node.exe"
    assert kwargs["env"]["SWARM_WINDOWS_PI_ENTRY"] == "C:/runtime/pi/cli.js"
    assert "SWARM_AGENT_ID" not in kwargs["env"] and "PI_SESSION_FILE" not in kwargs["env"]


def test_cygwin_discovers_and_adopts_native_session_without_wsl_conversion(tmp_path, monkeypatch):
    root = tmp_path / "native sessions"
    root.mkdir()
    folder = tmp_path / "workspace"
    folder.mkdir()
    windows_root = r"C:\pi-config\sessions"
    windows_folder = r"C:\workspace"
    mapped = {windows_root: root, windows_folder: folder}

    def cygpath(value, mode):
        assert mode == "-au"
        return str(mapped[value])

    monkeypatch.setattr(paths, "cygwin", lambda: True)
    monkeypatch.setattr(paths, "_cygpath", cygpath)
    monkeypatch.setattr(sessions, "cygwin", lambda: True)
    monkeypatch.setattr(sessions, "is_wsl", lambda: False)
    monkeypatch.setattr(sessions, "running_pi_folders", lambda: [])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", r"C:\pi-config")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    path = root / "native.jsonl"
    session_id = str(uuid4())
    original = (
        json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": windows_folder})
        + "\n"
    )
    path.write_text(original)
    store = Store(tmp_path / "registry")
    service = sessions.Sessions(store)
    assert root in service.roots
    assert sessions.Sessions(store, [Path(windows_root)]).roots == [root]
    meta = service.metadata(path)
    assert meta["original_folder"] == windows_folder
    assert not meta["portable_copy_required"]
    agent = service.adopt(sessions.AdoptRequest(session_file=str(path)))
    assert agent.folder == str(folder)
    assert agent.session_id == session_id and agent.session_file == str(path)
    assert agent.import_source is None
    assert path.read_text() == original
