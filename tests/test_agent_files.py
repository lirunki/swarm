import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.cli import main
from swarm.models import Agent
from swarm.server import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes\x00\xff"


@pytest.fixture
def library(tmp_path, monkeypatch):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    controller = app.state.controller
    monkeypatch.setattr(controller.processes, "alive", lambda agent: False)
    folder = tmp_path / "workspace"
    folder.mkdir()
    agent = controller.store.create(
        Agent(
            name="Files",
            goal="Publish results",
            folder=str(folder),
            managed=True,
            auto_resume=False,
        )
    )
    return app, agent, folder


def endpoint(agent):
    return f"/api/agents/{agent.id}/files"


def test_registration_only_stores_reference_and_latest_product_is_unchanged(library, monkeypatch):
    app, agent, folder = library
    original = folder / "result.png"
    original.write_bytes(PNG)
    monkeypatch.setattr(
        "swarm.files.open_regular",
        lambda path: pytest.fail("payload must not be opened before click"),
    )
    with TestClient(app) as client:
        product = client.post(
            f"/api/agents/{agent.id}/product", json={"url": "https://example.com/results"}
        ).json()["product"]
        response = client.post(endpoint(agent), json={"path": original.name, "label": "A chart"})
        assert response.status_code == 201, response.text
        entry = response.json()["file"]
        assert entry["filename"] == "result.png" and "path" not in entry
        result = client.get(endpoint(agent))
        assert result.json()["files"][0]["available"]
        state = client.get("/api/state").json()["agents"][0]
        assert state["file_count"] == 1 and state["files_version"] == 1
        assert state["product"] == product
        assert str(original) not in json.dumps(state)
        stored = list(app.state.controller.files.root.iterdir())
        assert len(stored) == 1 and stored[0].suffix == ".json"
        assert json.loads(stored[0].read_text())["files"][0]["path"] == str(original)
        assert b"fake image bytes" not in stored[0].read_bytes()
        if os.name != "nt":
            assert stored[0].stat().st_mode & 0o777 == 0o600
        assert original.read_bytes() == PNG


def test_current_original_is_fetched_and_missing_files_stay_in_library(library):
    app, agent, folder = library
    original = folder / "report.png"
    original.write_bytes(PNG)
    with TestClient(app) as client:
        ref = client.post(endpoint(agent), json={"path": str(original)}).json()["file"]
        content = endpoint(agent) + f"/{ref['id']}/content"
        changed = PNG + b"new data after registration"
        original.write_bytes(changed)
        response = client.get(content)
        assert response.content == changed
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (
            client.get(content + "?download=true")
            .headers["content-disposition"]
            .startswith("attachment;")
        )
        assert client.get(content + "?download=true").content == changed
        assert client.get(endpoint(agent)).json()["files"][0]["size"] == len(changed)
        original.unlink()
        listing = client.get(endpoint(agent)).json()["files"]
        assert len(listing) == 1 and not listing[0]["available"]
        assert client.get(content).status_code == 410
        assert client.get("/api/state").json()["agents"][0]["file_count"] == 1


@pytest.mark.parametrize(
    "name,data,kind,inline",
    [
        ("plot.png", PNG, "image/png", True),
        ("plot.jpg", b"\xff\xd8\xfftest", "image/jpeg", True),
        ("plot.gif", b"GIF89a123", "image/gif", True),
        ("plot.webp", b"RIFF1234WEBP123", "image/webp", True),
        ("report.pdf", b"%PDF-1.4\nreport", "application/pdf", True),
        ("not-an-image.png", b"<script>unsafe</script>", "application/octet-stream", False),
        ("active.svg", b'<svg onload="unsafe()"></svg>', "application/octet-stream", False),
        ("active.html", b"<script>unsafe()</script>", "application/octet-stream", False),
        ("data.bin", b"\x00\xffdata", "application/octet-stream", False),
    ],
)
def test_safe_preview_types_are_detected_only_at_fetch(library, name, data, kind, inline):
    app, agent, folder = library
    source = folder / name
    source.write_bytes(data)
    with TestClient(app) as client:
        ref = client.post(endpoint(agent), json={"path": str(source)}).json()["file"]
        response = client.get(endpoint(agent) + f"/{ref['id']}/content")
        assert response.content == data
        assert response.headers["content-type"] == kind
        assert response.headers["content-disposition"].startswith(
            "inline;" if inline else "attachment;"
        )


def test_duplicate_paths_update_metadata_and_removal_never_deletes_original(library):
    app, agent, folder = library
    source = folder / "référence.txt"
    source.write_text("keep this")
    with TestClient(app) as client:
        first = client.post(endpoint(agent), json={"path": str(source)}).json()["file"]
        second = client.post(
            endpoint(agent), json={"path": source.name, "label": "New label"}
        ).json()["file"]
        assert first["id"] == second["id"] and second["label"] == "New label"
        assert len(client.get(endpoint(agent)).json()["files"]) == 1
        assert client.delete(endpoint(agent) + f"/{first['id']}").status_code == 200
        assert source.read_text() == "keep this"
        assert client.get(endpoint(agent)).json()["files"] == []
        assert client.get(endpoint(agent) + f"/{first['id']}/content").status_code == 404
        assert client.get("/api/state").json()["agents"][0]["file_count"] == 0


def test_original_replacement_symlinks_devices_and_size_caps_are_safe(library, monkeypatch):
    app, agent, folder = library
    source = folder / "result"
    source.write_bytes(b"1234")
    other = folder / "not-the-registered-file"
    other.write_bytes(b"must not be served through replacement")
    with TestClient(app) as client:
        assert client.post(endpoint(agent), json={"path": str(folder)}).status_code == 400
        ref = client.post(endpoint(agent), json={"path": str(source)}).json()["file"]
        path = endpoint(agent) + f"/{ref['id']}/content"
        source.unlink()
        source.symlink_to(other)
        assert not client.get(endpoint(agent)).json()["files"][0]["available"]
        assert client.get(path).status_code == 410
        source.unlink()
        source.write_bytes(b"new regular file")
        assert client.get(path).content == b"new regular file"
        monkeypatch.setattr("swarm.files.MAX_PRODUCT_BYTES", 3)
        assert client.get(path).status_code == 413
        assert client.post(endpoint(agent), json={"path": str(source)}).status_code == 400


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_fifo_is_rejected_without_blocking_and_replacement_is_unavailable(library):
    app, agent, folder = library
    source = folder / "stream"
    source.write_bytes(b"was regular")
    with TestClient(app) as client:
        ref = client.post(endpoint(agent), json={"path": str(source)}).json()["file"]
        source.unlink()
        os.mkfifo(source)
        assert client.post(endpoint(agent), json={"path": str(source)}).status_code == 400
        assert client.get(endpoint(agent) + f"/{ref['id']}/content").status_code == 410


def test_cross_agent_paths_and_cross_origin_access_are_rejected(library):
    app, agent, folder = library
    source = folder / "image.png"
    source.write_bytes(PNG)
    other = app.state.controller.store.create(Agent(name="Other", goal="Other", folder=str(folder)))
    with TestClient(app) as client:
        ref = client.post(endpoint(agent), json={"path": str(source)}).json()["file"]
        assert client.get(endpoint(other) + f"/{ref['id']}/content").status_code == 404
        assert client.get(endpoint(agent) + f"/{uuid4()}/content").status_code == 404
        assert (
            client.get(endpoint(agent), headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        assert client.get(endpoint(agent) + "/../../other/content").status_code in (404, 422)


def test_reference_metadata_survives_restart_but_not_agent_forgetting(library):
    app, agent, folder = library
    source = folder / "saved.pdf"
    source.write_bytes(b"%PDF-1.4\noriginal")
    with TestClient(app) as client:
        ref = client.post(endpoint(agent), json={"path": str(source)}).json()["file"]
    source.write_bytes(b"%PDF-1.4\ncurrent after restart")
    restarted = create_app(
        app.state.controller.store.home, recover=False, allowed_hosts=["testserver"]
    )
    with TestClient(restarted) as client:
        assert client.get("/api/state").json()["agents"][0]["file_count"] == 1
        assert client.get(endpoint(agent) + f"/{ref['id']}/content").content == source.read_bytes()
        assert client.delete(f"/api/agents/{agent.id}").status_code == 200
        assert source.exists()
        assert not restarted.state.controller.files.path(agent.id).exists()
        assert client.get(endpoint(agent)).status_code == 404


def test_caps_and_corrupt_index_do_not_lose_originals_or_other_agent_state(library, monkeypatch):
    app, agent, folder = library
    first, second = folder / "one", folder / "two"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    monkeypatch.setattr("swarm.files.MAX_AGENT_FILES", 1)
    with TestClient(app) as client:
        assert client.post(endpoint(agent), json={"path": str(first)}).status_code == 201
        assert client.post(endpoint(agent), json={"path": str(second)}).status_code == 400
        app.state.controller.files.path(agent.id).write_text("bad metadata")
        app.state.controller.files.cache.clear()
        state = client.get("/api/state").json()
        assert state["agents"][0]["files_error"] and state["errors"]
        assert first.read_bytes() == b"one" and second.read_bytes() == b"two"


def test_file_cli_uses_current_agent_and_does_not_embed_file_bytes(library, monkeypatch, capsys):
    _, agent, folder = library
    calls = []
    monkeypatch.setenv("SWARM_AGENT_ID", str(agent.id))
    monkeypatch.setattr(
        "swarm.cli.AgentClient.add_file",
        lambda self, id, body: calls.append((id, body)) or {"file": body},
    )
    monkeypatch.setattr("swarm.cli.AgentClient.files", lambda self, id: {"files": []})
    monkeypatch.setattr(
        "swarm.cli.AgentClient.remove_file", lambda self, id, file: {"removed": file}
    )
    main(["file", "add", str(folder / "chart.png"), "--label", "Chart"])
    assert calls[-1] == (str(agent.id), {"path": str(folder / "chart.png"), "label": "Chart"})
    assert json.loads(capsys.readouterr().out)["file"]["label"] == "Chart"
    main(["file", "list"])
    assert json.loads(capsys.readouterr().out)["files"] == []
    main(["file", "remove", "id"])
    assert json.loads(capsys.readouterr().out)["removed"] == "id"
    monkeypatch.delenv("SWARM_AGENT_ID")
    with pytest.raises(SystemExit):
        main(["file", "list"])
