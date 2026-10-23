import asyncio

import pytest

from swarm.federation import FederationClient, FederationConfig
from swarm.relay_settings import RelayConnectRequest, RelaySettings
from swarm.store import Store

KEY = "federation-auth-test-key-0123456789-abcdef"
TOKEN = "Bearer browser-session-token"


def config(**changes):
    values = {
        "url": "ws://127.0.0.1:9443/v1/connect",
        "node_id": "protected",
        "name": "Protected",
        "api_key": KEY,
        "local_api_url": "http://127.0.0.1:8787",
    }
    values.update(changes)
    return FederationConfig(**values)


class FakeAccess:
    required = True

    def __init__(self):
        self.tokens = {TOKEN}

    def authorize(self, authorization, service_allowed=False):
        assert service_allowed is False
        return authorization in self.tokens

    def public_state(self):
        return {"agents": [], "home": "", "health": {}, "errors": {}, "auth_required": True}


def test_protected_state_is_locked_until_authenticated_and_relocked_after_expiry():
    async def exercise():
        access = FakeAccess()
        full = {
            "agents": [{"id": "secret-agent"}],
            "home": "/secret/home",
            "health": {"pi": True},
            "errors": {"private": "detail"},
        }
        client = FederationClient(config(), lambda: full, access)
        client.websocket = object()
        client.nodes = {"peer": {"id": "peer", "capabilities": {}}}
        frames = []

        async def route(node_id, payload):
            frames.append((node_id, payload))

        client._route = route
        await client.publish_state()
        assert frames[-1][1]["state"] == {
            "agents": [],
            "home": "",
            "health": {},
            "errors": {},
            "auth_required": True,
        }

        client.authenticated_subscribers["peer"] = TOKEN
        await client.publish_state()
        assert frames[-1][1]["state"] == full

        access.tokens.clear()
        await client.publish_state()
        assert frames[-1][1]["state"]["auth_required"] is True
        assert frames[-1][1]["state"]["agents"] == []
        assert "peer" not in client.authenticated_subscribers

    asyncio.run(exercise())


def test_rpc_requires_browser_bearer_and_auth_check_registers_only_successful_sender():
    async def exercise():
        access = FakeAccess()
        client = FederationClient(config(), lambda: {"agents": [{"id": "private"}]}, access)
        routed = []
        local_calls = []

        async def route(node_id, payload):
            routed.append((node_id, payload))

        def local(method, path, query, body, authorization=""):
            local_calls.append((method, path, query, body, authorization))
            return {
                "status": 200,
                "content_type": "application/json",
                "content_disposition": None,
                "_body": b"{}",
            }

        client._route = route
        client._local_request = local
        request = {
            "type": "rpc.request",
            "request_id": "unauthorized",
            "method": "GET",
            "path": "/api/state",
            "query": "",
            "body": "",
            "authorization": "Bearer incorrect",
        }
        await client._serve_rpc("old-peer", request)
        assert routed[-1][1]["status"] == 401
        assert local_calls == []
        assert "old-peer" not in client.authenticated_subscribers

        request |= {
            "request_id": "authorized",
            "path": "/api/auth/check",
            "authorization": TOKEN,
        }
        await client._serve_rpc("browser-gateway", request)
        assert routed[-1][1]["status"] == 200
        assert local_calls[-1][-1] == TOKEN
        assert client.authenticated_subscribers == {"browser-gateway": TOKEN}

    asyncio.run(exercise())


def test_outbound_rpc_and_terminal_put_authorization_in_payload_not_query():
    async def exercise():
        client = FederationClient(config(node_id="gateway"), lambda: {})
        frames = []

        async def route(node_id, payload):
            frames.append(payload)
            pending_id = payload.get("request_id") or payload.get("stream_id")
            future = client.pending[pending_id]
            if payload["type"] == "rpc.request":
                future.set_result({"status": 200, "_body": b"{}"})
            else:
                future.set_result(True)

        client._route = route
        await client.request("protected", "GET", "/api/auth/check", "", b"", TOKEN)
        stream = await client.open_terminal("protected", "agent", 80, 24, TOKEN)
        assert frames[0]["authorization"] == TOKEN and frames[0]["query"] == ""
        assert frames[1]["authorization"] == TOKEN
        # Avoid routing a close frame after the synthetic stream-open test.
        client.streams.pop(("protected", stream.stream_id))

        with pytest.raises(ValueError, match="credentials"):
            await client.request(
                "protected", "GET", "/api/state", "authorization=Bearer+raw", b"", TOKEN
            )

    asyncio.run(exercise())


def test_receiving_locked_state_replaces_previous_secret_cache_and_hides_count():
    async def exercise():
        client = FederationClient(config(node_id="gateway"), lambda: {})
        client.nodes = {
            "protected": {
                "id": "protected",
                "name": "Protected",
                "capabilities": {"password_required": True},
            }
        }
        client.remote_states["protected"] = {"agents": [{"id": "secret"}], "home": "/private"}
        await client._handle_route(
            "protected",
            {
                "type": "state",
                "state": {
                    "agents": [],
                    "home": "",
                    "health": {},
                    "errors": {},
                    "auth_required": True,
                },
            },
        )
        assert client.remote_states["protected"]["home"] == ""
        assert client.summaries()[0]["agent_count"] is None

    asyncio.run(exercise())


def test_local_request_forwards_bearer_header(monkeypatch):
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, size):
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return Response()

    monkeypatch.setattr("swarm.federation.urllib.request.build_opener", lambda *args: Opener())
    client = FederationClient(config(), lambda: {}, FakeAccess())
    assert client._local_request("GET", "/api/auth/check", "", b"", TOKEN)["status"] == 200
    assert captured["authorization"] == TOKEN


def test_relay_settings_preserves_same_access_for_initial_and_future_clients(tmp_path, monkeypatch):
    made = []

    class FakeClient:
        def __init__(self, client_config, state_provider, access=None):
            self.config = client_config
            self.access = access
            self.ready = False
            self.last_error = None
            made.append(self)

        async def close(self):
            pass

        def start(self):
            pass

    monkeypatch.setattr("swarm.relay_settings.FederationClient", FakeClient)
    access = FakeAccess()
    service = RelaySettings(Store(tmp_path), "http://127.0.0.1:8787", lambda: {}, access=access)

    async def exercise():
        await service.connect(
            RelayConnectRequest(
                relay_url="wss://relay.example/v1/connect",
                relay_node_id="node-a",
                relay_name="Node A",
                relay_api_key=KEY,
            )
        )
        assert made[-1].access is access
        await service.disconnect()
        await service.connect(
            RelayConnectRequest(
                relay_url="wss://relay.example/v1/connect",
                relay_node_id="node-a",
                relay_name="Node A",
                relay_api_key=None,
            )
        )
        assert service.client.access is access

    asyncio.run(exercise())
