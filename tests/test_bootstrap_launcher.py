"""Exercise setup_swarm.sh with fake installers; never touch the host or launch Swarm."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")

SHIM = r"""#!PYTHON
import hashlib,json,os,subprocess,sys
from pathlib import Path
root=Path(os.environ['BOOTSTRAP_TEST_ROOT']);name=Path(sys.argv[0]).name;args=sys.argv[1:]
with (root/'commands.jsonl').open('a') as log:log.write(json.dumps([name,args])+"\n")
def make(where):
 where.parent.mkdir(parents=True,exist_ok=True);where.write_text(Path(__file__).read_text());where.chmod(0o755)
def runtime(): return Path(sys.argv[0]).parent.parent
if name.startswith('python'):
 if not args: sys.exit(0)
 if args[0]=='-c':
  code=args[1]
  if 'root.rglob' in code:
   exec(code);sys.exit(0)
  if 'pathlib.Path(sys.prefix).resolve()' in code:sys.exit(1 if os.environ.get('FAKE_BAD_VENV') else 0)
  if 'hashlib.sha256' in code:print(hashlib.sha256(Path(args[2]).read_bytes()).hexdigest())
  elif 'sys.version_info.major' in code:print('3.12')
  elif 'import fastapi' in code:sys.exit(0 if (runtime()/'installed').exists() else 1)
  sys.exit(0)
 if args[:2]==['-m','venv']:
  make(Path(args[2])/'bin/python');sys.exit(0)
 if args[:2]==['-m','ensurepip']:sys.exit(0)
 if args[:2]==['-m','pip']:
  if len(args)>2 and args[2]=='install':
   assert not any(key in os.environ for key in ('PIP_TARGET','PIP_PREFIX','PIP_USER'))
   if os.environ.get('FAKE_PIP_FAIL'):sys.exit(17)
   prefix=Path(args[args.index('--prefix')+1]);(prefix/'installed').touch();make(prefix/'bin/swarm')
  sys.exit(0)
 if args[0].endswith('/scripts/bootstrap_node.py'):
  make(Path(args[1])/'bin/node');make(Path(args[1])/'bin/npm');sys.exit(0)
 if args[0].endswith('/scripts/setup_wizard.py'):
  (root/'wizard.json').write_text(json.dumps({'args':args[1:],'cwd':os.getcwd(),'path':os.environ['PATH'],'venv':os.environ.get('VIRTUAL_ENV')}));sys.exit(0)
 raise SystemExit('unexpected fake Python args: '+repr(args))
if name=='uname':print(os.environ.get('FAKE_OS','Linux'));sys.exit(0)
if name=='tmux':
 print('tmux '+('3.3a' if (root/'tmux-updated').exists() else os.environ.get('FAKE_TMUX_VERSION','3.3a')));sys.exit(0)
if name in ('git','openssl'):sys.exit(0)
if name=='node':sys.exit(0 if '/node/' in str(Path(__file__)) else int(os.environ.get('FAKE_NODE_FAIL','0')))
if name=='pi':sys.exit(0 if '/pi/' in str(Path(__file__)) else int(os.environ.get('FAKE_PI_FAIL','0')))
if name=='npm':
 prefix=Path(args[args.index('--prefix')+1]);make(prefix/'node_modules/.bin/pi');sys.exit(0)
if name=='sudo':sys.exit(subprocess.call(args[1:] if args[0]=='--' else args))
if name=='brew':
 if args[0]=='--prefix':print(root/'brew-python')
 if args[0]=='install':
  make(root/'brew-python/bin/python3.12')
  for tool in ('git','tmux','openssl'):make(root/'bin'/tool)
  (root/'tmux-updated').touch()
 sys.exit(0)
if name=='dnf':
 if 'install' in args:
  for tool in ('git','tmux','openssl','python3.12'):make(root/'bin'/tool)
  (root/'tmux-updated').touch()
 sys.exit(0)
if name=='apt-cache':sys.exit(0)
if name=='apt-get':
 if args[0]=='install':
  for tool in ('git','tmux','openssl','python3.12'):make(root/'bin'/tool)
  (root/'tmux-updated').touch()
 sys.exit(0)
raise SystemExit('unexpected fake command '+name)
"""


@pytest.fixture
def sandbox(tmp_path):
    if not BASH:
        pytest.skip("Bash required")
    area = tmp_path / "bootstrap with spaces"
    repo = area / "repo"
    bin_dir = area / "bin"
    repo.mkdir(parents=True)
    bin_dir.mkdir()
    launcher = repo / "setup_swarm.sh"
    shutil.copyfile(ROOT / "setup_swarm.sh", launcher)
    launcher.chmod(0o755)
    (repo / "pyproject.toml").write_text("# fake project\n")
    (repo / "src/swarm").mkdir(parents=True)
    (repo / "src/swarm/__init__.py").touch()
    (repo / "scripts").mkdir()
    (repo / "scripts/bootstrap_node.py").touch()
    (repo / "scripts/setup_wizard.py").touch()
    shim = SHIM.replace("PYTHON", sys.executable, 1)

    def tool(name):
        path = bin_dir / name
        path.write_text(shim)
        path.chmod(0o755)

    for name in (
        "python3",
        "git",
        "tmux",
        "openssl",
        "pi",
        "node",
        "npm",
        "uname",
        "sudo",
        "apt-get",
        "apt-cache",
    ):
        tool(name)
    # Keep PATH hermetic: package managers and network clients cannot fall through
    # to host executables.
    for name in ("cat", "dirname", "mkdir", "rmdir", "env", "mv", "bash"):
        (bin_dir / name).symlink_to(shutil.which(name))
    cwd = area / "unrelated cwd"
    cwd.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SWARM_", "PI_", "PIP_", "PYTHON"))
        and key not in {"BASH_ENV", "ENV", "VIRTUAL_ENV"}
    }
    env.update(PATH=str(bin_dir), HOME=str(area / "home"), BOOTSTRAP_TEST_ROOT=str(area))

    class Sandbox:
        def __init__(self):
            self.area = area
            self.repo = repo
            self.bin = bin_dir
            self.cwd = cwd
            self.env = env
            self.launcher = launcher

        def run(self, *args, input_text=None, **overrides):
            return subprocess.run(
                [BASH, str(launcher), *args],
                cwd=cwd,
                env={**env, **overrides},
                input=input_text,
                text=True,
                capture_output=True,
                timeout=20,
            )

        def events(self):
            log = area / "commands.jsonl"
            return (
                [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            )

        def add(self, name):
            tool(name)

        def remove(self, *names):
            for name in names:
                (bin_dir / name).unlink()

    return Sandbox()


def test_help_is_side_effect_free_and_unknown_arguments_fail(sandbox):
    result = sandbox.run("--help")
    assert result.returncode == 0
    assert "--runtime-dir PATH" in result.stdout
    assert "--install-only" in result.stdout
    assert not sandbox.events()
    assert list(sandbox.cwd.iterdir()) == []

    unknown = sandbox.run("--port", "8000")
    assert unknown.returncode == 2
    assert "unknown argument" in unknown.stderr
    assert list(sandbox.cwd.iterdir()) == []


def test_prompts_for_runtime_and_permission_then_launches(sandbox):
    result = sandbox.run(input_text="\nyes\n")
    assert result.returncode == 0, result.stderr
    expected = sandbox.area / "home/.local/share/swarm/runtime"
    wizard = json.loads((sandbox.area / "wizard.json").read_text())
    assert wizard["args"][:2] == ["--runtime-dir", str(expected)]
    assert "$HOME/.local/share/swarm/runtime" in result.stderr


def test_cancel_happens_before_runtime_creation_or_install(sandbox):
    runtime = sandbox.cwd / "cancelled runtime"
    result = sandbox.run("--runtime-dir", str(runtime), input_text="no\n")
    assert result.returncode != 0
    assert "cancelled before making changes" in result.stderr
    assert not runtime.exists()
    assert not any(name in {"apt-get", "dnf", "brew", "npm"} for name, _ in sandbox.events())


def test_relative_runtime_and_config_use_caller_cwd_and_launch_helper(sandbox):
    result = sandbox.run(
        "--runtime-dir",
        "chosen runtime",
        "--config",
        "configs/custom.toml",
        "--no-launch",
        "--yes-install",
    )
    assert result.returncode == 0, result.stderr
    runtime = sandbox.cwd / "chosen runtime"
    wizard = json.loads((sandbox.area / "wizard.json").read_text())
    assert wizard["cwd"] == str(sandbox.cwd)
    assert wizard["args"] == [
        "--runtime-dir",
        str(runtime),
        "--source-dir",
        str(sandbox.repo),
        "--pi",
        str(sandbox.bin / "pi"),
        "--config",
        "configs/custom.toml",
        "--no-launch",
    ]
    assert wizard["venv"] == str(runtime / "venv")
    assert wizard["path"].startswith(str(runtime / "venv/bin") + os.pathsep)
    assert not (sandbox.repo / ".venv").exists()
    assert not (sandbox.repo / ".swarm-bootstrap").exists()
    assert not (runtime / "setup.lock").exists()


def test_legacy_launcher_env_and_credentials_are_not_required(sandbox):
    assert not (sandbox.repo / "launch_swarm.sh").exists()
    assert not (sandbox.repo / ".env").exists()
    result = sandbox.run(
        "--runtime-dir", str(sandbox.area / "runtime"), "--yes-install", "--install-only"
    )
    assert result.returncode == 0, result.stderr
    assert not (sandbox.area / "wizard.json").exists()


def test_setup_only_alias_provisions_without_wizard(sandbox):
    result = sandbox.run(
        "--runtime-dir", str(sandbox.area / "runtime"), "--yes-install", "--setup-only"
    )
    assert result.returncode == 0, result.stderr
    assert not (sandbox.area / "wizard.json").exists()


def test_repeated_setup_reuses_validated_installation(sandbox):
    args = ("--runtime-dir", str(sandbox.area / "runtime"), "--yes-install", "--install-only")
    assert sandbox.run(*args).returncode == 0
    before = len(sandbox.events())
    second = sandbox.run(*args)
    assert second.returncode == 0, second.stderr
    new_events = sandbox.events()[before:]
    assert not any(args[:3] == ["-m", "pip", "install"] for _, args in new_events)
    assert not any(name == "npm" for name, _ in new_events)


def test_missing_system_prerequisites_include_openssl(sandbox):
    sandbox.remove("python3", "git", "tmux", "openssl")
    result = sandbox.run(
        "--runtime-dir", str(sandbox.area / "runtime"), "--yes-install", "--install-only"
    )
    assert result.returncode == 0, result.stderr
    installs = [
        args for name, args in sandbox.events() if name == "apt-get" and args[0] == "install"
    ]
    assert len(installs) == 1
    assert {"python3.12", "python3.12-venv", "git", "tmux", "openssl", "ca-certificates"} <= set(
        installs[0]
    )


@pytest.mark.parametrize("manager,os_name", [("dnf", "Linux"), ("brew", "Darwin")])
def test_supported_system_package_manager_variants(sandbox, manager, os_name):
    sandbox.remove("apt-get", "python3", "git", "tmux", "openssl")
    sandbox.add(manager)
    result = sandbox.run(
        "--runtime-dir",
        str(sandbox.area / "runtime"),
        "--yes-install",
        "--install-only",
        FAKE_OS=os_name,
    )
    assert result.returncode == 0, result.stderr
    assert any(name == manager and args[0] == "install" for name, args in sandbox.events())


def test_missing_pi_uses_verified_node_helper_and_private_pinned_pi(sandbox):
    sandbox.remove("pi")
    result = sandbox.run(
        "--runtime-dir",
        str(sandbox.area / "runtime"),
        "--yes-install",
        "--install-only",
        FAKE_NODE_FAIL="1",
    )
    assert result.returncode == 0, result.stderr
    events = sandbox.events()
    assert any(args and args[0].endswith("/scripts/bootstrap_node.py") for _, args in events)
    npm_args = next(args for name, args in events if name == "npm" and args[0] == "install")
    assert npm_args[npm_args.index("--prefix") + 1] == str(sandbox.area / "runtime/pi")
    assert "--ignore-scripts" in npm_args
    assert npm_args[-1] == "@earendil-works/pi-coding-agent@1.2.0"


def test_runtime_root_symlink_is_never_modified(sandbox):
    external = sandbox.area / "external-root"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("original")
    runtime = sandbox.area / "runtime"
    runtime.symlink_to(external, target_is_directory=True)
    result = sandbox.run("--runtime-dir", str(runtime), "--yes-install", "--install-only")
    assert result.returncode != 0
    assert "root must not be a symlink" in result.stderr
    assert sentinel.read_text() == "original"


@pytest.mark.parametrize("relative", ["venv", "node", "pi"])
def test_runtime_child_symlinks_are_never_modified(sandbox, relative):
    runtime = sandbox.area / "runtime"
    runtime.mkdir()
    external = sandbox.area / f"external-{relative}"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("original")
    (runtime / relative).symlink_to(external, target_is_directory=True)
    result = sandbox.run("--runtime-dir", str(runtime), "--yes-install", "--install-only")
    assert result.returncode != 0
    assert "must not be symlinks" in result.stderr
    assert sentinel.read_text() == "original"
    assert not (runtime / "setup.lock").exists()


def test_invalid_existing_venv_is_preserved(sandbox):
    runtime = sandbox.area / "runtime"
    (runtime / "venv").mkdir(parents=True)
    sentinel = runtime / "venv/preserve"
    sentinel.write_text("keep")
    result = sandbox.run(
        "--runtime-dir",
        str(runtime),
        "--yes-install",
        "--install-only",
        FAKE_BAD_VENV="1",
    )
    assert result.returncode != 0
    assert "not deleted" in result.stderr
    assert sentinel.read_text() == "keep"
    assert not (runtime / "setup.lock").exists()


def test_global_pip_targets_are_not_inherited(sandbox):
    result = sandbox.run(
        "--runtime-dir",
        str(sandbox.area / "runtime"),
        "--yes-install",
        "--install-only",
        PIP_TARGET="/do-not-touch",
        PIP_PREFIX="/do-not-touch",
        PIP_USER="1",
    )
    assert result.returncode == 0, result.stderr


def test_native_windows_failure_is_actionable_and_makes_no_runtime(sandbox):
    runtime = sandbox.area / "runtime"
    result = sandbox.run("--runtime-dir", str(runtime), "--yes-install", FAKE_OS="MINGW64_NT-10.0")
    assert result.returncode != 0
    assert "swarm-windows.sh" in result.stderr
    assert not runtime.exists()


def test_shell_contains_no_relay_endpoint_or_hardcoded_host_defaults():
    shell = (ROOT / "setup_swarm.sh").read_text().lower()
    assert "relay_host" not in shell
    assert "relay.example" not in shell
    assert "cert.pem" not in shell
    assert "https://" not in shell


def test_marker_temporary_symlink_never_overwrites_external_file(sandbox):
    runtime = sandbox.area / "runtime"
    assert (
        sandbox.run("--runtime-dir", str(runtime), "--yes-install", "--install-only").returncode
        == 0
    )
    target = sandbox.area / "external"
    target.write_text("keep")
    (runtime / "venv/.swarm-bootstrap-fingerprint.tmp").symlink_to(target)
    (sandbox.repo / "pyproject.toml").write_text("# force reinstall\n")
    result = sandbox.run("--runtime-dir", str(runtime), "--yes-install", "--install-only")
    assert result.returncode != 0 and "unsafe marker temporary" in result.stderr
    assert target.read_text() == "keep"


def test_npm_rejects_external_nested_package_symlinks(sandbox):
    runtime = sandbox.area / "runtime"
    runtime.mkdir()
    (runtime / "pi").mkdir()
    target = sandbox.area / "external"
    target.mkdir()
    (target / "keep").write_text("original")
    (runtime / "pi/node_modules").symlink_to(target, target_is_directory=True)
    sandbox.remove("pi")
    result = sandbox.run("--runtime-dir", str(runtime), "--yes-install", "--install-only")
    assert result.returncode != 0 and "external symlinks" in result.stderr
    assert (target / "keep").read_text() == "original" and list(target.iterdir()) == [
        target / "keep"
    ]
    assert not any(name == "npm" for name, _ in sandbox.events())
