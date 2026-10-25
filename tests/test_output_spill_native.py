"""Run an actual Pi turn with an in-memory fake provider: no HTTP/model-service calls."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from swarm.importers.pi_history import PiHistory
from swarm.paths import agent_path

EXTENSION = Path(__file__).resolve().parents[1] / "src" / "swarm" / "extensions" / "output-spill.js"


@pytest.mark.skipif(not shutil.which("pi"), reason="Installed Pi optional")
@pytest.mark.parametrize("managed", [False, True])
def test_native_pi_spills_before_model_followup_and_session_persistence(tmp_path, managed):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"enableAnalytics": False, "enableInstallTelemetry": False})
    )
    config = tmp_path / "output-spill.json"
    config.write_text(json.dumps({"maxBytes": 4096, "previewBytes": 2048}))
    observed = tmp_path / "observed.json"
    fixture = tmp_path / "provider.js"
    fixture.write_text(r"""const fs = require('node:fs');
const {Type} = require('typebox');
const {createAssistantMessageEventStream} = require('@earendil-works/pi-ai');
module.exports = function(pi) {
 pi.registerTool({name:'spill_fixture',label:'Spill Fixture',description:'Test only',parameters:Type.Object({}),
  async execute() {return {content:[{type:'text',text:'HEAD\n'+'x'.repeat(23318071)+'\nTAIL'}],
   details:{sentinel:'preserve-renderer-state'},isError:true};}
 });
 pi.registerProvider('swarm-output-fixture',{api:'swarm-output-fixture',apiKey:'not-a-real-key',baseUrl:'https://unused.invalid',
  models:[{id:'fixture',name:'Fixture',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:1000000,maxTokens:1024}],
  streamSimple(model,context,options) {
   const stream=createAssistantMessageEventStream();
   queueMicrotask(()=> {
    const result=context.messages.find(m=>m.role==='toolResult'&&m.toolName==='spill_fixture');
    const message={role:'assistant',api:model.api,provider:model.provider,model:model.id,content:[],
     usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},
     timestamp:Date.now(),stopReason:result?'stop':'toolUse'};
    stream.push({type:'start',partial:message});
    if(!result) {
     const call={type:'toolCall',id:'spill-fixture-call',name:'spill_fixture',arguments:{}};
     message.content.push(call);
     stream.push({type:'toolcall_start',contentIndex:0,partial:message});
     stream.push({type:'toolcall_delta',contentIndex:0,delta:'{}',partial:message});
     stream.push({type:'toolcall_end',contentIndex:0,toolCall:call,partial:message});
    } else {
     const text=result.content.filter(b=>b.type==='text').map(b=>b.text).join('\n');
     fs.writeFileSync(process.env.SPILL_FIXTURE_OBSERVED,JSON.stringify({bytes:Buffer.byteLength(text),text,
       isError:result.isError,toolCallId:result.toolCallId,details:result.details}));
     message.content.push({type:'text',text:'Fixture done.'});
     stream.push({type:'text_start',contentIndex:0,partial:message});
     stream.push({type:'text_delta',contentIndex:0,delta:'Fixture done.',partial:message});
     stream.push({type:'text_end',contentIndex:0,content:'Fixture done.',partial:message});
    }
    stream.push({type:'done',reason:message.stopReason,message});stream.end();
   });
   return stream;
  }
 });
};
""")
    session = tmp_path / "pi-sessions" / "project" / "session.jsonl"
    session.parent.mkdir(parents=True)
    archives = session.parent / "output-spills"
    id = str(uuid4())
    session.write_text(PiHistory(id, agent_path(tmp_path), "Spill fixture").encode())
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("SWARM_", "PI_SESSION_", "PI_OUTPUT_SPILL_", "AZURE_", "AWS_", "GOOGLE_")
        )
        and not k.endswith("_API_KEY")
        and k not in {"GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_AUTH_TOKEN", "OPENAI_AUTH_TOKEN"}
    }
    env.update(
        PI_CODING_AGENT_DIR=agent_path(config_dir),
        PI_OUTPUT_SPILL_CONFIG=agent_path(config),
        SPILL_FIXTURE_OBSERVED=agent_path(observed),
    )
    if managed:
        agent_id = str(uuid4())
        env.update(
            SWARM_AGENT_ID=agent_id,
            SWARM_CONTROL_DIR=agent_path(tmp_path / "swarm" / "control" / agent_id),
        )
    args = [
        shutil.which("pi"),
        "--mode",
        "json",
        "--session",
        agent_path(session),
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--extension",
        agent_path(EXTENSION),
        "--extension",
        agent_path(fixture),
        "--provider",
        "swarm-output-fixture",
        "--model",
        "fixture",
        "Run the fixture only.",
    ]
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-4000:]
    assert observed.exists(), result.stdout[-4000:] + result.stderr[-4000:]
    actual = json.loads(observed.read_text())
    assert actual["bytes"] <= 4096 and "Output file:" in actual["text"]
    assert actual["isError"] and actual["toolCallId"] == "spill-fixture-call"
    assert actual["details"] == {"sentinel": "preserve-renderer-state"}
    entries = [json.loads(line) for line in session.read_text().splitlines()]
    outputs = [
        e["message"]
        for e in entries
        if e.get("type") == "message" and e.get("message", {}).get("role") == "toolResult"
    ]
    assert len(outputs) == 1 and "Output file:" in outputs[0]["content"][0]["text"]
    assert session.stat().st_size < 200000
    reference = next(
        e["data"] for e in entries if e.get("customType") == "swarm.output-spill.archive"
    )
    from swarm.paths import local_path

    expected_dir = archives / f"session-{hashlib.sha256(id.encode()).hexdigest()[:16]}"
    for field in ("file", "jsonFile", "metadataFile"):
        assert local_path(reference[field]).parent == expected_dir
    assert not (config_dir / "output-spills").exists()
    assert not (tmp_path / "swarm" / "output-spills").exists()
    full = local_path(reference["file"]).read_text()
    assert (
        len(full) == 23318071 + len("HEAD\n\nTAIL")
        and full.startswith("HEAD\n")
        and full.endswith("\nTAIL")
    )
    raw = json.loads(local_path(reference["jsonFile"]).read_text())
    assert raw["content"][0]["text"] == full and raw["toolCallId"] == "spill-fixture-call"
