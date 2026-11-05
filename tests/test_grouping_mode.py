import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.grouping import GroupingConflict, GroupingError, Groups
from swarm.models import Agent
from swarm.server import create_app
from swarm.store import Store


def agent(tmp_path, *, parent_id=None):
    return Agent(
        id=uuid4(),
        parent_id=parent_id,
        name="grouping mode agent",
        goal="test owner grouping policy",
        folder=str(tmp_path),
        auto_resume=False,
    )


def operation(action, revision, **fields):
    return {"action": action, "revision": revision, "request_id": str(uuid4()), **fields}


def test_new_store_defaults_to_browser_mode_without_creating_a_file(tmp_path):
    groups = Groups(Store(tmp_path / "home"))

    assert groups.settings() == {
        "revision": 0,
        "server_managed": False,
        "initialized": False,
        "grouped_branches": [],
    }
    assert groups.get() == {"revision": 0, "groups": [], "assignments": {}}
    assert not groups.path.exists()


def test_fresh_old_client_mutation_is_rejected_without_canonical_write(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    body = operation("create", 0, name="Old client", agent_ids=[])

    with pytest.raises(GroupingConflict, match="enable server-managed grouping"):
        groups.apply(body, [])

    assert not groups.path.exists()
    assert groups.get() == {"revision": 0, "groups": [], "assignments": {}}


def test_legacy_v1_projects_server_mode_and_migrates_only_on_mutation(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    group_id = str(uuid4())
    legacy = {
        "version": 1,
        "revision": 4,
        "groups": [{"id": group_id, "name": "Existing"}],
        "assignments": {},
        "receipts": [],
    }
    groups.path.write_text(json.dumps(legacy), encoding="utf-8")
    original = groups.path.read_bytes()

    assert groups.settings() == {
        "revision": 4,
        "server_managed": True,
        "initialized": False,
        "grouped_branches": [],
    }
    assert groups.path.read_bytes() == original

    created = groups.apply(
        operation("create", groups.get()["revision"], name="Legacy client", agent_ids=[]),
        [],
    )
    assert created["operation"]["action"] == "create"
    saved = json.loads(groups.path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["settings"]["server_managed"] is True
    assert saved["groups"][:1] == legacy["groups"]


def test_first_enable_seed_replaces_explicitly_then_disable_and_reenable_preserve(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    root = agent(tmp_path)
    child = agent(tmp_path, parent_id=root.id)
    seeded_group = str(uuid4())
    seed = {
        "groups": [{"id": seeded_group, "name": "Current browser view"}],
        "assignments": {str(child.id): seeded_group},
        "grouped_branches": [str(root.id)],
    }

    enabled = groups.apply_settings(
        operation(
            "configure",
            groups.settings()["revision"],
            server_managed=True,
            seed=seed,
        ),
        [root, child],
    )
    assert enabled["server_managed"] is True and enabled["initialized"] is True
    assert groups.get()["groups"] == seed["groups"]
    assert groups.get()["assignments"] == {
        str(root.id): seeded_group,
        str(child.id): seeded_group,
    }

    disabled = groups.apply_settings(
        operation("configure", enabled["revision"], server_managed=False), [root, child]
    )
    canonical = groups.get()
    assert disabled["server_managed"] is False
    assert disabled["grouped_branches"] == [str(root.id)]

    reenabled = groups.apply_settings(
        operation("configure", disabled["revision"], server_managed=True), [root, child]
    )
    assert reenabled["initialized"] is True
    assert groups.get()["groups"] == canonical["groups"]
    assert groups.get()["assignments"] == canonical["assignments"]
    with pytest.raises(GroupingConflict, match="first explicit"):
        groups.apply_settings(
            operation(
                "configure",
                reenabled["revision"],
                server_managed=True,
                seed=seed | {"groups": []},
            ),
            [root, child],
        )


def test_disabled_mode_retains_canonical_groups_rejects_new_mutation_and_replays_receipt(
    tmp_path,
):
    groups = Groups(Store(tmp_path / "home"))
    enabled = groups.apply_settings(
        operation("configure", groups.settings()["revision"], server_managed=True), []
    )
    create = operation("create", enabled["revision"], name="Committed", agent_ids=[])
    committed = groups.apply(create, [])
    disabled = groups.apply_settings(
        operation("configure", committed["revision"], server_managed=False), []
    )
    canonical = groups.get()
    original = groups.path.read_bytes()

    assert groups.apply(create, []) == canonical | {"operation": committed["operation"]}
    with pytest.raises(GroupingConflict, match="enable server-managed grouping"):
        groups.apply(
            operation("rename", disabled["revision"], group_id=committed["operation"]["group_id"], name="No"),
            [],
        )

    assert groups.path.read_bytes() == original
    assert groups.get() == canonical


def test_disabled_mode_checks_invalid_records_before_policy(tmp_path):
    groups = Groups(Store(tmp_path / "home"))

    with pytest.raises(GroupingConflict, match="Agent records could not be read"):
        groups.apply(
            operation("create", 0, name="Rejected", agent_ids=[]),
            [],
            records_valid=False,
        )
    assert not groups.path.exists()


def test_seed_and_branch_validation_never_partially_mutate(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    root = agent(tmp_path)
    child = agent(tmp_path, parent_id=root.id)
    first, second = str(uuid4()), str(uuid4())
    conflicting_seed = {
        "groups": [{"id": first, "name": "One"}, {"id": second, "name": "Two"}],
        "assignments": {str(root.id): first, str(child.id): second},
        "grouped_branches": [],
    }

    with pytest.raises(GroupingError, match="family"):
        groups.apply_settings(
            operation("configure", 0, server_managed=True, seed=conflicting_seed),
            [root, child],
        )
    assert groups.settings()["revision"] == 0
    assert not groups.path.exists()

    enabled = groups.apply_settings(operation("configure", 0, server_managed=True), [root, child])
    with pytest.raises(GroupingError, match="distinct agents"):
        groups.apply_settings(
            operation("branches", enabled["revision"], grouped_branches=[str(uuid4())]),
            [root, child],
        )
    assert groups.settings()["revision"] == enabled["revision"]


def test_branches_require_server_mode_but_legacy_uninitialized_mode_allows_them(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    one = agent(tmp_path)
    with pytest.raises(GroupingConflict, match="server-managed"):
        groups.apply_settings(
            operation("branches", 0, grouped_branches=[str(one.id)]), [one]
        )

    groups.path.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 0,
                "groups": [],
                "assignments": {},
                "receipts": [],
            }
        ),
        encoding="utf-8",
    )
    updated = groups.apply_settings(
        operation("branches", 0, grouped_branches=[str(one.id)]), [one]
    )
    assert updated["initialized"] is False
    assert updated["grouped_branches"] == [str(one.id)]

    projection, settings, error = groups.refresh_with_settings([])
    assert error is None and projection["revision"] == 2
    assert settings["grouped_branches"] == []


def test_settings_receipts_bad_records_and_concurrent_revision_are_safe(tmp_path):
    groups = Groups(Store(tmp_path / "home"))
    one = agent(tmp_path)
    body = operation("configure", 0, server_managed=True)
    enabled = groups.apply_settings(body, [one])
    assert groups.apply_settings(body, [one], records_valid=False) == enabled
    with pytest.raises(GroupingConflict, match="different operation"):
        groups.apply_settings(body | {"server_managed": False}, [one])
    with pytest.raises(GroupingConflict, match="Agent records"):
        groups.apply_settings(
            operation("branches", enabled["revision"], grouped_branches=[]),
            [],
            records_valid=False,
        )

    barrier = threading.Barrier(2)
    outcomes = []

    def update(value):
        barrier.wait()
        try:
            outcomes.append(
                groups.apply_settings(
                    operation("configure", enabled["revision"], server_managed=value), [one]
                )
            )
        except GroupingConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=update, args=(value,)) for value in (False, True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("conflict") == 1


def test_branches_and_groups_api_share_one_concurrent_revision(tmp_path):
    home = tmp_path / "home"
    root = agent(tmp_path)
    Store(home).create(root)
    app = create_app(home, recover=False, allowed_hosts=["testserver"])

    with TestClient(app) as client:
        enable = operation("configure", 0, server_managed=True)
        enabled = client.post("/api/grouping-settings", json=enable)
        assert enabled.status_code == 200
        assert enabled.json()["initialized"] is True

        created_request = operation(
            "create", enabled.json()["revision"], name="Preserved group", agent_ids=[]
        )
        created = client.post("/api/groups", json=created_request)
        assert created.status_code == 200
        group_id = created.json()["operation"]["group_id"]
        initial_branch_request = operation(
            "branches",
            created.json()["revision"],
            grouped_branches=[str(root.id)],
        )
        initial_branches = client.post(
            "/api/grouping-settings", json=initial_branch_request
        )
        assert initial_branches.status_code == 200

        revision = initial_branches.json()["revision"]
        branch_request = operation("branches", revision, grouped_branches=[])
        group_request = operation(
            "rename", revision, group_id=group_id, name="Concurrent rename"
        )
        barrier = threading.Barrier(2)
        outcomes = {}

        def post(name, path, body):
            barrier.wait()
            response = client.post(path, json=body)
            outcomes[name] = (response.status_code, response.json())

        threads = [
            threading.Thread(
                target=post,
                args=("branches", "/api/grouping-settings", branch_request),
            ),
            threading.Thread(target=post, args=("groups", "/api/groups", group_request)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(status for status, _ in outcomes.values()) == [200, 409]
        loser = next(value for value in outcomes.values() if value[0] == 409)
        assert "revision changed" in loser[1]["detail"].lower()

        settings_value = client.get("/api/grouping-settings").json()
        groups_value = client.get("/api/groups").json()
        state = client.get("/api/state").json()
        assert settings_value["revision"] == groups_value["revision"] == revision + 1
        assert state["grouping_settings"] == settings_value
        assert state["agent_groups"] == groups_value
        if outcomes["branches"][0] == 200:
            assert settings_value["grouped_branches"] == []
            assert groups_value["groups"] == [
                {"id": group_id, "name": "Preserved group"}
            ]
        else:
            assert settings_value["grouped_branches"] == [str(root.id)]
            assert groups_value["groups"] == [
                {"id": group_id, "name": "Concurrent rename"}
            ]

    saved = json.loads((home / "groups.json").read_text(encoding="utf-8"))
    assert saved["revision"] == revision + 1
    assert [receipt["request_id"] for receipt in saved["receipts"]] == [
        enable["request_id"],
        created_request["request_id"],
        initial_branch_request["request_id"],
        (branch_request if outcomes["branches"][0] == 200 else group_request)[
            "request_id"
        ],
    ]


def test_grouping_settings_api_and_state_keep_legacy_groups_projection(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        assert client.get("/api/grouping-settings").json() == {
            "revision": 0,
            "server_managed": False,
            "initialized": False,
            "grouped_branches": [],
        }
        body = operation("configure", 0, server_managed=True)
        response = client.post("/api/grouping-settings", json=body)
        assert response.status_code == 200
        settings = client.get("/api/grouping-settings").json()
        assert {key: response.json()[key] for key in settings} == settings
        state = client.get("/api/state").json()
        assert state["grouping_settings"] == settings
        assert state["grouping_settings_error"] is None
        assert client.get("/api/groups").json() == {
            "revision": 1,
            "groups": [],
            "assignments": {},
        }

        oversized = b'{' + b' ' * (128 * 1024)
        assert client.post(
            "/api/grouping-settings",
            content=oversized,
            headers={"content-type": "application/json"},
        ).status_code == 400
