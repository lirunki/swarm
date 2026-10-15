# Swarm

A local Python dashboard for many independent agents. Start coding workers,
researchers, and PR trackers; see what each is doing; attach to their actual
terminals without tying process lifetime to a browser tab.

**First backend: [pi coding agent](https://pi.dev).** Linux / WSL and macOS
(POSIX terminals + tmux). Native Windows is not supported; run it inside WSL
and open the dashboard in your Windows browser.

## Run

Requirements: Python 3.11+, **tmux 3.2+**, Git for worktrees, and an installed,
authenticated `pi`. Check `pi` works in your normal terminal first.

```bash
# Ubuntu / WSL prerequisites, if needed:
# sudo apt install python3-venv tmux git
# macOS: brew install tmux

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
swarm serve --open
```

Open **http://127.0.0.1:8787**. `swarm` with no subcommand also starts the server.
The frontend and terminal libraries are served locally; no CDN is needed.
Pi's model calls naturally still require your configured provider.

```bash
swarm doctor
swarm --home /path/to/registry serve --port 8788
swarm serve --pi /absolute/path/to/pi
swarm serve --no-recover   # inspect records without launching saved agents at startup
```

Only loopback bindings are allowed. Run **one controller per registry**, with
one Uvicorn worker (the CLI enforces the latter). Do not expose it through a
public reverse proxy. It is a local-user application, not a multi-user server,
a sandbox, or an authentication boundary against other local processes.

## Dashboard

- Five explicit goal states: **running**, **waiting for input**, **dormant**,
  **idle** (turn completed), **done** (goal accomplished).
- Live counts, status filters, search, current activity, activity checklist,
  checkpoint, folder, Git worktree/branch, session ID, process ID, and errors.
- **New agent** starts pi in an existing folder. Research folders need not be Git repos.
  Select **Create a new Git worktree** to run `git worktree add -b <branch>` in
  an existing **local repository**. Swarm never switches an existing folder's branch.
  Prefer a separate worktree per coding agent to prevent concurrent Git edits.
- **Compact / expand cards** with the chevron in each card header. Compact cards
  show only name, goal and status. Done, idle, dormant and offline agents compact
  automatically; live running and waiting-for-input agents start expanded.
  Manual choices take precedence and are remembered per agent, registry and browser,
  across live updates/reloads. If browser storage is unavailable, choices still work
  for the current page. This is display-only: model context/history and processes
  are unaffected. Expand a card to use its normal actions and inspect details.
- **Terminal** attaches to the real pi TUI using xterm.js and a tmux PTY client.
  Tabs retain scrollback while open, and open tabs are remembered locally.
- **Closing a terminal tab, the browser, or Swarm does not stop agents.**
  No hidden agent launch happens when a tab opens.
- **Wake** resumes an offline session or submits instructions to a live pi editor.
  Treat that editor as shared: avoid composing a message while another browser
  tab/user submits one. Pi queues messages received while working.
- **Stop** terminates that agent's tmux session and disables automatic restore;
  history and goal are retained. **Edit** changes registry metadata only —
  setting a status alone does not suspend/start the process.
- **Edit → Forget agent** removes an inactive agent from the dashboard. A live
  worker with status `running` must be stopped first; an offline worker with a stale
  running status can be forgotten. Idle/waiting/dormant/done agents may still have a
  live pi TUI: Forget terminates that Swarm-owned process before removing the record.
  A confirmation is required. If termination fails, the record is retained and
  auto-resume/timers are disabled. Externally observed PIDs are never terminated.
  Saved pi sessions (including adopted/imported histories), source Grok files,
  working folders, worktrees and branches stay untouched. You can adopt the retained
  pi session again later; for Grok imports, adopt the converted pi history rather
  than overwriting it with a new conversion.

Agents have your user account's filesystem, Git, network and provider access.
Choose goals and folders accordingly. Pi project trust prompts are left intact;
if pi is waiting at startup for trust/login/model selection, open its Terminal.

## Adopt an existing pi session

Quit the original pi process first (not just its terminal view). Then:

1. Click **Adopt pi session**.
2. Choose a saved inactive session; optionally edit its name and goal.
3. Click **Adopt session**. It appears **idle/offline**, with auto-resume off.
4. Click **Wake** when you want it to continue.

Adoption reuses the **exact original JSONL file, session UUID, and original
working folder**; it neither copies conversation history nor launches pi.
The original session is not deleted or moved. Keep that session file for future
recovery. If the goal evolved during the conversation, specify its current goal
when adopting (the default suggestion is the first user request).

```bash
swarm sessions
swarm adopt ~/.pi/agent/sessions/--project--/session.jsonl \
  --name 'PR tracker' --goal 'Track PR 42 until it merges'
```

Discovery uses `PI_CODING_AGENT_SESSION_DIR`, or
`$PI_CODING_AGENT_DIR/sessions` (default `~/.pi/agent/sessions`), plus Swarm's
own session directory. For other/custom pi session directories:

```bash
swarm --pi-sessions-dir /path/to/pi/sessions serve
swarm --pi-sessions-dir /path/to/pi/sessions adopt /path/to/pi/sessions/example.jsonl
```

Already registered sessions cannot be adopted twice. Because pi may replace its
process arguments with just `pi`, Swarm cannot always identify an unmanaged
process's exact open session. It conservatively blocks adoption of **any
session in a folder with a running pi process**. Unknown process folders block
all adoption; on macOS, `lsof` is used to resolve folders. Refresh after quitting
the original pi. These are point-in-time local process checks, not locks shared
with unmanaged pi: do not open the same saved session elsewhere after adoption.

## Import a Grok Build session into pi

Click **Import Grok session**, choose a saved conversation, check its working
folder, and click **Convert and adopt**. The new agent appears **idle/offline**
with auto-resume disabled. It runs **pi**, not Grok, when you explicitly Wake it.
Stop the original Grok worker before waking its imported counterpart.

The supported source is **Grok Build `chat_format_version: 1`**, organized as:

```text
~/.grok/sessions/
  <URL-encoded-workspace>/<session-uuid>/
    summary.json
    chat_history.jsonl
```

On Windows this is typically `C:\\Users\\<user>\\.grok\\sessions`. Swarm running
in WSL also discovers `/mnt/c/Users/<Linux-username>/.grok/sessions`, as well as
`USERPROFILE` when supplied. Native `~/.grok/sessions` and `$GROK_HOME/sessions`
are searched too. An explicit root overrides discovery:

```bash
swarm --grok-sessions-dir /mnt/c/Users/you/.grok/sessions serve
swarm --grok-sessions-dir /mnt/c/Users/you/.grok/sessions grok-sessions
swarm --grok-sessions-dir /mnt/c/Users/you/.grok/sessions import-grok \
  '/mnt/c/Users/you/.grok/sessions/C%3A%5Cproject/SESSION_UUID' \
  --name 'Imported investigator' --goal 'Finish the investigation'
```

Pass either the session directory or its `chat_history.jsonl`. Windows drive
paths map to `/mnt/<drive>` on WSL. The dialog's **Working folder** field (or CLI
`--folder`) lets you select an existing local folder when the project moved or
cannot be mapped. The source folder is retained in provenance and history.
This does not copy or check out the workspace; make sure it contains the work
that the conversation describes.

The converter writes a **new pi v3 session** under
`~/.swarm/sessions/imports/grok/` and uses the same offline registration pipeline
as pi adoption. The original Grok directory is read-only throughout import.
Converted UUIDs are distinct from Grok UUIDs, deterministic per source session,
and recorded together in `import_source` with the source path, SHA-256, original
working folder, import time, and warnings. Repeat/concurrent imports are rejected;
existing pi history is never overwritten. Original Grok credentials are never read.

Conversion policy:

- User text becomes pi user messages. Assistant text, visible reasoning summaries,
  and tool exchanges become labelled historical context, preserving the speaker
  without making pi restore the source Grok model. Original roles remain in the archive.
- **Every original history record** is also archived as a pi `custom` entry, along
  with source summary metadata, so fields that have no pi equivalent are retained.
- Recorded Grok tool calls/results are **inert text**, never pending pi tool calls.
  They are not replayed, and Grok's tool definitions/MCP configuration are not installed.
- Original system prompts, synthetic environment/skill preambles, and opaque
  encrypted reasoning are archive-only and are not installed as active instructions.
- Unknown record types and non-text attachments remain visible as historical JSON;
  attachment URLs and external files are never fetched. Grok memory, credentials,
  sandbox/profile settings, and live background jobs are not migrated.
- Original timestamps remain in archived source data. New pi entry times describe
  conversion time. Imported transcripts add no synthetic token/cost charges, and
  no historical model selection is forced: continuing uses pi's configured default.

Only local files inside configured session roots can be imported. Invalid or
partially written JSON, unsupported format versions, and histories over 64 MiB
are rejected. The importer checks source files again before committing; if Grok
is still writing, stop it and retry. This is a point-in-time snapshot, not a live
synchronization or a cross-runtime process lock. Later Grok changes are not merged.

If Swarm is already running, restart **only the Swarm controller** after updating
Python code, then refresh the browser to load the new endpoints and UI. Existing
tmux-backed agents keep running; opening the new dialog does not launch any agent.

## Persistence and reboot recovery

Default storage (`SWARM_HOME` overrides `~/.swarm`):

```text
~/.swarm/
  agents/<uuid>.md    # source of truth: YAML frontmatter + Markdown checkpoint
  sessions/*.jsonl    # exact session files for agents created by Swarm
  sessions/imports/   # converted foreign sessions (currently Grok Build)
  state.json         # atomically persisted last-good dashboard snapshot
  locks/             # cross-process report/adoption locks
  controller.lock    # single-controller lock
```

The registry is polled every 750 ms and pushed to browsers over WebSockets.
Changes and snapshots are written using temporary files, fsync, and atomic
rename. Reports merge under locks. Invalid/partial edits are shown as warnings;
a live controller retains the last-good record rather than losing the row.
Deleted files remove rows; snapshots never silently resurrect deleted records.
Do not delete a live agent's file to stop it — use Stop first. Use **Forget agent**
for safe dashboard removal; forgotten records are also removed from the snapshot
and are not resurrected by recovery.

**Restart Swarm after a reboot** (or configure your OS to do so):

| Saved state | Startup behavior if managed + auto-resume enabled |
| --- | --- |
| Process still alive | Reattach to it; no duplicate launch or resume prompt |
| Running, process gone | Open the exact session and submit “Resume where you were” with goal/activity/checkpoint |
| Waiting / idle, process gone | Reopen pi with its saved history, **no automatic work prompt** |
| Dormant | Keep waiting; expired timers wake once, otherwise wait for event/timer |
| Done / auto-resume disabled | Never automatically start |
| External observed record | Never execute automatically |

A failed launch is visible, not retried in an endless loop. Use Wake after fixing
pi credentials, missing folders, etc. Missing **known** session history is an
error, not permission to start an empty conversation. Running state records the
agent's intent; process liveness is shown separately so a crash never masquerades
as “goal accomplished.” Recovery is not transactional exactly-once execution:
resumed agents are instructed to verify external side effects before repeating them.

A user systemd example is in [`examples/swarm.service`](examples/swarm.service).
Adjust paths and PATH before installing it. Your normal shell/provider
environment is not necessarily present in a login service. For agents to
survive logout, your OS must keep the user's processes alive; on Linux this may
require user lingering. No OS services or startup settings are changed by Swarm.

## Agent communication skill

Canonical skill: [`src/swarm/skills/swarm.md`](src/swarm/skills/swarm.md).
Swarm explicitly loads it via `pi --skill` and tells agents to read it. It explains
reporting, all statuses, checkpoints, dormant timers, and safe recovery.
Agents must report **waiting before asking a question**, including before blocking
question/confirmation tools, and return to **running** when the answer arrives.

Inside a managed pi shell tool:

```bash
"$SWARM_PYTHON" -m swarm report --status running --activity 'Implementing the parser'
"$SWARM_PYTHON" -m swarm report --status waiting --wait-reason 'Need schema decision'
"$SWARM_PYTHON" -m swarm report --status dormant \
  --wait-reason 'Recheck CI' --wake-at '2030-01-02T15:00:00Z' \
  --notes 'PR 42 pending. Next: inspect CI and review comments.'
"$SWARM_PYTHON" -m swarm report --status done --wake-at none --notes 'Goal met; PR merged.'
```

The helper captures `PI_SESSION_ID` / `PI_SESSION_FILE` and preserves unrelated
fields. PID comes from tmux, not the shell helper. The Markdown body stores the
checkpoint. Status is **agent-reported**, not inferred from terminal text: the
skill must be followed. There is no pi extension or heuristic pretending to
know when a semantic goal is accomplished.

Other backends can publish matching files today for observation. To **launch**
another backend, implement the adapter boundary in `adapters.py` and register it
in `Processes.adapters`; only the pi adapter is provided in v0.1.

### Example record

```yaml
---
version: 1
id: 14db3599-537a-420d-b65e-00577df3d9da
name: PR tracker
backend: pi
goal: Track PR 42 until merged
status: dormant
activity: Waiting for CI
activities:
  - title: Open PR
    status: done
  - title: Check CI and reviews
    status: running
folder: /home/me/worktrees/pr42
repository: /home/me/worktrees/pr42
worktree: /home/me/worktrees/pr42
branch: feature/parser
pid: 1234
session_id: 7e2759d3-1e11-4738-8283-64424660ddbc
session_file: /home/me/.swarm/sessions/14db3599-537a-420d-b65e-00577df3d9da.jsonl
managed: true
auto_resume: true
wake_at: '2030-01-02T15:00:00Z'
wait_reason: Check CI again
updated_at: '2030-01-02T14:50:00Z'
---

PR opened. Next: inspect CI results and review comments.
```

Required fields: `id`, `name`, `goal`, `folder`. New arbitrary records default to
`managed: false`, and are **observation-only**. Use timezone-aware timestamps.
Filename must match the UUID. Prefer the helper over direct writes; external
writers should preserve unknown fields and atomically rename a temporary file.
Registry files and session history may contain sensitive material; do not commit them.

## External events and API

Dormant agents with no `wake_at` wait indefinitely. A PR webhook bridge or other
local service can explicitly wake one:

```bash
curl http://127.0.0.1:8787/api/agents/AGENT_UUID/wake \
  -H 'Content-Type: application/json' \
  -d '{"message":"PR 42 CI finished. Inspect the result and continue."}'
```

No webhook polling integration is bundled; agents can use timers to schedule
checks, or an external service can call Wake. Endpoints:

- `GET /api/state`, `WS /ws/state`: full live dashboard state.
- `POST /api/agents`: create/start; `PATCH /api/agents/{id}`: update metadata.
- `POST /api/agents/{id}/wake` and `/stop`: explicit process control.
- `DELETE /api/agents/{id}`: forget an inactive agent, terminating any remaining
  owned process and preserving all saved session files (409 if actively running).
- `GET /api/pi/sessions`, `POST /api/agents/adopt`: discover/adopt inactive sessions.
- `GET /api/grok/sessions`, `POST /api/agents/import/grok`: discover, convert, and adopt Grok Build history.
- `WS /ws/terminal/{id}`: attach-only terminal (binary output, JSON input/resize).
- `/openapi.json`: generated API schema (no CDN-dependent API explorer).

## Development

```bash
pip install -e '.[dev]'
pytest -q
ruff check src tests
ruff format --check src tests
node --check src/swarm/static/app.js
node --check src/swarm/static/card-views.js
node --test tests/frontend/*.test.cjs
```

When Node is installed, pytest also runs the dependency-free frontend tests for
card rendering/disclosure, automatic defaults, persisted overrides, storage failures,
Edit/Forget confirmation and server-race error handling. These use a minimal DOM
contract and are not a substitute for a real browser layout/keyboard check.

Tests cover record safety/concurrency, snapshots, recovery rules, one-shot timers,
adoption/import safeguards, Grok source preservation and conversion, worktree
isolation, live file updates, API access checks,
inactive-agent forgetting with history preservation, and **real tmux terminal
input/detach/server-restart survival** with a fake pi
(no provider credentials or paid model calls). Real pi/provider interactions and
OS-specific boot/login behavior still need testing in your own environment.

Terminal dependency versions and licenses are in
[`src/swarm/static/vendor/README.md`](src/swarm/static/vendor/README.md).
