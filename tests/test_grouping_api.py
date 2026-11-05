from uuid import uuid4

from fastapi.testclient import TestClient
from test_auth import PASSWORD, login, password_file

from swarm.models import Agent
from swarm.server import create_app


def add_agent(app, tmp_path, *, parent_id=None):
    return app.state.controller.store.create(
        Agent(
            name="grouped agent",
            goal="exercise API",
            folder=str(tmp_path),
            parent_id=parent_id,
            auto_resume=False,
        )
    )


def operation(action, revision, **fields):
    return {"action": action, "revision": revision, "request_id": str(uuid4()), **fields}


def enable_server_grouping(client):
    revision = client.get("/api/grouping-settings").json()["revision"]
    response = client.post(
        "/api/grouping-settings",
        json=operation("configure", revision, server_managed=True),
    )
    assert response.status_code == 200
    return response.json()


def test_groups_api_fresh_old_client_requires_server_managed_mode(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        response = client.post(
            "/api/groups",
            json=operation("create", 0, name="Old client", agent_ids=[]),
        )

        assert response.status_code == 409
        assert response.json() == {
            "detail": (
                "Group changes require server-managed grouping; "
                "enable server-managed grouping"
            )
        }
        assert not app.state.controller.groups.path.exists()


def test_groups_api_schema_revision_idempotence_and_state_projection(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    root = add_agent(app, tmp_path)
    child = add_agent(app, tmp_path, parent_id=root.id)
    with TestClient(app) as client:
        empty = client.get("/api/groups")
        assert empty.status_code == 200
        assert empty.json() == {"revision": 0, "groups": [], "assignments": {}}

        enabled = enable_server_grouping(client)
        body = operation(
            "create",
            enabled["revision"],
            name="<script>alert(1)</script>",
            agent_ids=[str(child.id)],
        )
        created = client.post("/api/groups", json=body)
        assert created.status_code == 200
        result = created.json()
        group_id = result["operation"]["group_id"]
        assert result["revision"] == enabled["revision"] + 1
        assert result["groups"] == [{"id": group_id, "name": "<script>alert(1)</script>"}]
        assert result["assignments"] == {str(root.id): group_id, str(child.id): group_id}

        retry = client.post("/api/groups", json=body)
        assert retry.status_code == 200 and retry.json() == result
        stale = client.post(
            "/api/groups",
            json=operation(
                "rename", enabled["revision"], group_id=group_id, name="stale"
            ),
        )
        assert stale.status_code == 409
        assert client.get("/api/groups").json() == {
            key: result[key] for key in ("revision", "groups", "assignments")
        }

        state = client.get("/api/state").json()
        assert state["agent_groups"] == client.get("/api/groups").json()
        assert state["agent_groups_error"] is None


def test_groups_api_validates_ids_names_origin_and_corrupt_state(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    add_agent(app, tmp_path)
    with TestClient(app) as client:
        crossed = client.get("/api/groups", headers={"Origin": "https://evil.example"})
        assert crossed.status_code == 403
        enabled = enable_server_grouping(client)
        invalid = client.post(
            "/api/groups",
            json=operation(
                "create", enabled["revision"], name="bad\nname", agent_ids=[]
            ),
        )
        assert invalid.status_code == 400
        unknown = client.post(
            "/api/groups",
            json=operation(
                "assign",
                enabled["revision"],
                group_id=None,
                agent_ids=[str(uuid4())],
            ),
        )
        assert unknown.status_code == 400
        assert client.get("/api/groups").json()["revision"] == enabled["revision"]

        path = app.state.controller.groups.path
        path.write_text('{"credentials":"must not echo"', encoding="utf-8")
        broken = client.get("/api/groups")
        assert broken.status_code == 409
        assert broken.json() == {"detail": "Saved groups could not be read"}
        settings_broken = client.get("/api/grouping-settings")
        assert settings_broken.status_code == 409
        assert settings_broken.json() == {"detail": "Saved groups could not be read"}
        state = client.get("/api/state").json()
        assert state["agent_groups_error"] == "Saved groups could not be read"
        assert state["grouping_settings_error"] == state["agent_groups_error"]
        assert "credentials" not in str(state)
        assert path.read_text(encoding="utf-8") == '{"credentials":"must not echo"'


def test_groups_api_rejects_mutation_when_any_agent_record_is_invalid(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    one = add_agent(app, tmp_path)
    with TestClient(app) as client:
        enabled = enable_server_grouping(client)
        create_body = operation(
            "create", enabled["revision"], name="safe", agent_ids=[str(one.id)]
        )
        created = client.post("/api/groups", json=create_body).json()
        original = app.state.controller.groups.path.read_bytes()
        app.state.controller.store.path(one.id).write_text("not a record", encoding="utf-8")

        replay = client.post("/api/groups", json=create_body)
        assert replay.status_code == 200 and replay.json() == created
        mismatch = client.post("/api/groups", json=create_body | {"name": "different"})
        assert mismatch.status_code == 409
        assert mismatch.json() == {
            "detail": "request_id was already used for a different operation"
        }

        rejected = client.post(
            "/api/groups",
            json=operation(
                "assign",
                created["revision"],
                group_id=None,
                agent_ids=[str(one.id)],
            ),
        )
        assert rejected.status_code == 409
        assert rejected.json() == {
            "detail": "Agent records could not be read; groups were not changed"
        }
        assert app.state.controller.groups.path.read_bytes() == original


def test_groups_api_uses_existing_browser_auth(tmp_path):
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password_file(tmp_path),
    )
    with TestClient(app) as client:
        assert client.get("/api/groups").status_code == 401
        assert client.post("/api/groups", json={}).status_code == 401
        headers = login(client)
        assert client.get("/api/groups", headers=headers).json() == {
            "revision": 0,
            "groups": [],
            "assignments": {},
        }
        assert PASSWORD not in app.state.controller.groups.path.as_posix()
