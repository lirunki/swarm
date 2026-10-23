import asyncio
import json
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from swarm.federation import FederationClient, FederationConfig
from swarm.relay import NODE_ID
from swarm.relay_settings import RelayConnectRequest, RelaySettings
from swarm.server import create_app
from swarm.store import Store

KEY = "test-relay-secret-0123456789-abcdefgh"


def payload(**changes):
    return {
        "relay_url": "wss://relay.example:9443/v1/connect",
        "relay_node_id": "office-a",
        "relay_name": "Office A",
        "relay_api_key": KEY,
        **changes,
    }


class FakeClient:
    def __init__(self, config, state_provider, access=None):
        config.validate()
        self.config, self.state_provider, self.access = config, state_provider, access
        self.ready, self.last_error, self.websocket = False, None, None
        self.starts = self.closes = 0

    def start(self):
        self.starts += 1
        self.ready, self.websocket = True, object()

    async def close(self):
        self.closes += 1
        self.ready, self.websocket = False, None

    def queue_state(self):
        pass

    def summaries(self):
        return []


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr("swarm.relay_settings.FederationClient", FakeClient)


def app_at(home, **kwargs):
    return create_app(home, recover=False, allowed_hosts=["testserver"], **kwargs)


def test_default_config_is_redacted_and_does_not_connect(tmp_path, fake):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/federation/settings")
        status = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert not status["enabled"] and not status["has_api_key"]
        assert status["status"] == "disconnected" and NODE_ID.fullmatch(status["relay_node_id"])
        assert app.state.federation is None
        assert not (tmp_path / "relay-settings.json").exists()


def test_connect_persists_protected_profile_without_returning_or_publishing_key(tmp_path, fake):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/federation/connect", json=payload())
        assert response.status_code == 202, response.text
        assert KEY not in response.text
        assert response.json()["has_api_key"] and response.json()["enabled"]
        assert app.state.federation.starts == 1
        for endpoint in ("/api/federation/settings", "/api/federation/nodes", "/api/state"):
            assert KEY not in client.get(endpoint).text
        profile = tmp_path / "relay-settings.json"
        assert json.loads(profile.read_text())["api_key"] == KEY
        assert json.loads(profile.read_text())["enabled"]
        if os.name != "nt":
            assert profile.stat().st_mode & 0o777 == 0o600
        assert KEY not in repr(app.state.federation.config)
        assert client.post("/api/federation/connect", json=payload()).status_code == 202
        assert app.state.federation.starts == 1  # Repeat Connect is idempotent.


def test_disconnect_is_durable_reuses_saved_key_and_keeps_same_runtime_client(tmp_path, fake):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        client.post("/api/federation/connect", json=payload())
        original = app.state.federation
        response = client.post("/api/federation/disconnect")
        assert response.status_code == 200
        assert not response.json()["enabled"] and response.json()["has_api_key"]
        assert client.get("/api/federation/nodes").json()["enabled"] is False
        assert json.loads((tmp_path / "relay-settings.json").read_text())["enabled"] is False
        assert original.closes == 1
        reconnect = client.post("/api/federation/connect", json=payload(relay_api_key=None))
        assert reconnect.status_code == 202
        assert app.state.federation is original and original.starts == 2
        client.post("/api/federation/disconnect")
    restarted = app_at(tmp_path)
    with TestClient(restarted) as client:
        status = client.get("/api/federation/settings").json()
        assert not status["enabled"] and status["has_api_key"]
        assert status["relay_name"] == "Office A"
        assert restarted.state.federation.starts == 0


def test_connected_profile_reconnects_on_restart_and_overrides_startup_defaults(tmp_path, fake):
    with TestClient(app_at(tmp_path)) as client:
        client.post("/api/federation/connect", json=payload())
    initial = FederationConfig(
        "wss://startup.example/v1/connect", "startup", "Startup", KEY, "http://127.0.0.1:8787"
    )
    app = app_at(tmp_path, federation_config=initial)
    with TestClient(app) as client:
        status = client.get("/api/federation/settings").json()
        assert status["relay_url"] == payload()["relay_url"]
        assert app.state.federation.starts == 1 and status["connected"]


@pytest.mark.parametrize(
    "changes",
    [
        {"relay_url": "https://relay.example/v1/connect"},
        {"relay_url": "ws://untrusted.example/v1/connect"},
        {"relay_url": "wss:///missing-host"},
        {"relay_url": "wss://user:password@relay.example/v1/connect"},
        {"relay_url": "wss://relay.example/v1/connect?key=private"},
        {"relay_url": "wss://relay.example:invalid/v1/connect"},
        {"relay_node_id": "invalid/node"},
        {"relay_name": " "},
        {"relay_api_key": "short-secret"},
        {"relay_api_key": KEY + "\r\nAuthorization: another"},
    ],
)
def test_bad_configuration_never_echoes_credentials_or_writes_profile(tmp_path, fake, changes):
    with TestClient(app_at(tmp_path)) as client:
        request = payload(**changes)
        result = client.post("/api/federation/connect", json=request)
        assert result.status_code == 400, result.text
        assert request["relay_api_key"] not in result.text
        assert not (tmp_path / "relay-settings.json").exists()


def test_validation_errors_do_not_echo_request_body_or_extra_secret_fields(tmp_path, fake):
    with TestClient(app_at(tmp_path)) as client:
        response = client.post(
            "/api/federation/connect", json=payload(relay_node_id={}, unexpected_key=KEY)
        )
        assert response.status_code == 422
        assert KEY not in response.text
        assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])


def test_saved_key_is_not_sent_to_a_different_url_without_explicit_reentry(tmp_path, fake):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        client.post("/api/federation/connect", json=payload())
        original = app.state.federation
        result = client.post(
            "/api/federation/connect",
            json=payload(relay_url="wss://other.example/v1/connect", relay_api_key=None),
        )
        assert result.status_code == 400 and "changing" in result.text
        assert original.closes == 0 and original.config.url == payload()["relay_url"]
        assert client.get("/api/federation/settings").json()["connected"]


def test_failed_save_preserves_current_live_connection(tmp_path, fake, monkeypatch):
    service = RelaySettings(Store(tmp_path), "http://127.0.0.1:8787", lambda: {})

    async def exercise():
        await service.connect(RelayConnectRequest(**payload()))
        original = service.client

        def fail(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(service, "_save", fail)
        with pytest.raises(OSError):
            await service.connect(RelayConnectRequest(**payload(relay_name="Different name")))
        assert service.client is original and original.closes == 0
        assert original.config.name == "Office A"
        await service.close()

    asyncio.run(exercise())


def test_corrupt_or_unsafe_profile_does_not_disclose_contents_or_autoconnect(tmp_path, fake):
    profile = tmp_path / "relay-settings.json"
    profile.write_text('{"private":"' + KEY)
    with TestClient(app_at(tmp_path)) as client:
        result = client.get("/api/federation/settings")
        assert KEY not in result.text and not result.json()["enabled"]
        assert "could not be loaded" in result.json()["last_error"]


def test_settings_cannot_be_proxied_or_changed_by_cross_origin_requests(tmp_path, fake):
    for path in (
        "/api/federation/settings",
        "/api/federation/connect",
        "/api/federation/disconnect",
    ):
        with pytest.raises(ValueError, match="forbidden"):
            FederationClient._validate_remote_path(path)
    with TestClient(app_at(tmp_path)) as client:
        assert (
            client.post(
                "/api/federation/connect",
                json=payload(),
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert not (tmp_path / "relay-settings.json").exists()


def test_connection_error_messages_and_config_repr_cannot_leak_key():
    error = RuntimeError("private server detail: " + KEY)
    assert KEY not in FederationClient._connection_error(error)
    config = FederationConfig(
        **{
            "url": payload()["relay_url"],
            "node_id": "node",
            "name": "Node",
            "api_key": KEY,
            "local_api_url": "http://localhost:8787",
        }
    )
    assert KEY not in repr(config)
    with pytest.raises(ValueError):
        replace(config, url="wss://host:invalid").validate()


def test_unsafe_profile_permissions_disable_saved_credentials(tmp_path, fake):
    if os.name == "nt":
        pytest.skip("Windows protects settings with user-profile ACLs")
    service = RelaySettings(Store(tmp_path), "http://localhost:8787", lambda: {})
    asyncio.run(service.connect(RelayConnectRequest(**payload())))
    service.path.chmod(0o644)
    reloaded = RelaySettings(Store(tmp_path), "http://localhost:8787", lambda: {})
    assert not reloaded.enabled and not reloaded.status()["has_api_key"]
    assert KEY not in json.dumps(reloaded.status())


def test_parallel_connect_requests_create_only_one_runtime_connection(tmp_path, fake):
    service = RelaySettings(Store(tmp_path), "http://localhost:8787", lambda: {})

    async def exercise():
        await asyncio.gather(*(service.connect(RelayConnectRequest(**payload())) for _ in range(4)))
        assert service.client.starts == 1
        await service.close()

    asyncio.run(exercise())


def test_relay_transport_debug_logging_is_disabled_before_sending_authorization(
    monkeypatch, caplog
):
    caplog.set_level("DEBUG")
    captured = []

    class FailedHandshake:
        async def __aenter__(self):
            raise RuntimeError(KEY)

        async def __aexit__(self, *args):
            pass

    def connect(*args, **kwargs):
        captured.append(kwargs)
        assert kwargs["logger"].disabled
        kwargs["logger"].debug("Authorization: Bearer %s", KEY)
        return FailedHandshake()

    monkeypatch.setattr("swarm.federation.connect", connect)

    async def exercise():
        client = FederationClient(
            FederationConfig(payload()["relay_url"], "node", "Node", KEY, "http://localhost:8787"),
            lambda: {},
        )
        client.start()
        for _ in range(100):
            if client.last_error:
                break
            await asyncio.sleep(0.001)
        assert client.last_error and KEY not in client.last_error
        await client.close()

    asyncio.run(exercise())
    assert captured and KEY not in caplog.text


def test_supported_unicode_configuration_roundtrips_within_profile_size_limit(tmp_path, fake):
    service = RelaySettings(Store(tmp_path), "http://localhost:8787", lambda: {})
    request = RelayConnectRequest(
        **payload(
            relay_url="wss://relay.example/" + "🙂" * 1800,
            relay_name="工作站",
            relay_api_key="x" * 4096,
        )
    )
    asyncio.run(service.connect(request))
    reloaded = RelaySettings(Store(tmp_path), "http://localhost:8787", lambda: {})
    assert reloaded.enabled and reloaded.status()["has_api_key"]
    assert reloaded.name == "工作站"
    assert service.path.stat().st_size < 16384
