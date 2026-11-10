import json
from uuid import uuid4

from fastapi.testclient import TestClient
from test_auth import login, password_file

from swarm.models import Agent, Status
from swarm.server import create_app


def mutation(limit, revision=0, request_id=None):
    return {
        "max_active_managed": limit,
        "revision": revision,
        "request_id": str(request_id or uuid4()),
    }


def add_agent(app, tmp_path, **fields):
    values = {
        "name": "worker",
        "goal": "test limits",
        "folder": str(tmp_path),
        "managed": True,
        "auto_resume": False,
        "status": Status.idle,
    }
    values.update(fields)
    return app.state.controller.store.create(Agent(**values))


def test_api_default_persistence_restart_raise_lower_and_state(tmp_path):
    home = tmp_path / "home"
    app = create_app(home, recover=False, allowed_hosts=["testserver"])
    active = add_agent(app, tmp_path, auto_resume=True, status=Status.running)
    running = {str(active.id)}
    app.state.controller.processes.alive = lambda item: str(item.id) in running
    app.state.controller.processes.pid = lambda item: 123 if str(item.id) in running else None
    stopped = []
    app.state.controller.processes.stop = lambda item: stopped.append(str(item.id))

    with TestClient(app) as client:
        initial = client.get("/api/agent-limits")
        assert initial.status_code == 200
        assert initial.json() == {
            "revision": 0,
            "max_active_managed": 20,
            "active_managed": 1,
            "scope": "orchestration",
        }
        assert not (home / "agent-limits.json").exists()

        raised = client.post("/api/agent-limits", json=mutation(30)).json()
        assert raised["revision"] == 1 and raised["max_active_managed"] == 30
        lowered = client.post("/api/agent-limits", json=mutation(1, revision=1))
        assert lowered.status_code == 200
        assert lowered.json()["active_managed"] == 1
        assert stopped == [] and running == {str(active.id)}

        state = client.get("/api/state").json()
        assert state["agent_limits"] == lowered.json()
        assert state["agent_limits_error"] is None

    restarted = create_app(home, recover=False, allowed_hosts=["testserver"])
    restarted.state.controller.processes.alive = lambda item: False
    with TestClient(restarted) as client:
        saved = client.get("/api/agent-limits").json()
        assert saved["revision"] == 2 and saved["max_active_managed"] == 1


def test_api_revision_idempotence_schema_range_origin_and_no_auto_retry(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        request = mutation(9)
        first = client.post("/api/agent-limits", json=request)
        assert first.status_code == 200
        replay = client.post("/api/agent-limits", json=request)
        assert replay.status_code == 200 and replay.json() == first.json()

        mismatch = client.post(
            "/api/agent-limits", json=request | {"max_active_managed": 10}
        )
        assert mismatch.status_code == 409
        stale = client.post("/api/agent-limits", json=mutation(10, revision=0))
        assert stale.status_code == 409
        assert client.get("/api/agent-limits").json()["revision"] == 1

        for invalid in (
            mutation(True, revision=1),
            mutation(10001, revision=1),
            mutation(4, revision=1) | {"unknown": 1},
        ):
            response = client.post("/api/agent-limits", json=invalid)
            assert response.status_code == 400
        assert client.post("/api/agent-limits", content=b"not-json").status_code == 400
        assert (
            client.get(
                "/api/agent-limits", headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )


def test_corrupt_settings_and_registry_fail_closed_without_file_contents(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    one = add_agent(app, tmp_path)
    path = app.state.controller.agent_limits.path
    path.write_text('{"credential":"must-not-echo"', encoding="utf-8")
    path.chmod(0o600)

    with TestClient(app) as client:
        get = client.get("/api/agent-limits")
        assert get.status_code == 409 and "must-not-echo" not in get.text
        post = client.post("/api/agent-limits", json=mutation(8))
        assert post.status_code == 409 and "must-not-echo" not in post.text
        state = client.get("/api/state").json()
        assert state["agent_limits"] is None
        assert state["agent_limits_error"] == "Saved agent limits could not be read"
        assert "must-not-echo" not in json.dumps(state)
        assert path.read_text(encoding="utf-8") == '{"credential":"must-not-echo"'

        path.unlink()
        app.state.controller.store.path(one.id).write_text("invalid record", encoding="utf-8")
        unavailable = client.get("/api/agent-limits")
        assert unavailable.status_code == 409
        refused = client.post("/api/agent-limits", json=mutation(8))
        assert refused.status_code == 409
        assert not path.exists()
        state = client.get("/api/state").json()
        assert state["agent_limits"] is None
        assert "unreadable records" in state["agent_limits_error"]


def test_api_uses_browser_auth_and_service_agent_cannot_mutate_owner_setting(tmp_path):
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password_file(tmp_path),
    )
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        assert client.get("/api/agent-limits").status_code == 401
        assert client.post("/api/agent-limits", json=mutation(8)).status_code == 401

        service = app.state.access.service_file.read_text(encoding="utf-8").strip()
        service_headers = {"Authorization": f"Bearer {service}"}
        assert client.get("/api/agent-limits", headers=service_headers).status_code == 200
        denied = client.post(
            "/api/agent-limits", json=mutation(8), headers=service_headers
        )
        assert denied.status_code == 403
        assert not app.state.controller.agent_limits.path.exists()

        owner_headers = login(client)
        accepted = client.post(
            "/api/agent-limits", json=mutation(8), headers=owner_headers
        )
        assert accepted.status_code == 200
