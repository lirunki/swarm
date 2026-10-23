from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from test_fleet_restarts import FakeProcesses, heartbeat
from test_password_relay import http
from test_password_relay import pair as pair

from swarm.importers.pi_history import PiHistory
from swarm.messaging import prepare_bridge
from swarm.models import Agent


def test_remote_fleet_mutations_require_explicit_remote_restart_opt_in(pair):
    app_a, app_b, base, headers = pair
    ctl = app_b.state.controller
    ctl.processes = FakeProcesses(ctl.store)
    id = uuid4()
    session_id = str(uuid4())
    path = ctl.store.sessions_dir / f"{id}.jsonl"
    path.write_text(PiHistory(session_id, str(ctl.store.home), "Remote").encode())
    agent = ctl.store.create(
        Agent(
            id=id,
            name="Remote restart target",
            goal="No model work",
            folder=str(ctl.store.home),
            managed=True,
            status="dormant",
            auto_resume=False,
            session_id=session_id,
            session_file=str(path),
        )
    )
    ctl.processes.next_pid += 1
    ctl.processes.live[str(id)] = ctl.processes.next_pid
    directory, _ = prepare_bridge(ctl.store, agent)
    heartbeat(ctl.store.get(id), directory)
    prefix = "/api/federation/beta/proxy"
    _, login = http(
        base,
        prefix + "/api/auth/login",
        "POST",
        {"password": "beta-separate-test-password"},
        headers,
    )
    auth = headers | {"X-Swarm-Authorization": "Bearer " + login["token"]}
    endpoint = prefix + "/api/fleet/restarts"
    status, preview = http(base, endpoint + "/preview", headers=auth)
    assert status == 200 and preview["targets"][0]["id"] == str(id)
    request = {
        "request_id": str(uuid4()),
        "confirmed": True,
        "targets": [{"id": t["id"], "token": t["token"]} for t in preview["targets"]],
    }
    assert http(base, endpoint, "POST", request, auth)[0] == 403
    for action in ("force", "legacy", "cancel"):
        assert (
            http(
                base,
                endpoint + "/" + request["request_id"] + "/" + action,
                "POST",
                {"confirmed": True, "agent_ids": [str(id)]},
                auth,
            )[0]
            == 403
        )
    assert not ctl.restarts.jobs() and not ctl.processes.kills
    # Simulate the configured opt-in; target authority, not gateway cache, enforces it.
    app_b.state.federation.config = replace(
        app_b.state.federation.config, allow_remote_restart=True
    )
    status, result = http(base, endpoint, "POST", request, auth)
    assert status == 202, result
    job_id = result["job"]["id"]
    assert http(base, endpoint + "/" + job_id, headers=auth)[0] == 200
    assert (
        http(
            base,
            endpoint + "/" + job_id + "/cancel",
            "POST",
            {"confirmed": True, "agent_ids": [str(id)]},
            auth,
        )[0]
        == 200
    )
    assert ctl.processes.alive(agent) and not ctl.processes.kills and not ctl.processes.prompts
    assert not Path(directory / "lifecycle.json").exists()
