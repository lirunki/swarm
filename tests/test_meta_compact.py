import json
import shlex
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from swarm.importers.pi_history import PiHistory
from swarm.meta_compact import (
    MetaCompactRequest,
    PiSummarizer,
    digest,
    project_session,
    split_transcript,
    validate_summary,
)
from swarm.models import Agent
from swarm.paths import agent_path
from swarm.server import create_app

SUMMARY = (
    "# Goal\nFinish the implementation.\n# Progress\nChanges are saved; tests still need review.\n"
    "# Decisions\nKeep original session archived.\n# Next steps\nInspect files before continuing."
)


def make_fake_pi(tmp_path, source):
    script = tmp_path / "fake-pi.py"
    script.write_text(source)
    executable = tmp_path / "fake-pi"
    # Interpreter paths containing spaces cannot be used directly in a shebang.
    executable.write_text(
        "#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n'
    )
    executable.chmod(0o700)
    return executable


def sample(folder):
    history = PiHistory(str(uuid4()), str(folder), "Large session")
    history.add("model_change", provider="test", modelId="large")
    history.user("Keep the goal and decisions. " * 300)
    history.historical("tool results", "Already ran tests, result success. " * 300)
    return history


@pytest.fixture
def app_agent(tmp_path, monkeypatch):
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    app = create_app(tmp_path / "swarm", recover=False, allowed_hosts=["testserver"])
    ctl = app.state.controller
    monkeypatch.setattr(ctl.processes, "alive", lambda a: False)
    history = sample(tmp_path)
    path = ctl.store.sessions_dir / "original.jsonl"
    path.write_text(history.encode())
    agent = ctl.store.create(
        Agent(
            name="Test",
            goal="G",
            folder=str(tmp_path),
            managed=True,
            session_file=str(path),
            session_id=history.entries[0]["id"],
            notes="Keep this checkpoint",
            auto_resume=False,
        )
    )
    return app, agent, path


def wait_job(app, agent):
    for thread in app.state.meta_compactor.threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    return app.state.controller.store.get(agent.id).meta_compaction


@pytest.mark.parametrize("chunk_count", [2, 3, 4, 16])
@pytest.mark.parametrize("overlap_tokens", [0, 10000, 100000])
def test_split_has_exact_shared_regions_and_full_unicode_coverage(chunk_count, overlap_tokens):
    text = "abc😀" * 50000 + "END"
    chunks, info = split_transcript(text, overlap_tokens, chunk_count)
    ranges = info["chunks"]
    assert len(chunks) == len(ranges) == info["chunk_count"] == chunk_count
    assert ranges[0]["start"] == 0 and ranges[-1]["end"] == len(text)
    expected_overlap = min(overlap_tokens * 4, len(text) // (2 * chunk_count))
    assert len(info["overlaps"]) == chunk_count - 1
    for i, overlap in enumerate(info["overlaps"]):
        start, end = overlap["start"], overlap["end"]
        assert end - start == expected_overlap
        assert ranges[i]["end"] == end and ranges[i + 1]["start"] == start
        shared = text[start:end]
        assert shared in chunks[i] and shared in chunks[i + 1]
        if i + 2 < chunk_count:
            assert ranges[i]["end"] <= ranges[i + 2]["start"]  # no triple overlap
    for i, chunk in enumerate(chunks):
        unique_start = info["overlaps"][i - 1]["end"] if i else 0
        unique_end = info["overlaps"][i]["start"] if i < chunk_count - 1 else len(text)
        assert text[unique_start:unique_end] in chunk
    assert chunks[0].startswith(text[: info["overlaps"][0]["start"]])
    assert chunks[-1].endswith(text[info["overlaps"][-1]["end"] :])


def test_split_defaults_and_short_input_overlap_cap():
    chunks, info = split_transcript("a" * 200, 10000)
    assert len(chunks) == 2 and info["overlap_tokens_estimate"] == 50 / 4


def test_projection_honors_branches_compaction_context_edits(tmp_path):
    history = sample(tmp_path)
    root = history.parent
    history.user("Abandoned branch must not leak")
    history.parent = root
    history.user("Old summarized text must not leak")
    history.add(
        "compaction", summary="OLDER SUMMARY", firstKeptEntryId=history.parent, tokensBefore=10000
    )
    old_compact = history.parent
    history.user("KEEP ORIGINAL")
    kept = history.parent
    history.add(
        "compaction", summary="LATEST SUMMARY", firstKeptEntryId=old_compact, tokensBefore=10000
    )
    history.add("context_edit", targetId=kept, replacement={"content": "CORRECTED CONTENT"})
    history.user("OMITTED TEXT")
    omitted = history.parent
    history.add("context_edit", targetId=omitted, replacement=None)
    history.user("LATEST TASK")
    header, text, model = project_session(history.encode().encode())
    assert header["id"] and model == "test/large"
    assert "LATEST SUMMARY" in text and "CORRECTED CONTENT" in text and "LATEST TASK" in text
    for absent in (
        "Abandoned branch",
        "Old summarized",
        "KEEP ORIGINAL",
        "OLDER SUMMARY",
        "OMITTED TEXT",
    ):
        assert absent not in text


def test_projection_rejects_broken_chain(tmp_path):
    history = sample(tmp_path)
    history.entries[-1]["parentId"] = "missing"
    with pytest.raises(ValueError, match="parent chain"):
        project_session(history.encode().encode())


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no JSON",
        '{"summary":""}',
        '{"summary":"tiny"}',
        '{"summary":12}',
        json.dumps({"summary": SUMMARY, "extra": 1}),
    ],
)
def test_summary_rejects_invalid_results(value):
    with pytest.raises(ValueError):
        validate_summary(value)


@pytest.mark.parametrize("chunk_count", [2, 4, 16])
def test_full_job_archives_replaces_same_identity_and_keeps_artifacts(app_agent, chunk_count):
    app, agent, path = app_agent
    original = path.read_bytes()
    calls, stages = [], []

    def summarize(prompt, session, model, cancel):
        assert path.read_bytes() == original  # untouched until every stage completes
        assert not cancel.is_set() and model == "test/large"
        calls.append(prompt.read_text())
        stages.append(app.state.controller.store.get(agent.id).meta_compaction["stage"])
        session.write_text(sample(session.parent).encode())
        return SUMMARY

    app.state.meta_compactor.summarize = summarize
    with TestClient(app) as client:
        response = client.post(
            f"/api/agents/{agent.id}/meta-compact",
            json={} if chunk_count == 2 else {"chunk_count": chunk_count},
        )
        assert response.status_code == 202, response.text
        job = wait_job(app, agent)
        assert job["status"] == "completed", job
        assert job["chunk_count"] == chunk_count and len(calls) == chunk_count + 1
        for i in range(chunk_count):
            assert f"chunk {i + 1} of {chunk_count}" in calls[i]
            assert stages[i] == f"Summarizing chunk {i + 1}/{chunk_count}"
            assert f"CHUNK {i + 1}:" in calls[-1]
        assert "Deduplicate" in calls[-1] and calls[-1].count(SUMMARY) == chunk_count
        directory = Path(job["directory"])
        assert (directory / "original.jsonl").read_bytes() == original
        assert not (directory / "source.jsonl").exists()
        for i in range(1, chunk_count + 1):
            assert (directory / f"chunk-{i}.txt").exists()
            assert (directory / f"chunk-{i}.jsonl").exists()
            assert (directory / f"summary-{i}.txt").exists()
        assert (directory / "replacement.jsonl").read_bytes() == path.read_bytes()
        header, projected, model = project_session(path.read_bytes())
        assert header["id"] == agent.session_id and SUMMARY in projected
        assert header["cwd"] == agent_path(agent.folder)
        assert model == "test/large"
        updated = app.state.controller.store.get(agent.id)
        assert updated.session_file == agent.session_file and updated.session_id == agent.session_id
        assert updated.notes == agent.notes and updated.goal == agent.goal
        assert not updated.auto_resume and updated.wake_at is None


@pytest.mark.parametrize("chunk_count,stage", [(2, 1), (2, 2), (2, 3), (4, 3), (4, 4), (4, 5)])
def test_each_stage_failure_leaves_original_untouched(app_agent, chunk_count, stage):
    app, agent, path = app_agent
    original = path.read_bytes()
    calls = 0

    def fail(prompt, session, model, cancel):
        nonlocal calls
        calls += 1
        if calls == stage:
            raise RuntimeError("Provider context overflow")
        return SUMMARY

    app.state.meta_compactor.summarize = fail
    with TestClient(app) as client:
        assert (
            client.post(
                f"/api/agents/{agent.id}/meta-compact", json={"chunk_count": chunk_count}
            ).status_code
            == 202
        )
        job = wait_job(app, agent)
        assert job["status"] == "failed" and "overflow" in job["error"]
        assert path.read_bytes() == original and calls == stage
        assert not app.state.controller.store.get(agent.id).auto_resume


@pytest.mark.parametrize("change", ["source", "external", "symlink"])
def test_concurrent_changes_fail_closed(app_agent, monkeypatch, change):
    app, agent, path = app_agent
    original = path.read_bytes()

    def summarize(prompt, session, model, cancel):
        if prompt.name == "merge.txt":
            if change == "source":
                path.write_bytes(original + b"\n")
            elif change == "external":
                monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [agent.folder])
            else:
                target = path.with_suffix(".saved")
                target.write_bytes(original)
                path.unlink()
                path.symlink_to(target)
        return SUMMARY

    app.state.meta_compactor.summarize = summarize
    with TestClient(app) as client:
        client.post(f"/api/agents/{agent.id}/meta-compact", json={})
        job = wait_job(app, agent)
        assert job["status"] == "failed", job
        assert path.read_bytes() == original + (b"\n" if change == "source" else b"")


def test_busy_job_guards_mutations_and_survives_dialog_close(app_agent):
    app, agent, path = app_agent
    entered, release = threading.Event(), threading.Event()

    def blocked(prompt, session, model, cancel):
        entered.set()
        assert release.wait(5)
        return SUMMARY

    app.state.meta_compactor.summarize = blocked
    with TestClient(app) as client:
        client.post(f"/api/agents/{agent.id}/meta-compact", json={})
        try:
            assert entered.wait(3)
            for suffix in ("wake", "stop", "meta-compact"):
                result = client.post(f"/api/agents/{agent.id}/{suffix}", json={})
                assert result.status_code == 409, result.text
            assert (
                client.patch(f"/api/agents/{agent.id}", json={"auto_resume": True}).status_code
                == 409
            )
            assert client.delete(f"/api/agents/{agent.id}").status_code == 409
            app.state.controller.recover()
            app.state.controller.tick()
            record = client.get("/api/state").json()["agents"][0]
            assert record["meta_compaction"]["status"] == "running" and not record["alive"]
        finally:
            release.set()
        assert wait_job(app, agent)["status"] == "completed"


def test_reject_live_unmanaged_invalid_request_and_cross_origin(app_agent, monkeypatch):
    app, agent, _ = app_agent
    with TestClient(app) as client:
        url = f"/api/agents/{agent.id}/meta-compact"
        for data in (
            {"overlap_tokens": -1},
            {"overlap_tokens": 1.5},
            {"overlap_tokens": 100001},
            {"chunk_count": 0},
            {"chunk_count": 1},
            {"chunk_count": 17},
            {"chunk_count": 2.5},
            {"chunk_count": True},
            {"chunk_count": "4"},
            {"path": "/etc/passwd"},
        ):
            assert client.post(url, json=data).status_code == 422
        assert (
            client.post(url, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        )
        monkeypatch.setattr(app.state.controller.processes, "alive", lambda a: True)
        assert client.post(url, json={}).status_code == 409
        monkeypatch.setattr(app.state.controller.processes, "alive", lambda a: False)
        app.state.controller.store.update(agent.id, {"managed": False})
        assert client.post(url, json={}).status_code == 400


@pytest.mark.parametrize("replaced", [False, True])
def test_recover_does_not_repeat_paid_work_and_reconciles_replace(app_agent, replaced):
    app, agent, path = app_agent
    directory = app.state.controller.store.home / "meta-compactions" / str(uuid4())
    directory.mkdir(parents=True)
    job = {
        "id": str(uuid4()),
        "status": "running",
        "stage": "Replacing session",
        "source": str(path),
        "directory": str(directory),
        "replacement_sha256": digest(path.read_bytes()) if replaced else "different",
    }
    app.state.meta_compactor.save(str(agent.id), job)
    app.state.controller.store.update(agent.id, {"auto_resume": True})
    with TestClient(app):
        updated = app.state.controller.store.get(agent.id)
        assert updated.meta_compaction["status"] == ("completed" if replaced else "failed")
        assert not updated.auto_resume and not app.state.meta_compactor.threads


def test_disk_failure_at_replace_preserves_archive_and_original(app_agent, monkeypatch):
    app, agent, path = app_agent
    original = path.read_bytes()
    import swarm.meta_compact as module

    real_write = module.atomic_write

    def fail(path_arg, content):
        if path_arg == path:
            raise OSError("Disk full")
        real_write(path_arg, content)

    monkeypatch.setattr(module, "atomic_write", fail)
    app.state.meta_compactor.summarize = lambda *args: SUMMARY
    with TestClient(app) as client:
        client.post(f"/api/agents/{agent.id}/meta-compact", json={})
        job = wait_job(app, agent)
        assert job["status"] == "failed"
        assert path.read_bytes() == original
        assert Path(job["archive"]).read_bytes() == original


def test_pi_runner_uses_stdin_isolation_and_validates_events(tmp_path, monkeypatch):
    fake = make_fake_pi(
        tmp_path,
        f"""
import json, os, sys
from pathlib import Path
from swarm.paths import local_path
assert '--no-tools' in sys.argv and '--no-extensions' in sys.argv
assert '--no-context-files' in sys.argv and '--no-mcp' in sys.argv
transport = {{'SWARM_WINDOWS_NODE', 'SWARM_WINDOWS_PI_ENTRY'}} if sys.platform == 'cygwin' else set()
assert all(k in transport for k in os.environ if k.startswith('SWARM_'))
assert 'PI_SESSION_FILE' not in os.environ
assert 'COMPLETE_INPUT_END' in sys.stdin.read()
path = local_path(sys.argv[sys.argv.index('--session') + 1])
path.write_text('worker session')
print(json.dumps({{'type':'message_end', 'message':{{'role':'assistant', 'stopReason':'stop',
 'content':[{{'type':'text','text':json.dumps({{'summary':{SUMMARY!r}}})}}]}}}}))
print(json.dumps({{'type':'agent_settled','aborted':False}}))
""",
    )
    prompt = tmp_path / "half.txt"
    prompt.write_text("x" * 200000 + "COMPLETE_INPUT_END")
    monkeypatch.setenv("SWARM_AGENT_ID", "parent-must-not-leak")
    monkeypatch.setenv("PI_SESSION_FILE", "parent.jsonl")
    output = PiSummarizer(str(fake))(prompt, tmp_path / "worker.jsonl", "p/m", threading.Event())
    assert output == SUMMARY
    assert (tmp_path / "worker.jsonl").exists()


def test_request_and_summary_validation():
    assert MetaCompactRequest().overlap_tokens == 10000
    assert MetaCompactRequest().chunk_count == 2
    assert validate_summary(json.dumps({"summary": SUMMARY})) == SUMMARY


@pytest.mark.parametrize("stop,aborted", [("error", False), ("length", False), ("stop", True)])
def test_pi_runner_rejects_failed_or_truncated_completion(tmp_path, stop, aborted):
    events = [
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": stop,
                "content": [{"type": "text", "text": json.dumps({"summary": SUMMARY})}],
            },
        },
        {"type": "agent_settled", "aborted": aborted},
    ]
    fake = make_fake_pi(
        tmp_path,
        f"import sys\nsys.stdin.read()\nprint({chr(10).join(json.dumps(e) for e in events)!r})\n",
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Input")
    with pytest.raises(ValueError, match="did not finish"):
        PiSummarizer(str(fake))(prompt, tmp_path / "worker.jsonl", None, threading.Event())


@pytest.mark.parametrize("reason", ["timeout", "cancel"])
def test_worker_is_reaped_on_timeout_and_cancellation(tmp_path, monkeypatch, reason):
    import swarm.meta_compact as module

    fake = make_fake_pi(tmp_path, "import time\ntime.sleep(30)\n")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Input")
    cancelled = threading.Event()
    if reason == "cancel":
        cancelled.set()
    else:
        monkeypatch.setattr(module, "WORKER_TIMEOUT", 0.01)
    with pytest.raises(RuntimeError, match="cancelled|timed out"):
        PiSummarizer(str(fake))(prompt, tmp_path / "worker.jsonl", None, cancelled)


def test_preflight_rejects_unknown_or_oversized_models_before_requests(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "swarm.meta_compact.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="test large 1M 128K yes yes\n"),
    )
    prompt = tmp_path / "input.txt"
    prompt.write_text("x" * 10000)
    runner = PiSummarizer("unused")
    runner.preflight([prompt] * 16, "test/large")
    with pytest.raises(ValueError, match="Choose"):
        runner.preflight([prompt], None)
    with pytest.raises(ValueError, match="unavailable"):
        runner.preflight([prompt], "test/missing")
    prompt.write_text("x" * 3000000)
    with pytest.raises(ValueError, match="70%"):
        runner.preflight([prompt], "test/large")


def test_preflight_accounts_for_every_summary_in_merge_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "swarm.meta_compact.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="test medium 200K 64K yes yes\n"),
    )
    prompt = tmp_path / "chunk.txt"
    prompt.write_text("Short input")
    runner = PiSummarizer("unused")
    runner.preflight([prompt] * 4, "test/medium")
    with pytest.raises(ValueError, match="combined summary budget"):
        runner.preflight([prompt] * 5, "test/medium")
