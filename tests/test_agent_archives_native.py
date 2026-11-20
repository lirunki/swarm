"""Native SDK round trip for whole-agent archives; never contacts a model provider."""

import hashlib
import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from swarm.agent_archives import AgentArchives, ArchiveRequest, ResurrectRequest
from swarm.controller import Controller
from swarm.history_archive import HistoryArchiver, HistoryArchiveRequest
from swarm.importers.pi_history import PiHistory
from swarm.meta_compact import MetaCompactor
from swarm.models import Agent, Status
from swarm.paths import agent_path
from swarm.store import Store

ROOT = Path(__file__).resolve().parents[1]
HISTORY_READER = ROOT / "src" / "swarm" / "extensions" / "history-reader.js"
OUTPUT_READER = ROOT / "src" / "swarm" / "extensions" / "output-spill.js"
NATIVE = pytest.mark.skipif(
    not shutil.which("pi") or not shutil.which("node"), reason="Installed Pi/Node optional"
)


class FakeProcesses:
    """A process boundary that makes accidental Pi launches fail loudly."""

    def alive(self, _agent):
        return False

    def pid(self, _agent):
        return None

    def health(self):
        return {"pi": True, "tmux": True}

    def __getattr__(self, name):
        raise AssertionError(f"archive test must not call Processes.{name}")


def add_message(history, role, content, **fields):
    history.add(
        "message",
        message={"role": role, "content": content, "timestamp": history.milliseconds, **fields},
    )


def wait_job(service, request_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = service.get_job(request_id)
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("whole-agent archive did not finish within five seconds")


def native_history(folder):
    history = PiHistory(str(uuid4()), str(folder), "Native whole-agent archive")
    history.add(
        "compaction",
        summary="OLD ACTIVE CHECKPOINT " + "s" * 8_000,
        firstKeptEntryId=history.parent,
        tokensBefore=4_000,
        systemMessage={
            "role": "system",
            "content": "CHECKPOINT SYSTEM",
            "sections": {"native": "unchanged"},
            "toolsAdded": [
                {"name": "fixture", "description": "fixture", "parameters": {"type": "object"}}
            ],
            "timestamp": 1,
        },
    )
    for index in range(4):
        history.user(f"TURN {index} " + chr(65 + index) * 8_000)
        call_id = f"safe-call-{index}"
        add_message(
            history,
            "assistant",
            [{"type": "toolCall", "id": call_id, "name": "fixture", "arguments": {"n": index}}],
            api="fixture-api",
            provider="fixture-provider",
            model="fixture-model",
            stopReason="toolUse",
            usage={"totalTokens": 1},
        )
        add_message(
            history,
            "toolResult",
            [{"type": "text", "text": f"SAFE RESULT {index}"}],
            toolCallId=call_id,
            toolName="fixture",
            isError=False,
        )
        add_message(
            history,
            "assistant",
            [{"type": "text", "text": f"ANSWER {index} " + "z" * 8_000}],
            api="fixture-api",
            provider="fixture-provider",
            model="fixture-model",
            stopReason="stop",
            usage={"totalTokens": 1},
        )
    return history


def append_spill_reference(session, session_id):
    records = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
    parent = records[-1]["id"]
    spill_id = str(uuid4())
    key = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    directory = session.parent / "output-spills" / f"session-{key}"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    text = ("NATIVE SPILL PAYLOAD\n" + "0123456789abcdef" * 1024).encode()
    raw = json.dumps(
        {
            "role": "toolResult",
            "toolCallId": "old-spill-call",
            "toolName": "fixture",
            "content": [{"type": "text", "text": text.decode()}],
            "isError": False,
        },
        separators=(",", ":"),
    ).encode()
    text_file = directory / f"{spill_id}.txt"
    json_file = directory / f"{spill_id}.json"
    metadata_file = directory / f"{spill_id}.meta.json"
    text_file.write_bytes(text)
    json_file.write_bytes(raw)
    metadata = {
        "id": spill_id,
        "session_id": session_id,
        "tool_call_id": "old-spill-call",
        "tool_name": "fixture",
        "original_bytes": len(text),
        "file": str(text_file),
        "json_file": str(json_file),
        "metadata_file": str(metadata_file),
        "created_at": "2025-01-01T00:00:00Z",
        "sha256": hashlib.sha256(text).hexdigest(),
        "json_sha256": hashlib.sha256(raw).hexdigest(),
    }
    metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
    for path in (text_file, json_file, metadata_file):
        path.chmod(0o600)
    entry_id = uuid4().hex[:8]
    records.append(
        {
            "type": "custom",
            "id": entry_id,
            "parentId": parent,
            "timestamp": "2025-01-01T00:00:00Z",
            "customType": "swarm.output-spill.archive",
            "data": {
                "id": spill_id,
                "session_id": session_id,
                "file": str(text_file),
                "jsonFile": str(json_file),
                "metadataFile": str(metadata_file),
            },
        }
    )
    session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    return text_file, text


def pi_root():
    return Path(shutil.which("pi")).resolve().parent.parent


@NATIVE
def test_whole_archive_restores_native_projection_and_both_readers_after_os_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("swarm.sessions.running_pi_folders", lambda: [])
    monkeypatch.setattr("swarm.sessions.running_pi_processes", lambda: [])
    store = Store(tmp_path / "swarm-home")
    controller = Controller(store, FakeProcesses())
    guard = MetaCompactor(controller, pi="/test-only/no-pi")
    archives = AgentArchives(guard)
    history_archiver = HistoryArchiver(guard)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    history = native_history(workspace)
    source = store.sessions_dir / "native-source.jsonl"
    source.write_text(history.encode(), encoding="utf-8")
    agent = store.create(
        Agent(
            name="Native archived worker",
            goal="Round-trip without model calls",
            folder=str(workspace),
            managed=True,
            auto_resume=False,
            status=Status.idle,
            session_id=history.entries[0]["id"],
            session_file=str(source),
            notes="exact tool checkpoint",
        )
    )

    history_report = history_archiver.archive(
        str(agent.id), HistoryArchiveRequest(percent=50, request_id=uuid4())
    )
    old_history_directory = Path(history_report["directory"])
    old_spill_file, spill_payload = append_spill_reference(source, agent.session_id)
    before_whole_archive = source.read_bytes()
    comparison = tmp_path / "before-whole-archive.jsonl"
    comparison.write_bytes(before_whole_archive)

    request = ArchiveRequest(include_files=False, request_id=uuid4())
    accepted = archives.start(str(agent.id), request)
    assert accepted["job"]["status"] in {"queued", "running"}
    completed = wait_job(archives, request.request_id)
    assert completed["status"] == "completed", completed
    archive_id = completed["archive"]["id"]
    assert not store.path(agent.id).exists()

    archive_zip = store.home / "archives" / f"{archive_id}.zip"
    raw_zip = archive_zip.read_bytes()
    with zipfile.ZipFile(archive_zip) as bundle:
        assert bundle.read("session.jsonl") == before_whole_archive
        names = set(bundle.namelist())
        assert any(name.endswith("/history.txt") for name in names)
        assert any(name.endswith(f"/{old_spill_file.name}") for name in names)

    # Simulate OS/session retention cleanup, not the archive service's normal forget path.
    source.unlink()
    shutil.rmtree(source.parent / "context-archives")
    shutil.rmtree(source.parent / "output-spills")
    assert not old_history_directory.exists() and not old_spill_file.exists()

    resurrected = archives.resurrect(
        archive_id, ResurrectRequest(request_id=uuid4())
    )
    restored = store.get(agent.id)
    restored_source = Path(restored.session_file)
    assert resurrected["agent"]["id"] == str(agent.id)
    assert restored.id == agent.id and restored.auto_resume is False
    assert restored.status == Status.idle and restored.pid is None
    assert restored_source != source and restored_source.is_file()
    assert restored_source.read_bytes() != before_whole_archive  # only archived paths relocate
    assert archive_zip.read_bytes() == raw_zip  # resurrection never rewrites evidence in the ZIP

    original_records = [json.loads(line) for line in before_whole_archive.splitlines()]
    restored_records = [json.loads(line) for line in restored_source.read_text().splitlines()]
    assert [item.get("id") for item in restored_records] == [
        item.get("id") for item in original_records
    ]
    restored_ref = next(
        item["data"]
        for item in restored_records
        if item.get("customType") == "swarm.output-spill.archive"
    )
    restored_spill = Path(restored_ref["file"])
    assert restored_spill != old_spill_file and restored_spill.read_bytes() == spill_payload

    session_hash = hashlib.sha256(agent.session_id.encode()).hexdigest()[:24]
    native_history_directory = (
        restored_source.parent
        / "context-archives"
        / session_hash
        / history_report["archive_id"]
    )
    assert (native_history_directory / "history.txt").is_file()
    history_manifest = native_history_directory / "manifest.json"
    manifest = json.loads(history_manifest.read_text(encoding="utf-8"))
    assert manifest["source"] == str(restored_source)
    assert manifest["agent_source"] == agent_path(restored_source)

    spill_metadata = json.loads(Path(restored_ref["metadataFile"]).read_text(encoding="utf-8"))
    assert spill_metadata["file"] == restored_ref["file"]
    assert spill_metadata["json_file"] == restored_ref["jsonFile"]
    assert spill_metadata["metadata_file"] == restored_ref["metadataFile"]

    config = tmp_path / "pi-config"
    config.mkdir()
    (config / "settings.json").write_text(
        json.dumps({"enableAnalytics": False, "enableInstallTelemetry": False})
    )
    script = r"""
import fs from 'node:fs';
const root=process.argv[1];
const {DefaultResourceLoader,SettingsManager,SessionManager,createAgentSession}=await import(root+'/index.js');
const {buildSessionProjection}=await import(root+'/core/session-manager.js');
const {getCurrentSystemMessage}=await import(process.argv[2]);
const cwd=process.argv[3],agentDir=process.argv[4],beforeFile=process.argv[5],afterFile=process.argv[6];
const historyExtension=process.argv[7],outputExtension=process.argv[8],historyId=process.argv[9],spillPath=process.argv[10];
const load=p=>fs.readFileSync(p,'utf8').trim().split('\n').map(JSON.parse).slice(1);
const before=buildSessionProjection(load(beforeFile)),after=buildSessionProjection(load(afterFile));
if(JSON.stringify(before.messages)!==JSON.stringify(after.messages)) throw Error('effective native messages changed');
if(JSON.stringify(getCurrentSystemMessage(before.messages))!==JSON.stringify(getCurrentSystemMessage(after.messages))) throw Error('system checkpoint changed');
const calls=m=>m.flatMap(x=>x.role==='assistant'?x.content.filter(b=>b.type==='toolCall').map(b=>b.id):[]);
const results=m=>m.filter(x=>x.role==='toolResult').map(x=>x.toolCallId);
if(JSON.stringify(calls(after.messages))!==JSON.stringify(results(after.messages))) throw Error('tool calls/results changed');
const settings=SettingsManager.create(cwd,agentDir,{projectTrusted:true});
const loader=new DefaultResourceLoader({cwd,agentDir,settingsManager:settings,additionalExtensionPaths:[historyExtension,outputExtension],noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true});
await loader.reload();
if(loader.getExtensions().errors.length) throw Error(JSON.stringify(loader.getExtensions().errors));
const manager=SessionManager.open(afterFile);
const {session}=await createAgentSession({cwd,agentDir,settingsManager:settings,resourceLoader:loader,sessionManager:manager,noTools:'builtin'});
await session.bindExtensions({});
const tools=session._getCallableTools();
const history=tools.find(tool=>tool.name==='swarm_read_history');
const output=tools.find(tool=>tool.name==='swarm_read_output');
if(!history||!output) throw Error('native archive readers were not registered');
const h=await history.execute('history-read',{archive_id:historyId,format:'text',offset:0,limit:333});
const o=await output.execute('output-read',{path:spillPath,offset:0,limit:333});
if(h.details.archive_id!==historyId||!h.content[0].text.includes('# Archived model context')) throw Error('history reader failed');
if(o.details.path!==spillPath||!o.content[0].text.includes('NATIVE SPILL PAYLOAD')) throw Error('output reader failed');
console.log(JSON.stringify({messages:after.messages.length,history:h.details,output:o.details}));
"""
    transcript = (
        pi_root().parent
        / "node_modules"
        / "@earendil-works"
        / "pi-ai"
        / "dist"
        / "utils"
        / "transcript.js"
    )
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            pi_root().as_uri(),
            transcript.as_uri(),
            str(workspace),
            str(config),
            str(comparison),
            str(restored_source),
            agent_path(HISTORY_READER),
            agent_path(OUTPUT_READER),
            history_report["archive_id"],
            str(restored_spill),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    native = json.loads(result.stdout)
    assert native["messages"] > 0
    assert native["history"]["next_offset"] > 0
    assert native["output"]["next_offset"] > 0
