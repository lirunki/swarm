import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from swarm.importers.pi_history import PiHistory
from swarm.meta_compact import digest
from swarm.models import Agent
from swarm.server import create_app
from swarm.store import Store
from swarm.tool_outputs import (
    HEAD_BYTES,
    MAX_OUTPUT_BYTES,
    TAIL_BYTES,
    ToolOutputRepair,
    output_size,
    prepare_repair,
    retrieve_output,
)


def assistant(history, calls):
    history.add(
        "message",
        message={
            "role": "assistant",
            "content": [
                {"type": "text", "text": "The completed tool call follows."},
                *copy.deepcopy(calls),
            ],
            "api": "openai-responses",
            "provider": "test",
            "model": "large",
            "responseId": "response-original",
            "usage": {"input": 1, "output": 1},
            "stopReason": "toolUse",
        },
    )
    return history.parent


def tool_result(history, call_id, content, *, name="bash", error=False):
    history.add(
        "message",
        message={
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": name,
            "isError": error,
            "content": copy.deepcopy(content),
        },
    )
    return history.parent


def native_history(folder, content, *, error=False):
    history = PiHistory(uuid4().hex, str(folder), "Tool output repair")
    history.user("Preserve the completed call and repair only its oversized output.")
    call = {
        "type": "toolCall",
        "id": "call_native|fc_original",
        "name": "bash",
        "arguments": {"command": "python -c 'print(1)'", "timeout": 120},
    }
    aid = assistant(history, [call])
    rid = tool_result(history, call["id"], content, name="bash", error=error)
    return history, aid, rid, call


@pytest.fixture
def repair_env(tmp_path, monkeypatch):
    external = []
    alive = {"value": False}
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: list(external))
    app = create_app(tmp_path / "swarm", recover=False, allowed_hosts=["testserver"])
    monkeypatch.setattr(app.state.controller.processes, "alive", lambda agent: alive["value"])
    return SimpleNamespace(
        app=app,
        controller=app.state.controller,
        store=app.state.controller.store,
        repair=ToolOutputRepair(app.state.meta_compactor),
        external=external,
        alive=alive,
        workspace=tmp_path / "workspace",
    )


def register(env, data, *, history=None, name="Agent", **fields):
    env.workspace.mkdir(parents=True, exist_ok=True)
    path = env.store.sessions_dir / f"{uuid4().hex}.jsonl"
    path.write_bytes(data)
    header = json.loads(data.splitlines()[0].decode("utf-8-sig"))
    values = {
        "name": name,
        "goal": "Repair oversized tool output",
        "folder": str(env.workspace),
        "managed": True,
        "session_id": header["id"],
        "session_file": str(path),
        "auto_resume": True,
        "pid": None,
        "notes": "Keep this record and session identity.",
    }
    values.update(fields)
    return env.store.create(Agent(**values)), path


def parse(data):
    return [json.loads(line) for line in data.decode("utf-8-sig").splitlines() if line.strip()]


def test_native_actual_large_output_is_append_only_and_exactly_archived(repair_env):
    # This is the size of the field observed in the real failing session, not a scaled fixture.
    text = "x" * 23_318_068 + "é😀Z"
    assert len(text) == 23_318_071
    image = {"type": "image", "data": "YWJj", "mimeType": "image/png"}
    history, aid, rid, call = native_history(
        repair_env.workspace, [{"type": "text", "text": text}, image], error=True
    )
    original_records = copy.deepcopy(history.entries)
    original = b"\xef\xbb\xbf" + history.encode().replace("\n", "\r\n").encode()
    agent, path = register(repair_env, original, history=history)

    report = repair_env.repair.repair(str(agent.id))

    assert report["status"] == "completed" and report["outputs_repaired"] == 1
    assert report["original_bytes"] == output_size(text)
    assert report["preview_bytes"] < MAX_OUTPUT_BYTES
    assert not repair_env.store.get(agent.id).auto_resume
    replacement = path.read_bytes()
    assert replacement.startswith(original)  # Includes the BOM, CRLF, spelling, and full tree.
    assert parse(replacement)[: len(original_records)] == original_records
    assert Path(report["archive"]).name == "original.jsonl"
    assert Path(report["archive"]).read_bytes() == original
    assert digest(original) == report["source_sha256"]
    assert digest(replacement) == report["replacement_sha256"]

    appended = parse(replacement)[len(original_records) :]
    edit = next(e for e in appended if e["type"] == "context_edit")
    guide = next(e for e in appended if e.get("customType") == "swarm.tool_output_repair")
    assert edit["targetId"] == rid
    preview_content = edit["replacement"]["content"]
    assert preview_content[1:] == [image]
    preview = preview_content[0]["text"]
    output_id = report["output_ids"][0]
    assert output_id in preview and guide["data"]["outputs"] == [output_id]
    assert guide["type"] == "custom"  # Not a user-like message after any pending call.
    assert output_size(preview) <= 32 * 1024
    assert (
        "FIRST EXCERPT (at most 24576 bytes)" in preview
        and "LAST EXCERPT (at most 8192 bytes)" in preview
    )
    assert text[: HEAD_BYTES // 2] in preview and text[-(TAIL_BYTES // 2) :] in preview
    assert "display" not in guide and guide["customType"] == "swarm.tool_output_repair"

    index = json.loads((Path(report["directory"]) / "index.json").read_text())
    item = index["outputs"][output_id]
    assert item["entry_id"] == rid and item["tool_call_id"] == call["id"]
    assert item["tool_name"] == "bash" and item["original_chars"] == 23_318_071
    payload = (Path(report["directory"]) / f"{output_id}.json").read_bytes()
    assert item["sha256"] == digest(payload) and json.loads(payload) == text
    # The original assistant input, caller ID, image and error bit were never rewritten.
    entries = {e.get("id"): e for e in parse(replacement)}
    assert entries[aid] == original_records[-2]
    assert entries[rid]["message"]["toolCallId"] == call["id"]
    assert entries[rid]["message"]["isError"] is True
    assert entries[rid]["message"]["content"][-1] == image


def test_aggregate_text_blocks_use_utf8_budget_and_preserve_empty_unicode_image(tmp_path):
    image = {"type": "image", "data": "AAEC", "mimeType": "image/webp"}
    blocks = [{"type": "text", "text": "é" * 512} for _ in range(1024)]
    blocks.insert(17, {"type": "text", "text": ""})
    blocks.append({"type": "text", "text": "終😀"})
    blocks.append(image)
    assert all(output_size(block["text"]) <= 1024 for block in blocks if block["type"] == "text")
    history, _, rid, _ = native_history(tmp_path, blocks)
    before = history.encode().encode()

    replacement, outputs, stats = prepare_repair(before, tmp_path, uuid4().hex)

    expected = "\n".join(block["text"] for block in blocks if block["type"] == "text")
    assert output_size(expected) > MAX_OUTPUT_BYTES
    assert before == history.encode().encode()  # Caller-owned input bytes/data were not mutated.
    assert stats["outputs_repaired"] == 1 and next(iter(outputs.values()))["text"] == expected
    records = parse(replacement)
    edit = next(e for e in records if e.get("type") == "context_edit" and e["targetId"] == rid)
    assert edit["replacement"]["content"][1:] == [image]
    assert "終😀" in expected and "\n\n" in expected


def test_threshold_is_utf8_bytes_not_character_count(tmp_path):
    exact = "é" * (MAX_OUTPUT_BYTES // 2)
    history, _, _, _ = native_history(tmp_path, [{"type": "text", "text": exact}])
    replacement, outputs, stats = prepare_repair(history.encode().encode(), tmp_path, uuid4().hex)
    assert output_size(exact) == MAX_OUTPUT_BYTES
    assert replacement is None and outputs == {} and stats["outputs_repaired"] == 0

    history, _, _, _ = native_history(tmp_path, [{"type": "text", "text": exact + "x"}])
    replacement, outputs, stats = prepare_repair(history.encode().encode(), tmp_path, uuid4().hex)
    assert replacement is not None and len(outputs) == stats["outputs_repaired"] == 1


def test_projection_honors_edits_omissions_branches_and_compaction_without_flattening(tmp_path):
    large = "original:" + "x" * MAX_OUTPUT_BYTES
    corrected = "corrected:" + "y" * MAX_OUTPUT_BYTES
    history, aid, rid, _ = native_history(tmp_path, [{"type": "text", "text": large}])
    history.add("context_edit", targetId=rid, replacement={"content": corrected})
    first_edit = history.parent
    history.add("context_edit", targetId=rid, replacement={"content": corrected + "!"})
    active = history.parent
    omitted = tool_result(history, "omitted-call", [{"type": "text", "text": large}])
    history.add("context_edit", targetId=omitted, replacement=None)
    active = history.parent
    abandoned = tool_result(history, "abandoned-call", [{"type": "text", "text": large}])
    history.parent = active
    history.add(
        "compaction",
        summary="Existing compacted checkpoint must remain unchanged.",
        firstKeptEntryId=aid,
        tokensBefore=50000,
    )
    compaction = history.parent
    history.user("Current branch after the existing compaction.")
    before = history.encode().encode()
    before_records = parse(before)

    replacement, outputs, stats = prepare_repair(before, tmp_path, uuid4().hex)

    assert stats["outputs_repaired"] == 1
    archived = next(iter(outputs.values()))
    assert archived["entry_id"] == rid and archived["text"] == corrected + "!"
    assert all(value["entry_id"] not in {omitted, abandoned} for value in outputs.values())
    assert replacement.startswith(before)
    records = parse(replacement)
    assert records[: len(before_records)] == before_records
    assert next(e for e in records if e["id"] == first_edit)["type"] == "context_edit"
    assert next(e for e in records if e["id"] == compaction)["summary"].startswith("Existing")
    new_edits = records[len(before_records) :]
    assert sum(e.get("type") == "context_edit" for e in new_edits) == 1
    assert not any(e.get("type") == "compaction" for e in new_edits)


@pytest.mark.parametrize("title", ["Pi toolResult", "Grok tool result"])
def test_known_imported_historical_outputs_are_repaired(tmp_path, title):
    history = PiHistory(uuid4().hex, str(tmp_path), "Imported history")
    payload = f"{title}:" + "ø" * (MAX_OUTPUT_BYTES // 2 + 1)
    history.historical(title, payload)
    target = history.parent

    replacement, outputs, stats = prepare_repair(history.encode().encode(), tmp_path, uuid4().hex)

    assert replacement.startswith(history.encode().encode())
    assert stats["outputs_repaired"] == 1
    record = next(iter(outputs.values()))
    assert record["entry_id"] == target
    assert record["text"].startswith(f"[Historical {title}; imported context")
    edit = next(e for e in parse(replacement) if e.get("targetId") == target)
    assert isinstance(edit["replacement"]["content"], str)
    assert "Output reference: toolout_" in edit["replacement"]["content"]


def test_input_elided_archived_output_is_known_and_keeps_metadata(tmp_path):
    history = PiHistory(uuid4().hex, str(tmp_path), "Input-elided history")
    image = {"type": "image", "data": "YWJj", "mimeType": "image/png"}
    content = [
        {"type": "text", "text": "[Historical output follows unchanged]"},
        {"type": "text", "text": "λ" * (MAX_OUTPUT_BYTES // 2 + 10)},
        {"type": "text", "text": ""},
        image,
    ]
    details = {
        "original_message_metadata": {
            "role": "toolResult",
            "toolCallId": "call_elided|fc_original",
            "toolName": "write",
            "isError": True,
        }
    }
    history.add(
        "custom_message",
        customType="swarm.archived_tool_output",
        display=True,
        content=copy.deepcopy(content),
        details=copy.deepcopy(details),
    )
    target = history.parent
    before_records = copy.deepcopy(history.entries)

    replacement, outputs, stats = prepare_repair(history.encode().encode(), tmp_path, uuid4().hex)

    assert stats["outputs_repaired"] == 1 and next(iter(outputs.values()))["entry_id"] == target
    records = parse(replacement)
    assert records[: len(before_records)] == before_records
    original = next(e for e in records if e["id"] == target)
    assert original["details"] == details and original["content"] == content
    edit = next(e for e in records if e.get("targetId") == target)
    assert edit["replacement"]["content"][1:] == [image]


def test_first_noop_still_disables_auto_resume_without_creating_archive(repair_env):
    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "already bounded"}]
    )
    agent, path = register(repair_env, history.encode().encode())
    before = path.read_bytes()

    report = repair_env.repair.repair(str(agent.id))

    assert report["status"] == "unchanged" and report["outputs_repaired"] == 0
    assert "archive" not in report and "directory" not in report
    assert path.read_bytes() == before and not repair_env.store.get(agent.id).auto_resume


def test_noop_repeat_keeps_archive_and_lookup_pages_by_character(repair_env):
    text = ("ab😀é" * (MAX_OUTPUT_BYTES // 4 + 100)) + "THE END"
    history, _, rid, call = native_history(repair_env.workspace, [{"type": "text", "text": text}])
    agent, path = register(repair_env, history.encode().encode())

    first = repair_env.repair.repair(str(agent.id))
    repaired = path.read_bytes()
    output_id = first["output_ids"][0]
    second = repair_env.repair.repair(str(agent.id))

    assert second["status"] == "unchanged" and second["archive_from_previous_repair"]
    for key in ("id", "directory", "archive", "source_sha256", "replacement_sha256"):
        assert second[key] == first[key]
    assert path.read_bytes() == repaired and not repair_env.store.get(agent.id).auto_resume

    parts, offset = [], 0
    while True:
        page = retrieve_output(repair_env.store, output_id, offset, 64000)
        assert page["output_id"] == output_id and page["entry_id"] == rid
        assert page["tool_call_id"] == call["id"] and page["offset"] == offset
        parts.append(page["output"])
        offset = page["next_offset"]
        if page["eof"]:
            break
    assert "".join(parts) == text and offset == len(text)
    assert retrieve_output(repair_env.store, output_id, len(text), 1)["output"] == ""


def test_lookup_rejects_bounds_tampering_and_path_escape(repair_env, tmp_path):
    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    agent, _ = register(repair_env, history.encode().encode())
    report = repair_env.repair.repair(str(agent.id))
    output_id = report["output_ids"][0]

    for value, offset, limit in (
        ("../secret", 0, 10),
        (f"{output_id}/../secret", 0, 10),
        (output_id, -1, 10),
        (output_id, 0, 0),
        (output_id, 0, 64001),
        (output_id, MAX_OUTPUT_BYTES * 10, 10),
    ):
        with pytest.raises((ValueError, OSError)):
            retrieve_output(repair_env.store, value, offset, limit)

    payload = Path(report["directory"]) / f"{output_id}.json"
    payload.write_bytes(payload.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum"):
        retrieve_output(repair_env.store, output_id)

    other = Store(tmp_path / "other-home")
    outside = tmp_path / "outside"
    outside.mkdir()
    (other.home / "tool-output-repairs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|contained"):
        retrieve_output(other, output_id)


def test_repair_requires_session_cwd(repair_env):
    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    del history.entries[0]["cwd"]
    agent, path = register(repair_env, history.encode().encode())
    before = path.read_bytes()

    with pytest.raises(ValueError, match="cwd|working directory"):
        repair_env.repair.repair(str(agent.id))
    assert path.read_bytes() == before


@pytest.mark.parametrize("case", ["running", "external", "unmanaged", "restart", "shared"])
def test_guard_rejects_unsafe_agents_without_touching_source(repair_env, case):
    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    fields = {}
    if case == "unmanaged":
        fields["managed"] = False
    if case == "restart":
        fields["restart_job_id"] = uuid4()
    agent, path = register(repair_env, history.encode().encode(), **fields)
    if case == "running":
        repair_env.alive["value"] = True
    if case == "external":
        repair_env.external.append(str(repair_env.workspace.resolve()))
    if case == "shared":
        repair_env.store.create(
            Agent(
                name="Shared",
                goal="Do not replace shared source",
                folder=str(repair_env.workspace),
                managed=True,
                session_id=agent.session_id,
                session_file=str(path),
                auto_resume=False,
            )
        )
    before = path.read_bytes()

    with pytest.raises((RuntimeError, ValueError), match="Stop|managed Pi|restart|shared"):
        repair_env.repair.repair(str(agent.id))
    assert path.read_bytes() == before


@pytest.mark.parametrize("race", ["source", "external", "restart"])
def test_races_after_archive_fail_closed_and_persist_report(repair_env, monkeypatch, race):
    import swarm.tool_outputs as module

    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    agent, path = register(repair_env, history.encode().encode())
    original = path.read_bytes()
    real_write = module.atomic_write

    def raced_write(target, content):
        real_write(target, content)
        if target.name != "replacement.jsonl":
            return
        if race == "source":
            path.write_bytes(original + b"\n")
        elif race == "external":
            repair_env.external.append(str(repair_env.workspace.resolve()))
        else:
            repair_env.alive["value"] = True

    monkeypatch.setattr(module, "atomic_write", raced_write)
    with pytest.raises(RuntimeError, match="changed|process|restarted"):
        repair_env.repair.repair(str(agent.id))

    current = repair_env.store.get(agent.id)
    report = current.tool_output_repair
    assert report["status"] == "failed" and not current.auto_resume
    assert Path(report["archive"]).read_bytes() == original
    assert path.read_bytes() == original + (b"\n" if race == "source" else b"")


def test_disk_full_at_atomic_replace_aborts_but_keeps_archive_and_report(repair_env, monkeypatch):
    import swarm.tool_outputs as module

    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    agent, path = register(repair_env, history.encode().encode(), last_error="output 10485760")
    original = path.read_bytes()
    real_write = module.atomic_write

    def fail_replace(target, content):
        if target == path:
            raise OSError("disk full")
        real_write(target, content)

    monkeypatch.setattr(module, "atomic_write", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        repair_env.repair.repair(str(agent.id))

    current = repair_env.store.get(agent.id)
    report = current.tool_output_repair
    assert report["status"] == "failed" and "disk full" in report["error"]
    assert path.read_bytes() == original and Path(report["archive"]).read_bytes() == original
    assert current.last_error == "output 10485760" and not current.auto_resume


@pytest.mark.parametrize("committed", [False, True])
def test_recover_reconciles_running_report_by_replacement_hash(repair_env, committed):
    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "small output"}]
    )
    agent, path = register(repair_env, history.encode().encode())
    directory = repair_env.store.home / "tool-output-repairs" / uuid4().hex
    directory.mkdir(parents=True)
    report = {
        "id": directory.name,
        "status": "running",
        "source": str(path),
        "directory": str(directory),
        "replacement_sha256": digest(path.read_bytes()) if committed else "0" * 64,
    }
    repair_env.repair.save(agent.id, report)
    repair_env.store.update(agent.id, {"auto_resume": True})

    repair_env.repair.recover()

    current = repair_env.store.get(agent.id)
    assert current.tool_output_repair["status"] == ("completed" if committed else "failed")
    assert not current.auto_resume and path.read_bytes() == history.encode().encode()
    persisted = json.loads((directory / "job.json").read_text())
    assert persisted["status"] == current.tool_output_repair["status"]


@pytest.mark.parametrize("pending", ["single", "partial", "orphan"])
def test_repair_adds_no_user_like_context_after_unfinished_tool_exchange(repair_env, pending):
    history, aid, rid, call = native_history(
        repair_env.workspace, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    if pending == "single":
        assistant(
            history,
            [
                {
                    "type": "toolCall",
                    "id": "pending",
                    "name": "bash",
                    "arguments": {"command": "do-not-replay"},
                }
            ],
        )
    elif pending == "partial":
        assistant(
            history,
            [
                {"type": "toolCall", "id": "partial1", "name": "bash", "arguments": {}},
                {"type": "toolCall", "id": "partial2", "name": "bash", "arguments": {}},
            ],
        )
        tool_result(history, "partial1", [{"type": "text", "text": "completed sibling"}])
    else:
        tool_result(history, "orphan", [{"type": "text", "text": "unrelated existing result"}])
    data = history.encode().encode()
    replacement, outputs, stats = prepare_repair(data, repair_env.store.home, uuid4().hex)
    assert replacement.startswith(data) and stats["outputs_repaired"] == 1
    appended = parse(replacement)[len(history.entries) :]
    assert all(entry["type"] in {"context_edit", "custom"} for entry in appended)
    assert [entry["message"] for entry in parse(replacement) if entry["type"] == "message"] == [
        entry["message"] for entry in history.entries if entry["type"] == "message"
    ]


@pytest.mark.parametrize("change", ["shared", "path", "backend", "restart", "lease"])
def test_commit_revalidates_current_agent_and_registry_ownership(repair_env, monkeypatch, change):
    import swarm.tool_outputs as module

    history, _, _, _ = native_history(
        repair_env.workspace, [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]
    )
    original = history.encode().encode()
    agent, path = register(repair_env, original)
    write = module.atomic_write

    def mutate(target, content):
        write(target, content)
        if target.name == "replacement.jsonl":
            if change == "shared":
                repair_env.store.create(
                    Agent(
                        name="Concurrent owner",
                        goal="Do not share",
                        folder=agent.folder,
                        managed=True,
                        session_file=str(path),
                        session_id=agent.session_id,
                    )
                )
            elif change == "path":
                repair_env.store.update(
                    agent.id, {"session_file": str(path.with_name("other.jsonl"))}
                )
            elif change == "backend":
                repair_env.store.update(agent.id, {"backend": "other"})
            elif change == "restart":
                repair_env.store.update(agent.id, {"restart_job_id": str(uuid4())})
            else:
                repair_env.store.update(
                    agent.id, {"tool_output_repair": {"id": "other-owner", "status": "running"}}
                )

    monkeypatch.setattr(module, "atomic_write", mutate)
    with pytest.raises(RuntimeError):
        repair_env.repair.repair(str(agent.id))
    assert path.read_bytes() == original
    if change == "lease":
        assert repair_env.store.get(agent.id).tool_output_repair["id"] == "other-owner"
