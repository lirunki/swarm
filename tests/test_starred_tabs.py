import json
import os
import socket
from contextlib import ExitStack
from uuid import uuid4

from fastapi.testclient import TestClient
from test_auth import login, password_file
from test_grouping_federation import http, running_server, wait_for_authenticated_remote

from swarm.federation import FederationConfig
from swarm.processes import Processes
from swarm.relay import create_relay_app
from swarm.server import create_app
from swarm.starred_tabs import MAX_RECEIPTS, MAX_STARS, StarredTabs


def mutation(action, revision=0, request_id=None, **fields):
    return {
        "action": action,
        "revision": revision,
        "request_id": str(request_id or uuid4()),
        **fields,
    }


def agent_token():
    return f"agent:{uuid4()}"


def group_token():
    return f"agent-group:{uuid4()}"


def test_api_persists_revisioned_stars_without_requiring_registry_targets(tmp_path):
    home = tmp_path / "home"
    app = create_app(home, recover=False, allowed_hosts=["testserver"])
    unknown_agent = agent_token()
    missing_group = group_token()

    with TestClient(app) as client:
        initial = client.get("/api/starred-tabs")
        assert initial.status_code == 200
        assert initial.json() == {"revision": 0, "starred": []}
        assert not (home / "starred-tabs.json").exists()

        starred = client.post(
            "/api/starred-tabs", json=mutation("star", tab_id=unknown_agent)
        )
        assert starred.status_code == 200
        assert starred.json() == {"revision": 1, "starred": [unknown_agent]}

        imported = client.post(
            "/api/starred-tabs",
            json=mutation(
                "import",
                revision=1,
                starred=[unknown_agent, "browser", missing_group, "browser"],
            ),
        )
        assert imported.status_code == 200
        assert imported.json() == {
            "revision": 2,
            "starred": [unknown_agent, "browser", missing_group],
        }

        removed = client.post(
            "/api/starred-tabs",
            json=mutation("unstar", revision=2, tab_id=unknown_agent),
        )
        assert removed.json() == {
            "revision": 3,
            "starred": ["browser", missing_group],
        }
        state = client.get("/api/state").json()
        assert state["starred_tabs"] == removed.json()
        assert state["starred_tabs_error"] is None

    saved = json.loads((home / "starred-tabs.json").read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert len(saved["receipts"]) == 3
    assert os.stat(home / "starred-tabs.json").st_mode & 0o777 == 0o600

    restarted = create_app(home, recover=False, allowed_hosts=["testserver"])
    with TestClient(restarted) as client:
        assert client.get("/api/starred-tabs").json() == removed.json()


def test_noops_do_not_increment_and_receipts_precede_revision_conflicts(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    token = agent_token()
    with TestClient(app) as client:
        first_request = mutation("star", tab_id=token)
        first = client.post("/api/starred-tabs", json=first_request)
        assert first.json()["revision"] == 1

        noop_request = mutation("star", revision=1, tab_id=token)
        noop = client.post("/api/starred-tabs", json=noop_request)
        assert noop.status_code == 200
        assert noop.json() == first.json()

        changed = client.post(
            "/api/starred-tabs",
            json=mutation("star", revision=1, tab_id="browser"),
        )
        assert changed.json()["revision"] == 2
        replay = client.post("/api/starred-tabs", json=noop_request)
        assert replay.status_code == 200
        assert replay.json() == changed.json()
        reused = client.post(
            "/api/starred-tabs", json=noop_request | {"tab_id": group_token()}
        )
        assert reused.status_code == 409
        stale = client.post(
            "/api/starred-tabs", json=mutation("unstar", revision=1, tab_id=token)
        )
        assert stale.status_code == 409


def test_schema_tokens_merge_limit_and_receipt_bound(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        invalid = (
            mutation("star", tab_id="agent:not-a-uuid"),
            mutation("star", tab_id="folder:/private/path"),
            mutation("star", tab_id="<img src=x onerror=alert(1)>"),
            mutation("unstar", tab_id="browser") | {"starred": []},
            mutation("import", starred="browser"),
            mutation("replace", starred=[]),
            mutation("import", starred=[]) | {"name": "secret"},
            mutation("import", revision=True, starred=[]),
        )
        for body in invalid:
            response = client.post("/api/starred-tabs", json=body)
            assert response.status_code == 400
        assert client.post("/api/starred-tabs", content=b"not-json").status_code == 400

        tokens = [f"agent:{uuid4()}" for _ in range(MAX_STARS)]
        imported = client.post(
            "/api/starred-tabs",
            json=mutation("import", starred=[tokens[0], *tokens, tokens[-1]]),
        )
        assert imported.status_code == 200
        assert imported.json() == {"revision": 1, "starred": tokens}
        overflow = client.post(
            "/api/starred-tabs",
            json=mutation("star", revision=1, tab_id=group_token()),
        )
        assert overflow.status_code == 400
        assert client.get("/api/starred-tabs").json() == imported.json()

    # Receipt retention is durable and independent from revision changes.
    other = StarredTabs(tmp_path / "other")
    nearly_full = other.empty()
    nearly_full["receipts"] = [
        {"request_id": str(uuid4()), "payload_hash": "0" * 64}
        for _ in range(MAX_RECEIPTS - 1)
    ]
    other._write(nearly_full)
    for _ in range(3):
        other.apply(mutation("unstar", tab_id="browser"))
    saved = json.loads(other.path.read_text(encoding="utf-8"))
    assert saved["revision"] == 0
    assert len(saved["receipts"]) == MAX_RECEIPTS


def test_corrupt_or_unsafe_file_is_reported_without_mutation_or_content_leak(tmp_path):
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    path = app.state.controller.starred_tabs.path
    path.write_text('{"password":"must-not-echo"', encoding="utf-8")
    path.chmod(0o600)

    with TestClient(app) as client:
        get = client.get("/api/starred-tabs")
        assert get.status_code == 409 and "must-not-echo" not in get.text
        post = client.post(
            "/api/starred-tabs", json=mutation("star", tab_id="browser")
        )
        assert post.status_code == 409 and "must-not-echo" not in post.text
        state = client.get("/api/state").json()
        assert state["starred_tabs"] is None
        assert state["starred_tabs_error"] == "Saved starred tabs could not be read"
        assert "must-not-echo" not in json.dumps(state)
        assert path.read_text(encoding="utf-8") == '{"password":"must-not-echo"'

        path.unlink()
        target = tmp_path / "outside"
        target.write_text("do-not-replace", encoding="utf-8")
        path.symlink_to(target)
        assert client.get("/api/starred-tabs").status_code == 409
        assert client.post(
            "/api/starred-tabs", json=mutation("star", tab_id="browser")
        ).status_code == 409
        assert target.read_text(encoding="utf-8") == "do-not-replace"


def test_post_requires_operator_but_service_credentials_can_get(tmp_path):
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password_file(tmp_path),
    )
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        request = mutation("star", tab_id="browser")
        assert client.get("/api/starred-tabs").status_code == 401
        assert client.post("/api/starred-tabs", json=request).status_code == 401

        service = app.state.access.service_file.read_text(encoding="utf-8").strip()
        service_headers = {"Authorization": f"Bearer {service}"}
        assert client.get("/api/starred-tabs", headers=service_headers).status_code == 200
        assert (
            client.post("/api/starred-tabs", json=request, headers=service_headers).status_code
            == 403
        )
        assert not app.state.controller.starred_tabs.path.exists()

        owner_headers = login(client)
        accepted = client.post(
            "/api/starred-tabs", json=request, headers=owner_headers
        )
        assert accepted.status_code == 200


def test_federated_stars_are_authenticated_and_destination_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(Processes, "health", lambda self: {"tmux": True, "pi": True})
    key = "test-starred-tabs-federation-key-0123456789"
    passwords = {"alpha": "alpha-starred-tabs-password", "beta": "beta-starred-tabs-password"}

    with ExitStack() as stack:
        relay_port = stack.enter_context(running_server(create_relay_app([key])))
        probes = [socket.socket(), socket.socket()]
        try:
            for probe in probes:
                probe.bind(("127.0.0.1", 0))
            ports = [probe.getsockname()[1] for probe in probes]
        finally:
            for probe in probes:
                probe.close()

        apps = {}
        for node, port in zip(("alpha", "beta"), ports, strict=True):
            root = tmp_path / node
            root.mkdir()
            base = f"http://127.0.0.1:{port}"
            app = create_app(
                root / "home",
                pi="/test-only/no-pi",
                recover=False,
                api_url=base,
                password_file=password_file(root, passwords[node]),
                federation_config=FederationConfig(
                    f"ws://127.0.0.1:{relay_port}/v1/connect",
                    node,
                    node.title(),
                    key,
                    base,
                ),
            )
            apps[node] = app
            stack.enter_context(running_server(app, port))

        bases = {
            node: f"http://127.0.0.1:{port}"
            for node, port in zip(("alpha", "beta"), ports, strict=True)
        }
        auth = {}
        for node in ("alpha", "beta"):
            status, result = http(
                bases[node], "/api/auth/login", "POST", {"password": passwords[node]}
            )
            assert status == 200
            auth[node] = {"Authorization": "Bearer " + result["token"]}
        wait_for_authenticated_remote(bases["alpha"], "beta", auth["alpha"])

        destination_auth = auth["alpha"] | {
            "X-Swarm-Authorization": auth["beta"]["Authorization"]
        }
        prefix = "/api/federation/beta/proxy/api/starred-tabs"
        assert http(bases["alpha"], prefix, headers=auth["alpha"])[0] == 401
        assert http(bases["alpha"], prefix, headers=destination_auth) == (
            200,
            {"revision": 0, "starred": []},
        )

        token = agent_token()
        body = mutation("star", tab_id=token)
        assert http(bases["alpha"], prefix, "POST", body, destination_auth) == (
            200,
            {"revision": 1, "starred": [token]},
        )
        assert not apps["alpha"].state.controller.starred_tabs.path.exists()
        assert apps["beta"].state.controller.starred_tabs.get() == {
            "revision": 1,
            "starred": [token],
        }
        status, remote_state = http(
            bases["alpha"],
            "/api/federation/beta/proxy/api/state",
            headers=destination_auth,
        )
        assert status == 200
        assert remote_state["starred_tabs"] == {"revision": 1, "starred": [token]}
        assert remote_state["starred_tabs_error"] is None
