"""Interactive configuration and process ownership; all paths/secrets are disposable."""

import copy
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from swarm import setup_wizard as wizard


@pytest.fixture
def config(tmp_path):
    return {
        "version": 1,
        "runtime_dir": str(tmp_path / "runtime"),
        "source_dir": str(tmp_path / "source"),
        "pi": str(tmp_path / "pi"),
        "path_entries": [str(tmp_path / "runtime/venv/bin")],
        "home": str(tmp_path / "home"),
        "relay": None,
        "launch_roles": ["server"],
        "server": {
            "host": "127.0.0.1",
            "port": 18787,
            "tls": None,
            "password_file": None,
            "connection": None,
            "recover": False,
            "allow_remote_restart": False,
            "allow_saved_relay_settings": False,
        },
    }


class Answers:
    def __init__(self, tmp_path, *, yes=None, ask=None):
        self.directory = tmp_path
        self.yes_answers = yes or {}
        self.ask_answers = ask or {}
        self.messages = []
        self.questions = []

    def say(self, text):
        self.messages.append(text)

    def ask(self, label, default=None):
        self.questions.append(label)
        for key, value in self.ask_answers.items():
            if key in label:
                return value
        if label == "Swarm registry/data directory":
            return str(self.directory / "home")
        if label == "Setup configuration JSON file":
            return str(self.directory / "settings" / "setup.json")
        return str(default) if default is not None else ""

    def yes(self, label, default=False):
        self.questions.append(label)
        for key, value in self.yes_answers.items():
            if key in label:
                return value
        return default

    def secret(self, label):
        return "X" * 48 if "Relay API key" in label else "fixture-password-not-printed"


def collect(tmp_path, answers):
    return wizard.collect_config(
        answers,
        runtime_dir=tmp_path / "custom runtime",
        source_dir=tmp_path / "checkout",
        pi=tmp_path / "custom pi",
        config_path=tmp_path / "settings" / "setup.json",
    )


def test_loopback_configuration_needs_no_endpoint_or_certificates(tmp_path):
    answers = Answers(tmp_path, yes={"Protect the loopback": False})
    value = collect(tmp_path, answers)
    assert value["relay"] is None and value["server"]["connection"] is None
    assert value["server"]["tls"] is None and not value["server"]["recover"]
    assert value["home"] == str(tmp_path / "home")
    assert not any("WebSocket URL" in q or "certificate file" in q for q in answers.questions)
    args = wizard.service_commands(value)["server"]
    assert "--no-recover" in args and "--relay-url" not in args and "--certfile" not in args


def test_configuration_only_and_no_launch_are_real_guards(tmp_path, monkeypatch):
    answers = Answers(tmp_path, yes={"Protect the loopback": False})
    monkeypatch.setattr(wizard, "Console", lambda: answers)
    monkeypatch.setattr(wizard, "launch_services", lambda *_: pytest.fail("no launch permitted"))
    assert (
        wizard.main(
            [
                "--runtime-dir",
                str(tmp_path / "runtime"),
                "--source-dir",
                str(tmp_path / "source"),
                "--pi",
                str(tmp_path / "pi"),
                "--no-launch",
            ]
        )
        == 0
    )
    path = tmp_path / "settings/setup.json"
    value = wizard.read_config(path)
    assert value["server"] is not None
    assert not any("Start the configured" in question for question in answers.questions)
    assert path.stat().st_mode & 0o077 == 0
    text = path.with_suffix(".run.sh").read_text()
    assert "--launch" in text and str(path) in text and "fixture-password" not in text
    assert wizard.main(["--launch", str(path), "--no-launch"]) == 0


@pytest.mark.skipif(not shutil.which("openssl"), reason="OpenSSL optional")
def test_local_repeater_generates_private_certs_key_and_explicit_server_connection(tmp_path):
    answers = Answers(tmp_path, yes={"Configure a local repeater": True})
    value = collect(tmp_path, answers)
    relay = value["relay"]
    server = value["server"]
    connection = server["connection"]
    assert connection["url"] == "wss://localhost:9443/v1/connect"
    assert connection["ca_file"] == relay["tls"]["certfile"]
    assert connection["api_key_file"] == relay["api_key_file"]
    assert server["password_file"]
    for path in (relay["tls"]["keyfile"], relay["api_key_file"], server["password_file"]):
        assert Path(path).stat().st_mode & 0o077 == 0
    key = Path(relay["api_key_file"]).read_text().strip()
    path = tmp_path / "settings/setup.json"
    wizard.save_config(path, value)
    assert key not in path.read_text() and "fixture-password" not in path.read_text()
    assert key not in "\n".join(answers.messages)
    assert "fixture-password" not in "\n".join(answers.messages)
    commands = wizard.service_commands(value)
    assert list(commands) == ["relay", "server"] and "--api-key-file" in commands["relay"]
    before = Path(relay["tls"]["keyfile"]).read_bytes()
    again = collect(tmp_path, answers)
    assert again["relay"]["tls"] == relay["tls"]
    assert Path(relay["tls"]["keyfile"]).read_bytes() == before


def test_existing_endpoint_is_asked_and_has_no_personal_domain_default(tmp_path):
    answers = Answers(
        tmp_path,
        yes={"Connect this server to an existing": True},
        ask={"WebSocket URL": "wss://relay.example.test:9555/v1/connect"},
    )
    value = collect(tmp_path, answers)
    assert value["server"]["connection"]["url"] == "wss://relay.example.test:9555/v1/connect"
    assert value["server"]["password_file"]
    assert "achilipuvm" not in json.dumps(value)


def test_invalid_endpoint_does_not_request_or_save_a_secret(tmp_path):
    answers = Answers(
        tmp_path,
        yes={"Connect this server to an existing": True},
        ask={"WebSocket URL": "wss://user:do-not-log@relay.example.test/v1/connect"},
    )
    with pytest.raises(wizard.SetupError, match="valid explicit wss") as error:
        collect(tmp_path, answers)
    assert "do-not-log" not in str(error.value)
    assert not any("API-key file" in question for question in answers.questions)
    assert not (tmp_path / "settings/relay.key").exists()


def test_existing_profiles_require_acknowledgement_and_password(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    profile = home / "relay-settings.json"
    profile.write_text("must-not-read")
    with pytest.raises(wizard.SetupError, match="different registry"):
        collect(tmp_path, Answers(tmp_path))
    answers = Answers(tmp_path, yes={"Explicitly allow": True, "Protect the loopback": False})
    value = collect(tmp_path, answers)
    assert value["server"]["allow_saved_relay_settings"] and value["server"]["password_file"]
    assert profile.read_text() == "must-not-read"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(secret="no-inline-secrets"),
        lambda c: c["server"].update(port=True),
        lambda c: c["server"].update(recover="false"),
        lambda c: c.update(home="relative"),
        lambda c: c["server"].update(host="0.0.0.0"),
        lambda c: c["server"].update(allow_saved_relay_settings=True),
    ],
)
def test_saved_config_validation_rejects_unsafe_or_unknown_fields(config, mutate):
    mutate(config)
    with pytest.raises(wizard.SetupError):
        wizard.validate_config(config)


def test_atomic_config_never_overwrites_without_permission_or_follows_symlinks(tmp_path, config):
    path = tmp_path / "setup.json"
    wizard.save_config(path, config)
    before = path.read_bytes()
    with pytest.raises(wizard.SetupError, match="already exists"):
        wizard.save_config(path, config)
    assert path.read_bytes() == before
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(wizard.SetupError, match="symbolic link"):
        wizard.save_config(link, config, replace=True)
    assert path.read_bytes() == before
    path.chmod(0o644)
    with pytest.raises(wizard.SetupError, match="owner-only"):
        wizard.read_config(path)


def test_malformed_json_never_echoes_its_body(tmp_path):
    path = tmp_path / "setup.json"
    path.write_text("secret-should-not-be-echoed")
    path.chmod(0o600)
    with pytest.raises(wizard.SetupError, match="Invalid saved") as error:
        wizard.read_config(path)
    assert "secret-should-not" not in str(error.value)


def test_environment_removes_inherited_relay_credentials_and_old_certificate(config, monkeypatch):
    monkeypatch.setenv("SWARM_RELAY_URL", "wss://not-selected.example")
    monkeypatch.setenv("SWARM_RELAY_API_KEY", "do-not-inherit")
    monkeypatch.setenv("SSL_CERT_FILE", "/not-selected.pem")
    environment = wizard.service_environment(config, "server")
    assert not any(k.startswith("SWARM_RELAY_") for k in environment)
    assert "SSL_CERT_FILE" not in environment
    assert environment["PATH"].startswith(config["path_entries"][0] + os.pathsep)


def test_busy_listener_is_not_killed_reused_or_replaced(config):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        config["server"]["port"] = listener.getsockname()[1]
        with pytest.raises(wizard.SetupError, match="no existing service was stopped"):
            wizard.launch_services(config, popen=lambda *_a, **_kw: pytest.fail("must not spawn"))
        assert listener.fileno() >= 0


def test_supervisor_cleans_only_owned_children_when_later_start_fails(config, monkeypatch):
    other = copy.deepcopy(config["server"])
    # Stub command/preflight generation so this ownership test has no TLS/network effects.
    monkeypatch.setattr(
        wizard, "service_commands", lambda _: {"relay": ["fake-relay"], "server": ["fake-server"]}
    )
    monkeypatch.setattr(wizard, "preflight", lambda *_: None)
    config["relay"] = other
    children = []

    class Child:
        def __init__(self):
            self.pid = 100 + len(children)
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            assert self.stopped
            return 0

    def spawn(*_args, **_kwargs):
        child = Child()
        children.append(child)
        return child

    def ready(_process, _service):
        if len(children) == 2:
            raise wizard.SetupError("startup failed")

    with pytest.raises(wizard.SetupError, match="startup failed"):
        wizard.launch_services(config, ["relay", "server"], popen=spawn, ready=ready)
    assert len(children) == 2 and all(child.stopped for child in children)


def test_unconfigured_and_duplicate_roles_are_not_started(config):
    for selected in (["relay"], ["server", "server"]):
        with pytest.raises(wizard.SetupError):
            wizard.launch_services(config, selected)
    assert wizard.launch_services(config, []) == 0


def test_generated_launcher_quotes_paths_without_evaluating_config(tmp_path, config):
    config["runtime_dir"] = str(tmp_path / "runtime space;no-shell")
    config["source_dir"] = str(tmp_path / "source space")
    path = tmp_path / "config space.json"
    wizard.save_config(path, config)
    launcher = wizard.write_launcher(path, config, Answers(tmp_path))
    assert launcher.stat().st_mode & 0o077 == 0
    result = subprocess.run(["bash", "-n", str(launcher)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert wizard.GENERATED in launcher.read_text()


def add_repeater(config, tmp_path):
    config["relay"] = {
        "host": "127.0.0.1",
        "port": 19443,
        "advertise_host": "localhost",
        "tls": {
            "certfile": str(tmp_path / "tls/cert.pem"),
            "keyfile": str(tmp_path / "tls/key.pem"),
        },
        "api_key_file": str(tmp_path / "relay.key"),
    }


def test_saved_start_selection_is_not_all_configured_services(config, tmp_path, monkeypatch):
    add_repeater(config, tmp_path)
    config["launch_roles"] = ["server"]
    monkeypatch.setattr(wizard, "preflight", lambda *_: None)
    started = []

    class Child:
        pid = 12

        def poll(self):
            return 0

    def spawn(args, **kwargs):
        started.append(args)
        return Child()

    assert wizard.launch_services(config, popen=spawn, ready=lambda *_: None) == 0
    assert len(started) == 1 and "serve" in started[0] and "relay" not in started[0]


def test_interactive_start_selection_is_persisted_for_later_runs(config, tmp_path, monkeypatch):
    add_repeater(config, tmp_path)
    config["launch_roles"] = ["relay", "server"]
    answers = Answers(tmp_path, yes={"Start the configured Swarm": True})
    monkeypatch.setattr(wizard, "Console", lambda: answers)
    monkeypatch.setattr(wizard, "collect_config", lambda *_a, **_kw: config)
    monkeypatch.setattr(wizard, "check_runtime", lambda _: None)
    started = []
    monkeypatch.setattr(wizard, "launch_services", lambda config, roles: started.extend(roles) or 0)
    assert (
        wizard.main(
            [
                "--runtime-dir",
                config["runtime_dir"],
                "--source-dir",
                config["source_dir"],
                "--pi",
                config["pi"],
            ]
        )
        == 0
    )
    saved = wizard.read_config(tmp_path / "settings/setup.json")
    assert saved["launch_roles"] == ["server"] and started == ["server"]


@pytest.mark.parametrize("accept", [True, False])
def test_saved_runtime_change_is_explicit_and_never_silently_launches_old_code(
    config, tmp_path, monkeypatch, accept
):
    path = tmp_path / "setup.json"
    wizard.save_config(path, config)
    before = path.read_bytes()
    answers = Answers(tmp_path, yes={"Update saved runtime": accept})
    monkeypatch.setattr(wizard, "Console", lambda: answers)
    monkeypatch.setattr(wizard, "launch_services", lambda *_: pytest.fail("no launch requested"))
    result = wizard.main(
        [
            "--runtime-dir",
            str(tmp_path / "new runtime"),
            "--source-dir",
            str(tmp_path / "new checkout"),
            "--pi",
            str(tmp_path / "new pi"),
            "--config",
            str(path),
            "--no-launch",
        ]
    )
    if accept:
        assert result == 0
        updated = wizard.read_config(path)
        assert updated["runtime_dir"] == str(tmp_path / "new runtime") and updated[
            "source_dir"
        ] == str(tmp_path / "new checkout")
    else:
        assert result == 1 and path.read_bytes() == before


def test_saved_run_refuses_runtime_source_mismatch_before_start(config, tmp_path, monkeypatch):
    path = tmp_path / "setup.json"
    wizard.save_config(path, config)
    monkeypatch.setattr(
        wizard, "launch_services", lambda *_: pytest.fail("cannot launch mismatched runtime")
    )
    assert wizard.main(["--launch", str(path)]) == 1


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP", "SIGQUIT"])
def test_terminal_signals_stop_owned_children_and_restore_handlers(
    config, monkeypatch, signal_name
):
    import signal

    number = getattr(signal, signal_name, None)
    if number is None:
        pytest.skip("signal unavailable")
    before = signal.getsignal(number)
    children = []
    monkeypatch.setattr(wizard, "preflight", lambda *_: None)

    class Child:
        pid = 17
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

    def spawn(*_args, **_kwargs):
        child = Child()
        children.append(child)
        return child

    def ready(*_):
        handler = signal.getsignal(number)
        assert callable(handler)
        handler(number, None)

    assert wizard.launch_services(config, popen=spawn, ready=ready) == 0
    assert children[0].stopped and signal.getsignal(number) == before


def test_unresponsive_child_is_stopped_without_touching_unowned_processes(config, monkeypatch):
    monkeypatch.setattr(wizard, "preflight", lambda *_: None)

    class Child:
        pid = 18
        killed = False
        terminated = False

        def poll(self):
            return 0 if self.killed else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("fake child", timeout)
            return 0

    child = Child()
    with pytest.raises(wizard.SetupError):
        wizard.launch_services(
            config,
            popen=lambda *_a, **_kw: child,
            ready=lambda *_: (_ for _ in ()).throw(wizard.SetupError("not ready")),
        )
    assert child.terminated and child.killed


def test_credential_outputs_must_not_collide_with_setup_json_or_launcher(config, tmp_path):
    path = tmp_path / "setup.json"
    config["server"]["password_file"] = str(path)
    with pytest.raises(wizard.SetupError, match="must not overlap"):
        wizard.save_config(path, config)
    assert not path.exists()
    config["server"]["password_file"] = str(path.with_suffix(".run.sh"))
    with pytest.raises(wizard.SetupError, match="must not overlap"):
        wizard.save_config(path, config)


def test_browser_password_must_not_reuse_relay_api_key_file(config, tmp_path):
    add_repeater(config, tmp_path)
    config["server"]["password_file"] = config["relay"]["api_key_file"]
    with pytest.raises(wizard.SetupError, match="separate files"):
        wizard.validate_config(config)


def test_remote_restart_without_a_connection_is_not_saved(config):
    config["server"]["allow_remote_restart"] = True
    with pytest.raises(wizard.SetupError, match="requires an explicit relay"):
        wizard.validate_config(config)


def test_generated_service_arguments_are_accepted_by_real_swarm_parser(config, tmp_path):
    from swarm.cli import parser

    add_repeater(config, tmp_path)
    for role, args in wizard.service_commands(config).items():
        parsed = parser().parse_args(args[3:])
        assert parsed.command == ("relay" if role == "relay" else "serve")


@pytest.mark.parametrize(
    "relative",
    [
        "relay-settings.json",
        "state.json",
        "controller.lock",
        "api-auth/client.token",
        "agents/some-agent.md",
    ],
)
def test_setup_and_credentials_cannot_overlap_service_state_paths(config, tmp_path, relative):
    path = Path(config["home"]) / relative
    with pytest.raises(wizard.SetupError, match="managed registry"):
        wizard.save_config(path, config)
    config["server"]["password_file"] = str(path)
    with pytest.raises(wizard.SetupError, match="managed registry"):
        wizard.validate_config(config)
    assert not path.exists()


def test_reserved_secret_path_is_rejected_before_any_file_is_created(tmp_path):
    home = tmp_path / "home"
    answers = Answers(tmp_path, ask={"Relay API-key file": str(home / "relay-settings.json")})
    with pytest.raises(wizard.SetupError, match="managed registry"):
        wizard.choose_key(answers, home / "unused", home=home)
    assert not home.exists()
