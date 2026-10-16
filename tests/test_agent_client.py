"""Internal client TLS support for the HTTPS dashboard (loopback-only)."""

from __future__ import annotations

import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.request import HTTPSHandler

import pytest

from swarm.agent_client import AgentClient, validate_api_url


def test_validate_api_url_accepts_https_loopback():
    assert validate_api_url("https://127.0.0.1:8787") == "https://127.0.0.1:8787"
    assert validate_api_url("https://[::1]:8787/") == "https://[::1]:8787"
    assert validate_api_url("https://localhost:8787") == "https://localhost:8787"


def test_validate_api_url_still_rejects_non_loopback_https():
    with pytest.raises(ValueError, match="loopback"):
        validate_api_url("https://example.com:8787")
    with pytest.raises(ValueError, match="loopback"):
        validate_api_url("https://192.168.1.1:8787")


def test_validate_api_url_rejects_non_http_schemes():
    with pytest.raises(ValueError):
        validate_api_url("ftp://127.0.0.1:8787")
    with pytest.raises(ValueError):
        validate_api_url("file:///tmp/x")


def test_https_client_registers_https_handler_with_ca_file(monkeypatch, tmp_path):
    ca = tmp_path / "controller-ca.pem"
    ca.write_text("dummy-ca")
    monkeypatch.setenv("SWARM_API_CA_FILE", str(ca))

    contexts: list[SimpleNamespace] = []

    def fake_create_default_context() -> SimpleNamespace:
        ctx = SimpleNamespace(
            verify_mode=ssl.CERT_REQUIRED,
            load_verify_locations=MagicMock(),
        )
        contexts.append(ctx)
        return ctx

    monkeypatch.setattr("swarm.agent_client.ssl.create_default_context", fake_create_default_context)

    handlers: list = []
    monkeypatch.setattr(
        "swarm.agent_client.build_opener", lambda *hs: handlers.extend(hs) or object()
    )

    AgentClient("https://127.0.0.1:8787")

    assert len(contexts) == 1
    contexts[0].load_verify_locations.assert_called_once_with(cafile=str(ca))
    https_handlers = [h for h in handlers if isinstance(h, HTTPSHandler)]
    assert len(https_handlers) == 1
    assert https_handlers[0]._context is contexts[0]


def test_https_client_keeps_verification_without_ca_file(monkeypatch):
    monkeypatch.delenv("SWARM_API_CA_FILE", raising=False)

    contexts: list[SimpleNamespace] = []

    def fake_create_default_context() -> SimpleNamespace:
        ctx = SimpleNamespace(
            verify_mode=ssl.CERT_REQUIRED,
            load_verify_locations=MagicMock(),
        )
        contexts.append(ctx)
        return ctx

    monkeypatch.setattr("swarm.agent_client.ssl.create_default_context", fake_create_default_context)
    monkeypatch.setattr("swarm.agent_client.build_opener", lambda *hs: object())

    AgentClient("https://localhost:8787")

    assert contexts and contexts[0].verify_mode == ssl.CERT_REQUIRED
    contexts[0].load_verify_locations.assert_not_called()


def test_http_client_does_not_register_https_handler(monkeypatch):
    handlers: list = []
    monkeypatch.setattr(
        "swarm.agent_client.build_opener", lambda *hs: handlers.extend(hs) or object()
    )
    AgentClient("http://127.0.0.1:8787")
    assert not any(isinstance(h, HTTPSHandler) for h in handlers)


# ---------------------------------------------------------------------------
# Federation local TLS (loopback dashboard only; remote relay unaffected)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from swarm.federation import FederationClient, FederationConfig, _local_ssl_context  # noqa: E402


def _fed_config(**changes):
    values = {
        "url": "wss://relay.example/v1/connect",
        "node_id": "node-a",
        "name": "Node A",
        "api_key": "secret-0123456789-abcdefghijklmnop",
        "local_api_url": "http://127.0.0.1:8787",
    }
    values.update(changes)
    return FederationConfig(**values)


def test_local_ssl_context_loads_ca_file_without_disabling_verification(monkeypatch, tmp_path):
    ca = tmp_path / "controller-ca.pem"
    monkeypatch.setenv("SWARM_API_CA_FILE", str(ca))

    contexts = []

    def fake_create_default_context():
        ctx = SimpleNamespace(verify_mode=ssl.CERT_REQUIRED, load_verify_locations=MagicMock())
        contexts.append(ctx)
        return ctx

    monkeypatch.setattr("swarm.federation.ssl.create_default_context", fake_create_default_context)
    ctx = _local_ssl_context()
    assert ctx is contexts[0]
    assert ctx.verify_mode == ssl.CERT_REQUIRED  # verification stays enabled
    ctx.load_verify_locations.assert_called_once_with(cafile=str(ca))


def test_local_request_uses_https_handler_for_https_local_url(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_API_CA_FILE", str(tmp_path / "ca.pem"))

    sentinel = SimpleNamespace(load_verify_locations=MagicMock(), verify_mode=ssl.CERT_REQUIRED)
    monkeypatch.setattr("swarm.federation.ssl.create_default_context", lambda: sentinel)

    captured = {}

    def fake_https_handler(context=None):
        captured["context"] = context
        return SimpleNamespace()

    response = SimpleNamespace(
        status=200,
        headers={"Content-Type": "application/json"},
        read=lambda n: b"{}",
    )
    opener = SimpleNamespace(open=lambda request, timeout: response)

    monkeypatch.setattr("swarm.federation.urllib.request.HTTPSHandler", fake_https_handler)
    monkeypatch.setattr("swarm.federation.urllib.request.build_opener", lambda *hs: opener)

    client = FederationClient(_fed_config(local_api_url="https://127.0.0.1:8787"), lambda: {})
    result = client._local_request("GET", "/api/state", "", b"")

    assert result["status"] == 200
    assert captured["context"] is not None  # HTTPS handler wired for local https


def test_local_request_skips_https_handler_for_http_local_url(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_API_CA_FILE", str(tmp_path / "ca.pem"))

    called = []

    def fake_https_handler(context=None):
        called.append(context)
        return SimpleNamespace()

    response = SimpleNamespace(
        status=200,
        headers={"Content-Type": "application/json"},
        read=lambda n: b"{}",
    )
    opener = SimpleNamespace(open=lambda request, timeout: response)

    monkeypatch.setattr("swarm.federation.urllib.request.HTTPSHandler", fake_https_handler)
    monkeypatch.setattr("swarm.federation.urllib.request.build_opener", lambda *hs: opener)

    client = FederationClient(_fed_config(local_api_url="http://127.0.0.1:8787"), lambda: {})
    client._local_request("GET", "/api/state", "", b"")
    assert called == []  # no TLS handler for plain loopback http


class _FakeLocalWS:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_serve_terminal_passes_local_ca_context_for_wss(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_API_CA_FILE", str(tmp_path / "ca.pem"))

    sentinel = SimpleNamespace(load_verify_locations=MagicMock(), verify_mode=ssl.CERT_REQUIRED)
    monkeypatch.setattr("swarm.federation.ssl.create_default_context", lambda: sentinel)

    captured = {}

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["ssl"] = kwargs.get("ssl")
        return _FakeLocalWS()

    monkeypatch.setattr("swarm.federation.connect", fake_connect)

    client = FederationClient(_fed_config(local_api_url="https://127.0.0.1:8787"), lambda: {})
    routed = []

    async def route(node_id, payload):
        routed.append(payload)

    client._route = route

    async def exercise():
        await client._serve_terminal(
            "node-b",
            {"stream_id": "s1", "agent_id": "a1", "cols": 80, "rows": 24},
        )

    asyncio.run(asyncio.wait_for(exercise(), 2))
    assert captured["url"].startswith("wss://127.0.0.1:8787/ws/terminal/a1")
    assert captured["ssl"] is sentinel  # local CA context used for the local wss socket


def test_serve_terminal_omits_ssl_for_ws_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_API_CA_FILE", str(tmp_path / "ca.pem"))

    captured = {}

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["ssl"] = kwargs.get("ssl", "MISSING")
        return _FakeLocalWS()

    monkeypatch.setattr("swarm.federation.connect", fake_connect)

    client = FederationClient(_fed_config(local_api_url="http://127.0.0.1:8787"), lambda: {})
    routed = []

    async def route(node_id, payload):
        routed.append(payload)

    client._route = route

    async def exercise():
        await client._serve_terminal(
            "node-b",
            {"stream_id": "s2", "agent_id": "a2", "cols": 80, "rows": 24},
        )

    asyncio.run(asyncio.wait_for(exercise(), 2))
    assert captured["url"].startswith("ws://127.0.0.1:8787/ws/terminal/a2")
    assert captured["ssl"] is None


def test_relay_connection_is_not_given_local_ssl_context(monkeypatch, tmp_path):
    """The remote relay wss:// connection must keep using its default TLS handling."""
    monkeypatch.setenv("SWARM_API_CA_FILE", str(tmp_path / "ca.pem"))

    captured = {}
    client_holder = {}

    class _FakeRelayWS:
        async def send(self, message):
            pass

        async def __aenter__(self):
            client_holder["client"].stopping = True  # exit the reconnect loop after one try
            return self

        async def __aexit__(self, *args):
            return None

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["ssl"] = kwargs.get("ssl", "MISSING")
        return _FakeRelayWS()

    monkeypatch.setattr("swarm.federation.connect", fake_connect)

    client = FederationClient(_fed_config(url="wss://relay.example/v1/connect"), lambda: {})
    client_holder["client"] = client

    async def noop_receive(websocket):
        return None

    client._receive_loop = noop_receive

    async def exercise():
        await client._run()

    asyncio.run(asyncio.wait_for(exercise(), 2))
    assert captured["url"] == "wss://relay.example/v1/connect"
    assert captured["ssl"] == "MISSING"  # local CA context never reaches the relay