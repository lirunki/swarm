import json
import shutil
import threading
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from swarm.agent_archives import AgentArchives, ArchiveCorrupt, ArchiveRequest, ResurrectRequest
from swarm.files import FileRequest
from swarm.history_archive import retrieve_history
from swarm.importers.pi_history import PiHistory
from swarm.meta_compact import digest
from swarm.models import Agent, Status
from swarm.paths import agent_path
from swarm.server import create_app
from swarm.tool_inputs import retrieve
from swarm.tool_outputs import retrieve_output
from swarm.uploads import UploadRequest


def setup_archive(tmp_path, monkeypatch, *, reference: Path | None = None):
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    monkeypatch.setattr("swarm.sessions.running_pi_processes", lambda: [])
    app = create_app(tmp_path / "home", recover=False, allowed_hosts=["testserver"])
    controller = app.state.controller
    monkeypatch.setattr(controller.processes, "alive", lambda _agent: False)
    history = PiHistory(str(uuid4()), str(tmp_path), "Whole-agent archive")
    history.user("Keep this exact raw history, including SECRET-IN-SESSION.")
    history.historical("assistant", "Finished safely")
    source = controller.store.sessions_dir / "source.jsonl"
    source.write_text(history.encode(), encoding="utf-8")
    agent = controller.store.create(
        Agent(
            name="Archived worker",
            goal="Preserve this worker",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            status=Status.idle,
            session_id=history.entries[0]["id"],
            session_file=str(source),
            notes="exact checkpoint",
        )
    )
    if reference is not None:
        controller.files.add(agent.id, FileRequest(path=str(reference), label="Result"))
    return app, agent, source


def wait_job(service, request_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get_job(request_id)
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("archive job did not finish")


def test_archive_before_forget_and_same_id_inert_round_trip(tmp_path, monkeypatch):
    referenced = tmp_path / "result.txt"
    referenced.write_bytes(b"result bytes")
    app, agent, source = setup_archive(tmp_path, monkeypatch, reference=referenced)
    service = app.state.agent_archives
    raw_session = source.read_bytes()
    raw_agent = app.state.controller.store.path(agent.id).read_bytes()

    request = ArchiveRequest(include_files=True, request_id=uuid4())
    archived = service.archive(str(agent.id), request)
    assert archived["agent_id"] == str(agent.id)
    assert archived["files_included"] is True
    assert not app.state.controller.store.path(agent.id).exists()

    archive_path = app.state.controller.store.home / "archives" / f"{archived['id']}.zip"
    assert archive_path.stat().st_mode & 0o077 == 0
    with zipfile.ZipFile(archive_path) as bundle:
        assert bundle.read("agent.md") == raw_agent
        assert bundle.read("session.jsonl") == raw_session
        manifest = json.loads(bundle.read("manifest.json"))
        refs = json.loads(bundle.read("file_reference.json"))
        assert manifest["agent_id"] == str(agent.id)
        assert bundle.read(refs["files"][0]["archive_member"]) == b"result bytes"

    # Uncertain retries return the durable receipt and do not make another ZIP.
    assert service.archive(str(agent.id), request)["id"] == archived["id"]
    assert len(list((app.state.controller.store.home / "archives").glob("*.zip"))) == 1

    result = service.resurrect(archived["id"], ResurrectRequest(request_id=uuid4()))
    restored = app.state.controller.store.get(agent.id)
    assert result["agent"]["id"] == str(agent.id)
    assert restored.id == agent.id
    assert restored.status == Status.idle
    assert restored.auto_resume is False
    assert restored.pid is None
    assert restored.session_file == str(source)
    restored_ref = app.state.controller.files.read(agent.id).files[0]
    assert Path(restored_ref.path).read_bytes() == b"result bytes"
    assert restored_ref.path != str(referenced)


def test_archive_rejects_secret_reference_and_preserves_agent(tmp_path, monkeypatch):
    secret = tmp_path / "credentials.json"
    secret.write_text('{"token":"do-not-copy"}', encoding="utf-8")
    app, agent, _ = setup_archive(tmp_path, monkeypatch, reference=secret)

    with pytest.raises(ValueError, match="credential-like"):
        app.state.agent_archives.archive(
            str(agent.id), ArchiveRequest(include_files=True, request_id=uuid4())
        )

    assert app.state.controller.store.get(agent.id).id == agent.id
    assert list((app.state.controller.store.home / "archives").glob("*.zip")) == []


def test_start_is_immediate_and_build_holds_neither_controller_nor_index_lock(
    tmp_path, monkeypatch
):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    entered, release = threading.Event(), threading.Event()
    original = service._build

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_build", blocked)
    request = ArchiveRequest(include_files=False, request_id=uuid4())
    started = time.monotonic()
    accepted = service.start(str(agent.id), request)
    assert time.monotonic() - started < 0.3
    assert accepted["job"]["status"] == "queued"
    assert entered.wait(2)

    started = time.monotonic()
    with app.state.controller.lock:
        current = app.state.controller.store.get(agent.id)
    assert time.monotonic() - started < 0.3
    assert current.agent_archive["request_id"] == str(request.request_id)
    with service._lock():
        assert service._read_index()["archives"] == []
    release.set()
    assert wait_job(service, request.request_id)["status"] == "completed"


def test_start_does_not_snapshot_or_hash_session_on_request_path(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    entered, release = threading.Event(), threading.Event()
    from swarm import agent_archives as archive_module

    original = archive_module._source_snapshot

    def blocked(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(archive_module, "_source_snapshot", blocked)
    request = ArchiveRequest(include_files=False, request_id=uuid4())
    started = time.monotonic()
    accepted = service.start(str(agent.id), request)
    assert time.monotonic() - started < 0.3
    assert accepted["job"]["status"] == "queued"
    assert entered.wait(2)
    assert not service._snapshot_path(str(request.request_id), "source.jsonl").exists()
    release.set()
    assert wait_job(service, request.request_id)["status"] == "completed"


def test_shutdown_requeues_worker_instead_of_marking_archive_failed(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    original = service._build

    def close_during_build(*args, **kwargs):
        service._closing.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_build", close_during_build)
    request = ArchiveRequest(include_files=False, request_id=uuid4())
    with pytest.raises(RuntimeError, match="shutting down"):
        service.archive(str(agent.id), request)
    assert service.get_job(request.request_id)["status"] == "queued"
    assert app.state.controller.store.get(agent.id).agent_archive["status"] == "queued"


def test_two_agents_build_concurrently_and_source_race_preserves_record(tmp_path, monkeypatch):
    app, first, first_source = setup_archive(tmp_path, monkeypatch)
    controller = app.state.controller
    history = PiHistory(str(uuid4()), str(tmp_path), "Second archive")
    history.user("second")
    history.historical("assistant", "done")
    second_source = controller.store.sessions_dir / "second.jsonl"
    second_source.write_text(history.encode(), encoding="utf-8")
    second = controller.store.create(
        Agent(
            name="Second worker",
            goal="archive concurrently",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            status=Status.idle,
            session_id=history.entries[0]["id"],
            session_file=str(second_source),
        )
    )
    service = app.state.agent_archives
    original = service._build
    release = threading.Event()
    both_entered = threading.Event()
    count_lock = threading.Lock()
    entered = 0

    def blocked(*args, **kwargs):
        nonlocal entered
        with count_lock:
            entered += 1
            if entered == 2:
                both_entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_build", blocked)
    one = ArchiveRequest(include_files=False, request_id=uuid4())
    two = ArchiveRequest(include_files=False, request_id=uuid4())
    service.start(str(first.id), one)
    service.start(str(second.id), two)
    assert both_entered.wait(2), "the two-worker pool should build separate agents concurrently"
    first_source.write_bytes(first_source.read_bytes() + b"\n")
    release.set()

    failed = wait_job(service, one.request_id)
    completed = wait_job(service, two.request_id)
    assert failed["status"] == "failed"
    assert "Session changed" in failed["error"]
    retained = controller.store.get(first.id)
    assert retained.agent_archive["status"] == "failed"
    assert retained.auto_resume is False
    assert completed["status"] == "completed"
    assert not controller.store.path(second.id).exists()


def test_build_failure_and_lease_race_never_forget_agent(tmp_path, monkeypatch):
    failed_app, failed_agent, _ = setup_archive(tmp_path / "write", monkeypatch)
    failed_service = failed_app.state.agent_archives

    def cannot_write(*_args, **_kwargs):
        raise OSError("simulated ZIP write failure")

    monkeypatch.setattr(failed_service, "_build", cannot_write)
    failed_request = ArchiveRequest(include_files=False, request_id=uuid4())
    failed_service.start(str(failed_agent.id), failed_request)
    failed = wait_job(failed_service, failed_request.request_id)
    assert failed["status"] == "failed"
    assert (
        failed_app.state.controller.store.get(failed_agent.id).agent_archive["status"] == "failed"
    )

    race_app, race_agent, _ = setup_archive(tmp_path / "lease", monkeypatch)
    race_service = race_app.state.agent_archives
    original = race_service._build
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(race_service, "_build", blocked)
    race_request = ArchiveRequest(include_files=False, request_id=uuid4())
    race_service.start(str(race_agent.id), race_request)
    assert entered.wait(2)
    race_app.state.controller.store.update(
        race_agent.id,
        {
            "agent_archive": {
                "request_id": str(uuid4()),
                "archive_id": str(uuid4()),
                "status": "running",
            }
        },
    )
    release.set()
    raced = wait_job(race_service, race_request.request_id)
    assert raced["status"] == "failed"
    assert race_app.state.controller.store.get(race_agent.id).id == race_agent.id


def test_queued_job_recovers_and_exact_retry_is_idempotent(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    old = app.state.agent_archives

    class Deferred:
        def submit(self, *args, **kwargs):
            return None

        def shutdown(self, **kwargs):
            return None

    old._executor = Deferred()
    request = ArchiveRequest(include_files=False, request_id=uuid4())
    accepted = old.start(str(agent.id), request)
    assert accepted["job"]["status"] == "queued"
    assert app.state.controller.store.get(agent.id).agent_archive["status"] == "queued"

    recovered = AgentArchives(app.state.meta_compactor)
    recovered.recover()
    job = wait_job(recovered, request.request_id)
    assert job["status"] == "completed"
    replay = recovered.start(str(agent.id), request)
    assert replay["job"]["status"] == "completed"
    assert replay["archive"]["id"] == job["archive"]["id"]
    assert len(list((app.state.controller.store.home / "archives").glob("*.zip"))) == 1
    recovered.close()


def test_resurrection_recovers_job_durable_before_index_reservation(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    request = ResurrectRequest(request_id=uuid4())
    actual_save = service._save_job
    faulted = False

    def save_then_fault(request_id, job):
        nonlocal faulted
        actual_save(request_id, job)
        if job.get("operation") == "resurrect" and not faulted:
            faulted = True
            raise OSError("crash after prepared resurrection job")

    monkeypatch.setattr(service, "_save_job", save_then_fault)
    with pytest.raises(OSError, match="prepared resurrection"):
        service.resurrect(archived["id"], request)
    assert not app.state.controller.store.path(agent.id).exists()
    assert service.list()["archives"][0]["status"] == "archived"

    monkeypatch.setattr(service, "_save_job", actual_save)
    recovered = AgentArchives(app.state.meta_compactor)
    recovered.recover()
    assert wait_job(recovered, request.request_id)["status"] == "completed"
    assert recovered.resurrect(archived["id"], request)["agent"]["id"] == str(agent.id)
    recovered.close()


def test_resurrection_recovers_index_reservation_durable_after_write_fault(
    tmp_path, monkeypatch
):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    request = ResurrectRequest(request_id=uuid4())
    actual_write = service._write_index
    faulted = False

    def write_then_fault(index):
        nonlocal faulted
        actual_write(index)
        record = service._find(index, archived["id"])
        if record.get("status") == "restoring" and not faulted:
            faulted = True
            raise OSError("crash after durable index reservation")

    monkeypatch.setattr(service, "_write_index", write_then_fault)
    with pytest.raises(OSError, match="durable index reservation"):
        service.resurrect(archived["id"], request)
    assert not app.state.controller.store.path(agent.id).exists()
    assert service.list()["archives"][0]["status"] == "restoring"

    monkeypatch.setattr(service, "_write_index", actual_write)
    recovered = AgentArchives(app.state.meta_compactor)
    recovered.recover()
    assert wait_job(recovered, request.request_id)["status"] == "completed"
    assert recovered.resurrect(archived["id"], request)["agent"]["id"] == str(agent.id)
    recovered.close()


def test_resurrection_recovers_receipt_durable_before_completed_job(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    request = ResurrectRequest(request_id=uuid4())
    actual_save = service._save_job
    faulted = False

    def fail_completed_job(request_id, job):
        nonlocal faulted
        if job.get("operation") == "resurrect" and job.get("status") == "completed" and not faulted:
            faulted = True
            raise OSError("crash after durable resurrection receipt")
        actual_save(request_id, job)

    monkeypatch.setattr(service, "_save_job", fail_completed_job)
    with pytest.raises(OSError, match="durable resurrection receipt"):
        service.resurrect(archived["id"], request)
    restored = app.state.controller.store.get(agent.id)
    assert restored.resurrection_request_id == str(request.request_id)
    with service._lock():
        raw_job = service._read_job(str(request.request_id))
        receipt = service._receipt(service._read_index(), str(request.request_id))
    assert raw_job["status"] == "restoring"
    assert receipt["archive_id"] == archived["id"]

    monkeypatch.setattr(service, "_save_job", actual_save)
    recovered = AgentArchives(app.state.meta_compactor)
    submitted = []
    monkeypatch.setattr(recovered._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    recovered.recover()
    assert submitted == []
    assert recovered.get_job(request.request_id)["status"] == "completed"
    assert recovered.resurrect(archived["id"], request)["agent"]["id"] == str(agent.id)
    recovered.close()


def test_job_retention_keeps_all_live_jobs_and_only_prunes_finished_snapshots(
    tmp_path, monkeypatch
):
    app, _, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    active_ids = [str(uuid4()) for _ in range(257)]
    old_finished = str(uuid4())
    with service._lock():
        for request_id in active_ids:
            service._save_job(
                request_id,
                {"request_id": request_id, "id": request_id, "status": "queued"},
            )
        active_snapshot = service._snapshot_path(active_ids[0], "agent.md")
        active_snapshot.write_bytes(b"live")
        service._save_job(
            old_finished,
            {"request_id": old_finished, "id": old_finished, "status": "completed"},
        )
        old_snapshot = service._snapshot_path(old_finished, "agent.md")
        old_snapshot.write_bytes(b"finished")
        for _ in range(255):
            request_id = str(uuid4())
            service._save_job(
                request_id,
                {"request_id": request_id, "id": request_id, "status": "completed"},
            )
    assert all(service._job_path(request_id).exists() for request_id in active_ids)
    assert active_snapshot.read_bytes() == b"live"
    assert not service._job_path(old_finished).exists()
    assert not old_snapshot.exists()


def test_pruned_completed_resurrection_replays_receipt_without_new_work(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archive_request = ArchiveRequest(include_files=False, request_id=uuid4())
    archived = service.archive(str(agent.id), archive_request)
    restore_request = ResurrectRequest(request_id=uuid4())
    first = service.resurrect(archived["id"], restore_request)
    with service._lock():
        for _ in range(255):
            request_id = str(uuid4())
            service._save_job(
                request_id,
                {"request_id": request_id, "id": request_id, "status": "completed"},
            )
    assert not service._job_path(str(restore_request.request_id)).exists()
    monkeypatch.setattr(
        app.state.controller.store,
        "create",
        lambda *_args, **_kwargs: pytest.fail("receipt replay must not create an agent"),
    )
    replay = service.resurrect(archived["id"], restore_request)
    assert replay["agent"]["id"] == first["agent"]["id"]
    assert service.get_job(restore_request.request_id)["status"] == "completed"


def test_pruned_archive_request_replays_receipt_after_resurrection(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    request = ArchiveRequest(include_files=False, request_id=uuid4())
    archived = service.archive(str(agent.id), request)
    service.resurrect(archived["id"], ResurrectRequest(request_id=uuid4()))
    with service._lock():
        for _ in range(255):
            request_id = str(uuid4())
            service._save_job(
                request_id,
                {"request_id": request_id, "id": request_id, "status": "completed"},
            )
    assert not service._job_path(str(request.request_id)).exists()
    before = app.state.controller.store.path(agent.id).read_bytes()
    replay = service.archive(str(agent.id), request)
    assert replay["id"] == archived["id"]
    assert app.state.controller.store.path(agent.id).read_bytes() == before
    with pytest.raises(RuntimeError, match="different operation"):
        service.archive(
            str(agent.id),
            ArchiveRequest(include_files=True, request_id=request.request_id),
        )


def test_failed_archive_retry_does_not_replace_lease(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    monkeypatch.setattr(service, "_build", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")))
    request = ArchiveRequest(include_files=False, request_id=uuid4())
    with pytest.raises(OSError, match="boom"):
        service.archive(str(agent.id), request)
    before = app.state.controller.store.path(agent.id).read_bytes()
    with pytest.raises(RuntimeError, match="boom"):
        service.archive(str(agent.id), request)
    assert app.state.controller.store.path(agent.id).read_bytes() == before


def test_verify_streams_members_and_returns_identity_bound_lazy_payloads(tmp_path, monkeypatch):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"a" * (2 * 1024 * 1024 + 17))
    second.write_bytes(b"b" * (2 * 1024 * 1024 + 31))
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    app.state.controller.files.add(agent.id, FileRequest(path=str(first), label="First"))
    app.state.controller.files.add(agent.id, FileRequest(path=str(second), label="Second"))
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=True, request_id=uuid4())
    )

    original_read = zipfile.ZipExtFile.read

    def bounded_read(stream, size=-1):
        assert 0 <= size <= 1024 * 1024
        return original_read(stream, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    manifest, payloads = service._verify(archived["id"])
    assert not isinstance(payloads, dict)
    members = [name for name in payloads if name.startswith("files/")]
    assert len(members) == 2
    assert payloads[members[0]] in {first.read_bytes(), second.read_bytes()}
    assert manifest["archive_id"] == archived["id"]

    path = service._path(archived["id"])
    replacement = path.with_suffix(".replacement")
    shutil.copyfile(path, replacement)
    path.unlink()
    replacement.rename(path)
    with pytest.raises(ArchiveCorrupt, match="identity changed"):
        payloads[members[1]]


def test_resurrection_verification_holds_no_controller_or_archive_lock(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    entered, release = threading.Event(), threading.Event()
    original = service._verify

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_verify", blocked)
    request = ResurrectRequest(request_id=uuid4())
    outcome = []
    worker = threading.Thread(
        target=lambda: outcome.append(service.resurrect(archived["id"], request))
    )
    worker.start()
    assert entered.wait(2)

    started = time.monotonic()
    assert service.list()["archives"][0]["status"] == "restoring"
    with app.state.controller.lock:
        app.state.controller.store.scan()
    assert time.monotonic() - started < 0.3
    with pytest.raises(RuntimeError, match="restoration is active"):
        service.delete(archived["id"])

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert outcome[0]["agent"]["id"] == str(agent.id)


def test_download_verifies_outside_archive_lock(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    entered, release = threading.Event(), threading.Event()
    original = service._verify

    def blocked(archive_id, source=None):
        if source is not None:
            entered.set()
            assert release.wait(5)
        return original(archive_id, source)

    monkeypatch.setattr(service, "_verify", blocked)
    result = []
    worker = threading.Thread(target=lambda: result.append(service.download(archived["id"])))
    worker.start()
    assert entered.wait(2)
    started = time.monotonic()
    assert service.list()["archives"][0]["id"] == archived["id"]
    assert time.monotonic() - started < 0.3
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    _, stream = result[0]
    try:
        assert stream.read(2) == b"PK"
    finally:
        stream.close()


def test_resurrection_replays_owned_candidate_after_create_and_index_faults(tmp_path, monkeypatch):
    referenced = tmp_path / "result.bin"
    referenced.write_bytes(b"restored result")
    app, agent, _ = setup_archive(tmp_path, monkeypatch, reference=referenced)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=True, request_id=uuid4())
    )
    request = ResurrectRequest(request_id=uuid4())
    store = app.state.controller.store
    actual_create = store.create
    actual_write = app.state.controller.files.write
    calls = {"create": 0, "write": 0}

    def create_then_fault(candidate):
        calls["create"] += 1
        result = actual_create(candidate)
        if calls["create"] == 1:
            raise OSError("fault after durable agent create")
        return result

    def index_fault_once(agent_id, index):
        calls["write"] += 1
        if calls["write"] == 1:
            raise OSError("file index fault")
        return actual_write(agent_id, index)

    monkeypatch.setattr(store, "create", create_then_fault)
    monkeypatch.setattr(app.state.controller.files, "write", index_fault_once)
    with pytest.raises(OSError, match="file index fault"):
        service.resurrect(archived["id"], request)

    candidate = store.get(agent.id)
    assert candidate.resurrection_request_id == str(request.request_id)
    assert candidate.restored_from_archive == archived["id"]
    assert candidate.auto_resume is False and candidate.pid is None
    assert service.get_job(request.request_id)["status"] == "restoring"

    replay = service.resurrect(archived["id"], request)
    assert replay["agent"]["id"] == str(agent.id)
    assert calls["create"] == 1
    assert (
        Path(app.state.controller.files.read(agent.id).files[0].path).read_bytes()
        == b"restored result"
    )


def test_recovery_completes_only_audit_owned_resurrection_candidate(tmp_path, monkeypatch):
    referenced = tmp_path / "recover.bin"
    referenced.write_bytes(b"recover me")
    app, agent, _ = setup_archive(tmp_path, monkeypatch, reference=referenced)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=True, request_id=uuid4())
    )
    request = ResurrectRequest(request_id=uuid4())
    actual_write = app.state.controller.files.write
    failed = False

    def fail_once(agent_id, index):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("crash before file index commit")
        return actual_write(agent_id, index)

    monkeypatch.setattr(app.state.controller.files, "write", fail_once)
    with pytest.raises(OSError, match="file index commit"):
        service.resurrect(archived["id"], request)
    candidate = app.state.controller.store.get(agent.id)
    assert candidate.resurrection_request_id == str(request.request_id)

    recovered = AgentArchives(app.state.meta_compactor)
    recovered.recover()
    assert wait_job(recovered, request.request_id)["status"] == "completed"
    current = app.state.controller.store.get(agent.id)
    assert current.auto_resume is False and current.pid is None
    assert (
        Path(app.state.controller.files.read(agent.id).files[0].path).read_bytes() == b"recover me"
    )
    recovered.close()


def test_resurrection_conflict_never_modifies_unowned_existing_record(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    conflict = agent.model_copy(update={"name": "Unowned replacement", "session_id": "other"})
    app.state.controller.store.create(conflict)
    before = app.state.controller.store.path(agent.id).read_bytes()

    with pytest.raises(RuntimeError, match="already exists"):
        service.resurrect(archived["id"], ResurrectRequest(request_id=uuid4()))
    assert app.state.controller.store.path(agent.id).read_bytes() == before


def test_tampered_crc_or_manifest_never_creates_agent(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    path = app.state.controller.store.home / "archives" / f"{archived['id']}.zip"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(data)

    with pytest.raises(ArchiveCorrupt):
        service.resurrect(archived["id"], ResurrectRequest(request_id=uuid4()))
    assert not app.state.controller.store.path(agent.id).exists()
    assert path.exists()  # A failed restore never consumes the only archive.


def test_uploaded_bytes_are_always_owned_and_missing_optional_refs_are_omitted(
    tmp_path, monkeypatch
):
    uploaded = tmp_path / "upload.bin"
    uploaded.write_bytes(b"owned upload bytes")
    missing = tmp_path / "ordinary.txt"
    missing.write_bytes(b"ordinary bytes")
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    app.state.controller.files.add(
        agent.id, FileRequest(path=str(uploaded), label="Upload"), uploaded=True
    )
    app.state.controller.files.add(agent.id, FileRequest(path=str(missing), label="Ordinary"))
    missing.unlink()

    archived = app.state.agent_archives.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    path = app.state.controller.store.home / "archives" / f"{archived['id']}.zip"
    with zipfile.ZipFile(path) as bundle:
        refs = json.loads(bundle.read("file_reference.json"))
        assert [entry["filename"] for entry in refs["files"]] == ["upload.bin"]
        assert refs["files"][0]["archive_member"]
        assert bundle.read(refs["files"][0]["archive_member"]) == b"owned upload bytes"
        assert refs["missing_files"][0]["filename"] == "ordinary.txt"

    uploaded.unlink()
    result = app.state.agent_archives.resurrect(
        archived["id"], ResurrectRequest(request_id=uuid4())
    )
    restored = app.state.controller.files.read(agent.id).files
    assert len(restored) == 1
    assert restored[0].filename == "upload.bin"
    assert Path(restored[0].path).read_bytes() == b"owned upload bytes"
    assert result["missing_files"][0]["filename"] == "ordinary.txt"


def test_archive_lease_blocks_file_and_upload_mutations(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives

    class Deferred:
        def submit(self, *args, **kwargs):
            return None

    service._executor = Deferred()
    service.start(str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4()))
    candidate = tmp_path / "late.txt"
    candidate.write_text("late", encoding="utf-8")
    with pytest.raises(RuntimeError, match="running|finish"):
        app.state.controller.files.add(agent.id, FileRequest(path=str(candidate)))
    with pytest.raises(RuntimeError, match="running|finish"):
        app.state.uploads.begin(
            str(agent.id),
            UploadRequest(upload_id=uuid4(), filename="late.bin", size=0, label=""),
        )


def test_pending_upload_prevents_archive_reservation(tmp_path, monkeypatch):
    app, agent, _ = setup_archive(tmp_path, monkeypatch)
    app.state.uploads.temp_root = tmp_path / "upload-temp"
    app.state.uploads.begin(
        str(agent.id),
        UploadRequest(upload_id=uuid4(), filename="pending.bin", size=1, label=""),
    )
    with pytest.raises(RuntimeError, match="pending upload"):
        app.state.agent_archives.archive(
            str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
        )
    assert getattr(app.state.controller.store.get(agent.id), "agent_archive", None) is None


def test_all_referenced_tool_dependencies_restore_to_canonical_lookup_roots(tmp_path, monkeypatch):
    app, agent, source = setup_archive(tmp_path, monkeypatch)
    input_job, output_job = uuid4().hex, uuid4().hex
    toolrun = f"toolrun_{input_job}_{'1' * 16}"
    toolout = f"toolout_{output_job}_{'2' * 16}"
    source.write_bytes(
        source.read_bytes().replace(
            b"Finished safely", f"Finished safely {toolrun} {toolout}".encode()
        )
    )
    source_data = source.read_bytes()
    home = app.state.controller.store.home

    input_dir = home / "tool-input-compactions" / input_job
    input_dir.mkdir(parents=True)
    input_payload = b'{"command":"echo hi"}'
    (input_dir / "original.jsonl").write_bytes(source_data)
    (input_dir / f"{toolrun}.json").write_bytes(input_payload)
    (input_dir / "index.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "source_sha256": digest(source_data),
                "calls": {
                    toolrun: {
                        "tool_name": "bash",
                        "tool_call_id": "call-input",
                        "sha256": digest(input_payload),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    output_dir = home / "tool-output-repairs" / output_job
    output_dir.mkdir(parents=True)
    output_payload = json.dumps("full archived output").encode()
    (output_dir / "original.jsonl").write_bytes(source_data)
    (output_dir / f"{toolout}.json").write_bytes(output_payload)
    (output_dir / "index.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "source_sha256": digest(source_data),
                "outputs": {
                    toolout: {
                        "entry_id": "entry",
                        "tool_call_id": "call-output",
                        "sha256": digest(output_payload),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    archived = app.state.agent_archives.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    shutil.rmtree(input_dir)
    shutil.rmtree(output_dir)
    app.state.agent_archives.resurrect(archived["id"], ResurrectRequest(request_id=uuid4()))
    assert retrieve(app.state.controller.store, toolrun)["input"] == input_payload.decode()
    assert retrieve_output(app.state.controller.store, toolout)["output"] == "full archived output"


def test_session_dependencies_relocate_to_native_roots_and_rebind_owned_metadata(
    tmp_path, monkeypatch
):
    app, agent, source = setup_archive(tmp_path, monkeypatch)
    session_key = digest(agent.session_id.encode())[:24]
    spill_key = digest(agent.session_id.encode())[:16]
    history_id = uuid4().hex
    history_dir = source.parent / "context-archives" / session_key / history_id
    history_dir.mkdir(parents=True)
    history_text = b"# Archived model context\n\nEXACT HISTORICAL BYTES\n"
    (history_dir / "history.txt").write_bytes(history_text)
    (history_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "archive_id": history_id,
                "agent_id": str(agent.id),
                "session_id": agent.session_id,
                "source": str(source),
                "agent_source": agent_path(source),
                "source_descriptor": {"path": str(source), "sha256": "archived-value"},
                "files": {
                    "history.txt": {
                        "sha256": digest(history_text),
                        "length": len(history_text),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    spill_id = uuid4().hex
    spill_dir = source.parent / "output-spills" / f"session-{spill_key}"
    spill_dir.mkdir(parents=True)
    spill_text = b"owned spill bytes"
    spill_json = json.dumps({"content": spill_text.decode()}).encode()
    spill_file = spill_dir / f"{spill_id}.txt"
    spill_json_file = spill_dir / f"{spill_id}.json"
    spill_metadata_file = spill_dir / f"{spill_id}.meta.json"
    spill_file.write_bytes(spill_text)
    spill_json_file.write_bytes(spill_json)
    spill_metadata_file.write_text(
        json.dumps(
            {
                "id": spill_id,
                "session_id": agent.session_id,
                "file": str(spill_file),
                "json_file": str(spill_json_file),
                "metadata_file": str(spill_metadata_file),
            }
        ),
        encoding="utf-8",
    )
    records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    records.append(
        {
            "type": "custom",
            "id": uuid4().hex[:8],
            "parentId": records[-1]["id"],
            "timestamp": "2025-01-01T00:00:00Z",
            "customType": "swarm.output-spill.archive",
            "data": {
                "id": spill_id,
                "session_id": agent.session_id,
                "file": str(spill_file),
                "jsonFile": str(spill_json_file),
                "metadataFile": str(spill_metadata_file),
                "historyGuide": agent_path(history_dir / "history.txt"),
            },
        }
    )
    source.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    archived = app.state.agent_archives.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    source.unlink()
    shutil.rmtree(source.parent / "context-archives")
    shutil.rmtree(source.parent / "output-spills")
    app.state.agent_archives.resurrect(
        archived["id"], ResurrectRequest(request_id=uuid4())
    )

    restored = app.state.controller.store.get(agent.id)
    restored_source = Path(restored.session_file)
    restored_history = (
        restored_source.parent / "context-archives" / session_key / history_id
    )
    assert (restored_history / "history.txt").read_bytes() == history_text
    history_manifest = json.loads((restored_history / "manifest.json").read_text())
    assert history_manifest["source"] == str(restored_source)
    assert history_manifest["agent_source"] == agent_path(restored_source)
    assert history_manifest["source_descriptor"]["path"] == str(source)
    assert retrieve_history(app.state.controller.store, agent.id, history_id)["output"].startswith(
        "# Archived model context"
    )

    restored_records = [json.loads(line) for line in restored_source.read_text().splitlines()]
    restored_ref = next(
        item["data"]
        for item in restored_records
        if item.get("customType") == "swarm.output-spill.archive"
    )
    restored_spill = (
        restored_source.parent / "output-spills" / f"session-{spill_key}" / spill_file.name
    )
    assert Path(restored_ref["file"]) == restored_spill
    assert Path(restored_ref["file"]).read_bytes() == spill_text
    assert restored_ref["historyGuide"] == agent_path(restored_history / "history.txt")
    metadata = json.loads(Path(restored_ref["metadataFile"]).read_text())
    assert metadata["file"] == restored_ref["file"]
    assert metadata["json_file"] == restored_ref["jsonFile"]
    assert metadata["metadata_file"] == restored_ref["metadataFile"]


def test_dependency_restore_never_overwrites_different_canonical_bytes(tmp_path, monkeypatch):
    app, agent, source = setup_archive(tmp_path, monkeypatch)
    job_id = uuid4().hex
    toolrun = f"toolrun_{job_id}_{'3' * 16}"
    source.write_bytes(source.read_bytes().replace(b"Finished safely", toolrun.encode()))
    source_data = source.read_bytes()
    directory = app.state.controller.store.home / "tool-input-compactions" / job_id
    directory.mkdir(parents=True)
    payload = b'{"safe":true}'
    (directory / "original.jsonl").write_bytes(source_data)
    (directory / f"{toolrun}.json").write_bytes(payload)
    (directory / "index.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "source_sha256": digest(source_data),
                "calls": {
                    toolrun: {
                        "tool_name": "read",
                        "tool_call_id": "call",
                        "sha256": digest(payload),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    archived = app.state.agent_archives.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    (directory / f"{toolrun}.json").write_bytes(b"different")

    with pytest.raises(RuntimeError, match="overwrite different bytes"):
        app.state.agent_archives.resurrect(archived["id"], ResurrectRequest(request_id=uuid4()))
    assert not app.state.controller.store.path(agent.id).exists()


def test_changed_original_session_is_not_overwritten(tmp_path, monkeypatch):
    app, agent, source = setup_archive(tmp_path, monkeypatch)
    service = app.state.agent_archives
    original = source.read_bytes()
    archived = service.archive(
        str(agent.id), ArchiveRequest(include_files=False, request_id=uuid4())
    )
    source.write_bytes(b"new unrelated bytes\n")

    service.resurrect(archived["id"], ResurrectRequest(request_id=uuid4()))
    restored = app.state.controller.store.get(agent.id)
    assert source.read_bytes() == b"new unrelated bytes\n"
    assert Path(restored.session_file).read_bytes() == original
    assert Path(restored.session_file).is_relative_to(app.state.controller.store.sessions_dir)
