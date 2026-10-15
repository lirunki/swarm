import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

import pytest

from swarm.models import Agent
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
