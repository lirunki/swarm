"""Exercise decision through the installed Pi SDK with an in-memory chat provider."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[1] / "src" / "swarm" / "extensions" / "decision.js"
NATIVE = pytest.mark.skipif(
    not shutil.which("pi") or not shutil.which("node"), reason="Installed Pi/Node optional"
)


def pi_root():
    return Path(shutil.which("pi")).resolve().parent.parent


def isolated_env(config):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SWARM_", "PI_SESSION_", "PI_DECISION_", "AZURE_", "AWS_", "GOOGLE_"))
        and not key.endswith("_API_KEY")
        and key
        not in {"GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_AUTH_TOKEN", "OPENAI_AUTH_TOKEN"}
    }
    env.update(PI_DECISION_CONFIG=str(config), PI_OFFLINE="1")
    return env


def run_native(script, tmp_path, config, timeout=45):
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            pi_root().as_uri(),
            str(tmp_path),
            str(EXTENSION),
        ],
        cwd=tmp_path,
        env=isolated_env(config),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr[-6000:]
    return json.loads(result.stdout)


@NATIVE
def test_native_package_discovery_and_managed_mailbox_load_register_once(tmp_path):
    config = tmp_path / "decision.json"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import Module from 'node:module';
import {fileURLToPath} from 'node:url';
const root=process.argv[1],cwd=process.argv[2],extensionPath=process.argv[3];
process.env.NODE_PATH=path.resolve(fileURLToPath(root),'../node_modules');
Module._initPaths();
const packageDir=path.join(path.dirname(path.dirname(extensionPath)),'pi-decision');
const packageEntry=path.join(packageDir,'index.js');
const mailboxPath=path.join(path.dirname(extensionPath),'mailbox.js');
const skillFile=path.join(path.dirname(path.dirname(extensionPath)),'skills','decision','SKILL.md');
const {DefaultResourceLoader,SettingsManager}=await import(root+'/index.js');
globalThis.fetch=()=>{throw new Error('network access is forbidden in the native decision fixture');};

const isolatedSettings=values=>SettingsManager.inMemory({enableAnalytics:false,enableInstallTelemetry:false,...values},{projectTrusted:true});
const loaderOptions={cwd,agentDir:cwd+'/agent',noPromptTemplates:true,noThemes:true,noContextFiles:true};
const registrations=extensions=>({
 tools:extensions.flatMap(extension=>[...extension.tools.keys()]).filter(name=>name==='decision').length,
 commands:extensions.flatMap(extension=>[...extension.commands.keys()]).filter(name=>name==='decision').length,
});

process.env.SWARM_CONTROL_DIR=cwd+'/control-must-not-be-created';
process.env.SWARM_AGENT_ID='00000000-0000-4000-8000-000000000001';
const managedLoader=new DefaultResourceLoader({...loaderOptions,settingsManager:isolatedSettings({}),
 additionalExtensionPaths:[packageEntry,mailboxPath],noExtensions:true,noSkills:true});
await managedLoader.reload();
const managed=managedLoader.getExtensions();
assert.deepEqual(managed.errors,[]);
assert.deepEqual(managed.warnings,[]);
assert.deepEqual(managed.extensions.map(item=>item.resolvedPath),[packageEntry,mailboxPath]);
assert.deepEqual(registrations(managed.extensions),{tools:1,commands:1});
assert.equal(fs.existsSync(process.env.SWARM_CONTROL_DIR),false,'managed discovery wrote Swarm control state');

delete process.env.SWARM_CONTROL_DIR;
delete process.env.SWARM_AGENT_ID;
const standaloneLoader=new DefaultResourceLoader({...loaderOptions,
 settingsManager:isolatedSettings({packages:[packageDir]})});
await standaloneLoader.reload();
const standalone=standaloneLoader.getExtensions();
assert.deepEqual(standalone.errors,[]);
assert.deepEqual(standalone.warnings,[]);
assert.equal(standalone.extensions.length,1);
assert.equal(standalone.extensions[0].resolvedPath,packageEntry);
assert.deepEqual(registrations(standalone.extensions),{tools:1,commands:1});
const skills=standaloneLoader.getSkills();
assert.deepEqual(skills.diagnostics,[]);
assert.equal(skills.skills.length,1);
assert.equal(skills.skills[0].name,'decision');
assert.equal(skills.skills[0].filePath,skillFile);
assert.match(skills.skills[0].description,/exactly one configured model/);
assert.match(fs.readFileSync(skills.skills[0].filePath,'utf8'),/^---\s*\nname: decision\s*$/m);
assert.equal(fs.existsSync(process.env.PI_DECISION_CONFIG),false,'resource discovery wrote decision config');
console.log(JSON.stringify({managed:registrations(managed.extensions),standalone:registrations(standalone.extensions),skill:skills.skills[0].name}));
"""
    output = run_native(script, tmp_path, config)
    assert output == {
        "managed": {"tools": 1, "commands": 1},
        "standalone": {"tools": 1, "commands": 1},
        "skill": "decision",
    }


@NATIVE
def test_native_sdk_loads_configures_and_runs_decision_without_network(tmp_path):
    config = tmp_path / "private" / "decision.json"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
const root=process.argv[1],cwd=process.argv[2],extensionPath=process.argv[3];
const {DefaultResourceLoader,ModelRuntime,SessionManager,SettingsManager,createAgentSession}=await import(root+'/index.js');
const {createAssistantMessageEventStream}=await import(root+'/../node_modules/@earendil-works/pi-ai/dist/index.js');
const {Check}=await import(root+'/../node_modules/typebox/build/value/index.mjs');
globalThis.fetch=()=>{throw new Error('network access is forbidden in the native decision fixture');};

const usage={input:7,output:4,cacheRead:0,cacheWrite:0,totalTokens:11,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const calls=[];
function message(model,content,stopReason='stop',messageUsage=usage) {
 return {role:'assistant',api:model.api,provider:model.provider,model:model.id,content,usage:messageUsage,timestamp:Date.now(),stopReason};
}
function completedStream(model,content,stopReason='stop',messageUsage=usage,beforeResponse) {
 const stream=createAssistantMessageEventStream();
 queueMicrotask(async()=>{
  await beforeResponse?.();
  const final=message(model,content,stopReason,messageUsage);
  stream.push({type:'start',partial:{...final,content:[]}});
  content.forEach((block,index)=>{
   if(block.type==='toolCall') {
    stream.push({type:'toolcall_start',contentIndex:index,partial:final});
    stream.push({type:'toolcall_delta',contentIndex:index,delta:JSON.stringify(block.arguments),partial:final});
    stream.push({type:'toolcall_end',contentIndex:index,toolCall:block,partial:final});
   } else {
    stream.push({type:'text_start',contentIndex:index,partial:final});
    stream.push({type:'text_delta',contentIndex:index,delta:block.text,partial:final});
    stream.push({type:'text_end',contentIndex:index,content:block.text,partial:final});
   }
  });
  stream.push({type:'done',reason:stopReason,message:final}); stream.end();
 });
 return stream;
}
function streamSimple(model,context,options) {
 calls.push({model:model.id,context,options:{maxTokens:options?.maxTokens,reasoning:options?.reasoning,maxRetries:options?.maxRetries}});
 if(model.id==='decider') {
  const answers={
   route:{type:'choice',choice:'safe',probabilities:{safe:0.75,fast:0.25},confidence:0.8},
   quality:{type:'score',score:1.5,confidence:0.6},
   approve:{type:'noul',noul:0.9},
  };
  return completedStream(model,[{type:'text',text:JSON.stringify({answers})}]);
 }
 const observePayload=()=>options?.onPayload?.({context},model);
 const prior=context.messages.find(item=>item.role==='toolResult'&&item.toolName==='decision');
 if(prior) return completedStream(model,[{type:'text',text:'Native fixture complete.'}],'stop',
  {...usage,input:2,output:1,totalTokens:3},observePayload);
 const arguments_={state_asset:{type:'context',last_n_turns:1,include_system:false},questions:{
  route:{type:'choice',instructions:'Choose a route',criteria:{safe:'Safer',fast:'Faster'}},
  quality:{type:'score',instructions:'Rate quality',criteria:['low','medium','high']},
  approve:{type:'noul',instructions:'Approve?'},
 }};
 return completedStream(model,[{type:'toolCall',id:'native-decision-call',name:'decision',arguments:arguments_}],'toolUse',
  {...usage,input:3,output:2,totalTokens:5},observePayload);
}

const authPath=cwd+'/isolated-auth.json';
const runtime=await ModelRuntime.create({authPath,modelsPath:null,refreshOnCreate:false,allowModelNetwork:false});
runtime.registerProvider('decision-fixture',{
 apiKey:'not-a-real-key',baseUrl:'https://unused.invalid',api:'decision-fixture',streamSimple,
 models:[
  {id:'main',name:'Fixture Main',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:100000,maxTokens:1024},
  {id:'decider',name:'Fixture Decider',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:100000,maxTokens:1024},
 ],
});
const settings=SettingsManager.inMemory({enableAnalytics:false,enableInstallTelemetry:false},{projectTrusted:true});
const loader=new DefaultResourceLoader({cwd,agentDir:cwd+'/agent',settingsManager:settings,
 additionalExtensionPaths:[extensionPath],noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true});
await loader.reload();
const loaded=loader.getExtensions();
assert.deepEqual(loaded.errors,[]);
assert.equal(loaded.extensions.length,1);
assert.equal(loaded.extensions[0].resolvedPath,extensionPath);
const main=runtime.getModel('decision-fixture','main');
const {session}=await createAgentSession({cwd,agentDir:cwd+'/agent',settingsManager:settings,resourceLoader:loader,
 sessionManager:SessionManager.inMemory(cwd),modelRuntime:runtime,model:main,noTools:'builtin'});
const notices=[];
await session.bindExtensions({mode:'print',uiContext:{notify:(text,level)=>notices.push({text,level})}});

const tools=session.getAllTools().filter(tool=>tool.name==='decision');
assert.equal(tools.length,1);
assert.equal(tools[0].parameters.additionalProperties,false);
assert.deepEqual(Object.keys(tools[0].parameters.properties).sort(),['assets','model','questions','state','state_asset']);
const definition=session.getToolDefinition('decision');
assert.deepEqual(Object.keys(definition.outputSchema.properties).sort(),['answers','metadata']);
assert.equal(definition.outputSchema.additionalProperties,false);
assert.equal(fs.existsSync(process.env.PI_DECISION_CONFIG),false);
await session.prompt('/decision status');
await session.prompt('/decision list');
await session.prompt('/decision models');
assert.equal(fs.existsSync(process.env.PI_DECISION_CONFIG),false,'read-only commands must not materialize defaults');
assert.match(notices.map(item=>item.text).join('\n'),/Decision model: luna/);
assert.match(notices.map(item=>item.text).join('\n'),/decision-fixture\/main/);
assert.match(notices.map(item=>item.text).join('\n'),/decision-fixture\/decider/);
assert.equal(calls.length,0,'commands must not invoke a model');

await session.prompt('/decision add fake decision-fixture/decider');
await session.prompt('/decision set fake');
assert.equal(session.model.provider,'decision-fixture');
assert.equal(session.model.id,'main','decision selection must not replace the main model');
const saved=JSON.parse(fs.readFileSync(process.env.PI_DECISION_CONFIG,'utf8'));
assert.equal(saved.selected,'fake');
assert.deepEqual(saved.models.fake,{adapter:'llm',provider:'decision-fixture',model:'decider',images:false,box:false});
await session.prompt('LATEST USER SENTINEL');
await session.waitForIdle();

assert.deepEqual(calls.map(call=>call.model),['main','decider','main']);
const decisionCalls=calls.filter(call=>call.model==='decider');
assert.equal(decisionCalls.length,1);
assert.deepEqual(decisionCalls[0].options,{maxTokens:4096,reasoning:'low',maxRetries:0});
const request=JSON.parse(decisionCalls[0].context.messages[1].content[0].text);
assert.deepEqual(request.state.conversation,[{role:'user',content:[{type:'text',text:'LATEST USER SENTINEL'}]}]);
assert.equal(JSON.stringify(request).includes('native-decision-call'),false,'current partial tool batch leaked into context');
const result=session.messages.find(item=>item.role==='toolResult'&&item.toolName==='decision');
assert.ok(result);
assert.equal(result.isError,false);
assert.deepEqual(result.usage,usage);
const structured=JSON.parse(result.content[0].text);
assert.equal(Check(definition.outputSchema,structured),true,'registered TypeBox output schema rejected native output');
assert.equal(structured.answers.route.choice,'safe');
assert.equal(structured.answers.quality.score,1.5);
assert.equal(structured.answers.approve.noul,0.9);
assert.equal(structured.metadata.probability_source,'llm_estimate');
assert.equal(structured.metadata.provider,'decision-fixture');
assert.equal(structured.metadata.model,'decider');
assert.match(structured.metadata.request_id,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
assert.match(structured.metadata.description_hash,/^[0-9a-f]{64}$/);
assert.equal(session.model.id,'main');
assert.equal(session.getLastAssistantText(),'Native fixture complete.');
assert.deepEqual(JSON.parse(fs.readFileSync(authPath,'utf8')),{},'isolated auth storage gained a credential');
session.dispose();
console.log(JSON.stringify({models:calls.map(call=>call.model),selected:saved.selected,answers:Object.keys(structured.answers)}));
"""
    output = run_native(script, tmp_path, config)
    assert output == {
        "models": ["main", "decider", "main"],
        "selected": "fake",
        "answers": ["route", "quality", "approve"],
    }


@NATIVE
def test_native_context_uses_privacy_transformed_final_payload_and_active_branch(tmp_path):
    config = tmp_path / "decision.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "selected": "fake",
                "models": {
                    "fake": {
                        "adapter": "llm",
                        "provider": "decision-fixture",
                        "model": "decider",
                        "images": False,
                        "box": False,
                    }
                },
                "limits": {
                    "max_input_bytes": 524288,
                    "max_output_bytes": 65536,
                    "timeout_ms": 60000,
                },
            }
        )
    )
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
const root=process.argv[1],cwd=process.argv[2],extensionPath=process.argv[3];
const {DefaultResourceLoader,ModelRuntime,SessionManager,SettingsManager,createAgentSession}=await import(root+'/index.js');
const {createAssistantMessageEventStream}=await import(root+'/../node_modules/@earendil-works/pi-ai/dist/index.js');
globalThis.fetch=()=>{throw new Error('network access is forbidden in the native decision privacy fixture');};

const privacyPath=cwd+'/privacy.js';
fs.writeFileSync(privacyPath,`"use strict";
const {Type}=require("typebox");
function scrub(value) {
 if (typeof value === "string") return value.replaceAll("RAW_SECRET", "REDACTED");
 if (Array.isArray(value)) return value.map(scrub);
 if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,scrub(item)]));
 return value;
}
module.exports=function privacy(pi) {
 pi.on("context_with_system",event=>({messages:scrub(event.messages)}));
 pi.registerTool({name:"privacy_sibling",label:"Privacy sibling",description:"Native fixture sibling",
  parameters:Type.Object({}, {additionalProperties:false}),
  async execute(){return {content:[{type:"text",text:"sibling complete"}],details:undefined};}});
};
module.exports.default=module.exports;\n`);

const usage={input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const calls=[],finalPayloads=[];
function completedStream(model,content,stopReason='stop',beforeResponse) {
 const stream=createAssistantMessageEventStream();
 queueMicrotask(async()=>{
  await beforeResponse?.();
  const final={role:'assistant',api:model.api,provider:model.provider,model:model.id,content,usage,timestamp:Date.now(),stopReason};
  stream.push({type:'start',partial:{...final,content:[]}});
  content.forEach((block,index)=>{
   if(block.type==='toolCall') {
    stream.push({type:'toolcall_start',contentIndex:index,partial:final});
    stream.push({type:'toolcall_delta',contentIndex:index,delta:JSON.stringify(block.arguments),partial:final});
    stream.push({type:'toolcall_end',contentIndex:index,toolCall:block,partial:final});
   } else {
    stream.push({type:'text_start',contentIndex:index,partial:final});
    stream.push({type:'text_delta',contentIndex:index,delta:block.text,partial:final});
    stream.push({type:'text_end',contentIndex:index,content:block.text,partial:final});
   }
  });
  stream.push({type:'done',reason:stopReason,message:final}); stream.end();
 });
 return stream;
}
function streamSimple(model,context,options) {
 calls.push({model:model.id,context});
 if(model.id==='decider') {
  return completedStream(model,[{type:'text',text:JSON.stringify({answers:{approved:{type:'noul',noul:0.8}}})}]);
 }
 const observePayload=async()=>{
  const initial={messages:context.messages};
  finalPayloads.push(await options.onPayload(initial,model) ?? initial);
 };
 if(context.messages.some(message=>message.role==='toolResult'&&message.toolName==='decision')) {
  return completedStream(model,[{type:'text',text:'Privacy fixture complete.'}],'stop',observePayload);
 }
 return completedStream(model,[
  {type:'toolCall',id:'CURRENT_DECISION_RAW_SECRET',name:'decision',arguments:{
   state_asset:{type:'context',last_n_turns:3,include_system:false},
   questions:{approved:{type:'noul',instructions:'Approve the redacted context?'}},
  }},
  {type:'toolCall',id:'CURRENT_SIBLING_RAW_SECRET',name:'privacy_sibling',arguments:{}},
 ],'toolUse',observePayload);
}

const runtime=await ModelRuntime.create({authPath:cwd+'/isolated-auth.json',modelsPath:null,refreshOnCreate:false,allowModelNetwork:false});
runtime.registerProvider('decision-fixture',{apiKey:'not-a-real-key',baseUrl:'https://unused.invalid',api:'decision-fixture',streamSimple,
 models:[
  {id:'main',name:'Fixture Main',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:100000,maxTokens:1024},
  {id:'decider',name:'Fixture Decider',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:100000,maxTokens:1024},
 ]});
const settings=SettingsManager.inMemory({enableAnalytics:false,enableInstallTelemetry:false},{projectTrusted:true});
const loader=new DefaultResourceLoader({cwd,agentDir:cwd+'/agent',settingsManager:settings,
 additionalExtensionPaths:[extensionPath,privacyPath],noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true});
await loader.reload();
assert.deepEqual(loader.getExtensions().errors,[]);
assert.equal(loader.getExtensions().extensions.length,2);

const manager=SessionManager.inMemory(cwd);
const assistant=(text,extra={})=>({role:'assistant',api:'decision-fixture',provider:'decision-fixture',model:'main',
 content:[{type:'text',text}],usage,timestamp:Date.now(),stopReason:'stop',...extra});
manager.appendMessage({role:'user',content:'TOO_OLD_ACTIVE_CONTEXT',timestamp:Date.now()});
manager.appendMessage(assistant('TOO_OLD_ACTIVE_ASSISTANT'));
manager.appendMessage({role:'user',content:'PRIOR_COMPLETE_USER',timestamp:Date.now()});
manager.appendMessage({role:'assistant',api:'decision-fixture',provider:'decision-fixture',model:'main',content:[
 {type:'text',text:'PRIOR_COMPLETE_ASSISTANT'},
 {type:'toolCall',id:'prior-complete-call',name:'historical_tool',arguments:{safe:'PRIOR_COMPLETE_ARGUMENT'}},
],usage,timestamp:Date.now(),stopReason:'toolUse',metadata:{secret:'PROJECTED_METADATA_SECRET'},cachedTools:['CACHED_TOOL_SECRET']});
const branchPoint=manager.appendMessage({role:'toolResult',toolCallId:'prior-complete-call',toolName:'historical_tool',
 content:[{type:'text',text:'PRIOR_COMPLETE_RESULT'}],isError:false,timestamp:Date.now()});
manager.appendMessage({role:'user',content:'ABANDONED_BRANCH_SECRET',timestamp:Date.now()});
manager.appendMessage(assistant('ABANDONED_BRANCH_ASSISTANT'));
manager.branch(branchPoint);
manager.appendMessage({role:'user',content:'ACTIVE_BRANCH_USER',timestamp:Date.now()});
manager.appendMessage(assistant('ACTIVE_BRANCH_ASSISTANT',{metadata:{hidden:'ACTIVE_METADATA_SECRET'},cachedTools:['ACTIVE_CACHED_TOOL_SECRET']}));

const {session}=await createAgentSession({cwd,agentDir:cwd+'/agent',settingsManager:settings,resourceLoader:loader,
 sessionManager:manager,modelRuntime:runtime,model:runtime.getModel('decision-fixture','main'),noTools:'builtin'});
await session.bindExtensions({mode:'print',uiContext:{notify:()=>{}}});
assert.equal(session.getAllTools().filter(tool=>tool.name==='decision').length,1);
await session.prompt('CURRENT RAW_SECRET USER');
await session.waitForIdle();

assert.deepEqual(calls.map(call=>call.model),['main','decider','main']);
assert.equal(finalPayloads.length,2);
assert.equal(JSON.stringify(finalPayloads[0]).includes('RAW_SECRET'),false,'privacy handler did not own final provider payload');
assert.equal(JSON.stringify(finalPayloads[0]).includes('REDACTED'),true);
const decisionCall=calls.find(call=>call.model==='decider');
const request=JSON.parse(decisionCall.context.messages[1].content[0].text);
const serialized=JSON.stringify(request.state.conversation);
assert.equal(serialized.includes('RAW_SECRET'),false);
assert.equal(serialized.includes('REDACTED'),true);
assert.equal(serialized.includes('TOO_OLD_ACTIVE_CONTEXT'),false,'last_n_turns was ignored');
assert.equal(serialized.includes('ABANDONED_BRANCH_SECRET'),false,'abandoned branch entered provider context');
assert.equal(serialized.includes('ABANDONED_BRANCH_ASSISTANT'),false,'abandoned assistant entered provider context');
assert.equal(serialized.includes('PROJECTED_METADATA_SECRET'),false);
assert.equal(serialized.includes('ACTIVE_METADATA_SECRET'),false);
assert.equal(serialized.includes('CACHED_TOOL_SECRET'),false);
assert.equal(serialized.includes('ACTIVE_CACHED_TOOL_SECRET'),false);
assert.equal(serialized.includes('CURRENT_DECISION_RAW_SECRET'),false,'current decision invocation leaked into its snapshot');
assert.equal(serialized.includes('CURRENT_SIBLING_RAW_SECRET'),false,'sibling current-batch call leaked into context');
assert.equal(serialized.includes('PRIOR_COMPLETE_USER'),true);
assert.equal(serialized.includes('PRIOR_COMPLETE_ASSISTANT'),true);
assert.equal(serialized.includes('prior-complete-call'),true,'a completed historical tool call was stripped');
assert.equal(serialized.includes('PRIOR_COMPLETE_RESULT'),true,'a completed historical tool result was stripped');
assert.equal(serialized.includes('ACTIVE_BRANCH_USER'),true);
assert.equal(serialized.includes('ACTIVE_BRANCH_ASSISTANT'),true);
const users=request.state.conversation.filter(message=>message.role==='user');
assert.equal(users.length,3,'last_n_turns did not retain exactly three active user turns');
assert.equal(session.getLastAssistantText(),'Privacy fixture complete.');
assert.deepEqual(JSON.parse(fs.readFileSync(cwd+'/isolated-auth.json','utf8')),{});
session.dispose();
console.log(JSON.stringify({models:calls.map(call=>call.model),users:users.length,redacted:serialized.includes('REDACTED')}));
"""
    output = run_native(script, tmp_path, config)
    assert output == {
        "models": ["main", "decider", "main"],
        "users": 3,
        "redacted": True,
    }


@NATIVE
def test_native_sdk_decision_errors_timeout_and_abort_are_bounded_and_sanitized(tmp_path):
    config = tmp_path / "decision.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "selected": "fake",
                "models": {
                    "fake": {
                        "adapter": "llm",
                        "provider": "decision-fixture",
                        "model": "decider",
                        "images": False,
                        "box": False,
                    }
                },
                "limits": {
                    "max_input_bytes": 524288,
                    "max_output_bytes": 65536,
                    "timeout_ms": 30,
                },
            }
        )
    )
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
const root=process.argv[1],cwd=process.argv[2],extensionPath=process.argv[3];
const {DefaultResourceLoader,ModelRuntime,SessionManager,SettingsManager,createAgentSession}=await import(root+'/index.js');
const {createAssistantMessageEventStream}=await import(root+'/../node_modules/@earendil-works/pi-ai/dist/index.js');
globalThis.fetch=()=>{throw new Error('network access is forbidden in the native decision fixture');};
let behavior='pending',calls=0;
const usage={input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
function streamSimple(model) {
 calls++;
 const stream=createAssistantMessageEventStream();
 if(behavior==='invalid') queueMicrotask(()=>{
  const message={role:'assistant',api:model.api,provider:model.provider,model:model.id,
   content:[{type:'text',text:'SECRET_PROVIDER_OUTPUT not json'}],usage,timestamp:Date.now(),stopReason:'stop'};
  stream.push({type:'start',partial:{...message,content:[]}});
  stream.push({type:'text_start',contentIndex:0,partial:message});
  stream.push({type:'text_delta',contentIndex:0,delta:message.content[0].text,partial:message});
  stream.push({type:'text_end',contentIndex:0,content:message.content[0].text,partial:message});
  stream.push({type:'done',reason:'stop',message}); stream.end();
 });
 return stream;
}
const authPath=cwd+'/isolated-auth.json';
const runtime=await ModelRuntime.create({authPath,modelsPath:null,refreshOnCreate:false,allowModelNetwork:false});
runtime.registerProvider('decision-fixture',{apiKey:'not-a-real-key',baseUrl:'https://unused.invalid',api:'decision-fixture',streamSimple,
 models:[{id:'decider',name:'Fixture Decider',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:100000,maxTokens:1024}]});
const settings=SettingsManager.inMemory({enableAnalytics:false,enableInstallTelemetry:false},{projectTrusted:true});
const loader=new DefaultResourceLoader({cwd,agentDir:cwd+'/agent',settingsManager:settings,additionalExtensionPaths:[extensionPath],
 noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true});
await loader.reload();
assert.deepEqual(loader.getExtensions().errors,[]);
const {session}=await createAgentSession({cwd,agentDir:cwd+'/agent',settingsManager:settings,resourceLoader:loader,
 sessionManager:SessionManager.inMemory(cwd),modelRuntime:runtime,model:runtime.getModel('decision-fixture','decider'),noTools:'builtin'});
const tool=session.state.tools.find(item=>item.name==='decision');
assert.ok(tool,'decision is not an executable native AgentTool');
const request={state:{secret:'SECRET_STATE'},questions:{approve:{type:'noul',instructions:'SECRET_QUESTION'}}};
function checkError(result,category) {
 assert.equal(result.isError,true);
 assert.equal(result.details.category,category);
 assert.equal('structuredContent' in result,false,'errors must not masquerade as structured output');
 assert.match(result.details.request_id,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
 const rendered=JSON.stringify(result);
 assert.equal(rendered.includes('SECRET_STATE'),false);
 assert.equal(rendered.includes('SECRET_QUESTION'),false);
 assert.equal(rendered.includes('SECRET_PROVIDER_OUTPUT'),false);
}
const timedOut=await tool.execute('native-timeout',request,undefined);
checkError(timedOut,'timeout');
assert.equal(calls,1,'timeout retried or doubled the decision call');

const controller=new AbortController();
const abortedPromise=tool.execute('native-abort',request,controller.signal);
setTimeout(()=>controller.abort(),5);
const aborted=await abortedPromise;
checkError(aborted,'aborted');
assert.equal(calls,2,'abort retried or doubled the decision call');

const missing=await tool.execute('native-missing',{...request,model:'unknown-alias'},undefined);
checkError(missing,'config');
assert.equal(calls,2,'unknown aliases must fail before provider dispatch');

behavior='invalid';
const invalid=await tool.execute('native-invalid',request,undefined);
checkError(invalid,'output');
assert.deepEqual(invalid.usage,usage,'paid-usage shape must survive invalid provider output');
assert.equal(calls,3);
assert.deepEqual(JSON.parse(fs.readFileSync(authPath,'utf8')),{},'isolated auth storage gained a credential');
session.dispose();
console.log(JSON.stringify({calls,categories:[timedOut.details.category,aborted.details.category,missing.details.category,invalid.details.category]}));
"""
    output = run_native(script, tmp_path, config)
    assert output == {
        "calls": 3,
        "categories": ["timeout", "aborted", "config", "output"],
    }
