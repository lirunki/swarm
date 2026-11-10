import fcntl
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

import swarm.agent_limits as agent_limits_module
from swarm.agent_limits import (
    AgentLimits,
    AgentLimitsConflict,
    AgentLimitsCorrupt,
    AgentLimitsError,
    active,
    active_entry,
    count_active,
)
from swarm.models import Agent, Status
from swarm.store import Store


class Processes:
    def __init__(self, *running):
        self.running = {str(item) for item in running}

    def alive(self, agent):
        return str(agent.id) in self.running


def body(limit, revision=0, request_id=None):
    return {
        "max_active_managed": limit,
        "revision": revision,
        "request_id": str(request_id or uuid4()),
    }


def test_default_is_read_only_and_first_update_is_private_and_durable(tmp_path):
    store = Store(tmp_path / "home")
    limits = AgentLimits(store)

    assert limits.get(3) == {
        "revision": 0,
        "max_active_managed": 20,
        "active_managed": 3,
        "scope": "orchestration",
    }
    assert not limits.path.exists()

    updated = limits.apply(body(37), active_managed=3)
    assert updated == {
        "revision": 1,
        "max_active_managed": 37,
        "active_managed": 3,
        "scope": "orchestration",
    }
    assert limits.path.stat().st_mode & 0o777 == 0o600
    assert AgentLimits(Store(store.home)).get(4) == updated | {"active_managed": 4}


def test_revision_and_uuid_receipts_are_idempotent_before_stale_check(tmp_path):
    limits = AgentLimits(Store(tmp_path / "home"))
    request = body(7)
    first = limits.apply(request, 2)
    assert limits.apply(request, 2) == first

    with pytest.raises(AgentLimitsConflict, match="different payload"):
        limits.apply(request | {"max_active_managed": 8}, 2)
    with pytest.raises(AgentLimitsConflict, match="stale"):
        limits.apply(body(8, revision=0), 2)

    second = limits.apply(body(8, revision=1), 2)
    assert second["revision"] == 2 and second["max_active_managed"] == 8


def test_receipts_are_bounded_to_256(tmp_path):
    limits = AgentLimits(Store(tmp_path / "home"))
    revision = 0
    for number in range(260):
        limits.apply(body((number % 10) + 1, revision=revision), 0)
        revision += 1
    saved = json.loads(limits.path.read_text(encoding="utf-8"))
    assert saved["revision"] == 260
    assert len(saved["receipts"]) == 256


@pytest.mark.parametrize("value", [True, False, 0, -1, 10001, 1.5, "20", None])
def test_limit_is_strict_bounded_integer(tmp_path, value):
    limits = AgentLimits(Store(tmp_path / str(uuid4())))
    with pytest.raises(AgentLimitsError, match="max_active_managed"):
        limits.apply(body(value), 0)
    assert not limits.path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"max_active_managed": 4, "revision": 0},
        body(4) | {"extra": "no"},
        body(4) | {"revision": True},
        body(4) | {"request_id": "not-a-uuid"},
    ],
)
def test_request_schema_is_strict(tmp_path, payload):
    with pytest.raises(AgentLimitsError):
        AgentLimits(Store(tmp_path / str(uuid4()))).apply(payload, 0)


def write_with_mode(path, content, mode):
    previous_umask = os.umask(0)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    finally:
        os.umask(previous_umask)
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)


def write_private(path, content):
    write_with_mode(path, content, 0o600)


def test_malformed_or_nonprivate_file_fails_closed_without_echo_or_rewrite(tmp_path):
    limits = AgentLimits(Store(tmp_path / "home"))
    secret = b'{"credential":"must-not-echo"'
    write_private(limits.path, secret)

    for operation in (lambda: limits.get(0), lambda: limits.apply(body(9), 0)):
        with pytest.raises(AgentLimitsCorrupt) as error:
            operation()
        assert "must-not-echo" not in str(error.value)
    assert limits.path.read_bytes() == secret

    limits.path.unlink()
    write_with_mode(limits.path, json.dumps(AgentLimits.empty()).encode(), 0o640)
    with pytest.raises(AgentLimitsCorrupt):
        limits.get(0)


def test_lock_timeout_is_bounded_and_does_not_change_settings(tmp_path, monkeypatch):
    limits = AgentLimits(Store(tmp_path / "home"))
    limits.apply(body(8), 0)
    original = limits.path.read_bytes()
    held = os.open(limits.lock_path, os.O_RDWR | os.O_NONBLOCK)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = [100.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(agent_limits_module, "LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(agent_limits_module, "LOCK_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(agent_limits_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(agent_limits_module.time, "sleep", sleep)
    try:
        with pytest.raises(AgentLimitsCorrupt, match="temporarily unavailable"):
            limits.apply(body(9, revision=1), 0)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    assert clock[0] <= 100.06
    assert limits.path.read_bytes() == original


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
@pytest.mark.parametrize("target_name", ["settings", "lock"])
def test_special_settings_and_lock_paths_fail_closed_promptly(tmp_path, kind, target_name):
    limits = AgentLimits(Store(tmp_path / "home"))
    path = limits.path if target_name == "settings" else limits.lock_path
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o600)
    else:
        destination = tmp_path / f"{target_name}-destination"
        write_private(destination, json.dumps(AgentLimits.empty()).encode())
        path.symlink_to(destination)

    with pytest.raises(AgentLimitsCorrupt):
        limits.get(0)


@pytest.mark.parametrize("target_name", ["settings", "lock"])
def test_hardlinked_settings_and_lock_paths_are_rejected(tmp_path, target_name):
    limits = AgentLimits(Store(tmp_path / "home"))
    source = tmp_path / f"{target_name}-source"
    content = json.dumps(AgentLimits.empty()).encode() if target_name == "settings" else b""
    write_private(source, content)
    path = limits.path if target_name == "settings" else limits.lock_path
    os.link(source, path)

    with pytest.raises(AgentLimitsCorrupt):
        limits.get(0)


@pytest.mark.parametrize("target_name", ["settings", "lock"])
def test_wrong_mode_settings_and_lock_paths_are_not_repaired(tmp_path, target_name):
    limits = AgentLimits(Store(tmp_path / "home"))
    path = limits.path if target_name == "settings" else limits.lock_path
    content = json.dumps(AgentLimits.empty()).encode() if target_name == "settings" else b""
    write_with_mode(path, content, 0o640)

    with pytest.raises(AgentLimitsCorrupt):
        limits.get(0)
    assert path.stat().st_mode & 0o777 == 0o640


def test_read_rejects_file_changed_while_descriptor_is_open(tmp_path, monkeypatch):
    limits = AgentLimits(Store(tmp_path / "home"))
    write_private(limits.path, json.dumps(AgentLimits.empty()).encode())
    real_read = os.read
    changed = False

    def changing_read(descriptor, size):
        nonlocal changed
        content = real_read(descriptor, size)
        if not changed:
            changed = True
            with limits.path.open("r+b") as stream:
                stream.write(b" ")
                stream.flush()
                os.fsync(stream.fileno())
        return content

    monkeypatch.setattr(agent_limits_module.os, "read", changing_read)
    with pytest.raises(AgentLimitsCorrupt):
        limits.get(0)


def test_concurrent_same_revision_has_one_winner_without_receipt_overwrite(tmp_path):
    store = Store(tmp_path / "home")
    first_limits = AgentLimits(store)
    second_limits = AgentLimits(Store(store.home))
    requests = [body(31), body(47)]

    def apply(args):
        limits, item = args
        try:
            return ("ok", limits.apply(item, 0))
        except AgentLimitsConflict as exc:
            return ("conflict", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, zip((first_limits, second_limits), requests)))

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    saved = json.loads(second_limits.path.read_text(encoding="utf-8"))
    winner = next(request for request, result in zip(requests, results) if result[0] == "ok")
    normalized, payload_hash = AgentLimits._request(winner)
    assert saved["revision"] == 1
    assert saved["max_active_managed"] == normalized["max_active_managed"]
    assert saved["receipts"] == [
        {"request_id": normalized["request_id"], "payload_hash": payload_hash}
    ]


def agent(**fields):
    values = {
        "name": "worker",
        "goal": "test",
        "folder": "/tmp",
        "managed": True,
        "auto_resume": False,
        "status": Status.idle,
    }
    values.update(fields)
    return Agent(**values)


def test_active_predicate_matches_cached_entries_and_managed_filtering():
    queued = agent(spawn_state="provisioning")
    reserved = agent(restart_job_id=str(uuid4()))
    resumed = agent(auto_resume=True, status=Status.waiting)
    done_alive = agent(status=Status.done)
    done_stopped = agent(status=Status.done, auto_resume=True)
    observed = agent(managed=False)
    processes = Processes(done_alive.id, observed.id)
    agents = [queued, reserved, resumed, done_alive, done_stopped, observed]

    assert [active(item, processes) for item in agents] == [True, True, True, True, False, True]
    assert count_active(agents, processes) == 4
    for item in agents:
        entry = item.model_dump(mode="json") | {"alive": processes.alive(item)}
        assert active_entry(entry) == active(item, processes)
