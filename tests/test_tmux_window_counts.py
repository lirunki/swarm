from types import SimpleNamespace

from swarm.models import Agent
from swarm.processes import Processes
from swarm.store import Store


def test_window_counts_uses_one_read_only_query(tmp_path, monkeypatch):
    processes = Processes(Store(tmp_path))
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="swarm-a\nswarm-a\nswarm-b\nother\n")

    monkeypatch.setattr(processes, "run", run)
    assert processes.window_counts() == {"swarm-a": 2, "swarm-b": 1}
    assert calls == [(("list-windows", "-a", "-F", "#{session_name}"), {"check": False})]
    monkeypatch.setattr(processes, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    assert processes.window_counts() == {}


def test_state_reports_only_extra_windows_and_refreshes_changes(tmp_path, monkeypatch):
    from test_controller import FakeProcesses

    from swarm.controller import Controller

    store = Store(tmp_path)
    first = store.create(Agent(name="First", goal="Test", folder=str(tmp_path), managed=True, auto_resume=False))
    second = store.create(Agent(name="Second", goal="Test", folder=str(tmp_path), managed=True, auto_resume=False))
    processes = FakeProcesses(store)
    counts = {f"swarm-{first.id}": 1, f"swarm-{second.id}": 3}
    monkeypatch.setattr(processes, "window_counts", lambda: counts, raising=False)
    controller = Controller(store, processes)
    controller.refresh()
    values = {agent["id"]: agent["tmux_extra_windows"] for agent in controller.state["agents"]}
    assert values == {str(first.id): 0, str(second.id): 2}
    counts[f"swarm-{first.id}"] = 2
    counts.pop(f"swarm-{second.id}")
    controller.refresh()
    values = {agent["id"]: agent["tmux_extra_windows"] for agent in controller.state["agents"]}
    assert values == {str(first.id): 1, str(second.id): None}
