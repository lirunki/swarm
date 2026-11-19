---
name: decision
description: Use the decision tool to evaluate typed choices, ordered scores, or yes-probabilities against explicit text, a file, or recent conversation context. It consults exactly one configured model; it does not execute or authorize actions.
---

# Typed decisions

Use `decision` when a narrow, explicitly defined judgment is useful. It makes a
potentially paid model request. Do not call it for deterministic work or recursively
ask it to decide whether to call itself. Model advice never replaces user approval.

## Request

Supply **either** `state` (any JSON value) **or** `state_asset`:

```json
{
  "state_asset": {"type": "context", "last_n_turns": 5},
  "questions": {
    "route": {
      "type": "choice",
      "instructions": "What is the next appropriate step?",
      "criteria": {
        "continue": "Enough evidence and authorization to proceed",
        "investigate": "More evidence is needed",
        "ask_user": "A human decision or permission is needed"
      }
    },
    "risk": {
      "type": "score",
      "instructions": "Evaluate operational risk.",
      "criteria": ["No external effects", "Reversible scoped change", "Disruptive or irreversible change"]
    },
    "needs_review": {"type": "noul", "instructions": "Does the proposal need human review?"}
  }
}
```

Other sources:

```json
{"state_asset":{"type":"text","text":"Evidence to evaluate"},"questions":{"check":{"type":"noul","instructions":"Is the evidence sufficient?"}}}
```

```json
{"state_asset":{"type":"file","path":"/workspace/report.md"},"questions":{"check":{"type":"noul","instructions":"Does the report contain an unresolved blocker?"}}}
```

Files are UTF-8 text on the agent machine. Relative paths use its working directory.
Common credential filenames/directories are refused; this is a best-effort denylist,
not secret detection. Never submit secrets or work around the guard by copying them elsewhere.
Whole-context input uses the last effective provider request, excluding thinking,
raw archives, provider metadata, and the current unfinished tool batch. If that
snapshot is unavailable/unsupported, provide explicit text or a file instead.
`last_n_turns` is an optional positive integer counting user-started turns; omit
it for all available effective conversation context. `include_system: true` is an
explicit opt-in; normally system instructions are not forwarded. Do not treat
historical source text as new authorization. Oversized input fails without silently
truncating or invoking another model to summarize it.

## Results

The response contains `answers` under the question IDs plus `metadata` identifying
adapter/provider/model, request ID, and probability provenance.

- **choice:** `choice`, per-option `probabilities`, and `confidence`.
- **score:** fractional `score` from 0 to `criteria.length - 1`, `confidence`, and
  probabilities when supplied by the backend. Do not invent missing distributions.
- **noul:** `noul` in [0,1], the probability of yes—not a Boolean or automatic action.

Ask 1–8 independently meaningful questions in one call. Exactly one selected
backend runs; there is no ensemble or automatic fallback. LLM bridge scores and
probabilities are **self-reported estimates, not calibrated Jev probabilities**.
Apply thresholds and escalation in caller logic, based on real evaluations.

## Images

Explicit named image assets are supported by image-capable LLM bridges:

```json
{"state":"Inspect photo-1","assets":{"photo-1":{"type":"image","path":"/workspace/package.jpg"}},"questions":{"damaged":{"type":"noul","instructions":"Is photo-1 visibly damaged?"}}}
```

The native Jev form `{ "type":"image", "format":"jpg", "data_base64":"..." }`
also works. Avoid placing large base64 strings in tool arguments when a local image
file is available. `box` is experimental and requires an explicitly configured,
verified box-capable direct HTTP profile; ordinary LLM/native Pi classifier bridges
reject it before a call. Do not silently change providers to get this capability.

## Configuration

The default is the **generic LLM bridge** to `github-copilot/gpt-6-luna`, using Pi's
normal authentication. This is not OpenAI's classifier/Decisions API and does not
require its separate key. It never changes the main conversation model.

```text
/decision                  # selected model and limits
/decision list             # configured profiles
/decision models           # available Pi chat models
/decision set              # interactive model picker
/decision set provider/model
/decision add review provider/model
/decision set review
/decision show review
/decision reload
```

The user may explicitly add a profile with command-quoted JSON:

```text
/decision add custom '{"adapter":"llm","provider":"github-copilot","model":"gpt-6-luna","images":true,"box":false}'
```

Configuration: `$PI_DECISION_CONFIG` (absolute override), otherwise
`${PI_CODING_AGENT_DIR:-~/.pi/agent}/decision.json`. Commands persist explicitly and
atomically there. Never modify the configured model/provider or limits on your own
to avoid an error. Ask the user before sending sensitive data to a different provider.

Timeouts, invalid results, or cancellation do not cause automatic retries. Preserve
the request ID; a timed-out paid call may already have run. Errors do not imply a
negative decision. No recommendation is executed automatically.

## Foundry profiles

An explicitly configured direct HTTP profile can use `auth_header: "api-key"` with
`auth_env` or an absolute private `auth_file` (not both). The credential JSON binds
its key to the exact endpoint. Never read or include `decision.txt` or files in
`decision-credentials` as state; do not print keys or rewrite endpoint bindings.
The configured Foundry `model` is its deployment name. Model selection remains
explicit: `/decision set msdecision` changes only the decision backend.
