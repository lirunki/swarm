#!/usr/bin/env bash
# Interactively provision an isolated Swarm runtime, then run the setup wizard.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: setup_swarm.sh [OPTIONS]

Provision an isolated Swarm runtime and start the interactive setup wizard.

Options:
  --runtime-dir PATH  Runtime root (default prompt: ~/.local/share/swarm/runtime)
  --yes-install       Permit system package changes (possibly sudo) and private runtime installation without confirmation
  --install-only      Provision dependencies and exit without running the wizard
  --setup-only        Alias for --install-only
  --no-launch         Ask the wizard to configure without launching Swarm
  --config PATH       Pass an existing configuration path to the wizard
  -h, --help          Show this help without making changes

Relative runtime and config paths are interpreted from the directory where this
command was invoked. Native Windows users should run swarm-windows.sh instead.
EOF
}

runtime_input=''
config_input=''
yes_install=false
install_only=false
no_launch=false
while (( $# )); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --runtime-dir)
      (( $# >= 2 )) || { printf '%s\n' 'setup_swarm.sh: --runtime-dir requires PATH.' >&2; exit 2; }
      runtime_input=$2
      shift 2
      ;;
    --yes-install)
      yes_install=true
      shift
      ;;
    --install-only|--setup-only)
      install_only=true
      shift
      ;;
    --no-launch)
      no_launch=true
      shift
      ;;
    --config)
      (( $# >= 2 )) || { printf '%s\n' 'setup_swarm.sh: --config requires PATH.' >&2; exit 2; }
      config_input=$2
      shift 2
      ;;
    --)
      shift
      (( $# == 0 )) || { printf 'setup_swarm.sh: unknown argument: %s\n' "$1" >&2; exit 2; }
      ;;
    *)
      printf 'setup_swarm.sh: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

# Nothing before this point may inspect the host or modify the filesystem: help is
# deliberately side-effect free.
caller_cwd=$(pwd -P)
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
log() { printf 'Swarm setup: %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) die 'Use bash /path/to/swarm/swarm-windows.sh on native Windows; this setup supports Linux/WSL and macOS.' ;;
esac

[[ -f "$script_dir/pyproject.toml" && -f "$script_dir/scripts/bootstrap_node.py" ]] \
  || die 'Keep setup_swarm.sh in the Swarm source checkout.'

if [[ -z "$runtime_input" ]]; then
  default_runtime='$HOME/.local/share/swarm/runtime'
  printf 'Runtime directory [%s]: ' "$default_runtime" >&2
  if ! IFS= read -r runtime_input; then
    die 'Setup cancelled before making changes.'
  fi
  [[ -n "$runtime_input" ]] || runtime_input="$HOME/.local/share/swarm/runtime"
fi
[[ -n "$runtime_input" ]] || die 'Runtime directory must not be empty.'

resolve_from_caller() {
  local value=$1
  case "$value" in
    '~') printf '%s\n' "$HOME" ;;
    '~/'*) printf '%s/%s\n' "$HOME" "${value#\~/}" ;;
    '~'*) die "Unsupported home-directory form: $value" ;;
    /*) printf '%s\n' "${value%/}" ;;
    *) printf '%s/%s\n' "$caller_cwd" "${value%/}" ;;
  esac
}
runtime=$(resolve_from_caller "$runtime_input")
[[ -n "$runtime" && "$runtime" != / ]] || die 'The runtime root cannot be the filesystem root.'
config=$config_input

if ! $yes_install; then
  printf 'Install missing OS packages system-wide (may use sudo) and private dependencies under %s? [y/N] ' "$runtime" >&2
  if ! IFS= read -r answer || [[ ! "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    die 'Setup cancelled before making changes.'
  fi
fi

unset PYTHONHOME PYTHONPATH
umask 077
[[ ! -L "$runtime" ]] || die 'The runtime root must not be a symlink; it was not changed.'
if [[ -e "$runtime" && ! -d "$runtime" ]]; then die 'The runtime root exists and is not a directory.'; fi
if [[ -d "$runtime" && ! -O "$runtime" ]]; then die 'The runtime root must be owned by the current user.'; fi
mkdir -p -- "$runtime"
runtime=$(cd -- "$runtime" && pwd -P)
venv="$runtime/venv"
node_root="$runtime/node"
pi_root="$runtime/pi"
lock="$runtime/setup.lock"
for path in "$venv" "$node_root" "$pi_root"; do
  [[ ! -L "$path" ]] || die "Runtime directories must not be symlinks: $path was not changed."
done
mkdir -- "$lock" 2>/dev/null || die "Another setup is running, or $lock is stale. Inspect it before removing an abandoned lock."
trap 'rmdir -- "$lock" 2>/dev/null || true' EXIT

python_ok() { "$1" -c 'import sys; raise SystemExit(sys.version_info < (3,11) or sys.platform == "win32")' >/dev/null 2>&1; }
find_python() {
  python_bin=''
  if [[ -n "${SWARM_BOOTSTRAP_PYTHON:-}" ]]; then
    python_ok "$SWARM_BOOTSTRAP_PYTHON" || die 'SWARM_BOOTSTRAP_PYTHON must identify a working POSIX Python 3.11+.'
    python_bin=$SWARM_BOOTSTRAP_PYTHON
    return
  fi
  local candidate
  for candidate in "$venv/bin/python" python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      python_bin=$(command -v "$candidate")
      return
    fi
  done
}
tmux_ok() {
  local version
  version=$(tmux -V 2>/dev/null) || return 1
  [[ "$version" =~ ^tmux[[:space:]]+([0-9]+)\.([0-9]+) ]] || return 1
  (( BASH_REMATCH[1] > 3 || (BASH_REMATCH[1] == 3 && BASH_REMATCH[2] >= 2) ))
}
privileged() {
  if (( EUID == 0 )); then "$@"
  elif command -v sudo >/dev/null 2>&1; then sudo -- "$@"
  else die 'Installing missing OS prerequisites requires sudo (or an administrator to install them).'
  fi
}

find_python
need_python=false
need_venv=false
need_git=false
need_tmux=false
need_openssl=false
[[ -n "$python_bin" ]] || need_python=true
if [[ -n "$python_bin" ]] && ! "$python_bin" -c 'import venv, ensurepip' >/dev/null 2>&1; then need_venv=true; fi
git --version >/dev/null 2>&1 || need_git=true
tmux_ok || need_tmux=true
openssl version >/dev/null 2>&1 || need_openssl=true
if $need_python || $need_venv || $need_git || $need_tmux || $need_openssl; then
  log 'Installing missing system prerequisites (package-manager privilege may be requested).'
  packages=()
  if command -v apt-get >/dev/null 2>&1; then
    privileged apt-get update
    if $need_python; then
      python_package=''
      for version in 3.12 3.13 3.11 3.14; do
        if apt-cache show "python${version}-venv" >/dev/null 2>&1; then python_package="python${version}"; break; fi
      done
      [[ -n "$python_package" ]] || die 'No Python 3.11+ venv package found in configured apt repositories. Upgrade the distribution or supply SWARM_BOOTSTRAP_PYTHON.'
      packages+=("$python_package" "${python_package}-venv")
    elif $need_venv; then
      version=$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
      packages+=("python${version}-venv")
    fi
    $need_git && packages+=(git)
    $need_tmux && packages+=(tmux)
    $need_openssl && packages+=(openssl)
    packages+=(ca-certificates)
    privileged apt-get install -y -- "${packages[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    if $need_python; then
      python_package=python3
      for version in 3.12 3.11 3.13; do
        if dnf -q list --available "python${version}" >/dev/null 2>&1; then python_package="python${version}"; break; fi
      done
      packages+=("$python_package" "${python_package}-pip")
    elif $need_venv; then
      version=$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
      packages+=("python${version}" "python${version}-pip")
    fi
    $need_git && packages+=(git)
    $need_tmux && packages+=(tmux)
    $need_openssl && packages+=(openssl)
    packages+=(ca-certificates)
    privileged dnf install -y "${packages[@]}"
  elif command -v pacman >/dev/null 2>&1; then
    if $need_python || $need_venv; then packages+=(python python-pip); fi
    $need_git && packages+=(git)
    $need_tmux && packages+=(tmux)
    $need_openssl && packages+=(openssl)
    packages+=(ca-certificates)
    privileged pacman -S --needed --noconfirm "${packages[@]}"
  elif command -v brew >/dev/null 2>&1; then
    if $need_python || $need_venv; then packages+=(python@3.12); fi
    $need_git && packages+=(git)
    $need_tmux && packages+=(tmux)
    $need_openssl && packages+=(openssl)
    brew install "${packages[@]}"
    if $need_python || $need_venv; then python_bin="$(brew --prefix python@3.12)/bin/python3.12"; fi
  else
    die 'No supported package manager. Install Python 3.11+ with venv/pip, tmux 3.2+, Git, and OpenSSL, then rerun (macOS requires Homebrew).'
  fi
  if [[ -z "$python_bin" ]] || ! python_ok "$python_bin"; then find_python; fi
fi
[[ -n "$python_bin" ]] && python_ok "$python_bin" || die 'Python 3.11+ is still unavailable; provide SWARM_BOOTSTRAP_PYTHON or update the OS packages.'
git --version >/dev/null 2>&1 || die 'Git installation did not provide a working git on PATH.'
tmux_ok || die 'tmux 3.2+ is still unavailable; update its OS package.'
openssl version >/dev/null 2>&1 || die 'OpenSSL is still unavailable; update its OS package.'

if [[ ! -e "$venv" ]]; then
  log "Creating $venv"
  "$python_bin" -m venv "$venv" || die 'Cannot create the runtime venv; install the matching Python venv/ensurepip package and rerun.'
fi
vpython="$venv/bin/python"
[[ -x "$vpython" ]] && python_ok "$vpython" || die 'Existing runtime venv is invalid/too old. Move it aside yourself and rerun; it was not deleted.'
"$vpython" -c 'import pathlib,sys,sysconfig; root=pathlib.Path(sys.argv[1]); assert sys.prefix != sys.base_prefix and pathlib.Path(sys.prefix).resolve() == root; assert all(pathlib.Path(sysconfig.get_path(key)).resolve().is_relative_to(root) for key in ("purelib","platlib","scripts","data"))' "$venv" || die 'Refusing pip outside the selected runtime venv.'
export VIRTUAL_ENV="$venv"
export PATH="$venv/bin:$PATH"
if ! "$vpython" -m pip --version >/dev/null 2>&1; then "$vpython" -m ensurepip --upgrade; fi

fingerprint=$("$vpython" -c 'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$script_dir/pyproject.toml")
marker="$venv/.swarm-bootstrap-fingerprint"
[[ ! -L "$marker" ]] || die 'Refusing a symlink installation marker.'
installed=false
if [[ -f "$marker" && "$(<"$marker")" == "$fingerprint" ]]; then
  if [[ -x "$venv/bin/swarm" ]] && "$vpython" -c 'import fastapi,anyio,uvicorn,websockets,pydantic,yaml,swarm,pathlib,sys; assert pathlib.Path(swarm.__file__).resolve() == pathlib.Path(sys.argv[1]).resolve()' "$script_dir/src/swarm/__init__.py" >/dev/null 2>&1 && "$vpython" -m pip check >/dev/null 2>&1; then installed=true; fi
fi
if ! $installed; then
  log 'Installing Swarm and its Python dependencies into the runtime venv.'
  env -u PIP_TARGET -u PIP_PREFIX -u PIP_USER "$vpython" -m pip install --disable-pip-version-check --no-input --no-user --prefix "$venv" -e "$script_dir"
  "$vpython" -m pip check
  "$vpython" -c 'import fastapi,anyio,uvicorn,websockets,pydantic,yaml,swarm,pathlib,sys; assert pathlib.Path(swarm.__file__).resolve() == pathlib.Path(sys.argv[1]).resolve()' "$script_dir/src/swarm/__init__.py"
  [[ -x "$venv/bin/swarm" ]] || die 'Installation did not create the runtime swarm command.'
  [[ ! -e "$marker.tmp" && ! -L "$marker.tmp" ]] || die 'Stale or unsafe marker temporary file; inspect it before retrying.'
  (set -o noclobber; printf '%s\n' "$fingerprint" > "$marker.tmp")
  mv -f -- "$marker.tmp" "$marker"
fi

# Prefer a working Pi already selected by the caller's PATH. Otherwise install a
# pinned private Pi, using a verified private Node distribution only when needed.
if ! command -v pi >/dev/null 2>&1 || ! pi --version >/dev/null 2>&1; then
  node_ok() { node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=19)?0:1)' >/dev/null 2>&1; }
  if ! node_ok || ! command -v npm >/dev/null 2>&1; then
    if [[ ! -e "$node_root" ]]; then "$vpython" "$script_dir/scripts/bootstrap_node.py" "$node_root"; fi
    export PATH="$node_root/bin:$PATH"
    node_ok || die "Private Node is invalid/too old. Inspect $node_root; it was not replaced."
  fi
  export PATH="$pi_root/node_modules/.bin:$PATH"
  if ! command -v pi >/dev/null 2>&1 || ! pi --version >/dev/null 2>&1; then
    command -v npm >/dev/null 2>&1 || die 'The selected Node installation does not provide npm.'
    pi_version=${SWARM_PI_VERSION:-1.2.0}
    [[ "$pi_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'SWARM_PI_VERSION must be a numeric release such as 1.2.0.'
    log "Installing private Pi $pi_version (no global npm packages changed)."
    "$vpython" -c 'import pathlib,sys; root=pathlib.Path(sys.argv[-1]).resolve(); assert all(not p.is_symlink() or p.resolve().is_relative_to(root) for p in root.rglob("*")), "Private Pi tree contains external symlinks"' "$pi_root" || die 'Refusing npm writes through external symlinks in the private Pi tree.'
    npm install --prefix "$pi_root" --engine-strict --ignore-scripts --no-audit --no-fund "@earendil-works/pi-coding-agent@$pi_version"
    pi --version >/dev/null || die 'Private Pi failed its version check.'
  fi
fi

rmdir -- "$lock"
trap - EXIT
if $install_only; then
  log 'Runtime dependencies are ready; the setup wizard was not run.'
  exit 0
fi
[[ -f "$script_dir/scripts/setup_wizard.py" ]] || die 'The setup wizard is missing from this source checkout.'
wizard_args=(--runtime-dir "$runtime" --source-dir "$script_dir" --pi "$(command -v pi)")
[[ -z "$config" ]] || wizard_args+=(--config "$config")
$no_launch && wizard_args+=(--no-launch)
exec "$vpython" "$script_dir/scripts/setup_wizard.py" "${wizard_args[@]}"
