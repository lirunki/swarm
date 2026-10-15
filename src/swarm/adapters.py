"""Backend boundary: Pi today, other CLI agents can implement Adapter later."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Protocol

from .models import Agent
from .paths import agent_path, local_path

SKILL = Path(__file__).parent / "skills" / "swarm.md"


class Adapter(Protocol):
    def command(self, agent: Agent, prompt: str | None) -> list[str]: ...


class PiAdapter:
    def __init__(self, executable: str = "pi"):
        self.executable = executable

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def command(self, agent: Agent, prompt: str | None) -> list[str]:
        if not self.available():
            raise RuntimeError(f"pi executable not found: {self.executable}")
        if not agent.session_file:
            raise ValueError("managed pi agents require an exact session_file")
        if agent.session_id and not local_path(agent.session_file).is_file():
            raise RuntimeError(
                "saved pi session is missing; refusing to silently start a new session"
            )
        skill = agent_path(SKILL)
        args = [
            self.executable,
            "--session",
            agent_path(agent.session_file),
            "--skill",
            skill,
            "--append-system-prompt",
            f"You are a Swarm-managed agent. Read {skill} before working. "
            "Use its file-based reporting protocol to maintain your goal, activities, "
            "checkpoint and status at every transition, including before ending a turn. "
            "SWARM_AGENT_ID and SWARM_HOME identify your existing record. "
            "Do not use /new, /fork, /clone or switch sessions; retain this session.",
        ]
        if agent.model:
            args.extend(["--model", agent.model])
        if prompt:
            args.extend(["--", prompt])
        return args


def session_header(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with local_path(path).open(encoding="utf-8") as stream:
            header = json.loads(stream.readline(65536))
        if header.get("type") == "session":
            return str(header["id"])
    except (OSError, ValueError, KeyError):
        pass
    return None


def resume_prompt(agent: Agent) -> str:
    return (
        "Resume where you were. Swarm restored this session after an interruption. "
        "Read your existing Swarm record and session history first. Verify the repository "
        "and any external side effects before repeating work; do not assume the interrupted "
        "operation failed. Continue the current activity toward the existing goal. "
        "If blocked, report waiting or dormant rather than inventing progress.\n\n"
        f"Goal: {agent.goal}\nCurrent activity: {agent.activity or '(see session history)'}\n"
        f"Checkpoint:\n{agent.notes or '(see session history)'}"
    )
