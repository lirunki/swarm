import json
from urllib.error import URLError

import pytest

from swarm.agent_client import DEFAULT_TIMEOUT, AgentClient, validate_api_url
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


def test_spawn_uses_managed_parent_and_complete_payload(tmp_path, monkeypatch, capsys):
    opener = install_opener(
        monkeypatch, [Response({"agent": {"id": "child"}, "reused": False}, status=201)]
    )
    monkeypatch.setenv("SWARM_AGENT_ID", "parent/id")
    repository = tmp_path / "repo"
    worktree = tmp_path / "new-worktree"

    main(
        [
            "spawn",
            "--request-id",
            "stable-task-v1",
            "--name",
            "Coder",
            "--goal",
            "Ship it",
            "--repository",
            str(repository),
            "--worktree",
            str(worktree),
            "--branch",
            "agent/parser",
            "--context",
            "fork",
            "--model",
            "provider/model",
            "--instructions",
            "Run tests",
        ]
    )

    assert json.loads(capsys.readouterr().out)["reused"] is False
    method, url, body, _ = decoded_call(opener)
    assert (method, url) == (
        "POST",
        "http://127.0.0.1:8787/api/agents/parent%2Fid/children",
    )
    assert body == {
        "request_id": "stable-task-v1",
        "name": "Coder",
        "goal": "Ship it",
        "instructions": "Run tests",
        "repository": str(repository),
        "worktree": str(worktree),
        "branch": "agent/parser",
        "base": "HEAD",
        "context": "fork",
        "model": "provider/model",
    }


@pytest.mark.parametrize(
    ("arguments", "method", "suffix", "body"),
    [
        (["agent", "list"], "GET", "", None),
        (["agent", "status", "child/a"], "GET", "/child%2Fa", None),
        (
            ["agent", "message", "child", "hello"],
            "POST",
            "/child/message",
            {"message": "hello"},
        ),
        (
            ["agent", "wake", "child", "--message", "go"],
            "POST",
            "/child/wake",
            {"message": "go"},
        ),
        (["agent", "stop", "child"], "POST", "/child/stop", {"force": False}),
        (["agent", "stop", "child", "--force"], "POST", "/child/stop", {"force": True}),
        (["agent", "wait", "child"], "POST", "/child/wait", {}),
    ],
)
def test_agent_commands_use_only_http(arguments, method, suffix, body, monkeypatch, capsys):
    opener = install_opener(monkeypatch, [Response({"ok": True})])
    main(["--parent-id", "operator", *arguments])
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    actual_method, url, actual_body, timeout = decoded_call(opener)
    assert actual_method == method
    assert url == f"http://127.0.0.1:8787/api/agents/operator/children{suffix}"
    assert actual_body == body
    assert timeout == DEFAULT_TIMEOUT


def test_orchestration_requires_parent_and_absolute_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SWARM_AGENT_ID", raising=False)
    with pytest.raises(SystemExit):
        main(["agent", "list"])
    assert "managed context" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(
            [
                "--parent-id",
                "p",
                "spawn",
                "--request-id",
                "key",
                "--name",
                "n",
                "--goal",
                "g",
                "--repository",
                "relative",
                "--worktree",
                str(tmp_path / "worktree"),
                "--branch",
                "new",
            ]
        )
    assert "--repository must be an absolute path" in capsys.readouterr().err


def test_client_allows_only_loopback_http_and_disables_proxy_redirects(monkeypatch):
    handlers = []
    monkeypatch.setattr(
        "swarm.agent_client.build_opener", lambda *values: handlers.extend(values) or Opener([])
    )
    assert validate_api_url("http://[::1]:8787/") == "http://[::1]:8787"
    AgentClient("http://localhost:8787")
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, "", {}, "http://127.0.0.1") is None
    for url in ("https://127.0.0.1", "http://example.com", "http://user@localhost:8787"):
        with pytest.raises(ValueError):
            validate_api_url(url)


def test_transport_error_gives_idempotent_spawn_guidance(monkeypatch):
    opener = install_opener(monkeypatch, [URLError("lost response")])
    client = AgentClient("http://127.0.0.1:8787", opener=opener)
    with pytest.raises(RuntimeError, match="same --request-id"):
        client.spawn("parent", {"request_id": "stable"})


def test_serve_passes_public_api_url(tmp_path, monkeypatch):
    captured = {}

    def create_app(home, **kwargs):
        captured.update(home=home, **kwargs)
        return object()

    monkeypatch.setattr("swarm.server.create_app", create_app)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app, run=kwargs))
    main(["--home", str(tmp_path), "serve", "--host", "::1", "--port", "9000"])
    assert captured["api_url"] == "http://[::1]:9000"
