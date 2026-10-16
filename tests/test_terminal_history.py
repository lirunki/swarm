import json
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.models import Agent
from swarm.processes import Processes
from swarm.server import create_app
from swarm.store import Store


@pytest.fixture
def capture(tmp_path, monkeypatch):
    processes = Processes(Store(tmp_path / "home"))
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "first\nsecond\nlast\n", "")

    monkeypatch.setattr(processes, "run", run)
    agent = Agent(name="History", goal="Read only", folder=str(tmp_path), managed=True)
    return processes, agent, calls


def test_capture_is_scoped_plain_text_and_does_not_change_pane_mode(capture):
    processes, agent, calls = capture
    result = processes.capture_history(agent, 2)
    assert result["text"] == "second\nlast" and result["lines"] == 2
    assert result["truncated"] and result["captured_at"]
    assert calls == [
        ("capture-pane", "-p", "-t", processes.target(agent) + ":0.0", "-S", "-2", "-E", "-")
    ]


@pytest.mark.parametrize("lines", [0, -1, 10001, True, 1.5])
def test_invalid_line_limits_do_not_invoke_tmux(capture, lines):
    processes, agent, calls = capture
    with pytest.raises(ValueError, match="line count"):
        processes.capture_history(agent, lines)
    assert not calls


def test_unowned_agents_cannot_read_by_pid_or_supplied_folder(capture):
    processes, agent, calls = capture
    agent.managed = False
    agent.pid = 1
    with pytest.raises(ValueError, match="managed terminals"):
        processes.capture_history(agent)
    assert not calls


def test_payload_limit_handles_utf8_and_empty_capture(capture, monkeypatch):
    processes, agent, _ = capture
    monkeypatch.setattr("swarm.processes.MAX_HISTORY_BYTES", 7)
    monkeypatch.setattr(
        processes, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "汉" * 8, "")
    )
    result = processes.capture_history(agent)
    assert len(result["text"].encode()) <= 7 and result["text"] == "汉汉"
    assert result["truncated"]
    monkeypatch.setattr(
        processes, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "\n\n", "")
    )
    result = processes.capture_history(agent)
    assert result["text"] == "" and result["lines"] == 0


def test_missing_history_is_an_error_not_an_implicit_launch(capture, monkeypatch):
    processes, agent, _ = capture
    monkeypatch.setattr(
        processes,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "missing"),
    )
    monkeypatch.setattr(
        processes, "launch", lambda *args: pytest.fail("history must never launch a process")
    )
    with pytest.raises(RuntimeError, match="No retained"):
        processes.capture_history(agent)


def test_history_api_limits_cache_and_unknown_agents(tmp_path, monkeypatch):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    controller = app.state.controller
    agent = controller.store.create(
        Agent(name="Stored", goal="History", folder=str(tmp_path), managed=True, auto_resume=False)
    )
    seen = []

    def capture(agent, lines):
        seen.append((agent.id, lines))
        return {
            "text": "<script>literal output</script>",
            "lines": 1,
            "limit": lines,
            "truncated": False,
        }

    monkeypatch.setattr(controller.processes, "capture_history", capture)
    with TestClient(app) as client:
        result = client.get(f"/api/agents/{agent.id}/terminal-history?lines=80")
        assert result.status_code == 200 and result.json()["text"].startswith("<script>")
        assert result.headers["cache-control"] == "no-store"
        assert seen == [(agent.id, 80)]
        for value in ["0", "10001", "nan"]:
            assert (
                client.get(f"/api/agents/{agent.id}/terminal-history?lines={value}").status_code
                == 422
            )
        assert client.get(f"/api/agents/{uuid4()}/terminal-history").status_code == 404
        assert (
            client.get(
                f"/api/agents/{agent.id}/terminal-history",
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is required")
def test_real_history_capture_never_sends_input_or_enters_copy_mode(tmp_path):
    fake = tmp_path / "fake-pi"
    input_file = tmp_path / "received-input.txt"
    fake.write_text(f"""#!{sys.executable}
from pathlib import Path
for i in range(150):
    print(f"HISTORY-LINE-{{i:04d}}", flush=True)
print("HISTORY-READY", flush=True)
for line in __import__('sys').stdin:
    Path({str(input_file)!r}).write_text(line)
""")
    fake.chmod(0o700)
    app = create_app(tmp_path / "home", pi=str(fake), recover=False, allowed_hosts=["testserver"])
    processes = app.state.controller.processes
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agents",
                json={"name": "Live", "goal": "History safety", "folder": str(tmp_path)},
            )
            assert response.status_code == 201 and not response.json()["last_error"]
            agent = app.state.controller.store.get(response.json()["id"])
            before_pid = processes.pid(agent)
            for _ in range(100):
                response = client.get(f"/api/agents/{agent.id}/terminal-history")
                if "HISTORY-READY" in response.json().get("text", ""):
                    break
                time.sleep(0.02)
            assert response.status_code == 200
            assert "HISTORY-LINE-0000" in response.json()["text"]
            assert not input_file.exists()
            assert processes.pid(agent) == before_pid and processes.alive(agent)
            mode = processes.run(
                "display-message", "-p", "-t", processes.target(agent) + ":0.0", "#{pane_in_mode}"
            )
            assert mode.stdout.strip() == "0"
            assert (
                processes.run(
                    "show-option", "-v", "-t", processes.target(agent), "mouse"
                ).stdout.strip()
                == "on"
            )
            # Dead panes retain history too; reading is not an implicit wake.
            processes.run("send-keys", "-t", processes.target(agent) + ":0.0", "C-d")
            for _ in range(100):
                if not processes.alive(agent):
                    break
                time.sleep(0.01)
            assert not processes.alive(agent)
            assert (
                "HISTORY-READY"
                in client.get(f"/api/agents/{agent.id}/terminal-history").json()["text"]
            )
            assert not processes.alive(agent)
    finally:
        processes.run("kill-server", check=False)


def test_history_uses_existing_federation_proxy_and_stays_uncached(tmp_path, monkeypatch):
    import socket
    import urllib.request

    from test_federation_integration import running_server, wait_for_remote

    from swarm.federation import FederationConfig
    from swarm.relay import create_relay_app

    key = "history-fixture-key-0123456789-abcdefgh"
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        a, b = first.getsockname()[1], second.getsockname()[1]
    with running_server(create_relay_app([key])) as relay_port:
        relay = f"ws://127.0.0.1:{relay_port}/v1/connect"
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
        agent = app_b.state.controller.store.create(
            Agent(
                name="Remote", goal="History", folder=str(tmp_path), managed=True, auto_resume=False
            )
        )
        monkeypatch.setattr(
            app_b.state.controller.processes,
            "capture_history",
            lambda source, lines: {
                "text": "REMOTE-ONLY-OUTPUT",
                "lines": 1,
                "limit": lines,
                "truncated": False,
            },
        )
        with running_server(app_a, a), running_server(app_b, b):
            wait_for_remote(base_a, "beta")
            url = f"{base_a}/api/federation/beta/proxy/api/agents/{agent.id}/terminal-history?lines=70"
            with urllib.request.urlopen(url, timeout=10) as response:
                value = json.loads(response.read())
                assert value["text"] == "REMOTE-ONLY-OUTPUT" and value["limit"] == 70
                assert response.headers["cache-control"] == "no-store"
