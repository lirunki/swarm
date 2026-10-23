"""Opt-in network mode tests; Uvicorn is mocked so no network port is exposed."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from swarm import cli, server


@pytest.mark.parametrize(
    "host,expected_bind,expected_url,expected_hosts",
    [
        (None, "127.0.0.1", "http://127.0.0.1:9876", None),
        ("localhost", "localhost", "http://localhost:9876", None),
        ("::1", "::1", "http://[::1]:9876", None),
        ("0.0.0.0", "0.0.0.0", "http://127.0.0.1:9876", ["*"]),
    ],
)
def test_cli_binding_browser_url_and_host_policy(
    tmp_path, monkeypatch, capsys, host, expected_bind, expected_url, expected_hosts
):
    captured, opened = {}, []
    real_create = server.create_app

    def create(home, **kwargs):
        captured["factory"] = kwargs
        return real_create(home, **kwargs)

    def run(app, **kwargs):
        captured["run"] = kwargs

    monkeypatch.setattr(server, "create_app", create)
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(
        cli.threading,
        "Timer",
        lambda interval, callback: SimpleNamespace(daemon=False, start=callback),
    )
    argv = ["--home", str(tmp_path), "serve", "--port", "9876", "--no-recover", "--open"]
    if host is not None:
        argv += ["--host", host]
    cli.main(argv)
    assert captured["run"]["host"] == expected_bind
    assert captured["run"]["port"] == 9876 and captured["run"]["workers"] == 1
    assert captured["factory"]["allowed_hosts"] == expected_hosts
    assert not captured["factory"]["recover"]
    assert opened == [expected_url]
    output = capsys.readouterr()
    assert f"Swarm: {expected_url}" in output.out
    if host == "0.0.0.0":
        assert "all IPv4 interfaces" in output.out
        assert "no authentication" in output.err and "never expose it publicly" in output.err
    else:
        assert not output.err


def test_cli_ip_allowlist_is_passed_to_app_and_disables_proxy_headers(tmp_path, monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        server,
        "create_app",
        lambda home, **kwargs: captured.setdefault("factory", kwargs),
    )
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: captured.setdefault("run", kwargs)
    )
    monkeypatch.setattr(cli.webbrowser, "open", lambda _: None)

    cli.main(
        [
            "--home",
            str(tmp_path),
            "serve",
            "--host",
            "0.0.0.0",
            "--allow-ip",
            "192.0.2.10",
            "--allowed-ip",
            "2001:db8::10",
            "--allow-ip",
            "192.0.2.10",
        ]
    )

    assert captured["factory"]["allowed_client_ips"] == ["192.0.2.10", "2001:db8::10"]
    assert captured["run"]["proxy_headers"] is False
    assert "Client IP allowlist: 192.0.2.10, 2001:db8::10" in capsys.readouterr().out


def test_cli_rejects_invalid_ip_allowlist_entry():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["serve", "--allow-ip", "not-an-ip"])


def test_ip_allowlist_logs_each_new_peer_once(caplog):
    async def exercise():
        async def app(scope, receive, send):
            pass

        middleware = server.ClientIPAllowlistMiddleware(app, ["192.0.2.10"])

        async def call(peer):
            async def send(event):
                pass

            await middleware({"type": "http", "client": peer}, lambda: None, send)

        await call(("192.0.2.10", 60000))
        await call(("192.0.2.10", 60000))
        await call(("192.0.2.10", 60001))
        await call(("192.0.2.11", 60002))
        await call(("192.0.2.11", 60002))

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    asyncio.run(exercise())

    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("Accepted new IP-allowlisted client 192.0.2.10:60000") == 1
    assert messages.count("Accepted new IP-allowlisted client 192.0.2.10:60001") == 1
    assert messages.count("Rejected new client 192.0.2.11:60002: IP is not allowlisted") == 1


def test_ip_allowlist_blocks_http_and_websocket_before_app_dispatch():
    async def exercise():
        dispatched = []

        async def app(scope, receive, send):
            dispatched.append(scope["type"])

        middleware = server.ClientIPAllowlistMiddleware(
            app, ["192.0.2.10", "2001:db8::10"]
        )

        async def call(scope):
            events = []

            async def send(event):
                events.append(event)

            await middleware(scope, lambda: None, send)
            return events

        blocked = await call({"type": "http", "client": ("192.0.2.11", 1234)})
        assert blocked[0]["type"] == "http.response.start"
        assert blocked[0]["status"] == 403
        blocked_ws = await call({"type": "websocket", "client": ("192.0.2.11", 1234)})
        assert blocked_ws[0]["type"] == "websocket.close"
        await call({"type": "http", "client": ("::ffff:192.0.2.10", 1234)})
        await call({"type": "websocket", "client": ("2001:db8::10", 1234)})
        assert dispatched == ["http", "websocket"]

    asyncio.run(exercise())


def test_cli_still_rejects_unsupported_bind_addresses():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["serve", "--host", "example.invalid"])


def test_network_mode_accepts_lan_http_and_websocket_hosts_but_rejects_cross_origin(tmp_path):
    app = server.create_app(tmp_path, recover=False, allowed_hosts=["*"])
    origin = "http://192.168.50.2:8787"
    with TestClient(app, base_url=origin) as client:
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/state", headers={"Origin": origin}).status_code == 200
        assert client.get("/", headers={"Host": "swarm.lan:8787"}).status_code == 200
        assert (
            client.get("/api/state", headers={"Origin": "http://evil.example"}).status_code == 403
        )
        with client.websocket_connect(
            "ws://192.168.50.2:8787/ws/state", headers={"Origin": origin}
        ) as ws:
            assert ws.receive_json()["agents"] == []
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "ws://192.168.50.2:8787/ws/state", headers={"Origin": "http://evil.example"}
            ):
                pass


def test_default_app_still_rejects_lan_hosts(tmp_path):
    app = server.create_app(tmp_path, recover=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/state", headers={"Host": "192.168.50.2:8787"}).status_code == 400
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 400
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/state", headers={"Host": "192.168.50.2:8787"}):
                pass
