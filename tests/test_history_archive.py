import json
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from swarm.history_archive import (
    MAX_PAGE_BYTES,
    ArchiveConflictError,
    HistoryArchiver,
    HistoryArchiveRequest,
    prepare,
    retrieve_history,
)
from swarm.importers.pi_history import PiHistory
from swarm.models import Agent
from swarm.store import Store


def add_message(history, role, content=None, **fields):
    message = {"role": role, "timestamp": history.milliseconds, **fields}
    if content is not None:
        message["content"] = content
    history.add("message", message=message)
    return history.parent


def rich_history(tmp_path, turns=8):
    history = PiHistory(str(uuid4()), str(tmp_path), "History archive test")
    add_message(
        history,
        "system",
        "base prompt",
        sections={"preamble": "first", "remove-me": "old"},
        toolsAdded=[{"name": "read", "description": "read", "parameters": {"type": "object"}}],
    )
    for index in range(turns):
        history.user(f"USER-{index} " + (chr(65 + index % 20) * 1600))
        add_message(
            history,
            "assistant",
            [
                {"type": "thinking", "thinking": f"private-{index}"},
                {"type": "text", "text": f"ANSWER-{index} " + "z" * 900},
            ],
            provider="test",
            model="large",
            api="test",
            stopReason="stop",
            usage={"totalTokens": 999999, "cost": {"total": 0}},
        )
    return history


def decoded_lines(data):
    return [json.loads(line) for line in data.decode("utf-8-sig").splitlines() if line.strip()]


@pytest.mark.parametrize("percent", [1, 50, 95])
def test_prepare_is_append_only_and_archives_safe_oldest_context(tmp_path, percent):
    history = rich_history(tmp_path, turns=20)
    original = history.encode().encode()
    archive_id = uuid4().hex
    directory = tmp_path / "context-archives" / "session" / archive_id
    replacement, payloads, stats = prepare(original, directory, percent, archive_id)

    assert replacement.startswith(original)
    assert payloads["original.jsonl"] == original
    assert payloads["replacement.jsonl"] == replacement
    assert set(payloads) == {
        "original.jsonl",
        "history.jsonl",
        "history.txt",
        "replacement.jsonl",
        "manifest.json",
    }
    assert stats["percent_requested"] == percent
    assert 0 < stats["archive_bytes"] < stats["context_before_bytes"]
    assert stats["kept_entries"] >= 2  # newest user span and its answer
    assert stats["actual_percent"] == pytest.approx(
        stats["archive_bytes"] * 100 / stats["context_before_bytes"]
    )
    appended = decoded_lines(replacement[len(original) :])
    compact = appended[0]
    assert compact["type"] == "compaction" and compact["fromHook"] is True
    assert compact["firstKeptEntryId"] == stats["first_kept_entry_id"]
    assert "no LLM summary" in compact["summary"] and archive_id in compact["summary"]
    assert "not an exact token count" in compact["details"]["measurement"]
    manifest = json.loads(payloads["manifest.json"])
    assert manifest["version"] == 1 and manifest["length"] == len(original)
    assert manifest["sha256"] and manifest["source"] is None
    assert manifest["files"]["original.jsonl"]["sha256"]


def test_projection_uses_latest_compaction_branch_and_carries_edits(tmp_path):
    history = rich_history(tmp_path, turns=3)
    abandoned_parent = history.parent
    history.user("ABANDONED " + "x" * 3000)
    history.parent = abandoned_parent
    kept = history.parent
    history.add(
        "compaction",
        summary="PRIOR SUMMARY " + "s" * 5000,
        firstKeptEntryId=kept,
        tokensBefore=4000,
        systemMessage={
            "role": "system",
            "content": "checkpoint prompt",
            "sections": {"preamble": "checkpoint", "remove-me": "gone"},
            "toolsAdded": [
                {"name": "read", "description": "old", "parameters": {"type": "object"}},
                {"name": "write", "description": "write", "parameters": {"type": "object"}},
            ],
            "timestamp": 1,
        },
    )
    add_message(
        history,
        "system",
        "later prompt",
        sections={"remove-me": None, "skills": "new"},
        toolsRemoved=[{"name": "write"}],
        toolsAdded=[{"name": "read", "description": "new", "parameters": {"type": "object"}}],
    )
    history.user("KEEP ORIGINAL " + "k" * 2500)
    target = history.parent
    history.add("context_edit", targetId=target, replacement={"content": "CORRECTED " + "c" * 2500})
    history.user("NEWEST " + "n" * 2500)
    original = history.encode().encode()

    replacement, payloads, stats = prepare(original, tmp_path / uuid4().hex, 50)
    archived_text = payloads["history.txt"].decode()
    assert "ABANDONED" not in archived_text
    assert "PRIOR SUMMARY" in archived_text
    appended = decoded_lines(replacement[len(original) :])
    compact = appended[0]
    system = compact["systemMessage"]
    assert system["content"] == "checkpoint prompt\n\nlater prompt"
    assert system["sections"] == {"preamble": "checkpoint", "skills": "new"}
    assert [tool["name"] for tool in system["toolsAdded"]] == ["read"]
    assert system["toolsAdded"][0]["description"] == "new"
    assert system["timestamp"] > 1
    carries = [entry for entry in appended if entry["type"] == "context_edit"]
    assert any(
        entry["targetId"] == target and "CORRECTED" in entry["replacement"]["content"]
        for entry in carries
    )
    assert stats["first_kept_entry_id"]


def test_compaction_without_system_checkpoint_does_not_resurrect_old_system(tmp_path):
    history = rich_history(tmp_path, turns=4)
    history.add(
        "compaction",
        summary="prior summary " + "s" * 5000,
        firstKeptEntryId=history.entries[2]["id"],
        tokensBefore=1000,
    )
    add_message(history, "system", "ONLY POST-COMPACTION SYSTEM")
    history.user("new user " + "n" * 5000)
    replacement, _, _ = prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)
    compact = decoded_lines(replacement[len(history.encode().encode()) :])[0]
    assert compact["systemMessage"]["content"] == "ONLY POST-COMPACTION SYSTEM"
    assert "base prompt" not in compact["systemMessage"]["content"]


def test_notice_has_actual_path_and_exact_bounded_retrieval_commands(tmp_path):
    history = rich_history(tmp_path, turns=8)
    archive_id = uuid4().hex
    directory = tmp_path / "context-archives" / "session" / archive_id
    replacement, _, _ = prepare(history.encode().encode(), directory, 50, archive_id)
    notice = decoded_lines(replacement[len(history.encode().encode()) :])[0]["summary"]
    assert str(directory / "history.txt") in notice
    assert f"swarm_read_history archive_id={archive_id} format=text offset=0 limit=8000" in notice
    assert (
        f'"$SWARM_PYTHON" -m swarm history-archive "$SWARM_AGENT_ID" {archive_id} '
        "--offset 0 --limit 8000"
    ) in notice
    assert len(notice.encode()) <= 4096


def test_history_jsonl_materializes_edits_preserves_metadata_and_full_blocks(tmp_path):
    history = PiHistory(str(uuid4()), str(tmp_path), "Projected")
    history.user("old " + "a" * 5000)
    edited = history.parent
    history.add("context_edit", targetId=edited, replacement={"content": "edited " + "b" * 5000})
    add_message(
        history,
        "assistant",
        [
            {"type": "thinking", "thinking": "full thought", "thinkingSignature": "opaque"},
            {"type": "image", "mimeType": "image/png", "data": "AA=="},
            {"type": "text", "text": "answer"},
        ],
        provider="p",
        model="m",
        api="a",
        stopReason="stop",
        usage={"totalTokens": 5},
        diagnostics={"keep": True},
    )
    history.user("newest " + "c" * 5000)
    replacement, payloads, _ = prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)
    entries = decoded_lines(payloads["history.jsonl"])
    projected = next(entry for entry in entries if entry.get("id") == edited)
    assert projected["message"]["content"].startswith("edited")
    assert "old " not in projected["message"]["content"]
    text = payloads["history.txt"].decode()
    if any(entry.get("message", {}).get("role") == "assistant" for entry in entries):
        assert "full thought" in text and "Image preserved in history.jsonl" in text
    assert replacement.startswith(history.encode().encode())


def tool_history(tmp_path, malformed=None):
    history = PiHistory(str(uuid4()), str(tmp_path), "Tools")
    history.user("old request " + "x" * 5000)
    call_id = "call-1"
    calls = [{"type": "toolCall", "id": call_id, "name": "read", "arguments": {"path": "a"}}]
    if malformed == "duplicate-call":
        calls.append(dict(calls[0]))
    add_message(
        history,
        "assistant",
        calls,
        provider="p",
        model="m",
        api="a",
        stopReason="toolUse",
        usage={},
    )
    if malformed != "pending":
        result_id = "other" if malformed == "orphan" else call_id
        add_message(
            history,
            "toolResult",
            [{"type": "text", "text": "FULL OUTPUT " + "o" * 3000}],
            toolCallId=result_id,
            toolName="read",
            isError=False,
            details={"nested": "not measured"},
        )
    history.user("new request " + "n" * 5000)
    add_message(
        history,
        "assistant",
        [{"type": "text", "text": "done"}],
        provider="p",
        model="m",
        api="a",
        stopReason="stop",
        usage={},
    )
    return history


def test_completed_tool_exchange_is_not_split_and_output_is_full(tmp_path):
    history = tool_history(tmp_path)
    _, payloads, _ = prepare(history.encode().encode(), tmp_path / uuid4().hex, 55)
    entries = decoded_lines(payloads["history.jsonl"])
    roles = [entry.get("message", {}).get("role") for entry in entries]
    assert ("assistant" in roles) == ("toolResult" in roles)
    if "toolResult" in roles:
        assert "FULL OUTPUT" in payloads["history.txt"].decode()


@pytest.mark.parametrize("stop_reason", ["stop", "toolUse", "length", "error", "aborted"])
def test_completed_terminal_tool_batch_is_archived_without_rewriting_metadata(
    tmp_path, stop_reason
):
    history = tool_history(tmp_path)
    history.entries[3]["message"]["stopReason"] = stop_reason
    original = history.encode().encode()
    replacement, payloads, stats = prepare(original, tmp_path / uuid4().hex, 55)
    assert replacement.startswith(original)
    archived = decoded_lines(payloads["history.jsonl"])
    assistant = next(
        entry for entry in archived if entry.get("message", {}).get("role") == "assistant"
    )
    assert assistant["message"]["stopReason"] == stop_reason
    assert assistant["message"]["content"][0]["id"] == "call-1"
    result = next(
        entry for entry in archived if entry.get("message", {}).get("role") == "toolResult"
    )
    assert result["message"]["toolCallId"] == "call-1"
    assert "FULL OUTPUT" in payloads["history.txt"].decode()
    assert stats["first_kept_entry_id"] == history.entries[5]["id"]


@pytest.mark.parametrize("malformed", ["pending", "orphan", "duplicate-call"])
@pytest.mark.parametrize("stop_reason", ["stop", "toolUse", "length", "error", "aborted"])
def test_terminal_tool_batch_still_requires_complete_unambiguous_results(
    tmp_path, malformed, stop_reason
):
    history = tool_history(tmp_path, malformed)
    history.entries[3]["message"]["stopReason"] = stop_reason
    with pytest.raises(ValueError, match="missing|incomplete|orphan|duplicate"):
        prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)


@pytest.mark.parametrize("stop_reason", ["pending", "deferred", "unknown", None])
def test_unfinished_tool_batch_is_refused_even_with_results(tmp_path, stop_reason):
    history = tool_history(tmp_path)
    message = history.entries[3]["message"]
    if stop_reason is None:
        message.pop("stopReason")
    else:
        message["stopReason"] = stop_reason
    with pytest.raises(ValueError, match="stopReason"):
        prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)


def test_bash_execution_is_measured_and_is_a_safe_boundary(tmp_path):
    history = rich_history(tmp_path, turns=3)
    add_message(
        history,
        "bashExecution",
        command="pytest -q",
        output="BASH OUTPUT " + "b" * 6000,
        exitCode=0,
        cancelled=False,
        truncated=False,
    )
    history.user("newest " + "n" * 5000)
    _, payloads, stats = prepare(history.encode().encode(), tmp_path / uuid4().hex, 70)
    assert stats["context_before_bytes"] > 6000
    assert payloads["history.jsonl"]


@pytest.mark.parametrize(
    "malformed,match",
    [
        ("pending", "missing|incomplete"),
        ("orphan", "orphan"),
        ("duplicate-call", "duplicate"),
    ],
)
def test_ambiguous_or_incomplete_tool_sequences_refuse(tmp_path, malformed, match):
    history = tool_history(tmp_path, malformed)
    with pytest.raises(ValueError, match=match):
        prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)


@pytest.mark.parametrize("stop_reason", ["stop", "length", "error", "aborted"])
def test_assistant_terminal_stop_reason_matrix_accepts_no_calls(tmp_path, stop_reason):
    history = rich_history(tmp_path, turns=5)
    history.entries[4]["message"]["stopReason"] = stop_reason
    replacement, _, _ = prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)
    assert replacement.startswith(history.encode().encode())


def test_assistant_tool_use_stop_reason_requires_completed_nonempty_calls(tmp_path):
    history = tool_history(tmp_path)
    replacement, _, _ = prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)
    assert replacement.startswith(history.encode().encode())

    empty = rich_history(tmp_path, turns=5)
    empty.entries[4]["message"]["stopReason"] = "toolUse"
    with pytest.raises(ValueError, match="nonempty"):
        prepare(empty.encode().encode(), tmp_path / uuid4().hex, 50)


@pytest.mark.parametrize("stop_reason", [None, "pending", "deferred", "unknown"])
def test_assistant_missing_or_unfinished_stop_reasons_refuse(tmp_path, stop_reason):
    history = rich_history(tmp_path, turns=5)
    message = history.entries[4]["message"]
    if stop_reason is None:
        message.pop("stopReason")
    else:
        message["stopReason"] = stop_reason
    with pytest.raises(ValueError, match="stopReason"):
        prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)


def test_unknown_role_and_no_safe_current_turn_refuse(tmp_path):
    history = rich_history(tmp_path, turns=2)
    history.entries[-1]["message"]["role"] = "augmentedUnknown"
    with pytest.raises(ValueError, match="unknown"):
        prepare(history.encode().encode(), tmp_path / uuid4().hex, 50)

    one = PiHistory(str(uuid4()), str(tmp_path), "One")
    one.user("only current user turn " + "x" * 10000)
    add_message(
        one,
        "assistant",
        [{"type": "text", "text": "answer"}],
        provider="p",
        model="m",
        api="a",
        stopReason="stop",
        usage={},
    )
    with pytest.raises(ValueError, match="safe nonempty"):
        prepare(one.encode().encode(), tmp_path / uuid4().hex, 50)


def test_unicode_surrogates_and_image_payload_are_preserved_safely(tmp_path):
    history = rich_history(tmp_path, turns=4)
    history.entries[3]["message"]["content"] += " 😀 \ud800"
    history.entries[4]["message"]["content"].append(
        {"type": "image", "mimeType": "image/png", "data": "YWJj" * 100}
    )
    raw_text = history.encode()
    invalid = raw_text.encode("utf-8", errors="surrogatepass")
    with pytest.raises(ValueError, match="invalid Pi JSONL"):
        prepare(invalid, tmp_path / uuid4().hex, 50)
    # JSON's escaped surrogate spelling is accepted, measured with surrogatepass, and retained.
    escaped = raw_text.replace("\ud800", "\\ud800").encode("utf-8")
    replacement, payloads, _ = prepare(escaped, tmp_path / uuid4().hex, 50)
    assert replacement.startswith(escaped)
    assert b"\\ud800" in payloads["history.jsonl"] or b"\\ud800" in payloads["original.jsonl"]
    assert b"Image preserved" in payloads["history.txt"] or b"YWJj" in payloads["history.jsonl"]


class FakeProcesses:
    def __init__(self):
        self.live = False

    def alive(self, agent):
        return self.live


class FakeGuard:
    def __init__(self, store):
        self.controller = SimpleNamespace(
            store=store, processes=FakeProcesses(), lock=threading.RLock()
        )
        self.store = store
        self.external = False

    def check_external(self, agent):
        if self.external:
            raise RuntimeError("external Pi is running")

    def eligible(self, agent, *, read_snapshot=True):
        if not agent.managed or agent.backend != "pi" or not agent.session_file:
            raise ValueError("requires managed Pi")
        if self.controller.processes.alive(agent):
            raise RuntimeError("stop the agent")
        self.check_external(agent)
        return Path(agent.session_file).expanduser().absolute()


@pytest.fixture
def archiver_setup(tmp_path):
    store = Store(tmp_path / "swarm")
    history = rich_history(tmp_path, turns=10)
    session = store.sessions_dir / "active.jsonl"
    session.write_text(history.encode(), encoding="utf-8")
    agent = store.create(
        Agent(
            name="Native parent",
            goal="archive safely",
            folder=str(tmp_path),
            managed=True,
            auto_resume=True,
            session_id=history.entries[0]["id"],
            session_file=str(session),
        )
    )
    guard = FakeGuard(store)
    return HistoryArchiver(guard), store, agent, session


def test_archive_lifecycle_idempotence_manifest_and_safe_retrieval(archiver_setup):
    archiver, store, agent, session = archiver_setup
    original = session.read_bytes()
    request = HistoryArchiveRequest(percent=50)
    report = archiver.archive(str(agent.id), request)
    assert report["status"] == "completed"
    assert session.read_bytes().startswith(original)
    assert Path(report["archive"]).read_bytes() == original
    assert Path(report["history_file"]).is_file()
    assert not store.get(agent.id).auto_resume

    repeated = archiver.archive(str(agent.id), request)
    assert repeated["archive_id"] == report["archive_id"]
    assert list(session.resolve().parent.glob("context-archives/*/*/manifest.json")) == [
        Path(report["manifest"])
    ]

    first = retrieve_history(store, agent.id, report["archive_id"], 0, 97)
    assert first["offset"] == 0 and 0 < first["next_offset"] <= 100
    second = retrieve_history(
        store, agent.id, report["archive_id"], first["next_offset"], 97, "text"
    )
    assert second["offset"] == first["next_offset"]
    structured = retrieve_history(store, agent.id, report["archive_id"], 0, 200, "jsonl")
    assert structured["output"].startswith("{")

    with pytest.raises(ArchiveConflictError):
        archiver.archive(
            str(agent.id), HistoryArchiveRequest(percent=51, request_id=request.request_id)
        )
    other = store.create(
        Agent(
            name="Other",
            goal="G",
            folder=agent.folder,
            managed=True,
            auto_resume=False,
            session_id="different",
            session_file=str(session.with_name("other.jsonl")),
        )
    )
    Path(other.session_file).write_text(rich_history(Path(agent.folder), 2).encode())
    with pytest.raises(ValueError, match="missing|belong"):
        retrieve_history(store, other.id, report["archive_id"])


def test_retry_receipt_survives_newer_archive_and_binds_source_path(archiver_setup):
    archiver, store, agent, session = archiver_setup
    first_request = HistoryArchiveRequest(percent=35)
    first = archiver.archive(str(agent.id), first_request)
    second = archiver.archive(str(agent.id), HistoryArchiveRequest(percent=35))
    assert second["archive_id"] != first["archive_id"]
    replay = archiver.archive(str(agent.id), first_request)
    assert replay["archive_id"] == first["archive_id"]
    assert replay["status"] == "completed"

    moved = session.with_name("same-session-other-path.jsonl")
    moved.write_bytes(session.read_bytes())
    store.update(agent.id, {"session_file": str(moved)})
    with pytest.raises(ArchiveConflictError, match="different parameters"):
        archiver.archive(str(agent.id), first_request)


def test_completed_receipt_retry_ignores_other_active_maintenance(archiver_setup):
    archiver, store, agent, _ = archiver_setup
    request = HistoryArchiveRequest(percent=40)
    first = archiver.archive(str(agent.id), request)
    maintenance = {"id": uuid4().hex, "status": "running"}
    store.update(agent.id, {"meta_compaction": maintenance})

    replay = archiver.archive(str(agent.id), request)

    assert replay["archive_id"] == first["archive_id"]
    assert store.get(agent.id).meta_compaction == maintenance
    assert store.get(agent.id).context_archive["archive_id"] == first["archive_id"]


def test_retrieval_rejects_bad_offsets_missing_files_and_tampering(archiver_setup):
    archiver, store, agent, _ = archiver_setup
    report = archiver.archive(str(agent.id), HistoryArchiveRequest())
    with pytest.raises(ValueError, match="offset"):
        retrieve_history(store, agent.id, report["archive_id"], -1)
    with pytest.raises(ValueError):
        retrieve_history(store, agent.id, uuid4().hex)
    assert MAX_PAGE_BYTES == 64_000
    with pytest.raises(ValueError, match="64000"):
        retrieve_history(store, agent.id, report["archive_id"], limit=64_001)
    history_path = Path(report["history_file"])
    history_path.write_bytes(history_path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="length|checksum"):
        retrieve_history(store, agent.id, report["archive_id"])


def test_published_archive_permissions_are_private_and_retrieval_never_repairs(
    archiver_setup,
):
    archiver, store, agent, session = archiver_setup
    report = archiver.archive(str(agent.id), HistoryArchiveRequest())
    directory = Path(report["directory"])
    generated_dirs = [directory.parent.parent, directory.parent, directory]
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in generated_dirs)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())

    manifest = directory / "manifest.json"
    manifest.chmod(0o644)
    with pytest.raises(ValueError, match="owner-private"):
        retrieve_history(store, agent.id, report["archive_id"])
    assert manifest.stat().st_mode & 0o777 == 0o644

    manifest.chmod(0o600)
    generated_dirs[0].chmod(0o755)
    with pytest.raises(ValueError, match="owner-private"):
        retrieve_history(store, agent.id, report["archive_id"])
    assert generated_dirs[0].stat().st_mode & 0o777 == 0o755
    assert session.is_file()


def test_archive_refuses_preexisting_broad_generated_root_before_lease(archiver_setup):
    archiver, store, agent, session = archiver_setup
    original = session.read_bytes()
    root = session.parent / "context-archives"
    root.mkdir(mode=0o700)
    root.chmod(0o755)
    with pytest.raises(ValueError, match="owner-private"):
        archiver.archive(str(agent.id), HistoryArchiveRequest())
    assert session.read_bytes() == original
    assert getattr(store.get(agent.id), "context_archive", None) is None
    assert root.stat().st_mode & 0o777 == 0o755


def test_source_race_and_failed_replace_never_overwrite_original(archiver_setup, monkeypatch):
    archiver, store, agent, session = archiver_setup
    original = session.read_bytes()
    real_publish = HistoryArchiver._publish

    def race(directory, payloads):
        real_publish(directory, payloads)
        session.write_bytes(original + b"\n")

    monkeypatch.setattr(HistoryArchiver, "_publish", staticmethod(race))
    with pytest.raises(RuntimeError, match="changed"):
        archiver.archive(str(agent.id), HistoryArchiveRequest())
    assert session.read_bytes() == original + b"\n"
    assert store.get(agent.id).context_archive["status"] == "failed"


def test_disk_failure_at_target_keeps_exact_source_and_archive(archiver_setup, monkeypatch):
    archiver, store, agent, session = archiver_setup
    original = session.read_bytes()
    import swarm.history_archive as module

    def fail(path, replacement, expected_identity, expected_digest):
        raise OSError("disk full")

    monkeypatch.setattr(module, "commit_replacement", fail)
    with pytest.raises(OSError, match="disk full"):
        archiver.archive(str(agent.id), HistoryArchiveRequest())
    report = store.get(agent.id).context_archive
    assert report["status"] == "failed" and session.read_bytes() == original
    assert Path(report["archive"]).read_bytes() == original


def test_descriptor_replacement_race_is_caught_before_final_rename(archiver_setup, monkeypatch):
    archiver, store, agent, session = archiver_setup
    original = session.read_bytes()
    import swarm.history_archive as module

    real_commit = module.commit_replacement

    def race(path, replacement, expected_identity, expected_digest):
        Path(path).write_bytes(original + b" ")
        return real_commit(path, replacement, expected_identity, expected_digest)

    monkeypatch.setattr(module, "commit_replacement", race)
    with pytest.raises(RuntimeError, match="changed"):
        archiver.archive(str(agent.id), HistoryArchiveRequest())
    assert session.read_bytes() == original + b" "
    assert store.get(agent.id).context_archive["status"] == "failed"


@pytest.mark.parametrize("artifact", ["replacement.jsonl", "original.jsonl"])
def test_recovery_rejects_tampered_or_truncated_artifacts(archiver_setup, artifact):
    archiver, store, agent, _ = archiver_setup
    report = archiver.archive(str(agent.id), HistoryArchiveRequest())
    target = Path(report["directory"], artifact)
    target.write_bytes(target.read_bytes()[:-7])
    running = dict(report, status="running", stage="Replacing")
    # Bypass save: a real crash can leave the durable job artifacts and running agent lease
    # disagreeing, and save would intentionally refresh the receipt/job files.
    store.update(agent.id, {"context_archive": running})
    archiver.recover()
    recovered = store.get(agent.id).context_archive
    assert recovered["status"] == "failed"
    assert recovered["stage"] == "Interrupted"


def test_recovery_rejects_truncated_replacement_prefix_false_positive(archiver_setup):
    archiver, store, agent, session = archiver_setup
    report = archiver.archive(str(agent.id), HistoryArchiveRequest())
    replacement_path = Path(report["directory"], "replacement.jsonl")
    truncated = replacement_path.read_bytes()[:-1]
    replacement_path.write_bytes(truncated)
    session.write_bytes(truncated + b"garbage")
    store.update(agent.id, {"context_archive": dict(report, status="running")})
    archiver.recover()
    assert store.get(agent.id).context_archive["status"] == "failed"


def test_symlinked_source_parent_is_rejected_before_lease(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    store = Store(tmp_path / "swarm")
    history = rich_history(real, turns=8)
    source = real / "active.jsonl"
    source.write_text(history.encode())
    agent = store.create(
        Agent(
            name="Symlink parent",
            goal="reject",
            folder=str(real),
            managed=True,
            auto_resume=False,
            session_id=history.entries[0]["id"],
            session_file=str(link / "active.jsonl"),
        )
    )
    archiver = HistoryArchiver(FakeGuard(store))
    with pytest.raises(ValueError, match="canonical directory"):
        archiver.archive(str(agent.id), HistoryArchiveRequest())
    assert getattr(store.get(agent.id), "context_archive", None) is None


def test_committed_append_survives_final_save_failure_and_recovery(archiver_setup, monkeypatch):
    archiver, store, agent, session = archiver_setup
    original_save = archiver.save
    appended = False

    def fail_completed_save(agent_id, report, expected_ids=None):
        nonlocal appended
        if report.get("status") == "completed":
            if not appended:
                current = decoded_lines(session.read_bytes())
                tail = {
                    "type": "custom",
                    "id": uuid4().hex[:8],
                    "parentId": current[-1]["id"],
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "customType": "valid-tail",
                    "data": {},
                }
                session.write_bytes(session.read_bytes() + json.dumps(tail).encode() + b"\n")
                appended = True
            raise OSError("final report save failed")
        return original_save(agent_id, report, expected_ids)

    monkeypatch.setattr(archiver, "save", fail_completed_save)
    report = archiver.archive(str(agent.id), HistoryArchiveRequest())
    assert report["status"] == "completed"
    assert store.get(agent.id).context_archive["status"] == "running"
    committed_with_tail = session.read_bytes()

    monkeypatch.setattr(archiver, "save", original_save)
    archiver.recover()
    assert store.get(agent.id).context_archive["status"] == "completed"
    assert session.read_bytes() == committed_with_tail


def test_completed_commit_does_not_overwrite_a_different_current_lease(archiver_setup, monkeypatch):
    archiver, store, agent, _ = archiver_setup
    original_save = archiver.save
    replacement_lease = {"id": uuid4().hex, "status": "running", "stage": "Other"}
    swapped = False

    def change_owner_before_completed_save(agent_id, report, expected_ids=None):
        nonlocal swapped
        if report.get("status") == "completed" and not swapped:
            swapped = True
            store.update(agent.id, {"context_archive": replacement_lease})
        return original_save(agent_id, report, expected_ids)

    monkeypatch.setattr(archiver, "save", change_owner_before_completed_save)
    report = archiver.archive(str(agent.id), HistoryArchiveRequest())
    assert report["status"] == "completed"
    assert store.get(agent.id).context_archive == replacement_lease


def test_recover_accepts_committed_replacement_prefix_and_preserves_timer(archiver_setup):
    archiver, store, agent, session = archiver_setup
    report = archiver.archive(str(agent.id), HistoryArchiveRequest())
    replacement = Path(report["directory"], "replacement.jsonl").read_bytes()
    records = decoded_lines(replacement)
    tail = {
        "type": "custom",
        "id": uuid4().hex[:8],
        "parentId": records[-1]["id"],
        "timestamp": "2025-01-01T00:00:00+00:00",
        "customType": "future",
        "data": {},
    }
    session.write_bytes(replacement + json.dumps(tail).encode() + b"\n")
    running = dict(report, status="running", stage="Replacing")
    archiver.save(str(agent.id), running, expected_ids={report["id"]})
    store.update(agent.id, {"auto_resume": True})

    archiver.recover()
    recovered = store.get(agent.id)
    assert recovered.context_archive["status"] == "completed"
    assert not recovered.auto_resume
    assert session.read_bytes().startswith(replacement)


def test_request_validation_is_strict():
    assert HistoryArchiveRequest().percent == 50
    for value in (0, 96, True, 1.5, "50"):
        with pytest.raises(ValidationError):
            HistoryArchiveRequest(percent=value)
