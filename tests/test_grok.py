import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.cli import main
from swarm.importers.grok import GrokImportRequest, GrokSessions, local_path
from swarm.models import Status
from swarm.server import create_app
from swarm.sessions import AdoptRequest, Sessions
from swarm.store import Store


@pytest.fixture
def grok(tmp_path):
    root = tmp_path / "grok-sessions"
    folder = tmp_path / "project"
    folder.mkdir()
    session_id = str(uuid4())
    directory = root / quote(str(folder), safe="") / session_id
    directory.mkdir(parents=True)
    summary = {
        "info": {"id": session_id, "cwd": str(folder)},
        "chat_format_version": 1,
        "generated_title": "Parser investigation",
        "current_model_id": "grok-example",
        "created_at": "2026-01-01T12:00:00Z",
    }
    (directory / "summary.json").write_text(json.dumps(summary))
    records = [
        {"type": "system", "content": "Historical Grok system prompt, not pi instructions"},
        {
            "type": "user",
            "content": [{"type": "text", "text": "<user_info>OS: example</user_info>"}],
        },
        {
            "type": "user",
            "synthetic_reason": "system_reminder",
            "content": [{"type": "text", "text": "Old tools catalog"}],
        },
        {
            "type": "user",
            "prompt_index": 0,
            "content": [{"type": "text", "text": "<user_query>\nFix the parser\n</user_query>"}],
        },
        {
            "type": "reasoning",
            "encrypted_content": "OPAQUE-DO-NOT-REPLAY",
            "summary": [{"type": "summary_text", "text": "Inspect token handling"}],
        },
        {
            "type": "assistant",
            "content": "Reading the parser",
            "model_id": "grok-example",
            "tool_calls": [
                {"id": "call-1", "name": "read_file", "arguments": '{"target_file":"parser.py"}'}
            ],
        },
        {"type": "tool_result", "tool_call_id": "call-1", "content": "def parse(): return 42"},
        {"type": "backend_tool_call", "kind": {"web_search": {"query": "parser reference"}}},
        {
            "type": "assistant",
            "content": "The parser needs a bounds check",
            "model_id": "grok-example",
        },
    ]
    path = directory / "chat_history.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    service = GrokSessions(Store(tmp_path / "swarm"), [root])
    return service, path, folder, records


def entries(agent):
    return [json.loads(line) for line in Path(agent.session_file).read_text().splitlines()]


def test_discovers_actual_grok_directory_layout_and_ignores_prompt_history(grok):
    service, path, folder, records = grok
    (path.parent.parent / "prompt_history.jsonl").write_text("not a session")
    result = service.list()
    assert not result["errors"] and len(result["sessions"]) == 1
    meta = result["sessions"][0]
    assert meta["name"] == "Parser investigation" and meta["goal"] == "Fix the parser"
    assert meta["folder"] == str(folder) and meta["num_messages"] == len(records)
    assert meta["available"] and meta["warnings"]


def test_convert_then_adopt_offline_preserves_raw_records_and_context(grok, monkeypatch):
    service, path, folder, records = grok
    original = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    # A new converter-created pi file cannot have a pre-existing running writer.
    monkeypatch.setattr(
        "swarm.sessions.running_pi_folders",
        lambda: pytest.fail("must not block fresh import on unrelated pi cwd"),
    )
    agent = service.import_session(GrokImportRequest(session_file=str(path.parent)))
    assert agent.backend == "pi" and agent.status == Status.idle and agent.pid is None
    assert agent.managed and not agent.auto_resume and agent.folder == str(folder)
    assert agent.import_source.backend == "grok" and agent.import_source.sha256
    assert all(p.read_bytes() == data for p, data in original.items())
    converted = entries(agent)
    assert converted[0]["version"] == 3 and converted[0]["cwd"] == str(folder)
    assert agent.session_id != agent.import_source.session_id
    archived = [
        e["data"]["record"] for e in converted if e.get("customType") == "swarm.import.grok.record"
    ]
    assert archived == records
    context = [e for e in converted if e["type"] in {"message", "custom_message"}]
    encoded = json.dumps(context)
    assert "Fix the parser" in encoded and "def parse(): return 42" in encoded
    assert "target_file" in encoded and "parser reference" in encoded
    assert "Inspect token handling" in encoded
    assert "OPAQUE-DO-NOT-REPLAY" not in encoded
    assert "Historical Grok system prompt, not pi instructions" not in encoded
    assert "Old tools catalog" not in encoded
    assert not any(e["type"] == "model_change" for e in converted)
    assert not any(
        block.get("type") == "toolCall"
        for e in context
        if e["type"] == "message"
        for block in e["message"]["content"]
        if isinstance(block, dict)
    )
    parent = None
    for e in converted[1:]:
        assert e["parentId"] == parent
        parent = e["id"]
    assert (
        Sessions(service.store).metadata(Path(agent.session_file))["session_id"] == agent.session_id
    )
    assert not service.list()["sessions"][0]["available"]


def test_duplicate_and_concurrent_imports_never_overwrite_pi_history(grok):
    service, path, _, _ = grok

    def attempt(_):
        independent = GrokSessions(Store(service.store.home), service.roots)
        try:
            return independent.import_session(GrokImportRequest(session_file=str(path)))
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(a is not None for a in results) == 1
    agent = next(a for a in results if a is not None)
    saved = Path(agent.session_file).read_bytes()
    with pytest.raises(ValueError, match="Already imported"):
        service.import_session(GrokImportRequest(session_file=str(path)))
    assert Path(agent.session_file).read_bytes() == saved


def test_missing_or_moved_workspace_can_be_overridden(grok, tmp_path):
    service, path, folder, _ = grok
    folder.rmdir()
    assert not service.list()["sessions"][0]["folder_exists"]
    with pytest.raises(ValueError, match="working folder must exist"):
        service.import_session(GrokImportRequest(session_file=str(path)))
    new = tmp_path / "new-folder"
    new.mkdir()
    agent = service.import_session(
        GrokImportRequest(session_file=str(path), folder=str(new), name="New name", goal="New goal")
    )
    assert agent.folder == str(new) and agent.name == "New name" and agent.goal == "New goal"
    assert entries(agent)[0]["cwd"] == str(new)


def test_windows_mapping_and_default_discovery(grok, tmp_path, monkeypatch):
    import swarm.importers.grok as module

    mount = tmp_path / "mnt"
    (mount / "c").mkdir(parents=True)
    monkeypatch.setattr(module, "WSL_MOUNT_ROOT", mount)
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert local_path(r"C:\Users\tester\work") == mount / "c/Users/tester/work"
    with pytest.raises(ValueError, match="absolute"):
        local_path("C:relative")
    with pytest.raises(ValueError, match="cannot be mapped"):
        local_path(r"Z:\Unavailable")
    winhome = mount / "c" / "Users" / Path.home().name
    winhome.mkdir(parents=True)
    assert winhome / ".grok/sessions" in module.default_roots()


@pytest.mark.parametrize(
    "invalid", ['{"type":', "[]", '{"type":5}', '{"type":"user","content":NaN}']
)
def test_corrupt_history_is_rejected_without_creating_records(grok, invalid):
    service, path, _, _ = grok
    path.write_text(invalid)
    with pytest.raises(ValueError):
        service.import_session(GrokImportRequest(session_file=str(path)))
    assert service.store.scan() == []
    assert not list(service.store.sessions_dir.rglob("*.jsonl"))
    assert service.list()["errors"]


def test_unsupported_version_size_limits_and_outside_roots(grok, tmp_path, monkeypatch):
    import swarm.importers.grok as module

    service, path, _, _ = grok
    outside = tmp_path / "chat_history.jsonl"
    outside.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="configured Grok"):
        service.snapshot(outside)
    monkeypatch.setattr(module, "MAX_HISTORY_BYTES", 10)
    with pytest.raises(ValueError, match="limit"):
        service.snapshot(path)
    monkeypatch.setattr(module, "MAX_HISTORY_BYTES", 64 * 1024 * 1024)
    summary = json.loads((path.parent / "summary.json").read_text())
    summary["chat_format_version"] = 999
    (path.parent / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="chat_format_version"):
        service.snapshot(path)


def test_failed_adoption_removes_only_new_conversion(grok, monkeypatch):
    service, path, _, _ = grok
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(Sessions, "adopt_import", fail)
    with pytest.raises(RuntimeError, match="registration failure"):
        service.import_session(GrokImportRequest(session_file=str(path)))
    assert path.read_bytes() == original and not list(service.store.sessions_dir.rglob("*.jsonl"))


def test_changed_source_and_existing_conversion_are_not_overwritten(grok, monkeypatch):
    service, path, _, _ = grok
    snapshot = service.snapshot(path)
    path.write_text(path.read_text() + '{"type":"assistant","content":"new turn"}\n')
    with pytest.raises(ValueError, match="changed during import"):
        snapshot.assert_unchanged()
    output = service.store.sessions_dir / "imports/grok" / f"{snapshot.pi_id}.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text("Keep this existing history")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        service.import_session(GrokImportRequest(session_file=str(path)))
    assert output.read_text() == "Keep this existing history"


def test_unknown_records_and_attachments_are_preserved(grok):
    service, path, _, _ = grok
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "future_event", "payload": {"keep": True}}) + "\n")
        stream.write(
            json.dumps(
                {
                    "type": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.invalid/private"},
                        }
                    ],
                }
            )
            + "\n"
        )
    agent = service.import_session(GrokImportRequest(session_file=str(path)))
    result = json.dumps(entries(agent))
    assert "future_event" in result and "example.invalid/private" in result
    assert any("Non-text" in warning for warning in agent.import_source.warnings)


def test_import_api_and_cli(grok, tmp_path, capsys):
    service, path, _, _ = grok
    app = create_app(
        service.store.home,
        recover=False,
        grok_session_roots=service.roots,
        allowed_hosts=["testserver"],
    )
    with TestClient(app) as client:
        assert client.get("/api/grok/sessions").json()["sessions"][0]["available"]
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_json()
            result = client.post("/api/agents/import/grok", json={"session_file": str(path)})
            assert result.status_code == 201, result.text
            agent = result.json()
            assert (
                not agent["alive"]
                and not agent["auto_resume"]
                and agent["import_source"]["backend"] == "grok"
            )
            assert ws.receive_json()["agents"][0]["id"] == agent["id"]
        assert (
            client.post("/api/agents/import/grok", json={"session_file": str(path)}).status_code
            == 400
        )
        assert client.get("/api/state").json()["agents"][0]["import_source"]["session_id"]
    main(
        [
            "--home",
            str(tmp_path / "cli-home"),
            "--grok-sessions-dir",
            str(service.roots[0]),
            "grok-sessions",
        ]
    )
    assert json.loads(capsys.readouterr().out)["sessions"][0]["available"]
    main(
        [
            "--home",
            str(tmp_path / "cli-home"),
            "--grok-sessions-dir",
            str(service.roots[0]),
            "import-grok",
            str(path),
        ]
    )
    assert json.loads(capsys.readouterr().out)["status"] == "idle"


@pytest.mark.skipif(
    not shutil.which("node") or not shutil.which("pi"), reason="optional real pi parser check"
)
def test_converted_history_loads_in_real_pi_session_manager(grok):
    service, path, _, _ = grok
    executable = Path(shutil.which("pi")).resolve()
    module = executable.parent / "index.js"
    if not module.is_file():
        pytest.skip("pi installation does not expose a sibling SessionManager module")
    agent = service.import_session(GrokImportRequest(session_file=str(path)))
    script = """
        import assert from 'node:assert/strict';
        const {SessionManager, convertToLlm} = await import(process.argv[1]);
        const manager = SessionManager.open(process.argv[2]);
        const context = manager.buildSessionContext();
        assert.equal(manager.getHeader().version, 3);
        assert.equal(manager.getSessionId(), process.argv[3]);
        assert.equal(manager.getCwd(), process.argv[4]);
        assert.ok(context.model == null, 'import must not select the historical Grok model');
        const llm = convertToLlm(context.messages);
        const text = JSON.stringify(llm);
        assert.ok(text.includes('Fix the parser'));
        assert.ok(text.includes('def parse(): return 42'));
        assert.ok(!text.includes('OPAQUE-DO-NOT-REPLAY'));
        assert.ok(!text.includes('Historical Grok system prompt, not pi instructions'));
        assert.ok(!llm.some(m => m.role === 'toolResult'));
        assert.ok(!llm.some(m => Array.isArray(m.content) && m.content.some(c => c.type === 'toolCall')));
        console.log('Loaded converted history and rebuilt model context without provider calls');
    """
    result = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            script,
            module.as_uri(),
            agent.session_file,
            agent.session_id,
            agent.folder,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_specific_adoption_cannot_register_external_file(grok, tmp_path):
    service, path, _, _ = grok
    agent = service.import_session(GrokImportRequest(session_file=str(path)))
    external = tmp_path / "external.jsonl"
    external.write_bytes(Path(agent.session_file).read_bytes())
    sessions = Sessions(service.store, [tmp_path])
    with pytest.raises(ValueError, match="inside Swarm"):
        sessions.adopt_import(AdoptRequest(session_file=str(external)), agent.import_source)
