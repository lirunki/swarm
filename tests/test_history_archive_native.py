"""Exercise history archives against the installed Pi projection and SDK, without model calls."""

import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from swarm.adapters import MAILBOX_EXTENSION
from swarm.history_archive import HistoryArchiver, HistoryArchiveRequest, prepare
from swarm.importers.pi_history import PiHistory
from swarm.models import Agent
from swarm.paths import agent_path
from swarm.store import Store

NATIVE = pytest.mark.skipif(
    not shutil.which("pi") or not shutil.which("node"), reason="Installed Pi/Node optional"
)


def add_message(history, role, content=None, **fields):
    message = {"role": role, "timestamp": history.milliseconds, **fields}
    if content is not None:
        message["content"] = content
    history.add("message", message=message)
    return history.parent


def records(data):
    if isinstance(data, bytes):
        data = data.decode()
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def native_fixture(tmp_path, checkpoint, tool_stop_reason="toolUse"):
    """Build a branch containing edits, system deltas, rich blocks, and a tool pair."""
    history = PiHistory(str(uuid4()), str(tmp_path), "Native archive")
    history.add("model_change", provider="sentinel-provider", modelId="sentinel-model")
    history.add("thinking_level_change", thinkingLevel="high")
    read_v1 = {"name": "read", "description": "old", "parameters": {"type": "object"}}
    write = {"name": "write", "description": "write", "parameters": {"type": "object"}}
    add_message(
        history,
        "system",
        "BASE SYSTEM",
        sections={"keep": "base", "delete": "old"},
        toolsAdded=[read_v1, write],
    )
    history.user("OLD-ORIGINAL " + "a" * 10_000)
    edited_old = history.parent
    history.add(
        "context_edit",
        targetId=edited_old,
        replacement={"content": "OLD-EDITED " + "e" * 10_000},
    )
    add_message(
        history,
        "assistant",
        [
            {"type": "thinking", "thinking": "ARCHIVED-THINKING", "thinkingSignature": "opaque"},
            {"type": "image", "mimeType": "image/png", "data": "YWJj"},
            {"type": "text", "text": "OLD-ANSWER " + "b" * 3000},
        ],
        api="sentinel-api",
        provider="sentinel-provider",
        model="sentinel-model",
        stopReason="stop",
        usage={"input": 7, "output": 9, "totalTokens": 16, "cost": {"total": 0}},
        sentinel={"metadata": "unchanged"},
    )
    live_parent = history.parent
    history.user("ABANDONED-BRANCH " + "x" * 6000)
    history.parent = live_parent

    if checkpoint:
        history.add(
            "compaction",
            summary="OLDER ARCHIVE REFERENCE " + "s" * 9000,
            firstKeptEntryId=edited_old,
            tokensBefore=4000,
            systemMessage={
                "role": "system",
                "content": "CHECKPOINT SYSTEM",
                "sections": {"keep": "checkpoint", "delete": "still-here"},
                "toolsAdded": [read_v1, write],
                "timestamp": 1,
            },
        )
    else:
        # A checkpoint with no systemMessage must not resurrect BASE SYSTEM.
        history.add(
            "compaction",
            summary="OLDER ARCHIVE REFERENCE " + "s" * 9000,
            firstKeptEntryId=edited_old,
            tokensBefore=4000,
        )
    read_v2 = {"name": "read", "description": "new", "parameters": {"type": "object"}}
    add_message(
        history,
        "system",
        "POST SYSTEM",
        sections={"delete": None, "post": "yes"},
        toolsRemoved=[{"name": "write"}],
        toolsAdded=[read_v2],
    )
    history.user("TOOL REQUEST " + "t" * 10_000)
    call_id = "native-call"
    add_message(
        history,
        "assistant",
        [{"type": "toolCall", "id": call_id, "name": "read", "arguments": {"path": "exact"}}],
        api="sentinel-api",
        provider="sentinel-provider",
        model="sentinel-model",
        stopReason=tool_stop_reason,
        usage={"totalTokens": 3},
    )
    add_message(
        history,
        "toolResult",
        [{"type": "text", "text": "TOOL RESULT " + "r" * 5000}],
        toolCallId=call_id,
        toolName="read",
        isError=True,
        details={"preserve": [1, 2, 3]},
    )
    history.user("EDIT-ME " + "c" * 10_000)
    edited_kept = history.parent
    history.add(
        "context_edit",
        targetId=edited_kept,
        replacement={"content": "EDITED-KEPT " + "d" * 10_000},
    )
    history.user("NEWEST " + "n" * 2000)
    add_message(
        history,
        "assistant",
        [{"type": "text", "text": "NEWEST ANSWER " + "z" * 2000}],
        api="sentinel-api",
        provider="sentinel-provider",
        model="sentinel-model",
        stopReason="stop",
        usage={"totalTokens": 11},
    )
    return history, edited_old, edited_kept


def pi_root():
    return Path(shutil.which("pi")).resolve().parent.parent


@NATIVE
@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("tool_stop_reason", ["toolUse", "stop", "length", "error", "aborted"])
def test_installed_pi_projection_and_wire_preserve_native_context_contract(
    tmp_path, checkpoint, tool_stop_reason
):
    history, edited_old, edited_kept = native_fixture(tmp_path, checkpoint, tool_stop_reason)
    original = history.encode().encode()
    archive_id = uuid4().hex
    directory = tmp_path / "context-archives" / "session" / archive_id
    replacement, payloads, stats = prepare(original, directory, 50, archive_id)
    assert replacement.startswith(original)
    assert payloads["original.jsonl"] == original
    assert b"ABANDONED-BRANCH" in original and b"ABANDONED-BRANCH" not in payloads["history.jsonl"]

    original_path = tmp_path / "original.jsonl"
    replacement_path = tmp_path / "replacement.jsonl"
    archive_path = tmp_path / "history.jsonl"
    original_path.write_bytes(original)
    replacement_path.write_bytes(replacement)
    archive_path.write_bytes(payloads["history.jsonl"])
    script = r"""
import fs from 'node:fs';
const {buildSessionContext,buildSessionProjection}=await import(process.argv[1]+'/core/session-manager.js');
const {convertToLlm,COMPACTION_SUMMARY_PREFIX,COMPACTION_SUMMARY_SUFFIX}=await import(process.argv[1]+'/core/messages.js');
const {getCurrentSystemMessage}=await import(process.argv[2]);
const load=p=>fs.readFileSync(p,'utf8').trim().split('\n').map(JSON.parse).slice(1);
const originalEntries=load(process.argv[3]),replacementEntries=load(process.argv[4]);
const before=buildSessionProjection(originalEntries);
const after=buildSessionProjection(replacementEntries);
if(JSON.stringify(buildSessionContext(originalEntries).messages)!==JSON.stringify(before.messages)||JSON.stringify(buildSessionContext(replacementEntries).messages)!==JSON.stringify(after.messages)) throw Error('native context/projection disagreement');
const archived=load(process.argv[5]);
const checkpoint=process.argv[6]==='true';
const archiveIds=new Set(archived.map(e=>e.id));
if(after.model?.provider!=='sentinel-provider'||after.model?.modelId!=='sentinel-model'||after.thinkingLevel!=='high') throw Error('model/thinking selection changed');
const projectedIds=after.entries.slice(1).map(e=>e.sourceEntry.id);
if(projectedIds.some(id=>archiveIds.has(id))) throw Error('archived source survived in model context');
if(after.entries[0].sourceEntry.type!=='compaction') throw Error('new checkpoint is not projection head');
const head=after.entries[0].messages;
if(head.at(-1)?.role!=='compactionSummary'||!head.at(-1).summary.includes('no LLM summary')||!head.at(-1).summary.includes(process.argv[7])) throw Error('archive notice is not the native summary');
const wire=convertToLlm(after.messages);
const expected=COMPACTION_SUMMARY_PREFIX+head.at(-1).summary+COMPACTION_SUMMARY_SUFFIX;
if(wire.filter(m=>m.role==='user'&&m.content?.[0]?.text===expected).length!==1) throw Error('summary wire shape changed');
const system=getCurrentSystemMessage(after.messages);
if(!system||system.content!==(checkpoint?'CHECKPOINT SYSTEM\n\nPOST SYSTEM':'POST SYSTEM')) throw Error('system replay mismatch: '+JSON.stringify(system));
if(JSON.stringify(system.sections)!==JSON.stringify(checkpoint?{keep:'checkpoint',post:'yes'}:{post:'yes'})) throw Error('system sections mismatch');
if(system.toolsAdded.length!==1||system.toolsAdded[0].name!=='read'||system.toolsAdded[0].description!=='new') throw Error('system tools mismatch');
const beforeById=new Map(before.entries.map(e=>[e.sourceEntry.id,e.messages]));
for(const item of after.entries.slice(1)) {
 if(item.messages.length===0) continue; // carried context_edit entries are new provenance only
 const old=beforeById.get(item.sourceEntry.id);
 if(!old||JSON.stringify(old)!==JSON.stringify(item.messages)) throw Error('retained message or metadata changed: '+item.sourceEntry.id);
}
const calls=[]; const results=[];
for(const message of after.messages) {
 if(message.role==='assistant') for(const block of message.content) if(block.type==='toolCall') calls.push(block.id);
 if(message.role==='toolResult') results.push(message.toolCallId);
}
if(JSON.stringify(calls)!==JSON.stringify(results)||calls.includes(undefined)) throw Error('orphaned native tool call/result');
const retained=after.entries.find(e=>e.sourceEntry.id===process.argv[8]);
if(!retained?.messages[0]?.content.startsWith('EDITED-KEPT')) throw Error('retained context edit was not carried');
if(after.entries.some(e=>e.sourceEntry.id===process.argv[9])) throw Error('archived edited source remained live');
console.log(JSON.stringify({wireRoles:wire.map(m=>m.role),projectedIds}));
"""
    ai_transcript = (
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
            ai_transcript.as_uri(),
            str(original_path),
            str(replacement_path),
            str(archive_path),
            str(checkpoint).lower(),
            archive_id,
            edited_kept,
            edited_old,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    native = json.loads(result.stdout)
    assert native["wireRoles"][0] == "system"
    assert stats["first_kept_entry_id"] in native["projectedIds"]
    archived = records(payloads["history.jsonl"])
    rich = next((entry for entry in archived if entry.get("message", {}).get("sentinel")), None)
    assert rich is not None
    assert rich["message"]["sentinel"] == {"metadata": "unchanged"}
    assert rich["message"]["content"][0]["thinking"] == "ARCHIVED-THINKING"
    assert rich["message"]["content"][1] == {
        "type": "image",
        "mimeType": "image/png",
        "data": "YWJj",
    }
    assert rich["message"]["usage"]["totalTokens"] == 16
    projected_old = next(entry for entry in archived if entry.get("id") == edited_old)
    assert projected_old["message"]["content"].startswith("OLD-EDITED")
    assert b"OLD-ORIGINAL" in payloads["original.jsonl"]


@NATIVE
@pytest.mark.parametrize("percent", [1, 50, 95])
def test_native_percentage_measurement_uses_dynamic_uneven_turns(tmp_path, percent):
    history = PiHistory(str(uuid4()), str(tmp_path), "Percentage matrix")
    for index, size in enumerate((10_000, 10_000, 10_000, 2000)):
        history.user(f"USER-{index} " + chr(65 + index) * size)
        add_message(
            history,
            "assistant",
            [{"type": "text", "text": f"ANSWER-{index} " + "q" * size}],
            api="fixture",
            provider="fixture",
            model="fixture",
            stopReason="stop",
            usage={"totalTokens": 999_999},
        )
    source = history.encode().encode()
    replacement, _, stats = prepare(source, tmp_path / uuid4().hex, percent)
    assert replacement.startswith(source)
    assert stats["percent_requested"] == percent
    assert 0 < stats["archive_bytes"] < stats["context_before_bytes"]
    assert stats["kept_entries"] >= 2
    # The value is selected from real message boundaries, not fabricated from the request.
    assert stats["actual_percent"] == pytest.approx(
        100 * stats["archive_bytes"] / stats["context_before_bytes"]
    )


class FakeProcesses:
    def alive(self, _agent):
        return False


class FakeGuard:
    def __init__(self, store):
        self.store = store
        self.controller = SimpleNamespace(
            store=store, processes=FakeProcesses(), lock=threading.RLock()
        )

    def eligible(self, agent, *, read_snapshot=True):
        return Path(agent.session_file).expanduser().absolute()

    def check_external(self, _agent):
        return None


def archive_setup(tmp_path):
    store = Store(tmp_path / "registry")
    history = PiHistory(str(uuid4()), str(tmp_path), "Managed native archive")
    for index in range(6):
        history.user(f"FULL USER {index} " + "u" * 7000)
        add_message(
            history,
            "assistant",
            [{"type": "text", "text": f"FULL ANSWER {index} " + "a" * 3000}],
            api="fixture",
            provider="fixture",
            model="fixture",
            stopReason="stop",
            usage={"totalTokens": 1},
        )
    session = store.sessions_dir / "native-history.jsonl"
    session.write_text(history.encode())
    agent = store.create(
        Agent(
            name="Native archive",
            goal="No model calls",
            folder=str(tmp_path),
            managed=True,
            auto_resume=False,
            session_id=history.entries[0]["id"],
            session_file=str(session),
        )
    )
    return HistoryArchiver(FakeGuard(store)), store, agent, session


@NATIVE
def test_full_archiver_repeats_once_then_sdk_reader_registers_and_executes(tmp_path):
    archiver, store, agent, session = archive_setup(tmp_path)
    original = session.read_bytes()
    request = HistoryArchiveRequest(percent=50)
    first = archiver.archive(str(agent.id), request)
    assert session.read_bytes().startswith(original)
    assert archiver.archive(str(agent.id), request)["archive_id"] == first["archive_id"]
    assert len(list(session.parent.glob("context-archives/*/*/manifest.json"))) == 1

    current = records(session.read_bytes())
    parent = current[-1]["id"]
    for index in range(2):
        user_id = uuid4().hex[:8]
        current.append(
            {
                "type": "message",
                "id": user_id,
                "parentId": parent,
                "timestamp": "2025-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": f"APPENDED FULL USER {index} " + "v" * 9000,
                    "timestamp": 1,
                },
            }
        )
        answer_id = uuid4().hex[:8]
        current.append(
            {
                "type": "message",
                "id": answer_id,
                "parentId": user_id,
                "timestamp": "2025-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "APPENDED ANSWER " + "w" * 4000}],
                    "api": "fixture",
                    "provider": "fixture",
                    "model": "fixture",
                    "stopReason": "stop",
                    "usage": {"totalTokens": 1},
                    "timestamp": 1,
                },
            }
        )
        parent = answer_id
    session.write_text("".join(json.dumps(item) + "\n" for item in current))
    second = archiver.archive(str(agent.id), HistoryArchiveRequest(percent=50))
    assert second["archive_id"] != first["archive_id"]
    second_manifest = json.loads(Path(second["manifest"]).read_text())
    assert first["archive_id"] in second_manifest["previous_archives"]
    assert first["archive_id"] in Path(second["history_file"]).read_text()

    config = tmp_path / "pi-config"
    config.mkdir()
    (config / "settings.json").write_text(
        json.dumps({"enableAnalytics": False, "enableInstallTelemetry": False})
    )
    # Use Pi's real SDK loader/session binding. Calling the registered read-only tool directly
    # proves registration and archive access without entering an agent turn or provider stream.
    script = r"""
const root=process.argv[1];
const {DefaultResourceLoader,SettingsManager,SessionManager,createAgentSession}=await import(root+'/index.js');
const cwd=process.argv[2],agentDir=process.argv[3],extension=process.argv[4],sessionFile=process.argv[5],archiveId=process.argv[6];
const settings=SettingsManager.create(cwd,agentDir,{projectTrusted:true});
const loader=new DefaultResourceLoader({cwd,agentDir,settingsManager:settings,additionalExtensionPaths:[extension],noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true});
await loader.reload();
if(loader.getExtensions().errors.length) throw Error(JSON.stringify(loader.getExtensions().errors));
const manager=SessionManager.open(sessionFile);
const {session}=await createAgentSession({cwd,agentDir,settingsManager:settings,resourceLoader:loader,sessionManager:manager,noTools:'builtin'});
const names=session.getActiveToolNames();
if(!names.includes('swarm_read_history')) throw Error('history reader is not active: '+names.join(','));
const tool=session._getCallableTools().find(item=>item.name==='swarm_read_history');
if(!tool) throw Error('history reader is not callable');
const result=await tool.execute('native-history-read',{archive_id:archiveId,format:'text',offset:0,limit:211});
if(result.details.archive_id!==archiveId||result.details.next_offset<=0||Buffer.byteLength(result.content[0].text)>1000) throw Error('bad bounded tool result');
console.log(JSON.stringify({names,details:result.details,text:result.content[0].text}));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            pi_root().as_uri(),
            str(tmp_path),
            str(config),
            agent_path(MAILBOX_EXTENSION),
            str(session),
            second["archive_id"],
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    output = json.loads(result.stdout)
    assert "swarm_read_history" in output["names"]
    assert output["details"]["offset"] == 0
    assert output["text"].startswith("[History archive page:")
    assert "# Archived model context" in output["text"]
    assert store.get(agent.id).context_archive["status"] == "completed"
