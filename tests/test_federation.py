import asyncio
import base64

import pytest

from swarm.federation import RPC_CHUNK, FederatedStream, FederationClient, FederationConfig


def config(**changes):
    values = {
        "url": "ws://127.0.0.1:9443/v1/connect",
        "node_id": "node-a",
        "name": "Node A",
        "api_key": "secret-0123456789-abcdefghijklmnop",
        "local_api_url": "http://127.0.0.1:8787",
    }
    values.update(changes)
    return FederationConfig(**values)


def test_federation_config_requires_tls_except_for_loopback():
    config().validate()
    with pytest.raises(ValueError, match="loopback"):
        config(url="ws://relay.example/v1/connect").validate()
    with pytest.raises(ValueError, match="node ID"):
        config(node_id="bad/id").validate()
    config(url="wss://relay.example/v1/connect").validate()


def test_remote_restart_is_denied_unless_controller_opted_in():
    async def exercise():
        client = FederationClient(config(), lambda: {})
        routed = []

        async def route(node_id, payload):
            routed.append((node_id, payload))

        client._route = route
        await client._serve_rpc(
            "node-b",
            {
                "request_id": "request-1",
                "method": "POST",
                "path": "/api/controller/restart",
                "query": "",
                "body": "",
            },
        )
        response = routed[0][1]
        assert response["type"] == "rpc.response"
        assert response["status"] == 403
        assert b"disabled" in base64.b64decode(response["body"])

    asyncio.run(exercise())


def test_rpc_response_is_chunked_and_reassembled_without_relay_frame_growth():
    async def exercise():
        sender = FederationClient(config(), lambda: {})
        frames = []

        async def capture(node_id, payload):
            assert node_id == "node-b"
            frames.append(payload)

        sender._route = capture
        content = b"x" * (RPC_CHUNK * 2 + 17)
        sender._local_request = lambda *args: {
            "status": 200,
            "content_type": "application/octet-stream",
            "content_disposition": None,
            "_body": content,
        }
        await sender._serve_rpc(
            "node-b",
            {
                "request_id": "request-2",
                "method": "GET",
                "path": "/api/agents/00000000-0000-0000-0000-000000000000/products/00000000-0000-0000-0000-000000000001/download",
                "query": "",
                "body": "",
            },
        )
        assert [frame["type"] for frame in frames] == [
            "rpc.response",
            "rpc.chunk",
            "rpc.chunk",
            "rpc.chunk",
            "rpc.end",
        ]

        receiver = FederationClient(config(node_id="node-b"), lambda: {})
        future = asyncio.get_running_loop().create_future()
        receiver.pending["request-2"] = future
        for frame in frames:
            await receiver._handle_route("node-a", frame)
        response = await future
        assert response["status"] == 200
        assert response["_body"] == content

    asyncio.run(exercise())


def test_encoded_or_recursive_remote_paths_are_rejected():
    for path in (
        "/api/federation/nodes",
        "/api/%66ederation/nodes",
        "/api/agents/../state",
        "/api//state",
    ):
        with pytest.raises(ValueError, match="forbidden"):
            FederationClient._validate_remote_path(path)


def test_slow_terminal_queue_closes_only_that_stream_without_blocking_receiver():
    async def exercise():
        client = FederationClient(config(), lambda: {})
        stream = FederatedStream(client, "node-b", "stream-1")
        client.streams[("node-b", "stream-1")] = stream
        for _ in range(stream.queue.maxsize):
            stream.queue.put_nowait(b"old")
        routed = []

        async def route(node_id, payload):
            routed.append((node_id, payload))

        client._route = route
        await asyncio.wait_for(
            client._deliver_stream_data(
                "node-b",
                {
                    "type": "stream.data",
                    "stream_id": "stream-1",
                    "data": base64.b64encode(b"new").decode(),
                },
            ),
            0.2,
        )
        await asyncio.sleep(0)
        assert stream.closed
        assert await stream.receive() is None
        assert ("node-b", "stream-1") not in client.streams
        await asyncio.gather(*client.inbound_tasks, return_exceptions=True)
        assert routed[0][1]["type"] == "stream.close"

    asyncio.run(exercise())


def test_federation_summaries_exclude_local_node_and_include_state_count():
    client = FederationClient(config(), lambda: {})
    client.nodes = {
        "node-a": {"id": "node-a", "name": "Local", "capabilities": {}},
        "node-b": {"id": "node-b", "name": "Remote", "capabilities": {}},
    }
    client.remote_states["node-b"] = {"agents": [{"id": "1"}, {"id": "2"}]}
    assert client.summaries() == [
        {
            "id": "node-b",
            "name": "Remote",
            "capabilities": {},
            "online": True,
            "agent_count": 2,
            "state_ready": True,
        }
    ]
