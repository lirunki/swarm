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

### Interactive setup: no activation needed

```bash
# Run from any working directory; keep the script with its source checkout.
bash /absolute/path/to/swarm/setup_swarm.sh

# Answer configuration questions and write files, but do not start services:
bash /absolute/path/to/swarm/setup_swarm.sh --no-launch

# Explicit installation/config locations (still asks before installing):
bash /path/to/swarm/setup_swarm.sh --runtime-dir /path/to/runtime --config /path/to/setup.json

# Dependencies only, without the configuration wizard or service launch:
bash /path/to/swarm/setup_swarm.sh --runtime-dir /path/to/runtime --install-only
```

`setup_swarm.sh` replaces the earlier noenv wrapper. It **does not invoke the
personal `launch_swarm.sh`**, source `.env`/shell startup files, or assume a relay
hostname, existing certificate/key, checkout virtualenv, or registry folder.
It suggests paths, but asks the user to select/approve them:

1. **Runtime installation directory** and permission to install dependencies.
   Missing Python 3.11+ with venv/pip, tmux 3.2+, Git, OpenSSL and CA packages are
   installed using apt/dnf/pacman or an existing Homebrew installation. The prompt
   explicitly identifies system-wide changes and possible `sudo` use.
2. **Configuration JSON path** and **Swarm registry/data directory**.
3. Whether to configure a local **repeater** (`swarm relay serve`), a Swarm
   **dashboard/server**, both, or neither. Bind addresses/ports are prompted;
   loopback is the safe suggested bind. A relay-only installation does not require
   configuring or starting a dashboard.
4. For TLS, whether to **generate self-signed certificates if absent** or reuse
   selected certificate/key files, their paths, client-visible DNS/IP SANs and
   validity. Existing identities are never silently replaced; a half-existing pair
   is rejected. Dashboard HTTPS is optional on loopback and required for LAN binds.
5. For a local repeater, generate a private random API key or choose an existing
   key. For an **existing external repeater**, enter its explicit `wss://` endpoint,
   provide the operator's existing key through a private file or hidden terminal
   prompt, and choose system CA trust or a supplied CA/public certificate. Setup
   does not generate a random key and pretend the external repeater accepts it.
6. Choose a browser password (required for network/relayed access), Pi node name/ID,
   and explicit opt-ins for saved-agent recovery and remote restarts. Credentials
   are entered without echo, not as command arguments. Existing dashboard relay
   Settings can override CLI connections, so using such a registry requires an
   explicit acknowledgement; setup never silently overwrites those settings.
7. **Ask separately whether to start the repeater and the server now.** Defaults
   are no. The chosen launch roles are saved, not inferred from all configured
   services. `--no-launch` never starts anything and preserves an existing saved
   launch selection.

The selected runtime contains `venv/`, optional `node/`, and optional `pi/`; it need
not be inside the checkout. Swarm Python packages stay in this venv. A working Pi
already on PATH is reused; otherwise a private pinned
`@earendil-works/pi-coding-agent@1.2.0` is installed with npm integrity checks and
`--ignore-scripts` (`SWARM_PI_VERSION` accepts an explicit numeric override).
If suitable Node 22.19+ and npm are unavailable, official Node 24 LTS is downloaded
over HTTPS and verified against nodejs.org's SHA256 manifest **before** bounded,
safe extraction. No global Node/Pi or Pi settings are replaced. Normal Pi provider
login is still required; setup makes no model requests. Internet access/package
caches and supported OS repositories are required; no extra repositories or remote
shell installers are added. `SWARM_BOOTSTRAP_PYTHON` selects an existing interpreter.
Native Windows continues to use `swarm-windows.sh` and its private Cygwin setup.

The JSON config is written atomically with **0600** permissions and stores absolute
paths, roles and policies—not secret values. API keys/passwords/private keys live
in separate owner-only files. It also writes `<config-name>.run.sh` beside the
configuration, containing only safely quoted paths:

```bash
# Launch exactly the roles saved during setup:
bash /path/to/setup.run.sh

# Explicitly choose a later launch role, even if setup saved "none":
bash /path/to/setup.run.sh --roles server
bash /path/to/setup.run.sh --roles relay server

# Validate saved JSON without launching:
bash /path/to/setup.run.sh --no-launch
```

Reusing a config with a different newly selected runtime/source/Pi asks for an
explicit update rather than silently launching stale code. Keep the source checkout
and runtime available; the reusable launcher verifies its runtime/source association.
Rerun setup with `--config` to reuse or intentionally reconfigure an installation.
`--yes-install` skips **only** installation consent; it does not skip configuration
or authorize a server/repeater launch. Runtime setup uses `setup.lock`; inspect a
stale lock after a hard crash before removing it. Invalid environments are not
silently deleted/recreated, and symlink runtime roots are rejected.

Requested services remain foreground-supervised. Occupied ports are rejected;
existing listeners are never killed or blindly reused. Ctrl+C/TERM/HUP/QUIT shuts
down only children started by that launcher; after a graceful timeout, only those
owned service processes are forcibly stopped. It does not stop unrelated services
or tmux-owned agents. Stop an existing controller before installing into its active
runtime or launching a second controller for the same registry.

**TLS is not authentication.** Self-signed public certificates must be explicitly
trusted by browsers/remote controllers; setup never disables TLS verification,
installs machine-wide trust, changes firewalls, or exposes a public bind silently.
Never copy the TLS private key to clients or publish credential files. JSON and
launch-script paths are rejected if they collide with credential/TLS paths.

The older personal launcher still supports an environment override independently:
`SWARM_RELAY_HOST=relay.example.com bash /path/to/swarm/launch_swarm.sh`.
It is not used by the interactive setup and its personal defaults are not imported.

### Manual launch

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
# Restrict network mode to one or more exact client IPs (repeat the option):
swarm serve --host 0.0.0.0 --allow-ip 192.168.1.25 --allow-ip 2001:db8::25
```

Remote clients use `http://<server-ip>:<port>`; `--open` still opens the local
`127.0.0.1` URL. In this opt-in network mode, Host headers are unrestricted so
LAN IP addresses/hostnames work; browser same-origin checks remain enabled.
Repeat `--allow-ip IP` to reject HTTP and WebSocket requests from every other
client IP; these are exact addresses, not CIDR ranges. The filter uses the direct
socket peer address (forwarded proxy headers are ignored when it is enabled).
New peer endpoints are logged as accepted (`INFO`) or rejected (`WARNING`); repeat
requests on the same peer endpoint do not generate duplicate allowlist logs.
Loopback mode retains its strict Host checks. Network mode also removes the
default Host-based DNS-rebinding protection; same-origin checks are not authentication.

**Without `--password-file`, anyone who can reach the port can read private data
and control agents with your user account's permissions.** Optional browser password
authentication is described below. Use HTTPS and restrictive firewall rules even
when a password is configured. Swarm does not configure your firewall automatically
and remains a single-user tool, not an Internet-facing multi-user service.

Run **one controller per registry**, with one Uvicorn worker (the CLI enforces
the latter). It is a single-user application, not a multi-user server, a sandbox,
or an authentication boundary against other local processes.

### Optional browser password

Start each controller with its own private, one-line UTF-8 password file:

```bash
# Create this file with your editor; do not put the password on a shell command line.
chmod 600 /private/path/swarm-browser-password.txt
swarm serve --password-file /private/path/swarm-browser-password.txt
# For LAN access, add --host 0.0.0.0 and use HTTPS certificate options below.
```

The password file must be a regular non-symlink file, with a nonempty password of
at most 1,024 characters. Keep it private to its owner (0600 on POSIX); mode-bit
enforcement is currently disabled for compatibility, so configuring safe permissions
is your responsibility. It is read once at startup; restart to change it. Missing,
unreadable, empty, oversized or symlink files fail startup rather than silently
disabling authentication. Do not use an API key
or relay credential as the browser password. Use a strong, unique password.

The browser shows a password dialog before loading private state or controls.
Successful sign-in creates a random bearer session; **authorization is held only
in JavaScript memory**, separately for each Swarm. No auth cookies, localStorage
or sessionStorage are used. Refreshing the page or opening a new page requires
sign-in again. Password-manager behavior is up to your browser; Swarm itself does
not save the password. Backend restart invalidates sessions; server-side sessions
also expire after 24 hours and are bounded in number. Failed sign-ins are rate
limited per controller. Authentication response bodies and private APIs are not
cacheable.

**Relay access uses the destination's password too.** Unlocking the local gateway
or possessing the relay's API key does not unlock a password-protected remote Swarm.
Selecting a protected remote prompts for that server's password. Each node enforces
its own HTTP, file, state-stream and terminal checks. Protected fleet state is not
broadcast to unauthenticated peers; cached state does not authorize a browser.
Session validity is rechecked for active sockets, and before forwarding remote state
updates, so logout/expiry cannot piggyback on another browser's authorization.
Older unprotected peers remain accessible; upgrade all peers before relying on this
feature to protect their data.

File Open/Download and WebSockets use 30-second, one-use, path-bound access tickets
because browser navigation/standard WebSockets cannot set bearer headers. Passwords
and bearer sessions never go in URLs. Ticket parameters are stripped before routing
and redacted from standard Uvicorn access/WebSocket logs; an already issued unused
ticket remains a narrow capability until it is used, expires or its session is
revoked. Existing third-party reverse proxies should redact query strings too.

Local agent automation still works without prompting: the running controller writes
a separate owner-only service credential to `$SWARM_HOME/api-auth/client.token`.
Managed agents receive `SWARM_API_TOKEN_FILE`, and local CLI clients read that file
(or the registry selected by `--home`) on each request, including after controller
restart. This credential is accepted only from loopback clients without a browser
Origin header, and **never as relayed browser authorization**. Anyone with access
to the same OS account/registry already has full agent privileges; this is not a
sandbox against local processes. Offline `swarm report` remains file-based and
needs no browser password or running controller. Never publish the password or
service-token files as products/references.

The relay API key authenticates controller-to-relay connections; the browser
password authenticates users to a controller. They are separate. Use HTTPS for
browser/LAN traffic and WSS for the relay: passwords are not safe over unencrypted
networks. The existing relay transport is not end-to-end encrypted between peers;
relay operators and authenticated local controllers remain trusted intermediaries.

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

TLS encrypts traffic; it does not enable authentication by itself. Add
`--password-file` for the browser password gate. The network/firewall guidance above
still applies. Prefer a publicly trusted certificate for production relays.

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

Open the **bell → Notifications → Settings** to configure the **local controller**.
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
- The dashboard uses a slim **Mission Control** toolbar: the state multiselect
  sits under the label on the left and **+ New agent** on the right. Text search
  lives inside the state dropdown; `/` opens it and focuses search. A small search
  marker indicates a text filter remains active after the menu closes. There is
  no global Collapse all button; cards keep their independent expand controls.
  The large hero and redundant fleet headings are omitted so tiles start below
  the controls. Workspace, relay/settings and terminal navigation stay available.
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
  source folder's branch. Nested branch names such as `feature/subbranch` keep their
  Git name; only the sibling directory name replaces the slashes. Prefer a separate worktree per coding agent to prevent concurrent
  Git edits.
- **Background creation:** clicking Create immediately persists and displays a
  **Setting up** card. Workspace validation, branch/worktree checkout, optional
  child-context preparation and Pi startup run in the agent's managed **tmux pane**,
  not inside the HTTP request or the controller's global lock. Compact cards show
  the current stage (for example **Creating worktree…**); expand for setup errors
  and the private log path, or open **↗** to watch live Git output. A slow checkout
  does not block the fleet dashboard or hold a browser/relay request open.
- **Compact org chart:** agents appear as 128×128 square tiles containing only
  their name, status, expand arrow and a **↗** terminal shortcut directly beneath
  the arrow, plus their latest product link when one is
  advertised. Hover over the tile/name to see current
  activity; a red name means a live process, independent of goal status. Cards
  default to compact even when status changes; saved manual expansion choices
  still take precedence; use each card's expand control to change it independently.
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
  are remembered locally. **Dashboard** stays pinned at the left while agent tabs
  scroll horizontally. Agent tab titles use the same status colors as their cards;
  the active title has an orange outline (the former underline extended around it).
  Hover/accessibility labels include the status. The terminal toolbar is a single 28px row (about half
  its previous height), with long titles/metadata ellipsized and available on hover.
  Its buttons retain 24px click targets. It keeps **Reconnect**; use the tab's
  **×** to close it. Wake remains available on the agent card and in the offline
  terminal overlay, rather than as a redundant toolbar button.
- **Closing a terminal tab, the browser, or Swarm does not stop agents.**
  No hidden agent launch happens when a tab opens.
- The Swarm picker sits beside the logo/bell cluster, without a redundant small
  label. Its dropdown is 136.5px wide (1.3× the previous 105px); long workspace names
  remain available in the option list and selected-value tooltip, with an accessible
  “Active Swarm” label. Header controls stay on one line when space permits.
- **Bell → Notifications → ⋯ Master control** opens a preview/confirmation panel for restarting live agent
  processes on the selected Swarm. It is separate from backend restart; see the
  [graceful fleet lifecycle](#master-control-restart-awake-agent-processes) below.
- **Bell → Notifications → ↻ Restart active Swarm** has an accessible backend-restart
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

**Esc · Tab · Ctrl · Alt · ← · ↑ · ↓ · → · Home · End · Sup**

The complete row is centered within **90% of the current visible width**. Labels
scale to fit on narrow screens; no horizontal scrolling is required.

- **Ctrl** and **Alt** toggle a one-shot modifier for the next typed character or
  accessory key; tap both for a combination. They reset after a key, when the
  keyboard hides, when focus leaves the terminal, or when the connection is unavailable.
- Arrow keys respect the terminal's normal/application cursor mode and modifier
  combinations. **Home** and **End** go to the beginning/end of the line in the
  terminal application. **Sup** sends forward Delete (`Delete`), not Backspace. **Tab** sends a terminal
  tab without moving browser focus.
- **Esc** and control shortcuts are real terminal input: Ctrl+C can interrupt an
  application just as it does with a physical keyboard. No keys are queued while offline.
- Button taps retain input focus, keeping the software keyboard open. The terminal
  resizes above the key bar rather than being covered by it. When the software
  keyboard hides, the active connected mobile tmux view reconnects once after the
  viewport settles, refreshing its full-size screen without restarting the agent or
  reopening the keyboard. Dashboard, copy mode, dialogs, offline/closed tabs and
  desktop browsers do not trigger this reattachment.
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
are grouped under **+ New agent → Adopt session → pi session / Grok session**.
The adoption menu sits at the bottom-left of the creation dialog, aligned with
Cancel and Launch agent; it opens upward and switches to the selected adoption
flow without submitting the create form. Registration/import behavior is unchanged.

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

### Agent setup lifecycle

Dashboard creation and `swarm spawn` return **HTTP 201 with a queued record** before
setup has finished. Workspace validation errors (existing branch/destination,
invalid base, conflicting nested branch, inaccessible source, etc.) appear on that
same card rather than losing the agent behind an HTTP 500. No Pi work starts until
the workspace and session are ready. The form returns to Dashboard immediately;
setup errors do not silently retry the create request or create duplicate cards.
Filesystem/process failures that still occur on the API path return JSON details;
if the response is uncertain, inspect the cards before submitting another creation.

The controller starts a worker on its next monitor pass. It starts in the registry
folder because the destination may not exist yet, provisions the workspace, then
execs Pi in the **same pane**, with the correct working directory and agent identity.
A close-on-exec readiness lock keeps Wake, child messages and file Insert out of
the setup process until Pi takes over. Stop is available during queued/active
setup; it cancels pending launch and terminates the owned pane without deleting
partial branches/folders. Incomplete setup cannot be woken into the parent folder.

Git worktree checkout has a 30-minute worker timeout, not the old 30-second HTTP
helper timeout. Git output and stages remain in the pane and in
`$SWARM_HOME/setup/<agent-id>/setup.log`; `request.json` records the private setup
plan and initial instructions. Git hooks are disabled during provisioning and
provider/cloud credential environment variables are removed from Git children.
This is not a sandbox against repositories with user-installed Git filters or
other access to the same local account.

Setup survives a controller-only restart because tmux owns the worker. Queued
requests, or a worker-launch interruption before setup began, may start after
restart (at most three terminal-launch attempts). Once setup has begun, a missing
worker is marked interrupted instead of replaying uncertain Git side effects.
Inspect the retained artifacts before creating a new destination/branch. A valid
workspace whose Pi startup failed can be woken explicitly after fixing Pi; Git
checkout is not repeated. No automatic rollback or worktree deletion is performed.
Up to 16 dashboard setups can be queued at a time; normal child spawning limits
still apply. Setup plans and logs contain user instructions and should be treated
as private session data. Automatic model work is never performed by the setup
worker; only the requested Pi launch starts the agent.

## Master control: restart awake agent processes

Open the **bell → Notifications → ⋯ Master control** button to view the fleet
restart panel. It never restarts anything by opening the panel. Review the process
snapshot, select targets, then confirm **Restart selected**. Cancel is initially
focused; once a job exists the footer says Close, which only closes the panel.
**Skipped** and **Restart job** are collapsed-by-default cards: their headers keep
skip counts and job status/counts visible. Expand either header (mouse or keyboard)
to see the same full reasons, target stages/errors and explicit actions. Live
refresh/polling preserves your expansion; reopening Master control starts compact.

“Awake” here means a **live, managed Pi process** on the currently selected Swarm.
This includes live terminals whose goal status is idle, waiting, done or dormant.
Stopped agents are never started by the batch. Observed agents, incomplete setup,
compaction, pending stops, missing/mismatched session history and existing restart
reservations are skipped with reasons. Names and process-bound tokens are frozen
by the preview: a changed/replaced process requires a new preview and confirmation,
even when the agent UUID is unchanged. Other Swarms are not included automatically.

### Graceful default

1. Persist the job and per-agent reservation under `$SWARM_HOME/restarts/`.
2. Pause automatic wakes and new mailbox delivery without clearing their intent.
3. Ask the mailbox extension to **prepare**. Pi reports ready only when idle with
   no pending messages/commands. Preparation never exits the process.
4. Persist a shutdown **commit**, then let the extension call native
   `ctx.shutdown()` (the supported graceful exit API), not editor/keyboard input.
5. Observe the captured process actually exit. An acknowledgement alone, a failed
   tmux query or a missing pane is not proof of exit.
6. Reopen the exact tracked session/workspace **without a user prompt** and verify
   the replacement mailbox is connected.

The agent ID, session ID/file, goal, checkpoint, status, auto-resume setting,
`wake_at`/pending wake delivery and child waits are preserved. Latest user/agent
reports are not overwritten with an older metadata snapshot. Existing schedules
and previously queued mail may resume authorized work once restart finishes; the
restart itself submits no new work. **Unsent terminal drafts can be lost when Pi
exits.** This is process restart, not `/new`, `/fork` or a new worktree.

Jobs survive backend restarts. A safely unstarted launch can resume; once a new
launch epoch was persisted and the outcome is unknown, Swarm does not blindly
repeat it. Failed/uncertain dead launches pause automatic recovery and require
explicit attention/Wake. Up to two replacements launch per monitor cycle. The
last 100 terminal job records are retained (active/orphan leases are not collected),
and the API/panel lists the most recent 20 jobs.

Tmux identity inspection is fail-closed. Relaunch respawns only the captured dead
pane without `-k`, or atomically creates a missing session with a startup barrier.
It never unconditionally kills a reusable session name. Native readiness is checked
before releasing the reservation. A malformed/missing job leaves only that agent
paused with a repair warning; other fleet work continues and explicit Stop remains
available.

### Attention and explicit fallbacks

- The default five-minute attention deadline **never kills or auto-forces**. A
  blocked job can still finish when Pi becomes idle/exits later. Use Cancel
  pre-commit to withdraw waiting targets. Once exit is committed it cannot be
  recalled. Failed prepare cleanup keeps cancellation pending rather than claiming
  success while leaving Pi's mailbox paused.
- **Legacy /quit…** is a separately confirmed, one-time fallback for older agents
  without the shutdown-capable extension. It types `/quit` and Enter into the
  captured original pane. **Check the editor is empty first:** an existing draft
  could be submitted instead of quitting, and active work may be interrupted.
  This action is never automatically retried after an uncertain outcome.
- **Force restart…** has its own warning. It terminates only the captured owned
  pane after revalidation, never an arbitrary PID or replacement process. Tool
  calls/external effects may already have happened and cannot be rolled back.
- Normal **Stop** cancels the restart's relaunch intent and stops that agent using
  ordinary Stop semantics (including disabling auto-resume). Wake, edits, Forget,
  compaction and file Insert are blocked while the restart reservation is active.
- Through federation, mutating fleet actions require the destination's
  **`--allow-remote-restart`** opt-in as well as its browser authentication. Preview
  and job viewing can remain read-only. This opt-in now covers fleet restart and
  backend restart; ordinary individual agent controls retain their existing rules.

These are operator actions, not arbitrary slash messages or model-callable `/quit`.
The self-command tool continues to reject `/quit` and session-switching commands.
No batch runs merely because the UI or controller was updated. Restart the updated
backend and refresh the dashboard to activate the panel; upgrade/opt in remote
controllers separately when needed.

### Fleet restart API

- `GET /api/fleet/restarts/preview`: eligible process-bound targets and skip reasons.
- `POST /api/fleet/restarts`: `{request_id: UUID, targets: [{id, token}],
  timeout_seconds: 300, confirmed: true}` returns HTTP 202 with `{job, reused}`.
  Reusing an ID with identical input returns the same job; different input is refused.
- `GET /api/fleet/restarts` / `GET /api/fleet/restarts/{job_id}`: progress/history.
- `POST /api/fleet/restarts/{job_id}/cancel|legacy|force` with
  `{agent_ids: [UUID], confirmed: true}`: explicit target-scoped actions.

Only literal JSON `true` counts as confirmation. The target list is bounded to
128 and a persisted job to 2 MiB. Jobs expose stage/error/counts without their
private process/history binding data. They are also published through the normal
state/WebSocket stream as `restart_jobs` and per-agent `restart` metadata. Keep
request IDs after uncertain HTTP outcomes and inspect the saved job before retrying.

## Agent Files library (references and browser uploads)

Agents can register **multiple files** without copying payloads into Swarm. This is
separate from the single latest-product advertisement below: adding another file
never replaces earlier references or the featured product.

```bash
# Inside a managed agent, using its existing SWARM_AGENT_ID/API URL:
"$SWARM_PYTHON" -m swarm file add /tmp/chart.png --label 'Experiment chart'
"$SWARM_PYTHON" -m swarm file add /absolute/project/artifacts/report.pdf --label 'Research report'
"$SWARM_PYTHON" -m swarm file list
"$SWARM_PYTHON" -m swarm file remove FILE_UUID

# Operator targeting a particular agent:
swarm file --id AGENT_UUID list
```

A **Files N** button appears on compact/expanded cards with registered entries;
**Files** beside **Reconnect** in each terminal opens the same browser. Its text is
white when the library is empty and yellow when it has registered files; both
controls update with live file counts without fetching file contents. The terminal
need not be awake. The list shows labels, filenames, sizes/types and availability,
with Open, Download, Insert and removal actions. Raster images and PDFs open in a
new browser tab; other/active formats such as HTML and SVG download rather than
execute inside Swarm. Download always forces a download. **Remove reference never
deletes an existing server original.** Browser uploads instead offer **Remove
upload**, which explicitly deletes their owned temporary copy (never the original
on your browser's computer).

### Send a browser-local file to an agent

1. Open **Files** beside Reconnect in the agent's terminal (available even when its
   library is empty), or use the card's Files button.
2. Click **Upload** and select a file on the browser's computer. Swarm copies it
   into a private subdirectory of the **selected agent machine's system temporary
   folder** and registers it in that agent's Files library. It does **not** paste,
   submit a prompt, or wake the agent.
   You can also copy an image/file and press **Ctrl+V / ⌘V inside Files**, or click
   **Paste**. The clipboard item uses the same upload flow; it does not insert or
   submit anything to the agent. Nameless clipboard images receive a timestamped
   filename such as `clipboard-<timestamp>.png`.
3. Click **Download** to retrieve the uploaded copy later.
4. Click **Insert** when you want to refer to it in your conversation. Swarm pastes
   a quoted absolute path into that agent's existing tmux input, preserving your
   draft and **never pressing Enter**. The dialog closes and its terminal opens;
   continue typing and submit yourself when ready. Native Windows Pi receives a
   translated Windows path, not a controller-only POSIX path.

Clipboard upload accepts **one image/file at a time**, only after an explicit
paste gesture. The Paste button requires browser clipboard-read support and usually
HTTPS/localhost plus permission; keyboard paste is an alternative when the button
is unavailable. Browsers may convert copied images to PNG. Some browsers expose
Explorer/Finder copies only as text paths rather than file bytes—use Upload in that
case. Plain-text, URL and HTML clipboard formats are not turned into uploads or
fetched automatically (a copied text/HTML file is still a file). Normal
pasting into text fields or the terminal outside Files is unchanged. Closing Files
or switching agents/workspaces while permission is pending discards the stale read.

Insert works for previously registered server files too, requires a live managed
agent, and is blocked while it is stopping or compacting. It does not read file
contents or start an LLM turn. If delivery cannot be confirmed, inspect the
terminal before retrying—Insert is never automatically retried.

Uploads and insertion route to the selected local or federated Swarm, not the
browser machine or whichever peer is selected later. Files are transferred as
bounded 192 KiB chunks, below the relay's 512 KiB request limit. No file bytes are
stored in browser storage or the receiving proxy's disk cache. Progress and cancel
controls are shown; closing/switching the dialog cancels unfinished work where
possible. A completed registration survives a lost response and cannot be deleted
by a late upload-cancel request. The browser remembers only pending upload IDs and
captured API paths in session storage to retry cleanup after reload.

Limits: 128 MiB per file, 64 references per agent, 8 active transfers per controller,
and 1 GiB of owned temporary uploads. Safe plain filenames are required. Unregistered
partial uploads expire after ten minutes of inactivity (cleaned when another
upload starts) and are canceled on controller restart; completed uploads remain.
Metadata is bounded to 4,096 records and 256 cancellation/expiry tombstones.
**Remove upload** reclaims its temporary bytes and reference; forgetting the agent
also removes owned uploaded copies, not other registered originals or session
history. This keeps the quota reclaimable through the UI. The copies live under
`<system-temp>/swarm-uploads-<registry-hash>/<agent-id>/<upload-id>/` and may also be
removed by OS temp cleanup. Move important results into durable workspace storage
before relying on them long-term. These are ordinary live files: later edits by
the agent are reflected by Download. Treat uploaded content as user-supplied data,
not instructions to execute merely because it was inserted.

### On-demand behavior and lifetime

- Registering an existing server file only resolves/stats the original and stores private path metadata
  under `$SWARM_HOME/files/<agent-uuid>.json` (mode 0600 on POSIX). No source bytes,
  base64 or payload snapshot are saved, and no payload is read when opening a list.
- Dashboard/relay state advertises only count/revision, not original paths or bytes.
  File contents are read **only when the user opens/downloads an entry**. Local
  responses stream binary; remote access uses the existing bounded base64/chunked
  federation transport, decoded back to binary HTTP for the browser. No LLM turn,
  agent wake or base64 tool-output dump is involved.
- References use resolved local paths. Updated originals are what the next click
  receives; there is no version archive. Registering the same resolved path updates
  that entry. Use distinct filenames if several versions should remain available.
- Deleted/moved files stay listed as unavailable; restoring the regular file at the
  same registered path makes it usable again. Replacement symlinks/devices are
  refused. Finish writing before publishing; changing a file during transfer may
  fail the read. Keep durable results in a workspace artifacts directory rather
  than a temp directory that will be cleaned up.
- A remote controller must be online to fetch its original file; there is no
  receiving-node disk cache. Browser responses are marked `no-store`.
- References survive controller restarts, but forgetting an agent removes its
  reference index and owned browser-upload copies (not other originals, worktrees
  or pi session files).

Limits are **64 entries per agent** and **128 MiB per file**. Only regular local
files can be registered. Requests serve validated agent/file UUIDs, never arbitrary
browser-provided paths. Safe preview MIME is detected from the content on fetch;
unknown/active content stays download-only. This is a fully trusted same-user mesh,
not a sandbox against peers/agents with filesystem access. Never register private
configuration, credentials, keys or unrelated data. Protect user-profile ACLs on
Windows too.

The local backend API is:

- `GET /api/agents/{id}/files`: metadata/current availability, no file payload.
- `POST /api/agents/{id}/files`: `{ "path": "/local/file", "label": "Result" }`.
- `DELETE /api/agents/{id}/files/{file_id}`: unregister an original; for an owned
  browser upload, also delete that temporary copy.
- `POST /api/agents/{id}/files/{file_id}/insert`: paste its agent-native quoted path,
  without Enter/wake; returns `inserted`, `file_id`, `path` and `text`.
- `POST /api/agents/{id}/files/uploads`: `{upload_id: UUID, filename, size}` starts
  an idempotent transfer, returning the current offset and maximum chunk size.
- `POST /api/agents/{id}/files/uploads/{upload_id}/chunks`: `{offset, data}` with
  Base64 chunk bytes; exact duplicate chunks are accepted, mismatched offsets fail.
- `POST /api/agents/{id}/files/uploads/{upload_id}/finish`: register a complete upload.
- `DELETE /api/agents/{id}/files/uploads/{upload_id}`: cancel an unregistered upload;
  completed registrations are retained.
- `GET /api/agents/{id}/files/{file_id}/content`: current bytes, safe inline preview
  or download; `?download=true` always downloads.

For a federated workspace the existing `/api/federation/{node}/proxy/...` route
handles the same URLs. After updating Python code, restart the controller once
(and refresh the browser) to load these new API routes. Restarting the controller
leaves tmux-backed agents running. An older controller displays a clear Files API
unavailable/update message rather than silently showing an empty repository.

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

## Global user notifications

The illuminated bell beside the top-left **swarm** brand is a durable global inbox.
It aggregates unread notifications from the local Swarm and every online Swarm
connected through the relay. Each row shows its source Swarm, agent, recorded state,
time and notification text. A partial-source warning is shown if an offline or
protected source cannot be read; one unavailable Swarm does not hide results from
others.

The inbox also contains **Master control**, **Settings**, and **Restart active
Swarm**. Master control/restart keep their active-Swarm scope and existing remote
permission checks; Settings always configures the browser's local relay connection.
These controls are no longer duplicated in the top-right header.

### Broadcast messages to agents

Type a message (up to 32,000 characters) in **Broadcast to agents**, then click
**Select & broadcast**. The checkbox picker offers:

- **Only this Swarm** or **All connected Swarms** (online sources only).
- **All agents**, **Only awaken**, or **Only non-awaken**, plus text search.
  Awaken means a live managed process, matching Master control—not goal status.
  Dormant/done processes may be live but still hold their messages until resumed.
- **Select all shown**, **Select none shown**, individual checkboxes and refresh.
  Only visible, checked managed agents are sent to; hidden selections never leak
  into a filtered broadcast. Observed agents cannot receive managed inbox messages.

The confirmation shows the exact target and Swarm counts. Sending queues durable
messages using the existing authenticated per-agent API; it **never wakes an agent**
or types into tmux. A captured draft/target/source snapshot prevents in-flight UI
changes from retargeting a message. Per-target results retain request UUIDs and
accepted/error/uncertain outcomes. Offline/invalid sources are excluded visibly;
partial failures do not erase the draft or blindly retry other targets. The session
remembers accepted/uncertain sends for the same draft across picker close/reopen;
inspect their request IDs rather than assuming a lost response means no delivery.
An API timeout is uncertain, not proof the backend rejected the message. Requests
still completing after a workspace change cannot schedule more recipients there.

### Required agent-to-user notifications

Agents **must** post a Swarm inbox notification whenever they need any action from
the user (including decisions, login, credentials, permissions, manual commands,
reviews or reloads), and whenever the user's requested work is genuinely complete.
Status changes, chat replies, ask-tool dialogs and OS desktop notifications are not
substitutes. Post only after the persisted record is in the appropriate state:

- `waiting`, with a nonblank `wait_reason`, when work requires a human answer,
  decision, approval or other input; or
- `done`, when that agent's overall human-assigned goal is genuinely complete.

```bash
# Ask for human input: report first, then notify once.
swarm report --status waiting --wake-at none \
  --wait-reason 'Choose the migration strategy.'
swarm notify 'Decision needed: choose the migration strategy.'

# Announce completion: final report first, then notify once.
swarm report --status done --wake-at none --activity 'Goal complete'
swarm notify 'Completed: migration implemented and validation passed.'
```

The inbox is intentionally not a progress feed. Agents must not notify for routine
checkpoints, recoverable errors, `running`, `idle` or `dormant` states, automated
waits, child updates, or completed subtasks. Notification text is limited to 32 KiB.
`swarm notify` defaults to `SWARM_AGENT_ID`, captures the source agent name/state,
and writes directly to `$SWARM_HOME/notifications.json`, so posting remains durable
while the web controller is down. `--request-id UUID` makes an exact retry
idempotent; reusing it with different agent/text content is rejected.

The source Swarm remains authoritative. `GET /api/notifications` lists unread items
and `DELETE /api/notifications/{notification_id}` removes one. Both routes work
through the existing authenticated federation proxy. Opening **Open agent** first
removes the source notification, then selects the correct Swarm. If that live agent
already has a terminal tab, the tab is activated; otherwise Swarm opens its
Dashboard, clears filters that could hide it, expands and scrolls to its card, and
highlights the card. Deleted notification receipts are retained in a bounded window
for request-ID safety; unread records survive controller and relay restarts.

## Durable agent messages and Pi self-commands

Use the mailbox API instead of typing into an agent's tmux pane. It is durable across
controller restarts and is the primary path for new agent-to-agent/operator messages:

```bash
swarm message send AGENT_ID 'Review the failing parser test.'
swarm message send AGENT_ID '/this remains ordinary text' \
  --request-id 6f485ccd-1a35-4eb7-a6d7-5aa15e9ba6fc --sender-id SENDER_UUID
swarm message list AGENT_ID
swarm message delete AGENT_ID MESSAGE_ID

# Queue a command for this managed agent (or use --id as a local operator):
swarm command '/compact retain the current implementation plan'
swarm command '/thinking high' --id AGENT_ID

swarm p                         # short alias for `swarm list` (all records)
```

`message send` creates a UUID request ID locally when it is omitted; `--sender-id`
defaults to `SWARM_AGENT_ID` and is null for an operator outside a managed environment.
`command` similarly generates its request ID and defaults `--id` to `SWARM_AGENT_ID`.
A managed process cannot target a different ID; an operator without that environment
must explicitly select one. The CLI uses the same bounded, authenticated loopback-only
client as other agent operations. It does not access tmux and never automatically retries
a mutating request. On a timeout the outcome is unknown: retain the printed request ID,
inspect `message list`/agent state, and reuse that ID only when retrying the same logical
operation.

The protocol is:

- `POST /api/agents/{id}/messages` with
  `{"text":"…","sender_id":UUID-or-null,"request_id":UUID}` returns HTTP 202 after
  durable persistence.
- `GET /api/agents/{id}/messages` lists outstanding records and their transport state.
- `DELETE /api/agents/{id}/messages/{message_id}` cancels a pending, failed, or uncertain
  record. A record already being delivered returns HTTP 409.
- `POST /api/agents/{id}/commands` with
  `{"text":"/compact …","sender_id":id,"request_id":UUID}` returns HTTP 202 and uses
  the same durable outbox. Command text is kept separate from ordinary message text.

An ordinary-message delivery acknowledgement means the text was persisted into Pi
context; it does **not** mean the requested task finished. Messages beginning with `/` are still enveloped
and delivered as ordinary text. Self-commands wait until Pi is idle and are never injected
as terminal keystrokes. Pi initially supports `/compact [instructions]`, `/model
provider/model`, `/thinking off|minimal|low|medium|high|xhigh|max`, `/name NAME`, and
`/reload`. Session/identity switching commands `/new`, `/fork`, `/clone`, `/resume`,
`/tree`, `/import`, and `/quit` are forbidden. Every other command is explicitly rejected,
not silently passed to the model as a prompt. Reload is treated as a safe action, but
failures and uncertain outcomes remain visible and are not blindly replayed. The generic
mailbox/CLI protocol is backend-neutral; native delivery and self-commands currently
use Pi's extension transport. Other backends need an explicit safe adapter, not blind
terminal injection. `/model` changes the live Pi session; a configured Swarm model
override is still reapplied on a later launch unless separately changed.

Delivery occurs only while an agent is **awake**: managed, alive and fully provisioned,
with status running/waiting/idle, and neither stopping nor compacting. Queueing never wakes it. Older Pi processes launched
without Swarm's trusted mailbox extension must be relaunched; their messages remain pending
with the transport state visible. Newly launched Pi loads that extension, including its
`swarm_session_command` tool for queueing its own supported commands.

Each agent is limited to 128 outstanding records, message text to 32,000 characters, and the queue to
about 2 MiB. Sender permissions use the existing same-user service authentication; this is
not a hostile-agent sandbox. If every agent is busy, use event-driven child `agent wait`, a
scheduled wake, or bounded mailbox polling with a delay/backoff—never a no-sleep agents
busy loop. A persistence outage leaves durable inbox entries available for later delivery.
The older direct-child `swarm agent message` route now uses the same durable queue,
while retaining its ownership checks; prefer the mailbox CLI for new work.

Queue data lives under `$SWARM_HOME/messages/<agent-id>.json`. Each Pi process gets
an epoch/session-bound private `$SWARM_HOME/control/<agent-id>/` bridge. No message
text is broadcast in fleet state; only outstanding counts/revisions are included,
and the card fetches bodies on demand. The compact envelope button opens the mailbox;
expanded cards list outstanding messages with Delete controls. These routes also
work through the authenticated federation proxy and require the destination's password.

Delivery is at-least-once across uncertain process/crash windows. Pi records stable
message IDs in saved context and deduplicates them on restart. Do not repeat external
side effects merely because an ID reappears. Up to 256 final receipt metadata entries
are retained for request-ID idempotency; always generate a new UUID for a genuinely
new request. Deleted/delivered records do not keep duplicate bodies in the queue.
Already delivering/executing items cannot be recalled (HTTP 409). If the bridge is
stale for over two minutes, an in-flight item becomes **uncertain**, not falsely
successful or automatically replayed. A late positive receipt can still resolve it.
Deleting an uncertain item removes tracking only; input/actions already accepted by
Pi cannot be recalled. Unknown command outcomes are never blindly re-executed.

The Pi `swarm_session_command` tool durably writes a bounded private self-request
before returning; it must finish the current turn before an idle-only command can
run. The controller can ingest that request after downtime. Native commands never
clear the user's editor, press Enter, or switch sessions. `/compact` can incur normal
model costs. `/reload` is acknowledged only by a new runtime reporting an actual
reload lifecycle; an interrupted startup is not proof of completion. Forgetting an
agent removes its mailbox/control metadata, not its session or worktree.

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

The spawn response initially contains a queued/provisioning child. Git/fork checks
and checkout run in its managed tmux terminal after the response. Inspect its stage
or use `agent wait`; a queued child is work in progress, not a completed child.
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
including its queued or error state, rather than creating another worktree or launching twice.
For a ready child whose Pi launch failed, use explicit `agent wake` after fixing
the runtime; an incomplete workspace must be inspected and repaired separately.

## Adopt an existing pi session

Quit the original pi process first (not just its terminal view). Then:

1. Open **+ New agent → Adopt session → pi session**.
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

## Automatically spill large Pi tool output to files

Swarm's trusted Pi mailbox entry point now loads `extensions/output-spill.js`.
For newly finalized, model-facing **tool-result text**, it joins all text blocks,
measures UTF-8 bytes, and archives results over the configured threshold **before
Pi saves the result or sends it in the next model request**. The replacement says
which local file contains the output, retains a bounded head/tail preview, and
provides paged retrieval instructions. Tool-call IDs, error flags, usage and other
tool metadata remain unchanged. Assistant answers/reasoning, user prompts and image
payloads are not intercepted. Nested programmatic tool calls keep their original
schemas/data; only the outer result committed to the conversation is transformed.

Defaults are **on, 1 MiB threshold, 4 KiB notice/preview target**. File configuration:

- Managed Swarm: `$SWARM_HOME/output-spill.json`, inferred from the agent-native
  `SWARM_CONTROL_DIR` (including native Windows path translation).
- Standalone Pi: `${PI_CODING_AGENT_DIR:-~/.pi/agent}/output-spill.json`.
- Override the config path with **`PI_OUTPUT_SPILL_CONFIG`** (absolute path).

```json
{
  "enabled": true,
  "maxBytes": 1048576,
  "previewBytes": 4096,
  "maxArchiveBytes": 536870912
}
```

The file is optional; missing means defaults. Edit it to change defaults for all
sessions using it; file changes are picked up on the next result/command. Malformed
or unknown configuration retains the last valid settings (or initial safe defaults)
and warns rather than silently disabling protection. A UTF-8 BOM is accepted.
No existing Pi `settings.json` is edited or overwritten.

### `/output-spill` controls

```text
/output-spill                         # status, paths and effective limits
/output-spill off                     # only this session
/output-spill on
/output-spill limit 128KiB
/output-spill preview 4KiB
/output-spill preview 0               # file notice only, no excerpts
/output-spill reload                  # reread file, retain session overrides
/output-spill defaults                # clear session overrides, follow the file
/output-spill save                    # explicitly persist effective defaults
```

`on/off/limit/preview` changes are session-scoped, saved as non-context custom
entries and restored from the active branch on `/reload` or ordinary session
reopening. They do **not** silently change other agents. `save` writes the selected
config file atomically, preserving its archive settings, then clears session
overrides so the file becomes authoritative. It refuses to clobber malformed or
unknown existing configuration. A cooperative `.lock` prevents concurrent saves;
a stale lock after an abrupt process death requires manual inspection/removal.
The threshold is 4 KiB–128 MiB; the preview target is 0–64 KiB and cannot exceed the
threshold. B/KB/KiB/MB/MiB suffixes are accepted (K/M shorthand uses binary units).
The mandatory file notice can exceed a very small preview target but never the
threshold. Keep the threshold comfortably below your provider's per-field limit.

### Archive location and retrieval

Default archives are private persistent files **under the actual Pi session
folder**, for both Swarm-managed and standalone sessions:

```text
<directory containing the active session .jsonl>/
  <session>.jsonl
  output-spills/
    session-<session-hash>/
      <output-id>.txt
      <output-id>.json
      <output-id>.meta.json
```

This follows custom/imported session-file locations too, rather than using the
working directory, Swarm home, or Pi's global config folder. If no session file
is available, Pi's session directory is used; if neither is available, archival
retains the original with a warning rather than writing elsewhere.
Set an absolute `archiveDir` in the config file, or **`PI_OUTPUT_SPILL_DIR`**
(which takes precedence), to explicitly choose another root. `save` does not
persist a derived session directory as a shared override. Existing archives in
previous locations remain readable; they are not automatically moved or deleted.

Each spill produces a UUID-named **`.txt`** joined output, **`.json`** full original
tool-result message, and **`.meta.json`** session/checksum manifest. The text file
is normal UTF-8; JSON preserves original block structure and escaped code units.
Files and directory entries are synced where the filesystem supports it.
POSIX directories/files are private (0700/0600); on Windows choose a private
location with appropriate inherited ACLs. No files are automatically published to
the Files library or as products, since tool output may contain sensitive data.
Images and tool-specific `details`/nested metadata are preserved in the session
for rendering/state compatibility; a tool that duplicates its output in metadata
can therefore still make raw history large. That metadata is not model text.

The new read-only model tool **`swarm_read_output`** accepts an exact generated
`.txt`/`.json` path, optional **byte** `offset`, and `limit` (default 8,000, max 64,000).
It returns a bounded page with visible `next_offset`, `total_bytes` and `eof`.
Follow `next_offset` for UTF-8-safe paging; do not dump the whole archive back into
context or rerun the original tool merely to recover output. Pages are reduced
when necessary to remain below the current threshold and cannot recursively spill
themselves. Arbitrary paths, other-session references and symlinks are rejected.
Checksums are verified once per unchanged file identity, with page-read stability
checks; altered archives are rejected. Historical roots are retained in the bounded
private `<config-path>.roots.json` sidecar so same-session references can survive
manual/offline compaction and a later archive-root change. Retain those manifests
and archive files while references may be useful; there is no automatic deletion.

Large strings are streamed to archive files, with bounded JSON metadata serialization,
rather than making multiple complete output buffers. Total archives are capped at
`maxArchiveBytes` (at most 512 MiB), and complex JSON metadata has a 64 MiB skeleton
safety limit. **If archival cannot be completed, the original result is retained
and a warning is shown** (stderr in non-UI modes). This favors data preservation;
it can mean the provider still rejects that result. It never claims a nonexistent
file, retries the tool or makes a summarization/model call. If appending the session
reference has an uncertain outcome, already-durable files are kept rather than
breaking a possibly committed reference.

### Activation and limits

Existing mailbox-capable managed Pi sessions can load this through **`/reload`**;
new launches load it automatically. Agents predating the mailbox need one relaunch.
For standalone use:

```bash
pi --extension /path/to/swarm/extensions/output-spill.js
```

Do not load the standalone copy a second time when Swarm's mailbox already owns it.
Other extensions can rewrite results later; place it after other output rewriters
when composing standalone extensions. Partial streaming UI events are not filtered.
This prevents **new** giant tool text; it does not rewrite existing oversized
history. Use the separate Edit repair below for the old Copilot error. It is not
reasoning suppression or whole-conversation compaction.

## Fix oversized tool-output fields

An error such as:

```text
Invalid 'input[7].output': string too long.
maximum length 10485760, got 23318071
```

is a **single output-string limit** in the Copilot API, not necessarily the model's
aggregate context-window limit. Ordinary `/compact` or changing model is not a
reliable fix when the same giant tool result is still replayed.

Use **Stop → Edit → Oversized tool outputs → Fix output limit…**. Confirm the archive/preview operation, then explicitly
**Wake** and retry/continue your prompt. The action makes **no model calls** and never
automatically wakes or submits a prompt. It requires a stopped, managed Pi agent
with a valid saved session. Other Pi writers in the same workspace and shared
session ownership must be stopped/resolved first. Setup, restart and other maintenance
operations block repair.

Swarm examines the active branch's effective model context, honoring existing
compactions and context edits. Each recognized tool-output text field **over 1 MiB
of UTF-8 text** is archived and replaced in future context by a preview of **at most
32 KiB total**, including the retrieval instructions and reference. It combines
all text blocks of a result before checking the threshold, just as the provider
serializer does. The conservative byte budget stays well below Copilot's
10,485,760-character limit, including for Unicode output. Head/tail excerpts are
retained; the omitted middle can be retrieved in bounded pages.

The repair is **append-only**: original file bytes, branches, tool arguments,
call/result IDs, error flags, images, reasoning and other metadata are retained.
New `context_edit` entries change only future model-visible content. A non-context
metadata entry records the repair; no user-like guide message is inserted after a
pending call, so incomplete exchanges keep their original order. Each preview
contains its own recovery instructions. Native Pi results, Swarm's historical
Windows/Grok tool-result imports and input-elided historical outputs are supported.
Unknown large user/assistant messages and binary image payloads are not shortened.

The action also saves a byte-exact full original at:

```text
$SWARM_HOME/tool-output-repairs/<repair-id>/
  original.jsonl
  replacement.jsonl
  index.json
  job.json
  toolout_<repair-id>_<entry-hash>.json
```

The extracted output archive contains the exact joined text that would have been
sent as the tool-output field; full original block structure remains in
`original.jsonl`. These are private session artifacts, not automatically published
products or library downloads. Keep them while their references are needed.

The agent can read **one output** without dumping the whole session:

```bash
"$SWARM_PYTHON" -m swarm tool-output TOOL_OUTPUT_ID --offset 0 --limit 8000
```

Offsets/limits are characters. The JSON response contains `output`, `next_offset`,
`total_chars` and `eof`; the maximum page is 64,000 characters. Each archive payload
is checksum-verified. This is read-only retrieval, never a request to rerun the
historical tool. Do not print the whole original output back into the conversation,
which would recreate the failure.

Before replacement Swarm rechecks agent/session ownership, maintenance/restart state,
liveness, external writers and the source hash under the agent record lock. Failures
leave the source unchanged and retain any created archive. Replacement is atomic;
restart reconciles interrupted commits by hash without repeating the operation.
The session ID/path and checkpoint remain unchanged. Automatic recovery is disabled;
Wake or re-enabling recovery is a separate operator action. Saved timer intent is not erased. Repeating repair is a no-op
when the active outputs are already bounded. Edit shows counts, before/preview text
bytes, errors and archive paths. An unchanged report can reference the previous repair
archive; that archive is not a snapshot of later conversation history.

This fixes the **per-field string problem**, not every possible API/context error.
It reduces tool-output text in model context but does not shrink the raw session
file or summarize the rest of the conversation. Navigating to a point before the
new edits can reveal the oversized value again; repair that active branch if needed.
Sessions are limited to 128 MiB, extracted output payloads to 512 MiB and lookup
indexes to 1 MiB. Near the raw session limit, use separate compaction/archive
maintenance rather than append an unreadable file. To undo manually, stop all writers,
back up the current session, verify no newer history will be lost, and restore
`original.jsonl`—which also restores the original oversized-output problem.

API: `POST /api/agents/<id>/repair-tool-outputs` returns the report. The last report
is persisted as `agent.tool_output_repair` and included in state. Existing password,
origin, IP and federation protections apply; a relay repair executes only on the
selected destination machine. Restart upgraded controllers and refresh the UI to
activate the endpoint; it does not repair any session merely by upgrading.

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

Open **+ New agent → Adopt session → Grok session**, choose a saved conversation, check its working
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
  agents/<uuid>.md    # source of truth: fields/checkpoint + durable wake delivery state
  sessions/*.jsonl    # exact session files for agents created by Swarm
  sessions/imports/   # converted foreign sessions (currently Grok Build)
  state.json         # atomically persisted last-good dashboard snapshot
  files/<uuid>.json  # private original-file reference metadata; no payload copies
  relay-settings.json # protected relay profile/key and connection intent (if configured)
  locks/             # cross-process report/adoption locks
  controller.lock    # single-controller lock
```

The registry is polled every 750 ms and pushed to browsers over WebSockets.
Changes and snapshots are written using temporary files, fsync, and atomic
rename. Reports merge under locks; normalized reports with no substantive state
change preserve the existing file and `updated_at`. Invalid/partial edits are shown as warnings;
a live controller retains the last-good record rather than losing the row.
Deleted files remove rows; snapshots never silently resurrect deleted records.
Do not delete a live agent's file to stop it — use Stop first. Use **Forget agent**
for safe dashboard removal; forgotten records are also removed from the snapshot
and are not resurrected by recovery.

**Restart Swarm after a reboot** (or configure your OS to do so):

| Saved state | Startup behavior if managed + auto-resume enabled |
| --- | --- |
| Waiting / dormant with a future `wake_at` | Keep the timer; do not launch early |
| Waiting / dormant with an overdue timer (or pending delivery retry) | Attempt the saved wake event, whether the process survived or must be relaunched |
| Process still alive, no due timer | Reattach to it; no unsolicited duplicate launch/resume prompt |
| Running, process gone, no scheduled delivery | Open the exact session and submit “Resume where you were” with goal/activity/checkpoint |
| Waiting / idle without a timer, process gone | Reopen pi with its saved history, **no automatic work prompt** |
| Dormant without `wake_at` | Keep waiting for an external event |
| Done / auto-resume disabled | Never automatically start |
| External observed record | Never execute automatically |

An ordinary failed startup/manual launch is visible and requires explicit Wake
after fixing credentials, folders, etc. **Explicitly scheduled wakes** instead keep
their intent and retry with persisted backoff (see below), not on every monitor tick.
Missing **known** session history is an
error, not permission to start an empty conversation. Running state records the
agent's intent; process liveness is shown separately so a crash never masquerades
as “goal accomplished.” Recovery is not transactional exactly-once execution:
resumed agents are instructed to verify external side effects before repeating them.

A user systemd example is in [`examples/swarm.service`](examples/swarm.service).
Adjust paths and PATH before installing it. Your normal shell/provider
environment is not necessarily present in a login service. For agents to
survive logout, your OS must keep the user's processes alive; on Linux this may
require user lingering. No OS services or startup settings are changed by Swarm.

### Durable scheduled wakeups

The `wake_at` timestamp was already stored atomically in the agent record. The
scheduler now handles both **waiting** and **dormant** timers and uses a durable
outbox instead of clearing the timestamp before delivery. This works even when an
agent runs `swarm report` while the backend is offline: the reporting helper writes
to the registry directly, without needing the HTTP server.

```bash
wake=$(date -u -d '+5 minutes' +%Y-%m-%dT%H:%M:%SZ)
"$SWARM_PYTHON" -m swarm report --status waiting --wake-at "$wake" \
  --activity 'Monitoring CI' --wait-reason 'Waiting for CI or review changes'
```

When due, Swarm persists `wake_delivery` with a stable event ID, attempt count and
retry deadline **before** touching tmux. Successful handoff marks it delivered and
clears that one-shot `wake_at`. Failures retain the request and `last_error`, with
backoff of 5s, 10s, 20s… capped at 5 minutes. Restart does not reset this backoff.
Overdue requests are processed when the controller returns, not lost or replayed
once per missed interval. A future scheduled waiting agent is not reopened early
just because its process disappeared during downtime.

Changed schedules and cancellations use the same file lock as agent reports;
completion of an old attempt cannot erase a newer deadline or checkpoint. Repeating
an unchanged deadline preserves the existing pending event ID and retry state.
Stop, manual Wake (including promptless Wake), `--wake-at none`, and reporting
`done` cancel pending timers. Disabling auto-resume pauses them; observed agents
never execute automatically. Compaction/stop-in-progress protections remain in force.
`--no-recover` disables general startup restoration, not the monitor's processing
of explicitly scheduled timers. The same registry must be available after restart;
Swarm cannot deliver while its backend is off, and it does not install an OS service.

This is **at-least-once delivery**, not transactional exactly-once execution. If the
controller dies after tmux accepted input but before its acknowledgement is saved,
it may send the **same event ID** again. The wake prompt tells the agent to check
its record and ignore canceled/replaced/already-handled events, then verify external
side effects. `delivered` means handed to the process—not an LLM acknowledgement or
proof the requested work finished. Expanded cards show delivery state, event ID and
next retry; this metadata is also in the agent Markdown and `/api/state`.

### Other restart-sensitive state

- **Already durable:** checkpoints, child wait/event notifications and retries,
  pending stop intent, spawn request identities, compaction manifests/backups,
  file-reference indexes and latest-product snapshots.
- **Durable messages:** ordinary messages and direct-child messages now use a
  persistent mailbox with request IDs and native Pi acknowledgements (see above).
  Custom text supplied directly to **Wake** and the cooperative-stop checkpoint
  prompt are still immediate deliveries. Extending durable semantics to those paths
  remains useful; blindly replaying an ambiguous external action is unsafe.
- **Uploaded file bytes:** completed browser uploads survive backend restarts if
  their system-temp files remain, but OS reboot/temp cleanup can remove them.
  Their reference metadata is durable. An opt-in durable-upload location would be
  useful when inputs must survive machine reboot; the current temporary-storage
  behavior requested by the upload feature is unchanged here. Unfinished uploads
  are intentionally canceled on controller restart rather than resumed blindly.
- **Intentionally transient:** relay sockets/RPC response buffers, terminal
  keystrokes, clipboard reads and browser connections. Reconnect/reconcile them;
  do not automatically replay raw input. Compaction work is reconciled on restart,
  not blindly repeated with additional model charges.

## Blocking pull-request monitor

Editable/package installation provides two read-only PR watchers intended for agent shell calls:
`swarm-pr-monitor-ghe` for GitHub Enterprise (also available as `swarm-pr-monitor`) and
`swarm-pr-monitor-one` for Azure DevOps One. They poll outside model context and block until CI
fails, blocking review feedback appears, observed CI settles green, the PR merges/completes, or
the local deadline expires. This avoids paying for repeated unchanged polling turns.

```bash
swarm-pr-monitor-ghe https://microsoft.ghe.com/bic/lobster/pull/22065 \
  --timeout 3600 --interval 30 --settle 30 \
  --ci-output /tmp/pr22065-ci.md --feedback-output /tmp/pr22065-feedback.md

swarm-pr-monitor-one \
  https://msazure.visualstudio.com/One/_git/AAD-FirstPartyApps/pullrequest/17640047 \
  --timeout 3600 --interval 30 --settle 30 \
  --ci-output /tmp/pr17640047-ci.md --feedback-output /tmp/pr17640047-feedback.md
```

The command atomically writes separate CI and review-feedback Markdown reports. Exit `0` means
all green/merged; `10` means CI failures; `11` blocking feedback; `12` both; `20` timeout; and
`21` repeated query errors. Codes `10`–`20` are expected monitor outcomes. Read both output files
after the command returns. GHE authentication comes from `gh`; One authentication comes from
`az devops` and its `azure-devops` extension.
See [`src/swarm/skills/pr-monitor.md`](src/swarm/skills/pr-monitor.md) for the complete agent
workflow, exact semantics and safety rules.

## Agent communication skills

Canonical coordination skill: [`src/swarm/skills/swarm.md`](src/swarm/skills/swarm.md).
Swarm explicitly loads only this entry point via `pi --skill` and tells agents to read it. It
explains reporting, all statuses, checkpoints, durable waiting/dormant timers, and safe recovery,
and routes matching tasks to repository companions such as
[`pr-monitor.md`](src/swarm/skills/pr-monitor.md) and
[`geneva-monitor/SKILL.md`](src/swarm/skills/geneva-monitor/SKILL.md). The companions are not
copied into Pi's global user-skill directory or separately injected on the command line.
Agents report real status changes immediately—especially **waiting before asking a
question**, including before blocking question/confirmation tools; dormant/timer
changes; errors that alter progress; and completion. While running, reports are for
major milestones or material plan changes, not each tool, test, or thought. Routine
updates should usually be no more frequent than about once every five minutes, but
real transitions are never delayed or dropped. A simple already-running task may skip
an opening report, and an unchanged status/checkpoint needs no duplicate end report.

Inside a managed pi shell tool:

```bash
"$SWARM_PYTHON" -m swarm report --status running --activity 'Implementing the parser'
"$SWARM_PYTHON" -m swarm report --status waiting --wake-at none --wait-reason 'Need schema decision'
"$SWARM_PYTHON" -m swarm report --status dormant \
  --wait-reason 'Recheck CI' --wake-at '2030-01-02T15:00:00Z' \
  --notes 'PR 42 pending. Next: inspect CI and review comments.'
"$SWARM_PYTHON" -m swarm report --status done --wake-at none --notes 'Goal met; PR merged.'
```

Successful `swarm report` calls are quiet by default; add `--json` to print the
complete updated record. Errors remain nonzero on stderr. The helper captures
`PI_SESSION_ID` / `PI_SESSION_FILE` and preserves unrelated fields. PID comes from
tmux, not the shell helper. Normalized no-op reports do not rewrite the record or
advance `updated_at` (timer cancellation and `done` normalization still apply).
Keep activity near 80 characters and checkpoint notes as the latest delta rather
than a full log; roughly 3–6 bullets / 1,000 characters is guidance, never a reason
to truncate important uncertainty, side effects, or recovery details. Status is
**agent-reported**, not inferred from terminal text: the skill must be followed.
There is no pi extension or heuristic pretending to know when a semantic goal is
accomplished.

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
