import copy
import json
import os
import threading
from uuid import uuid4

import pytest

import swarm.grouping as grouping
from swarm.grouping import GroupingConflict, GroupingCorrupt, GroupingError, Groups
from swarm.models import Agent
from swarm.store import Store


def agent(tmp_path, *, agent_id=None, parent_id=None, name="agent"):
    return Agent(
        id=agent_id or uuid4(),
        parent_id=parent_id,
        name=name,
        goal="test grouping",
        folder=str(tmp_path),
        auto_resume=False,
    )


def request(action, revision, **fields):
    return {"action": action, "revision": revision, "request_id": str(uuid4()), **fields}


def test_create_assigns_entire_family_and_retry_is_idempotent(tmp_path):
    store = Store(tmp_path / "home")
    root = agent(tmp_path)
    child = agent(tmp_path, parent_id=root.id)
    sibling = agent(tmp_path, parent_id=root.id)
    unrelated = agent(tmp_path)
    agents = [root, child, sibling, unrelated]
    groups = Groups(store)
    body = request("create", 0, name="<b>literal</b>", agent_ids=[str(child.id)])

    created = groups.apply(body, agents)
    duplicate = groups.apply(body, agents)

    group_id = created["operation"]["group_id"]
    assert created == duplicate
    assert created["groups"] == [{"id": group_id, "name": "<b>literal</b>"}]
    assert created["assignments"] == {
        str(root.id): group_id,
        str(child.id): group_id,
        str(sibling.id): group_id,
    }
    assert str(unrelated.id) not in created["assignments"]
    assert groups.path.stat().st_mode & 0o777 == 0o600


def test_revision_validation_names_ids_and_request_reuse_do_not_mutate(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    one = agent(tmp_path)
    agents = [one]
    body = request("create", 0, name="valid", agent_ids=[])
    created = groups.apply(body, agents)

    with pytest.raises(GroupingConflict):
        groups.apply(body | {"name": "different"}, agents)
    for name in (
        "",
        " \t ",
        "x" * 81,
        "bad\nname",
        "bad\u200bname",
        "DEFAULTGROUP",
        "Default Group",
        "UNGROUPED",
    ):
        with pytest.raises(GroupingError):
            groups.apply(request("create", 1, name=name, agent_ids=[]), agents)
    with pytest.raises(GroupingError):
        groups.apply(request("assign", 1, group_id=None, agent_ids=[str(uuid4())]), agents)
    with pytest.raises(GroupingError):
        groups.apply(request("assign", True, group_id=None, agent_ids=[]), agents)
    assert groups.get() == {key: created[key] for key in ("revision", "groups", "assignments")}


def test_names_are_trimmed_and_unique_by_casefold_on_create_and_rename(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    made = groups.apply(request("create", 0, name="  Team  ", agent_ids=[]), [])
    assert made["groups"][0]["name"] == "Team"

    with pytest.raises(GroupingError, match="unique"):
        groups.apply(request("create", 1, name="team", agent_ids=[]), [])
    second = groups.apply(request("create", 1, name="Other", agent_ids=[]), [])
    with pytest.raises(GroupingError, match="unique"):
        groups.apply(
            request(
                "rename",
                2,
                group_id=second["operation"]["group_id"],
                name=" TEAM ",
            ),
            [],
        )
    assert groups.get()["revision"] == 2


def test_action_specific_fields_and_invalid_unicode_hash_do_not_mutate(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    invalid = [
        request("create", 0, name="x", group_id=None),
        request("assign", 0, group_id=None, agent_ids=[], name="x"),
        request("rename", 0, group_id=str(uuid4()), name="x", agent_ids=[]),
        request("delete", 0, group_id=str(uuid4()), agent_ids=[]),
    ]
    for body in invalid:
        with pytest.raises(GroupingError, match="Invalid fields"):
            groups.apply(body, [])
    with pytest.raises(GroupingError, match="Unknown grouping action"):
        groups.apply({"action": [], "revision": 0, "request_id": str(uuid4())}, [])
    with pytest.raises(GroupingError, match="valid JSON"):
        groups.apply(request("create", 0, name="\ud800", agent_ids=[]), [])
    assert groups.get() == {"revision": 0, "groups": [], "assignments": {}}


def test_saved_state_rejects_bool_version_bad_hash_duplicate_receipts_and_assignment_cap():
    group_id = str(uuid4())
    request_id = str(uuid4())
    valid = {
        "version": 1,
        "revision": 0,
        "groups": [{"id": group_id, "name": "A"}],
        "assignments": {},
        "receipts": [
            {
                "request_id": request_id,
                "payload_hash": "0" * 64,
                "operation": {"action": "create", "group_id": group_id},
            }
        ],
    }
    bad_version = copy.deepcopy(valid)
    bad_version["version"] = True
    bad_hash = copy.deepcopy(valid)
    bad_hash["receipts"][0]["payload_hash"] = 42
    duplicate_receipt = copy.deepcopy(valid)
    duplicate_receipt["receipts"].append(copy.deepcopy(duplicate_receipt["receipts"][0]))
    too_many_assignments = copy.deepcopy(valid)
    too_many_assignments["assignments"] = {str(uuid4()): group_id for _ in range(10_001)}
    for state in (bad_version, bad_hash, duplicate_receipt, too_many_assignments):
        with pytest.raises(GroupingCorrupt, match="Saved groups could not be read"):
            Groups._validate_state(state)


def test_reconcile_new_child_reparent_deleted_root_missing_parent_and_cycle(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    root = agent(tmp_path)
    child = agent(tmp_path, parent_id=root.id)
    other = agent(tmp_path)
    made = groups.apply(
        request("create", 0, name="A", agent_ids=[str(root.id)]), [root, child, other]
    )
    group_a = made["operation"]["group_id"]
    made = groups.apply(
        request("create", 1, name="B", agent_ids=[str(other.id)]), [root, child, other]
    )
    group_b = made["operation"]["group_id"]

    new_child = agent(tmp_path, parent_id=child.id)
    state, error = groups.refresh([root, child, new_child, other])
    assert error is None and state["assignments"][str(new_child.id)] == group_a

    merged_other = other.model_copy(update={"parent_id": child.id})
    state, _ = groups.refresh([root, child, new_child, merged_other])
    assert set(state["assignments"].values()) == {group_a}

    missing = agent(tmp_path, parent_id=uuid4())
    state, _ = groups.refresh([child, new_child, merged_other, missing])
    assert str(root.id) not in state["assignments"]
    assert state["assignments"][str(child.id)] == group_a
    assert str(missing.id) not in state["assignments"]

    first_id, second_id = sorted((uuid4(), uuid4()), key=str)
    first = agent(tmp_path, agent_id=first_id, parent_id=second_id)
    second = agent(tmp_path, agent_id=second_id, parent_id=first_id)
    assigned = groups.apply(
        request("assign", state["revision"], group_id=group_b, agent_ids=[str(second.id)]),
        [first, second],
    )
    assert assigned["assignments"] == {str(first.id): group_b, str(second.id): group_b}


def test_create_rejects_family_over_assignment_cap_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(grouping, "MAX_ASSIGNMENTS", 2)
    groups = Groups(Store(tmp_path / "home"))
    root = agent(tmp_path)
    child = agent(tmp_path, parent_id=root.id)
    grandchild = agent(tmp_path, parent_id=child.id)

    with pytest.raises(GroupingError, match="At most 2 agent assignments are allowed"):
        groups.apply(
            request("create", 0, name="too large", agent_ids=[str(root.id)]),
            [root, child, grandchild],
        )

    assert not groups.path.exists()
    assert groups.get() == {"revision": 0, "groups": [], "assignments": {}}


def test_refresh_over_assignment_cap_keeps_last_good_file(tmp_path, monkeypatch):
    monkeypatch.setattr(grouping, "MAX_ASSIGNMENTS", 3)
    groups = Groups(Store(tmp_path / "home"))
    root = agent(tmp_path)
    child = agent(tmp_path, parent_id=root.id)
    sibling = agent(tmp_path, parent_id=root.id)
    agents = [root, child, sibling]
    created = groups.apply(
        request("create", 0, name="safe", agent_ids=[str(root.id)]), agents
    )
    expected = {key: created[key] for key in ("revision", "groups", "assignments")}
    original = groups.path.read_bytes()
    new_child = agent(tmp_path, parent_id=child.id)

    state, error = groups.refresh(agents + [new_child])

    assert state == expected
    assert error == "At most 3 agent assignments are allowed"
    assert groups.path.read_bytes() == original
    assert groups.get() == expected


def test_refresh_over_serialization_cap_keeps_last_good_file(tmp_path, monkeypatch):
    groups = Groups(Store(tmp_path / "home"))
    root = agent(tmp_path)
    created = groups.apply(
        request("create", 0, name="safe", agent_ids=[str(root.id)]), [root]
    )
    expected = {key: created[key] for key in ("revision", "groups", "assignments")}
    original = groups.path.read_bytes()
    monkeypatch.setattr(grouping, "MAX_FILE_BYTES", len(original))
    child = agent(tmp_path, parent_id=root.id)

    state, error = groups.refresh([root, child])

    assert state == expected
    assert error == "Saved groups are too large"
    assert groups.path.read_bytes() == original
    assert groups.get() == expected


def test_bad_group_file_and_bad_agent_read_never_overwrite_memberships(tmp_path):
    store = Store(tmp_path / "home")
    groups = Groups(store)
    one = agent(tmp_path)
    made = groups.apply(request("create", 0, name="safe", agent_ids=[str(one.id)]), [one])
    original = groups.path.read_bytes()

    state, error = groups.refresh([], records_valid=False)
    assert error is None and state["assignments"] == made["assignments"]
    assert groups.path.read_bytes() == original

    groups.path.write_text("{broken", encoding="utf-8")
    fallback, error = groups.refresh([one])
    assert error == "Saved groups could not be read"
    assert fallback["assignments"] == made["assignments"]
    assert groups.path.read_text(encoding="utf-8") == "{broken"
    with pytest.raises(GroupingCorrupt):
        groups.get()


def test_reading_empty_store_has_no_groups_file_or_home_permission_side_effect(tmp_path):
    store = Store(tmp_path / "home")
    os.chmod(store.home, 0o750)
    groups = Groups(store)

    assert groups.get() == {"revision": 0, "groups": [], "assignments": {}}
    assert not groups.path.exists()
    assert store.home.stat().st_mode & 0o777 == 0o750


def test_non_regular_groups_file_is_rejected_without_blocking(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    os.mkfifo(groups.path)
    with pytest.raises(GroupingCorrupt, match="Saved groups could not be read"):
        groups.get()


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_non_regular_group_lock_is_rejected_without_blocking(tmp_path, kind):
    groups = Groups(Store(tmp_path / "home"))
    if kind == "fifo":
        os.mkfifo(groups.lock_path)
    else:
        groups.lock_path.mkdir()

    with pytest.raises(GroupingCorrupt, match="Groups could not be read or saved"):
        groups.get()


def test_group_lock_symlink_is_rejected_without_chmodding_target(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    target = tmp_path / "external.lock"
    target.write_text("external", encoding="utf-8")
    os.chmod(target, 0o640)
    groups.lock_path.symlink_to(target)

    with pytest.raises(GroupingCorrupt, match="Groups could not be read or saved"):
        groups.get()

    assert target.read_text(encoding="utf-8") == "external"
    assert target.stat().st_mode & 0o777 == 0o640


def test_invalid_agent_records_guard_apply_without_pruning_or_writing(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    one = agent(tmp_path)
    made = groups.apply(request("create", 0, name="safe", agent_ids=[str(one.id)]), [one])
    original = groups.path.read_bytes()

    with pytest.raises(GroupingConflict, match="Agent records could not be read"):
        groups.apply(
            request("assign", 1, group_id=None, agent_ids=[str(one.id)]),
            [],
            records_valid=False,
        )
    assert groups.path.read_bytes() == original
    assert groups.get()["assignments"] == made["assignments"]


def test_two_group_instances_share_lock_for_idempotent_concurrent_retry(tmp_path):
    home = tmp_path / "home"
    first = Groups(Store(home))
    second = Groups(Store(home))
    body = request("create", 0, name="one", agent_ids=[])
    barrier = threading.Barrier(2)
    outcomes = []

    def create(groups):
        barrier.wait()
        outcomes.append(groups.apply(body, []))

    threads = [threading.Thread(target=create, args=(groups,)) for groups in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 2 and outcomes[0] == outcomes[1]
    saved = json.loads(first.path.read_text(encoding="utf-8"))
    assert len(saved["groups"]) == 1
    assert len(saved["receipts"]) == 1


def test_concurrent_same_revision_allows_one_commit(tmp_path):
    store = Store(tmp_path / "home")
    agents = [agent(tmp_path)]
    groups = Groups(store)
    barrier = threading.Barrier(2)
    outcomes = []

    def create(name):
        barrier.wait()
        try:
            outcomes.append(groups.apply(request("create", 0, name=name, agent_ids=[]), agents))
        except GroupingConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=create, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("conflict") == 1
    assert len(groups.get()["groups"]) == 1
    saved = json.loads(groups.path.read_text(encoding="utf-8"))
    assert len(saved["receipts"]) == 1
