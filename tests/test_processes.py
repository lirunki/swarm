"""Processes propagates SWARM_API_CA_FILE to child agents only when set."""

from __future__ import annotations

import subprocess

import pytest
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


@pytest.mark.parametrize(
    "action,expected",
    [
        ("split-horizontal", ("split-window", "-h")),
        ("split-vertical", ("split-window", "-v")),
        ("reset-layout", ("select-layout", "tiled")),
        ("new-window", ("new-window",)),
        ("zoom-pane", ("resize-pane", "-Z")),
        ("pane-up", ("select-pane", "-U")),
        ("pane-down", ("select-pane", "-D")),
        ("pane-left", ("select-pane", "-L")),
        ("pane-right", ("select-pane", "-R")),
        ("next-window", ("next-window",)),
        ("previous-window", ("previous-window",)),
    ],
)
def test_tmux_menu_dispatches_only_mapped_layout_commands(tmp_path, action, expected):
    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store)
    commands = []

    def run(*args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    proc.run = run
    proc.alive = lambda _: True
    proc.tmux_action(agent, action)

    if action in {"split-horizontal", "split-vertical", "new-window"}:
        assert commands == [
            (*expected, "-c", str(Path(agent.folder)), "-t", proc.target(agent), "bash")
        ]
    else:
        assert commands == [(*expected, "-t", proc.target(agent))]


def test_tmux_close_split_preserves_primary_agent_pane(tmp_path):
    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store)
    commands = []

    def run(*args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="%1\n" if args[0] == "display-message" else "",
            stderr="",
        )

    proc.run = run
    proc.alive = lambda _: True
    proc.identity = lambda _: {"pane": "%1"}
    with pytest.raises(ValueError, match="main agent pane"):
        proc.tmux_action(agent, "close-pane")
    assert [command[0] for command in commands] == ["display-message"]


def test_tmux_close_split_targets_non_primary_pane_only(tmp_path):
    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store)
    commands = []

    def run(*args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="%2\n" if args[0] == "display-message" else "",
            stderr="",
        )

    proc.run = run
    proc.alive = lambda _: True
    proc.identity = lambda _: {"pane": "%1"}
    proc.tmux_action(agent, "close-pane")
    assert commands[-1] == ("kill-pane", "-t", "%2")


def test_tmux_menu_rejects_unmanaged_and_unknown_actions(tmp_path):
    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store)
    agent.managed = False
    with pytest.raises(ValueError, match="managed agent"):
        proc.tmux_action(agent, "new-window")
    agent.managed = True
    proc.alive = lambda _: True
    with pytest.raises(ValueError, match="Unsupported tmux action"):
        proc.tmux_action(agent, "send-keys")


def test_launch_still_passes_api_url(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_API_CA_FILE", raising=False)

    store = Store(tmp_path / "registry")
    agent = _make_agent(store, tmp_path / "work")
    proc = Processes(store, api_url="https://localhost:9000")

    new_session = next(
        cmd for cmd in _launching(proc, agent) if cmd[0] == "new-session"
    )
    assert "SWARM_API_URL=https://localhost:9000" in new_session