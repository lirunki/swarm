"""Windows boundary contracts, runnable without a Windows host.

Actual Cygwin/ConPTY/native Node behavior must additionally pass the Windows
smoke script. Mocks here do not assert that a Windows installation works.
"""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm import adapters, cli, entrypoint, paths, sessions, windows_pi
from swarm.importers import pi_history
from swarm.models import Agent
from swarm.store import Store


def test_git_bash_with_cygwin_ostype_uses_dedicated_runtime(tmp_path):
    root = tmp_path / "install with spaces"
    tools = tmp_path / "tools"
    tools.mkdir()
    (root / "cygwin/bin").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts/windows/swarm"
    launcher = root / "swarm"
    launcher.write_bytes(source.read_bytes())
    runtime = root / "swarm-runtime.sh"
    runtime.write_text("#!/bin/sh\nexit 77\n")
    runtime.chmod(0o700)
    outfile = tmp_path / "arguments"
    fake_bash = root / "cygwin/bin/bash.exe"
    fake_bash.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$SWARM_ARGV_LOG"\n')
    fake_bash.chmod(0o700)
    uname = tools / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Msys\\n'\n")
    uname.chmod(0o700)
    converter = '#!/bin/sh\nfor value; do :; done\nprintf "%s\\n" "$value"\n'
    for path in [tools / "cygpath", root / "cygwin/bin/cygpath.exe"]:
        path.write_text(converter)
        path.chmod(0o700)
    env = os.environ | {
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "SWARM_ARGV_LOG": str(outfile),
    }
    args = ["--home", "C:/folder's path & spaces", "doctor"]
    result = subprocess.run(
        [
            "bash",
            "-c",
            'script=$1; shift; OSTYPE=cygwin; source "$script"',
            "probe",
            str(launcher),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert outfile.read_text().splitlines() == [
        "--noprofile",
        "--norc",
        str(runtime),
        *args,
    ]


@pytest.fixture
def wheel_repair_tool(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts/windows/retag-wheels.py"
    spec = importlib.util.spec_from_file_location("swarm_wheel_repair", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(
            platform="cygwin",
            version_info=SimpleNamespace(major=3, minor=12),
            executable="/cygwin/python",
        ),
    )
    monkeypatch.setattr(
        module,
        "sysconfig",
        SimpleNamespace(
            get_platform=lambda: "cygwin-3.6.11-x86_64",
        ),
    )
    return module


def test_repair_only_known_locally_built_platform_tag(tmp_path, monkeypatch, wheel_repair_tool):
    wheel = tmp_path / "pydantic_core-2.50.1-cp312-cp312-cygwin_86_64.whl"
    wheel.touch()
    calls = []
    monkeypatch.setattr(
        wheel_repair_tool,
        "subprocess",
        SimpleNamespace(
            run=lambda *a, **kw: calls.append((a, kw)),
        ),
    )
    wheel_repair_tool.repair(tmp_path)
    assert calls == [
        (
            (
                [
                    "/cygwin/python",
                    "-m",
                    "wheel",
                    "tags",
                    "--platform-tag",
                    "cygwin_3_6_11_x86_64",
                    "--remove",
                    str(wheel),
                ],
            ),
            {"check": True},
        )
    ]


@pytest.mark.parametrize(
    "tag",
    ["cp312-cp312-win_amd64", "cp313-cp313-cygwin_86_64", "cp312-cp312-manylinux_2_17_x86_64"],
)
def test_repair_refuses_other_platforms_and_abis(tmp_path, wheel_repair_tool, tag):
    (tmp_path / f"pydantic_core-2.50.1-{tag}.whl").touch()
    with pytest.raises(RuntimeError, match="unknown ABI/platform"):
        wheel_repair_tool.repair(tmp_path)


def test_repair_leaves_correct_wheel_untouched(tmp_path, monkeypatch, wheel_repair_tool):
    (tmp_path / "pydantic_core-2.50.1-cp312-cp312-cygwin_3_6_11_x86_64.whl").touch()

    def unexpected(*args, **kwargs):
        pytest.fail("An already-correct wheel must not be rewritten")

    monkeypatch.setattr(wheel_repair_tool, "subprocess", SimpleNamespace(run=unexpected))
    wheel_repair_tool.repair(tmp_path)


def test_native_windows_python_gets_setup_instructions(monkeypatch):
    monkeypatch.setattr(entrypoint, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(SystemExit, match="swarm-windows.sh"):
        entrypoint.main(["doctor"])


@pytest.fixture
def cygwin_paths(monkeypatch):
    monkeypatch.setattr(paths, "cygwin", lambda: True)
    calls = []

    def translate(value, mode):
        calls.append((value, mode))
        if mode == "-au":
            return "/cygdrive/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
        assert mode == "-am"
        if value.startswith("/cygdrive/"):
            return value[10].upper() + ":/" + value[12:]
        return "C:/cygwin" + value

    monkeypatch.setattr(paths, "_cygpath", translate)
    return calls


@pytest.mark.parametrize(
    "value", [r"C:\Users\A B\project", "C:/Users/A B/project", "/c/Users/A B/project"]
)
def test_cygwin_accepts_native_and_git_bash_paths(value, cygwin_paths):
    assert paths.local_path(value) == Path("/cygdrive/c/Users/A B/project")


def test_native_agent_paths_and_unc(cygwin_paths, monkeypatch):
    assert paths.agent_path("/cygdrive/c/Users/A B/session.jsonl") == "C:/Users/A B/session.jsonl"
    with pytest.raises(ValueError, match="absolute"):
        paths.local_path("C:relative")
    # Do not silently collapse //server/share into a local /server/share path.
    with pytest.raises(ValueError, match="UNC network paths"):
        paths.local_path(r"\\server\share\project")
    with pytest.raises(ValueError, match="UNC network paths"):
        paths.local_path(r"\\?\UNC\server\share\project")


def test_cygpath_failure_is_actionable(monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError("cygpath")

    monkeypatch.setattr(paths.subprocess, "run", fail)
    with pytest.raises(ValueError, match="runtime is incomplete"):
        paths._cygpath("C:/workspace", "-au")


def test_posix_paths_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "cygwin", lambda: False)
    assert paths.local_path(tmp_path) == tmp_path
    assert paths.agent_path(tmp_path) == str(tmp_path)


def test_pi_adapter_maps_only_paths_not_prompt(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    session.touch()
    agent = Agent(name="Win", goal="Ship", folder=str(tmp_path), session_file=str(session))
    monkeypatch.setattr(adapters.PiAdapter, "available", lambda self: True)
    monkeypatch.setattr(adapters, "agent_path", lambda value: "native:" + str(value))
    prompt = "Keep C:/a and /cygdrive/c/b exactly; 'quotes' & Unicode: café"
    argv = adapters.PiAdapter().command(agent, prompt)
    assert argv[argv.index("--session") + 1] == "native:" + str(session)
    assert argv[argv.index("--skill") + 1] == "native:" + str(adapters.SKILL)
    assert argv[-2:] == ["--", prompt]
    assert "native:" + str(adapters.SKILL) in argv[argv.index("--append-system-prompt") + 1]


def test_reporting_accepts_native_alias_of_exact_session(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    session = tmp_path / "exact.jsonl"
    agent = store.create(
        Agent(name="Pi", goal="Ship", folder=str(tmp_path), managed=True, session_file=str(session))
    )
    monkeypatch.setenv("PI_SESSION_FILE", "C:/session/exact.jsonl")
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    monkeypatch.setattr(
        cli,
        "local_path",
        lambda value: session if str(value).startswith("C:") else Path(value).resolve(),
    )
    args = cli.parser().parse_args(["report", "--id", str(agent.id), "--status", "running"])
    assert cli.run_report(store, args)["session_file"] == str(session)


@pytest.mark.parametrize("response,expected", [("[]", []), ("[123,456]", [None, None])])
def test_native_process_discovery_is_conservative(monkeypatch, response, expected):
    def run(argv, **kwargs):
        assert argv[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
        assert "Win32_Process" in argv[-1] and "CommandLine" in argv[-1]
        assert kwargs["check"] and kwargs["timeout"]
        return SimpleNamespace(stdout=response)

    monkeypatch.setattr(sessions.subprocess, "run", run)
    monkeypatch.setattr(sessions, "cygwin", lambda: True)
    assert sessions.running_pi_folders() == expected


@pytest.mark.parametrize("response", ["not json", "123", '["123"]', "null"])
def test_native_process_discovery_fails_closed(monkeypatch, response):
    monkeypatch.setattr(
        sessions.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=response)
    )
    with pytest.raises(RuntimeError, match="adoption is disabled"):
        sessions.windows_pi_folders()


def test_native_process_discovery_command_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("powershell.exe", 15)

    monkeypatch.setattr(sessions.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="adoption is disabled"):
        sessions.windows_pi_folders()


def test_pi_shim_executes_node_without_shell_and_preserves_arguments(tmp_path, monkeypatch):
    node = tmp_path / "node.exe"
    entry = tmp_path / "pi cli.js"
    node.touch()
    entry.touch()
    monkeypatch.setattr(windows_pi, "cygwin", lambda: True)
    monkeypatch.setattr(windows_pi, "agent_path", lambda value: "native:" + str(value))
    monkeypatch.setenv("SWARM_WINDOWS_NODE", str(node))
    monkeypatch.setenv("SWARM_WINDOWS_PI_ENTRY", str(entry))
    monkeypatch.setenv("SWARM_AGENT_FILE", str(tmp_path / "agent.md"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi config"))
    calls = []
    monkeypatch.setattr(windows_pi.os, "execve", lambda *args: calls.append(args))
    args = ["--session", "C:/exact path/session.jsonl", "--", 'prompt " café & $HOME']
    windows_pi.launch(args)
    executable, argv, env = calls[0]
    assert executable == str(node)
    assert argv == ["native:" + str(node), "native:" + str(entry), *args]
    assert env["SWARM_AGENT_FILE"] == "native:" + str(tmp_path / "agent.md")
    assert env["PI_CODING_AGENT_DIR"] == "native:" + str(tmp_path / "pi config")


def test_pi_shim_rejects_wrong_runtime(monkeypatch):
    monkeypatch.setattr(windows_pi, "cygwin", lambda: False)
    with pytest.raises(RuntimeError, match="Cygwin Python"):
        windows_pi.launch([])


def test_pi_shim_missing_install_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_pi, "cygwin", lambda: True)
    monkeypatch.delenv("SWARM_WINDOWS_NODE", raising=False)
    with pytest.raises(ValueError, match="rerun setup-windows"):
        windows_pi.launch([])
    monkeypatch.setenv("SWARM_WINDOWS_NODE", str(tmp_path / "missing.exe"))
    with pytest.raises(ValueError, match="Missing Windows runtime artifact"):
        windows_pi.launch([])


def test_windows_session_header_normalizes_working_folder(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    folder = tmp_path / "project"
    folder.mkdir()
    path = root / "original.jsonl"
    monkeypatch.setattr(pi_history, "agent_path", lambda value: "C:/project")
    history = pi_history.PiHistory(
        "ea08fb61-7f1c-4025-a155-36d754fa206a", str(folder), "Converted session"
    )
    path.write_text(history.encode())
    assert json.loads(path.read_text().splitlines()[0])["cwd"] == "C:/project"
    original = sessions.local_path
    monkeypatch.setattr(
        sessions, "local_path", lambda value: folder if value == "C:/project" else original(value)
    )
    service = sessions.Sessions(Store(tmp_path / "home"), [root])
    assert service.metadata(path)["folder"] == str(folder)
