import json
import re
import shlex
import shutil
import sys
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from swarm.models import Agent
from swarm.paths import agent_path
from swarm.server import create_app


def make_fake_pi(tmp_path, source):
    # A venv path may contain spaces. POSIX shebangs cannot quote interpreter
    # paths, so launch the Python fixture through a small, quoted shell wrapper.
    script = tmp_path / "fake-pi.py"
    script.write_text(source)
    fake = tmp_path / "fake-pi"
    fake.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n')
    fake.chmod(0o700)
    return fake


def make_app(tmp_path, **kwargs):
    return create_app(tmp_path / "swarm", recover=False, allowed_hosts=["testserver"], **kwargs)


def test_state_static_security_and_validation(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        script = client.get("/static/app.js")
        assert script.status_code == 200
        # Catch mismatched static selectors while browser validation is optional.
        ids = re.findall(r'\bid="([\w-]+)"', page.text)
        assert len(ids) == len(set(ids))
        referenced = set(re.findall(r"""\$\(['"]#([\w-]+)['"]""", script.text))
        assert referenced.issubset(ids), referenced - set(ids)
        assert 'id="import-grok-button"' in page.text
        assert 'id="import-grok-form"' in page.text
        assert client.get("/static/vendor/xterm.js").status_code == 200
        state = client.get("/api/state").json()
        assert state["agents"] == []
        assert (
            client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
        )
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 400
        assert client.post("/api/agents", json={"name": "Missing fields"}).status_code == 422
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/state", headers={"Origin": "https://evil.example"}):
                pass
        with client.websocket_connect("/ws/state") as ws:
            assert ws.receive_json()["agents"] == []


def test_repeated_state_websocket_close_finishes_child_cleanup(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        for _ in range(20):
            with client.websocket_connect("/ws/state") as ws:
                assert ws.receive_json()["agents"] == []


def test_file_changes_push_over_websocket_and_bad_records_reported(tmp_path):
    app = make_app(tmp_path)
    store = app.state.controller.store
    with TestClient(app) as client, client.websocket_connect("/ws/state") as ws:
        ws.receive_json()
        agent = store.create(Agent(name="External", goal="Track PR", folder=str(tmp_path)))
        assert ws.receive_json()["agents"][0]["name"] == "External"
        store.path(agent.id).write_text("partial edit")
        changed = ws.receive_json()
        assert changed["agents"][0]["name"] == "External"
        assert changed["errors"]


def test_adopt_endpoint_stays_offline_and_immutable_fields_are_protected(tmp_path, monkeypatch):
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    root = tmp_path / "pi"
    root.mkdir()
    path = root / "session.jsonl"
    session_id = str(uuid4())
    path.write_text(
        json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": str(tmp_path)}) + "\n"
    )
    app = make_app(tmp_path, session_roots=[root])
    with TestClient(app) as client:
        assert client.get("/api/pi/sessions").json()["sessions"][0]["available"]
        response = client.post(
            "/api/agents/adopt", json={"session_file": str(path), "goal": "Finish research"}
        )
        assert response.status_code == 201, response.text
        agent = response.json()
        assert agent["session_id"] == session_id and not agent["alive"] and not agent["auto_resume"]
        assert client.post("/api/agents/adopt", json={"session_file": str(path)}).status_code == 400
        assert client.patch(f"/api/agents/{agent['id']}", json={"pid": 1}).status_code == 422
        assert (
            client.patch(f"/api/agents/{agent['id']}", json={"status": "done"}).status_code == 200
        )


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is required")
def test_real_terminal_detach_and_server_restart_preserve_agent(tmp_path):
    # A real interactive process, but no model calls or user credentials involved.
    fake = make_fake_pi(
        tmp_path,
        """
import json, os, sys, uuid
from pathlib import Path
from swarm.paths import local_path
path = local_path(sys.argv[sys.argv.index("--session") + 1])
with (Path(os.environ["SWARM_HOME"]) / "launch-log.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv) + "\\n")
if not path.exists():
    path.write_text(json.dumps({"type":"session", "version":3, "id":str(uuid.uuid4()), "cwd":os.getcwd()}) + "\\n")
print("TERMINAL-READY", flush=True)
for line in sys.stdin:
    print("REPLY:" + line.rstrip(), flush=True)
""",
    )
    home = tmp_path / "swarm"
    app = create_app(home, pi=str(fake), recover=False, allowed_hosts=["testserver"])
    proc = app.state.controller.processes
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agents",
                json={"name": "Live", "goal": "Test terminals", "folder": str(tmp_path)},
            )
            assert response.status_code == 201, response.text
            record = response.json()
            agent_id = record["id"]
            assert not record["last_error"], record
            agent = app.state.controller.store.get(agent_id)
            assert proc.alive(agent)
            with client.websocket_connect(f"/ws/terminal/{agent_id}?cols=100&rows=25") as ws:
                data = b""
                for _ in range(25):
                    data += ws.receive_bytes()
                    if b"TERMINAL-READY" in data:
                        break
                assert b"TERMINAL-READY" in data, data
                ws.send_json({"type": "input", "data": "hello from browser\r"})
                for _ in range(25):
                    data += ws.receive_bytes()
                    if b"REPLY:hello from browser" in data:
                        break
                assert b"REPLY:hello from browser" in data
                ws.send_json({"type": "resize", "cols": 90, "rows": 22})
            assert proc.alive(agent), "closing a tab must only detach"
        assert proc.alive(agent), "stopping Swarm must not kill the agent"
        first_pid = proc.pid(agent)
        restarted = create_app(home, pi=str(fake), recover=True, allowed_hosts=["testserver"])
        with TestClient(restarted) as client:
            assert proc.pid(agent) == first_pid, "do not relaunch live agents on server restart"
            current = client.get("/api/state").json()["agents"][0]
            assert current["session_id"]
            saved_session_id = current["session_id"]
        # Simulate a reboot: saved files survive but the agent process is gone.
        proc.stop(agent)
        recovered = create_app(home, pi=str(fake), recover=True, allowed_hosts=["testserver"])
        with TestClient(recovered) as client:
            assert proc.alive(agent)
            assert proc.pid(agent) != first_pid
            for _ in range(50):
                launches = (home / "launch-log.jsonl").read_text().splitlines()
                if len(launches) == 2:
                    break
                time.sleep(0.02)
            assert len(launches) == 2
            args = json.loads(launches[-1])
            assert "Resume where you were" in args[-1]
            assert args[args.index("--session") + 1] == agent_path(agent.session_file)
            assert client.get("/api/state").json()["agents"][0]["session_id"] == saved_session_id
            assert client.post(f"/api/agents/{agent_id}/stop").status_code == 200
            assert not proc.alive(agent)
            assert not recovered.state.controller.store.get(agent_id).auto_resume
    finally:
        proc.run("kill-server", check=False)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is required")
def test_forget_terminates_inactive_pi_and_preserves_its_session(tmp_path):
    fake = make_fake_pi(
        tmp_path,
        """
import json, os, sys, uuid
from pathlib import Path
from swarm.paths import local_path
path = local_path(sys.argv[sys.argv.index('--session') + 1])
path.write_text(json.dumps({'type': 'session', 'version': 3, 'id': str(uuid.uuid4()), 'cwd': os.getcwd()}) + '\\n')
for line in sys.stdin:
    pass
""",
    )
    app = make_app(tmp_path, pi=str(fake))
    controller = app.state.controller
    processes = controller.processes
    try:
        with TestClient(app) as client:
            result = client.post(
                "/api/agents",
                json={"name": "Forget me", "goal": "Test forgetting", "folder": str(tmp_path)},
            )
            assert result.status_code == 201, result.text
            record = result.json()
            assert not record["last_error"]
            agent = controller.store.get(record["id"])
            path = controller.store.sessions_dir / f"{agent.id}.jsonl"
            for _ in range(100):
                if path.exists() and path.stat().st_size:
                    break
                time.sleep(0.01)
            original = path.read_bytes()
            assert client.delete(f"/api/agents/{agent.id}").status_code == 409
            assert processes.alive(agent)
            assert (
                client.patch(f"/api/agents/{agent.id}", json={"status": "idle"}).status_code == 200
            )
            with client.websocket_connect("/ws/state") as ws:
                ws.receive_json()
                deleted = client.delete(f"/api/agents/{agent.id}")
                assert deleted.status_code == 200, deleted.text
                assert deleted.json()["forgotten"]
                assert ws.receive_json()["agents"] == []
            assert not processes.alive(agent)
            assert not controller.store.path(agent.id).exists()
            assert path.read_bytes() == original
            assert client.get("/api/state").json()["agents"] == []
            assert client.delete(f"/api/agents/{agent.id}").status_code == 404
        controller.recover()
        assert not processes.alive(agent)
    finally:
        processes.run("kill-server", check=False)
