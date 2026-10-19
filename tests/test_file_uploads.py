import base64
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.files import FileRequest
from swarm.models import Agent, now
from swarm.processes import Processes
from swarm.server import create_app
from swarm.uploads import CHUNK_BYTES, MAX_CHUNK_BODY


class FakeProcesses:
    def __init__(self):
        self.live = True
        self.pastes = []
        self.draft = "Please read "

    def alive(self, agent):
        return self.live

    def pid(self, agent):
        return None

    def health(self):
        return {"tmux": True, "pi": True}

    def paste_text(self, agent, text):
        self.pastes.append((str(agent.id), text))
        self.draft += text

    def send_prompt(self, *args):
        raise AssertionError("Insert must not submit a prompt")

    def launch(self, *args):
        raise AssertionError("Upload/Insert must never launch an agent")


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    app.state.controller.processes = FakeProcesses()
    app.state.uploads.temp_root = tmp_path / "agent-machine-tmp"
    agent = app.state.controller.store.create(
        Agent(
            name="Reader",
            goal="Read later",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            status="idle",
        )
    )
    return app, agent, f"/api/agents/{agent.id}/files"


def start(client, base, data, filename="résumé notes.txt", upload_id=None):
    id = str(upload_id or uuid4())
    request = {"upload_id": id, "filename": filename, "size": len(data)}
    response = client.post(base + "/uploads", json=request)
    assert response.status_code == 201, response.text
    return id, request, response.json()


def finish(client, base, id, data):
    for offset in range(0, len(data), CHUNK_BYTES):
        block = data[offset : offset + CHUNK_BYTES]
        result = client.post(
            base + f"/uploads/{id}/chunks",
            json={"offset": offset, "data": base64.b64encode(block).decode()},
        )
        assert result.status_code == 200, result.text
        assert result.json()["offset"] == offset + len(block)
    response = client.post(base + f"/uploads/{id}/finish", json={})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("data", [b"", b"\x00\xff\r\n" * 200000])
def test_upload_download_then_explicit_insert(setup, data):
    app, agent, base = setup
    with TestClient(app) as client:
        id, request, started = start(client, base, data)
        assert started["chunk_bytes"] == CHUNK_BYTES
        assert client.get(base).json()["files"] == []
        result = finish(client, base, id, data)
        file_id = result["file"]["id"]
        assert not app.state.controller.processes.pastes  # Upload never inserts.
        info = client.get(base).json()["files"][0]
        assert info["filename"] == request["filename"] and "path" not in info
        download = client.get(base + f"/{file_id}/content?download=true")
        assert download.content == data and download.headers["content-disposition"].startswith(
            "attachment;"
        )
        before = app.state.controller.store.get(agent.id)
        inserted = client.post(base + f"/{file_id}/insert", json={})
        assert inserted.status_code == 200, inserted.text
        inserted = inserted.json()
        path = Path(inserted["path"])
        assert path.is_relative_to(app.state.uploads.temp_root) and path.read_bytes() == data
        assert json.loads(inserted["text"].strip()) == str(path)
        assert not any(c in inserted["text"] for c in "\r\n\x1b")
        assert app.state.controller.processes.draft == "Please read " + inserted["text"]
        after = app.state.controller.store.get(agent.id)
        assert after.status == before.status and after.auto_resume == before.auto_resume
        assert client.post(base + f"/uploads/{id}/finish", json={}).json() == result
        assert len(client.get(base).json()["files"]) == 1
        assert client.delete(base + f"/uploads/{id}").json() == {
            "cancelled": False,
            "complete": True,
        }
        assert path.is_file()
        client.delete(base + f"/{file_id}")
        assert not path.exists()  # Explicit removal reclaims only the owned uploaded copy.


def test_idempotent_begin_chunks_and_offset_guards(setup):
    app, _, base = setup
    with TestClient(app) as client:
        id, request, _ = start(client, base, b"abcdef")
        chunk = {"offset": 0, "data": base64.b64encode(b"abc").decode()}
        assert client.post(base + f"/uploads/{id}/chunks", json=chunk).json() == {"offset": 3}
        assert client.post(base + f"/uploads/{id}/chunks", json=chunk).json() == {"offset": 3}
        assert client.post(base + "/uploads", json=request).json()["offset"] == 3
        assert (
            client.post(base + "/uploads", json=request | {"filename": "other"}).status_code == 409
        )
        assert (
            client.post(base + f"/uploads/{id}/chunks", json=chunk | {"offset": 5}).status_code
            == 409
        )
        assert (
            client.post(
                base + f"/uploads/{id}/chunks", json={"offset": 0, "data": "eHl6"}
            ).status_code
            == 409
        )
        assert client.post(base + f"/uploads/{id}/finish", json={}).status_code == 409
        state = app.state.uploads.read(request_agent(base), id)
        assert app.state.uploads.payload(request_agent(base), state).read_bytes() == b"abc"
        assert client.delete(base + f"/uploads/{id}").json()["cancelled"]
        assert not app.state.uploads.payload(request_agent(base), state).exists()
        assert client.post(base + "/uploads", json=request).status_code == 410


def request_agent(base):
    return base.split("/")[3]


@pytest.mark.parametrize(
    "filename",
    [
        "../secret",
        "/tmp/secret",
        "a/b",
        "a\\b",
        "C:evil",
        "..",
        "CON",
        "NUL.txt",
        "bad\nname",
        "bad\x1bname",
        "trailing.",
    ],
)
def test_upload_rejects_unsafe_filenames(setup, filename):
    app, _, base = setup
    with TestClient(app) as client:
        response = client.post(
            base + "/uploads", json={"upload_id": str(uuid4()), "filename": filename, "size": 1}
        )
        assert response.status_code == 400
        assert not app.state.uploads.temp_root.exists()


def test_chunk_size_encoding_body_and_origin_guards(setup):
    _, _, base = setup
    app = setup[0]
    with TestClient(app) as client:
        id, _, _ = start(client, base, b"one")
        assert (
            client.post(
                base + f"/uploads/{id}/chunks", json={"offset": 0, "data": "%%%"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                base + f"/uploads/{id}/chunks", json={"offset": 0, "data": "YWJjZA=="}
            ).status_code
            == 400
        )
        assert (
            client.post(
                base + f"/uploads/{id}/chunks", content=b"x" * (MAX_CHUNK_BODY + 1)
            ).status_code
            == 413
        )
        assert (
            client.post(
                base + "/uploads",
                json={"upload_id": str(uuid4()), "filename": "x", "size": 128 * 1024 * 1024 + 1},
            ).status_code
            == 400
        )
        assert (
            client.post(
                base + f"/uploads/{id}/finish", json={}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )


def test_cancel_expire_and_cleanup_preserve_complete_uploads(setup):
    app, agent, base = setup
    with TestClient(app) as client:
        complete, _, _ = start(client, base, b"complete")
        result = finish(client, base, complete, b"complete")
        pending, _, _ = start(client, base, b"not uploaded")
        state = app.state.uploads.read(agent.id, pending)
        state.touched_at = (now() - timedelta(hours=2)).isoformat()
        app.state.uploads.state_path(agent.id, pending).write_text(state.model_dump_json())
        app.state.uploads.cleanup()
        assert app.state.uploads.read(agent.id, pending).status == "expired"
        assert not app.state.uploads.payload(agent.id, state).exists()
        assert client.get(base + f"/{result['file']['id']}/content").content == b"complete"


def test_finish_recovers_after_reference_written_before_manifest(setup, monkeypatch):
    app, agent, base = setup
    with TestClient(app, raise_server_exceptions=False) as client:
        id, _, _ = start(client, base, b"")
        save = app.state.uploads.save

        def fail_after_index(id, state):
            if state.status == "complete":
                raise OSError("interrupted manifest write")
            save(id, state)

        monkeypatch.setattr(app.state.uploads, "save", fail_after_index)
        assert client.post(base + f"/uploads/{id}/finish", json={}).status_code == 500
        file_id = client.get(base).json()["files"][0]["id"]
        assert client.delete(base + f"/uploads/{id}").json()["complete"]
        monkeypatch.setattr(app.state.uploads, "save", save)
        restored = client.post(base + f"/uploads/{id}/finish", json={}).json()
        assert restored["file"]["id"] == file_id
        assert len(client.get(base).json()["files"]) == 1


def test_limits_and_missing_reference_do_not_paste(setup, monkeypatch):
    app, agent, base = setup
    with TestClient(app) as client:
        monkeypatch.setattr("swarm.uploads.MAX_ACTIVE_UPLOADS", 1)
        first, _, _ = start(client, base, b"x")
        assert (
            client.post(
                base + "/uploads", json={"upload_id": str(uuid4()), "filename": "x", "size": 1}
            ).status_code
            == 409
        )
        client.delete(base + f"/uploads/{first}")
        monkeypatch.setattr("swarm.uploads.MAX_TEMP_BYTES", 0)
        assert (
            client.post(
                base + "/uploads", json={"upload_id": str(uuid4()), "filename": "x", "size": 1}
            ).status_code
            == 400
        )
        assert client.post(base + f"/{uuid4()}/insert", json={}).status_code == 404
        assert not app.state.controller.processes.pastes


def test_upload_offline_but_insert_requires_live_managed_agent(setup):
    app, agent, base = setup
    app.state.controller.processes.live = False
    with TestClient(app) as client:
        id, _, _ = start(client, base, b"later")
        result = finish(client, base, id, b"later")
        url = base + f"/{result['file']['id']}/insert"
        assert client.post(url, json={}).status_code == 409
        app.state.controller.processes.live = True
        for change in (
            {"managed": False},
            {"meta_compaction": {"status": "running"}},
            {"stop_pending": True},
        ):
            saved = app.state.controller.store.get(agent.id).model_dump()
            app.state.controller.store.update(agent.id, change)
            assert client.post(url, json={}).status_code == 409
            app.state.controller.store.update(agent.id, {key: saved.get(key) for key in change})
        assert not app.state.controller.processes.pastes


def test_existing_file_insert_is_stat_only_and_maps_agent_path(setup, monkeypatch):
    app, agent, base = setup
    path = Path(agent.folder) / "already here.txt"
    path.write_text("original file")
    ref = app.state.controller.files.add(agent.id, FileRequest(path=str(path)))["file"]
    monkeypatch.setattr("swarm.controller.agent_path", lambda p: "C:/agent tmp/already here.txt")
    with TestClient(app) as client:
        response = client.post(base + f"/{ref['id']}/insert", json={})
        assert response.status_code == 200
        assert (
            json.loads(app.state.controller.processes.pastes[0][1])
            == "C:/agent tmp/already here.txt"
        )
        path.unlink()
        assert client.post(base + f"/{ref['id']}/insert", json={}).status_code == 410
        assert len(app.state.controller.processes.pastes) == 1


def test_tmux_reference_paste_never_sends_enter(setup, monkeypatch):
    app, agent, _ = setup
    proc = Processes(app.state.controller.store)
    calls, payloads = [], []
    monkeypatch.setattr(proc, "alive", lambda a: True)

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "load-buffer":
            payloads.append(Path(args[-1]).read_text())
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(proc, "run", run)
    proc.paste_text(agent, ' "/tmp/input file.txt" ')
    assert payloads == [' "/tmp/input file.txt" ']
    assert any(c[0] == "paste-buffer" and "-p" in c for c in calls)
    assert not any(c[0] == "send-keys" for c in calls)
    assert not list(app.state.controller.store.home.glob("prompt-*"))
    proc.send_prompt(agent, "Submit this")
    assert calls[-2][0] == "send-keys" and calls[-2][-1] == "Enter"


def test_completed_upload_removal_reclaims_quota_and_forget_keeps_other_originals(
    setup, monkeypatch
):
    app, agent, base = setup
    app.state.controller.processes.live = False
    original = Path(agent.folder) / "existing.txt"
    original.write_text("keep this original")
    app.state.controller.files.add(agent.id, FileRequest(path=str(original)))
    monkeypatch.setattr("swarm.uploads.MAX_TEMP_BYTES", 4)
    with TestClient(app) as client:
        id, _, _ = start(client, base, b"four")
        result = finish(client, base, id, b"four")
        ref = app.state.controller.files.reference(agent.id, result["file"]["id"])
        assert ref.uploaded
        assert (
            client.post(
                base + "/uploads", json={"upload_id": str(uuid4()), "filename": "x", "size": 1}
            ).status_code
            == 400
        )
        assert client.delete(base + f"/{ref.id}").json()["deleted_upload"]
        assert not Path(ref.path).exists()
        other, _, _ = start(client, base, b"more")
        uploaded = finish(client, base, other, b"more")
        path = app.state.controller.files.reference(agent.id, uploaded["file"]["id"]).path
        assert client.delete(f"/api/agents/{agent.id}").status_code == 200
        assert not Path(path).exists() and original.read_text() == "keep this original"


def test_cancelled_metadata_is_bounded_and_restart_cleans_unfinished_data(setup, monkeypatch):
    app, agent, base = setup
    monkeypatch.setattr("swarm.uploads.MAX_TOMBSTONES", 2)
    with TestClient(app) as client:
        for _ in range(5):
            id, _, _ = start(client, base, b"x")
            client.delete(base + f"/uploads/{id}")
        app.state.uploads.cleanup()
        assert len(list(app.state.uploads.states())) <= 2
        pending, _, _ = start(client, base, b"pending")
        state = app.state.uploads.read(agent.id, pending)
        app.state.uploads.cleanup(interrupted=True)
        assert app.state.uploads.read(agent.id, pending).status == "expired"
        assert not app.state.uploads.payload(agent.id, state).exists()


def test_upload_storage_symlink_and_replacement_never_overwrite_outside_file(setup):
    app, agent, base = setup
    victim = Path(agent.folder) / "victim.txt"
    victim.write_bytes(b"untouched")
    with TestClient(app) as client:
        id, _, _ = start(client, base, b"x")
        state = app.state.uploads.read(agent.id, id)
        path = app.state.uploads.payload(agent.id, state)
        path.unlink()
        path.symlink_to(victim)
        response = client.post(base + f"/uploads/{id}/chunks", json={"offset": 0, "data": "eA=="})
        assert response.status_code == 400 and victim.read_bytes() == b"untouched"


def test_replaced_regular_upload_file_is_not_written(setup):
    import os

    app, agent, base = setup
    victim = Path(agent.folder) / "outside-empty.txt"
    victim.write_bytes(b"")
    with TestClient(app) as client:
        id, _, _ = start(client, base, b"x")
        state = app.state.uploads.read(agent.id, id)
        path = app.state.uploads.payload(agent.id, state)
        path.unlink()
        os.link(victim, path)
        result = client.post(base + f"/uploads/{id}/chunks", json={"offset": 0, "data": "eA=="})
        assert result.status_code == 409
        assert victim.read_bytes() == b""


def test_control_characters_in_registered_parent_path_are_never_pasted(setup):
    app, agent, base = setup
    directory = Path(agent.folder) / "line\nbreak"
    directory.mkdir()
    path = directory / "safe.txt"
    path.write_text("contents")
    ref = app.state.controller.files.add(agent.id, FileRequest(path=str(path)))["file"]
    with TestClient(app) as client:
        assert client.post(base + f"/{ref['id']}/insert", json={}).status_code == 400
        assert not app.state.controller.processes.pastes


def test_cancelled_upload_path_reused_as_original_is_not_deleted_on_forget(setup):
    app, agent, base = setup
    app.state.controller.processes.live = False
    with TestClient(app) as client:
        id, _, _ = start(client, base, b"x")
        state = app.state.uploads.read(agent.id, id)
        path = app.state.uploads.payload(agent.id, state)
        client.delete(base + f"/uploads/{id}")
        path.parent.mkdir(parents=True)
        path.write_text("new independent original")
        ref = app.state.controller.files.add(agent.id, FileRequest(path=str(path)))
        assert not ref["file"]["uploaded"]
        assert client.delete(f"/api/agents/{agent.id}").status_code == 200
        assert path.read_text() == "new independent original"


def test_insert_does_not_stat_original_under_global_controller_lock(setup, monkeypatch):
    app, agent, base = setup
    path = Path(agent.folder) / "slow-mount.txt"
    path.write_text("metadata check")
    ref = app.state.controller.files.add(agent.id, FileRequest(path=str(path)))["file"]
    original = app.state.controller.files.reference

    def lookup(*args):
        assert not app.state.controller.lock._is_owned()
        return original(*args)

    monkeypatch.setattr(app.state.controller.files, "reference", lookup)
    with TestClient(app) as client:
        assert client.post(base + f"/{ref['id']}/insert", json={}).status_code == 200
