from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm import edgemcp_launcher
from swarm.remote_browser import RemoteBrowser
from swarm.server import create_app


def executable(path: Path, content: bytes = b"\x7fELFfixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)
    return path


def test_linux_discovery_preserves_snap_wrapper_path(tmp_path):
    root = tmp_path / "root"
    snap = executable(root / "usr/bin/snap")
    chromium = root / "snap/bin/chromium"
    chromium.parent.mkdir(parents=True)
    chromium.symlink_to(snap)

    result = edgemcp_launcher.discover_browsers(
        platform="linux", environ={}, search_path=str(chromium.parent), root=root
    )

    assert result == {
        "browsers": [
            {"name": "Chromium", "executable": str(chromium), "supported": True}
        ],
        "unavailable": [],
    }
    assert result["browsers"][0]["executable"] != str(snap)


def test_find_browser_uses_first_supported_discovery_path_without_resolving(
    tmp_path, monkeypatch
):
    target = executable(tmp_path / "snap")
    wrapper = tmp_path / "snap-bin/chromium"
    wrapper.parent.mkdir()
    wrapper.symlink_to(target)
    result = {
        "browsers": [
            {"name": "Chromium", "executable": str(wrapper), "supported": True},
            {"name": "Chromium", "executable": str(target), "supported": True},
        ],
        "unavailable": [],
    }
    monkeypatch.setattr(edgemcp_launcher, "discover_browsers", lambda: result)

    assert edgemcp_launcher._find_browser(None) == wrapper


def test_configured_browser_symlink_is_returned_lexically(tmp_path):
    target = executable(tmp_path / "browser-target")
    alias = tmp_path / "configured-browser"
    alias.symlink_to(target)

    assert edgemcp_launcher._find_browser(str(alias)) == alias


def test_launchable_path_rejects_disguised_pe_and_arbitrary_text(tmp_path):
    pe_target = executable(tmp_path / "browser-target", b"MZfixture")
    pe_alias = tmp_path / "browser-alias"
    pe_alias.symlink_to(pe_target)
    text = executable(tmp_path / "chromium", b"not an executable format")

    assert edgemcp_launcher._launchable_path(pe_alias, platform="linux") is None
    assert edgemcp_launcher._launchable_path(text, platform="linux") is None
    with pytest.raises(RuntimeError, match="native browser executable"):
        edgemcp_launcher._find_browser(str(text))


def test_windows_launchable_requires_pe_target_with_exe_suffix(tmp_path):
    target = executable(tmp_path / "browser-target", b"MZfixture")
    alias = tmp_path / "msedge.exe"
    alias.symlink_to(target)

    assert edgemcp_launcher._launchable_path(alias, platform="win32") is None


def test_windows_discovery_accepts_native_pe_fixture(tmp_path):
    program_files = tmp_path / "Program Files"
    edge = executable(
        program_files / "Microsoft/Edge/Application/msedge.exe", b"MZfixture"
    )

    result = edgemcp_launcher.discover_browsers(
        platform="win32",
        environ={"PROGRAMFILES": str(program_files)},
        search_path="",
    )

    assert result == {
        "browsers": [
            {"name": "Microsoft Edge", "executable": str(edge), "supported": True}
        ],
        "unavailable": [],
    }


def test_native_validation_reads_only_header_without_binary_size_limit(tmp_path):
    browser = executable(tmp_path / "browser", b"\x7fELF" + b"x" * (65 * 1024))

    assert edgemcp_launcher._launchable_path(browser, platform="linux") == browser


@pytest.mark.parametrize(
    ("platform", "header"),
    [
        ("linux", b"#!/bin/sh\n"),
        ("darwin", b"\xcf\xfa\xed\xfe"),
        ("darwin", b"#!/bin/sh\n"),
    ],
)
def test_native_validation_accepts_platform_formats(tmp_path, platform, header):
    browser = executable(tmp_path / f"browser-{platform}-{len(header)}", header)

    assert edgemcp_launcher._launchable_path(browser, platform=platform) == browser


def test_linux_discovery_prioritizes_edge_aliases_before_chrome_and_chromium(tmp_path):
    commands = tmp_path / "bin"
    edge = executable(commands / "msedge")
    chrome = executable(commands / "google-chrome")
    chromium = executable(commands / "chromium-browser")

    result = edgemcp_launcher.discover_browsers(
        platform="linux", environ={}, search_path=str(commands), root=tmp_path / "empty-root"
    )

    assert [(item["name"], item["executable"]) for item in result["browsers"]] == [
        ("Microsoft Edge", str(edge)),
        ("Google Chrome", str(chrome)),
        ("Chromium", str(chromium)),
    ]


def test_wsl_windows_edge_is_reported_as_unavailable_not_launchable(tmp_path):
    root = tmp_path / "root"
    windows_edge = executable(
        root / "mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", b"MZfixture"
    )

    result = edgemcp_launcher.discover_browsers(
        platform="linux",
        environ={"WSL_DISTRO_NAME": "Fixture"},
        search_path="",
        root=root,
    )

    assert result["browsers"] == []
    assert result["unavailable"] == [{
        "name": "Windows Microsoft Edge",
        "executable": str(windows_edge),
        "reason": "Use native Windows Swarm or install Edge/Chromium inside WSL.",
    }]


def test_remote_discovery_is_read_only(tmp_path):
    class Launcher:
        @staticmethod
        def discover_browsers():
            return {"browsers": [], "unavailable": []}

    remote = RemoteBrowser(
        tmp_path / "home",
        {"enabled": False, "headless": True, "executable": None, "cdp_port": 0},
        launcher=Launcher(),
    )

    assert remote.discovery() == {"browsers": [], "unavailable": []}
    assert not remote.root.exists()


def test_discovery_api_is_operator_authenticated_and_keeps_settings_projection(tmp_path):
    password = tmp_path / "password.txt"
    password.write_text("discovery-password\n", encoding="utf-8")
    password.chmod(0o600)
    app = create_app(
        tmp_path / "home",
        recover=False,
        allowed_hosts=["testserver"],
        password_file=password,
    )
    app.state.controller.processes.health = lambda: {}
    expected = {
        "browsers": [
            {"name": "Chromium", "executable": "/snap/bin/chromium", "supported": True}
        ],
        "unavailable": [],
    }
    app.state.remote_browser.discovery = lambda: expected

    with TestClient(app) as client:
        assert client.get("/api/browser/discovery").status_code == 401
        login = client.post("/api/auth/login", json={"password": "discovery-password"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        response = client.get("/api/browser/discovery", headers=headers)
        settings = client.get("/api/browser/settings", headers=headers)

    assert response.status_code == 200
    assert response.json() == expected
    assert settings.json() == {
        "revision": 0,
        "enabled": False,
        "headless": True,
        "executable": None,
    }
    assert not app.state.remote_browser.root.exists()
