import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import swarm.browser_settings as browser_settings_module
from swarm import edgemcp_launcher, wsl_edge
from swarm.browser_settings import BrowserSettings, BrowserSettingsError


def fixture_file(path: Path, content: bytes = b"MZfixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)
    return path


def decode_script(command: list[str]) -> str:
    encoded = command[command.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16le")


def test_wsl_launch_uses_fixed_encoded_script_and_validated_environment(tmp_path, monkeypatch):
    calls = []

    def run(command, **options):
        calls.append((command, options))
        port = int(options["env"]["SWARM_EDGE_CDP_PORT"])
        tag = options["env"]["SWARM_EDGE_PROFILE_TAG"]
        payload = {
            "stage": "launched",
            "pid": 4321,
            "birth": 638500000000000000,
            "profile": f"C:\\Users\\Fixture\\AppData\\Local\\Swarm\\Edge-CDP-{port}-{tag}",
            "executable": "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
            "port": port,
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")

    profile = tmp_path / "profile with spaces; Write-Host PWNED"
    monkeypatch.setattr(wsl_edge, "powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(wsl_edge.subprocess, "run", run)
    wsl_edge.WSL_ATTEMPTS.clear()

    handle = wsl_edge.launch(9333, profile, headless=True)

    command, options = calls[0]
    script = decode_script(command)
    assert command[:4] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]
    assert str(profile) not in script
    assert "Write-Host PWNED" not in script
    assert "--remote-debugging-address=127.0.0.1" in script
    assert "--headless=new" in script
    assert "--no-sandbox" not in script.lower()
    assert "ProgramFiles(x86)" in script and "$env:ProgramFiles" in script
    assert "$env:LOCALAPPDATA" in script
    assert "TcpListener" in script and "Start-Process" in script
    assert options["shell"] is False and options["timeout"] == 10
    assert options["env"]["SWARM_EDGE_CDP_PORT"] == "9333"
    assert options["env"]["SWARM_EDGE_HEADLESS"] == "1"
    assert options["env"]["SWARM_EDGE_PROFILE_TAG"] == hashlib.sha256(
        str(profile.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    assert handle.pid == 4321 and handle.poll() is None
    assert not profile.exists()


def test_windows_profile_is_dedicated_by_port_and_source_profile(tmp_path, monkeypatch):
    profiles = []

    def run(_command, **options):
        env = options["env"]
        windows_profile = (
            "C:\\Local\\Swarm\\Edge-CDP-"
            f"{env['SWARM_EDGE_CDP_PORT']}-{env['SWARM_EDGE_PROFILE_TAG']}"
        )
        profiles.append(windows_profile)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "stage": "launched",
            "pid": 1000 + len(profiles),
            "birth": 638500000000000000 + len(profiles),
            "profile": windows_profile,
            "executable": "C:\\Edge\\msedge.exe",
            "port": int(env["SWARM_EDGE_CDP_PORT"]),
        }).encode(), stderr=b"")

    monkeypatch.setattr(wsl_edge, "powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(wsl_edge.subprocess, "run", run)
    wsl_edge.WSL_ATTEMPTS.clear()

    wsl_edge.launch(9001, tmp_path / "one")
    wsl_edge.launch(9001, tmp_path / "two")
    wsl_edge.launch(9002, tmp_path / "one")

    assert len(set(profiles)) == 3
    assert all("Edge-CDP-" in profile for profile in profiles)


def test_bridge_failure_is_sanitized_and_attempt_is_not_recorded(tmp_path, monkeypatch):
    secret = "secret-from-powershell"
    monkeypatch.setattr(wsl_edge, "powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(wsl_edge.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout=b"", stderr=secret.encode()
    ))
    wsl_edge.WSL_ATTEMPTS.clear()

    with pytest.raises(wsl_edge.WslEdgeLaunchError, match="launch bridge failed") as error:
        wsl_edge.launch(9444, tmp_path / "profile")

    assert secret not in str(error.value)
    assert error.value.started is True and error.value.definite is False
    assert wsl_edge.WSL_ATTEMPTS == {}


def test_structured_preflight_failure_is_definite_and_preserves_safe_message(tmp_path, monkeypatch):
    payload = {"stage": "preflight", "error": "Microsoft Edge was not found on Windows"}
    monkeypatch.setattr(wsl_edge, "powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(wsl_edge.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout=json.dumps(payload).encode(), stderr=b"private command text"
    ))
    wsl_edge.WSL_ATTEMPTS.clear()

    with pytest.raises(wsl_edge.WslEdgeLaunchError, match="Microsoft Edge was not found") as caught:
        wsl_edge.launch(9445, tmp_path / "profile")

    assert caught.value.started is False
    assert caught.value.definite is True
    assert caught.value.code == "preflight_failed"
    assert "private command text" not in str(caught.value)


def test_windows_handle_poll_requires_fixed_identity_proof(monkeypatch):
    results = iter([
        {"state": "owned", "pid": 2222, "birth": 638500000000000000},
        {"state": "gone", "pid": None, "birth": None},
    ])
    monkeypatch.setattr(wsl_edge, "_inspect", lambda *_args: next(results))
    handle = wsl_edge.WindowsEdgeHandle(
        2222, 638500000000000000, "C:\\profile", "C:\\Edge\\msedge.exe", 9555, "a" * 16
    )

    assert handle.poll() is None
    handle._checked_at -= wsl_edge.INSPECT_CACHE_SECONDS
    assert handle.poll() == 0


def test_sentinel_validation_requires_linux_wsl(monkeypatch):
    monkeypatch.setattr(browser_settings_module.sys, "platform", "linux")
    monkeypatch.setattr(browser_settings_module.paths, "is_wsl", lambda: True)
    assert BrowserSettings._validate_executable("wsl:msedge") == "wsl:msedge"

    monkeypatch.setattr(browser_settings_module.paths, "is_wsl", lambda: False)
    with pytest.raises(BrowserSettingsError, match="only from WSL"):
        BrowserSettings._validate_executable("wsl:msedge")

    monkeypatch.setattr(browser_settings_module.sys, "platform", "win32")
    monkeypatch.setattr(browser_settings_module.paths, "is_wsl", lambda: True)
    with pytest.raises(BrowserSettingsError, match="only from WSL"):
        BrowserSettings._validate_executable("wsl:msedge")


def test_discovery_offers_wsl_mode_only_with_edge_and_powershell(tmp_path):
    root = tmp_path / "root"
    fixture_file(root / "mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    fixture_file(
        root / "mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    )

    result = edgemcp_launcher.discover_browsers(
        platform="linux",
        environ={"WSL_DISTRO_NAME": "Fixture"},
        search_path="",
        root=root,
    )

    assert {"name": "Microsoft Edge via WSL", "executable": "wsl:msedge", "supported": True} in result["browsers"]
    assert result["unavailable"] == []


def test_auto_discovery_never_selects_wsl_sentinel(monkeypatch):
    monkeypatch.setattr(edgemcp_launcher, "discover_browsers", lambda: {
        "browsers": [{
            "name": "Microsoft Edge via WSL",
            "executable": "wsl:msedge",
            "supported": True,
        }],
        "unavailable": [],
    })

    with pytest.raises(RuntimeError, match="not found"):
        edgemcp_launcher._find_browser(None)


def test_explicit_sentinel_passes_without_path_resolution(monkeypatch):
    monkeypatch.setattr(edgemcp_launcher.sys, "platform", "linux")
    monkeypatch.setattr(edgemcp_launcher.paths, "is_wsl", lambda **_kwargs: True)
    monkeypatch.setattr(
        edgemcp_launcher, "_launchable_path",
        lambda *_args, **_kwargs: pytest.fail("sentinel must not be resolved as a path"),
    )

    assert edgemcp_launcher._find_browser("wsl:msedge") == "wsl:msedge"


def test_parent_launcher_dispatches_explicit_wsl_mode(tmp_path, monkeypatch):
    browser_id = "11111111-1111-4111-8111-111111111111"
    probes = iter([None, {"browser": "Edg/123", "browser_id": browser_id}])
    launched = []
    handle = SimpleNamespace(pid=2222, poll=lambda: None, owns_port=lambda: True)

    monkeypatch.setattr(edgemcp_launcher.sys, "platform", "linux")
    monkeypatch.setattr(edgemcp_launcher.paths, "is_wsl", lambda **_kwargs: True)
    monkeypatch.setattr(edgemcp_launcher, "probe_identity", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(
        edgemcp_launcher.wsl_edge,
        "launch",
        lambda *args, **kwargs: launched.append((args, kwargs)) or handle,
    )
    profile = tmp_path / "profile"
    key = (9555, str(profile.resolve()))
    edgemcp_launcher._ATTEMPTS.pop(key, None)
    edgemcp_launcher._READY.discard(key)

    result = edgemcp_launcher.launch(
        9555, executable="wsl:msedge", profile=profile, headless=True, timeout=1
    )

    assert launched == [((9555, profile.resolve()), {
        "executable": "wsl:msedge", "headless": True,
    })]
    assert result == {
        "started": True,
        "already_running": False,
        "browser": "Edg/123",
        "browser_id": browser_id,
        "launch_pid": 2222,
    }
    edgemcp_launcher._ATTEMPTS.pop(key, None)
    edgemcp_launcher._READY.discard(key)


def test_parent_launcher_does_not_claim_foreign_windows_listener(tmp_path, monkeypatch):
    browser_id = "22222222-2222-4222-8222-222222222222"
    probes = iter([None, {"browser": "Edg/123", "browser_id": browser_id}])
    handle = SimpleNamespace(pid=3333, poll=lambda: None, owns_port=lambda: False)
    monkeypatch.setattr(edgemcp_launcher, "probe_identity", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(edgemcp_launcher, "_find_browser", lambda _configured: wsl_edge.SENTINEL)
    monkeypatch.setattr(edgemcp_launcher.wsl_edge, "launch", lambda *_args, **_kwargs: handle)
    profile = tmp_path / "foreign-profile"
    key = (9666, str(profile.resolve()))
    edgemcp_launcher._ATTEMPTS.pop(key, None)
    edgemcp_launcher._READY.discard(key)

    with pytest.raises(wsl_edge.WslEdgeLaunchError, match="different process") as caught:
        edgemcp_launcher.launch(
            9666, executable=wsl_edge.SENTINEL, profile=profile, timeout=1
        )

    assert caught.value.started is True and caught.value.definite is False
    assert key not in edgemcp_launcher._READY
    edgemcp_launcher._ATTEMPTS.pop(key, None)
