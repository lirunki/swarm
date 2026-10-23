"""Detached tmux processes; browser terminals are disposable attachment clients."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from .adapters import SKILL, Adapter, PiAdapter
from .models import Agent
from .paths import agent_path, local_path
from .store import Store


class Processes:
    def __init__(self, store: Store, pi: str = "pi", api_url: str = "http://127.0.0.1:8787"):
        self.store = store
        self.api_url = api_url
        self.api_token_file: Path | None = None
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

    @staticmethod
    def process_birth(pid: int) -> tuple[str | None, str | None]:
        if Path("/proc").is_dir():
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
                return fields[19], fields[0]
            except FileNotFoundError:
                return None, None
            except (OSError, IndexError, ValueError):
                raise RuntimeError("Could not inspect the captured process lifetime") from None
        return None, None

    def inspect_pane(self, target: str) -> dict | None:
        result = self.run(
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_dead}|#{session_id}|#{pane_id}|#{pane_pid}|#{pane_start_command}",
            check=False,
        )
        if result.returncode:
            error = result.stderr.lower()
            absent = (
                "can't find pane",
                "can't find session",
                "no server running",
                "no such file or directory",
            )
            if any(value in error for value in absent):
                return None  # Namespace absence; exited() must still verify the original PID.
            raise RuntimeError("Tmux inspection failed; process exit is not confirmed")
        values = result.stdout.strip().split("|", 4)
        if (
            len(values) != 5
            or values[0] not in {"0", "1"}
            or not re.fullmatch(r"\$[0-9]+", values[1])
            or not re.fullmatch(r"%[0-9]+", values[2])
            or not values[3].isdigit()
        ):
            raise RuntimeError("Invalid tmux process identity; exit is not confirmed")
        pid = int(values[3])
        if pid <= 0:
            raise RuntimeError("Invalid tmux process PID; exit is not confirmed")
        birth, state = self.process_birth(pid)
        return {
            "session": values[1],
            "pane": values[2],
            "pid": pid,
            "command": values[4],
            "birth": birth,
            "dead": values[0] == "1",
            "process_state": state,
        }

    def identity(self, agent: Agent) -> dict | None:
        if not agent.managed:
            return None
        pane = self.inspect_pane(self.target(agent) + ":0.0")
        if pane is None or pane["dead"] or pane["command"].strip("\"'") == "sleep 86400":
            return None
        return {k: v for k, v in pane.items() if k not in {"dead", "process_state", "command"}}

    def exited(self, identity: dict) -> bool:
        pid = int(identity["pid"])
        birth, state = self.process_birth(pid)
        if state == "Z" or (identity.get("birth") and birth and birth != identity["birth"]):
            return True
        if Path("/proc").is_dir():
            return not Path(f"/proc/{pid}").exists()
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            raise RuntimeError("Could not confirm captured process exit") from None
        return False

    @staticmethod
    def identity_condition(identity: dict, dead: bool) -> str:
        sid, pane, pid = identity["session"], identity["pane"], int(identity["pid"])
        if not re.fullmatch(r"\$[0-9]+", sid) or not re.fullmatch(r"%[0-9]+", pane):
            raise ValueError("Invalid captured tmux identity")
        return (
            "#{&&:#{==:#{pane_dead},"
            + str(int(dead))
            + "},#{&&:#{==:#{session_id},"
            + sid
            + "},#{&&:#{==:#{pane_id},"
            + pane
            + "},#{==:#{pane_pid},"
            + str(pid)
            + "}}}}"
        )

    def exact_command(self, identity: dict, command: str, *, dead: bool = False):
        info = self.inspect_pane(identity["pane"])
        if info is None or any(info[k] != identity[k] for k in ("session", "pane", "pid")):
            raise RuntimeError("Captured pane no longer exists; no replacement was targeted")
        if not dead and identity.get("birth") and info.get("birth") != identity["birth"]:
            raise RuntimeError("Captured process lifetime changed")
        result = self.run(
            "if-shell",
            "-F",
            "-t",
            identity["pane"],
            self.identity_condition(identity, dead),
            command,
            "display-message -p SWARM_RESTART_TARGET_MISMATCH",
        )
        if "SWARM_RESTART_TARGET_MISMATCH" in result.stdout:
            raise RuntimeError("Captured pane changed before the action; nothing was targeted")

    def stop_identity(self, agent: Agent, identity: dict):
        info = self.inspect_pane(identity["pane"])
        if info is None or info["dead"]:
            if not self.exited(identity):
                raise RuntimeError(
                    "Captured process may still be alive without an owned pane; inspect it manually"
                )
            return
        self.exact_command(identity, shlex.join(["kill-pane", "-t", identity["pane"]]))

    def legacy_quit(self, agent: Agent, identity: dict):
        buffer = f"swarm-{uuid4()}"
        fd, name = tempfile.mkstemp(prefix="quit-", dir=self.store.home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("/quit")
            self.run("load-buffer", "-b", buffer, name)
            command = (
                shlex.join(["paste-buffer", "-d", "-p", "-b", buffer, "-t", identity["pane"]])
                + " ; "
                + shlex.join(["send-keys", "-t", identity["pane"], "Enter"])
            )
            self.exact_command(identity, command)
        finally:
            Path(name).unlink(missing_ok=True)
            self.run("delete-buffer", "-b", buffer, check=False)

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

    def launch_restart(self, agent: Agent, identity: dict) -> int | None:
        """Never kill a stable session name while restarting: reuse only the exact dead pane.

        A missing session is created with a barrier, so remain-on-exit is set before
        exec. Name collisions fail rather than replacing someone else's process.
        """
        if not self.exited(identity):
            raise RuntimeError("Original process exit has not been confirmed")
        current = self.inspect_pane(self.target(agent) + ":0.0")
        if current is not None and (
            not current["dead"]
            or any(current[k] != identity[k] for k in ("session", "pane", "pid"))
        ):
            raise RuntimeError(
                "A different pane/process owns this agent; no replacement was launched"
            )
        if not local_path(agent.folder).is_dir():
            raise ValueError("Working folder is missing")
        adapter = self.adapters.get(agent.backend)
        if adapter is None:
            raise ValueError("No restart adapter")
        command = adapter.command(agent, None)
        from .messaging import prepare_bridge

        directory, epoch = prepare_bridge(self.store, agent)
        environment = self._environment(agent, directory, epoch)
        shell = shlex.join(
            ["exec", "env", "-u", "PI_SESSION_ID", "-u", "PI_SESSION_FILE", *command]
        )
        if current is not None:
            # Clear inherited managed keys removed since the previous launch.
            old = self.run("show-environment", "-t", identity["session"]).stdout
            for line in old.splitlines():
                key = line.lstrip("-").split("=", 1)[0]
                managed = key.startswith(("SWARM_", "AZURE_", "AWS_", "GOOGLE_")) or key.endswith(
                    "_API_KEY"
                )
                if managed and key not in environment:
                    self.run("set-environment", "-r", "-t", identity["session"], key)
            for key, value in environment.items():
                self.run("set-environment", "-t", identity["session"], key, value)
            # No -k: even a last-moment live replacement cannot be terminated.
            self.exact_command(
                identity,
                shlex.join(
                    [
                        "respawn-pane",
                        "-t",
                        identity["pane"],
                        "-c",
                        str(local_path(agent.folder)),
                        shell,
                    ]
                ),
                dead=True,
            )
        else:
            token = str(uuid4())
            plan = directory / f"launch-{token}.json"
            ready = directory / f"launch-{token}.ready"
            from .store import atomic_write

            atomic_write(plan, json.dumps(command))
            code = (
                "import os,sys,time,json;from pathlib import Path;"
                "p=Path(sys.argv[1]);r=Path(sys.argv[2]);"
                "deadline=time.monotonic()+20;"
                "exec('while not r.exists():\\n if time.monotonic()>deadline: raise SystemExit(2)\\n time.sleep(.05)');"
                "c=json.loads(p.read_text());p.unlink();r.unlink();"
                "e={k:v for k,v in os.environ.items() if k not in ('PI_SESSION_ID','PI_SESSION_FILE')};"
                "os.execvpe(c[0],c,e)"
            )
            guarded = shlex.join(["exec", sys.executable, "-c", code, str(plan), str(ready)])
            options = []
            for key, value in environment.items():
                options.extend(["-e", f"{key}={value}"])
            result = self.run(
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{session_id}|#{pane_id}|#{pane_pid}",
                "-s",
                self.target(agent),
                "-c",
                str(local_path(agent.folder)),
                *options,
                guarded,
            )
            sid, pane, pid = result.stdout.strip().split("|")
            owned = {"session": sid, "pane": pane, "pid": int(pid)}
            actual = self.inspect_pane(self.target(agent) + ":0.0")
            if actual is None or any(actual[k] != owned[k] for k in owned):
                raise RuntimeError(
                    "New session identity changed before launch; barrier was not released"
                )
            self.run("set-option", "-t", sid, "remain-on-exit", "on")
            self.run("set-option", "-t", sid, "status", "off")
            self.run("set-option", "-t", sid, "history-limit", "10000")
            self.run("set-option", "-t", sid, "mouse", "on")
            atomic_write(ready, "ready")
        return self.pid(agent)

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

    def _environment(self, agent: Agent, directory: Path, epoch: str) -> dict:
        env = {
            "SWARM_HOME": str(self.store.home),
            "SWARM_AGENT_ID": str(agent.id),
            "SWARM_AGENT_FILE": str(self.store.path(agent.id)),
            "SWARM_PYTHON": sys.executable,
            "SWARM_SKILL": str(SKILL),
            "SWARM_API_URL": self.api_url,
            "SWARM_PARENT_ID": str(agent.parent_id) if agent.parent_id else "",
            "SWARM_CONTROL_DIR": agent_path(directory),
            "SWARM_CONTROL_EPOCH": epoch,
            "TERM": "xterm-256color",
        }
        if self.api_token_file is not None:
            env["SWARM_API_TOKEN_FILE"] = str(self.api_token_file)
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
        return inherited | env

    def _start(self, agent: Agent, command: list[str], folder: Path) -> int | None:
        from .messaging import prepare_bridge

        directory, epoch = prepare_bridge(self.store, agent)
        target = self.target(agent)
        # Dead panes retain output; replace only when explicitly launching/recovering.
        self.run("kill-session", "-t", target, check=False)
        environment = self._environment(agent, directory, epoch)
        options = []
        for key, value in environment.items():
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

    def tmux_action(self, agent: Agent, action: str) -> None:
        """Run a small allowlisted layout command in this managed agent's session."""
        if not agent.managed:
            raise ValueError("tmux controls require a managed agent")
        if not self.alive(agent):
            raise ValueError("tmux controls require a live agent")

        target = self.target(agent)
        commands = {
            "split-horizontal": ("split-window", "-h"),
            "split-vertical": ("split-window", "-v"),
            "reset-layout": ("select-layout", "tiled"),
            "new-window": ("new-window",),
            "zoom-pane": ("resize-pane", "-Z"),
            "pane-up": ("select-pane", "-U"),
            "pane-down": ("select-pane", "-D"),
            "pane-left": ("select-pane", "-L"),
            "pane-right": ("select-pane", "-R"),
            "next-window": ("next-window",),
            "previous-window": ("previous-window",),
        }
        if action == "close-pane":
            primary = self.identity(agent)
            if primary is None:
                raise RuntimeError("Could not verify the main agent pane; no pane was closed")
            current = self.run(
                "display-message", "-p", "-t", target, "#{pane_id}"
            ).stdout.strip()
            if not re.fullmatch(r"%[0-9]+", current):
                raise RuntimeError("Could not identify the selected tmux pane; no pane was closed")
            if current == primary["pane"]:
                raise ValueError("Cannot close the main agent pane; select a split pane instead")
            self.run("kill-pane", "-t", current)
            return

        command = commands.get(action)
        if command is None:
            raise ValueError("Unsupported tmux action")
        self.run(*command, "-t", target)

    def attach_argv(self, agent: Agent) -> list[str]:
        return self.argv("attach-session", "-t", self.target(agent))

    def health(self) -> dict[str, bool]:
        adapter = self.adapters["pi"]
        return {"tmux": bool(shutil.which("tmux")), "pi": adapter.available()}
