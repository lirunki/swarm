"""Native Chromium coverage for the multi-instance browser registry.

Every browser uses a temporary private profile, and every navigated page is
served on loopback.  These tests never discover or control a personal browser.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_remote_browser_native import local_pages, native_chromium  # noqa: F401

from swarm import edgemcp_launcher
from swarm.browser_registry import BrowserRegistry, BrowserRegistryConflict
from swarm.browser_settings import BrowserSettings
from swarm.remote_browser import RemoteBrowser


@pytest.fixture
def native_site(local_pages):  # noqa: F811
    """Reuse the native RemoteBrowser test's loopback-only HTTP fixture."""
    base_url, _state = local_pages
    return base_url


def _operation(registry: BrowserRegistry, **fields):
    return {
        "revision": registry.list()["revision"],
        "request_id": str(uuid4()),
        **fields,
    }


def _profile(registry: BrowserRegistry, browser_id: str) -> Path:
    return registry.instances / UUID(browser_id).hex / "remote-browser" / "profile"


def _wait_for_target(
    registry: BrowserRegistry,
    browser_id: str,
    target_id: str,
    *,
    timeout: float = 5,
):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = registry.request(browser_id, "tabs")
        if any(tab["id"] == target_id for tab in latest["tabs"]):
            return latest
        time.sleep(0.05)
    pytest.fail(f"browser {browser_id} did not expose tab {target_id} within {timeout}s: {latest!r}")


def _assert_png(content: bytes) -> None:
    assert isinstance(content, bytes)
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(content) > 24


def _stop_managed_browsers(registry: BrowserRegistry) -> None:
    """Gracefully stop every registry-owned browser, using fresh revisions."""
    try:
        for item in list(registry.list()["browsers"]):
            current = next(
                (
                    candidate
                    for candidate in registry.list()["browsers"]
                    if candidate["id"] == item["id"]
                ),
                None,
            )
            if current is not None and current["kind"] == "managed" and current["running"]:
                registry.stop(_operation(registry, id=current["id"]))
    finally:
        registry.close()


def test_native_registry_runs_two_isolated_managed_browsers(
    tmp_path, native_chromium, native_site  # noqa: F811
):
    home = tmp_path / "registry-home"
    registry = BrowserRegistry(home, BrowserSettings(home))
    processes: dict[str, subprocess.Popen] = {}

    try:
        first = registry.launch(
            _operation(
                registry,
                name="Native managed A",
                executable=str(native_chromium),
                headless=True,
            )
        )
        first_id = first["operation"]["browser_id"]
        second = registry.launch(
            _operation(
                registry,
                name="Native managed B",
                executable=str(native_chromium),
                headless=True,
            )
        )
        second_id = second["operation"]["browser_id"]

        listed = registry.list()
        by_id = {item["id"]: item for item in listed["browsers"]}
        assert set(by_id) == {first_id, second_id}
        assert first_id != second_id
        assert {by_id[first_id]["name"], by_id[second_id]["name"]} == {
            "Native managed A",
            "Native managed B",
        }
        assert all(by_id[browser_id]["running"] for browser_id in (first_id, second_id))
        ports = {by_id[first_id]["port"], by_id[second_id]["port"]}
        assert None not in ports and len(ports) == 2

        for browser_id in (first_id, second_id):
            profile = _profile(registry, browser_id)
            assert profile.is_dir()
            assert registry.instances in profile.parents
            if os.name != "nt":
                assert stat.S_IMODE(profile.stat().st_mode) == 0o700
            key = (by_id[browser_id]["port"], str(profile.resolve()))
            processes[browser_id] = edgemcp_launcher._ATTEMPTS[key]
            assert processes[browser_id].poll() is None

            assert registry.request(browser_id, "tabs")["tabs"]
            made = registry.request(
                browser_id,
                "new",
                {"url": f"{native_site}/fixture?browser={browser_id}"},
            )
            _wait_for_target(registry, browser_id, made["id"])
            _assert_png(
                registry.request(
                    browser_id,
                    "screenshot",
                    query={"target_id": made["id"]},
                )
            )

        first_profile = _profile(registry, first_id)
        marker = first_profile / "registry-preserves-profile.bin"
        marker_bytes = b"native managed profile remains after unregister\x00\xff"
        marker.write_bytes(marker_bytes)

        registry.stop(_operation(registry, id=first_id))
        processes[first_id].wait(timeout=10)
        assert processes[first_id].poll() is not None

        after_first_stop = {item["id"]: item for item in registry.list()["browsers"]}
        assert after_first_stop[first_id]["running"] is False
        assert after_first_stop[second_id]["running"] is True
        assert processes[second_id].poll() is None
        second_tabs = registry.request(second_id, "tabs")
        _assert_png(
            registry.request(
                second_id,
                "screenshot",
                query={"target_id": second_tabs["selected"]},
            )
        )

        registry.remove(_operation(registry, id=first_id))
        remaining = registry.list()["browsers"]
        assert [item["id"] for item in remaining] == [second_id]
        assert first_profile.is_dir()
        assert marker.read_bytes() == marker_bytes
        assert processes[first_id].poll() is not None
        assert edgemcp_launcher.probe_identity(by_id[first_id]["port"], timeout=1) is None
    finally:
        _stop_managed_browsers(registry)
        for process in processes.values():
            process.wait(timeout=10)


def test_native_registry_registers_external_without_owning_lifecycle(
    tmp_path, native_chromium  # noqa: F811
):
    external_home = tmp_path / "external-home"
    external_settings = BrowserSettings(external_home)
    external_settings.apply(
        {
            "revision": 0,
            "request_id": str(uuid4()),
            "enabled": True,
            "headless": True,
            "executable": str(native_chromium),
        }
    )
    external = RemoteBrowser(external_home, external_settings)
    external_process: subprocess.Popen | None = None

    registry_home = tmp_path / "registry-home"
    registry = BrowserRegistry(registry_home, BrowserSettings(registry_home))
    try:
        launched = external.launch(uuid4())
        assert launched["running"] is True
        assert external.status()["state"] == "running"
        state = external._read_state()
        assert state is not None
        assert state["browser_id"] is not None
        assert external.profile == external_home.resolve() / "remote-browser" / "profile"
        if os.name != "nt":
            assert stat.S_IMODE(external.profile.stat().st_mode) == 0o700

        process_key = (state["port"], str(external.profile.resolve()))
        external_process = edgemcp_launcher._ATTEMPTS[process_key]
        assert external_process.poll() is None

        added = registry.add_existing(
            _operation(registry, name="Native external", port=state["port"])
        )
        registered_id = added["operation"]["browser_id"]
        item = next(browser for browser in added["browsers"] if browser["id"] == registered_id)
        assert item["kind"] == "external"
        assert item["port"] == state["port"]
        assert item["running"] is True
        assert item["can_stop"] is False
        record = next(
            browser
            for browser in registry._snapshot()["browsers"]
            if browser["id"] == registered_id
        )
        assert record["browser_uuid"] == state["browser_id"]
        assert registry.request(registered_id, "tabs")["tabs"]

        with pytest.raises(BrowserRegistryConflict, match="cannot be stopped"):
            registry.stop(_operation(registry, id=registered_id))
        assert external.status()["running"] is True
        assert external_process.poll() is None

        registry.remove(_operation(registry, id=registered_id))
        assert registry.list()["browsers"] == []
        assert external.status()["running"] is True
        assert external_process.poll() is None
    finally:
        registry.close()
        state = external._read_state()
        if state is not None and external.status()["running"]:
            external.stop(uuid4())
        external.close()
        if external_process is not None:
            external_process.wait(timeout=10)
