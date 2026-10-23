import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from swarm.models import Agent, WakeDelivery
from swarm.store import Store, decode, encode


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "home")


def make_agent(store, **fields):
    return store.create(Agent(name="Test", goal="Ship safely", folder=str(store.home), **fields))


def test_roundtrip_preserves_markdown_unknown_fields_and_quoted_text(store):
    agent = make_agent(
        store,
        notes="# Checkpoint\n\n- Keep this",
        future={"adapter": "test"},
        activity="a: b\n---\nquotes ' and \"",
    )
    restored = decode(encode(agent))
    assert restored == agent
    assert store.get(agent.id).notes == agent.notes
    assert restored.model_extra == {"future": {"adapter": "test"}}


def test_bad_edit_keeps_last_good_and_deletion_removes_record(store):
    agent = make_agent(store)
    store.scan()
    store.path(agent.id).write_text("---\nstatus: [oops\n", encoding="utf-8")
    assert store.scan() == [agent]
    assert f"{agent.id}.md" in store.errors
    store.path(agent.id).unlink()
    assert store.scan() == []


def test_wrong_filename_and_path_traversal_rejected(store):
    agent = make_agent(store)
    with pytest.raises(ValueError):
        store.get("../../secrets")
    path = store.path(uuid4())
    path.write_text(encode(agent), encoding="utf-8")
    store.scan()
    assert "id must match" in store.errors[path.name]


def test_concurrent_updates_merge_without_lost_fields(store):
    agent = make_agent(store)
    keys = ["activity", "branch", "repository", "worktree", "notes"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda key: Store(store.home).update(agent.id, {key: key}), keys))
    actual = store.get(agent.id)
    for key in keys:
        assert getattr(actual, key) == key


def test_snapshot_and_timezone_validation(store):
    agent = make_agent(store)
    store.snapshot([agent])
    assert json.loads((store.home / "state.json").read_text())["agents"][0]["id"] == str(agent.id)
    with pytest.raises(ValueError, match="timezone"):
        store.update(agent.id, {"wake_at": datetime(2030, 1, 1)})


def test_normalized_no_op_preserves_record_and_timestamp(store):
    wake_at = datetime(2030, 1, 1, tzinfo=UTC)
    delivery = WakeDelivery(id=uuid4(), scheduled_for=wake_at)
    agent = make_agent(
        store,
        status="waiting",
        activity="Monitoring CI",
        session_id="session-1",
        session_file="/tmp/session.jsonl",
        wake_at=wake_at,
        wake_delivery=delivery,
    )
    path = store.path(agent.id)
    before_stat = path.stat()
    before_bytes = path.read_bytes()

    returned = store.transition(
        agent.id,
        lambda _: {
            "status": "waiting",
            "activity": "Monitoring CI",
            "session_id": "session-1",
            "session_file": "/tmp/session.jsonl",
            "wake_at": "2030-01-01T00:00:00Z",
        },
    )

    after_stat = path.stat()
    current = store.get(agent.id)
    assert returned is not None and returned == current == agent
    assert current.wake_delivery == delivery
    assert current.updated_at == agent.updated_at
    assert path.read_bytes() == before_bytes
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )


def test_explicit_timer_cancellation_and_done_normalization_are_not_no_ops(store):
    scheduled = datetime.now(UTC) + timedelta(hours=1)
    delivery = WakeDelivery(id=uuid4(), scheduled_for=scheduled)
    cancellable = make_agent(store, wake_at=None, wake_delivery=delivery)
    cancelled = store.update(cancellable.id, {"wake_at": None})
    assert cancelled.wake_at is None and cancelled.wake_delivery is None
    assert cancelled.updated_at > cancellable.updated_at

    done = make_agent(store, status="done", wake_at=scheduled, wake_delivery=delivery)
    normalized = store.update(done.id, {"status": "done"})
    assert normalized.status.value == "done"
    assert normalized.wake_at is None and normalized.wake_delivery is None
    assert normalized.updated_at > done.updated_at


def test_duplicate_controller_rejected(store):
    with store.controller_lock(), pytest.raises(RuntimeError, match="another Swarm"):
        with Store(store.home).controller_lock():
            pass


def test_symlink_record_rejected(store, tmp_path):
    agent = make_agent(store)
    path = store.path(agent.id)
    target = tmp_path / "external.md"
    target.write_text(encode(agent))
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        store.get(agent.id)
