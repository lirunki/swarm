import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from swarm.relay import create_relay_app


def receive_type(websocket, expected):
    for _ in range(6):
        message = websocket.receive_json()
        if message["type"] == expected:
            return message
    raise AssertionError(f"did not receive {expected}")


def hello(node_id, name=None, restart=False):
    return {
        "type": "hello",
        "protocol": 1,
        "node_id": node_id,
        "name": name or node_id,
        "capabilities": {"remote_restart": restart},
    }


def test_relay_authenticates_two_rotation_keys_and_routes_between_nodes():
    app = create_relay_app(
        ["current-secret-0123456789-abcdefghij", "next-secret-0123456789-abcdefghijklm"]
    )
    with TestClient(app) as client:
        assert client.get("/health").json()["connected_nodes"] == 0
        with client.websocket_connect(
            "/v1/connect", headers={"Authorization": "Bearer current-secret-0123456789-abcdefghij"}
        ) as first:
            first.send_json(hello("alpha", "Alpha"))
            receive_type(first, "welcome")
            with client.websocket_connect(
                "/v1/connect",
                headers={"Authorization": "Bearer next-secret-0123456789-abcdefghijklm"},
            ) as second:
                second.send_json(hello("beta", "Beta", restart=True))
                receive_type(second, "welcome")
                presence = receive_type(first, "presence")
                assert {node["id"] for node in presence["nodes"]} == {"alpha", "beta"}
                first.send_json(
                    {
                        "type": "route",
                        "to": "beta",
                        "payload": {"type": "state", "state": {"agents": []}},
                    }
                )
                routed = receive_type(second, "route")
                assert routed["from"] == "alpha"
                assert routed["payload"]["state"] == {"agents": []}
            presence = receive_type(first, "presence")
            assert [node["id"] for node in presence["nodes"]] == ["alpha"]


def test_relay_rejects_bad_key_duplicate_identity_and_offline_route():
    app = create_relay_app(["secret-0123456789-abcdefghijklmnop"])
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/connect", headers={"Authorization": "Bearer wrong"}):
                pass
        with client.websocket_connect(
            "/v1/connect", headers={"Authorization": "Bearer secret-0123456789-abcdefghijklmnop"}
        ) as first:
            first.send_json(hello("alpha"))
            receive_type(first, "welcome")
            with client.websocket_connect(
                "/v1/connect",
                headers={"Authorization": "Bearer secret-0123456789-abcdefghijklmnop"},
            ) as duplicate:
                duplicate.send_json(hello("alpha"))
                with pytest.raises(WebSocketDisconnect):
                    while True:
                        duplicate.receive_json()
            first.send_json({"type": "route", "to": "missing", "payload": {"request_id": "r"}})
            error = receive_type(first, "route_error")
            assert error["request_id"] == "r"
            assert "offline" in error["detail"]
