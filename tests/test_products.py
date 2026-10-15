import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.cli import main
from swarm.models import Agent
from swarm.server import create_app


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    app = create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    ctl = app.state.controller
    monkeypatch.setattr(ctl.processes, "alive", lambda a: False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = ctl.store.create(
        Agent(
            name="Producer",
            goal="Make a report",
            folder=str(workspace),
            managed=True,
            auto_resume=False,
        )
    )
    return app, agent, workspace


def download_url(agent, product):
    return f"/api/agents/{agent.id}/products/{product['id']}/download"


def test_last_product_replaces_previous_and_reports_do_not_clear_it(fixture):
    app, agent, workspace = fixture
    with TestClient(app) as client:
        endpoint = f"/api/agents/{agent.id}/product"
        first = client.post(
            endpoint, json={"url": "https://github.com/example/repo/pull/12", "label": "PR #12"}
        )
        assert first.status_code == 200, first.text
        second = client.post(
            endpoint, json={"url": "https://ci.example/build/42", "label": "Build 42"}
        ).json()["product"]
        assert first.json()["product"]["id"] != second["id"]
        app.state.controller.store.update(agent.id, {"status": "done", "notes": "Finished"})
        state = client.get("/api/state").json()["agents"][0]
        assert state["product"] == second and state["goal"] == agent.goal
        assert state["product"]["label"] == "Build 42"
        assert client.delete(endpoint).json()["product"] is None
        assert client.get("/api/state").json()["agents"][0]["product"] is None


def test_snapshot_download_keeps_bytes_and_old_links_stop_working(fixture):
    app, agent, workspace = fixture
    source = workspace / "résultat.html"
    original = b"<script>alert('download only')</script>\x00\xff\r\n"
    source.write_bytes(original)
    with TestClient(app) as client:
        endpoint = f"/api/agents/{agent.id}/product"
        result = client.post(endpoint, json={"path": source.name, "label": "Download result"})
        assert result.status_code == 200, result.text
        product = result.json()["product"]
        assert product["kind"] == "file" and product["size"] == len(original)
        assert "path" not in product
        source.write_bytes(b"original was later changed")
        url = download_url(agent, product)
        response = client.get(url)
        assert response.content == original
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert "r%C3%A9sultat.html" in response.headers["content-disposition"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-length"] == str(len(original))
        assert client.get(url, headers={"Origin": "https://evil.example"}).status_code == 403
        snapshot = app.state.products.directory(agent.id, product["id"])
        assert snapshot.is_dir()
        new = client.post(endpoint, json={"url": "https://example.com/final"}).json()["product"]
        assert new["kind"] == "link"
        assert client.get(url).status_code == 404 and not snapshot.exists()
        assert source.read_bytes() == b"original was later changed"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": "https://example.com", "path": "file.txt"},
        {"url": "javascript:alert(1)"},
        {"url": "data:text/html,test"},
        {"url": "file:///etc/passwd"},
        {"url": "//example.com"},
        {"url": "https://user:secret@example.com"},
        {"url": "https://@example.com"},
        {"url": "https://example.com\n/evil"},
        {"url": "https://example.com\\evil"},
        {"url": "http://[broken"},
        {"url": "http://host:broken"},
        {"url": "https://example.com", "unexpected": "field"},
    ],
)
def test_invalid_product_keeps_previous_advertisement(fixture, body):
    app, agent, _ = fixture
    with TestClient(app) as client:
        endpoint = f"/api/agents/{agent.id}/product"
        previous = client.post(endpoint, json={"url": "https://example.com/good"}).json()["product"]
        response = client.post(endpoint, json=body)
        assert response.status_code in (400, 422), response.text
        assert client.get("/api/state").json()["agents"][0]["product"] == previous


def test_file_validation_and_failed_copy_preserve_product(fixture, monkeypatch):
    app, agent, workspace = fixture
    with TestClient(app) as client:
        endpoint = f"/api/agents/{agent.id}/product"
        previous = client.post(endpoint, json={"url": "https://example.com/good"}).json()["product"]
        assert client.post(endpoint, json={"path": "missing"}).status_code == 404
        assert client.post(endpoint, json={"path": str(workspace)}).status_code == 400
        large = workspace / "large.bin"
        large.write_bytes(b"large")
        monkeypatch.setattr("swarm.products.MAX_PRODUCT_BYTES", 3)
        assert client.post(endpoint, json={"path": str(large)}).status_code == 400
        assert client.get("/api/state").json()["agents"][0]["product"] == previous


def test_cleanup_after_clear_forget_and_restart(fixture):
    app, agent, workspace = fixture
    source = workspace / "result.bin"
    source.write_bytes(b"output")
    with TestClient(app) as client:
        endpoint = f"/api/agents/{agent.id}/product"
        first = client.post(endpoint, json={"path": str(source)}).json()["product"]
        second = client.post(endpoint, json={"path": str(source)}).json()["product"]
        assert not app.state.products.directory(agent.id, first["id"]).exists()
        orphan = app.state.products.directory(agent.id, uuid4())
        orphan.mkdir()
        (orphan / "payload").write_bytes(b"interrupted publication")
        app.state.products.collect()
        assert not orphan.exists()
        assert client.get(download_url(agent, second)).content == b"output"
        client.delete(endpoint)
        assert not app.state.products.directory(agent.id, second["id"]).exists()
        third = client.post(endpoint, json={"path": str(source)}).json()["product"]
        assert client.delete(f"/api/agents/{agent.id}").status_code == 200
        assert not app.state.products.directory(agent.id, third["id"]).exists()
        assert source.read_bytes() == b"output"


def test_persisted_product_survives_restart(fixture):
    app, agent, workspace = fixture
    path = workspace / "report.txt"
    path.write_text("saved content")
    with TestClient(app) as client:
        product = client.post(f"/api/agents/{agent.id}/product", json={"path": str(path)}).json()[
            "product"
        ]
    restarted = create_app(
        app.state.controller.store.home, recover=False, allowed_hosts=["testserver"]
    )
    with TestClient(restarted) as client:
        assert client.get(download_url(agent, product)).content == b"saved content"


def test_download_rejects_tampering_other_agent_and_symlinks(fixture):
    app, agent, workspace = fixture
    source = workspace / "output.txt"
    source.write_text("published bytes")
    other = app.state.controller.store.create(
        Agent(name="Other", goal="Other", folder=str(workspace))
    )
    with TestClient(app) as client:
        product = client.post(f"/api/agents/{agent.id}/product", json={"path": str(source)}).json()[
            "product"
        ]
        assert client.get(download_url(other, product)).status_code == 404
        payload = app.state.products.directory(agent.id, product["id"]) / "payload"
        payload.write_bytes(b"tampered bytes!")
        assert client.get(download_url(agent, product)).status_code == 400
        payload.unlink()
        payload.symlink_to(source)
        assert client.get(download_url(agent, product)).status_code == 400


def test_publish_rejects_symlink_storage_without_deleting_original(fixture):
    app, agent, workspace = fixture
    outside = workspace / "not-swarm-storage"
    outside.mkdir()
    source = workspace / "output"
    source.write_bytes(b"bytes")
    app.state.products.root.symlink_to(outside, target_is_directory=True)
    with TestClient(app) as client:
        response = client.post(f"/api/agents/{agent.id}/product", json={"path": str(source)})
        assert response.status_code == 400
        assert not list(outside.iterdir())
        assert source.read_bytes() == b"bytes"


def test_source_changes_during_copy_do_not_replace_previous_product(fixture, monkeypatch):
    app, agent, workspace = fixture
    source = workspace / "output"
    source.write_bytes(b"original bytes")
    with TestClient(app) as client:
        endpoint = f"/api/agents/{agent.id}/product"
        previous = client.post(endpoint, json={"url": "https://example.com/old"}).json()["product"]
        real_fsync = os.fsync

        def mutate(fd):
            real_fsync(fd)
            source.write_bytes(b"changed length while copying")

        monkeypatch.setattr("swarm.products.os.fsync", mutate)
        response = client.post(endpoint, json={"path": str(source)})
        assert response.status_code == 409
        assert str(app.state.controller.store.get(agent.id).product.id) == previous["id"]


def test_product_cli_uses_current_agent_and_controller(fixture, monkeypatch, capsys):
    app, agent, workspace = fixture
    calls = []
    monkeypatch.setenv("SWARM_AGENT_ID", str(agent.id))
    monkeypatch.setattr(
        "swarm.cli.AgentClient.product",
        lambda self, id, payload: (
            calls.append((id, payload)) or {"agent_id": id, "product": payload}
        ),
    )
    main(["product", "--url", "https://example.com/pr", "--label", "PR"])
    assert calls[-1] == (str(agent.id), {"url": "https://example.com/pr", "label": "PR"})
    assert json.loads(capsys.readouterr().out)["product"]["label"] == "PR"
    main(["product", "--file", str(workspace / "output")])
    assert calls[-1][1]["path"] == str(workspace / "output")
    main(["product", "--clear"])
    assert calls[-1] == (str(agent.id), None)
    monkeypatch.delenv("SWARM_AGENT_ID")
    with pytest.raises(SystemExit):
        main(["product", "--url", "https://example.com"])


def test_cleanup_never_follows_noncanonical_uuid_alias(fixture):
    app, agent, workspace = fixture
    root = app.state.products.root
    root.mkdir()
    outside = workspace / "private"
    outside.mkdir()
    unrelated = outside / str(uuid4())
    unrelated.mkdir()
    (unrelated / "payload").write_bytes(b"must not delete")
    (root / agent.id.hex).symlink_to(outside, target_is_directory=True)
    app.state.products.collect()
    assert (unrelated / "payload").read_bytes() == b"must not delete"


def test_failed_record_update_retains_old_snapshot(fixture, monkeypatch):
    app, agent, workspace = fixture
    source = workspace / "report"
    source.write_bytes(b"first version")
    with TestClient(app, raise_server_exceptions=False) as client:
        endpoint = f"/api/agents/{agent.id}/product"
        previous = client.post(endpoint, json={"path": str(source)}).json()["product"]
        source.write_bytes(b"second version")
        original_update = app.state.controller.store.update

        def fail_product(id, fields):
            if "product" in fields:
                raise OSError("record write failed")
            return original_update(id, fields)

        monkeypatch.setattr(app.state.controller.store, "update", fail_product)
        assert client.post(endpoint, json={"path": str(source)}).status_code == 500
        assert client.get(download_url(agent, previous)).content == b"first version"
        assert len(list(app.state.products.directory(agent.id).iterdir())) == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO test requires POSIX")
def test_fifo_input_is_rejected_without_blocking(fixture):
    app, agent, workspace = fixture
    pipe = workspace / "not-a-file"
    os.mkfifo(pipe)
    with TestClient(app) as client:
        assert (
            client.post(f"/api/agents/{agent.id}/product", json={"path": str(pipe)}).status_code
            == 400
        )
