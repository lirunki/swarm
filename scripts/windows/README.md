# Windows without WSL (experimental)

On x64 Windows 10/11 with ConPTY support, Swarm uses a private
**Cygwin + tmux + POSIX Python 3.12** runtime and native
Windows Node/Pi. Stock Git Bash is the entry point; nothing is installed into
Git Bash itself. Native Windows Python is not the Swarm controller runtime.

## One-command start

From a checkout in **Git Bash**:

```bash
bash ./swarm-windows.sh
```

Or double-click `swarm-windows.cmd`. The first run installs prerequisites, runs
provider-free integration checks, and opens the dashboard. Later runs reuse the
installation. No WSL, administrator rights, Python installation, Node installation,
or manual tmux configuration is required. Internet access and enough disk space
for Cygwin's C++/Rust build toolchain are required. The initial build can take
several minutes; logs show progress.

Provider credentials cannot be supplied automatically. Existing Windows Pi
credentials/configuration are reused; otherwise open an agent's Terminal and
complete Pi's normal login/model selection. Project trust prompts are preserved.

## What setup changes

Default install location: `%LOCALAPPDATA%\Swarm`.

- Downloads the official Cygwin setup executable over HTTPS; checks a tracked
  SHA256 pin **and** a valid Authenticode signature from its expected maintainer
  before executing it. Cygwin verifies its repository metadata/packages.
- Installs a user-local Cygwin runtime with Python 3.12, tmux 3.2+, Git,
  C++/Rust compilers and packaged Python build backends.
- Downloads private Windows Node LTS; verifies its zip against the official
  HTTPS `SHASUMS256.txt`. HTTPS to nodejs.org is the trust root for that manifest.
- Installs private Pi using npm's integrity checks and `--ignore-scripts`.
  No global npm package is replaced.
- Creates a dedicated Cygwin venv, **not the checkout's `.venv`**. It sees the
  private Cygwin system packages so it can use packaged pip/maturin rather than
  attempting to compile the build backend recursively.
- Builds/caches dependency wheels locally. Cygwin maturin 1.15 emits an invalid
  `cygwin_86_64` platform tag for locally built pydantic-core; setup repairs only
  that known tag, with the same interpreter/platform, using `wheel tags`.
  It never relabels Windows CPython/Linux wheels as Cygwin-compatible. Package
  dependency checks and full server imports must pass afterwards.
- Creates launchers and tests real native Node TTY support, reporting,
  HTTP/WebSocket terminal I/O, detachment, server restart, exact-session recovery
  and stopping the native worker. These use a fake Pi and a temporary registry:
  **no model calls, real agent sessions or provider credentials are used**.
- **Changes Pi's global `shellPath` to this installation's Cygwin Bash.** This
  also affects standalone Pi after restart. An existing `settings.json` is
  backed up first; all other settings, including trust/model choices, are
  preserved. The custom `PI_CODING_AGENT_DIR` is respected. The change happens
  only after the runtime checks pass. Project-local shell overrides can still
  override Pi's global setting; managed agents need the Cygwin shell to report.

Setup does not change the machine/global PATH, install a Windows service,
modify Git Bash's files, or disable certificate validation. The bootstrap requests
a process-local PowerShell execution-policy override; administrator-enforced
policies still apply, and machine execution policies are not changed.
The installer writes `ready.json` only after validation and shell configuration
succeed. Partial failures are not reported as a ready installation.

Swarm's registry defaults to `%USERPROFILE%\.swarm`, separate from a WSL home.
Existing Pi sessions/auth stay under the user's Windows `.pi` directory.
Do not share an active registry between WSL and Cygwin controllers. Keep the
checkout and runtime at their installed locations; the Python install is editable.
Use local drive folders; UNC/network-share projects or install locations are
not supported in this first version.

## Commands

```bash
# Default: install if needed, then serve --open
bash ./swarm-windows.sh

bash ./swarm-windows.sh doctor
bash ./swarm-windows.sh --home 'C:/Users/you/swarm-registry' serve --port 8788

# Setup/update only (Windows-style absolute paths for installer options):
bash ./setup-windows.sh
bash ./setup-windows.sh -Update
bash ./setup-windows.sh -InstallDir 'D:/Tools/Swarm'
```

For a custom installation, set `SWARM_WINDOWS_INSTALL_DIR` when using the root
launcher, or use `<install-dir>/swarm` directly from Git Bash. The root launcher
preserves the calling working directory. CLI paths may use Windows drive paths,
Cygwin paths or Git Bash `/c/...` paths; prompts are never path-rewritten.

`<install-dir>\swarm.cmd` is also available. Windows cmd/PowerShell 5 argument
serialization has limitations with embedded quotes/empty arguments; prefer the
Git Bash/POSIX launcher for complex CLI arguments. Managed Pi reporting runs
directly through Cygwin, not this cmd/PowerShell round-trip.

`-Update` refreshes private Pi, rebuilds/installs Swarm and reruns checks.
`-Force` additionally overlays Cygwin packages, replaces private Node and
recreates the venv. **Stop agents and the controller before forcing an update**;
locked executable files cannot be safely replaced. Neither option deletes
saved sessions, registries, worktrees or projects.

## Troubleshooting and offline package cache

Logs: `<install-dir>\logs\install-*.log`, plus Cygwin setup logs. On failure,
fix the reported issue and rerun setup; validated stages can be reused. If a
partial Cygwin install is found, setup retries rather than treating `bash.exe`
alone as success.

Setup requires access to official Cygwin mirrors, nodejs.org, registry.npmjs.org,
pypi.org/files.pythonhosted.org and Rust crate hosts. Corporate proxies or
firewalls may require approved configuration. Setup never disables TLS checks
or changes firewall/proxy policies to work around blocked downloads.

An optional pre-populated package directory is supported:

```bash
bash ./setup-windows.sh -Wheelhouse 'D:/Packages/swarm-python'
```

It must contain the required pure-Python wheels, Cygwin-native wheels or source
archives, plus `tomlkit` and `trove-classifiers`. Ordinary Windows CPython native
wheels are **not** compatible with Cygwin. This option makes the Python package
stage offline; Cygwin/Node/npm downloads still need their own cache/network.
The development validation on the initial Windows test host used this option
because that host's TLS connections to files.pythonhosted.org failed. This is
not a claim that a clean online install passed on that host.

A Cygwin bootstrap hash mismatch can mean the official installer was updated.
Update Swarm first; do not bypass verification. Maintainers refreshing the pin
must verify the new official artifact/signature and update the tracked hash.
`-SetupSha256` accepts an explicitly verified replacement hash; signature
validation remains mandatory. `-Mirror` accepts HTTPS mirrors only.

Advanced `-SkipCygwin`, `-SkipNode`, `-SkipPi`, `-SkipVenv`, `-SkipSettings` and
`-SkipLauncher` switches exist for diagnosis. Skipping shell configuration or
launcher checks does not create `ready.json`. They are not needed for normal use.

To remove this runtime, first stop its agents/controller and restore only the
previous Pi `shellPath` value from the settings backup (preserve any subsequent
settings changes). Then remove the installation directory. Do not delete
`.swarm` or `.pi` if you want to retain agents/history/credentials.

## Validation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests/windows/test-installer.ps1
```

`tests/windows/test-runtime.ps1 -InstallDir <runtime>` additionally exercises
actual Git Bash/cmd launchers, cwd/quoted paths and ready-marker/settings behavior
using a disposable profile; it does not change the real user's Pi settings.

The installer runs `probe.py` (native Node TTY) and `smoke.py` (native worker,
reporting, terminal, HTTP/WebSocket and lifecycle). They use unique tmux servers
and temporary registries and clean them up. Python unit tests in
`tests/test_windows.py` cover platform boundaries with mocks; those tests alone
do not establish Windows runtime compatibility.

An opt-in GitHub Actions workflow, `.github/workflows/windows.yml`, repeats
setup and tests on a fresh Windows runner with a space-containing install path.
It is manual while this route is experimental; no provider secrets are needed.

Initial validation (2026-10-15): the full suite passed **81 tests on Linux** and
**80 tests on Windows/Cygwin, with one optional Pi parser integration test skipped**. Provider-free
native-worker/HTTP/WebSocket/recovery/termination smoke checks and actual Git
Bash/cmd launcher checks passed. The Windows package build used the explicit
offline cache described above; clean online installation and the hosted CI
workflow have not been verified on a fresh machine.

The browser UI and tmux lifetime guarantees remain unchanged. Real Pi/provider
interactions, extensions and arbitrary project-specific shell settings still
need real-world testing; this Windows route remains experimental.
