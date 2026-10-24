import json
from pathlib import Path

from fastapi.testclient import TestClient
from test_auth import login, password_file
from test_password_relay import http
from test_password_relay import pair as pair
from test_tool_outputs import native_history

from swarm.cli import main
from swarm.models import Agent
from swarm.server import create_app
from swarm.tool_outputs import MAX_OUTPUT_BYTES, PREVIEW_BYTES


def add_source(app):
    store = app.state.controller.store
    history, _, _, _ = native_history(
        store.home, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    source = history.encode().encode()
    path = store.sessions_dir / "large.jsonl"
    path.write_bytes(source)
    a = store.create(
        Agent(
            name="Repair target",
            goal="Fix field budget",
            folder=str(store.home),
            managed=True,
            status="idle",
            auto_resume=False,
            session_file=str(path),
            session_id=history.entries[0]["id"],
        )
    )
    return a, path, source


def test_repair_api_is_authenticated_and_cli_retrieves_bounded_archive(
    tmp_path, monkeypatch, capsys
):
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password_file(tmp_path),
    )
    monkeypatch.setattr(app.state.controller.processes, "alive", lambda a: False)
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    a, path, source = add_source(app)
    with TestClient(app) as client:
        endpoint = f"/api/agents/{a.id}/repair-tool-outputs"
        assert client.post(endpoint, json={}).status_code == 401
        auth = login(client)
        assert (
            client.post(
                endpoint, json={}, headers=auth | {"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        response = client.post(endpoint, json={}, headers=auth)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["status"] == "completed" and report["outputs_repaired"] == 1
        assert report["preview_bytes"] <= PREVIEW_BYTES
        assert (
            path.read_bytes().startswith(source) and Path(report["archive"]).read_bytes() == source
        )
        record = client.get("/api/state", headers=auth).json()["agents"][0]
        assert record["tool_output_repair"]["status"] == "completed" and not record["alive"]
        assert not record["auto_resume"]
        main(
            [
                "--home",
                str(app.state.controller.store.home),
                "tool-output",
                report["output_ids"][0],
                "--offset",
                "100",
                "--limit",
                "33",
            ]
        )
        result = json.loads(capsys.readouterr().out)
        assert result["output"] == "x" * 33 and result["next_offset"] == 133 and not result["eof"]
        assert client.post(endpoint, json={}, headers=auth).json()["status"] == "unchanged"


def test_authenticated_relay_repairs_only_the_selected_node_source(pair, monkeypatch):
    app_a, app_b, base, headers = pair
    monkeypatch.setattr(app_b.state.controller.processes, "alive", lambda a: False)
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    a, path, source = add_source(app_b)
    prefix = "/api/federation/beta/proxy"
    endpoint = prefix + f"/api/agents/{a.id}/repair-tool-outputs"
    assert http(base, endpoint, "POST", {}, headers)[0] == 401
    _, signed_in = http(
        base,
        prefix + "/api/auth/login",
        "POST",
        {"password": "beta-separate-test-password"},
        headers,
    )
    auth = headers | {"X-Swarm-Authorization": "Bearer " + signed_in["token"]}
    status, report = http(base, endpoint, "POST", {}, auth)
    assert status == 200, report
    assert report["status"] == "completed" and report["preview_bytes"] <= PREVIEW_BYTES
    assert Path(report["archive"]).is_relative_to(app_b.state.controller.store.home)
    assert Path(report["archive"]).read_bytes() == source and path.read_bytes().startswith(source)
    assert not (app_a.state.controller.store.home / "tool-output-repairs").exists()
    status, state = http(base, prefix + "/api/state", headers=auth)
    assert status == 200 and state["agents"][0]["tool_output_repair"]["outputs_repaired"] == 1
