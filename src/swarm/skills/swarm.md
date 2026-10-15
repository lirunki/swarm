---
name: swarm
description: Report goals, activities, checkpoints, session metadata and status to the Swarm dashboard. Use throughout every Swarm-managed agent session, especially on status transitions and before ending a turn.
---

# Swarm agent coordination

You are one worker in a swarm. Read your existing record at `$SWARM_AGENT_FILE`
(or `$SWARM_HOME/agents/$SWARM_AGENT_ID.md`; home defaults to `~/.swarm`).
Do not create a new identity or overwrite another agent's record.

## Report before working, on activity transitions, and before ending every turn

Use the Python interpreter supplied by Swarm. It has the reporting tool installed:

```bash
"$SWARM_PYTHON" -m swarm report --status running \
  --activity 'Implementing the parser' \
  --activities '[{"title":"Design parser","status":"done"},{"title":"Implement parser","status":"running"},{"title":"Test parser","status":"pending"}]'
```

The command inherits SWARM_HOME and SWARM_AGENT_ID. It updates the existing
record under a lock, atomically replaces it, and captures PI_SESSION_ID and
PI_SESSION_FILE from pi's shell tool environment. Swarm supplies the process ID;
do not report a shell PID as the agent PID. Preserve the goal unless it changes.
Report actual metadata when it changes (`--branch`, `--worktree`, `--repository`,
`--folder`). Never switch pi sessions with /new, /fork, /clone or /resume inside
a managed process: Swarm must retain the exact session file it launched.

Status values (use these exact spellings):

- `running`: actively executing an activity toward the goal.
- `waiting`: need human input. Explain exactly what is needed with `--wait-reason`.
- `dormant`: intentionally waiting for an external event or timer. Explain the
  event with `--wait-reason`. Use `--wake-at` for a timer (timezone-aware ISO-8601),
  or `--wake-at none` for event-only waiting. Do not busy-loop or sleep in a tool
  to implement timers; return control and let Swarm wake you.
- `idle`: this turn ended, but the overall goal is not accomplished.
- `done`: the overall goal is genuinely accomplished, not merely this turn.

## Before asking the user a question

**Notify Swarm before asking, not after.** Whenever you need user input, a
clarification, a choice, confirmation, or approval:

1. First report `waiting` with `--wait-reason` containing the question or a
   concise description of the input needed. Clear any scheduled wake timer with
   `--wake-at none` so a timer cannot resume work while you await the answer.
2. Only after that report succeeds, ask the question. This applies both to
   plain-language questions and to question/confirmation tools (such as `ask`).
   Do not call a blocking question tool first: Swarm must have the waiting state
   before the tool suspends the agent.
3. Keep the status `waiting` while the answer is outstanding, including when
   ending a turn with a question. Do not overwrite it with `idle` or `done`.
4. When the user supplies the needed answer and you resume work, report
   `running` and clear the old `--wait-reason` before continuing. If the answer
   is still insufficient, report the remaining question before asking again.

```bash
# BEFORE presenting the question or invoking a question tool:
"$SWARM_PYTHON" -m swarm report --status waiting --wake-at none \
  --wait-reason 'Should I preserve the existing API or introduce a breaking change?'

# Ask the user now, then wait for their answer.

# AFTER receiving the answer, before resuming work:
"$SWARM_PYTHON" -m swarm report --status running --wait-reason '' \
  --activity 'Implementing the agreed API design'
```

If reporting fails, retry or repair the report before invoking a blocking question
tool. If you cannot report, explicitly tell the user that Swarm could not be
updated; do not claim that the dashboard shows `waiting`.

## Other reporting examples

```bash
"$SWARM_PYTHON" -m swarm report --status waiting \
  --wait-reason 'Which migration strategy should I use?' \
  --notes 'Parser implemented. Tests pass. Need a decision before changing database schema.'

"$SWARM_PYTHON" -m swarm report --status dormant \
  --wait-reason 'Check PR 42 after CI' --wake-at '2030-01-02T15:00:00Z' \
  --notes 'PR 42 is open. Last check: tests pending. Next: inspect CI and review comments.'

"$SWARM_PYTHON" -m swarm report --status done --wake-at none \
  --activity 'Goal complete' --notes 'Implemented, tested, and documented. PR 42 merged.'
```

## Orchestrating direct children

A managed agent may create and control only its **direct children** through the local
controller API. This is a cooperative same-user ownership boundary, not a sandbox
against hostile agents: children still have the user's filesystem, Git, network and
provider access. Never manipulate tmux, child records, session files, or worktrees
for orchestration; use the CLI. An operator outside a managed session must put global
`--parent-id PARENT` before the subcommand. Managed sessions inherit
`SWARM_AGENT_ID` automatically.

Before spawning, choose a stable task key and persist it in your checkpoint. Reuse
that key for every retry of the same logical task:

```bash
"$SWARM_PYTHON" -m swarm spawn \
  --request-id stable-task-v1 --name 'Parser worker' --goal 'Implement parser' \
  --repository /absolute/repo --worktree /absolute/new-worktree \
  --branch agent/parser --base HEAD --context fresh \
  --instructions 'Run focused tests and leave changes uncommitted.'

"$SWARM_PYTHON" -m swarm agent list
"$SWARM_PYTHON" -m swarm agent status CHILD_ID
"$SWARM_PYTHON" -m swarm agent message CHILD_ID 'Please also test malformed input.'
"$SWARM_PYTHON" -m swarm agent wake CHILD_ID --message 'Continue from your checkpoint.'
"$SWARM_PYTHON" -m swarm agent stop CHILD_ID
"$SWARM_PYTHON" -m swarm agent stop CHILD_ID --force
"$SWARM_PYTHON" -m swarm agent wait CHILD_ID
```

The repository must be an absolute existing repository directory. The worktree must
be an absolute, nonexistent destination; the branch must be new, and `--base` must
explicitly identify the intended starting revision (normally `HEAD`). Dirty changes
are never copied into the child worktree. Swarm does not merge, delete a worktree or
branch, or commit for you.

`--context fresh` starts clean context. `--context fork` creates a portable,
bounded checkpoint (at most 400,000 characters) without model calls, gives the child
a new identity, and archives the source used to build it. It does not share a live
session. Fork snapshots are limited to 16 MiB each and 256 MiB total retained
snapshots per registry. The character cap is not a provider token guarantee; use
fresh context or compact first if the chosen model cannot fit it.
**Never use pi's `/fork` on the parent session** (nor `/new`, `/clone`, or
`/resume`) in a managed process.

Limits for agent-initiated spawns/wakes are four active children per parent,
16 active managed agents globally, and three child nesting levels. At most 32 children are retained per parent. A live process still consumes an active
slot even if its goal is marked done; stop finished children to free those slots. Failed children
remain visible with their records and artifacts and are not automatically retried.
Inspect the existing child and artifacts before choosing a new request key.

`agent wait` is event-driven: it registers the parent for a child completion or
attention event and marks the parent dormant; it does not poll. If the response
has `ready: true`, the child already needs attention—inspect that result immediately.
Otherwise finish the current turn so the parent can actually become dormant. A matching event
wakes the parent. Pending delivery is durable across restarts; notification messages
include an event ID and may repeat if delivery succeeded just before an interruption.
Ignore already-handled event IDs. Failed delivery is retried up to three times,
30 seconds apart, then shown as an attention/error state. Operator stop clears the
wait; disabling the parent's `auto_resume` suppresses wakeups while disabled. Cooperative `agent stop` requests a checkpoint and allows up to 30 seconds
before forcing termination; `--force` stops immediately.

The CLI accepts only loopback HTTP controller URLs, ignores proxies and redirects,
and uses request timeouts. If a spawn response is lost, inspect `agent list` and
retry with the **same** request ID; a new key can create duplicate work. For messages
or other uncertain operations, inspect status before repeating a side effect.

## Checkpoints and recovery

Use `--notes` to persist a concise Markdown checkpoint: completed work, current
activity, exact next step, relevant URLs/PRs/artifacts, decisions, and any action
whose result was uncertain at interruption. Notes become the Markdown body.
Record enough that a resumed agent can safely continue after a machine reboot.
A "Resume where you were" prompt means read the record AND existing session
history, verify actual Git/external state, then continue. Never blindly repeat a
commit, merge, purchase, deployment, or other side effect.

Timers are one-shot. Event-only dormant workers stay dormant until a user or
external service explicitly calls Swarm's wake endpoint. Tab closure does not
mean stop; waiting/idle/dormant do not mean goal accomplished.

## File protocol (for other agent backends)

`$SWARM_HOME/agents/<uuid>.md` has YAML frontmatter plus a Markdown checkpoint.
Required fields: `id` (UUID matching filename), `name`, `goal`, `folder`.
Other fields include `backend`, `status`, `activity`, `activities`, `repository`,
`worktree`, `branch`, `pid`, `session_id`, `session_file`, `wake_at`, `wait_reason`,
`updated_at`, `managed`, and `auto_resume`. Timestamps require a timezone.
Preserve unknown fields. Write UTF-8 to a temporary sibling then rename atomically;
prefer the reporting command to avoid racing Swarm's runtime metadata updates.
Do not set managed/auto_resume or alter runtime identity yourself. Arbitrary
external files are observed only and are never automatically executed.
