import json
import uuid
from urllib.error import URLError

import pytest

from swarm.agent_client import DEFAULT_TIMEOUT
from swarm.cli import main


class Response:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode()
        self.status = status

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def install_opener(monkeypatch, responses):
    opener = Opener(responses)
    monkeypatch.setattr("swarm.agent_client.build_opener", lambda *handlers: opener)
    return opener


def decoded_call(opener, index=0):
    request, timeout = opener.calls[index]
    body = None if request.data is None else json.loads(request.data)
    return request.get_method(), request.full_url, body, timeout


def test_message_send_generates_request_id_and_defaults_sender(monkeypatch, capsys):
    opener = install_opener(monkeypatch, [Response({"message": {"id": "m1"}}, status=202)])
    generated = uuid.UUID("6f485ccd-1a35-4eb7-a6d7-5aa15e9ba6fc")
    monkeypatch.setattr("swarm.cli.uuid.uuid4", lambda: generated)
    monkeypatch.setenv("SWARM_AGENT_ID", "sender/id")

    main(["message", "send", "recipient/id", "/still ordinary text"])

    assert json.loads(capsys.readouterr().out) == {"message": {"id": "m1"}}
    method, url, body, timeout = decoded_call(opener)
    assert (method, url) == (
        "POST",
        "http://127.0.0.1:8787/api/agents/recipient%2Fid/messages",
    )
    assert body == {
        "text": "/still ordinary text",
        "sender_id": "sender/id",
        "request_id": str(generated),
    }
    assert timeout == DEFAULT_TIMEOUT


@pytest.mark.parametrize(
    ("arguments", "method", "suffix"),
    [
        (["message", "list", "agent/a"], "GET", "/api/agents/agent%2Fa/messages"),
        (
            ["message", "delete", "agent/a", "message/b"],
            "DELETE",
            "/api/agents/agent%2Fa/messages/message%2Fb",
        ),
    ],
)
def test_message_list_and_delete_paths(arguments, method, suffix, monkeypatch, capsys):
    opener = install_opener(monkeypatch, [Response({"ok": True})])
    main(arguments)
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    actual_method, url, body, _ = decoded_call(opener)
    assert actual_method == method
    assert url == "http://127.0.0.1:8787" + suffix
    assert body is None


def test_command_targets_self_and_uses_separate_text_body(monkeypatch, capsys):
    opener = install_opener(monkeypatch, [Response({"command": {"id": "c1"}}, status=202)])
    request_id = "dcb91623-1616-4e9f-90be-17f7ba062583"
    monkeypatch.setenv("SWARM_AGENT_ID", "self-id")

    main(["command", "/compact keep tests", "--request-id", request_id])

    assert json.loads(capsys.readouterr().out) == {"command": {"id": "c1"}}
    method, url, body, _ = decoded_call(opener)
    assert (method, url) == (
        "POST",
        "http://127.0.0.1:8787/api/agents/self-id/commands",
    )
    assert body == {
        "text": "/compact keep tests",
        "sender_id": "self-id",
        "request_id": request_id,
    }


def test_local_operator_may_target_command_explicitly(monkeypatch, capsys):
    opener = install_opener(monkeypatch, [Response({"queued": True}, status=202)])
    monkeypatch.delenv("SWARM_AGENT_ID", raising=False)
    main(["command", "/reload", "--id", "agent", "--request-id", str(uuid.uuid4())])
    assert json.loads(capsys.readouterr().out) == {"queued": True}
    assert decoded_call(opener)[2]["sender_id"] == "agent"


def test_managed_command_cannot_target_another_agent(monkeypatch, capsys):
    monkeypatch.setenv("SWARM_AGENT_ID", "self")
    with pytest.raises(SystemExit):
        main(["command", "/reload", "--id", "other"])
    assert "only for itself" in capsys.readouterr().err


@pytest.mark.parametrize(
    "text",
    [
        "/new",
        "/fork anything",
        "/quit",
        "/unknown value",
        "/thinking extreme",
        "/model",
        "/reload now",
        "compact without slash",
    ],
)
def test_command_rejects_forbidden_or_unsupported_forms(text, monkeypatch, capsys):
    monkeypatch.setenv("SWARM_AGENT_ID", "self")
    with pytest.raises(SystemExit):
        main(["command", text])
    assert "swarm:" in capsys.readouterr().err


def test_invalid_request_id_and_oversized_message_fail_before_network(monkeypatch, capsys):
    opener = install_opener(monkeypatch, [])
    with pytest.raises(SystemExit):
        main(["message", "send", "agent", "hello", "--request-id", "not-a-uuid"])
    assert "must be a UUID" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["message", "send", "agent", "x" * (32 * 1024 + 1)])
    assert "32 KiB" in capsys.readouterr().err
    assert opener.calls == []


def test_timeout_reports_generated_request_id_without_retry(monkeypatch, capsys):
    opener = install_opener(monkeypatch, [URLError("lost response")])
    generated = uuid.UUID("c2347728-7c19-4860-a589-b5ed62537ec8")
    monkeypatch.setattr("swarm.cli.uuid.uuid4", lambda: generated)

    with pytest.raises(SystemExit):
        main(["message", "send", "agent", "hello"])

    error = capsys.readouterr().err
    assert str(generated) in error
    assert "swarm message list AGENT_ID" in error
    assert len(opener.calls) == 1


def test_p_is_alias_for_all_record_list(tmp_path, capsys):
    main(["--home", str(tmp_path), "p"])
    assert json.loads(capsys.readouterr().out) == {"agents": [], "errors": {}}
