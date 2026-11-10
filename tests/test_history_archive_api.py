from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.importers.pi_history import PiHistory
from swarm.models import Agent
from swarm.server import create_app

PASSWORD = "history-archive-test-password"


def history(folder: Path, label: str) -> PiHistory:
    value = PiHistory(str(uuid4()), str(folder), label)
    for index in range(4):
        value.user(f"USER-{index} " + "u" * 4000)
        value.historical("assistant", f"ANSWER-{index} " + "a" * 4000)
    return value


def setup_app(tmp_path, monkeypatch):
    password = tmp_path / "password.txt"
    password.write_text(PASSWORD + "\n", encoding="utf-8")
    password.chmod(0o600)
    app = create_app(
        tmp_path / "registry",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password,
    )
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: {})
    monkeypatch.setattr(app.state.controller.processes, "alive", lambda agent: False)
    store = app.state.controller.store
    source_history = history(tmp_path, "Archive API")
    source = store.sessions_dir / "source.jsonl"
    source.write_text(source_history.encode(), encoding="utf-8")
    agent = store.create(
        Agent(
            name="Archive owner",
            goal="Preserve context",
            folder=str(tmp_path),
            managed=True,
            auto_resume=True,
            session_id=source_history.entries[0]["id"],
            session_file=str(source),
        )
    )
    return app, agent, source


def login(client: TestClient) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_archive_post_is_authenticated_idempotent_and_retrievable(tmp_path, monkeypatch):
    app, agent, source = setup_app(tmp_path, monkeypatch)
    request_id = str(uuid4())
    original = source.read_bytes()
    endpoint = f"/api/agents/{agent.id}/archive-history"

    with TestClient(app) as client:
        assert (
            client.post(endpoint, json={"percent": 50, "request_id": request_id}).status_code == 401
        )
        headers = login(client)
        response = client.post(
            endpoint, json={"percent": 50, "request_id": request_id}, headers=headers
        )
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["status"] == "completed" and report["percent_requested"] == 50
        assert source.read_bytes().startswith(original)
        assert app.state.controller.store.get(agent.id).auto_resume is False

        repeated = client.post(
            endpoint, json={"percent": 50, "request_id": request_id}, headers=headers
        )
        assert repeated.status_code == 200
        assert repeated.json()["archive_id"] == report["archive_id"]
        manifests = list(source.parent.glob("context-archives/*/*/manifest.json"))
        assert manifests == [Path(report["manifest"])]

        page_url = f"/api/agents/{agent.id}/history-archives/{report['archive_id']}"
        assert client.get(page_url).status_code == 401
        first = client.get(
            page_url,
            params={"offset": 0, "limit": 127, "format": "text"},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["offset"] == 0 and 0 < body["next_offset"] <= 130
        structured = client.get(page_url, params={"limit": 300, "format": "jsonl"}, headers=headers)
        assert structured.status_code == 200
        assert structured.json()["output"].startswith("{")

        for params in ({"offset": -1}, {"limit": 0}, {"limit": 64001}, {"format": "raw"}):
            assert client.get(page_url, params=params, headers=headers).status_code == 422
        assert (
            client.get(page_url, headers=headers | {"Origin": "https://evil.example"}).status_code
            == 403
        )


def test_archive_get_cannot_cross_agent_or_address_paths(tmp_path, monkeypatch):
    app, agent, _ = setup_app(tmp_path, monkeypatch)
    other_history = history(tmp_path, "Other")
    other_source = app.state.controller.store.sessions_dir / "other.jsonl"
    other_source.write_text(other_history.encode(), encoding="utf-8")
    other = app.state.controller.store.create(
        Agent(
            name="Other owner",
            goal="Separate history",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            session_id=other_history.entries[0]["id"],
            session_file=str(other_source),
        )
    )

    with TestClient(app) as client:
        headers = login(client)
        report = client.post(
            f"/api/agents/{agent.id}/archive-history",
            json={"percent": 50, "request_id": str(uuid4())},
            headers=headers,
        ).json()
        foreign = client.get(
            f"/api/agents/{other.id}/history-archives/{report['archive_id']}", headers=headers
        )
        assert foreign.status_code in {400, 404}
        assert (
            client.get(
                f"/api/agents/{agent.id}/history-archives/not-a-uuid", headers=headers
            ).status_code
            == 422
        )
        attack = client.get(
            f"/api/agents/{agent.id}/history-archives/%2e%2e%2fmanifest.json", headers=headers
        )
        assert attack.status_code in {404, 422}


def test_archive_and_retrieval_need_no_model_worker_or_agent_wake(tmp_path, monkeypatch):
    app, agent, source = setup_app(tmp_path, monkeypatch)

    def forbidden(*_args, **_kwargs):
        pytest.fail("History archival and retrieval must not invoke an LLM or wake an agent")

    class ForbiddenSummarizer:
        def __call__(self, *_args, **_kwargs):
            forbidden()

        def preflight(self, *_args, **_kwargs):
            forbidden()

    app.state.meta_compactor.summarize = ForbiddenSummarizer()
    monkeypatch.setattr(app.state.meta_compactor, "start", forbidden)
    monkeypatch.setattr(app.state.meta_compactor, "run", forbidden)
    controller = app.state.controller
    monkeypatch.setattr(controller, "wake", forbidden)
    monkeypatch.setattr(controller, "_launch", forbidden)
    for method in (
        "launch",
        "launch_restart",
        "launch_setup",
        "_start",
        "send_prompt",
        "paste_text",
    ):
        monkeypatch.setattr(controller.processes, method, forbidden)

    request = {"percent": 50, "request_id": str(uuid4())}
    original = source.read_bytes()
    with TestClient(app) as client:
        headers = login(client)
        endpoint = f"/api/agents/{agent.id}/archive-history"
        response = client.post(endpoint, json=request, headers=headers)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["status"] == "completed"
        assert source.read_bytes().startswith(original)
        assert "no LLM summary was generated" in source.read_text()
        for format in ("text", "jsonl"):
            page = client.get(
                f"/api/agents/{agent.id}/history-archives/{report['archive_id']}",
                params={"format": format, "limit": 100},
                headers=headers,
            )
            assert page.status_code == 200, page.text
            assert page.json()["output"]
        repeated = client.post(endpoint, json=request, headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["archive_id"] == report["archive_id"]
        assert controller.store.get(agent.id).auto_resume is False


def test_dashboard_shell_does_not_cache_away_archive_controls(tmp_path, monkeypatch):
    app, _, _ = setup_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        shell = client.get("/")
        assert shell.status_code == 200
        assert shell.headers["cache-control"] == "no-store"
        assert 'id="archive-history"' in shell.text
        assert 'id="history-archive-percent"' in shell.text
        assert "/static/history-archive.js?v=" in shell.text
        assert "No LLM" in shell.text
        assert client.get("/static/history-archive.js").status_code == 200
