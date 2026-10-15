import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.cli import main
from swarm.importers.pi_history import PiHistory
from swarm.meta_compact import digest, session_context
from swarm.models import Agent
from swarm.paths import agent_path
from swarm.server import create_app
from swarm.sessions import text_blocks
from swarm.store import Store
from swarm.tool_inputs import encode, prepare, retrieve

ARGS = {"path": "src/hello.py", "content": "print('héllo 😀')\n" * 1000}
OUTPUT = [
    {"type": "text", "text": "A very large output stays intact.\n" * 500},
    {"type": "image", "data": "YWJj", "mimeType": "image/png"},
]


def assistant(history, content, stop="toolUse"):
    history.add(
        "message",
        message={
            "role": "assistant",
            "content": content,
            "api": "openai-responses",
            "provider": "test",
            "model": "large",
            "responseId": "provider-connection-id",
            "usage": {},
            "stopReason": stop,
        },
    )
    return history.parent


def tool_result(history, call_id, output=OUTPUT):
    history.add(
        "message",
        message={
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "write",
            "isError": False,
            "content": copy.deepcopy(output),
        },
    )
    return history.parent


def sample(folder, complete=True):
    history = PiHistory(str(uuid4()), str(folder), "Input-heavy session")
    history.user("Keep the goal, conversation and tool outputs unchanged.")
    call = {
        "type": "toolCall",
        "id": "call_first|fc_original",
        "name": "write",
        "arguments": copy.deepcopy(ARGS),
    }
    aid = assistant(history, [{"type": "text", "text": "Writing the file."}, call])
    rid = tool_result(history, call["id"]) if complete else None
    return history, aid, rid


@pytest.fixture
def app_agent(tmp_path, monkeypatch):
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    app = create_app(tmp_path / "swarm", recover=False, allowed_hosts=["testserver"])
    ctl = app.state.controller
    monkeypatch.setattr(ctl.processes, "alive", lambda a: False)
    history, aid, rid = sample(tmp_path)
    path = ctl.store.sessions_dir / "saved.jsonl"
    # Exact archive must retain original spelling/line endings, not re-serialized JSON.
    data = b"\xef\xbb\xbf" + history.encode().replace("\n", "\r\n").encode()
    path.write_bytes(data)
    agent = ctl.store.create(
        Agent(
            name="Example",
            goal="Implement",
            folder=str(tmp_path),
            managed=True,
            session_id=history.entries[0]["id"],
            session_file=str(path),
            auto_resume=False,
            notes="Keep notes",
        )
    )
    return app, agent, path, aid, rid


def test_compact_keeps_outputs_identity_archive_and_exact_retrieval(app_agent, capsys):
    app, agent, path, aid, rid = app_agent
    original = path.read_bytes()
    with TestClient(app) as client:
        result = client.post(f"/api/agents/{agent.id}/compact-tool-inputs", json={})
        assert result.status_code == 200, result.text
        report = result.json()
        assert report["status"] == "completed" and report["calls_compacted"] == 1
        assert report["estimated_tokens_saved"] > 0
        assert Path(report["archive"]).read_bytes() == original
        header, branch, selected, model = session_context(path.read_bytes())
        assert header["id"] == agent.session_id and model == "test/large"
        assert header["cwd"] == agent_path(agent.folder)
        entries = {entry["id"]: entry for entry in branch}
        assert entries[aid]["type"] == "custom_message"
        assert entries[rid]["content"][1:] == OUTPUT
        assert (
            entries[rid]["details"]["original_message_metadata"]["toolCallId"]
            == "call_first|fc_original"
        )
        assert "Writing the file." in encode(entries[aid])
        assert not any(e.get("message", {}).get("role") == "toolResult" for e in selected)
        index = json.loads((Path(report["directory"]) / "index.json").read_text())
        toolrun_id = next(iter(index["calls"]))
        assert toolrun_id in encode(entries[aid])
        assert encode(ARGS)[:100] in "\n".join(b["text"] for b in entries[aid]["content"])
        assert ARGS["content"] not in encode(entries[aid])
        store = app.state.controller.store
        parts, offset = [], 0
        while True:
            page = retrieve(store, toolrun_id, offset, 713)
            parts.append(page["input"])
            offset = page["next_offset"]
            if page["eof"]:
                break
        assert json.loads("".join(parts)) == ARGS
        main(
            ["--home", str(store.home), "tool-input", toolrun_id, "--offset", "0", "--limit", "100"]
        )
        assert json.loads(capsys.readouterr().out)["input"] == encode(ARGS)[:100]
        current = store.get(agent.id)
        assert current.session_file == agent.session_file and current.notes == agent.notes
        assert current.session_id == agent.session_id and not current.auto_resume
        assert current.tool_input_compaction["status"] == "completed"
        compacted = path.read_bytes()
        again = client.post(f"/api/agents/{agent.id}/compact-tool-inputs", json={}).json()
        assert again["status"] == "unchanged" and path.read_bytes() == compacted
        assert json.loads(retrieve(store, toolrun_id, 0, 64000)["input"]) == ARGS


@pytest.mark.parametrize(
    "case",
    ["pending", "partial-group", "foreign-result", "duplicate-result", "bad-arguments", "tiny"],
)
def test_unsafe_or_unprofitable_groups_are_unchanged(tmp_path, case):
    history, aid, _ = sample(tmp_path, complete=False)
    if case == "partial-group":
        history.entries[-1]["message"]["content"].append(
            {"type": "toolCall", "id": "second", "name": "read", "arguments": {"path": "x"}}
        )
        tool_result(history, "call_first|fc_original")
    elif case == "foreign-result":
        tool_result(history, "unknown")
        tool_result(history, "call_first|fc_original")
    elif case == "duplicate-result":
        tool_result(history, "call_first|fc_original")
        tool_result(history, "call_first|fc_original")
    elif case == "bad-arguments":
        history.entries[-1]["message"]["content"].append(
            {"type": "toolCall", "id": "second", "name": "read", "arguments": None}
        )
        tool_result(history, "call_first|fc_original")
        tool_result(history, "second")
    elif case == "tiny":
        history.entries[-1]["message"]["content"][1]["arguments"] = {"command": "pwd"}
        tool_result(history, "call_first|fc_original")
    replacement, lookup, stats = prepare(history.encode().encode(), tmp_path, uuid4().hex)
    assert replacement is None and not lookup and stats["calls_compacted"] == 0


def test_mixed_completed_calls_all_inert_but_only_large_input_elided(tmp_path):
    history, aid, _ = sample(tmp_path, complete=False)
    history.entries[-1]["message"]["content"].append(
        {"type": "toolCall", "id": "small", "name": "bash", "arguments": {"command": "pwd"}}
    )
    rid = tool_result(history, "call_first|fc_original")
    other = tool_result(history, "small", [{"type": "text", "text": "workspace"}])
    result, lookup, _ = prepare(history.encode().encode(), tmp_path, uuid4().hex)
    _, branch, _, _ = session_context(result.encode())
    entries = {e["id"]: e for e in branch}
    assert len(lookup) == 1
    assert '\\"command\\":\\"pwd\\"' in encode(entries[aid])
    assert entries[rid]["content"][1:] == OUTPUT
    assert entries[other]["content"][1:] == [{"type": "text", "text": "workspace"}]


def test_context_edits_and_abandoned_branch_are_not_reintroduced(tmp_path):
    history, aid, rid = sample(tmp_path)
    # A repeated edit must not corrupt IDs/parent links or reintroduce original arguments.
    modified = copy.deepcopy(history.entries[-2]["message"]["content"])
    modified[1]["arguments"]["content"] = "corrected\n" * 1000
    history.add("context_edit", targetId=aid, replacement={"content": modified})
    history.add("context_edit", targetId=aid, replacement={"content": modified})
    history.add("context_edit", targetId=rid, replacement={"content": "Corrected tool output"})
    active_parent = history.parent
    history.user("Abandoned task")
    history.parent = active_parent
    history.user("Current task")
    result, lookup, _ = prepare(history.encode().encode(), tmp_path, uuid4().hex)
    _, branch, _, _ = session_context(result.encode())
    assert "Abandoned task" not in result and "Current task" in result
    entries = {e["id"]: e for e in branch}
    assert entries[rid]["content"][-1]["text"] == "Corrected tool output"
    assert next(iter(lookup.values()))["arguments"] == modified[1]["arguments"]
    assert not any(
        e.get("type") == "context_edit" and e.get("targetId") in {aid, rid} for e in branch
    )


def test_windows_imported_inert_tool_call_is_shortened_without_touching_output(tmp_path):
    history, _, _ = sample(tmp_path)
    imported = PiHistory(str(uuid4()), str(tmp_path), "Imported Windows")
    output_entry = None
    for index, record in enumerate(history.entries[1:]):
        imported.add(
            "custom", customType="swarm.import.pi.record", data={"index": index, "record": record}
        )
        message = record.get("message", {})
        if message.get("role") == "assistant":
            imported.assistant(text_blocks(message["content"]), "large")
        elif message.get("role") == "toolResult":
            imported.historical("Pi toolResult", text_blocks(message["content"]))
            output_entry = copy.deepcopy(imported.entries[-1])
        elif message.get("role") == "user":
            imported.user(message["content"])
    result, lookup, stats = prepare(imported.encode().encode(), tmp_path, uuid4().hex)
    assert stats["calls_compacted"] == 1 and next(iter(lookup.values()))["arguments"] == ARGS
    _, branch, selected, _ = session_context(result.encode())
    assert next(e for e in branch if e["id"] == output_entry["id"]) == output_entry
    visible = [e for e in selected if e.get("type") == "custom_message"]
    assert "toolrun_" in encode(visible)
    assert json.dumps(ARGS["content"], ensure_ascii=False) not in encode(visible)


@pytest.mark.parametrize("change", ["write-failure", "source-change", "external-process"])
def test_failure_keeps_source_and_archive(app_agent, monkeypatch, change):
    app, agent, path, _, _ = app_agent
    import swarm.tool_inputs as module

    original = path.read_bytes()
    real_write = module.atomic_write

    def write(target, text):
        if change == "write-failure" and target == path:
            raise OSError("disk full")
        real_write(target, text)
        if target.name == "replacement.jsonl":
            if change == "source-change":
                path.write_bytes(original + b"\n")
            elif change == "external-process":
                monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [agent.folder])

    monkeypatch.setattr(module, "atomic_write", write)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(f"/api/agents/{agent.id}/compact-tool-inputs", json={})
        assert response.status_code in (409, 500)
        report = app.state.controller.store.get(agent.id).tool_input_compaction
        assert report["status"] == "failed"
        assert Path(report["archive"]).read_bytes() == original
        assert path.read_bytes() == original + (b"\n" if change == "source-change" else b"")


def test_live_busy_and_origin_guards(app_agent, monkeypatch):
    app, agent, path, _, _ = app_agent
    with TestClient(app) as client:
        url = f"/api/agents/{agent.id}/compact-tool-inputs"
        assert (
            client.post(url, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        )
        monkeypatch.setattr(app.state.controller.processes, "alive", lambda a: True)
        assert client.post(url, json={}).status_code == 409
        monkeypatch.setattr(app.state.controller.processes, "alive", lambda a: False)
        store = app.state.controller.store
        for field in ("meta_compaction", "tool_input_compaction"):
            store.update(agent.id, {field: {"status": "running"}})
            for suffix in ("compact-tool-inputs", "meta-compact", "wake"):
                assert client.post(f"/api/agents/{agent.id}/{suffix}", json={}).status_code == 409
            assert client.patch(f"/api/agents/{agent.id}", json={"name": "Oops"}).status_code == 409
            assert client.delete(f"/api/agents/{agent.id}").status_code == 409
            store.update(agent.id, {field: {"status": "failed"}})


@pytest.mark.parametrize("replaced", [True, False])
def test_restart_reconciles_tool_compaction(app_agent, replaced):
    app, agent, path, _, _ = app_agent
    store = app.state.controller.store
    directory = store.home / "tool-input-compactions" / uuid4().hex
    directory.mkdir(parents=True)
    job = {
        "status": "running",
        "source": str(path),
        "directory": str(directory),
        "replacement_sha256": digest(path.read_bytes()) if replaced else "not-committed",
    }
    app.state.tool_input_compactor.save(agent.id, job)
    with TestClient(app):
        record = store.get(agent.id)
        assert record.tool_input_compaction["status"] == ("completed" if replaced else "failed")
        assert not record.auto_resume


def test_lookup_rejects_tampering_and_escaping_paths(app_agent, tmp_path):
    app, agent, _, _, _ = app_agent
    with TestClient(app) as client:
        report = client.post(f"/api/agents/{agent.id}/compact-tool-inputs", json={}).json()
        directory = Path(report["directory"])
        toolrun_id = next(iter(json.loads((directory / "index.json").read_text())["calls"]))
        store = app.state.controller.store
        for value, offset, limit in (
            ("../secret", 0, 10),
            (toolrun_id, -1, 10),
            (toolrun_id, 0, 64001),
            (toolrun_id, 10**9, 10),
        ):
            with pytest.raises(ValueError):
                retrieve(store, value, offset, limit)
        (directory / f"{toolrun_id}.json").write_text("tampered")
        with pytest.raises(ValueError, match="checksum"):
            retrieve(store, toolrun_id)
    other_store = Store(tmp_path / "other-home")
    outside = tmp_path / "outside"
    outside.mkdir()
    (other_store.home / "tool-input-compactions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        retrieve(other_store, toolrun_id)
