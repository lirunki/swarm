import json
import logging
import os

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from swarm.auth import AuthFailure, BrowserAccess, canonical
from swarm.files import FileRequest
from swarm.models import Agent
from swarm.server import create_app

PASSWORD = "test-only-password_not-a-real-credential"


def password_file(tmp_path, value=PASSWORD):
    path = tmp_path / "browser-password.txt"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def app(tmp_path):
    return create_app(
        tmp_path / "registry",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password_file(tmp_path),
    )


def login(client):
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_optional_auth_keeps_unconfigured_servers_compatible(tmp_path):
    with TestClient(
        create_app(tmp_path / "registry", recover=False, allowed_hosts=["testserver"])
    ) as client:
        assert client.get("/api/auth/status").json() == {"required": False}
        assert client.get("/api/state").status_code == 200
        with client.websocket_connect("/ws/state") as ws:
            assert ws.receive_json()["agents"] == []


def test_browser_password_gates_all_data_and_controls_not_static_shell(app):
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/auth/status").json() == {"required": True}
        for method, path in [
            ("GET", "/api/state"),
            ("GET", "/api/config/models"),
            ("GET", "/api/folders"),
            ("GET", "/api/pi/sessions"),
            ("GET", "/api/federation/nodes"),
            ("GET", "/api/federation/settings"),
            ("GET", "/openapi.json"),
            ("POST", "/api/agents"),
            ("POST", "/api/controller/restart"),
        ]:
            response = client.request(method, path, json={})
            assert response.status_code == 401, path
            assert response.json()["detail"] == "Authentication required"
            assert "no-store" in response.headers["cache-control"]
        headers = login(client)
        assert client.get("/api/state", headers=headers).status_code == 200
        assert client.get("/openapi.json", headers=headers).status_code == 200
        assert not client.cookies
        assert "set-cookie" not in client.get("/api/auth/status").headers
        assert client.get("/api/state").status_code == 401  # Browser state is not cookie based.
        assert (
            client.get("/api/state", params={"token": headers["Authorization"]}).status_code == 401
        )


def test_wrong_password_invalid_body_rate_limit_and_no_secret_echo(app, caplog):
    with TestClient(app) as client:
        for body in (
            {"password": "wrong"},
            {"password": {"secret": PASSWORD}},
            {"password": PASSWORD, "extra": True},
        ):
            response = client.post("/api/auth/login", json=body)
            assert response.status_code in (400, 401)
            assert PASSWORD not in response.text and "wrong" not in response.text
        assert client.post("/api/auth/login", content=b"x" * 8193).status_code == 413
        assert (
            client.post(
                "/api/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        for _ in range(20):
            client.post("/api/auth/login", json={"password": "incorrect"})
        assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429
        assert PASSWORD not in caplog.text


def test_logout_and_server_restart_invalidate_browser_tokens(app):
    with TestClient(app) as client:
        headers = login(client)
        assert client.delete("/api/auth/session", headers=headers).status_code == 200
        assert client.get("/api/state", headers=headers).status_code == 401
        headers = login(client)
    # New controller instance, same password and registry: old browser session is invalid.
    second = create_app(
        app.state.controller.store.home,
        recover=False,
        allowed_hosts=["testserver"],
        password_file=app.state.controller.store.home.parent / "browser-password.txt",
    )
    with TestClient(second) as client:
        assert client.get("/api/state", headers=headers).status_code == 401
        assert client.get("/api/state", headers=login(client)).status_code == 200


def test_service_token_is_private_loopback_only_and_relay_ineligible(app):
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        path = app.state.access.service_file
        assert path.is_file() and path.stat().st_mode & 0o077 == 0
        headers = {"Authorization": "Bearer " + path.read_text().strip()}
        assert client.get("/api/state", headers=headers).status_code == 200
        assert (
            client.get("/api/state", headers=headers | {"Origin": "http://testserver"}).status_code
            == 401
        )
        assert not app.state.access.authorize(headers["Authorization"])
        assert app.state.access.authorize(headers["Authorization"], service_allowed=True)
    with TestClient(app, client=("192.0.2.10", 10000)) as remote:
        assert remote.get("/api/state", headers=headers).status_code == 401


def test_file_ticket_is_path_bound_one_use_and_download_requires_auth(app, tmp_path):
    ctl = app.state.controller
    agent = ctl.store.create(Agent(name="Files", goal="Read", folder=str(tmp_path)))
    file = tmp_path / "report.txt"
    file.write_bytes(b"private report bytes")
    reference = ctl.files.add(agent.id, FileRequest(path=str(file)))["file"]
    path = f"/api/agents/{agent.id}/files/{reference['id']}/content?download=true"
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        headers = login(client)
        assert client.get(path, headers=headers).content == b"private report bytes"
        issued = client.post("/api/auth/ticket", json={"path": path}, headers=headers)
        assert issued.status_code == 200, issued.text
        ticket = issued.json()["ticket"]
        url = path + "&access_ticket=" + ticket
        result = client.get(url)
        assert result.status_code == 200 and result.content == b"private report bytes"
        assert "attachment" in result.headers["content-disposition"]
        assert client.get(url).status_code == 401
        ticket = client.post("/api/auth/ticket", json={"path": path}, headers=headers).json()[
            "ticket"
        ]
        assert client.get("/api/state?access_ticket=" + ticket).status_code == 401
        assert client.get(path + "&access_ticket=" + ticket).status_code == 401
        for forbidden in (
            "https://evil.example/x",
            "/api/controller/restart",
            "/api/state",
            "//example.com",
            "/api/../state",
        ):
            assert (
                client.post(
                    "/api/auth/ticket", json={"path": forbidden}, headers=headers
                ).status_code
                == 400
            )
        ticket = client.post("/api/auth/ticket", json={"path": path}, headers=headers).json()[
            "ticket"
        ]
        assert client.post(path + "&access_ticket=" + ticket, json={}).status_code == 401


def test_websockets_require_bearer_or_single_use_ticket_before_sending_state(app):
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/ws/state"):
                pass
        assert denied.value.code == 4401
        headers = login(client)
        with client.websocket_connect("/ws/state", headers=headers) as ws:
            assert ws.receive_json()["agents"] == []
        ticket = client.post(
            "/api/auth/ticket", json={"path": "/ws/state"}, headers=headers
        ).json()["ticket"]
        with client.websocket_connect("/ws/state?access_ticket=" + ticket) as ws:
            assert ws.receive_json()["agents"] == []
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/state?access_ticket=" + ticket):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/terminal/00000000-0000-0000-0000-000000000001"):
                pass


def test_tokens_and_navigation_tickets_expire_and_access_logs_redact_tickets(app, monkeypatch):
    access = app.state.access
    result = access.login(PASSWORD)
    credential = "Bearer " + result["token"]
    assert access.authorize(credential)
    ticket = access.ticket("/ws/state", credential, "", False)
    value = access.tickets[ticket]
    access.tickets[ticket] = (0, *value[1:])
    with pytest.raises(AuthFailure):
        access.consume(ticket, "websocket", "/ws/state")
    access.sessions = type(access.sessions)((key, 0) for key in access.sessions)
    assert not access.authorize(credential)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s",
        ("/ws/state?cols=80&access_ticket=abc_secret",),
        None,
    )
    for filter in logging.getLogger("uvicorn.access").filters:
        filter.filter(record)
    assert "abc_secret" not in record.getMessage() and "[redacted]" in record.getMessage()


@pytest.mark.parametrize("value", ["", "\n", "x\ny", "bad\x00password", "x" * 5000])
def test_password_file_validation_does_not_echo_contents(tmp_path, value):
    path = password_file(tmp_path, value)
    with pytest.raises(ValueError, match="Password file must") as failure:
        BrowserAccess(tmp_path / "home", path)
    assert value not in str(failure.value) if len(value) > 20 else True


def test_readable_password_file_is_accepted_but_symlinks_rejected(tmp_path):
    path = password_file(tmp_path)
    if os.name != "nt":
        path.chmod(0o644)
        assert BrowserAccess(tmp_path / "home", path).required
    path.chmod(0o600)
    link = tmp_path / "password-link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        BrowserAccess(tmp_path / "home", link)
    access = BrowserAccess(tmp_path / "home", path)
    access.start()
    assert access.service_file.read_text().strip() != PASSWORD


def test_ticket_query_canonicalization_does_not_allow_credentials():
    assert canonical("/ws/state?b=2&a=1") == "/ws/state?a=1&b=2"
    for path in [
        "/ws/state?token=secret",
        "/ws/state?password=secret",
        "/ws/state?access_ticket=secret",
    ]:
        with pytest.raises(AuthFailure):
            canonical(path)


def test_open_state_socket_is_revoked_after_logout(app, monkeypatch):
    monkeypatch.setattr("swarm.auth.SOCKET_CHECK_SECONDS", 0.02)
    with TestClient(app) as client:
        headers = login(client)
        with client.websocket_connect("/ws/state", headers=headers) as ws:
            ws.receive_json()
            client.delete("/api/auth/session", headers=headers)
            with pytest.raises(WebSocketDisconnect) as failure:
                ws.receive_json()
            assert failure.value.code == 4401


def test_ws_error_logs_redact_tickets_in_format_and_args(app):
    for msg, args in [
        ("WebSocket %s accepted", ("/ws/state?access_ticket=one_use_secret",)),
        ("WebSocket /ws/state?access_ticket=another_secret accepted", ()),
    ]:
        record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, msg, args, None)
        for filter in logging.getLogger("uvicorn.error").filters:
            filter.filter(record)
        assert (
            "one_use_secret" not in record.getMessage()
            and "another_secret" not in record.getMessage()
        )
        assert "[redacted]" in record.getMessage()


@pytest.mark.parametrize("name", ["Authorization", "ACCESS_TICKET", "api_key", "access_token"])
def test_tickets_reject_case_variants_of_query_credentials(name):
    with pytest.raises(AuthFailure):
        canonical("/ws/state?" + name + "=secret")


def test_native_cli_reads_private_service_key_and_never_needs_browser_password(
    tmp_path, monkeypatch, capsys
):
    from test_federation_integration import running_server

    from swarm.cli import main

    pwd = password_file(tmp_path)
    app = create_app(tmp_path / "registry", recover=False, password_file=pwd)
    agent = app.state.controller.store.create(
        Agent(name="Local CLI", goal="Work", folder=str(tmp_path), auto_resume=False)
    )
    monkeypatch.delenv("SWARM_API_TOKEN_FILE", raising=False)
    with running_server(app) as port:
        main(
            [
                "--home",
                str(app.state.controller.store.home),
                "--api-url",
                f"http://127.0.0.1:{port}",
                "product",
                "--id",
                str(agent.id),
                "--url",
                "https://example.invalid/result",
            ]
        )
        value = json.loads(capsys.readouterr().out)
        assert value["product"]["url"] == "https://example.invalid/result"
        assert PASSWORD not in json.dumps(value)
