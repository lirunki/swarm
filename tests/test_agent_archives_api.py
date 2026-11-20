import time
from uuid import uuid4

from fastapi.testclient import TestClient

from swarm.importers.pi_history import PiHistory
from swarm.models import Agent
from swarm.server import create_app

PASSWORD = "agent-archive-owner-password"


def setup_app(tmp_path, monkeypatch):
    password = tmp_path / "password.txt"
    password.write_text(PASSWORD + "\n", encoding="utf-8")
    password.chmod(0o600)
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    monkeypatch.setattr("swarm.sessions.running_pi_processes", lambda: [])
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password,
    )
    controller = app.state.controller
    monkeypatch.setattr(controller.processes, "alive", lambda _agent: False)
    history = PiHistory(str(uuid4()), str(tmp_path), "API archive")
    history.user("Archive through the owner-only API")
    history.historical("assistant", "Done")
    source = controller.store.sessions_dir / "api.jsonl"
    source.write_text(history.encode(), encoding="utf-8")
    agent = controller.store.create(
        Agent(
            name="API worker",
            goal="Test API archival",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            session_id=history.entries[0]["id"],
            session_file=str(source),
        )
    )
    return app, agent


def login(client: TestClient) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def wait_for_job(client: TestClient, request_id: str, headers: dict[str, str]) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/archive-jobs/{request_id}", headers=headers)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("archive job did not finish")


def test_owner_archive_list_download_resurrect_delete_round_trip(tmp_path, monkeypatch):
    app, agent = setup_app(tmp_path, monkeypatch)
    archive_request = {"include_files": False, "request_id": str(uuid4())}
    endpoint = f"/api/agents/{agent.id}/archive-and-forget"

    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        assert client.post(endpoint, json=archive_request).status_code == 401
        service_headers = {
            "Authorization": "Bearer " + app.state.access.service_token,
        }
        assert (
            client.post(endpoint, json=archive_request, headers=service_headers).status_code == 403
        )
        headers = login(client)

        started_at = time.monotonic()
        response = client.post(endpoint, json=archive_request, headers=headers)
        assert response.status_code == 202, response.text
        assert time.monotonic() - started_at < 0.3
        accepted = response.json()
        assert accepted["job"]["id"] == archive_request["request_id"]
        assert accepted["job"]["status"] in {"queued", "running"}
        assert "may contain secrets" in accepted["warning"]

        completed = wait_for_job(client, archive_request["request_id"], headers)
        assert completed["status"] == "completed", completed
        archived = completed["archive"]
        assert archived["agent_id"] == str(agent.id)
        assert archived["status"] == "archived"
        assert archived["restorable"] is True

        repeated = client.post(endpoint, json=archive_request, headers=headers)
        assert repeated.status_code == 202
        assert repeated.json()["job"]["archive_id"] == archived["id"]
        assert repeated.json()["archive"]["id"] == archived["id"]

        listing = client.get("/api/archives", headers=headers)
        assert listing.status_code == 200
        item = listing.json()["archives"][0]
        expected = {
            "id",
            "name",
            "agent_id",
            "created_at",
            "archived_at",
            "last_accessed_at",
            "last_resurrected_at",
            "status",
            "goal",
            "folder",
            "session_id",
            "size",
            "files_included",
            "restorable",
        }
        assert set(item) == expected | {"missing_zip"}
        assert item["missing_zip"] is False
        assert listing.json()["jobs"][0]["id"] == archive_request["request_id"]
        assert item["last_accessed_at"] is None

        download = client.get(f"/api/archives/{archived['id']}/download", headers=headers)
        assert download.status_code == 200
        assert download.content.startswith(b"PK")
        assert client.get("/api/archives", headers=headers).json()["archives"][0][
            "last_accessed_at"
        ]

        resurrection = client.post(
            f"/api/archives/{archived['id']}/resurrect",
            json={"request_id": str(uuid4())},
            headers=headers,
        )
        assert resurrection.status_code == 200, resurrection.text
        assert resurrection.json()["agent"]["id"] == str(agent.id)
        assert resurrection.json()["agent"]["auto_resume"] is False
        assert resurrection.json()["agent"]["alive"] is False

        conflict = client.post(
            f"/api/archives/{archived['id']}/resurrect",
            json={"request_id": str(uuid4())},
            headers=headers,
        )
        assert conflict.status_code == 409

        deleted = client.delete(f"/api/archives/{archived['id']}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json() == {"id": archived["id"], "deleted": True}
        final_listing = client.get("/api/archives", headers=headers).json()
        assert final_listing["archives"] == []
        assert final_listing["jobs"][0]["status"] == "completed"


def test_archive_api_validates_uuid_bodies_and_operator_auth(tmp_path, monkeypatch):
    app, agent = setup_app(tmp_path, monkeypatch)
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        headers = login(client)
        endpoint = f"/api/agents/{agent.id}/archive-and-forget"
        assert client.post(endpoint, json={}, headers=headers).status_code == 422
        assert (
            client.post(
                endpoint,
                json={"request_id": "not-a-uuid", "include_files": False},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                endpoint,
                json={"request_id": str(uuid4()), "include_files": False, "extra": True},
                headers=headers,
            ).status_code
            == 422
        )
        assert client.get("/api/archives").status_code == 401
        assert (
            client.get(
                "/api/archives",
                headers={"Authorization": "Bearer " + app.state.access.service_token},
            ).status_code
            == 403
        )
