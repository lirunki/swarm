import fcntl
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

import swarm.browser_settings as browser_settings_module
from swarm.browser_settings import (
    MAX_FILE_BYTES,
    BrowserSettings,
    BrowserSettingsConflict,
    BrowserSettingsCorrupt,
    BrowserSettingsError,
)
from swarm.remote_browser import RemoteBrowser


def request(*, revision=0, request_id=None, enabled=True, headless=True, executable=None):
    return {
        "revision": revision,
        "request_id": str(request_id or uuid4()),
        "enabled": enabled,
        "headless": headless,
        "executable": executable,
    }


def write_with_mode(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    finally:
        os.umask(old_umask)
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)


def write_private(path, content):
    write_with_mode(path, content, 0o600)


@pytest.fixture
def native_executable(tmp_path):
    source = Path(sys.executable).resolve()
    target = tmp_path / source.name
    shutil.copyfile(source, target)
    target.chmod(0o700)
    return str(target)


def test_default_get_is_read_only_and_first_apply_is_private(tmp_path, native_executable):
    settings = BrowserSettings(tmp_path / "home")

    assert settings.get() == {
        "revision": 0,
        "enabled": False,
        "headless": True,
        "executable": None,
    }
    assert not settings.path.exists()

    result = settings.apply(
        request(enabled=True, headless=False, executable=native_executable)
    )
    assert result == {
        "revision": 1,
        "enabled": True,
        "headless": False,
        "executable": native_executable,
    }
    assert settings.path.stat().st_mode & 0o777 == 0o600
    assert settings.lock_path.stat().st_mode & 0o777 == 0o600
    assert BrowserSettings(settings.home).get() == result


def test_config_is_only_public_fields_plus_fixed_automatic_port(tmp_path):
    settings = BrowserSettings(tmp_path / "home")
    assert settings.config() == {
        "revision": 0,
        "enabled": False,
        "headless": True,
        "executable": None,
        "cdp_port": 0,
    }
    assert RemoteBrowser(tmp_path, settings)._config() == {
        "enabled": False,
        "headless": True,
        "executable": None,
    }
    assert "profile" not in settings.config()


@pytest.mark.parametrize("field", ["enabled", "headless"])
@pytest.mark.parametrize("value", [0, 1, "true", None, [], {}])
def test_boolean_fields_are_strict(tmp_path, field, value):
    settings = BrowserSettings(tmp_path / str(uuid4()))
    with pytest.raises(BrowserSettingsError, match=field):
        settings.apply(request(**{field: value}))
    assert not settings.path.exists()


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"revision": 0},
        request() | {"extra": False},
        request(revision=True),
        request(revision=-1),
        request(request_id="not-a-uuid"),
    ],
)
def test_request_schema_is_exact(tmp_path, body):
    settings = BrowserSettings(tmp_path / str(uuid4()))
    with pytest.raises(BrowserSettingsError):
        settings.apply(body)
    assert not settings.path.exists()


def test_request_payload_is_bounded_before_path_validation(tmp_path):
    settings = BrowserSettings(tmp_path / "home")
    oversized = request(executable="/" + "x" * (16 * 1024))
    with pytest.raises(BrowserSettingsError, match="16 KiB"):
        settings.apply(oversized)
    assert not settings.path.exists()


@pytest.mark.parametrize("value", ["browser", "./browser", "", "/tmp/a\nbrowser"])
def test_executable_requires_clean_absolute_path(tmp_path, value):
    settings = BrowserSettings(tmp_path / str(uuid4()))
    with pytest.raises(BrowserSettingsError, match="executable|control|absolute"):
        settings.apply(request(executable=value))


def test_executable_accepts_native_shebang_and_symlink_paths_lexically(
    tmp_path, native_executable
):
    native_settings = BrowserSettings(tmp_path / "native-home")
    assert native_settings.apply(request(executable=native_executable))["executable"] == str(
        Path(native_executable)
    )

    script = tmp_path / "browser-wrapper"
    script.write_text("#!/bin/sh\nexec chromium \"$@\"\n")
    script.chmod(0o700)
    script_settings = BrowserSettings(tmp_path / "script-home")
    assert script_settings.apply(request(executable=str(script)))["executable"] == str(script)

    link = tmp_path / "browser-link"
    link.symlink_to(script)
    link_settings = BrowserSettings(tmp_path / "link-home")
    assert link_settings.apply(request(executable=str(link)))["executable"] == str(link)


def test_executable_rejects_missing_nonregular_and_symlink_loops(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    loop = tmp_path / "loop"
    loop.symlink_to(loop.name)

    for index, path in enumerate((tmp_path / "missing", directory, loop)):
        settings = BrowserSettings(tmp_path / f"home-{index}")
        with pytest.raises(BrowserSettingsError):
            settings.apply(request(executable=str(path)))
        assert not settings.path.exists()


def test_linux_rejects_windows_pe_with_actionable_message(tmp_path, monkeypatch):
    executable = tmp_path / "msedge"
    executable.write_bytes(b"MZ" + b"x" * 126)
    executable.chmod(0o700)
    monkeypatch.setattr(browser_settings_module.sys, "platform", "linux")

    with pytest.raises(BrowserSettingsError, match="Windows.*WSL/Linux"):
        BrowserSettings(tmp_path / "home").apply(request(executable=str(executable)))


def test_windows_accepts_native_mz_executable_without_resolving_alias(
    tmp_path, monkeypatch
):
    target = tmp_path / "msedge.exe"
    target.write_bytes(b"MZ" + b"x" * 126)
    target.chmod(0o700)
    alias = tmp_path / "configured-msedge.exe"
    alias.symlink_to(target)
    monkeypatch.setattr(browser_settings_module.sys, "platform", "win32")

    result = BrowserSettings(tmp_path / "home").apply(request(executable=str(alias)))
    assert result["executable"] == str(alias)


def test_executable_header_read_is_bounded_to_128_bytes(tmp_path, monkeypatch):
    wrapper = tmp_path / "browser-wrapper"
    wrapper.write_bytes(b"#!/bin/sh\n" + b"x" * 4096)
    wrapper.chmod(0o700)
    real_read = browser_settings_module.os.read
    sizes = []

    def bounded_read(descriptor, size):
        sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(browser_settings_module.os, "read", bounded_read)
    BrowserSettings(tmp_path / "home").apply(request(executable=str(wrapper)))
    assert sizes == [128]


def test_nonexecutable_native_file_is_rejected(tmp_path, native_executable):
    if os.name == "nt":
        pytest.skip("Windows execution permission is extension/ACL based")
    Path(native_executable).chmod(0o600)
    with pytest.raises(BrowserSettingsError, match="not executable"):
        BrowserSettings(tmp_path / "home").apply(request(executable=native_executable))


def test_revision_receipts_are_checked_before_stale_revision(tmp_path):
    settings = BrowserSettings(tmp_path / "home")
    operation = request(enabled=True, headless=False)
    first = settings.apply(operation)

    assert settings.apply(operation) == first
    with pytest.raises(BrowserSettingsConflict, match="different payload"):
        settings.apply(operation | {"headless": True})
    with pytest.raises(BrowserSettingsConflict, match="stale"):
        settings.apply(request(revision=0, enabled=False))
    assert settings.apply(request(revision=1, enabled=False))["revision"] == 2


def test_receipt_hashes_are_canonical_and_bounded_to_256(tmp_path):
    settings = BrowserSettings(tmp_path / "home")
    for revision in range(260):
        settings.apply(request(revision=revision, enabled=bool(revision % 2)))

    saved = json.loads(settings.path.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and saved["revision"] == 260
    assert len(saved["receipts"]) == 256
    assert all(
        len(item["payload_hash"]) == 64
        and set(item["payload_hash"]) <= set("0123456789abcdef")
        for item in saved["receipts"]
    )
    assert settings.path.stat().st_size <= MAX_FILE_BYTES


def test_corrupt_content_fails_closed_without_echo_or_rewrite(tmp_path):
    settings = BrowserSettings(tmp_path / "home")
    secret = b'{"credential":"must-not-echo"'
    write_private(settings.path, secret)

    for operation in (settings.get, lambda: settings.apply(request())):
        with pytest.raises(BrowserSettingsCorrupt) as error:
            operation()
        assert "must-not-echo" not in str(error.value)
    assert settings.path.read_bytes() == secret


@pytest.mark.parametrize("kind", ["oversized", "wrong-mode", "symlink", "hardlink"])
def test_unsafe_settings_file_is_rejected_and_never_repaired(tmp_path, kind):
    settings = BrowserSettings(tmp_path / "home")
    content = json.dumps(BrowserSettings.empty()).encode()
    if kind == "oversized":
        write_private(settings.path, b" " * (MAX_FILE_BYTES + 1))
    elif kind == "wrong-mode":
        write_with_mode(settings.path, content, 0o640)
    else:
        source = tmp_path / "source"
        write_private(source, content)
        if kind == "symlink":
            settings.path.parent.mkdir(parents=True, exist_ok=True)
            settings.path.symlink_to(source)
        else:
            settings.path.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, settings.path)

    with pytest.raises(BrowserSettingsCorrupt):
        settings.get()
    if kind == "wrong-mode":
        assert settings.path.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "hardlink", "wrong-mode"])
def test_lock_must_be_private_regular_owned_single_link(tmp_path, kind):
    settings = BrowserSettings(tmp_path / "home")
    settings.home.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(settings.lock_path, 0o600)
    elif kind == "directory":
        settings.lock_path.mkdir(mode=0o600)
    elif kind == "wrong-mode":
        write_with_mode(settings.lock_path, b"", 0o640)
    else:
        source = tmp_path / "lock-source"
        write_private(source, b"")
        if kind == "symlink":
            settings.lock_path.symlink_to(source)
        else:
            os.link(source, settings.lock_path)

    with pytest.raises(BrowserSettingsCorrupt):
        settings.get()
    if kind == "wrong-mode":
        assert settings.lock_path.stat().st_mode & 0o777 == 0o640


def test_lock_timeout_is_bounded_and_preserves_settings(tmp_path, monkeypatch):
    settings = BrowserSettings(tmp_path / "home")
    settings.apply(request())
    original = settings.path.read_bytes()
    held = os.open(settings.lock_path, os.O_RDWR | os.O_NONBLOCK)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = [50.0]

    monkeypatch.setattr(browser_settings_module, "LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(browser_settings_module, "LOCK_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(browser_settings_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        browser_settings_module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    try:
        with pytest.raises(BrowserSettingsCorrupt, match="temporarily unavailable"):
            settings.apply(request(revision=1, enabled=False))
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    assert clock[0] <= 50.06
    assert settings.path.read_bytes() == original


def test_read_rejects_file_changed_while_descriptor_is_open(tmp_path, monkeypatch):
    settings = BrowserSettings(tmp_path / "home")
    settings.apply(request())
    real_read = os.read
    changed = False

    def changing_read(descriptor, size):
        nonlocal changed
        content = real_read(descriptor, size)
        if not changed and descriptor != -1:
            changed = True
            with settings.path.open("r+b") as stream:
                stream.write(b" ")
                stream.flush()
                os.fsync(stream.fileno())
        return content

    monkeypatch.setattr(browser_settings_module.os, "read", changing_read)
    with pytest.raises(BrowserSettingsCorrupt):
        settings.get()


def test_concurrent_same_revision_has_exactly_one_winner(tmp_path):
    home = tmp_path / "home"
    services = (BrowserSettings(home), BrowserSettings(home))
    requests = (request(enabled=True), request(enabled=False, headless=False))

    def apply(item):
        service, body = item
        try:
            return "ok", service.apply(body)
        except BrowserSettingsConflict as exc:
            return "conflict", str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, zip(services, requests)))

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    saved = json.loads(services[0].path.read_text(encoding="utf-8"))
    winner = next(body for body, result in zip(requests, results) if result[0] == "ok")
    normalized, payload_hash = BrowserSettings._request(winner)
    assert saved["revision"] == 1
    assert saved["enabled"] is normalized["enabled"]
    assert saved["headless"] is normalized["headless"]
    assert saved["receipts"] == [
        {"request_id": normalized["request_id"], "payload_hash": payload_hash}
    ]
