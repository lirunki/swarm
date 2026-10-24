"""Verify the installed Pi projection and Copilot wire formatter, without model requests."""

import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from swarm.importers.pi_history import PiHistory
from swarm.tool_outputs import PREVIEW_BYTES, prepare_repair


@pytest.mark.skipif(
    not shutil.which("pi") or not shutil.which("node"), reason="Installed Pi/Node optional"
)
@pytest.mark.parametrize("images", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_native_pi_projection_bounds_the_actual_function_call_output_string(
    tmp_path, images, pending
):
    history = PiHistory(str(uuid4()), str(tmp_path), "Provider wire repair test")
    original_input = {"path": "result.log", "data": "Do not shorten this input. " * 1000}
    history.add(
        "message",
        message={
            "role": "assistant",
            "api": "openai-responses",
            "provider": "github-copilot",
            "model": "test-model",
            "content": [
                {
                    "type": "toolCall",
                    "id": "call_big|fc_big",
                    "name": "fetch_log",
                    "arguments": original_input,
                }
            ],
            "stopReason": "toolUse",
            "timestamp": 1,
            "usage": {},
        },
    )
    content = [{"type": "text", "text": "BEGIN\n" + "x" * 11_000_000 + "\nEND"}]
    if images:
        content.append({"type": "image", "data": "YWJj", "mimeType": "image/png"})
    history.add(
        "message",
        message={
            "role": "toolResult",
            "toolName": "fetch_log",
            "toolCallId": "call_big|fc_big",
            "content": content,
            "isError": True,
            "timestamp": 2,
        },
    )
    if pending:
        history.add(
            "message",
            message={
                "role": "assistant",
                "api": "openai-responses",
                "provider": "github-copilot",
                "model": "test-model",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call_pending|fc_pending",
                        "name": "fetch_log",
                        "arguments": {"pending": True},
                    }
                ],
                "stopReason": "toolUse",
                "timestamp": 3,
                "usage": {},
            },
        )
    source = history.encode().encode()
    replacement, outputs, stats = prepare_repair(source, tmp_path, uuid4().hex)
    assert stats["outputs_repaired"] == 1 and replacement.startswith(source)
    path = tmp_path / "repaired.jsonl"
    path.write_bytes(replacement)
    original_path = tmp_path / "original.jsonl"
    original_path.write_bytes(source)
    root = Path(shutil.which("pi")).resolve().parent.parent
    provider = (
        root.parent
        / "node_modules"
        / "@earendil-works"
        / "pi-ai"
        / "dist"
        / "api"
        / "openai-responses-shared.js"
    )
    script = """
import fs from 'node:fs';
const {buildSessionContext}=await import(process.argv[1]+'/core/session-manager.js');
const {convertToLlm}=await import(process.argv[1]+'/core/messages.js');
const {convertResponsesMessages}=await import(process.argv[2]);
const records=fs.readFileSync(process.argv[3],'utf8').trim().split('\\n').map(JSON.parse);
const projected=buildSessionContext(records.slice(1));
const original=fs.readFileSync(process.argv[6],'utf8').trim().split('\\n').map(JSON.parse);
const baseline=buildSessionContext(original.slice(1));
if(JSON.stringify(projected.messages.map(m=>[m.role,m.toolCallId]))!==JSON.stringify(baseline.messages.map(m=>[m.role,m.toolCallId])))throw Error('Repair inserted or reordered model messages');
const messages=convertToLlm(projected.messages);
const model={id:'test-model',name:'test',api:'openai-responses',provider:'github-copilot',
 input:process.argv[4]==='true'?['text','image']:['text'],contextWindow:1000000,maxTokens:8000};
const result=convertResponsesMessages(model,{messages},new Set(['github-copilot']));
const outputs=result.filter(item=>item.type==='function_call_output');
const output=outputs.find(item=>item.call_id==='call_big');
if(!output)throw Error('Expected original paired output');
const text=typeof output.output==='string'?output.output:output.output.filter(b=>b.type==='input_text').map(b=>b.text).join('\\n');
if(Buffer.byteLength(text)>Number(process.argv[5]))throw Error('Output is still oversized');
if(!text.includes('toolout_')||!text.includes('BEGIN')||!text.includes('END'))throw Error('Missing preview/reference');
if(output.call_id!=='call_big')throw Error('Call/result pairing changed');
const call=result.find(item=>item.type==='function_call');
if(JSON.parse(call.arguments).data!=='Do not shorten this input. '.repeat(1000))throw Error('Input was modified');
if(process.argv[4]==='true'&&!output.output.some(b=>b.type==='input_image'&&b.image_url==='data:image/png;base64,YWJj'))throw Error('Image changed');
console.log(JSON.stringify({output_bytes:Buffer.byteLength(text),call_id:output.call_id}));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            root.as_uri(),
            provider.as_uri(),
            str(path),
            str(images).lower(),
            str(PREVIEW_BYTES),
            str(original_path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["output_bytes"] <= PREVIEW_BYTES
