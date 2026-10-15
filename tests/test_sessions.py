import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from swarm.models import Status
from swarm.sessions import AdoptRequest, Sessions
from swarm.store import Store


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    root = tmp_path / "pi-sessions"
    root.mkdir()
    folder = tmp_path / "original-project"
    folder.mkdir()
    session_id = str(uuid4())
    path = root / "original.jsonl"
    entries = [
        {"type": "session", "version": 3, "id": session_id, "cwd": str(folder)},
        {"type": "message", "message": {"role": "user", "content": "Investigate flaky tests"}},
        {"type": "session_info", "name": "Flaky test investigator"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return Sessions(Store(tmp_path / "swarm"), [root]), path


def test_adoption_preserves_exact_session_and_does_not_launch(sessions):
    service, path = sessions
    original = path.read_bytes()
    discovered = service.list()["sessions"]
    assert len(discovered) == 1 and discovered[0]["available"]
    agent = service.adopt(AdoptRequest(session_file=str(path)))
    assert agent.name == "Flaky test investigator"
    assert agent.goal == "Investigate flaky tests"
    assert agent.session_file == str(path.resolve())
    assert agent.session_id == discovered[0]["session_id"]
    assert agent.status == Status.idle and not agent.auto_resume
    assert agent.pid is None and agent.managed
    assert path.read_bytes() == original
    assert agent.folder == discovered[0]["folder"]
    assert not service.list()["sessions"][0]["available"]


def test_duplicate_and_running_sessions_are_rejected(sessions, monkeypatch):
    service, path = sessions
    folder = service.metadata(path)["folder"]
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [folder])
    assert not service.list()["sessions"][0]["available"]
    with pytest.raises(ValueError, match="running"):
        service.adopt(AdoptRequest(session_file=str(path)))
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    service.adopt(AdoptRequest(session_file=str(path)))
    with pytest.raises(ValueError, match="Already registered"):
        service.adopt(AdoptRequest(session_file=str(path)))


def test_unknown_live_process_folder_blocks_all_adoption(sessions, monkeypatch):
    service, path = sessions
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [None])
    with pytest.raises(ValueError, match="unknown folder"):
        service.adopt(AdoptRequest(session_file=str(path)))


def test_path_outside_pi_roots_and_missing_cwd_rejected(sessions, tmp_path):
    service, path = sessions
    other = tmp_path / "outside.jsonl"
    other.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="configured pi"):
        service.adopt(AdoptRequest(session_file=str(other)))
    folder = service.metadata(path)["folder"]
    Path(folder).rmdir()
    with pytest.raises(ValueError, match="folder is missing"):
        service.adopt(AdoptRequest(session_file=str(path)))


def test_concurrent_adoption_creates_only_one_record(sessions):
    service, path = sessions

    def attempt(_):
        independent = Sessions(Store(service.store.home), service.roots)
        try:
            return independent.adopt(AdoptRequest(session_file=str(path)))
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(item is not None for item in results) == 1
    assert len(service.store.scan()) == 1


def test_new_user_goal_overrides_first_prompt(sessions):
    service, path = sessions
    agent = service.adopt(
        AdoptRequest(session_file=str(path), name="Research", goal="Finish the report")
    )
    assert agent.goal == "Finish the report" and agent.name == "Research"


def test_wsl_discovers_windows_pi_sessions_and_imports_portable_copy(tmp_path, monkeypatch):
    import swarm.sessions as module

    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    monkeypatch.setattr(module, "is_wsl", lambda: True)
    mount = tmp_path / "mnt"
    monkeypatch.setattr(module, "WSL_MOUNT_ROOT", mount)
    windows_home = mount / "c" / "Users" / Path.home().name
    sessions_root = windows_home / ".pi" / "agent" / "sessions"
    sessions_root.mkdir(parents=True)
    project = windows_home / "work"
    project.mkdir()
    session_id = str(uuid4())
    path = sessions_root / "windows.jsonl"
    original = "\n".join(
        json.dumps(entry)
        for entry in [
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "cwd": fr"C:\\Users\\{Path.home().name}\\work",
            },
            {
                "type": "message",
                "message": {"role": "user", "content": "Resume the Windows session"},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done already."}],
                    "model": "test-model",
                },
            },
            {"type": "session_info", "name": "Windows session"},
        ]
    ) + "\n"
    path.write_text(original)

    service = Sessions(Store(tmp_path / "swarm"))
    assert sessions_root.resolve() in service.roots
    discovered = service.list()["sessions"]
    assert len(discovered) == 1 and discovered[0]["available"]
    assert discovered[0]["folder"] == str(project.resolve())
    assert discovered[0]["portable_copy_required"]

    agent = service.adopt(AdoptRequest(session_file=str(path)))
    assert agent.folder == str(project.resolve()) and agent.name == "Windows session"
    assert agent.import_source and agent.import_source.backend == "pi"
    assert agent.import_source.session_file == str(path.resolve())
    assert Path(agent.session_file).is_file()
    assert Path(agent.session_file).is_relative_to(service.store.sessions_dir)
    assert path.read_text() == original
