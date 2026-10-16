import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI, WebSocket

from swarm.federation import FederationClient, FederationConfig
from swarm.relay import create_relay_app
from swarm.server import create_app


@contextmanager
def running_server(app, port=0):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", ws_max_size=1_048_576)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("test server did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def read_json(url, method="GET"):
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    return response.status, json.loads(response.read())


def wait_for_remote(base_url, node_id):
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        status, last = read_json(base_url + "/api/federation/nodes")
        if status == 200 and any(
            node["id"] == node_id and node["state_ready"] for node in last["nodes"]
        ):
            return last
        time.sleep(0.05)
    raise AssertionError(f"remote node did not become ready: {last}")


def test_two_swarm_backends_exchange_state_and_proxy_api_through_relay(tmp_path):
    with running_server(create_relay_app(["mesh-key-0123456789-abcdefghijklmn"])) as relay_port:
        relay_url = f"ws://127.0.0.1:{relay_port}/v1/connect"
        # Reserve stable local ports before creating each app, because connectors
        # need their own loopback API origin in the handshake lifecycle.
        probe_a, probe_b = socket.socket(), socket.socket()
        probe_a.bind(("127.0.0.1", 0))
        probe_b.bind(("127.0.0.1", 0))
        port_a, port_b = probe_a.getsockname()[1], probe_b.getsockname()[1]
        probe_a.close()
        probe_b.close()
        app_a = create_app(
            tmp_path / "a",
            pi="/bin/echo",
            recover=False,
            api_url=f"http://127.0.0.1:{port_a}",
            federation_config=FederationConfig(
                relay_url,
                "alpha",
                "Alpha",
                "mesh-key-0123456789-abcdefghijklmn",
                f"http://127.0.0.1:{port_a}",
            ),
        )
        app_b = create_app(
            tmp_path / "b",
            pi="/bin/echo",
            recover=False,
            api_url=f"http://127.0.0.1:{port_b}",
            federation_config=FederationConfig(
                relay_url,
                "beta",
                "Beta",
                "mesh-key-0123456789-abcdefghijklmn",
                f"http://127.0.0.1:{port_b}",
            ),
        )
        with running_server(app_a, port_a) as actual_a, running_server(app_b, port_b) as actual_b:
            assert (actual_a, actual_b) == (port_a, port_b)
            base_b = f"http://127.0.0.1:{port_b}"
            nodes = wait_for_remote(base_b, "alpha")
            alpha = next(node for node in nodes["nodes"] if node["id"] == "alpha")
            assert alpha["name"] == "Alpha"
            assert alpha["agent_count"] == 0

            status, state = read_json(base_b + "/api/federation/alpha/proxy/api/state")
            assert status == 200
            assert state["agents"] == []
            assert state["health"] == {"tmux": True, "pi": True}

            status, error = read_json(
                base_b + "/api/federation/alpha/proxy/api/controller/restart",
                method="POST",
            )
            assert status == 403
            assert "disabled" in error["detail"]


def test_terminal_bytes_are_multiplexed_bidirectionally_through_relay():
    terminal_app = FastAPI()

    @terminal_app.websocket("/ws/terminal/{agent_id}")
    async def terminal(websocket: WebSocket, agent_id: str):
        await websocket.accept()
        await websocket.send_bytes(f"ready:{agent_id}".encode())
        message = await websocket.receive_text()
        await websocket.send_bytes(("echo:" + message).encode())
        await websocket.close()

    with running_server(create_relay_app(["mesh-key-0123456789-abcdefghijklmn"])) as relay_port:
        with running_server(terminal_app) as terminal_port:
            relay_url = f"ws://127.0.0.1:{relay_port}/v1/connect"

            async def exercise():
                alpha = FederationClient(
                    FederationConfig(
                        relay_url,
                        "alpha",
                        "Alpha",
                        "mesh-key-0123456789-abcdefghijklmn",
                        "http://127.0.0.1:1",
                    ),
                    lambda: {"agents": []},
                )
                beta = FederationClient(
                    FederationConfig(
                        relay_url,
                        "beta",
                        "Beta",
                        "mesh-key-0123456789-abcdefghijklmn",
                        f"http://127.0.0.1:{terminal_port}",
                    ),
                    lambda: {"agents": []},
                )
                alpha.start()
                beta.start()
                try:
                    deadline = asyncio.get_running_loop().time() + 10
                    while not any(node["id"] == "beta" for node in alpha.summaries()):
                        if asyncio.get_running_loop().time() > deadline:
                            raise AssertionError("federation peers did not connect")
                        await asyncio.sleep(0.02)
                    stream = await alpha.open_terminal("beta", "agent-7", 120, 34)
                    assert await asyncio.wait_for(stream.receive(), 5) == b"ready:agent-7"
                    await stream.send_text('{"type":"input","data":"hello"}')
                    assert await asyncio.wait_for(stream.receive(), 5) == (
                        b'echo:{"type":"input","data":"hello"}'
                    )
                    await stream.close()
                finally:
                    await alpha.close()
                    await beta.close()

            asyncio.run(exercise())
