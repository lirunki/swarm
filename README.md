# Swarm

A local Python dashboard for many independent agents. Start coding workers,
researchers, and PR trackers; see what each is doing; attach to their actual
terminals without tying process lifetime to a browser tab.

**First backend: [pi coding agent](https://pi.dev).** Linux / WSL and macOS
(POSIX terminals + tmux). **Experimental Windows without WSL** uses an automated,
user-local Cygwin/tmux runtime; start it from regular Git Bash as described below.
Stock Windows Python is not the controller runtime.

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

Binding defaults to **127.0.0.1** (local access only). To explicitly listen on
all IPv4 interfaces for access from another machine:

```bash
swarm serve --host 0.0.0.0
swarm serve --host 0.0.0.0 --port 8788 --open
```

Remote clients use `http://<server-ip>:<port>`; `--open` still opens the local
`127.0.0.1` URL. In this opt-in network mode, Host headers are unrestricted so
LAN IP addresses/hostnames work; browser same-origin checks remain enabled.
Loopback mode retains its strict Host checks. Network mode also removes the
default Host-based DNS-rebinding protection; same-origin checks are not authentication.

**Network mode has no authentication. Anyone who can reach the port can read
private data and control agents with your user account's permissions.** Use it
only on a trusted private network with restrictive firewall rules. Do not expose
it to the public internet or through a public reverse proxy. Swarm does not
configure your firewall automatically.

Run **one controller per registry**, with one Uvicorn worker (the CLI enforces
the latter). It is a single-user application, not a multi-user server, a sandbox,
or an authentication boundary against other local processes.

### Automatic HTTPS certificates

Both the dashboard and relay support `--generate-certs-if-absent`. The flag
requires the **OpenSSL command** on PATH and enables HTTPS/WSS for that server:

```bash
# Dashboard: https://127.0.0.1:8787
swarm serve --generate-certs-if-absent --open

# Relay only: https://swarm-relay.example.com:9443
swarm relay serve --generate-certs-if-absent \
  --cert-host swarm-relay.example.com \
  --api-key-file ~/.config/swarm/relay.key
```

The API key is still required for the relay; certificate generation does not create it.
You can use `./.venv/bin/swarm` without activating the virtual environment.

Default PEM files are stored under the selected registry (`--home`, `SWARM_HOME`,
or `~/.swarm`):

- Dashboard: `tls/dashboard/cert.pem` and `tls/dashboard/privkey.pem`.
- Relay: `tls/relay/cert.pem` and `tls/relay/privkey.pem`.

Use `--certfile /path/to/cert.pem --keyfile /path/to/privkey.pem` together to override
these paths. Without the generation flag, those options use your existing certificate
pair. Plain `swarm serve` remains HTTP; the relay always requires TLS.

Generated certificates last **365 days** (`--cert-days N` overrides this), and include
`localhost`, `127.0.0.1`, `::1`, and the machine hostname. Repeat `--cert-host DNS_OR_IP`
for each additional hostname or IP clients will use. Binding to `0.0.0.0` does **not**
automatically discover or certify your LAN/public IP. For example:

```bash
swarm serve --host 0.0.0.0 --generate-certs-if-absent \
  --cert-host 192.168.1.50 --cert-host workstation.example.com
```

Existing pairs are reused unchanged; the flag does not renew expired certificates
or update their SANs. If only one file exists, startup fails rather than overwriting
an identity. To change names or renew, stop the server and choose two new file paths
(or back up and remove both old files), then distribute the new public certificate.
Private keys are unencrypted for unattended startup and created with mode **0600**.
Never distribute `privkey.pem`.

**Self-signed certificates are not automatically trusted.** Import the public
`cert.pem` into the browser/OS trust configuration for dashboard access. For relay
connections, securely copy the relay's public certificate to each controller and
start that controller with Python's trust configuration set, for example:

```bash
SSL_CERT_FILE="$HOME/.config/swarm/relay-cert.pem" \
  swarm serve --generate-certs-if-absent \
  --relay-url wss://swarm-relay.example.com:9443/v1/connect \
  --relay-node-id workstation-a \
  --relay-api-key-file ~/.config/swarm/relay.key

curl --cacert ~/.config/swarm/relay-cert.pem \
  https://swarm-relay.example.com:9443/health
```

Dashboard HTTPS automatically supplies its local certificate to managed agents and
local federation forwarding through `SWARM_API_CA_FILE`, without disabling TLS
verification or replacing the relay's trust configuration. For manually invoked
agent commands, set `SWARM_API_URL=https://127.0.0.1:8787` and
`SWARM_API_CA_FILE=/absolute/path/to/tls/dashboard/cert.pem` in that shell too.

TLS encrypts traffic; **it does not add dashboard authentication**. The network-mode
warning above still applies. Prefer a publicly trusted certificate for production relays.

## Federate multiple Swarms through an Azure relay

A Swarm controller can make one outbound WSS connection to a central relay. Every
connected controller appears in the dashboard's top-level **Swarm** selector. The browser
continues to connect only to its local controller; that controller multiplexes remote state,
API calls, downloads and terminal bytes through the relay. Remote machines need no inbound
port. Switching the selector displays one complete registry at a time.

This is a **fully trusted mesh**. Either relay API key grants terminal and API control over
every connected controller with that controller user's operating-system permissions. Keep
keys out of URLs, browser storage, command lines and logs. The relay is stateless: offline
controllers disappear from presence and requests are not queued or retried.

### 1. Prepare the Azure VM

Create a DNS record such as `swarm-relay.example.com`, allow inbound TCP 9443 in the Azure
NSG/firewall, install this package in `/opt/swarm`, and obtain a certificate whose SAN matches
the DNS name. Python terminates TLS directly, so give the relay service account read access
to a copied/renewed full chain and private key. Restart the service after certificate renewal.
Do not use a self-signed certificate unless every controller explicitly trusts its CA.

Create one 32-byte-or-longer random mesh key and an optional second rotation key:

```bash
sudo install -d -o swarm-relay -g swarm-relay -m 0700 /etc/swarm-relay
openssl rand -base64 48 | sudo tee /etc/swarm-relay/api-key.current >/dev/null
openssl rand -base64 48 | sudo tee /etc/swarm-relay/api-key.next >/dev/null
sudo chown swarm-relay:swarm-relay /etc/swarm-relay/api-key.*
sudo chmod 0600 /etc/swarm-relay/api-key.*
```

Run the foreground service directly while testing:

```bash
swarm relay serve \
  --host 0.0.0.0 --port 9443 \
  --certfile /etc/swarm-relay/fullchain.pem \
  --keyfile /etc/swarm-relay/privkey.pem \
  --api-key-file /etc/swarm-relay/api-key.current \
  --api-key-file /etc/swarm-relay/api-key.next
```

`GET https://swarm-relay.example.com:9443/health` reports protocol version and connected-node
count without disclosing identities. The relay deliberately supports one process/worker;
live WebSocket ownership is in memory. [`examples/swarm-relay.service`](examples/swarm-relay.service)
is a hardened systemd starting point. Replace its user and paths, then:

```bash
sudo cp examples/swarm-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now swarm-relay
sudo systemctl status swarm-relay
```

### 2. Connect each Swarm backend

Use the **Settings gear icon** at the dashboard's top-right to configure the **local controller**.
The dialog remains local even while the Swarm selector displays a remote workspace.
Enter:

- **Relay URL**, for example `wss://swarm-relay.example.com:9443/v1/connect`.
- **Relay node ID**, a unique stable ID such as `workstation-a`.
- **Relay name**, the display name such as `Office workstation`.
- **Relay API key**, one of the relay's active keys (32–4096 printable ASCII characters).

Click **Connect** to save the configuration and start the existing outbound relay
client without restarting the controller or any agent. Status progresses from
Connecting to Connected, or Reconnecting with a safe error message. The button is
**Disconnect** while connection/retry is enabled; disconnect before editing fields.
Closing the dialog does not disconnect. Remote workspaces appear in the existing
Swarm selector when the connection is ready.

The backend stores the profile, key and connection intent in
`$SWARM_HOME/relay-settings.json` (mode 0600 on POSIX). Protect the user profile/file
ACL on Windows too. This file contains a secret: do not commit it, publish it as a
product or share it. The API returns only a `has_api_key` indicator; it never returns
the saved key. Password drafts are cleared after submission and when closing the
dialog. No key is written to browser storage. Submit keys over **localhost or HTTPS**.
URLs must use WSS (plain WS is accepted only for loopback tests), and must not contain
credentials, query strings or fragments.

Leave the key blank to reuse it for the same relay URL. Changing the URL requires
explicit key entry to avoid forwarding a saved credential to a different relay.
**Disconnect** saves a disabled state, retaining the profile/key for a later Connect.
An enabled saved profile reconnects when the controller restarts; a disconnected
profile stays disconnected. Saved Settings take precedence over startup relay defaults,
so after configuring the UI you can launch plain `swarm serve`. Remove the protected
profile file while the controller is stopped to return to CLI-only defaults.
The local API origin and remote-restart permission remain startup policy, not form inputs.

Settings call these **local** backend endpoints (not the active remote workspace):

- `GET /api/federation/settings`: redacted configuration, intent, connection state/error.
- `POST /api/federation/connect`: `relay_url`, `relay_node_id`, `relay_name`, optional
  `relay_api_key` (null/empty reuses a saved key only for an unchanged URL).
- `POST /api/federation/disconnect`: stop relay connection/retries, keeping agent processes alive.

Responses are marked `Cache-Control: no-store`. Settings endpoints are deliberately
under the federation prefix, which mesh RPC is not allowed to proxy. This reduces
accidental remote configuration; it is not a sandbox against trusted mesh peers with
filesystem/terminal control.

CLI-only configuration is also supported. Copy one active mesh key to a mode-0600
file on each backend and choose a unique stable node ID:

```bash
swarm serve \
  --relay-url wss://swarm-relay.example.com:9443/v1/connect \
  --relay-node-id workstation-a \
  --relay-name "Office workstation" \
  --relay-api-key-file ~/.config/swarm/relay.key
```

Duplicate live node IDs are rejected instead of silently replacing a connection. Controllers
reconnect with bounded exponential backoff. State updates and terminal traffic share the
outbound WSS connection; API responses larger than one frame are chunked with bounded sizes.
Mutating API operations are never retried after a disconnect because their outcome may be
unknown.

Remote controller restart is denied by default. Opt in only on controllers where every mesh
member is allowed to restart that backend:

```bash
swarm serve ... --allow-remote-restart
```

The capability is advertised to dashboards, which disable the remote restart button when it
is absent. Agent processes remain tmux-owned during a controller restart. There is no remote
Git fetch, pull or push API.

For zero-downtime key rotation: start the relay with old and new key files, then disconnect,
update and reconnect controllers one at a time through Settings (or restart CLI-only
controllers with the new key). Finally restart the relay with only the new key.
The relay accepts at most two keys and compares them in constant time.

## Windows without WSL (experimental)

From regular Git Bash in this checkout:

```bash
bash ./swarm-windows.sh
```

Or double-click `swarm-windows.cmd`. First run installs private Cygwin/Python/tmux,
Node and Pi, builds dependencies, validates the terminal/lifecycle, then opens
the dashboard. Later runs reuse the installation. No administrator rights or
manual tmux setup are required; the first build can take several minutes and
requires network access and disk space for the compiler toolchain.

**Pi shell configuration:** setup backs up the existing Windows Pi settings and
sets global `shellPath` to the private Cygwin Bash. This also affects standalone
Pi after restart. Other settings, credentials, history and project-trust choices
are preserved. Provider login/model selection may still require interaction.
There are no machine PATH changes, Git Bash modifications or Windows services.

```bash
bash ./swarm-windows.sh doctor
bash ./setup-windows.sh -Update
```

See [`scripts/windows/README.md`](scripts/windows/README.md) for setup details,
custom locations, logs, network/offline-cache limitations and verification status.
Use local drive paths, not UNC shares. This route is experimental; real
Pi/provider configurations still need Windows testing. Windows session adoption
conservatively blocks while any native Pi has an unknown working folder.

## Dashboard

- Five explicit goal states: **running**, **waiting for input**, **dormant**,
  **idle** (turn completed), **done** (goal accomplished).
- The dashboard uses a slim, two-row **Mission Control** layout: the state
  multiselect sits under the label on the left; **Adopt session / New agent** sit
  above **Collapse all / Search** on the right. The large hero and redundant fleet
  headings are omitted so tiles start immediately below the controls. Workspace,
  relay/settings and terminal navigation remain available outside the dashboard.
- Choose several states together; each entry shows its live count and state color.
  **All** checks or clears every state. Search and filter matches retain ancestor
  context; a short explanation appears only when needed. Activity, checkpoints,
  workspace metadata and errors remain available by expanding a card.
- **New agent** starts pi in an existing server-local folder. The folder selector lists
  folders already used by agents; **Browse…** navigates directories visible to the Swarm
  server. Research folders need not be Git repositories. The model selector is populated
  from the configured Pi executable's available model catalog (`pi --list-models`) and
  identifies Pi's configured default without exposing credentials.
- Select **Create a sibling Git worktree from this folder** to use the chosen existing Git
  worktree as source. For source `X` and branch `feature/name`, Swarm creates the branch in
  sibling `X_feature_name` (`/` and `\\` become `_`) using `git worktree add -b`. The
  destination is shown before launch and must not already exist. Swarm never switches the
  source folder's branch. Prefer a separate worktree per coding agent to prevent concurrent
  Git edits.
- **Compact org chart:** agents appear as 128×128 square tiles containing only
  their name, status, expand arrow and a **↗** terminal shortcut directly beneath
  the arrow, plus their latest product link when one is
  advertised. Hover over the tile/name to see current
  activity; a red name means a live process, independent of goal status. Cards
  default to compact even when status changes; saved manual expansion choices
  still take precedence. **Collapse all** restores the dense view in one click.
  Expanding reveals the goal, activity, checkpoint, metadata and normal actions.
- **Parent → child hierarchy:** connector lines show which agent created each
  child using `parent_id`. Independent agents form separate roots. Roots wrap to
  fit the viewport; wide families scroll horizontally without compressing names
  into unreadable strips. Search/status filters retain dimmed ancestors so a
  matching child keeps its lineage visible. Missing parents and invalid cycles
  remain visible as roots with tooltip/accessibility warnings, never disappear.
  Expanding an ancestor does not hide its descendants.
- Card display preferences are remembered per agent, registry and browser across
  updates/reloads. If browser storage is unavailable, they still work for the
  current page. Keyboard users can focus the chart to scroll and use each tile's
  expand button (with its activity description). These are display-only controls:
  model context/history and process state are unaffected.
- **Terminal ↗** (or **↗** on a compact tile) attaches to the real pi TUI using
  xterm.js and a tmux PTY client. Tabs retain scrollback while open, and open tabs
  are remembered locally. The terminal toolbar is a single 28px row (about half
  its previous height), with long titles/metadata ellipsized and available on hover.
  Its buttons retain 24px click targets. It keeps **Reconnect**; use the tab's
  **×** to close it. Wake remains available on the agent card and in the offline
  terminal overlay, rather than as a redundant toolbar button.
- **Closing a terminal tab, the browser, or Swarm does not stop agents.**
  No hidden agent launch happens when a tab opens.
- The Swarm picker sits directly beside the main logo, without a redundant small
  label. Its dropdown is 136.5px wide (1.3× the previous 105px); long workspace names
  remain available in the option list and selected-value tooltip, with an accessible
  “Active Swarm” label. Header controls stay on one line when space permits.
- **↻ Restart backend** is icon-only in the top-right header, with an accessible
  label and tooltip (including while restarting). It confirms, then replaces only the Swarm
  controller process and reconnects the dashboard automatically. Agent processes remain
  owned by tmux and continue running throughout the restart.
- **Wake** resumes an offline session or submits instructions to a live pi editor.
  Leave the optional instructions empty to resume or mark running **without sending any
  prompt**. API clients get the same behavior with an explicit `{"message":""}`; omitting
  `message` retains the generated safety resume prompt used by timers and automation.
  Treat a live editor as shared: avoid composing a message while another browser tab/user
  submits one. Pi queues nonempty messages received while working.
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

### Mobile terminal keyboard

When the **standard software keyboard is open** and the live terminal input is
focused, a mobile-only accessory bar appears immediately above it:

**Ctrl · Alt · ← · ↑ · ↓ · → · Esc · Supr**

- **Ctrl** and **Alt** toggle a one-shot modifier for the next typed character or
  accessory key; tap both for a combination. They reset after a key, when the
  keyboard hides, when focus leaves the terminal, or when the connection is unavailable.
- Arrow keys respect the terminal's normal/application cursor mode and modifier
  combinations. **Supr** sends forward Delete (`Delete`), not Backspace.
- **Esc** and control shortcuts are real terminal input: Ctrl+C can interrupt an
  application just as it does with a physical keyboard. No keys are queued while offline.
- Button taps retain input focus, keeping the software keyboard open. The terminal
  resizes above the key bar rather than being covered by it.
- Copy text, other forms/dialogs, the dashboard and desktop browsers have no key bar.
  Native paste and multi-character IME/paste text are not converted into control
  commands; terminal mouse reports and device replies are passed through unchanged.

Keyboard visibility uses the browser's VirtualKeyboard geometry when available,
otherwise focused-input plus VisualViewport/resize measurements. Pinch zoom and
small browser-toolbar changes are not treated as a keyboard. Floating keyboards
that report neither geometry nor viewport changes may not be detectable; hardware
keyboards without the software keyboard do not show the accessory bar.

This uses the existing terminal WebSocket/PTY transport, including remote terminals.
Refresh the page after updating; no backend restart is needed.

### Mobile terminal copying

On touch-first/mobile browsers, **Copy text** appears beside **Reconnect**. It puts
only the currently visible xterm screen into a **read-only native textarea covering
exactly the terminal control**, not a dialog or a separate history view. Select text
with the phone's normal handles and choose **Copy**. Once the browser's native copy
action finishes, the layer closes automatically. **Done copying** (the same button)
or Escape closes it without copying.

Live output continues behind the layer, but the captured text stays unchanged for
selection. While copying, swipe-to-tmux gestures and terminal keystroke forwarding
are disabled; normal terminal behavior resumes when the layer closes. The copied
text is never saved in browser storage or fetched from a new backend API, and the
layer is cleared when closed/disposed. The implementation uses native selection and
Copy—not `navigator.clipboard`—so it needs no app clipboard permission and also
works where automatic clipboard APIs are unavailable on plain LAN HTTP.

Desktop terminals keep their existing toolbar and interaction. Hold **Shift** while
dragging to select past tmux mouse handling, then use the browser/system Copy action.
The mobile copy control is hidden on mouse-first desktop browsers.

Use the top-right **?** button for a quick guide to dashboard states, adoption,
terminal scrolling/copying, process controls and relay safety. The adoption actions
are grouped under **Adopt session → pi session / Grok session**; their original
registration/import behavior is unchanged.

### Mobile terminal gestures

Swipe **down** to scroll toward older output and **up** toward newer output, just
as with a desktop mouse wheel. These gestures are translated into xterm's negotiated
mouse-wheel reports and sent over the existing terminal WebSocket to tmux. There
is no separate History view, snapshot request, or new backend endpoint.

Swarm already enables tmux mouse support. With the default tmux bindings, scrolling
up enters copy mode and scrolling back to the bottom exits it; then tap the terminal
to focus the keyboard and type. Applications that handle mouse scrolling themselves
receive the wheel events instead, exactly as they do on desktop. The agent keeps
running while its view is scrolled, and other clients attached to the same pane can
see the same tmux mode/view changes.

Vertical dragging uses pointer capture on the stable terminal container so xterm
redrawing its rows does not cut the gesture short. Taps, horizontal/back gestures,
long-press selection and multi-finger pinch/zoom are not converted to wheel events.
Without mouse reporting, only xterm's own normal scrollback is scrolled; Swarm never
falls back to sending editor Up/Down keys into a mouse-free alternate screen.

After updating, refresh the mobile page to load the new frontend assets. No backend
restart is needed for this touch-to-wheel implementation; it uses the same input
transport as existing keyboard/mouse support, including through the federation relay.

## Advertise agent products

Agents can publish their latest deliverable as a link on both compact and expanded
cards. **One product is retained per agent:** every successful advertisement
replaces the previous one, even across work cycles. Status/checkpoint updates do
not clear it. Product publication does not change process liveness or goal status.

```bash
swarm product --label 'PR #42' --url 'https://github.com/example/repo/pull/42'
swarm product --label 'Pipeline run' --url 'https://ci.example/runs/123'
swarm product --label 'Download report' --file /path/to/report.zip
swarm product --clear
```

The command uses `SWARM_AGENT_ID`; operators can specify `product --id AGENT_ID`.
Like child orchestration, it calls the running loopback controller using
`SWARM_API_URL` or global `--api-url`. CLI relative file paths are resolved from
the caller's working directory. Direct API relative paths use the agent's folder.

External links accept HTTP(S) only, disallow embedded credentials, and are never
fetched server-side. They open in a new tab without opener/referrer access.
For local products, Swarm snapshots a regular server-local file up to **128 MiB**
into private `$SWARM_HOME/products/<agent-id>/<product-id>/payload` storage. The
original file is never modified or deleted; later edits/removal do not affect the
download. Downloads are forced attachments with `nosniff`/no-cache headers and
verify the stored checksum. They use agent/product IDs, never arbitrary download
paths. Directories and devices cannot be published; package multiple files into
an archive first. Only publish intended deliverables, not secrets/private configs.

Superseded or cleared snapshot copies are removed; old download URLs return 404.
Interrupted/unreferenced snapshots are cleaned on startup. If an open download or
filesystem permission prevents cleanup, it is logged and retried during a later
cleanup. An already-started download can finish. Original producer files are kept,
and removing an agent only cleans its Swarm-owned product copies.

API:
- `POST /api/agents/<id>/product` with `{"label":"PR","url":"https://…"}` or
  `{"label":"Report","path":"/server/path/report.zip"}` (exactly one source).
- `DELETE /api/agents/<id>/product` clears the current advertisement.
- `GET /api/agents/<id>/products/<product-id>/download` downloads only the current
  file product. Link products do not have a download endpoint.

The `agent.product` object is persisted in the agent record and included in live
state and child-status responses. This shares Swarm's trusted local-user boundary;
product publication is not a remote upload service or a separate authorization
system. The Swarm skill includes these instructions for agents.

## Orchestrate child agents from the CLI

A Swarm-managed agent can delegate work to direct children through the controller:

```bash
swarm spawn --request-id stable-task-v1 --name 'Parser worker' \
  --goal 'Implement the parser' --repository /absolute/repo \
  --worktree /absolute/new-worktree --branch agent/parser --base HEAD \
  --context fresh --instructions 'Run focused tests.'

swarm agent list
swarm agent status CHILD_ID
swarm agent message CHILD_ID 'Also test malformed input.'
swarm agent wake CHILD_ID --message 'Continue from the checkpoint.'
swarm agent stop CHILD_ID          # checkpoint request; force after 30 seconds
swarm agent stop CHILD_ID --force  # immediate termination
swarm agent wait CHILD_ID
```

The parent defaults to `SWARM_AGENT_ID`, so these commands normally require a
managed session. A local operator may instead put `--parent-id PARENT_ID` before the
subcommand, for example `swarm --parent-id PARENT_ID agent list`. The controller URL
defaults to `SWARM_API_URL` or `http://127.0.0.1:8787`; override it with global
`--api-url`. Only loopback HTTP URLs are accepted. The client disables environment
proxies and redirects and uses timeouts; it never manipulates tmux, records,
sessions, or worktrees itself.

Each spawn needs a stable `request_id` slug (maximum 80 characters). Persist it in
the parent's checkpoint and reuse it when retrying the same logical request. If a
response is lost, inspect `agent list` and retry with the same key—do not choose a
new key until you have inspected the retained child. Message delivery can likewise
be uncertain after a transport timeout, so inspect status before resending.

A child owns a new branch in a new, nonexistent absolute worktree destination made
from an absolute repository directory and an explicit base revision (normally
`HEAD`). Dirty parent changes are not copied. Swarm never automatically commits,
merges, deletes branches, or removes worktrees. `--context fresh` starts without the
parent conversation. `--context fork` deterministically makes a portable checkpoint
of at most 400,000 characters without model calls, gives the child a new identity,
and archives its source; it does not share a live session. Snapshots are limited
to 16 MiB each and 256 MiB retained per registry. The character limit does not
guarantee fit in every model; use fresh context or compact first if necessary.
Never use pi `/fork` on
the parent session.

Ownership is direct-child and cooperative, not a hostile-agent security sandbox;
all agents run as the same user. Limits are four concurrent children per parent,
16 active managed agents globally, three child nesting levels, and 32 retained children per
parent. Failures remain as records/artifacts and are not automatically retried;
inspect them before assigning a new key.

`agent wait` registers an event-driven wait for child completion or attention and
marks the parent dormant—there is no polling. The parent must finish its current
turn after registration, unless the response has `ready: true` (the child already
needs attention). The event wakes it; pending notifications survive restarts and
carry stable event IDs so the parent can ignore a duplicate after uncertain
delivery. Delivery failures retry up to three times, 30 seconds apart, then become
an attention/error state. Operator stop clears the wait; `auto_resume: false`
suppresses wakeups while disabled. Normal stop asks the child to checkpoint,
then forces termination after 30 seconds. `--force` stops immediately. A pending
termination survives controller restart and is not considered complete until
process liveness confirms it.

Worktree path, repository identity and branch are rechecked on child wake/recovery.
Changing a child's checkout to another branch causes launch to be refused. Failed
workspace setup is not retried automatically; partial folders/branches are kept
for inspection. The registered request and optional parent snapshot live under
`$SWARM_HOME/spawns/<child-id>/`. Reusing the same key returns the existing child,
including its error state, rather than creating another worktree or launching twice.
For a ready child whose Pi launch failed, use explicit `agent wake` after fixing
the runtime; an incomplete workspace must be inspected and repaired separately.

## Adopt an existing pi session

Quit the original pi process first (not just its terminal view). Then:

1. Open **Adopt session → pi session**.
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
own session directory. When Swarm runs inside WSL, it also checks the current
user's Windows Pi sessions under `/mnt/c/Users/<user>/.pi/agent/sessions`
(and `USERPROFILE` when available), mapping Windows workspace paths back into
`/mnt/<drive>/...` automatically. Because pi's provider-side connection state is
not portable across Windows/WSL, adopting one of those Windows-authored sessions
creates a local portable copy under Swarm before wake/resume. For other/custom
pi session directories:

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

## Compact large tool inputs without model calls

Use **Stop → Edit → Compact tool inputs** to move bulky historical tool arguments
out of active context. This is a separate, deterministic operation from
Meta-compact: it makes **no model calls** and does not summarize outputs.

For each eligible completed tool call, Swarm retains the first **100 characters**
of its serialized arguments, the tool name, target path when available,
original call ID, and a generated `toolrun_id`. Inputs qualify when they are
at least 1,000 characters or contain multiple escaped newlines; they are only
elided if the preview and retrieval instructions actually save context.
Incomplete, ambiguous or malformed call/result groups are left alone.

To avoid broken provider call/result pairing and replay signatures, affected
**completed exchanges become inert historical context**. In a multi-tool exchange,
small sibling calls retain their complete arguments. Tool-output text and image
payloads are preserved verbatim, with separate historical labels added; outputs
are never shortened. The current branch and effective context edits are retained.
The full original session, including other branches, is archived byte-for-byte.
Native Pi tool calls and Swarm's Windows-Pi imported transcripts are supported.

The compacted session contains instructions for reading one archived input:

```bash
"$SWARM_PYTHON" -m swarm tool-input toolrun_<job>_<call> --offset 0 --limit 8000
```

For a different registry, use `swarm --home /path/to/home tool-input ...`.
The response is JSON containing `input`, `next_offset`, `total_chars` and `eof`.
Offsets/limits are in characters, with at most 64,000 characters per request.
This **only reads arguments**; it never executes the archived call. Retrieve
additional pages as needed rather than loading the entire original session.
Checksums detect changed archived inputs.

Artifacts are kept under `$SWARM_HOME/tool-input-compactions/<job>/`:
`original.jsonl`, `replacement.jsonl`, `index.json`, `job.json`, and individual
`toolrun_*.json` input files. Lookup IDs map to the original entry ID and tool-call
position in the sidecar index; the original archive is never annotated or edited.
Treat these files as private session data and retain them as long as their
references remain useful.

### Revert tool-input compaction

Use **Stop → Edit → Revert tool-input compaction** to restore the exact original
archive, including its full inputs and other branches. Swarm verifies the archive
checksum, session identity and tracked path, saves the compacted session as a
`compacted-before-revert-<id>.jsonl` backup alongside the archive, then atomically
restores the original bytes. The agent remains stopped with auto-resume disabled;
Wake it explicitly afterward. This makes no model calls, and the original
context-size problem may return.

**By default, revert refuses if the session has changed since compaction**,
including new conversation turns or another kind of compaction. To intentionally
restore the old snapshot anyway, check **Allow discarding history added since
compaction**. This option is unchecked whenever Edit opens, and the confirmation
explicitly warns that newer history and other changes will be removed from the
active session. The **entire current session**, including that newer history, is
saved in the backup first. Nothing is merged into the original archive.

Even with the override, archive checksum/identity and stopped-process checks
remain mandatory. Writes occurring *during* the revert cause it to be refused:
the override only covers the current snapshot that was backed up. The report
records the opt-in and whether changed history was actually replaced. A repeated
no-op Compact tool inputs action retains the undo
metadata, and retrying an already completed revert does not rewrite the session.
Server restart reconciles interrupted restores by hash without repeating them.
Edit displays the restored status and compacted-backup path.

API: `POST /api/agents/<id>/revert-tool-input-compaction` returns the report with
HTTP 200, or refuses changed/live sessions with HTTP 409. Send
`{"allow_history_loss": true}` only to explicitly authorize dropping pre-existing
changes; omitted/false keeps the safe default. The flag must be a JSON boolean.
Invalid or corrupted archives are rejected without replacing the session. Revert
uses only Swarm's recorded archive; callers cannot supply a file path.

Both operations require a stopped managed Pi agent. Compaction checks for other session
owners, active processes, and source changes before atomic replacement. The
tracked session ID/path stay unchanged and auto-resume remains disabled after a
successful compaction. Failed or no-op operations do not replace the source.
Edit shows the affected-call count, estimated token savings, archive path and
errors; re-running on already shortened calls is a no-op. Output-heavy sessions
may still need Meta-compact.

API: `POST /api/agents/<id>/compact-tool-inputs` returns the result with HTTP 200.
The last result is also published as `agent.tool_input_compaction` in state.

## Recover oversized sessions with Meta-compact

If ordinary `/compact` fails because even its summarization request exceeds the
provider's context window, use **Edit → Meta-compact…**:

1. **Stop** the agent first, including any external Pi using the same session.
2. Open **Edit → Meta-compact…**. Optionally enter an exact `provider/model` ID.
   By default Swarm uses the agent model override or the model saved in the
   session; if neither exists, an explicit model is required.
3. Choose **Number of chunks** (default **2**, range **2–16**). Leave the shared
   overlap at **10,000 estimated tokens** per adjacent pair, or adjust it.
4. Confirm the model usage and replacement. Progress remains visible in Edit
   even if you close the dialog. After completion, **Wake** explicitly.

Swarm snapshots the saved Pi v3 session and projects its active branch, honoring
existing compaction boundaries and context edits. It splits that text into the
requested number of approximately equal chunks, with shared overlap between
adjacent chunks (estimated at four characters per token, capped at half a base
chunk for short inputs). Each chunk is saved to a file and supplied in full via
stdin to a separate, persisted Pi worker session. One final worker merges all
chunk summaries with instructions to remove duplicated overlap and
preserve unique facts. Tools, extensions, MCP, skills and context-file discovery
are disabled for these workers; historical commands are data, never executed.

This is **lossy summarization**, not exact compression. With N chunks there are
**N + 1 model stages** (N summaries plus one merge); provider retries/recovery
can incur additional requests and costs. More chunks reduce each input's size
but increase model usage. Swarm checks the selected model's advertised context
and rejects chunks whose estimated input exceeds 70% of it before summarizing.
It also checks a worst-case merge budget for all N summaries. Estimates are not
exact provider token counts: a model can still reject a chunk. Increase the
chunk count or choose a larger model if inputs do not fit; if the combined
summary budget is too large, use fewer chunks or a larger model. Sessions over
128 MiB are rejected; worker calls time out after 30 minutes each.

Only after all stages succeed and the result validates does Swarm archive the
byte-for-byte original and atomically replace the tracked session file with a
Pi compaction checkpoint. The session ID, tracked path and Swarm agent identity
are retained. The agent stays stopped with auto-resume disabled. Wake, edits,
Forget and duplicate jobs are blocked while compaction runs. Source changes or
newly detected external Pi processes cause replacement to be refused.

Artifacts are kept outside session discovery roots at:

```text
$SWARM_HOME/meta-compactions/<agent-id>/<job-id>/
  job.json              # persisted stage/status and hashes
  split.json            # chunk ranges and overlap boundaries
  chunk-1.txt … chunk-N.txt             # inputs with marked overlap
  chunk-1.jsonl … chunk-N.jsonl, merge.jsonl  # Pi worker sessions
  summary-1.txt … summary-N.txt, combined.txt
  original.jsonl        # exact original archive (source.jsonl before commit)
  replacement.jsonl    # validated replacement
```

Worker event streams and stderr logs are retained there too; treat all artifacts
as private session data. Failures leave the source unchanged. Server shutdown
cancels workers; restart reconciles any completed replacement and marks other
interrupted jobs failed, without repeating paid requests. To restore manually,
stop the agent and all other writers, back up its current tracked file, then
copy `original.jsonl` back to that path. Restoring the archive also restores its
original context-size problem.

API: `POST /api/agents/<id>/meta-compact` with
`{"model": "provider/model", "overlap_tokens": 10000, "chunk_count": 4}` returns
a job with HTTP 202. `chunk_count` defaults to 2 when omitted. The model is
optional when saved metadata supplies it. Job status is published
as `agent.meta_compaction` through the normal state API/WebSocket.

## Import a Grok Build session into pi

Open **Adopt session → Grok session**, choose a saved conversation, check its working
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
  relay-settings.json # protected relay profile/key and connection intent (if configured)
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
- `GET /api/config/models`: models available to the configured Pi executable; `GET /api/folders`: one level of the server-local directory picker.
- `POST /api/controller/restart`: send a response, then replace only the controller process in place; tmux-owned agents remain running.
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
node --check src/swarm/static/terminal-touch.js
node --check src/swarm/static/terminal-keys.js
node --check src/swarm/static/terminal-copy.js
node --check src/swarm/static/dashboard-controls.js
node --check src/swarm/static/org-chart.js
node --test tests/frontend/*.test.cjs
```

When Node is installed, pytest also runs the dependency-free frontend tests for
card rendering/disclosure, automatic defaults, persisted overrides, storage failures,
Edit/Forget confirmation and server-race error handling. These use a minimal DOM
contract and are not a substitute for a real browser layout/keyboard check.
The org-chart tests also cover filters/ancestor retention, missing and cyclic
parent links, deterministic order, and deep hierarchies without recursive traversal.

Optional Chromium layout checks use synthetic agents and an isolated static server,
not your running Swarm or real sessions:

```bash
pip install playwright
python -m playwright install --with-deps chromium
pytest -q tests/test_dashboard_browser.py
```

These test desktop/mobile tile dimensions, non-overlap, scrolling, expand/collapse,
activity tooltips, filtered lineage, keyboard focus after updates and safe text
rendering. They skip when Playwright or its browser runtime is unavailable.

Tests cover record safety/concurrency, snapshots, recovery rules, one-shot timers,
adoption/import safeguards, Grok source preservation and conversion, worktree
isolation, live file updates, API access checks,
inactive-agent forgetting with history preservation, and **real tmux terminal
input/detach/server-restart survival** with a fake pi
(no provider credentials or paid model calls). Real pi/provider interactions and
OS-specific boot/login behavior still need testing in your own environment.

Terminal dependency versions and licenses are in
[`src/swarm/static/vendor/README.md`](src/swarm/static/vendor/README.md).
