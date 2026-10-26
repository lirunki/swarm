"""Provider-free saved-config startup/shutdown of a real disposable TLS dashboard."""

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from swarm import setup_wizard as wizard
from swarm.certificates import generate_if_absent


@pytest.mark.skipif(os.name == "nt" or not shutil.which("openssl"), reason="POSIX/OpenSSL required")
@pytest.mark.parametrize("with_repeater", [False, True])
def test_saved_setup_starts_tls_server_and_stops_it_with_supervisor(tmp_path, with_repeater):
    if sys.prefix == sys.base_prefix:
        pytest.skip("Uses the existing test venv read-only, never installs dependencies")
    root = Path(__file__).resolve().parents[1]
    runtime = tmp_path / "custom runtime"
    runtime.mkdir()
    # No environment installation or modification: alias the existing test venv.
    (runtime / "venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    pi = tmp_path / "fake-pi"
    pi.write_text('#!/bin/sh\nprintf "fixture pi\\n"\n')
    pi.chmod(0o755)
    cert = tmp_path / "tls/cert.pem"
    key = tmp_path / "tls/key.pem"
    generate_if_absent(cert, key, hosts=["127.0.0.1"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = {
        "version": 1,
        "runtime_dir": str(runtime),
        "source_dir": str(root),
        "pi": str(pi),
        "path_entries": [str(Path(sys.executable).parent), str(tmp_path)],
        "home": str(tmp_path / "registry"),
        "relay": None,
        "launch_roles": ["server"],
        "server": {
            "host": "127.0.0.1",
            "port": port,
            "tls": {"certfile": str(cert), "keyfile": str(key)},
            "password_file": None,
            "connection": None,
            "recover": False,
            "allow_remote_restart": False,
            "allow_saved_relay_settings": False,
        },
    }
    relay_port = None
    if with_repeater:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            relay_port = probe.getsockname()[1]
        if relay_port == port:
            pytest.skip("ephemeral port collision")
        relay_key = tmp_path / "relay.key"
        password_file = tmp_path / "browser-password.txt"
        wizard.write_private(relay_key, "X" * 64 + "\n")
        wizard.write_private(password_file, "native-fixture-password\n")
        config["relay"] = {
            "host": "127.0.0.1",
            "port": relay_port,
            "advertise_host": "127.0.0.1",
            "tls": {"certfile": str(cert), "keyfile": str(key)},
            "api_key_file": str(relay_key),
        }
        config["server"]["password_file"] = str(password_file)
        config["server"]["connection"] = {
            "url": f"wss://127.0.0.1:{relay_port}/v1/connect",
            "node_id": "native-setup-test",
            "name": "Native setup test",
            "api_key_file": str(relay_key),
            "ca_file": str(cert),
        }
        config["launch_roles"] = ["relay", "server"]
    saved = tmp_path / "configuration/setup.json"
    wizard.save_config(saved, config)
    isolated_home = tmp_path / "empty user home"
    isolated_home.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SWARM_", "PI_", "PYTHON"))
        and k
        not in {
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "XDG_CONFIG_HOME",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        }
    }
    env.update(
        HOME=str(isolated_home),
        PI_CODING_AGENT_DIR=str(isolated_home / "pi"),
        XDG_CONFIG_HOME=str(isolated_home / "config"),
        NO_PROXY="127.0.0.1,localhost,::1",
    )
    log = tmp_path / "service.log"
    context = ssl.create_default_context(cafile=str(cert))
    with log.open("w+") as output:
        supervisor = subprocess.Popen(
            [sys.executable, str(root / "scripts/setup_wizard.py"), "--launch", str(saved)],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if supervisor.poll() is not None:
                    output.flush()
                    pytest.fail(log.read_text()[-4000:])
                try:
                    with urllib.request.urlopen(
                        f"https://127.0.0.1:{port}/api/auth/status", context=context, timeout=0.5
                    ) as response:
                        body = json.load(response)
                        assert body["required"] is with_repeater
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            else:
                pytest.fail("Disposable server did not start; " + log.read_text()[-4000:])
            if with_repeater:
                request = urllib.request.Request(
                    f"https://127.0.0.1:{port}/api/auth/login",
                    data=json.dumps({"password": "native-fixture-password"}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, context=context, timeout=2) as response:
                    token = json.load(response)["token"]
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    request = urllib.request.Request(
                        f"https://127.0.0.1:{port}/api/federation/settings",
                        headers={"Authorization": "Bearer " + token},
                    )
                    with urllib.request.urlopen(request, context=context, timeout=2) as response:
                        status = json.load(response)
                    if status["connected"]:
                        break
                    time.sleep(0.1)
                else:
                    pytest.fail(
                        "Configured repeater connection was not established: "
                        + log.read_text()[-4000:]
                    )
        finally:
            if supervisor.poll() is None:
                supervisor.terminate()
            try:
                supervisor.wait(timeout=25)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)
                raise
    assert supervisor.returncode == 0, log.read_text()[-4000:]
    with socket.socket() as check:
        check.settimeout(0.5)
        assert check.connect_ex(("127.0.0.1", port)) != 0, (
            "Owned dashboard should have stopped with the supervisor"
        )

    if with_repeater:
        with socket.socket() as check:
            check.settimeout(0.5)
            assert check.connect_ex(("127.0.0.1", relay_port)) != 0, (
                "Owned repeater should stop with the supervisor"
            )
