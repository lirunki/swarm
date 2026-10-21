"""Detached tmux processes; browser terminals are disposable attachment clients."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from .adapters import SKILL, Adapter, PiAdapter
from .models import Agent
from .paths import local_path
from .store import Store


class Processes:
    def __init__(self, store: Store, pi: str = "pi", api_url: str = "http://127.0.0.1:8787"):
        self.store = store
        self.api_url = api_url
        digest = hashlib.sha256(str(store.home).encode()).hexdigest()[:16]
        self.socket = f"swarm-{digest}"
        self.adapters: dict[str, Adapter] = {"pi": PiAdapter(pi)}

    def argv(self, *args: str) -> list[str]:
        return ["tmux", "-L", self.socket, "-f", "/dev/null", *args]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(self.argv(*args), text=True, capture_output=True, timeout=10)
        except FileNotFoundError as exc:
            raise RuntimeError("tmux is required; install it with your OS package manager") from exc
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or "tmux command failed")
        return result

    @staticmethod
    def target(agent: Agent) -> str:
        return f"swarm-{UUID(str(agent.id))}"

    def alive(self, agent: Agent) -> bool:
        if not agent.managed or not shutil.which("tmux"):
            return False
        result = self.run(
            "display-message",
            "-p",
            "-t",
            self.target(agent) + ":0.0",
            "#{pane_dead}|#{pane_start_command}",
            check=False,
        )
        dead, _, command = result.stdout.strip().partition("|")
        # A crash between new-session and respawn-pane must not strand a placeholder.
        return result.returncode == 0 and dead == "0" and command.strip("\\\"'") != "sleep 86400"

    def pid(self, agent: Agent) -> int | None:
        result = self.run(
            "display-message", "-p", "-t", self.target(agent) + ":0.0", "#{pane_pid}", check=False
        )
        try:
            return int(result.stdout.strip()) if result.returncode == 0 else None
        except ValueError:
            return None

    def launch(self, agent: Agent, prompt: str | None) -> int | None:
        if self.alive(agent):
            return self.pid(agent)
        if not local_path(agent.folder).is_dir():
            raise ValueError(f"working folder is missing: {agent.folder}")
        if not agent.managed:
            raise ValueError("observed agents must be explicitly adopted before launching")
        adapter = self.adapters.get(agent.backend)
        if not adapter:
            raise ValueError(f"no adapter for backend {agent.backend}")
        command = adapter.command(agent, prompt)
        return self._start(agent, command, local_path(agent.folder))

    def launch_setup(self, agent: Agent) -> int | None:
        """Provision and exec Pi in this managed pane, off the HTTP path."""
        if not agent.managed or agent.spawn_state != "provisioning":
            raise ValueError("setup requires a managed provisioning record")
        if self.alive(agent):
            return self.pid(agent)
        adapter = self.adapters.get(agent.backend)
        if not isinstance(adapter, PiAdapter):
            raise ValueError("background setup currently supports the Pi backend only")
        command = [
            sys.executable,
            "-m",
            "swarm.provisioning",
            "--home",
            str(self.store.home),
            "--id",
            str(agent.id),
            "--pi",
            adapter.executable,
        ]
        return self._start(agent, command, self.store.home)

    def _start(self, agent: Agent, command: list[str], folder: Path) -> int | None:
        target = self.target(agent)
        # Dead panes retain output; replace only when explicitly launching/recovering.
        self.run("kill-session", "-t", target, check=False)
        env = {
            "SWARM_HOME": str(self.store.home),
            "SWARM_AGENT_ID": str(agent.id),
            "SWARM_AGENT_FILE": str(self.store.path(agent.id)),
            "SWARM_PYTHON": sys.executable,
            "SWARM_SKILL": str(SKILL),
            "SWARM_API_URL": self.api_url,
            "SWARM_PARENT_ID": str(agent.parent_id) if agent.parent_id else "",
            "TERM": "xterm-256color",
        }
        # Pass the local controller CA down so child agents can verify an HTTPS dashboard.
        ca_file = os.environ.get("SWARM_API_CA_FILE")
        if ca_file:
            env["SWARM_API_CA_FILE"] = ca_file
        # tmux's server may predate refreshed credentials. Set this session's environment.
        inherited = {
            key: val
            for key, val in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "SHELL",
                "PYTHONPATH",
                "PI_CODING_AGENT_DIR",
                "PI_CODING_AGENT_SESSION_DIR",
                "USERPROFILE",
                "SYSTEMROOT",
                "SystemRoot",
                "COMSPEC",
                "ComSpec",
                "APPDATA",
                "LOCALAPPDATA",
                "TEMP",
                "TMP",
                "CYGWIN",
                "BROWSER",
                "SWARM_WINDOWS_NODE",
                "SWARM_WINDOWS_PI_ENTRY",
                "SWARM_VENV_PYTHON",
            }
            or key.endswith("_API_KEY")
            or key.startswith(("AZURE_", "AWS_", "GOOGLE_"))
        }
        options = []
        for key, value in (inherited | env).items():
            options.extend(["-e", f"{key}={value}"])
        shell = shlex.join(
            ["exec", "env", "-u", "PI_SESSION_ID", "-u", "PI_SESSION_FILE", *command]
        )
        # Apply remain-on-exit BEFORE the agent can exit, so startup errors stay inspectable.
        self.run(
            "new-session",
            "-d",
            "-s",
            target,
            "-c",
            str(folder),
            "-x",
            "120",
            "-y",
            "34",
            *options,
            "sleep 86400",
        )
        try:
            self.run("set-option", "-t", target, "remain-on-exit", "on")
            self.run("set-option", "-t", target, "status", "off")
            self.run("set-option", "-t", target, "history-limit", "10000")
            self.run("set-option", "-t", target, "mouse", "on")
            self.run("set-option", "-t", target, "extended-keys", "on", check=False)
            self.run("set-option", "-t", target, "extended-keys-format", "csi-u", check=False)
            self.run(
                "respawn-pane",
                "-k",
                "-t",
                target + ":0.0",
                "-c",
                str(folder),
                shell,
            )
        except Exception:
            self.run("kill-session", "-t", target, check=False)
            raise
        return self.pid(agent)

    def send_prompt(self, agent: Agent, message: str) -> None:
        self.paste_text(agent, message, submit=True)

    def paste_text(self, agent: Agent, message: str, *, submit: bool = False) -> None:
        """Insert literal text into the owned pane; Enter is never implicit."""
        if not self.alive(agent):
            raise ValueError("agent is not running")
        buffer = f"swarm-{uuid4()}"
        fd, name = tempfile.mkstemp(prefix="prompt-", dir=self.store.home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(message)
            self.run("load-buffer", "-b", buffer, name)
            # Bracketed paste prevents multiline prompts being submitted as separate turns.
            self.run("paste-buffer", "-d", "-p", "-b", buffer, "-t", self.target(agent) + ":0.0")
            if submit:
                self.run("send-keys", "-t", self.target(agent) + ":0.0", "Enter")
        finally:
            Path(name).unlink(missing_ok=True)
            self.run("delete-buffer", "-b", buffer, check=False)

    def stop(self, agent: Agent) -> None:
        # Never kill by a PID supplied in a Markdown record (PID reuse after reboot).
        self.run("kill-session", "-t", self.target(agent), check=False)

    def attach_argv(self, agent: Agent) -> list[str]:
        return self.argv("attach-session", "-t", self.target(agent))

    def health(self) -> dict[str, bool]:
        adapter = self.adapters["pi"]
        return {"tmux": bool(shutil.which("tmux")), "pi": adapter.available()}
