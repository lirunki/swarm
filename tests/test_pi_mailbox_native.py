"""Load the real installed Pi in RPC mode, but exercise only no-model control commands."""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from swarm.adapters import MAILBOX_EXTENSION
from swarm.controller import Controller
from swarm.importers.pi_history import PiHistory
from swarm.messaging import MessageRequest, prepare_bridge
from swarm.models import Agent
from swarm.paths import agent_path
from swarm.store import Store, atomic_write


@pytest.mark.skipif(not shutil.which("pi"), reason="Installed Pi is optional")
def test_native_pi_extension_loads_and_executes_self_name_and_reload_without_model_calls(tmp_path):
    store = Store(tmp_path / "registry")
    session_id = str(uuid4())
    session = store.sessions_dir / "native.jsonl"
    session.write_text(PiHistory(session_id, str(tmp_path), "Before").encode())
    agent = store.create(
        Agent(
            name="Native test",
            goal="Control only",
            folder=str(tmp_path),
            managed=True,
            status="idle",
            auto_resume=False,
            session_file=str(session),
            session_id=session_id,
        )
    )
    directory, epoch = prepare_bridge(store, agent)
    agent = store.get(agent.id)
    config = tmp_path / "empty-pi-config"
    config.mkdir()
    (config / "settings.json").write_text(
        json.dumps({"enableInstallTelemetry": False, "enableAnalytics": False})
    )
    # No real provider keys or saved auth are available to this child. No model prompt is sent.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.endswith("_API_KEY")
        and not k.startswith(("AZURE_", "AWS_", "GOOGLE_", "PI_SESSION_"))
        and k
        not in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_AUTH_TOKEN",
            "PI_CODING_AGENT_SESSION_DIR",
        }
    }
    env.update(
        SWARM_CONTROL_DIR=agent_path(directory),
        SWARM_CONTROL_EPOCH=epoch,
        SWARM_AGENT_ID=str(agent.id),
        PI_CODING_AGENT_DIR=agent_path(config),
    )
    args = [
        shutil.which("pi"),
        "--mode",
        "rpc",
        "--session",
        agent_path(session),
        "--no-tools",
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--extension",
        agent_path(MAILBOX_EXTENSION),
    ]
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
    )
    output = queue.Queue()
    errors = []

    def read_output():
        for line in process.stdout:
            try:
                output.put(json.loads(line))
            except ValueError:
                errors.append(line)

    def read_errors():
        for line in process.stderr:
            errors.append(line)

    threads = [
        threading.Thread(target=read_output, daemon=True),
        threading.Thread(target=read_errors, daemon=True),
    ]
    for thread in threads:
        thread.start()

    class FakeProcesses:
        def alive(self, a):
            return process.poll() is None

        def pid(self, a):
            return process.pid

        def health(self):
            return {"tmux": True, "pi": True}

    controller = Controller(store, FakeProcesses())

    def wait(predicate):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            assert process.poll() is None, "".join(errors)[-4000:]
            time.sleep(0.05)
        raise AssertionError("Native Pi mailbox deadline: " + "".join(errors)[-4000:])

    def rpc(kind):
        id = str(uuid4())
        process.stdin.write(json.dumps({"id": id, "type": kind}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            event = output.get(timeout=20)
            if event.get("id") == id:
                return event
        raise AssertionError("No RPC response")

    try:
        wait(lambda: controller.messages.heartbeat(agent))
        commands = rpc("get_commands")
        assert commands["success"] and any(
            c["name"] == "swarm-control-reload" for c in commands["data"]["commands"]
        )
        first = controller.messages.send(
            str(agent.id),
            MessageRequest(text="/name Mailbox controlled session", sender_id=agent.id),
            "command",
        )["message"]
        assert first["status"] == "delivering"
        ack = directory / "acks" / f"{first['id']}.json"
        wait(lambda: ack.is_file() and json.loads(ack.read_text()).get("status") == "delivered")
        controller.messages.tick(store.get(agent.id))
        assert controller.messages.list(str(agent.id))["messages"] == []
        assert rpc("get_state")["data"]["sessionName"] == "Mailbox controlled session"
        second = controller.messages.send(
            str(agent.id), MessageRequest(text="/reload", sender_id=agent.id), "command"
        )["message"]
        receipt = directory / "acks" / f"{second['id']}.json"
        wait(
            lambda: (
                receipt.is_file() and json.loads(receipt.read_text()).get("status") == "delivered"
            )
        )
        controller.messages.tick(store.get(agent.id))
        assert controller.messages.list(str(agent.id))["messages"] == []
        assert rpc("get_state")["data"]["sessionId"] == session_id
        assert not any(
            json.loads(line).get("type") == "message"
            and json.loads(line).get("message", {}).get("role") == "assistant"
            for line in session.read_text().splitlines()
        )
        assert "Failed to load extension" not in "".join(errors)
        job_id = str(uuid4())
        request = {
            "id": job_id,
            "agent_id": str(agent.id),
            "epoch": epoch,
            "session_id": session_id,
            "action": "shutdown",
            "phase": "prepare",
            "requested_at": datetime.now(UTC).isoformat(),
        }
        atomic_write(directory / "lifecycle.json", json.dumps(request))
        receipt = directory / "lifecycle-ack.json"
        wait(lambda: receipt.exists() and json.loads(receipt.read_text()).get("state") == "ready")
        assert process.poll() is None
        atomic_write(directory / "lifecycle.json", json.dumps(request | {"phase": "commit"}))
        wait(lambda: json.loads(receipt.read_text()).get("state") == "shutdown_requested")
        # RPC mode observes deferred shutdown at the next command boundary; the
        # interactive tmux test separately proves exit without a browser/input poke.
        process.stdin.write(json.dumps({"type": "get_state"}) + "\n")
        process.stdin.flush()
        process.wait(timeout=15)
        assert json.loads(receipt.read_text())["state"] == "shutdown_requested"
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for thread in threads:
            thread.join(timeout=2)
