import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.cli import main
from swarm.models import Agent
from swarm.notifications import Notifications
from swarm.server import create_app
from swarm.store import Store


def make_agent(store, tmp_path, **fields):
    return store.create(
        Agent(name="Notifier", goal="Finish safely", folder=str(tmp_path), managed=True, **fields)
    )


def test_notification_store_is_idempotent_bounded_and_retains_deleted_receipt(tmp_path):
    store = Store(tmp_path / "home")
    agent = make_agent(store, tmp_path, status="waiting", wait_reason="Needs user review")
    notifications = Notifications(store)
    request_id = uuid4()

    first = notifications.post(agent.id, "Please review\nresult", request_id)
    repeated = Notifications(store).post(agent.id, "Please review\nresult", request_id)
    assert first["reused"] is False and repeated["reused"] is True
    assert first["notification"] == repeated["notification"]
    assert notifications.summary() == {"count": 1, "revision": 1}
    assert (store.home / "notifications.lock").is_file()

    deleted = notifications.delete(request_id)
    again = Notifications(store).delete(request_id)
    assert deleted["revision"] == 2 and again["revision"] == 2 and again["reused"]
    assert notifications.list() == {"notifications": [], "count": 0, "revision": 2}
    stored = notifications.read().notifications[0]
    assert stored.text == "" and stored.payload_sha256
    with pytest.raises(RuntimeError, match="different notification content"):
        notifications.post(agent.id, "A different payload", request_id)


@pytest.mark.parametrize(
    ("status", "wait_reason", "allowed"),
    [
        ("waiting", "blocked", True),
        ("waiting", "   ", False),
        ("done", "", True),
        ("running", "", False),
    ],
)
def test_notification_post_requires_persisted_terminal_status(
    tmp_path, status, wait_reason, allowed
):
    store = Store(tmp_path / status / "home")
    agent = make_agent(store, tmp_path, status=status, wait_reason=wait_reason)
    if allowed:
        assert (
            Notifications(store).post(agent.id, "Status update")["notification"]["status"] == status
        )
    else:
        with pytest.raises(ValueError, match="only while waiting"):
            Notifications(store).post(agent.id, "Status update")


def test_delete_all_marks_every_unread_notification_and_is_idempotent(tmp_path):
    store = Store(tmp_path / "home")
    agent = make_agent(store, tmp_path, status="done")
    notifications = Notifications(store)
    for text in ("First", "Second", "Third"):
        notifications.post(agent.id, text)

    cleared = notifications.delete_all()
    repeated = notifications.delete_all()

    assert cleared == {"deleted": 3, "revision": 4, "reused": False}
    assert repeated == {"deleted": 0, "revision": 4, "reused": True}
    assert notifications.list() == {"notifications": [], "count": 0, "revision": 4}
    assert all(item.state == "deleted" and item.text == "" for item in notifications.read().notifications)


def test_notification_text_validation_and_concurrent_atomic_posts(tmp_path):
    store = Store(tmp_path / "home")
    agent = make_agent(store, tmp_path, status="done")
    notifications = Notifications(store)
    for text in ("   ", "unsafe\x00text", "x" * (32 * 1024 + 1)):
        with pytest.raises(ValueError):
            notifications.post(agent.id, text)

    ids = [uuid4() for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda item: Notifications(store).post(agent.id, str(item), item), ids))
    assert notifications.summary()["count"] == len(ids)
    json.loads((store.home / "notifications.json").read_text(encoding="utf-8"))


def test_notify_cli_writes_without_controller_and_checks_session_owner(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    store = Store(home)
    session = tmp_path / "owned.jsonl"
    agent = make_agent(
        store,
        tmp_path,
        status="done",
        session_id="owned-session",
        session_file=str(session),
    )
    request_id = str(uuid4())
    monkeypatch.setenv("SWARM_AGENT_ID", str(agent.id))
    monkeypatch.setenv("PI_SESSION_ID", "owned-session")
    monkeypatch.setenv("PI_SESSION_FILE", str(session))

    main(["--home", str(home), "notify", "Work is complete", "--request-id", request_id])
    result = json.loads(capsys.readouterr().out)
    assert result["notification"]["id"] == request_id
    assert result["notification"]["agent_name"] == "Notifier"

    monkeypatch.setenv("PI_SESSION_FILE", str(tmp_path / "other.jsonl"))
    with pytest.raises(SystemExit):
        main(["--home", str(home), "notify", "Another update"])
    assert "different pi session" in capsys.readouterr().err

    other = make_agent(store, tmp_path, status="done")
    with pytest.raises(SystemExit):
        main(["--home", str(home), "notify", "Spoofed", "--id", str(other.id)])
    assert "only for itself" in capsys.readouterr().err


def test_notifications_api_state_delete_and_websocket_broadcast(tmp_path):
    home = tmp_path / "home"
    app = create_app(home, recover=False, allowed_hosts=["testserver"])
    store = app.state.controller.store
    agent = make_agent(store, tmp_path, status="done")
    posted = Notifications(store).post(agent.id, "Finished the requested work")
    notification_id = posted["notification"]["id"]

    with TestClient(app) as client, client.websocket_connect("/ws/state") as websocket:
        initial = websocket.receive_json()
        assert initial["notifications"] == {"count": 1, "revision": 1}
        listing = client.get("/api/notifications")
        assert listing.status_code == 200
        assert listing.json()["notifications"][0]["id"] == notification_id

        removed = client.delete(f"/api/notifications/{notification_id}")
        assert removed.status_code == 200 and removed.json()["deleted"]
        changed = websocket.receive_json()
        assert changed["notifications"] == {"count": 0, "revision": 2}
        repeated = client.delete(f"/api/notifications/{notification_id}")
        assert repeated.status_code == 200 and repeated.json()["reused"]
        assert client.get("/api/notifications").json()["notifications"] == []

        Notifications(store).post(agent.id, "Another update")
        Notifications(store).post(agent.id, "One more update")
        cleared = client.delete("/api/notifications")
        assert cleared.status_code == 200
        assert cleared.json()["deleted"] == 2
        changed = websocket.receive_json()
        assert changed["notifications"]["count"] == 0
        assert client.get("/api/notifications").json()["notifications"] == []
