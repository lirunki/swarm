"""Real two-node federation coverage for destination-owned agent archives."""

import os
import socket
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from test_grouping_federation import (
    http,
    password_file,
    running_server,
    wait_for_authenticated_remote,
)

from swarm.federation import FederationConfig
from swarm.importers.pi_history import PiHistory
from swarm.models import Agent, Status
from swarm.processes import Processes
from swarm.relay import create_relay_app
from swarm.server import create_app


def reserve_ports(count):
    probes = [socket.socket() for _ in range(count)]
    try:
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        return [probe.getsockname()[1] for probe in probes]
    finally:
        for probe in probes:
            probe.close()


def login(base, password):
    status, body = http(base, "/api/auth/login", "POST", {"password": password})
    assert status == 200
    return {"Authorization": "Bearer " + body["token"]}


def wait_remote_job(base, prefix, request_id, headers):
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        status, last = http(base, f"{prefix}/api/archive-jobs/{request_id}", headers=headers)
        assert status == 200, last
        if last["status"] in {"completed", "failed"}:
            return last
        time.sleep(0.02)
    raise AssertionError(f"remote archive job did not finish: {last}")


def source_agent(app, workspace):
    # Random hex is safe fixture data but remains poorly compressible enough to exercise
    # chunked federation responses with a ZIP substantially larger than one MiB.
    payload = os.urandom(1_200_000).hex()
    history = PiHistory(str(uuid4()), str(workspace), "Federated archive source")
    for index in range(4):
        history.user(f"FEDERATED TURN {index} " + payload[index * 600_000 : (index + 1) * 600_000])
        history.historical("assistant", f"SAFE ANSWER {index}")
    source = app.state.controller.store.sessions_dir / "remote-large.jsonl"
    source.write_text(history.encode(), encoding="utf-8")
    assert source.stat().st_size > 2 * 1024 * 1024
    agent = app.state.controller.store.create(
        Agent(
            name="Remote archive worker",
            goal="Remain owned by node beta",
            folder=str(workspace),
            managed=True,
            auto_resume=False,
            status=Status.idle,
            session_id=history.entries[0]["id"],
            session_file=str(source),
        )
    )
    return agent, source


def test_remote_archive_download_resurrect_and_delete_stay_destination_owned(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Processes, "alive", lambda self, agent: False)
    monkeypatch.setattr(Processes, "health", lambda self: {"tmux": True, "pi": True})

    def no_launch(self, *args, **kwargs):
        raise AssertionError(f"archive/resurrection must not launch Pi: {args!r} {kwargs!r}")

    monkeypatch.setattr(Processes, "launch", no_launch)
    monkeypatch.setattr(Processes, "launch_setup", no_launch)
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    monkeypatch.setattr("swarm.sessions.running_pi_processes", lambda: [])

    key = "test-agent-archives-federation-key-0123456789"
    passwords = {"alpha": "alpha-agent-archives-password", "beta": "beta-agent-archives-password"}
    with ExitStack() as stack:
        relay_port = stack.enter_context(running_server(create_relay_app([key])))
        alpha_port, beta_port = reserve_ports(2)
        apps = {}
        for node, port in (("alpha", alpha_port), ("beta", beta_port)):
            root = tmp_path / node
            root.mkdir()
            base = f"http://127.0.0.1:{port}"
            apps[node] = create_app(
                root / "home",
                pi="/test-only/no-pi",
                recover=False,
                api_url=base,
                password_file=password_file(root, passwords[node]),
                federation_config=FederationConfig(
                    f"ws://127.0.0.1:{relay_port}/v1/connect",
                    node,
                    node.title(),
                    key,
                    base,
                ),
            )

        workspace = tmp_path / "beta-workspace"
        workspace.mkdir()
        agent, source = source_agent(apps["beta"], workspace)
        original_session = source.read_bytes()
        stack.enter_context(running_server(apps["alpha"], alpha_port))
        stack.enter_context(running_server(apps["beta"], beta_port))
        alpha_base = f"http://127.0.0.1:{alpha_port}"
        beta_base = f"http://127.0.0.1:{beta_port}"
        alpha_auth = login(alpha_base, passwords["alpha"])
        beta_auth = login(beta_base, passwords["beta"])
        both_auth = alpha_auth | {
            "X-Swarm-Authorization": beta_auth["Authorization"]
        }
        wait_for_authenticated_remote(alpha_base, "beta", alpha_auth)

        prefix = "/api/federation/beta/proxy"
        archive_endpoint = f"{prefix}/api/agents/{agent.id}/archive-and-forget"
        request_id = str(uuid4())
        request = {"include_files": False, "request_id": request_id}

        assert http(alpha_base, archive_endpoint, "POST", request)[0] == 401
        assert http(alpha_base, archive_endpoint, "POST", request, alpha_auth)[0] == 401
        alpha_service = {
            "Authorization": "Bearer " + apps["alpha"].state.access.service_token
        }
        beta_service = "Bearer " + apps["beta"].state.access.service_token
        assert http(alpha_base, "/api/archives", headers=alpha_service)[0] == 403
        # Federation rejects a service credential as non-browser authentication before
        # the owner-only endpoint's own (direct-service 403) check is reached.
        assert (
            http(
                alpha_base,
                archive_endpoint,
                "POST",
                request,
                alpha_auth | {"X-Swarm-Authorization": beta_service},
            )[0]
            == 401
        )

        status, accepted = http(
            alpha_base, archive_endpoint, "POST", request, both_auth
        )
        assert status == 202, accepted
        assert accepted["job"]["id"] == request_id
        assert accepted["job"]["status"] in {"queued", "running"}
        completed = wait_remote_job(alpha_base, prefix, request_id, both_auth)
        assert completed["status"] == "completed", completed
        archived = completed["archive"]
        archive_id = archived["id"]
        assert archived["agent_id"] == str(agent.id)
        assert not apps["beta"].state.controller.store.path(agent.id).exists()
        assert apps["alpha"].state.controller.store.scan() == []
        assert apps["alpha"].state.agent_archives.list()["archives"] == []

        status, listing = http(alpha_base, f"{prefix}/api/archives", headers=both_auth)
        assert status == 200
        assert [item["id"] for item in listing["archives"]] == [archive_id]
        zip_path = apps["beta"].state.controller.store.home / "archives" / f"{archive_id}.zip"
        assert zip_path.exists() and zip_path.stat().st_size > 1024 * 1024

        status, downloaded = http(
            alpha_base,
            f"{prefix}/api/archives/{archive_id}/download",
            headers=both_auth,
        )
        assert status == 200
        assert isinstance(downloaded, bytes) and downloaded.startswith(b"PK")
        assert downloaded == zip_path.read_bytes() and len(downloaded) > 1024 * 1024

        resurrection_id = str(uuid4())
        status, result = http(
            alpha_base,
            f"{prefix}/api/archives/{archive_id}/resurrect",
            "POST",
            {"request_id": resurrection_id},
            both_auth,
        )
        assert status == 200, result
        restored = apps["beta"].state.controller.store.get(agent.id)
        assert result["agent"]["id"] == str(agent.id)
        assert restored.folder == str(workspace) and Path(restored.folder).is_dir()
        assert restored.status == Status.idle and restored.auto_resume is False
        assert restored.pid is None and apps["beta"].state.controller.processes.alive(restored) is False
        assert Path(restored.session_file).read_bytes() == original_session
        assert apps["alpha"].state.controller.store.scan() == []

        status, deleted = http(
            alpha_base,
            f"{prefix}/api/archives/{archive_id}",
            "DELETE",
            headers=both_auth,
        )
        assert status == 200 and deleted == {"id": archive_id, "deleted": True}
        assert not zip_path.exists()
        assert Path(restored.session_file).read_bytes() == original_session
        assert apps["beta"].state.controller.store.get(agent.id).id == agent.id
        assert http(alpha_base, f"{prefix}/api/archives", headers=both_auth)[1]["archives"] == []
        assert http(beta_base, "/api/archives", headers=beta_auth)[1]["archives"] == []
