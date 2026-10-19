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


def test_remote_upload_and_insert_target_only_the_selected_agent_machine(tmp_path):
    import base64
    from pathlib import Path
    from uuid import uuid4

    from test_file_uploads import FakeProcesses

    from swarm.uploads import CHUNK_BYTES

    def post(url, payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    key = "file-upload-mesh-test-0123456789-abcdefgh"
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        a, b = first.getsockname()[1], second.getsockname()[1]
    with running_server(create_relay_app([key])) as port:
        relay = f"ws://127.0.0.1:{port}/v1/connect"
        base_a, base_b = f"http://127.0.0.1:{a}", f"http://127.0.0.1:{b}"
        app_a = create_app(
            tmp_path / "upload-a",
            recover=False,
            api_url=base_a,
            federation_config=FederationConfig(relay, "alpha", "Alpha", key, base_a),
        )
        app_b = create_app(
            tmp_path / "upload-b",
            recover=False,
            api_url=base_b,
            federation_config=FederationConfig(relay, "beta", "Beta", key, base_b),
        )
        app_a.state.controller.processes = FakeProcesses()
        app_b.state.controller.processes = FakeProcesses()
        app_a.state.uploads.temp_root = tmp_path / "tmp-alpha"
        app_b.state.uploads.temp_root = tmp_path / "tmp-beta"
        agent = app_b.state.controller.store.create(
            Agent(
                name="Remote reader",
                goal="Read uploaded file",
                folder=str(tmp_path),
                managed=True,
                auto_resume=False,
            )
        )
        content = b"\x00\xffRemote upload bytes\r\n" * (CHUNK_BYTES // 3)
        with running_server(app_a, a), running_server(app_b, b):
            wait_for_remote(base_a, "beta")
            prefix = f"{base_a}/api/federation/beta/proxy/api/agents/{agent.id}/files"
            upload_id = str(uuid4())
            post(
                prefix + "/uploads",
                {"upload_id": upload_id, "filename": "input notes.bin", "size": len(content)},
            )
            for offset in range(0, len(content), CHUNK_BYTES):
                chunk = content[offset : offset + CHUNK_BYTES]
                post(
                    prefix + f"/uploads/{upload_id}/chunks",
                    {"offset": offset, "data": base64.b64encode(chunk).decode()},
                )
            result = post(prefix + f"/uploads/{upload_id}/finish", {})
            assert not app_b.state.controller.processes.pastes
            file_id = result["file"]["id"]
            assert get(prefix + f"/{file_id}/content?download=true")[2] == content
            inserted = post(prefix + f"/{file_id}/insert", {})
            assert Path(inserted["path"]).is_relative_to(app_b.state.uploads.temp_root)
            assert Path(inserted["path"]).read_bytes() == content
            assert app_b.state.controller.processes.pastes == [(str(agent.id), inserted["text"])]
            assert not app_a.state.controller.processes.pastes
            assert not app_a.state.uploads.temp_root.exists()
            assert not app_a.state.controller.files.root.exists()
