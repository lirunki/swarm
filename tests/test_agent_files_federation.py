import json
import socket
import urllib.error
import urllib.request

from test_federation_integration import running_server, wait_for_remote

from swarm.federation import RPC_CHUNK, FederationConfig
from swarm.files import FileRequest
from swarm.models import Agent
from swarm.relay import create_relay_app
from swarm.server import create_app


def get(url):
    try:
        response = urllib.request.urlopen(url, timeout=20)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, dict(response.headers), response.read()


def test_remote_reference_reads_current_bytes_in_chunks_only_on_open(tmp_path):
    key = "file-reference-mesh-test-0123456789-abcdefgh"
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        a, b = first.getsockname()[1], second.getsockname()[1]
    with running_server(create_relay_app([key])) as port:
        relay = f"ws://127.0.0.1:{port}/v1/connect"
        base_a, base_b = f"http://127.0.0.1:{a}", f"http://127.0.0.1:{b}"
        app_a = create_app(
            tmp_path / "a",
            recover=False,
            api_url=base_a,
            federation_config=FederationConfig(relay, "alpha", "Alpha", key, base_a),
        )
        app_b = create_app(
            tmp_path / "b",
            recover=False,
            api_url=base_b,
            federation_config=FederationConfig(relay, "beta", "Beta", key, base_b),
        )
        folder = tmp_path / "remote-work"
        folder.mkdir()
        source = folder / "résultat.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nold")
        agent = app_b.state.controller.store.create(
            Agent(
                name="Remote file producer", goal="Results", folder=str(folder), auto_resume=False
            )
        )
        entry = app_b.state.controller.files.add(agent.id, FileRequest(path=str(source)))["file"]
        assert len(list(app_b.state.controller.files.root.iterdir())) == 1
        assert not app_a.state.controller.files.root.exists()
        changed = b"\x89PNG\r\n\x1a\n" + b"\x00\xffnew bytes\r\n" * (RPC_CHUNK // 4)
        assert len(changed) > RPC_CHUNK * 2
        source.write_bytes(changed)
        with running_server(app_a, a), running_server(app_b, b):
            wait_for_remote(base_a, "beta")
            prefix = f"{base_a}/api/federation/beta/proxy/api/agents/{agent.id}/files"
            status, _, data = get(prefix)
            files = json.loads(data)["files"]
            assert status == 200 and len(files) == 1 and files[0]["available"]
            assert str(source).encode() not in data
            status, headers, data = get(prefix + f"/{entry['id']}/content")
            assert status == 200 and data == changed
            assert headers["content-type"] == "image/png"
            assert headers["content-disposition"].startswith("inline;")
            assert headers["cache-control"] == "no-store"
            assert "r%C3%A9sultat.png" in headers["content-disposition"]
            status, _, state = get(f"{base_a}/api/federation/beta/proxy/api/state")
            assert status == 200 and json.loads(state)["agents"][0]["file_count"] == 1
            assert str(source).encode() not in state
            source.unlink()
            status, _, data = get(prefix)
            assert not json.loads(data)["files"][0]["available"]
            assert get(prefix + f"/{entry['id']}/content")[0] == 410
            assert (
                not app_a.state.controller.files.root.exists()
            )  # No receiving-node payload cache.
