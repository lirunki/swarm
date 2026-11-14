import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4, uuid5

import pytest

from swarm.browser_registry import (
    BrowserRegistry,
    BrowserRegistryConflict,
    BrowserRegistryCorrupt,
    BrowserRegistryError,
    BrowserRegistryUncertain,
)
from swarm.remote_browser import BrowserError


class FakeLauncher:
    def __init__(self):
        self.identities = {}
        self.launches = []
        self.stops = []
        self.probes = []
        self.fail_after_launch = False
        self.fail_after_stop = False

    def probe_identity(self, port, timeout=1, host="127.0.0.1"):
        self.probes.append((host, port, timeout))
        identity = self.identities.get((host, port), self.identities.get(port))
        return dict(identity) if identity else None

    def launch(self, port, **options):
        self.launches.append((port, options))
        identity = self.identities.get(port)
        if identity is None:
            identity = {
                "browser": "Chromium/Fake",
                "browser_id": str(uuid4()),
            }
            self.identities[port] = identity
        if self.fail_after_launch:
            raise RuntimeError("launch response was lost")
        return {"started": True, **identity}

    def stop_browser(self, port, browser_id, timeout=5):
        self.stops.append((port, browser_id, timeout))
        identity = self.identities.get(port)
        if identity and identity["browser_id"] != browser_id:
            raise RuntimeError("different browser")
        self.identities.pop(port, None)
        if self.fail_after_stop:
            raise RuntimeError("stop response was lost")
        return {"running": False, "stopped": identity is not None}


class FakeControls:
    made = []

    def __init__(self, port, *, host="127.0.0.1"):
        self.port = port
        self.host = host
        self.closed = False
        self.calls = []
        self.__class__.made.append(self)

    def tabs(self):
        self.calls.append("tabs")
        return {
            "tabs": [], "selected": None, "source_host": self.host,
            "source_port": self.port,
        }

    def close(self):
        self.closed = True


def operation(revision, **fields):
    return {"revision": revision, "request_id": str(uuid4()), **fields}


@pytest.fixture(autouse=True)
def reset_controls():
    FakeControls.made = []


@pytest.fixture
def registry(tmp_path):
    launcher = FakeLauncher()
    value = BrowserRegistry(
        tmp_path / "home",
        {"enabled": False, "headless": False, "executable": None, "cdp_port": 0},
        launcher=launcher,
        browser_factory=FakeControls,
    )
    return value, launcher


def test_default_list_is_read_only_and_launches_isolated_managed_profiles(registry):
    value, launcher = registry
    assert value.list() == {"revision": 0, "browsers": []}
    assert not value.root.exists()

    first_request = operation(0, name="One", executable=None, headless=True)
    first = value.launch(first_request)
    second = value.launch(operation(1, name="Two", executable=None, headless=False))

    assert first["revision"] == 1 and second["revision"] == 2
    first_id = first["operation"]["browser_id"]
    assert first_id == str(uuid5(UUID(first_request["request_id"]), "browser-instance"))
    assert {item["name"] for item in second["browsers"]} == {"One", "Two"}
    profiles = [options["profile"] for _port, options in launcher.launches]
    assert len(set(profiles)) == 2
    assert profiles[0] == value.instances / UUID(first_id).hex / "remote-browser" / "profile"
    assert all(value.instances in profile.parents for profile in profiles)
    saved = json.loads(value.path.read_text())
    assert all("home" not in item and "profile" not in item for item in saved["browsers"])
    assert value.path.stat().st_mode & 0o777 == 0o600


def test_names_revisions_and_receipt_payloads_are_strict(registry):
    value, _launcher = registry
    request = operation(0, name="Browser", executable=None, headless=True)
    first = value.launch(request)
    replay = value.launch(request)
    assert replay["operation"] == first["operation"]
    assert replay["revision"] == 1

    with pytest.raises(BrowserRegistryConflict, match="different payload"):
        value.launch(request | {"name": "Different"})
    with pytest.raises(BrowserRegistryConflict, match="stale"):
        value.launch(operation(0, name="Other", executable=None, headless=True))
    with pytest.raises(BrowserRegistryConflict, match="name"):
        value.launch(operation(1, name="bRoWsEr", executable=None, headless=True))
    with pytest.raises(BrowserRegistryError):
        value.rename(operation(1, id=first["operation"]["browser_id"], name="bad\nname"))
    with pytest.raises(BrowserRegistryError, match="missing or unexpected"):
        value.stop(operation(1, id=first["operation"]["browser_id"], extra=True))


def test_external_registration_verifies_owner_on_every_list_and_request(registry):
    value, launcher = registry
    browser_uuid = str(uuid4())
    launcher.identities[9222] = {"browser": "Chromium/External", "browser_id": browser_uuid}

    added = value.add_existing(operation(0, name="Attached", port=9222))
    browser_id = added["operation"]["browser_id"]
    item = added["browsers"][0]
    assert item | {"running": True} == item
    assert item["kind"] == "external" and item["can_stop"] is False
    assert item["executable"] is None and item["headless"] is False
    assert value.request(browser_id, "tabs")["source_port"] == 9222
    assert value.request(browser_id, "tabs")["source_host"] == "127.0.0.1"

    launcher.identities[9222]["browser_id"] = str(uuid4())
    listed = value.list()["browsers"][0]
    assert listed["state"] == "error" and listed["running"] is False
    with pytest.raises(BrowserError, match="different browser"):
        value.request(browser_id, "tabs")
    with pytest.raises(BrowserRegistryConflict, match="cannot be stopped"):
        value.stop(operation(1, id=browser_id))
    assert launcher.stops == []


def test_remove_external_only_unregisters_and_detaches(registry):
    value, launcher = registry
    launcher.identities[9333] = {"browser": "Chromium/External", "browser_id": str(uuid4())}
    added = value.add_existing(operation(0, name="External", port=9333))
    browser_id = added["operation"]["browser_id"]
    value.request(browser_id, "tabs")
    control = FakeControls.made[-1]

    removed = value.remove(operation(1, id=browser_id))
    assert removed["browsers"] == []
    assert launcher.identities[9333]
    assert launcher.stops == []
    assert control.closed is True


@pytest.mark.parametrize("port", [True, 0, 65536, "9222", None])
def test_external_port_is_a_strict_loopback_port(registry, port):
    value, launcher = registry
    with pytest.raises(BrowserRegistryError, match="port"):
        value.add_existing(operation(0, name="External", port=port))
    assert launcher.launches == [] and launcher.identities == {}


def test_external_hosts_are_canonical_and_endpoints_are_host_port_pairs(registry):
    value, launcher = registry
    first_uuid, second_uuid = str(uuid4()), str(uuid4())
    launcher.identities[("10.0.0.8", 9222)] = {
        "browser": "Chromium/Remote", "browser_id": first_uuid,
    }
    launcher.identities[("10.0.0.9", 9222)] = {
        "browser": "Chromium/Remote", "browser_id": second_uuid,
    }

    first = value.add_existing(
        operation(0, name="Remote one", host="10.0.0.8", port=9222)
    )
    second = value.add_existing(
        operation(1, name="Remote two", host="10.0.0.9", port=9222)
    )

    assert [item["host"] for item in second["browsers"]] == ["10.0.0.8", "10.0.0.9"]
    assert value.request(first["operation"]["browser_id"], "tabs")["source_host"] == "10.0.0.8"
    with pytest.raises(BrowserRegistryConflict, match="already registered"):
        value.add_existing(operation(2, name="Duplicate", host="10.0.0.8", port=9222))


def test_legacy_external_record_defaults_host_without_rewriting_file(tmp_path):
    home = tmp_path / "home"
    root = home / "remote-browser"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    browser_uuid = str(uuid4())
    record = {
        "id": str(uuid4()), "name": "Old external", "kind": "external",
        "executable": None, "headless": False, "legacy": False,
        "port": 9222, "browser_uuid": browser_uuid,
    }
    content = json.dumps({
        "version": 1, "revision": 1, "legacy_imported": True,
        "browsers": [record], "receipts": [],
    }, separators=(",", ":")) + "\n"
    path = root / "registry.json"
    path.write_text(content)
    path.chmod(0o600)
    launcher = FakeLauncher()
    launcher.identities[9222] = {"browser": "Chromium/Old", "browser_id": browser_uuid}
    value = BrowserRegistry(home, {}, launcher=launcher, browser_factory=FakeControls)

    listed = value.list()

    assert listed["browsers"][0]["host"] == "127.0.0.1"
    assert path.read_text() == content


def test_managed_remove_refuses_running_then_preserves_profile_after_stop(registry):
    value, launcher = registry
    launch_request = operation(0, name="Managed", executable=None, headless=True)
    launched = value.launch(launch_request)
    browser_id = launched["operation"]["browser_id"]
    profile = value.instances / UUID(browser_id).hex / "remote-browser" / "profile"

    with pytest.raises(BrowserRegistryConflict, match="Stop"):
        value.remove(operation(1, id=browser_id))
    stopped = value.stop(operation(1, id=browser_id))
    assert stopped["revision"] == 2 and len(launcher.stops) == 1
    removed = value.remove(operation(2, id=browser_id))
    assert removed["browsers"] == []
    replayed = value.launch(launch_request)
    assert replayed["revision"] == 3 and replayed["browsers"] == []
    assert len(launcher.launches) == 1
    assert profile.exists()


def test_pending_launch_recovers_after_restart_without_duplicate_launch(tmp_path):
    launcher = FakeLauncher()
    home = tmp_path / "home"
    request = operation(0, name="Recovered", executable=None, headless=True)
    first = BrowserRegistry(home, {}, launcher=launcher, browser_factory=FakeControls)
    real_write = first._write
    writes = 0

    def lose_completion(state):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise BrowserRegistryCorrupt("completion response was lost")
        real_write(state)

    first._write = lose_completion
    with pytest.raises(BrowserRegistryCorrupt, match="lost"):
        first.launch(request)
    assert len(launcher.launches) == 1
    saved = json.loads(first.path.read_text())
    assert saved["receipts"][0]["status"] == "pending"

    restarted = BrowserRegistry(home, {}, launcher=launcher, browser_factory=FakeControls)
    recovered = restarted.launch(request)
    assert recovered["browsers"][0]["running"] is True
    assert len(launcher.launches) == 1
    assert json.loads(first.path.read_text())["receipts"][0]["status"] == "completed"


def test_uncertain_pending_launch_is_not_resent(registry):
    value, launcher = registry
    launcher.fail_after_launch = True
    request = operation(0, name="Uncertain", executable=None, headless=True)
    with pytest.raises(RuntimeError):
        value.launch(request)
    port = launcher.launches[0][0]
    launcher.identities.pop(port)
    launcher.fail_after_launch = False

    restarted = BrowserRegistry(value.home, {}, launcher=launcher, browser_factory=FakeControls)
    with pytest.raises(BrowserRegistryUncertain, match=f"Request ID: {request['request_id']}") as caught:
        restarted.launch(request)
    assert caught.value.request_id == request["request_id"]
    assert len(launcher.launches) == 1


def test_pending_stop_recovers_on_list_and_can_then_be_removed(registry):
    value, launcher = registry
    launched = value.launch(operation(0, name="Managed", executable=None, headless=True))
    browser_id = launched["operation"]["browser_id"]
    launcher.fail_after_stop = True
    stop_request = operation(1, id=browser_id)

    with pytest.raises(BrowserRegistryUncertain, match=stop_request["request_id"]):
        value.stop(stop_request)
    saved = json.loads(value.path.read_text())
    assert saved["revision"] == 1
    assert saved["receipts"][-1]["status"] == "pending"

    listed = value.list()
    assert listed["revision"] == 2
    assert listed["browsers"][0]["state"] == "stopped"
    saved = json.loads(value.path.read_text())
    assert saved["receipts"][-1]["status"] == "completed"
    assert saved["receipts"][-1]["revision"] == 2

    removed = value.remove(operation(2, id=browser_id))
    assert removed["revision"] == 3
    assert removed["browsers"] == []


def test_slow_launch_releases_file_lock_and_preserves_unrelated_revision(registry):
    value, launcher = registry
    first = value.launch(operation(0, name="One", executable=None, headless=True))
    first_id = first["operation"]["browser_id"]
    entered = threading.Event()
    release = threading.Event()
    original_launch = launcher.launch

    def blocked_launch(port, **options):
        entered.set()
        assert release.wait(5)
        return original_launch(port, **options)

    launcher.launch = blocked_launch
    launch_request = operation(1, name="Two", executable=None, headless=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(value.launch, launch_request)
        assert entered.wait(2)
        listed = value.list()
        assert next(item for item in listed["browsers"] if item["name"] == "Two")["state"] == "busy"
        renamed = value.rename(operation(1, id=first_id, name="Renamed"))
        assert renamed["revision"] == 2
        release.set()
        launched = future.result(timeout=5)

    assert launched["revision"] == 3
    assert {item["name"] for item in launched["browsers"]} == {"Renamed", "Two"}
    receipts = json.loads(value.path.read_text())["receipts"]
    launch_receipt = next(item for item in receipts if item["request_id"] == launch_request["request_id"])
    assert launch_receipt["status"] == "completed"
    assert launch_receipt["revision"] == 3


def test_registry_file_lock_is_available_while_list_probes_status(registry):
    value, _launcher = registry
    launched = value.launch(operation(0, name="Slow", executable=None, headless=True))
    record = value._snapshot()["browsers"][0]
    manager = value._manager(record)
    entered = threading.Event()
    release = threading.Event()
    original_status = manager.status

    def blocked_status():
        entered.set()
        assert release.wait(5)
        return original_status()

    manager.status = blocked_status
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(value.list)
        assert entered.wait(2)
        with value._lock():
            snapshot = value._read()
        assert snapshot["revision"] == launched["revision"]
        release.set()
        assert future.result(timeout=5)["revision"] == launched["revision"]


def test_legacy_entry_is_lazy_stable_and_migrated_without_changing_source(tmp_path):
    home = tmp_path / "home"
    root = home / "remote-browser"
    profile = root / "profile"
    profile.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    profile.chmod(0o700)
    browser_uuid = str(uuid4())
    original = json.dumps({"version": 1, "port": 9444, "browser_id": browser_uuid})
    state_path = root / "state.json"
    state_path.write_text(original)
    state_path.chmod(0o600)
    launcher = FakeLauncher()
    launcher.identities[9444] = {"browser": "Chromium/Legacy", "browser_id": browser_uuid}
    value = BrowserRegistry(
        home,
        {"enabled": False, "headless": True, "executable": None, "cdp_port": 0},
        launcher=launcher,
        browser_factory=FakeControls,
    )

    listed = value.list()
    legacy = listed["browsers"][0]
    assert legacy["name"] == "Existing Swarm browser"
    assert legacy["kind"] == "managed" and legacy["running"] is True
    assert not value.path.exists()
    assert not value.lock_path.exists()

    renamed = value.rename(operation(0, id=legacy["id"], name="Legacy renamed"))
    assert renamed["browsers"][0]["name"] == "Legacy renamed"
    assert state_path.read_text() == original
    saved = json.loads(value.path.read_text())
    assert saved["legacy_imported"] is True and saved["browsers"][0]["legacy"] is True


def test_corrupt_or_unsafe_registry_fails_closed(tmp_path):
    home = tmp_path / "home"
    root = home / "remote-browser"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    path = root / "registry.json"
    path.write_text("{}")
    path.chmod(0o600)
    value = BrowserRegistry(home, {})
    with pytest.raises(BrowserRegistryCorrupt, match="could not be read"):
        value.list()

    if os.name != "nt":
        path.chmod(0o644)
        with pytest.raises(BrowserRegistryCorrupt):
            value.list()
