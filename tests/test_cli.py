import json
from uuid import uuid4

import pytest

from swarm.cli import main, parser, run_report
from swarm.models import Agent
from swarm.store import Store


def test_reporting_captures_pi_metadata_and_keeps_pid(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    session = tmp_path / "session.jsonl"
    agent = store.create(
        Agent(
            name="Coder",
            goal="Ship",
            folder=str(tmp_path),
            managed=True,
            session_file=str(session),
            pid=12345,
        )
    )
    session_id = str(uuid4())
    monkeypatch.setenv("PI_SESSION_ID", session_id)
    monkeypatch.setenv("PI_SESSION_FILE", str(session))
    args = parser().parse_args(
        [
            "report",
            "--id",
            str(agent.id),
            "--status",
            "dormant",
            "--wake-at",
            "2030-01-02T15:00:00Z",
            "--notes",
            "Next: check CI",
        ]
    )
    record = run_report(store, args)
    assert record["session_id"] == session_id and record["pid"] == 12345
    assert record["notes"] == "Next: check CI" and record["wake_at"]
    args = parser().parse_args(["report", "--id", str(agent.id), "--wake-at", "none"])
    assert run_report(store, args)["wake_at"] is None


def test_wrong_pi_session_cannot_overwrite_registered_identity(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    agent = store.create(
        Agent(
            name="Coder",
            goal="Ship",
            folder=str(tmp_path),
            managed=True,
            session_file=str(tmp_path / "expected.jsonl"),
        )
    )
    monkeypatch.setenv("PI_SESSION_FILE", str(tmp_path / "wrong.jsonl"))
    args = parser().parse_args(["report", "--id", str(agent.id), "--status", "done"])
    with pytest.raises(ValueError, match="different pi session"):
        run_report(store, args)
    assert store.get(agent.id).status.value == "idle"


def test_cli_lists_and_custom_roots(tmp_path, capsys):
    main(["--home", str(tmp_path), "--pi-sessions-dir", str(tmp_path / "custom"), "doctor"])
    result = json.loads(capsys.readouterr().out)
    assert result["session_roots"] == [str(tmp_path / "custom")]
    main(["--home", str(tmp_path), "list"])
    assert json.loads(capsys.readouterr().out)["agents"] == []
