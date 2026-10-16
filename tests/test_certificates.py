from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from swarm.agent_client import AgentClient, AgentClientError
from swarm.certificates import certificate_names, generate_if_absent
from swarm.cli import main
from swarm.federation import FederationClient, FederationConfig


@pytest.fixture(scope="module")
def tls_pair(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL is required for real certificate generation")
    directory = tmp_path_factory.mktemp("tls")
    cert, key = directory / "cert.pem", directory / "privkey.pem"
    assert generate_if_absent(cert, key, hosts=["relay.example", "192.0.2.1"], days=2)
    return cert, key


def test_certificate_names_validate_and_deduplicate(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "My-PC")
    names = certificate_names(["localhost", "0.0.0.0", "::", "Relay.Example", "192.0.2.1"])
    assert names == ["DNS:localhost", "IP:127.0.0.1", "IP:::1", "DNS:my-pc",
                     "DNS:relay.example", "IP:192.0.2.1"]
    for value in ["bad,name", "foo\n[evil]", "https://relay.example", "*.example", "-bad"]:
        with pytest.raises(ValueError, match="hostname"):
            certificate_names([value])


def test_generated_certificate_is_valid_and_reused(tls_pair, monkeypatch):
    cert, key = tls_pair
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
    details = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
        check=True, capture_output=True, text=True,
    ).stdout
    for value in ["DNS:localhost", "DNS:relay.example", "IP Address:127.0.0.1", "IP Address:192.0.2.1"]:
        assert value in details
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in tls_pair]
    monkeypatch.setattr(shutil, "which", lambda _: None)  # Reuse needs no OpenSSL.
    assert not generate_if_absent(cert, key, hosts=[])
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in tls_pair]


@pytest.mark.parametrize("existing", ["cert.pem", "privkey.pem"])
def test_partial_pair_is_never_overwritten(tmp_path, existing):
    path = tmp_path / existing
    path.write_text("keep me")
    with pytest.raises(ValueError, match="only one TLS file"):
        generate_if_absent(tmp_path / "cert.pem", tmp_path / "privkey.pem", hosts=[])
    assert path.read_text() == "keep me"
    assert len(list(tmp_path.iterdir())) == 1


def test_missing_openssl_and_invalid_inputs(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="requires OpenSSL"):
        generate_if_absent(cert, key, hosts=[])
    with pytest.raises(ValueError, match="validity"):
        generate_if_absent(cert, key, hosts=[], days=0)
    with pytest.raises(ValueError, match="different paths"):
        generate_if_absent(cert, cert, hosts=[])
    cert.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="symbolic links"):
        generate_if_absent(cert, key, hosts=[])


def test_openssl_failure_leaves_no_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/openssl")

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "openssl")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="could not generate"):
        generate_if_absent(tmp_path / "cert.pem", tmp_path / "privkey.pem", hosts=[])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("relay", [False, True])
def test_cli_generates_and_reuses_certificates(tmp_path, monkeypatch, capsys, relay):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL required")
    monkeypatch.delenv("SWARM_RELAY_URL", raising=False)
    monkeypatch.delenv("SWARM_RELAY_NODE_ID", raising=False)
    monkeypatch.setenv("SWARM_RELAY_API_KEY", "a" * 48)
    monkeypatch.setenv("SWARM_API_CA_FILE", "")  # Restore the CLI's environment mutation afterwards.
    captured = {}
    monkeypatch.setattr("swarm.server.create_app", lambda home, **kw: captured.update(kw) or object())
    monkeypatch.setattr("swarm.relay.create_relay_app", lambda keys: object())
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(run=kw))
    args = ["--home", str(tmp_path), *(["relay"] if relay else []), "serve",
            "--generate-certs-if-absent", "--cert-host", "relay.example"]
    main(args)
    directory = tmp_path / "tls" / ("relay" if relay else "dashboard")
    assert captured["run"]["ssl_certfile"] == str(directory / "cert.pem")
    assert captured["run"]["ssl_keyfile"] == str(directory / "privkey.pem")
    if not relay:
        assert captured["api_url"] == "https://127.0.0.1:8787"
        assert os.environ["SWARM_API_CA_FILE"] == str(directory / "cert.pem")
    before = (directory / "cert.pem").read_bytes()
    main(args)
    assert (directory / "cert.pem").read_bytes() == before
    output = capsys.readouterr()
    assert "generated:" in output.out and "reused:" in output.out
    assert "TLS does not add authentication" in output.err


def test_dashboard_plain_http_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_RELAY_URL", raising=False)
    monkeypatch.delenv("SWARM_RELAY_NODE_ID", raising=False)
    captured = {}
    monkeypatch.setattr("swarm.server.create_app", lambda home, **kw: captured.update(kw) or object())
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(run=kw))
    main(["--home", str(tmp_path), "serve"])
    assert captured["api_url"] == "http://127.0.0.1:8787"
    assert "ssl_certfile" not in captured["run"]
    assert not (tmp_path / "tls").exists()


def test_explicit_pair_and_browser_https(tmp_path, tls_pair, monkeypatch):
    monkeypatch.delenv("SWARM_RELAY_URL", raising=False)
    monkeypatch.delenv("SWARM_RELAY_NODE_ID", raising=False)
    monkeypatch.setenv("SWARM_API_CA_FILE", "")
    captured = {}
    monkeypatch.setattr("swarm.server.create_app", lambda home, **kw: captured.update(kw) or object())
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(run=kw))
    monkeypatch.setattr("webbrowser.open", lambda url: captured.update(browser=url))

    class Timer:
        def __init__(self, delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

    monkeypatch.setattr("swarm.cli.threading.Timer", Timer)
    main(["--home", str(tmp_path), "serve", "--host", "::1", "--open",
          "--certfile", str(tls_pair[0]), "--keyfile", str(tls_pair[1])])
    assert captured["browser"] == captured["api_url"] == "https://[::1]:8787"
    assert captured["run"]["ssl_certfile"] == str(tls_pair[0])


@pytest.mark.parametrize("options, message", [
    (["serve", "--certfile", "cert.pem"], "must be supplied together"),
    (["serve", "--cert-host", "example.com"], "requires --generate-certs-if-absent"),
    (["relay", "serve"], "relay TLS requires"),
])
def test_cli_rejects_incomplete_tls_options(tmp_path, monkeypatch, capsys, options, message):
    monkeypatch.delenv("SWARM_RELAY_URL", raising=False)
    monkeypatch.delenv("SWARM_RELAY_NODE_ID", raising=False)
    monkeypatch.setenv("SWARM_RELAY_API_KEY", "a" * 48)
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("must not start server"))
    with pytest.raises(SystemExit) as error:
        main(["--home", str(tmp_path), *options])
    assert error.value.code == 1
    assert message in capsys.readouterr().err


def test_live_https_clients_verify_generated_certificate(tls_pair, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*tls_pair)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://127.0.0.1:{server.server_port}"
    try:
        monkeypatch.delenv("SWARM_API_CA_FILE", raising=False)
        with pytest.raises(AgentClientError, match="CERTIFICATE_VERIFY_FAILED"):
            AgentClient(url).request("GET", "/", operation="status")
        monkeypatch.setenv("SWARM_API_CA_FILE", str(tls_pair[0]))
        assert AgentClient(url).request("GET", "/", operation="status") == {"ok": True}
        federation = FederationClient(FederationConfig(
            url="wss://relay.example/v1/connect", node_id="test", name="Test",
            api_key="a" * 48, local_api_url=url,
        ), lambda: {})
        assert federation._local_request("GET", "/", "", b"")["status"] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
