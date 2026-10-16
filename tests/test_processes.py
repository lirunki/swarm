"""Processes propagates SWARM_API_CA_FILE to child agents only when set."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from swarm.models import Agent
from swarm.processes import Processes
from swarm.store import Store


def _make_agent(store: Store, folder: Path) -> Agent:
    folder.mkdir(parents=True, exist_ok=True)
    agent = Agent(
        name="Worker",
        goal="Do work",
        backend="pi",
        managed=True,
        folder=str(folder),
    )
    return store.create(agent)


def _launching(proc: Processes, agent: Agent):
    """Drive launch() with tmux mocked out, returning captured argv tuples."""
    commands: list[tuple] = []

    def fake_run(*args: str, **kwargs) -> subprocess.CompletedProcess:
        commands.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    proc.adapters = {"pi": SimpleNamespace(command=lambda a, p: ["fake-pi"])}
    proc.run = fake_run  # type: ignore[method-assign]
    proc.alive = lambda a: False  # type: ignore[method-assign]
    proc.pid = lambda a: 777  # type: ignore[method-assign]
    proc.launch(agent, "Do work")
    return commands


def _new_session_env(commands: list[tuple]) -> list[str]:
    new_session = next(cmd for cmd in commands if cmd[0] == "new-session")
    return [opt for opt in new_session if opt.startswith("SWARM_API_CA_FILE=")]


def test_launch_passes_ca_file_to_child_when_set(tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy-ca")
    monkeypatch.setenv("SWARM_API_CA_FILE", str(ca))

    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store, api_url="https://127.0.0.1:8787")

    env = _new_session_env(_launching(proc, agent))
    assert env == [f"SWARM_API_CA_FILE={ca}"]


def test_launch_omits_ca_file_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_API_CA_FILE", raising=False)

    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store, api_url="http://127.0.0.1:8787")

    env = _new_session_env(_launching(proc, agent))
    assert env == []


def test_launch_still_passes_api_url(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_API_CA_FILE", raising=False)

    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store, api_url="https://localhost:9000")

    new_session = next(
        cmd for cmd in _launching(proc, agent) if cmd[0] == "new-session"
    )
    assert "SWARM_API_URL=https://localhost:9000" in new_session